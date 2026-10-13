import logging
import time
import types

import pytest

import conftest
import decide
import decisions_command
from openai_decisions import Evaluator, Predicate


def test_proxy_disabled():
    assert decisions_command.build_proxies({}) is None
    assert decisions_command.build_proxies({"proxy_enabled": "0", "proxy_url": "proxy"}) is None


@pytest.mark.parametrize(
    "proxy_type, rdns, scheme",
    [
        ("http", "0", "http"),
        ("socks4", "0", "socks4"),
        ("socks4", "1", "socks4a"),
        ("socks5", "0", "socks5"),
        ("socks5", "1", "socks5h"),
        (None, None, "http"),
    ],
)
def test_proxy_schemes(proxy_type, rdns, scheme):
    proxy = {"proxy_enabled": "1", "proxy_url": "proxy.example.com", "proxy_port": "3128"}
    if proxy_type:
        proxy["proxy_type"] = proxy_type
    if rdns:
        proxy["proxy_rdns"] = rdns
    url = f"{scheme}://proxy.example.com:3128"
    assert decisions_command.build_proxies(proxy) == {"http": url, "https": url}


def test_proxy_credentials_are_url_encoded():
    proxy = {
        "proxy_enabled": "true",
        "proxy_url": "10.0.0.1",
        "proxy_port": "8080",
        "proxy_username": "dom\\user",
        "proxy_password": "p@ss:word/",
    }
    url = "http://dom%5Cuser:p%40ss%3Aword%2F@10.0.0.1:8080"
    assert decisions_command.build_proxies(proxy) == {"http": url, "https": url}


def test_enabled_proxy_needs_host_and_port():
    with pytest.raises(decisions_command.SettingsError):
        decisions_command.build_proxies({"proxy_enabled": "1", "proxy_url": "proxy"})


class FakeClient:
    def __init__(self):
        self.inputs = []

    def decide(self, prompt, text, model):
        self.inputs.append(text)
        return 0.9 if "fail" in text else 0.1


def new_command():
    command = decide.DecideCommand()
    command.options.reset()  # applies option defaults, as splunklib does on dispatch
    return command


def make_command(**options):
    command = new_command()
    for name, value in options.items():
        setattr(command, name, value)
    command._evaluator = Evaluator(FakeClient(), "q")
    command._use_cache = False
    command._log = logging.getLogger("test")
    command._warned = set()
    command.warnings = []
    command.write_warning = lambda message, *args: command.warnings.append(message.format(*args))
    return command


def run_getinfo(monkeypatch, raw_args):
    return conftest.run_getinfo(monkeypatch, decide.DecideCommand, raw_args)


@pytest.mark.parametrize(
    "raw_args, prompt",
    [
        (['"Is this a failed login?"'], "Is this a failed login?"),
        (['"Is this a failed login?"', "field=msg"], "Is this a failed login?"),
        (["field=msg", '"Is this a failed login?"'], "Is this a failed login?"),
        (['prompt="Is this a failed login?"', "field=msg"], "Is this a failed login?"),
        (["suspicious?", "field=msg"], "suspicious?"),
        # A quoted prompt containing "=" is still the prompt, not an option.
        (['"Is status=500 an error?"', "field=msg"], "Is status=500 an error?"),
        (['"field=msg"'], "field=msg"),
    ],
)
def test_prompt_is_positional_or_an_option(monkeypatch, raw_args, prompt):
    command, errors = run_getinfo(monkeypatch, raw_args)
    assert errors == []
    assert command.prompt == prompt
    assert command.field == ("msg" if "field=msg" in raw_args else "_raw")


@pytest.mark.parametrize(
    "raw_args, error",
    [
        ([], "decide: a prompt is required."),
        (["field=msg"], "decide: a prompt is required."),
        (
            ['"Is this a failed login?"', 'prompt="Is this a failed login?"'],
            "decide: give the prompt either as the first argument or as prompt=, not both",
        ),
        (
            ["Is", "this", "a", "failed", "login?"],
            "decide: the prompt must be one double-quoted string, but got 5 separate terms.",
        ),
        (['"one"', '"two"'], "decide: the prompt must be one double-quoted string"),
        (['""'], "decide: the prompt is empty."),
        (['prompt=" "'], "decide: the prompt is empty."),
    ],
)
def test_prompt_errors(monkeypatch, raw_args, error):
    _, errors = run_getinfo(monkeypatch, raw_args)
    assert len(errors) == 1 and errors[0].startswith(error)


def test_unknown_options_are_still_rejected(monkeypatch):
    _, errors = run_getinfo(monkeypatch, ['"q?"', "feild=msg"])
    assert errors == ["Unrecognized option: feild=msg"]


def test_command_defaults():
    command = new_command()
    assert command.field == "_raw"
    assert command.output == "decision_probability"
    assert command.error_field == "decision_error"
    assert command.model == "gpt-6-luna"
    assert command.cache is None


def test_stream_adds_both_fields_to_every_event_and_drops_no_events():
    command = make_command()
    records = [{"_raw": "login fail"}, {"_raw": ""}, {"_raw": "ok"}, {"host": "x"}]
    out = list(command.stream(records))
    assert out == [
        {"_raw": "login fail", "decision_probability": 0.9, "decision_error": None},
        {"_raw": "", "decision_probability": None, "decision_error": "empty input"},
        {"_raw": "ok", "decision_probability": 0.1, "decision_error": None},
        {"host": "x", "decision_probability": None, "decision_error": "empty input"},
    ]
    assert command.warnings == ["decide: some events could not be decided: empty input"]


def test_stream_custom_fields_and_multivalue_input():
    command = make_command(field="msg", output="p", error_field="why")
    out = list(command.stream([{"msg": ["first", "fail"]}]))
    assert out == [{"msg": ["first", "fail"], "p": 0.9, "why": None}]
    assert command._evaluator._client.inputs == ["first\nfail"]


def test_from_cache_field_only_when_the_cache_is_enabled(kv):
    from decision_cache import KVStoreCache, cache_key

    kv.docs[cache_key("gpt-6-luna", "q", "cached fail")] = {
        "_key": cache_key("gpt-6-luna", "q", "cached fail"),
        "probability": 0.75,
        "created": 1e12,
    }
    records = [{"_raw": "cached fail"}, {"_raw": "fresh fail"}, {"_raw": ""}]

    command = make_command()
    command._use_cache = True
    command._evaluator = Evaluator(FakeClient(), "q", cache=KVStoreCache(kv, clock=lambda: 1e12))
    assert [
        (r["decision_probability"], r["decision_from_cache"]) for r in command.stream(records)
    ] == [
        (0.75, "true"),
        (0.9, "false"),
        (None, "false"),
    ]

    command = make_command()
    out = list(command.stream([{"_raw": "fresh fail"}]))
    assert "decision_from_cache" not in out[0]


def start_with_cache(monkeypatch, service):
    """A command that builds its KV store cache in _start(), as on the first chunk."""
    monkeypatch.setattr(decisions_command.splunk_client, "Service", service)
    command = make_command()
    command._use_cache = True
    command._evaluator = None
    command._question = Predicate("q")
    command._start_args = (FakeClient(), 1, 7, 1000, False)
    command._metadata = types.SimpleNamespace(
        searchinfo=types.SimpleNamespace(
            splunkd_uri="https://127.0.0.1:8089", session_key="s", sid="1"
        )
    )
    return command


def test_cache_hits_through_the_kv_store_wiring(monkeypatch, kv):
    from decision_cache import COLLECTION, cache_key

    key = cache_key("gpt-6-luna", "q", "cached fail")
    kv.docs[key] = {"_key": key, "probability": 0.75, "created": time.time()}
    service = types.SimpleNamespace(kvstore={COLLECTION: types.SimpleNamespace(data=kv)})
    command = start_with_cache(monkeypatch, lambda **kwargs: service)
    out = list(command.stream([{"_raw": "cached fail"}, {"_raw": "fresh fail"}]))
    assert [(r["decision_probability"], r["decision_from_cache"]) for r in out] == [
        (0.75, "true"),
        (0.9, "false"),
    ]
    assert command.warnings == []


def test_cache_failures_are_search_warnings(monkeypatch, kv, caplog):
    from decision_cache import COLLECTION

    kv.fail = {"batch_find"}
    service = types.SimpleNamespace(kvstore={COLLECTION: types.SimpleNamespace(data=kv)})
    command = start_with_cache(monkeypatch, lambda **kwargs: service)
    with caplog.at_level(logging.WARNING, logger="test"):
        out = list(command.stream([{"_raw": "login fail"}]))
    assert out[0]["decision_probability"] == 0.9
    assert out[0]["decision_from_cache"] == "false"
    message = "decide cache lookup failed, continuing without it: kvstore batch_find unavailable"
    assert command.warnings == [message]
    assert message in [r.getMessage() for r in caplog.records]


def test_unavailable_cache_is_a_search_warning(monkeypatch, caplog):
    def service(**kwargs):
        raise OSError("connection refused")

    command = start_with_cache(monkeypatch, service)
    with caplog.at_level(logging.WARNING, logger="test"):
        out = list(command.stream([{"_raw": "login fail"}]))
    assert (out[0]["decision_probability"], out[0]["decision_from_cache"]) == (0.9, "false")
    message = "decide cache unavailable, continuing without it: connection refused"
    assert command.warnings == [message]
    assert message in [r.getMessage() for r in caplog.records]


def test_ui_warnings_are_capped():
    command = make_command()
    for i in range(10):
        command._report(f"error {i}")
    assert len(command.warnings) == decisions_command.MAX_UI_WARNINGS


def test_repeated_errors_are_logged_once_at_warning(caplog):
    command = make_command()
    with caplog.at_level(logging.DEBUG, logger="test"):
        for _ in range(3):
            command._report("HTTP 401: Incorrect API key provided.")
    levels = [r.levelname for r in caplog.records]
    assert levels == ["WARNING", "DEBUG", "DEBUG"]
    assert command.warnings == [
        "decide: some events could not be decided: HTTP 401: Incorrect API key provided."
    ]
