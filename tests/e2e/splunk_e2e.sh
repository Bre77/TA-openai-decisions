#!/usr/bin/env bash
# End-to-end check of the built add-on in a real Splunk container, against the
# local stub Decisions API (tests/stub_decisions_api.py) running inside it.
#
#   tests/e2e/splunk_e2e.sh <container> <package.tar.gz>
#
# The container must be a healthy docker.io/splunk/splunk with admin password
# "devcontainer" (e.g. SplunkUI-devcontainer's test-harness container). The
# script installs the package, restarts Splunk, and asserts the decide
# command's behaviour: the positional and prompt= forms of the prompt,
# probabilities, per-event errors that do not stop the search, the where-clause
# filter, field/output options, the KV store cache and decision_from_cache, an
# invalid key short-circuit, and that the API key never reaches a log.
set -euo pipefail

CONTAINER="${1:?usage: splunk_e2e.sh <container> <package.tar.gz>}"
PACKAGE="${2:?usage: splunk_e2e.sh <container> <package.tar.gz>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=TA-openai-decisions
AUTH=admin:devcontainer
API_KEY="sk-e2e-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
STUB_PORT=8765
FAILURES=0

log() { printf '\n== %s\n' "$*"; }
pass() { printf 'PASS %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
dexec() { docker exec -u splunk "$CONTAINER" "$@"; }

# splunkd REST call inside the container: rest <method> <path> [curl args...]
rest() {
  local method="$1" path="$2"
  shift 2
  dexec curl -sk -u "$AUTH" -X "$method" "https://localhost:8089$path" "$@"
}

# Run a blocking search job and print its results as JSON lines (one object
# per row), followed by one {"_message": ...} line per job message.
search() {
  local user_auth="${2:-$AUTH}" sid
  sid="$(dexec curl -sk -u "$user_auth" https://localhost:8089/services/search/jobs \
    --data-urlencode "search=$1" -d exec_mode=blocking -d output_mode=json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("sid",""))')"
  if [ -z "$sid" ]; then echo '{"_message": {"type": "ERROR", "text": "job not created"}}'; return; fi
  {
    dexec curl -sk -u "$user_auth" "https://localhost:8089/services/search/jobs/$sid/results?output_mode=json&count=0"
    echo
    dexec curl -sk -u "$user_auth" "https://localhost:8089/services/search/jobs/$sid?output_mode=json"
  } | python3 -c '
import json, sys
results, job = [l for l in sys.stdin.read().split("\n") if l.strip()][:2]
for row in json.loads(results).get("results", []):
    print(json.dumps(row, sort_keys=True))
for m in json.loads(job)["entry"][0]["content"].get("messages") or []:
    print(json.dumps({"_message": m}, sort_keys=True))
'
}

stub_stats() { dexec curl -s "http://127.0.0.1:$STUB_PORT/v1/stats"; }
stub_reset() { dexec curl -s -X POST "http://127.0.0.1:$STUB_PORT/v1/reset" >/dev/null; }

settings() {
  rest POST "/servicesNS/nobody/$APP/ta_openai_decisions_settings/$1" "${@:2}" -o /dev/null -w '%{http_code}'
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

log "install $PACKAGE"
docker cp "$PACKAGE" "$CONTAINER:/tmp/$APP.tar.gz"
dexec /opt/splunk/bin/splunk install app "/tmp/$APP.tar.gz" -update 1 -auth "$AUTH"
dexec /opt/splunk/bin/splunk restart >/dev/null 2>&1
for _ in $(seq 60); do
  kv_status="$(rest GET "/services/kvstore/status?output_mode=json" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["entry"][0]["content"]["current"]["status"])' 2>/dev/null || true)"
  [ "$kv_status" = ready ] && break
  sleep 5
done
[ "$kv_status" = ready ] && pass "KV store ready" || fail "KV store ready (status: ${kv_status:-unknown})"
dexec /opt/splunk/bin/splunk btool check --app="$APP" 2>&1 | grep -v '^$' || true
expect "app registered" 'rows[0]["name"] == "'$APP'"' \
  "$(rest GET "/services/apps/local/$APP?output_mode=json" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["entry"][0]))')"

log "start stub Decisions API in the container"
docker cp "$HERE/../stub_decisions_api.py" "$CONTAINER:/tmp/stub_decisions_api.py"
dexec pkill -f stub_decisions_api.py || true
docker exec -d -u splunk "$CONTAINER" /opt/splunk/bin/python3 /tmp/stub_decisions_api.py \
  --port "$STUB_PORT" --api-key "$API_KEY"
for _ in $(seq 20); do stub_stats >/dev/null 2>&1 && break; sleep 0.5; done

log "configure the add-on through its UCC REST handlers"
[ "$(settings openai -d api_key="$API_KEY" -d base_url="http://127.0.0.1:$STUB_PORT/v1" \
  -d request_timeout=5 -d max_concurrency=4)" = 200 ] && pass "openai settings saved" || fail "openai settings saved"
[ "$(settings cache -d cache_enabled=1 -d ttl_days=7 -d max_entries=1000000)" = 200 ] \
  && pass "cache settings saved" || fail "cache settings saved"
[ "$(settings logging -d loglevel=DEBUG)" = 200 ] && pass "log level saved" || fail "log level saved"
conf_key="$(dexec sh -c "grep -h api_key /opt/splunk/etc/apps/$APP/local/ta_openai_decisions_settings.conf || true")"
case "$conf_key" in
  *"$API_KEY"*) fail "api_key stored encrypted (found cleartext in conf)" ;;
  *) pass "api_key not stored in cleartext conf ($conf_key)" ;;
esac
rest DELETE "/servicesNS/nobody/$APP/storage/collections/data/ta_openai_decisions_cache" >/dev/null

BASE='| makeresults count=6 | streamstats count as n
| eval _raw=case(n=1,"user bob login failed", n=2,"user alice logged in", n=3,"REFUSE this",
                 n=4,"HTTP500 boom", n=5,"user bob login failed", n=6,"BADJSON")'

log "decide: probabilities and per-event errors (positional prompt)"
stub_reset
rows="$(search "$BASE | decide \"Is this a failed login?\" | table n _raw decision_probability decision_error decision_from_cache")"
printf '%s\n' "$rows"
stats="$(stub_stats)"
expect "all 6 events returned in order" '[r["n"] for r in rows] == ["1","2","3","4","5","6"]' "$rows"
expect "probabilities" '[r.get("decision_probability") for r in rows] == ["0.95","0.05",None,None,"0.95",None]' "$rows"
expect "errors" '[r.get("decision_error") for r in rows] == [None, None, "refusal", "HTTP 500: The server had an error while processing your request.", None, "invalid response: body is not JSON"]' "$rows"
expect "duplicate input decided once; HTTP 500 retried" \
  'data["by_input"] == {"user bob login failed": 1, "user alice logged in": 1, "REFUSE this": 1, "HTTP500 boom": 3, "BADJSON": 1}' "$rows" "$stats"
expect "request body shape" \
  'all(b["model"] == "gpt-6-luna" and b["questions"] == [{"type": "predicate", "name": "decision", "instructions": "Is this a failed login?"}] for b in data["bodies"])' "$rows" "$stats"
expect "search warnings surfaced" 'sum("some events could not be decided" in m["text"] for m in messages) == 3' "$rows"
expect "first run: decision_from_cache false on every event" '[r.get("decision_from_cache") for r in rows] == ["false"] * 6' "$rows"

log "decide: repeat run is served from the KV store cache, and expired entries are purged"
rest POST "/servicesNS/nobody/$APP/storage/collections/data/ta_openai_decisions_cache" \
  -H 'Content-Type: application/json' -d '{"_key": "expired-e2e", "probability": 0.5, "created": 1}' >/dev/null
stub_reset
rows="$(search "$BASE | decide prompt=\"Is this a failed login?\" | table n decision_probability decision_error decision_from_cache")"
stats="$(stub_stats)"
expect "same probabilities from cache" '[r.get("decision_probability") for r in rows] == ["0.95","0.05",None,None,"0.95",None]' "$rows"
expect "repeat run: decision_from_cache true on cache hits, false on errors" \
  '[r.get("decision_from_cache") for r in rows] == ["true","true","false","false","true","false"]' "$rows"
expect "only uncached failures hit the API" \
  'data["by_input"] == {"REFUSE this": 1, "HTTP500 boom": 3, "BADJSON": 1}' "$rows" "$stats"
kv="$(rest GET "/servicesNS/nobody/$APP/storage/collections/data/ta_openai_decisions_cache?output_mode=json")"
expect "KV store holds exactly the 2 successful results (expired entry purged)" \
  'len(data) == 2 and sorted(d["probability"] for d in data) == [0.05, 0.95] and all(len(d["_key"]) == 64 and d["created"] > 0 for d in data)' "" "$kv"

log "decide: cache=false bypasses the cache"
stub_reset
rows="$(search "$BASE | decide cache=false \"Is this a failed login?\" | table n decision_probability decision_from_cache")"
stats="$(stub_stats)"
expect "cache=false calls the API for every distinct input" \
  'data["by_input"].get("user bob login failed") == 1 and data["by_input"].get("user alice logged in") == 1' "$rows" "$stats"
expect "cache=false: no decision_from_cache field" 'len(rows) == 6 and all("decision_from_cache" not in r for r in rows)' "$rows"

log "decide | where filters on the probability"
rows="$(search "$BASE | decide \"Is this a failed login?\" | where decision_probability > 0.8 | table n")"
expect "where keeps only the two failed logins" '[r["n"] for r in rows] == ["1","5"]' "$rows"

log "decide field= output= error_field= model="
stub_reset
rows="$(search '| makeresults count=2 | streamstats count as n | eval msg=if(n=1,"access denied","welcome"), _raw="ignored"
| decide prompt="Was access denied?" field=msg output=p error_field=why model=gpt-6-luna-test | table n p why')"
stats="$(stub_stats)"
expect "custom fields" '[(r["n"], r.get("p"), r.get("why")) for r in rows] == [("1","0.95",None),("2","0.05",None)]' "$rows"
expect "field= input and model= sent" \
  'sorted(b["input"] for b in data["bodies"]) == ["access denied", "welcome"] and {b["model"] for b in data["bodies"]} == {"gpt-6-luna-test"}' "$rows" "$stats"

log "decide: a quoted positional prompt may contain = and follow the options"
stub_reset
rows="$(search '| makeresults | eval msg="status 500" | decide field=msg "Is status=500 an error?" | table decision_probability decision_error')"
stats="$(stub_stats)"
expect "positional prompt with = decided" 'len(rows) == 1 and rows[0].get("decision_error") is None and rows[0].get("decision_probability")' "$rows"
expect "positional prompt with = sent verbatim" \
  '[b["questions"][0]["instructions"] for b in data["bodies"]] == ["Is status=500 an error?"] and data["bodies"][0]["input"] == "status 500"' "$rows" "$stats"

log "decide: prompt mistakes fail the search with a clear message"
rows="$(search '| makeresults | decide')"
expect "missing prompt error" 'any("a prompt is required" in m["text"] and m["type"] in ("ERROR", "FATAL") for m in messages)' "$rows"
rows="$(search '| makeresults | decide "Is this a failed login?" prompt="Is this a failed login?"')"
expect "both forms error" 'any("not both" in m["text"] and m["type"] in ("ERROR", "FATAL") for m in messages)' "$rows"
rows="$(search '| makeresults | decide Is this a failed login?')"
expect "unquoted multi-word prompt error" 'any("one double-quoted string, but got 5 separate terms" in m["text"] and m["type"] in ("ERROR", "FATAL") for m in messages)' "$rows"

log "decide: an invalid API key stops calling the API but keeps the events"
settings openai -d api_key=sk-wrong-key >/dev/null
stub_reset
rows="$(search '| makeresults count=20 | streamstats count as n | eval _raw="event ".n | decide "q?" cache=false | table n decision_probability decision_error')"
stats="$(stub_stats)"
expect "20 events with the 401 error" 'len(rows) == 20 and all(r.get("decision_error") == "HTTP 401: Incorrect API key provided." and r.get("decision_probability") is None for r in rows)' "$rows"
expect "at most max_concurrency requests sent" 'data["requests"] <= 4' "$rows" "$stats"
settings openai -d api_key="$API_KEY" >/dev/null

log "decide: a user without list_storage_passwords gets a clear error"
rest POST /services/authentication/users -d name=e2euser -d password=devcontainer1 -d roles=user -o /dev/null || true
rows="$(search '| makeresults | decide "q?"' e2euser:devcontainer1)"
printf '%s\n' "$rows"
expect "permission error explains the capability" 'any("list_storage_passwords" in m["text"] for m in messages)' "$rows"

log "logs: useful lines, and the API key never appears"
dexec sh -c 'tail -n 8 /opt/splunk/var/log/splunk/ta_openai_decisions_decide.log'
if dexec sh -c "grep -rlF '$API_KEY' /opt/splunk/var/log/splunk /opt/splunk/var/run/splunk/dispatch 2>/dev/null | grep -v -e audit.log -e configuration_change.log"; then
  fail "API key found in logs"
else
  pass "API key absent from splunk logs and search artifacts"
fi

dexec pkill -f stub_decisions_api.py || true
log "result"
if [ "$FAILURES" -eq 0 ]; then echo "E2E_RESULT: PASS"; else echo "E2E_RESULT: FAIL ($FAILURES)"; exit 1; fi
