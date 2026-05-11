# MERIT moved

The MERIT wrapper (evalctl, proto/merit/v1, docker/, docs/merit-*,
adapters, etc.) used to live in this fork. It now lives in its own
repository:

  https://github.com/tcligg/merit

Why the split (HLD §5.1 in the new repo):
1. Pluggable benchmark backends (profiling can ship its own adapter).
2. This fork can merge with upstream lm-evaluation-harness on its own
   cadence without touching MERIT's release cycle.
3. lm-evaluation-harness can be swapped for a successor backend without
   rewriting the wrapper.

This fork now exists solely to track local task overlays + any
upstream merges of EleutherAI/lm-evaluation-harness. MERIT consumes
this codebase as a pinned PyPI dependency (`pip install
"lm_eval[api]==X.Y.Z"`).

To work on MERIT, clone the new repo:

```bash
git clone https://github.com/tcligg/merit
cd merit
make install            # installs evalctl
make demo-seed          # stages demo fixtures
evalctl ls              # try it
```
