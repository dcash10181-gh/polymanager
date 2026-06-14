"""Claude reasoning layer — resolution-ambiguity and risk reviewer.

Claude is a *reasoning and veto* layer, never an execution authority. It reads a
compact, structured packet (market rules + the proposed signal) and returns
strict JSON: allow/deny, risk level, ambiguity flag, risks, a confidence
adjustment, and a short comment. It never sees private keys, wallet secrets, or
signing payloads.

If the model returns unparseable output, refuses, errors, or times out, the
review is treated as **invalid** and the trade is blocked downstream.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.logger import get_logger
from app.models import ClaudeReview, Market, OrderBook, Position, RiskLevel, Signal

log = get_logger("reasoner")

# Strict JSON schema the model must return (structured outputs).
_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "allow_trade": {"type": "boolean"},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "resolution_ambiguity": {"type": "boolean"},
        "main_risks": {"type": "array", "items": {"type": "string"}},
        "confidence_adjustment": {"type": "number"},
        "comment": {"type": "string"},
    },
    "required": [
        "allow_trade", "risk_level", "resolution_ambiguity",
        "main_risks", "confidence_adjustment", "comment",
    ],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are reviewing a proposed Polymarket trade for risk, ambiguity, and "
    "resolution issues. You do not place trades. You do not control private "
    "keys. You only return structured JSON.\n\n"
    "Review the market rules carefully. Reject trades (allow_trade=false) when "
    "the resolution criteria are ambiguous, the resolution source is unclear, "
    "the data looks stale, the outcome depends on subjective interpretation or "
    "announcement timing, or the risk is otherwise excessive. Set "
    "resolution_ambiguity=true whenever the rules could plausibly resolve more "
    "than one way. confidence_adjustment is a number in [-1, 1] that nudges the "
    "signal's confidence (negative = less confident)."
)


class _ReviewResponse(BaseModel):
    allow_trade: bool
    risk_level: RiskLevel
    resolution_ambiguity: bool
    main_risks: list[str] = Field(default_factory=list)
    confidence_adjustment: float = 0.0
    comment: str = ""


def build_packet(market: Market, signal: Signal, book: OrderBook | None,
                 position: Position | None) -> dict:
    """Compact, secrets-free packet describing the market and proposed trade."""
    return {
        "market": {
            "title": market.question,
            "description": (market.description or "")[:1500],
            "resolution_source": market.resolution_source[:300],
            "end_date": market.end_date,
            "liquidity_usd": round(market.liquidity_usd, 2),
            "volume_24h_usd": round(market.volume_24h_usd, 2),
            "outcomes": [t.outcome for t in market.tokens],
        },
        "book": None if book is None else {
            "best_bid": book.best_bid,
            "best_ask": book.best_ask,
            "spread": book.spread,
            "age_seconds": round(book.age_seconds(), 2),
        },
        "signal": {
            "strategy": signal.strategy.value,
            "side": signal.side.value,
            "estimated_fair_price": signal.estimated_fair_price,
            "edge": signal.edge,
            "confidence": signal.confidence,
            "suggested_size_usd": signal.suggested_size_usd,
            "rationale": signal.rationale,
        },
        "current_position_shares": None if position is None else position.shares,
    }


class ClaudeReasoner:
    def __init__(self, settings: Settings, client=None) -> None:
        self.s = settings
        self._client = client  # injectable for tests
        self._init_error: str | None = None
        if client is None and not settings.reasoner_disabled:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            except Exception as e:  # missing key / package — fall back to block
                self._init_error = str(e)
                log.warning("reasoner unavailable: %s", e)

    def review(self, market: Market, signal: Signal,
               book: OrderBook | None = None,
               position: Position | None = None) -> ClaudeReview:
        # Offline mode: explicit opt-out auto-allows (paper/research only; the
        # risk manager blocks this in any order-placing mode).
        if self.s.reasoner_disabled:
            return ClaudeReview(
                allow_trade=True, risk_level=RiskLevel.low,
                resolution_ambiguity=False, valid_json=True,
                comment="reasoner disabled (offline auto-allow)",
            )
        if self._client is None:
            return self._blocked(f"reasoner not initialised: {self._init_error}")

        packet = build_packet(market, signal, book, position)
        try:
            resp = self._client.messages.create(
                model=self.s.reasoner_model,
                max_tokens=1024,
                system=_SYSTEM,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": _REVIEW_SCHEMA},
                },
                messages=[{
                    "role": "user",
                    "content": json.dumps(packet, default=str),
                }],
            )
        except Exception as e:  # network / auth / rate-limit / bad request
            return self._blocked(f"reasoner API error: {type(e).__name__}: {e}")

        if getattr(resp, "stop_reason", None) == "refusal":
            return self._blocked("reasoner refused the request")

        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        try:
            parsed = _ReviewResponse.model_validate_json(text)
        except (ValidationError, ValueError) as e:
            return self._blocked(f"reasoner returned invalid JSON: {e}")

        return ClaudeReview(
            allow_trade=parsed.allow_trade,
            risk_level=parsed.risk_level,
            resolution_ambiguity=parsed.resolution_ambiguity,
            main_risks=parsed.main_risks,
            confidence_adjustment=max(-1.0, min(1.0, parsed.confidence_adjustment)),
            comment=parsed.comment[:500],
            valid_json=True,
        )

    @staticmethod
    def _blocked(reason: str) -> ClaudeReview:
        return ClaudeReview(
            allow_trade=False, risk_level=RiskLevel.high,
            resolution_ambiguity=True, valid_json=False,
            main_risks=["reasoner_unavailable"], comment=reason[:500],
        )
