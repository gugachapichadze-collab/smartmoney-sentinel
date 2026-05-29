#!/usr/bin/env python3
"""
post_mortem.py — the DIAGNOSE layer for Sentinel.

WHAT IT DOES
------------
The tracker (track_outcomes.py) measures WHAT happened to each past signal
(return vs QQQ). This module asks WHY, and judges the decision quality, by
producing a structured REPORT CARD for every signal whose horizon has closed.

For each closed outcome it asks Claude (with web search) to fill in:
  - WHAT was recommended (price, edge, confidence, flags) — from the journal
  - WHAT happened (return, alpha vs QQQ) — from the outcomes table
  - WHY (hypothesis only): news / analyst-target changes / earnings around the
    window that likely drove the move — WITH SOURCES, never stated as fact
  - CALL vs REASONING: was the recommendation right? was the reasoning right?
    (these differ — you can be right for the wrong reason, which is luck, or
    wrong for the right reason, which is variance — both are learnings)
  - SUGGESTED TWEAK: one concrete, optional change to the system

CRITICAL DISCIPLINE
-------------------
An LLM explaining why a stock moved WILL sometimes produce a confident, wrong
story. So every "why" is labelled a HYPOTHESIS, must cite sources, and must
never assert causation as fact. A report that says "likely driven by X (per
2 sources)" is useful; "it fell because X" is dangerous. The prompt enforces
this. The human reads the card and decides — the system never auto-changes.

OUTPUT
------
Writes a dated markdown report card to the Obsidian vault and prints a summary.
Idempotent: won't re-analyze an outcome it already has a card for (tracked in
a `postmortem_log` table).

RUN
    python3 post_mortem.py                 # analyze newly-closed outcomes
    python3 post_mortem.py --horizon 1m    # only this horizon
    python3 post_mortem.py --dry-run       # show what WOULD be analyzed, no API calls
    python3 post_mortem.py --limit 5       # cap API calls this run (cost control)
"""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path as _P
_env = _P.home() / ".config" / "guga" / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.strip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "journal.db"

# Where to write report cards. Mirrors sentinel.py's vault location pattern.
VAULT_DIR = Path("/Users/ruska/Documents/GugaBrain/GugaBrain/Investment Briefs/Post-Mortems")

MODEL = "claude-haiku-4-5-20251001"   # same family the brief uses; cheap + capable
MAX_TOKENS = 1500


def ensure_log(con):
    con.execute("""CREATE TABLE IF NOT EXISTS postmortem_log(
        signal_date TEXT, ticker TEXT, kind TEXT, action TEXT, horizon TEXT,
        analyzed_at TEXT,
        UNIQUE(signal_date, ticker, kind, action, horizon))""")
    con.commit()


def closed_outcomes(con, horizon_filter=None):
    """Outcomes that have a realized return and no post-mortem yet."""
    rows = con.execute(
        "SELECT signal_date, ticker, kind, action, horizon, signal_price, "
        "price_then, return_pct, bench_return_pct, alpha_pct, predicted_edge, "
        "quality, confidence FROM outcomes "
        "WHERE return_pct IS NOT NULL").fetchall()
    done = set(con.execute(
        "SELECT signal_date, ticker, kind, action, horizon FROM postmortem_log"
    ).fetchall())
    out = []
    for r in rows:
        key = (r[0], r[1], r[2], r[3], r[4])
        if key in done:
            continue
        if horizon_filter and r[4] != horizon_filter:
            continue
        out.append(r)
    return out


PROMPT = """You are writing a post-mortem report card for one past stock signal
from Guga's investment system. Be precise, hedged, and honest. The goal is to
help Guga learn whether the SYSTEM's logic was sound — not to tell a good story.

SIGNAL FACTS (from the journal — these are ground truth, do not contradict them):
- Ticker: {ticker}
- Signal date: {signal_date}
- System verdict: {action}  (kind: {kind})
- Price at signal: ${signal_price}
- Predicted edge: {edge}
- Confidence: {confidence}
- Quality score: {quality}/100

WHAT ACTUALLY HAPPENED over the {horizon} window after the signal:
- Price then: ${price_then}
- Stock return: {ret}
- QQQ return same window: {bench}
- ALPHA (stock minus QQQ): {alpha}

Use web search to find what happened to {ticker} between {signal_date} and the
window end: earnings results, analyst rating/target changes, sector moves, major
news. Then write a report card with EXACTLY these sections:

## {ticker} — {action} on {signal_date} ({horizon})
**Outcome:** [one line: did it beat QQQ or not, by how much]

**Why (HYPOTHESIS — not fact):** [2-4 sentences on the LIKELY drivers of the
move. You MUST cite sources. Use hedged language: "likely", "appears", "per
[source]". If the evidence is thin, SAY the why is uncertain. Never assert
causation as fact.]

**Was the CALL right?** [Yes/No/Mixed — did the verdict make money vs QQQ]

**Was the REASONING right?** [Yes/No/Mixed — separate from the call. Right call +
wrong reason = luck. Wrong call + sound reason = variance. Judge the logic, not
just the result.]

**Suggested tweak (optional):** [ONE concrete system change this single case
hints at, OR "none — single data point, no change warranted." Be conservative:
one outcome rarely justifies a change. Prefer "none" unless the case is stark.]

Keep it under 250 words. Hedge all causation. Cite sources for every claim about
why the price moved."""


def _g(x, pct=True):
    if x is None:
        return "n/a"
    return f"{x:+.1%}" if pct else str(x)


def analyze_one(client, row):
    (sig_date, ticker, kind, action, horizon, sig_price, price_then,
     ret, bench, alpha, edge, quality, conf) = row
    prompt = PROMPT.format(
        ticker=ticker, signal_date=sig_date, action=action, kind=kind,
        signal_price=f"{sig_price:.2f}" if sig_price else "n/a",
        edge=_g(edge), confidence=conf or "n/a",
        quality=f"{quality:.0f}" if quality is not None else "n/a",
        horizon=horizon,
        price_then=f"{price_then:.2f}" if price_then else "n/a",
        ret=_g(ret), bench=_g(bench), alpha=_g(alpha))

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )
    # Concatenate text blocks (web search interleaves tool blocks).
    parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    return "\n".join(p for p in parts if p).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--horizon", default=None, help="only this horizon (2w/1m/3m/6m)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=20, help="max API calls this run")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"DB not found: {db}", file=sys.stderr); sys.exit(1)
    con = sqlite3.connect(db)
    ensure_log(con)

    todo = closed_outcomes(con, args.horizon)
    if not todo:
        print("No newly-closed outcomes to analyze. "
              "(Outcomes appear once track_outcomes.py records a realized return.)")
        return

    print(f"{len(todo)} closed outcome(s) awaiting post-mortem.")
    if args.dry_run:
        for r in todo:
            print(f"  would analyze: {r[1]} {r[3]} {r[0]} ({r[4]}) alpha={_g(r[9])}")
        return

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY not set in environment.", file=sys.stderr)
        sys.exit(1)
    import anthropic
    client = anthropic.Anthropic(api_key=key)

    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    cards = []
    for r in todo[:args.limit]:
        sig_date, ticker, kind, action, horizon = r[0], r[1], r[2], r[3], r[4]
        try:
            card = analyze_one(client, r)
        except Exception as e:
            print(f"  ! {ticker} {horizon}: {e}")
            continue
        cards.append(card)
        con.execute("INSERT OR IGNORE INTO postmortem_log VALUES (?,?,?,?,?,?)",
                    (sig_date, ticker, kind, action, horizon, today))
        con.commit()
        print(f"  + {ticker} {action} {horizon} analyzed")

    if cards:
        path = VAULT_DIR / f"post-mortem-{today}.md"
        body = (f"# Sentinel Post-Mortems — {today}\n\n"
                f"*Diagnose layer. Every 'why' is a HYPOTHESIS with sources, "
                f"not fact. One outcome rarely justifies a system change — "
                f"read for patterns across many cards, not single verdicts.*\n\n"
                + "\n\n---\n\n".join(cards) + "\n")
        path.write_text(body)
        print(f"\nWrote {len(cards)} report card(s) -> {path}")
    con.close()


if __name__ == "__main__":
    main()
