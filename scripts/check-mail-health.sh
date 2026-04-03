#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

echo "== docker compose ps =="
docker compose ps
echo

echo "== container health =="
docker inspect --format '{{.Name}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' mailserver
echo

echo "== listening ports =="
ss -ltn | grep -E ':(25|465|587|993)\b' || true
