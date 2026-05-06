#!/usr/bin/env bash
# Configure Cloudflare Access on api-health.splash-wonderland.com so only
# the bma-health Cloudflare Worker (with a service token) can reach the
# tunnel. Direct browser/curl hits to that hostname will be challenged.
#
# Steps:
#   1. Create an Access service token (returns client_id + client_secret).
#   2. Create a self-hosted Access application bound to the hostname.
#   3. Attach a policy: allow-only-if-service-token.
#   4. Push client_id + client_secret as Worker secrets so the middleware
#      can inject CF-Access-Client-Id / CF-Access-Client-Secret on every
#      upstream call (already wired into src/middleware.ts).
#
# Idempotent: if app/service-token already exist by name, skip creation
# and reuse. Re-running is safe.
#
# Required env:
#   CF_API_TOKEN — token with scopes:
#                    Account -> Access: Apps and Policies: Edit
#                    Account -> Access: Service Tokens: Edit
#                    Account -> Cloudflare Tunnel: Read (to verify route)
#                    Account: Read

set -euo pipefail

: "${CF_API_TOKEN:?Set CF_API_TOKEN to a Cloudflare API token with Access scopes}"

HOST="api-health.splash-wonderland.com"
APP_NAME="bma-health-api-tunnel"
SERVICE_TOKEN_NAME="bma-health-worker"
POLICY_NAME="Allow only bma-health Worker"
WORKER_NAME="bma-health"
WRANGLER="npx --yes wrangler"

api() {
  curl --fail --silent --show-error \
    -H "Authorization: Bearer ${CF_API_TOKEN}" \
    -H "Content-Type: application/json" "$@"
}

echo ">> resolving account id"
ACCOUNT_ID=$(api "https://api.cloudflare.com/client/v4/accounts" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(r[0]["id"])')
echo "   account id: ${ACCOUNT_ID}"

# ---------------------------------------------------------------------------
# 1. Service token — create or reuse
# ---------------------------------------------------------------------------
echo ">> looking up service token '${SERVICE_TOKEN_NAME}'"
EXISTING_TOK=$(api "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/access/service_tokens" \
  | python3 -c "
import json, sys
toks = json.load(sys.stdin)['result']
for t in toks:
    if t['name'] == '${SERVICE_TOKEN_NAME}':
        print(t['id'])
        break
")

if [[ -n "$EXISTING_TOK" ]]; then
  echo "   reusing service token id ${EXISTING_TOK}"
  echo "   ⚠ existing tokens cannot reveal client_secret — to rotate, delete + recreate manually"
  echo "   skipping Worker secret push (no plaintext available)"
  CLIENT_ID=""
  CLIENT_SECRET=""
  TOKEN_ID="$EXISTING_TOK"
else
  echo ">> creating service token"
  CREATED=$(api -X POST \
    "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/access/service_tokens" \
    -d "{\"name\":\"${SERVICE_TOKEN_NAME}\"}")
  CLIENT_ID=$(echo "$CREATED"   | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["client_id"])')
  CLIENT_SECRET=$(echo "$CREATED" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["client_secret"])')
  TOKEN_ID=$(echo "$CREATED"      | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["id"])')
  echo "   service token id: ${TOKEN_ID}"
  echo "   client_id length: ${#CLIENT_ID} (echo verified non-empty)"
fi

# ---------------------------------------------------------------------------
# 2. Access application — create or reuse
# ---------------------------------------------------------------------------
echo ">> looking up Access app '${APP_NAME}'"
EXISTING_APP=$(api "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/access/apps" \
  | python3 -c "
import json, sys
apps = json.load(sys.stdin)['result']
for a in apps:
    if a['name'] == '${APP_NAME}':
        print(a['id'])
        break
")

if [[ -n "$EXISTING_APP" ]]; then
  APP_ID="$EXISTING_APP"
  echo "   reusing app id ${APP_ID}"
else
  echo ">> creating Access app"
  APP=$(api -X POST \
    "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/access/apps" \
    -d "{
      \"name\":\"${APP_NAME}\",
      \"type\":\"self_hosted\",
      \"domain\":\"${HOST}\",
      \"session_duration\":\"24h\",
      \"auto_redirect_to_identity\":false,
      \"app_launcher_visible\":false
    }")
  APP_ID=$(echo "$APP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["id"])')
  echo "   app id: ${APP_ID}"
fi

# ---------------------------------------------------------------------------
# 3. Policy — allow-only-if-service-token (idempotent by name)
# ---------------------------------------------------------------------------
echo ">> ensuring policy '${POLICY_NAME}'"
EXISTING_POLICY=$(api "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/access/apps/${APP_ID}/policies" \
  | python3 -c "
import json, sys
ps = json.load(sys.stdin)['result']
for p in ps:
    if p['name'] == '${POLICY_NAME}':
        print(p['id'])
        break
")

POLICY_BODY=$(python3 -c "
import json
print(json.dumps({
  'name': '${POLICY_NAME}',
  'decision': 'non_identity',
  'include': [{'service_token': {'token_id': '${TOKEN_ID}'}}],
  'precedence': 1
}))
")

if [[ -n "$EXISTING_POLICY" ]]; then
  echo "   updating policy ${EXISTING_POLICY}"
  api -X PUT \
    "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/access/apps/${APP_ID}/policies/${EXISTING_POLICY}" \
    -d "${POLICY_BODY}" >/dev/null
else
  echo "   creating policy"
  api -X POST \
    "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/access/apps/${APP_ID}/policies" \
    -d "${POLICY_BODY}" >/dev/null
fi

# ---------------------------------------------------------------------------
# 4. Push Worker secrets (only when we just minted a fresh token)
# ---------------------------------------------------------------------------
if [[ -n "$CLIENT_ID" && -n "$CLIENT_SECRET" ]]; then
  echo ">> pushing CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET to Worker '${WORKER_NAME}'"
  echo -n "$CLIENT_ID"     | $WRANGLER secret put CF_ACCESS_CLIENT_ID     --name "$WORKER_NAME"
  echo -n "$CLIENT_SECRET" | $WRANGLER secret put CF_ACCESS_CLIENT_SECRET --name "$WORKER_NAME"
  echo ">> redeploying Worker so secrets bind"
  ( cd "$(dirname "$0")/../../../bma-health/frontend" && pnpm run deploy ) >/dev/null 2>&1 || \
    echo "   ⚠ deploy step failed — run 'pnpm run deploy' manually from bma-health/frontend"
else
  echo ">> service token already existed; cannot recover client_secret programmatically."
  echo "   if Worker secrets are missing, delete the service token in Cloudflare dashboard"
  echo "   then re-run this script to mint a fresh pair."
fi

echo
echo "Done. Cloudflare Access guards ${HOST}:"
echo "  - direct browser/curl  -> 302 to login (or 401 if no IdP)"
echo "  - bma-health Worker    -> passes via service-token headers"
