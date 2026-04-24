# Eval runner image (Phase 2).
#
# Tag convention: eval-harness:<harness_version>-<wrapper_git_sha>
# Build args:
#   HARNESS_VERSION  Pinned lm-evaluation-harness version. Echoed into
#                    the manifest at runtime; must match what's installed.
#   WRAPPER_COMMIT   Short git SHA of the wrapper repo at build time.
#
# Multi-stage:
#   builder  - installs python deps, runs proto codegen via grpcio-tools.
#   runtime  - slim image with installed deps + wrapper sources + harness.
#              Entrypoint is `evalctl`.
#
# Image is intentionally self-contained: no bazel at runtime; protoc is
# only used at build time. Auth (Phase 4) and GCS/BQ publish (Phase 3)
# arrive in later phases; this image lays the foundation.

# ---------- Stage 1: builder -------------------------------------------------
FROM python:3.11-slim AS builder

ARG HARNESS_VERSION=0.4.12.dev0
ARG WRAPPER_COMMIT=unknown

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# System deps: git for `pip install -e`, build-essential for any wheels
# that don't have prebuilt aarch64/x86_64 binaries.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git build-essential ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install evalctl runtime deps first (cached layer when only sources change).
COPY tools/python/requirements.in /build/requirements.in
RUN pip install --upgrade pip \
 && pip install -r /build/requirements.in

# Copy the harness sources + wrapper sources together. The harness installs
# editable so its custom_tasks/ resolution still works at runtime.
COPY pyproject.toml MANIFEST.in README.md /build/
COPY lm_eval /build/lm_eval
COPY custom_tasks /build/custom_tasks
COPY proto /build/proto
COPY tools /build/tools
COPY examples /build/examples

# Pin lm_eval to the pyproject's version. The HARNESS_VERSION arg is
# asserted against `lm_eval.__version__` at runtime by eval_runner.
# Install the [api] extras so local-chat-completions / vertex_chat work
# (tenacity, aiohttp, etc.).
RUN pip install -e "/build[api]"

# Generate _pb2.py files from the proto IDL using grpcio-tools' bundled
# protoc. Output lands in /build/proto/eval/v1/*_pb2.py and is picked up
# at runtime via the existing import path.
RUN pip install grpcio-tools \
 && python -m grpc_tools.protoc \
        --proto_path=/build \
        --python_out=/build \
        proto/eval/v1/common.proto \
        proto/eval/v1/config.proto \
        proto/eval/v1/manifest.proto \
        proto/eval/v1/metrics.proto \
 && touch /build/proto/__init__.py /build/proto/eval/__init__.py /build/proto/eval/v1/__init__.py

# ---------- Stage 2: runtime -------------------------------------------------
FROM python:3.11-slim AS runtime

ARG HARNESS_VERSION=0.4.12.dev0
ARG WRAPPER_COMMIT=unknown

LABEL org.opencontainers.image.title="eval-harness" \
      org.opencontainers.image.source="https://github.com/tcligg/lm-evaluation-harness" \
      org.opencontainers.image.description="evalctl + lm-evaluation-harness, pinned for reproducible eval runs" \
      eval.harness_version="${HARNESS_VERSION}" \
      eval.wrapper_commit="${WRAPPER_COMMIT}"

ENV EVALCTL_HARNESS_VERSION=${HARNESS_VERSION} \
    EVALCTL_WRAPPER_COMMIT=${WRAPPER_COMMIT} \
    EVALCTL_REPO=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/usr/local/bin:/usr/bin:/bin

# gcloud SDK is part of the HLD requirements (auth lands Phase 4 but the
# image stays consistent across phases). We install only the gcloud-cli
# subset to keep the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git \
 && curl -sS https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] " \
        "https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends google-cloud-cli \
 && rm -rf /var/lib/apt/lists/*

# Copy installed site-packages + the editable harness/wrapper layout from
# the builder. Two COPYs so layer cache stays useful when only one half
# changes.
COPY --from=builder /usr/local/lib/python3.11/site-packages \
                    /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /build /app

WORKDIR /app

# Install the launcher into /usr/local/bin so users get `evalctl`
# directly (matches the host install pattern from Phase 0).
RUN install -m 0755 /app/tools/evalctl/evalctl /usr/local/bin/evalctl

# Set up an `evalctl` UID for prod deployments (Vertex Custom Job /
# Cloud Run both require non-root for serving). Phase 2 still RUNs as
# root by default so bind-mounted scratch dirs Just Work for local
# development; production deployments override with `--user 1000`.
RUN echo 'evalctl:x:1000:1000::/home/evalctl:/bin/bash' >> /etc/passwd \
 && echo 'evalctl:x:1000:' >> /etc/group \
 && mkdir -p /home/evalctl \
 && chown 1000:1000 /home/evalctl

ENTRYPOINT ["evalctl"]
CMD ["--help"]
