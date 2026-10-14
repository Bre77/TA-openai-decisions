#!/usr/bin/env bash
# End-to-end check of the built add-on in a real Splunk container, against the
# local stub Decisions API (tests/stub_decisions_api.py) running inside it.
#
#   tests/e2e/splunk_e2e.sh <container> <package.tar.gz>
#
# The container must be a healthy docker.io/splunk/splunk with admin password
# "devcontainer" (e.g. SplunkUI-devcontainer's test-harness container). The
# KV store REST API differs between Splunk versions (10.4 and 10.6 accept
# different batch_find "fields" encodings), so run it on the oldest and the
# newest supported version. The script installs the package, restarts Splunk,
# and asserts the decide command's behaviour: the positional and prompt= forms
# of the prompt, probabilities, per-event errors that do not stop the search,
# the where-clause filter, field/output options, the KV store cache and
# decision_from_cache (including after a transforming command over indexed
# events, and for a role that can read but not write the cache), an invalid
# key short-circuit, and that the API key never reaches a log. It then checks
# the choose and grade commands: their output fields, per-event errors, a
# repeat run served from the cache without touching decide's entries, quoted
# options as Splunk delivers them, and their argument errors. Last, it checks
# the limit on API calls for all three commands with a warm cache: cached
# rows are answered, uncached rows past the limit get the limit error and one
# search warning, and the default limit of 1000.
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
expect "repeat run: no cache warnings" 'not any("decide cache" in m["text"] for m in messages)' "$rows"
kv="$(rest GET "/servicesNS/nobody/$APP/storage/collections/data/ta_openai_decisions_cache?output_mode=json")"
expect "KV store holds exactly the 2 successful results (expired entry purged)" \
  'len(data) == 2 and sorted(d["probability"] for d in data) == [0.05, 0.95] and all(len(d["_key"]) == 64 and d["created"] > 0 for d in data)' "" "$kv"

log "decide after a transforming command over indexed events: a repeat run is served from the cache"
# The shape of a real search that reported no cache hits on Splunk 10.4:
# index=... | rex | top 100 input | decide ... field=input
SRC="e2e_syscall_$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
for _ in 1 2 3; do
  for input in "process=/usr/bin/curl parent=bash user=www uid=33 cwd=/tmp/" \
    "process=/bin/sh parent=cron user=root uid=0 cwd=/root/" \
    "process=/usr/bin/sudo parent=bash user=bob uid=1000 cwd=/home/bob/ result=denied"; do
    rest POST "/services/receivers/simple?index=main&sourcetype=e2e_syscall&source=$SRC" \
      --data-binary "$(date -u +%Y-%m-%dT%H:%M:%SZ) e2ehost $input" -o /dev/null
  done
done
for _ in $(seq 30); do
  indexed="$(search "search index=main source=$SRC | stats count" | python3 -c 'import json,sys; print(json.loads(sys.stdin.readline())["count"])' 2>/dev/null || true)"
  [ "$indexed" = 9 ] && break
  sleep 2
done
[ "$indexed" = 9 ] && pass "9 syscall events indexed" || fail "9 syscall events indexed (got ${indexed:-none})"
SYSCALL='search index=main source='"$SRC"' | rex "\S+ \S+ (?<input>.*)" | top 100 input
| decide "Is this process execution dangerous?" field=input
| table input count decision_probability decision_error decision_from_cache'
stub_reset
rows="$(search "$SYSCALL")"
printf '%s\n' "$rows"
stats="$(stub_stats)"
expect "top: first run decides the 3 distinct inputs, none from the cache" \
  'len(rows) == 3 and all(r["count"] == "3" and r.get("decision_probability") and r.get("decision_from_cache") == "false" for r in rows)' "$rows"
expect "top: first run calls the API once per input" 'data["requests"] == 3' "$rows" "$stats"
expect "top: first run has no cache warnings" 'not any("decide cache" in m["text"] for m in messages)' "$rows"
first="$(printf '%s\n' "$rows" | python3 -c '
import json, sys
rows = [json.loads(l) for l in sys.stdin if l.strip()]
print(json.dumps({r["input"]: r.get("decision_probability") for r in rows if "input" in r}))')"
stub_reset
rows="$(search "$SYSCALL")"
printf '%s\n' "$rows"
stats="$(stub_stats)"
expect "top: repeat run has decision_from_cache true on every row" \
  'len(rows) == 3 and all(r.get("decision_from_cache") == "true" for r in rows)' "$rows"
expect "top: repeat run returns the same probabilities" \
  '{r["input"]: r.get("decision_probability") for r in rows} == data' "$rows" "$first"
expect "top: repeat run makes no API calls" 'data["requests"] == 0' "$rows" "$stats"
expect "top: repeat run has no cache warnings" 'not any("decide cache" in m["text"] for m in messages)' "$rows"

log "decide: a role that can read but not write the cache gets hits, and a warning instead of a silent write failure"
# solnlib's settings reader also needs list_settings (it reads server.conf [sslConfig]).
rest POST /services/authorization/roles -d name=e2e_decide -o /dev/null
rest POST /services/authorization/roles/e2e_decide -d imported_roles=user \
  -d capabilities=list_storage_passwords -d capabilities=list_settings -o /dev/null
rest POST /services/authentication/users -d name=e2edecide -d password=devcontainer1 \
  -d roles=e2e_decide -o /dev/null || true
stub_reset
rows="$(search "$SYSCALL" e2edecide:devcontainer1)"
stats="$(stub_stats)"
expect "read-only role: cache hits" 'len(rows) == 3 and all(r.get("decision_from_cache") == "true" for r in rows)' "$rows"
expect "read-only role: no API calls for cached inputs" 'data["requests"] == 0' "$rows" "$stats"
stub_reset
rows="$(search "| makeresults | eval _raw=\"read-only role $SRC\" | decide \"Is this a failed login?\" | table decision_probability decision_from_cache" e2edecide:devcontainer1)"
printf '%s\n' "$rows"
expect "read-only role: a new input is decided" \
  'len(rows) == 1 and rows[0].get("decision_probability") == "0.05" and rows[0].get("decision_from_cache") == "false"' "$rows"
expect "read-only role: the failed cache write and purge are search warnings" \
  'sorted(m["text"].split(":")[0] for m in messages if m["text"].startswith("decide cache")) == ["decide cache purge failed, old entries were not removed", "decide cache write failed, not caching further results"]' "$rows"
expect "read-only role: each warning quotes the KV store error once" \
  'all(m["text"].count("cannot write") == 1 for m in messages if m["text"].startswith("decide cache"))' "$rows"

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

log "choose: the chosen option, its probability, and every option's probability"
# The same prompt and inputs as decide's cached entries above, which must not be reused.
CHOOSE='| choose "Is this a failed login?" "login failed" "The sign-in attempt failed" "logged in" "The user signed in" other "Anything else"'
CHOOSE_FIELDS='choice choice_probability choice_confidence choice_probability_login_failed choice_probability_logged_in choice_probability_other choice_error choice_from_cache'
stub_reset
rows="$(search "$BASE $CHOOSE | table n $CHOOSE_FIELDS")"
printf '%s\n' "$rows"
stats="$(stub_stats)"
expect "choose: all 6 events returned in order" '[r["n"] for r in rows] == ["1","2","3","4","5","6"]' "$rows"
expect "choose: chosen options" '[r.get("choice") for r in rows] == ["login failed","logged in",None,None,"login failed",None]' "$rows"
expect "choose: probability and confidence of the choice" \
  '[(r.get("choice_probability"), r.get("choice_confidence")) for r in rows] == [("0.8","0.7"),("0.8","0.7"),(None,None),(None,None),("0.8","0.7"),(None,None)]' "$rows"
expect "choose: every option's probability" \
  '[(r.get("choice_probability_login_failed"), r.get("choice_probability_logged_in"), r.get("choice_probability_other")) for r in rows] == [("0.8","0.1","0.1"),("0.1","0.8","0.1"),(None,None,None),(None,None,None),("0.8","0.1","0.1"),(None,None,None)]' "$rows"
expect "choose: errors" '[r.get("choice_error") for r in rows] == [None, None, "refusal", "HTTP 500: The server had an error while processing your request.", None, "invalid response: body is not JSON"]' "$rows"
expect "choose: decide's cache entries for the same prompt and inputs are not used" \
  'data["by_input"] == {"user bob login failed": 1, "user alice logged in": 1, "REFUSE this": 1, "HTTP500 boom": 3, "BADJSON": 1}' "$rows" "$stats"
expect "choose: request body shape" \
  'all(b["model"] == "gpt-6-luna" and b["questions"] == [{"type": "choice", "name": "decision", "instructions": "Is this a failed login?", "choices": [{"value": "login failed", "description": "The sign-in attempt failed"}, {"value": "logged in", "description": "The user signed in"}, {"value": "other", "description": "Anything else"}]}] for b in data["bodies"])' "$rows" "$stats"
expect "choose: search warnings surfaced" 'sum("choose: some events could not be decided" in m["text"] for m in messages) == 3' "$rows"
expect "choose: first run has choice_from_cache false on every event" '[r.get("choice_from_cache") for r in rows] == ["false"] * 6' "$rows"
first="$(printf '%s\n' "$rows" | python3 -c '
import json, sys
lines = [json.loads(l) for l in sys.stdin if l.strip()]
print(json.dumps([{k: v for k, v in l.items() if k != "choice_from_cache"} for l in lines if "_message" not in l]))')"

log "choose: a repeat run is served from the cache"
stub_reset
rows="$(search "$BASE $CHOOSE | table n $CHOOSE_FIELDS")"
stats="$(stub_stats)"
expect "choose: repeat run returns the same fields" \
  '[{k: v for k, v in r.items() if k != "choice_from_cache"} for r in rows] == data' "$rows" "$first"
expect "choose: repeat run has choice_from_cache true on cache hits, false on errors" \
  '[r.get("choice_from_cache") for r in rows] == ["true","true","false","false","true","false"]' "$rows"
expect "choose: only uncached failures hit the API" \
  'data["by_input"] == {"REFUSE this": 1, "HTTP500 boom": 3, "BADJSON": 1}' "$rows" "$stats"
expect "choose: repeat run has no cache warnings" 'not any("cache" in m["text"] for m in messages)' "$rows"
kv="$(rest GET "/servicesNS/nobody/$APP/storage/collections/data/ta_openai_decisions_cache?output_mode=json")"
expect "choose: KV store holds the 2 choice answers beside decide's entries" \
  'sorted((d["choice"], json.loads(d["probabilities"]), d["confidence"]) for d in data if "choice" in d) == [("logged in", [0.1, 0.8, 0.1], 0.7), ("login failed", [0.8, 0.1, 0.1], 0.7)] and all(len(d["_key"]) == 64 for d in data)' "" "$kv"

log "choose: output= names the fields, and | where filters on them"
rows="$(search "$BASE $CHOOSE output=kind | where kind=\"login failed\" AND kind_probability > 0.5 | table n kind kind_probability_other")"
expect "choose output=: where keeps the two failed logins" \
  '[(r["n"], r.get("kind"), r.get("kind_probability_other")) for r in rows] == [("1","login failed","0.1"), ("5","login failed","0.1")]' "$rows"

log "grade: the score, its confidence, and every level's probability"
GRADE='| grade "Is this a failed login?" "logged in" "A routine sign-in" "login failed" "A failed sign-in, worth a look"'
GRADE_FIELDS='grade grade_confidence grade_probability_logged_in grade_probability_login_failed grade_error grade_from_cache'
stub_reset
rows="$(search "$BASE $GRADE | table n $GRADE_FIELDS")"
printf '%s\n' "$rows"
stats="$(stub_stats)"
expect "grade: scores" '[r.get("grade") for r in rows] == ["0.8","0.2",None,None,"0.8",None]' "$rows"
expect "grade: confidence and every level's probability" \
  '[(r.get("grade_confidence"), r.get("grade_probability_logged_in"), r.get("grade_probability_login_failed")) for r in rows] == [("0.6","0.2","0.8"),("0.6","0.8","0.2"),(None,None,None),(None,None,None),("0.6","0.2","0.8"),(None,None,None)]' "$rows"
expect "grade: errors" '[r.get("grade_error") for r in rows] == [None, None, "refusal", "HTTP 500: The server had an error while processing your request.", None, "invalid response: body is not JSON"]' "$rows"
expect "grade: decide's and choose's cache entries are not used" \
  'data["by_input"] == {"user bob login failed": 1, "user alice logged in": 1, "REFUSE this": 1, "HTTP500 boom": 3, "BADJSON": 1}' "$rows" "$stats"
expect "grade: request body shape" \
  'all(b["questions"] == [{"type": "score", "name": "decision", "instructions": "Is this a failed login?", "levels": [{"label": "logged in", "description": "A routine sign-in"}, {"label": "login failed", "description": "A failed sign-in, worth a look"}]}] for b in data["bodies"])' "$rows" "$stats"
expect "grade: first run has grade_from_cache false on every event" '[r.get("grade_from_cache") for r in rows] == ["false"] * 6' "$rows"
stub_reset
rows="$(search "$BASE $GRADE | table n $GRADE_FIELDS")"
stats="$(stub_stats)"
expect "grade: repeat run returns the same scores from the cache" \
  '[(r.get("grade"), r.get("grade_probability_login_failed"), r.get("grade_from_cache")) for r in rows] == [("0.8","0.8","true"),("0.2","0.2","true"),(None,None,"false"),(None,None,"false"),("0.8","0.8","true"),(None,None,"false")]' "$rows"
expect "grade: repeat run calls the API only for uncached failures" \
  'data["by_input"] == {"REFUSE this": 1, "HTTP500 boom": 3, "BADJSON": 1}' "$rows" "$stats"
expect "grade: repeat run has no cache warnings" 'not any("cache" in m["text"] for m in messages)' "$rows"

log "limit: with a warm cache, cached rows are answered and uncached rows past the limit get the limit error"
# "user bob login failed" and "user alice logged in" are cached for all three
# commands by the runs above; the "limit new" inputs are not.
LIMITED='| makeresults count=8 | streamstats count as n
| eval _raw=case(n=1,"user bob login failed", n=2,"limit new 1", n=3,"user alice logged in", n=4,"limit new 2",
                 n=5,"limit new 3", n=6,"limit new 1", n=7,"user bob login failed", n=8,"limit new 4")'
LIMIT_WARNING='limit of 2 API calls reached, so events that needed another call were not decided. Raise the limit with limit=<number>, or use limit=0 for no limit.'
stub_reset
rows="$(search "$LIMITED | decide \"Is this a failed login?\" limit=2 | table n decision_probability decision_error decision_from_cache")"
printf '%s\n' "$rows"
stats="$(stub_stats)"
expect "limit=2: all 8 events returned in order" '[r["n"] for r in rows] == [str(n) for n in range(1, 9)]' "$rows"
expect "limit=2: cached rows answered, a repeat answered from memory, rows past the limit empty" \
  '[r.get("decision_probability") for r in rows] == ["0.95","0.05","0.05","0.05",None,"0.05","0.95",None]' "$rows"
expect "limit=2: rows past the limit get the limit error" \
  '[r.get("decision_error") for r in rows] == [None,None,None,None,"limit of 2 API calls reached",None,None,"limit of 2 API calls reached"]' "$rows"
expect "limit=2: decision_from_cache true on the cached rows only" \
  '[r.get("decision_from_cache") for r in rows] == ["true","false","true","false","false","false","true","false"]' "$rows"
expect "limit=2: only the first 2 uncached inputs call the API" \
  'data["by_input"] == {"limit new 1": 1, "limit new 2": 1}' "$rows" "$stats"
WANT="decide: $LIMIT_WARNING" expect "limit=2: one limit warning, and no per-event error warning" \
  '[m["text"] for m in messages if "limit of" in m["text"] or "could not be decided" in m["text"]] == [os.environ["WANT"]]' "$rows"

stub_reset
rows="$(search "$LIMITED | decide \"Is this a failed login?\" limit=0 | table n decision_probability decision_error decision_from_cache")"
stats="$(stub_stats)"
expect "limit=0: every row answered" \
  '[r.get("decision_probability") for r in rows] == ["0.95","0.05","0.05","0.05","0.05","0.05","0.95","0.05"] and not any(r.get("decision_error") for r in rows)' "$rows"
expect "limit=0: the inputs answered under the limit were cached, the rest call the API" \
  'data["by_input"] == {"limit new 3": 1, "limit new 4": 1}' "$rows" "$stats"
expect "limit=0: no limit or per-event warnings" 'not any("limit of" in m["text"] or "could not be decided" in m["text"] for m in messages)' "$rows"

for command in choose grade; do
  if [ "$command" = choose ]; then question="$CHOOSE" field=choice; else question="$GRADE" field=grade; fi
  stub_reset
  rows="$(search "$LIMITED $question limit=2 | table n $field ${field}_error ${field}_from_cache")"
  stats="$(stub_stats)"
  F="$field" expect "$command limit=2: cached rows answered, rows past the limit get the limit error" \
    '[(r.get(os.environ["F"]) is not None, r.get(os.environ["F"] + "_error"), r.get(os.environ["F"] + "_from_cache")) for r in rows] == [(True,None,"true"),(True,None,"false"),(True,None,"true"),(True,None,"false"),(False,"limit of 2 API calls reached","false"),(True,None,"false"),(True,None,"true"),(False,"limit of 2 API calls reached","false")]' "$rows"
  expect "$command limit=2: only the first 2 uncached inputs call the API" \
    'data["by_input"] == {"limit new 1": 1, "limit new 2": 1}' "$rows" "$stats"
  WANT="$command: $LIMIT_WARNING" expect "$command limit=2: one limit warning" \
    '[m["text"] for m in messages if "limit of" in m["text"] or "could not be decided" in m["text"]] == [os.environ["WANT"]]' "$rows"
done

log "limit: the default is 1000 API calls"
stub_reset
rows="$(search '| makeresults count=1003 | streamstats count as n | eval _raw="default limit ".n | decide "q?" cache=false | stats count(decision_probability) as decided count(eval(decision_error="limit of 1000 API calls reached")) as limited count as events')"
# The full stats, with 1000 request bodies, are too long for an environment variable.
stats="$(stub_stats | python3 -c '
import json, sys
stats = json.load(sys.stdin)
print(json.dumps({"requests": stats["requests"], "events": sorted(int(i.split()[-1]) for i in stats["by_input"])}))')"
printf '%s\n' "$rows"
expect "default limit: 1000 decided and 3 limited, none dropped" \
  'rows == [{"decided": "1000", "limited": "3", "events": "1003"}]' "$rows"
expect "default limit: exactly 1000 requests, for the first 1000 events" \
  'data == {"requests": 1000, "events": list(range(1, 1001))}' "$rows" "$stats"
expect "default limit: the warning names the limit" \
  'any(m["text"].startswith("decide: limit of 1000 API calls reached") for m in messages)' "$rows"

log "choose: quoted arguments arrive as written"
stub_reset
rows="$(search '| makeresults | eval msg="x" | choose field=msg "Is status=500 \"bad\"?" "a, b" "Has, a comma" "c=d" "Has an =" "say \"e\"" "A \"quoted\" description" f,g cache=false | table choice choice_probability_a_b choice_probability_c_d choice_probability_say_e choice_probability_f choice_error')"
stats="$(stub_stats)"
printf '%s\n' "$rows"
expect "choose: quoted values and descriptions with commas, = and escaped quotes; an unquoted comma separates terms" \
  '[(b["questions"][0]["instructions"], [(c["value"], c["description"]) for c in b["questions"][0]["choices"]]) for b in data["bodies"]] == [("Is status=500 \"bad\"?", [("a, b", "Has, a comma"), ("c=d", "Has an ="), ("say \"e\"", "A \"quoted\" description"), ("f", "g")])]' "$rows" "$stats"
expect "choose: field names for options with punctuation" \
  'len(rows) == 1 and rows[0].get("choice_error") is None and [rows[0].get(f) for f in ("choice_probability_a_b", "choice_probability_c_d", "choice_probability_say_e", "choice_probability_f")] == ["0.8", "0.0667", "0.0667", "0.0667"]' "$rows"

log "argument mistakes fail the search with a clear message"
for case in \
  '| makeresults | choose|choose: a double-quoted question and its options are required.' \
  '| makeresults | choose "Which team?" network Routers|choose: at least 2 options are required, but got 1.' \
  '| makeresults | choose "Which team?" network storage identity|choose: each option must be followed by its description, but got an odd number of terms (3)' \
  '| makeresults | choose "Which team?" network Routers storage Disks network Again|choose: duplicate option '"'"'network'"'"'.' \
  '| makeresults | choose Which team? network Routers storage Disks|choose: put the question in double quotes.' \
  '| makeresults | grade "How bad?" low "Fine" high|grade: each level must be followed by its description, but got an odd number of terms (3)' \
  '| makeresults | grade "How bad?" low "Fine"|grade: at least 2 levels are required, but got 1.' \
  '| makeresults | grade "How bad?" low "Fine" low "Bad"|grade: duplicate level '"'"'low'"'"'.' \
  '| makeresults | decide "q?" limit=-1|decide: limit must be a number of API calls, or 0 for no limit, but got -1' \
  '| makeresults | choose "Which?" a A b B limit=-5|choose: limit must be a number of API calls, or 0 for no limit, but got -5' \
  '| makeresults | grade "How bad?" low "Fine" high "Bad" limit=-1|grade: limit must be a number of API calls, or 0 for no limit, but got -1'; do
  spl="${case%%|choose:*}"; spl="${spl%%|grade:*}"; spl="${spl%%|decide:*}"; want="${case#"$spl|"}"
  rows="$(search "$spl")"
  WANT="$want" expect "error: $want" 'any(os.environ["WANT"] in m["text"] and m["type"] in ("ERROR", "FATAL") for m in messages) and not rows' "$rows"
done

log "logs: useful lines, and the API key never appears"
dexec sh -c 'tail -n 4 /opt/splunk/var/log/splunk/ta_openai_decisions_decide.log /opt/splunk/var/log/splunk/ta_openai_decisions_choose.log /opt/splunk/var/log/splunk/ta_openai_decisions_grade.log'
if dexec sh -c "grep -rlF '$API_KEY' /opt/splunk/var/log/splunk /opt/splunk/var/run/splunk/dispatch 2>/dev/null | grep -v -e audit.log -e configuration_change.log"; then
  fail "API key found in logs"
else
  pass "API key absent from splunk logs and search artifacts"
fi

dexec pkill -f stub_decisions_api.py || true
log "result"
if [ "$FAILURES" -eq 0 ]; then echo "E2E_RESULT: PASS"; else echo "E2E_RESULT: FAIL ($FAILURES)"; exit 1; fi
