#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export OIDC_ISSUER="http://localhost:8081/realms/pfp-dev"
export OIDC_AUDIENCE="pfp-api"
export OIDC_CLIENT_ID="moneta"
export OIDC_JWKS_URL="${OIDC_ISSUER}/protocol/openid-connect/certs"

echo "Starting Moneta with Keycloak OIDC"
echo "Issuer: ${OIDC_ISSUER}"
echo "URL: http://127.0.0.1:18080/demo"

exec python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 18080 \
  --workers 2
