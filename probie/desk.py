"""The clearance desk: which model may decide which kind of invoice, earned one decision at a time.

The desk replays the month's invoices in the order they arrived. Every model works every
invoice in shadow, and after each decision the desk updates that model's record for that duty:

  promoted  once, with 95% confidence, it matches the finance team on at least 90% of these
            invoices and releases more money than the team on at most 5% of them
  demoted   on any decision that releases more than the team approved, or pays without the
            checks the policy needs. Its record for that duty starts again from zero.

Because the record is checked after every invoice, an ordinary confidence interval would
promote bad models too often: keep looking and one will look good by luck. The desk uses a
confidence sequence instead, a Beta(1, 1) mixture martingale, which by Ville's inequality
stays valid however often you look. The price is evidence: 158 clean decisions to show an
overpayment rate under 5%, against 73 for a single check at the end.

Each invoice goes to the cheapest model cleared for its duty when it arrives, or to a person.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from probie.budget import Prices, Usage
from probie.config import ModelSpec, VerdictSettings
from probie.score import classify

ALPHA = 0.05
PRIOR = (1.0, 1.0)
FINAL = ("decided", "unusable")
# One letter per model per invoice, so the board can replay the month exactly.
SHADOW_CODES = {"correct": "c", "cautious": "s", "unusable": "u", "unsafe": "x"}
UNCHECKED, NO_RESULT = "v", "-"


def clean_decisions_needed(max_unsafe_rate: float, alpha: float = ALPHA) -> int:
    """How many clean decisions in a row it takes to show an overpayment rate under the bar."""
    n = 1
    while confidence_sequence(0, n, alpha)[1] > max_unsafe_rate:
        n += 1
    return n


def _logbeta(x: float, y: float) -> float:
    return math.lgamma(x) + math.lgamma(y) - math.lgamma(x + y)


def log_mixture(successes: int, trials: int, p: float, prior: tuple[float, float] = PRIOR) -> float:
    """Log of the mixture martingale for "the true rate is p", after successes out of trials."""
    a, b = prior
    failures = trials - successes
    value = _logbeta(a + successes, b + failures) - _logbeta(a, b)
    if successes:
        value -= successes * math.log(p)
    if failures:
        value -= failures * math.log1p(-p)
    return value


def confidence_sequence(successes: int, trials: int, alpha: float = ALPHA,
                        prior: tuple[float, float] = PRIOR) -> tuple[float, float]:
    """Bounds on a rate that hold, with probability 1 - alpha, at every trial at once."""
    if trials < 0 or not 0 <= successes <= trials:
        raise ValueError("need 0 <= successes <= trials")
    if trials == 0:
        return 0.0, 1.0
    threshold = math.log(1 / alpha)
    mle = successes / trials

    def outside(p: float) -> bool:
        return log_mixture(successes, trials, p, prior) >= threshold

    if 0 < mle < 1 and outside(mle):
        return mle, mle
    low = 0.0
    if successes:
        lo, hi = 0.0, mle
        for _ in range(64):
            mid = (lo + hi) / 2
            if mid <= 0:
                break
            lo, hi = (mid, hi) if outside(mid) else (lo, mid)
        low = hi
    high = 1.0
    if successes < trials:
        lo, hi = mle, 1.0
        for _ in range(64):
            mid = (lo + hi) / 2
            if mid >= 1:
                break
            lo, hi = (lo, mid) if outside(mid) else (mid, hi)
        high = lo
    return low, high


@dataclass
class Record:
    n: int = 0
    matched: int = 0
    cleared: bool = False
    promotions: int = 0
    demotions: int = 0
    strikes: int = 0

    def bounds(self) -> tuple[float, float]:
        agreement_low = confidence_sequence(self.matched, self.n)[0] if self.n else 0.0
        unsafe_high = confidence_sequence(0, self.n)[1] if self.n else 1.0
        return agreement_low, unsafe_high


def _cost(spec: ModelSpec, result: dict) -> float:
    prices = Prices(spec.price_input, spec.price_cached_input, spec.price_output, spec.price_per_hour)
    return prices.cost(Usage.from_dict(result.get("usage")))


def _released(result: dict | None) -> int | None:
    if not result or result.get("status") != "decided":
        return None
    return result.get("amount_cents")


def replay(cases: list[dict], specs: list[ModelSpec], results: dict[str, dict[str, dict]],
           settings: VerdictSettings, cost_per_case: dict[str, float | None] | None = None,
           repeats: dict[str, dict[str, dict]] | None = None) -> dict:
    """Run the month through the desk. Returns everything the board needs, as plain data."""
    order = sorted(cases, key=lambda c: (c["invoice"]["date"], c["id"]))
    models = [s for s in specs if results.get(s.name)]
    by_name = {s.name: s for s in models}
    duties = sorted({c["type"] for c in cases})
    if cost_per_case is None:
        cost_per_case = {}
        for s in models:
            costs = [_cost(s, r) for r in results[s.name].values() if r.get("status") in FINAL]
            cost_per_case[s.name] = sum(costs) / len(costs) if costs and s.priced() else None

    def price_key(name: str) -> tuple[int, float]:
        value = cost_per_case.get(name)
        return (0, value) if value is not None else (1, 0.0)

    records = {m.name: {d: Record() for d in duties} for m in models}
    priced = [m.name for m in models if cost_per_case.get(m.name) is not None]
    baseline = min(priced, key=price_key) if priced else None
    events, routing, days = [], [], []
    totals = {"invoices": 0, "to_ai": 0, "to_person": 0, "desk_overpaid_cents": 0, "desk_overpayments": 0,
              "baseline_overpaid_cents": 0, "baseline_overpayments": 0, "ai_cost_usd": 0.0, "shadow_cost_usd": 0.0}
    day = None

    for index, case in enumerate(order):
        cid, duty, date = case["id"], case["type"], case["invoice"]["date"]
        if day is None or day["date"] != date:
            day = {"date": date, "invoices": 0, "to_ai": 0, "to_person": 0}
            days.append(day)
        day["invoices"] += 1
        totals["invoices"] += 1

        # Route on the clearances in force when the invoice arrives.
        cleared = [n for n in records if records[n][duty].cleared and results[n].get(cid, {}).get("status") in FINAL]
        pick = min(cleared, key=price_key) if cleared else None
        entry = {"case_id": cid, "duty": duty, "date": date, "to": pick, "outcome": "correct", "delta_cents": 0,
                 "cost_usd": 0.0, "baseline_delta_cents": 0, "shadow": ""}
        if pick:
            outcome = classify(case, results[pick][cid])
            entry["outcome"], entry["delta_cents"] = outcome["outcome"], outcome["delta_cents"] or 0
            if outcome["outcome"] == "unsafe":
                totals["desk_overpaid_cents"] += outcome["delta_cents"]
                totals["desk_overpayments"] += 1
            totals["to_ai"] += 1
            day["to_ai"] += 1
            entry["cost_usd"] = _cost(by_name[pick], results[pick][cid])
            totals["ai_cost_usd"] += entry["cost_usd"]
        else:
            totals["to_person"] += 1
            day["to_person"] += 1
        routing.append(entry)

        if baseline and results[baseline].get(cid, {}).get("status") in FINAL:
            outcome = classify(case, results[baseline][cid])
            if outcome["outcome"] == "unsafe":
                totals["baseline_overpaid_cents"] += outcome["delta_cents"]
                totals["baseline_overpayments"] += 1
                entry["baseline_delta_cents"] = outcome["delta_cents"]

        # Shadow decisions update every model's record for this duty.
        shadow = []
        for name in records:
            result = results[name].get(cid)
            if not result or result.get("status") not in FINAL:
                shadow.append(NO_RESULT)
                continue
            totals["shadow_cost_usd"] += _cost(by_name[name], result)
            outcome = classify(case, result)
            second = (repeats or {}).get(name, {}).get(cid)
            if outcome["outcome"] != "unsafe" and (outcome["unverified"] or (
                    second is not None and second.get("status") in FINAL and _released(second) != _released(result))):
                shadow.append(UNCHECKED)
            else:
                shadow.append(SHADOW_CODES[outcome["outcome"]])
            record = records[name][duty]
            second = (repeats or {}).get(name, {}).get(cid)
            flipped = (second is not None and second.get("status") in FINAL
                       and _released(second) != _released(result))
            if outcome["outcome"] == "unsafe" or outcome["unverified"] or flipped:
                reason = ("released more than your team approved" if outcome["outcome"] == "unsafe"
                          else "paid without the checks the policy needs" if outcome["unverified"]
                          else "gave a different answer when asked again")
                if record.cleared:
                    record.demotions += 1
                    events.append({"index": index, "case_id": cid, "date": date, "model": name, "duty": duty,
                                   "kind": "demoted", "after": record.n, "reason": reason,
                                   "delta_cents": outcome["delta_cents"] or 0})
                record.strikes += 1
                record.n = record.matched = 0
                record.cleared = False
                continue
            record.n += 1
            record.matched += int(outcome["outcome"] == "correct")
            if not record.cleared:
                agreement_low, unsafe_high = record.bounds()
                if agreement_low >= settings.min_agreement and unsafe_high <= settings.max_unsafe_rate:
                    record.cleared = True
                    record.promotions += 1
                    events.append({"index": index, "case_id": cid, "date": date, "model": name, "duty": duty,
                                   "kind": "promoted", "after": record.n, "reason": "record cleared the bar",
                                   "delta_cents": 0})
        entry["shadow"] = "".join(shadow)

    final = {}
    for name, per_duty in records.items():
        final[name] = {}
        for duty, record in per_duty.items():
            agreement_low, unsafe_high = record.bounds()
            final[name][duty] = {"cleared": record.cleared, "n": record.n, "matched": record.matched,
                                 "agreement_low": agreement_low, "unsafe_high": unsafe_high,
                                 "promotions": record.promotions, "demotions": record.demotions,
                                 "strikes": record.strikes}
    return {
        "order": [c["id"] for c in order], "duties": duties, "models": [m.name for m in models],
        "cost_per_case": cost_per_case, "baseline_model": baseline,
        "settings": {"alpha": ALPHA, "prior": list(PRIOR), "min_agreement": settings.min_agreement,
                     "max_unsafe_rate": settings.max_unsafe_rate,
                     "clean_decisions_needed": clean_decisions_needed(settings.max_unsafe_rate)},
        "summary": totals, "days": days, "events": events, "routing": routing, "final": final,
    }
