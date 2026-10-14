import io
import logging
import types

import pytest
from splunklib import binding

from conftest import API_KEY
from decision_cache import KVStoreCache, cache_key, describe_error, question_cache_key
from openai_decisions import Choice, ChoiceAnswer, DecisionsClient, Evaluator, Score, ScoreAnswer

DAY = 86400.0


def described(*values):
    """Choice options with a description each."""
    return [(value, f"About {value}.") for value in values]


class Clock:
    def __init__(self, now=1000 * DAY):
        self.now = now

    def __call__(self):
        return self.now


def make_evaluator(stub, cache, prompt="Is this a failure?", model="gpt-6-luna", limit=None):
    """``prompt`` is a predicate prompt or a question object."""
    client = DecisionsClient(API_KEY, base_url=stub.base_url, max_retries=0, sleep=lambda s: None)
    return Evaluator(client, prompt, model, concurrency=4, cache=cache, limit=limit)


def run(evaluator, texts):
    return [(p, err) for _, p, err, _ in evaluator.evaluate(texts, lambda t: t)]


def test_cache_key_covers_model_prompt_and_exact_input():
    key = cache_key("gpt-6-luna", "q", "text")
    assert len(key) == 64 and int(key, 16) >= 0
    assert key == cache_key("gpt-6-luna", "q", "text")
    assert (
        len(
            {
                key,
                cache_key("other-model", "q", "text"),
                cache_key("gpt-6-luna", "q2", "text"),
                cache_key("gpt-6-luna", "q", "text "),
                # The values are delimited, so shifting text between them changes the key.
                cache_key("gpt-6-luna", "qt", "ext"),
            }
        )
        == 5
    )


def test_miss_calls_the_api_and_stores_the_result(stub, kv):
    clock = Clock()
    evaluator = make_evaluator(stub, KVStoreCache(kv, clock=clock))
    assert run(evaluator, ["login failed", "all good"]) == [(0.95, None), (0.05, None)]
    assert stub.state.requests == 2
    assert evaluator.stats == {
        "events": 2,
        "api_calls": 2,
        "cache_hits": 0,
        "cached": 2,
        "limited": 0,
    }
    key = cache_key("gpt-6-luna", "Is this a failure?", "login failed")
    assert kv.docs[key] == {"_key": key, "probability": 0.95, "created": clock.now}


def test_hit_skips_the_api(stub, kv):
    clock = Clock()
    run(make_evaluator(stub, KVStoreCache(kv, clock=clock)), ["login failed", "all good"])
    stub.state.reset()

    evaluator = make_evaluator(stub, KVStoreCache(kv, clock=clock))
    assert run(evaluator, ["login failed", "all good", "login failed"]) == [
        (0.95, None),
        (0.05, None),
        (0.95, None),
    ]
    assert stub.state.requests == 0
    assert evaluator.stats == {
        "events": 3,
        "api_calls": 0,
        "cache_hits": 2,
        "cached": 0,
        "limited": 0,
    }


def test_cache_hits_do_not_count_against_the_limit(stub, kv):
    run(make_evaluator(stub, KVStoreCache(kv)), ["cached fail", "cached ok"])
    stub.state.reset()

    evaluator = make_evaluator(stub, KVStoreCache(kv), limit=1)
    texts = ["cached fail", "new 1", "cached ok", "new 2", "cached fail", "new 1"]
    assert run(evaluator, texts) == [
        (0.95, None),
        (0.05, None),
        (0.05, None),
        (None, "limit of 1 API call reached"),
        (0.95, None),
        (0.05, None),
    ]
    assert stub.state.by_input == {"new 1": 1}
    # Cache hits after the limit are still answered, in later chunks too.
    assert run(evaluator, ["new 2", "cached ok"]) == [
        (None, "limit of 1 API call reached"),
        (0.05, None),
    ]
    assert stub.state.requests == 1
    # Only the answer from the API is new to the cache.
    assert len(kv.docs) == 3


def test_from_cache_is_true_only_for_cached_probabilities(stub, kv):
    run(make_evaluator(stub, KVStoreCache(kv)), ["login failed"])
    evaluator = make_evaluator(stub, KVStoreCache(kv))

    def decide(texts):
        return [(p, err, c) for _, p, err, c in evaluator.evaluate(texts, lambda t: t)]

    assert decide(["login failed", "all good", "REFUSE this", "", "login failed"]) == [
        (0.95, None, True),
        (0.05, None, False),
        (None, "refusal", False),
        (None, "empty input", False),
        (0.95, None, True),
    ]
    # Later chunks reuse the search's in-memory copy, which still came from the cache.
    assert decide(["login failed", "all good"]) == [(0.95, None, True), (0.05, None, False)]


def test_a_different_prompt_or_model_misses(stub, kv):
    run(make_evaluator(stub, KVStoreCache(kv)), ["login failed"])
    run(make_evaluator(stub, KVStoreCache(kv), prompt="Another question?"), ["login failed"])
    run(make_evaluator(stub, KVStoreCache(kv), model="other-model"), ["login failed"])
    assert stub.state.requests == 3
    assert len(kv.docs) == 3


def test_disabled_cache_never_touches_the_kv_store(stub, kv):
    evaluator = make_evaluator(stub, None)
    run(evaluator, ["login failed"])
    run(make_evaluator(stub, None), ["login failed"])
    assert stub.state.requests == 2
    assert kv.calls == []


def test_errors_and_refusals_are_never_cached(stub, kv):
    evaluator = make_evaluator(stub, KVStoreCache(kv))
    results = run(evaluator, ["REFUSE", "HTTP500", "BADJSON", "", "ok"])
    assert [err for _, err in results][:4] == [
        "refusal",
        "HTTP 500: The server had an error while processing your request.",
        "invalid response: body is not JSON",
        "empty input",
    ]
    assert [d["probability"] for d in kv.docs.values()] == [0.05]


def test_expired_entries_miss(stub, kv):
    clock = Clock()
    run(make_evaluator(stub, KVStoreCache(kv, ttl_days=7, clock=clock)), ["login failed"])
    clock.now += 7 * DAY + 1
    stub.state.reset()
    evaluator = make_evaluator(stub, KVStoreCache(kv, ttl_days=7, clock=clock))
    assert run(evaluator, ["login failed"]) == [(0.95, None)]
    assert stub.state.requests == 1
    key = cache_key("gpt-6-luna", "Is this a failure?", "login failed")
    assert kv.docs[key]["created"] == clock.now


def test_one_lookup_per_chunk_and_batched_writes(stub, kv):
    evaluator = make_evaluator(stub, KVStoreCache(kv))
    texts = [f"event {i}" for i in range(1201)]
    run(evaluator, texts)
    assert kv.calls == ["batch_find", "batch_save", "batch_save"]
    assert len(kv.docs) == 1201

    kv.calls = []
    stub.state.reset()
    evaluator = make_evaluator(stub, KVStoreCache(kv))
    assert all(err is None for _, err in run(evaluator, texts))
    assert kv.calls == ["batch_find"]
    assert stub.state.requests == 0


def test_inputs_already_decided_in_this_search_skip_the_lookup(stub, kv):
    evaluator = make_evaluator(stub, KVStoreCache(kv))
    run(evaluator, ["a"])
    kv.calls = []
    run(evaluator, ["a"])
    assert kv.calls == []


def test_lookup_query_is_accepted_by_splunk_10_4_and_10_6(kv):
    # A "fields" list in batch_find gets HTTP 400 on Splunk 10.4, which turned
    # every lookup into a miss while writes still succeeded.
    key = cache_key("gpt-6-luna", "q", "text")
    kv.docs[key] = {"_key": key, "probability": 0.75, "created": 1000 * DAY}
    warnings = []
    cache = KVStoreCache(kv, clock=Clock(), warn=warnings.append)
    assert cache.lookup([key, cache_key("gpt-6-luna", "q", "other")]) == {key: 0.75}
    assert warnings == []


def test_kv_store_failures_degrade_to_the_api_with_a_warning(stub, kv, caplog):
    kv.fail = {"batch_find", "batch_save"}
    warnings = []
    cache = KVStoreCache(kv, logger=logging.getLogger("test"), warn=warnings.append)
    evaluator = make_evaluator(stub, cache)
    with caplog.at_level(logging.WARNING, logger="test"):
        assert run(evaluator, ["login failed"]) == [(0.95, None)]
        assert run(evaluator, ["all good"]) == [(0.05, None)]
    # Each failing operation is tried once, then disabled for the search.
    assert kv.calls == ["batch_find", "batch_save"]
    assert evaluator.stats["cached"] == 0
    assert warnings == [
        "decide cache lookup failed, continuing without it: kvstore batch_find unavailable",
        "decide cache write failed, not caching further results: kvstore batch_save unavailable",
    ]
    assert [r.getMessage() for r in caplog.records] == warnings


def test_purge_removes_expired_and_excess_entries(kv):
    clock = Clock()
    cache = KVStoreCache(kv, ttl_days=7, max_entries=3, clock=clock)
    for age_days in (10, 8, 5, 4, 3, 2, 1):
        kv.docs[f"k{age_days}"] = {
            "_key": f"k{age_days}",
            "probability": 0.5,
            "created": clock.now - age_days * DAY,
        }
    cache.purge()
    assert sorted(kv.docs) == ["k1", "k2", "k3"]


def kv_store_http_error(status, reason, message):
    response = types.SimpleNamespace(
        status=status,
        reason=reason,
        headers=[],
        body=io.BytesIO(
            f'<response><messages><msg type="ERROR">{message}</msg></messages></response>'.encode()
        ),
    )
    return binding.HTTPError(response)


def test_describe_error_drops_the_repeated_kv_store_message():
    denied = "User 'bob' with roles { user } cannot write: /nobody/app/collections/c"
    assert describe_error(kv_store_http_error(403, denied, denied)) == f"HTTP 403 {denied}"
    invalid = kv_store_http_error(400, "Bad Request", "The provided 'fields' argument is invalid.")
    assert describe_error(invalid) == (
        "HTTP 400 Bad Request -- The provided 'fields' argument is invalid."
    )
    assert describe_error(OSError("connection refused")) == "connection refused"


def test_purge_failure_is_a_warning_not_an_exception(kv):
    kv.fail = {"delete"}
    warnings = []
    KVStoreCache(kv, warn=warnings.append).purge()
    assert warnings == [
        "decide cache purge failed, old entries were not removed: kvstore delete unavailable"
    ]


# --- choice and score answers -------------------------------------------------

TEAMS = Choice("Is this a failure?", described("network", "storage", "identity"))
SEVERITY = Score("Is this a failure?", [("low", "Fine."), ("high", "Failed.")])


def test_question_keys_cover_type_options_and_their_order():
    key = TEAMS.cache_key("gpt-6-luna", "text")
    assert key == question_cache_key(
        "choice",
        "gpt-6-luna",
        "Is this a failure?",
        [
            ["network", "About network."],
            ["storage", "About storage."],
            ["identity", "About identity."],
        ],
        "text",
    )
    keys = {
        key,
        # The same model, instructions and input as a predicate or a score question.
        cache_key("gpt-6-luna", "Is this a failure?", "text"),
        Score("Is this a failure?", [("network", ""), ("storage", ""), ("identity", "")]).cache_key(
            "gpt-6-luna", "text"
        ),
        Choice("Is this a failure?", described("storage", "network", "identity")).cache_key(
            "gpt-6-luna", "text"
        ),
        Choice("Is this a failure?", described("network", "storage")).cache_key(
            "gpt-6-luna", "text"
        ),
        Choice("Is this a failure?", described("network", "storage", "identity")).cache_key(
            "other-model", "text"
        ),
        Choice("Another?", described("network", "storage", "identity")).cache_key(
            "gpt-6-luna", "text"
        ),
        TEAMS.cache_key("gpt-6-luna", "text "),
        # A different description, or text shifted between options, must change the key.
        Choice(
            "Is this a failure?",
            [
                ("network", "Routers."),
                ("storage", "About storage."),
                ("identity", "About identity."),
            ],
        ).cache_key("gpt-6-luna", "text"),
        Choice("Is this a failure?", described("networks", "torage", "identity")).cache_key(
            "gpt-6-luna", "text"
        ),
    }
    assert len(keys) == 10


def test_score_keys_cover_level_descriptions_and_order():
    keys = {
        Score("q", [("low", "Fine."), ("high", "Bad.")]).cache_key("m", "t"),
        Score("q", [("low", "Fine"), ("high", "Bad.")]).cache_key("m", "t"),
        Score("q", [("high", "Bad."), ("low", "Fine.")]).cache_key("m", "t"),
        # Shifting text between a label and its description must change the key.
        Score("q", [("lowF", "ine."), ("high", "Bad.")]).cache_key("m", "t"),
    }
    assert len(keys) == 4


@pytest.mark.parametrize(
    "question, text, answer, doc",
    [
        (
            TEAMS,
            "storage failed",
            ChoiceAnswer("storage", 0.8, 0.7, (0.1, 0.8, 0.1)),
            {"choice": "storage", "confidence": 0.7, "probabilities": "[0.1, 0.8, 0.1]"},
        ),
        (
            SEVERITY,
            "high load",
            ScoreAnswer(0.8, 0.6, (0.2, 0.8)),
            {"score": 0.8, "confidence": 0.6, "probabilities": "[0.2, 0.8]"},
        ),
    ],
)
def test_choice_and_score_answers_round_trip_through_the_cache(
    stub, kv, question, text, answer, doc
):
    clock = Clock()
    assert run(make_evaluator(stub, KVStoreCache(kv, clock=clock), prompt=question), [text]) == [
        (answer, None)
    ]
    key = question.cache_key("gpt-6-luna", text)
    assert kv.docs == {key: {**doc, "_key": key, "created": clock.now}}

    stub.state.reset()
    evaluator = make_evaluator(stub, KVStoreCache(kv, clock=clock), prompt=question)
    results = list(evaluator.evaluate([text], lambda t: t))
    assert [(a, err, from_cache) for _, a, err, from_cache in results] == [(answer, None, True)]
    assert stub.state.requests == 0


def test_question_types_never_share_cache_entries(stub, kv):
    text = "storage failed"
    run(make_evaluator(stub, KVStoreCache(kv)), [text])
    run(make_evaluator(stub, KVStoreCache(kv), prompt=TEAMS), [text])
    run(make_evaluator(stub, KVStoreCache(kv), prompt=SEVERITY), [text])
    run(
        make_evaluator(
            stub, KVStoreCache(kv), prompt=Choice(TEAMS.instructions, described("a", "b"))
        ),
        [text],
    )
    assert stub.state.requests == 4
    assert len(kv.docs) == 4


@pytest.mark.parametrize(
    "doc",
    [
        {"choice": "legal", "confidence": 0.7, "probabilities": "[0.1, 0.8, 0.1]"},
        {"choice": "storage", "confidence": 0.7, "probabilities": "[0.1, 0.9]"},
        {"choice": "storage", "confidence": 0.7, "probabilities": "[0.1, 1.8, 0.1]"},
        {"choice": "storage", "confidence": 0.7, "probabilities": "not json"},
        {"choice": "storage", "confidence": 0.7, "probabilities": '["a", 0.8, 0.1]'},
        {"choice": "storage", "probabilities": "[0.1, 0.8, 0.1]"},
        # A predicate entry stored under the same key.
        {"probability": 0.5},
    ],
)
def test_unreadable_choice_entries_are_misses(stub, kv, doc):
    key = TEAMS.cache_key("gpt-6-luna", "storage failed")
    kv.docs[key] = {**doc, "_key": key, "created": Clock().now}
    cache = KVStoreCache(kv, clock=Clock())
    assert cache.lookup([key], TEAMS) == {}


def test_cache_warnings_name_the_command(kv):
    kv.fail = {"batch_find", "delete"}
    warnings = []
    cache = KVStoreCache(kv, warn=warnings.append, command="choose")
    cache.purge()
    assert cache.lookup(["k"], TEAMS) == {}
    assert [w.split(":")[0] for w in warnings] == [
        "choose cache purge failed, old entries were not removed",
        "choose cache lookup failed, continuing without it",
    ]
