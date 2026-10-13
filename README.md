# TA-openai-decisions

Splunk search command that scores events with the [OpenAI Decisions API](https://developers.openai.com/api/docs/guides/decisions).

`decide` is a streaming search command. It asks a fast yes/no ("predicate") model a question about each event and adds the probability that the answer is yes. It never drops events, so you filter with your own `where`:

```spl
index=auth sourcetype=linux_secure
| decide "Is this a failed login?"
| where decision_probability > 0.8
```

The OpenAI Decisions API is in public beta. This add-on uses its `predicate` question type with the `gpt-6-luna` model.

## Install

1. Download the package (`TA-openai-decisions-<version>.tar.gz`) from a [GitHub Release](https://github.com/Bre77/TA-openai-decisions/releases), or build it yourself (see [Development](#development)).
2. Install it on your search heads: **Apps > Manage Apps > Install app from file**, or `splunk install app TA-openai-decisions-<version>.tar.gz`.
3. Restart Splunk.

The command runs only on the search head that runs the search, never on indexers, so you don't need to install the add-on on indexers. Supported on Splunk with Python 3.9 or 3.13.

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
| Logging | Log level | INFO | Applies to `$SPLUNK_HOME/var/log/splunk/ta_openai_decisions_decide.log`. |

### Permissions

Reading the stored API key requires the `list_storage_passwords` capability, which `admin` and `sc_admin` have by default. To let other users run `decide`, give their role that capability. This also limits who can spend API credits.

To write to the result cache, a role also needs write access to the add-on's `ta_openai_decisions_cache` KV store collection (by default `admin` and `sc_admin`). Users who can only read the collection still get cache hits, but their new results aren't cached, and their searches show `decide cache purge failed` and `decide cache write failed` warnings.

## Usage

```
decide "<prompt>" [field=<field>] [output=<field>] [error_field=<field>] [model=<string>] [cache=<bool>]
decide prompt="<prompt>" [field=<field>] [output=<field>] [error_field=<field>] [model=<string>] [cache=<bool>]
```

| Option | Default | Description |
| --- | --- | --- |
| `"<prompt>"` or `prompt` | required | The yes/no question each event is evaluated against. See [The prompt](#the-prompt). |
| `field` | `_raw` | The field whose value is sent to the API. Multivalue fields are joined with newlines. |
| `output` | `decision_probability` | The field that receives the probability of "yes", from 0 to 1. |
| `error_field` | `decision_error` | The field that receives a short reason when an event can't be decided. |
| `model` | `gpt-6-luna` | The Decisions model. |
| `cache` | Cache setting | `true` or `false` overrides the add-on's cache setting for this search. |

Examples:

```spl
index=auth | decide "Is this a failed login?" | where decision_probability > 0.8

sourcetype=tickets
| decide "Is the customer asking for a refund?" field=body output=refund_p
| where refund_p >= 0.5

index=web | decide "Does this request look like an injection attempt?" cache=false
| stats count by decision_error
```

### The prompt

Give the prompt as one double-quoted string, anywhere among the options, or as `prompt="<prompt>"`. Both forms behave the same. The search fails with a clear error if:

- there is no prompt, or it's empty.
- the prompt is given both ways, such as `decide "Is this a failed login?" prompt="Is this a failed login?"`.
- the prompt is more than one term, such as `decide Is this a failed login?`. Splunk splits an unquoted prompt into separate terms, drops its commas, and treats square brackets as a subsearch, so `decide` doesn't join the terms back together. Put the prompt in double quotes instead. A one-word prompt, such as `decide suspicious?`, works without quotes.

A quoted prompt can contain `=`, such as `decide "Is status=500 an error?"`.

### When an event can't be decided

A failure on one event never stops the search. If an event can't be decided, its `output` field is left empty, `error_field` gets a short reason, a search warning is shown for each distinct reason (up to 5), and the add-on log records it. Possible reasons:

| `decision_error` | Meaning |
| --- | --- |
| `refusal` | The model refused to answer for this input. |
| `empty input` | The input field is missing or empty, so no request was sent. |
| `HTTP <status>: <message>` | The API returned an error. 408, 409, 429, 500, 502, 503 and 504 responses are retried twice, honoring `Retry-After`. |
| `timeout after <n>s` | The request timed out, after retries. |
| `connection error: ...`, `proxy error: ...`, `TLS error: ...` | The request didn't reach the API, after retries. |
| `invalid response: ...` | The API answered with something the add-on doesn't recognize. |

After a 401, 403 or 404 (for example an invalid API key or a wrong base URL), the command stops calling the API for the rest of the search, and every remaining event gets the same error.

Events whose `output` is empty fail a numeric `where` comparison, so `| where decision_probability > 0.8` also drops events that couldn't be decided. To keep and inspect them, add `OR isnotnull(decision_error)`.

## Result cache

Results are cached in the KV store collection `ta_openai_decisions_cache`, which is on by default:

- The key is a SHA-256 hash of the model, the prompt, and the exact input text. The value is the probability and the time it was created. Errors and refusals are never cached.
- For each chunk of events, the command looks up all the chunk's inputs in one KV store request before it calls the API, and it writes new results back in batches of up to 1,000.
- Entries older than the time to live are ignored. Expired entries, and the oldest entries beyond the maximum size, are purged when a search that uses the cache starts.
- Within a single search, identical inputs are sent to the API only once, even when the cache is off.
- While the cache is on for a search, every event also gets a `decision_from_cache` field: `true` when its probability came from the cache, and `false` otherwise (the input wasn't cached, it couldn't be decided, or the KV store was unavailable). When the cache is off, through `cache=false` or the Cache setting, the field isn't added.

Exact-match cache hits are rare on raw `_raw`, because every event has its own timestamp. The cache pays off when `field=` points at a normalized field that repeats across events, such as a message template, a URL path or a user agent:

```spl
index=web
| eval request=method." ".uri_path
| decide "Is this an admin or debug endpoint?" field=request
```

## Cost and latency

- **Every event that isn't a cache hit is one API request.** The Decisions API bills input tokens only, at $0.10 per 1M input tokens at the time of writing; check OpenAI's current pricing. A live test request for one sshd log line used 191 input tokens, so short log lines cost about $0.02 per 1,000 events. Long events cost more. Put `decide` after your filtering commands, and use `head` or `sample` while you tune a prompt.
- **`decide` is much slower than built-in commands.** Requests run in parallel up to the concurrency setting (default 4). In a live test, 5 uncached events took about 3 seconds end to end, and a repeat run served entirely from the cache took about 0.5 seconds. Large searches take proportionally longer, and can hit your OpenAI rate limits. 429 responses are retried, then reported per event.
- Raising the maximum concurrent requests speeds up large searches, but spends your rate limit faster, and applies separately to every search running at the same time.
- Event text is sent to OpenAI. Make sure that's acceptable for the data you run `decide` on.

## Troubleshooting

- `decide: no OpenAI API key is configured`: set the key on the Configuration page.
- `decide: could not read the add-on settings ... list_storage_passwords`: your role can't read the stored API key. See [Permissions](#permissions).
- `decide cache lookup failed`, `decide cache write failed`, `decide cache purge failed` or `decide cache unavailable`: a KV store request for the result cache failed, and the message ends with the KV store's error. The search still completes, but without that part of the cache, so events that could have been cache hits are sent to the API.
- Per-event errors, cache statistics, and each search's settings are logged to `$SPLUNK_HOME/var/log/splunk/ta_openai_decisions_decide.log`, which is indexed in `_internal`:

  ```spl
  index=_internal source=*ta_openai_decisions_decide.log
  ```

## Development

This is a [UCC](https://splunk.github.io/addonfactory-ucc-generator/) add-on. `globalConfig.json` defines the Configuration page, and `package/` holds the search command and the static configuration:

- `package/bin/decide.py`: the search command, its settings and the cache wiring. It's written by hand rather than generated from `customSearchCommand` in `globalConfig.json`, because it must declare `distributed=False` so it runs once on the search head.
- `package/bin/openai_decisions.py`: the Decisions API client, and the ordered, bounded-concurrency evaluator.
- `package/bin/decision_cache.py`: the KV store result cache.

Build with Python 3.9 so the vendored libraries resolve for Splunk's oldest supported runtime:

```bash
uv venv --seed --python 3.9 .venv-ucc && source .venv-ucc/bin/activate
pip install splunk-add-on-ucc-framework==6.5.3
./build.sh 1.1.1      # writes output/TA-openai-decisions and dist/TA-openai-decisions-1.1.1.tar.gz
```

`ucc-gen build` rewrites `globalConfig.json`. Commit any change it makes.

Run the unit tests and the linter (CI runs the tests on Python 3.9 and 3.13):

```bash
pip install -r package/lib/requirements.txt pytest ruff
pytest
ruff check package/bin tests && ruff format --check package/bin tests
```

The unit tests use a local stand-in for the Decisions API, `tests/stub_decisions_api.py`, and an in-memory KV store. `tests/e2e/splunk_e2e.sh` installs a built package into a running `splunk/splunk` container (admin password `devcontainer`), points the add-on at the stub, and checks the command end to end, including the cache. The KV store REST API differs between Splunk versions, so run it on the oldest and the newest supported Splunk:

```bash
tests/e2e/splunk_e2e.sh <container> dist/TA-openai-decisions-1.1.1.tar.gz
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
