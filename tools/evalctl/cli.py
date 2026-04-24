"""evalctl CLI surface.

Subcommands:
  run       Submit an eval run (local in Phase 0; remote arrives in Phase 5).
  validate  Schema/semantic check on a config without executing.
  show      Inspect a previously stored run (stub until BQ sink lands in Phase 3).
  compare   Diff two runs (stub until BQ sink lands).

Conventions:
  - Exit codes: 0 success, 1 user error (bad config), 2 runtime failure.
  - All stdout messages are user-facing; runner internals log to runner.log.

Proto availability:
  The CLI gracefully degrades when the generated protobuf modules
  (proto/eval/v1/_pb2.py) aren't on the import path. The pure-Python
  fallback uses tools.evalctl._pure for YAML loading and lm_eval argv
  translation. Fallback mode loses proto3 strict field-name validation
  but keeps the same on-the-wire behavior. CI (Phase 2) always runs
  with proto codegen enabled.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import typer

from tools.evalctl._pure import (
    build_lm_eval_argv,
    filter_env,
    load_yaml_with_sha,
    normalize_enums,
)


# ---------------------------------------------------------------------------
# Proto detection. We import lazily so the CLI starts even when protobuf
# isn't installed. Both _PROTO_OK and the imported names are inspected by
# the run/validate paths below.
# ---------------------------------------------------------------------------

_PROTO_OK = True
try:
    from google.protobuf import json_format  # noqa: F401
    from proto.eval.v1 import config_pb2, manifest_pb2  # noqa: F401

    from tools.evalctl.config_loader import ConfigError, load_config
    from tools.evalctl.execution import dispatch_local_host
    from tools.evalctl.manifest import build_manifest
except ImportError as _proto_err:
    _PROTO_OK = False
    _PROTO_ERR_MSG = str(_proto_err)


app = typer.Typer(
    add_completion=False,
    help="Eval runner CLI. See docs/eval-system-hld.md for the design.",
    no_args_is_help=True,
)


@app.command()
def run(
    config: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                  help="Path to config.yaml"),
    local: bool = typer.Option(False, "--local",
                               help="Execute locally (Phase 0+: host-Python or container)."),
    no_publish: bool = typer.Option(False, "--no-publish",
                                    help="Skip GCS/BQ publish (no-op until Phase 3)."),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Validate + print the lm_eval command without executing."),
    workdir: Path | None = typer.Option(None, "--workdir",
                                        help="Override scratch dir (default: /tmp/run/<run_id>/)."),
    programmatic: bool = typer.Option(
        True, "--programmatic/--subprocess",
        help="Phase 1: invoke lm_eval.simple_evaluate in-process (default). "
             "--subprocess falls back to Phase 0's CLI shell-out."),
    limit: int | None = typer.Option(
        None, "--limit",
        help="Cap docs per task. Phase 1 smoke-test convenience."),
    container: bool = typer.Option(
        False, "--container",
        help="Phase 2: run inside the pinned eval-harness Docker image."),
    image: str | None = typer.Option(
        None, "--image",
        help="Override container image ref. Default: $EVALCTL_IMAGE_REF "
             "or local/eval-harness:latest."),
) -> None:
    """Submit an eval run."""
    if not local and not dry_run:
        typer.secho(
            "Phase 0/1/2 support --local only. Remote dispatch lands in Phase 5.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    if container:
        _run_in_container(config, workdir=workdir, image=image,
                          dry_run=dry_run, programmatic=programmatic,
                          limit=limit, no_publish=no_publish)
        return

    if _PROTO_OK:
        _run_with_proto(config, no_publish=no_publish, dry_run=dry_run,
                        workdir=workdir, programmatic=programmatic, limit=limit)
    else:
        _run_pure(config, no_publish=no_publish, dry_run=dry_run,
                  workdir=workdir, programmatic=programmatic, limit=limit)


def _run_in_container(config: Path, *, workdir: Path | None, image: str | None,
                      dry_run: bool, programmatic: bool, limit: int | None,
                      no_publish: bool) -> None:
    """Phase 2: dispatch via `docker run` against the pinned image."""
    from tools.evalctl.container import (
        DEFAULT_IMAGE, ContainerError, run_in_container,
    )

    img = image or DEFAULT_IMAGE
    run_dir = workdir or Path(tempfile.gettempdir()) / "run" / str(uuid.uuid4())

    typer.echo(f"image:       {img}")
    typer.echo(f"workdir:     {run_dir}")

    if dry_run:
        typer.echo("DRY RUN \u2014 would docker-run the image with this config mounted.")
        return

    extra: list[str] = []
    if not programmatic:
        extra.append("--subprocess")
    if limit:
        extra += ["--limit", str(limit)]
    if no_publish:
        extra.append("--no-publish")

    try:
        rc = run_in_container(config, workdir=run_dir, image=img,
                              extra_evalctl_args=extra)
    except ContainerError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if rc != 0:
        typer.secho(f"Container exited with code {rc}", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    typer.secho(f"Container run complete. Artifacts in {run_dir}",
                fg=typer.colors.GREEN)


@app.command()
def validate(
    config: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Validate a config.yaml without executing."""
    if _PROTO_OK:
        cfg, sha = _load_or_die_proto(config)
        typer.secho("OK", fg=typer.colors.GREEN)
        typer.echo(f"config_sha256: {sha}")
        typer.echo(f"name:          {cfg.run.name}")
        typer.echo(f"tasks:         {', '.join(t.name for t in cfg.tasks)}")
        typer.echo(f"endpoint:      {cfg.endpoint.model_id}")
        return

    # Pure-Python fallback. No proto3 strict-field-name checks; we
    # surface that to the user so they're not surprised.
    data, sha = load_yaml_with_sha(config)
    _basic_pure_validate_or_die(data, source=str(config))
    typer.secho("OK (pure-Python validation; proto strict checks skipped)",
                fg=typer.colors.YELLOW)
    typer.echo(f"config_sha256: {sha}")
    typer.echo(f"name:          {data.get('run', {}).get('name', '')}")
    typer.echo(f"tasks:         "
               f"{', '.join(t['name'] for t in data.get('tasks', []))}")
    typer.echo(f"endpoint:      {data.get('endpoint', {}).get('model_id', '')}")


@app.command()
def show(run_id: str) -> None:
    """Show a stored run (stub: BQ sink lands in Phase 3)."""
    typer.secho(
        f"`show` requires the BigQuery sink (Phase 3). Asked for run_id={run_id}.",
        fg=typer.colors.YELLOW,
    )
    raise typer.Exit(code=1)


@app.command()
def compare(run_id_a: str, run_id_b: str) -> None:
    """Compare two stored runs (stub: BQ sink lands in Phase 3)."""
    typer.secho(
        f"`compare` requires the BigQuery sink (Phase 3). Asked for {run_id_a} vs {run_id_b}.",
        fg=typer.colors.YELLOW,
    )
    raise typer.Exit(code=1)


@app.command()
def repro(
    results: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                   help="Path to a historical results_*.json"),
    out: Path = typer.Option(..., "--out", "-o",
                             help="Where to write the reconstructed config.yaml"),
    name: str = typer.Option("reproduced_run", "--name",
                             help="run.name for the new config"),
) -> None:
    """Reconstruct an EvalConfig from a historical results_*.json.

    Phase 1: enables A/B reproducibility checks. Reads the harness's
    captured `config` and per-task `configs` keys and emits a YAML
    that, when re-run, should produce equivalent metric scores.
    """
    from tools.evalctl.repro import reconstruct_config

    try:
        cfg = reconstruct_config(results, run_name=name)
    except Exception as e:
        typer.secho(f"failed to reconstruct: {type(e).__name__}: {e}",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(cfg)
    typer.secho(f"Wrote {out} ({len(cfg.splitlines())} lines)",
                fg=typer.colors.GREEN)


@app.command()
def diff(
    a: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                             help="Reference results_*.json (the historical run)."),
    b: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                             help="Candidate results_*.json (your new run)."),
    score_tol: float = typer.Option(
        0.0, "--score-tol",
        help="Absolute tolerance for per-metric score deltas. 0 = exact."),
    n_samples_tol: int = typer.Option(
        0, "--n-samples-tol",
        help="Allowed |delta| in n-samples per task. 0 = exact."),
) -> None:
    """Structural + numeric diff between two results_*.json files.

    Exit codes:
      0 - no significant drift
      3 - drift detected (config, scores, or sample counts)
    """
    from tools.evalctl.repro import diff_results

    report, drifted = diff_results(a, b, score_tol=score_tol,
                                   n_samples_tol=n_samples_tol)
    typer.echo(report)
    if drifted:
        typer.secho("DRIFT DETECTED", fg=typer.colors.RED)
        raise typer.Exit(code=3)
    typer.secho("MATCH", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# Proto path
# ---------------------------------------------------------------------------


def _run_with_proto(config: Path, *, no_publish: bool, dry_run: bool,
                    workdir: Path | None, programmatic: bool,
                    limit: int | None) -> None:
    cfg, sha = _load_or_die_proto(config)

    manifest = build_manifest(
        cfg,
        config_sha256=sha,
        execution_mode=manifest_pb2.EXECUTION_MODE_LOCAL,
    )

    run_dir = workdir or Path(tempfile.gettempdir()) / "run" / manifest.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Write the manifest + resolved config up front (Phase 0/1 parity).
    # The dispatcher writes results_*.json + samples_*.jsonl alongside.
    from google.protobuf import json_format as _jf
    (run_dir / "manifest.json").write_text(
        _jf.MessageToJson(manifest, preserving_proto_field_name=True, indent=2)
        + "\n"
    )
    (run_dir / "config.resolved.yaml").write_text(
        _jf.MessageToJson(cfg, preserving_proto_field_name=True, indent=2)
        + "\n"
    )

    if programmatic:
        # Convert to dict and route through the programmatic dispatcher.
        cfg_dict = _jf.MessageToDict(cfg, preserving_proto_field_name=True,
                                     use_integers_for_enums=False)
        # Friendly enum form expected by the programmatic dispatcher.
        if "endpoint" in cfg_dict and "type" in cfg_dict["endpoint"]:
            cfg_dict["endpoint"]["type"] = (
                cfg_dict["endpoint"]["type"].removeprefix("ENDPOINT_TYPE_").lower()
            )
        _execute_programmatic(cfg_dict, manifest_dict={
            "run_id": manifest.run_id,
            "name": manifest.name,
        }, run_dir=run_dir, no_publish=no_publish, dry_run=dry_run, limit=limit,
            mode_note="local (host-python, proto + programmatic)")
        return

    _print_run_header(manifest.run_id, manifest.name, run_dir, no_publish,
                      mode_note="local (host-python, proto + subprocess)")

    rc = dispatch_local_host(cfg, manifest, workdir=run_dir, dry_run=dry_run)
    _exit_for_rc(rc, run_dir)


def _load_or_die_proto(path: Path):
    try:
        return load_config(path)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Pure-Python fallback path (no protobuf required)
# ---------------------------------------------------------------------------


def _run_pure(config: Path, *, no_publish: bool, dry_run: bool,
              workdir: Path | None, programmatic: bool,
              limit: int | None) -> None:
    """Run without the generated proto modules.

    Trades proto3 strict validation for the ability to run on a box where
    protoc / the pip protobuf package isn't available. The user-visible
    behavior (manifest layout, lm_eval argv, scratch dir) is identical.
    """
    typer.secho(
        f"Note: protobuf not importable ({_PROTO_ERR_MSG}); "
        "using pure-Python fallback. Field-name typos won't be caught.",
        fg=typer.colors.YELLOW,
    )

    data, sha = load_yaml_with_sha(config)
    _basic_pure_validate_or_die(data, source=str(config))

    run_id = str(uuid.uuid4())
    run_dir = workdir or Path(tempfile.gettempdir()) / "run" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = _build_manifest_pure(
        data, run_id=run_id, config_sha256=sha,
    )
    _write_manifest_pure(manifest, run_dir / "manifest.json")
    _write_resolved_config_pure(data, run_dir / "config.resolved.yaml")

    if programmatic:
        _execute_programmatic(data, manifest_dict={
            "run_id": run_id, "name": manifest["name"],
        }, run_dir=run_dir, no_publish=no_publish, dry_run=dry_run, limit=limit,
            mode_note="local (host-python, pure + programmatic)")
        return

    _print_run_header(run_id, manifest["name"], run_dir, no_publish,
                      mode_note="local (host-python, pure + subprocess)")

    argv = build_lm_eval_argv(data, output_path=str(run_dir))
    if limit:
        argv += ["--limit", str(limit)]

    if dry_run:
        typer.echo("DRY RUN — would execute:")
        typer.echo("  " + " ".join(shlex.quote(a) for a in argv))
        return

    typer.echo(f"Executing lm_eval (run_id={run_id}) ...")
    typer.echo("  " + " ".join(shlex.quote(a) for a in argv))
    log_path = run_dir / "runner.log"
    with log_path.open("w") as log:
        proc = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT)
    _exit_for_rc(proc.returncode, run_dir)


def _execute_programmatic(data: dict, *, manifest_dict: dict, run_dir: Path,
                          no_publish: bool, dry_run: bool, limit: int | None,
                          mode_note: str) -> None:
    """Phase 1: in-process lm_eval.simple_evaluate dispatch.

    Same artifact layout as the subprocess path; the difference is that
    we get the EvalResults dict in process and write the canonical
    results_*.json + samples_*.jsonl ourselves.
    """
    _print_run_header(manifest_dict["run_id"], manifest_dict["name"],
                      run_dir, no_publish, mode_note=mode_note)
    if limit:
        typer.echo(f"limit:       {limit} docs/task (smoke mode)")

    if dry_run:
        typer.echo("DRY RUN — would call lm_eval.simple_evaluate(...) with:")
        from tools.evalctl.programmatic import (
            _build_model_args, _group_tasks_by_fewshot,
        )
        typer.echo(f"  model_args   = {_build_model_args(data)}")
        typer.echo(f"  task_groups  = {_group_tasks_by_fewshot(data.get('tasks') or [])}")
        typer.echo(f"  limit        = {limit}")
        return

    try:
        from tools.evalctl.programmatic import run_programmatic
    except ImportError as e:
        typer.secho(f"lm_eval not importable: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    typer.echo(f"Executing lm_eval.simple_evaluate (run_id={manifest_dict['run_id']}) ...")
    try:
        results = run_programmatic(data, workdir=run_dir, limit=limit)
    except Exception as e:
        typer.secho(f"lm_eval failed: {type(e).__name__}: {e}",
                    fg=typer.colors.RED)
        raise typer.Exit(code=2)

    n_tasks = len(results.get("results", {}))
    typer.secho(
        f"Run complete: {n_tasks} task(s). Artifacts in {run_dir}",
        fg=typer.colors.GREEN,
    )


def _basic_pure_validate_or_die(data: dict, *, source: str) -> None:
    """Minimum semantic checks reproducible without proto schema."""
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append(f"schema_version must be 1 (got {data.get('schema_version')!r})")
    if not data.get("run", {}).get("name"):
        errors.append("run.name is required")
    if not data.get("harness", {}).get("version"):
        errors.append("harness.version is required")
    endpoint = data.get("endpoint", {}) or {}
    if not endpoint.get("type"):
        errors.append("endpoint.type is required")
    if not endpoint.get("model_id"):
        errors.append("endpoint.model_id is required")
    if endpoint.get("type") == "vertex_chat" and not endpoint.get("endpoint_id"):
        errors.append("endpoint.endpoint_id is required when endpoint.type=vertex_chat")
    if endpoint.get("type") in ("local_chat", "local_vllm") and not endpoint.get("base_url"):
        errors.append("endpoint.base_url is required for local endpoints")
    tasks = data.get("tasks") or []
    if not tasks:
        errors.append("at least one task must be specified")
    for i, t in enumerate(tasks):
        if not t.get("name"):
            errors.append(f"tasks[{i}].name is required")
    if errors:
        joined = "\n  - ".join(errors)
        typer.secho(f"{source}: validation failed:\n  - {joined}",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def _build_manifest_pure(data: dict, *, run_id: str, config_sha256: str) -> dict:
    """Build a manifest dict matching the proto3 JSON form of RunManifest."""
    endpoint = data.get("endpoint", {}) or {}
    return {
        "run_id": run_id,
        "schema_version": 1,
        "name": data.get("run", {}).get("name", ""),
        "user_id": os.getenv("USER") or os.getenv("LOGNAME") or "unknown",
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "execution_mode": "EXECUTION_MODE_LOCAL",
        "git": _git_info_pure(data.get("harness", {}).get("version", "")),
        "container": {"image": "", "digest": ""},
        "endpoint": {
            "type": endpoint.get("type", ""),
            "model_id": endpoint.get("model_id", ""),
            "endpoint_id": endpoint.get("endpoint_id", ""),
        },
        "env_allow_listed": filter_env(os.environ),
        "config_sha256": config_sha256,
        "tags": dict(data.get("run", {}).get("tags", {}) or {}),
        "status": "RUN_STATUS_RUNNING",
        "vertex_job_id": "",
    }


def _git_info_pure(harness_version: str) -> dict:
    def _git(args):
        try:
            r = subprocess.run(["git", *args], capture_output=True, text=True,
                               timeout=5, check=True)
            return r.stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            return ""
    sha = _git(["rev-parse", "HEAD"])
    dirty = _git(["status", "--porcelain"]) != ""
    return {
        "wrapper_commit": sha,
        "wrapper_dirty": dirty,
        "harness_version": harness_version,
        "harness_commit": sha,
    }


def _write_manifest_pure(manifest: dict, path: Path) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")


def _write_resolved_config_pure(data: dict, path: Path) -> None:
    # Persist as JSON (valid YAML subset; matches proto path's behavior).
    path.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Shared output helpers
# ---------------------------------------------------------------------------


def _print_run_header(run_id: str, name: str, run_dir: Path,
                      no_publish: bool, *, mode_note: str) -> None:
    typer.echo(f"run_id:      {run_id}")
    typer.echo(f"name:        {name}")
    typer.echo(f"workdir:     {run_dir}")
    typer.echo(f"mode:        {mode_note}")
    if no_publish:
        typer.echo("publish:     disabled (--no-publish)")


def _exit_for_rc(rc: int, run_dir: Path) -> None:
    if rc != 0:
        typer.secho(f"lm_eval exited with code {rc}", fg=typer.colors.RED)
        typer.secho(f"Logs: {run_dir / 'runner.log'}", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    typer.secho(f"Run complete. Artifacts in {run_dir}",
                fg=typer.colors.GREEN)


if __name__ == "__main__":  # pragma: no cover
    app()
