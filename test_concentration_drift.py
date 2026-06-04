#!/usr/bin/env python3
"""
test_concentration_drift.py — verify the CONCENTRATION-DRIFT review flag.

Checks two things against the stored holdings weights:
  1. No false positives — current holdings must NOT trip the flag.
  2. The flag DOES fire on a synthetic position forced above the 45% ceiling.

Uses stored weights only (no network); position_health takes weight as input.
Reads the real holdings.json when present, else falls back to the shipped
holdings.example.json (the real file is gitignored / never public).
"""
import json
from pathlib import Path

from engine import position_health, MAX_ANY_WEIGHT, Fundamentals

HERE = Path(__file__).resolve().parent
_real = HERE / "holdings.json"
_src = _real if _real.exists() else HERE / "holdings.example.json"
HOLDINGS = json.loads(_src.read_text())["holdings"]


def _stub(ticker, is_etf=False):
    """Minimal Fundamentals stub: ETF so hard_floors() is skipped (we only
    exercise the weight/concentration branch here, not quality floors)."""
    return Fundamentals(ticker=ticker, price=0.0, fcf_per_share=0.0,
                        roic=0.0, is_etf=True)


def has_drift(flags):
    return any(fl.startswith("CONCENTRATION-DRIFT") for fl in flags)


def has_conc(flags):
    # the per-ticker CONCENTRATION flag (not the DRIFT variant)
    return any(fl.startswith("CONCENTRATION:") for fl in flags)


print("=" * 70)
print(f"  CONCENTRATION-DRIFT test  (ceiling = {MAX_ANY_WEIGHT:.0%})")
print("=" * 70)

# 1. Live holdings — none should trip the drift flag.
print("\n  Stored holdings:")
tripped = []
for h in sorted(HOLDINGS, key=lambda x: x.get("weight", 0), reverse=True):
    tk = h["ticker"]
    w = h.get("weight", 0) or 0
    res = position_health(_stub(tk), w)
    drift = has_drift(res["flags"])
    mark = "  <-- DRIFT" if drift else ""
    print(f"    {tk:6} weight={w:6.1%}  drift={drift}{mark}")
    if drift:
        tripped.append(tk)

print(f"\n  -> stored positions tripping DRIFT: {tripped or 'NONE'}")
assert not tripped, f"FALSE POSITIVE on stored holdings: {tripped}"
print("  PASS: no false positives on stored holdings.")

# 2a. Dedupe — NVDA (per-ticker cap = 45%) forced to 60% must show ONLY the
#     per-ticker CONCENTRATION flag, NOT the DRIFT variant (already caught).
print("\n  Dedupe case (NVDA forced to 60%, cap already 45%):")
res = position_health(_stub("NVDA"), 0.60)
print(f"    flags = {res['flags']}")
assert has_conc(res["flags"]), "CONCENTRATION should fire for NVDA at 60%"
assert not has_drift(res["flags"]), "DRIFT must NOT double-fire when CONCENTRATION already caught it"
print("  PASS: NVDA shows only CONCENTRATION (no double-flag).")

# 2b. Drift safety net — a CORE ticker (cap = 1.0) drifting to 60% is NOT caught
#     by its loose per-ticker cap, so DRIFT must fire to flag the breach.
print("\n  Drift safety-net case (MSFT, CORE cap=100%, forced to 60%):")
res = position_health(_stub("MSFT"), 0.60)
print(f"    flags = {res['flags']}")
assert has_drift(res["flags"]), "DRIFT must fire for an un-capped position above 45%"
assert not has_conc(res["flags"]), "CONCENTRATION should NOT fire (cap=100% not breached)"
print("  PASS: un-capped drift is caught by CONCENTRATION-DRIFT.")

# 3. Boundary — exactly 45% must NOT fire (strict >).
res = position_health(_stub("TEST"), MAX_ANY_WEIGHT)
print(f"\n  Boundary at exactly {MAX_ANY_WEIGHT:.0%}: drift={has_drift(res['flags'])}")
assert not has_drift(res["flags"]), "boundary should be strict >, not >="
print("  PASS: exactly at ceiling does not fire (strict >).")

print("\n  ALL TESTS PASSED.")
