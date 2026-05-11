"""Tests for the CLI subcommands that have non-trivial dispatch logic.

Today: just `evalctl config <run_id>` because it has 3 resolution
paths (config.resolved.yaml; reconstruct from results; neither
present). The other subcommands (`run`, `validate`, `show`,
`compare`, `ls`, `repro`, `diff`) are exercised by their backing
modules' unit tests (pure_test, repro_test, runs_test, etc.) plus
the smoke driver's end-to-end paths.

Uses typer.testing.CliRunner so the tests don't fork subprocesses.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from typer.testing import CliRunner

from tools.evalctl.cli import app


def _write_run(root: Path, run_id: str, *, with_resolved: bool = True,
               with_results: bool = True) -> Path:
    """Create a fixture run dir matching what the runner produces."""
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": run_id, "name": "test", "user_id": "tcli",
        "status": "RUN_STATUS_SUCCESS",
        "execution_mode": "EXECUTION_MODE_LOCAL",
        "timestamp_utc": "2026-05-09T00:00:00Z",
    }))
    if with_resolved:
        (run_dir / "config.resolved.yaml").write_text(json.dumps({
            "schema_version": 1,
            "run": {"name": "test"},
            "harness": {"version": "0.4.12"},
            "endpoint": {
                "type": "ENDPOINT_TYPE_VERTEX_CHAT",
                "model_id": "google/foo",
                "endpoint_id": "projects/p/locations/r/endpoints/e",
            },
            "tasks": [{"name": "task_a", "num_fewshot": 3}],
        }))
    if with_results:
        (run_dir / "results_2026-05-09T00-00-00.json").write_text(json.dumps({
            "results": {"task_a": {"acc": 0.5}},
            "configs": {"task_a": {"task": "task_a", "num_fewshot": 3}},
            "n-samples": {"task_a": {"original": 10, "effective": 10}},
            "config": {
                "model": "local-chat-completions",
                "model_args": (
                    "model=google/foo,"
                    "base_url=https://x.aiplatform.googleapis.com/v1/"
                    "projects/p/locations/r/endpoints/e/chat/completions"
                ),
                "model_source": "local-chat-completions",
            },
        }))
    return run_dir


class EvalctlConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_emits_resolved_yaml_when_present(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            _write_run(root, "uuid-resolved", with_resolved=True,
                       with_results=False)
            with mock.patch("tools.evalctl.runs.DEFAULT_RUN_ROOTS", (root,)):
                result = self.runner.invoke(app, ["config", "uuid-resolved"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("runner-saved", result.output)
        self.assertIn("ENDPOINT_TYPE_VERTEX_CHAT", result.output)

    def test_falls_back_to_repro_when_no_resolved(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            _write_run(root, "uuid-no-resolved", with_resolved=False,
                       with_results=True)
            with mock.patch("tools.evalctl.runs.DEFAULT_RUN_ROOTS", (root,)):
                result = self.runner.invoke(app, ["config", "uuid-no-resolved"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("reconstructed from", result.output)
        self.assertIn("no config.resolved.yaml on disk", result.output)
        # Reconstructed YAML uses friendly enum strings, not proto names.
        self.assertIn("type: vertex_chat", result.output)

    def test_force_repro_ignores_resolved_yaml(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            _write_run(root, "uuid-both", with_resolved=True,
                       with_results=True)
            with mock.patch("tools.evalctl.runs.DEFAULT_RUN_ROOTS", (root,)):
                result = self.runner.invoke(
                    app, ["config", "uuid-both", "--force-repro"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--force-repro requested", result.output)
        self.assertNotIn("runner-saved", result.output)

    def test_writes_to_out_path_when_specified(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            out = root / "extracted.yaml"
            _write_run(root, "uuid-out", with_resolved=True,
                       with_results=False)
            with mock.patch("tools.evalctl.runs.DEFAULT_RUN_ROOTS", (root,)):
                result = self.runner.invoke(
                    app, ["config", "uuid-out", "--out", str(out)])
            # Assert inside the `with` block so the tempdir still exists.
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(out.exists(), f"out missing; output: {result.output}")
            self.assertIn("ENDPOINT_TYPE_VERTEX_CHAT", out.read_text())

    def test_exit_4_when_run_id_unknown(self) -> None:
        with TemporaryDirectory() as d:
            with mock.patch("tools.evalctl.runs.DEFAULT_RUN_ROOTS", (Path(d),)):
                result = self.runner.invoke(app, ["config", "missing-id"])
        self.assertEqual(result.exit_code, 4)
        self.assertIn("No run found", result.output)

    def test_exit_5_when_run_has_no_config_no_results(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            _write_run(root, "uuid-skel", with_resolved=False,
                       with_results=False)
            with mock.patch("tools.evalctl.runs.DEFAULT_RUN_ROOTS", (root,)):
                result = self.runner.invoke(app, ["config", "uuid-skel"])
        self.assertEqual(result.exit_code, 5)
        self.assertIn("Cannot extract a config", result.output)


class EvalctlShowTest(unittest.TestCase):
    """Just spot-check that `show` mentions the config file path now."""

    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_show_prints_config_path_when_present(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            _write_run(root, "uuid-show", with_resolved=True,
                       with_results=True)
            with mock.patch("tools.evalctl.runs.DEFAULT_RUN_ROOTS", (root,)):
                result = self.runner.invoke(app, ["show", "uuid-show"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("config:", result.output)
        self.assertIn("config.resolved.yaml", result.output)
        self.assertIn("evalctl config uuid-show", result.output)

    def test_show_hints_at_extraction_when_no_resolved(self) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            _write_run(root, "uuid-show-noresolved", with_resolved=False,
                       with_results=True)
            with mock.patch("tools.evalctl.runs.DEFAULT_RUN_ROOTS", (root,)):
                result = self.runner.invoke(
                    app, ["show", "uuid-show-noresolved"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("reconstruct with", result.output)
        self.assertIn("evalctl config uuid-show-noresolved", result.output)


if __name__ == "__main__":
    unittest.main()
