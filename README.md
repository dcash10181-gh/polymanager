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

**One-command setup** (creates the virtualenv, installs deps, runs the tests):

```bash
make setup
```

Then, step by step:

1. **See it run with no funds, keys, or network** — the full pipeline on
   synthetic data, ending in a performance report:
   ```bash
   make demo
   ```
2. **Configure** (only needed to trade *live* Polymarket data/orders):
   ```bash
   cp .env.example .env       # edit; NEVER commit .env or paste keys into any chat
   ```
   Leave `BOT_MODE=paper` (the default) to simulate against live books with no
   funds. Set `REASONER_DISABLED=true` to skip the Anthropic key too.
3. **Run it** (paper mode unless you change `BOT_MODE`):
   ```bash
   make run            # = python -m app.main
   ```
4. **Review performance** by strategy (win rate, EV per dollar, fill rate):
   ```bash
   make report         # = python -m app.report
   ```

`make help` lists every target. No `make`? The raw equivalents:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scripts.demo_paper   # offline demo, no funds
python -m app.main             # paper mode by default
python -m app.report           # performance report
```

Paper mode needs **no credentials**. Real funds are only ever at risk in
`tiny_live`/`live`, which require an explicit mode change **and** a wallet key.
Before going anywhere near live, follow **[`docs/RUNBOOK.md`](docs/RUNBOOK.md)**.

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
