"""Programmatic dispatcher.

Phase 1: replaces the Phase 0 subprocess-based `lm_eval` invocation
with `lm_eval.simple_evaluate(...)`. Benefits:

  * No JSON-out-then-JSON-in roundtrip; we get the EvalResults dict in
    process and can write our own canonical artifact layout.
  * Per-task `num_fewshot` is supported (Phase 0 only honored tasks[0]).
    If the config groups tasks by distinct num_fewshot value, we issue
    one simple_evaluate call per group and merge the results.
  * The chat-template + fewshot_as_multiturn flags are passed through
    keyword args (no shell quoting risk).

This module is intentionally proto-free: it operates on a plain dict
that mirrors the EvalConfig JSON projection. Both the proto path
(execution.py) and the pure fallback (cli.py) call into it.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from tools.evalctl._pure import (
    ENDPOINT_TO_ADAPTER,
    resolve_chat_template,
)


def run_programmatic(
    cfg: Mapping[str, Any],
    *,
    workdir: Path,
    limit: int | None = None,
) -> dict:
    """Execute lm_eval.simple_evaluate against the resolved config.

    `limit` (if not None) caps docs per task — useful for smoke runs.
    Returns the merged results dict; also writes results_*.json and
    samples_*.jsonl to `workdir` matching the harness's on-disk layout.
    """
    # Imported lazily so cli.py can introspect availability before
    # paying the import cost (lm_eval imports torch, etc.).
    from lm_eval import simple_evaluate

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    endpoint_type = cfg.get("endpoint", {}).get("type")
    if endpoint_type not in ENDPOINT_TO_ADAPTER:
        raise ValueError(f"unsupported endpoint type: {endpoint_type!r}")
    adapter = ENDPOINT_TO_ADAPTER[endpoint_type]

    model_args = _build_model_args(cfg)
    apply_ct, ct_name, multiturn = resolve_chat_template(cfg)
    apply_chat_template_arg = ct_name if (apply_ct and ct_name) else apply_ct

    gen_kwargs = dict(cfg.get("gen_kwargs") or {})

    seeds = cfg.get("seed") or []
    seed_kwargs = _seed_kwargs_from_list(seeds)

    # Group tasks by num_fewshot so each simple_evaluate call has a
    # consistent value. This sidesteps the global-num_fewshot limitation.
    task_groups = _group_tasks_by_fewshot(cfg.get("tasks") or [])

    aggregated: dict[str, Any] = {
        "results": {},
        "configs": {},
        "versions": {},
        "n-samples": {},
        "higher_is_better": {},
        "samples": {},  # keyed by task name when log_samples=True
        "config": None,
    }
    start = time.time()

    for fewshot, task_names in task_groups.items():
        result = simple_evaluate(
            model=adapter,
            model_args=model_args,
            tasks=task_names,
            num_fewshot=fewshot,
            limit=limit,
            log_samples=True,
            apply_chat_template=apply_chat_template_arg,
            fewshot_as_multiturn=multiturn,
            gen_kwargs=gen_kwargs if gen_kwargs else None,
            **seed_kwargs,
        )
        if result is None:
            # rank > 0 in distributed setting; nothing to merge.
            continue
        _merge_results(aggregated, result)

    aggregated["total_evaluation_time_seconds"] = time.time() - start

    _write_results(aggregated, workdir)
    _write_samples(aggregated, workdir)
    return aggregated


# --- helpers -----------------------------------------------------------------


def _build_model_args(cfg: Mapping[str, Any]) -> dict[str, Any]:
    endpoint = cfg.get("endpoint", {}) or {}
    model_args_in = cfg.get("model_args", {}) or {}
    out: dict[str, Any] = {"model": endpoint.get("model_id", "")}
    if endpoint.get("base_url"):
        out["base_url"] = endpoint["base_url"]
    for key in ("num_concurrent", "max_gen_toks", "timeout"):
        if model_args_in.get(key):
            out[key] = model_args_in[key]
    return out


def _seed_kwargs_from_list(seeds: list[int]) -> dict[str, int]:
    """Map the team's [a,b,c,d] convention onto simple_evaluate kwargs.

    Mirrors the harness's CLI behavior: --seed a,b,c,d sets
    (random, numpy, torch, fewshot) seeds in order. Missing positions
    fall back to the harness defaults.
    """
    names = ("random_seed", "numpy_random_seed",
             "torch_random_seed", "fewshot_random_seed")
    return {n: int(s) for n, s in zip(names, seeds)}


def _group_tasks_by_fewshot(
    tasks: list[Mapping[str, Any]],
) -> dict[int | None, list[str]]:
    """Group task names by their num_fewshot value.

    None preserves the harness's per-task default (i.e. don't override).
    """
    groups: dict[int | None, list[str]] = defaultdict(list)
    for t in tasks:
        nf = t.get("num_fewshot")
        # Treat 0 and None distinctly: 0 is an explicit override, None
        # means "use the task's YAML default."
        groups[nf].append(t["name"])
    return dict(groups)


def _merge_results(into: dict[str, Any], src: dict[str, Any]) -> None:
    """Shallow merge of two EvalResults dicts.

    Only merges the keys we care about for repro. Conflicts on `config`
    keep the first writer's value (the task-level configs already live
    under `configs`, indexed by task).
    """
    for key in ("results", "configs", "versions", "n-samples", "higher_is_better"):
        into.setdefault(key, {}).update(src.get(key, {}))
    if "samples" in src and src["samples"]:
        into.setdefault("samples", {}).update(src["samples"])
    # First call's run-level config wins; subsequent calls would just
    # repeat the same model_args anyway.
    if into.get("config") is None and "config" in src:
        into["config"] = src["config"]


def _write_results(aggregated: dict[str, Any], workdir: Path) -> None:
    """Write results_<ts>.json matching the harness's layout."""
    ts = time.strftime("%Y-%m-%dT%H-%M-%S.000")
    path = workdir / f"results_{ts}.json"
    # Strip in-memory-only `samples` (lives in samples_*.jsonl instead).
    out = {k: v for k, v in aggregated.items() if k != "samples"}
    path.write_text(json.dumps(out, indent=2, default=str) + "\n")


def _write_samples(aggregated: dict[str, Any], workdir: Path) -> None:
    """Write samples_<task>_<ts>.jsonl per task, matching upstream."""
    samples = aggregated.get("samples") or {}
    if not samples:
        return
    ts = time.strftime("%Y-%m-%dT%H-%M-%S.000")
    for task_name, rows in samples.items():
        path = workdir / f"samples_{task_name}_{ts}.jsonl"
        with path.open("w") as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")
