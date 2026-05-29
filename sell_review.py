"""
sell_review.py — fundamentals-only SELL-REVIEW escalation for Sentinel.

PHILOSOPHY (matches Guga's 20-year plan)
----------------------------------------
NEVER sell on price. NEVER sell on negative alpha. Only escalate when the
BUSINESS is failing — and even then, raise a REVIEW flag for a human, never
an order. Down != sell. This module turns the existing one-shot "THESIS-WATCH"
into a time-aware escalation:

  THESIS-WATCH  -> floors breached this quarter (already exists, informational)
  SELL-REVIEW   -> EITHER 2+ consecutive quarters of the SAME floor breached
                          (persistent failure, not a one-off bad print)
                   OR 1 quarter of a SEVERE structural break:
                          netDebt/EBITDA > 5.0  (balance sheet coming apart)
                          ROIC <= 1%            (capital returns collapsed)

A flag, never an action. The module presents the case; Guga decides.

DATA MODEL
----------
The engine logs the *verdict* note, but NOT the health breaches. So we add a
dedicated table `health_log` that records, per run, which floors each holding
breached. Escalation reads consecutive entries from there.

`record_breaches()` is called once per run from sentinel.py (one line).
`evaluate_escalations()` reads history and returns SELL-REVIEW verdicts.

QUARTER LOGIC
-------------
Fundamentals only change quarterly, but the engine runs daily — so daily rows
would fake "persistence". We collapse to one record per (ticker, year-quarter):
the latest run in each quarter wins. "2 consecutive quarters" therefore means
two distinct calendar quarters, which is the real signal.
"""

from __future__ import annotations
import sqlite3
from datetime import datetime
from typing import Optional

# Severe one-quarter triggers (structural breaks that don't need confirmation)
SEVERE_LEVERAGE = 5.0     # netDebt/EBITDA above this = escalate now
SEVERE_ROIC = 0.01        # ROIC at/below this = escalate now
PERSIST_QUARTERS = 2      # consecutive quarters of same floor = escalate


def ensure_health_table(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS health_log(
        date TEXT, quarter TEXT, ticker TEXT,
        breached_floors TEXT,      -- comma-joined floor keys, e.g. "ROIC,FCF,LEV"
        roic REAL, fcf_margin REAL, rev_growth REAL, net_debt_ebitda REAL,
        UNIQUE(quarter, ticker))""")
    con.commit()


def _quarter(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.year}Q{(d.month - 1)//3 + 1}"


# Canonical floor keys so we can compare "same floor breached" across quarters
# regardless of the exact numbers in the message.
def _floor_keys(fails: list) -> list:
    keys = []
    for f in fails:
        fl = f.lower()
        if "roic" in fl:
            keys.append("ROIC")
        elif "fcf" in fl:
            keys.append("FCF")
        elif "rev-growth" in fl or "rev growth" in fl:
            keys.append("REVG")
        elif "netdebt" in fl or "ebitda" in fl:
            keys.append("LEV")
        elif "moat" in fl:
            keys.append("MOAT")
    return keys


def record_breaches(con: sqlite3.Connection, date_str: str, ticker: str,
                    fails: list, roic: float, fcf_margin: float,
                    rev_growth: float, net_debt_ebitda: float) -> None:
    """Called once per holding per run. Collapses to one row per quarter
    (latest run in the quarter overwrites earlier ones)."""
    ensure_health_table(con)
    q = _quarter(date_str)
    keys = ",".join(_floor_keys(fails))
    con.execute(
        "INSERT INTO health_log VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(quarter, ticker) DO UPDATE SET "
        "date=excluded.date, breached_floors=excluded.breached_floors, "
        "roic=excluded.roic, fcf_margin=excluded.fcf_margin, "
        "rev_growth=excluded.rev_growth, net_debt_ebitda=excluded.net_debt_ebitda",
        (date_str, q, ticker, keys, roic, fcf_margin, rev_growth, net_debt_ebitda))
    con.commit()


def _consecutive_same_floor(con, ticker: str) -> tuple[int, set]:
    """Return (run_length, floor_set) of the most recent consecutive quarters
    that ALL share at least one common breached floor."""
    rows = con.execute(
        "SELECT quarter, breached_floors FROM health_log "
        "WHERE ticker=? ORDER BY quarter DESC", (ticker,)).fetchall()
    if not rows:
        return 0, set()

    # Walk back from most recent; track the intersection of breached floors.
    common: Optional[set] = None
    run = 0
    for _q, keys in rows:
        kset = set(k for k in keys.split(",") if k)
        if not kset:
            break  # a clean quarter ends the streak
        if common is None:
            common = kset
        else:
            inter = common & kset
            if not inter:
                break  # different floors — streak of "same floor" ends
            common = inter
        run += 1
    return run, (common or set())


def evaluate_escalations(con: sqlite3.Connection) -> list:
    """Return a list of SELL-REVIEW dicts for holdings that escalate.
    Each: {ticker, level, reasons, quarters}. Empty if none / insufficient data."""
    ensure_health_table(con)
    tickers = [r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM health_log").fetchall()]
    out = []
    for tk in tickers:
        latest = con.execute(
            "SELECT roic, net_debt_ebitda, breached_floors FROM health_log "
            "WHERE ticker=? ORDER BY quarter DESC LIMIT 1", (tk,)).fetchone()
        if not latest:
            continue
        roic, nde, keys = latest
        reasons = []

        # --- Severe one-quarter structural breaks ---
        if nde is not None and nde > SEVERE_LEVERAGE:
            reasons.append(f"SEVERE: netDebt/EBITDA {nde:.1f} > {SEVERE_LEVERAGE} "
                           f"(balance sheet stress — escalate without waiting)")
        if roic is not None and roic <= SEVERE_ROIC and "ROIC" in (keys or ""):
            reasons.append(f"SEVERE: ROIC {roic:.0%} <= {SEVERE_ROIC:.0%} "
                           f"(capital returns collapsed)")

        # --- Persistent same-floor breach across quarters ---
        run, common = _consecutive_same_floor(con, tk)
        if run >= PERSIST_QUARTERS and common:
            reasons.append(f"PERSISTENT: {'/'.join(sorted(common))} breached "
                           f"{run} consecutive quarters (not a one-off)")

        if reasons:
            out.append({"ticker": tk, "level": "SELL-REVIEW",
                        "reasons": reasons, "quarters": run})
    return out


def escalation_lines(con: sqlite3.Connection) -> list:
    """Human-readable lines for the brief. Includes an honest 'insufficient
    history' note when no quarter has accumulated enough to ever fire."""
    qcount = con.execute(
        "SELECT COUNT(DISTINCT quarter) FROM health_log").fetchone()[0]
    esc = evaluate_escalations(con)
    lines = []
    if esc:
        for e in esc:
            lines.append(f"  {e['ticker']}: SELL-REVIEW — " + "; ".join(e["reasons"]))
            lines.append("    (REVIEW, not an order — confirm thesis is broken, "
                         "not just price. Your rule: never sell on price alone.)")
    else:
        if qcount < PERSIST_QUARTERS:
            lines.append(f"  none — only {qcount} quarter(s) of health history logged; "
                         f"persistent-breach escalation needs {PERSIST_QUARTERS}. "
                         f"Severe one-quarter breaks would still fire if present.")
        else:
            lines.append("  none — no holding meets SELL-REVIEW criteria.")
    return lines
