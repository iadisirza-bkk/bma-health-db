#!/usr/bin/env bash
# =============================================================================
# Chat end-to-end smoke test (curl).
#
# Pings the live API + LMStudio path:
#   1. API health
#   2. LMStudio reachable on $LMSTUDIO_URL
#   3. /api/v2/chat   — create thread → stream → list messages → delete
#   4. /api/health/chat/stream  (legacy SSE)
#   5. /api/health/chat         (legacy sync)
#
# Exits non-zero on any failure; prints a summary at the end.
#
# Usage:
#   ./scripts/smoke_chat.sh                       # uses defaults
#   API_URL=https://prod ... ./scripts/smoke_chat.sh
# =============================================================================
set -uo pipefail

API_URL="${API_URL:-http://localhost:9002}"
API_KEY="${API_KEY:-dev-api-key}"
LMSTUDIO_URL="${LMSTUDIO_URL:-http://localhost:5555}"
QUESTION="${QUESTION:-ภาพรวมโรคทั้งหมด}"
STREAM_TIMEOUT="${STREAM_TIMEOUT:-90}"

PASS=0
FAIL=0
declare -a FAILURES

# -------- helpers ------------------------------------------------------------
green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$1"; }
gray()  { printf '\033[0;90m%s\033[0m\n' "$1"; }

ok() {
  PASS=$((PASS+1))
  green "✓ $1"
}
fail() {
  FAIL=$((FAIL+1))
  FAILURES+=("$1")
  red "✗ $1"
  [[ -n "${2:-}" ]] && gray "    $2"
}

step() { printf '\n──── %s ────\n' "$1"; }

# -------- 1. API health ------------------------------------------------------
step "1. API health"
# /health is unauthenticated; the X-API-Key middleware lets it through.
http_code=$(curl -s -o /dev/null -w '%{http_code}' "$API_URL/health")
if [[ "$http_code" == "200" ]]; then
  ok "API reachable (HTTP $http_code)"
else
  fail "API health check returned HTTP $http_code" "URL: $API_URL/health"
fi

# -------- 2. LMStudio --------------------------------------------------------
step "2. LMStudio"
lm_code=$(curl -s -o /dev/null -w '%{http_code}' "$LMSTUDIO_URL/v1/models" --max-time 5)
if [[ "$lm_code" == "200" ]]; then
  ok "LMStudio responding on $LMSTUDIO_URL"
else
  fail "LMStudio not reachable (HTTP $lm_code) — chat-stream tests will likely fail" "URL: $LMSTUDIO_URL"
fi

# -------- 3. /api/v2/chat full lifecycle -------------------------------------
step "3. /api/v2/chat — create → stream → list → delete"

create_resp=$(curl -s -X POST "$API_URL/api/v2/chat/threads" \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"first_message\": \"$QUESTION\"}")
thread_id=$(printf '%s' "$create_resp" | python3 -c "import json,sys; print(json.load(sys.stdin).get('thread_id',''))" 2>/dev/null)

if [[ -n "$thread_id" ]]; then
  ok "Created thread $thread_id"
else
  fail "Could not create thread" "Response: $create_resp"
  thread_id=""
fi

if [[ -n "$thread_id" ]]; then
  # Stream — capture into a temp file so we can grep for events.
  stream_file=$(mktemp)
  trap "rm -f '$stream_file'" EXIT

  curl -s -N --max-time "$STREAM_TIMEOUT" \
    -X POST "$API_URL/api/v2/chat/threads/$thread_id/stream" \
    -H "X-API-Key: $API_KEY" \
    -H 'Content-Type: application/json' \
    -H 'Accept: text/event-stream' \
    -d "{\"message\": \"$QUESTION\"}" \
    > "$stream_file" 2>&1 &
  curl_pid=$!

  # Wait for `event: done` or until timeout.
  waited=0
  while ! grep -q '^event: done' "$stream_file" 2>/dev/null; do
    sleep 1
    waited=$((waited+1))
    if [[ $waited -ge $STREAM_TIMEOUT ]] || ! kill -0 $curl_pid 2>/dev/null; then
      break
    fi
  done
  wait $curl_pid 2>/dev/null

  bytes=$(wc -c < "$stream_file" | tr -d ' ')
  thread_id_evt=$(grep -c '^event: thread_id' "$stream_file")
  token_evt=$(grep -c '^event: token' "$stream_file")
  done_evt=$(grep -c '^event: done' "$stream_file")
  error_evt=$(grep -c '^event: error' "$stream_file")

  if [[ $thread_id_evt -ge 1 ]]; then
    ok "Stream emitted thread_id event"
  else
    fail "Stream missing thread_id event" "first 200 bytes: $(head -c 200 "$stream_file")"
  fi

  if [[ $done_evt -ge 1 ]]; then
    ok "Stream completed with done event ($bytes bytes, ${waited}s)"
  else
    fail "Stream did not finish within ${STREAM_TIMEOUT}s" "got $token_evt token events, $error_evt error events"
  fi

  if [[ $token_evt -ge 1 ]]; then
    ok "Stream produced $token_evt token events"
  else
    fail "Stream produced no token events" "(LLM may be down or refusing)"
  fi

  if [[ $error_evt -gt 0 ]]; then
    err_line=$(grep -m1 '^event: error' -A1 "$stream_file" | tail -1)
    fail "Stream emitted $error_evt error event(s)" "$err_line"
  fi

  # List messages — must contain at least the user msg + assistant reply.
  list_resp=$(curl -s "$API_URL/api/v2/chat/threads/$thread_id" -H "X-API-Key: $API_KEY")
  msg_count=$(printf '%s' "$list_resp" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else len(d.get('data',d).get('messages',[])) if isinstance(d.get('data',d),dict) else 0)" 2>/dev/null || echo 0)

  if [[ "$msg_count" -ge 2 ]]; then
    ok "Thread has $msg_count persisted messages (user + assistant)"
  else
    fail "Expected >=2 persisted messages, got $msg_count" "Response: $(printf '%s' "$list_resp" | head -c 300)"
  fi

  # Delete (cleanup).
  del_code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
    "$API_URL/api/v2/chat/threads/$thread_id" -H "X-API-Key: $API_KEY")
  if [[ "$del_code" =~ ^(200|204)$ ]]; then
    ok "Thread deleted (HTTP $del_code)"
  else
    fail "Thread delete returned HTTP $del_code"
  fi
fi

# -------- 4. Legacy /api/health/chat/stream ---------------------------------
step "4. /api/health/chat/stream (legacy SSE)"

legacy_file=$(mktemp)
# Defensive: stream_file may be unset if section 3 was skipped due to early failure.
trap "rm -f '${stream_file:-}' '$legacy_file'" EXIT

curl -s -N --max-time "$STREAM_TIMEOUT" \
  "$API_URL/api/health/chat/stream?message=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$QUESTION")" \
  -H "X-API-Key: $API_KEY" \
  -H 'Accept: text/event-stream' \
  > "$legacy_file" 2>&1 &
curl_pid=$!

# Legacy stream uses `data: {"type":"done"}` (no `event:` line).
waited=0
while ! grep -q '"type":[[:space:]]*"done"' "$legacy_file" 2>/dev/null; do
  sleep 1
  waited=$((waited+1))
  if [[ $waited -ge $STREAM_TIMEOUT ]] || ! kill -0 $curl_pid 2>/dev/null; then
    break
  fi
done
wait $curl_pid 2>/dev/null

bytes=$(wc -c < "$legacy_file" | tr -d ' ')
content_count=$(grep -c '"type":[[:space:]]*"content"' "$legacy_file")
done_count=$(grep -c '"type":[[:space:]]*"done"' "$legacy_file")
err_count=$(grep -c '"type":[[:space:]]*"error"' "$legacy_file")

if [[ $done_count -ge 1 ]]; then
  ok "Legacy stream finished with done ($bytes bytes, ${waited}s)"
else
  fail "Legacy stream did not finish within ${STREAM_TIMEOUT}s"
fi
if [[ $content_count -ge 1 ]]; then
  ok "Legacy stream produced $content_count content chunks"
else
  fail "Legacy stream produced no content chunks"
fi
if [[ $err_count -gt 0 ]]; then
  err_line=$(grep -m1 '"type":[[:space:]]*"error"' "$legacy_file")
  fail "Legacy stream emitted $err_count error event(s)" "$err_line"
fi

# -------- 5. Legacy /api/health/chat (sync) ---------------------------------
step "5. /api/health/chat (sync fallback)"

sync_resp=$(curl -s --max-time "$STREAM_TIMEOUT" \
  "$API_URL/api/health/chat?message=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$QUESTION")" \
  -H "X-API-Key: $API_KEY")
sync_content=$(printf '%s' "$sync_resp" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('content','') or d.get('detail',''))" 2>/dev/null || true)

if [[ -n "$sync_content" ]]; then
  ok "Sync chat returned content (${#sync_content} chars)"
else
  fail "Sync chat returned empty/invalid response" "Response: $(printf '%s' "$sync_resp" | head -c 300)"
fi

# -------- summary ------------------------------------------------------------
echo
echo "============================================================"
printf '  %d passed,  %d failed\n' "$PASS" "$FAIL"
echo "============================================================"
if [[ $FAIL -gt 0 ]]; then
  echo "Failures:"
  for f in "${FAILURES[@]}"; do echo "  - $f"; done
  exit 1
fi
exit 0
