#!/usr/bin/env bash
# Build and push the radio-tracker image to the local Docker server.
#
# Reads DOCKER_SERVER_IP and DOCKER_REGISTRY_PORT from .env (or the shell
# environment, which takes precedence). The image is tagged as:
#   <DOCKER_SERVER_IP>:<DOCKER_REGISTRY_PORT>/radio-tracker:latest
#
# If your Docker server does not run a registry, skip `docker push` and instead
# transfer the image manually (see the commented fallback at the bottom).
set -euo pipefail

cd "$(dirname "$0")"

read_env_value() {
    grep -E "^$1=" .env 2>/dev/null | head -n1 | cut -d= -f2- | tr -d "'\""
}

SERVER_IP="${DOCKER_SERVER_IP:-$(read_env_value DOCKER_SERVER_IP)}"
REGISTRY_PORT="${DOCKER_REGISTRY_PORT:-$(read_env_value DOCKER_REGISTRY_PORT)}"
REGISTRY_PORT="${REGISTRY_PORT:-5000}"

if [[ -z "${SERVER_IP}" ]]; then
    echo "ERROR: DOCKER_SERVER_IP is not set. Add it to .env or export it." >&2
    exit 1
fi

IMAGE="${SERVER_IP}:${REGISTRY_PORT}/radio-tracker:latest"

# The Docker server is x86_64, so build a matching linux/amd64 image even
# though this Mac is arm64. `docker build --platform` cross-compiles with
# QEMU, which Docker Desktop handles automatically.
PLATFORMS="${DOCKER_BUILD_PLATFORMS:-linux/amd64}"

echo "Building ${IMAGE} (platform: ${PLATFORMS}) ..."
docker build --platform "${PLATFORMS}" -t "${IMAGE}" .

# Push with the Docker daemon (not buildx) so the daemon's insecure-registries
# setting for the HTTP registry is respected.
echo "Pushing ${IMAGE} ..."
docker push "${IMAGE}"

echo "Done. On the Docker server, pull and run with:"
echo "  docker pull ${IMAGE}"

# --- Fallback when the server has no registry running -------------------------
# docker save "${IMAGE}" | gzip > radio-tracker.tar.gz
# scp radio-tracker.tar.gz <user>@${SERVER_IP}:
# ssh <user>@${SERVER_IP} 'gunzip -c radio-tracker.tar.gz | docker load'
