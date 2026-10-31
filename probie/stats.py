"""Confidence intervals for proportions."""

from __future__ import annotations

import math

Z_95 = 1.959963984540054


def wilson(successes: int, trials: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for successes out of trials (95% by default).

    It stays sensible at 0 and at trials, unlike the normal approximation:
    0 out of 80 gives an upper bound of about 4.6%, not zero.
    """
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("need 0 <= successes <= trials")
    if trials == 0:
        return 0.0, 1.0
    p = successes / trials
    z2 = z * z
    denominator = 1 + z2 / trials
    centre = (p + z2 / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials)) / denominator
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == trials else min(1.0, centre + half)
    return low, high
