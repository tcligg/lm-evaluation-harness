# evalctl

Developer CLI for the eval runner system. See `docs/eval-system-hld.md` for
the full design.

## Status

Phase 0 (skeleton). Supports:

- `evalctl validate <config.yaml>` — schema + semantic validation.
- `evalctl run <config.yaml> --local --dry-run` — print the lm_eval command.
- `evalctl run <config.yaml> --local` — execute via `lm_eval` in the current
  Python env, write `manifest.json` + `config.resolved.yaml` + `runner.log`
  + harness outputs to `/tmp/run/<run_id>/`.

Not yet implemented (later phases):

- Docker dispatch (Phase 2).
- GCS upload + BQ insert (Phase 3).
- ADC auth flow (Phase 4).
- Vertex Custom Job dispatch (Phase 5).
- `evalctl show` / `evalctl compare` (require Phase 3).

## Testing

There are two test tiers:

| Tier | Command | Deps | Coverage |
|---|---|---|---|
| **Smoke** (no proto) | `make smoke` | python, PyYAML | env allow-list, YAML+sha256, enum normalization, lm_eval argv translation. 25 unit tests + an end-to-end YAML→argv check. |
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

- **Proto mode** (preferred): `protoc`-generated `proto.eval.v1._pb2`
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
  evalctl            Bash launcher script (installed to $PREFIX/bin).
  _pure.py           Proto-free helpers (env allow-list, YAML loader,
                     enum normalization, lm_eval argv translation).
  cli.py             Typer entry, subcommands. Lazy proto imports with
                     pure-Python fallback when proto isn't available.
  config_loader.py   YAML -> EvalConfig (proto); delegates to _pure.
  manifest.py        Builds RunManifest (UUID, git, env allow-list).
  execution.py       EvalConfig -> lm_eval argv via _pure; runs locally.
  pure_test.py       Proto-free unit tests (25 cases).
  smoke_test.sh      4-step smoke driver (env, syntax, units, e2e).
```

Schema lives in `proto/eval/v1/`.
