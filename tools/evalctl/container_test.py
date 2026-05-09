"""Tests for tools.evalctl.container (Phase 2).

Mocks subprocess.run so the suite runs without Docker installed.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tools.evalctl import container
from tools.evalctl.container import (
    ContainerError,
    _redact_argv,
    docker_available,
    resolve_image_digest,
    run_in_container,
)


class DockerAvailableTest(unittest.TestCase):
    def test_returns_true_when_docker_runs(self) -> None:
        with mock.patch.object(container.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            self.assertTrue(docker_available())

    def test_returns_false_when_docker_missing(self) -> None:
        with mock.patch.object(container.subprocess, "run",
                               side_effect=FileNotFoundError):
            self.assertFalse(docker_available())

    def test_returns_false_when_daemon_down(self) -> None:
        with mock.patch.object(container.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, ["docker"])):
            self.assertFalse(docker_available())


class ResolveImageDigestTest(unittest.TestCase):
    def test_returns_digest_string(self) -> None:
        fake = mock.Mock(stdout="sha256:abc123\n", returncode=0)
        with mock.patch.object(container.subprocess, "run", return_value=fake):
            self.assertEqual(resolve_image_digest("img:tag"), "sha256:abc123")

    def test_blank_when_image_missing(self) -> None:
        with mock.patch.object(container.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, [])):
            self.assertEqual(resolve_image_digest("img:tag"), "")


class RunInContainerTest(unittest.TestCase):
    def test_raises_when_docker_unavailable(self) -> None:
        with TemporaryDirectory() as d:
            cfg = Path(d) / "cfg.yaml"
            cfg.write_text("schema_version: 1\n")
            with mock.patch.object(container, "docker_available",
                                   return_value=False):
                with self.assertRaises(ContainerError):
                    run_in_container(cfg, workdir=Path(d) / "out")

    def test_constructs_expected_docker_argv(self) -> None:
        with TemporaryDirectory() as d:
            cfg = Path(d) / "cfg.yaml"
            cfg.write_text("schema_version: 1\n")
            captured: dict = {}

            def fake_run(argv, *args, **kwargs):
                captured["argv"] = argv
                return mock.Mock(returncode=0)

            with mock.patch.object(container, "docker_available", return_value=True), \
                 mock.patch.object(container, "resolve_image_digest",
                                   return_value="sha256:deadbeef"), \
                 mock.patch.object(container.subprocess, "run", side_effect=fake_run), \
                 mock.patch("builtins.print"):  # silence the diagnostic print
                rc = run_in_container(cfg, workdir=Path(d) / "out",
                                      image="my-img:tag",
                                      extra_evalctl_args=["--limit", "5"])
        self.assertEqual(rc, 0)
        argv = captured["argv"]
        self.assertEqual(argv[0:2], ["docker", "run"])
        # Image ref must appear before the entry-point args.
        self.assertIn("my-img:tag", argv)
        # Config + workdir mounts present.
        joined = " ".join(argv)
        self.assertIn(":/run/config.yaml:ro", joined)
        self.assertIn(":/run/out", joined)
        # Image digest propagated to the container env.
        self.assertIn("MERIT_IMAGE_REF=my-img:tag", argv)
        self.assertIn("MERIT_IMAGE_DIGEST=sha256:deadbeef", argv)
        # Extra evalctl args appended.
        self.assertIn("--limit", argv)
        self.assertIn("5", argv)


class RedactArgvTest(unittest.TestCase):
    """Critical security boundary: secret env var values must NEVER appear
    in stdout/stderr or CI logs. The diagnostic print() in
    run_in_container goes through _redact_argv first."""

    def test_hf_token_value_redacted(self) -> None:
        argv = ["docker", "run", "-e", "HF_TOKEN=hf_real_secret_value", "img:tag"]
        out = _redact_argv(argv)
        self.assertIn("HF_TOKEN=***REDACTED***", out)
        self.assertNotIn("hf_real_secret_value", " ".join(out))

    def test_openai_key_value_redacted(self) -> None:
        argv = ["docker", "run", "-e", "OPENAI_API_KEY=sk-leakable", "img"]
        out = _redact_argv(argv)
        self.assertNotIn("sk-leakable", " ".join(out))

    def test_non_secret_env_var_passes_through(self) -> None:
        argv = ["docker", "run", "-e", "MERIT_IMAGE_REF=img:tag", "img"]
        out = _redact_argv(argv)
        self.assertEqual(out, argv)

    def test_mixed_argv_only_secrets_masked(self) -> None:
        argv = [
            "docker", "run",
            "-e", "MERIT_IMAGE_REF=img:tag",
            "-e", "HF_TOKEN=hf_secret",
            "-v", "/host:/in:ro",
            "-e", "HUGGING_FACE_HUB_TOKEN=hf_other",
            "img:tag", "evalctl", "run",
        ]
        out = _redact_argv(argv)
        joined = " ".join(out)
        # Secrets gone:
        self.assertNotIn("hf_secret", joined)
        self.assertNotIn("hf_other", joined)
        # Non-secrets and structure preserved:
        self.assertIn("MERIT_IMAGE_REF=img:tag", out)
        self.assertIn("/host:/in:ro", out)
        self.assertEqual(out[-3:], ["img:tag", "evalctl", "run"])

    def test_value_with_equals_sign_handled(self) -> None:
        argv = ["docker", "run", "-e", "OPENAI_API_KEY=sk-a=b=c", "img"]
        out = _redact_argv(argv)
        self.assertNotIn("sk-a=b=c", " ".join(out))
        self.assertIn("OPENAI_API_KEY=***REDACTED***", out)


if __name__ == "__main__":
    unittest.main()
