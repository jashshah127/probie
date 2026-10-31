"""A small client for OpenAI-compatible /chat/completions endpoints.

Meta Model API, Baseten, Fireworks and vLLM (on Modal) all speak this format, so
one client covers every model on the ladder. Standard library only.
"""

from __future__ import annotations

import email.utils
import http.client
import json
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Protocol

from probie import __version__

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
MAX_BACKOFF_SECONDS = 60.0
MAX_RETRY_AFTER_SECONDS = 120.0


class ProviderError(RuntimeError):
    """A request failed. retryable says whether trying again later might work."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class ChatClient(Protocol):
    def chat(self, payload: dict) -> dict: ...


class RateLimiter:
    """Spaces request starts so they stay under a requests-per-minute limit, across threads."""

    def __init__(self, rpm: float | None, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 60.0 / rpm if rpm else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            self._sleep(start - now)


def retry_after_seconds(header: str | None, *, now: float | None = None) -> float | None:
    """Read a Retry-After header given in seconds or as an HTTP date."""
    if not header:
        return None
    header = header.strip()
    try:
        seconds = float(header)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - (time.time() if now is None else now)
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def check_response(data: object) -> dict:
    """Make sure a response has the shape the agent loop relies on."""
    if not isinstance(data, dict):
        raise ProviderError("the response wasn't a JSON object", retryable=True)
    if "error" in data and "choices" not in data:
        raise ProviderError(f"the provider returned an error: {_short(json.dumps(data['error']))}", retryable=False)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError("the response had no choices", retryable=True)
    if not isinstance(choices[0].get("message"), dict):
        raise ProviderError("the response had no message", retryable=True)
    return data


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


class HTTPChatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        timeout: float = 180.0,
        max_retries: int = 5,
        rate_limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.rate_limiter = rate_limiter or RateLimiter(None)
        self._sleep = sleep
        self._rng = rng or random.Random()

    def _redact(self, text: str) -> str:
        if self._api_key:
            text = text.replace(self._api_key, "[redacted]")
        return text

    def _backoff(self, attempt: int) -> float:
        base = min(MAX_BACKOFF_SECONDS, 2.0 ** attempt)
        return base * (0.5 + self._rng.random() / 2)

    def chat(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"probie/{__version__}",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        attempt = 0
        while True:
            self.rate_limiter.wait()
            delay: float | None = None
            try:
                request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise ProviderError("the response wasn't valid JSON", retryable=True) from None
                return check_response(data)
            except urllib.error.HTTPError as exc:
                try:
                    detail = self._redact(_short(exc.read().decode("utf-8", "replace")))
                except Exception:  # the body is only for the error message
                    detail = "(no response body)"
                if exc.code not in RETRYABLE_STATUS:
                    raise ProviderError(f"HTTP {exc.code} from {self.url}: {detail}", status=exc.code) from None
                error = ProviderError(f"HTTP {exc.code} from {self.url}: {detail}", status=exc.code, retryable=True)
                delay = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
            except ProviderError as exc:
                if not exc.retryable:
                    raise
                error = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                error = ProviderError(f"couldn't reach {self.url}: {self._redact(str(reason))}", retryable=True)
            if attempt >= self.max_retries:
                raise ProviderError(f"{error} (gave up after {attempt + 1} attempts)",
                                    status=error.status, retryable=True) from None
            self._sleep(delay if delay is not None else self._backoff(attempt))
            attempt += 1
