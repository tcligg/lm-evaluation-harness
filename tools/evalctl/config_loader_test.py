"""Tests for config_loader.

Covers:
- Happy path on the example config.
- Enum normalization (vertex_chat -> ENDPOINT_TYPE_VERTEX_CHAT).
- Unknown-field rejection.
- Required-field semantic validation.
"""

from __future__ import annotations

import textwrap
import unittest
from pathlib import Path

from tools.evalctl.config_loader import ConfigError, load_config


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "examples" / "configs" / "gpqa_diamond_3shot_grok42.yaml"


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(textwrap.dedent(body).lstrip())
    return p


class ConfigLoaderTest(unittest.TestCase):
    def test_example_config_loads(self) -> None:
        cfg, sha = load_config(EXAMPLE)
        self.assertEqual(cfg.run.name, "gpqa_diamond_3shot_grok42_non_reason")
        self.assertEqual(cfg.harness.version, "0.4.2")
        self.assertEqual(len(cfg.tasks), 1)
        self.assertEqual(cfg.tasks[0].num_fewshot, 3)
        self.assertEqual(len(sha), 64)  # SHA-256 hex

    def test_enum_normalization(self) -> None:
        cfg, _ = load_config(EXAMPLE)
        # vertex_chat string in YAML should resolve to enum int 1.
        self.assertEqual(cfg.endpoint.type, 1)

    def test_unknown_field_rejected(self) -> None:
        # Use a tmp file rather than monkey-patching the example.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _write(Path(d), """
                schema_version: 1
                run: { name: x }
                harness: { version: "0.4.2" }
                endpoint:
                  type: vertex_chat
                  model_id: foo
                  endpoint_id: bar
                tasks: [{ name: t }]
                bogus_field: 42
            """)
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_missing_endpoint_id_for_vertex(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _write(Path(d), """
                schema_version: 1
                run: { name: x }
                harness: { version: "0.4.2" }
                endpoint:
                  type: vertex_chat
                  model_id: foo
                tasks: [{ name: t }]
            """)
            with self.assertRaises(ConfigError) as ctx:
                load_config(p)
            self.assertIn("endpoint_id", str(ctx.exception))

    def test_schema_version_mismatch(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = _write(Path(d), """
                schema_version: 999
                run: { name: x }
                harness: { version: "0.4.2" }
                endpoint:
                  type: vertex_chat
                  model_id: foo
                  endpoint_id: bar
                tasks: [{ name: t }]
            """)
            with self.assertRaises(ConfigError) as ctx:
                load_config(p)
            self.assertIn("schema_version", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
