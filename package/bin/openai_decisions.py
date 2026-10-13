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
"""OpenAI Decisions API client and the ordered, bounded-concurrency evaluator
behind the ``decide`` search command.

This module has no Splunk imports so it can be unit tested on its own.
"""

import math
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor

import requests

from decision_cache import cache_key

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 2
DEFAULT_CACHE_SIZE = 10000

# Name of the single predicate question sent with every request.
QUESTION_NAME = "decision"

MAX_ERROR_LENGTH = 200
MAX_RETRY_DELAY = 30.0

# Worth another attempt: rate limits and transient server-side failures.
RETRYABLE_STATUSES = frozenset([408, 409, 429, 500, 502, 503, 504])
# Every later request would fail the same way, so stop calling the API.
FATAL_STATUSES = frozenset([401, 403, 404])


class DecisionError(Exception):
    """A failure to decide one input.

    ``str(error)`` is the short message written to the event's error field.
    """

    def __init__(self, message, retryable=False, fatal=False, retry_after=None):
        super().__init__(_truncate(message))
        self.retryable = retryable
        self.fatal = fatal
        self.retry_after = retry_after


def build_request(prompt, text, model=DEFAULT_MODEL):
    """Return the JSON body for a single predicate question about ``text``."""
    return {
        "model": model,
        "input": text,
        "questions": [
            {
                "type": "predicate",
                "name": QUESTION_NAME,
                "instructions": prompt,
            }
        ],
    }


def parse_response(payload):
    """Return the predicate probability from a Decisions API response body.

    Raises DecisionError for a refusal or a malformed response.
    """
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
    if answer_type != "predicate":
        raise DecisionError(f"invalid response: unexpected answer type {answer_type!r}")
    probability = answer.get("probability")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        raise DecisionError("invalid response: probability missing")
    probability = float(probability)
    if math.isnan(probability) or not 0.0 <= probability <= 1.0:
        raise DecisionError("invalid response: probability out of range")
    return probability


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
        body = build_request(prompt, text, model)
        attempt = 0
        while True:
            try:
                return self._post(body)
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

    def _post(self, body):
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
        return parse_response(payload)

    def _redact(self, message):
        for secret in self._secrets:
            message = message.replace(secret, "***")
        return message


class Evaluator:
    """Decides chunks of inputs with bounded concurrency, preserving order.

    Identical inputs within a search share one API call (bounded in-memory
    cache). With a result ``cache`` (see decision_cache.KVStoreCache), each
    chunk's remaining inputs are looked up in one batched read before any API
    call, and the chunk's new probabilities are written back in batches.
    After a fatal error such as an invalid API key no further requests are sent.
    """

    def __init__(
        self,
        client,
        prompt,
        model=DEFAULT_MODEL,
        concurrency=4,
        cache=None,
        memory_size=DEFAULT_CACHE_SIZE,
    ):
        self._client = client
        self._prompt = prompt
        self._model = model
        self._pool = ThreadPoolExecutor(max_workers=concurrency)
        self._window = concurrency * 4
        self._cache = cache
        self._memory = OrderedDict()
        self._memory_size = memory_size
        self._fatal = None
        self._lock = threading.Lock()
        self._calls = 0  # client.decide invocations, for stats
        self.stats = {}

    def evaluate(self, items, get_text):
        """Yield ``(item, probability, error)`` for each item, in input order.

        ``get_text(item)`` returns the text to decide; an empty value yields an
        error without calling the API. Exactly one of probability and error is
        None. ``stats`` describes the chunk once the generator is exhausted.
        """
        items = list(items)
        texts = [get_text(item) for item in items]
        stats = {"events": len(items), "api_calls": 0, "cache_hits": 0, "cached": 0}
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
            keys = {text: cache_key(self._model, self._prompt, text) for text in unknown}
            hits = self._cache.lookup(list(keys.values()))
            for text in unknown:
                if keys[text] in hits:
                    chunk[text] = self._remember(text, _resolved(hits[keys[text]]))
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
            stats["cached"] = self._cache.store(fresh)
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
            return self._client.decide(self._prompt, text, self._model)
        except DecisionError as e:
            if e.fatal and self._fatal is None:
                self._fatal = e
            raise

    @staticmethod
    def _result(item, future):
        if future is None:
            return item, None, "empty input"
        try:
            return item, future.result(), None
        except DecisionError as e:
            return item, None, str(e)
        except Exception as e:  # never let one event kill the search
            return item, None, _truncate(f"internal error: {type(e).__name__}: {e}")


def _resolved(value):
    future = Future()
    future.set_result(value)
    return future


def _transient_failure(future):
    if not future.done():
        return False
    error = future.exception()
    return isinstance(error, DecisionError) and error.retryable
