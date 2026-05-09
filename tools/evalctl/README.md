# evalctl

Developer CLI for MERIT (Model Evaluation, Reproducibility, and Tracking). See `docs/merit-hld.md` for
the full design and `docs/merit-plan.md` for current implementation
status (which phases shipped, which are pending, what's blocked).

## Status

Phases 0 + 1 + 2 complete.

**Phase 0 (skeleton):**

- `evalctl validate <config.yaml>` — schema + semantic validation.
- `evalctl run <config.yaml> --local --dry-run` — print what would execute.
- `evalctl run <config.yaml> --local --subprocess` — Phase 0's CLI shell-out.
- Manifest, config.resolved.yaml, runner.log written to `/tmp/run/<run_id>/`.

**Phase 1 (reproducibility):**

- `evalctl run ... --local` (default) — programmatic in-process dispatch via
  `lm_eval.simple_evaluate(...)`. Lifts the Phase 0 limitation around per-task
  `num_fewshot` (each distinct value gets its own `simple_evaluate` call,
  results merged).
- `evalctl run ... --limit N` — cap docs per task (smoke convenience).
- `evalctl repro <results_*.json> --out cfg.yaml` — reconstruct an evalctl
  config from a historical harness output. Infers endpoint type/id from the
  recorded `model_args`, preserves per-task `num_fewshot`, emits a
  `chat_template:` block when needed.
- `evalctl diff <a.json> <b.json> [--score-tol] [--n-samples-tol]` —
  structural + numeric comparison of two `results_*.json` files. Exit 0 on
  match, exit 3 on drift. Used to gate "this change doesn't perturb
  historical sweeps."

**Phase 2 (containerization + CI):**

- `docker/Dockerfile` builds a multi-stage runtime image. Tag convention:
  `merit:<harness_version>-<wrapper_git_sha>`.
- `make docker-build` / `docker-push` / `docker-run` / `docker-shell`.
  Override registry with `IMAGE_REGISTRY=us-docker.pkg.dev/<proj>/eval`.
- `evalctl run ... --local --container [--image <ref>]` — dispatch via
  `docker run` against the pinned image. Mounts the config + a workdir,
  passes the image ref/digest into the container so the manifest records
  it (R1, R2).
- Manifest auto-detects `container.image` + `container.digest` from
  `MERIT_IMAGE_REF` / `MERIT_IMAGE_DIGEST` env vars set by the image.
- GitHub Actions workflow (`.github/workflows/evalctl-ci.yml`) runs the
  smoke tier on every push/PR plus a docker build smoke; the proto-coupled
  bazel tier runs on push (skipped on fork PRs). Artifact Registry push
  is stubbed and gated behind WIF secrets.

Not yet implemented (later phases):

- GCS upload + BQ insert (Phase 3).
- ADC auth flow (Phase 4).
- Vertex Custom Job dispatch (Phase 5).
- `evalctl show` / `evalctl compare` (require Phase 3).

### Pre-requisites for an actual `--local` run against a Vertex endpoint

Phase 0 dispatches `lm_eval` but doesn't yet handle auth. To actually
hit a Vertex chat-completions endpoint:

1. **Replace `REPLACE_ME` in your config** with the real endpoint resource
   ID (look in Vertex AI console → Endpoints).

2. **Export an OAuth bearer token** that `lm_eval`'s `local-chat-completions`
   adapter can use. The team's existing pattern:

   ```bash
   ./refresh_token.sh &              # background loop, refreshes /tmp/gcloud_token.txt
   export OPENAI_API_KEY="$(cat /tmp/gcloud_token.txt)"
   evalctl run examples/configs/<your-config>.yaml --local
   ```

   This is intentionally manual until Phase 4 wires up ADC-based token
   exchange. If `OPENAI_API_KEY` is empty the harness retries with
   `Authorization: Bearer ` (empty) and gets 401s.

3. **Chat endpoints require `chat_template`.** Configs targeting
   `vertex_chat` or `local_chat` must enable chat templating (see the
   `chat_template:` section in `examples/configs/gpqa_diamond_3shot_grok42.yaml`).
   Without it, `lm_eval` raises:

   ```
   AssertionError: LocalChatCompletion expects messages as list[dict].
   ```

   `evalctl` defaults `chat_template.mode = AUTO` for chat endpoints, so
   you only need to set this if you want to override.

## Phase 1: Reproducibility procedure

The intended workflow for validating that an `evalctl` config reproduces
a historical sweep:

```bash
# 1. Reconstruct an evalctl config from the historical results.
evalctl repro path/to/historical/results_2026-03-24.json \
  --out /tmp/repro_cfg.yaml --name gpqa_repro

# 2. Inspect / edit the reconstructed config (TODO comments highlight
#    fields that don't round-trip, e.g. auth, gcs_bucket).
$EDITOR /tmp/repro_cfg.yaml

# 3. Re-run with --limit for a smoke test first, then full.
evalctl run /tmp/repro_cfg.yaml --local --limit 10
evalctl run /tmp/repro_cfg.yaml --local

# 4. Diff the new results against the historical reference.
evalctl diff path/to/historical/results_2026-03-24.json \
             /tmp/run/<run_id>/results_*.json \
             --score-tol 0.02 --n-samples-tol 0
```

Exit codes from `evalctl diff`: `0 = match`, `3 = drift`. Use this in CI
to gate "do not perturb the historical sweeps" guarantees.

## Testing

There are two test tiers:

| Tier | Command | Deps | Coverage |
|---|---|---|---|
| **Smoke** (no proto) | `make smoke` | python, PyYAML | env allow-list, YAML+sha256, enum normalization, lm_eval argv translation, chat-template defaulting, results-roundtrip (Phase 1), programmatic dispatcher with mocked lm_eval, container dispatcher with mocked docker (Phase 2). 70 unit tests + 2 end-to-end checks. |
| **Full** (proto-coupled) | `make test` | bazel, protoc | Adds config_loader_test (proto roundtrip) and manifest_test (proto-typed RunManifest builder). |

Run the smoke tier on any dev laptop without bazel/protoc:

```
make smoke              # ./tools/evalctl/smoke_test.sh
```

Or invoke the unit suite directly:

```
python -m unittest tools.evalctl.pure_test
```

The proto-coupled tier runs in CI (Phase 2 wires up `bazel test //...` on
every PR). It exercises the same logic but through the real generated
protobuf classes, catching schema/IDL drift the smoke tier can't see.

### Why two tiers?

Corp Airlock blocks `pip install` on dev machines, and `protoc` isn't in
the apt repo. Splitting the proto-coupled logic from the proto-free
logic lets developers run a meaningful smoke check before pushing,
without waiting on CI for every YAML edit. The `_pure.py` module is the
single source of truth for argv translation and security-critical helpers
(env allow-list); the proto-coupled modules call into it.

## Install

For everyday use, install the launcher onto your `PATH`:

```
make install                       # symlinks $HOME/.local/bin/evalctl -> this checkout
make install PREFIX=/some/where    # alt prefix; binary at $PREFIX/bin/evalctl
make uninstall                     # remove the launcher
```

If `~/.local/bin` isn't on your `$PATH`, add it (e.g. in `~/.bashrc`):

```
export PATH="$HOME/.local/bin:$PATH"
```

The launcher hard-codes `EVALCTL_REPO=<this checkout>`, so `evalctl ...`
works from any cwd. Pulling in this repo only updates the source; no
re-install needed.

### Proto / no-proto modes

`evalctl` runs in two modes depending on whether the generated protobuf
modules are importable:

- **Proto mode** (preferred): `protoc`-generated `proto.merit.v1._pb2`
  modules are on the import path (typically via `bazel run` or after
  `make proto`). Strict proto3 field-name validation, full BQ/manifest
  schema parity. This is what CI runs.
- **Pure-Python fallback**: triggered automatically when `protobuf` or
  the generated modules aren't importable. Same on-the-wire behavior
  (manifest layout, lm_eval argv) but **field-name typos won't be
  caught**. Useful on dev laptops behind Corp Airlock.

You'll see a yellow note at the top of the output when the fallback
kicks in:

```
Note: protobuf not importable (...); using pure-Python fallback.
```

## Build

```
make build    # bazel build //...
make test     # bazel test //...
make smoke    # proto-free smoke check (no bazel needed)
```

## Run

After `make install`:

```
evalctl validate examples/configs/gpqa_diamond_3shot_grok42.yaml
evalctl run examples/configs/gpqa_diamond_3shot_grok42.yaml --local --dry-run
evalctl run examples/configs/gpqa_diamond_3shot_grok42.yaml --local
```

Without install:

```
make evalctl ARGS="validate examples/configs/gpqa_diamond_3shot_grok42.yaml"
bazel run //tools/evalctl:evalctl -- run path/to/config.yaml --local --dry-run
python -m tools.evalctl run path/to/config.yaml --local --dry-run
```

## Layout

```
tools/evalctl/
  evalctl              Bash launcher script (installed to $PREFIX/bin).
  _pure.py             Proto-free helpers (env allow-list, YAML loader,
                       enum normalization, lm_eval argv translation,
                       chat-template defaulting).
  cli.py               Typer entry, subcommands. Lazy proto imports with
                       pure-Python fallback when proto isn't available.
  config_loader.py     YAML -> EvalConfig (proto); delegates to _pure.
  manifest.py          Builds RunManifest (UUID, git, env allow-list,
                       container image+digest from $MERIT_IMAGE_*).
  execution.py         EvalConfig -> lm_eval argv via _pure; subprocess
                       dispatch (Phase 0 fallback).
  programmatic.py      Phase 1: in-process lm_eval.simple_evaluate dispatch.
                       Per-task num_fewshot via call grouping.
  repro.py             Phase 1: results.json -> EvalConfig and
                       diff_results(a, b) for drift detection.
  container.py         Phase 2: docker run dispatcher.
  pure_test.py         Proto-free unit tests for _pure (33 cases).
  repro_test.py        Proto-free unit tests for repro (20 cases).
  programmatic_test.py Proto-free unit tests for programmatic (10 cases).
  container_test.py    Proto-free unit tests for container (7 cases).
  smoke_test.sh        5-step smoke driver (env, syntax, units, e2e, repro).

docker/
  Dockerfile           Multi-stage runtime image (Phase 2).
.github/workflows/
  evalctl-ci.yml       Smoke + docker + bazel CI (Phase 2).
```

Schema lives in `proto/merit/v1/`.
