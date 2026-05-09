"""Reproducibility utilities (Phase 1).

Two operations:

  reconstruct_config(results.json) -> yaml string
      Materialize an EvalConfig from a harness results_*.json.
      The harness writes the full run + per-task config into the
      results, so this is a mechanical projection.

  diff_results(a.json, b.json) -> (report_text, drifted_bool)
      Structural + numeric comparison of two results files. Used by
      `evalctl diff` and CI to gate "this change doesn't perturb
      historical sweeps."

Both operate on the harness's existing on-disk format (as documented in
docs/merit-hld.md \u00a76.5) so they don't depend on protoc / our
proto codegen being available.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# reconstruct_config
# ---------------------------------------------------------------------------

def reconstruct_config(results_path: Path, *, run_name: str = "reproduced_run") -> str:
    """Read a results_*.json and emit an evalctl-compatible config.yaml.

    The reverse of execution.py's argv translation. Best-effort: some
    harness CLI flags don't round-trip through the results JSON
    (e.g. --include_path), so we fill in sensible defaults and warn
    via inline YAML comments where the user should review.
    """
    data = json.loads(Path(results_path).read_text())

    run_cfg = data.get("config", {}) or {}
    task_configs = data.get("configs", {}) or {}

    model = run_cfg.get("model", "")
    model_args = _parse_model_args(run_cfg.get("model_args"))

    endpoint_type = _infer_endpoint_type(model, model_args)
    base_url = model_args.get("base_url", "")
    endpoint_id = _infer_endpoint_id(base_url)

    # Per-task num_fewshot lives in `configs[<task>].num_fewshot`.
    tasks_yaml: list[dict[str, Any]] = []
    for task_name, tc in sorted(task_configs.items()):
        tasks_yaml.append({
            "name": task_name,
            "num_fewshot": tc.get("num_fewshot", 0) or 0,
            "metrics": _extract_metric_names(tc),
        })

    seeds = _seed_list_from_config(run_cfg)
    gen_kwargs = run_cfg.get("gen_kwargs") or {}
    if isinstance(gen_kwargs, str):
        gen_kwargs = _parse_kv_string(gen_kwargs)

    has_chat_template = bool(
        run_cfg.get("apply_chat_template") or run_cfg.get("fewshot_as_multiturn")
    )

    out_lines: list[str] = []
    out_lines.append(f"# Reconstructed by `evalctl repro` from {results_path.name}.")
    out_lines.append("# Review fields marked TODO before re-running; some flags")
    out_lines.append("# (e.g. --include_path, auth) don't round-trip.")
    out_lines.append("schema_version: 1")
    out_lines.append("")
    out_lines.append("run:")
    out_lines.append(f"  name: {run_name}")
    out_lines.append("  tags:")
    out_lines.append("    source: reconstructed")
    out_lines.append(f"    source_file: {results_path.name}")
    out_lines.append("")
    out_lines.append("harness:")
    out_lines.append(f"  version: \"{data.get('lm_eval_version') or '0.0.0'}\"")
    out_lines.append("")
    out_lines.append("endpoint:")
    out_lines.append(f"  type: {endpoint_type}")
    out_lines.append(f"  model_id: {model_args.get('model') or model}")
    if endpoint_id:
        out_lines.append(f"  endpoint_id: \"{endpoint_id}\"")
    if base_url:
        out_lines.append(f"  base_url: \"{base_url}\"")
    out_lines.append("  auth:")
    out_lines.append("    mode: gcloud_adc  # TODO confirm")
    out_lines.append("")
    out_lines.append("tasks:")
    for t in tasks_yaml:
        out_lines.append(f"  - name: {t['name']}")
        out_lines.append(f"    num_fewshot: {t['num_fewshot']}")
        if t["metrics"]:
            out_lines.append(f"    metrics: {t['metrics']}")
    out_lines.append("")
    out_lines.append("model_args:")
    for k in ("num_concurrent", "max_gen_toks", "timeout"):
        if model_args.get(k) is not None:
            out_lines.append(f"  {k}: {model_args[k]}")
    out_lines.append("")
    if gen_kwargs:
        out_lines.append("gen_kwargs:")
        for k, v in gen_kwargs.items():
            out_lines.append(f"  {k}: {_yaml_scalar(v)}")
        out_lines.append("")
    if seeds:
        out_lines.append(f"seed: {seeds}")
        out_lines.append("")
    out_lines.append("limits:")
    limit = run_cfg.get("limit")
    out_lines.append(f"  per_task: {int(limit) if limit else 0}")
    out_lines.append("")
    if has_chat_template or endpoint_type in ("vertex_chat", "local_chat"):
        out_lines.append("chat_template:")
        if isinstance(run_cfg.get("apply_chat_template"), str):
            out_lines.append("  mode: MODE_NAMED")
            out_lines.append(f"  template_name: \"{run_cfg['apply_chat_template']}\"")
        else:
            out_lines.append("  mode: MODE_AUTO")
        out_lines.append(
            f"  fewshot_as_multiturn: "
            f"{str(bool(run_cfg.get('fewshot_as_multiturn', True))).lower()}"
        )
        out_lines.append("")
    out_lines.append("output:")
    out_lines.append("  gcs_bucket: \"gs://merit-artifacts-prod\"  # TODO confirm")
    out_lines.append("  bq_dataset: \"merit_results\"               # TODO confirm")
    return "\n".join(out_lines) + "\n"


def _parse_model_args(value: Any) -> dict[str, Any]:
    """Accept either dict (programmatic) or comma-separated KV string (CLI)."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        return _parse_kv_string(value)
    return {}


def _parse_kv_string(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in s.split(","):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _infer_endpoint_type(model: str, model_args: dict[str, Any]) -> str:
    if model in ("local-chat-completions",):
        url = model_args.get("base_url", "")
        if "aiplatform" in url:
            return "vertex_chat"
        return "local_chat"
    if model == "local-completions":
        return "local_vllm"
    if model in ("hf", "huggingface", "hf-auto"):
        return "hf"
    return "local_chat"  # safe default for chat-shaped APIs


def _infer_endpoint_id(base_url: str) -> str:
    """Pick the projects/.../endpoints/<id> substring out of a Vertex URL."""
    if "/endpoints/" not in base_url:
        return ""
    # Trim from "projects/" to the trailing "/chat/completions" if present.
    idx = base_url.find("projects/")
    if idx < 0:
        return ""
    tail = base_url[idx:]
    for suffix in ("/chat/completions", "/completions", ":predict"):
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)]
            break
    return tail


def _extract_metric_names(task_config: dict[str, Any]) -> list[str]:
    metrics = task_config.get("metric_list") or []
    names: list[str] = []
    for m in metrics:
        if isinstance(m, dict) and "metric" in m:
            names.append(m["metric"])
        elif isinstance(m, str):
            names.append(m)
    return names


def _seed_list_from_config(cfg: dict[str, Any]) -> list[int]:
    """Pull out the four seeds the harness records."""
    keys = ("random_seed", "numpy_random_seed",
            "torch_random_seed", "fewshot_random_seed")
    out = [cfg.get(k) for k in keys]
    if all(v is None for v in out):
        return []
    return [int(v) if v is not None else 0 for v in out]


def _yaml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if any(c in s for c in ":#{}[]&*!|>'\"%@`,"):
        return json.dumps(s)
    return s


# ---------------------------------------------------------------------------
# diff_results
# ---------------------------------------------------------------------------


def diff_results(
    a_path: Path,
    b_path: Path,
    *,
    score_tol: float = 0.0,
    n_samples_tol: int = 0,
) -> tuple[str, bool]:
    """Compare two results_*.json files; return (human_report, drifted_bool)."""
    a = json.loads(Path(a_path).read_text())
    b = json.loads(Path(b_path).read_text())

    lines: list[str] = [f"diff {a_path} -> {b_path}", "=" * 60]
    drifted = False

    # 1. Tasks present.
    a_tasks = set((a.get("results") or {}).keys())
    b_tasks = set((b.get("results") or {}).keys())
    only_a = sorted(a_tasks - b_tasks)
    only_b = sorted(b_tasks - a_tasks)
    if only_a:
        drifted = True
        lines.append(f"  TASK MISSING in B: {only_a}")
    if only_b:
        drifted = True
        lines.append(f"  TASK NEW in B:     {only_b}")

    # 2. Per-task metric comparison.
    common = sorted(a_tasks & b_tasks)
    for task in common:
        a_res = a["results"][task]
        b_res = b["results"][task]
        for metric_key in sorted(set(a_res) | set(b_res)):
            if metric_key.startswith("alias") or metric_key.endswith("_stderr,none"):
                continue
            av = a_res.get(metric_key)
            bv = b_res.get(metric_key)
            if not isinstance(av, (int, float)) or not isinstance(bv, (int, float)):
                continue
            delta = bv - av
            if abs(delta) > score_tol:
                drifted = True
                marker = "DRIFT"
            else:
                marker = "ok   "
            lines.append(
                f"  [{marker}] {task}.{metric_key}: "
                f"{av:.6f} -> {bv:.6f} (\u0394={delta:+.6f}, tol={score_tol})"
            )

    # 3. n-samples per task.
    a_ns = a.get("n-samples") or {}
    b_ns = b.get("n-samples") or {}
    for task in sorted(set(a_ns) | set(b_ns)):
        an = (a_ns.get(task) or {}).get("effective", 0)
        bn = (b_ns.get(task) or {}).get("effective", 0)
        if abs(bn - an) > n_samples_tol:
            drifted = True
            lines.append(
                f"  [DRIFT] {task} n-samples: {an} -> {bn} "
                f"(\u0394={bn - an:+d}, tol={n_samples_tol})"
            )

    # 4. Run-level config drift (model, gen_kwargs).
    a_cfg = a.get("config") or {}
    b_cfg = b.get("config") or {}
    for key in ("model", "model_source", "fewshot_as_multiturn"):
        if a_cfg.get(key) != b_cfg.get(key):
            drifted = True
            lines.append(f"  [DRIFT] config.{key}: "
                         f"{a_cfg.get(key)!r} -> {b_cfg.get(key)!r}")

    return ("\n".join(lines), drifted)
