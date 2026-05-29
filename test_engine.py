#!/usr/bin/env python3
"""Sanity tests for the engine. Run: python test_engine.py"""
from engine import Fundamentals, evaluate, reverse_dcf_implied_growth, _dcf_value

def line(): print("-" * 64)

# --- Test 1: internal consistency. If implied growth is plugged back into
# the DCF, value must equal price. ---
price, fcf = 100.0, 4.0
g = reverse_dcf_implied_growth(price, fcf)
recovered = _dcf_value(fcf, g)
print(f"Consistency: price=100, fcf=4 -> implied g={g:.2%}, "
      f"DCF back={recovered:.2f} (should be ~100)")
assert abs(recovered - 100) < 0.5
line()

# --- Test 2: realistic-ish names (illustrative numbers, not live) ---
samples = [
    # A premium compounder, fairly priced
    Fundamentals(ticker="MSFT", price=430, fcf_per_share=11.5, roic=0.29,
                 gross_margin=0.69, fcf_margin=0.30, rev_growth=0.15,
                 net_debt_ebitda=0.3, ath=470, analyst_fcf_growth=0.13,
                 hist_fcf_cagr=0.16, moat="enterprise lock-in + cloud scale"),
    # Same company after a 20% drawdown -> should flip toward BUY
    Fundamentals(ticker="MSFT-dip", price=344, fcf_per_share=11.5, roic=0.29,
                 gross_margin=0.69, fcf_margin=0.30, rev_growth=0.15,
                 net_debt_ebitda=0.3, ath=470, analyst_fcf_growth=0.13,
                 hist_fcf_cagr=0.16, moat="enterprise lock-in + cloud scale"),
    # A low-quality name -> must REJECT before price is even looked at
    Fundamentals(ticker="LOWQ", price=20, fcf_per_share=0.5, roic=0.06,
                 gross_margin=0.22, fcf_margin=0.05, rev_growth=0.03,
                 net_debt_ebitda=4.1, ath=35, moat=""),
    # Quality but wildly expensive -> WAIT
    Fundamentals(ticker="PRICEY", price=900, fcf_per_share=6.0, roic=0.25,
                 gross_margin=0.75, fcf_margin=0.28, rev_growth=0.20,
                 net_debt_ebitda=0.1, ath=950, analyst_fcf_growth=0.18,
                 hist_fcf_cagr=0.22, moat="network effect"),
]

for f in samples:
    v = evaluate(f)
    ig = f"{v.implied_growth:+.1%}" if v.implied_growth is not None else "n/a"
    ag = f"{v.achievable_growth:+.1%}" if v.achievable_growth is not None else "n/a"
    print(f"{f.ticker:10} -> {v.action:7} | implied {ig:>7} | "
          f"achievable {ag:>7} | conf {v.confidence}")
    print(f"           {v.note}")
line()
print("All assertions passed.")
