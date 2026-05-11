# MERIT — 5–7 min Engineering Walkthrough

**Audience:** the eng team. Assumes familiarity with the ad-hoc
`lm_eval` workflow we're replacing.
**Style:** live terminal, no slides; one live `--dry-run`, all other
runs from pre-staged fixtures so we don't fight HF auth or Vertex
network during the demo.
**Time budget:** ~6 minutes.

## Pre-flight (do once before the meeting)

```bash
cd /path/to/lm-evaluation-harness
make install                # installs evalctl to ~/.local/bin
rm -rf /tmp/run/*           # optional: clean leftover dev runs so `ls`
                            # shows only the demo fixtures
make demo-seed              # renders fixtures into /tmp/run/ and config
                            # into /tmp/demo/demo_smoke.yaml
```

### Seeding with your own GCP project

The fixtures + config use `@@PROJECT@@`, `@@REGION@@`, `@@ENDPOINT@@`
tokens. `make demo-seed` substitutes them with these defaults:

| Var | Default |
|---|---|
| `MERIT_DEMO_PROJECT`  | `cloud-llm-preview1` |
| `MERIT_DEMO_REGION`   | `us-central1` |
| `MERIT_DEMO_ENDPOINT` | `grok-4p20-non-reasoning-h200` |

Override per-demo by passing them on the make command line:

```bash
make demo-seed \
  MERIT_DEMO_PROJECT=my-real-project \
  MERIT_DEMO_REGION=us-east1 \
  MERIT_DEMO_ENDPOINT=my-real-endpoint
```

Or export them once for the shell:

```bash
export MERIT_DEMO_PROJECT=my-real-project
export MERIT_DEMO_ENDPOINT=my-real-endpoint
make demo-seed
```

The substitution rewrites in three places: the two run dirs under
`/tmp/run/` (so `evalctl show` displays your project), and the rendered
config at `/tmp/demo/demo_smoke.yaml` (so `evalctl validate` /
`evalctl run --dry-run` use it).

Verify in a fresh terminal:

```bash
evalctl ls                                          # should show exactly demo_run_a + demo_run_b
evalctl validate /tmp/demo/demo_smoke.yaml          # should print OK
grep endpoint_id /tmp/demo/demo_smoke.yaml          # confirms your project shows
```

If `evalctl` isn't on `$PATH`, add `~/.local/bin` to it.

---

## Act 1 — What you're looking at  (~30s)

Open `docs/merit-hld.md` and read out the first paragraph and the
goals table:

```bash
sed -n '1,40p' docs/merit-hld.md
```

> **Talking point:** "This is the wrapper that turns ad-hoc
> `lm_eval` invocations into reproducible, queryable runs. Today
> I'll show: config-as-code, repro round-trip, container provenance,
> run lookup by uuid, and the drift gate."

---

## Act 2 — Config-as-code  (~60s)

Show that a run is fully described by one YAML file:

```bash
bat /tmp/demo/demo_smoke.yaml         # or `cat`, `less`
```

Then validate it:

```bash
evalctl validate /tmp/demo/demo_smoke.yaml
```

Expected output:

```
OK
config_sha256: <hex>
name:          demo_smoke_gpqa
tasks:         gpqa_diamond_generative_n_shot
endpoint:      google/openmaas-2.0-test
```

> **Talking point:** "proto-defined schema. `config_sha256` becomes
> the link between this YAML and the run's manifest — change one
> character, get a different hash, get a different run."

**Bonus mid-Act 2 (10s):** show that the `config_sha256` you just
printed matches the one recorded in the demo run's manifest:

```bash
grep config_sha256 /tmp/run/11111111-1111-1111-1111-111111111111/manifest.json
```

> "Same hash. That's how `evalctl show` proves a run came from this
> exact config."

---

## Act 3 — Live: config → lm_eval invocation  (~90s)

The one live moment. Two flavors — pick whichever lands better with
the room (or show both):

**Subprocess form** (matches the legacy `lm_eval ...` shell command
the team has been typing):

```bash
evalctl run /tmp/demo/demo_smoke.yaml --local --dry-run --subprocess
```

Expected: a single `lm_eval ...` argv with `--apply_chat_template`,
`--fewshot_as_multiturn`, `--num_fewshot 3`, `--seed 0,1234,1234,1234`,
`--gen_kwargs until=<|eos|>`, and the limit `--limit 1`.

**Programmatic form** (default; in-process `simple_evaluate` call):

```bash
evalctl run /tmp/demo/demo_smoke.yaml --local --dry-run
```

Expected: model_args dict, task_groups dict (one group per distinct
`num_fewshot`), limit value.

> **Talking point:** "Same config produces the same lm_eval call
> regardless of dispatch path. The argv translation is unit-tested
> in `tools/evalctl/pure_test.py`. No more 'oops I forgot
> `--apply_chat_template`' surprises like we'd hit by hand."

---

## Act 4 — Reproducibility round-trip  (~90s)

Take the historical results from the fixture, reverse-engineer a
config, and show it round-trips:

```bash
evalctl repro examples/runs/demo_run_a/results_*.json \
  --out /tmp/repro_demo.yaml --name demo_smoke_repro
diff /tmp/demo/demo_smoke.yaml /tmp/repro_demo.yaml | head -30
```

> **Talking point:** "Given any historical `results_*.json` you can
> reconstruct the config that produced it. Endpoint type/id are
> inferred from the recorded `model_args`; per-task `num_fewshot`
> from per-task configs. Any field that doesn't round-trip — auth,
> output sinks — gets a `# TODO confirm` comment."

---

## Act 5 — Containerization + provenance  (~60s)

Show the image is real and that `manifest.json` records what ran:

```bash
docker images local/merit | head
cat /tmp/run/11111111-1111-1111-1111-111111111111/manifest.json \
  | jq '.container, .config_sha256, .git'
```

Expected: image+digest, config sha, git commit + dirty flag.

> **Talking point:** "Tag = `merit:<harness>-<wrapper-sha>`. Image
> digest is recorded automatically. Combined with `config_sha256`
> and `wrapper_commit`, you can byte-for-byte reproduce any run
> as long as the image is still in the registry."

---

## Act 6 — Run lookup by uuid  (~60s)

The HLD makes `run_id` the universal join key. Show the lookup
working today against local artifacts:

```bash
evalctl ls
evalctl show 11111111-1111-1111-1111-111111111111
```

Expected: 2 rows from `ls`, then a full summary card with metrics
and artifact paths. The `config_sha:` line matches what you saw from
`evalctl validate` in Act 2.

> **Talking point:** "Today the resolver hits the local scratch dir.
> Phase 3 adds GCS and BigQuery backends behind the same API — the
> CLI surface won't change. The config_sha256 is what proves this
> run came from /tmp/demo/demo_smoke.yaml — anyone can re-validate
> the config and see the same hash."

---

## Act 7 — Drift detection  (~30s)

The whole point of having a comparison story:

```bash
evalctl compare \
  11111111-1111-1111-1111-111111111111 \
  22222222-2222-2222-2222-222222222222
echo "exit code: $?"
```

Expected: `DRIFT DETECTED` (5pp regression on `exact_match,strict-match`),
exit 3.

Then show a tolerance:

```bash
evalctl compare \
  11111111-1111-1111-1111-111111111111 \
  22222222-2222-2222-2222-222222222222 \
  --score-tol 0.1
```

Expected: `MATCH`, exit 0.

> **Talking point:** "Exit 3 = drift, exit 0 = match. Drop this in
> CI as a regression gate: 'this PR doesn't perturb my historical
> sweeps by more than X.'"

---

## Act 8 — What's next  (~30s)

Open the plan doc to the parallelization map:

```bash
sed -n '15,90p' docs/merit-plan.md
```

> **Talking point:** "Phases 0/1/2 + lookup are shipped. Phase 3
> (GCS+BQ) is next; it unblocks `show`/`compare` against remote
> runs and unblocks Phase 6 (Looker). Phases 1/2/3 actually
> parallelize, so a 2-SWE assignment can finish 4–7 in ~2.5
> weeks instead of the serial 4-week estimate."

---

## Cleanup

```bash
make demo-clean             # removes the two fixture runs from /tmp/run/
rm -f /tmp/repro_demo.yaml
```

---

## Backup answers (for likely Q&A)

**Q: What if my run fails halfway? Is the manifest still useful?**
A: Yes. The manifest is written before the runner invokes lm_eval,
with `status: RUN_STATUS_RUNNING`. On completion the runner patches
status to `SUCCESS` / `PARTIAL` / `FAILED`. `evalctl show` works
against in-progress runs (no `results_*.json` yet, just shows
manifest).

**Q: How do I run this against my own endpoint without changing the
example?**
A: Three options: (1) re-seed with overrides — `make demo-seed
MERIT_DEMO_PROJECT=my-proj MERIT_DEMO_ENDPOINT=my-endpoint`; (2)
`evalctl repro` from a previous `results.json`; (3) once Phase 4
lands, just `gcloud auth application-default login` and
`evalctl run cfg.yaml --local`.

**Q: Where do results go?**
A: Today: `/tmp/run/<run_id>/`. Phase 3: also pushed to
`gs://merit-artifacts-prod/runs/<run_id>/` and merged into BigQuery
`merit_results.{merit_runs,merit_metrics,merit_manifest_raw}`.

**Q: Why "MERIT"?**
A: Model Evaluation, Reproducibility, and Tracking — the three pillars
in the HLD. Acronym maps onto the requirement IDs (R2 tracking, R3
reproducibility/store).

**Q: Can I run two configs in parallel?**
A: Today: yes, just `evalctl run cfg_a.yaml --local & evalctl run
cfg_b.yaml --local &`. Each gets its own `run_id` and workdir.
Phase 5 will add Vertex Custom Job dispatch for true cluster-scale
parallelism.
