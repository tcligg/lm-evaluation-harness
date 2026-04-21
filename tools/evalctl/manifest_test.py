"""Tests for manifest.build_manifest."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from proto.eval.v1 import config_pb2, manifest_pb2
from tools.evalctl.manifest import ENV_ALLOW_LIST, build_manifest


def _minimal_config() -> config_pb2.EvalConfig:
    cfg = config_pb2.EvalConfig(schema_version=1)
    cfg.run.name = "smoke"
    cfg.run.tags["team"] = "openmaas"
    cfg.harness.version = "0.4.2"
    cfg.endpoint.type = config_pb2.ENDPOINT_TYPE_VERTEX_CHAT
    cfg.endpoint.model_id = "google/foo"
    cfg.endpoint.endpoint_id = "projects/x/endpoints/y"
    t = cfg.tasks.add()
    t.name = "gpqa_diamond_generative_n_shot"
    return cfg


class ManifestTest(unittest.TestCase):
    def test_run_id_is_uuid(self) -> None:
        m = build_manifest(_minimal_config(), config_sha256="a" * 64,
                           execution_mode=manifest_pb2.EXECUTION_MODE_LOCAL)
        # uuid4 hex form is 36 chars with hyphens.
        self.assertEqual(len(m.run_id), 36)
        self.assertEqual(m.run_id.count("-"), 4)

    def test_status_initialized_to_running(self) -> None:
        m = build_manifest(_minimal_config(), config_sha256="x",
                           execution_mode=manifest_pb2.EXECUTION_MODE_LOCAL)
        self.assertEqual(m.status, manifest_pb2.RUN_STATUS_RUNNING)

    def test_tags_propagated(self) -> None:
        m = build_manifest(_minimal_config(), config_sha256="x",
                           execution_mode=manifest_pb2.EXECUTION_MODE_LOCAL)
        self.assertEqual(dict(m.tags), {"team": "openmaas"})

    def test_endpoint_ref_human_form(self) -> None:
        m = build_manifest(_minimal_config(), config_sha256="x",
                           execution_mode=manifest_pb2.EXECUTION_MODE_LOCAL)
        self.assertEqual(m.endpoint.type, "vertex_chat")
        self.assertEqual(m.endpoint.model_id, "google/foo")

    def test_env_allow_list_filters_secrets(self) -> None:
        fake_env = {
            "USER": "tcli",
            "EVALCTL_VERTEX_PROJECT": "my-proj",
            "OPENAI_API_KEY": "sk-secret-should-be-dropped",
            "AWS_SECRET_ACCESS_KEY": "should-be-dropped",
        }
        with mock.patch.dict(os.environ, fake_env, clear=True):
            m = build_manifest(_minimal_config(), config_sha256="x",
                               execution_mode=manifest_pb2.EXECUTION_MODE_LOCAL)
        captured = dict(m.env_allow_listed)
        self.assertIn("USER", captured)
        self.assertIn("EVALCTL_VERTEX_PROJECT", captured)
        self.assertNotIn("OPENAI_API_KEY", captured)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", captured)
        # Defensive cross-check: nothing leaked outside the allow list.
        self.assertTrue(set(captured).issubset(ENV_ALLOW_LIST))

    def test_execution_mode_recorded(self) -> None:
        m = build_manifest(_minimal_config(), config_sha256="x",
                           execution_mode=manifest_pb2.EXECUTION_MODE_REMOTE,
                           image="img:tag", image_digest="sha256:deadbeef")
        self.assertEqual(m.execution_mode, manifest_pb2.EXECUTION_MODE_REMOTE)
        self.assertEqual(m.container.image, "img:tag")
        self.assertEqual(m.container.digest, "sha256:deadbeef")


if __name__ == "__main__":
    unittest.main()
