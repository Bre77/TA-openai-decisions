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
"""OpenAI Decisions API client, the question types it asks, and the ordered,
bounded-concurrency evaluator behind the ``decide``, ``choose`` and ``grade``
search commands.

This module has no Splunk imports so it can be unit tested on its own.
"""

import json
import math
import re
import threading
import time
from collections import OrderedDict, namedtuple
from concurrent.futures import Future, ThreadPoolExecutor

import requests

from decision_cache import PROBABILITY, cache_key, question_cache_key

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 2
DEFAULT_CACHE_SIZE = 10000
DEFAULT_LIMIT = 1000

# Name of the single question sent with every request.
QUESTION_NAME = "decision"

# The API's limits on a choice question's options.
MIN_CHOICES = 2
MAX_CHOICES = 255
# A score with fewer levels has nothing to rate; the API documents no maximum.
MIN_LEVELS = 2

MAX_ERROR_LENGTH = 200
MAX_RETRY_DELAY = 30.0

# Worth another attempt: rate limits and transient server-side failures.
RETRYABLE_STATUSES = frozenset([408, 409, 429, 500, 502, 503, 504])
# Every later request would fail the same way, so stop calling the API.
FATAL_STATUSES = frozenset([401, 403, 404])

ChoiceAnswer = namedtuple("ChoiceAnswer", "choice probability confidence probabilities")
ChoiceAnswer.__doc__ = """A choice answer: the selected option, its probability, the
answer's confidence, and the probability of every option, in option order."""

ScoreAnswer = namedtuple("ScoreAnswer", "score confidence probabilities")
ScoreAnswer.__doc__ = """A score answer: the probability-weighted average of the level
indices, the answer's confidence, and the probability of every level, in level order."""


class DecisionError(Exception):
    """A failure to decide one input.

    ``str(error)`` is the short message written to the event's error field.
    """

    def __init__(self, message, retryable=False, fatal=False, retry_after=None):
        super().__init__(_truncate(message))
        self.retryable = retryable
        self.fatal = fatal
        self.retry_after = retry_after


class Predicate:
    """A yes/no question. The answer is the probability that it is true."""

    type = "predicate"

    def __init__(self, instructions):
        self.instructions = instructions

    def to_request(self):
        return {"type": self.type, "name": QUESTION_NAME, "instructions": self.instructions}

    def parse(self, answer):
        return _probability(answer.get("probability"), "probability")

    def cache_key(self, model, text):
        return cache_key(model, self.instructions, text)

    encode = staticmethod(PROBABILITY.encode)
    decode = staticmethod(PROBABILITY.decode)

    def ask(self, client, text, model):
        return client.decide(self.instructions, text, model)


class Choice:
    """Select one of the ``choices``, ``(value, description)`` pairs with
    distinct string values. The answer is a ChoiceAnswer.

    Raises ValueError for a choice list the API would reject.
    """

    type = "choice"

    def __init__(self, instructions, choices):
        self.instructions = instructions
        self.choices = tuple((value, description) for value, description in choices)
        _check_labels(self.options, "option", MIN_CHOICES, MAX_CHOICES)

    @property
    def options(self):
        return tuple(value for value, _ in self.choices)

    def to_request(self):
        return {
            "type": self.type,
            "name": QUESTION_NAME,
            "instructions": self.instructions,
            "choices": [
                {"value": value, "description": description} for value, description in self.choices
            ],
        }

    def parse(self, answer):
        probabilities = _distribution(answer.get("probabilities"), self.options)
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in self.options:
            raise DecisionError("invalid response: choice is not one of the options")
        confidence = _number(answer.get("confidence"), "confidence")
        index = self.options.index(choice)
        return ChoiceAnswer(choice, probabilities[index], confidence, probabilities)

    def cache_key(self, model, text):
        choices = [list(choice) for choice in self.choices]
        return question_cache_key(self.type, model, self.instructions, choices, text)

    @staticmethod
    def encode(answer):
        return {
            "choice": answer.choice,
            "confidence": answer.confidence,
            "probabilities": json.dumps(list(answer.probabilities)),
        }

    def decode(self, doc):
        probabilities = _cached_probabilities(doc, len(self.options))
        choice, confidence = doc.get("choice"), _cached_number(doc.get("confidence"))
        if probabilities is None or confidence is None or choice not in self.options:
            return None
        index = self.options.index(choice)
        return ChoiceAnswer(choice, probabilities[index], confidence, probabilities)

    def ask(self, client, text, model):
        return client.ask(self, text, model)


class Score:
    """Rate the input against ordered ``levels``, ``(label, description)`` pairs
    from lowest to highest. The answer is a ScoreAnswer.

    Raises ValueError for a level list the API would reject.
    """

    type = "score"

    def __init__(self, instructions, levels):
        self.instructions = instructions
        self.levels = tuple((label, description) for label, description in levels)
        _check_labels([label for label, _ in self.levels], "level", MIN_LEVELS)

    @property
    def labels(self):
        return tuple(label for label, _ in self.levels)

    def to_request(self):
        return {
            "type": self.type,
            "name": QUESTION_NAME,
            "instructions": self.instructions,
            "levels": [
                {"label": label, "description": description} for label, description in self.levels
            ],
        }

    def parse(self, answer):
        indices = tuple(range(len(self.levels)))
        probabilities = _distribution(answer.get("probabilities"), indices)
        score = _number(answer.get("score"), "score")
        confidence = _number(answer.get("confidence"), "confidence")
        return ScoreAnswer(score, confidence, probabilities)

    def cache_key(self, model, text):
        levels = [list(level) for level in self.levels]
        return question_cache_key(self.type, model, self.instructions, levels, text)

    @staticmethod
    def encode(answer):
        return {
            "score": answer.score,
            "confidence": answer.confidence,
            "probabilities": json.dumps(list(answer.probabilities)),
        }

    def decode(self, doc):
        probabilities = _cached_probabilities(doc, len(self.levels))
        score, confidence = _cached_number(doc.get("score")), _cached_number(doc.get("confidence"))
        if probabilities is None or score is None or confidence is None:
            return None
        return ScoreAnswer(score, confidence, probabilities)

    def ask(self, client, text, model):
        return client.ask(self, text, model)


def as_question(question):
    """A question object, or a Predicate for a plain prompt string."""
    return Predicate(question) if isinstance(question, str) else question


def _check_labels(labels, kind, minimum, maximum=None):
    if len(labels) < minimum:
        raise ValueError(f"at least {minimum} {kind}s are required, but got {len(labels)}")
    if maximum is not None and len(labels) > maximum:
        raise ValueError(f"at most {maximum} {kind}s are allowed, but got {len(labels)}")
    seen = set()
    for label in labels:
        if not label.strip():
            raise ValueError(f"{kind}s must not be empty")
        if label in seen:
            raise ValueError(f"duplicate {kind} {label!r}")
        seen.add(label)


def build_request(question, text, model=DEFAULT_MODEL):
    """Return the JSON body for a single question about ``text``.

    ``question`` is a question object, or a prompt string for a predicate.
    """
    return {
        "model": model,
        "input": text,
        "questions": [as_question(question).to_request()],
    }


def parse_response(payload, question=None):
    """Return the answer to ``question`` (default: a predicate) from a
    Decisions API response body.

    Raises DecisionError for a refusal or a malformed response.
    """
    question = question or _PREDICATE
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, list):
        raise DecisionError("invalid response: no answers")
    answer = next(
        (a for a in answers if isinstance(a, dict) and a.get("name") == QUESTION_NAME),
        None,
    )
    if answer is None:
        raise DecisionError("invalid response: answer missing")
    answer_type = answer.get("type")
    if answer_type == "refusal":
        raise DecisionError("refusal")
    if answer_type != question.type:
        raise DecisionError(f"invalid response: unexpected answer type {answer_type!r}")
    return question.parse(answer)


_PREDICATE = Predicate(None)


def _number(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionError(f"invalid response: {what} missing")
    value = float(value)
    if not math.isfinite(value):
        raise DecisionError(f"invalid response: {what} out of range")
    return value


def _probability(value, what):
    value = _number(value, what)
    if not 0.0 <= value <= 1.0:
        raise DecisionError(f"invalid response: {what} out of range")
    return value


def _distribution(items, values):
    """The probabilities in ``items`` (``[{"value": v, "probability": p}]``),
    in the order of ``values``, which they must cover exactly once each."""
    if not isinstance(items, list):
        raise DecisionError("invalid response: probabilities missing")
    found = {}
    for item in items:
        value = item.get("value") if isinstance(item, dict) else None
        # bool is an int subclass, and True == 1, so match the value's type too.
        if not any(type(value) is type(v) and value == v for v in values) or value in found:
            raise DecisionError("invalid response: probabilities do not match the options")
        found[value] = _probability(item.get("probability"), "probability")
    if len(found) != len(values):
        raise DecisionError("invalid response: probabilities do not match the options")
    return tuple(found[value] for value in values)


def _cached_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _cached_probabilities(doc, count):
    try:
        values = json.loads(doc.get("probabilities"))
    except (TypeError, ValueError):
        return None
    if not isinstance(values, list) or len(values) != count:
        return None
    values = [_cached_number(v) for v in values]
    if any(v is None or not 0.0 <= v <= 1.0 for v in values):
        return None
    return tuple(values)


def _truncate(message):
    message = " ".join(str(message).split())
    if len(message) > MAX_ERROR_LENGTH:
        message = message[: MAX_ERROR_LENGTH - 3] + "..."
    return message


# The useful part of urllib3's nested connection error text, e.g.
# "[Errno 111] Connection refused" or "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed".
_ERROR_DETAIL = re.compile(r"\[(?:Errno -?\d+|SSL: \w+)\] ([^'\"()\]]+)")


def _describe_request_error(error):
    """Short description of a requests transport error."""
    if isinstance(error, requests.exceptions.ProxyError):
        kind = "proxy error"
    elif isinstance(error, requests.exceptions.SSLError):
        kind = "TLS error"
    else:
        kind = "connection error"
    match = _ERROR_DETAIL.search(str(error))
    detail = match.group(1).strip().rstrip(".") if match else type(error).__name__
    return f"{kind}: {detail}"


def _retry_after(response):
    value = response.headers.get("Retry-After")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _error_message(response):
    try:
        error = response.json().get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
    except (ValueError, AttributeError):
        pass
    return response.reason or response.text or "request failed"


class DecisionsClient:
    """Synchronous client for ``POST {base_url}/decisions``.

    Safe to share between threads: each thread gets its own requests session.
    """

    def __init__(
        self,
        api_key,
        base_url=DEFAULT_BASE_URL,
        timeout=DEFAULT_TIMEOUT,
        max_retries=DEFAULT_MAX_RETRIES,
        proxies=None,
        secrets=(),
        sleep=time.sleep,
    ):
        if not api_key:
            raise ValueError("an API key is required")
        self.url = base_url.rstrip("/") + "/decisions"
        self.timeout = timeout
        self.max_retries = max_retries
        self._api_key = api_key
        self._proxies = proxies or None
        self._secrets = [s for s in (api_key, *tuple(secrets)) if s]
        self._sleep = sleep
        self._local = threading.local()

    def decide(self, prompt, text, model=DEFAULT_MODEL):
        """Return the probability that ``text`` satisfies ``prompt``.

        Retries rate-limited and transient failures, then raises DecisionError.
        """
        return self.ask(Predicate(prompt), text, model)

    def ask(self, question, text, model=DEFAULT_MODEL):
        """Return the answer to ``question`` (a question object) about ``text``.

        Retries rate-limited and transient failures, then raises DecisionError.
        """
        body = build_request(question, text, model)
        attempt = 0
        while True:
            try:
                return self._post(body, question)
            except DecisionError as e:
                if not e.retryable or attempt >= self.max_retries:
                    raise
                delay = e.retry_after if e.retry_after is not None else 2.0**attempt
                self._sleep(min(delay, MAX_RETRY_DELAY))
                attempt += 1

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "Authorization": "Bearer " + self._api_key,
                    "Content-Type": "application/json",
                    "User-Agent": "TA-openai-decisions",
                }
            )
            if self._proxies:
                session.proxies.update(self._proxies)
                # Configured proxies win over HTTP(S)_PROXY from the environment.
                session.trust_env = False
            self._local.session = session
        return session

    def _post(self, body, question):
        try:
            response = self._session().post(self.url, json=body, timeout=self.timeout)
        except requests.Timeout:
            raise DecisionError(f"timeout after {self.timeout}s", retryable=True) from None
        except requests.RequestException as e:
            raise DecisionError(self._redact(_describe_request_error(e)), retryable=True) from e
        if response.status_code != 200:
            status = response.status_code
            raise DecisionError(
                f"HTTP {status}: {self._redact(_error_message(response))}",
                retryable=status in RETRYABLE_STATUSES,
                fatal=status in FATAL_STATUSES,
                retry_after=_retry_after(response),
            )
        try:
            payload = response.json()
        except ValueError:
            raise DecisionError("invalid response: body is not JSON") from None
        return parse_response(payload, question)

    def _redact(self, message):
        for secret in self._secrets:
            message = message.replace(secret, "***")
        return message


class Evaluator:
    """Decides chunks of inputs with bounded concurrency, preserving order.

    ``question`` is a question object (Predicate, Choice or Score), or a
    prompt string for a predicate. Identical inputs within a search share one
    API call (bounded in-memory cache). With a result ``cache`` (see
    decision_cache.KVStoreCache), each chunk's remaining inputs are looked up
    in one batched read before any API call, and the chunk's new answers are
    written back in batches.
    At most ``limit`` API requests are sent over all chunks (0 or None: no
    limit), not counting retries of a request; inputs answered from the cache
    or from memory do not count. Once the limit is reached, inputs that need
    a request get the ``limit_error`` instead.
    After a fatal error such as an invalid API key no further requests are sent.
    """

    def __init__(
        self,
        client,
        question,
        model=DEFAULT_MODEL,
        concurrency=4,
        cache=None,
        memory_size=DEFAULT_CACHE_SIZE,
        limit=None,
    ):
        self._client = client
        self._question = as_question(question)
        self._model = model
        self._pool = ThreadPoolExecutor(max_workers=concurrency)
        self._window = concurrency * 4
        self._cache = cache
        self._memory = OrderedDict()
        self._memory_size = memory_size
        self._fatal = None
        self._lock = threading.Lock()
        self._calls = 0  # API requests attempted (before retries), for stats
        self._limit = limit or None
        self._submitted = 0  # API requests submitted, counted against the limit
        self.limit_error = limit_error(limit) if self._limit else None
        self.stats = {}

    def evaluate(self, items, get_text):
        """Yield ``(item, answer, error, from_cache)`` for each item, in input order.

        ``get_text(item)`` returns the text to decide; an empty value yields an
        error without calling the API. Exactly one of answer and error is
        None. ``from_cache`` is True when the answer was read from the result
        cache, in this chunk or an earlier one. ``stats`` describes the
        chunk once the generator is exhausted.
        """
        items = list(items)
        texts = [get_text(item) for item in items]
        stats = {"events": len(items), "api_calls": 0, "cache_hits": 0, "cached": 0, "limited": 0}
        calls_before = self._calls

        chunk = {}  # text -> future, for every distinct non-empty text in the chunk
        unknown = []
        for text in texts:
            if text and text not in chunk:
                chunk[text] = self._known(text)
                if chunk[text] is None:
                    unknown.append(text)

        keys = {}
        if self._cache is not None and unknown:
            keys = {text: self._question.cache_key(self._model, text) for text in unknown}
            hits = self._cache.lookup(list(keys.values()), self._question)
            for text in unknown:
                if keys[text] in hits:
                    chunk[text] = self._remember(text, _from_cache(hits[keys[text]]))
                    stats["cache_hits"] += 1

        # Submit API calls lazily, at most `window` records ahead of the record
        # being yielded, so an abandoned search does not leave a long queue of
        # paid requests behind it.
        pending = [text for text in unknown if chunk[text] is None]
        first_seen = {}
        for index, text in enumerate(texts):
            if text and text not in first_seen:
                first_seen[text] = index
        submitted = {}
        position = 0
        for index, (item, text) in enumerate(zip(items, texts)):
            while position < len(pending) and first_seen[pending[position]] < index + self._window:
                text_to_submit = pending[position]
                if self._limit is not None and self._submitted >= self._limit and not self._fatal:
                    # Not remembered, so a later chunk looks it up in the cache again.
                    # After a fatal error the input is submitted anyway: _decide fails
                    # it with that error without sending a request.
                    chunk[text_to_submit] = _failed(DecisionError(self.limit_error))
                    stats["limited"] += 1
                else:
                    self._submitted += 1
                    future = self._pool.submit(self._decide, text_to_submit)
                    chunk[text_to_submit] = submitted[text_to_submit] = self._remember(
                        text_to_submit, future
                    )
                position += 1
            yield self._result(item, chunk[text] if text else None)
        stats["api_calls"] = self._calls - calls_before

        if self._cache is not None and submitted:
            fresh = {}
            for text, future in submitted.items():
                if future.exception() is None:
                    fresh[keys[text]] = future.result()
            stats["cached"] = self._cache.store(fresh, self._question)
        self.stats = stats

    def close(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _known(self, text):
        """Return the in-memory future for ``text`` unless it needs another try."""
        future = self._memory.get(text)
        if future is None or _transient_failure(future):
            return None
        self._memory.move_to_end(text)
        return future

    def _remember(self, text, future):
        self._memory[text] = future
        self._memory.move_to_end(text)
        while len(self._memory) > self._memory_size:
            self._memory.popitem(last=False)
        return future

    def _decide(self, text):
        if self._fatal is not None:
            raise DecisionError(str(self._fatal), fatal=True)
        with self._lock:
            self._calls += 1
        try:
            return self._question.ask(self._client, text, self._model)
        except DecisionError as e:
            if e.fatal and self._fatal is None:
                self._fatal = e
            raise

    @staticmethod
    def _result(item, future):
        if future is None:
            return item, None, "empty input", False
        try:
            return item, future.result(), None, isinstance(future, _CachedResult)
        except DecisionError as e:
            return item, None, str(e), False
        except Exception as e:  # never let one event kill the search
            return item, None, _truncate(f"internal error: {type(e).__name__}: {e}"), False


class _CachedResult(Future):
    """An answer read from the result cache rather than the API."""


def _from_cache(value):
    future = _CachedResult()
    future.set_result(value)
    return future


def _failed(error):
    future = Future()
    future.set_exception(error)
    return future


def limit_error(limit):
    """The error given to an input that needs an API request once ``limit`` is reached."""
    return f"limit of {limit} API call{'' if limit == 1 else 's'} reached"


def _transient_failure(future):
    if not future.done():
        return False
    error = future.exception()
    return isinstance(error, DecisionError) and error.retryable
