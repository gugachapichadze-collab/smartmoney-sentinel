#!/usr/bin/env python3
"""
track_outcomes.py  (v3 — kind-aware: portfolio track + watchlist track)

WHAT'S NEW IN v3
----------------
v2 measured alpha vs QQQ but blended your CURRENT HOLDINGS with your WATCHLIST
candidates into one pile. Those are two different questions:

  PORTFOLIO TRACK (kind='hold'):
    Are the 14 stocks I already own still earning their place vs just holding QQQ?
    Which holdings are dead weight I should consider trimming?

  WATCHLIST TRACK (kind='watch'):
    Are my BUY signals on candidates actually adding alpha?
    Do my BUYs beat the names I skipped? Was the 8% gate justified?

v3 scores both, separately. The journal already tags every row kind='hold' or
'watch' (sentinel.py does this), so no engine change is needed — we just read it.

Everything is alpha vs QQQ and sample-size gated (n<10 = directional only).

RUN
    python3 track_outcomes.py            # backfill + both reports
    python3 track_outcomes.py --report   # reports only, no network
    python3 track_outcomes.py --db journal_v2.db.bak
"""

from __future__ import annotations
import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "journal.db"

BENCHMARK = "QQQ"
BENCHMARK2 = "RSP"   # FIX #4: equal-weight S&P, de-correlates from tech-heavy QQQ
HORIZONS = {"2w": 14, "1m": 30, "3m": 91, "6m": 182}
MIN_N = 10

# Watchlist: forward-looking decisions worth scoring.
WATCH_ACTIONS = ("BUY", "WAIT", "FAIR")
# Holdings: we score whatever verdict they carry (HOLD, BUY-more, etc.) — the
# question is "is this holding beating the index", regardless of action label.


def ensure_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS outcomes(
        signal_date TEXT, ticker TEXT, kind TEXT, action TEXT, horizon TEXT,
        signal_price REAL, price_then REAL, return_pct REAL,
        bench_return_pct REAL, alpha_pct REAL,
        predicted_edge REAL, quality REAL, confidence TEXT,
        recorded_at TEXT, note TEXT,
        UNIQUE(signal_date, ticker, kind, action, horizon))""")
    # FIX #4: second benchmark columns (added later — ALTER for existing DBs).
    for col in ("bench2_return_pct REAL", "alpha2_pct REAL"):
        try:
            con.execute(f"ALTER TABLE outcomes ADD COLUMN {col}")
        except Exception:
            pass   # already exists
    con.commit()


_CACHE: dict[str, object] = {}


def _history(ticker: str):
    if ticker in _CACHE:
        return _CACHE[ticker]
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="2y", auto_adjust=True)
        _CACHE[ticker] = None if (h is None or h.empty) else h
    except Exception:
        _CACHE[ticker] = None
    return _CACHE[ticker]


def price_as_of(ticker: str, when: datetime):
    h = _history(ticker)
    if h is None or "Close" not in h:
        return None
    try:
        closes = h["Close"].dropna()
        prior = closes[closes.index.date <= when.date()]
        return float(prior.iloc[-1]) if not prior.empty else None
    except Exception:
        return None


def return_between(ticker: str, d0: datetime, d1: datetime):
    p0 = price_as_of(ticker, d0)
    p1 = price_as_of(ticker, d1)
    if p0 is None or p1 is None or p0 <= 0:
        return None
    return p1 / p0 - 1.0


def backfill(con, today: datetime) -> int:
    # kind is in the decisions table; pull it so we can split tracks.
    rows = con.execute(
        "SELECT date, ticker, kind, action, price, edge, quality, confidence "
        "FROM decisions").fetchall()

    already = set(con.execute(
        "SELECT signal_date, ticker, kind, action, horizon FROM outcomes").fetchall())

    written = 0
    for sig_date, ticker, kind, action, sig_price, edge, quality, conf in rows:
        # Score watchlist decisions and all holdings. Skip REJECT noise on watch.
        if kind == "watch" and action not in WATCH_ACTIONS:
            continue
        if not sig_price or sig_price <= 0:
            continue
        try:
            d0 = datetime.strptime(sig_date, "%Y-%m-%d")
        except (TypeError, ValueError):
            continue
        for hname, hdays in HORIZONS.items():
            if (sig_date, ticker, kind, action, hname) in already:
                continue
            target = d0 + timedelta(days=hdays)
            if target > today:
                continue
            # FIX #1 (scale consistency): compute the STOCK leg the same way as the
            # benchmark — adjusted close at signal date AND horizon date, from one
            # auto_adjust series. This puts both legs on the same dividend-/split-
            # adjusted scale (true total return) instead of mixing the logged
            # unadjusted signal price with an adjusted horizon price.
            adj_signal = price_as_of(ticker, d0)      # adjusted close at signal date
            p_then = price_as_of(ticker, target)       # adjusted close at horizon
            stock_ret = return_between(ticker, d0, target)   # adjusted / adjusted
            bench_ret = return_between(BENCHMARK, d0, target)
            alpha = (stock_ret - bench_ret) if (stock_ret is not None
                                                and bench_ret is not None) else None
            bench2_ret = return_between(BENCHMARK2, d0, target)   # FIX #4: vs RSP
            alpha2 = (stock_ret - bench2_ret) if (stock_ret is not None
                                                  and bench2_ret is not None) else None
            note = "" if stock_ret is not None else "no price-as-of"
            con.execute(
                "INSERT OR IGNORE INTO outcomes "
                "(signal_date,ticker,kind,action,horizon,signal_price,price_then,"
                "return_pct,bench_return_pct,alpha_pct,predicted_edge,quality,"
                "confidence,recorded_at,note,bench2_return_pct,alpha2_pct) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sig_date, ticker, kind, action, hname, sig_price, p_then,
                 stock_ret, bench_ret, alpha, edge, quality, conf,
                 today.strftime("%Y-%m-%d"), note, bench2_ret, alpha2))
            written += 1
    con.commit()
    return written


def _agg(vals):
    v = [x for x in vals if x is not None]
    n = len(v)
    if n == 0:
        return 0, None, None
    return n, sum(v) / n, sum(1 for x in v if x > 0) / n


def _fmt(n, mean, hit, unit="alpha"):
    if n == 0:
        return "n=0   (no data)"
    flag = "" if n >= MIN_N else "  WARN n<10 directional"
    return f"n={n:<3} mean {unit} {mean:+6.1%}  beat-rate {hit:5.0%}{flag}"


def _watchlist_report(rows):
    o = ["\n" + "#" * 66,
         "WATCHLIST TRACK — are my BUY signals adding alpha?",
         "#" * 66]

    o += [f"\nW1. DO 'BUY' SIGNALS BEAT {BENCHMARK}?", "-" * 66]
    for h in HORIZONS:
        a = [r["alpha"] for r in rows if r["action"] == "BUY" and r["horizon"] == h]
        n, mean, beat = _agg(a)
        o.append(f"  {h:>3}: {_fmt(n, mean, beat)}")

    # FIX #4: same BUY signals vs RSP (equal-weight) — strips the tech-beta tilt.
    # If alpha vs QQQ is positive but alpha vs RSP is ~0, the "edge" was just
    # being long mega-cap tech, not stock selection.
    o += [f"\nW1b. DO 'BUY' SIGNALS BEAT {BENCHMARK2} (de-correlated check)?", "-" * 66]
    for h in HORIZONS:
        a2 = [r["alpha2"] for r in rows if r["action"] == "BUY" and r["horizon"] == h]
        n, mean, beat = _agg(a2)
        o.append(f"  {h:>3}: {_fmt(n, mean, beat)}")

    o += ["\nW2. DO 'BUY' CALLS BEAT THE 'WAIT/FAIR' CALLS I SKIPPED?", "-" * 66]
    for h in HORIZONS:
        buy = [r["ret"] for r in rows if r["action"] == "BUY" and r["horizon"] == h]
        skip = [r["ret"] for r in rows if r["action"] in ("WAIT", "FAIR") and r["horizon"] == h]
        nb, mb, hb = _agg(buy)
        ns, ms, hs = _agg(skip)
        if nb == 0 and ns == 0:
            continue
        o.append(f"  {h:>3}: BUY  {_fmt(nb, mb, hb, 'ret')}")
        o.append(f"       SKIP {_fmt(ns, ms, hs, 'ret')}")
        if nb >= MIN_N and ns >= MIN_N and mb is not None and ms is not None:
            o.append(f"       -> {'BUY/skip decision ADDS value' if mb > ms else 'WARNING: skipped names beat buys — too cautious'}")
        else:
            o.append("       -> pending (need n>=10 each side)")

    o += ["\nW3. WAS THE 8% EDGE GATE JUSTIFIED (alpha basis)?", "-" * 66]
    for h in HORIZONS:
        thin = [r["alpha"] for r in rows if r["horizon"] == h and r["edge"] is not None and 0.04 <= r["edge"] < 0.08]
        strong = [r["alpha"] for r in rows if r["horizon"] == h and r["edge"] is not None and r["edge"] >= 0.08]
        nt, mt, ht = _agg(thin)
        ns, ms, hs = _agg(strong)
        if nt == 0 and ns == 0:
            continue
        o.append(f"  {h:>3}: thin 4-8%  {_fmt(nt, mt, ht)}")
        o.append(f"       strong 8%+ {_fmt(ns, ms, hs)}")
        if nt >= MIN_N and ns >= MIN_N and ms is not None and mt is not None:
            o.append(f"       -> {'8% JUSTIFIED' if ms > mt else 'RECONSIDER 8%'}")
        else:
            o.append("       -> pending (need n>=10 each band)")
    return o


def _portfolio_report(rows):
    o = ["\n" + "#" * 66,
         "PORTFOLIO TRACK — are my current holdings beating the index?",
         "#" * 66]

    o += [f"\nP1. DO MY HOLDINGS (as a group) BEAT {BENCHMARK}?", "-" * 66]
    for h in HORIZONS:
        a = [r["alpha"] for r in rows if r["horizon"] == h]
        n, mean, beat = _agg(a)
        o.append(f"  {h:>3}: {_fmt(n, mean, beat)}")

    o += ["\nP2. PER-HOLDING ALPHA @3m (who's dead weight, who's pulling?)", "-" * 66]
    tickers = sorted({r["ticker"] for r in rows})
    scored = []
    for tk in tickers:
        a = [r["alpha"] for r in rows if r["ticker"] == tk and r["horizon"] == "3m"]
        n, mean, beat = _agg(a)
        if n > 0:
            scored.append((mean, tk, n, beat))
    scored.sort()  # worst alpha first
    if scored:
        for mean, tk, n, beat in scored:
            tag = "  <-- laggard, review" if (mean is not None and mean < 0) else ""
            o.append(f"  {tk:6} {_fmt(n, mean, beat)}{tag}")
    else:
        o.append("  (no 3m holding outcomes yet)")
    o.append("\n  NOTE: negative alpha alone is NOT a sell signal — your rule is")
    o.append("  'never sell on price, only on broken thesis'. This flags WHAT to")
    o.append("  review, not what to dump. Time-in-market still beats timing.")
    return o


def report(con) -> str:
    # FIX #1b (survivorship disclosure): unresolved outcomes (NULL return — e.g.
    # delisting, ticker rename, missing history) are recorded but excluded from
    # averages. Surface their COUNT so the gap is visible, not silently skipped.
    try:
        _unresolved = con.execute(
            "SELECT COUNT(*) FROM outcomes WHERE return_pct IS NULL").fetchone()[0]
        _resolved = con.execute(
            "SELECT COUNT(*) FROM outcomes WHERE return_pct IS NOT NULL").fetchone()[0]
    except Exception:
        _unresolved, _resolved = 0, 0
    _surv = (f"DATA INTEGRITY: {_resolved} outcomes scored, {_unresolved} unresolved "
             f"(NULL return — delisting/rename/missing history). "
             + ("⚠ unresolved outcomes bias results upward — investigate."
                if _unresolved else "none missing.") + "\n")
    raw = con.execute(
        "SELECT kind, action, horizon, return_pct, bench_return_pct, alpha_pct, "
        "predicted_edge, quality, confidence, ticker, alpha2_pct FROM outcomes").fetchall()
    head = ["=" * 66,
            f"SENTINEL CALIBRATION — ALPHA vs {BENCHMARK} (two tracks)",
            "=" * 66,
            _surv.rstrip()]
    if not raw:
        head += ["\nNo elapsed outcomes yet. Earliest signal must age 14 days",
                 "before the first (2w) alpha can be scored. Re-run weekly."]
        return "\n".join(head)

    rows = [dict(kind=r[0], action=r[1], horizon=r[2], ret=r[3],
                 bench=r[4], alpha=r[5], edge=r[6], quality=r[7],
                 conf=r[8], ticker=r[9], alpha2=r[10]) for r in raw]
    watch = [r for r in rows if r["kind"] == "watch"]
    hold = [r for r in rows if r["kind"] == "hold"]

    out = head
    out += _watchlist_report(watch)
    out += _portfolio_report(hold)
    out += ["\n" + "=" * 66,
            "Alpha = return minus QQQ over the same window. n<10 = noise.",
            "=" * 66]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    db = Path(args.db)
    if not db.exists():
        print(f"DB not found: {db}", file=sys.stderr); sys.exit(1)
    con = sqlite3.connect(db)
    ensure_table(con)
    if not args.report:
        print(f"Backfill: {backfill(con, datetime.now())} new outcome rows.\n")
    print(report(con))
    con.close()


if __name__ == "__main__":
    main()
