"""Tests for tools.evalctl.repro (Phase 1).

Covers:
  - reconstruct_config: results.json -> YAML projection.
  - diff_results: identical / drifted / tolerance-honoring comparison.
  - Endpoint type / id inference from various URL shapes.

Proto-free; runnable under `make smoke`.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.evalctl.repro import (
    _infer_endpoint_id,
    _infer_endpoint_type,
    _parse_kv_string,
    _seed_list_from_config,
    diff_results,
    reconstruct_config,
)


def _write_results(d: dict, dest: Path) -> Path:
    p = dest / "results_2026-01-01T00-00-00.000.json"
    p.write_text(json.dumps(d))
    return p


def _baseline_results() -> dict:
    return {
        "results": {
            "gpqa_diamond_generative_n_shot": {
                "alias": "gpqa_diamond_generative_n_shot",
                "exact_match,strict-match": 0.4242,
                "exact_match_stderr,strict-match": 0.035,
                "exact_match,flexible-extract": 0.4848,
                "exact_match_stderr,flexible-extract": 0.034,
            }
        },
        "configs": {
            "gpqa_diamond_generative_n_shot": {
                "task": "gpqa_diamond_generative_n_shot",
                "num_fewshot": 3,
                "metric_list": [
                    {"metric": "exact_match", "higher_is_better": True}
                ],
            }
        },
        "n-samples": {
            "gpqa_diamond_generative_n_shot": {"original": 198, "effective": 198}
        },
        "config": {
            "model": "local-chat-completions",
            "model_args": (
                "model=google/openmaas-2.0-test,"
                "base_url=https://us-central1-staging-aiplatform.sandbox.googleapis.com"
                "/v1/projects/108520237679/locations/us-central1/endpoints/grok-4p2/chat/completions,"
                "num_concurrent=32,max_gen_toks=131072,timeout=1800"
            ),
            "gen_kwargs": {"until": "<|eos|>"},
            "random_seed": 0,
            "numpy_random_seed": 1234,
            "torch_random_seed": 1234,
            "fewshot_random_seed": 1234,
            "apply_chat_template": True,
            "fewshot_as_multiturn": True,
            "limit": None,
            "model_source": "local-chat-completions",
        },
        "lm_eval_version": "0.4.12.dev0",
    }


class ReconstructConfigTest(unittest.TestCase):
    def test_emits_required_top_level_keys(self) -> None:
        with TemporaryDirectory() as d:
            p = _write_results(_baseline_results(), Path(d))
            yaml_str = reconstruct_config(p, run_name="repro_x")
        for needle in ("schema_version: 1", "run:", "harness:", "endpoint:",
                       "tasks:", "model_args:", "chat_template:"):
            self.assertIn(needle, yaml_str, f"missing {needle!r}")

    def test_endpoint_inference_for_vertex(self) -> None:
        with TemporaryDirectory() as d:
            p = _write_results(_baseline_results(), Path(d))
            yaml_str = reconstruct_config(p)
        self.assertIn("type: vertex_chat", yaml_str)
        self.assertIn("endpoint_id: \"projects/108520237679/locations/us-central1/endpoints/grok-4p2\"",
                      yaml_str)

    def test_per_task_num_fewshot_extracted(self) -> None:
        results = _baseline_results()
        results["configs"]["second_task"] = {
            "task": "second_task", "num_fewshot": 0,
            "metric_list": [{"metric": "acc"}],
        }
        results["results"]["second_task"] = {"acc": 0.5}
        with TemporaryDirectory() as d:
            p = _write_results(results, Path(d))
            yaml_str = reconstruct_config(p)
        # Both tasks emitted, sorted alphabetically.
        self.assertIn("- name: gpqa_diamond_generative_n_shot", yaml_str)
        self.assertIn("- name: second_task", yaml_str)
        self.assertIn("    num_fewshot: 3", yaml_str)
        self.assertIn("    num_fewshot: 0", yaml_str)

    def test_chat_template_block_emitted_for_chat_endpoint(self) -> None:
        with TemporaryDirectory() as d:
            p = _write_results(_baseline_results(), Path(d))
            yaml_str = reconstruct_config(p)
        self.assertIn("chat_template:", yaml_str)
        self.assertIn("mode: MODE_AUTO", yaml_str)

    def test_chat_template_named_when_string_provided(self) -> None:
        results = _baseline_results()
        results["config"]["apply_chat_template"] = "llama3"
        with TemporaryDirectory() as d:
            p = _write_results(results, Path(d))
            yaml_str = reconstruct_config(p)
        self.assertIn("mode: MODE_NAMED", yaml_str)
        self.assertIn("template_name: \"llama3\"", yaml_str)

    def test_seed_list_extracted(self) -> None:
        with TemporaryDirectory() as d:
            p = _write_results(_baseline_results(), Path(d))
            yaml_str = reconstruct_config(p)
        self.assertIn("seed: [0, 1234, 1234, 1234]", yaml_str)


class InferenceHelpersTest(unittest.TestCase):
    def test_endpoint_type_vertex_chat(self) -> None:
        self.assertEqual(
            _infer_endpoint_type("local-chat-completions",
                                 {"base_url": "https://x.aiplatform.googleapis.com/y"}),
            "vertex_chat",
        )

    def test_endpoint_type_local_chat(self) -> None:
        self.assertEqual(
            _infer_endpoint_type("local-chat-completions",
                                 {"base_url": "http://localhost:8000/v1"}),
            "local_chat",
        )

    def test_endpoint_type_local_vllm(self) -> None:
        self.assertEqual(_infer_endpoint_type("local-completions", {}),
                         "local_vllm")

    def test_endpoint_type_hf(self) -> None:
        self.assertEqual(_infer_endpoint_type("hf", {}), "hf")
        self.assertEqual(_infer_endpoint_type("huggingface", {}), "hf")

    def test_endpoint_id_extraction(self) -> None:
        url = ("https://us-central1-staging-aiplatform.sandbox.googleapis.com"
               "/v1/projects/108520237679/locations/us-central1/endpoints/grok-4p2/chat/completions")
        self.assertEqual(
            _infer_endpoint_id(url),
            "projects/108520237679/locations/us-central1/endpoints/grok-4p2",
        )

    def test_endpoint_id_blank_when_no_endpoints_segment(self) -> None:
        self.assertEqual(_infer_endpoint_id("http://localhost:8000/v1"), "")

    def test_parse_kv_string(self) -> None:
        out = _parse_kv_string("a=1,b=hello,c=true")
        self.assertEqual(out, {"a": "1", "b": "hello", "c": "true"})

    def test_seed_list_returns_empty_when_all_unset(self) -> None:
        self.assertEqual(_seed_list_from_config({}), [])


class DiffResultsTest(unittest.TestCase):
    def test_identical_files_match(self) -> None:
        with TemporaryDirectory() as d:
            p = _write_results(_baseline_results(), Path(d))
            report, drifted = diff_results(p, p)
        self.assertFalse(drifted)
        self.assertIn("ok", report)

    def test_score_drift_detected(self) -> None:
        a = _baseline_results()
        b = _baseline_results()
        b["results"]["gpqa_diamond_generative_n_shot"]["exact_match,strict-match"] = 0.5
        with TemporaryDirectory() as d:
            ap = _write_results(a, Path(d))
            bp = Path(d) / "b.json"
            bp.write_text(json.dumps(b))
            report, drifted = diff_results(ap, bp)
        self.assertTrue(drifted)
        self.assertIn("DRIFT", report)
        self.assertIn("exact_match,strict-match", report)

    def test_score_tolerance_suppresses_small_drift(self) -> None:
        a = _baseline_results()
        b = _baseline_results()
        b["results"]["gpqa_diamond_generative_n_shot"]["exact_match,strict-match"] = 0.43
        with TemporaryDirectory() as d:
            ap = _write_results(a, Path(d))
            bp = Path(d) / "b.json"
            bp.write_text(json.dumps(b))
            _, drifted_strict = diff_results(ap, bp, score_tol=0.0)
            _, drifted_loose = diff_results(ap, bp, score_tol=0.05)
        self.assertTrue(drifted_strict)
        self.assertFalse(drifted_loose)

    def test_n_samples_drift_detected(self) -> None:
        a = _baseline_results()
        b = _baseline_results()
        b["n-samples"]["gpqa_diamond_generative_n_shot"]["effective"] = 100
        with TemporaryDirectory() as d:
            ap = _write_results(a, Path(d))
            bp = Path(d) / "b.json"
            bp.write_text(json.dumps(b))
            _, drifted = diff_results(ap, bp)
            _, with_tol = diff_results(ap, bp, n_samples_tol=200)
        self.assertTrue(drifted)
        self.assertFalse(with_tol)

    def test_task_added_or_removed_flagged(self) -> None:
        a = _baseline_results()
        b = _baseline_results()
        b["results"]["new_task"] = {"acc": 0.5}
        del b["results"]["gpqa_diamond_generative_n_shot"]
        with TemporaryDirectory() as d:
            ap = _write_results(a, Path(d))
            bp = Path(d) / "b.json"
            bp.write_text(json.dumps(b))
            report, drifted = diff_results(ap, bp)
        self.assertTrue(drifted)
        self.assertIn("TASK NEW in B", report)
        self.assertIn("TASK MISSING in B", report)

    def test_config_drift_flagged(self) -> None:
        a = _baseline_results()
        b = _baseline_results()
        b["config"]["fewshot_as_multiturn"] = False
        with TemporaryDirectory() as d:
            ap = _write_results(a, Path(d))
            bp = Path(d) / "b.json"
            bp.write_text(json.dumps(b))
            report, drifted = diff_results(ap, bp)
        self.assertTrue(drifted)
        self.assertIn("config.fewshot_as_multiturn", report)


if __name__ == "__main__":
    unittest.main()
