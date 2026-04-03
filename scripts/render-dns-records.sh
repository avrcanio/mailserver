#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
PUBLIC_IP="${PUBLIC_IP:-65.108.196.92}"

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <domain>" >&2
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy .env.example to .env first." >&2
  exit 1
fi

DOMAIN="$1"

set -a
source "${ENV_FILE}"
set +a

if [[ -z "${MAIL_HOSTNAME:-}" ]]; then
  echo "MAIL_HOSTNAME must be set in .env." >&2
  exit 1
fi

DKIM_FILE="${ROOT_DIR}/docker-data/dms/config/opendkim/keys/${DOMAIN}/mail.txt"

echo "A mail.${DOMAIN} ${PUBLIC_IP}"
echo "MX ${DOMAIN} 10 ${MAIL_HOSTNAME}."
echo "TXT ${DOMAIN} \"v=spf1 mx a:${MAIL_HOSTNAME} -all\""
echo "TXT _dmarc.${DOMAIN} \"v=DMARC1; p=quarantine; rua=mailto:dmarc@${DOMAIN}\""

if [[ -f "${DKIM_FILE}" ]]; then
  echo
  echo "# DKIM"
  cat "${DKIM_FILE}"
else
  echo
  echo "# DKIM file not found yet: ${DKIM_FILE}" >&2
  echo "# Run ./scripts/mail.sh config dkim after adding the domain/mailboxes." >&2
fi
