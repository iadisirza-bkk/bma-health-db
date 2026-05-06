#!/usr/bin/env bash
# Configure Cloudflare WAF Rate Limiting for api-health.splash-wonderland.com.
#
# Idempotent: deletes any existing rule whose description matches our marker
# before creating the new one, so re-running won't pile up duplicate rules.
#
# Required env:
#   CF_API_TOKEN — token with scope "Zone -> Zone WAF: Edit" + "Zone: Read"
#                  scoped to the splash-wonderland.com zone.
#
# Optional env:
#   CF_RL_PERIOD     — sliding window in seconds (default 60)
#   CF_RL_REQS       — request budget per IP per window (default 100)
#   CF_RL_MITIGATION — block duration in seconds (default 600)
#   CF_RL_HOST       — hostname to protect (default api-health.splash-wonderland.com)

set -euo pipefail

: "${CF_API_TOKEN:?Set CF_API_TOKEN to a Cloudflare API token with WAF scope}"

ZONE_NAME="splash-wonderland.com"
HOST="${CF_RL_HOST:-api-health.splash-wonderland.com}"
PERIOD="${CF_RL_PERIOD:-60}"
REQS="${CF_RL_REQS:-100}"
MITIGATION="${CF_RL_MITIGATION:-600}"
DESCRIPTION="bma-health: rate-limit ${HOST} ${REQS}r/${PERIOD}s per IP"

api() {
  curl --fail --silent --show-error \
    -H "Authorization: Bearer ${CF_API_TOKEN}" \
    -H "Content-Type: application/json" "$@"
}

echo ">> resolving zone id for ${ZONE_NAME}"
ZONE_ID=$(api "https://api.cloudflare.com/client/v4/zones?name=${ZONE_NAME}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"][0]["id"])')
echo "   zone id: ${ZONE_ID}"

echo ">> fetching http_ratelimit ruleset"
RULESET=$(api "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/rulesets/phases/http_ratelimit/entrypoint" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d["result"]))')
RULESET_ID=$(echo "$RULESET" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "   ruleset id: ${RULESET_ID}"

echo ">> removing any existing rule matching marker"
EXISTING=$(echo "$RULESET" | python3 -c "
import json, sys
rs = json.load(sys.stdin)
for r in rs.get('rules', []):
    if r.get('description') == '${DESCRIPTION}':
        print(r['id'])
")
for rid in $EXISTING; do
  echo "   deleting rule ${rid}"
  api -X DELETE "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/rulesets/${RULESET_ID}/rules/${rid}" >/dev/null
done

echo ">> creating rate-limit rule"
PAYLOAD=$(python3 -c "
import json
print(json.dumps({
  'action': 'block',
  'ratelimit': {
    'characteristics': ['ip.src'],
    'period': ${PERIOD},
    'requests_per_period': ${REQS},
    'mitigation_timeout': ${MITIGATION}
  },
  'expression': '(http.host eq \"${HOST}\")',
  'description': '${DESCRIPTION}',
  'enabled': True
}))
")
RESULT=$(api -X POST \
  "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/rulesets/${RULESET_ID}/rules" \
  -d "${PAYLOAD}")
RULE_ID=$(echo "$RESULT" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["result"]["rules"][-1]["id"])' 2>/dev/null || echo "?")
echo "   rule id: ${RULE_ID}"
echo
echo "Done. Rate limit active: ${REQS} req / ${PERIOD}s per IP on ${HOST}."
echo "Block duration after threshold: ${MITIGATION}s."
