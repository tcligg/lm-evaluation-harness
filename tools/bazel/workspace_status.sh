#!/usr/bin/env bash
# Stamp keys consumed by the manifest builder. Bazel invokes this on every
# build when --stamp is set; output is exposed via ctx.info_file / ctx.version_file.
#
# Keys must be of the form `KEY value`, one per line. STABLE_ keys are baked
# into cacheable artifacts; non-STABLE keys are not.

set -euo pipefail

git_commit="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
git_dirty="false"
if ! git diff --quiet HEAD 2>/dev/null; then
  git_dirty="true"
fi

echo "STABLE_GIT_COMMIT ${git_commit}"
echo "STABLE_GIT_DIRTY ${git_dirty}"
echo "BUILD_USER ${USER:-unknown}"
echo "BUILD_TIMESTAMP $(date -u +%Y-%m-%dT%H:%M:%SZ)"
