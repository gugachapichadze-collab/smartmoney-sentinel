#!/usr/bin/env python3
"""Single-source (validation-degraded) flag — pure-helper tests.

No network, no journal.db: exercises _degraded_tickers / _single_source_lines
directly on synthetic raw_info dicts that mirror exactly what datasource.fetch
stamps. Proves: right names listed, dual/discordance/ETF excluded, deduped,
order preserved, and SILENT when every name was dual-sourced.
"""
import sentinel

_PASSES = []
def check(label, cond):
    _PASSES.append(cond)
    print(f"  {'PASS' if cond else 'FAIL'}: {label}")
    assert cond, label

# raw_info shapes exactly as datasource.fetch writes them
DUAL    = {"data_source_note": "dual-source agree"}
NOKEY   = {"data_source_note": "single-source (no FMP key)"}
FMPFAIL = {"data_source_note": "single-source (FMP failed: 402 paywall)"}
DISCORD = {"data_discordance": "price: Y=100 vs FMP=80 (gap 20%)"}  # dual-sourced
ETF     = {}                                                        # note never set

print("Group 1 — classification picks the right names, excludes the rest")
got = sentinel._degraded_tickers([
    ("AAPL", DUAL), ("SPOT", FMPFAIL), ("CVS", NOKEY),
    ("KO", DISCORD), ("VOO", ETF),
])
check("lists exactly the two single-source names, in order", got == ["SPOT", "CVS"])
check("dual-source agree excluded", "AAPL" not in got)
check("discordance (was dual-sourced) excluded", "KO" not in got)
check("ETF (note unset) excluded", "VOO" not in got)

print("Group 2 — dedupe (a name both held and watched)")
got = sentinel._degraded_tickers([("CVS", NOKEY), ("CVS", NOKEY), ("SPOT", FMPFAIL)])
check("CVS appears once", got == ["CVS", "SPOT"])

print("Group 3 — SILENT when all dual-sourced / no eligible names")
check("all dual -> no degraded names",
      sentinel._degraded_tickers([("AAPL", DUAL), ("KO", DISCORD), ("VOO", ETF)]) == [])
check("empty degraded -> no brief lines", sentinel._single_source_lines([]) == [])
check("empty fetched -> no brief lines",
      sentinel._single_source_lines(sentinel._degraded_tickers([])) == [])

print("Group 4 — formatting matches the spec exactly")
lines = sentinel._single_source_lines(["SPOT", "CVS"])
check("emits one line", len(lines) == 1)
check("text matches the agreed wording",
      lines[0] == "\nSingle-source today (no cross-check): SPOT, CVS.")

print("Group 5 — end-to-end: fetched list -> brief lines")
fetched = [("AAPL", DUAL), ("SPOT", FMPFAIL), ("CVS", NOKEY), ("VOO", ETF)]
out = sentinel._single_source_lines(sentinel._degraded_tickers(fetched))
check("pipeline lists SPOT, CVS only",
      out == ["\nSingle-source today (no cross-check): SPOT, CVS."])

print(f"\nALL {len(_PASSES)} CHECKS PASSED.")
