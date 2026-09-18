# OVERNIGHT / GAP family — summary

**Data/benchmark.** ES_F_60m (24h bars, 2024-03-31 → 2026-08-21; IS/OOS split at 2025-12-01). `bars_per_year(ES)=5796 = 23×252` — annualization sanity-checked, fine (buy&hold Sharpe ~1.0 is plausible for this window; sparse strategies are annualized on the same full calendar incl. flat bars, so Sharpes are comparable). **ES buy&hold benchmark: IS Sharpe 0.97 / OOS 1.18 / full 1.03, CAGR 16.9%, maxDD −21.1%.**
24 tuned configs (cap 40), tuned on IS only; OOS touched once. All 31 runs (incl. benchmark + 6 robustness checks) in `results_overnight.json`.

**Timing conventions** (hourly grid, close-to-close PnL, one-bar delay): "entry 17:00" = first pos bar is the 16:00-labeled bar (enters at the 17:00 settle); "exit 10:00" = last pos bar 8:00-labeled (exits at the 10:00 close). The 9:30-open exit was bracketed by 9:00 (fully pre-open) and 10:00 exits. SPY gap = 9:30 bar's OPEN vs prior day's last close (opens/closes only); pos set on the 9:30 bar ⇒ entry realized at the **10:30** price per the one-bar delay; exit at the day close. VIX gate uses the prior day's close, strictly lagged.

## Top-3 by IS Sharpe (≥15 IS trades)

| # | Config | IS Shp | OOS Shp | OOS CAGR | OOS maxDD | Trades IS/OOS | p_boot | Survives 3bp/side? |
|---|--------|-------:|--------:|---------:|----------:|:-------------:|-------:|:--|
| 1 | **GAP_SPY_dn0.3_long (fade)** — gap ≤ −0.3%, long 10:30→close | 0.91 | **−1.09** | −5.1% | −7.0% | 90 / 47 | 0.617 | Nominally (full 3bp Shp 0.22) but OOS already negative → **no** |
| 2 | **GAP_SPY_up0.5_short (fade)** — gap ≥ +0.5%, short 10:30→close | 0.76 | 0.43 | +1.8% | −3.0% | 70 / 43 | **0.020** | Weakly (full 3bp Shp 0.37), but fails NQ OOS |
| 3 | **ON_ES_weekendONLY_e17_x10** — long Fri 17:00 settle → Mon 10:00 | 0.74 | **1.66** | +10.1% | −4.3% | 88 / 36 | 0.137 | **Yes** (full 3bp Shp 0.75, CAGR 4.2%) |

## Robustness checks (best + rest of top-3; no tuning)

| Check | IS Shp | OOS Shp | full Shp | full 3bp Shp | p_boot |
|---|---:|---:|---:|---:|---:|
| **Best (#1) on NQ** (gap dn0.3 fade) | 0.62 | −0.84 | 0.27 | 0.07 | 0.477 |
| **Best (#1) on ES_30m** (translated; **~2.4-month window, 8 trades — noise**) | −4.60 | −6.66 | −4.72 | −5.04 | 0.993 |
| #2 on NQ (gap up0.5 fade) | 0.81 | **−1.51** | 0.08 | −0.18 | 0.347 |
| #2 on ES_30m (7 trades — noise) | −5.52 | 0.00 | −4.61 | −5.02 | 0.990 |
| #3 on NQ (weekend hold) | **1.08** | **1.60** | 1.24 | **1.03** | 0.067 |
| #3 on ES_30m (10 weekends — tiny but directionally +) | 5.77 | 3.34 | 5.15 | 4.86 | 0.007 |

NQ **confirms the IS sign but replicates the OOS failure** of the best-by-IS config (#1), **rejects** #2 OOS, and **cleanly confirms #3** (weekend hold) in both halves. The ES_30m file covers only Jun–Aug 2026, so its gap checks (6–8 trades) are statistically worthless; the weekend translate-check at least agrees in direction.

## The 3bp cost sensitivity is decisive (headline result)

Overnight-drift configs churn a round trip per night (~250 turns/yr). The per-night edge is only ~+2–5bp net at 1.5bp/side (e.g. e17→x9: avg trade +2.4bp IS, +4.8bp OOS, 56% hit), so doubling costs to 3bp/side (6bp round trip) **flips every daily-churn config negative**: family-1 full-sample Sharpes go 0.41–0.56 → **−0.53…−0.03**; the VIX-gated variants were already negative IS (gating away VIX>20 removed the Aug-2024/spring-2025 payoff nights, IS Shp −0.57/−0.21). Placebo p-values for daily overnight configs are 0.66–0.80 — their timing is indistinguishable from random placement of same-length holds. Only the **weekend hold** (one round trip per week, ~2.6 days held per turn) keeps a positive full-sample Sharpe at 3bp on both ES (0.75) and NQ (1.03).

## Gap priors at this horizon

The daily-bar prior (down-gaps 0.3–0.5% close in the gap direction 59%) does **not** survive the tradable one-bar-delay entry: at the 10:30 entry, gap-**follow** loses badly in IS on both sides (IS Shp −0.9…−1.4) and gap-**fade** wins IS (+0.64…+0.91) — the continuation the prior measures is spent in the first hour, and from 10:30 to the close gap days mean-reverted in-sample. The fade edge itself is fragile: down-gap fade collapsed OOS (−1.09), and only the up-gap fade has a low placebo p (0.02) while failing on NQ OOS. Adding the first-hour gap-and-go confirmation (family 3) did not help (IS ≤0.34 down-side, negative up-side, OOS negative).

## Verdict (5 sentences)

The famous overnight drift is real but un-harvestable here at daily churn: it exists gross (~55–58% of weekday nights up, +2–5bp/night net at 1.5bp/side, full Sharpe ~0.4–0.5 < buy&hold's 1.03), yet the 3bp sensitivity kills every nightly config (Sharpe −0.5…0.0) and placebo p≈0.7 says the timing adds nothing over random exposure. The one deployable expression is concentrating the same exposure into the Friday-17:00→Monday-10:00 weekend hold, which turns over 5× less: IS 0.74 → OOS 1.66 on ES, confirmed out-of-family on NQ (IS 1.08 / OOS 1.60), survives 3bp on both (0.75 / 1.03), though its placebo p (0.14 ES / 0.07 NQ) clears no formal 5% bar. The best-by-IS config, fading 0.3% down-gaps on SPY from 10:30, is an in-sample artifact: OOS −1.09 with NQ replicating the failure. Gap-follow — the 59%-continuation prior — is outright unprofitable once you can only enter at 10:30; whatever continuation exists is spent in the first hour. Net: nothing in this family beats ES buy-and-hold after costs; the weekend hold is the only candidate worth carrying forward, as a cost-efficient partial-exposure substitute rather than an alpha claim.
