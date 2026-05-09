# MERIT — Implementation Plan & Status

**MERIT — Model Evaluation, Reproducibility, and Tracking**

**Companion to:** [`merit-hld.md`](./merit-hld.md)
**Status:** Active — Phase 2 complete, Phase 3 next
**Owner:** tcli
**Last updated:** 2026-05-09 (rev 4 — annotation system + parallelization map)

This file tracks **actual** delivery against the phased plan in HLD §15.
Update on every commit that closes a phase task.

---

## Annotation legend

Every task row carries a tag set so you can filter by SWE, dependency,
or tier without reading prose:

| Tag | Meaning | Values |
|---|---|---|
| **Status** | Where the task stands | ✅ shipped • 🔜 in-progress • ⏳ pending • ⚠️ blocked |
| **Owner** | Who's on the hook | `@SWE-1` `@SWE-2` `@joint` `@unassigned` |
| **Track** | Workstream | `[A]` runner • `[B]` platform • `[J]` joint (touches shared file) • `[I]` infra |
| **Tier** | Parallelization tier | `T0` blocking • `T1` parallel after T0 • `T2` parallel after T1 • `T3` final |
| **Effort** | Calendar estimate | `S` ≤1d • `M` 1–3d • `L` 3–5d • `XL` >5d |
| **Deps** | Hard dependencies | phase numbers / commit SHAs |
| **Commits** | Shipped work | git short SHAs |

Example annotation: `✅ @SWE-1 [A] T1 M  deps: P0  commits: 0e3ffc8`

---

## Parallelization map

```
                              Phase 0  (T0)
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
        Phase 1 (T1)        Phase 2 (T1)        Phase 3 (T1)
        [A] runner          [B] platform        [B] platform
              │                   │                   │
              │                   │           ┌───────┴───────┐
              │                   │           ▼               ▼
              │                   │     Phase 4 (T2)    Phase 6 (T2)
              │                   │     [B] platform   [B] dashboard
              │                   │           │               │
              │                   └───────────┤               │
              │                               ▼               │
              │                         Phase 5 (T3)          │
              │                         [A] runner            │
              │                               │               │
              └───────────────────────────────┼───────────────┘
                                              ▼
                                        Phase 7 (T3)
                                        [J] migration
```

**Key insight:** after Phase 0 (the T0 blocker) lands, **Phases 1, 2, and
3 can all run in parallel** because they touch nearly disjoint files.
Phase 6 (Looker dashboard) can start as soon as Phase 3 has produced one
real BQ row — it's UI configuration, not code in this repo.

**Hard serial chains:**
- Phase 5 needs Phases 2 (image) + 3 (publish path) + 4 (auth).
- Phase 7 needs Phases 5 (remote dispatch) + 6 (dashboard) for full cutover.

**Recommended 2-SWE assignment** (assuming Phases 0–2 already shipped):

| Calendar | SWE-1 (Track A) | SWE-2 (Track B) |
|---|---|---|
| Wk 1 | Phase 3 (sinks) | Phase 4 (ADC auth) |
| Wk 2 | Phase 5 prep (Vertex dispatcher) | Phase 6 (Looker dashboard) |
| Wk 3 | Phase 5 finish + own half of Phase 7 | Own other half of Phase 7 |

Cuts calendar from 3 weeks (serial) to ~2.5 weeks (parallel). The only
shared files are `cli.py` (each adds subcommands) and `BUILD.bazel`
(each adds targets) — mechanical conflicts, no design overlap.

---

## At-a-glance

| Phase | Scope | Status | Owner | Track | Tier | Deps | Commits |
|---|---|---|---|---|---|---|---|
| HLD | Design doc | ✅ | @joint | [J] | T0 | — | `48911c6` |
| 0 | `evalctl` skeleton, proto IDL, Bazel workspace, manifest | ✅ | @joint | [J] | T0 | HLD | `eb962db` `e8b603e` `6d0e33c` `f9ab799` |
| 1 | Programmatic dispatch, per-task num_fewshot, repro/diff | ✅ | @SWE-1 | [A] | T1 | P0 | `0e3ffc8` |
| 2 | Dockerfile, container dispatch, GitHub Actions CI | ✅ | @SWE-2 | [B] | T1 | P0 | `4f28c0e` `d0daf99` `09431c3` |
| Rename | System named MERIT; proto/Bazel/image/env-var renames | ✅ | @joint | [J] | T1 | P2 | `3f03e2c` `af21d8d` |
| Lookup | `evalctl show / compare / ls` against local artifacts | ✅ | @SWE-1 | [A] | T1 | P1 | (this branch) |
| 3 | GCS upload + BQ insert (sinks) | 🔜 | @unassigned | [B] | T1 | P0 | — |
| 4 | ADC auth, retire `refresh_token.sh` | ⏳ | @unassigned | [B] | T2 | P3 | — |
| 5 | Vertex Custom Job dispatcher | ⏳ | @unassigned | [A] | T3 | P2+P3+P4 | — |
| 6 | Looker Studio dashboard | ⏳ | @unassigned | [B] | T2 | P3 (1 row) | — |
| 7 | Migrate active sweeps; deprecate ad-hoc CLI | ⏳ | @unassigned | [J] | T3 | P5+P6 | — |

---

## What works end-to-end today

After `make install` and `make docker-build`:

```bash
evalctl validate <config>                              # schema + semantic check
evalctl run <config> --local --dry-run                 # print what would execute
evalctl run <config> --local                           # in-process via simple_evaluate
evalctl run <config> --local --container --image <ref> # inside the pinned image
evalctl run <config> --local --limit N                 # smoke cap on docs/task
evalctl repro <results.json> --out cfg.yaml            # reverse-engineer a config
evalctl diff a.json b.json [--score-tol] [--n-samples-tol]   # drift gate
evalctl show <run_id> [--json]                         # inspect a stored run
evalctl compare <run_id_a> <run_id_b>                  # diff two runs by id
evalctl ls [-n 20]                                     # list recent local runs
make smoke                                             # unit tests, ~50ms
```

Container-recorded provenance (HLD R2) verified via a real run:

```json
{
  "container": {
    "image": "local/merit:latest",
    "digest": "sha256:c8f013cf..."
  },
  "config_sha256": "dca546a8d2216ce912076e1cf3447c2ea571c7f0b1769e2d3beb3281502f789e",
  ...
}
```

---

## Phase 0 — `evalctl` skeleton

**Annotation:** ✅ @joint [J] T0 L  deps: HLD

| Item | Owner | Effort | Commit |
|---|---|---|---|
| Proto IDL (`config.proto`, `manifest.proto`, `metrics.proto`, `common.proto`) | @joint | M | `eb962db` |
| Bazel bzlmod workspace, `MODULE.bazel`, BUILD files | @joint | S | `eb962db` |
| Typer CLI: `run` / `validate` / `show` / `compare` | @joint | M | `eb962db` |
| Manifest builder (UUID, git SHA, env allow-list) | @joint | S | `eb962db` |
| Example config (port of historical sweep) | @joint | S | `eb962db` |
| Proto-free smoke tier + 11 initial unit tests | @joint | M | `e8b603e` |
| Installable launcher (`make install`) + lazy proto imports | @joint | S | `6d0e33c` |
| Chat-template defaulting (`--apply_chat_template` for chat endpoints) | @joint | S | `f9ab799` |

---

## Phase 1 — Reproducibility (Programmatic + repro/diff)

**Annotation:** ✅ @SWE-1 [A] T1 M  deps: P0  commits: `0e3ffc8`

| Item | Owner | Effort | Notes |
|---|---|---|---|
| Programmatic dispatch via `lm_eval.simple_evaluate()` | @SWE-1 | M | replaces subprocess shell-out |
| Per-task `num_fewshot` (call-grouping) | @SWE-1 | S | lifts Phase 0 limitation |
| `--limit N` flag | @SWE-1 | S | smoke convenience |
| `evalctl repro <results.json> --out cfg.yaml` | @SWE-1 | M | reverse-engineer config from harness output |
| `evalctl diff <a.json> <b.json>` | @SWE-1 | S | structural + numeric comparison; exits 3 on drift |
| 30 new unit tests (`pure_test`, `repro_test`, `programmatic_test`) | @SWE-1 | M | mocks `lm_eval` via `sys.modules` |

**Deferred to follow-up:** real reproducibility A/B against an actual historical
sweep — original `groq-4p2-…` result dirs were untracked and lost before Phase 0
landed. Once a real `results_*.json` is checked in or accessible via gs://, the
existing pipeline validates it.

---

## Phase 2 — Containerization + CI

**Annotation:** ✅ @SWE-2 [B] T1 L  deps: P0  commits: `4f28c0e` `d0daf99` `09431c3`

| Item | Owner | Effort | Notes |
|---|---|---|---|
| `docker/Dockerfile` (multi-stage) | @SWE-2 | M | builder runs `protoc` via grpcio-tools; runtime adds gcloud SDK |
| Image tag convention `merit:<harness>-<wrapper_sha>` | @SWE-2 | S | built locally; AR push stub commented out |
| Container dispatch (`evalctl run --local --container [--image]`) | @SWE-2 | M | `tools/evalctl/container.py` |
| Manifest auto-detects `container.image` + `container.digest` from `MERIT_IMAGE_*` env | @SWE-2 | S | `manifest.py` |
| Latent enum bug fix (`config_pb2.EndpointType` → `common_pb2.EndpointType`) | @SWE-2 | S | surfaced once proto path was actually exercised |
| `_run_with_proto` writes `manifest.json` + `config.resolved.yaml` | @SWE-2 | S | parity with `_run_pure` |
| HF/OpenAI auth env passthrough into container | @SWE-2 | S | mounts `~/.cache/huggingface` read-only |
| Secret redaction in dispatch logs | @SWE-2 | S | `_redact_argv()` masks 7 known secret env-var values |
| GitHub Actions CI (`.github/workflows/merit-ci.yml`) | @SWE-2 | M | smoke + docker + bazel jobs |
| 12 container_test cases | @SWE-2 | S | mocks `subprocess.run` |

**Image stats:** ~501 MB compressed / 2.63 GB uncompressed. Biggest contributor
is gcloud SDK (Phase 4 anchor). Image rebuild after a code change is ~60s
(deps layer cached).

**Outstanding:** Artifact Registry push job is stubbed; needs WIF secrets
(`GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`) before the
`docker-push` job can be uncommented.

---

## Lookup — `evalctl show / compare / ls`

**Annotation:** ✅ @SWE-1 [A] T1 S  deps: P1

Brings the HLD's "everything joins on `run_id`" guarantee to the CLI now,
without waiting for Phase 3. Subcommands resolve `run_id` via a backend
chain (local artifacts now; GCS + BQ stubs that activate in Phase 3).

| Item | Owner | Effort | Notes |
|---|---|---|---|
| `tools/evalctl/runs.py` resolver (`find_run_by_id`, `list_local_runs`) | @SWE-1 | M | local backend live; GCS / BQ stubs defer until Phase 3 |
| `evalctl show <run_id> [--json]` | @SWE-1 | S | manifest + metric summary; --json for machine consumption |
| `evalctl compare <a> <b>` | @SWE-1 | S | resolves both ids, calls `diff_results` |
| `evalctl ls [-n N]` | @SWE-1 | S | newest-first list; needed because users can't memorize uuids |

---

## Phase 3 — Centralized result store (next up)

**Annotation:** 🔜 @unassigned [B] T1 L  deps: P0

Per HLD R3 / §6.5 / §8.

### Scope

| Item | Effort | Notes |
|---|---|---|
| `tools/merit_runner/publish.py`: GCS upload of run scratch dir | M | Atomic via temp prefix + rename. Idempotent on `run_id`. |
| BigQuery `MERGE` into `merit_results.{merit_runs, merit_metrics, merit_manifest_raw}` | M | Idempotent on `run_id`. Runner writes `published.marker` on success. |
| BQ schemas via `protoc-gen-bq-schema` from `proto/merit/v1/metrics.proto` | S | Emit `schemas/bq/*.json`, check in. |
| Provisioning helpers: `bq mk` script + optional Terraform stubs | S | HLD §17 open item: choose Terraform vs scripts. |
| `evalctl run --no-publish` becomes meaningful | S | Was a no-op. |
| Activate GCS + BQ backends in `runs.py` resolver | S | Stubs already in place — just fill in. |
| `merit_runs` view (`v_latest_metrics`) for Looker Studio in Phase 6 | S | One SQL view. |

### Open questions before kickoff
- Terraform vs. one-off `bq mk` scripts (HLD §17 Open Items).
- Real GCP project + bucket name to hard-code in defaults (or env-var lookup).

---

## Phase 4 — ADC auth

**Annotation:** ⏳ @unassigned [B] T2 M  deps: P3

Per HLD §6.4. Replaces `refresh_token.sh` with in-process ADC token exchange
inside the container. Tested via a real Vertex chat endpoint.

| Item | Effort | Notes |
|---|---|---|
| `merit_runner.auth.get_chat_token()` | S | `google.auth.default()` + lazy refresh on 401 |
| Drop `refresh_token.sh` from the team's workflow | S | README + migration guide update |
| IAM bindings doc: SA + roles | S | aiplatform.endpoints.predict, bigquery.dataEditor, storage.objectAdmin |
| Local fallback (host gcloud) docs + smoke | S | Already mounted in Phase 2 container; just exercise it |
| Non-root container `USER` for production | S | Deferred from Phase 2 |

---

## Phase 5 — Vertex Custom Job dispatcher

**Annotation:** ⏳ @unassigned [A] T3 L  deps: P2 + P3 + P4

Per HLD §6.1. `evalctl run` without `--local` submits a Vertex Custom Job using
the Phase 2 image; the in-container runner reads the config from GCS and
writes results back via the Phase 3 publisher.

| Item | Effort | Notes |
|---|---|---|
| `tools/evalctl/dispatch/vertex.py` | M | submit Custom Job; return `vertex_job_id` |
| Stage config + manifest to GCS at submit time | S | reuses Phase 3 GCS client |
| Plumb `vertex_job_id` into manifest | S | proto already has the field |
| Stream / tail Vertex logs into local stdout | M | Cloud Logging API |
| `evalctl run` without `--local` flips default to remote | S | `--local` becomes opt-in |

---

## Phase 6 — Looker Studio dashboard

**Annotation:** ⏳ @unassigned [B] T2 M  deps: P3 (1 row sufficient)

Per HLD §6.6. Two pages (Comparison + Trend) over the BQ tables; tracked
externally (no code in this repo).

| Item | Effort | Notes |
|---|---|---|
| `v_latest_metrics` BQ view | S | joins merit_runs + merit_metrics |
| Comparison page (Looker Studio) | M | two run-id parameter controls + delta calc field |
| Trend page (Looker Studio) | M | time series per (model_id, task, metric) |
| Embed link / dashboard ACL | S | tracked outside repo |

---

## Phase 7 — Migration + deprecation

**Annotation:** ⏳ @unassigned [J] T3 M  deps: P5 + P6

Convert active sweeps to `config.yaml`; mark ad-hoc `lm_eval` invocation as
deprecated in the README; provide the flag→config mapping table from
HLD §18.

---

## Currently outstanding (cross-phase)

### Blockers (need user action)
- 🚨 **Rotate the HF token** that leaked to terminal scrollback during
  Phase 2 verification: `hf_DoRQGjgJiuCQR…` at
  https://huggingface.co/settings/tokens.

### Environmental (not code defects)
- **Container outbound network** failed on the last live run with
  `FileNotFoundError ... cannot find the requested files in the local cache`.
  Probably Corp Airlock blocking the container's HTTPS to `huggingface.co`.
  Workaround: pre-fetch the dataset on the host (`load_dataset(...)`) so the
  mounted `~/.cache/huggingface/` cache satisfies the lookup.
- **GPQA gated dataset**: HF account needs to have requested access at
  https://huggingface.co/datasets/Idavidrein/gpqa.

### Deferred
- Real-historical-sweep reproducibility A/B (Phase 1 follow-up) — needs
  on-disk reference artifacts.
- A non-root container `USER` for production deployments (Phase 4 hardening).

---

## How to update this file

When closing a phase task, update the relevant row:

1. Bump status emoji (`⏳` → `🔜` → `✅`).
2. Fill in the commit SHA.
3. Refresh the task's annotation tag if owner / tier / effort changed.
4. Bump `Last updated` in the header.

Keep it terse — the HLD is the design source of truth; this file is just
the "where are we and who's on it" status.
