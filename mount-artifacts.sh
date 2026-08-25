#!/usr/bin/env bash
# Mount (or unmount) the artifacts SMB share at ./artifacts on macOS.
#
# This lets the local (non-Docker) dev server read and write the same artifact
# files as the Docker container. Credentials are read from .env.
#
# Usage:
#   ./mount-artifacts.sh            # mount the share at ./artifacts
#   ./mount-artifacts.sh --unmount  # unmount it
set -euo pipefail

cd "$(dirname "$0")"

read_env_value() {
    grep -E "^$1=" .env 2>/dev/null | head -n1 | cut -d= -f2- | tr -d "'\""
}

HOST="${ARTIFACTS_STORE_HOST:-$(read_env_value ARTIFACTS_STORE_HOST)}"
FOLDER="${ARTIFACTS_STORE_FOLDER:-$(read_env_value ARTIFACTS_STORE_FOLDER)}"
USER="${ARTIFACTS_USER:-$(read_env_value ARTIFACTS_USER)}"
PASSWORD="${ARTIFACTS_PASSWORD:-$(read_env_value ARTIFACTS_PASSWORD)}"

MOUNT_POINT="$(pwd)/artifacts"

if [[ "${1:-}" == "--unmount" ]]; then
    echo "Unmounting ${MOUNT_POINT} ..."
    diskutil unmount "${MOUNT_POINT}" 2>/dev/null || umount "${MOUNT_POINT}" 2>/dev/null || true
    exit 0
fi

if [[ -z "${HOST}" || -z "${FOLDER}" ]]; then
    echo "ERROR: ARTIFACTS_STORE_HOST / ARTIFACTS_STORE_FOLDER not set in .env." >&2
    exit 1
fi

mkdir -p "${MOUNT_POINT}"

if mount | grep -Fq " on ${MOUNT_POINT} "; then
    echo "Already mounted at ${MOUNT_POINT}"
    exit 0
fi

# Percent-encode credentials for the SMB URL (handles #, !, @, etc.).
enc() {
    python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1" \
        || printf '%s' "$1"
}

ENC_USER="$(enc "${USER}")"
ENC_PASSWORD="$(enc "${PASSWORD}")"

echo "Mounting //${HOST}/${FOLDER} at ${MOUNT_POINT} (user: ${USER}) ..."
mount_smbfs "//${ENC_USER}:${ENC_PASSWORD}@${HOST}/${FOLDER}" "${MOUNT_POINT}"
echo "Mounted. Unmount with: ./mount-artifacts.sh --unmount"
