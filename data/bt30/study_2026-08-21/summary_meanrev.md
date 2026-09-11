# MEAN-REVERSION family — SPY 60m (2023-09 → 2026-08)

Harness: /tmp/bt30/harness.py (1-bar delay, 1.5bp/side + 3bp sensitivity, IS=first 70% / OOS=last 30%, 300-draw placebo).
Benchmark SPY 60m buy&hold: **+78.3% total, Sharpe 1.41, maxDD −19.4%**. 32 grid configs tested (13 RSI incl. 1 short, 7 z-score, 6 IBS, 2 VWAP-fade, 2 VIX-gated) + 4 confirmation runs; full log in `results_meanrev.json`. All signals on closes; VIX gate lagged one day; tuning on IS only.

## Top-3 by IS Sharpe (eligible: >=15 IS trades)

| rank | config | IS Sharpe | OOS Sharpe | OOS CAGR | maxDD (full / OOS) | trades (full) | p_boot | 3bp full Sharpe (survives?) |
|---|---|---|---|---|---|---|---|---|
| 1 | `rsi2_e10_x70_k14_vix20` (RSI2<10, exit>70 or 14 bars, prior-day VIX<20) | 1.49 | **0.41** | 2.4% | −4.9% / −4.0% | 190 | 0.187 | 0.85 (weakly) |
| 2 | `rsi2_e10_x70_k14` (same, ungated) | 1.47 | **1.54** | 12.4% | −10.4% / −4.0% | 229 | **0.060** | **1.21 (yes)** |
| 3 | `ibs_th10_h2d` (day-range IBS<=0.10 at 15:30 bar, hold 2 days) | 1.27 | **1.51** | 8.8% | −8.9% / −5.7% | 53 | 0.063 | **1.25 (yes)** |

Ranks 1 and 2 are the same strategy separated by 0.02 IS Sharpe — a statistical tie. The VIX<20 gate added +0.02 IS Sharpe but collapsed OOS (1.54 → 0.41) by filtering out the OOS window's most profitable dip entries: the gate is **IS-fit noise, rejected**. The ungated RSI2 dip-buyer is the family's real product: ~25% exposure, full-sample Sharpe 1.49 vs 1.41 buy&hold with roughly half the drawdown (−10.4% vs −19.4%), though only 43% of buy&hold's total return.

## Confirmation runs (best config; ungated base also shown since rank-1/2 are tied)

| run | full Sharpe | OOS Sharpe | 3bp full Sharpe | p_boot | note |
|---|---|---|---|---|---|
| `rsi2..._vix20` QQQ 60m | 0.65 | 1.65 | 0.43 | 0.497 | gate hurts here too |
| `rsi2..._vix20` SPY 30m | −0.61 | −2.30 | −1.21 | 0.660 | only ~59 sessions |
| `rsi2_e10_x70_k14` QQQ 60m | 1.01 | 2.12 | 0.80 | 0.240 | positive, weaker than SPY |
| `rsi2_e10_x70_k14` SPY 30m | −0.19 | −2.30 | −0.79 | 0.557 | **translate-check FAILS** (33 trades, ~59 sessions, May–Aug 2026 only) |

## Sub-family post-mortem

- **RSI**: deepest oversold (RSI2<10) is the only robust cell; shallower entries (20/30) churn and die at 3bp (0.21–0.72). The **mandated short-side variant** (`rsi2s_e90_x30_k14`, fade RSI2>90) lost money everywhere: IS −1.23, OOS −2.25, 3bp −1.83, p=0.93 — shorting overbought on a bull-tape index is a documented failure.
- **Z-score**: mediocre and rank-unstable (best IS 0.76; deep thresholds had negative IS but strong OOS — noise, not signal). Dominated by RSI2.
- **IBS**: running-IBS<=0.10 at the last bar with a 2-day hold is the quiet winner — 80% IS hit rate, nearly cost-immune (53 trades), p=0.063. The classic **lagged** variant (enter next day's first-bar close) is OOS-negative (−0.54/−0.61): the edge requires executing at/near the closing bar.
- **VWAP fade**: negative in IS and OOS at both depths (best full −0.18, 3bp −0.59) — consistent with this project's earlier rejection of the VWAP-reclaim tell; honestly tested, honestly dead.
- **3bp sensitivity**: mean reversion here does *not* uniformly die at 3bp — the two survivors keep Sharpe 1.21–1.25 because entries are rare and holds are multi-bar — but every high-frequency cell (RSI entries 20/30, K=7 exits, VWAP) degrades 0.3–0.5 Sharpe or goes negative.

## Verdict (5 sentences)

Hourly dip-buying on SPY genuinely reverts: the ungated RSI2<10 config posts IS 1.47 / OOS 1.54 Sharpe, survives 3bp costs (1.21), and its placebo p of 0.06 is suggestive though it misses the conventional 5% bar, so "promising, not proven" is the honest label. Its economics are risk-adjusted, not absolute — it captures ~43% of buy&hold's return at ~25% exposure and half the drawdown, i.e., it is a better *per-unit-risk* way to hold a bull tape, not a way to beat it. The IBS<=0.10 two-day hold independently confirms the same close-at-the-lows effect with near-zero cost sensitivity, and QQQ agrees directionally (full 1.01, OOS 2.12) though with a placebo p of 0.24. The failures are informative: shorts lose badly, the VWAP fade is dead at this horizon too, the extra-lag IBS variant dies (the fill must happen near the close), and the VIX<20 gate is IS overfit that halves OOS performance. The 30m translate-check is negative (−0.19 full, −2.30 OOS) on a tiny 59-session sample, so treat the edge as specific to the hourly horizon and the 2023–2026 sample until retested on longer 30m history.
