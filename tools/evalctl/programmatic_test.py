"""Tests for tools.evalctl.programmatic.

Mocks `lm_eval.simple_evaluate` so the suite runs without network and
without actually loading the harness; we just verify the dispatcher
calls it with the right kwargs and merges multi-call results correctly.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


# Inject a stub `lm_eval` module so `from lm_eval import simple_evaluate`
# inside programmatic.py resolves. Each test patches `simple_evaluate`
# on this stub.
_stub = types.ModuleType("lm_eval")
_stub.simple_evaluate = lambda *a, **kw: None  # placeholder, patched per-test
sys.modules.setdefault("lm_eval", _stub)

from tools.evalctl.programmatic import (
    _build_model_args,
    _group_tasks_by_fewshot,
    _seed_kwargs_from_list,
    run_programmatic,
)


class HelpersTest(unittest.TestCase):
    def test_build_model_args_includes_only_set_fields(self) -> None:
        cfg = {
            "endpoint": {"model_id": "x", "base_url": "http://localhost"},
            "model_args": {"num_concurrent": 16, "timeout": 0},
        }
        out = _build_model_args(cfg)
        self.assertEqual(out["model"], "x")
        self.assertEqual(out["base_url"], "http://localhost")
        self.assertEqual(out["num_concurrent"], 16)
        # timeout=0 should be dropped (treated as unset)
        self.assertNotIn("timeout", out)
        self.assertNotIn("max_gen_toks", out)

    def test_seed_kwargs_partial_list(self) -> None:
        out = _seed_kwargs_from_list([7, 8])
        self.assertEqual(out, {"random_seed": 7, "numpy_random_seed": 8})

    def test_seed_kwargs_full_list(self) -> None:
        out = _seed_kwargs_from_list([1, 2, 3, 4])
        self.assertEqual(out["random_seed"], 1)
        self.assertEqual(out["numpy_random_seed"], 2)
        self.assertEqual(out["torch_random_seed"], 3)
        self.assertEqual(out["fewshot_random_seed"], 4)

    def test_group_tasks_groups_distinct_fewshot(self) -> None:
        tasks = [
            {"name": "a", "num_fewshot": 3},
            {"name": "b", "num_fewshot": 0},
            {"name": "c", "num_fewshot": 3},
            {"name": "d"},  # None: use task default
        ]
        groups = _group_tasks_by_fewshot(tasks)
        self.assertEqual(set(groups[3]), {"a", "c"})
        self.assertEqual(groups[0], ["b"])
        self.assertEqual(groups[None], ["d"])


class RunProgrammaticTest(unittest.TestCase):
    def _cfg(self):
        return {
            "endpoint": {
                "type": "vertex_chat",
                "model_id": "google/x",
                "base_url": "https://x.googleapis.com",
            },
            "tasks": [
                {"name": "task_a", "num_fewshot": 3},
                {"name": "task_b", "num_fewshot": 0},
            ],
            "model_args": {"num_concurrent": 8},
            "gen_kwargs": {"until": "<|eos|>"},
            "seed": [0, 1234, 1234, 1234],
        }

    def _fake_simple_evaluate(self, captured_calls):
        """Returns a stand-in that records its kwargs and returns canned results."""

        def _fake(**kwargs):
            captured_calls.append(kwargs)
            tasks = kwargs.get("tasks") or []
            return {
                "results": {t: {"acc": 0.5} for t in tasks},
                "configs": {t: {"task": t, "num_fewshot": kwargs.get("num_fewshot")}
                            for t in tasks},
                "n-samples": {t: {"original": 100, "effective": 100} for t in tasks},
                "samples": {t: [{"doc_id": 0, "target": "x"}] for t in tasks},
                "config": {"model": kwargs.get("model")},
                "versions": {t: "1.0" for t in tasks},
                "higher_is_better": {t: {"acc": True} for t in tasks},
            }

        return _fake

    def test_one_call_per_distinct_fewshot(self) -> None:
        calls: list[dict] = []
        with mock.patch.object(sys.modules["lm_eval"], "simple_evaluate",
                               self._fake_simple_evaluate(calls)):
            with TemporaryDirectory() as d:
                results = run_programmatic(self._cfg(), workdir=Path(d))
        # Two distinct num_fewshot values -> two calls.
        self.assertEqual(len(calls), 2)
        # Aggregated results contain both tasks.
        self.assertEqual(set(results["results"].keys()), {"task_a", "task_b"})

    def test_writes_results_and_samples_files(self) -> None:
        calls: list[dict] = []
        with mock.patch.object(sys.modules["lm_eval"], "simple_evaluate",
                               self._fake_simple_evaluate(calls)):
            with TemporaryDirectory() as d:
                workdir = Path(d)
                run_programmatic(self._cfg(), workdir=workdir)
                files = sorted(p.name for p in workdir.iterdir())
        self.assertTrue(any(f.startswith("results_") and f.endswith(".json")
                            for f in files))
        self.assertTrue(any(f.startswith("samples_task_a") for f in files))
        self.assertTrue(any(f.startswith("samples_task_b") for f in files))

    def test_passes_chat_template_kwargs(self) -> None:
        calls: list[dict] = []
        with mock.patch.object(sys.modules["lm_eval"], "simple_evaluate",
                               self._fake_simple_evaluate(calls)):
            with TemporaryDirectory() as d:
                run_programmatic(self._cfg(), workdir=Path(d))
        # Vertex_chat -> apply_chat_template should be True (auto).
        for call in calls:
            self.assertTrue(call["apply_chat_template"])
            self.assertTrue(call["fewshot_as_multiturn"])

    def test_passes_seeds(self) -> None:
        calls: list[dict] = []
        with mock.patch.object(sys.modules["lm_eval"], "simple_evaluate",
                               self._fake_simple_evaluate(calls)):
            with TemporaryDirectory() as d:
                run_programmatic(self._cfg(), workdir=Path(d))
        for call in calls:
            self.assertEqual(call["random_seed"], 0)
            self.assertEqual(call["numpy_random_seed"], 1234)

    def test_limit_passed_through(self) -> None:
        calls: list[dict] = []
        with mock.patch.object(sys.modules["lm_eval"], "simple_evaluate",
                               self._fake_simple_evaluate(calls)):
            with TemporaryDirectory() as d:
                run_programmatic(self._cfg(), workdir=Path(d), limit=5)
        for call in calls:
            self.assertEqual(call["limit"], 5)

    def test_unknown_endpoint_rejected(self) -> None:
        cfg = self._cfg()
        cfg["endpoint"]["type"] = "klingon"
        with TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                run_programmatic(cfg, workdir=Path(d))


if __name__ == "__main__":
    unittest.main()
