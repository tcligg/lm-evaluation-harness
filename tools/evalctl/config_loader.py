"""YAML <-> EvalConfig (proto) loader.

The user authors `config.yaml`; we load it as a dict and parse into the
generated proto via `json_format.ParseDict`. This keeps a single source of
truth (the .proto IDL) while preserving the team's preferred authoring
format.

Validation strategy:
- proto3 enforces field types and (with `ignore_unknown_fields=False`)
  rejects typos in keys.
- Semantic checks beyond what proto can express live in `validate_semantics`.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import yaml
from google.protobuf import json_format

# Generated proto module. When run under bazel, the import path comes from
# the py_proto_library target; under host venv, it resolves via PYTHONPATH
# pointing at the bazel-bin output.
from proto.eval.v1 import config_pb2

CURRENT_SCHEMA_VERSION = 1


class ConfigError(ValueError):
    """Raised on any malformed or semantically invalid config."""


def load_config(path: str | Path) -> tuple[config_pb2.EvalConfig, str]:
    """Load and validate a YAML config.

    Returns the parsed `EvalConfig` proto and the SHA-256 hex digest of
    the raw bytes (suitable for the manifest's `config_sha256` field).
    """
    p = Path(path)
    raw_bytes = p.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    try:
        data = yaml.safe_load(io.BytesIO(raw_bytes))
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top-level must be a mapping, got {type(data).__name__}")

    # Normalize enum-string fields to the proto's UPPER_SNAKE form so users
    # can write `endpoint.type: vertex_chat` instead of ENDPOINT_TYPE_VERTEX_CHAT.
    _normalize_enums(data)

    cfg = config_pb2.EvalConfig()
    try:
        json_format.ParseDict(data, cfg, ignore_unknown_fields=False)
    except json_format.ParseError as e:
        raise ConfigError(f"{p}: schema violation: {e}") from e

    validate_semantics(cfg, source=str(p))
    return cfg, sha256


def validate_semantics(cfg: config_pb2.EvalConfig, *, source: str = "<config>") -> None:
    """Checks that proto3 cannot express on its own."""
    errors: list[str] = []

    if cfg.schema_version != CURRENT_SCHEMA_VERSION:
        errors.append(
            f"schema_version={cfg.schema_version} unsupported "
            f"(this evalctl supports {CURRENT_SCHEMA_VERSION})"
        )
    if not cfg.run.name:
        errors.append("run.name is required")
    if not cfg.harness.version:
        errors.append("harness.version is required")
    if cfg.endpoint.type == 0:
        errors.append("endpoint.type is required (e.g. 'vertex_chat')")
    if not cfg.endpoint.model_id:
        errors.append("endpoint.model_id is required")
    if not cfg.tasks:
        errors.append("at least one task must be specified")
    for i, task in enumerate(cfg.tasks):
        if not task.name:
            errors.append(f"tasks[{i}].name is required")

    # vertex_chat needs an endpoint_id; local endpoints need a base_url.
    if cfg.endpoint.type == 1 and not cfg.endpoint.endpoint_id:  # VERTEX_CHAT
        errors.append("endpoint.endpoint_id is required when endpoint.type=vertex_chat")
    if cfg.endpoint.type in (2, 3) and not cfg.endpoint.base_url:  # LOCAL_CHAT/VLLM
        errors.append("endpoint.base_url is required for local endpoints")

    if errors:
        joined = "\n  - ".join(errors)
        raise ConfigError(f"{source}: semantic validation failed:\n  - {joined}")


# --- internal helpers ---------------------------------------------------------

# Map the lowercase user-facing form -> proto3 enum string. Centralized so
# we can extend without touching the loader.
_ENUM_NORMALIZATIONS: dict[tuple[str, ...], dict[str, str]] = {
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


def _normalize_enums(data: dict[str, Any]) -> None:
    """Mutate `data` in place to convert friendly enum strings to proto form."""
    for path, mapping in _ENUM_NORMALIZATIONS.items():
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
