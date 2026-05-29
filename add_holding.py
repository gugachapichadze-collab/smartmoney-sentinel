#!/usr/bin/env python3
"""Add or update a holding without hand-editing JSON.

Usage:
  python add_holding.py TICKER WEIGHT [moat...]
  python add_holding.py NVDA 0.25 "CUDA lock-in + AI dominance"
  python add_holding.py remove TICKER

WEIGHT is a fraction (0.25 = 25%) OR a dollar value if > 1 (auto-converted
using the total of all current holdings + this one... see note). Simplest:
pass the fraction. To recompute all weights from TBC values, just re-edit
holdings.json directly or re-run with fresh fractions.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATH = HERE / "holdings.json"


def load():
    return json.loads(PATH.read_text())


def save(data):
    PATH.write_text(json.dumps(data, indent=2))


def main():
    if len(sys.argv) < 2:
        print("Usage: python add_holding.py TICKER WEIGHT [moat]  |  remove TICKER")
        return

    data = load()
    holdings = data.get("holdings", [])

    if sys.argv[1].lower() == "remove":
        tk = sys.argv[2].upper()
        data["holdings"] = [h for h in holdings if h["ticker"] != tk]
        save(data)
        print(f"Removed {tk}. {len(data['holdings'])} holdings remain.")
        return

    ticker = sys.argv[1].upper()
    weight = float(sys.argv[2])
    moat = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""

    found = False
    for h in holdings:
        if h["ticker"] == ticker:
            h["weight"] = weight
            if moat:
                h["moat"] = moat
            found = True
            break
    if not found:
        holdings.append({"ticker": ticker, "entry_price": 0,
                         "weight": weight, "moat": moat})

    data["holdings"] = holdings
    save(data)
    action = "Updated" if found else "Added"
    print(f"{action} {ticker} (weight {weight:.1%}). {len(holdings)} holdings total.")
    total = sum(h["weight"] for h in holdings)
    if total > 1.05:
        print(f"  ⚠ weights sum to {total:.0%} — consider rebalancing your fractions.")


if __name__ == "__main__":
    main()
