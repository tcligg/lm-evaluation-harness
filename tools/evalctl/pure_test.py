"""Unit tests for tools.evalctl._pure (proto-free).

These tests deliberately avoid importing proto modules so they can run on
any host with stdlib + PyYAML. They cover:

- The env allow-list (security-critical: no secret env vars must leak).
- YAML loading + sha256 stability.
- Enum normalization (the friendly-string -> proto-name mapping).
- lm_eval argv translation against a representative config.

The proto-coupled call paths are exercised in config_loader_test.py and
manifest_test.py, which require protoc-generated _pb2 modules.
"""

from __future__ import annotations

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.evalctl._pure import (
    ENDPOINT_TO_ADAPTER,
    ENV_ALLOW_LIST,
    build_lm_eval_argv,
    filter_env,
    load_yaml_with_sha,
    normalize_enums,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "examples" / "configs" / "gpqa_diamond_3shot_grok42.yaml"


class EnvAllowListTest(unittest.TestCase):
    def test_known_safe_vars_pass(self) -> None:
        env = {"USER": "tcli", "EVALCTL_VERTEX_PROJECT": "my-proj"}
        self.assertEqual(filter_env(env), env)

    def test_secrets_are_dropped(self) -> None:
        env = {
            "USER": "tcli",
            "OPENAI_API_KEY": "sk-leak-me",
            "AWS_SECRET_ACCESS_KEY": "leak",
            "GCLOUD_TOKEN": "leak",
            "PATH": "/usr/bin",  # also not on the list
        }
        out = filter_env(env)
        self.assertEqual(out, {"USER": "tcli"})
        # Defensive: nothing leaked outside the explicit allow-list.
        self.assertTrue(set(out).issubset(ENV_ALLOW_LIST))

    def test_empty_env(self) -> None:
        self.assertEqual(filter_env({}), {})


class YamlLoadTest(unittest.TestCase):
    def test_example_loads_with_stable_sha(self) -> None:
        data, sha = load_yaml_with_sha(EXAMPLE)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(len(sha), 64)  # SHA-256 hex
        # Stability: re-load should produce the same digest.
        _, sha2 = load_yaml_with_sha(EXAMPLE)
        self.assertEqual(sha, sha2)

    def test_top_level_must_be_mapping(self) -> None:
        with TemporaryDirectory() as d:
            p = Path(d) / "list.yaml"
            p.write_text("- 1\n- 2\n")
            with self.assertRaises(ValueError):
                load_yaml_with_sha(p)


class EnumNormalizationTest(unittest.TestCase):
    def test_endpoint_type(self) -> None:
        d = {"endpoint": {"type": "vertex_chat", "model_id": "x"}}
        normalize_enums(d)
        self.assertEqual(d["endpoint"]["type"], "ENDPOINT_TYPE_VERTEX_CHAT")

    def test_auth_mode(self) -> None:
        d = {"endpoint": {"auth": {"mode": "gcloud_adc"}}}
        normalize_enums(d)
        self.assertEqual(d["endpoint"]["auth"]["mode"], "AUTH_MODE_GCLOUD_ADC")

    def test_unknown_string_left_alone(self) -> None:
        # Unknown enum strings pass through; proto3 will reject them later.
        d = {"endpoint": {"type": "klingon"}}
        normalize_enums(d)
        self.assertEqual(d["endpoint"]["type"], "klingon")

    def test_missing_path_is_safe(self) -> None:
        d = {"unrelated": True}
        normalize_enums(d)  # must not raise
        self.assertEqual(d, {"unrelated": True})

    def test_already_normalized_left_alone(self) -> None:
        d = {"endpoint": {"type": "ENDPOINT_TYPE_VERTEX_CHAT"}}
        normalize_enums(d)
        self.assertEqual(d["endpoint"]["type"], "ENDPOINT_TYPE_VERTEX_CHAT")


class LmEvalArgvTest(unittest.TestCase):
    """Verifies the translation matches the team's existing ad-hoc command
    documented in eval_rerun.log:

      lm_eval --model local-chat-completions \
        --model_args model=...,base_url=...,num_concurrent=32,...
        --tasks gpqa_diamond_generative_n_shot,...
        --gen_kwargs until=<|eos|>
        --num_fewshot 3
        --output_path <dir>
        --log_samples
    """

    def _cfg(self, **overrides):
        cfg = {
            "endpoint": {
                "type": "vertex_chat",
                "model_id": "google/openmaas-2.0-test",
                "base_url": "https://example.googleapis.com/v1/.../chat/completions",
            },
            "model_args": {
                "num_concurrent": 32,
                "max_gen_toks": 131072,
                "timeout": 1800,
            },
            "tasks": [{"name": "gpqa_diamond_generative_n_shot", "num_fewshot": 3}],
            "gen_kwargs": {"until": "<|eos|>"},
            "seed": [0, 1234, 1234, 1234],
            "limits": {"per_task": 0},
        }
        cfg.update(overrides)
        return cfg

    def test_baseline_invocation(self) -> None:
        argv = build_lm_eval_argv(self._cfg(), output_path="/tmp/run/abc")
        self.assertEqual(argv[0], "lm_eval")
        self.assertEqual(argv[argv.index("--model") + 1], "local-chat-completions")
        # model_args is one comma-joined string at the position after --model_args.
        margs = argv[argv.index("--model_args") + 1]
        self.assertIn("model=google/openmaas-2.0-test", margs)
        self.assertIn("base_url=https://example.googleapis.com", margs)
        self.assertIn("num_concurrent=32", margs)
        self.assertIn("max_gen_toks=131072", margs)
        self.assertIn("timeout=1800", margs)

    def test_tasks_comma_joined(self) -> None:
        argv = build_lm_eval_argv(
            self._cfg(tasks=[{"name": "a"}, {"name": "b"}]),
            output_path="/tmp",
        )
        self.assertEqual(argv[argv.index("--tasks") + 1], "a,b")

    def test_log_samples_always_set(self) -> None:
        argv = build_lm_eval_argv(self._cfg(), output_path="/tmp")
        self.assertIn("--log_samples", argv)

    def test_include_path_custom_tasks(self) -> None:
        argv = build_lm_eval_argv(self._cfg(), output_path="/tmp")
        self.assertEqual(argv[argv.index("--include_path") + 1], "custom_tasks")

    def test_num_fewshot_from_first_task(self) -> None:
        argv = build_lm_eval_argv(self._cfg(), output_path="/tmp")
        self.assertEqual(argv[argv.index("--num_fewshot") + 1], "3")

    def test_num_fewshot_omitted_when_zero(self) -> None:
        argv = build_lm_eval_argv(
            self._cfg(tasks=[{"name": "t", "num_fewshot": 0}]),
            output_path="/tmp",
        )
        self.assertNotIn("--num_fewshot", argv)

    def test_seed_passed_through(self) -> None:
        argv = build_lm_eval_argv(self._cfg(), output_path="/tmp")
        self.assertEqual(argv[argv.index("--seed") + 1], "0,1234,1234,1234")

    def test_gen_kwargs_until_passed_through(self) -> None:
        argv = build_lm_eval_argv(self._cfg(), output_path="/tmp")
        self.assertEqual(argv[argv.index("--gen_kwargs") + 1], "until=<|eos|>")

    def test_limit_omitted_when_zero(self) -> None:
        argv = build_lm_eval_argv(self._cfg(), output_path="/tmp")
        self.assertNotIn("--limit", argv)

    def test_limit_emitted_when_set(self) -> None:
        argv = build_lm_eval_argv(
            self._cfg(limits={"per_task": 100}),
            output_path="/tmp",
        )
        self.assertEqual(argv[argv.index("--limit") + 1], "100")

    def test_unknown_endpoint_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            build_lm_eval_argv(
                self._cfg(endpoint={"type": "klingon", "model_id": "x"}),
                output_path="/tmp",
            )
        self.assertIn("klingon", str(ctx.exception))

    def test_no_tasks_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_lm_eval_argv(self._cfg(tasks=[]), output_path="/tmp")

    def test_local_vllm_uses_completions_adapter(self) -> None:
        cfg = self._cfg(endpoint={
            "type": "local_vllm",
            "model_id": "meta/llama",
            "base_url": "http://localhost:8000/v1",
        })
        argv = build_lm_eval_argv(cfg, output_path="/tmp")
        self.assertEqual(argv[argv.index("--model") + 1], "local-completions")

    def test_adapter_table_covers_all_endpoint_strings(self) -> None:
        # Sanity: every entry in ENUM_NORMALIZATIONS for endpoint.type
        # should resolve to an adapter, otherwise builds will fail at runtime.
        from tools.evalctl._pure import ENUM_NORMALIZATIONS
        endpoint_types = ENUM_NORMALIZATIONS[("endpoint", "type")].keys()
        for t in endpoint_types:
            self.assertIn(t, ENDPOINT_TO_ADAPTER, f"{t!r} missing from adapter table")


class IntegrationYamlToArgvTest(unittest.TestCase):
    """End-to-end: load the example YAML, normalize, build argv. Mirrors
    what `evalctl run --dry-run` does up to the proto step."""

    def test_example_yaml_to_argv(self) -> None:
        data, _ = load_yaml_with_sha(EXAMPLE)
        # The pure path doesn't normalize enums to proto names; it expects
        # the user-facing string. So skip normalize_enums and feed the dict
        # straight in.
        argv = build_lm_eval_argv(data, output_path="/tmp/run/example")
        self.assertEqual(argv[0], "lm_eval")
        self.assertIn("--log_samples", argv)
        self.assertIn("gpqa_diamond_generative_n_shot",
                      argv[argv.index("--tasks") + 1])


if __name__ == "__main__":
    unittest.main()
