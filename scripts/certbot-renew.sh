#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
CF_FILE="${ROOT_DIR}/docker-data/certbot/secrets/cloudflare.ini"
CERT_DIR="${ROOT_DIR}/docker-data/certbot/certs"
LOG_DIR="${ROOT_DIR}/docker-data/certbot/logs"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy .env.example to .env first." >&2
  exit 1
fi

if [[ ! -f "${CF_FILE}" ]]; then
  echo "Missing ${CF_FILE}. Create it with the Cloudflare API token." >&2
  exit 1
fi

set -a
source "${ENV_FILE}"
set +a

if [[ -z "${MAIL_HOSTNAME:-}" || -z "${LETSENCRYPT_EMAIL:-}" ]]; then
  echo "MAIL_HOSTNAME and LETSENCRYPT_EMAIL must be set in .env." >&2
  exit 1
fi

DOMAIN_ARGS=(-d "${MAIL_HOSTNAME}")
if [[ -n "${ADDITIONAL_CERT_DOMAINS:-}" ]]; then
  IFS=',' read -r -a extra_domains <<< "${ADDITIONAL_CERT_DOMAINS}"
  for domain in "${extra_domains[@]}"; do
    trimmed="$(echo "${domain}" | xargs)"
    [[ -n "${trimmed}" ]] && DOMAIN_ARGS+=(-d "${trimmed}")
  done
fi

STAGING_ARGS=()
if [[ "${CERTBOT_STAGING:-0}" == "1" ]]; then
  STAGING_ARGS+=(--staging)
fi

docker run --rm \
  --name mailserver-certbot-run \
  -v "${CERT_DIR}:/etc/letsencrypt" \
  -v "${LOG_DIR}:/var/log/letsencrypt" \
  -v "${CF_FILE}:/run/secrets/cloudflare.ini:ro" \
  certbot/dns-cloudflare:latest certonly \
  --non-interactive \
  --agree-tos \
  --dns-cloudflare \
  --dns-cloudflare-credentials /run/secrets/cloudflare.ini \
  --preferred-challenges dns-01 \
  --keep-until-expiring \
  --expand \
  --email "${LETSENCRYPT_EMAIL}" \
  --cert-name "${MAIL_HOSTNAME}" \
  "${STAGING_ARGS[@]}" \
  "${DOMAIN_ARGS[@]}"
