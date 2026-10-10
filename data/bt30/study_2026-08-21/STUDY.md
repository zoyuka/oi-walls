# The 30-Minute Chart Study — what survived
*Aug 21, 2026 · 3y SPY/QQQ hourly + 2.4y ES/NQ 24h + 60 sessions true 30m · shared harness: next-bar execution, 1.5bp/side (3bp & 5bp sensitivity), IS/OOS 70/30, placebo bootstrap, ~167 configs across 5 families, adversarial verification pass*

## Verdict in one line
Trade rarely, long-only, **buy panic** (30m RSI(2) < 10), exit into strength (RSI > 70 or ~2 days), **size by calm** (scale down when realized vol is high) — and skip everything else we tested.

## The survivor — hourly/30m RSI(2) deep-dip buy (WEAKENED but real)
- Rule: long when RSI(2) on the bar closes < 10; exit when RSI(2) > 70 or after 14 bars. Long only. ~25% time in market, ~2–4 trades/month.
- IS Sharpe 1.47 → **OOS 1.54** (improved OOS — the only timing rule in the study that did), placebo p 0.06, survives 3bp costs (1.21).
- Robustness: **all 36 parameter-neighborhood cells OOS-positive** (median 1.31); daily analog positive in every decade since 1990; independent IBS≤0.10 cousin confirms (OOS 1.51).
- Haircuts you must respect: top-5 trades carry most of it (OOS 0.43 without them — the edge IS the rare crash-rebound, you must take every signal); multiplicity-adjusted expectation ≈ **Sharpe 1.0, not 1.5**; true-30m sample too short to certify (59 sessions).
- Character: roughly buy-and-hold's risk-adjusted return with ~half the drawdown at a quarter of the exposure — a discipline machine, not a money printer.

## The overlay — realized-vol targeting (no alpha, real DD control)
- pos = min(1, 15% / realized vol of last 33 bars). Full Sharpe 1.54 vs 1.41 benchmark, maxDD −12.5% vs −19.4%; flat 12/12 parameter surface; survives costs trivially.
- Verifier: its Sharpe "edge" concentrates in the Apr-2025 crash window and OOS it trailed benchmark — credit it **zero alpha**; keep it as position-sizing hygiene.
- Combined honest book (dip-buy + vol-sizing): OOS Sharpe 1.44 vs 1.22 buy-hold, OOS maxDD −3.8% vs −9.1%.

## Killed — with the number that killed each
- **Monday / weekend-drift long** (looked like the star: OOS 2.69, beat the multiplicity bar): GSPC Mondays averaged **−11 to −16 bp/wk with t ≈ −4.7 for 1950–89** and ~0 for 2000–2019 — the effect flipped sign for decades before our window; 5 weekends = 58% of the ES leg's PnL. Recent-regime artifact.
- **Every VIX gate** (level & percentile): IS 2.21 → **OOS −0.98**, replicated on QQQ. Textbook regime-fit.
- **Trend/breakout family**: best OOS 0.99 < buy-hold's 1.18 on the same bars; uniform IS→OOS decay = selection bias. (Donchian's one virtue: maxDD −8% — inferior to vol-sizing's route to the same thing.)
- **Nightly overnight-drift harvest on ES**: real gross (+2–5bp/night), **dead at any realistic cost** (3bp: Sharpe ≤ 0); placebo p≈0.7.
- **Gap plays at an honest entry** (10:30 next-bar): follow AND fade both lose — the continuation documented in our daily studies is spent in the first hour. (Consistent, not contradictory: the daily edge exists at the open, which a 30m-chart trader without the open print can't capture.)
- **Opening-range breakout, short-side anything, session-VWAP fade**: negative in-sample; VWAP-fade now rejected at two horizons.

## Honesty box
3 years of hourly data = one regime (strong bull + one crash + recovery). True 30m history is 60 sessions (Yahoo's cap). ~167 configs mean the expected best-by-luck OOS Sharpe was 1.8 — only results that cleared plateau/stationarity/concentration attacks are reported as real. Expressing the dip-buy through options adds the toll (spread + theta) the scanner measures; shares/micro-futures express it cleanly.
