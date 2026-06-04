#!/usr/bin/env python3
"""Compare old vs new FCF calculation across tickers.

OLD: info["freeCashflow"]            (yfinance's headline number)
NEW: _computed_fcf(t, info)          (OCF + Capex - SBC, from the statement)

Prints the OCF / Capex / SBC components per ticker so each NEW number can be
audited against the cash-flow statement rather than trusted blindly.
Hits the live yfinance API. Read-only — touches no other part of the engine.
"""
from __future__ import annotations

import yfinance as yf

from yahoo import _computed_fcf, _safe

TICKERS = ["NVDA", "PLTR", "CRM", "MSFT", "GOOGL", "MU", "META"]

B = 1e9


def _pick(df, names):
    """Same selection logic _computed_fcf uses, exposed for the audit columns."""
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            val = df.loc[n].iloc[0]
            if val is not None:
                try:
                    fv = float(val)
                except (TypeError, ValueError):
                    continue
                if fv == fv:  # NaN guard
                    return fv
    return None


def _components(t):
    cf = t.cashflow
    ocf = _pick(cf, ["Operating Cash Flow", "Total Cash From Operating Activities",
                     "OperatingCashFlow",
                     "Cash Flow From Continuing Operating Activities"])
    capex = _pick(cf, ["Capital Expenditure", "Capital Expenditures",
                       "CapitalExpenditures"])
    sbc = _pick(cf, ["Stock Based Compensation", "StockBasedCompensation"])
    return ocf, capex, sbc


def _b(x):
    return f"{x / B:7.1f}" if x is not None else "    n/a"


def main():
    hdr = (f"{'TICK':<6}{'OLD $B':>9}{'NEW $B':>9}{'ABSΔ $B':>9}{'%Δ':>8}"
           f"  | {'OCF':>8}{'CAPEX':>8}{'SBC':>8}")
    print(hdr)
    print("-" * len(hdr))
    for tk in TICKERS:
        try:
            t = yf.Ticker(tk)
            info = t.info or {}
            old = float(_safe(info, "freeCashflow", 0) or 0)
            new = _computed_fcf(t, info)
            ocf, capex, sbc = _components(t)
            absd = new - old
            pct = (absd / abs(old) * 100) if old else float("nan")
            print(f"{tk:<6}{old/B:9.1f}{new/B:9.1f}{absd/B:9.1f}{pct:7.0f}%"
                  f"  | {_b(ocf)}{_b(capex)}{_b(sbc)}")
        except Exception as e:
            print(f"{tk:<6}  ERROR: {e}")
    print("\nNEW = OCF + CAPEX(neg) - SBC. All figures in $B (latest fiscal year).")


if __name__ == "__main__":
    main()
