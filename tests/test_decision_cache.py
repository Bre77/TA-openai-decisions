from conftest import API_KEY
from decision_cache import KVStoreCache, cache_key
from openai_decisions import DecisionsClient, Evaluator

DAY = 86400.0


class Clock:
    def __init__(self, now=1000 * DAY):
        self.now = now

    def __call__(self):
        return self.now


def make_evaluator(stub, cache, prompt="Is this a failure?", model="gpt-6-luna"):
    client = DecisionsClient(API_KEY, base_url=stub.base_url, max_retries=0, sleep=lambda s: None)
    return Evaluator(client, prompt, model, concurrency=4, cache=cache)


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
    assert evaluator.stats == {"events": 2, "api_calls": 2, "cache_hits": 0, "cached": 2}
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
    assert evaluator.stats == {"events": 3, "api_calls": 0, "cache_hits": 2, "cached": 0}


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


def test_kv_store_failures_degrade_to_the_api(stub, kv):
    kv.fail = {"batch_find", "batch_save"}
    cache = KVStoreCache(kv)
    evaluator = make_evaluator(stub, cache)
    assert run(evaluator, ["login failed"]) == [(0.95, None)]
    assert run(evaluator, ["all good"]) == [(0.05, None)]
    # Each failing operation is tried once, then disabled for the search.
    assert kv.calls == ["batch_find", "batch_save"]
    assert evaluator.stats["cached"] == 0


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


def test_purge_failure_is_not_raised(kv):
    kv.fail = {"delete"}
    KVStoreCache(kv).purge()
