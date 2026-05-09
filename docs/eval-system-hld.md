# High-Level Design: Eval Runner System

**Status:** Draft
**Owner:** tcli
**Last updated:** 2026-04-21 (rev 2 — proto/Bazel pivot)
**Implementation status:** see [`eval-system-plan.md`](./eval-system-plan.md)

## 1. Background

Today, evaluation runs against our Vertex-hosted models are executed ad-hoc: engineers invoke `lm_eval` from a shell, manage auth via a background `refresh_token.sh` loop, and dump outputs into hand-named directories (e.g. `groq-4p2-0309b-tp4-non-reason-gpqa-3shot-temp0-v2/`). There is no run manifest, no central result store, no leaderboard, and no reproducibility guarantee. Comparing two model versions requires reading JSON files by hand.

## 2. Goals

| ID | Goal |
|---|---|
| G1 | Every eval is reproducible from a single declarative config. |
| G2 | Every eval auto-generates rich provenance metadata. |
| G3 | Results are queryable in one place; raw artifacts are durably stored. |
| G4 | Engineers can compare runs and watch trends without writing code. |
| G5 | Local iteration stays fast; local and remote runs are first-class peers. |

## 3. Non-Goals

- Replacing `lm-evaluation-harness` itself; we wrap it.
- Building a custom eval UI (Looker Studio is sufficient initially).
- Supporting non-LM evals (vision, RL, etc.) in v1.
- Multi-tenant isolation beyond per-user GCP IAM.

## 4. Requirements Traceability

| Req | Component(s) |
|---|---|
| R1 Standardized execution | Docker image `eval-harness:<ver>`; `config.yaml` schema; in-container `eval_runner` |
| R2 Tracking & metadata | `manifest.json` emitter in `evalctl`; UUID propagated to all artifacts |
| R3 Centralized results | BigQuery dataset `eval_results`; GCS bucket `gs://eval-artifacts-prod/runs/<run_id>/` |
| R4 Visualization | Looker Studio dashboard over BQ views |
| R5 Local triggering | `evalctl run --local`; same image, same manifest, `execution_mode=local` tag |

## 5. Architecture Overview

```
            ┌───────────────────────────────────────────┐
            │  evalctl  (CLI, in lm-evaluation-harness) │
            │  parse + validate config.yaml             │
            │  build manifest (UUID, git, user, …)      │
            │  dispatch local | remote                  │
            └──────────────┬────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              │                         │
       ┌──────▼──────┐          ┌───────▼────────┐
       │ Local       │          │ Vertex AI       │
       │ docker run  │          │ Custom Job      │
       └──────┬──────┘          └───────┬────────┘
              │      same image:        │
              │  eval-harness:<ver>     │
              └────────────┬────────────┘
                           │
                ┌──────────▼───────────┐
                │  eval_runner (in     │
                │  container)          │
                │  ADC -> chat token   │
                │  lm_eval.simple_eval │
                │  emit + publish      │
                └──────────┬───────────┘
                           │
              ┌────────────┴────────────┐
       ┌──────▼──────┐          ┌───────▼────────┐
       │ GCS         │          │ BigQuery        │
       │ runs/<id>/  │          │ eval_runs       │
       │  manifest   │          │ eval_metrics    │
       │  results    │          │ eval_manifest_  │
       │  samples    │          │   raw           │
       └─────────────┘          └───────┬────────┘
                                        │
                                ┌───────▼────────┐
                                │ Looker Studio   │
                                │ Comparison +    │
                                │ Trend pages     │
                                └────────────────┘
```

## 6. Components

### 6.1 `evalctl` (CLI)
Lives at `tools/evalctl/`. Typer-based. Subcommands:
- `run <config.yaml> [--local] [--no-publish] [--dry-run] [--detach]`
- `validate <config.yaml>`
- `show <run_id>` / `compare <run_id_a> <run_id_b>`

Responsibilities: schema validation, manifest construction, image-digest resolution, dispatch to local Docker or Vertex Custom Job. Imports generated proto classes from `proto/eval/v1/`. Bazel target: `//tools/evalctl:evalctl`.

### 6.2 `eval_runner` (in-container entrypoint)
Lives at `tools/eval_runner/`. Re-validates the config (defence in depth), translates it into `lm_eval.simple_evaluate(...)` arguments, executes the eval in-process, then publishes artifacts to GCS and rows to BigQuery. Idempotent on `run_id`. Imports `config_pb2`, `manifest_pb2`, `metrics_pb2` from `eval.v1`. Bazel target: `//tools/eval_runner:eval_runner`.

### 6.3 Container image
`docker/Dockerfile`, tag `eval-harness:<harness_version>-<wrapper_git_sha>`, pushed to Artifact Registry by CI on every merge to main. Contains pinned harness, `custom_tasks/`, the runner, and gcloud SDK.

### 6.4 Auth
Application Default Credentials (ADC) inside the container. Local: mount `~/.config/gcloud`. Remote: Vertex Custom Job runs as a dedicated service account. `eval_runner.auth.get_chat_token()` exchanges ADC for a bearer token, refreshes on 401. Replaces `refresh_token.sh`.

### 6.5 Storage
- **GCS:** `gs://eval-artifacts-prod/runs/<run_id>/` holds `manifest.json`, `config.resolved.yaml`, `results_<ts>.json`, `samples_<task>_<ts>.jsonl`, `runner.log`.
- **BigQuery dataset `eval_results`:** three tables (see §8).

### 6.6 Leaderboard
Looker Studio dashboard over a saved BQ view (`v_latest_metrics`). Two pages: Comparison (run-vs-run delta with stderr-aware significance) and Trend (time series per `(model_id, task, metric)`).

### 6.7 Build & Codegen
Bazel-based monorepo build (bzlmod, pending Day-0 confirmation; see §17). Layout:

- `proto/eval/v1/` — `config.proto`, `manifest.proto`, `metrics.proto`, `common.proto` exposed via `proto_library` + `py_proto_library`.
- BQ table schemas generated by a `genrule` invoking `protoc-gen-bq-schema` against `metrics.proto`; outputs land in `schemas/bq/`.
- `tools/evalctl/` and `tools/eval_runner/` are `py_binary` targets depending on the generated proto libraries.
- `docker/Dockerfile` is multi-stage: `bazel build //...` produces wheels + generated assets, copied into the runtime image.
- CI gates on `bazel test //...`. Bazel version pinned via `.bazelversion`.
- `make build` shim wraps `bazel build //...` for devs unfamiliar with Bazel.

## 7. Data Model

### 7.1 `config.yaml` (input)
```yaml
schema_version: 1
run: { name, tags{} }
harness: { version }
endpoint: { type, model_id, endpoint_id, base_url?, auth{} }
tasks: [ { name, num_fewshot, metrics[] } ]
model_args: { num_concurrent, max_gen_toks, timeout }
sampling_params: { temperature, top_p, max_tokens }
gen_kwargs: { ... }
prompt_template: <id>           # resolved against tools/evalctl/templates/
seed: [int]
limits: { per_task? }
output: { gcs_bucket, bq_dataset }
```
Defined in `proto/eval/v1/config.proto`. YAML configs are loaded as dicts and parsed via `google.protobuf.json_format.ParseDict`. Free-form fields (`gen_kwargs`) use `google.protobuf.Struct` to preserve harness pass-through behavior.

### 7.2 `manifest.json` (output)
Defined in `proto/eval/v1/manifest.proto`. Serialized to GCS as proto3 JSON; filename `manifest.json` retained for human readability and `jq` ergonomics. The example below shows the proto3 JSON form:

```json
{
  "run_id": "<uuid4>",
  "schema_version": 1,
  "name": "...",
  "user_id": "...",
  "timestamp_utc": "...",
  "execution_mode": "local|remote",
  "git": { "wrapper_commit", "wrapper_dirty",
           "harness_version", "harness_commit" },
  "container": { "image", "digest" },
  "env": { "allow_listed_vars": {} },
  "config_sha256": "...",
  "endpoint": { "type", "model_id", "endpoint_id" },
  "tags": {},
  "status": "running|success|failed|partial",
  "vertex_job_id": "..."
}
```

## 8. BigQuery Schemas

Schemas are Bazel-generated artifacts of `proto/eval/v1/metrics.proto` via `protoc-gen-bq-schema`. Checked-in copies under `schemas/bq/*.json` exist as reference for dashboard authors and are regenerated on every proto change. Partitioning and clustering hints are expressed as proto field options.

**`eval_runs`** (one row per run): `run_id PK`, `name`, `status`, `user_id`, `timestamp_utc`, `execution_mode`, `model_id`, `endpoint_id`, `harness_version`, `wrapper_commit`, `image_digest`, `config_sha256`, `gcs_uri`, `tags JSON`, `total_seconds`. Partitioned on `DATE(timestamp_utc)`, clustered on `model_id`.

**`eval_metrics`** (long format): `run_id FK`, `task`, `metric`, `filter`, `score`, `stderr`, `n_samples`, `higher_is_better`. Clustered on `model_id, task` via join with `eval_runs`.

**`eval_manifest_raw`**: `run_id`, `manifest JSON`, `ingested_at`. Forensic lookup.

## 9. Execution Flow

1. Engineer runs `evalctl run cfg.yaml [--local]`.
2. `evalctl` validates config, mints `run_id`, builds manifest, resolves image digest.
3. Dispatch:
   - `--local`: `docker run` the image with config + manifest mounted.
   - default: submit Vertex Custom Job; config + manifest staged to `gs://…/runs/<run_id>/`.
4. In-container `eval_runner` exchanges ADC for chat-endpoint token, calls `lm_eval.simple_evaluate(...)`, writes artifacts to a working dir (`/tmp/run/<run_id>/` — container-local and ephemeral; persistent storage is GCS only).
5. On completion: upload working dir to GCS, MERGE rows into `eval_runs` + `eval_metrics`, patch `status`.
6. Looker Studio reflects the run within seconds via `v_latest_metrics`.

## 10. Local vs Remote Parity

| Aspect | Local | Remote |
|---|---|---|
| Image | same `eval-harness:<ver>` | same |
| Entrypoint | `eval_runner` | `eval_runner` |
| Manifest schema | identical | identical |
| `execution_mode` | `local` | `remote` |
| Auth | ADC (mounted `~/.config/gcloud`) | Vertex SA + Workload Identity |
| Publish | on by default; `--no-publish` opt-out | always on |
| Endpoint | `local_chat`, `local_vllm`, `hf` allowed | `vertex_chat` |

This satisfies R5: same artifact shape, filterable by `execution_mode`.

## 11. Security & IAM

- Service account for remote runs: `aiplatform.endpoints.predict`, `bigquery.dataEditor` on `eval_results`, `storage.objectAdmin` on the artifact bucket only.
- Manifest captures only an allow-listed set of env vars to prevent secret leakage.
- GCS bucket: uniform bucket-level access; object versioning on for audit.
- Container image scanned by Artifact Registry on push.

## 12. Observability

- `runner.log` per run, uploaded with artifacts.
- `eval_runs.status` is the canonical liveness signal; `running` rows older than 4h are flagged stale by a scheduled BQ check.
- Vertex Custom Job logs flow to Cloud Logging, linked from `eval_runs` via `vertex_job_id` (added to manifest for remote runs).

## 13. Failure Modes

| Failure | Behavior |
|---|---|
| Endpoint 5xx mid-run | Harness retries (existing); on persistent failure, runner marks `partial` with per-task error notes. |
| BQ insert flake | Idempotent `MERGE` on `run_id`; runner writes `published.marker` before exit; restarts skip republish. |
| Image digest mismatch | Runner asserts `harness_version` against vendored harness on startup; refuses to run on mismatch. |
| GCS upload partial | Runner uploads atomically (temp prefix → rename); BQ rows only inserted after upload completes. |
| Local user lacks Docker | `--no-container` escape hatch (host venv, manifest still emitted, `image_digest=null`). Off by default. |

## 14. Migration Plan

Phased over ~3 weeks, tracked in §15. The cutover step requires (a) one historical run reproduced bit-for-bit from a config, (b) leaderboard parity reviewed by the team, and (c) README deprecating ad-hoc CLI usage with a flag-to-config mapping table.

## 15. Phasing

| Phase | Scope | Est. |
|---|---|---|
| 0 | `evalctl` skeleton, **proto IDL (`config.proto`, `manifest.proto`, `metrics.proto`, `common.proto`) + Bazel workspace + BUILD files**, manifest emitter, host-Python execution | 2–3d |
| 1 | Reproduce one historical run from a config | 2d |
| 2 | Dockerfile + CI + Artifact Registry push | 2d |
| 3 | GCS + BQ sinks, Terraform/`bq mk` for schemas | 2–3d |
| 4 | ADC auth, retire `refresh_token.sh`, `--local` Docker path | 2d |
| 5 | Vertex Custom Job dispatcher + IAM | 3–4d |
| 6 | Looker Studio dashboard (Comparison + Trend) | 2d |
| 7 | Migrate active sweeps; deprecate ad-hoc CLI | 2d |

## 16. Risks

- **Looker comparison ergonomics.** Side-by-side diff with sample drill-through is awkward; mitigation is a future Streamlit fallback if the team requests it after ~1 month of use.
- **Harness pinning drift.** Mitigated by image-digest assertions and `harness_version` echoed in both config and manifest.
- **Sample file size.** Hundreds of MB possible; kept in GCS only, never BQ; size recorded in manifest for dashboard flagging.
- **Engineer adoption.** Mitigated by shipping a working `examples/configs/` set covering current sweeps and a flag→config migration table.
- **Bazel adoption curve.** Mitigation: 1-page bootstrap doc, `make build` shim, pin Bazel via `.bazelversion`.
- **Proto schema migrations.** Mitigation: enforce `reserved` field numbers on deletion; both SWEs must approve any change to `proto/eval/v1/`; package versioned `v1` from day one to make a future `v2` cheap.

## 17. Open Items

- Terraform vs. one-off `bq mk` scripts for BQ/GCS provisioning — depends on whether the team has an IaC story already.
- Whether to track managed-service version changes (e.g. underlying Vertex model bumps) as a first-class `service_versions` table now or defer.
- Retention policy for `samples_*.jsonl` in GCS (default proposal: 180d nearline → 1y coldline → delete; needs sign-off).
- Bzlmod vs. legacy WORKSPACE for Bazel bootstrap (lean toward bzlmod).

## 18. Appendix: CLI → config.yaml Migration

| Old `lm_eval` CLI | `config.yaml` |
|---|---|
| `--model local-chat-completions` | `endpoint.type: vertex_chat` |
| `--model_args model=…,base_url=…` | `endpoint.model_id`, `endpoint.endpoint_id` |
| `--model_args num_concurrent=…,max_gen_toks=…,timeout=…` | `model_args.{...}` |
| `--tasks gpqa_diamond_…,mmlu_pro` | `tasks[].name` + per-task `num_fewshot` |
| `--gen_kwargs until=<\|eos\|>` | `gen_kwargs.until` |
| `--seed 0,1234,1234,1234` | `seed: [...]` |
| `--include_path custom_tasks` | implicit |
| `--output_path …` | implicit (`/tmp/run/<run_id>/`) |
