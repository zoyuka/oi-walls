# Adversarial verification report — three survivors of the 30m study

Verifier pass 2026-08-21. Code: `/tmp/bt30/verify.py` (attacks 1–9); raw numbers: `/tmp/bt30/verify_out.json`; machine verdicts: `/tmp/bt30/verdicts.json`. All reruns use the untouched shared harness semantics (1-bar delay, 1.5bp/side, IS = first 70%, OOS = last 30%). 167 configs were tested across the five families (34+35+26+31+41) to surface these three.

**Verdicts: A `rsi2_e10_x70_k14` WEAKENED · B Monday/weekend drift KILLED (recent-regime only) · C `vt_33_15` WEAKENED.**

---

## Attack 1 — Code audit for lookahead: CLEAN, with one attribution finding

- **Reproduction**: independently reimplemented all five position generators; harness.run reproduces every stored number exactly (A IS 1.47/OOS 1.54, IBS 1.27/1.51, dow_mon 0.88/2.69, ES-weekend 0.74/1.66, vt 1.74/1.08 full 1.54). Grid spot-checks also exact (e.g. `rsi2_e20_x70_k14` 1.06/1.17, `rsi2_e10_x60_k7` 0.84/1.42).
- **Causality truncation test** (recompute signal at bar t using only data ≤ t, 25 random bars): 0/25 mismatches for A (RSI state machine) and C (rv sizing). B's `want.shift(-1)` reads only the deterministic exchange calendar — benign, no price lookahead. VIX gates (`merge_asof`, `allow_exact_matches=False`) are strictly prior-day; not load-bearing for any survivor anyway.
- **Harness mechanics**: `pos.shift(1)` delay and cost timing (`costs.shift(1)` on `|Δpos|`) are correctly aligned; fills occur at the signal bar's own close (stated convention — realistic for MOC-style execution, flagged as the one optimistic assumption).
- **Execution-lag stress (+1 extra bar)**: A full 1.49→1.11, OOS 1.54→1.09 — degrades but survives an hour-late fill. IBS cousin 1.34→1.30. dow_mon full 1.39→0.41, OOS 2.69→**−0.42**: shifting the window one hour proves **100% of B's edge is the Fri-close→Mon-10:30 leg** (weekend gap + first hour). That is an attribution fact, not a leak (a Friday MOC order captures it), and it matches the overnight family's own decomposition. ES-weekend improves with the shift (1.02→1.67) — the drift sits late in the weekend window.

**No lookahead found in any winner's code path.**

## Attack 2 — Parameter plateau

**A (RSI2 neighborhood, 4×3×3 = 36 cells, OOS Sharpe):** min 0.78, median **1.31**, max 1.66; **100% of cells OOS > 0, 92% OOS > 1**; chosen cell (10,70,14) = 1.54, the 83rd percentile of its own neighborhood — a plateau, not a spike. This is A's single strongest piece of evidence.

| OOS Sharpe | x60 | x70 | x80 | (K=7/14/21 within each) |
|---|---|---|---|---|
| ent 5 | 1.55/1.41/1.41 | 1.66/1.61/1.61 | 1.32/1.30/1.00 | |
| ent 10 | 1.42/1.38/1.38 | 1.38/**1.54**/1.55 | 1.32/1.58/1.27 | |
| ent 15 | 1.13/1.12/1.12 | 1.24/1.30/1.33 | 0.94/1.09/0.78 | |
| ent 20 | 1.08/1.13/1.13 | 1.30/1.17/1.19 | 1.42/1.38/1.13 | |

**C (rv window × target, 12 cells):** full Sharpe range **1.45–1.56** (every cell ≥ bench 1.41), maxDD −11.6% to −15.9% (every cell better than bench −19.4%). Perfectly flat — no parameter luck. But note: **OOS Sharpe 0.72–1.17 in all 12 cells, every one below bench OOS 1.18.**

## Attack 3 — Sub-period stability (6 equal windows)

| SPY window | A_rsi2 | B_dowmon | C_vt | bench | C−bench |
|---|---|---|---|---|---|
| 2023-09..2024-03 | 2.53 | 2.92 | 3.34 | 3.17 | +0.17 |
| 2024-03..2024-09 | 2.51 | 0.55 | 0.96 | 1.07 | −0.11 |
| 2024-09..2025-03 | 0.42 | −1.33 | 0.55 | 0.58 | −0.03 |
| 2025-03..2025-09 (crash) | 1.44 | 1.55 | 1.58 | 1.22 | **+0.36** |
| 2025-09..2026-02 | 1.05 | 3.40 | 1.18 | 1.31 | −0.13 |
| 2026-02..2026-08 | 1.87 | 2.48 | 1.75 | 1.75 | 0.00 |

- **A: positive in all six windows**; excluding 2025-03-15..05-31 entirely, full Sharpe *rises* 1.49→1.59. Not a crash artifact. PASS.
- **B (dow_mon)**: 4/6 positive, one at −1.33; crash exclusion changes nothing (1.39→1.39). Not one-window — survives this attack (it dies in attack 5). ES-weekend windows: 0.09, 1.52, −0.80, **4.97**, 0.63, 3.59 — lumpy, half the sample flat-to-negative.
- **C**: the Sharpe edge over bench lives **entirely in the one crash window** (+0.36; every other window −0.13..+0.17). The DD numbers, not the Sharpe, are C's real content.

## Attack 4 — Top-trade concentration (remove 5 best)

| stream | n trades | full before→after | OOS before→after (top-5 OOS removed) | top-5 share of trade PnL |
|---|---|---|---|---|
| A_rsi2 | 229 | 1.49 → 1.03 | 1.54 → **0.43** | 34.3% |
| A_ibs cousin | 53 | 1.34 → 0.68 | 1.51 → **−0.62** | 51.0% |
| B_dowmon | 139 | 1.39 → 0.91 | 2.69 → **1.35** | 35.9% |
| B_es_weekend | 124 | 1.02 → 0.38 | 1.66 → **0.02** | **57.8%** |

A's best trades are the Apr-2025 (+5.3%, +3.2%) and Mar-2026 rebounds; its OOS headline is carried by 5 trades. dow_mon is the *least* concentrated survivor (still 1.35 OOS after amputation). **ES-weekend is 5 weekends** (2025-05-09, 2026-06-12, 2025-10-10, 2025-03-21, 2026-03-20 = 58% of all PnL; OOS 0.02 without them) — its statistical standing dies here.

## Attack 5 — Deep-history stationarity (decisive for B)

**Monday close-to-close (Fri close→Mon close) by decade:**

| decade | GSPC mean bp | GSPC t-stat | GSPC ann Sharpe | SPY mean bp (1993+) | SPY t |
|---|---|---|---|---|---|
| 1950s | −12.8 | −3.24 | −1.02 | | |
| 1960s | **−15.8** | **−4.65** | −1.46 | | |
| 1970s | −12.0 | −2.84 | −0.89 | | |
| 1980s | −10.7 | −1.65 | −0.52 | | |
| 1990s | +11.9 | +2.68 | +0.84 | +12.0 | 2.07 |
| 2000s | −1.9 | −0.27 | −0.08 | +0.9 | 0.12 |
| 2010s | +0.2 | +0.04 | +0.01 | +1.5 | 0.35 |
| 2020s | +11.7 | +1.50 | +0.58 | +13.4 | 1.78 |

The Monday effect **flipped sign**: significantly negative for four straight decades (1950s–80s, the classic weekend effect), **dead zero for the two decades immediately preceding this sample** (2000s t −0.27, 2010s t +0.04), positive only in the 1990s and 2020s. Per the pre-registered rule, **B is labeled recent-regime only regardless of its 3y OOS.** A long-Monday strategy is a bet that the post-2020 regime persists, against 70 years of precedent that it doesn't.

**A's analog (daily RSI2<10 dip-buy, exit >70 or 14d, 1.5bp) on GSPC:** 1950s −0.05, 1960s −0.85, 1970s −0.57, 1980s +0.16, **1990s +1.05, 2000s +0.43, 2010s +0.35, 2020s +0.61** (SPY 1993+: 1.46/0.48/0.38/0.71). Verified: the phenomenon behind A has been positive for four consecutive decades post-1990 — regime-dependent on the century view, but the supporting regime is 35 years old, vs 5 years for B's.

## Attack 6 — Cost curve (bp/side)

| stream | 1.5bp full/OOS | 3bp full/OOS | 5bp full/OOS | full-sample breakeven |
|---|---|---|---|---|
| A_rsi2 | 1.49 / 1.54 | 1.21 / 1.19 | 0.84 / 0.74 | ≈9bp (0.09 @9bp, −0.27 @11bp) |
| A_ibs | 1.34 / 1.51 | 1.25 / 1.41 | 1.13 / 1.26 | far above 5bp |
| B_dowmon | 1.39 / 2.69 | 1.16 / 2.45 | 0.86 / 2.13 | ≈10–11bp (−0.05 @11bp) |
| B_es_weekend | 1.02 / 1.66 | 0.75 / 1.39 | 0.38 / 1.05 | ≈7bp (0.02 @7bp) |

Nobody dies inside the mandated {1.5,3,5} range; ES-weekend is the closest to the edge.

## Attack 7 — Overlap, correlation, combined portfolio

- Daily-return correlations (SPY window): **A–B 0.121**, A–C 0.506, B–C 0.398. dow_mon vs ES-weekend: **0.603** (0.594 on Mondays only) — confirmed the same phenomenon; the "two datasets" are one bet, not two confirmations.
- **A ∩ IBS: 96% of IBS trades overlap an A trade** (bar-Jaccard 0.132 — IBS just holds longer; daily corr 0.445). The IBS "cousin confirmation" is substantially the same trades, not independent evidence.
- **Combined A+B+C portfolio** (equal-risk weights from IS vols only: A 0.31 / B 0.46 / C 0.22, daily basis): **full Sharpe 2.03 vs bench 1.40; IS 1.89 vs 1.48; OOS 2.34 vs 1.22; maxDD full −6.8% vs −19.0%, OOS −1.9% vs −9.1%** — at 6.0% ann vol vs bench 15.0% (a low-vol stream; Sharpe/DD are the comparable numbers). Caveat: 46% of its risk sits in the killed candidate B.
- **A+C only (post-kill portfolio)**: full 1.76, OOS **1.44 vs bench 1.22**, OOS maxDD **−3.8% vs −9.1%**.

## Attack 8 — Multiplicity haircut (167 configs tested; nulls matched on trade count/length, with costs; 200 experiments)

| bar | A_rsi2 (OOS 1.54) | B_dowmon (OOS 2.69) | B_es_wknd (OOS 1.66) |
|---|---|---|---|
| E[max OOS of 167 random] (p95) | 2.73 (3.39) | 2.79 (3.50) | 2.90 (3.65) |
| P(null max ≥ candidate) | 1.00 | 0.555 | 1.00 |
| Pipeline-faithful bar: E[max OOS among top-15-by-IS] (= 5 families × top-3) | 1.83 | 1.90 | 1.97 |
| **P(pipeline null ≥ candidate)** | **0.70** | **0.06** | **0.70** |
| P(OOS of single IS-best null ≥ candidate) | 0.075 | 0.01 | 0.095 |

Under the study's actual selection process, an OOS Sharpe of ~1.8–2.0 is *expected* from pure chance on this tape. **A's 1.54 and ES-weekend's 1.66 do not clear the bar (P=0.70); dow_mon's 2.69 is the only number that does (P=0.06).** A's rescue is attack 2 (an all-positive 36-cell plateau is not what single-config selection luck looks like) plus attack 5's 35-year analog — but its headline OOS figure per se carries no evidential weight after the haircut.

**C's null (is the timing real or just less exposure?):** constant pos = 0.93 (C's mean) gives full 1.41 / DD −18.3% vs vt 1.54 / **−12.5%** — in-sample the *timing* genuinely added Sharpe and 5.8pp of DD. But **OOS: vt 1.08 / −8.8% vs constant 1.18 / −9.0%** (bench 1.18 / −9.65%) — in the vol-event-free OOS year the timing added nothing and cost 0.10 Sharpe.

---

## Final verdicts

**A — RSI2 hourly dip-buy: WEAKENED.** Clean code, a real 36-cell OOS-positive plateau (median 1.31), positive in all 6 sub-windows, survives 5bp, and rides a 4-decade-old daily mean-reversion regime. But the OOS 1.54 headline is indistinguishable from 167-config selection (pipeline null mean 1.83, P=0.70), placebo p 0.06/0.075 never clears 5%, and 5 trades carry the OOS (0.43 without them). Deployable small; plan on Sharpe ≈ 1.0 (its minus-top-5 full number), not 1.5. The IBS cousin is the same trades (96% overlap), not confirmation.

**B — Monday/weekend drift: KILLED (recent-regime only).** The only candidate to beat the multiplicity bar (P=0.06), concentration-robust (OOS 1.35 minus top-5) and cost-robust (2.13 OOS at 5bp) — the 2020s phenomenon is real. But GSPC Mondays ran −10.7 to −15.8bp (t to −4.65) for 1950–89 and +0.2/−1.9bp (t ≈ 0) for 2000–2019: the effect has flipped sign twice and was flat for the 20 years before this sample, with no economic anchor. The ES-weekend leg fails independently: 5 weekends = 58% of PnL, OOS 0.02 without them, breakeven ≈7bp, corr 0.60 to dow_mon (same bet). Trade it only as an explicit, small, kill-switched regime bet; it is not a durable edge.

**C — Realized-vol targeting: WEAKENED.** Verified not-curve-fit (flat 12-cell plateau, all cells beat bench full-sample on both Sharpe and DD; beats a constant-exposure control in-sample: DD −12.5% vs −18.3%). But its entire Sharpe edge is one in-sample episode (Apr-2025 crash window, C−bench +0.36; ≈0 in the other five), and OOS it trailed bench Sharpe in **all 12 cells** (1.08 vs 1.18 chosen) with zero DD benefit over constant exposure (−8.8 vs −9.0). Keep as a drawdown-control overlay justified by external literature; book no Sharpe alpha from this sample.
