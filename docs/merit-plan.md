# MERIT — Implementation Plan & Status

**MERIT — Model Evaluation, Reproducibility, and Tracking**

**Companion to:** [`merit-hld.md`](./merit-hld.md)
**Status:** Active — Phase 2 complete, Phase 3 next
**Owner:** tcli
**Last updated:** 2026-04-24 (rev 2 — system named MERIT)

This file tracks **actual** delivery against the phased plan in HLD §15.
Update on every commit that closes a phase task.

---

## At-a-glance

| Phase | Scope | Status | Notes |
|---|---|---|---|
| HLD | Design doc | ✅ shipped | `48911c6` |
| 0 | `evalctl` skeleton, proto IDL, Bazel workspace, manifest | ✅ shipped | 4 commits |
| 1 | Programmatic dispatch, per-task num_fewshot, repro/diff | ✅ shipped | `0e3ffc8` |
| 2 | Dockerfile, container dispatch, GitHub Actions CI | ✅ shipped | 3 commits |
| 3 | GCS upload + BQ insert (sinks) | 🔜 next |  |
| 4 | ADC auth, retire `refresh_token.sh` | ⏳ pending | depends on 3 |
| 5 | Vertex Custom Job dispatcher | ⏳ pending | depends on 2 (image) + 4 (auth) |
| 6 | Looker Studio dashboard | ⏳ pending | depends on 3 (BQ schema live) |
| 7 | Migrate active sweeps; deprecate ad-hoc CLI | ⏳ pending | depends on 5 + 6 |

Legend: ✅ shipped • 🔜 in-progress / next up • ⏳ not started • ⚠️ blocked

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
make smoke                                             # 75 unit tests, ~50ms
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

**Status:** ✅ shipped

| Item | Commit |
|---|---|
| Proto IDL (`config.proto`, `manifest.proto`, `metrics.proto`, `common.proto`) | `eb962db` |
| Bazel bzlmod workspace, `MODULE.bazel`, BUILD files | `eb962db` |
| Typer CLI: `run` / `validate` / `show` / `compare` | `eb962db` |
| Manifest builder (UUID, git SHA, env allow-list) | `eb962db` |
| Example config (port of historical sweep) | `eb962db` |
| Proto-free smoke tier + 11 initial unit tests | `e8b603e` |
| Installable launcher (`make install`) + lazy proto imports | `6d0e33c` |
| Chat-template defaulting (`--apply_chat_template` for chat endpoints) | `f9ab799` |

---

## Phase 1 — Reproducibility (Programmatic + repro/diff)

**Status:** ✅ shipped (`0e3ffc8`)

| Item | Notes |
|---|---|
| Programmatic dispatch via `lm_eval.simple_evaluate()` | replaces subprocess shell-out |
| Per-task `num_fewshot` (call-grouping) | lifts Phase 0 limitation |
| `--limit N` flag | smoke convenience |
| `evalctl repro <results.json> --out cfg.yaml` | reverse-engineer config from harness output; infers endpoint type/id from `model_args` |
| `evalctl diff <a.json> <b.json>` | structural + numeric comparison; exits 3 on drift |
| 30 new unit tests (`pure_test`, `repro_test`, `programmatic_test`) | mocks `lm_eval` via `sys.modules` |

**Deferred to follow-up:** real reproducibility A/B against an actual historical
sweep — original `groq-4p2-…` result dirs were untracked and lost before Phase 0
landed. Once a real `results_*.json` is checked in or accessible via gs://, the
existing pipeline validates it.

---

## Phase 2 — Containerization + CI

**Status:** ✅ shipped (`4f28c0e` + `d0daf99` + `09431c3`)

| Item | Notes |
|---|---|
| `docker/Dockerfile` (multi-stage) | builder runs `protoc` via grpcio-tools; runtime adds gcloud SDK |
| Image tag convention `merit:<harness>-<wrapper_sha>` | built locally; AR push stub commented out |
| Container dispatch (`evalctl run --local --container [--image]`) | `tools/evalctl/container.py` |
| Manifest auto-detects `container.image` + `container.digest` from `MERIT_IMAGE_*` env | `manifest.py` |
| Latent enum bug fix (`config_pb2.EndpointType` → `common_pb2.EndpointType`) | surfaced once proto path was actually exercised |
| `_run_with_proto` now writes `manifest.json` + `config.resolved.yaml` | parity with `_run_pure` |
| HF/OpenAI auth env passthrough into container | mounts `~/.cache/huggingface` read-only |
| Secret redaction in dispatch logs | `_redact_argv()` masks 7 known secret env-var values |
| GitHub Actions CI (`.github/workflows/evalctl-ci.yml`) | smoke + docker + bazel jobs |
| 12 container_test cases | mocks `subprocess.run` |

**Image stats:** ~501 MB compressed / 2.63 GB uncompressed. Biggest contributor
is gcloud SDK (Phase 4 anchor). Image rebuild after a code change is ~60s
(deps layer cached).

**Outstanding:** Artifact Registry push job is stubbed; needs WIF secrets
(`GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`) before the
`docker-push` job can be uncommented.

---

## Phase 3 — Centralized result store (next up)

**Status:** 🔜 not started

Per HLD R3 / §6.5 / §8.

### Scope

- `tools/merit_runner/publish.py`: GCS upload of the entire run scratch dir to
  `gs://merit-artifacts-prod/runs/<run_id>/`. Atomic upload via temp prefix +
  rename. Idempotent on `run_id`.
- BigQuery insert into the three `merit_results.*` tables. `MERGE` on `run_id`
  for retry safety. Runner writes `published.marker` after successful upload
  to make repeats no-ops.
- BQ schemas generated from `proto/merit/v1/metrics.proto` via
  `protoc-gen-bq-schema`. Emit to `schemas/bq/*.json`, check in.
- Provisioning helpers under `schemas/bq/`: a `bq mk` script + (optional)
  Terraform stubs.
- `evalctl run --no-publish` flag becomes meaningful (was a no-op).
- `evalctl show <run_id>` and `evalctl compare <a> <b>` finally implemented
  (they're stubbed with exit-1 today).
- `merit_runs` view (`v_latest_metrics`) for Looker Studio in Phase 6.

### Estimate
2–3 days per HLD §15.

### Open questions before kickoff
- Terraform vs. one-off `bq mk` scripts (HLD §17 Open Items).
- Real GCP project + bucket name to hard-code in defaults (or env-var lookup).

---

## Phase 4 — ADC auth (after Phase 3)

**Status:** ⏳ pending

Per HLD §6.4. Replaces `refresh_token.sh` with in-process ADC token exchange
inside the container. Tested via a real Vertex chat endpoint.

---

## Phase 5 — Vertex Custom Job dispatcher (after 2 + 4)

**Status:** ⏳ pending

Per HLD §6.1. `evalctl run` without `--local` submits a Vertex Custom Job using
the same image; the in-container runner reads the config from GCS and writes
results back via the Phase 3 publisher.

---

## Phase 6 — Looker Studio dashboard (after 3)

**Status:** ⏳ pending

Per HLD §6.6. Two pages (Comparison + Trend) over the BQ tables; tracked
externally (no code in this repo).

---

## Phase 7 — Migration + deprecation (after 5 + 6)

**Status:** ⏳ pending

Convert active sweeps to `config.yaml`; mark ad-hoc `lm_eval` invocation as
deprecated in the README; provide the flag→config mapping table from
HLD §18.

---

## Currently outstanding (cross-phase)

### Blockers (need user action)
- 🚨 **Rotate the HF token** that leaked to terminal scrollback during
  Phase 2 verification: `hf_DoRQGjgJiuCQR…` at
  https://huggingface.co/settings/tokens.
- **Push commit `09431c3`** (secret redaction) — local push hangs because
  this dev box has no GitHub credentials configured. Run
  `git push origin evalctl` from a shell with creds set up.

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

When closing a phase task, update the relevant row above with the commit SHA
and bump "Last updated". Keep it terse — the HLD is the design source of
truth; this file is just the "where are we" status.
