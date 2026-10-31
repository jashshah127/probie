"""A record of real spend per credit pool, so runs use the free credits fully and stop before your card.

Every run and ping appends what it actually spent to runs/spend.jsonl. Spend is worked out
from token counts and the prices in probie.toml, so it can differ a little from the
provider's own billing; [credits] margin keeps a slice of each pool in reserve for that.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

LEDGER = "spend.jsonl"


def record(runs_dir: str | Path, *, pool: str | None, model: str, usd: float, what: str) -> None:
    if pool is None or usd <= 0:
        return
    path = Path(runs_dir) / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "pool": pool,
             "model": model, "usd": round(float(usd), 6), "what": what}
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry) + "\n")


def spent(runs_dir: str | Path) -> dict[str, float]:
    path = Path(runs_dir) / LEDGER
    totals: dict[str, float] = {}
    if not path.exists():
        return totals
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            totals[entry["pool"]] = totals.get(entry["pool"], 0.0) + float(entry["usd"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue  # a damaged line costs nothing; the next run still sees the rest
    return totals


def pool_for(spec) -> str | None:
    """The pool a model's spend is booked to. Paid hosts outside the four credit pools go to "other"."""
    if getattr(spec, "credit_pool", None):
        return spec.credit_pool
    if getattr(spec, "offline", False):
        return None
    return spec.pool() or "other"


def total_left(total: float | None, margin: float, runs_dir: str | Path) -> float | None:
    """What's left under the limit on all API spend combined, after the margin. None when there's no limit."""
    if total is None:
        return None
    return total * (1 - margin) - sum(spent(runs_dir).values())


def usable(credits: dict[str, float], margin: float, runs_dir: str | Path) -> dict[str, float]:
    """What each pool still has to spend, after the safety margin."""
    used = spent(runs_dir)
    return {pool: amount * (1 - margin) - used.get(pool, 0.0) for pool, amount in credits.items()}
