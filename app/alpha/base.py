"""External signal provider interface.

A provider maps a :class:`Market` to the operator-alpha dict consumed by the
strategies (keys like ``fair_price:<token_id>``, ``true_prob:<token_id>``,
``catalyst:<token_id>``). It is the injection point for *real* edge — the
strategies stay dormant without it.

The main loop calls ``app.external_provider(market)``; both the ABC and the
composite are callable for that reason.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.logger import get_logger
from app.models import Market

log = get_logger("alpha")


class ExternalSignalProvider(ABC):
    @abstractmethod
    def signals(self, market: Market) -> dict:
        ...

    def __call__(self, market: Market) -> dict:
        return self.signals(market)


class CompositeProvider(ExternalSignalProvider):
    """Merge several providers; one failing provider never blocks the others."""

    def __init__(self, providers: list[ExternalSignalProvider]) -> None:
        self.providers = providers

    def signals(self, market: Market) -> dict:
        out: dict = {}
        for p in self.providers:
            try:
                out.update(p.signals(market) or {})
            except Exception:
                log.exception("provider %s failed on %s",
                              type(p).__name__, market.id)
        return out
