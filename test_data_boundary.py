#!/usr/bin/env python3
"""
test_data_boundary.py — verify the data-boundary guard in yahoo.fetch().

Free APIs intermittently hand back "N/A"/None/NaN/inf where a float is
expected. The guard (_num + DataQualityError, price as the only required
field) must:

  1. COERCE junk in OPTIONAL fields to a safe default (no crash, no NaN).
  2. DROP the ticker cleanly when the one REQUIRED field (price) is junk —
     via DataQualityError, which subclasses ValueError so run_engine's
     per-ticker `except Exception` logs it under DATA ERRORS: instead of
     crashing the pipeline.
  3. Close the real bug: a NaN margin used to slip PAST hard_floors
     (NaN < floor is False), passing a gate it should fail.
  4. STRICTEST: on a CLEAN payload, produce a Fundamentals byte-identical
     to what naive float() coercion produced before the guard existed —
     proving the guard only ever touches bad data.

No network: yf.Ticker is stubbed so t.info / t.financials / t.cashflow /
t.calendar / t.earnings_dates / t.history are all controlled, making
fetch() depend solely on the supplied info dict.
"""
import math

import yahoo
from yahoo import fetch, DataQualityError, _num
from engine import Fundamentals, hard_floors, FLOOR_REV_GROWTH


# ---------------------------------------------------------------------------
# Network-free stub for yf.Ticker
# ---------------------------------------------------------------------------
class _FakeTicker:
    """Returns the supplied info dict; all statement accessors are empty/None
    so the financial-statement helpers take their documented None paths
    (_real_roic -> None -> ROE proxy; _computed_fcf -> info['freeCashflow'];
    _hist_fcf_cagr -> None; earnings/trend context -> None). This isolates
    the test to the info-dict float coercions the guard governs."""
    def __init__(self, info):
        self.info = info
        self.financials = None
        self.balance_sheet = None
        self.cashflow = None
        self.calendar = {}
        self.earnings_dates = None

    def history(self, *a, **k):
        return None


def _install_stub(info):
    yahoo.yf.Ticker = lambda ticker: _FakeTicker(info)


_PASSES = []


def check(label, cond):
    assert cond, f"FAIL: {label}"
    _PASSES.append(label)
    print(f"    PASS: {label}")


print("=" * 70)
print("  DATA-BOUNDARY guard test  (yahoo.fetch coercion / clean-drop)")
print("=" * 70)


# ---------------------------------------------------------------------------
# 0. _num unit behaviour — the helper every coercion routes through.
# ---------------------------------------------------------------------------
print("\n  _num() coercion primitives:")
check("None -> default", _num(None) == 0.0)
check("'N/A' -> default", _num("N/A") == 0.0)
check("'' / '-' / '--' -> default", _num("") == 0.0 and _num("-") == 0.0 and _num("--") == 0.0)
check("NaN -> default (not NaN)", _num(float("nan")) == 0.0)
check("inf / -inf -> default", _num(float("inf")) == 0.0 and _num(float("-inf")) == 0.0)
check("bool not mapped to 1.0", _num(True) == 0.0 and _num(False) == 0.0)
check("clean float passes through", _num(0.65) == 0.65)
check("clean int -> float", _num(7) == 7.0 and isinstance(_num(7), float))
check("numeric string parses", _num("1,234.5") == 1234.5)
check("custom default honoured", _num("N/A", default=None) is None)


# ---------------------------------------------------------------------------
# 1. OPTIONAL junk fields coerce; ticker survives (no crash).
# ---------------------------------------------------------------------------
print("\n  Optional fields full of junk -> coerced, ticker NOT dropped:")
junk = {
    "currentPrice": 100.0,          # the one required field is valid
    "sharesOutstanding": "n/a",
    "totalRevenue": float("nan"),
    "freeCashflow": "N/A",
    "returnOnEquity": "--",
    "grossMargins": float("nan"),
    "revenueGrowth": None,
    "totalDebt": "N/A",
    "totalCash": None,
    "ebitda": float("inf"),
    "fiftyTwoWeekHigh": None,
    "marketCap": "",
    "forwardPE": "N/A",
    "enterpriseValue": None,
    "earningsGrowth": "n/a",
    "fiftyTwoWeekLow": None,
}
_install_stub(junk)
f = fetch("JUNK", moat="wide")          # must not raise
check("survives junk payload (no drop)", isinstance(f, Fundamentals))
check("price preserved", f.price == 100.0)
# every coerced optional float is a finite default, never NaN/inf
for name in ("fcf_per_share", "roic", "gross_margin", "fcf_margin",
             "rev_growth", "net_debt_ebitda", "market_cap"):
    v = getattr(f, name)
    check(f"{name} finite default ({v!r})", v == 0.0)
check("ath falls back to price", f.ath == 100.0)
# genuinely-optional fields coerce junk to None, not 0.0
check("forward_pe junk -> None", f.forward_pe is None)
check("enterprise_value None -> None", f.enterprise_value is None)
check("ebitda_val inf -> None", f.ebitda_val is None)
check("analyst_growth junk -> None", f.analyst_growth is None)


# ---------------------------------------------------------------------------
# 2. REQUIRED field (price) unrecoverable -> clean drop via DataQualityError.
# ---------------------------------------------------------------------------
print("\n  Required price unrecoverable -> DataQualityError (clean drop):")
for bad in (
    {"currentPrice": "N/A", "regularMarketPrice": None},
    {"currentPrice": None, "regularMarketPrice": float("nan")},
    {"currentPrice": 0.0, "regularMarketPrice": 0.0},
    {"currentPrice": -5.0, "regularMarketPrice": "-"},
    {},  # field entirely missing
):
    _install_stub(bad)
    raised = None
    try:
        fetch("BADPX")
    except DataQualityError as e:
        raised = e
    check(f"raises DataQualityError on {bad!r}", raised is not None)
# the contract that makes run_engine treat it as a logged drop, not a crash:
check("DataQualityError IS-A ValueError (caught as clean drop)",
      issubclass(DataQualityError, ValueError))


# ---------------------------------------------------------------------------
# 3. THE REAL BUG: a NaN margin must FAIL hard_floors, not slip past it.
# ---------------------------------------------------------------------------
print("\n  NaN-past-floor regression (the actual silent-garbage bug):")
# document the pre-guard failure mode: naive float(nan) slips a floor gate.
check("naive NaN slips floor (proves the bug existed): nan<floor is False",
      (float("nan") < FLOOR_REV_GROWTH) is False)
nan_rev = {
    "currentPrice": 100.0,
    "revenueGrowth": float("nan"),   # the poisoned field
    # make every OTHER floor pass so the only possible failure is rev-growth
    "returnOnEquity": 0.30,
    "totalRevenue": 1000.0,
    "freeCashflow": 300.0,
    "grossMargins": 0.70,
    "ebitda": 0.0,                   # nde -> 0.0, below ceiling
}
_install_stub(nan_rev)
f = fetch("NANREV", moat="wide")
check("rev_growth coerced to 0.0", f.rev_growth == 0.0)
check("rev_growth is not NaN", f.rev_growth == f.rev_growth)
fails = hard_floors(f)
check("NaN margin now FAILS the floor it used to pass",
      any("rev-growth" in x for x in fails))


# ---------------------------------------------------------------------------
# 4. STRICTEST: clean payload -> byte-identical Fundamentals (guard no-op).
# ---------------------------------------------------------------------------
print("\n  Clean payload -> byte-identical Fundamentals (guard touches nothing):")
# values picked as exact binary fractions so equality is true byte-identity,
# not float fuzz. Every divide resolves exactly.
clean = {
    "currentPrice": 100.0,
    "sharesOutstanding": 1000.0,
    "freeCashflow": 5000.0,        # cf stub None -> _computed_fcf returns this
    "totalRevenue": 20000.0,
    "returnOnEquity": 0.30,        # financials None -> ROE proxy
    "grossMargins": 0.65,
    "revenueGrowth": 0.18,
    "totalDebt": 1000.0,
    "totalCash": 500.0,
    "ebitda": 4000.0,
    "fiftyTwoWeekHigh": 150.0,
    "marketCap": 2_000_000_000.0,
    "forwardPE": 25.0,
    "enterpriseValue": 2_100_000_000.0,
    "earningsGrowth": 0.20,
    "fiftyTwoWeekLow": 80.0,
}
# EXPECTED = what naive float() coercion produced before the guard existed.
# (_num(x) == float(x) for every finite/clean x, so this is the pre-guard output.)
expected = Fundamentals(
    ticker="CLEAN",
    price=float(clean["currentPrice"]),
    fcf_per_share=float(clean["freeCashflow"]) / float(clean["sharesOutstanding"]),
    roic=float(clean["returnOnEquity"]),
    roic_is_proxy=True,
    gross_margin=float(clean["grossMargins"]),
    fcf_margin=float(clean["freeCashflow"]) / float(clean["totalRevenue"]),
    rev_growth=float(clean["revenueGrowth"]),
    net_debt_ebitda=(float(clean["totalDebt"]) - float(clean["totalCash"]))
                    / float(clean["ebitda"]),
    ath=float(clean["fiftyTwoWeekHigh"]),
    market_cap=float(clean["marketCap"]),
    analyst_growth=float(clean["earningsGrowth"]),
    hist_fcf_cagr=None,
    moat="wide",
    next_earnings_days=None,
    days_since_earnings=None,
    last_surprise_pct=None,
    post_earnings_drift=None,
    chg_90d=None,
    above_52w_low=float(clean["currentPrice"]) / float(clean["fiftyTwoWeekLow"]) - 1.0,
    is_etf=False,
    raw_info=clean,
    forward_pe=float(clean["forwardPE"]),
    enterprise_value=float(clean["enterpriseValue"]),
    ebitda_val=float(clean["ebitda"]),
)
_install_stub(clean)
got = fetch("CLEAN", moat="wide")
# sanity: no field is NaN (a NaN would make == lie by always being False)
for fld, val in vars(got).items():
    if isinstance(val, float):
        check(f"clean.{fld} is finite", not math.isnan(val) and not math.isinf(val))
check("clean payload Fundamentals == pre-guard expectation (byte-identical)",
      got == expected)


print("\n" + "=" * 70)
print(f"  ALL {len(_PASSES)} CHECKS PASSED.")
print("=" * 70)
