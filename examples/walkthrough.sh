#!/usr/bin/env bash
# A walk through the HTTP API with curl. Start a server first, for example:
#
#   export FLAGS_ADMIN_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
#   FLAGS_DATABASE=/tmp/walkthrough.db python -m flagservice.server &
#   ./examples/walkthrough.sh
#
# Each step prints the command and the server's answer as Markdown;
# examples/walkthrough.md holds a recorded run. The secret part of API keys is replaced by
# <secret> in the printed output. Needs bash, curl and python3.
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}
: "${FLAGS_ADMIN_KEY:?export FLAGS_ADMIN_KEY first}"
LAST=$(mktemp -d)
trap 'rm -rf "$LAST"' EXIT

# Print a Markdown section: the title, the command (read from stdin) and the
# response with its status line, the headers that matter here and the body.
step() {
    local title=$1 command
    command=$(cat)
    printf '### %s\n\n```console\n$ %s\n' "$title" "$command"
    rm -f "$LAST/headers" "$LAST/body"
    eval "$command" -sS -D "$LAST/headers" -o "$LAST/body"
    python3 - "$LAST/headers" "$LAST/body" <<'PY'
import json, os, re, sys
headers = open(sys.argv[1]).read().splitlines()
print(headers[0].strip())
for line in headers[1:]:
    if line.split(":", 1)[0].lower() in ("etag", "location", "www-authenticate", "cache-control"):
        print(line.strip())
body = open(sys.argv[2]).read() if os.path.exists(sys.argv[2]) else ""
if body:
    text = json.dumps(json.loads(body), indent=2)
    print(re.sub(r"((?:srv|sdk)\.env_[0-9a-f]{12}\.key_[0-9a-f]{12}\.)[A-Za-z0-9_-]{43}", r"\1<secret>", text))
PY
    printf '```\n\n'
}

field() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$LAST/body" "$1"; }
etag() { grep -i '^etag:' "$LAST/headers" | cut -d' ' -f2- | tr -d '\r'; }
current_etag() {
    curl -sS -o /dev/null -D - "$BASE/v1/projects/shop/flags/$1" \
        -H "Authorization: Bearer $FLAGS_ADMIN_KEY" | grep -i '^etag:' | cut -d' ' -f2- | tr -d '\r'
}

echo "## Walkthrough"
echo
echo "Real responses from a fresh server; ids, salts, times and ETags differ on every run."
echo

step "Create a project and two environments" <<'CMD'
curl -X POST "$BASE/v1/projects" -H "Authorization: Bearer $FLAGS_ADMIN_KEY" \
  -H "Content-Type: application/json" -d '{"key": "shop", "name": "Web shop"}'
CMD
for env in staging production; do
    curl -sS -o /dev/null -X POST "$BASE/v1/projects/shop/environments" \
        -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
        -d "{\"key\": \"$env\", \"name\": \"$env\"}"
done

step "List the environments (each has a public id)" <<'CMD'
curl "$BASE/v1/projects/shop/environments" -H "Authorization: Bearer $FLAGS_ADMIN_KEY"
CMD

step "Create a server key for production (the token is shown only here)" <<'CMD'
curl -X POST "$BASE/v1/projects/shop/environments/production/keys" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"kind": "server", "name": "checkout backend"}'
CMD
SERVER_KEY=$(field token)

step "Create an SDK key for production" <<'CMD'
curl -X POST "$BASE/v1/projects/shop/environments/production/keys" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"kind": "sdk", "name": "web app"}'
CMD
SDK_KEY=$(field token)
ENV_ID=$(python3 -c 'import sys; print(sys.argv[1].split(".")[1])' "$SDK_KEY")

step "Create a boolean flag (variants default to off and on)" <<'CMD'
curl -X POST "$BASE/v1/projects/shop/flags" -H "Authorization: Bearer $FLAGS_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key": "new-checkout", "type": "boolean", "description": "One-page checkout"}'
CMD
ETAG=$(etag)

step "Turn it on in production: staff always, 10% of everyone else (needs If-Match)" <<'CMD'
curl -X PUT "$BASE/v1/projects/shop/flags/new-checkout/environments/production" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -H "If-Match: $ETAG" -d '{
    "enabled": true,
    "default_variant": "off",
    "rules": [
      {"conditions": [{"attribute": "email", "operator": "contains", "value": "@shop.example"}],
       "variant": "on"},
      {"split": [{"variant": "on", "weight": 10}, {"variant": "off", "weight": 90}]}
    ]
  }'
CMD

step "A second writer still holding the old ETag gets 412 and the current version" <<'CMD'
curl -X PUT "$BASE/v1/projects/shop/flags/new-checkout/environments/production" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -H "If-Match: $ETAG" -d '{"enabled": false, "default_variant": "off"}'
CMD

step "Without If-Match the server answers 428" <<'CMD'
curl -X PUT "$BASE/v1/projects/shop/flags/new-checkout/environments/production" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"enabled": false, "default_variant": "off"}'
CMD

ETAG=$(current_etag new-checkout)
step "With the current ETag, a rule that serves an unknown variant is refused with 422" <<'CMD'
curl -X PUT "$BASE/v1/projects/shop/flags/new-checkout/environments/staging" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -H "If-Match: $ETAG" \
  -d '{"enabled": true, "default_variant": "off", "rules": [{"variant": "maybe"}]}'
CMD

step "Evaluate on the server: a staff member matches the first rule" <<'CMD'
curl -X POST "$BASE/v1/evaluate" -H "Authorization: Bearer $SERVER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"flag": "new-checkout", "context": {"key": "user-17", "email": "ana@shop.example"}}'
CMD

step "Anyone else is bucketed by key into the 10% split" <<'CMD'
curl -X POST "$BASE/v1/evaluate" -H "Authorization: Bearer $SERVER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"flag": "new-checkout", "context": {"key": "user-42", "email": "bo@mail.example"}}'
CMD

step "Without a key the split cannot place the context, so the default is served" <<'CMD'
curl -X POST "$BASE/v1/evaluate" -H "Authorization: Bearer $SERVER_KEY" \
  -H "Content-Type: application/json" -d '{"flag": "new-checkout", "context": {}}'
CMD

step "An SDK key cannot call the evaluation endpoint" <<'CMD'
curl -X POST "$BASE/v1/evaluate" -H "Authorization: Bearer $SDK_KEY" \
  -H "Content-Type: application/json" -d '{"flag": "new-checkout", "context": {}}'
CMD

step "The SDK downloads the whole ruleset of its environment" <<'CMD'
curl "$BASE/v1/environments/$ENV_ID/config" -H "Authorization: Bearer $SDK_KEY"
CMD
CONFIG_ETAG=$(etag)

step "and polls with If-None-Match; nothing changed, so 304 and no body" <<'CMD'
curl "$BASE/v1/environments/$ENV_ID/config" -H "Authorization: Bearer $SDK_KEY" \
  -H "If-None-Match: $CONFIG_ETAG"
CMD

step "The audit log, newest first, one entry per page" <<'CMD'
curl "$BASE/v1/projects/shop/audit?flag=new-checkout&limit=1" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY"
CMD
CURSOR=$(field next_cursor)

step "The next page, through the opaque cursor" <<'CMD'
curl "$BASE/v1/projects/shop/audit?flag=new-checkout&limit=1&cursor=$CURSOR" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY"
CMD
