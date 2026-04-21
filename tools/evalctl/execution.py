"""Execution dispatch.

Phase 0 supports only the host-Python local path: shells out to `lm_eval`
in the current Python environment. Docker (Phase 2) and Vertex Custom Job
(Phase 5) dispatchers will plug in here behind the same `dispatch()` API.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from google.protobuf import json_format

from proto.eval.v1 import config_pb2, manifest_pb2


# Maps proto EndpointType -> lm_eval --model adapter name.
_ENDPOINT_TO_ADAPTER: dict[int, str] = {
    config_pb2.ENDPOINT_TYPE_VERTEX_CHAT: "local-chat-completions",
    config_pb2.ENDPOINT_TYPE_LOCAL_CHAT: "local-chat-completions",
    config_pb2.ENDPOINT_TYPE_LOCAL_VLLM: "local-completions",
    config_pb2.ENDPOINT_TYPE_HF: "hf",
}


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

    argv = _build_lm_eval_argv(cfg, output_path=workdir)

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


def _build_lm_eval_argv(cfg: config_pb2.EvalConfig, *, output_path: Path) -> list[str]:
    """Translate EvalConfig into a `lm_eval` CLI invocation.

    Mirrors the team's existing ad-hoc command (see eval_rerun.log) so
    Phase 1 reproducibility validation has a clean A/B target.
    """
    adapter = _ENDPOINT_TO_ADAPTER.get(cfg.endpoint.type)
    if adapter is None:
        raise ValueError(f"unsupported endpoint type: {cfg.endpoint.type}")

    model_args_pairs = [f"model={cfg.endpoint.model_id}"]
    if cfg.endpoint.base_url:
        model_args_pairs.append(f"base_url={cfg.endpoint.base_url}")
    if cfg.model_args.num_concurrent:
        model_args_pairs.append(f"num_concurrent={cfg.model_args.num_concurrent}")
    if cfg.model_args.max_gen_toks:
        model_args_pairs.append(f"max_gen_toks={cfg.model_args.max_gen_toks}")
    if cfg.model_args.timeout:
        model_args_pairs.append(f"timeout={cfg.model_args.timeout}")

    argv = [
        "lm_eval",
        "--model", adapter,
        "--model_args", ",".join(model_args_pairs),
        "--tasks", ",".join(t.name for t in cfg.tasks),
        "--output_path", str(output_path),
        "--log_samples",
        "--include_path", "custom_tasks",
    ]

    # Per-task num_fewshot is awkward in lm_eval's CLI (it takes one global
    # --num_fewshot). Phase 0 honors the value of the first task; Phase 1
    # will switch to programmatic invocation via lm_eval.simple_evaluate.
    if cfg.tasks and cfg.tasks[0].num_fewshot:
        argv += ["--num_fewshot", str(cfg.tasks[0].num_fewshot)]

    if cfg.seed:
        argv += ["--seed", ",".join(str(s) for s in cfg.seed)]

    if cfg.gen_kwargs and len(cfg.gen_kwargs.fields):
        kv = ",".join(
            f"{k}={_struct_value_to_str(v)}" for k, v in cfg.gen_kwargs.fields.items()
        )
        argv += ["--gen_kwargs", kv]

    if cfg.limits.per_task:
        argv += ["--limit", str(cfg.limits.per_task)]

    return argv


def _struct_value_to_str(v) -> str:
    """Render a protobuf Value as its plain CLI string form."""
    kind = v.WhichOneof("kind")
    if kind == "string_value":
        return v.string_value
    if kind == "number_value":
        n = v.number_value
        return str(int(n)) if n.is_integer() else str(n)
    if kind == "bool_value":
        return "true" if v.bool_value else "false"
    raise ValueError(f"unsupported gen_kwarg value kind: {kind}")


def _write_manifest(manifest: manifest_pb2.RunManifest, path: Path) -> None:
    js = json_format.MessageToJson(manifest, preserving_proto_field_name=True, indent=2)
    path.write_text(js + "\n")


def _write_resolved_config(cfg: config_pb2.EvalConfig, path: Path) -> None:
    # Resolved form is the proto's JSON projection. We persist as JSON-in-YAML
    # so the file extension matches the source format and stays jq/yq-friendly.
    js = json_format.MessageToJson(cfg, preserving_proto_field_name=True, indent=2)
    # JSON is valid YAML; no transformation needed.
    path.write_text(js + "\n")
