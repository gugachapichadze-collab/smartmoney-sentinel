#!/usr/bin/env python3
"""SmartMoney Sentinel — daily run v3 (06:00).

Engine decides; Claude narrates. Pipeline:
  load config -> fetch Yahoo -> evaluate -> RANK + SIZE buys -> log to journal
  -> learning-loop read-back -> Claude brief (EN + KA) -> Telegram + Obsidian.

Secrets in ~/.config/guga/.env: ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os
import json
import sqlite3
import dbconn
import datetime
from pathlib import Path

import anthropic
import requests
import trend_guard
import portfolio
import sell_review

from engine import evaluate, position_health, rank_buys, suggest_size, HURDLE, hard_floors
import datasource as data_source

HERE = Path(__file__).resolve().parent
env_path = Path.home() / ".config" / "guga" / ".env"
for line in env_path.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

DATE = datetime.date.today().isoformat()
VAULT_DIR = Path("/Users/ruska/Documents/GugaBrain/GugaBrain/Investment Briefs")
VAULT_DIR.mkdir(parents=True, exist_ok=True)
VAULT_PATH = VAULT_DIR / f"{DATE}.md"
DB_PATH = HERE / "journal.db"
MONTHLY_BUDGET = 250          # your monthly contribution, for sizing suggestions
MAX_ACTIONS = 3


def journal_init():
    con = dbconn.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS decisions(
        date TEXT, ticker TEXT, kind TEXT, action TEXT,
        price REAL, implied_g REAL, achievable_g REAL, edge REAL,
        quality REAL, final_score REAL, confidence TEXT, note TEXT)""")
    con.commit()
    return con


def journal_write(con, kind, v, price):
    con.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (DATE, v.ticker, kind, v.action, price,
                 v.implied_growth, v.achievable_growth, v.edge,
                 v.quality_score, v.final_score, v.confidence, v.note))
    con.commit()


def load(name):
    return json.loads((HERE / name).read_text())


def run_engine():
    con = journal_init()
    holdings = load("holdings.json").get("holdings", [])
    watch = load("watchlist.json").get("watchlist", [])
    all_verdicts, watch_proximity, health_flags, errors = [], [], [], []

    for w in watch:
        try:
            f = data_source.fetch(w["ticker"], "", ath=w.get("ath", 0),
                                  moat=w.get("moat", ""))
            v = evaluate(f)
            if v.action == "BUY":
                ctx = trend_guard.classify(f.ticker, f.price, info=getattr(f, "raw_info", {}) or {})
                cav = trend_guard.trap_caveat(ctx)
                if cav:
                    v.note += " | " + cav
            journal_write(con, "watch", v, f.price)
            all_verdicts.append((v, f, w.get("trigger")))
            trig = w.get("trigger")
            if trig and f.price <= trig * 1.05 and not v.rejected:
                watch_proximity.append((v, f, trig))
        except Exception as e:
            errors.append(f"{w['ticker']}: {e}")

    for h in holdings:
        try:
            f = data_source.fetch(h["ticker"], "", ath=h.get("ath", 0),
                                  moat=h.get("moat", ""), is_etf=h.get("is_etf", False))
            hp = position_health(f, h.get("weight", 0))
            sell_review.record_breaches(con, DATE, f.ticker,
                [] if f.is_etf else hard_floors(f),
                f.roic, f.fcf_margin, f.rev_growth, f.net_debt_ebitda)
            v = evaluate(f)
            journal_write(con, "hold", v, f.price)
            if hp["flags"]:
                health_flags.append((hp, f))
        except Exception as e:
            errors.append(f"{h['ticker']}: {e}")

    con.close()
    buys = rank_buys([v for v, _, _ in all_verdicts])[:MAX_ACTIONS]
    return buys, watch_proximity, health_flags, errors


def learning_readback():
    """Read the journal back: how have past BUY signals scored over time?
    This is the calibration loop — the system learning from itself."""
    try:
        con = dbconn.connect(DB_PATH)
        rows = con.execute(
            "SELECT confidence, COUNT(*), AVG(edge) FROM decisions "
            "WHERE action='BUY' GROUP BY confidence").fetchall()
        con.close()
        if not rows:
            return ""
        parts = [f"{c}: {n} signals, avg edge {e:+.0%}" for c, n, e in rows if e is not None]
        return " | ".join(parts)
    except Exception:
        return ""


def build_payload(buys, prox, health, errors):
    def g(x): return f"{x:+.1%}" if x is not None else "n/a"
    lines = [f"DATE: {DATE}", f"HURDLE: {HURDLE:.0%}", f"MONTHLY BUDGET: USD {MONTHLY_BUDGET}", ""]

    lines.append("RANKED BUY SIGNALS (best risk-adjusted first; suggested USD size):")
    if buys:
        for v in buys:
            size = suggest_size(v, MONTHLY_BUDGET)
            _star = "⭐CONVICTION " if getattr(v, "conviction", False) else ""
            _cons = f" | {v.consensus_summary}" if getattr(v, "consensus_summary", "") else ""
            _trend = getattr(v, "trend_context", "")
            lines.append(
                f"  #{buys.index(v)+1} {v.ticker}: {_star}edge {g(v.edge)}, "
                f"quality {v.quality_score:.0f}/100, confidence {v.confidence}, "
                f"fscore {v.final_score} -> suggest USD {size:.0f}. {v.note}{_cons}")
            if _trend:
                lines.append(f"       trend: {_trend}")
    else:
        lines.append("  none")

    lines.append("\nWATCHLIST PROXIMITY (near trigger, not a BUY):")
    lines += [f"  {v.ticker}: ${f.price:.2f} vs trigger ${trig} -> {v.action}"
              for v, f, trig in prox] or ["  none"]

    lines.append("\nHOLDING HEALTH FLAGS:")
    lines += [f"  {hp['ticker']}: " + "; ".join(hp["flags"]) for hp, f in health] or \
             ["  none — all holdings pass floors, no concentration breach"]

    # FIX #5: FAST-WARNING tier. A holding that is BOTH already fundamentally
    # flagged (THESIS-WATCH) AND in a fast/deep price breakdown (>=15% below 200d)
    # gets an early heads-up — months before the quarterly sell-review clock can
    # fire. This is NOT a sell order and NOT price-only: it requires the
    # fundamentals to ALSO be weak, preserving the "never sell on price" rule.
    lines.append("\nFAST-WARNING (early heads-up — NOT a sell order; your call):")
    _fast = []
    FAST_DROP = -0.15
    for hp, f in health:
        try:
            ctx = trend_guard.classify(f.ticker, f.price,
                                       info=getattr(f, "raw_info", {}) or {})
            p200 = ctx.pct_vs_200
            if p200 is not None and p200 <= FAST_DROP:
                _fast.append(
                    f"  ⚠ {f.ticker}: {p200:+.0%} vs 200d AND fundamentals weak "
                    f"({'; '.join(hp['flags'])}) — review thesis NOW. Not a sell order.")
        except Exception:
            pass
    lines += _fast or ["  none — no flagged holding is also in a deep price breakdown"]

    lines.append("\nSELL-REVIEW (fundamentals-only escalation; flag, not order):")
    _src = dbconn.connect(DB_PATH)
    lines += sell_review.escalation_lines(_src)
    _src.close()

    rb = learning_readback()
    if rb:
        lines.append(f"\nLEARNING LOOP (historical signal calibration): {rb}")

    if errors:
        lines.append("\nDATA ERRORS:")
        lines += [f"  {e}" for e in errors]
    return "\n".join(lines)


SYSTEM = """You are Guga's investment Chief of Staff. Write a short daily brief.

HARD RULES:
- The engine already decided. You narrate faithfully — never invent a buy/sell.
- Most days = "no action". Say it plainly; do not manufacture urgency.
- Lead with ONE line: the single most important thing.
- When a BUY is MEDIUM or LOW confidence, LEAD WITH THE CAVEAT, not the verdict.
  A confident-sounding brief on shaky data is the failure mode to avoid.
- If a BUY rests on a [ROE proxy] or LOW confidence, say so in plain words.
- Numbers over adjectives. No motivational filler.
- Respect the 20-year plan: down price + intact quality = opportunity, not panic.
- Holding flags are for REVIEW, never auto-sell.
- Use the suggested USD size, but frame it as a suggestion, not an order.
- End with: "⚠️ Not financial advice. Data as of {date}."

FORMAT:
**One line:** ...
**Top buy (if any):** ticker, size, why, and the main caveat
**Other signals:** ...
**Watchlist proximity:** ...
**Holding flags:** ...
Keep it English-only. No Georgian.

CAVEAT DISCIPLINE: the real risk in a BUY is almost never the quality score
(72/100 is GOOD, not "low"). The real risk is whether the achievable-growth
assumption is realistic for a large, mature company. When you state a caveat,
caveat the GROWTH ASSUMPTION, not the quality number."""


def narrate(payload):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1300,
        system=SYSTEM.replace("{date}", DATE),
        messages=[{"role": "user", "content":
                   "Today's engine verdicts. Write the brief:\n\n" + payload}],
    )
    return resp.content[0].text


def deliver(brief):
    # Prepend the live portfolio snapshot so every brief opens with where you stand.
    try:
        snap = portfolio.snapshot_text()
        brief = snap + "\n\n" + "=" * 40 + "\n\n" + brief
    except Exception as _e:
        print(f"[snapshot skipped: {_e}]")
    VAULT_PATH.write_text(
        f"# SmartMoney Sentinel — {DATE}\n\n{brief}\n\n---\n"
        f"*Auto-generated 06:00 · engine-decided, Claude-narrated · v3*\n")
    print(f"Written to Obsidian: {VAULT_PATH}")
    tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    r = requests.post(url, json={"chat_id": chat, "text": f"📊 Sentinel {DATE}\n\n{brief}",
                                 "parse_mode": "Markdown"}, timeout=20)
    if not r.ok:
        requests.post(url, json={"chat_id": chat,
                      "text": f"📊 Sentinel {DATE}\n\n{brief}"}, timeout=20)
    print("Sent to Telegram.")


def _alert(msg: str):
    """Standalone Telegram alert — reuses the .env-loaded token. Never raises."""
    try:
        tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        requests.post(url, json={"chat_id": chat, "text": msg}, timeout=20)
    except Exception as _e:
        print(f"[alert failed] {_e}")


def _heartbeat(job: str = "daily_brief"):
    """Record a successful run timestamp so the watchdog can detect silent failures."""
    try:
        con = dbconn.connect(DB_PATH)
        con.execute("CREATE TABLE IF NOT EXISTS heartbeat("
                    "job TEXT PRIMARY KEY, last_success_utc TEXT)")
        con.execute("INSERT OR REPLACE INTO heartbeat VALUES (?,?)",
                    (job, datetime.datetime.now(datetime.timezone.utc).isoformat()))
        con.commit()
        con.close()
    except Exception as _e:
        print(f"[heartbeat failed] {_e}")


def main():
    buys, prox, health, errors = run_engine()
    payload = build_payload(buys, prox, health, errors)
    print(payload)
    deliver(narrate(payload))
    _heartbeat("daily_brief")   # mark success AFTER delivery


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        _alert(f"\u26a0\ufe0f Sentinel CRASHED {DATE}\n\n{tb[-1500:]}")
        raise   # re-raise so launchd logs it too
