# SmartMoney Sentinel — Architecture & Operating Manual

A complete technical reference for the system. For a high-level overview, start with the [README](../README.md).

> ⚠️ Not financial advice. This document describes a personal decision-support tool. All outputs are research inputs for a human decision-maker.

---

## 1. What Sentinel is — and is not

Sentinel is a locally-run Python system that every morning evaluates a watchlist of candidate stocks and a portfolio of current holdings against a fundamentals-based valuation engine, then delivers a disciplined brief to Telegram and an Obsidian vault. Over time it scores its own past recommendations against two market benchmarks and produces post-mortems explaining what happened and why.

**It is:** a decision-support and self-measurement system. It finds candidates, flags risks, triangulates valuation across three independent methods, sizes suggestions, tracks the live portfolio, and grades its own track record honestly.

**It is not:** an auto-trader. It never places orders, never moves money, and never auto-changes its own parameters. Every output is a flag or a suggestion for a human.

This restraint is deliberate. On a small capital base with few decisions, autonomous self-modification overfits to noise. The system measures and recommends; the human decides and executes.

---

## 2. The three-loop architecture

A genuinely intelligent decision system has three loops: decide, measure, diagnose. Sentinel implements all three, with the final adaptation step intentionally human-driven.

### Loop 1 — Decide (daily)
Fetch data, evaluate each name, guard against falling knives, rank the buys, size them, flag holding health, escalate sells on broken fundamentals, narrate, deliver. Every decision is logged to the journal database.

### Loop 2 — Measure (weekly)
Re-price every past signal at 2-week, 1-month, 3-month and 6-month horizons; compute true total return and alpha versus two benchmarks (QQQ and RSP); produce a calibration report across two tracks — watchlist signal quality and portfolio health.

### Loop 3 — Diagnose (weekly)
For each closed outcome, ask Claude (with web search) why it moved, labelling every causal claim as a hypothesis with sources and forcing a null-hypothesis check so the system cannot manufacture confident hindsight.

### Loop 3.5 — Adapt (manual, by design)
The human reads the calibration and post-mortems and decides whether to change configuration. Automated tuning becomes appropriate only once there are ≈30+ closed outcomes per bucket.

---

## 3. The files

| File | Role |
|------|------|
| `sentinel.py` | Loop 1 orchestrator. Loads config, drives fetch → evaluate, builds the brief, narrates via Claude, prepends the live portfolio snapshot, delivers to Telegram + Obsidian. Writes a heartbeat and alerts on crash. |
| `engine.py` | Valuation engine. Hard floors, quality score, reverse-DCF implied growth, achievable growth, multi-method consensus, ranking, sizing, position health, verdict overrides. |
| `yahoo.py` | Data layer. Pulls fundamentals from yfinance into a `Fundamentals` object, including post-earnings context and trend context. |
| `valuation_consensus.py` | Three independent valuation votes (DCF, PEG on revenue growth, sector-relative EV/EBITDA). |
| `trend_guard.py` | Value-trap / momentum guard from 50-day and 200-day moving averages. |
| `sell_review.py` | Fundamentals-only sell escalation: persistence-OR-severity. Flag, never order. |
| `track_outcomes.py` | Loop 2 outcome tracker, two tracks, two benchmarks, survivorship disclosure. |
| `post_mortem.py` | Loop 3 Claude + web-search hedged why-engine. |
| `portfolio.py` | Live USD portfolio: value, weight, P&L per position and total; records buys/sells and recomputes average cost; supplies the daily snapshot. |
| `heartbeat_check.py` | Watchdog: alerts if a scheduled run never happened. |
| `add_holding.py` | Utility to add/remove holdings without hand-editing JSON. |
| `holdings.json` | Your holdings with shares + entry_price + moat (gitignored). |
| `watchlist.json` | Candidate stocks with buy triggers (gitignored). |
| `journal.db` | SQLite memory: decisions, outcomes, health_log, postmortem_log, heartbeat (gitignored). |

---

## 4. Loop 1 — the daily decision pipeline

### Step 1 — Data acquisition
For each ticker, `yahoo.py` returns a `Fundamentals` object: price, 52-week high/low, 50d/200d moving averages, ROIC (with an ROE proxy fallback, flagged), gross and FCF margins, revenue growth, FCF per share, net-debt-to-EBITDA, forward P/E, enterprise value, EBITDA, analyst growth, historical FCF CAGR, market cap, moat, days to next earnings, days since last earnings, last EPS surprise, post-earnings price drift, 90-day change, distance above 52-week low, and an ETF flag.

### Step 2 — Hard floors
A stock must clear minimum quality or it is rejected: ROIC, FCF margin, and revenue growth above floors; net-debt-to-EBITDA below ceiling; a moat stated. ETFs skip this.

### Step 3 — Quality score
Survivors get a 0–100 score combining ROIC, margins, growth, and balance-sheet health.

### Step 4 — Reverse-DCF (implied growth)
Given price and FCF per share, binary-search for the growth rate the market implies at the hurdle. This is "what the price assumes."

### Step 5 — Achievable growth
A blend of analyst growth and historical FCF CAGR, capped by a market-cap fade and bounded by revenue growth. This is "what is realistically deliverable."

### Step 6 — Edge
`edge = achievable − implied`. Positive means the market implies less growth than is achievable — potentially cheap.

### Step 7 — Multi-method consensus
Three independent methods each vote CHEAP / NEUTRAL / EXPENSIVE:
- **M1 — DCF:** edge at/above the buy gate is CHEAP.
- **M2 — PEG on reported revenue growth** (not achievable growth — de-correlates from M1 so one bad growth estimate can't flip both).
- **M3 — EV/EBITDA vs the stock's sector median** (cheap below 0.8×, expensive above 1.25×), which stops the method rewarding structurally-cheap value traps.

Rule: 2-of-3 cheap = BUY; 3-of-3 = BUY + CONVICTION. A single cheap method is noise.

### Step 8 — Discipline overrides
BUY downgraded to WAIT if within 5% of all-time high (ATH discipline) or earnings within 5 days (wait for the print).

### Step 9 — Trend guard
Each BUY is classified from its moving averages (VALUE-TRAP-RISK / WEAK-TREND / BASING / UPTREND). The caveat attaches; the signal is not suppressed.

### Step 10 — Post-earnings guard
If earnings landed in the last 7 days (using the real announcement date from `earnings_dates`, not the fiscal quarter-end):
- **beat but price down ≥5%** → WATCH, flag `BEAT-BUT-SOLD` ("market sold the guidance — verify before buying")
- **outright EPS miss** → WATCH, flag `MISS`
- **reaction not negative** → BUY stands, flag `FRESH-OK`, with a note that earnings just passed

The system never claims to read guidance (that is text it deliberately does not parse). It infers trouble from the price reaction and forces a human read.

### Step 11 — Trend context
Every buy carries `chg_90d` and `above_52w_low`. A deep 90-day decline near the 52-week low raises a ⚠ falling-knife note — visible even when no rule fires, so a slow bleed is never hidden behind a bare BUY.

### Step 12 — Holding health & fast-warning
Floor and concentration breaches flag THESIS-WATCH. A FAST-WARNING fires only when a holding is BOTH fundamentally flagged AND ≥15% below its 200d MA — an early heads-up months before the quarterly sell-review clock, without violating "never sell on price" (price alone never triggers it).

### Step 13 — Sell-review escalation
Holdings escalate to SELL-REVIEW on persistence-OR-severity: the same floor breached two consecutive quarters, or one severe break (net-debt-to-EBITDA above 5.0 or ROIC at/below 1%). Fundamentals-only, always a flag.

### Step 14 — Rank, size, narrate, deliver
Buys ranked by final score, sized in USD against the monthly budget. The brief — opening with the live portfolio snapshot, then ranked buys with consensus votes, conviction markers, trend lines, watchlist proximity, holding health, fast-warning, sell-review, and the learning readback — is narrated by Claude and pushed to Obsidian + Telegram. A heartbeat timestamp is written on success.

---

## 5. Live portfolio tracking

`holdings.json` stores exact shares and average cost per position. `portfolio.py` fetches live prices and computes current value, real weight, and P&L per position and total, in USD. The snapshot is embedded at the top of every daily brief.

Recording trades (no hand-editing JSON):

```bash
python3 portfolio.py                       # view live snapshot
python3 portfolio.py buy  NVDA 2 175.50    # add 2 shares @ $175.50 (recomputes avg cost)
python3 portfolio.py add  AMD  3 230 "AI"  # brand-new position
python3 portfolio.py sell CVS 1            # sell 1 share (avg cost unchanged)
```

A buy averages cost basis correctly: `(old_shares × old_avg + new_shares × new_price) / total_shares`. A sell reduces shares and leaves average cost untouched.

---

## 6. Loop 2 — honest outcome measurement

The learning apparatus rests on this loop telling the truth.

- **Return consistency.** Both the stock leg and benchmark leg are computed from the same dividend- and split-adjusted series, so dividend-payers don't show a false negative return.
- **Survivorship disclosure.** Unresolved outcomes (delisting, rename, missing history) are counted and disclosed in a `DATA INTEGRITY` header, with an upward-bias warning.
- **Two benchmarks.** Alpha is measured against QQQ *and* RSP (equal-weight S&P 500). QQQ overlaps mega-cap tech holdings, so "beats QQQ" alone can be a beta tilt. RSP strips that. If a signal beats QQQ but not RSP, the edge was being long mega-cap tech, not selection skill.
- **Two tracks.** Watchlist track (do BUY signals add alpha? do they beat the skipped WAIT/FAIR names? was the edge gate justified?) and portfolio track (do holdings beat the index, worst-alpha first — with an explicit note that negative alpha is a review flag, never a sell).
- **Sample-size gating.** Anything below n=10 is tagged "directional only."

---

## 7. Loop 3 — diagnosis with discipline

For each closed signal, Claude is given what was recommended and what happened, then asked why — with web search. The prompt forbids asserting causation, requires sources, and forces a null-hypothesis case. The human reads for patterns across many; the system never acts on a single verdict and never auto-changes.

---

## 8. Reliability

- **Crash alert.** The orchestrator is wrapped so any exception sends a Telegram message with the traceback before the process dies, then re-raises.
- **Watchdog.** A separate process reads the last successful-run timestamp and sends a silent alert if it is older than 26 hours — catching the one failure an in-process alert cannot: a run that never started.

The heartbeat is written only after successful delivery, so it means "ran and delivered," not merely "started."

---

## 9. Data model (`journal.db`)

| Table | Written by | Purpose |
|-------|-----------|---------|
| `decisions` | daily | Every verdict: date, ticker, action, price, growth figures, edge, quality, score, confidence, note. |
| `outcomes` | weekly | Realized results per signal/horizon: adjusted prices, total return, both benchmark returns and alphas, predicted edge, quality, confidence. |
| `health_log` | daily | One row per ticker per quarter recording breached floors. Drives sell escalation. |
| `postmortem_log` | weekly | Tracks which outcomes already have report cards (idempotency). |
| `heartbeat` | daily | Last successful-run timestamp per job. Read by the watchdog. |

---

## 10. Scheduling

On macOS via `launchd`:

| Job | When | Runs |
|-----|------|------|
| daily brief | 06:00 daily | `sentinel.py` |
| calibration | Monday 07:00 | `track_outcomes.py` |
| post-mortem | Monday 07:30 | `post_mortem.py` |
| watchdog | 09:00 daily | `heartbeat_check.py` |

On Linux, the equivalent is four `cron` or `systemd` timer entries.

---

## 11. The stack

- **Language:** Python 3.12.
- **Data:** yfinance (info dict, financial statements, `earnings_dates`, adjusted price history).
- **Storage:** SQLite.
- **AI:** Anthropic API — Claude (Haiku) for daily narration and post-mortem diagnosis (with web search).
- **Delivery:** Telegram Bot API + Obsidian vault (markdown).
- **Scheduling:** `launchd` (macOS) / `cron` (Linux).
- **Secrets:** a flat `.env` read at runtime; no credentials in source or scheduler files.

---

## 12. Known weaknesses & roadmap

1. **Single data source.** A data-sanity layer (range/variance checks) and ideally a second source are the top open item.
2. **No regime / breadth awareness.**
3. **No run-over-run diff.**
4. **Guidance not parsed** (deliberate — facts surfaced, judgment left to human).
5. **Young track record** — meaningful statistics need months of closed outcomes.
6. **Adaptation is human** — correct at this sample size, revisited at n≈30+ per bucket.

---

## 13. One-line summary

A lean, self-measuring investment-decision system that tracks a live portfolio, triangulates valuation across three independent methods, guards against falling knives and bad earnings reactions, escalates sells only on broken fundamentals, scores every past decision against two benchmarks with sample sizes disclosed, diagnoses its own hits and misses, and alerts the moment it itself breaks — all as flags for a human, never as automated trades.
