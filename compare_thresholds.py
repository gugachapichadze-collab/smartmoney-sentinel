#!/usr/bin/env python3
"""Compare BUY sensitivity at different edge thresholds on live data.
Shows how many/which signals fire at 4% (current) vs 8% (stricter).
Read-only — touches nothing, just prints a comparison."""
import json
from pathlib import Path
import engine
import yahoo

HERE = Path(__file__).resolve().parent
watch = json.loads((HERE / "watchlist.json").read_text())["watchlist"]

rows = []
for w in watch:
    try:
        f = yahoo.fetch(w["ticker"], "", ath=w.get("ath", 0), moat=w.get("moat", ""))
        v = engine.evaluate(f)
        if v.edge is not None and not v.rejected:
            rows.append(v)
    except Exception as e:
        print(f"  (skip {w['ticker']}: {e})")

rows.sort(key=lambda v: v.edge, reverse=True)

def verdict_at(edge, threshold):
    if edge >= threshold: return "BUY"
    if edge <= engine.WAIT_EDGE: return "WAIT"
    return "FAIR"

print(f"\n{'TICKER':7} {'EDGE':>7} {'QUAL':>5} {'CONF':6} {'@4%(now)':>9} {'@8%(strict)':>12}")
print("-" * 56)
n4 = n8 = 0
for v in rows:
    a4 = verdict_at(v.edge, 0.04)
    a8 = verdict_at(v.edge, 0.08)
    if a4 == "BUY": n4 += 1
    if a8 == "BUY": n8 += 1
    print(f"{v.ticker:7} {v.edge:+7.1%} {v.quality_score:5.0f} {v.confidence:6} {a4:>9} {a8:>12}")
print("-" * 56)
print(f"BUY count:  current(4%)={n4}   strict(8%)={n8}")
print("\nAt 4%: more opportunities, more noise, more trading temptation.")
print("At 8%: fewer/stronger signals, more compounder-like, closer to 'do nothing'.")
