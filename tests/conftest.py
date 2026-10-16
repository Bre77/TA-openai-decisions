import io
import json
import logging
import operator
import os
import re
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "package", "bin"))
sys.path.insert(0, HERE)

# decide.py imports the UCC-generated import_declare_test to put lib/ on
# sys.path; the test environment installs those libraries directly.
sys.modules.setdefault("import_declare_test", types.ModuleType("import_declare_test"))

from stub_decisions_api import serve_in_thread  # noqa: E402

API_KEY = "sk-test-0123456789abcdef"


def splunk_args(raw_args):
    """The args Splunk derives from raw_args, as observed on Splunk 10.4 and
    10.6: quotes around a whole argument or an option value are removed, and
    the backslash of an escaped double quote or backslash is dropped."""
    return [
        re.sub(r'\\(["\\])', r"\1", re.sub(r'^(\w+=)?"(.*)"$', r"\1\2", arg, flags=re.S))
        for arg in raw_args
    ]


SETTINGS = {"openai": {"api_key": "k"}, "cache": {}, "proxy": {}, "logging": {}}


def run_getinfo(monkeypatch, command_class, raw_args, sid="1"):
    """Run the protocol v2 getinfo exchange the way splunkd starts the command,
    and return the command and the error messages it wrote."""
    command, reply = getinfo(monkeypatch, command_class, raw_args, sid)
    messages = reply.get("inspector", {}).get("messages", [])
    return command, [text for level, text in messages if level == "ERROR"]


def getinfo(monkeypatch, command_class, raw_args, sid="1", load_settings=lambda key: SETTINGS):
    """Run the getinfo exchange, and return the command and its reply metadata."""
    import decisions_command

    monkeypatch.setattr(decisions_command, "load_settings", load_settings)
    monkeypatch.setattr(
        decisions_command, "_make_logger", lambda level, command: logging.getLogger("test")
    )
    command = command_class()
    searchinfo = {
        "args": splunk_args(raw_args),
        "raw_args": raw_args,
        "session_key": "s",
        "sid": sid,
        "app": "search",
        "owner": "admin",
        "username": "admin",
        "search": "| " + command.name,
        "splunk_version": "10.0.0",
        "earliest_time": "0",
        "latest_time": "0",
        "dispatch_dir": "/tmp",
        "splunkd_uri": "https://127.0.0.1:8089",
    }
    metadata = json.dumps({"action": "getinfo", "preview": False, "searchinfo": searchinfo})
    ifile = io.BytesIO(f"chunked 1.0,{len(metadata)},0\n{metadata}".encode())
    ofile = io.BytesIO()
    try:
        command.process([command.name + ".py"], ifile, ofile)
    except SystemExit:  # the command exits after reporting an error
        pass
    header, _, body = ofile.getvalue().decode().partition("\n")
    return command, json.loads(body[: int(header.split(",")[1])])


@pytest.fixture
def stub():
    server = serve_in_thread(API_KEY)
    yield server
    server.shutdown()
    server.server_close()


class FakeKVData:
    """In-memory stand-in for splunklib's KVStoreCollectionData, supporting
    the query shapes decision_cache uses."""

    def __init__(self):
        self.docs = {}
        self.calls = []
        self.fail = None

    def _check(self, name):
        self.calls.append(name)
        if self.fail and name in self.fail:
            raise RuntimeError(f"kvstore {name} unavailable")

    def batch_find(self, *queries):
        self._check("batch_find")
        results = []
        for q in queries:
            assert set(q) <= {"query", "fields", "limit"}
            fields = q.get("fields")
            # Splunk 10.4 accepts only a projection object here; 10.6 also takes
            # a list, but neither takes a comma-separated string.
            if fields is not None and not isinstance(fields, dict):
                raise RuntimeError(
                    "HTTP 400 Bad Request -- The provided 'fields' argument is invalid."
                )
            matched = [
                {k: v for k, v in d.items() if fields is None or fields.get(k)}
                for d in self.docs.values()
                if _match(d, q["query"])
            ]
            results.append(matched[: q.get("limit") or None])
        return results

    def batch_save(self, *docs):
        self._check("batch_save")
        assert len(docs) <= 1000
        for doc in docs:
            self.docs[doc["_key"]] = dict(doc)
        return [d["_key"] for d in docs]

    def delete(self, query=None):
        import json

        self._check("delete")
        q = json.loads(query)
        for key in [k for k, d in self.docs.items() if _match(d, q)]:
            del self.docs[key]

    def query(self, sort=None, skip=0, limit=0, fields=None):
        self._check("query")
        field, direction = sort.split(":")
        docs = sorted(self.docs.values(), key=lambda d: d[field], reverse=direction == "-1")
        docs = docs[skip:]
        return [dict(d) for d in (docs[:limit] if limit else docs)]


COMPARISONS = {"$gte": operator.ge, "$lt": operator.lt, "$lte": operator.le}


def _match(doc, query):
    for field, cond in query.items():
        if field == "$and":
            if not all(_match(doc, q) for q in cond):
                return False
        elif field == "$or":
            if not any(_match(doc, q) for q in cond):
                return False
        elif isinstance(cond, dict):
            actual = doc.get(field)
            for op, value in cond.items():
                if not COMPARISONS[op](actual, value):
                    return False
        elif doc.get(field) != cond:
            return False
    return True


@pytest.fixture
def kv():
    return FakeKVData()
