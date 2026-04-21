"""evalctl CLI surface.

Subcommands:
  run       Submit an eval run (local in Phase 0; remote arrives in Phase 5).
  validate  Schema/semantic check on a config without executing.
  show      Inspect a previously stored run (stub until BQ sink lands in Phase 3).
  compare   Diff two runs (stub until BQ sink lands).

Conventions:
  - Exit codes: 0 success, 1 user error (bad config), 2 runtime failure.
  - All stdout messages are user-facing; runner internals log to runner.log.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import typer
from google.protobuf import json_format

from tools.evalctl.config_loader import ConfigError, load_config
from tools.evalctl.execution import dispatch_local_host
from tools.evalctl.manifest import build_manifest

from proto.eval.v1 import manifest_pb2

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
                               help="Execute locally (Phase 0: host-Python only)."),
    no_publish: bool = typer.Option(False, "--no-publish",
                                    help="Skip GCS/BQ publish (no-op until Phase 3)."),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Validate + print the lm_eval command without executing."),
    workdir: Path | None = typer.Option(None, "--workdir",
                                        help="Override scratch dir (default: /tmp/run/<run_id>/)."),
) -> None:
    """Submit an eval run."""
    if not local and not dry_run:
        typer.secho(
            "Phase 0 supports --local only. Remote dispatch lands in Phase 5.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    cfg, sha = _load_or_die(config)

    manifest = build_manifest(
        cfg,
        config_sha256=sha,
        execution_mode=manifest_pb2.EXECUTION_MODE_LOCAL,
    )

    run_dir = workdir or Path(tempfile.gettempdir()) / "run" / manifest.run_id
    typer.echo(f"run_id:      {manifest.run_id}")
    typer.echo(f"name:        {manifest.name}")
    typer.echo(f"workdir:     {run_dir}")
    typer.echo(f"mode:        local (host-python)")
    if no_publish:
        typer.echo("publish:     disabled (--no-publish)")

    rc = dispatch_local_host(cfg, manifest, workdir=run_dir, dry_run=dry_run)
    if rc != 0:
        typer.secho(f"lm_eval exited with code {rc}", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    typer.secho(
        f"Run complete. Artifacts in {run_dir}",
        fg=typer.colors.GREEN,
    )


@app.command()
def validate(
    config: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Validate a config.yaml without executing."""
    cfg, sha = _load_or_die(config)
    typer.secho("OK", fg=typer.colors.GREEN)
    typer.echo(f"config_sha256: {sha}")
    typer.echo(f"name:          {cfg.run.name}")
    typer.echo(f"tasks:         {', '.join(t.name for t in cfg.tasks)}")
    typer.echo(f"endpoint:      {cfg.endpoint.model_id}")


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


# --- helpers ------------------------------------------------------------------


def _load_or_die(path: Path):
    try:
        return load_config(path)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    app()
