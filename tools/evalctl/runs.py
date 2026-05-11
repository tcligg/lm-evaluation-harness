"""Run discovery + lookup, keyed on run_id.

The HLD makes `run_id` (uuid4) the universal join key across manifests,
GCS objects, BQ rows, and Vertex job ids. This module is the single
entry point for "give me everything I have about <run_id>" so that
`evalctl show`, `evalctl compare`, and (later) `evalctl publish` /
`evalctl pull` all share the same lookup semantics.

Backends are tried in order; the first hit wins:

  1. Local scratch dirs under MERIT_RUN_ROOTS (defaults to /tmp/run
     and the user's home cache). Phase 0 onward.
  2. GCS at gs://<bucket>/runs/<run_id>/. Phase 3 (stub today).
  3. BigQuery merit_results.merit_runs row. Phase 3 (stub today).

The resolver is proto-free: it works against the on-disk JSON layout
the runner produces. Callers that need typed proto objects can re-load
from the JSON if they want; for `show` and `compare` the dict form is
sufficient.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


# Where to look for local runs. Comma-separated, in priority order.
# /tmp/run is the runner's default workdir; ~/.cache/merit/runs is a
# proposed long-lived spot for runs the user opts to keep.
DEFAULT_RUN_ROOTS = (
    Path(os.getenv("MERIT_RUN_ROOTS", "/tmp/run").split(":")[0]),
    Path.home() / ".cache" / "merit" / "runs",
)


@dataclass
class RunRef:
    """A resolved run, regardless of backend.

    Fields are populated best-effort; missing fields are left as None /
    empty. `source` records where this RunRef was found so callers can
    tell the user 'local' vs 'gcs' vs 'bq'.
    """
    run_id: str
    source: str                     # "local" | "gcs" | "bq"
    workdir: Path | None = None     # populated when source == "local"
    gcs_uri: str | None = None      # populated when source == "gcs"
    manifest: dict | None = None
    config: dict | None = None
    config_path: Path | None = None # config.resolved.yaml on disk, if present
    results: dict | None = None     # parsed results_*.json (latest)
    sample_files: list[Path] = field(default_factory=list)
    log_path: Path | None = None


class RunNotFound(LookupError):
    """No backend has a record of this run_id."""


# ---------------------------------------------------------------------------
# Local backend
# ---------------------------------------------------------------------------

def _scan_local_roots(roots: tuple[Path, ...] | None = None) -> list[Path]:
    """Return existing run dirs across all configured roots, newest first."""
    roots = roots or DEFAULT_RUN_ROOTS
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.is_dir():
                found.append(child)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def _load_local_run(run_dir: Path, run_id: str) -> RunRef:
    """Populate a RunRef from the on-disk artifact layout."""
    ref = RunRef(run_id=run_id, source="local", workdir=run_dir)

    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        try:
            ref.manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            pass

    config_path = run_dir / "config.resolved.yaml"
    if config_path.exists():
        ref.config_path = config_path
        try:
            # The runner writes JSON-in-.yaml (proto3 JSON projection).
            ref.config = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            pass

    # Pick the newest results_*.json file (multiple can exist if a run
    # was retried inside the same workdir).
    results = sorted(run_dir.glob("results_*.json"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if results:
        try:
            ref.results = json.loads(results[0].read_text())
        except json.JSONDecodeError:
            pass

    ref.sample_files = sorted(run_dir.glob("samples_*.jsonl"))
    log_path = run_dir / "runner.log"
    if log_path.exists():
        ref.log_path = log_path
    return ref


def _find_local_by_id(run_id: str,
                      roots: tuple[Path, ...] | None = None) -> RunRef | None:
    """Look for a workdir matching run_id under any configured root.

    Two matching strategies:
      - exact: <root>/<run_id>/  (the default workdir layout)
      - manifest: any sibling dir whose manifest.json has matching run_id
        (covers --workdir overrides where the dir name doesn't match)
    """
    for root in (roots or DEFAULT_RUN_ROOTS):
        candidate = root / run_id
        if candidate.is_dir():
            return _load_local_run(candidate, run_id)

    # Slow path: manifest scan. Only used when the dir name doesn't
    # match (e.g. user invoked --workdir /some/path).
    for run_dir in _scan_local_roots(roots):
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            data = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            continue
        if data.get("run_id") == run_id:
            return _load_local_run(run_dir, run_id)
    return None


# ---------------------------------------------------------------------------
# GCS backend (Phase 3 stub)
# ---------------------------------------------------------------------------

def _find_gcs_by_id(run_id: str) -> RunRef | None:
    """Stub: Phase 3 will implement gs://<bucket>/runs/<run_id>/ lookup.

    Today, returns None unless MERIT_GCS_BUCKET is set AND the
    google-cloud-storage client can be imported. In that case we
    surface a 'not implemented' note so the user knows to check back
    after Phase 3 lands.
    """
    bucket = os.getenv("MERIT_GCS_BUCKET")
    if not bucket:
        return None
    # Lazy-import to avoid forcing google-cloud-storage on smoke runs.
    try:
        # noqa: F401 - imported only for availability check
        from google.cloud import storage  # type: ignore[import-not-found]
    except ImportError:
        return None
    # TODO(phase 3): list gs://<bucket>/runs/<run_id>/ and populate.
    return None


# ---------------------------------------------------------------------------
# BigQuery backend (Phase 3 stub)
# ---------------------------------------------------------------------------

def _find_bq_by_id(run_id: str) -> RunRef | None:
    """Stub: Phase 3 will SELECT * FROM merit_results.merit_runs."""
    if not os.getenv("MERIT_BQ_DATASET"):
        return None
    try:
        from google.cloud import bigquery  # noqa: F401 # type: ignore[import-not-found]
    except ImportError:
        return None
    # TODO(phase 3): SELECT row, then JOIN merit_metrics for scores.
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_run_by_id(
    run_id: str,
    *,
    roots: tuple[Path, ...] | None = None,
) -> RunRef:
    """Resolve a run_id across all backends. Raises RunNotFound on miss.

    Backends are tried in order; the first non-None hit wins.
    """
    backends = (
        lambda: _find_local_by_id(run_id, roots),
        lambda: _find_gcs_by_id(run_id),
        lambda: _find_bq_by_id(run_id),
    )
    for backend in backends:
        ref = backend()
        if ref is not None:
            return ref
    raise RunNotFound(
        f"No run found with run_id={run_id!r}. Searched: "
        f"local roots {[str(r) for r in (roots or DEFAULT_RUN_ROOTS)]}, "
        f"GCS (MERIT_GCS_BUCKET={os.getenv('MERIT_GCS_BUCKET') or 'unset'}), "
        f"BQ (MERIT_BQ_DATASET={os.getenv('MERIT_BQ_DATASET') or 'unset'})."
    )


def list_local_runs(
    roots: tuple[Path, ...] | None = None,
    *,
    limit: int | None = 20,
) -> list[RunRef]:
    """Enumerate local runs, newest first. Used by `evalctl ls`."""
    out: list[RunRef] = []
    for run_dir in _scan_local_roots(roots):
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            data = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            continue
        run_id = data.get("run_id")
        if not run_id:
            continue
        out.append(_load_local_run(run_dir, run_id))
        if limit is not None and len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Convenience: project a RunRef into a one-line summary for `ls` output.
# ---------------------------------------------------------------------------

def summarize(ref: RunRef) -> str:
    m = ref.manifest or {}
    name = m.get("name", "?")
    status = m.get("status", "?").removeprefix("RUN_STATUS_").lower()
    user = m.get("user_id", "?")
    ts = m.get("timestamp_utc", "")
    # Trim ISO down to YYYY-MM-DD HH:MM for readability.
    ts_short = ts.replace("T", " ").rsplit(":", 1)[0] if ts else ""
    n_tasks = len((ref.results or {}).get("results", {})) if ref.results else 0
    return (
        f"{ref.run_id}  {ts_short:<16}  {status:<8}  "
        f"{user:<12}  tasks={n_tasks}  {name}"
    )


def humanize_age(epoch: float) -> str:
    """Render a seconds-since-epoch as '3m ago' / '2h ago' / '5d ago'."""
    delta = max(0, int(time.time() - epoch))
    for unit, sec in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= sec:
            return f"{delta // sec}{unit} ago"
    return f"{delta}s ago"
