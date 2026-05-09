"""Proto-free helpers, extracted so they can be unit-tested without
requiring protoc/bazel codegen to be available.

Anything in this module MUST NOT import from `proto.merit.v1`. The
proto-coupled callers (config_loader, manifest, execution) re-export or
delegate to these helpers.

Rationale: the Corp Airlock policy and the absence of protoc on dev
laptops make a proto-required test suite painful for everyday smoke
checks. Keeping the conversion logic and the security-critical
allow-list logic here lets us exercise them with stdlib + PyYAML alone.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any, Mapping

import yaml

# ---------------------------------------------------------------------------
# Env allow-list (mirrors manifest.ENV_ALLOW_LIST). Defined here so
# manifest.py can import it without dragging proto deps into tests.
# ---------------------------------------------------------------------------

ENV_ALLOW_LIST: frozenset[str] = frozenset({
    "USER",
    "LOGNAME",
    "HOSTNAME",
    "MERIT_VERTEX_PROJECT",
    "MERIT_VERTEX_REGION",
    "MERIT_OVERRIDE_BASE_URL",
    "VLLM_HOST",
    "HF_HOME",
})


def filter_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop everything not on the allow-list. Defense against secret leakage."""
    return {k: v for k, v in env.items() if k in ENV_ALLOW_LIST}


# ---------------------------------------------------------------------------
# YAML loading + sha256 (proto-free). Returns the parsed dict and the
# digest of the on-disk bytes. Semantic validation that requires the
# proto schema lives in config_loader.validate_semantics.
# ---------------------------------------------------------------------------

CURRENT_SCHEMA_VERSION = 1


def load_yaml_with_sha(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read a YAML file and return (parsed_dict, sha256_hex)."""
    p = Path(path)
    raw = p.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    data = yaml.safe_load(io.BytesIO(raw))
    if not isinstance(data, dict):
        raise ValueError(f"{p}: top-level must be a mapping")
    return data, sha


# ---------------------------------------------------------------------------
# Friendly enum normalization. Users write `endpoint.type: vertex_chat`;
# json_format.ParseDict expects the proto3 enum name form.
# ---------------------------------------------------------------------------

ENUM_NORMALIZATIONS: dict[tuple[str, ...], dict[str, str]] = {
    ("endpoint", "type"): {
        "vertex_chat": "ENDPOINT_TYPE_VERTEX_CHAT",
        "local_chat": "ENDPOINT_TYPE_LOCAL_CHAT",
        "local_vllm": "ENDPOINT_TYPE_LOCAL_VLLM",
        "hf": "ENDPOINT_TYPE_HF",
    },
    ("endpoint", "auth", "mode"): {
        "gcloud_adc": "AUTH_MODE_GCLOUD_ADC",
        "static_token": "AUTH_MODE_STATIC_TOKEN",
        "none": "AUTH_MODE_NONE",
    },
}


def normalize_enums(data: dict[str, Any]) -> None:
    """Mutate `data` in place to upper-snake enum strings."""
    for path, mapping in ENUM_NORMALIZATIONS.items():
        node: Any = data
        for key in path[:-1]:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if not isinstance(node, dict):
            continue
        leaf = path[-1]
        val = node.get(leaf)
        if isinstance(val, str) and val in mapping:
            node[leaf] = mapping[val]


# ---------------------------------------------------------------------------
# lm_eval argv translation. The proto-coupled execution.py wraps this with
# a thin adapter that reads from the EvalConfig proto; here we operate on
# a plain dict so the translation logic is testable without protoc.
# ---------------------------------------------------------------------------

# Map the user-facing endpoint string -> lm_eval --model adapter name.
ENDPOINT_TO_ADAPTER: dict[str, str] = {
    "vertex_chat": "local-chat-completions",
    "local_chat": "local-chat-completions",
    "local_vllm": "local-completions",
    "hf": "hf",
}

# Endpoints that require chat-template wrapping by default. Other
# endpoints (local_vllm, hf) speak the completions protocol and must
# NOT have --apply_chat_template added.
_CHAT_TEMPLATE_REQUIRED: frozenset[str] = frozenset({"vertex_chat", "local_chat"})


def resolve_chat_template(
    cfg: Mapping[str, Any],
) -> tuple[bool, str | None, bool]:
    """Decide whether to pass --apply_chat_template (and --fewshot_as_multiturn).

    Returns (apply, template_name_or_None, fewshot_as_multiturn).

    Defaulting rules (when cfg.chat_template is unset or mode unspecified):
      - chat endpoints  -> apply=True, template=None, multiturn=True
      - other endpoints -> apply=False
    Explicit user values override the defaults.
    """
    endpoint_type = cfg.get("endpoint", {}).get("type")
    chat = cfg.get("chat_template") or {}
    mode = chat.get("mode", "MODE_UNSPECIFIED")

    if mode == "MODE_DISABLED":
        return (False, None, False)
    if mode == "MODE_AUTO":
        return (True, None, bool(chat.get("fewshot_as_multiturn", True)))
    if mode == "MODE_NAMED":
        name = chat.get("template_name") or None
        return (True, name, bool(chat.get("fewshot_as_multiturn", True)))

    # MODE_UNSPECIFIED -> decide by endpoint.
    if endpoint_type in _CHAT_TEMPLATE_REQUIRED:
        return (True, None, True)
    return (False, None, False)


def build_lm_eval_argv(cfg: Mapping[str, Any], *, output_path: str) -> list[str]:
    """Translate a (validated) config dict into an `lm_eval` argv.

    Mirrors execution._build_lm_eval_argv but operates on a plain dict so
    the translation rules can be unit-tested without proto codegen. The
    proto path delegates here after stringifying its enum field.
    """
    endpoint_type = cfg.get("endpoint", {}).get("type")
    if endpoint_type not in ENDPOINT_TO_ADAPTER:
        raise ValueError(f"unsupported endpoint type: {endpoint_type!r}")
    adapter = ENDPOINT_TO_ADAPTER[endpoint_type]

    endpoint = cfg.get("endpoint", {})
    model_args = cfg.get("model_args", {}) or {}

    pairs = [f"model={endpoint['model_id']}"]
    if endpoint.get("base_url"):
        pairs.append(f"base_url={endpoint['base_url']}")
    if model_args.get("num_concurrent"):
        pairs.append(f"num_concurrent={model_args['num_concurrent']}")
    if model_args.get("max_gen_toks"):
        pairs.append(f"max_gen_toks={model_args['max_gen_toks']}")
    if model_args.get("timeout"):
        pairs.append(f"timeout={model_args['timeout']}")

    tasks = cfg.get("tasks", []) or []
    if not tasks:
        raise ValueError("at least one task required")

    argv = [
        "lm_eval",
        "--model", adapter,
        "--model_args", ",".join(pairs),
        "--tasks", ",".join(t["name"] for t in tasks),
        "--output_path", output_path,
        "--log_samples",
        "--include_path", "custom_tasks",
    ]

    # Chat-template handling. lm_eval's local-chat-completions adapter
    # requires --apply_chat_template; without it, requests fail with
    # "expects messages as list[dict]". The team's reference invocation
    # implicitly relied on the harness's `fewshot_as_multiturn=True`
    # default which itself requires --apply_chat_template.
    apply_ct, template_name, multiturn = resolve_chat_template(cfg)
    if apply_ct:
        argv.append("--apply_chat_template")
        if template_name:
            argv.append(template_name)
        if multiturn:
            argv.append("--fewshot_as_multiturn")

    # Phase 0 limitation: lm_eval CLI takes a single --num_fewshot. We
    # honor tasks[0]; Phase 1 switches to programmatic invocation.
    if tasks and tasks[0].get("num_fewshot"):
        argv += ["--num_fewshot", str(tasks[0]["num_fewshot"])]

    seed = cfg.get("seed") or []
    if seed:
        argv += ["--seed", ",".join(str(s) for s in seed)]

    gen_kwargs = cfg.get("gen_kwargs") or {}
    if gen_kwargs:
        kv = ",".join(f"{k}={_render_scalar(v)}" for k, v in gen_kwargs.items())
        argv += ["--gen_kwargs", kv]

    per_task = (cfg.get("limits") or {}).get("per_task") or 0
    if per_task:
        argv += ["--limit", str(per_task)]

    return argv


def _render_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)
