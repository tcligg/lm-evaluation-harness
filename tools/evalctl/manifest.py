"""RunManifest builder.

Called once per run (at submit time) to produce the immutable provenance
record. Status is initialized to RUNNING; the runner patches it on completion.

Env-var capture is allow-list only — secrets MUST NOT leak here. Any var
not on the list is silently dropped.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from google.protobuf import timestamp_pb2

from proto.eval.v1 import config_pb2, manifest_pb2
from tools.evalctl._pure import ENV_ALLOW_LIST, filter_env  # noqa: F401 (re-exported)


def build_manifest(
    cfg: config_pb2.EvalConfig,
    *,
    config_sha256: str,
    execution_mode: manifest_pb2.ExecutionMode,
    image: str = "",
    image_digest: str = "",
    repo_root: Path | None = None,
) -> manifest_pb2.RunManifest:
    """Construct a RunManifest for a fresh run.

    `image` and `image_digest` are blank for host-Python (Phase 0) runs;
    they get populated once the docker dispatcher lands in Phase 2.
    """
    now = datetime.now(timezone.utc)
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(now)

    manifest = manifest_pb2.RunManifest(
        run_id=str(uuid.uuid4()),
        schema_version=1,
        name=cfg.run.name,
        user_id=os.getenv("USER") or os.getenv("LOGNAME") or "unknown",
        timestamp_utc=ts,
        execution_mode=execution_mode,
        config_sha256=config_sha256,
        status=manifest_pb2.RUN_STATUS_RUNNING,
    )

    manifest.git.CopyFrom(_collect_git_info(cfg.harness.version, repo_root))
    manifest.container.image = image
    manifest.container.digest = image_digest
    manifest.endpoint.CopyFrom(_endpoint_ref_from_config(cfg))

    for k, v in filter_env(os.environ).items():
        manifest.env_allow_listed[k] = v
    for k, v in cfg.run.tags.items():
        manifest.tags[k] = v

    return manifest


def _endpoint_ref_from_config(cfg: config_pb2.EvalConfig) -> manifest_pb2.EndpointRef:
    type_name = config_pb2.EndpointType.Name(cfg.endpoint.type)
    # Strip "ENDPOINT_TYPE_" prefix for human-friendly storage.
    short = type_name.removeprefix("ENDPOINT_TYPE_").lower()
    return manifest_pb2.EndpointRef(
        type=short,
        model_id=cfg.endpoint.model_id,
        endpoint_id=cfg.endpoint.endpoint_id,
    )


def _collect_git_info(harness_version: str, repo_root: Path | None) -> manifest_pb2.GitInfo:
    """Capture commit SHAs for both the wrapper repo and the vendored harness.

    Failures are non-fatal: missing git (e.g. inside a stripped container) just
    leaves the field blank. The manifest still uniquely identifies the run via
    `run_id` + `config_sha256` + `image_digest`.
    """
    info = manifest_pb2.GitInfo(harness_version=harness_version)
    cwd = repo_root or Path.cwd()

    info.wrapper_commit = _git(["rev-parse", "HEAD"], cwd) or ""
    info.wrapper_dirty = _git(["status", "--porcelain"], cwd) != ""
    info.harness_commit = info.wrapper_commit  # vendored in same repo
    return info


def _git(args: list[str], cwd: Path) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""
