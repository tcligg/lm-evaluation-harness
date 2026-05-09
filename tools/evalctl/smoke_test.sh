#!/usr/bin/env bash
# Phase 0 + Phase 1 smoke test for evalctl.
#
# Runs the proto-free unit suite (pure_test, repro_test, programmatic_test)
# plus an end-to-end YAML-to-argv check + a results-roundtrip check.
# Designed to work on a developer laptop with stdlib + PyYAML only — no
# protoc, no bazel, no network access. CI (Phase 2) runs the full
# `bazel test //...` suite, including the proto-coupled tests.
#
# Exit codes:
#   0 - all checks passed
#   1 - one or more checks failed
#   2 - environment problem (missing python or PyYAML)

set -euo pipefail

# Resolve repo root regardless of where this script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[34m%s\033[0m\n' "$*"; }

# ---------- 1. Environment check ---------------------------------------------
blue "[1/5] Environment check"
if ! command -v python >/dev/null 2>&1; then
  red "python not on PATH"; exit 2
fi
if ! python -c "import yaml" 2>/dev/null; then
  red "PyYAML not installed in the active python ($(which python))"
  echo "      Install with: pip install pyyaml"
  exit 2
fi
green "  python: $(python --version 2>&1)"
green "  PyYAML: $(python -c 'import yaml; print(yaml.__version__)')"

# ---------- 2. Syntax check --------------------------------------------------
blue "[2/5] Syntax check (py_compile)"
python -m py_compile \
  tools/evalctl/__init__.py \
  tools/evalctl/__main__.py \
  tools/evalctl/_pure.py \
  tools/evalctl/cli.py \
  tools/evalctl/config_loader.py \
  tools/evalctl/container.py \
  tools/evalctl/execution.py \
  tools/evalctl/manifest.py \
  tools/evalctl/programmatic.py \
  tools/evalctl/repro.py \
  tools/evalctl/runs.py
green "  all evalctl modules compile cleanly"

# ---------- 3. Proto-free unit suite -----------------------------------------
blue "[3/5] Proto-free unit tests (pure, repro, programmatic, container, runs)"
if ! python -m unittest \
  tools.evalctl.pure_test \
  tools.evalctl.repro_test \
  tools.evalctl.programmatic_test \
  tools.evalctl.container_test \
  tools.evalctl.runs_test 2>&1; then
  red "unit tests FAILED"; exit 1
fi
green "  all proto-free tests pass"

# ---------- 4. End-to-end YAML -> argv translation ---------------------------
blue "[4/5] End-to-end: example YAML -> lm_eval argv"
python - <<'PY'
import sys
from pathlib import Path

from tools.evalctl._pure import build_lm_eval_argv, load_yaml_with_sha

example = Path("examples/configs/gpqa_diamond_3shot_grok42.yaml")
data, sha = load_yaml_with_sha(example)
argv = build_lm_eval_argv(data, output_path="/tmp/run/smoke")

# Spot-check the translation matches what eval_rerun.log captured.
expected = {
    "model": "local-chat-completions",
    "model_args_contains": [
        "model=google/openmaas-2.0-test",
        "num_concurrent=32",
        "max_gen_toks=131072",
        "timeout=1800",
    ],
    "tasks": "gpqa_diamond_generative_n_shot",
    "num_fewshot": "3",
    "seed": "0,1234,1234,1234",
    "gen_kwargs": "until=<|eos|>",
    "log_samples_present": True,
    "include_path": "custom_tasks",
}

def at(flag):
    return argv[argv.index(flag) + 1]

failures = []
if at("--model") != expected["model"]:
    failures.append(f"--model: got {at('--model')!r}")
margs = at("--model_args")
for needle in expected["model_args_contains"]:
    if needle not in margs:
        failures.append(f"--model_args missing {needle!r} (got: {margs})")
if at("--tasks") != expected["tasks"]:
    failures.append(f"--tasks: got {at('--tasks')!r}")
if at("--num_fewshot") != expected["num_fewshot"]:
    failures.append(f"--num_fewshot: got {at('--num_fewshot')!r}")
if at("--seed") != expected["seed"]:
    failures.append(f"--seed: got {at('--seed')!r}")
if at("--gen_kwargs") != expected["gen_kwargs"]:
    failures.append(f"--gen_kwargs: got {at('--gen_kwargs')!r}")
if "--log_samples" not in argv:
    failures.append("--log_samples missing")
if at("--include_path") != expected["include_path"]:
    failures.append(f"--include_path: got {at('--include_path')!r}")
# Required for chat endpoints; without it the harness fails with
# "expects messages as list[dict]".
if "--apply_chat_template" not in argv:
    failures.append("--apply_chat_template missing (chat endpoint)")
if "--fewshot_as_multiturn" not in argv:
    failures.append("--fewshot_as_multiturn missing (chat endpoint)")

if failures:
    print("FAILED:")
    for f in failures:
        print("  -", f)
    print("\nFull argv:")
    print(" ", " ".join(argv))
    sys.exit(1)

print(f"  config_sha256: {sha}")
print(f"  argv length:   {len(argv)}")
print(f"  argv:          {' '.join(argv)}")
PY

green "  YAML -> argv translation OK"

# ---------- 5. Phase 1 round-trip: results.json -> config -> validate ---------
blue "[5/5] Phase 1: results.json -> repro -> validate roundtrip"
python - <<'PY'
import json, sys, tempfile
from pathlib import Path

from tools.evalctl.repro import reconstruct_config

# Synthesize a minimal results_*.json (mirrors what the harness writes).
fixture = {
    "results": {"gpqa_diamond_generative_n_shot": {
        "alias": "gpqa_diamond_generative_n_shot",
        "exact_match,strict-match": 0.4242,
    }},
    "configs": {"gpqa_diamond_generative_n_shot": {
        "task": "gpqa_diamond_generative_n_shot",
        "num_fewshot": 3,
        "metric_list": [{"metric": "exact_match"}],
    }},
    "n-samples": {"gpqa_diamond_generative_n_shot": {"original": 198, "effective": 198}},
    "config": {
        "model": "local-chat-completions",
        "model_args": "model=google/openmaas-2.0-test,base_url=https://x.aiplatform.googleapis.com/v1/projects/1/locations/us/endpoints/foo/chat/completions,num_concurrent=32,max_gen_toks=131072,timeout=1800",
        "gen_kwargs": {"until": "<|eos|>"},
        "random_seed": 0, "numpy_random_seed": 1234,
        "torch_random_seed": 1234, "fewshot_random_seed": 1234,
        "apply_chat_template": True, "fewshot_as_multiturn": True,
        "model_source": "local-chat-completions",
    },
    "lm_eval_version": "0.4.12.dev0",
}

with tempfile.TemporaryDirectory() as d:
    rp = Path(d) / "results.json"
    rp.write_text(json.dumps(fixture))
    yaml_str = reconstruct_config(rp, run_name="smoke_repro")

    cp = Path(d) / "reconstructed.yaml"
    cp.write_text(yaml_str)

    # The reconstructed YAML should pass the same semantic validation
    # the CLI applies in the pure-fallback path.
    sys.path.insert(0, ".")
    from tools.evalctl._pure import load_yaml_with_sha
    data, _ = load_yaml_with_sha(cp)

    # Manual semantic checks (mirrors cli._basic_pure_validate_or_die).
    assert data["schema_version"] == 1
    assert data["run"]["name"] == "smoke_repro"
    assert data["endpoint"]["type"] == "vertex_chat"
    assert data["endpoint"]["model_id"] == "google/openmaas-2.0-test"
    assert data["endpoint"]["endpoint_id"]
    assert data["tasks"][0]["num_fewshot"] == 3
    assert data["chat_template"]["mode"] == "MODE_AUTO"
    print(f"  reconstructed YAML: {len(yaml_str.splitlines())} lines")
    print(f"  endpoint_id:        {data['endpoint']['endpoint_id']}")
PY

green "  results.json roundtrip OK"

echo
green "All smoke checks passed."
