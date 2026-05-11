# Convenience shims around bazel for devs unfamiliar with it.
# Real build/test/run still goes through bazel; these are just aliases.

.PHONY: build test smoke evalctl proto clean dev-install install uninstall fmt \
        docker-build docker-push docker-run docker-shell \
        demo-seed demo-clean

# Where the launcher script gets symlinked. Override with PREFIX=...
PREFIX ?= $(HOME)/.local
REPO   := $(abspath .)

# Docker image config. Override per-build:
#   make docker-build IMAGE_REGISTRY=us-docker.pkg.dev/my-proj/eval
HARNESS_VERSION ?= 0.4.12.dev0
WRAPPER_COMMIT  := $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
IMAGE_NAME      ?= merit
IMAGE_TAG       ?= $(HARNESS_VERSION)-$(WRAPPER_COMMIT)
IMAGE_REGISTRY  ?= local
IMAGE_REF       := $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

build:
	bazel build //...

test:
	bazel test //...

# Proto-free smoke test. Runs in any python venv with PyYAML; no bazel/protoc
# needed. Use this for quick local validation before pushing.
smoke:
	./tools/evalctl/smoke_test.sh

# Run the CLI under bazel (forwards CLI args via `make evalctl ARGS="..."`).
evalctl:
	bazel run //tools/evalctl:evalctl -- $(ARGS)

# Build only the proto layer (useful while iterating on schema).
proto:
	bazel build //proto/merit/v1:eval_py_proto

# Host venv install for dev iteration without bazel. Requires a venv to be
# already activated. Useful in Phase 0 before bazel is set up everywhere.
dev-install:
	pip install -r tools/python/requirements.in
	@echo "NOTE: proto codegen still requires bazel. Run 'make proto' first."

# Symlink tools/evalctl/evalctl into $(PREFIX)/bin so `evalctl ...` works
# from anywhere. The launcher hard-codes EVALCTL_REPO so it always finds
# this checkout regardless of cwd.
install:
	@mkdir -p $(PREFIX)/bin
	@printf '#!/usr/bin/env bash\nexport EVALCTL_REPO=%s\nexec %s/tools/evalctl/evalctl "$$@"\n' \
		"$(REPO)" "$(REPO)" > $(PREFIX)/bin/evalctl
	@chmod +x $(PREFIX)/bin/evalctl
	@echo "Installed: $(PREFIX)/bin/evalctl -> $(REPO)"
	@case ":$$PATH:" in *":$(PREFIX)/bin:"*) ;; *) \
		echo "WARNING: $(PREFIX)/bin is not on your PATH; add it to use 'evalctl' directly." ;; esac

uninstall:
	@rm -f $(PREFIX)/bin/evalctl
	@echo "Removed: $(PREFIX)/bin/evalctl"

clean:
	bazel clean

fmt:
	@echo "(no formatter wired yet)"

# ---------- Docker (Phase 2) -------------------------------------------------

# Build the runtime image. Tag = <registry>/<name>:<harness-version>-<sha>.
docker-build:
	docker build \
	  --file docker/Dockerfile \
	  --build-arg HARNESS_VERSION=$(HARNESS_VERSION) \
	  --build-arg WRAPPER_COMMIT=$(WRAPPER_COMMIT) \
	  --tag $(IMAGE_REF) \
	  --tag $(IMAGE_REGISTRY)/$(IMAGE_NAME):latest \
	  .
	@echo
	@echo "Built: $(IMAGE_REF)"
	@docker image inspect $(IMAGE_REF) --format='digest: {{index .Id}}' || true

# Push to the registry. Will fail with a clear message if IMAGE_REGISTRY=local.
docker-push:
	@if [ "$(IMAGE_REGISTRY)" = "local" ]; then \
	  echo "ERROR: IMAGE_REGISTRY=local. Set e.g. IMAGE_REGISTRY=us-docker.pkg.dev/my-proj/eval"; \
	  exit 1; \
	fi
	docker push $(IMAGE_REF)
	docker push $(IMAGE_REGISTRY)/$(IMAGE_NAME):latest

# Run the image with a config mounted in. Smoke convenience.
docker-run:
	docker run --rm -it $(IMAGE_REF) $(ARGS)

docker-shell:
	docker run --rm -it --entrypoint /bin/bash $(IMAGE_REF)

# ---------- Demo (engineering walkthrough) -----------------------------------

# Stage the two pre-built fixture runs into /tmp/run/ so `evalctl ls /
# show / compare` have something to talk about. Idempotent.
#
# Project / region / endpoint substituted via env vars (defaults shown).
# Override per-demo:
#   make demo-seed MERIT_DEMO_PROJECT=my-proj \
#                  MERIT_DEMO_REGION=us-central1 \
#                  MERIT_DEMO_ENDPOINT=my-endpoint
DEMO_RUN_A          := 11111111-1111-1111-1111-111111111111
DEMO_RUN_B          := 22222222-2222-2222-2222-222222222222
MERIT_DEMO_PROJECT  ?= cloud-llm-preview1
MERIT_DEMO_REGION   ?= us-central1
MERIT_DEMO_ENDPOINT ?= grok-4p20-non-reasoning-h200
DEMO_DIR            := /tmp/demo

# Substitute @@PROJECT@@ / @@REGION@@ / @@ENDPOINT@@ in any file under
# the destination tree. Uses sed -i so the source fixtures stay
# tokenized (committed) and only the staged copies get rendered.
define render_tokens
	@find $(1) -type f \( -name '*.json' -o -name '*.yaml' \) -print0 \
	  | xargs -0 sed -i \
	      -e 's|@@PROJECT@@|$(MERIT_DEMO_PROJECT)|g' \
	      -e 's|@@REGION@@|$(MERIT_DEMO_REGION)|g' \
	      -e 's|@@ENDPOINT@@|$(MERIT_DEMO_ENDPOINT)|g'
endef

demo-seed:
	@mkdir -p /tmp/run $(DEMO_DIR)
	@rm -rf /tmp/run/$(DEMO_RUN_A) /tmp/run/$(DEMO_RUN_B) $(DEMO_DIR)/demo_smoke.yaml
	@cp -r examples/runs/demo_run_a /tmp/run/$(DEMO_RUN_A)
	@cp -r examples/runs/demo_run_b /tmp/run/$(DEMO_RUN_B)
	@cp examples/configs/demo_smoke.template.yaml $(DEMO_DIR)/demo_smoke.yaml
	$(call render_tokens,/tmp/run/$(DEMO_RUN_A))
	$(call render_tokens,/tmp/run/$(DEMO_RUN_B))
	$(call render_tokens,$(DEMO_DIR))
	@# Recompute config_sha256 in each manifest so it matches the actual
	@# bytes of the rendered demo config. Without this the manifest's
	@# sha is stale and a sharp-eyed audience member spots the mismatch
	@# during 'evalctl show'.
	@python3 -c "import hashlib, json, pathlib; \
sha = hashlib.sha256(pathlib.Path('$(DEMO_DIR)/demo_smoke.yaml').read_bytes()).hexdigest(); \
[(p.write_text(json.dumps({**json.loads(p.read_text()), 'config_sha256': sha}, indent=2) + '\n')) \
 for p in [pathlib.Path('/tmp/run/$(DEMO_RUN_A)/manifest.json'), \
           pathlib.Path('/tmp/run/$(DEMO_RUN_B)/manifest.json')]]; \
print(f'  config_sha256 patched: {sha}')"
	@echo "Seeded with:"
	@echo "  project:  $(MERIT_DEMO_PROJECT)"
	@echo "  region:   $(MERIT_DEMO_REGION)"
	@echo "  endpoint: $(MERIT_DEMO_ENDPOINT)"
	@echo
	@echo "  /tmp/run/$(DEMO_RUN_A)  (demo_smoke_gpqa, baseline) <- produced by /tmp/demo/demo_smoke.yaml"
	@echo "  /tmp/run/$(DEMO_RUN_B)  (demo_smoke_gpqa, drifted)  <- same config, later snapshot"
	@echo "  $(DEMO_DIR)/demo_smoke.yaml          (rendered config used by both runs)"
	@echo
	@echo "Try:  evalctl ls"
	@echo "      evalctl validate $(DEMO_DIR)/demo_smoke.yaml"
	@echo "      evalctl show $(DEMO_RUN_A)   # config_sha256 will match the validate output"
	@echo "      evalctl compare $(DEMO_RUN_A) $(DEMO_RUN_B)"

demo-clean:
	@rm -rf /tmp/run/$(DEMO_RUN_A) /tmp/run/$(DEMO_RUN_B) $(DEMO_DIR)
	@echo "Removed demo fixtures from /tmp/run/ and $(DEMO_DIR)/"
