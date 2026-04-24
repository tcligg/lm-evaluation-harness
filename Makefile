# Convenience shims around bazel for devs unfamiliar with it.
# Real build/test/run still goes through bazel; these are just aliases.

.PHONY: build test smoke evalctl proto clean dev-install install uninstall fmt \
        docker-build docker-push docker-run docker-shell

# Where the launcher script gets symlinked. Override with PREFIX=...
PREFIX ?= $(HOME)/.local
REPO   := $(abspath .)

# Docker image config. Override per-build:
#   make docker-build IMAGE_REGISTRY=us-docker.pkg.dev/my-proj/eval
HARNESS_VERSION ?= 0.4.12.dev0
WRAPPER_COMMIT  := $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
IMAGE_NAME      ?= eval-harness
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
	bazel build //proto/eval/v1:eval_py_proto

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
