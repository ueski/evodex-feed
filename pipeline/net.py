"""Polite HTTP + on-disk cache (stdlib only).

- Custom User-Agent on every request (TCGCSV answers 401 to the default
  ``Python-urllib`` agent and its docs ask for an identifiable one).
- Per-host minimum interval between requests (a simple shared rate limiter),
  so a thread pool can never burst above the configured requests/second.
- Retries with exponential backoff on network errors, 429 and 5xx; honours
  ``Retry-After``. 404 is raised immediately as ``NotFound``.
- Responses are cached as plain files; callers decide the cache key and the
  maximum age, so "fetch once per TCGCSV build" or "refresh TCGdex weekly" is a
  policy of the caller, not of this module.
"""

import gzip
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request

USER_AGENT = "BinderlyPipeline/0.1 (Binderly iOS data pipeline; stdlib urllib)"


class NotFound(Exception):
    pass


class FetchError(Exception):
    pass


class RateLimiter:
    """Guarantees at least ``min_interval`` seconds between request starts."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.min_interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.network = 0
        self.cached = 0
        self.retries = 0
        self.bytes = 0

    def add(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)

    def __repr__(self):
        return "network=%d cached=%d retries=%d bytes=%d" % (
            self.network, self.cached, self.retries, self.bytes)


STATS = Stats()


def _read_body(resp):
    body = resp.read()
    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
        body = gzip.decompress(body)
    return body


def http_get(url, limiter=None, retries=4, timeout=30, data=None, headers=None):
    """GET (or POST when ``data`` is given) with retries. Returns bytes."""
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    attempt = 0
    while True:
        if limiter:
            limiter.wait()
        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = _read_body(resp)
                STATS.add(network=1, bytes=len(body))
                return body
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url)
            retryable = e.code == 429 or e.code >= 500
            if not retryable or attempt >= retries:
                raise FetchError("%s -> HTTP %s" % (url, e.code))
            retry_after = e.headers.get("Retry-After") if e.headers else None
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt >= retries:
                raise FetchError("%s -> %r" % (url, e))
            delay = 2 ** attempt
        attempt += 1
        STATS.add(retries=1)
        time.sleep(delay + random.uniform(0, 0.5))


def http_status(url, limiter=None, retries=3, timeout=30):
    """HEAD ``url`` and return the HTTP status code (200, 403, 404 ...).
    Retries network errors, 429 and 5xx; returns the last status (or 0 for a
    network failure) instead of raising, so callers can record it."""
    attempt = 0
    while True:
        if limiter:
            limiter.wait()
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                STATS.add(network=1)
                return resp.status
        except urllib.error.HTTPError as e:
            STATS.add(network=1)
            if not (e.code == 429 or e.code >= 500) or attempt >= retries:
                return e.code
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt >= retries:
                return 0
        attempt += 1
        STATS.add(retries=1)
        time.sleep(2 ** attempt + random.uniform(0, 0.5))


def _atomic_write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, path)


def cached_json(url, cache_path, max_age=None, limiter=None, offline=False, data=None):
    """Return parsed JSON for ``url``, using ``cache_path`` if fresh enough.

    ``max_age`` is in seconds; ``None`` means "cache forever" (immutable keys).
    A cached 404 is stored as ``null`` so it is not re-requested every run.
    """
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if offline or max_age is None or age <= max_age:
            STATS.add(cached=1)
            with open(cache_path, "rb") as f:
                return json.loads(f.read())
    if offline:
        raise FetchError("offline and not cached: %s" % url)
    try:
        body = http_get(url, limiter=limiter, data=data,
                        headers={"Content-Type": "application/json"} if data else None)
    except NotFound:
        _atomic_write(cache_path, b"null")
        return None
    obj = json.loads(body)
    _atomic_write(cache_path, body)
    return obj


def cached_text(url, cache_path, max_age=None, limiter=None, offline=False):
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if offline or max_age is None or age <= max_age:
            STATS.add(cached=1)
            with open(cache_path, "r", encoding="utf-8") as f:
                return f.read()
    if offline:
        raise FetchError("offline and not cached: %s" % url)
    body = http_get(url, limiter=limiter)
    _atomic_write(cache_path, body)
    return body.decode("utf-8")
