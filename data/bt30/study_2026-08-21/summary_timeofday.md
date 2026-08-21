# TIME-OF-DAY / SEASONALITY family — summary

Data: SPY_60m 2023-09-25 to 2026-08-21 (RTH hourly, 5067 bars; IS = first 70% to ~2025-09, OOS = final 30%). ES_F_60m 2024-03-31 to 2026-08-21 (24h hourly, 13698 bars). Costs 1.5bp/side (+3bp sensitivity). Harness one-bar delay: a bar's close-to-close return is captured by holding pos on the prior bar; calendar masks are shifted with `want.shift(-1)` (deterministic calendar, causal). **On RTH-only data the 9:30 bar contains the overnight gap** (prev 16:00 close to 10:30); `hour_0930` is the SPY overnight proxy, and `dow_mon` includes the weekend gap.

Benchmarks: SPY 60m buy&hold total 78.29%, Sharpe 1.41 (IS 1.51 / OOS 1.18), maxDD -19.43%. ES 60m buy&hold total 44.51%, Sharpe 1.03 (IS 0.97 / OOS 1.18), maxDD -21.12%.

## Top-3 by IS Sharpe (>=15 IS trades; OOS untouched)

| config | IS Sharpe | OOS Sharpe | OOS CAGR | maxDD (OOS/full) | trades IS/OOS | p_boot | full Sharpe @3bp (survives?) |
|---|---|---|---|---|---|---|---|
| **dow_mon** (long Mondays incl. weekend gap) | 0.88 | 2.69 | 17.1% | -2.6% / -7.0% | 97/43 | 0.077 | 1.16 (YES) |
| dow_wed (long Wednesdays) | 0.86 | 0.80 | 4.6% | -5.7% / -5.7% | 103/46 | 0.357 | 0.62 (YES) |
| half_am (long prev-close to 12:30) | 0.75 | 1.16 | 14.2% | -8.5% / -22.2% | 505/217 | 0.470 | 0.27 (marginal) |

Survives-3bp column = full-sample Sharpe rerun at 3bp/side. dow_mon keeps Sharpe 1.16 at 3bp (one round-trip/week, ~19% exposure, full-sample Sharpe 1.39 vs B&H 1.41 with 1/3 the drawdown); half_am decays 0.87 to 0.27 (daily round-trip) and effectively dies; dow_wed is marginal. No top-3 p_boot clears 0.05 — best is dow_mon at 0.077, i.e. ~8% of random same-shape placements match it, so even the winner is only weak evidence against the calendar-mining null.

## Confirm & translate checks for best (dow_mon)

| check | IS Sharpe | OOS Sharpe | full Sharpe | @3bp | p_boot | exposure |
|---|---|---|---|---|---|---|
| confirm_QQQ_dow_mon | 0.96 | 2.87 | 1.52 | 1.35 | 0.053 | 19% |
| translate_SPY30_dow_mon | 3.26 | 3.31 | 3.20 | 2.96 | 0.043 | 20% |

QQQ confirms cleanly (same construction, stronger everywhere, p 0.053, survives 3bp at 1.35). SPY_30m translate-check agrees in sign and is strong (full 3.20), but the 30m file spans only ~3 months (~12 Mondays) — directional confirmation only. OPEX overlay: `dow_mon_opexflat` is identical to `dow_mon` **by construction** (OPEX/quad-witching days are 3rd Fridays; a Monday-only config never touches them). Applied where it binds, `half_am_opexflat` improves IS 0.75->0.93 but *worsens* OOS 1.16->1.05 — the overlay does not generalize.

## ES overnight drift, documented honestly

| cost | IS Sharpe | OOS Sharpe | full Sharpe | full CAGR | p_boot |
|---|---|---|---|---|---|
| 0bp (gross) | 1.03 | 1.08 | 1.04 | 10.3% | 0.247 |
| 1.5bp/side | 0.25 | 0.32 | 0.27 | 2.2% | 0.813 |
| 3bp/side | — | — | -0.50 | -5.3% | — |

The attribution anomaly is real: gross, the overnight window (bars 18:00-08:00 = 17:00->09:00 move, incl. weekend gaps, 65% of bars) earns Sharpe 1.04 = the entirety of ES buy&hold's 1.03, while es_rth_only is negative even gross of its edge (net IS -0.40 / OOS -0.19); IS mean returns are 0.27bp/bar overnight vs 0.13bp/bar RTH. But one round-trip per night costs 3bp/day at 1.5bp/side (~7.5%/yr) against ~4bp/night of gross drift: net Sharpe collapses to 0.27 (p_boot 0.813) and goes to -0.50 at 3bp/side. Same picture on SPY: the overnight-gap proxy `hour_0930` is Sharpe 1.53 gross (p 0.047, beats B&H 1.41 with 14% exposure) but 0.85 at 1.5bp and 0.16 at 3bp. **The overnight drift is an attribution fact, not a tradeable daily-round-trip edge at these costs.**

## All configs (full-sample Sharpe at 1.5bp / at 3bp; IS/OOS at 1.5bp)

| config | IS | OOS | full | @3bp | p_boot | expo |
|---|---|---|---|---|---|---|
| hour_0930 | 0.62 | 1.46 | 0.85 | 0.16 | 0.290 | 14% |
| hour_1030 | -2.00 | -1.79 | -1.92 | -3.58 | 1.000 | 14% |
| hour_1130 | -1.07 | -1.69 | -1.25 | -3.11 | 1.000 | 14% |
| hour_1230 | -1.16 | -2.23 | -1.36 | -2.85 | 1.000 | 14% |
| hour_1330 | -1.11 | -1.37 | -1.16 | -2.98 | 1.000 | 14% |
| hour_1430 | -1.84 | -3.73 | -2.37 | -4.41 | 1.000 | 14% |
| hour_1530 | -1.50 | -3.96 | -2.07 | -4.20 | 1.000 | 14% |
| half_am | 0.75 | 1.16 | 0.87 | 0.27 | 0.470 | 43% |
| half_pm | -0.14 | -2.02 | -0.58 | -1.50 | 1.000 | 57% |
| dow_mon | 0.88 | 2.69 | 1.39 | 1.16 | 0.077 | 19% |
| dow_tue | 0.25 | -0.15 | 0.13 | -0.11 | 0.853 | 21% |
| dow_wed | 0.86 | 0.80 | 0.84 | 0.62 | 0.357 | 20% |
| dow_thu | 0.09 | -1.24 | -0.29 | -0.51 | 0.977 | 20% |
| dow_fri | 0.17 | -0.53 | -0.02 | -0.25 | 0.880 | 20% |
| tom_L3F2 | 0.07 | 1.19 | 0.38 | 0.33 | 0.767 | 24% |
| tom_L2F1 | -0.42 | 2.47 | 0.34 | 0.27 | 0.687 | 14% |
| tom_L4F3 | 0.06 | 1.87 | 0.53 | 0.48 | 0.730 | 33% |
| fhm_up_20 | -0.04 | -0.79 | -0.22 | -0.53 | 0.957 | 22% |
| fhm_up_40 | 0.60 | -0.34 | 0.38 | 0.22 | 0.563 | 9% |
| fhm_dn_20_short | 0.40 | -1.19 | -0.03 | -0.35 | 0.123 | 20% |
| fhm_dn_40_short | -0.19 | -0.28 | -0.21 | -0.38 | 0.360 | 8% |
| lasthour_gate_up | -2.14 | -4.74 | -2.60 | -4.19 | 1.000 | 8% |
| lasthour_gate_dn | 0.09 | -1.43 | -0.32 | -1.72 | 0.893 | 6% |
| es_overnight | 0.25 | 0.32 | 0.27 | -0.50 | 0.813 | 65% |
| es_rth_only | -0.40 | -0.19 | -0.35 | -0.95 | 0.940 | 30% |
| es_euro_am | -0.63 | 0.96 | -0.16 | -1.16 | 0.907 | 39% |
| dow_mon_opexflat | 0.88 | 2.69 | 1.39 | 1.16 | 0.077 | 19% |
| half_am_opexflat | 0.93 | 1.05 | 0.97 | 0.39 | 0.350 | 41% |

Notes: every intraday single hour of SPY is flat-to-negative net (10:30 and 14:30 worst); the last-hour effect is absent (hour_1530 IS -1.50, both gates negative); first-hour momentum has no OOS follow-through; turn-of-month is ~flat IS (0.06-0.07) and only looks good OOS (1.2-1.9) — it was not selectable on IS and its p_boots (0.69-0.77) say noise; IS mean per-bar returns show 9:30 (overnight) at +5.79bp vs -0.3 to +1.3bp for all pure intraday hours.

## Verdict (5 sentences)

1. The only time-of-day structure with genuine support in this sample is that **essentially all index return accrues overnight** — SPY's 9:30 gap-bar carries +5.8bp/bar IS while every pure intraday hour is ~0 — but at 1.5bp/side (and certainly 3bp) the daily-round-trip cost of harvesting it wipes it out, on ES (gross 1.04 -> net 0.27 -> -0.50 at 3bp) and SPY alike, so the famous overnight drift does NOT survive costs as a standalone strategy.
2. The family's best tradeable config is **dow_mon** (long Mondays incl. weekend gap): IS 0.88, OOS 2.69, OOS CAGR 17.2%, full-sample Sharpe 1.39 ~= buy&hold's 1.41 at 19% exposure and one-third the drawdown, and it survives 3bp (1.16) because it trades once a week.
3. It replicates on QQQ (OOS 2.87, 3bp 1.35) and directionally on the short SPY_30m sample, but its p_boot of 0.077 (QQQ 0.053) never clears 0.05 — for a family that is the textbook home of false positives, that reads as 'interesting, unproven', not as an edge.
4. Everything else fails on its own terms: intraday hours, last-hour and gated last-hour, first-hour momentum, and turn-of-month are all IS-flat, OOS-unconfirmed, cost-fragile (half_am 0.87 -> 0.27 at 3bp), or placebo-indistinguishable (all p_boot >= 0.12 except the overnight-gap constructions).
5. Recommended disposition: keep dow_mon and the overnight-gap decomposition as *risk-timing overlays* (when to hold exposure), reject the family as a source of standalone alpha; the OPEX flat rule is a no-op on the winner and does not generalize where it binds.

Files: /tmp/bt30/strat_timeofday.py (runner), /tmp/bt30/results_timeofday.json (all 35 saved configs incl. benchmarks/checks), /tmp/bt30/timeofday_raw.json.
