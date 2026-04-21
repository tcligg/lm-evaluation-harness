#!/usr/bin/env bash
# Phase 0 smoke test for evalctl.
#
# Runs the proto-free unit suite + a hand-rolled YAML-to-argv check.
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
blue "[1/4] Environment check"
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
blue "[2/4] Syntax check (py_compile)"
python -m py_compile \
  tools/evalctl/__init__.py \
  tools/evalctl/__main__.py \
  tools/evalctl/_pure.py \
  tools/evalctl/cli.py \
  tools/evalctl/config_loader.py \
  tools/evalctl/execution.py \
  tools/evalctl/manifest.py
green "  all evalctl modules compile cleanly"

# ---------- 3. Proto-free unit suite -----------------------------------------
blue "[3/4] Proto-free unit tests (tools/evalctl/pure_test.py)"
if ! python -m unittest tools.evalctl.pure_test 2>&1; then
  red "unit tests FAILED"; exit 1
fi
green "  all proto-free tests pass"

# ---------- 4. End-to-end YAML -> argv translation ---------------------------
blue "[4/4] End-to-end: example YAML -> lm_eval argv"
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

echo
green "All smoke checks passed."
