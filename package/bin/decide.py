#
# Copyright 2026 Brett Adams
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""decide: score each event against a yes/no prompt with the OpenAI Decisions API.

    ... | decide "<question>" [field=<field>] [output=<field>]
                 [error_field=<field>] [model=<model>] [cache=<bool>]

The question can also be given as ``prompt="<question>"``. Adds the predicate
probability to every event and never drops events; filtering is left to a
following ``where``.

This dispatch module is hand-written rather than generated from globalConfig
``customSearchCommand`` because the command must declare ``distributed=False``
so it runs once on the search head (where the API key and proxy settings live)
instead of fanning API calls out from every indexer.
"""

import logging
import re
import sys
from urllib.parse import quote, urlsplit

import import_declare_test  # noqa: F401  (puts the add-on's lib/ on sys.path)
from solnlib import conf_manager, log
from solnlib.utils import is_true
from splunklib import client as splunk_client
from splunklib.searchcommands import Configuration, Option, StreamingCommand, dispatch, validators

from decision_cache import COLLECTION, DEFAULT_MAX_ENTRIES, DEFAULT_TTL_DAYS, KVStoreCache
from openai_decisions import DEFAULT_MODEL, DecisionsClient, Evaluator

APP_NAME = "TA-openai-decisions"
SETTINGS_CONF = "ta_openai_decisions_settings"
SETTINGS_REALM = f"__REST_CREDENTIAL__#{APP_NAME}#configs/conf-{SETTINGS_CONF}"
LOG_NAMESPACE = "ta_openai_decisions"

# Set to "true" or "false" on every event while the cache is enabled for the search.
FROM_CACHE_FIELD = "decision_from_cache"

# An argument of this form, unless Splunk saw it quoted, is an option.
OPTION_ARG = re.compile(r"[A-Za-z_]\w*=")

PROMPT_EXAMPLE = '| decide "Is this a failed login?"'

# Distinct per-event errors surfaced as search warnings; the rest go to the log only.
MAX_UI_WARNINGS = 5
# Distinct per-event errors remembered so repeats are logged at DEBUG only.
MAX_LOGGED_ERRORS = 1000

PROXY_SCHEMES = {
    ("http", False): "http",
    ("http", True): "http",
    ("socks4", False): "socks4",
    ("socks4", True): "socks4a",
    ("socks5", False): "socks5",
    ("socks5", True): "socks5h",
}


class SettingsError(Exception):
    pass


def build_proxies(proxy):
    """Return a requests proxies mapping for the Proxy tab stanza, or None."""
    if not proxy or not is_true(proxy.get("proxy_enabled") or "0"):
        return None
    host = (proxy.get("proxy_url") or "").strip()
    port = (proxy.get("proxy_port") or "").strip()
    if not host or not port:
        raise SettingsError("the proxy is enabled but its host or port is not set")
    proxy_type = (proxy.get("proxy_type") or "http").strip().lower()
    scheme = PROXY_SCHEMES.get((proxy_type, is_true(proxy.get("proxy_rdns") or "0")))
    if scheme is None:
        raise SettingsError(f"unsupported proxy type {proxy_type!r}")
    auth = ""
    username = proxy.get("proxy_username")
    if username:
        auth = quote(username, safe="")
        password = proxy.get("proxy_password")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    url = f"{scheme}://{auth}{host}:{port}"
    return {"http": url, "https": url}


def load_settings(session_key):
    """Read the OpenAI, cache, proxy, and logging stanzas, decrypting stored secrets."""
    try:
        cfm = conf_manager.ConfManager(session_key, APP_NAME, realm=SETTINGS_REALM)
        conf = cfm.get_conf(SETTINGS_CONF)
        return {name: _get_stanza(conf, name) for name in ("openai", "cache", "proxy", "logging")}
    except SettingsError:
        raise
    except Exception as e:
        raise SettingsError(
            f"could not read the add-on settings ({type(e).__name__}: {e}). Reading the "
            "stored API key requires the list_storage_passwords capability."
        ) from e


def _get_stanza(conf, name):
    try:
        return conf.get(name)
    except conf_manager.ConfStanzaNotExistException:
        return {}


def _int_setting(settings, name, default):
    try:
        return int(settings.get(name) or default)
    except ValueError:
        raise SettingsError(f"setting {name} must be an integer") from None


@Configuration(distributed=False)
class DecideCommand(StreamingCommand):
    """
    ##Syntax
    decide (<string> | prompt=<string>) [field=<field>] [output=<field>]
    [error_field=<field>] [model=<string>] [cache=<bool>]

    ##Description
    Asks the OpenAI Decisions API a yes/no question about each event and adds the
    predicate probability (0 to 1) as a field. Events are never dropped. The
    question is the first argument, in double quotes, or the prompt option.
    """

    prompt = Option(
        doc="The yes/no question each event is evaluated against, when it is not "
        "given as the first argument.",
    )
    field = Option(
        doc="Field whose value is sent as the input. Default: _raw.",
        default="_raw",
        validate=validators.Fieldname(),
    )
    output = Option(
        doc="Field that receives the probability. Default: decision_probability.",
        default="decision_probability",
        validate=validators.Fieldname(),
    )
    error_field = Option(
        doc="Field that receives a short error when an event could not be decided. "
        "Default: decision_error.",
        default="decision_error",
        validate=validators.Fieldname(),
    )
    model = Option(
        doc="Decisions model. Default: " + DEFAULT_MODEL + ".",
        default=DEFAULT_MODEL,
    )
    cache = Option(
        doc="Use the KV store result cache. Default: the add-on's Cache setting.",
        default=None,
        validate=validators.Boolean(),
    )

    def _protocol_v2_option_parser(self, arg):
        # splunklib splits every argument at its first "=", so a quoted prompt
        # such as "Is status=500 an error?" would be read as an option. Splunk
        # strips the quotes from args but keeps them in raw_args.
        if OPTION_ARG.match(arg) and f'"{arg}"' not in self._raw_args():
            return arg.split("=", 1)
        return [arg]

    def _raw_args(self):
        return getattr(self.metadata.searchinfo, "raw_args", None) or ()

    def prepare(self):
        self.prompt = self._resolve_prompt()
        if not self.prompt.strip():
            self._fail("decide: the prompt is empty. " + _prompt_hint())
        if not (self.model or "").strip():
            self._fail('A non-empty value for "model" is required')

        try:
            settings = load_settings(self.metadata.searchinfo.session_key)
            openai = settings["openai"]
            self._log = _make_logger(settings["logging"].get("loglevel"))
            api_key = openai.get("api_key")
            if not api_key:
                raise SettingsError(
                    "no OpenAI API key is configured. Set one on the add-on's Configuration page."
                )
            proxies = build_proxies(settings["proxy"])
            proxy_password = settings["proxy"].get("proxy_password")
            client = DecisionsClient(
                api_key,
                base_url=openai.get("base_url") or "https://api.openai.com/v1",
                timeout=_int_setting(openai, "request_timeout", 30),
                proxies=proxies,
                secrets=(proxy_password,) if proxy_password else (),
            )
            concurrency = max(1, _int_setting(openai, "max_concurrency", 4))
            cache_settings = settings["cache"]
            ttl_days = _int_setting(cache_settings, "ttl_days", DEFAULT_TTL_DAYS)
            max_entries = _int_setting(cache_settings, "max_entries", DEFAULT_MAX_ENTRIES)
        except SettingsError as e:
            self._fail(f"decide: {e}")

        use_cache = self.cache
        if use_cache is None:
            use_cache = is_true(cache_settings.get("cache_enabled") or "1")
        self._use_cache = use_cache
        self._start_args = (client, concurrency, ttl_days, max_entries, bool(proxies))
        self._evaluator = None
        self._warned = set()

    def _resolve_prompt(self):
        """Return the prompt from the first argument or the prompt option."""
        positional = self.fieldnames or []
        if len(positional) > 1:
            self._fail(
                f"decide: the prompt must be one double-quoted string, but got {len(positional)} "
                f"separate terms. {_prompt_hint()}"
            )
        if positional and self.prompt is not None:
            self._fail(
                "decide: give the prompt either as the first argument or as prompt=, not both"
            )
        if positional:
            return positional[0]
        if self.prompt is None:
            self._fail("decide: a prompt is required. " + _prompt_hint())
        return self.prompt

    def _start(self):
        """Per-search setup, deferred to the first chunk: Splunk also runs
        prepare() in a short-lived parse-phase process that never streams."""
        client, concurrency, ttl_days, max_entries, proxied = self._start_args
        cache = self._make_cache(ttl_days, max_entries) if self._use_cache else None
        self._evaluator = Evaluator(
            client, self.prompt, self.model, concurrency=concurrency, cache=cache
        )
        self._log.info(
            "decide starting: sid=%s model=%s field=%s output=%s concurrency=%d proxy=%s cache=%s",
            self.metadata.searchinfo.sid,
            self.model,
            self.field,
            self.output,
            concurrency,
            "on" if proxied else "off",
            "on" if cache else "off",
        )

    def _make_cache(self, ttl_days, max_entries):
        info = self.metadata.searchinfo
        try:
            uri = urlsplit(info.splunkd_uri)
            service = splunk_client.Service(
                scheme=uri.scheme,
                host=uri.hostname,
                port=uri.port,
                token=info.session_key,
                owner="nobody",
                app=APP_NAME,
            )
            data = service.kvstore[COLLECTION].data
        except Exception as e:
            self._log.warning("decide cache unavailable, continuing without it: %s", e)
            return None
        cache = KVStoreCache(data, ttl_days=ttl_days, max_entries=max_entries, logger=self._log)
        cache.purge()
        return cache

    def stream(self, records):
        if self._evaluator is None:
            self._start()
        total = failed = 0
        for record, probability, error, from_cache in self._evaluator.evaluate(records, self._text):
            total += 1
            # Set the fields on every record so the chunk header always carries them.
            record[self.output] = probability
            record[self.error_field] = error
            if self._use_cache:
                record[FROM_CACHE_FIELD] = "true" if from_cache else "false"
            if error is not None:
                failed += 1
                self._report(error)
            yield record
        self._log.info(
            "decide chunk done: events=%d errors=%d api_calls=%d cache_hits=%d cached=%d",
            total,
            failed,
            self._evaluator.stats.get("api_calls", 0),
            self._evaluator.stats.get("cache_hits", 0),
            self._evaluator.stats.get("cached", 0),
        )

    def _text(self, record):
        value = record.get(self.field)
        if isinstance(value, (list, tuple)):
            value = "\n".join(str(v) for v in value if v is not None)
        return value if value else None

    def _report(self, error):
        # Each distinct error is logged once at WARNING and surfaced as a search
        # warning (up to MAX_UI_WARNINGS); repeats are DEBUG and counted per chunk.
        if error in self._warned:
            self._log.debug("decide event failed: %s", error)
            return
        self._log.warning("decide event failed: %s", error)
        if len(self._warned) < MAX_LOGGED_ERRORS:
            self._warned.add(error)
        if len(self._warned) <= MAX_UI_WARNINGS:
            self.write_warning("decide: some events could not be decided: {}", error)

    def _fail(self, message):
        logger = getattr(self, "_log", None)
        if logger is not None:
            logger.error(message)
        self.write_error(message)
        sys.exit(1)


def _prompt_hint():
    return f"Put the yes/no question in double quotes, for example: {PROMPT_EXAMPLE}"


def _make_logger(level_name):
    log.Logs.set_context(namespace=LOG_NAMESPACE)
    logger = log.Logs().get_logger("decide")
    logger.setLevel(getattr(logging, (level_name or "INFO").upper(), logging.INFO))
    return logger


if __name__ == "__main__":
    dispatch(DecideCommand, sys.argv, sys.stdin, sys.stdout, __name__)
