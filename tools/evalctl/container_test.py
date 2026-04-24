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
        self.assertIn("EVALCTL_IMAGE_REF=my-img:tag", argv)
        self.assertIn("EVALCTL_IMAGE_DIGEST=sha256:deadbeef", argv)
        # Extra evalctl args appended.
        self.assertIn("--limit", argv)
        self.assertIn("5", argv)


if __name__ == "__main__":
    unittest.main()
