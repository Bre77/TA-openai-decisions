import operator
import os
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
