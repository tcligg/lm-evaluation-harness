# Convenience shims around bazel for devs unfamiliar with it.
# Real build/test/run still goes through bazel; these are just aliases.

.PHONY: build test evalctl proto clean dev-install fmt

build:
	bazel build //...

test:
	bazel test //...

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

clean:
	bazel clean

fmt:
	@echo "(no formatter wired yet)"
