import threading
from typing import ClassVar

import pytest
import requests

from conftest import API_KEY
from openai_decisions import (
    Choice,
    ChoiceAnswer,
    DecisionError,
    DecisionsClient,
    Evaluator,
    Predicate,
    Score,
    ScoreAnswer,
    build_request,
    parse_response,
)


def described(*values):
    """Choice options with a description each."""
    return [(value, f"About {value}.") for value in values]


def make_client(stub, **kwargs):
    kwargs.setdefault("sleep", lambda seconds: None)
    return DecisionsClient(kwargs.pop("api_key", API_KEY), base_url=stub.base_url, **kwargs)


# --- request construction -------------------------------------------------


def test_build_request_is_a_single_predicate_question():
    assert build_request("Is this a failed login?", "user bob failed", "gpt-6-luna") == {
        "model": "gpt-6-luna",
        "input": "user bob failed",
        "questions": [
            {"type": "predicate", "name": "decision", "instructions": "Is this a failed login?"}
        ],
    }


def test_client_posts_to_decisions_with_bearer_auth(stub):
    client = make_client(stub)
    assert client.url == stub.base_url + "/decisions"
    assert client.decide("Is this a failure?", "login failed", "gpt-6-luna") == 0.95
    (body,) = stub.state.bodies
    assert body == build_request("Is this a failure?", "login failed", "gpt-6-luna")


def test_base_url_trailing_slash_is_ignored():
    client = DecisionsClient("k", base_url="https://api.openai.com/v1/")
    assert client.url == "https://api.openai.com/v1/decisions"


def test_api_key_is_required():
    with pytest.raises(ValueError):
        DecisionsClient("")


def test_configured_proxies_override_environment():
    proxies = {"http": "http://proxy:3128", "https": "http://proxy:3128"}
    session = DecisionsClient("k", proxies=proxies)._session()
    assert session.proxies == proxies
    assert session.trust_env is False


# --- response parsing -----------------------------------------------------


@pytest.mark.parametrize("probability", [0, 0.5, 1, 0.0123])
def test_parse_predicate_probability(probability):
    payload = {"answers": [{"type": "predicate", "name": "decision", "probability": probability}]}
    assert parse_response(payload) == float(probability)


def test_parse_picks_the_decision_answer_by_name():
    payload = {
        "answers": [
            {"type": "predicate", "name": "other", "probability": 0.1},
            {"type": "predicate", "name": "decision", "probability": 0.7},
        ]
    }
    assert parse_response(payload) == 0.7


def test_parse_refusal():
    with pytest.raises(DecisionError, match=r"^refusal$"):
        parse_response({"answers": [{"type": "refusal", "name": "decision"}]})


@pytest.mark.parametrize(
    "payload, message",
    [
        ([], "no answers"),
        ({}, "no answers"),
        ({"answers": None}, "no answers"),
        ({"answers": [{"type": "predicate", "name": "x", "probability": 1}]}, "answer missing"),
        ({"answers": [{"type": "choice", "name": "decision"}]}, "unexpected answer type"),
        ({"answers": [{"type": "predicate", "name": "decision"}]}, "probability missing"),
        (
            {"answers": [{"type": "predicate", "name": "decision", "probability": True}]},
            "probability missing",
        ),
        (
            {"answers": [{"type": "predicate", "name": "decision", "probability": "0.5"}]},
            "probability missing",
        ),
        (
            {"answers": [{"type": "predicate", "name": "decision", "probability": 1.5}]},
            "out of range",
        ),
        (
            {"answers": [{"type": "predicate", "name": "decision", "probability": float("nan")}]},
            "out of range",
        ),
    ],
)
def test_parse_rejects_malformed_responses(payload, message):
    with pytest.raises(DecisionError, match=message):
        parse_response(payload)


# --- HTTP errors, retries, timeouts ---------------------------------------


def test_refusal_over_http(stub):
    with pytest.raises(DecisionError) as e:
        make_client(stub).decide("q", "REFUSE this")
    assert str(e.value) == "refusal"
    assert not e.value.retryable and not e.value.fatal


def test_invalid_api_key_is_fatal_and_not_retried(stub):
    with pytest.raises(DecisionError) as e:
        make_client(stub, api_key="sk-wrong").decide("q", "text")
    assert str(e.value) == "HTTP 401: Incorrect API key provided."
    assert e.value.fatal
    assert stub.state.requests == 1


def test_server_error_is_retried_then_reported(stub):
    sleeps = []
    with pytest.raises(DecisionError) as e:
        make_client(stub, sleep=sleeps.append, max_retries=2).decide("q", "HTTP500")
    assert str(e.value) == "HTTP 500: The server had an error while processing your request."
    assert e.value.retryable and not e.value.fatal
    assert stub.state.requests == 3
    assert sleeps == [1.0, 2.0]


def test_transient_error_recovers_on_retry(stub):
    assert make_client(stub).decide("q", "FLAKY but denied") == 0.95
    assert stub.state.by_input["FLAKY but denied"] == 2


def test_rate_limit_honours_retry_after(stub):
    sleeps = []
    with pytest.raises(DecisionError, match=r"HTTP 429: Rate limit reached\."):
        make_client(stub, sleep=sleeps.append).decide("q", "RATELIMIT")
    assert sleeps == [0.0, 0.0]


def test_non_json_body(stub):
    with pytest.raises(DecisionError, match="invalid response: body is not JSON"):
        make_client(stub).decide("q", "BADJSON")


def test_timeout(stub):
    with pytest.raises(DecisionError) as e:
        make_client(stub, timeout=1, max_retries=0).decide("q", "SLOW")
    assert str(e.value) == "timeout after 1s"
    assert e.value.retryable


def test_connection_error_is_reported_without_secrets():
    client = DecisionsClient(
        API_KEY, base_url="http://127.0.0.1:9/v1", max_retries=0, sleep=lambda s: None
    )
    with pytest.raises(DecisionError) as e:
        client.decide("q", "text")
    assert str(e.value) == "connection error: Connection refused"
    assert e.value.retryable


@pytest.mark.parametrize(
    "error, message",
    [
        (
            requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='api.openai.com', port=443): Max retries exceeded "
                'with url: /v1/decisions (Caused by NameResolutionError("<urllib3.connection.'
                "HTTPSConnection object at 0x7f>: Failed to resolve 'api.openai.com' "
                '([Errno -3] Temporary failure in name resolution)"))'
            ),
            "connection error: Temporary failure in name resolution",
        ),
        (
            requests.exceptions.SSLError(
                "HTTPSConnectionPool(host='api.openai.com', port=443): Max retries exceeded "
                "(Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]"
                " certificate verify failed: self-signed certificate in certificate chain "
                "(_ssl.c:1006)')))"
            ),
            "TLS error: certificate verify failed: self-signed certificate in certificate chain",
        ),
        (
            requests.exceptions.ProxyError(
                "HTTPSConnectionPool(host='api.openai.com', port=443): Max retries exceeded "
                "(Caused by ProxyError('Unable to connect to proxy', NewConnectionError("
                "'<urllib3.connection.HTTPSConnection object at 0x7f>: Failed to establish a "
                "new connection: [Errno 111] Connection refused')))"
            ),
            "proxy error: Connection refused",
        ),
        (
            requests.exceptions.ChunkedEncodingError("broken"),
            "connection error: ChunkedEncodingError",
        ),
    ],
)
def test_transport_errors_are_summarised(monkeypatch, error, message):
    def post(*args, **kwargs):
        raise error

    monkeypatch.setattr(requests.Session, "post", post)
    client = DecisionsClient(API_KEY, max_retries=0)
    with pytest.raises(DecisionError) as e:
        client.decide("q", "text")
    assert str(e.value) == message
    assert e.value.retryable


def test_error_messages_redact_secrets(monkeypatch):
    class Response:
        status_code = 400
        reason = "Bad Request"
        headers: ClassVar[dict] = {}
        text = ""

        def json(self):
            return {"error": {"message": f"key {API_KEY} and proxy hunter2 rejected"}}

    client = DecisionsClient(API_KEY, secrets=("hunter2",))
    monkeypatch.setattr(requests.Session, "post", lambda *a, **k: Response())
    with pytest.raises(DecisionError) as e:
        client.decide("q", "text")
    assert str(e.value) == "HTTP 400: key *** and proxy *** rejected"


# --- evaluator ------------------------------------------------------------


def run(evaluator, texts):
    return [(p, err) for _, p, err, _ in evaluator.evaluate(texts, lambda t: t)]


def test_evaluator_preserves_order_and_isolates_failures(stub):
    evaluator = Evaluator(make_client(stub, max_retries=0), "q", concurrency=4)
    texts = ["login failed", "all good", "REFUSE", None, "HTTP500", "", "access denied", "BADJSON"]
    assert run(evaluator, texts) == [
        (0.95, None),
        (0.05, None),
        (None, "refusal"),
        (None, "empty input"),
        (None, "HTTP 500: The server had an error while processing your request."),
        (None, "empty input"),
        (0.95, None),
        (None, "invalid response: body is not JSON"),
    ]


def test_evaluator_calls_the_api_once_per_distinct_input(stub):
    evaluator = Evaluator(make_client(stub), "q", concurrency=4)
    run(evaluator, ["a", "b", "a", "a"])
    run(evaluator, ["b", "c"])
    assert stub.state.by_input == {"a": 1, "b": 1, "c": 1}
    assert evaluator.stats["api_calls"] == 1


def test_evaluator_retries_transient_failures_in_later_chunks(stub):
    evaluator = Evaluator(make_client(stub, max_retries=0), "q", concurrency=2)
    assert run(evaluator, ["FLAKY"]) == [(None, "HTTP 503: The engine is currently overloaded.")]
    assert run(evaluator, ["FLAKY"]) == [(0.05, None)]


def test_evaluator_stops_calling_after_a_fatal_error(stub):
    evaluator = Evaluator(make_client(stub, api_key="sk-wrong"), "q", concurrency=1)
    results = run(evaluator, [f"event {i}" for i in range(20)])
    assert results == [(None, "HTTP 401: Incorrect API key provided.")] * 20
    assert stub.state.requests == 1
    assert evaluator.stats["api_calls"] == 1


def test_evaluator_bounds_requests_ahead_of_the_consumer():
    started = []
    release = threading.Event()

    class SlowClient:
        def decide(self, prompt, text, model):
            started.append(text)
            release.wait(5)
            return 0.5

    evaluator = Evaluator(SlowClient(), "q", concurrency=2)
    results = evaluator.evaluate([f"t{i}" for i in range(100)], lambda t: t)
    release.set()
    next(results)
    evaluator.close()
    assert len(started) <= 2 * 4 + 2


def test_evaluator_contains_unexpected_exceptions():
    class BrokenClient:
        def decide(self, prompt, text, model):
            raise KeyError("boom")

    evaluator = Evaluator(BrokenClient(), "q")
    assert run(evaluator, ["x"]) == [(None, "internal error: KeyError: 'boom'")]


# --- choice and score questions -------------------------------------------

TEAMS = Choice(
    "Which team owns this?", described("network", "storage", "identity", "security operations")
)
SEVERITY = Score(
    "How severe is this event?",
    [
        ("low", "Routine activity, no action needed."),
        ("medium", "Worth a look during business hours."),
        ("high risk", "Needs immediate investigation."),
    ],
)

# Live gpt-6-luna answers to these questions about an sshd failed-password line.
LIVE_CHOICE = {
    "model": "gpt-6-luna",
    "answers": [
        {
            "type": "choice",
            "name": "decision",
            "choice": "security operations",
            "probabilities": [
                {"value": "network", "probability": 0.0},
                {"value": "storage", "probability": 0.01},
                {"value": "identity", "probability": 0.3},
                {"value": "security operations", "probability": 0.69},
            ],
            "confidence": 0.59,
        }
    ],
    "usage": {"input_tokens": 142, "total_tokens": 142},
}
LIVE_SCORE = {
    "model": "gpt-6-luna",
    "answers": [
        {
            "type": "score",
            "name": "decision",
            "score": 0.75,
            "probabilities": [
                {"value": 0, "label": "low", "probability": 0.26},
                {"value": 1, "label": "medium", "probability": 0.73},
                {"value": 2, "label": "high risk", "probability": 0.01},
            ],
            "confidence": 0.6,
        }
    ],
    "usage": {"input_tokens": 157, "total_tokens": 157},
}


def test_build_request_for_a_choice_question():
    assert build_request(Choice("Which?", described("a", "b c")), "text", "gpt-6-luna") == {
        "model": "gpt-6-luna",
        "input": "text",
        "questions": [
            {
                "type": "choice",
                "name": "decision",
                "instructions": "Which?",
                "choices": [
                    {"value": "a", "description": "About a."},
                    {"value": "b c", "description": "About b c."},
                ],
            }
        ],
    }


def test_build_request_for_a_score_question():
    question = Score("How bad?", [("low", "Fine."), ("high", "Bad.")])
    assert build_request(question, "text", "gpt-6-luna")["questions"] == [
        {
            "type": "score",
            "name": "decision",
            "instructions": "How bad?",
            "levels": [
                {"label": "low", "description": "Fine."},
                {"label": "high", "description": "Bad."},
            ],
        }
    ]


def test_build_request_with_a_predicate_question_matches_a_prompt():
    assert build_request(Predicate("q"), "t") == build_request("q", "t")


def test_parse_live_choice_response():
    assert parse_response(LIVE_CHOICE, TEAMS) == ChoiceAnswer(
        "security operations", 0.69, 0.59, (0.0, 0.01, 0.3, 0.69)
    )


def test_parse_live_score_response():
    assert parse_response(LIVE_SCORE, SEVERITY) == ScoreAnswer(0.75, 0.6, (0.26, 0.73, 0.01))


def test_choice_probabilities_follow_the_option_order_not_the_response_order():
    payload = {
        "answers": [
            {
                "type": "choice",
                "name": "decision",
                "choice": "b",
                "probabilities": [
                    {"value": "b", "probability": 0.7},
                    {"value": "a", "probability": 0.3},
                ],
                "confidence": 0.4,
            }
        ]
    }
    assert parse_response(payload, Choice("q", described("a", "b"))) == ChoiceAnswer(
        "b", 0.7, 0.4, (0.3, 0.7)
    )


@pytest.mark.parametrize("question", [TEAMS, SEVERITY])
def test_parse_choice_and_score_refusals(question):
    with pytest.raises(DecisionError, match=r"^refusal$"):
        parse_response({"answers": [{"type": "refusal", "name": "decision"}]}, question)


def choice_answer(**changes):
    answer = dict(LIVE_CHOICE["answers"][0])
    answer.update(changes)
    return {"answers": [{k: v for k, v in answer.items() if v is not None}]}


def score_answer(**changes):
    answer = dict(LIVE_SCORE["answers"][0])
    answer.update(changes)
    return {"answers": [{k: v for k, v in answer.items() if v is not None}]}


def probabilities(*pairs):
    return [{"value": v, "probability": p} for v, p in pairs]


@pytest.mark.parametrize(
    "payload, question, message",
    [
        (LIVE_SCORE, TEAMS, "unexpected answer type 'score'"),
        (LIVE_CHOICE, SEVERITY, "unexpected answer type 'choice'"),
        ({"answers": [{"type": "choice", "name": "x"}]}, TEAMS, "answer missing"),
        (choice_answer(choice="legal"), TEAMS, "choice is not one of the options"),
        (choice_answer(choice=True), TEAMS, "choice is not one of the options"),
        (choice_answer(probabilities=None), TEAMS, "probabilities missing"),
        (choice_answer(probabilities="0.5"), TEAMS, "probabilities missing"),
        (
            choice_answer(probabilities=probabilities(("network", 1.0))),
            TEAMS,
            "probabilities do not match the options",
        ),
        (
            choice_answer(
                probabilities=[
                    *LIVE_CHOICE["answers"][0]["probabilities"],
                    {"value": "legal", "probability": 0},
                ]
            ),
            TEAMS,
            "probabilities do not match the options",
        ),
        (
            choice_answer(
                probabilities=[
                    *LIVE_CHOICE["answers"][0]["probabilities"][:3],
                    {"value": "network", "probability": 0.69},
                ]
            ),
            TEAMS,
            "probabilities do not match the options",
        ),
        (
            choice_answer(
                probabilities=[
                    *LIVE_CHOICE["answers"][0]["probabilities"][:3],
                    {"value": "security operations", "probability": 1.5},
                ]
            ),
            TEAMS,
            "probability out of range",
        ),
        (
            choice_answer(probabilities=[{"value": ["network"], "probability": 1.0}]),
            TEAMS,
            "probabilities do not match the options",
        ),
        (choice_answer(probabilities=[None]), TEAMS, "probabilities do not match the options"),
        (choice_answer(confidence=None), TEAMS, "confidence missing"),
        (choice_answer(confidence="high"), TEAMS, "confidence missing"),
        (choice_answer(confidence=float("inf")), TEAMS, "confidence out of range"),
        (score_answer(score=None), SEVERITY, "score missing"),
        (score_answer(score=float("nan")), SEVERITY, "score out of range"),
        (score_answer(confidence=None), SEVERITY, "confidence missing"),
        (
            # True == 1, but a boolean is not a level index.
            score_answer(probabilities=probabilities((0, 0.2), (True, 0.7), (2, 0.1))),
            SEVERITY,
            "probabilities do not match the options",
        ),
        (
            score_answer(probabilities=probabilities((0, 0.2), (1, 0.7), (3, 0.1))),
            SEVERITY,
            "probabilities do not match the options",
        ),
    ],
)
def test_parse_rejects_malformed_choice_and_score_answers(payload, question, message):
    with pytest.raises(DecisionError, match=message):
        parse_response(payload, question)


@pytest.mark.parametrize(
    "make, message",
    [
        (lambda: Choice("q", described("a")), "at least 2 options are required, but got 1"),
        (lambda: Choice("q", described()), "at least 2 options are required, but got 0"),
        (lambda: Choice("q", described(*[str(i) for i in range(256)])), "at most 255 options"),
        (lambda: Choice("q", described("a", "b", "a")), "duplicate option 'a'"),
        (lambda: Choice("q", described("a", "")), "options must not be empty"),
        (lambda: Score("q", [("low", "d")]), "at least 2 levels are required, but got 1"),
        (lambda: Score("q", [("low", "d"), ("low", "e")]), "duplicate level 'low'"),
        (lambda: Score("q", [("low", "d"), (" ", "e")]), "levels must not be empty"),
    ],
)
def test_questions_reject_option_lists_the_api_would(make, message):
    with pytest.raises(ValueError, match=message):
        make()


def test_choice_and_score_over_http(stub):
    client = make_client(stub)
    teams = Choice("Which team?", described("network", "storage", "identity"))
    answer = client.ask(teams, "the storage array is full")
    assert answer.choice == "storage" and answer.probability == 0.8
    assert answer.probabilities == (0.1, 0.8, 0.1) and answer.confidence == 0.7
    severity = Score("How bad?", [("low", "Fine."), ("medium", "Hmm."), ("high", "Bad.")])
    answer = client.ask(severity, "a HIGH load")
    assert answer == ScoreAnswer(1.7, 0.6, (0.1, 0.1, 0.8))
    assert [b["questions"][0]["type"] for b in stub.state.bodies] == ["choice", "score"]
    with pytest.raises(DecisionError, match=r"^refusal$"):
        client.ask(teams, "REFUSE this")


def test_evaluator_answers_choice_questions(stub):
    teams = Choice("Which team?", described("network", "storage"))
    evaluator = Evaluator(make_client(stub, max_retries=0), teams, concurrency=2)
    results = run(evaluator, ["network down", "disk full", "network down", "HTTP500", ""])
    assert [(a and a.choice, err) for a, err in results] == [
        ("network", None),
        ("network", None),
        ("network", None),
        (None, "HTTP 500: The server had an error while processing your request."),
        (None, "empty input"),
    ]
    assert stub.state.by_input == {"network down": 1, "disk full": 1, "HTTP500": 1}
