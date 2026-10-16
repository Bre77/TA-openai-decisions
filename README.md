# TA-openai-decisions

Splunk search commands that classify events with the [OpenAI Decisions API](https://developers.openai.com/api/docs/guides/decisions).

Three streaming search commands ask a fast model a question about each event and add the answer as fields. They never drop events, so you filter with your own `where`:

- `decide` asks a yes/no question and adds the probability that the answer is yes.
- `choose` picks one of several options and adds the choice, and the probability of every option.
- `grade` rates each event against ordered levels and adds a score, and the probability of every level.

```spl
index=auth sourcetype=linux_secure
| decide "Is this a failed login?"
| where decision_probability > 0.8

sourcetype=tickets
| choose "Which team should handle this ticket?" network "Routers, firewalls and DNS" storage "Disks, backups and file shares" "service desk" "Laptops, accounts and everything else" field=body
| stats count by choice

index=auth
| grade "How severe is this event?" low "Routine activity, no action needed" medium "Worth a look during business hours" high "Needs immediate investigation"
| where grade > 1.5
```

The OpenAI Decisions API is in public beta. This add-on uses its `predicate`, `choice` and `score` question types with the `gpt-6-luna` model.

## Install

1. Download the package (`TA-openai-decisions-<version>.tar.gz`) from a [GitHub Release](https://github.com/Bre77/TA-openai-decisions/releases), or build it yourself (see [Development](#development)).
2. Install it on your search heads: **Apps > Manage Apps > Install app from file**, or `splunk install app TA-openai-decisions-<version>.tar.gz`.
3. Restart Splunk.

By default the commands run only on the search head that runs the search, so you don't need to install the add-on on indexers. To have the indexers run them too, see [Running on the indexers](#running-on-the-indexers). Supported on Splunk with Python 3.9 or 3.13.

## Set up the API key

Open **OpenAI Decisions Add-on > Configuration**:

| Tab | Setting | Default | Notes |
| --- | --- | --- | --- |
| OpenAI | API key | - | Required. Stored encrypted in Splunk's credential store and never logged. |
| OpenAI | Base URL | `https://api.openai.com/v1` | Change only for a compatible gateway or a test endpoint. |
| OpenAI | Request timeout | 30 seconds | Per request. A timed-out event gets an error, and the search continues. |
| OpenAI | Maximum concurrent requests | 4 | Upper limit on requests in flight for each running search (1-32). |
| Cache | Enable result cache | on | See [Result cache](#result-cache). |
| Cache | Time to live | 7 days | Cached results older than this are ignored and purged. |
| Cache | Maximum entries | 1,000,000 | The oldest entries beyond this count are purged. |
| Proxy | Enable, type, host, port, credentials | off | `http`, `socks4` and `socks5` proxies. When enabled, overrides `HTTP(S)_PROXY` from the environment. |
| Logging | Log level | INFO | Applies to `$SPLUNK_HOME/var/log/splunk/ta_openai_decisions_<command>.log`, one log for each command. |

### Permissions

Reading the stored API key requires the `list_storage_passwords` and `list_settings` capabilities, which `admin` and `sc_admin` have by default. To let other users run the commands, give their role both capabilities. This also limits who can spend API credits.

To write to the result cache, a role also needs write access to the add-on's `ta_openai_decisions_cache` KV store collection (by default `admin` and `sc_admin`). Users who can only read the collection still get cache hits, but their new results aren't cached, and their searches show `<command> cache purge failed` and `<command> cache write failed` warnings, such as `decide cache write failed`.

## Usage

All three commands take the same keyword options:

| Option | Default | Description |
| --- | --- | --- |
| `field` | `_raw` | The field whose value is sent to the API. Multivalue fields are joined with newlines. |
| `output` | see each command | The field that receives the answer. For `choose` and `grade` it's also the prefix of the other output fields. |
| `error_field` | see each command | The field that receives a short reason when an event can't be decided. |
| `model` | `gpt-6-luna` | The Decisions model. |
| `cache` | Cache setting | `true` or `false` overrides the add-on's cache setting for this search. |
| `limit` | `1000` | The most API requests the search sends. Cache hits don't count. `0` means no limit. See [Limiting API calls](#limiting-api-calls). |
| `distributed` | `false` | `true` has the indexers run the command on their own events and call the API themselves, without the cache or the limit. See [Running on the indexers](#running-on-the-indexers). |

### decide

```
decide "<prompt>" [field=<field>] [output=<field>] [error_field=<field>] [model=<string>] [cache=<bool>] [limit=<int>] [distributed=<bool>]
decide prompt="<prompt>" [field=<field>] [output=<field>] [error_field=<field>] [model=<string>] [cache=<bool>] [limit=<int>] [distributed=<bool>]
```

| Field | Default name | Value |
| --- | --- | --- |
| `output` | `decision_probability` | The probability of "yes", from 0 to 1. |
| `error_field` | `decision_error` | Why the event couldn't be decided. |
| | `decision_from_cache` | `true` or `false`, while the cache is on. See [Result cache](#result-cache). |

Examples:

```spl
index=auth | decide "Is this a failed login?" | where decision_probability > 0.8

sourcetype=tickets
| decide "Is the customer asking for a refund?" field=body output=refund_p
| where refund_p >= 0.5

index=web | decide "Does this request look like an injection attempt?" cache=false
| stats count by decision_error
```

#### The prompt

Give the prompt as one double-quoted string, anywhere among the options, or as `prompt="<prompt>"`. Both forms behave the same. The search fails with a clear error if:

- there is no prompt, or it's empty.
- the prompt is given both ways, such as `decide "Is this a failed login?" prompt="Is this a failed login?"`.
- the prompt is more than one term, such as `decide Is this a failed login?`. Splunk splits an unquoted prompt into separate terms, drops its commas, and treats square brackets as a subsearch, so `decide` doesn't join the terms back together. Put the prompt in double quotes instead. A one-word prompt, such as `decide suspicious?`, works without quotes.

A quoted prompt can contain `=`, such as `decide "Is status=500 an error?"`.

### choose

```
choose "<question>" <option> <description> <option> <description> [<option> <description> ...]
       [field=<field>] [output=<field>] [error_field=<field>] [model=<string>] [cache=<bool>]
       [limit=<int>] [distributed=<bool>]
```

The model picks the option that fits each event best. Give 2 to 255 options, each a distinct value followed by a description of when it applies. The description is sent to the model with the option and is required. Include a fallback option, such as `other "Anything else"`, when your options don't cover every event.

| Field | Default name | Value |
| --- | --- | --- |
| `output` | `choice` | The chosen option's value. |
| `<output>_probability` | `choice_probability` | The probability of the chosen option, from 0 to 1. |
| `<output>_confidence` | `choice_confidence` | The model's confidence in the answer, from 0 to 1. |
| `<output>_probability_<option>` | `choice_probability_<option>` | The probability of each option, one field per option. See [Field names for options](#field-names-for-options). |
| `error_field` | `choice_error` | Why the event couldn't be decided. |
| | `choice_from_cache` | `true` or `false`, while the cache is on. |

The option probabilities add up to about 1. The API rounds them to 2 decimal places.

Examples:

```spl
sourcetype=tickets
| choose "Which team should handle this ticket?" network "Routers, firewalls and DNS" storage "Disks, backups and file shares" "service desk" "Laptops, accounts and everything else" field=body
| where choice_probability >= 0.7
| stats count by choice

index=web
| choose "What kind of client sent this request?" browser "A person using a web browser" api "A program calling the API" scanner "A vulnerability scanner or crawler" output=client
| where client="scanner" AND client_probability_scanner > 0.9
```

### grade

```
grade "<question>" <level> <description> <level> <description> [<level> <description> ...]
      [field=<field>] [output=<field>] [error_field=<field>] [model=<string>] [cache=<bool>]
      [limit=<int>] [distributed=<bool>]
```

The model rates each event against levels that you give from lowest to highest, each a distinct label followed by a description of its criteria. Give at least 2 levels, and give adjacent levels clearly different criteria. The description is required.

| Field | Default name | Value |
| --- | --- | --- |
| `output` | `grade` | The score: the probability-weighted average of the level indices, where the first level is 0 and the last is the number of levels minus 1. It can fall between levels. |
| `<output>_confidence` | `grade_confidence` | The model's confidence in the answer, from 0 to 1. |
| `<output>_probability_<level>` | `grade_probability_<level>` | The probability of each level, one field per level. See [Field names for options](#field-names-for-options). |
| `error_field` | `grade_error` | Why the event couldn't be decided. |
| | `grade_from_cache` | `true` or `false`, while the cache is on. |

For example, with the levels `low`, `medium` and `high`, probabilities of 0.1, 0.7 and 0.2 give a score of 0.7 + 2 x 0.2 = 1.1. Use the score to sort or to set a threshold, and the level probabilities when you need the single most likely level.

Examples:

```spl
index=auth
| grade "How severe is this event?" low "Routine activity, no action needed" medium "Worth a look during business hours" high "Needs immediate investigation"
| where grade > 1.5
| sort - grade

sourcetype=tickets
| grade "How urgent is this ticket?" routine "Can wait a week" urgent "Needs a reply today" critical "Production is down" field=body output=urgency
| table _time subject urgency urgency_probability_critical
```

The command is called `grade` rather than `score` because the Splunk Machine Learning Toolkit (AI Toolkit) already has a `score` command.

### Questions, options and descriptions

For `choose` and `grade`, the question is the first argument and must be in double quotes. The options or levels follow it, among the keyword options in any order. The search fails with a clear error if:

- there is no question, or it's empty, or it isn't in double quotes. Splunk splits an unquoted question into separate terms, which would be read as options.
- there are fewer than 2 options or levels, or more than 255 options.
- an option or level is given twice, or an option, level or description is empty.
- there is an odd number of terms after the question, such as an option without a description.

Quote every option, level and description that contains a space, a comma, `=` or square brackets. Splunk splits an unquoted term at commas, so `a,b` is two terms. Only double quotes group words: `'single quoted'` is two terms. To put a double quote inside a quoted term, write `\"`, such as `"Did the user say \"yes\"?"`. Values are case-sensitive, so `Network` and `network` are different options.

#### Field names for options

The per-option and per-level fields are named `<output>_probability_<name>`, where `<name>` is the option's value or the level's label with each run of characters other than ASCII letters, digits and underscores replaced by one underscore, and leading and trailing underscores removed:

| Option or level | Field (default `output`) |
| --- | --- |
| `network` | `choice_probability_network` |
| `service desk` | `choice_probability_service_desk` |
| `R&D / Ops` | `choice_probability_R_D_Ops` |
| `high risk` | `grade_probability_high_risk` |

A name with no such characters left, such as `日本`, uses the option's position from 1 instead (`choice_probability_3` for the third option). If two options map to the same field, such as `a-b` and `a b`, the later one gets its position appended (`choice_probability_a_b_2`). In `eval` and `where`, a field name that starts with a digit or contains other characters needs single quotes, but these names don't.

### When an event can't be decided

A failure on one event never stops the search. If an event can't be decided, its output fields are left empty, `error_field` gets a short reason, a search warning is shown for each distinct reason (up to 5), and the add-on log records it. Possible reasons:

| Error | Meaning |
| --- | --- |
| `refusal` | The model refused to answer for this input. |
| `empty input` | The input field is missing or empty, so no request was sent. |
| `HTTP <status>: <message>` | The API returned an error. 408, 409, 429, 500, 502, 503 and 504 responses are retried twice, honoring `Retry-After`. |
| `timeout after <n>s` | The request timed out, after retries. |
| `connection error: ...`, `proxy error: ...`, `TLS error: ...` | The request didn't reach the API, after retries. |
| `invalid response: ...` | The API answered with something the add-on doesn't recognize. |
| `limit of <n> API calls reached` | The search already sent `limit` requests, so no request was sent for this input. See [Limiting API calls](#limiting-api-calls). |

After a 401, 403 or 404 (for example an invalid API key or a wrong base URL), the command stops calling the API for the rest of the search, and every remaining event gets the same error.

Events whose output is empty fail a numeric `where` comparison, so `| where decision_probability > 0.8` also drops events that couldn't be decided. To keep and inspect them, add `OR isnotnull(decision_error)`.

### Limiting API calls

To guard against unexpected API spend on a large search, each command sends at most `limit` API requests per search, 1,000 by default. Only inputs that need a request count: cache hits, and inputs already answered earlier in the same search, don't. Retries of a failed request don't count either. The limit applies across the whole search, and the requests go to the first inputs that need one, in event order.

Once the limit is reached, events are still kept. Events whose answer is in the cache still get it, and every other event gets empty output fields and `limit of <n> API calls reached` in `error_field`. The search shows one warning, such as `decide: limit of 1000 API calls reached, so events that needed another call were not decided. Raise the limit with limit=<number>, or use limit=0 for no limit.`

```spl
index=auth | decide "Is this a failed login?" limit=5000

index=auth | decide "Is this a failed login?" limit=0
```

`limit=0` removes the limit. A negative limit fails the search with an error. With `distributed=true` there is no limit, and `limit` fails the search. See [Running on the indexers](#running-on-the-indexers).

## Result cache

Results are cached in the KV store collection `ta_openai_decisions_cache`, which is on by default:

- For `decide`, the key is a SHA-256 hash of the model, the prompt, and the exact input text. For `choose` and `grade`, it's a SHA-256 hash of the question type, the model, the question, every option or level with its description in order, and the exact input text, so the commands never share entries, and changing, adding or reordering an option is a cache miss. Errors and refusals are never cached.
- Each entry stores the answer and the time it was created: the probability for `decide`; the choice or score, the confidence and every option's probability for `choose` and `grade`.
- For each chunk of events, the command looks up all the chunk's inputs in one KV store request before it calls the API, and it writes new results back in batches of up to 1,000.
- Entries older than the time to live are ignored. Expired entries, and the oldest entries beyond the maximum size, are purged when a search that uses the cache starts. All three commands share the collection, its time to live and its maximum size.
- Within a single search, identical inputs are sent to the API only once, even when the cache is off.
- The cache is off for searches with `distributed=true`.
- While the cache is on for a search, every event also gets a `decision_from_cache` (`decide`), `choice_from_cache` (`choose`) or `grade_from_cache` (`grade`) field: `true` when its answer came from the cache, and `false` otherwise (the input wasn't cached, it couldn't be decided, or the KV store was unavailable). When the cache is off, through `cache=false` or the Cache setting, the field isn't added.

Exact-match cache hits are rare on raw `_raw`, because every event has its own timestamp. The cache pays off when `field=` points at a normalized field that repeats across events, such as a message template, a URL path or a user agent:

```spl
index=web
| eval request=method." ".uri_path
| decide "Is this an admin or debug endpoint?" field=request
```

## Cost and latency

- **Every distinct input that isn't a cache hit is one API request**, up to the [limit](#limiting-api-calls) of 1,000 per search by default. The Decisions API bills input tokens only, at $0.10 per 1M input tokens at the time of writing; check OpenAI's current pricing. A live test request for one sshd log line used 191 input tokens with `decide`, 142 with a 4-option `choose` and 157 with a 3-level `grade`, so short log lines cost about $0.02 per 1,000 events. Long events, and long option and level descriptions, cost more. Put the command after your filtering commands, and use `head` or `sample` while you tune a question.
- **The commands are much slower than built-in commands.** Requests run in parallel up to the concurrency setting (default 4). In a live test, 5 uncached events took about 3 seconds end to end with `decide`, and a repeat run served entirely from the cache took about 0.5 seconds. Large searches take proportionally longer, and can hit your OpenAI rate limits. 429 responses are retried, then reported per event.
- Raising the maximum concurrent requests speeds up large searches, but spends your rate limit faster, and applies separately to every search running at the same time.
- With `distributed=true`, every indexer sends up to the maximum concurrent requests, so a search can have that many times the number of indexers in flight. OpenAI's rate limits apply to the API key and organization, not to each indexer, so expect more `429` retries and errors.
- Event text is sent to OpenAI. Make sure that's acceptable for the data you run the commands on.

## Running on the indexers

By default the commands run on the search head: the indexers send every event to it, and the search head makes all the API calls. With `distributed=true`, each indexer also runs the command on its own events and calls the API itself, which spreads the requests of a large search across the indexers:

```spl
index=web | decide "Does this request look like an injection attempt?" distributed=true
```

With `distributed=true`:

- The result cache is off, because each indexer would have its own cache. `cache=true` fails the search, and events get no `<command>_from_cache` field.
- There is no limit on API calls, because each indexer would count only its own calls. `limit` fails the search.
- Identical inputs are sent to the API once for each indexer, not once for the search.
- Each indexer sends up to the maximum concurrent requests. See [Cost and latency](#cost-and-latency).
- A search warning from an indexer starts with its name, such as `[idx1] decide: some events could not be decided: ...`, and each indexer logs to its own `ta_openai_decisions_<command>.log`.
- Commands that run on the search head before it, such as `head`, `sort` or `stats`, keep the command on the search head.

### What the indexers need

The add-on's `default/distsearch.conf` adds three things to the knowledge bundle that the search head sends to its indexers: the add-on's `lib/` folder (about 5 MB), its settings, and its own encrypted credentials (only this add-on's `local/passwords.conf`). The bundle carries them even when no search uses `distributed=true`. Splunk reads `distsearch.conf` at startup, so restart the search head after you install or upgrade the add-on, and allow a few minutes for the new bundle to reach the indexers.

Each indexer then needs the API key, and it tries two sources in order:

1. **The key configured on the search head**, from the knowledge bundle. An indexer can decrypt it only if it has the same `splunk.secret` as the search head.
2. **Its own configuration**, if the add-on is installed and configured on that indexer.

So it depends on the platform:

- **Splunk Cloud Platform:** the search heads and indexers of a stack share one `splunk.secret`, so installing the add-on and setting the key on the search head is enough. This was tested on a Victoria Experience stack.
- **Splunk Enterprise:** do one of these:
  - Give every search head and indexer the same `splunk.secret` before Splunk first starts on them, as many clustered deployments already do. See Splunk's [Deploy secure passwords across multiple servers](https://help.splunk.com/en/splunk-enterprise/administer/manage-users-and-security/9.4/install-splunk-enterprise-securely/deploy-secure-passwords-across-multiple-servers). Don't replace the `splunk.secret` of an instance that has already run: it can no longer decrypt its own passwords.
  - Install the add-on on every indexer and set the API key there, through the add-on's REST endpoint, because indexers usually have no Splunk Web: `curl -k -u admin https://<indexer>:8089/servicesNS/nobody/TA-openai-decisions/ta_openai_decisions_settings/openai -d api_key=<key>`. Set any proxy the same way, at `.../ta_openai_decisions_settings/proxy`. The endpoint encrypts the key; a key written into a `.conf` file by hand is not.

If neither works on an indexer, the search fails with `[<indexer>] <command>: could not decrypt the API key from the search head, and the add-on isn't configured on this search peer`, or `the knowledge bundle has no API key` when the bundle doesn't carry the settings yet.

Also:

- **Network:** the indexers make the HTTPS requests, so they need outbound access to the API's base URL, directly or through the proxy on the Proxy tab. The indexers use the proxy settings from the same source as the key.
- **Permissions:** roles reach the indexers in the knowledge bundle. The search head still reads the API key as the user, so a user without the [capabilities](#permissions) can't start a distributed search either.
- **Security:** with the key from the search head, the add-on's encrypted API key and proxy password are copied to every indexer with the knowledge bundle, and on Splunk Cloud Platform to the stack's bundle storage. They stay encrypted, but any process that runs as Splunk on an indexer that shares the `splunk.secret` can decrypt them, as on the search head.
- **Decryption:** an indexer decrypts the key with Splunk's own `splunk show-decrypted`, through an internal Splunk Python helper. If a Splunk release changes that, the search fails with the decrypt error above rather than running without a key.

## Troubleshooting

- `<command>: no OpenAI API key is configured`: set the key on the Configuration page.
- `<command>: could not read the add-on settings ... list_storage_passwords and list_settings`: your role can't read the stored API key. See [Permissions](#permissions).
- `<command>: cache=true can't be used with distributed=true` or `<command>: limit can't be used with distributed=true`: remove the option. See [Running on the indexers](#running-on-the-indexers).
- `[<indexer>] <command>: could not decrypt the API key from the search head ...` or `... the knowledge bundle has no API key ...`: the indexer can't get the API key. See [What the indexers need](#what-the-indexers-need).
- `[<indexer>] ... No module named 'splunklib'` or `Unknown search command` with `distributed=true`: the indexer still has a knowledge bundle without the add-on's `lib/` or the add-on itself. Restart the search head after installing or upgrading the add-on, and retry after a few minutes.
- `<command>: limit of <n> API calls reached`: the search needed more API requests than its `limit`, so some events weren't decided. Raise it with `limit=<number>`, or remove it with `limit=0`. See [Limiting API calls](#limiting-api-calls).
- `<command> cache lookup failed`, `<command> cache write failed`, `<command> cache purge failed` or `<command> cache unavailable`: a KV store request for the result cache failed, and the message ends with the KV store's error. The search still completes, but without that part of the cache, so events that could have been cache hits are sent to the API.
- Per-event errors, cache statistics, and each search's settings are logged to `$SPLUNK_HOME/var/log/splunk/ta_openai_decisions_decide.log`, `ta_openai_decisions_choose.log` and `ta_openai_decisions_grade.log`, which are indexed in `_internal`:

  ```spl
  index=_internal source=*ta_openai_decisions_*.log
  ```

## Development

This is a [UCC](https://splunk.github.io/addonfactory-ucc-generator/) add-on. `globalConfig.json` defines the Configuration page, and `package/` holds the search commands and the static configuration:

- `package/bin/decide.py`, `choose.py` and `grade.py`: the search commands, each with its own arguments, question and output fields. They're written by hand rather than generated from `customSearchCommand` in `globalConfig.json`, because they declare `distributed=False`, so they run once on the search head unless a search sets `distributed=true`.
- `package/bin/decisions_command.py`: what the commands share: the settings, argument parsing, the cache wiring, and streaming events through the evaluator.
- `package/bin/openai_decisions.py`: the Decisions API client, the `predicate`, `choice` and `score` question types, and the ordered, bounded-concurrency evaluator.
- `package/bin/decision_cache.py`: the KV store result cache.
- `package/default/distsearch.conf`: adds the add-on's `lib/`, settings and credentials to the knowledge bundle, for `distributed=true`.

Build with Python 3.9 so the vendored libraries resolve for Splunk's oldest supported runtime:

```bash
uv venv --seed --python 3.9 .venv-ucc && source .venv-ucc/bin/activate
pip install splunk-add-on-ucc-framework==6.5.3
./build.sh 1.2.0      # writes output/TA-openai-decisions and dist/TA-openai-decisions-1.2.0.tar.gz
```

`ucc-gen build` rewrites `globalConfig.json`. Commit any change it makes.

Run the unit tests and the linter (CI runs the tests on Python 3.9 and 3.13):

```bash
pip install -r package/lib/requirements.txt pytest ruff
pytest
ruff check package/bin tests && ruff format --check package/bin tests
```

The unit tests use a local stand-in for the Decisions API, `tests/stub_decisions_api.py`, and an in-memory KV store. `tests/e2e/splunk_e2e.sh` installs a built package into a running `splunk/splunk` container (admin password `devcontainer`), points the add-on at the stub, and checks the commands end to end, including the cache. The KV store REST API differs between Splunk versions, so run it on the oldest and the newest supported Splunk:

```bash
tests/e2e/splunk_e2e.sh <container> dist/TA-openai-decisions-1.2.0.tar.gz
```

`tests/e2e/splunk_distributed_e2e.sh` checks `distributed=true` across a search head container and an indexer container that is its search peer, both with admin password `devcontainer` on one docker network. With a pair that shares a `splunk.secret` (the same `SPLUNK_SECRET` on both before their first start) it checks that the indexer decrypts and uses the search head's key. With a pair that doesn't, it checks the could-not-decrypt error, then installs and configures the add-on on the indexer and checks that the indexer uses its own key:

```bash
tests/e2e/splunk_distributed_e2e.sh <search-head> <indexer> dist/TA-openai-decisions-1.2.0.tar.gz
```

## Releasing

Pushing a `v<version>` tag runs `.github/workflows/release.yml`. It checks that the tag matches the version in `globalConfig.json` and `package/app.manifest`, builds the package and runs AppInspect on it (precert, and the Splunk Cloud `cloud` and `aarch64_compatibility` tags), and runs the fleet's reusable build and AppInspect. Only if no check has an error or failure does it publish a GitHub Release with the package, its `.sha256` checksum and the AppInspect reports (the fleet's are prefixed `fleet-`). Warnings don't block a release. A version with a hyphen, such as `1.1.0-rc.1`, is published as a pre-release. The workflow never uploads to Splunkbase.

1. Bump `meta.version` in `globalConfig.json` and `info.id.version` in `package/app.manifest`, and merge that to `main`.
2. Tag the merge commit and push the tag:

   ```bash
   git checkout main && git pull
   git tag v1.1.0
   git push origin v1.1.0
   ```

3. When the Release workflow is green, download `TA-openai-decisions-<version>.tar.gz` from the new [release](https://github.com/Bre77/TA-openai-decisions/releases).
4. Upload it to Splunkbase by hand.

If the workflow fails, fix the cause on `main`, delete the tag (`git push origin :v1.1.0 && git tag -d v1.1.0`) and tag again.

## License

Apache License 2.0. See [LICENSE](LICENSE).
