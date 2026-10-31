"""What calls cost, and the spend cap.

A provider's spend alert doesn't stop billing, so the cap is enforced here. Before
each call the runner reserves that call's worst case: every input character counted
as half a token (real text runs closer to a quarter) plus the full max_tokens of
output. If the reservation would cross the cap, the call isn't made.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

PER_MILLION = 1_000_000
IMAGE_TOKENS_UPPER = 6_000  # generous: an invoice image costs roughly 1,500 to 3,000 tokens


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

    def as_dict(self) -> dict:
        return {
            "input": self.input_tokens,
            "cached_input": self.cached_input_tokens,
            "output": self.output_tokens,
            "reasoning": self.reasoning_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Usage":
        data = data or {}
        return cls(int(data.get("input", 0)), int(data.get("cached_input", 0)), int(data.get("output", 0)),
                   int(data.get("reasoning", 0)))


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return 0


def extract_usage(response: dict) -> Usage:
    """Read token counts from an OpenAI-style response, allowing for the usual variations."""
    usage = response.get("usage") or {}
    if not isinstance(usage, dict):
        return Usage()
    input_tokens = _int(usage.get("prompt_tokens", usage.get("input_tokens")))
    output_tokens = _int(usage.get("completion_tokens", usage.get("output_tokens")))
    cached = 0
    for container in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(container)
        if isinstance(details, dict):
            cached = max(cached, _int(details.get("cached_tokens")))
    cached = max(cached, _int(usage.get("cached_tokens")), _int(usage.get("prompt_cache_hit_tokens")))
    reasoning = 0
    for container in ("completion_tokens_details", "output_tokens_details"):
        details = usage.get(container)
        if isinstance(details, dict):
            reasoning = max(reasoning, _int(details.get("reasoning_tokens")))
    return Usage(input_tokens, min(cached, input_tokens), output_tokens, reasoning)


@dataclass(frozen=True)
class Prices:
    """USD per million tokens, or per hour for a self-hosted model."""

    input: float | None = None
    cached_input: float | None = None
    output: float | None = None
    per_hour: float | None = None

    @property
    def per_token(self) -> bool:
        return self.per_hour is None

    @property
    def known(self) -> bool:
        return self.per_hour is not None or (self.input is not None and self.output is not None)

    def cost(self, usage: Usage) -> float:
        """Cost of one call. Zero for per-hour models, whose cost comes from run time."""
        if not self.per_token or not self.known:
            return 0.0
        cached_rate = self.cached_input if self.cached_input is not None else self.input
        uncached = usage.input_tokens - usage.cached_input_tokens
        return (uncached * self.input + usage.cached_input_tokens * cached_rate + usage.output_tokens * self.output) / PER_MILLION

    def worst_case(self, payload: dict, max_tokens: int) -> float:
        if not self.per_token or not self.known:
            return 0.0
        return (upper_bound_tokens(payload) * self.input + max_tokens * self.output) / PER_MILLION


def upper_bound_tokens(payload: dict) -> int:
    """A deliberate overestimate of a request's input tokens."""
    images = 0

    def without_images(value):
        nonlocal images
        if isinstance(value, dict):
            if value.get("type") in ("image_url", "input_audio"):
                images += 1
                return {"type": value["type"]}
            return {k: without_images(v) for k, v in value.items()}
        if isinstance(value, list):
            return [without_images(v) for v in value]
        return value

    text = json.dumps({k: without_images(payload.get(k)) for k in ("messages", "tools")}, ensure_ascii=False)
    return len(text) // 2 + 64 + images * IMAGE_TOKENS_UPPER


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """A hard spend cap shared by every thread in a run.

    reserve() books a call's worst case before it's made and raises BudgetExceeded
    if that would cross the cap. settle() swaps the booking for the real cost.
    For per-hour models, spend is elapsed time times the hourly rate.
    """

    def __init__(self, cap_usd: float, *, per_hour: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        if cap_usd <= 0:
            raise ValueError("the budget must be more than zero")
        self.cap = cap_usd
        self.per_hour = per_hour
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._spent = 0.0
        self._reserved: dict[int, float] = {}
        self._next_token = 0

    def _time_cost(self) -> float:
        if self.per_hour is None:
            return 0.0
        return (self._clock() - self._started) / 3600 * self.per_hour

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent + self._time_cost()

    @property
    def reserved(self) -> float:
        with self._lock:
            return sum(self._reserved.values())

    def reserve(self, amount: float) -> int:
        amount = max(0.0, float(amount))
        with self._lock:
            committed = self._spent + self._time_cost() + sum(self._reserved.values())
            if committed + amount > self.cap or committed >= self.cap:
                raise BudgetExceeded(
                    f"the next call could cost up to ${amount:.4f}, which would pass the ${self.cap:.2f} cap "
                    f"(${self._spent + self._time_cost():.4f} spent so far)"
                )
            self._next_token += 1
            self._reserved[self._next_token] = amount
            return self._next_token

    def settle(self, token: int, actual: float) -> None:
        with self._lock:
            self._reserved.pop(token, None)
            self._spent += max(0.0, float(actual))

    def release(self, token: int) -> None:
        with self._lock:
            self._reserved.pop(token, None)
