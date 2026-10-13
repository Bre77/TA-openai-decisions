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
"""KV store result cache for the ``decide`` search command.

Only successful probabilities are cached; errors and refusals never are.
Entries expire after a TTL and the collection is capped at a maximum size.

This module has no Splunk imports; it drives a splunklib
``KVStoreCollectionData`` (or any object with the same methods).
"""

import hashlib
import json
import logging
import time

COLLECTION = "ta_openai_decisions_cache"
DEFAULT_TTL_DAYS = 7
DEFAULT_MAX_ENTRIES = 1000000

# Keys per $or clause in one batch_find query; all clauses for a chunk go in a
# single batch_find request.
LOOKUP_GROUP_SIZE = 500
# Splunk's default limits.conf [kvstore] max_documents_per_batch_save.
SAVE_BATCH_SIZE = 1000


def cache_key(model, prompt, text):
    """sha256 over the model, the question instructions, and the exact input.

    The three values are JSON-encoded as an array so no two different
    combinations can produce the same byte string.
    """
    material = json.dumps([model, prompt, text], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class KVStoreCache:
    """Batched lookups and writes against the cache collection.

    Any KV store failure is logged and degrades to calling the API: a failed
    lookup disables lookups for the rest of the search, a failed write
    disables writes (for example when the user's role can read but not write
    the collection).
    """

    def __init__(
        self,
        data,
        ttl_days=DEFAULT_TTL_DAYS,
        max_entries=DEFAULT_MAX_ENTRIES,
        logger=None,
        clock=time.time,
    ):
        self._data = data
        self._ttl = float(ttl_days) * 86400
        self._max_entries = max_entries
        self._log = logger or logging.getLogger(__name__)
        self._clock = clock
        self._can_read = True
        self._can_write = True

    def lookup(self, keys):
        """Return ``{key: probability}`` for the unexpired entries among ``keys``."""
        keys = sorted(set(keys))
        if not keys or not self._can_read:
            return {}
        cutoff = self._clock() - self._ttl
        queries = [
            {
                "query": {
                    "$and": [
                        {"$or": [{"_key": k} for k in keys[i : i + LOOKUP_GROUP_SIZE]]},
                        {"created": {"$gte": cutoff}},
                    ]
                },
                "fields": ["_key", "probability"],
                "limit": LOOKUP_GROUP_SIZE,
            }
            for i in range(0, len(keys), LOOKUP_GROUP_SIZE)
        ]
        try:
            results = self._data.batch_find(*queries)
        except Exception as e:
            self._can_read = False
            self._log.warning("decide cache lookup failed, continuing without it: %s", e)
            return {}
        hits = {}
        for group in results:
            for doc in group:
                probability = _probability(doc.get("probability"))
                if probability is not None:
                    hits[doc.get("_key")] = probability
        return hits

    def store(self, results):
        """Save ``{key: probability}`` in batches. Returns the number written."""
        if not results or not self._can_write:
            return 0
        now = self._clock()
        docs = [
            {"_key": key, "probability": probability, "created": now}
            for key, probability in sorted(results.items())
        ]
        written = 0
        for i in range(0, len(docs), SAVE_BATCH_SIZE):
            batch = docs[i : i + SAVE_BATCH_SIZE]
            try:
                self._data.batch_save(*batch)
            except Exception as e:
                self._can_write = False
                self._log.warning("decide cache write failed, not caching further results: %s", e)
                break
            written += len(batch)
        return written

    def purge(self):
        """Delete expired entries, then the oldest beyond the maximum size."""
        try:
            cutoff = self._clock() - self._ttl
            self._data.delete(query=json.dumps({"created": {"$lt": cutoff}}))
            newest_excess = self._data.query(
                sort="created:-1", skip=self._max_entries, limit=1, fields="created"
            )
            if newest_excess:
                created = newest_excess[0].get("created")
                self._data.delete(query=json.dumps({"created": {"$lte": created}}))
        except Exception as e:
            self._log.debug("decide cache purge skipped: %s", e)


def _probability(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if 0.0 <= value <= 1.0 else None
