#!/usr/bin/env bash
# Test real provider dispatch in the exact Hermes base used by the production Dockerfile.
set -euo pipefail
cd "$(dirname "$0")/.."
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
base_image=${HERMES_TEST_IMAGE:-$(sed -n 's/^ARG HERMES_IMAGE=//p' Dockerfile)}
cp src/hermes-patches/foundry_inference.py "$context/"
cp src/hermes-patches/apply_nous_inference_patch.py "$context/"
cp src/hermes-patches/apply_fallback_entra_patch.py "$context/"
cp src/hermes-patches/apply_foundry_annotations_patch.py "$context/"
cp tests/pinned_inference_smoke.py "$context/verify.py"
cat > "$context/Dockerfile" <<'DOCKER'
ARG HERMES_IMAGE
FROM ${HERMES_IMAGE}
USER root
COPY foundry_inference.py /opt/hermes/hermes_cli/foundry_inference.py
COPY apply_nous_inference_patch.py apply_fallback_entra_patch.py apply_foundry_annotations_patch.py /tmp/
RUN /opt/hermes/.venv/bin/python /tmp/apply_nous_inference_patch.py \
 && /opt/hermes/.venv/bin/python /tmp/apply_nous_inference_patch.py \
 && /opt/hermes/.venv/bin/python /tmp/apply_fallback_entra_patch.py \
 && /opt/hermes/.venv/bin/python /tmp/apply_foundry_annotations_patch.py
COPY verify.py /tmp/verify.py
RUN /opt/hermes/.venv/bin/python /tmp/verify.py
DOCKER
docker build --network=none --build-arg "HERMES_IMAGE=$base_image" "$context"
