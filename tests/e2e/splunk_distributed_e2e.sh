#!/usr/bin/env bash
# End-to-end check of distributed=true across a search head and an indexer
# that is its search peer, against the stub Decisions API
# (tests/stub_decisions_api.py) running inside each container.
#
#   tests/e2e/splunk_distributed_e2e.sh <search-head> <indexer> <package.tar.gz>
#
# Both containers must be healthy docker.io/splunk/splunk with admin password
# "devcontainer", on one docker network where the search head reaches the
# indexer by its container name. The script installs the package on the search
# head only, restarts it so the add-on's distsearch.conf applies, adds the
# indexer as a search peer, configures the add-on on the search head with an
# API key long enough to be stored in several parts, and indexes events on the
# indexer. It then checks that without distributed=true the search head makes
# every API call, that distributed=true rejects cache=true and limit, and
# that the search head still checks the user's capabilities.
#
# What the indexer does with distributed=true depends on the pair:
# - If the two share a splunk.secret (give both containers the same
#   SPLUNK_SECRET before their first start), the indexer reads the search
#   head's settings from the knowledge bundle, decrypts its key and calls its
#   own stub with it, for decide, choose and grade, and for a role that has
#   only the documented capabilities.
# - Otherwise the indexer fails with the could-not-decrypt message. The script
#   then installs the add-on on the indexer, configures it there with its own
#   key, and checks that the indexer calls the API with that key.
set -euo pipefail

SH="${1:?usage: splunk_distributed_e2e.sh <search-head> <indexer> <package.tar.gz>}"
IDX="${2:?usage: splunk_distributed_e2e.sh <search-head> <indexer> <package.tar.gz>}"
PACKAGE="${3:?usage: splunk_distributed_e2e.sh <search-head> <indexer> <package.tar.gz>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=TA-openai-decisions
AUTH=admin:devcontainer
# Over 255 characters, so Splunk's credential store holds it in two parts.
SH_KEY="sk-e2e-sh-$(od -An -N150 -tx1 /dev/urandom | tr -d ' \n')"
IDX_KEY="sk-e2e-idx-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
STUB_PORT=8765
FAILURES=0

log() { printf '\n== %s\n' "$*"; }
pass() { printf 'PASS %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
dexec() { local c="$1"; shift; docker exec -u splunk "$c" "$@"; }

# splunkd REST call inside a container: rest <container> <method> <path> [curl args...]
rest() {
  local c="$1" method="$2" path="$3"
  shift 3
  dexec "$c" curl -sk -u "$AUTH" -X "$method" "https://localhost:8089$path" "$@"
}

# Run a blocking search job on the search head and print its results as JSON
# lines (one object per row), followed by one {"_message": ...} line per job message.
search() {
  local user_auth="${2:-$AUTH}" sid
  sid="$(dexec "$SH" curl -sk -u "$user_auth" https://localhost:8089/services/search/jobs \
    --data-urlencode "search=$1" -d exec_mode=blocking -d output_mode=json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("sid",""))')"
  if [ -z "$sid" ]; then echo '{"_message": {"type": "ERROR", "text": "job not created"}}'; return; fi
  {
    dexec "$SH" curl -sk -u "$user_auth" "https://localhost:8089/services/search/jobs/$sid/results?output_mode=json&count=0"
    echo
    dexec "$SH" curl -sk -u "$user_auth" "https://localhost:8089/services/search/jobs/$sid?output_mode=json"
  } | python3 -c '
import json, sys
results, job = [l for l in sys.stdin.read().split("\n") if l.strip()][:2]
for row in json.loads(results).get("results", []):
    print(json.dumps(row, sort_keys=True))
for m in json.loads(job)["entry"][0]["content"].get("messages") or []:
    print(json.dumps({"_message": m}, sort_keys=True))
'
}

stub_start() {
  local c="$1" key="$2"
  docker cp "$HERE/../stub_decisions_api.py" "$c:/tmp/stub_decisions_api.py"
  dexec "$c" pkill -f stub_decisions_api.py || true
  # A stub from an earlier run that still holds the port would answer with its own key.
  for _ in $(seq 20); do stub_stats "$c" >/dev/null 2>&1 || break; sleep 0.5; done
  docker exec -d -u splunk "$c" /opt/splunk/bin/python3 /tmp/stub_decisions_api.py \
    --port "$STUB_PORT" --api-key "$key"
  for _ in $(seq 20); do stub_stats "$c" >/dev/null 2>&1 && break; sleep 0.5; done
}
stub_stats() { dexec "$1" curl -s "http://127.0.0.1:$STUB_PORT/v1/stats"; }
stub_reset() { for c in "$SH" "$IDX"; do dexec "$c" curl -s -X POST "http://127.0.0.1:$STUB_PORT/v1/reset" >/dev/null; done; }
# Both stubs' stats, as {"sh": ..., "idx": ...}.
both_stats() { printf '{"sh": %s, "idx": %s}' "$(stub_stats "$SH")" "$(stub_stats "$IDX")"; }

settings() {
  local c="$1" stanza="$2"
  shift 2
  rest "$c" POST "/servicesNS/nobody/$APP/ta_openai_decisions_settings/$stanza" "$@" -o /dev/null -w '%{http_code}'
}

# expect <description> <python expression> <rows> [data]
# The expression sees `rows` (search result rows as dicts), `messages` (search
# job messages), and `data` (parsed JSON).
expect() {
  local desc="$1" expr="$2" rows="$3" data="${4:-{\}}"
  if ROWS="$rows" DATA="$data" python3 -c '
import json, os, sys
lines = [json.loads(l) for l in os.environ["ROWS"].splitlines() if l.strip()]
rows = [l for l in lines if "_message" not in l]
messages = [l["_message"] for l in lines if "_message" in l]
data = json.loads(os.environ["DATA"])
sys.exit(0 if ('"$expr"') else 1)
'; then pass "$desc"; else fail "$desc"; printf '%s\n' "$rows" "$data" | head -40; fi
}

wait_for_splunkd() {
  for _ in $(seq 60); do
    [ "$(rest "$1" GET /services/server/info -o /dev/null -w '%{http_code}' 2>/dev/null || true)" = 200 ] && return 0
    sleep 5
  done
  return 1
}

restart() {
  dexec "$1" /opt/splunk/bin/splunk restart >/dev/null 2>&1
  wait_for_splunkd "$1" && pass "$1 restarted" || fail "$1 restarted"
}

secret_hash() { dexec "$1" sha256sum /opt/splunk/etc/auth/splunk.secret | cut -d' ' -f1; }
if [ "$(secret_hash "$SH")" = "$(secret_hash "$IDX")" ]; then SHARED=1; else SHARED=0; fi
log "pair: search head $SH, indexer $IDX, shared splunk.secret: $([ $SHARED = 1 ] && echo yes || echo no)"

log "install $PACKAGE on the search head only"
if dexec "$IDX" test -d "/opt/splunk/etc/apps/$APP"; then
  dexec "$IDX" /opt/splunk/bin/splunk remove app "$APP" -auth "$AUTH" >/dev/null
  restart "$IDX"
fi
docker cp "$PACKAGE" "$SH:/tmp/$APP.tar.gz"
dexec "$SH" /opt/splunk/bin/splunk install app "/tmp/$APP.tar.gz" -update 1 -auth "$AUTH"
restart "$SH"

log "add the indexer as a search peer"
if ! rest "$SH" GET "/services/search/distributed/peers?output_mode=json" | grep -q "\"$IDX:8089\""; then
  dexec "$SH" /opt/splunk/bin/splunk add search-server "https://$IDX:8089" -auth "$AUTH" \
    -remoteUsername admin -remotePassword devcontainer
fi
peer_status() {
  rest "$SH" GET "/services/search/distributed/peers?output_mode=json" \
    | python3 -c 'import json,sys; print(" ".join(e["content"]["status"] for e in json.load(sys.stdin)["entry"]))'
}
for _ in $(seq 30); do [ "$(peer_status)" = Up ] && break; sleep 2; done
[ "$(peer_status)" = Up ] && pass "indexer is an Up search peer" || fail "indexer is an Up search peer ($(peer_status))"

log "start the stub Decisions API in both containers"
stub_start "$SH" "$SH_KEY"
# With a shared splunk.secret the indexer uses the search head's key; otherwise its own.
if [ $SHARED = 1 ]; then stub_start "$IDX" "$SH_KEY"; else stub_start "$IDX" "$IDX_KEY"; fi

log "configure the add-on on the search head"
[ "$(settings "$SH" openai -d api_key="$SH_KEY" -d base_url="http://127.0.0.1:$STUB_PORT/v1" \
  -d request_timeout=5 -d max_concurrency=4)" = 200 ] && pass "openai settings saved" || fail "openai settings saved"
[ "$(settings "$SH" cache -d cache_enabled=1)" = 200 ] && pass "cache on" || fail "cache on"
[ "$(settings "$SH" logging -d loglevel=DEBUG)" = 200 ] && pass "log level saved" || fail "log level saved"
parts="$(dexec "$SH" sh -c "grep -c '^\[credential:.*#$APP#configs/conf-ta_openai_decisions_settings:openai' /opt/splunk/etc/apps/$APP/local/passwords.conf")"
expect "the key is stored in 2 parts and an end mark" 'data == 3' "" "$parts"

log "index events on the indexer"
SRC="e2e_dist_$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
for input in "user bob login failed" "user alice logged in" "user carol login failed" \
  "user bob login failed" "service restarted" "REFUSE this"; do
  rest "$IDX" POST "/services/receivers/simple?index=main&sourcetype=e2e_dist&source=$SRC" \
    --data-binary "$input" -o /dev/null
done
for _ in $(seq 30); do
  indexed="$(search "search index=main source=$SRC | stats count" | python3 -c 'import json,sys; print(json.loads(sys.stdin.readline())["count"])' 2>/dev/null || true)"
  [ "$indexed" = 6 ] && break
  sleep 2
done
[ "$indexed" = 6 ] && pass "6 events indexed on the indexer" || fail "6 events indexed on the indexer (got ${indexed:-none})"

log "the knowledge bundle on the indexer carries lib/ and the add-on's current settings"
SETTINGS_FILES="local/passwords.conf local/ta_openai_decisions_settings.conf default/ta_openai_decisions_settings.conf"
# The bundle that the indexer used for the latest search from the search head.
# A newly replicated bundle can sit on the indexer for a while before the
# search head has searches use it.
used_bundle() {
  dexec "$IDX" sh -c "d=\$(ls -td /opt/splunk/var/run/splunk/dispatch/remote_${SH}_* 2>/dev/null | head -1); [ -n \"\$d\" ] && sed -n 's/.*bs-pathname=\"\\([^\"]*\\)\".*/\\1/p' \"\$d/search.log\" | head -1" || true
}
bundle_sums() {
  local b
  b="$(used_bundle)"
  [ -n "$b" ] && dexec "$IDX" sh -c "cd '$b/apps/$APP' 2>/dev/null && test -d lib/splunklib && sha256sum $SETTINGS_FILES" 2>/dev/null || true
}
sh_sums() { dexec "$SH" sh -c "cd /opt/splunk/etc/apps/$APP && sha256sum $SETTINGS_FILES"; }
for _ in $(seq 60); do
  search "search index=main source=$SRC | head 1" >/dev/null
  [ "$(bundle_sums)" = "$(sh_sums)" ] && break
  sleep 5
done
[ "$(bundle_sums)" = "$(sh_sums)" ] && pass "searches use a bundle with lib/ and the search head's current settings and passwords.conf" \
  || fail "searches use a bundle with lib/ and the search head's current settings and passwords.conf"
other="$(dexec "$IDX" sh -c "find '$(used_bundle)' -name passwords.conf -not -path '*/apps/$APP/*' | wc -l")"
expect "no other app's passwords.conf is replicated" 'data == 0' "" "$other"

EVENTS="search index=main source=$SRC"
DISTINCT='{"user bob login failed": 1, "user alice logged in": 1, "user carol login failed": 1, "service restarted": 1, "REFUSE this": 1}'

log "without distributed=true the search head makes every API call"
stub_reset
rows="$(search "$EVENTS | decide \"Is this a failed login?\" cache=false | table _raw splunk_server decision_probability decision_error")"
printf '%s\n' "$rows"
stats="$(both_stats)"
expect "6 rows from the indexer" 'len(rows) == 6 and {r["splunk_server"] for r in rows} == {"'"$IDX"'"}' "$rows"
expect "every row decided on the search head" \
  'data["sh"]["by_input"] == '"$DISTINCT"' and data["idx"]["requests"] == 0' "$rows" "$stats"

log "distributed=true rejects cache=true and limit, before any API call"
stub_reset
rows="$(search "$EVENTS | decide \"q?\" distributed=true cache=true")"
expect "cache=true error" 'any("decide: cache=true can'"'"'t be used with distributed=true" in m["text"] and m["type"] in ("ERROR", "FATAL") for m in messages) and not rows' "$rows"
rows="$(search "$EVENTS | choose \"Which?\" a A b B distributed=t limit=5")"
expect "limit error" 'any("choose: limit can'"'"'t be used with distributed=true" in m["text"] and m["type"] in ("ERROR", "FATAL") for m in messages) and not rows' "$rows"
expect "no API calls" 'data["sh"]["requests"] == 0 and data["idx"]["requests"] == 0' "" "$(both_stats)"

log "the search head checks the user's capabilities before distributing"
rest "$SH" POST /services/authentication/users -d name=e2edistuser -d password=devcontainer1 -d roles=user -o /dev/null || true
rows="$(search "$EVENTS | decide \"q?\" distributed=true" e2edistuser:devcontainer1)"
printf '%s\n' "$rows"
expect "permission error names both capabilities" \
  'any("list_storage_passwords and list_settings capabilities" in m["text"] for m in messages) and not any("[" + "'"$IDX"'" + "]" in m["text"] for m in messages)' "$rows"

idx_log() { dexec "$IDX" sh -c "grep -h 'on a search peer' /opt/splunk/var/log/splunk/ta_openai_decisions_$1.log 2>/dev/null | tail -1" || true; }

if [ $SHARED = 1 ]; then
  log "shared splunk.secret: the indexer decrypts the search head's key from the knowledge bundle"
  stub_reset
  rows="$(search "$EVENTS | decide \"Is this a failed login?\" distributed=true | table _raw splunk_server decision_probability decision_error decision_from_cache")"
  printf '%s\n' "$rows"
  stats="$(both_stats)"
  expect "6 rows, decided on the indexer" \
    'len(rows) == 6 and all(r["splunk_server"] == "'"$IDX"'" for r in rows) and sorted(r.get("decision_probability") or "-" for r in rows) == ["-", "0.05", "0.05", "0.95", "0.95", "0.95"]' "$rows"
  expect "a refusal is a per-event error, reported with the indexer's name" \
    '[r.get("decision_error") for r in rows if r["_raw"] == "REFUSE this"] == ["refusal"] and any(m["text"] == "['"$IDX"'] decide: some events could not be decided: refusal" for m in messages)' "$rows"
  expect "the cache is off: no decision_from_cache" 'not any("decision_from_cache" in r for r in rows)' "$rows"
  expect "the indexer's stub got every call with the search head's key, the search head's none" \
    'data["idx"]["by_input"] == '"$DISTINCT"' and data["sh"]["requests"] == 0' "$rows" "$stats"
  expect "the indexer logged the bundle as the source" '"settings from the knowledge bundle" in data' "" "$(idx_log decide | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"

  stub_reset
  rows="$(search "$EVENTS | choose \"Did the login fail?\" other \"Anything else\" failed \"The login failed\" distributed=t | table _raw splunk_server choice choice_error")"
  stats="$(both_stats)"
  expect "choose distributed: options chosen on the indexer" \
    'len(rows) == 6 and sorted(r.get("choice") or "-" for r in rows) == ["-", "failed", "failed", "failed", "other", "other"]' "$rows"
  expect "choose distributed: the indexer made the calls" 'data["idx"]["requests"] == 5 and data["sh"]["requests"] == 0' "$rows" "$stats"
  stub_reset
  rows="$(search "$EVENTS | grade \"How bad?\" fine \"Routine\" failed \"A failure\" distributed=t | table _raw splunk_server grade grade_error")"
  stats="$(both_stats)"
  expect "grade distributed: graded on the indexer" \
    'len(rows) == 6 and sorted(r.get("grade") or "-" for r in rows) == ["-", "0.2", "0.2", "0.8", "0.8", "0.8"]' "$rows"
  expect "grade distributed: the indexer made the calls" 'data["idx"]["requests"] == 5 and data["sh"]["requests"] == 0' "$rows" "$stats"

  log "a role with only the documented capabilities runs a distributed search"
  rest "$SH" POST /services/authorization/roles -d name=e2e_dist -o /dev/null || true
  rest "$SH" POST /services/authorization/roles/e2e_dist -d imported_roles=user \
    -d capabilities=list_storage_passwords -d capabilities=list_settings -o /dev/null
  rest "$SH" POST /services/authentication/users -d name=e2edist -d password=devcontainer1 -d roles=e2e_dist -o /dev/null || true
  stub_reset
  rows="$(search "$EVENTS | decide \"Is this a failed login?\" distributed=true | table splunk_server decision_probability" e2edist:devcontainer1)"
  stats="$(both_stats)"
  expect "role: decided on the indexer" 'len(rows) == 6 and sum(1 for r in rows if r.get("decision_probability")) == 5' "$rows"
  expect "role: the indexer made the calls" 'data["idx"]["requests"] == 5 and data["sh"]["requests"] == 0' "$rows" "$stats"
else
  log "separate splunk.secret, add-on only on the search head: the could-not-decrypt message"
  stub_reset
  rows="$(search "$EVENTS | decide \"Is this a failed login?\" distributed=true | table _raw splunk_server decision_probability")"
  printf '%s\n' "$rows"
  expect "the indexer's error names both remedies" \
    'any(m["text"].startswith("['"$IDX"'] decide: could not decrypt the API key from the search head, and the add-on isn'"'"'t configured on this search peer. For distributed=true, indexers need the search head'"'"'s splunk.secret, or the add-on installed and configured on each one.") for m in messages)' "$rows"
  expect "no API calls" 'data["sh"]["requests"] == 0 and data["idx"]["requests"] == 0' "$rows" "$(both_stats)"

  log "separate splunk.secret, add-on installed and configured on the indexer: it uses its own key"
  docker cp "$PACKAGE" "$IDX:/tmp/$APP.tar.gz"
  dexec "$IDX" /opt/splunk/bin/splunk install app "/tmp/$APP.tar.gz" -update 1 -auth "$AUTH"
  restart "$IDX"
  [ "$(settings "$IDX" openai -d api_key="$IDX_KEY" -d base_url="http://127.0.0.1:$STUB_PORT/v1")" = 200 ] \
    && pass "indexer openai settings saved" || fail "indexer openai settings saved"
  for _ in $(seq 30); do [ "$(peer_status)" = Up ] && break; sleep 2; done
  stub_reset
  rows="$(search "$EVENTS | decide \"Is this a failed login?\" distributed=true | table _raw splunk_server decision_probability decision_error")"
  printf '%s\n' "$rows"
  stats="$(both_stats)"
  expect "6 rows, decided on the indexer" \
    'len(rows) == 6 and all(r["splunk_server"] == "'"$IDX"'" for r in rows) and sum(1 for r in rows if r.get("decision_probability")) == 5' "$rows"
  expect "the indexer's stub got every call with the indexer's key, the search head's none" \
    'data["idx"]["by_input"] == '"$DISTINCT"' and data["sh"]["requests"] == 0' "$rows" "$stats"
  expect "the indexer logged its own configuration as the source" \
    '"settings from this search peer'"'"'s configuration, because could not decrypt the API key from the search head" in data' "" \
    "$(idx_log decide | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
fi

log "logs: the API keys never appear"
for c in "$SH" "$IDX"; do
  if dexec "$c" sh -c "grep -rlF -e '$SH_KEY' -e '$IDX_KEY' /opt/splunk/var/log/splunk /opt/splunk/var/run/splunk/dispatch /opt/splunk/var/run/searchpeers 2>/dev/null | grep -v -e audit.log -e configuration_change.log"; then
    fail "$c: API key found in logs, search artifacts or bundles"
  else
    pass "$c: API keys absent from logs, search artifacts and bundles"
  fi
done

for c in "$SH" "$IDX"; do dexec "$c" pkill -f stub_decisions_api.py || true; done
log "result"
if [ "$FAILURES" -eq 0 ]; then echo "E2E_RESULT: PASS"; else echo "E2E_RESULT: FAIL ($FAILURES)"; exit 1; fi
