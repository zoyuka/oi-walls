# VOLATILITY / REGIME family — SPY 60m (2023-09-25 → 2025-08, 5067 bars)

Harness: 1-bar delay, 1.5bp/side (+3bp rerun), IS/OOS 70/30, placebo p.
Benchmark SPY 60m buy&hold: **full +78.3% / Sharpe 1.41 / maxDD −19.43%**; IS Sharpe 1.51; OOS Sharpe 1.18, OOS maxDD −9.65%.
26 configs tested (cap 40): VIX-level gates (6), VIX 1y-percentile gates (3), realized-vol targeting (6), hourly-ATR expansion filters (4), daily-ATR band-break regimes (4), MA-trend combos (3). All VIX/daily features lagged one day; selection on IS only.

## Top-3 by IS Sharpe (eligible: >=15 IS trades, or >=30 exposure changes for always-in)

| rank | config | rule | IS Shp | OOS Shp | OOS CAGR | full maxDD (vs −19.4%) | trades / turnover | p_boot | 3bp Shp |
|---|---|---|---|---|---|---|---|---|---|
| 1 | vix_pct_40 | long iff prior-day VIX < its 252d 40th pctl | **2.21** | **−0.98** | −5.9% | −6.7% (**+12.7pp better**) | 39 tr, turn 76 | 0.200 | 1.22 (survives) |
| 2 | combo_vix_pct_40_macross | pct40 gate AND 20h MA>100h MA | 2.10 | −1.55 | −8.5% | −8.6% (+10.9pp) | 37 tr, turn 74 | 0.360 | 0.97 (survives) |
| 3 | band_dayflat | close < prevC−1xATR14(d,lag) → flat rest of day | 1.89 | 0.43 | +4.7% | −14.9% (+4.6pp) | 78 tr, turn 155 | 0.170 | 1.42 (survives) |

OOS maxDD: vix_pct_40 −6.7%, combo −8.6%, band_dayflat −14.9% vs bench OOS −9.65% (band_dayflat is WORSE than bench OOS).

**Next-ranked (context, IS Sharpe 1.63–1.82) — the robust block:** realized-vol targeting, always-in scaled.

| config | IS Shp | OOS Shp | OOS CAGR | full maxDD | OOS maxDD | turnover IS/OOS (Δexp) | 3bp Shp |
|---|---|---|---|---|---|---|---|
| vt_33_10 (min(1,10%/rv33h)) | 1.82 | 0.68 | +6.7% | −10.6% (+8.8pp) | −8.0% | 40 / 21 (1025 chg) | 1.44 |
| vt_33_15 (min(1,15%/rv33h)) | 1.74 | 1.08 | +13.9% | −12.5% (+7.0pp) | −8.8% | 17 / 9 (479 chg) | 1.53 |
| vt_66_15 | 1.63 | 1.13 | +14.7% | −14.5% (+7.0pp) | −8.5% | 9 / 4 | 1.47 |

Full-sample Sharpe: vt_33_15 **1.54 vs bench 1.41** at 2/3 the drawdown. (p_boot is degenerate for always-in scaled positions — one contiguous segment — so it is not informative for the vt rows; gate p_boots >=0.17 are all non-significant.)

## Cross-checks

- **Best (vix_pct_40) on QQQ 60m:** IS 2.08 / OOS −1.20, full 0.95, DD −13.8%, p 0.367 → replicates the IS strength AND the OOS failure; the gate is regime-fit, not asset-specific alpha.
- **Best on SPY 30m** (771 bars ≈ 3 months): IS −4.2 / OOS +3.1, p 0.98 → window too short, dominated by one vol spike; inconclusive.
- **vt_33_15 on QQQ 60m** (supplementary): full Sharpe 1.27 vs QQQ b&h 1.31 with maxDD −15.7% vs −23.2% → same "bench-like Sharpe, much smaller DD" character. On SPY 30m both strategy and bench just track the 3-month tape (DD −4.3% vs −4.5%); not contradictory.

## Verdict (5 sentences)

The IS-Sharpe leaders — VIX 1y-percentile gates and their MA combos (IS ≈ 2.1–2.2) — collapse out-of-sample (OOS Sharpe −1.0 to −1.6) and do so identically on QQQ, a textbook regime-fit: "hold only when VIX is in its calmest 40%" was optimal in the 2023–24 melt-up and wrong after the 2025 vol spike, so they are rejected despite their large drawdown improvements. The band-break day-flat rule keeps a positive OOS Sharpe (0.43) but trails buy-hold OOS and worsens OOS drawdown, and no gate's placebo p gets below 0.17. The one deployable finding is the classic realized-vol-targeted portfolio, vt_33_15 (pos = min(1, 15%/rv over 33 hourly bars)): full Sharpe 1.54 vs 1.41 for buy-hold with maxDD −12.5% vs −19.4% (+7.0pp), OOS Sharpe 1.08 vs bench 1.18 with OOS maxDD −8.8% vs −9.65%, surviving 3bp costs (Sharpe 1.53) on trivial turnover. QQQ confirms the same profile (bench-like Sharpe, drawdown cut from −23.2% to −15.7%), which is exactly the vol-managed-portfolio effect this family is supposed to harvest. Bottom line: nothing here beats buy-hold on raw OOS Sharpe, VIX-threshold/percentile gates should be treated as unvalidated, and realized-vol targeting is the family's genuine risk-adjusted improvement — roughly bench-level Sharpe at one-half to two-thirds of the drawdown.
