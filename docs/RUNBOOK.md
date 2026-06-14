# Polymanager Operating Runbook

The procedure that turns this code into a **disciplined process** instead of a
gamble. Read it before running anything that can touch real funds.

> ⚠️ Automated trading can lose money fast. This bot defaults to **paper**. It
> never places a live order unless you set `BOT_MODE=tiny_live`/`live` **and**
> supply credentials. Not financial advice. You are responsible for complying
> with Polymarket's terms and your jurisdiction — do not evade geofencing.

---

## 0. Golden rules

1. **One rung at a time.** `research → paper → shadow → tiny_live → live`. Never
   skip. Spend real time at each.
2. **The data decides, not your gut.** Advance only when `python -m app.report`
   clears the go/no-go gate for that rung (§5).
3. **Size for survival.** A "guaranteed" trade can still fail. No martingale, no
   revenge trading, no doubling after a loss.
4. **When in doubt, pause.** A tripped kill switch is the system working. Find
   the cause before you reset.

---

## 1. Prerequisites

- **Jurisdiction.** Confirm you may legally trade on Polymarket from where you
  are. Do not use a VPN to bypass access controls.
- **Python 3.11+**, then:
  ```bash
  python -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  pytest -q                      # expect all green before trusting a run
  cp .env.example .env           # edit; NEVER commit it or paste keys into chat
  ```
- **Wallet (only for shadow and beyond).** A funded Polymarket (Polygon/USDC)
  wallet. Set in `.env`:
  - `POLYMARKET_PRIVATE_KEY` — signs orders, stays local, never logged.
  - `POLYMARKET_FUNDER_ADDRESS` + `POLYMARKET_SIGNATURE_TYPE`
    (`0`=EOA, `1`=email/magic proxy, `2`=browser wallet).
- **Anthropic key.** `ANTHROPIC_API_KEY` is **required** for any order-placing
  mode — the risk manager blocks live trading when `REASONER_DISABLED=true`.

---

## 2. The mode ladder

| Mode | Orders? | Creds? | What it's for |
|---|---|---|---|
| `research` | none | no | Scan + score + log hypothetical trades. Sanity-check discovery and signals. |
| `paper` | simulated | no | Conservative fill simulation. Measure strategy edge with zero risk. |
| `shadow` | none | yes | Real account state + real intents, **no execution**. Validate against live books and latency. |
| `tiny_live` | real, tiny | yes | $1–$5 orders, **one strategy**, daily manual review. |
| `live` | real, scaled | yes | Multiple strategies, larger size — only after a positive track record. |
| `paused` | none | — | Kill-switch state. No new entries. |

Set with `BOT_MODE=` in `.env`. It **never defaults to live**.

---

## 3. Running it

```bash
python -m app.main                 # loop using BOT_MODE from .env
python -m app.main --once          # single pass over the watchlist, then exit
python -m app.main --iterations 50 # bounded run
python -m app.report               # performance report from the SQLite log
python -m app.report --json        # machine-readable
```

Everything is logged to `logs/events.jsonl` and `data/polymanager.db`
(tables: `signals`, `claude_reviews`, `orders`, `fills`, `positions`,
`risk_events`, `rejections`, `pnl_snapshots`).

Stop with `Ctrl-C` — it cancels open orders and closes cleanly.

### Wiring alpha

Arbitrage works from books alone. Near-certainty / momentum / cheap-tail need a
fair-value input or they stay dormant. The built-in crypto-threshold provider:

```bash
ENABLE_CRYPTO_ALPHA="true"     # prices "Will BTC be above $X by Y?" from live spot
```

Or set `app.external_provider` to your own `callable(market) -> dict`.

---

## 4. Procedure per rung

### Rung 1 — research (a few sessions)
- `BOT_MODE=research`, `REASONER_DISABLED=true` is fine here.
- Run, then read `logs/events.jsonl`. Confirm: the watchlist is sane, signals
  fire only where you'd expect, rejection reasons make sense.
- **Gate to paper:** no crashes, no nonsense signals, discovery filters behaving.

### Rung 2 — paper (the main proving ground)
- `BOT_MODE=paper`. Enable the strategies/alpha you intend to trade.
- Let it accumulate **≥ 200 closed trades** per strategy you care about.
- Run `python -m app.report` and apply the §5 gate.
- **Gate to shadow:** overall **EV/$ > 0 after fees**, each strategy you'll run
  has **EV/$ ≥ 0**, **no daily-loss breach**, **no kill-switch trips from bugs**,
  fill rate is believable (paper already assumes you're behind the queue).

### Rung 3 — shadow (validate against reality)
- `BOT_MODE=shadow` with real credentials. Real books, real account, **no orders**.
- Compare emitted intents to what actually happened in the book. Watch latency
  and book-staleness rejections.
- Manually place **one** tiny test order and cancel it through your wallet UI to
  confirm credentials + signing work end-to-end.
- **Gate to tiny_live:** intents look right against live books, latency under
  `MAX_API_LATENCY_MS`, staleness rejections rare, credentials verified.

### Rung 4 — tiny_live (real money, minimal)
- `BOT_MODE=tiny_live`. **One strategy.** `BANKROLL_USD` set to real funds.
- Keep position caps tiny (defaults: momentum 0.5%, near-certainty 1%, tail 0.3%).
- **Review every day** with `app.report`. Reconcile `positions` against your
  wallet. Expect the kill switch to do its job.
- **Gate to live:** a positive, stable track record over **hundreds** of real
  closed trades; realized EV within model expectation; drawdown under limit.

### Rung 5 — live (scale deliberately)
- Add strategies one at a time. Apply the scaling rule (§6). All kill switches on.

---

## 5. Reading the report (the go/no-go gate)

`python -m app.report` gives, per strategy and overall:

| Metric | What good looks like |
|---|---|
| **EV/$** (realized PnL ÷ volume) | **> 0 after fees**, and stable across the sample — the single most important number. |
| **win rate** | Consistent with the strategy's design (near-certainty high; cheap-tail low but with big avg win). |
| **fill rate** | Plausible, not 100%. If everything fills, your fill model is too optimistic. |
| **avg win vs avg loss** | For low win-rate strategies, avg win must dwarf avg loss. |
| **realized PnL** | Positive over a meaningful sample (≥ 200 closes), not one lucky trade. |
| **top rejection reasons** | Mostly benign (spread/edge filters). Frequent `stale_order_book`, `claude_invalid_json`, or `*_exposure_cap` means investigate. |
| **claude allow-rate** | Very low → rules too strict or markets too ambiguous; ~100% → review may not be discriminating. |

**Do not advance** on a positive total driven by < 100 trades or a single
outlier. EV per dollar after costs, over a large sample, is the gate.

---

## 6. Scaling and compounding (Section 23)

Bankroll allocation guideline: reserve ≥ 50% idle; momentum ≤ 20%;
near-certainty ≤ 15%; arbitrage ≤ 10%; cheap tails ≤ 5%.

**Every 100 closed trades:**
- if realized EV > 0.70 × expected EV **and** max drawdown < limit **and** error
  rate < limit → increase size **10–20%**.
- else → keep size or **reduce**.

Never double after losses. No martingale. No revenge trading.

---

## 7. Incident response

A tripped kill switch ends the run loop after cancelling open orders. It is
tripped automatically by: daily-loss limit, API latency over threshold, stale
data, invalid/failed Claude review, high order-rejection rate, position
mismatch, wallet mismatch, or any unhandled exception in the loop.

When it trips:
1. **Do not blindly restart.** Read the last `risk_events` / log lines for the
   reason.
2. Reconcile `positions` (DB) against your actual wallet holdings.
3. Fix the root cause (bad market, network, credential, bug).
4. Flatten or accept open positions deliberately.
5. Only then restart the process (a fresh run starts with the kill switch armed
   but not tripped).

| Symptom | Likely cause | Action |
|---|---|---|
| `daily_loss_limit` | Strategy bleeding today | Stop for the day; review per-strategy EV. |
| `stale_order_book` frequent | Network / feed lag | Check connectivity; widen `MAX_ORDER_BOOK_AGE_SECONDS` only if justified. |
| `claude_invalid_json` / `reasoner_unavailable` | Anthropic key/rate/network | Fix the key or back off; trading stays blocked meanwhile (by design). |
| `position_mismatch` | DB vs exchange drift | Halt, reconcile manually before anything else. |
| `buy_price_chasing` rejections | Signals lagging fast books | Expected guard; review momentum thresholds. |

---

## 8. Pre-flight checklist (before any live rung)

- [ ] `pytest -q` green on the exact commit you're running.
- [ ] `.env` has real `BANKROLL_USD`; risk caps reviewed.
- [ ] `REASONER_DISABLED=false` and `ANTHROPIC_API_KEY` set.
- [ ] Credentials verified with a manual test order + cancel.
- [ ] `app.report` clears the §5 gate for the rung you're entering.
- [ ] You know how to `Ctrl-C` and how to flatten positions in your wallet.
- [ ] You accept that you can lose the funds at risk.

---

*One trade does not carry the result. PnL comes from repeating small edges until
they compound — while avoiding ruin.*
