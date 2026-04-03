#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${ROOT_DIR}/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="${BACKUP_DIR}/mailserver-backup-${STAMP}.tar.gz"

mkdir -p "${BACKUP_DIR}"

tar -czf "${ARCHIVE}" \
  -C "${ROOT_DIR}" \
  docker-data/dms/config \
  docker-data/dms/mail-data \
  docker-data/dms/mail-state \
  docker-data/certbot/certs

sha256sum "${ARCHIVE}" > "${ARCHIVE}.sha256"

echo "Backup created: ${ARCHIVE}"
echo "Checksum: ${ARCHIVE}.sha256"
