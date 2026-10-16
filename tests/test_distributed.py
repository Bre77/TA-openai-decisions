import logging
import os
import sys
import types

import pytest
from solnlib.credentials import CredentialManager

import choose
import conftest
import decide
import decisions_command
import grade
from decisions_command import (
    SETTINGS_REALM,
    BundleSettingsError,
    SettingsError,
    load_bundle_settings,
    load_peer_settings,
    read_conf,
    splunk_decrypt,
)

SEP = CredentialManager.SEP
END_MARK = CredentialManager.END_MARK
PEER_SID = "remote_sh1_1792133260.76"

QUESTION_ARGS = {
    decide.DecideCommand: ['"q?"'],
    choose.ChooseCommand: ['"Which?"', "a", "A", "b", "B"],
    grade.GradeCommand: ['"How bad?"', "low", "Fine", "high", "Bad"],
}


def getinfo(monkeypatch, command_class, args, **kwargs):
    command, reply = conftest.getinfo(
        monkeypatch, command_class, QUESTION_ARGS[command_class] + args, **kwargs
    )
    messages = reply.get("inspector", {}).get("messages", [])
    return command, reply, [text for level, text in messages if level == "ERROR"]


# --- the option on the search head -------------------------------------------


@pytest.mark.parametrize("command_class", list(QUESTION_ARGS))
def test_not_distributed_by_default(monkeypatch, command_class):
    command, reply, errors = getinfo(monkeypatch, command_class, [])
    assert errors == []
    assert command.distributed is False
    assert command.configuration.distributed is False
    # splunklib reports a command that is not distributed as stateful.
    assert reply["type"] == "stateful"
    assert command._call_limit == 1000


@pytest.mark.parametrize("command_class", list(QUESTION_ARGS))
def test_distributed_true_distributes_without_cache_or_limit(monkeypatch, command_class):
    command, reply, errors = getinfo(monkeypatch, command_class, ["distributed=t"])
    assert errors == []
    assert command.configuration.distributed is True
    assert reply["type"] == "streaming"
    assert "distributed" not in reply
    # The Cache setting is on by default, but each indexer would have its own cache.
    assert command._use_cache is False
    assert command._call_limit == 0


def test_distributed_false_and_cache_false_are_accepted(monkeypatch):
    _, reply, errors = getinfo(
        monkeypatch, decide.DecideCommand, ["distributed=false", "cache=false"]
    )
    assert errors == []
    assert reply["type"] == "stateful"
    _, reply, errors = getinfo(
        monkeypatch, decide.DecideCommand, ["distributed=true", "cache=false"]
    )
    assert errors == []
    assert reply["type"] == "streaming"


@pytest.mark.parametrize("command_class", list(QUESTION_ARGS))
@pytest.mark.parametrize(
    "arg, error",
    [
        (
            "cache=true",
            "{}: cache=true can't be used with distributed=true, because each indexer would "
            "have its own cache. Remove cache=true.",
        ),
        (
            "limit=5",
            "{}: limit can't be used with distributed=true, because each indexer would count "
            "only its own API calls. Remove limit.",
        ),
        # Even the default value, given explicitly, is rejected.
        ("limit=1000", "{}: limit can't be used with distributed=true"),
        ("limit=0", "{}: limit can't be used with distributed=true"),
    ],
)
def test_distributed_rejects_cache_and_limit(monkeypatch, command_class, arg, error):
    command, _, errors = getinfo(monkeypatch, command_class, ["distributed=t", arg])
    assert len(errors) == 1 and errors[0].startswith(error.format(command.name)), errors
    assert len(f"Error in '{command.name}' command: {errors[0]}") < 280


def test_distributed_rejects_an_invalid_value(monkeypatch):
    _, _, errors = getinfo(monkeypatch, decide.DecideCommand, ["distributed=maybe"])
    assert errors == ["Illegal value: distributed=maybe"]


def test_search_head_still_reads_the_settings_as_the_user(monkeypatch):
    # The capability check on the search head gates a distributed search
    # before it reaches any peer.
    def load_settings(session_key):
        raise SettingsError("could not read the add-on settings (AuthorizationFailed)")

    def load_peer_settings(session_key):
        raise AssertionError("the search head must not read the bundle")

    monkeypatch.setattr(decisions_command, "load_peer_settings", load_peer_settings)
    _, _, errors = getinfo(
        monkeypatch, decide.DecideCommand, ["distributed=t"], load_settings=load_settings
    )
    assert errors == ["decide: could not read the add-on settings (AuthorizationFailed)"]


# --- the command on a search peer ----------------------------------------------


@pytest.mark.parametrize("command_class", list(QUESTION_ARGS))
def test_peer_reads_the_peer_settings_and_reports_distributed(monkeypatch, command_class):
    calls = []

    def load_peer_settings(session_key):
        calls.append(session_key)
        return conftest.SETTINGS, "the knowledge bundle"

    def load_settings(session_key):
        raise AssertionError("a peer reads its settings through load_peer_settings")

    monkeypatch.setattr(decisions_command, "load_peer_settings", load_peer_settings)
    command, reply, errors = getinfo(
        monkeypatch, command_class, ["distributed=t"], sid=PEER_SID, load_settings=load_settings
    )
    assert errors == []
    assert calls == ["s"]
    assert command.configuration.distributed is True
    assert reply["type"] == "streaming"
    assert (command._use_cache, command._call_limit) == (False, 0)


def test_peer_is_distributed_even_without_the_option(monkeypatch):
    monkeypatch.setattr(
        decisions_command, "load_peer_settings", lambda key: (conftest.SETTINGS, "x")
    )
    command, reply, errors = getinfo(monkeypatch, decide.DecideCommand, [], sid=PEER_SID)
    assert errors == []
    assert reply["type"] == "streaming"
    assert command._use_cache is False


def test_peer_settings_failure_fails_the_command(monkeypatch):
    def load_peer_settings(session_key):
        raise SettingsError("could not decrypt the API key from the search head")

    monkeypatch.setattr(decisions_command, "load_peer_settings", load_peer_settings)
    _, _, errors = getinfo(monkeypatch, decide.DecideCommand, ["distributed=t"], sid=PEER_SID)
    assert errors == ["decide: could not decrypt the API key from the search head"]


# --- reading the settings from the knowledge bundle -------------------------------


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def credential(user, index=None):
    name = user if index is None else f"{user}{SEP}{index}"
    return f"[credential:{SETTINGS_REALM}:{name}:]"


DEFAULT_SETTINGS = """\
[openai]
api_key =
base_url = https://api.openai.com/v1
request_timeout = 30
max_concurrency = 4

[cache]
cache_enabled = True

[proxy]
proxy_enabled =
proxy_password =

[logging]
loglevel = INFO
"""


@pytest.fixture
def app(tmp_path):
    """An app directory laid out like the add-on's knowledge bundle copy, with
    its secrets "encrypted" as enc(<text>) for the fake decrypt below."""
    write(str(tmp_path / "default" / "ta_openai_decisions_settings.conf"), DEFAULT_SETTINGS)
    write(
        str(tmp_path / "local" / "ta_openai_decisions_settings.conf"),
        "[openai]\napi_key = ******\nmax_concurrency = 8\n\n"
        "[proxy]\nproxy_enabled = 1\nproxy_url = proxy\nproxy_port = 3128\n"
        "proxy_password = ******\n\n[logging]\nloglevel = DEBUG\n",
    )
    write(
        str(tmp_path / "local" / "passwords.conf"),
        "\n".join(
            [
                credential("openai", 1),
                'password = enc({"api_key": "sk-bundle-)',
                credential("openai", 2),
                'password = enc(key"})',
                credential("openai", 3),
                f"password = enc({END_MARK})",
                # A part left over from an earlier, longer value, after the end mark.
                credential("openai", 4),
                "password = enc(stale)",
                credential("proxy", 1),
                'password = enc({"proxy_password": "p@ss"})',
                credential("proxy", 2),
                f"password = enc({END_MARK})",
                # Another app's credential, and a stanza name that isn't a credential.
                "[credential:other_realm:openai``splunk_cred_sep``1:]",
                "password = enc(other)",
                "[not a credential]",
                "password = enc(other)",
                "",
            ]
        ),
    )
    return tmp_path


def fake_decrypt(calls=None):
    def decrypt(value):
        if calls is not None:
            calls.append(value)
        if value.startswith("enc(") and value.endswith(")"):
            return value[4:-1]
        return None

    return decrypt


def test_bundle_settings_merge_default_and_local_and_decrypt_secrets(app):
    calls = []
    settings = load_bundle_settings(str(app), fake_decrypt(calls))
    assert settings["openai"] == {
        "api_key": "sk-bundle-key",
        "base_url": "https://api.openai.com/v1",
        "request_timeout": "30",
        "max_concurrency": "8",
    }
    assert settings["cache"] == {"cache_enabled": "True"}
    assert settings["proxy"] == {
        "proxy_enabled": "1",
        "proxy_url": "proxy",
        "proxy_port": "3128",
        "proxy_password": "p@ss",
    }
    assert settings["logging"] == {"loglevel": "DEBUG"}
    # Decryption stops at the end mark, and other credentials are left alone.
    assert "enc(stale)" not in calls and "enc(other)" not in calls
    assert len(calls) == 5


def test_bundle_settings_read_a_credential_stored_whole(app):
    write(
        str(app / "local" / "passwords.conf"),
        f'{credential("openai")}\npassword = enc({{"api_key": "sk-whole"}})\n',
    )
    write(str(app / "local" / "ta_openai_decisions_settings.conf"), "[openai]\napi_key = ******\n")
    settings = load_bundle_settings(str(app), fake_decrypt())
    assert settings["openai"]["api_key"] == "sk-whole"


def test_bundle_settings_unescape_colons_in_credential_names(app, monkeypatch):
    monkeypatch.setattr(decisions_command, "SETTINGS_REALM", "realm:with:colons")
    write(
        str(app / "local" / "passwords.conf"),
        '[credential:realm\\:with\\:colons:openai:]\npassword = enc({"api_key": "sk-colons"})\n',
    )
    write(str(app / "local" / "ta_openai_decisions_settings.conf"), "[openai]\napi_key = ******\n")
    assert load_bundle_settings(str(app), fake_decrypt())["openai"]["api_key"] == "sk-colons"


@pytest.mark.parametrize(
    "remove, error",
    [
        # The search head's settings never reached the bundle, for example
        # before a restart applied the add-on's distsearch.conf.
        (("local/ta_openai_decisions_settings.conf", "local/passwords.conf"), "no API key"),
        (("local/passwords.conf",), "no API key"),
    ],
)
def test_bundle_without_the_settings(app, remove, error):
    for path in remove:
        os.remove(str(app / path))
    with pytest.raises(BundleSettingsError, match=rf"^the knowledge bundle has {error}$"):
        load_bundle_settings(str(app), fake_decrypt())


def test_bundle_without_the_proxy_password(app):
    write(
        str(app / "local" / "passwords.conf"),
        f'{credential("openai")}\npassword = enc({{"api_key": "sk-whole"}})\n',
    )
    with pytest.raises(BundleSettingsError, match=r"^the knowledge bundle has no proxy password$"):
        load_bundle_settings(str(app), fake_decrypt())


def test_bundle_without_any_settings(tmp_path):
    with pytest.raises(BundleSettingsError, match=r"^the knowledge bundle has no API key$"):
        load_bundle_settings(str(tmp_path), fake_decrypt())


@pytest.mark.parametrize(
    "decrypt",
    [
        # Another splunk.secret: Splunk's helper fails.
        lambda value: None,
        # A value that decrypts to something other than the stored JSON.
        lambda value: "\x8f\x01garbage",
        lambda value: '["not", "an", "object"]',
    ],
)
def test_bundle_secret_that_does_not_decrypt(app, decrypt):
    with pytest.raises(
        BundleSettingsError,
        match=r"^could not decrypt the API key from the search head$",
    ):
        load_bundle_settings(str(app), decrypt)


def test_bundle_secret_errors_never_include_values(app):
    with pytest.raises(BundleSettingsError) as e:
        load_bundle_settings(str(app), lambda value: "sk-plain-but-not-json")
    assert "sk-" not in str(e.value) and "enc(" not in str(e.value)


def test_read_conf(tmp_path):
    path = str(tmp_path / "a.conf")
    write(
        path,
        "top = level\n# comment\n[one]\n  key = value with = sign  \nmulti = first \\\n"
        "  second\n\n[two]\nempty =\n[one]\nlater = 1\n",
    )
    assert read_conf(path) == {
        "default": {"top": "level"},
        "one": {"key": "value with = sign", "multi": "first \nsecond", "later": "1"},
        "two": {"empty": ""},
    }
    assert read_conf(str(tmp_path / "missing.conf")) == {}


# --- the order of the peer's settings sources --------------------------------------


def rest_settings(api_key):
    def load_settings(session_key):
        assert session_key == "peer-key"
        return {"openai": {"api_key": api_key}, "cache": {}, "proxy": {}, "logging": {}}

    return load_settings


def failing_rest(session_key):
    raise SettingsError("could not read the add-on settings (HTTPError: Action forbidden.)")


def test_peer_prefers_the_bundle(app, monkeypatch):
    monkeypatch.setattr(decisions_command, "load_settings", rest_settings("sk-peer"))
    settings, source = load_peer_settings("peer-key", str(app), fake_decrypt())
    assert settings["openai"]["api_key"] == "sk-bundle-key"
    assert source == "the knowledge bundle"


def test_peer_falls_back_to_its_own_configuration(app, monkeypatch):
    # Enterprise without a shared splunk.secret, with the add-on configured on the indexer.
    monkeypatch.setattr(decisions_command, "load_settings", rest_settings("sk-peer"))
    settings, source = load_peer_settings("peer-key", str(app), lambda value: None)
    assert settings["openai"]["api_key"] == "sk-peer"
    assert source == (
        "this search peer's configuration, because could not decrypt the API key from the "
        "search head"
    )


REMEDY = (
    ", and the add-on isn't configured on this search peer. For distributed=true, indexers "
    "need the search head's splunk.secret, or the add-on installed and configured on each one."
)


@pytest.mark.parametrize("load_settings", [failing_rest, rest_settings("")])
def test_peer_without_any_settings_names_both_remedies(app, monkeypatch, load_settings):
    monkeypatch.setattr(decisions_command, "load_settings", load_settings)
    with pytest.raises(SettingsError) as e:
        load_peer_settings("peer-key", str(app), lambda value: None)
    assert str(e.value) == "could not decrypt the API key from the search head" + REMEDY
    assert len(f"Error in 'decide' command: decide: {e.value}") < 280

    with pytest.raises(SettingsError) as e:
        load_peer_settings("peer-key", str(app / "nowhere"), fake_decrypt())
    assert str(e.value) == "the knowledge bundle has no API key" + REMEDY


# --- Splunk's decrypt helper ------------------------------------------------------


@pytest.fixture
def cli_common(monkeypatch):
    """A stand-in for splunk.clilib.cli_common, which only Splunk's Python has."""
    module = types.ModuleType("splunk.clilib.cli_common")
    module.calls = []

    def decrypt(value, setEnv=False, ignoreErrors=False):
        module.calls.append((value, setEnv, logging.getLogger("splunk.clilib.cli_common").disabled))
        if setEnv:
            os.environ["ENCRYPTED_PASSWORD"] = value
        if value == "raise":
            raise OSError("splunk show-decrypted is missing")
        logging.getLogger("splunk.clilib.cli_common").error("Failed to decrypt value: %s", value)
        return None if value == "bad" else "clear:" + value

    module.decrypt = decrypt
    splunk = types.ModuleType("splunk")
    clilib = types.ModuleType("splunk.clilib")
    splunk.clilib, clilib.cli_common = clilib, module
    monkeypatch.setitem(sys.modules, "splunk", splunk)
    monkeypatch.setitem(sys.modules, "splunk.clilib", clilib)
    monkeypatch.setitem(sys.modules, "splunk.clilib.cli_common", module)
    monkeypatch.delenv("ENCRYPTED_PASSWORD", raising=False)
    return module


def test_splunk_decrypt_uses_splunks_helper_through_the_environment(cli_common, caplog):
    with caplog.at_level(logging.DEBUG):
        assert splunk_decrypt("$7$abc") == "clear:$7$abc"
        assert splunk_decrypt("bad") is None
        assert splunk_decrypt("raise") is None
    # The value goes through the environment, which is cleared after each call,
    # and the helper's logger, which would log the value, is silenced.
    assert cli_common.calls == [("$7$abc", True, True), ("bad", True, True), ("raise", True, True)]
    assert "ENCRYPTED_PASSWORD" not in os.environ
    assert not logging.getLogger("splunk.clilib.cli_common").disabled
    assert caplog.records == []


def test_splunk_decrypt_without_splunks_python(monkeypatch):
    monkeypatch.setitem(sys.modules, "splunk", None)
    assert splunk_decrypt("$7$abc") is None
