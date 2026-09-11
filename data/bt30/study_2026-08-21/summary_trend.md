# TREND / BREAKOUT family — SPY 60m (primary), 2023-09..2026-08

36 grid configs tested (18 MA cross LO/LS, 12 Donchian, 2 ORB, 4 daily-200dma-gated),
plus 2 confirmation runs and 3 buy&hold benchmarks; all through /tmp/bt30/harness.py
(1-bar delay, 1.5bp/side + 3bp sensitivity, IS = first 70%, OOS = last 30%).
Selection: top-3 by IS Sharpe among configs with >=15 IS trades; OOS reported untouched.
Benchmark SPY 60m buy&hold: full +78.3%, Sharpe 1.41 (IS 1.51 / OOS 1.18), maxDD -19.4%.

## Top-3 (by IS Sharpe, >=15 IS trades)

| config | IS Sharpe | OOS Sharpe | OOS CAGR | maxDD (OOS / full) | trades (IS/OOS) | p_boot | survives 3bp |
|---|---|---|---|---|---|---|---|
| don_55_55_LO (enter 55-bar-high close, exit 55-bar-low close, long-only) | 2.08 | 0.99 | 8.2% | -8.0% / -8.1% | 17 / 9 | 0.070 | yes (full 1.77 -> 1.74) |
| don_55_55_LO_gate200 (same + prev-day close>200dma gate, lagged 1d) | 1.88 | 0.69 | 5.3% | -7.3% / -7.3% | 17 / 9 | 0.130 | yes (1.53 -> 1.50) |
| don_20_55_LO | 1.84 | 0.31 | 2.7% | -14.0% / -14.0% | 23 / 15 | 0.217 | yes (1.42 -> 1.39) |

Notes: ma_10_200_LO / ma_20_200_LO / ma_5_200_LO posted higher-or-similar IS Sharpes
(2.34/2.14/2.09) but were excluded for <15 IS trades — and all three had NEGATIVE OOS
Sharpe, which vindicates the trade-count filter. Every long/short variant was worse than
its long-only twin (short legs uniformly lost money). Both ORB variants were outright
negative (IS Sharpe -0.56 LO / -0.98 LS on 274/449 trades; at 3bp full Sharpe -1.29 /
-1.74) — hourly opening-range breakout on SPY is a cost machine. The 200dma gate LOWERED
IS and OOS Sharpe on all four configs it was applied to (it only strips long exposure in
a mostly-bullish window).

## Best config cross-checks: don_55_55_LO

- QQQ 60m confirmation: full Sharpe 1.00, +37.6%, maxDD -14.9%, p_boot 0.41 — positive
  but BELOW QQQ buy&hold (Sharpe 1.31, +99.5%). Direction confirms, edge does not.
- SPY 30m translate-check (windows x2 = 110/110 bars, ~60 sessions): full Sharpe 0.49,
  +0.6%, only 2 trades — too small a sample to confirm or reject (30m buy&hold 0.76).

## Verdict (honest)

This family does not beat SPY buy&hold on a risk-adjusted basis out-of-sample: the best
config's OOS Sharpe of 0.99 is below buy&hold's 1.18 over the same bars, its OOS CAGR
(8.2%) is far below buy&hold's, and the IS-to-OOS decay (2.08 -> 0.99, worse for the
other two) is much steeper than buy&hold's own (1.51 -> 1.18) — the signature of
selection bias on a small trade sample. The best p_boot is 0.070, suggestive but not
significant at 5%, and the QQQ confirmation also lands below that index's own buy&hold,
so the apparent full-window outperformance (Sharpe 1.77 vs 1.41) should be treated as an
in-sample artifact. The one genuine virtue is defensive: don_55_55_LO cut max drawdown
to -8.1% versus -19.4% for buy&hold at only ~65% exposure, and with 25 round trips in
3 years it is insensitive to costs (1.77 -> 1.74 at 3bp). Opening-range breakout is
decisively dead at hourly granularity, short legs subtract value everywhere, and the
lagged 200dma trend gate helped nothing it touched. Conclusion: slow long-only Donchian
on hourly SPY works as a drawdown-control overlay, but as an alpha family
TREND/BREAKOUT fails here.
