# SmartMoney Sentinel

**A locally-run, self-measuring investment decision-support system for a concentrated, long-horizon portfolio.**

![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-production-success)

Sentinel runs on your own machine, evaluates a watchlist of candidate stocks and your current holdings against a fundamentals-based valuation engine every morning, and delivers a disciplined brief to Telegram and an Obsidian vault. Over time it grades its own past recommendations against two market benchmarks and writes post-mortems on what happened and why.

It is **not** an auto-trader. It never places orders, never moves money, and never changes its own parameters. Every output is a *flag* or a *suggestion* for a human to act on. This restraint is the entire design philosophy: on a small capital base, the value is in disciplined measurement and honest self-criticism, not autonomous trading.

> ⚠️ **Not financial advice.** This is a personal tool that produces research inputs for a human decision-maker. It does not execute trades and makes no guarantee of accuracy. Markets involve risk. Use at your own risk.

---

## Table of contents

- [What it does](#what-it-does)
- [Design philosophy](#design-philosophy)
- [Architecture: the three loops](#architecture-the-three-loops)
- [How a daily decision is made](#how-a-daily-decision-is-made)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Daily use & commands](#daily-use--commands)
- [Triggers reference](#triggers-reference)
- [Repository layout](#repository-layout)
- [Honest limitations](#honest-limitations)
- [Security](#security)
- [License](#license)

---

## What it does

Every morning, Sentinel:

1. **Pulls fundamentals** for each watchlist and held name from yfinance — price, margins, growth, balance sheet, valuation multiples, earnings dates.
2. **Values each name** through a reverse-DCF and a three-method consensus (DCF, PEG, sector-relative EV/EBITDA), so no single metric can produce a false signal.
3. **Guards against falling knives** using moving-average trend classification and a post-earnings reaction check.
4. **Ranks the buys**, sizes them against your monthly budget, and flags any holding whose thesis is weakening.
5. **Narrates** the structured result into a readable brief via Claude, and **delivers** it to Telegram + Obsidian, opening with a live portfolio snapshot.

Separately, on a weekly cadence, it **measures** every past signal against two benchmarks and **diagnoses** the closed outcomes with a web-searching post-mortem engine.

---

## Design philosophy

The system encodes a specific investing approach. The code is opinionated on purpose.

- **Compounding over trading.** A long horizon. Time in market beats timing the market.
- **Concentration over diversification.** A handful of strong positions beats a dozen weak ones.
- **Never sell on price alone — only on a broken thesis.** Price weakness is not a sell signal; a broken fundamental thesis is.
- **Margin of safety is mandatory.** A cheap signal from one method is noise. Agreement across independent methods is signal.
- **Honest measurement.** Every statistic is benchmarked against the index you would otherwise just hold, every sample size is disclosed, and the system can tell you when *it* is broken.
- **Flag, never trade.** The machine measures and recommends. The human decides and executes.

---

## Architecture: the three loops

A genuinely useful decision system has three loops — decide, measure, diagnose. Sentinel implements all three; the final adaptation step is deliberately human.

```
┌─────────────────────────────────────────────────────────────────┐
│  LOOP 1 — DECIDE  (daily)                                         │
│  fetch → value → guard → rank → size → flag → narrate → deliver   │
│  output: morning brief to Telegram + Obsidian                     │
└─────────────────────────────────────────────────────────────────┘
                              │ logs every decision
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  LOOP 2 — MEASURE  (weekly)                                       │
│  re-price every past signal at 2w / 1m / 3m / 6m horizons         │
│  compute alpha vs QQQ AND vs RSP (equal-weight), with n disclosed │
│  output: calibration report (are the signals actually good?)      │
└─────────────────────────────────────────────────────────────────┘
                              │ closed outcomes
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  LOOP 3 — DIAGNOSE  (weekly)                                      │
│  Claude + web search asks "why did this move?" — hedged,          │
│  sourced, with a forced null-hypothesis check                     │
│  output: report cards on hits and misses                          │
└─────────────────────────────────────────────────────────────────┘
                              │ patterns over time
                              ▼
        LOOP 3.5 — ADAPT  (manual, by design)
        the human reads the evidence and decides what to change
```

Why is adaptation manual? Because automated parameter tuning on a small sample overfits to noise. Auto-tuning becomes appropriate only once there are enough closed outcomes per bucket (≈30+) to be statistically meaningful — likely months of live data. Until then, a human in the loop is the correct design, not a limitation.

A full technical walkthrough lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## How a daily decision is made

The valuation pipeline, step by step:

1. **Hard floors** — a stock must clear minimum quality (ROIC, FCF margin, revenue growth, balance sheet, stated moat) or it is rejected outright. ETFs skip this.
2. **Quality score** — survivors get a 0–100 composite.
3. **Reverse-DCF** — solve for the growth rate the *market* is implying at the hurdle rate ("what does the price assume").
4. **Achievable growth** — a capped blend of analyst and historical growth ("what is realistically deliverable").
5. **Edge** = achievable − implied. Positive = potentially cheap.
6. **Three-method consensus** — DCF, PEG (on revenue growth), and sector-relative EV/EBITDA each vote cheap/neutral/expensive. 2-of-3 cheap = BUY; 3-of-3 = BUY + conviction. The methods use *different* inputs so one bad estimate can't flip them all.
7. **Discipline overrides** — BUY downgraded to WAIT if within 5% of all-time high, or if earnings are imminent.
8. **Trend guard** — classifies the chart (value-trap / weak / basing / uptrend) and attaches a caveat without suppressing the signal.
9. **Post-earnings guard** — if earnings landed in the last 7 days, checks the market's *reaction*: a beat the market sold off, or an outright miss, downgrades the BUY and asks the human to verify.
10. **Trend context** — every buy carries its 90-day change and distance above the 52-week low, so a slow falling-knife is always visible.
11. **Holding health & fast-warning** — flags holdings whose fundamentals are weakening, with an early heads-up if a flagged holding is also in a deep price breakdown.
12. **Rank, size, narrate, deliver.**

---

## Quickstart

```bash
# 1. Clone
git clone https://github.com/<you>/smartmoney-sentinel.git
cd smartmoney-sentinel

# 2. Install
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure secrets (never committed)
cp .env.example ~/.config/guga/.env
chmod 600 ~/.config/guga/.env
#   ...then edit that file with your real Anthropic + Telegram values.

# 4. Seed your portfolio + watchlist from the examples
cp holdings.example.json holdings.json
cp watchlist.example.json watchlist.json
#   ...edit both with your real positions and targets.

# 5. Run it
python3 sentinel.py            # full daily brief
python3 portfolio.py           # live portfolio snapshot
```

---

## Configuration

| File | Purpose |
|------|---------|
| `~/.config/guga/.env` | API keys & Telegram credentials (gitignored, never committed) |
| `holdings.json` | Your real positions: ticker, shares, entry price, weight, moat (gitignored) |
| `watchlist.json` | Candidate names with buy triggers (gitignored) |

The repo ships with `*.example.json` files containing fictional data so it runs out of the box as a demo. Your real files stay local and are gitignored.

---

## Daily use & commands

| Command | What it does |
|---------|--------------|
| `python3 sentinel.py` | Run the full daily brief and deliver it. |
| `python3 portfolio.py` | Live USD portfolio snapshot with per-position P&L. |
| `python3 portfolio.py buy NVDA 2 175.50` | Record a buy (averages cost basis). |
| `python3 portfolio.py add AMD 3 230 "moat note"` | Open a brand-new position. |
| `python3 portfolio.py sell CVS 1` | Record a sale (avg cost unchanged). |
| `python3 track_outcomes.py` | Weekly calibration: alpha vs QQQ and RSP, two tracks. |
| `python3 post_mortem.py` | Generate why-cards for newly-closed outcomes. |
| `python3 heartbeat_check.py` | Watchdog: alert if the system hasn't run recently. |

On macOS, the four jobs are wired to `launchd` (daily brief, weekly calibration, weekly post-mortem, daily watchdog). See `docs/ARCHITECTURE.md` for the schedule.

---

## Triggers reference

| Trigger | Condition | Effect |
|---------|-----------|--------|
| **BUY** | 2-of-3 consensus cheap, edge ≥ gate, not near ATH, no imminent earnings | ranked buy with USD size |
| **CONVICTION** | 3-of-3 consensus cheap | BUY + ⭐ marker |
| **WATCH-PROXIMITY** | price near a watchlist trigger | informational, not a buy |
| **THESIS-WATCH** | a holding breaches a fundamental floor | review flag |
| **FAST-WARNING** | flagged holding *also* ≥15% below its 200d MA | early heads-up (never a sell order) |
| **SELL-REVIEW** | same floor breached 2 quarters, or one severe break | escalation flag (still human-decided) |
| **BEAT-BUT-SOLD** | earnings beat but market sold the print | BUY → WATCH, verify guidance |
| **FALLING-KNIFE** | down ≥20% over 90d and near 52-week low | context warning on the buy line |
| **CRASH / SILENT** | unhandled exception / no successful run in 26h | Telegram alert |

---

## Repository layout

```
smartmoney-sentinel/
├── README.md                  ← you are here
├── LICENSE                    ← MIT + not-financial-advice notice
├── requirements.txt
├── .gitignore                 ← blocks secrets, db, real holdings
├── .env.example               ← template for your credentials
├── holdings.example.json      ← demo portfolio (copy → holdings.json)
├── watchlist.example.json     ← demo watchlist (copy → watchlist.json)
├── docs/
│   └── ARCHITECTURE.md        ← full technical deep-dive
├── sentinel.py                ← Loop 1 orchestrator
├── engine.py                  ← valuation engine + verdict logic
├── yahoo.py                   ← data layer (yfinance → Fundamentals)
├── valuation_consensus.py     ← 3-method consensus
├── trend_guard.py             ← value-trap / momentum guard
├── sell_review.py             ← fundamentals-only sell escalation
├── track_outcomes.py          ← Loop 2 outcome tracker
├── post_mortem.py             ← Loop 3 why-engine
├── portfolio.py               ← live USD portfolio + trade recording
├── heartbeat_check.py         ← watchdog
└── add_holding.py             ← holdings utility
```

> **Note:** the `.py` source files above are part of the author's local system and are referenced here for documentation completeness. This repository is structured so the documentation, configuration, and examples are public while real credentials and positions remain local.

---

## Honest limitations

A documented system should be honest about what it does *not* do. In rough priority order:

1. **Single data source.** Everything trusts yfinance; a bad number can propagate. A data-sanity layer and a second source are the top open item.
2. **No regime / breadth awareness.** It can't yet recognize that "all my buys are falling knives" is itself a market signal.
3. **No run-over-run diff.** It can't easily answer "what changed since the last scan."
4. **Guidance is not parsed.** The post-earnings guard infers trouble from the price reaction; it does not read the earnings call. This is deliberate — it surfaces facts and leaves judgment to the human rather than faking comprehension.
5. **Track record is young.** The measurement loop is honest but needs months of closed outcomes before its statistics are meaningful.
6. **The adaptation loop is human.** By design, not defect — see above.

The architecture (three-loop self-measurement + full buy/hold/sell discipline + live tracking) is more advanced than a typical screener, which finds stocks but never grades itself. What remains to harden is data robustness, not learning capability.

---

## Security

- Secrets live only in `~/.config/guga/.env`, gitignored, never in source.
- Real portfolio and watchlist files are gitignored; the repo ships with fictional examples.
- The system has no trading API access and cannot move money — it reads market data and writes notifications, nothing more.
- If you fork this, **rotate any key that has ever touched a committed file.**

---

## License

[MIT](LICENSE). Free to use, learn from, and build on. No warranty. Not financial advice.
