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
"""What the decide, choose and grade search commands share: the add-on's
settings, the API client and KV store cache wiring, argument parsing, and
streaming each chunk through the evaluator.

Each command module declares its own options and question and is written by
hand rather than generated from globalConfig ``customSearchCommand``, because
the commands must declare ``distributed=False`` so they run once on the search
head (where the API key and proxy settings live) instead of fanning API calls
out from every indexer.
"""

import logging
import re
import sys
from urllib.parse import quote, urlsplit

import import_declare_test  # noqa: F401  (puts the add-on's lib/ on sys.path)
from solnlib import conf_manager, log
from solnlib.utils import is_true
from splunklib import client as splunk_client
from splunklib.searchcommands import Option, StreamingCommand, validators

from decision_cache import (
    COLLECTION,
    DEFAULT_MAX_ENTRIES,
    DEFAULT_TTL_DAYS,
    KVStoreCache,
    describe_error,
)
from openai_decisions import DEFAULT_LIMIT, DEFAULT_MODEL, DecisionsClient, Evaluator

APP_NAME = "TA-openai-decisions"
SETTINGS_CONF = "ta_openai_decisions_settings"
SETTINGS_REALM = f"__REST_CREDENTIAL__#{APP_NAME}#configs/conf-{SETTINGS_CONF}"
LOG_NAMESPACE = "ta_openai_decisions"

# An argument of this form, unless Splunk saw it quoted, is an option.
OPTION_ARG = re.compile(r"[A-Za-z_]\w*=")
# Runs of characters that an option label contributes to a field name as "_".
UNSAFE_FIELD_CHARS = re.compile(r"[^A-Za-z0-9_]+")

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


def probability_fields(prefix, labels):
    """The per-option probability field for each label: ``<prefix>_probability_<label>``.

    Each run of characters other than ASCII letters, digits and underscores
    becomes one underscore, with leading and trailing underscores removed; a
    label with nothing left uses its 1-based position. A name already taken by
    an earlier label gets ``_<position>`` appended.
    """
    names = []
    for position, label in enumerate(labels, 1):
        safe = UNSAFE_FIELD_CHARS.sub("_", label).strip("_") or str(position)
        name = f"{prefix}_probability_{safe}"
        while name in names:
            name += f"_{position}"
        names.append(name)
    return names


class QuestionCommand(StreamingCommand):
    """Base for a streaming command that asks the Decisions API one question
    about each event. Events are never dropped.

    A subclass declares ``output`` and ``error_field`` options, sets
    ``FROM_CACHE_FIELD``, and implements ``_resolve_question`` and
    ``_write_answer``.
    """

    # Set to "true" or "false" on every event while the cache is enabled for the search.
    FROM_CACHE_FIELD = None

    field = Option(
        doc="Field whose value is sent as the input. Default: _raw.",
        default="_raw",
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
    limit = Option(
        doc="Maximum OpenAI API calls the search makes; inputs answered from the cache do "
        f"not count. 0 means no limit. Default: {DEFAULT_LIMIT}.",
        default=DEFAULT_LIMIT,
        validate=validators.Integer(),
    )

    def _resolve_question(self):
        """Return the question object for the command's arguments, or _fail()."""
        raise NotImplementedError

    def _write_answer(self, record, answer):
        """Set the output fields on ``record``; ``answer`` is None when the event
        could not be decided, and every output field must then be set to None."""
        raise NotImplementedError

    def _protocol_v2_option_parser(self, arg):
        # splunklib splits every argument at its first "=", so a quoted argument
        # such as "Is status=500 an error?" would be read as an option. Splunk
        # strips the quotes from args but keeps them in raw_args.
        if OPTION_ARG.match(arg) and not self._quoted(arg):
            return arg.split("=", 1)
        return [arg]

    def _raw_args(self):
        return getattr(self.metadata.searchinfo, "raw_args", None) or ()

    def _quoted(self, arg):
        """Whether Splunk saw ``arg`` as one double-quoted argument.

        Splunk removes the backslash from \\" and \\\\ in args, but raw_args keeps it.
        """
        escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
        raw_args = self._raw_args()
        return f'"{arg}"' in raw_args or f'"{escaped}"' in raw_args

    def _question_and_pairs(self, kind, example):
        """Split the positional arguments into the double-quoted question and
        the ``(label, description)`` pairs that follow it, and return them with
        a usage hint. Every description must be non-empty.

        Messages stay short because Splunk truncates a command's error banner.
        """
        hint = f"Example: {example}"
        positional = self.fieldnames or []
        if not positional:
            self._fail(
                f"{self.name}: a double-quoted question and its {kind}s are required. {hint}"
            )
        question, terms = positional[0], positional[1:]
        # An unquoted question would be split into terms that read as options.
        if self._raw_args() and not self._quoted(question):
            self._fail(
                f"{self.name}: put the question in double quotes. The first term, {question!r}, "
                f"is not quoted. {hint}"
            )
        if not question.strip():
            self._fail(f"{self.name}: the question is empty. {hint}")
        if len(terms) % 2:
            self._fail(
                f"{self.name}: each {kind} must be followed by its description, but got an odd "
                f"number of terms ({len(terms)}) after the question. {hint}"
            )
        pairs = list(zip(terms[0::2], terms[1::2]))
        for label, description in pairs:
            if label.strip() and not description.strip():
                self._fail(f"{self.name}: the description of {kind} {label!r} is empty. {hint}")
        return question, pairs, hint

    def prepare(self):
        self._question = self._resolve_question()
        if not (self.model or "").strip():
            self._fail('A non-empty value for "model" is required')
        if self.limit < 0:
            self._fail(
                f"{self.name}: limit must be a number of API calls, or 0 for no limit, "
                f"but got {self.limit}"
            )

        try:
            settings = load_settings(self.metadata.searchinfo.session_key)
            openai = settings["openai"]
            self._log = _make_logger(settings["logging"].get("loglevel"), self.name)
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
            self._fail(f"{self.name}: {e}")

        use_cache = self.cache
        if use_cache is None:
            use_cache = is_true(cache_settings.get("cache_enabled") or "1")
        self._use_cache = use_cache
        self._start_args = (client, concurrency, ttl_days, max_entries, bool(proxies))
        self._evaluator = None
        self._warned = set()
        self._limit_reached = False

    def _start(self):
        """Per-search setup, deferred to the first chunk: Splunk also runs
        prepare() in a short-lived parse-phase process that never streams."""
        client, concurrency, ttl_days, max_entries, proxied = self._start_args
        cache = self._make_cache(ttl_days, max_entries) if self._use_cache else None
        self._evaluator = Evaluator(
            client,
            self._question,
            self.model,
            concurrency=concurrency,
            cache=cache,
            limit=self.limit,
        )
        self._log.info(
            "%s starting: sid=%s model=%s field=%s output=%s concurrency=%d proxy=%s cache=%s "
            "limit=%d",
            self.name,
            self.metadata.searchinfo.sid,
            self.model,
            self.field,
            self.output,
            concurrency,
            "on" if proxied else "off",
            "on" if cache else "off",
            self.limit,
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
            message = f"{self.name} cache unavailable, continuing without it: {describe_error(e)}"
            self._log.warning(message)
            self._warn_cache(message)
            return None
        cache = KVStoreCache(
            data,
            ttl_days=ttl_days,
            max_entries=max_entries,
            logger=self._log,
            warn=self._warn_cache,
            command=self.name,
        )
        cache.purge()
        return cache

    def _warn_cache(self, message):
        # The cache only saves API calls, so its failures are search warnings, not errors.
        self.write_warning("{}", message)

    def stream(self, records):
        if self._evaluator is None:
            self._start()
        total = failed = limited = 0
        for record, answer, error, from_cache in self._evaluator.evaluate(records, self._text):
            total += 1
            # Set the fields on every record so the chunk header always carries them.
            self._write_answer(record, answer)
            record[self.error_field] = error
            if self._use_cache:
                record[self.FROM_CACHE_FIELD] = "true" if from_cache else "false"
            if error is not None and error == self._evaluator.limit_error:
                limited += 1
                self._report_limit()
            elif error is not None:
                failed += 1
                self._report(error)
            yield record
        self._log.info(
            "%s chunk done: events=%d errors=%d limited=%d api_calls=%d cache_hits=%d cached=%d",
            self.name,
            total,
            failed,
            limited,
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
            self._log.debug("%s event failed: %s", self.name, error)
            return
        self._log.warning("%s event failed: %s", self.name, error)
        if len(self._warned) < MAX_LOGGED_ERRORS:
            self._warned.add(error)
        if len(self._warned) <= MAX_UI_WARNINGS:
            self.write_warning(self.name + ": some events could not be decided: {}", error)

    def _report_limit(self):
        # One warning per search, however many events and chunks the limit affects.
        if self._limit_reached:
            return
        self._limit_reached = True
        message = (
            f"{self.name}: {self._evaluator.limit_error}, so events that needed another call "
            "were not decided. Raise the limit with limit=<number>, or use limit=0 for no limit."
        )
        self._log.warning(message)
        self.write_warning("{}", message)

    def _fail(self, message):
        logger = getattr(self, "_log", None)
        if logger is not None:
            logger.error(message)
        self.write_error(message)
        sys.exit(1)


def _make_logger(level_name, command):
    log.Logs.set_context(namespace=LOG_NAMESPACE)
    logger = log.Logs().get_logger(command)
    logger.setLevel(getattr(logging, (level_name or "INFO").upper(), logging.INFO))
    return logger
