"""Execution dispatch.

Phase 0 supports only the host-Python local path: shells out to `lm_eval`
in the current Python environment. Docker (Phase 2) and Vertex Custom Job
(Phase 5) dispatchers will plug in here behind the same `dispatch()` API.

The argv-construction logic lives in `tools.evalctl._pure.build_lm_eval_argv`
so it's testable without protoc. This module just bridges the proto types
to that pure helper and handles I/O (writing manifest, invoking subprocess).
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from google.protobuf import json_format

from proto.merit.v1 import config_pb2, manifest_pb2

from tools.evalctl._pure import build_lm_eval_argv


def dispatch_local_host(
    cfg: config_pb2.EvalConfig,
    manifest: manifest_pb2.RunManifest,
    *,
    workdir: Path,
    dry_run: bool = False,
) -> int:
    """Run lm_eval in the current Python environment.

    Phase 0 implementation: writes manifest + resolved config to `workdir`,
    invokes `lm_eval` as a subprocess, and returns its exit code. No GCS
    upload, no BQ insert (those land in Phase 3).
    """
    workdir.mkdir(parents=True, exist_ok=True)
    _write_manifest(manifest, workdir / "manifest.json")
    _write_resolved_config(cfg, workdir / "config.resolved.yaml")

    argv = build_lm_eval_argv(_proto_to_dict(cfg), output_path=str(workdir))

    if dry_run:
        print("DRY RUN — would execute:")
        print("  " + " ".join(shlex.quote(a) for a in argv))
        return 0

    print(f"Executing lm_eval (run_id={manifest.run_id}) ...")
    print("  " + " ".join(shlex.quote(a) for a in argv))
    log_path = workdir / "runner.log"
    with log_path.open("w") as log:
        proc = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT)
    return proc.returncode


# --- internals ----------------------------------------------------------------


def _proto_to_dict(cfg: config_pb2.EvalConfig) -> dict:
    """Project EvalConfig into the plain-dict form `_pure.build_lm_eval_argv`
    expects. Uses `MessageToDict` so enums are stringified (e.g. "vertex_chat"
    after the type_name lower-casing below).
    """
    d = json_format.MessageToDict(
        cfg,
        preserving_proto_field_name=True,
        use_integers_for_enums=False,
    )
    # MessageToDict emits enum strings as their UPPER_SNAKE proto name; the
    # _pure helper expects the user-facing lowercase form.
    if "endpoint" in d and "type" in d["endpoint"]:
        d["endpoint"]["type"] = (
            d["endpoint"]["type"].removeprefix("ENDPOINT_TYPE_").lower()
        )
    return d


def _write_manifest(manifest: manifest_pb2.RunManifest, path: Path) -> None:
    js = json_format.MessageToJson(manifest, preserving_proto_field_name=True, indent=2)
    path.write_text(js + "\n")


def _write_resolved_config(cfg: config_pb2.EvalConfig, path: Path) -> None:
    # Resolved form is the proto's JSON projection. JSON is valid YAML;
    # the .yaml extension keeps file naming consistent with the source.
    js = json_format.MessageToJson(cfg, preserving_proto_field_name=True, indent=2)
    path.write_text(js + "\n")
