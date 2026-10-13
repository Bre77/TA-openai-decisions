import logging

import pytest

import decide
from openai_decisions import Evaluator


def test_proxy_disabled():
    assert decide.build_proxies({}) is None
    assert decide.build_proxies({"proxy_enabled": "0", "proxy_url": "proxy"}) is None


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
    assert decide.build_proxies(proxy) == {"http": url, "https": url}


def test_proxy_credentials_are_url_encoded():
    proxy = {
        "proxy_enabled": "true",
        "proxy_url": "10.0.0.1",
        "proxy_port": "8080",
        "proxy_username": "dom\\user",
        "proxy_password": "p@ss:word/",
    }
    url = "http://dom%5Cuser:p%40ss%3Aword%2F@10.0.0.1:8080"
    assert decide.build_proxies(proxy) == {"http": url, "https": url}


def test_enabled_proxy_needs_host_and_port():
    with pytest.raises(decide.SettingsError):
        decide.build_proxies({"proxy_enabled": "1", "proxy_url": "proxy"})


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
    command._log = logging.getLogger("test")
    command._warned = set()
    command.warnings = []
    command.write_warning = lambda message, *args: command.warnings.append(message.format(*args))
    return command


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


def test_ui_warnings_are_capped():
    command = make_command()
    for i in range(10):
        command._report(f"error {i}")
    assert len(command.warnings) == decide.MAX_UI_WARNINGS


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
