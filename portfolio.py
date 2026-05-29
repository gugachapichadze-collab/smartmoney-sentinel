#!/usr/bin/env python3
"""
portfolio.py — live USD snapshot of your real TBC holdings.

VIEW (default):
    python3 portfolio.py
    -> current value, weight %, P&L $/% per position + portfolio totals

RECORD A TRADE (updates holdings.json, recomputes avg price):
    python3 portfolio.py buy  NVDA 2 175.50     # add 2 shares @ $175.50
    python3 portfolio.py sell NVDA 1            # sell 1 share (keeps avg price)
    python3 portfolio.py add  AMD  3 230  "AI accelerators"   # brand-new position

Prices are live from yfinance. Everything in USD.
"""
import json, sys
from pathlib import Path
import yfinance as yf

HERE = Path(__file__).resolve().parent
PATH = HERE / "holdings.json"

def load(): return json.loads(PATH.read_text())
def save(d): PATH.write_text(json.dumps(d, indent=2))

def live_price(tk):
    try:
        fi = yf.Ticker(tk).fast_info
        p = fi.get("lastPrice") or fi.get("last_price")
        return float(p) if p else None
    except Exception:
        return None

def view():
    data = load()
    rows = []
    tot_val = tot_cost = 0.0
    for h in data["holdings"]:
        tk = h["ticker"]
        sh = h.get("shares", 0) or 0
        avg = h.get("entry_price", 0) or 0
        px = live_price(tk)
        if px is None or sh == 0:
            rows.append((tk, sh, avg, px, None, None, None)); continue
        val = sh * px
        cost = sh * avg
        pnl = val - cost
        pnl_pct = (pnl / cost) if cost else 0
        tot_val += val; tot_cost += cost
        rows.append((tk, sh, avg, px, val, pnl, pnl_pct))

    print("=" * 78)
    print(f"  PORTFOLIO SNAPSHOT (live, USD)")
    print("=" * 78)
    print(f"  {'Ticker':6} {'Shares':>11} {'Avg':>9} {'Price':>9} "
          f"{'Value':>10} {'Weight':>7} {'P&L $':>9} {'P&L %':>7}")
    print("  " + "-" * 74)
    # sort by current value desc
    rows.sort(key=lambda r: (r[4] or 0), reverse=True)
    for tk, sh, avg, px, val, pnl, pnl_pct in rows:
        if val is None:
            print(f"  {tk:6} {sh:>11.4f} {avg:>9.2f} {'n/a':>9}  (price fetch failed)")
            continue
        w = val / tot_val if tot_val else 0
        sign = "+" if pnl >= 0 else ""
        print(f"  {tk:6} {sh:>11.4f} {avg:>9.2f} {px:>9.2f} "
              f"{val:>10.2f} {w:>6.1%} {sign}{pnl:>8.2f} {sign}{pnl_pct:>6.1%}")
    print("  " + "-" * 74)
    tot_pnl = tot_val - tot_cost
    tot_pct = (tot_pnl / tot_cost) if tot_cost else 0
    sign = "+" if tot_pnl >= 0 else ""
    print(f"  {'TOTAL':6} {'':>11} {'':>9} {'':>9} "
          f"{tot_val:>10.2f} {'100%':>7} {sign}{tot_pnl:>8.2f} {sign}{tot_pct:>6.1%}")
    print(f"\n  Invested: ${tot_cost:,.2f}   Current: ${tot_val:,.2f}   "
          f"P&L: {sign}${tot_pnl:,.2f} ({sign}{tot_pct:.1%})")
    print("\n  ⚠️ Not financial advice. Live prices via yfinance.")

def trade():
    cmd = sys.argv[1].lower()
    tk = sys.argv[2].upper()
    data = load()
    h = next((x for x in data["holdings"] if x["ticker"] == tk), None)

    if cmd in ("buy", "add"):
        add_sh = float(sys.argv[3])
        price = float(sys.argv[4])
        if h is None:  # new position
            moat = " ".join(sys.argv[5:]) if len(sys.argv) > 5 else ""
            data["holdings"].append({"ticker": tk, "entry_price": price,
                                     "shares": add_sh, "weight": 0, "moat": moat})
            print(f"Added NEW position {tk}: {add_sh} sh @ ${price:.2f}")
        else:          # average up/down
            old_sh = h.get("shares", 0); old_avg = h.get("entry_price", 0)
            new_sh = old_sh + add_sh
            new_avg = (old_sh * old_avg + add_sh * price) / new_sh
            h["shares"] = round(new_sh, 8); h["entry_price"] = round(new_avg, 4)
            print(f"{tk}: {old_sh:.4f} -> {new_sh:.4f} sh | "
                  f"avg ${old_avg:.2f} -> ${new_avg:.2f}")
        save(data)

    elif cmd == "sell":
        if h is None:
            print(f"{tk} not held."); return
        sell_sh = float(sys.argv[3])
        old_sh = h.get("shares", 0)
        new_sh = max(0.0, old_sh - sell_sh)
        h["shares"] = round(new_sh, 8)   # avg price unchanged on a sell
        save(data)
        print(f"{tk}: sold {sell_sh:.4f} | {old_sh:.4f} -> {new_sh:.4f} sh "
              f"(avg price unchanged)")
        if new_sh == 0:
            print(f"  note: {tk} now 0 shares — remove with: "
                  f"python3 add_holding.py remove {tk}")


def snapshot_text():
    """Return the portfolio table as a string (for embedding in the daily brief)."""
    data = load()
    rows, tot_val, tot_cost = [], 0.0, 0.0
    for h in data["holdings"]:
        tk = h["ticker"]; sh = h.get("shares", 0) or 0; avg = h.get("entry_price", 0) or 0
        px = live_price(tk)
        if px is None or sh == 0:
            rows.append((tk, sh, avg, px, None, None, None)); continue
        val = sh * px; cost = sh * avg; pnl = val - cost
        tot_val += val; tot_cost += cost
        rows.append((tk, sh, avg, px, val, pnl, (pnl/cost if cost else 0)))
    out = ["PORTFOLIO (live, USD):",
           f"  {'Tk':5}{'Value':>10}{'Wt':>7}{'P&L$':>9}{'P&L%':>7}"]
    rows.sort(key=lambda r: (r[4] or 0), reverse=True)
    for tk, sh, avg, px, val, pnl, pct in rows:
        if val is None:
            out.append(f"  {tk:5}{'n/a':>10}"); continue
        w = val/tot_val if tot_val else 0
        sg = "+" if pnl >= 0 else ""
        out.append(f"  {tk:5}{val:>10.0f}{w:>6.0%} {sg}{pnl:>7.0f} {sg}{pct:>5.0%}")
    tp = tot_val - tot_cost; tpc = (tp/tot_cost if tot_cost else 0)
    sg = "+" if tp >= 0 else ""
    out.append(f"  {'TOTAL':5}{tot_val:>10.0f}{'100%':>7} {sg}{tp:>7.0f} {sg}{tpc:>5.0%}")
    out.append(f"  Invested ${tot_cost:,.0f} | Now ${tot_val:,.0f} | "
               f"P&L {sg}${tp:,.0f} ({sg}{tpc:.1%})")
    return "\n".join(out)

if __name__ == "__main__":
    if len(sys.argv) == 1:
        view()
    else:
        trade()
