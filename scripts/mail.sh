#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER_NAME="${DMS_CONTAINER_NAME:-mailserver}"
IMAGE_NAME="${DMS_IMAGE_NAME:-ghcr.io/docker-mailserver/docker-mailserver:15.1.0}"

exec "${ROOT_DIR}/scripts/setup.sh" -c "${CONTAINER_NAME}" -i "${IMAGE_NAME}" "$@"
