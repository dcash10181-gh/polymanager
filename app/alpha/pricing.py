"""Closed-form probability that a price exceeds a threshold at expiry.

Models the underlying as geometric Brownian motion (lognormal terminal price)
with zero drift — a neutral, martingale-style assumption appropriate for a
prediction market. No SciPy dependency: the normal CDF uses ``math.erf``.
"""

from __future__ import annotations

import math


def normal_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(spot: float, strike: float, sigma_annual: float,
               tau_years: float, drift: float = 0.0) -> float | None:
    """P(S_T > strike) under lognormal dynamics.

    Returns None on invalid inputs. At/after expiry collapses to a step
    function (1 if already above, else 0).
    """
    if spot <= 0 or strike <= 0 or sigma_annual <= 0:
        return None
    if tau_years <= 0:
        return 1.0 if spot > strike else 0.0
    vol = sigma_annual * math.sqrt(tau_years)
    if vol <= 0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) + (drift - 0.5 * sigma_annual ** 2) * tau_years) / vol
    return normal_cdf(d2)
