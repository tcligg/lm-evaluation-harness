"""Tests for tools.evalctl.runs.

Exercises the local backend end-to-end against synthetic on-disk run
dirs. The GCS / BQ backends are stubs today; tests confirm they
gracefully return None when env vars / client libraries aren't present.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tools.evalctl.runs import (
    RunNotFound,
    find_run_by_id,
    list_local_runs,
    summarize,
)


def _write_run(root: Path, run_id: str, *, name: str = "test_run",
               status: str = "RUN_STATUS_SUCCESS",
               with_results: bool = True,
               with_samples: bool = True,
               with_log: bool = True) -> Path:
    """Create a fake run dir matching the runner's on-disk layout."""
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": run_id,
        "schema_version": 1,
        "name": name,
        "user_id": "tcli",
        "timestamp_utc": "2026-04-24T17:09:40.959221Z",
        "execution_mode": "EXECUTION_MODE_LOCAL",
        "status": status,
        "container": {"image": "local/merit:latest", "digest": "sha256:abc"},
        "endpoint": {"type": "vertex_chat", "model_id": "google/x"},
        "tags": {"team": "openmaas"},
    }))
    (run_dir / "config.resolved.yaml").write_text(json.dumps({
        "schema_version": 1, "run": {"name": name},
    }))
    if with_results:
        (run_dir / "results_2026-04-24T17-10-00.000.json").write_text(json.dumps({
            "results": {"task_a": {"acc": 0.5, "alias": "task_a"}},
            "n-samples": {"task_a": {"original": 10, "effective": 10}},
        }))
    if with_samples:
        (run_dir / "samples_task_a_2026-04-24T17-10-00.000.jsonl").write_text(
            '{"doc_id": 0}\n'
        )
    if with_log:
        (run_dir / "runner.log").write_text("ok\n")
    return run_dir


class FindRunByIdLocalTest(unittest.TestCase):
    def test_resolves_by_dir_name(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            _write_run(root, "11111111-2222-3333-4444-555555555555")
            ref = find_run_by_id(
                "11111111-2222-3333-4444-555555555555",
                roots=(root,),
            )
        self.assertEqual(ref.source, "local")
        self.assertEqual(ref.run_id, "11111111-2222-3333-4444-555555555555")
        self.assertIsNotNone(ref.manifest)
        self.assertEqual(ref.manifest["status"], "RUN_STATUS_SUCCESS")
        self.assertIsNotNone(ref.results)
        self.assertEqual(len(ref.sample_files), 1)
        self.assertIsNotNone(ref.log_path)

    def test_resolves_via_manifest_scan_when_dir_renamed(self) -> None:
        """If the user passed --workdir, the dir name may not match."""
        with TemporaryDirectory() as d:
            root = Path(d)
            run_id = "abcdef00-0000-0000-0000-000000000001"
            run_dir = root / "my_custom_workdir"  # NOT named after run_id
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text(
                json.dumps({"run_id": run_id, "name": "x",
                            "status": "RUN_STATUS_SUCCESS"})
            )
            ref = find_run_by_id(run_id, roots=(root,))
        self.assertEqual(ref.run_id, run_id)
        self.assertEqual(ref.workdir.name, "my_custom_workdir")

    def test_raises_run_not_found_on_miss(self) -> None:
        with TemporaryDirectory() as d:
            with self.assertRaises(RunNotFound) as ctx:
                find_run_by_id("does-not-exist", roots=(Path(d),))
            # Error message should hint where we looked.
            self.assertIn("local roots", str(ctx.exception))

    def test_partial_artifacts_still_returns_ref(self) -> None:
        """A still-running run won't have results_*.json yet; we should
        still return a usable RunRef so `evalctl show` can display the
        manifest and tell the user the run is in progress."""
        with TemporaryDirectory() as d:
            root = Path(d)
            run_id = "running-run-id"
            _write_run(root, run_id, status="RUN_STATUS_RUNNING",
                       with_results=False, with_samples=False, with_log=False)
            ref = find_run_by_id(run_id, roots=(root,))
        self.assertEqual(ref.manifest["status"], "RUN_STATUS_RUNNING")
        self.assertIsNone(ref.results)
        self.assertEqual(ref.sample_files, [])
        self.assertIsNone(ref.log_path)

    def test_picks_newest_results_file_when_multiple(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            run_id = "multi-results"
            run_dir = _write_run(root, run_id, with_results=True)
            # Add a second, newer results file with different content.
            newer = run_dir / "results_2026-04-25T00-00-00.000.json"
            newer.write_text(json.dumps(
                {"results": {"task_b": {"acc": 0.99}}}
            ))
            os.utime(newer, (newer.stat().st_atime + 100,
                             newer.stat().st_mtime + 100))
            ref = find_run_by_id(run_id, roots=(root,))
        # Newer file wins.
        self.assertIn("task_b", ref.results["results"])
        self.assertNotIn("task_a", ref.results["results"])


class FindRunByIdRemoteStubsTest(unittest.TestCase):
    def test_gcs_returns_none_when_env_unset(self) -> None:
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RunNotFound):
                find_run_by_id("missing", roots=(Path(d),))

    def test_bq_returns_none_when_env_unset(self) -> None:
        with TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RunNotFound):
                find_run_by_id("missing", roots=(Path(d),))

    def test_gcs_stub_with_env_but_no_client(self) -> None:
        """If MERIT_GCS_BUCKET is set but the google-cloud-storage
        client isn't installed, the backend silently returns None
        instead of crashing."""
        with TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"MERIT_GCS_BUCKET": "my-bucket"},
                             clear=True), \
             mock.patch.dict("sys.modules",
                             {"google.cloud.storage": None}):
            with self.assertRaises(RunNotFound):
                find_run_by_id("missing", roots=(Path(d),))


class ListLocalRunsTest(unittest.TestCase):
    def test_empty_roots(self) -> None:
        with TemporaryDirectory() as d:
            self.assertEqual(list_local_runs(roots=(Path(d),)), [])

    def test_returns_runs_newest_first(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            old = _write_run(root, "older")
            new = _write_run(root, "newer")
            # Force an ordering.
            os.utime(old, (1_000_000, 1_000_000))
            os.utime(new, (2_000_000, 2_000_000))
            refs = list_local_runs(roots=(root,))
        self.assertEqual([r.run_id for r in refs], ["newer", "older"])

    def test_limit_honored(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            for i in range(5):
                _write_run(root, f"run-{i}")
            refs = list_local_runs(roots=(root,), limit=2)
        self.assertEqual(len(refs), 2)

    def test_skips_dir_without_manifest(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "no_manifest_here").mkdir()
            _write_run(root, "good_run")
            refs = list_local_runs(roots=(root,))
        self.assertEqual([r.run_id for r in refs], ["good_run"])


class SummarizeTest(unittest.TestCase):
    def test_summary_is_one_line(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            _write_run(root, "01234567-89ab-cdef-0123-456789abcdef",
                       name="my_eval")
            ref = find_run_by_id("01234567-89ab-cdef-0123-456789abcdef",
                                 roots=(root,))
        line = summarize(ref)
        self.assertIn("01234567-89ab-cdef-0123-456789abcdef", line)
        self.assertIn("my_eval", line)
        self.assertIn("success", line)
        self.assertIn("tcli", line)
        # No literal newline.
        self.assertNotIn("\n", line)


if __name__ == "__main__":
    unittest.main()
