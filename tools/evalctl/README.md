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

## Build

```
make build    # bazel build //...
make test     # bazel test //...
```

## Run

```
make evalctl ARGS="validate examples/configs/gpqa_diamond_3shot_grok42.yaml"
make evalctl ARGS="run examples/configs/gpqa_diamond_3shot_grok42.yaml --local --dry-run"
```

Or directly:

```
bazel run //tools/evalctl:evalctl -- run path/to/config.yaml --local --dry-run
```

## Layout

```
tools/evalctl/
  cli.py             Typer entry, subcommands.
  config_loader.py   YAML -> EvalConfig (proto), with semantic validation.
  manifest.py        Builds RunManifest (UUID, git, env allow-list).
  execution.py       Translates EvalConfig -> lm_eval argv; runs locally.
```

Schema lives in `proto/eval/v1/`.
