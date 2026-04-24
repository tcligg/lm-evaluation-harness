"""Docker container dispatch (Phase 2).

Runs `evalctl` inside the runtime image. Same config + manifest layout
as the host path; the difference is that the harness + wrapper sources
all come from the pinned image, giving us the reproducibility guarantee
the HLD calls for (R1).

Used by `evalctl run --local --container`. The remote Vertex Custom Job
dispatcher (Phase 5) will reuse the same image but submit it via the
Vertex API instead of `docker run`.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Mapping


DEFAULT_IMAGE = os.getenv("EVALCTL_IMAGE_REF") or "local/eval-harness:latest"


class ContainerError(RuntimeError):
    """Raised when the container can't be dispatched (missing docker, etc.)."""


def docker_available() -> bool:
    """True if `docker` is on PATH and the daemon answers."""
    try:
        subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def resolve_image_digest(image: str) -> str:
    """Look up the sha256 digest of an image. Empty if not pulled locally."""
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def run_in_container(
    config_path: Path,
    *,
    workdir: Path,
    image: str = DEFAULT_IMAGE,
    extra_evalctl_args: list[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Mount the config + workdir into the image and exec `evalctl run`.

    Returns the container's exit code. Caller is responsible for setting
    up `workdir` and inspecting artifacts after the run.
    """
    if not docker_available():
        raise ContainerError(
            "docker not available on this host. Install Docker, or use "
            "`evalctl run --local --no-container` for the host-Python path."
        )

    config_path = config_path.resolve()
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    # Make the workdir traversable + writable from inside the container.
    # The image runs as UID 1000 in production; locally we run as root,
    # but the bind mount inherits host perms which may be 0750.
    workdir.chmod(0o777)

    digest = resolve_image_digest(image)
    if not digest:
        # Image isn't local; let docker run pull it. If pull fails the
        # subprocess call will surface a clear error.
        pass

    extra = extra_evalctl_args or []

    docker_argv = [
        "docker", "run", "--rm",
        # Mount the user's gcloud config so ADC works inside the
        # container without baking creds into the image.
        "-v", f"{Path.home()}/.config/gcloud:/home/evalctl/.config/gcloud:ro",
        # Mount the config and the run scratch dir.
        "-v", f"{config_path}:/run/config.yaml:ro",
        "-v", f"{workdir}:/run/out",
        "-e", f"EVALCTL_IMAGE_REF={image}",
        "-e", f"EVALCTL_IMAGE_DIGEST={digest}",
    ]
    for k, v in (env or {}).items():
        docker_argv += ["-e", f"{k}={v}"]
    docker_argv += [
        image,
        "run", "/run/config.yaml",
        "--local", "--workdir", "/run/out",
        *extra,
    ]

    print("Container dispatch:")
    print("  " + " ".join(shlex.quote(a) for a in docker_argv))
    proc = subprocess.run(docker_argv)
    return proc.returncode
