"""Tiny HTTP wrapper so the collector can be tested with recorded fixtures."""
import time

import requests

USER_AGENT = "SlateHunter/0.5 (personal research tool; low request volume)"


class HttpError(Exception):
    def __init__(self, status, url, message=""):
        super().__init__(f"HTTP {status} for {url} {message}".strip())
        self.status = status
        self.url = url


class HttpClient:
    """GET JSON with a timeout, one retry on transient failures, and polite spacing."""

    def __init__(self, timeout=15, min_interval=0.35):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.timeout = timeout
        self.min_interval = min_interval
        self._last = 0.0

    def get_json(self, url, params=None):
        """Return (data, headers). Raises HttpError or requests exceptions."""
        last_exc = None
        for attempt in range(2):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                time.sleep(2)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(3)
                continue
            if resp.status_code >= 400:
                # Never log query strings: they can contain the API key.
                raise HttpError(resp.status_code, url, resp.text[:200])
            return resp.json(), {k.lower(): v for k, v in resp.headers.items()}
        raise last_exc or HttpError(0, url, "retries exhausted")


class FixtureClient:
    """Test double: maps a URL substring to (data, headers) or an Exception."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        for needle, response in self.routes:
            if needle in url:
                result = response(url, params) if callable(response) else response
                if isinstance(result, Exception):
                    raise result
                return result if isinstance(result, tuple) else (result, {})
        raise HttpError(404, url, "no fixture route")
