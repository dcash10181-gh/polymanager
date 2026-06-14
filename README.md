# Polymanager

A **safety-first** automated trading agent for [Polymarket](https://polymarket.com).

> ⚠️ **RISK WARNING.** Automated trading can lose money quickly. This software
> defaults to **paper mode** and never places live orders unless you explicitly
> set `BOT_MODE=tiny_live`/`live` *and* provide credentials. This is **not
> financial advice**. You are responsible for complying with Polymarket's terms,
> API rules, and the laws of your jurisdiction (Polymarket geoblocks some
> regions — do not attempt to evade access controls).

## Architecture

```
Gamma + CLOB + Data APIs
  -> Market Discovery + Real-Time Market Data
  -> Signal Engine (near-certainty / momentum / cheap-tail / arbitrage)
  -> Claude Risk/Resolution Review  (reasoning layer, NOT execution authority)
  -> Deterministic Risk Manager     (hard limits, kill switches — final authority)
  -> Execution Engine               (paper broker, or gated live limit orders)
  -> Portfolio + SQLite Logs + PnL
```

Claude reviews resolution ambiguity and risk; it **never** controls keys or
places trades. Deterministic code owns sizing, exposure limits, and execution.

## Operating modes (safest → most permissive)

`research` → `paper` → `shadow` → `tiny_live` → `live` (`paused` = kill switch)

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit; NEVER commit .env or paste keys into any chat
pytest                       # run the test suite
python -m app.main           # runs in paper mode by default
```

Paper mode needs **no credentials**. Set `REASONER_DISABLED=true` to run fully
offline without an Anthropic key.

## Alpha sources (where the edge comes from)

Arbitrage derives real edge from order books alone. The other three strategies
(near-certainty, momentum-lag, cheap-tail) consume an **external alpha input**
and stay dormant without it — the bot never fabricates edge.

A provider is any `callable(market) -> dict` returning keys like
`fair_price:<token_id>`, `true_prob:<token_id>`, `catalyst:<token_id>`. Set
`app.external_provider` directly, or enable a built-in one via config.

**Built-in: crypto-threshold provider.** Prices markets like *"Will Bitcoin be
above $100k by Dec 31?"* from live spot (Coinbase) with a lognormal model — as
spot moves, fair value updates, which is the board-behind-the-move edge.

```bash
ENABLE_CRYPTO_ALPHA="true"   # in .env
```

Write your own by subclassing `app.alpha.base.ExternalSignalProvider` and adding
it to the `CompositeProvider`. Per-asset volatility is configurable; "reach/hit"
markets are priced conservatively as terminal (not barrier) options.

## Safety invariants

- `BOT_MODE` defaults to `paper`; it can never *default* to live.
- The read path cannot place orders (separate module from signing).
- Private keys are never logged (redaction filter) and never sent to Claude.
- The risk manager can veto both the signal engine and Claude.
- Kill switches halt new entries and cancel orders on any anomaly.

See **[`docs/RUNBOOK.md`](docs/RUNBOOK.md)** for the full operating procedure
(the `paper → shadow → tiny_live → live` ladder and go/no-go gates), and module
docstrings for implementation details.
