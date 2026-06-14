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

## Safety invariants

- `BOT_MODE` defaults to `paper`; it can never *default* to live.
- The read path cannot place orders (separate module from signing).
- Private keys are never logged (redaction filter) and never sent to Claude.
- The risk manager can veto both the signal engine and Claude.
- Kill switches halt new entries and cancel orders on any anomaly.

See `docs/` and module docstrings for details.
