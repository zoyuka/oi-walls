#!/usr/bin/env python3
"""Fetch OI walls, render charts, generate ToS studies + mobile dashboard.

Runs in GitHub Actions every ~10 minutes during market hours (plus an 8:15am ET
baseline right after the OCC open-interest refresh); output goes to docs/
(GitHub Pages).

Per ticker: OI walls + expected moves (ATM straddle) + VWAP + max pain +
estimated gamma-flip, on three pre-rendered timeframes (today 5m / week 15m /
3mo daily) toggled client-side with pure CSS. A small state.json persisted
between builds powers wall-volume momentum (Δ since previous build).
"""
import datetime as dt
import json
import math
import os
from zoneinfo import ZoneInfo
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BAND, N = 0.04, 3
PURPLE, YELLOW, GREEN, RED = "#a259ff", "#e0a800", "#26a69a", "#ef5350"
BLUE, TEAL, GRAY = "#4ea3ff", "#3fd0a4", "#6b6b73"
TICKERS = [("^SPX", "SPX"), ("SPY", "SPY"), ("QQQ", "QQQ")]
STOCKS = [("NVDA", "NVDA"), ("TSLA", "TSLA"), ("AAPL", "AAPL"), ("META", "META")]
STOCK_SET = {l for _, l in STOCKS}
SCAN_ONLY = ["AMZN", "MSFT", "GOOGL", "AMD", "NFLX", "PLTR"]
FUT = {"SPX": "ES=F", "SPY": "ES=F", "QQQ": "NQ=F"}
# same-day gap-fill base rates by |gap| bucket (SPX 2010–2026, n=4175):
# (lo, hi, % fully filled same day, % closed in gap's direction)
GAPFILL = [(0.3, 0.5, 39, 59), (0.5, 0.8, 31, 63), (0.8, 1.2, 27, 59), (1.2, 9e9, 18, 67)]

def gap_rates(g):
    for lo, hi, f, k in GAPFILL:
        if lo <= abs(g) < hi:
            return f, k
    return None, None

def wilder_rsi(close, period=2):
    """Exact formula the Aug-2026 bt30 study validated (strat_meanrev.py)."""
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    ru = up.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rd = dn.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    return 100.0 - 100.0 / (1.0 + ru / (rd + 1e-12))

def panic_scan(px_w, now_et):
    """Panic-dip signal on 30m bars. Survivor of the 167-config study: RSI(2)<10
    → long lean, exit RSI>70 or 14 bars (post-haircut Sharpe ≈1.0, profits
    concentrated in the scariest few signals). Only COMPLETED bars count."""
    out = {"mk": [], "last": None, "strength": 0, "rs": None}
    if px_w is None or len(px_w) < 30:
        return out
    w = px_w
    try:
        if (now_et - w.index[-1]).total_seconds() < 1700:   # forming 30m bar
            w = w.iloc[:-1]
    except Exception:
        pass
    if len(w) < 30:
        return out
    rs = wilder_rsi(w.Close, 2)
    def ets_(t):
        off = t.utcoffset()
        return int(t.timestamp() + (off.total_seconds() if off is not None else 0))
    out["mk"] = [[ets_(t), int(v < 5) + int(v < 10) + int(v < 15)]
                 for t, v in rs.items() if v == v and v < 15][-12:]
    lv = float(rs.iloc[-1])
    out.update(rs=rs, w=w, last=round(lv, 1),
               strength=int(lv < 5) + int(lv < 10) + int(lv < 15),
               raw_last_t=int(w.index[-1].timestamp()),
               last_px=float(w.Close.iloc[-1]))
    return out

def panic_update_log(label, scan, path="data/panic_log.json"):
    """Paper record: append new fires (completed-bar RSI2<10, 5h dedupe), fill
    entries at the NEXT bar's close (honest one-bar delay, like the backtest),
    resolve exits (RSI>70 or 14 bars). Returns (log, new_fire_or_None)."""
    import pandas as pd
    try:
        log = json.load(open(path))
    except Exception:
        log = []
    rs, w = scan.get("rs"), scan.get("w")
    if rs is None:
        return log, None
    for e in log:
        if e.get("label") != label:
            continue
        try:
            ent_t = pd.Timestamp(e["t"], unit="s", tz="UTC")
            after = rs[rs.index > ent_t]
            if not len(after):
                continue
            if e.get("px") is None:   # next-bar entry fill
                e["px"] = round(float(w.Close.asof(after.index[0])), 2)
            if e.get("status") != "open" or e.get("px") is None:
                continue
            hit = None
            for i, (t_, v_) in enumerate(after.items()):
                if i == 0:
                    continue
                if v_ > 70 or i >= 14:
                    hit = t_
                    break
            if hit is not None:
                xp = float(w.Close.asof(hit))
                e.update(status="done", exit_t=int(hit.timestamp()),
                         exit_px=round(xp, 2),
                         ret=round(100 * (xp / e["px"] - 1), 2))
        except Exception:
            continue
    new = None
    lv = scan.get("last")
    if lv is not None and lv < 10:
        rt = scan["raw_last_t"]
        if not [e for e in log if e.get("label") == label and rt - e.get("t", 0) < 5 * 3600]:
            new = {"t": rt, "label": label, "px": None, "sig_px": round(scan["last_px"], 2),
                   "rsi": lv, "cells": scan["strength"], "status": "open"}
            log.append(new)
    log = log[-400:]
    os.makedirs("data", exist_ok=True)
    json.dump(log, open(path, "w"))
    return log, new

def panic_push(new_fires, now_et):
    """One push per build when fresh fires exist — both channels, RTH-adjacent only."""
    if not new_fires:
        return
    m = now_et.hour * 60 + now_et.minute
    if now_et.weekday() >= 5 or not (420 <= m < 1200):
        return
    import urllib.request
    labs = ", ".join(f"{e['label']} RSI₂ {e['rsi']:g} ({e['cells']}/3)" for e in new_fires)
    title = "Levels — PANIC DIP (30m)"
    body = (f"{labs} — deep flush on the 30-minute chart. Backtested lean: long, exit "
            f"RSI>70 or ~2 days (post-haircut Sharpe ≈1.0; it only works taking every "
            f"signal). Paper-tracked — check the Week tab.")
    try:
        req = urllib.request.Request(
            "https://ntfy.sh",
            data=json.dumps({"topic": "levels-drk-56c5e740", "title": title,
                             "message": body, "priority": 4,
                             "tags": ["chart_with_downwards_trend"],
                             "click": "https://zoyuka.github.io/oi-walls/"}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        print("panic push sent:", labs)
    except Exception as e:
        print("panic ntfy failed:", e)
    try:
        from pywebpush import webpush
        if os.environ.get("VAPID_PRIVATE"):
            subs = (json.load(urllib.request.urlopen(
                "https://raw.githubusercontent.com/zoyuka/oi-walls/live/live.json",
                timeout=15)).get("wps") or [])
            for s in subs:
                try:
                    webpush(subscription_info=s,
                            data=json.dumps({"title": title, "body": body,
                                             "url": "https://zoyuka.github.io/oi-walls/"}),
                            vapid_private_key=os.environ["VAPID_PRIVATE"].strip(),
                            vapid_claims={"sub": "mailto:derekyz123@gmail.com"}, ttl=600)
                except Exception as e2:
                    print("panic webpush:", repr(e2)[:90])
    except Exception as e:
        print("panic webpush skipped:", e)

def fut_fetch():
    """Two weeks of 15m ES/NQ futures bars — the near-24h overnight tape,
    with pan-back history (tab opens on the last ~36h)."""
    out = {}
    for fs in ("ES=F", "NQ=F"):
        try:
            f = yf.Ticker(fs).history(period="1mo", interval="15m")
            if f is not None and len(f) > 10:
                out[fs] = f[f.index >= f.index[-1] - dt.timedelta(days=14)]
        except Exception:
            pass
    return out

def fmt_strike(x): return str(int(x)) if x == int(x) else str(x)

def fmt_k(v):
    v = int(v)
    if v >= 1_000_000: return f"{v/1e6:.1f}M"
    if v >= 10_000: return f"{v/1e3:.0f}k"
    if v >= 1_000: return f"{v/1e3:.1f}k"
    return str(v)

def tos_symbol(contract):
    for i, ch in enumerate(contract):
        if ch.isdigit():
            root, rest = contract[:i], contract[i:]
            break
    return f".{root}{rest[:6]}{rest[6]}{fmt_strike(int(rest[7:]) / 1000)}"

# ---------- options math ----------

def straddle_mid(ch, anchor):
    """ATM straddle mid at the strike nearest anchor — the market's expected move."""
    def mid(df):
        row = df.iloc[(df.strike - anchor).abs().argmin()]
        if row.bid > 0 and row.ask > 0:
            return (row.bid + row.ask) / 2
        return float(row.lastPrice)
    return mid(ch.calls) + mid(ch.puts)

def expected_moves(t, spot, ch0):
    """{'day': (anchor, em), 'week': (anchor, em)} — each None if not computable."""
    out = {"day": None, "week": None}
    try:
        closes = t.history(period="10d", interval="1d").Close
        # the day-EM anchor is the last COMPLETED close. Only skip the final row
        # when it is today's still-trading partial bar; after the close (and on
        # overnight/weekend builds, when no today-row exists) the last row IS the
        # completed close — iloc[-2] there would anchor two sessions back.
        now_e = dt.datetime.now(ZoneInfo("America/New_York"))
        intraday_partial = (len(closes) > 1 and closes.index[-1].date() == now_e.date()
                            and now_e.hour * 60 + now_e.minute < 960)
        prev_close = closes.iloc[-2] if intraday_partial else closes.iloc[-1]
        out["day"] = (float(prev_close), float(straddle_mid(ch0, prev_close)))
        today = dt.datetime.now(ZoneInfo("America/New_York")).date()
        fridays = [d for d in t.options
                   if dt.date.fromisoformat(d).weekday() == 4
                   and dt.date.fromisoformat(d) >= today]
        fri_closes = [(i, c) for i, c in closes.items() if i.weekday() == 4 and i.date() < today]
        if fridays and fri_closes:
            anchor = float(fri_closes[-1][1])
            chw = t.option_chain(fridays[0])
            out["week"] = (anchor, float(straddle_mid(chw, anchor)))
    except Exception:
        pass
    return out

def atm_spread_pct(ch, spot):
    """Bid/ask width of the ATM straddle as % of its mid — the toll to play."""
    try:
        c = ch.calls.iloc[(ch.calls.strike - spot).abs().argsort().iloc[0]]
        p = ch.puts.iloc[(ch.puts.strike - spot).abs().argsort().iloc[0]]
        mid = (c.bid + c.ask) / 2 + (p.bid + p.ask) / 2
        w = (c.ask - c.bid) + (p.ask - p.bid)
        if mid != mid or w != w or mid <= 0 or w < 0:
            return None
        return float(100 * w / mid)
    except Exception:
        return None

def front_volume(ch):
    try:
        return int(ch.calls.volume.fillna(0).sum() + ch.puts.volume.fillna(0).sum())
    except Exception:
        return None

def big_premium(ch, spot, exp, topn=3):
    """Per-contract premium concentration: where today's biggest dollars traded
    on this expiry (volume x mid x 100). Can't see buyer/seller, whale/crowd,
    or spread legs — but big dollars build big walls either way."""
    out = []
    try:
        for cp, df in (("C", ch.calls), ("P", ch.puts)):
            mid = (df.bid.fillna(0) + df.ask.fillna(0)) / 2
            mid = mid.where(mid > 0, df.lastPrice.fillna(0))
            prem = df.volume.fillna(0) * mid * 100
            for i in prem.nlargest(topn * 2).index:
                if prem[i] < 200_000:
                    continue
                r = df.loc[i]
                dist_ = 100 * (float(r.strike) / float(spot) - 1)
                # deep-ITM giants are financing structures (boxes/rolls), not positioning
                if (cp == "C" and dist_ < -8) or (cp == "P" and dist_ > 8):
                    continue
                out.append({"cp": cp, "k": float(r.strike), "exp": exp,
                            "prem": float(prem[i]), "vol": int(r.volume or 0),
                            "oi": int(r.openInterest or 0), "dist": dist_})
                if len([o for o in out if o["cp"] == cp]) >= topn:
                    break
    except Exception:
        pass
    return out

def chain_flow(ch):
    """$ premium traded per side on the front expiry (volume x mid x 100).
    UNSIGNED — traded, not necessarily bought; archived to test whether its
    extremes carry any signal before it's ever allowed to claim one."""
    try:
        def side(df):
            mid = (df.bid.fillna(0) + df.ask.fillna(0)) / 2
            mid = mid.where(mid > 0, df.lastPrice.fillna(0))
            return float((df.volume.fillna(0) * mid * 100).sum())
        return side(ch.calls), side(ch.puts)
    except Exception:
        return None, None

def next_earnings(t):
    try:
        c = t.calendar
        v = c.get("Earnings Date") if isinstance(c, dict) else None
        if v is not None:
            d0 = v[0] if isinstance(v, (list, tuple)) and len(v) else v
            s = str(d0)[:10]
            if len(s) == 10:
                return s
    except Exception:
        pass
    return None

def max_pain(ch, spot):
    """Strike minimizing total intrinsic value paid out (0DTE pin gravity)."""
    try:
        c = ch.calls[abs(ch.calls.strike/spot - 1) < 0.06]
        p = ch.puts[abs(ch.puts.strike/spot - 1) < 0.06]
        strikes = sorted(set(c.strike) | set(p.strike))
        best, bestv = None, None
        for s in strikes:
            pain = (c.openInterest.fillna(0) * (s - c.strike).clip(lower=0)).sum() \
                 + (p.openInterest.fillna(0) * (p.strike - s).clip(lower=0)).sum()
            if bestv is None or pain < bestv:
                best, bestv = s, pain
        return float(best) if best is not None else None
    except Exception:
        return None

def gamma_profile(ch, spot, T_years):
    """Estimated dealer gamma profile (long calls / short puts convention,
    BS gamma from Yahoo per-contract IV — crude, 0DTE chain only).
    Returns {'long': bool, 'flip': level|None, 'peak': level|None}:
    long gamma dampens moves and price gravitates toward the peak strike;
    short gamma amplifies; flip = zero-crossing."""
    try:
        T = max(T_years, 2 / 24 / 365)
        def rows(df):
            out = []
            for _, r in df.iterrows():
                iv, oi, K = r.impliedVolatility, r.openInterest, r.strike
                if iv != iv or iv < 0.01 or iv > 5: continue
                if oi != oi or oi <= 0: continue
                if abs(K/spot - 1) > 0.06: continue
                out.append((float(K), float(iv), float(oi)))
            return out
        cs, ps = rows(ch.calls), rows(ch.puts)
        if not cs or not ps:
            return None
        def net(S):
            g = 0.0
            for sign, side in ((+1, cs), (-1, ps)):
                for K, iv, oi in side:
                    d1 = (math.log(S/K) + 0.5*iv*iv*T) / (iv*math.sqrt(T))
                    g += sign * oi * math.exp(-d1*d1/2) / math.sqrt(2*math.pi) / (S*iv*math.sqrt(T))
            return g
        xs = [spot*(0.985 + 0.03*i/40) for i in range(41)]
        vs = [net(x) for x in xs]
        flip = None
        for a, b, va, vb in zip(xs, xs[1:], vs, vs[1:]):
            if (va <= 0 <= vb) or (vb <= 0 <= va):
                x0 = a + (b-a)*(0-va)/((vb-va) or 1e-9)
                if flip is None or abs(x0-spot) < abs(flip-spot):
                    flip = x0
        vmax = max(vs)
        peak = xs[vs.index(vmax)] if vmax > 0 else None
        return {"long": bool(net(spot) > 0),
                "flip": None if flip is None else float(flip),
                "peak": None if peak is None else float(peak)}
    except Exception:
        return None

def walls(ch, spot):
    def top(df):
        df = df[(df.strike > spot*(1-BAND)) & (df.strike < spot*(1+BAND))].copy()
        df["volume"] = df["volume"].fillna(0)
        df["openInterest"] = df["openInterest"].fillna(0)
        return list(df.sort_values("openInterest", ascending=False).head(N)
                    [["contractSymbol", "strike", "openInterest", "volume"]]
                    .itertuples(index=False))
    return top(ch.calls), top(ch.puts)

def study_text(label, exp, calls, puts):
    L = [f"# OI Walls (auto) — {label} exp {exp}", "declare upper;", ""]
    for i, c in enumerate(calls, 1):
        L += [f'input callSym{i} = "{tos_symbol(c.contractSymbol)}";', f"input callStrike{i} = {c.strike};"]
    for i, p in enumerate(puts, 1):
        L += [f'input putSym{i} = "{tos_symbol(p.contractSymbol)}";', f"input putStrike{i} = {p.strike};"]
    L.append("input showLabels = yes;\n")
    for i in range(1, N+1):
        L += [f"def c{i}OI = open_interest(callSym{i}, period = AggregationPeriod.DAY);",
              f"def p{i}OI = open_interest(putSym{i},  period = AggregationPeriod.DAY);",
              f"def c{i}V  = volume(callSym{i}, period = AggregationPeriod.DAY);",
              f"def p{i}V  = volume(putSym{i},  period = AggregationPeriod.DAY);"]
    L.append("")
    for i in range(1, N+1):
        L += [f"plot CallWall{i} = callStrike{i};",
              f"CallWall{i}.SetDefaultColor(CreateColor(162, 89, 255));",
              f"CallWall{i}.SetPaintingStrategy(PaintingStrategy.DASHES);",
              f"plot PutWall{i} = putStrike{i};",
              f"PutWall{i}.SetDefaultColor(CreateColor(224, 168, 0));",
              f"PutWall{i}.SetPaintingStrategy(PaintingStrategy.DASHES);"]
    L += ["", "def maxCallOI = Max(c1OI, Max(c2OI, c3OI));",
          "def maxPutOI  = Max(p1OI, Max(p2OI, p3OI));"]
    for i in range(1, N+1):
        L += [f"CallWall{i}.SetLineWeight(if c{i}OI == maxCallOI then 4 else 2);",
              f"PutWall{i}.SetLineWeight(if p{i}OI == maxPutOI then 4 else 2);"]
    L.append("")
    for i in range(1, N+1):
        L.append(f'AddLabel(showLabels, "C " + AsText(callStrike{i}, NumberFormat.TWO_DECIMAL_PLACES) + "  OI:" + c{i}OI + "  Vol:" + c{i}V, if c{i}V > c{i}OI then Color.WHITE else CreateColor(162, 89, 255));')
        L.append(f'AddLabel(showLabels, "P " + AsText(putStrike{i}, NumberFormat.TWO_DECIMAL_PLACES) + "  OI:" + p{i}OI + "  Vol:" + p{i}V, if p{i}V > p{i}OI then Color.WHITE else CreateColor(224, 168, 0));')
    return "\n".join(L)

# ---------- rendering ----------

def split_session(px):
    """(shown bars, index where today starts, prev close or None)."""
    last_day = px.index[-1].date()
    mask = [i.date() == last_day for i in px.index]
    day, prior = px[mask], px[[not m for m in mask]]
    prev_close = prior.Close.iloc[-1] if len(prior) else None
    if len(day) < 24 and len(prior):          # early session: keep context
        return px, len(prior), prev_close
    return day, 0, prev_close

def session_vwap(day):
    if day.Volume.sum() <= 0:
        return None
    tp = (day.High + day.Low + day.Close) / 3
    return (tp * day.Volume).cumsum() / day.Volume.cumsum()

def chart(path, px, spot, calls, puts, em=None, mode="day"):
    """Price action is the hero. Walls in range = clean lines with strike-only
    labels; out-of-range walls/EMs = small edge chips. mode: day|week|daily."""
    if mode == "day":
        shown, day_start, prev_close = split_session(px)
    else:
        shown, day_start, prev_close = px, 0, None
    fig, ax = plt.subplots(figsize=(7.6, 5.0), facecolor="#0d0d0f")
    ax.set_facecolor("#0d0d0f")
    n = len(shown)
    body_lw = max(1.4, min(4.5, 340 / max(n, 1)))
    for i, (_, r) in enumerate(shown.iterrows()):
        c = GREEN if r.Close >= r.Open else RED
        ax.plot([i, i], [r.Low, r.High], color=c, linewidth=max(0.5, body_lw*0.28), zorder=2)
        ax.plot([i, i], [r.Open, r.Close], color=c, linewidth=body_lw, solid_capstyle="butt", zorder=3)
    if mode == "week":
        prev = None
        for i, d in enumerate(shown.index.normalize()):
            if d != prev:
                ax.axvline(i, color="#26262b", linewidth=0.8, zorder=0)
                prev = d
    if mode == "day":
        if day_start:
            ax.axvline(day_start - 0.5, color="#26262b", linewidth=1.0, zorder=0)
        vwap = session_vwap(shown.iloc[day_start:])
        if vwap is not None:
            ax.plot(range(day_start, n), vwap, color=BLUE, linewidth=1.4, zorder=4)
    if mode == "daily":
        MASKR = dict(facecolor="#0d0d0f", edgecolor="none", pad=1.4)
        for w_, col_, lab_ in [(20, BLUE, "20d"), (50, GRAY, "50d")]:
            ma = shown.Close.rolling(w_).mean()
            ax.plot(range(n), ma.values, color=col_, linewidth=1.1,
                    linestyle=(0, (4, 2)), zorder=4)
            if ma.notna().any():
                ax.annotate(lab_, xy=(n - 1, ma.iloc[-1]), color=col_, fontsize=8.5,
                            ha="left", va="center", zorder=5, bbox=MASKR)

    plo, phi = shown.Low.min(), shown.High.max()
    if prev_close is not None:
        plo, phi = min(plo, prev_close), max(phi, prev_close)
    rng = (phi - plo) or spot * 0.005
    lo, hi = plo - rng*0.22, phi + rng*0.22
    ax.set_ylim(lo, hi)

    MASK = dict(facecolor="#0d0d0f", edgecolor="none", pad=1.6)
    MASKG = dict(facecolor="#0d0d0f", edgecolor="none", pad=1.4)
    if prev_close is not None and lo < prev_close < hi:
        ax.axhline(prev_close, color=GRAY, linewidth=0.8, linestyle=(0, (1, 2)), zorder=1)
        ax.annotate("prev close", xy=(0.005, prev_close), xycoords=("axes fraction", "data"),
                    color=GRAY, fontsize=8, va="bottom")
    em_out, em_in = [], []
    for tag, lv in (em or []):
        (em_in if lo < lv < hi else em_out).append((tag, lv))
    prev_ly = None
    for tag, lv in sorted(em_in, key=lambda t: t[1]):
        ax.axhline(lv, color="#8b8b93", linewidth=1.0, linestyle=(0, (5, 2, 1, 2)), zorder=1)
        ly = lv if prev_ly is None else max(lv, prev_ly + (hi - lo) * 0.05)
        prev_ly = ly
        ax.annotate(f"{tag} {lv:,.0f}", xy=(0.012, ly), xycoords=("axes fraction", "data"),
                    color="#9a9aa2", fontsize=8.5, va="bottom", zorder=5, bbox=MASKG)

    ws = sorted([("C", c, PURPLE) for c in calls] + [("P", p, YELLOW) for p in puts],
                key=lambda t: t[1].strike)
    maxoi = {"C": max((c.openInterest for c in calls), default=1),
             "P": max((p.openInterest for p in puts), default=1)}
    inside = [t for t in ws if lo < t[1].strike < hi]
    above  = [t for t in ws if t[1].strike >= hi]
    below  = [t for t in ws if t[1].strike <= lo]
    prev_ty, sep = None, (hi - lo) * 0.05
    for kind, w, col in inside:
        hot, big = w.volume > w.openInterest, w.openInterest == maxoi[kind]
        ax.axhline(w.strike, color=col, linewidth=(2.6 if big else 1.6) + (0.6 if hot else 0),
                   linestyle="-" if hot else (0, (4, 3)), alpha=0.95, zorder=1)
        ty = w.strike if prev_ty is None else max(w.strike, prev_ty + sep)
        prev_ty = ty
        ax.annotate(fmt_strike(w.strike), xy=(0.988, ty), xycoords=("axes fraction", "data"),
                    color="#ffffff" if hot else col, fontsize=10.5, ha="right", va="bottom",
                    fontweight="bold" if hot or big else "normal", zorder=5, bbox=MASK)
    for k, (kind, w, col) in enumerate(sorted(above, key=lambda t: t[1].strike)):
        hot = w.volume > w.openInterest
        ax.annotate(f"▲ {fmt_strike(w.strike)}  {100*(w.strike/spot-1):+.1f}%",
                    xy=(0.5, 0.988 - k*0.062), xycoords="axes fraction",
                    color="#ffffff" if hot else col, fontsize=9, ha="center", va="top",
                    fontweight="bold" if hot else "normal", zorder=6, bbox=MASK)
    for k, (kind, w, col) in enumerate(sorted(below, key=lambda t: -t[1].strike)):
        hot = w.volume > w.openInterest
        ax.annotate(f"▼ {fmt_strike(w.strike)}  {100*(w.strike/spot-1):+.1f}%",
                    xy=(0.5, 0.012 + k*0.062), xycoords="axes fraction",
                    color="#ffffff" if hot else col, fontsize=9, ha="center", va="bottom",
                    fontweight="bold" if hot else "normal", zorder=6, bbox=MASK)
    ups = [t for t in em_out if t[1] >= hi]; dns = [t for t in em_out if t[1] <= lo]
    for k, (tag, lv) in enumerate(sorted(ups, key=lambda t: t[1])):
        ax.annotate(f"▲ {tag} {lv:,.0f}  {100*(lv/spot-1):+.1f}%",
                    xy=(0.012, 0.988 - k*0.055), xycoords="axes fraction",
                    color="#9a9aa2", fontsize=8.5, ha="left", va="top", zorder=6, bbox=MASKG)
    for k, (tag, lv) in enumerate(sorted(dns, key=lambda t: -t[1])):
        ax.annotate(f"▼ {tag} {lv:,.0f}  {100*(lv/spot-1):+.1f}%",
                    xy=(0.012, 0.012 + k*0.055), xycoords="axes fraction",
                    color="#9a9aa2", fontsize=8.5, ha="left", va="bottom", zorder=6, bbox=MASKG)

    ax.axhline(spot, color=TEAL, linewidth=0.8, linestyle=":", zorder=2)
    ax.annotate(f"{spot:.2f}", xy=(0.005, spot), xycoords=("axes fraction", "data"),
                color=TEAL, fontsize=9.5, va="bottom", fontweight="bold", zorder=5, bbox=MASK)
    ax.tick_params(colors=GRAY, labelsize=9); ax.set_xticks([])
    for sp in ax.spines.values(): sp.set_color("#26262b")
    ax.yaxis.tick_right(); ax.margins(x=0.012)
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor="#0d0d0f")
    plt.close(fig)

def ladder_html(data):
    """Cross-ticker compare strip in HTML (crisp text): walls by % distance
    from spot on a shared scale, bar length = OI, white = vol > OI."""
    dists = [100*(w.strike/spot-1) for _, spot, calls, puts in data for w in calls+puts]
    m = max(0.6, max(abs(d) for d in dists)) * 1.18
    def pct(d): return (m - d) / (2*m) * 100
    cols = []
    for label, spot, calls, puts in data:
        colmax = max(w.openInterest for w in calls+puts) or 1
        ws = sorted([("c", c) for c in calls] + [("p", p) for p in puts],
                    key=lambda t: -t[1].strike)
        tops = [pct(100*(w.strike/spot-1)) for _, w in ws]
        ltops, prev = [], None
        for t in tops:
            lt = t if prev is None else max(t, prev + 9.5)
            ltops.append(lt); prev = lt
        ltops[-1] = min(ltops[-1], 91.5)
        for j in range(len(ltops) - 2, -1, -1):
            ltops[j] = min(ltops[j], ltops[j+1] - 9.5)
        items = []
        for (cls, w), top, ltop in zip(ws, tops, ltops):
            hot = " hot" if w.volume > w.openInterest else ""
            bw = 30 + 70*w.openInterest/colmax
            items.append(f'<div class="lw {cls}{hot}" style="top:{top:.1f}%">'
                         f'<i style="width:{bw:.0f}%"></i></div>'
                         f'<div class="ll {cls}{hot}" style="top:{max(ltop, 3):.1f}%">{fmt_strike(w.strike)}</div>')
        cols.append(f'<a class=lcol href="#{label}"><div class=lh><b>{label}</b> {spot:.2f}</div>'
                    f'<div class=lt><div class=lspot style="top:{pct(0):.1f}%"></div>{"".join(items)}</div></a>')
    return f'<div class=ladder>{"".join(cols)}</div>'


def _with_timeout(fn, seconds):
    """Run fn in a daemon thread with a hard budget; None on timeout/error.
    Keeps optional data fetches from ever stalling a build."""
    import threading
    box = {}
    def run():
        try:
            box["v"] = fn()
        except Exception:
            box["v"] = None
    t = threading.Thread(target=run, daemon=True)
    t.start(); t.join(seconds)
    return box.get("v")

MEGA = ["AAPL","MSFT","NVDA","AMZN","GOOGL","META","AVGO","TSLA","BRK-B","LLY",
        "JPM","V","UNH","XOM","MA","COST","HD","PG","NFLX","JNJ","ABBV","CRM",
        "BAC","ORCL","MRK","KO","CVX","AMD","PEP","WMT"]

def breadth_pulse():
    """Live intraday participation: mega-cap up/down count + RSP-SPY spread today."""
    try:
        q = yf.download(MEGA + ["SPY", "RSP"], period="2d", interval="1d",
                        progress=False, auto_adjust=True)["Close"]
        ch = q.iloc[-1] / q.iloc[-2] - 1
        ups = int((ch[MEGA] > 0).sum()); dns = int((ch[MEGA] < 0).sum())
        spread = float(ch["RSP"] - ch["SPY"]) * 100
        cls = "good" if (ups >= 20 and spread > 0) else ("warn" if (dns >= 20 or spread < -0.3) else "e")
        return (f'<span class="chip {cls}">now: megas {ups}↑ {dns}↓</span>'
                f'<span class="chip e">RSP−SPY today {spread:+.2f}%</span>')
    except Exception:
        return ""

def breadth_section(v, pulse=""):
    try:
        b = json.load(open("data/breadth.json"))
    except Exception:
        b = None
    if not b and not pulse:
        return ""
    chips, tabs = pulse, ""
    if b:
        adr = b["adv"] / max(b["dec"], 1)
        uvr = b["upvol"] / max(b["dnvol"], 1)
        chips += (
            f'<span class="chip {b["vcls"]}">{b["verdict"]}</span>'
            f'<span class="chip e">EOD A/D {b["adv"]}:{b["dec"]} ({adr:.1f}:1)</span>'
            f'<span class="chip e">up/dn vol {uvr:.1f}:1</span>'
            f'<span class="chip e">&gt;20d {b["pct20"]:.0f}% · &gt;50d {b["pct50"]:.0f}% · &gt;200d {b["pct200"]:.0f}%</span>'
            f'<span class="chip e">52w NH {b["nh"]} / NL {b["nl"]}</span>'
            f'<span class="chip e">McC {b["mcc_osc"]:+.0f} · sum {"↑" if b["nysi_rising"] else "↓"}</span>'
            f'<span class="chip e">RSP−SPY 20d {b["rsp_spy_20d"]:+.1f}%</span>')
        tabs = (f'<div class=tw>'
                f'<input type=radio name=tBR id=BR-0 class=t0 checked><label for="BR-0">A/D line</label>'
                f'<input type=radio name=tBR id=BR-1 class=t1><label for="BR-1">% &gt; MAs</label>'
                f'<input type=radio name=tBR id=BR-2 class=t2><label for="BR-2">McClellan</label>'
                f'<input type=radio name=tBR id=BR-3 class=t3><label for="BR-3">RSP/SPY</label>'
                f'<img class=i0 src="breadth_ad.png?v={v}" alt="A/D line">'
                f'<img class=i1 src="breadth_ma.png?v={v}" alt="percent above MAs">'
                f'<img class=i2 src="breadth_mcc.png?v={v}" alt="McClellan">'
                f'<img class=i3 src="breadth_rsp.png?v={v}" alt="RSP vs SPY">'
                f'</div>')
    date = f' · EOD data {b["date"]}' if b else ""
    return (f'<section id="BR"><h2>Breadth <span>how many stocks are coming along{date}</span></h2>'
            f'<div class=chips>{chips}</div>{tabs}</section>')

def gauge_strip(r, label="", W=0.015):
    """Thin strip overlaying every level in a ±1.5% window: the spatial glance."""
    spot = r["spot"]
    lo, hi = spot * (1 - W), spot * (1 + W)
    def pct(x): return max(1.5, min(98.5, (x - lo) / (hi - lo) * 100))
    ticks = []
    def tick(x, cls, title):
        if x is None: return
        ticks.append(f'<i class="gt {cls}" style="left:{pct(x):.1f}%" title="{title} {x:,.0f}"></i>')
    for w in r["puts"]:
        tick(w.strike, "p" + (" h" if w.volume > w.openInterest else ""), "put wall")
    for w in r["calls"]:
        tick(w.strike, "c" + (" h" if w.volume > w.openInterest else ""), "call wall")
    if r.get("dem"):
        tick(r["dem"][0], "g", "dEM low"); tick(r["dem"][1], "g", "dEM high")
    tick(r.get("pin"), "pn", "max pain")
    tick(r.get("vwap"), "vw", "VWAP")
    return (f'<div class=gtr>{"".join(ticks)}'
            f'<i class="gt px" id="dot-{label}" style="left:{pct(spot):.1f}%" title="price {spot:,.2f}"></i></div>')

def dists_text(r):
    spot = r["spot"]
    up = min((c.strike for c in r["calls"] if c.strike >= spot), default=None)
    dn = max((p.strike for p in r["puts"] if p.strike <= spot), default=None)
    return (f'{"▼" + format(100*(dn/spot-1), "+.1f") + "%" if dn else "▼—"} '
            f'{"▲" + format(100*(up/spot-1), "+.1f") + "%" if up else "▲—"}')

def session_autopsy(spx_daily, now_et, vwap_now=None):
    """After the close: what today actually did vs the morning map.
    Close-location base rates are backtested (2000–2026, n=6,694):
    closes at the low bounced 59% next day with bigger moves (±0.97% vs ±0.76%);
    closes at the high drifted flat-to-negative (52% up) — digestion."""
    try:
        import glob
        px5, spot = spx_daily.get("px5"), spx_daily.get("spot")
        if px5 is None or spot is None:
            return None
        tb = px5[[i.date() == now_et.date() for i in px5.index]]
        if len(tb) < 30:
            return None
        hi, lo, cl = float(tb.High.max()), float(tb.Low.min()), float(tb.Close.iloc[-1])
        clv = (cl - lo) / max(hi - lo, 1e-9)
        loc = ("at the low" if clv < .2 else "near the low" if clv < .4 else
               "mid-range" if clv < .6 else "near the high" if clv < .8 else "at the high")
        rate = {"at the low": "closes at the low bounced 59% of the time next day, with bigger "
                              "moves (±0.97% vs ±0.76% — the one strong close-location effect)",
                "near the low": "weak closes lean to a next-day bounce (55–59%) with extra movement",
                "mid-range": "mid-range closes carry no next-day lean",
                "near the high": "strong closes carry no next-day edge — digestion is the norm",
                "at the high": "closes at the high drifted flat-to-slightly-negative next day (52% up) "
                               "— digestion, not momentum"}[loc]
        try:  # measured override: the weak-close bounce edge does NOT survive into monthly OPEX
            nxt_ = now_et.date() + dt.timedelta(days=1)
            while nxt_.weekday() >= 5:
                nxt_ += dt.timedelta(days=1)
            d3_ = nxt_.replace(day=15)
            while d3_.weekday() != 4:
                d3_ += dt.timedelta(days=1)
            if nxt_ == d3_ and clv < 0.4:
                rate += (". <b class=w>Except tomorrow is monthly OPEX</b> — after a weak close "
                         "the bounce edge vanishes there: 45% up, median −0.1% (n=51 since 1990; "
                         "the strictest at-the-low cases ran 41% up, median −0.27%). Pinning eats "
                         "the drift — the best OPEX-day pop after this setup since 2010 was just "
                         "+1.5%, while the tails (1987, 2008, 2015) were macro-shock downs")
        except Exception:
            pass
        parts = []
        r_ = spx_daily.get("ret")
        rng_pct = 100 * (hi - lo) / cl
        parts.append(f"<b>Today's autopsy:</b> {'' if r_ is None else f'{r_:+.2f}%, '}closed {loc} "
                     f"of a {rng_pct:.2f}% range — {rate}.")
        try:
            mo = json.load(open(f"data/archive/{now_et:%Y-%m-%d}_open.json"))["tickers"]["SPX"]
            band = (mo.get("ems") or {}).get("day")
            if band:
                a, e = band
                out = abs(cl / a - 1) * 100 > (e / a * 100)
                held = tot = 0
                for f in sorted(glob.glob("data/archive/*_open.json"))[-21:]:
                    if f"{now_et:%Y-%m-%d}" in f:
                        continue
                    try:
                        b0 = (json.load(open(f))["tickers"]["SPX"].get("ems") or {}).get("day")
                        c0 = json.load(open(f.replace("_open", "")))["tickers"]["SPX"]["spot"]
                        if b0:
                            tot += 1
                            held += abs(c0 / b0[0] - 1) * 100 <= b0[1] / b0[0] * 100
                    except Exception:
                        pass
                tally = f" — it has held {held} of the last {tot} days" if tot >= 5 else ""
                parts.append(f"Closed {'<b class=w>outside</b>' if out else 'inside'} the morning "
                             f"±{100*e/a:.2f}% band{tally}.")
            wt = []
            for w in mo.get("calls", []):
                if lo <= w["k"] <= hi:
                    wt.append(f"{fmt_strike(w['k'])}C {'broke' if cl > w['k'] else 'held'}")
            for w in mo.get("puts", []):
                if lo <= w["k"] <= hi:
                    wt.append(f"{fmt_strike(w['k'])}P {'broke' if cl < w['k'] else 'held'}")
            parts.append("Morning walls traded: " + (", ".join(wt[:3]) + "." if wt
                         else "none — price never reached a mapped wall."))
        except Exception:
            pass
        if vwap_now:
            parts.append(f"Finished {'above' if cl >= vwap_now else 'below'} VWAP.")
        return " ".join(parts)
    except Exception:
        return None

def similar_days(px, spot, vix_now, now_et):
    """K-nearest historical analogs of today's state (SPX), from the committed
    daily CSVs. Matched on: today's move, intraday range, recent realized vol,
    VIX level & 5d trend, distance from 50d MA, distance from 20d high.
    Returns a collapsed <details> block of forward base rates — never a forecast."""
    try:
        import numpy as np
        import pandas as pd
        g = pd.read_csv("data/GSPC_daily.csv")
        g["Date"] = pd.to_datetime(g.Date, utc=True).dt.tz_localize(None).dt.normalize()
        vv = pd.read_csv("data/VIX_daily.csv")
        vv["Date"] = pd.to_datetime(vv.Date, utc=True).dt.tz_localize(None).dt.normalize()
        df = g[["Date", "High", "Low", "Close"]].merge(
            vv[["Date", "Close"]].rename(columns={"Close": "vix"}), on="Date")
        df = df[df.Date >= "1990-01-01"].reset_index(drop=True)
        # provisional "today" row from the live session, when today has real bars
        tb = px[[i.date() == now_et.date() for i in px.index]] if px is not None else []
        if len(tb) >= 3 and df.Date.iloc[-1].date() < now_et.date():
            df = pd.concat([df, pd.DataFrame([{
                "Date": pd.Timestamp(now_et.date()), "High": float(tb.High.max()),
                "Low": float(tb.Low.min()), "Close": float(spot),
                "vix": float(vix_now) if vix_now else float(df.vix.iloc[-1]),
            }])], ignore_index=True)
        df["ret1"] = df.Close.pct_change() * 100
        df["rng"] = 100 * (df.High - df.Low) / df.Close.shift(1)
        df["rlz5"] = df.ret1.abs().rolling(5).mean()
        df["vchg5"] = 100 * (df.vix / df.vix.shift(5) - 1)
        df["d50"] = 100 * (df.Close / df.Close.rolling(50).mean() - 1)
        df["hi20"] = 100 * (df.Close / df.Close.rolling(20).max() - 1)
        F = ["ret1", "rng", "rlz5", "vix", "vchg5", "d50", "hi20"]
        d = df.dropna(subset=F).reset_index(drop=True)
        if len(d) < 500:
            return ""
        tv = d.iloc[-1][F].astype(float)
        hist = d.iloc[:-22].copy()          # never match the trailing month (incl. today)
        mu, sd_ = hist[F].mean(), hist[F].std()
        z = ((hist[F] - mu) / sd_).astype(float)
        zt = ((tv - mu) / sd_).astype(float)
        hist["dist"] = np.sqrt(((z - zt) ** 2).sum(axis=1).astype(float))
        cl = d.Close.values
        for n, c in [(1, "f1"), (5, "f5"), (21, "f21")]:
            hist[c] = [100 * (cl[i + n] / cl[i] - 1) if i + n < len(cl) - 1 else np.nan
                       for i in hist.index]
        top = hist.nsmallest(30, "dist").dropna(subset=["f1", "f5", "f21"])
        base = hist.dropna(subset=["f21"])
        if len(top) < 15:
            return ""
        def line(c, nm, floor_):
            s, b = top[c], base[c]
            p25, p75, med, amed = (float(s.quantile(.25)), float(s.quantile(.75)),
                                   float(s.median()), float(b.median()))
            sc = max(abs(p25), abs(p75), abs(med), abs(amed), floor_) * 1.45
            P = lambda x: max(2.0, min(98.0, 50 + 50 * x / sc))
            return (f'<div class=arow><span class=alab>{nm}</span>'
                    f'<div class=astr><i class=az style="left:50%"></i>'
                    f'<i class=ab style="left:{min(P(p25), P(p75)):.1f}%;'
                    f'width:{abs(P(p75) - P(p25)):.1f}%" '
                    f'title="middle half of analog outcomes: {p25:+.1f}% … {p75:+.1f}%"></i>'
                    f'<i class="gt aa" style="left:{P(amed):.1f}%" '
                    f'title="all-days median {amed:+.2f}%"></i>'
                    f'<i class="gt am" style="left:{P(med):.1f}%" '
                    f'title="analog median {med:+.2f}%"></i></div>'
                    f'<span class=aval>{med:+.2f}% · {100*(s>0).mean():.0f}% up '
                    f'<span class=m>(all: {100*(b>0).mean():.0f}%)</span></span></div>')
        # the paths, not just the endpoints: worst dip / best pop within each horizon
        dds, ups = {1: [], 5: [], 21: []}, {1: [], 5: [], 21: []}
        ndip, round_trips, uw_days = 0, [], []
        for i in top.index:
            path = cl[i + 1:i + 22] / cl[i] - 1
            if len(path) < 21:
                continue
            cum = path * 100
            for h_ in (1, 5, 21):
                dds[h_].append(min(0.0, float(cum[:h_].min())))
                ups[h_].append(max(0.0, float(cum[:h_].max())))
            if float(cum.min()) <= -1.0:
                ndip += 1
                round_trips.append(float(cum[-1]) > 0)
                uw_days.append(int((cum < 0).sum()))
        def risk_row(h_, nm, floor_):
            d_, u_ = pd.Series(dds[h_]), pd.Series(ups[h_])
            mdd, bdd = float(d_.median()), float(d_.quantile(.10))
            mup, bup = float(u_.median()), float(u_.quantile(.90))
            sc = max(abs(bdd), abs(bup), floor_) * 1.15
            P = lambda x: max(2.0, min(98.0, 50 + 50 * x / sc))
            return (f'<div class=arow><span class=alab>{nm}</span>'
                    f'<div class=astr><i class=az style="left:50%"></i>'
                    f'<i class="ab dd" style="left:{P(mdd):.1f}%;width:{max(50 - P(mdd), 0.5):.1f}%" '
                    f'title="typical worst dip {mdd:+.1f}%"></i>'
                    f'<i class="ab uu" style="left:50%;width:{max(P(mup) - 50, 0.5):.1f}%" '
                    f'title="typical best pop {mup:+.1f}%"></i>'
                    f'<i class="gt bd" style="left:{P(bdd):.1f}%" title="bad-case dip {bdd:+.1f}% (worst 1 in 10)"></i>'
                    f'<i class="gt bu" style="left:{P(bup):.1f}%" title="strong-case pop {bup:+.1f}%"></i></div>'
                    f'<span class="aval rv">▼{mdd:+.1f}/{bdd:+.1f} · ▲{mup:+.1f}/{bup:+.1f}</span></div>')
        risk_html = ""
        if len(dds[21]) >= 15:
            rec = ""
            if ndip >= 5 and round_trips:
                rec = (f"Of the {ndip} analogs that dipped ≥1% inside the month, "
                       f"{100 * sum(round_trips) / len(round_trips):.0f}% still ended the month above "
                       f"water (median {int(pd.Series(uw_days).median())} days spent under today's close). ")
            risk_html = (
                f'<p class=m style="font-size:12px;margin:10px 0 2px"><b>The paths, not just the '
                f'endpoints</b> — how far it dipped and popped along the way (close-to-close; '
                f'intraday ran worse):</p>'
                + risk_row(1, "next day", 0.8) + risk_row(5, "next week", 2.0)
                + risk_row(21, "next month", 4.5)
                + f'<p class=m style="font-size:11.5px">{rec}▼ = deepest close below today\'s '
                  f'close, typical / worst-1-in-10 · ▲ = best pop, typical / best-1-in-10. '
                  f'Size so the typical dip is boring and the bad-case dip is survivable.</p>')
        quiet = ("quieter than normal" if top.f1.abs().median() < 0.85 * base.f1.abs().median()
                 else ("wilder than normal" if top.f1.abs().median() > 1.15 * base.f1.abs().median()
                       else "about normal size"))
        ex = " · ".join(f"{r.Date.date()} <span class=m>({r.f1:+.1f}% next)</span>"
                        for r in top.nsmallest(5, "dist").itertuples())
        mo_up = 100 * (top.f21 > 0).mean()
        head = (f"days like today — tomorrow: usually {quiet} · month out: {mo_up:.0f}% up "
                f"vs {100*(base.f21>0).mean():.0f}% normally")
        return (f'<details class=lg open><summary>{head}</summary>'
                f'<p class=m style="font-size:12px">The 30 most similar days since 1990 (of {len(hist):,}), '
                f'matched on today\'s move &amp; range, recent realized vol, VIX level &amp; trend, '
                f'trend position, and distance from the 20-day high. '
                f'<span style="color:#3fd0a4">teal</span> = days like today · '
                f'gray tick = ordinary days · band = middle half of outcomes.</p>'
                f'{line("f1", "next day", 0.8)}{line("f5", "next week", 1.8)}{line("f21", "next month", 4.0)}'
                f'{risk_html}'
                f'<p class=m style="font-size:12px">Closest matches: {ex}.</p>'
                f'<p class=m style="font-size:11.5px">Base rates from similar setups, not a forecast — '
                f'30 days is a small sample, and the month-out spread ran {top.f21.min():+.1f}% to '
                f'{top.f21.max():+.1f}%. If the numbers look like a coin flip, they are.</p></details>')
    except Exception as e:
        print("similar_days failed:", repr(e))
        return ""

def scanner_html(rows, by_und=None):
    """'Best tape' table: execution quality per name, never direction.
    Rank = spread cost ascending (the toll), volume as tiebreak."""
    if not rows:
        return ""
    by_und = by_und or {}
    def key(r):
        return (r["spr"] if r.get("spr") is not None else 99,
                -(r.get("vol") or 0))
    rows = sorted(rows, key=key)
    out = ['<section id=scan><h2>Best tape today <span class=al>single names · '
           'execution quality, not direction</span></h2>',
           '<table><tr><th>Name</th><th>EM d/w/m</th><th>Toll</th><th>Vol</th>'
           '<th>vs tape</th><th></th></tr>']
    for i, r in enumerate(rows):
        lab = r["lab"]
        name = f'<a href="#{lab}">{lab}</a>' if r.get("card") else lab
        ems_ = "/".join(f"{r[k]:.1f}" if r.get(k) is not None else "–"
                        for k in ("emd", "emw", "emm"))
        spr = f"{r['spr']:.1f}%" if r.get("spr") is not None else "–"
        vol = fmt_k(r["vol"]) if r.get("vol") else "–"
        tape = ""
        if r.get("emd") is not None and r.get("rlz5"):
            ratio = r["emd"] / max(r["rlz5"], 0.01)
            tape = ("cheap" if ratio < 0.85 else ("rich" if ratio > 1.5 else "fair"))
            tape = f'<span title="today\'s priced move vs 5-day realized ({ratio:.1f}x)">{tape}</span>'
        flags = []
        if r.get("earn") and r.get("moexp") and r["earn"] <= r["moexp"]:
            flags.append(f'<b class=w title="earnings {r["earn"]} — binary risk before monthly expiry">E</b>')
        h = by_und.get(lab)
        if h and abs(h.get("pnl", 0)) >= 5000:
            sgn = "+" if h["pnl"] > 0 else "−"
            flags.append(f'<span class=my title="your lifetime in {lab}: {h["n"]} closed, '
                         f'{sgn}${abs(h["pnl"]):,}">{sgn}</span>')
        cls = ' class=hot' if i < 3 else ''
        out.append(f'<tr{cls}><td>{name}</td><td>{ems_}%</td><td>{spr}</td>'
                   f'<td>{vol}</td><td>{tape}</td><td>{" ".join(flags)}</td></tr>')
    out.append('</table><p class=m style="font-size:11.5px;line-height:1.5">Toll = bid/ask width of the ATM straddle '
               '(what you lose to the spread just entering+exiting). EM = priced move per horizon. '
               'vs tape compares today\'s priced move with the last 5 days\' real moves. '
               'E = earnings lands before monthly expiry. <span class=my>+/−</span> = your own lifetime P&amp;L there. '
               'Tight toll + deep volume is what "good options to trade" actually means — direction is yours.</p></section>')
    return "".join(out)

def big_html(bigs):
    """Where today's biggest option dollars concentrated — a levels fact, not a
    direction signal. Research: the largest prints are mostly hedges, rolls and
    spread legs; informed traders hide in medium size. But big premium builds
    big walls, so these strikes matter mechanically regardless of intent."""
    if not bigs:
        return ""
    rows = []
    for b in bigs[:10]:
        exp_s = b["exp"][5:].replace("-", "/")
        fresh = b["vol"] > b["oi"]
        cls = "c" if b["cp"] == "C" else "p"
        rows.append(
            f'<tr{" class=hot" if fresh else ""}><td><b class={cls}>{b["lab"]} '
            f'{fmt_strike(b["k"])}{b["cp"]}</b> <span class=m>{exp_s}</span></td>'
            f'<td>${b["prem"]/1e6:.1f}M</td><td>{fmt_k(b["vol"])}</td>'
            f'<td>{(b["vol"]/b["oi"]) if b["oi"] else float("inf"):.1f}×'
            f'{" ⚡" if fresh else ""}</td><td>{b["dist"]:+.1f}%</td></tr>')
    return ('<details class=lg><summary>biggest dollars today — where option premium '
            'concentrated</summary>'
            '<table><tr><th>Contract</th><th>$ traded</th><th>Vol</th><th>V/OI</th>'
            '<th>Dist</th></tr>' + "".join(rows) + '</table>'
            '<p class=m style="font-size:11.5px">Premium traded per contract (volume × mid, '
            '15-min delayed) — this can\'t tell buyer from seller, one whale from a crowd, '
            'or a conviction bet from a hedge, roll or spread leg. The research says the '
            'biggest prints are usually <i>structures</i>, not directional bets, and informed '
            'traders hide in medium size. What big dollars reliably do is build walls: '
            '⚡ V/OI &gt; 1 means fresh positioning that becomes tomorrow\'s magnet/stall '
            'levels — which is exactly how the app already uses it. Archived daily; if '
            'concentration extremes ever test out as predictive, they\'ll earn a verdict line.</p>'
            '</details>')

def ladder_tiers(label, spot, side, lv_tabs, dem, day_bars, daily_px, bigs,
                 spy_pack=None, spy_spot=None):
    """Confluence ladder: every mapped level on one side of spot, clustered into
    zones and ranked by how much independent structure stacks there. side=-1 →
    supports below (the crash ladder), +1 → ceilings above. Ranking is density
    of evidence, NOT a measured hold-probability (see the card's footer)."""
    ff = (lambda x: f"{x:,.0f}") if spot >= 2000 else (lambda x: f"{x:,.2f}")
    ing = []          # (price, weight, short label)
    HZT = {"d": "0DTE", "w": "Fri", "m": "mo"}

    def wall_ing(packs, mapper=None, tag="", disc=1.0):
        good = ("p", "b") if side < 0 else ("c", "b")
        moi = max([e.get("oi", 0) for tb in ("d", "w", "m") for e in (packs.get(tb) or [])
                   if e.get("k") in good] + [1])
        seen_ = set()
        for tb in ("d", "w", "m"):
            for e in (packs.get(tb) or []):
                p0 = float(e["p"])
                p = p0 * mapper if mapper else p0
                if (side < 0) != (p < spot) or p == spot:
                    continue
                k = e.get("k")
                if k in good:
                    key_ = (tb, round(p, 1))
                    if key_ in seen_:
                        continue
                    seen_.add(key_)
                    oi_ = e.get("oi", 0)
                    w_ = disc * (0.7 + 1.3 * (oi_ / moi) + (0.15 if e.get("m") else 0))
                    cp = "C+P" if k == "b" else ("P" if side < 0 else "C")
                    if mapper:
                        ing.append((p, w_, f"{tag}{fmt_strike(p0)}{cp} ≈{ff(p)}"
                                           + (f" ({fmt_k(oi_)})" if oi_ else "")))
                    else:
                        ing.append((p, w_, f"{fmt_strike(p0)}{cp} "
                                           + (f"({fmt_k(oi_)}, {HZT[tb]})" if oi_ else f"({HZT[tb]})")))
                elif k in ("c", "p") and not mapper:
                    # wrong-side wall (e.g. a call wall below price): weak pivot, note it
                    ing.append((p, 0.45, f"{fmt_strike(p0)}{'C' if k == 'c' else 'P'} ({HZT[tb]})"))
                elif k == "g" and not mapper:
                    t = e.get("t", "")
                    if "EM" in t:
                        w_ = 1.2 if "dEM" in t else (1.0 if "wEM" in t else 0.85)
                        nm = "day band" if "dEM" in t else ("week band" if "wEM" in t else "month band")
                        ing.append((p, w_, f"{nm} edge {ff(p)}"))
                    elif "prev close" in t:
                        ing.append((p, 0.6, f"prev close {ff(p)}"))
                    elif "pin" in t:
                        ing.append((p, 0.6, f"pin {ff(p)}"))

    wall_ing(lv_tabs)
    if spy_pack and spy_spot:
        wall_ing(spy_pack, mapper=spot / spy_spot, tag="SPY ", disc=0.85)
    if dem:
        a_, e_ = (dem[0] + dem[1]) / 2, (dem[1] - dem[0]) / 2
        if e_ > 0:
            ing.append((a_ + side * 1.5 * e_, 1.0, "1.5× ring"))
            ing.append((a_ + side * 2.0 * e_, 0.8, "2× ring"))
    try:  # session extremes from the packed 5m bars (today + prior session)
        if day_bars:
            last_t = day_bars[-1][0]
            cut = last_t - (last_t % 86400)
            today = [b for b in day_bars if b[0] >= cut]
            prior = [b for b in day_bars if b[0] < cut]
            if prior:
                pcut = prior[-1][0] - (prior[-1][0] % 86400)
                yb = [b for b in prior if b[0] >= pcut]
                if yb:
                    v = min(b[3] for b in yb) if side < 0 else max(b[2] for b in yb)
                    if (side < 0) == (v < spot):
                        ing.append((v, 0.85, ("y'day low " if side < 0 else "y'day high ") + ff(v)))
            if len(today) > 3:
                v = min(b[3] for b in today) if side < 0 else max(b[2] for b in today)
                if (side < 0) == (v < spot) and abs(v / spot - 1) > 0.0008:
                    ing.append((v, 0.9, ("today's low " if side < 0 else "today's high ") + ff(v)))
    except Exception:
        pass
    try:  # long moving averages — the classic institutional reference points
        if daily_px is not None and len(daily_px) >= 50:
            dcl = daily_px.Close
            for w_, wt, nm in ((50, 1.1, "50-day avg"), (200, 1.4, "200-day avg")):
                if len(dcl) >= w_:
                    v = float(dcl.rolling(w_).mean().iloc[-1])
                    if v == v and (side < 0) == (v < spot):
                        ing.append((v, wt, f"{nm} {ff(v)}"))
    except Exception:
        pass
    for b_ in bigs or []:  # today's biggest single-contract premium prints
        try:
            if b_.get("lab") != label:
                continue
            k_ = float(b_.get("k", 0))
            cp_ = b_.get("cp")
            if not k_ or (side < 0) != (k_ < spot):
                continue
            if (side < 0 and cp_ != "P") or (side > 0 and cp_ != "C"):
                continue
            pm = float(b_.get("prem", 0))
            ing.append((k_, 1.0 + (0.3 if pm >= 2e7 else 0),
                        f"${pm/1e6:.0f}M {fmt_strike(k_)}{cp_} print"))
        except Exception:
            continue
    # keep a tradeable window, cluster chain-linked levels into zones, rank
    lo_cut, hi_cut = (spot * 0.945, spot * 0.9995) if side < 0 else (spot * 1.0005, spot * 1.055)
    ing = [i for i in ing if lo_cut <= i[0] <= hi_cut]
    if not ing:
        return []
    ing.sort(key=lambda x: (-x[0] if side < 0 else x[0]))
    tiers = []
    for p, w_, lbl in ing:
        if tiers:
            t0 = tiers[-1]
            near = abs(t0["near"] / p - 1) < 0.0018
            span = abs(t0["far"] / p - 1) < 0.0035
            if near and span:
                t0["near"] = p
                t0["score"] += w_
                t0["bits"].append(lbl)
                continue
        tiers.append({"far": p, "near": p, "score": w_, "bits": [lbl]})
    tiers = tiers[:9]
    def _merge_bits(bits):
        # "7500P (43k, Fri)" + "7500P (12k, mo)" -> "7500P (43k Fri / 12k mo)"
        order, info = [], {}
        for b in bits:
            if " (" in b and b.endswith(")"):
                pre, inner = b.split(" (", 1)
                inner = inner[:-1].replace(", ", " ")
                if pre not in info:
                    info[pre] = []
                    if pre not in order:
                        order.append(pre)
                if inner not in info[pre]:
                    info[pre].append(inner)
            elif b not in order:
                order.append(b)
        return [f"{p} ({' / '.join(info[p])})" if p in info and info[p] else p for p in order]
    for t0 in tiers:  # far = closer to spot, near = deeper; normalize to hi/lo
        t0["hi"], t0["lo"] = max(t0["far"], t0["near"]), min(t0["far"], t0["near"])
        t0["bits"] = _merge_bits(t0["bits"])
    order = sorted(range(len(tiers)), key=lambda i: -tiers[i]["score"])
    for rk, i in enumerate(order):
        tiers[i]["rank"] = rk + 1
    return tiers

def ladder_table(label, spot, side, tiers, mark=None):
    """One tbody of price-ordered tier rows: rank badges, strength dots,
    air-pocket gaps and a movable "you are here" row."""
    ff = (lambda x: f"{x:,.0f}") if spot >= 2000 else (lambda x: f"{x:,.2f}")
    rows = []  # (price, html) — sorted spot-outward at the end
    prev_edge = None
    for t0 in tiers:
        edge_in = t0["hi"] if side < 0 else t0["lo"]
        if prev_edge is not None and abs(prev_edge / edge_in - 1) > 0.0075:
            mid_ = (prev_edge + edge_in) / 2
            rows.append((mid_, f'<tr class=lair data-p={mid_:.2f}><td colspan=3>'
                               f'· {abs(prev_edge - edge_in):,.0f} pts of thin air ·</td></tr>'))
        prev_edge = t0["lo"] if side < 0 else t0["hi"]
        zone = ff(t0["hi"]) if abs(t0["hi"] / t0["lo"] - 1) < 0.0005 else f"{ff(t0['lo'])}–{ff(t0['hi'])}"
        strength = "●" * min(5, max(1, round(t0["score"])))
        cls = " l1" if t0["rank"] == 1 else (" l2" if t0["rank"] == 2 else "")
        anchor = t0["hi"] if side < 0 else t0["lo"]
        bshow = t0["bits"][:6] + ([f"+{len(t0['bits']) - 6} more"] if len(t0["bits"]) > 6 else [])
        rows.append((anchor, f'<tr data-p={anchor:.2f}><td class="lrk{cls}">{t0["rank"]}</td>'
                             f'<td class=lz>{zone}<div class=lstr>{strength}</div></td>'
                             f'<td class=lbits>{" + ".join(bshow)}</td></tr>'))
    if mark is not None:
        rows.append((mark["px"], f'<tr class=lmk id=lmk-{label} data-p={mark["px"]:.2f}>'
                                 f'<td colspan=3>→ {mark["lbl"]} {ff(mark["px"])} ←</td></tr>'))
    rows.sort(key=lambda r: (-r[0] if side < 0 else r[0]))
    tid = ("lad-" if side < 0 else "ladu-") + label
    return f'<table class=lad><tbody id={tid}>{"".join(h for _, h in rows)}</tbody></table>'

def plain_read(read_data, regime_dist, breadth_vcls, prem_rank, vix_txt, ahead=None,
               rlz=None, is_opex=False, on_txt=None, term=None, prep=None, ext_txt=None,
               prem_streak=1):
    """The top of the page: verdicts in plain English, with the WHY attached.
    Composed from the same signals as everything else — words first, data as receipts."""
    S = []
    if prem_rank is not None:
        if prem_rank < 15:
            if prem_streak >= 3:
                f1 = ("1-day vol is ALREADY above 30-day — that's the usual first crack"
                      if (term or {}).get("inv") else "1-day vol above 30-day (not yet)")
                f2 = ("the tape is ALREADY out-moving the straddle"
                      if (rlz and rlz.get("em") and rlz.get("avg5") and rlz["avg5"] > rlz["em"])
                      else "realized moves out-running the straddle (not yet)")
                S.append(f"<b class=w>Cheap-premium regime — session {prem_streak}:</b> option prices "
                         f"have sat in the bottom decile for {prem_streak} straight sessions, so this "
                         f"is the regime, not today's news. In it, straddle buyers lost ~83% of the "
                         f"time — and it usually ends loudly, not gradually. What would flip it: "
                         f"{f1}; {f2}. Until one of those shows, the daily verdict won't change.")
            else:
                S.append(f"<b class=w>Hard day to make money buying options:</b> they're about the cheapest "
                         f"they get (cheaper than {max(1, 100 - prem_rank):.0f}% of days) — but days this cheap "
                         f"usually stay quiet, and straddle buyers still lost ~83% of the time. "
                         f"Cheap has been cheap for a reason. Single-leg buyers wear the worst of it — "
                         f"if you take a shot anyway: half size, only at a level.")
        elif prem_rank > 80:
            if prem_streak >= 3:
                S.append(f"<b class=w>Elevated-premium regime — session {prem_streak}:</b> options have "
                         f"priced above the 80th percentile for {prem_streak} straight sessions — "
                         f"movement is being paid for daily. This was your only net-green regime, "
                         f"and also where oversizing kills fastest: moves travel both ways.")
            else:
                S.append(f"<b class=w>Options are expensive today</b> (pricier than {prem_rank:.0f}% of days). "
                         f"Big-move days cluster here — but you're paying up for the chance, and sellers "
                         f"get paid well precisely because these days run people over. Nobody gets a bargain.")
        else:
            S.append("Options are priced about normal today — no edge from premium being cheap or rich.")
    if term and term.get("inv"):
        S.append(f"<b class=w>Term structure inverted:</b> 1-day vol ({term['v1']:.1f}) is above 30-day "
                 f"({term['v30']:.1f}) — the market is pricing <i>today</i> as riskier than the month "
                 f"ahead. That's rare (~1 day in 7, usually around events) and it's the one tape where "
                 f"big moves actually follow: the day after an inverted close averaged ±1.2% vs ±0.65% "
                 f"normally, and straddle buyers beat their price 34% of the time vs 26% — still losing "
                 f"odds, but the closest premium gets to fairly priced. Moves travel; so do losses.")
    if rlz and rlz.get("em") and rlz.get("avg5"):
        if rlz["em"] < rlz["avg5"]:
            S.append(f"One more premium trap: today's straddle prices a ±{rlz['em']:.2f}% move — "
                     f"<b class=w>less than the market has actually been moving</b> "
                     f"(5-day average ±{rlz['avg5']:.2f}%, yesterday ±{rlz['y']:.2f}%). That looks like "
                     f"a bargain. Historically it isn't: on days priced below recent reality, the market "
                     f"beat its straddle only about one day in five (vs one in four normally) — the "
                     f"pricing is a bet that the tape calms down, and it usually wins.")
    if is_opex:
        S.append("<b>Monthly expiration day:</b> a large slice of index option positioning dies at "
                 "today's close — pinning to the biggest strikes tends to run stronger than usual "
                 "(OPEX Fridays have averaged smaller moves than ordinary Fridays), and those "
                 "magnets are gone Monday.")
    if on_txt:
        S.append(on_txt)
    if ext_txt:
        S.append(ext_txt)
    # (Aug 22: the amplifier/magnet day verdict is GONE. It rested on the naive
    # dealer-gamma sign assumption — stale OI + a fiction about who's short.
    # Tested honestly on 3y of hourly tape: day-ahead "trendiness" regime has
    # zero persistence (corr −0.05) and the follow-through split ran BACKWARDS
    # from the story. Neither the model nor a measured version earns a seat.)
    if regime_dist is not None:
        t = (f"the uptrend is intact (SPX {regime_dist:+.1f}% above its 50-day average)"
             if regime_dist >= 0 else
             f"<b class=w>price is below its 50-day average ({regime_dist:+.1f}%)</b> — historically "
             f"that's where most of the market's losses happen")
        b = {"good": ", and most stocks are participating — the healthy kind of move",
             "warn": ", <b class=w>but few stocks are participating</b> — rallies carried on narrow shoulders",
             "mixed": ", with mixed participation underneath"}.get(breadth_vcls, "")
        S.append(f"Bigger picture: {t}{b}.")
    if ahead:
        A = []
        spot_, hz = ahead.get("spot"), ahead.get("hz") or {}
        if spot_ and hz.get("tm"):
            t_ = hz["tm"]
            pct = t_["em"] / spot_ * 100
            r_, hi_ = ahead.get("ret"), ahead.get("at_hi")
            if ahead.get("fade"):
                calm_cohort = (regime_dist is not None and regime_dist >= 0
                               and term and term.get("v30") and term["v30"] < 20)
                tail = ("Downside tail in this regime (calm vol, above the 50-day): worst next "
                        "day in 26 years was −1.6%, and ≤−1% happened just 5% of the time"
                        if calm_cohort else
                        "<b class=w>Caution: in stressed tape (VIX 25+ or below the 50-day) this "
                        "same shape has crashed up to −4.4% next day</b>")
                rate = ("today was a gap-up-and-fade — historically the fade overshoots into the "
                        "close: the next session opened higher 58% of the time and closed up "
                        "~64% (avg +0.4%), one of the few day-shapes with a real next-day lean. "
                        + tail)
            elif r_ is not None and r_ >= 0.9 and hi_:
                rate = ("after strong days that close at the highs, the next day has been a "
                        "coin flip and usually <i>quieter</i> than normal — breakouts digest, "
                        "they rarely reverse hard")
            elif r_ is not None and r_ <= -1.0:
                rate = ("after 1%-plus down days, the next day averaged about flat with extra "
                        "chop — bounces are common but sloppy")
            elif r_ is not None and abs(r_) < 0.3:
                rate = "quiet days like today carry no reliable next-day lean"
            else:
                rate = "days like today carry no strong next-day lean either way"
            A.append(f"<b>Tomorrow</b> ({t_['exp']}): the market has priced a ±{pct:.1f}% move. "
                     f"History says {rate}.")
        if spot_ and hz.get("wk"):
            w_ = hz["wk"]
            A.append(f"<b>By Friday</b> ({w_['exp']}): priced ±{w_['em'] / spot_ * 100:.1f}% — "
                     f"and 83% of weeks close inside that band, which is why weekly lotto "
                     f"buyers bleed. The purple/yellow lines on the Week tab are where "
                     f"<i>that expiry's</i> big positions sit.")
        if spot_ and hz.get("mo"):
            m_ = hz["mo"]
            A.append(f"<b>By monthly expiry</b> ({m_['exp']}): priced ±{m_['em'] / spot_ * 100:.1f}%. "
                     f"The 3-month tab shows the monthly walls — the levels swing trades "
                     f"and the big funds care about.")
        if A:
            S.append('<span class=hzt>Looking ahead — </span>' + " ".join(A))
    if prep:
        hdr = (f"<b>Market closed — prep for {prep['next']}:</b> everything below reads on the "
               f"<i>next</i> session's chain and levels.")
        if prep.get("oi_stale"):
            hdr += (" Walls right now are today's flow on tomorrow's expiry — open interest "
                    "refreshes overnight, and the 8:15am build redraws the real map.")
        S.insert(0, hdr)
        if prep.get("autopsy"):
            S.insert(1, prep["autopsy"])
        if prep.get("inval"):
            S.append(prep["inval"])
    key = ("Reading the charts: <span class=kc>purple lines</span> = where rallies usually stall · "
           "<span class=kp>yellow</span> = where drops usually stall · teal = price now · "
           "the shaded cloud = where the market prices this expiry (darkest core: ~4 in 5 days end inside; "
           "amber ring to 1.5×: 1-in-3 of breaks reach it; red ring to 2×: 1-in-10), and the bars "
           "hugging the right edge are open interest at each wall — longer bar, bigger wall. "
           "White line/label = that level is being fought over right now.")
    if vix_txt and S:
        S[-1] += ' <span class=rc>(' + vix_txt.strip(" ·") + ')</span>'
    body = "".join("<p>" + s + "</p>" for s in S)
    return '<div class=say>' + body + '<p class=key>' + key + '</p></div>'

def build(fetch):
    """fetch(sym) -> dict(px, px_w, px_d, exp, calls, puts, ems, pin, gflip)."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    opex_day = False   # pin is an OPEX-only concept here (measured; audit Sep 22)
    try:
        d3o = now_et.date().replace(day=15)
        while d3o.weekday() != 4:
            d3o += dt.timedelta(days=1)
        opex_day = now_et.date() == d3o
    except Exception:
        pass
    v = int(now_utc.timestamp())
    # previous build's wall volumes -> momentum (ignore stale gaps > 45 min)
    prev_state = {}
    try:
        s = json.load(open("docs/state.json"))
        if (now_utc - dt.datetime.fromisoformat(s["_ts"])).total_seconds() < 2700:
            prev_state = s
    except Exception:
        pass
    state = {"_ts": now_utc.isoformat()}
    cards, data, arch, read_data, spx_daily, series_map = [], [], {}, {}, {}, {}
    scards, scan_rows, hzp, bigs = [], [], {}, []
    idx_dailies = {}   # SPX/QQQ daily frames for the ladder's moving averages
    panic_new, pan_state = [], {}   # 30m panic-dip signal (validated Aug 2026)
    futs = _with_timeout(fut_fetch, 45) or {}
    fscale = {}
    for sym, label in TICKERS + STOCKS:
        try:
            d = fetch(sym)
        except Exception as e:
            print("fetch failed:", label, repr(e))
            if label == "SPX":
                raise
            continue
        px, exp, calls, puts, ems = d["px"], d["exp"], d["calls"], d["puts"], d["ems"]
        spot = px["Close"].iloc[-1]
        # overnight sanity: a next-session chain before the ~5am OCC refresh has no
        # real OI — "walls" become leftover junk strikes far from price. Filter to
        # plausible distance/size; if nothing survives, show no walls (honest) and
        # drop pin/gamma, which are equally meaningless on an unseeded chain.
        wk_em_ = (d.get("hz") or {}).get("wk", {}).get("em")
        if ems.get("day") and wk_em_ and ems["day"][1] > wk_em_ * 1.15:
            ems["day"] = None   # day straddle wider than the week's = unseeded junk quotes
        lim_ = 0.02
        if ems.get("day"):
            lim_ = min(max(3 * ems["day"][1] / ems["day"][0], 0.008), 0.02)
        calls = [w for w in calls if abs(w.strike / spot - 1) <= lim_ and (w.openInterest or 0) >= 200]
        puts = [w for w in puts if abs(w.strike / spot - 1) <= lim_ and (w.openInterest or 0) >= 200]
        if not calls and not puts:
            d["pin"], d["gam"] = None, None
        open(f"docs/OI_Walls_{label}.txt", "w").write(study_text(label, exp, calls, puts))
        em_levels = []
        for tag, key in (("dEM", "day"), ("wEM", "week")):
            if ems.get(key):
                a, e = ems[key]
                em_levels += [(tag, a - e), (tag, a + e)]
        wk_levels = [t for t in em_levels if t[0] == "wEM"]
        day_levels = [t for t in em_levels if t[0] == "dEM"]  # wEM lives on the Week tab
        # extension ladder: once the day band breaks, moves reached 1.5×EM ~1 in 3
        # days and 2×EM ~1 in 10 (2000–2026 and 2023–2026 agree). Show 1.5× always;
        # show 2× only when today's tape has already broken the band.
        band_broke = False
        if ems.get("day"):
            a_, e_ = ems["day"]
            try:
                sh_, ds2_, _pc2 = split_session(px)
                tsl = sh_.iloc[ds2_:]
                if len(tsl) and sh_.index[-1].date() == now_et.date():
                    band_broke = (float(tsl.High.max()) > a_ + e_ or
                                  float(tsl.Low.min()) < a_ - e_)
            except Exception:
                pass
            day_levels.append(("prev close", a_))
            if opex_day and d.get("pin"):   # pin shown only on OPEX (measured effect)
                day_levels.append(("pin", d["pin"]))
        chart(f"docs/{label}.png", px, spot, calls, puts, day_levels, "day")
        if d.get("px_w") is not None and len(d["px_w"]):
            chart(f"docs/{label}_w.png", d["px_w"], spot, calls, puts, wk_levels, "week")
        if d.get("px_d") is not None and len(d["px_d"]):
            chart(f"docs/{label}_d.png", d["px_d"], spot, calls, puts, wk_levels, "daily")

        def ser(frame, day_vwap=False):
            """Packed series: candles as [t,o,h,l,c] arrays (expanded client-side)
            so week-deep 5m and years of daily history fit the page. All bars are
            kept; day_vwap adds a VWAP line over the current session only."""
            if frame is None or not len(frame):
                return {"c": [], "v": []}
            vw, vslice = None, None
            if day_vwap:
                shown2, ds2, _pc = split_session(frame)
                vslice = shown2.iloc[ds2:]
                vw = session_vwap(vslice)
            cs, vs = [], []
            def ets(t_):  # shift so the chart's UTC display reads as ET wall time
                off = t_.utcoffset()
                return int(t_.timestamp() + (off.total_seconds() if off is not None else 0))
            for t_, r_ in frame.iterrows():
                if r_.Close != r_.Close:  # NaN bar (Yahoo gap) -> invalid JSON, dead charts
                    continue
                cs.append([ets(t_), round(float(r_.Open), 2), round(float(r_.High), 2),
                           round(float(r_.Low), 2), round(float(r_.Close), 2)])
            if vw is not None:
                for (t_, _), vv in zip(vslice.iterrows(), vw):
                    if vv != vv:  # NaN vwap (zero-volume opening bars)
                        continue
                    vs.append([ets(t_), round(float(vv), 2)])
            return {"c": cs, "v": vs}
        def lvpack(levels, cw_=None, pw_=None):
            cw_ = calls if cw_ is None else cw_
            pw_ = puts if pw_ is None else pw_
            out = []
            max_c = max((w.openInterest for w in cw_), default=0)
            max_p = max((w.openInterest for w in pw_), default=0)
            for kind, w_ in sorted([("C", x) for x in cw_] + [("P", x) for x in pw_],
                                   key=lambda t: t[1].strike):
                hot = bool(w_.volume > w_.openInterest)
                major = bool(w_.openInterest >= (max_c if kind == "C" else max_p) * 0.999)
                out.append({"p": float(w_.strike), "t": f"{kind} wall",
                            "k": "c" if kind == "C" else "p", "h": hot, "m": major,
                            "oi": int(w_.openInterest or 0)})
            for tag, lv_ in levels:
                out.append({"p": float(lv_), "t": tag, "k": "g", "h": False, "m": False})
            # merge co-located levels (within 0.08%) into ONE line so the axis
            # never stacks four labels on the same price
            out.sort(key=lambda x: x["p"])
            merged = []
            for e in out:
                if merged and abs(e["p"] / merged[-1]["p"] - 1) < 0.0008:
                    m0 = merged[-1]
                    m0["t"] = m0["t"] + " · " + e["t"]
                    m0["h"] = m0["h"] or e["h"]
                    m0["m"] = m0["m"] or e.get("m", False)
                    ks = {m0["k"], e["k"]}
                    if ks == {"c", "p"}:
                        m0["k"] = "b"
                    elif e["k"] in ("c", "p") and m0["k"] == "g":
                        m0["k"] = e["k"]
                        m0["p"] = e["p"]   # strikes are the tradeable number
                else:
                    merged.append(dict(e))
            return merged
        hz = d.get("hz") or {}
        wkh, moh = hz.get("wk"), hz.get("mo")
        wk_lv = (lvpack(wk_levels, wkh["calls"], wkh["puts"]) if wkh
                 else lvpack(wk_levels))
        mo_lv = lvpack(
            ([("mEM", spot - moh["em"]), ("mEM", spot + moh["em"])] if moh else wk_levels),
            moh["calls"] if moh else None, moh["puts"] if moh else None)
        series_map[label] = {
            "d": ser(px, True), "w": ser(d.get("px_w")), "m": ser(d.get("px_d")),
            "lv": {"d": lvpack(day_levels), "w": wk_lv, "m": mo_lv},
        }
        # 24h view: futures bars scaled onto this index's level so walls still apply
        fpx = futs.get(FUT.get(label))
        if fpx is not None and len(fpx) > 10:
            try:
                fref = float(fpx.Close.asof(px.index[-1]))
            except Exception:
                fref = float("nan")
            if fref != fref or fref <= 0:  # NaN or bad -> latest futures price
                fref = float(fpx.Close.iloc[-1])
            sc_ = float(spot) / fref
            fs_ = fpx.copy()
            for c_ in ("Open", "High", "Low", "Close"):
                fs_[c_] = fs_[c_] * sc_
            series_map[label]["o"] = ser(fs_)
            series_map[label]["lv"]["o"] = lvpack(day_levels)
            fscale[label] = round(sc_, 6)
        # initial visible windows: each tab opens on the familiar view, the rest
        # of the loaded history is there when you drag back
        sd_l, vs_ = series_map[label], {}
        try:
            if sd_l["d"]["c"]:
                last_ts = sd_l["d"]["c"][-1][0]
                cut = last_ts - (last_ts % 86400)   # ET midnight of the last session
                vs_["d"] = next(a[0] for a in sd_l["d"]["c"] if a[0] >= cut)
            if sd_l["w"]["c"]:
                vs_["w"] = sd_l["w"]["c"][max(0, len(sd_l["w"]["c"]) - 66)][0]
            if sd_l["m"]["c"]:
                vs_["m"] = sd_l["m"]["c"][max(0, len(sd_l["m"]["c"]) - 64)][0]
            if sd_l.get("o", {}).get("c"):
                vs_["o"] = sd_l["o"]["c"][max(0, len(sd_l["o"]["c"]) - 100)][0]
        except Exception:
            pass
        sd_l["vs"] = vs_
        if label in ("SPX", "SPY", "QQQ"):
            try:
                pan_ = panic_scan(d.get("px_w"), now_et)
                sd_l["ps"] = pan_["mk"]
                _, newf_ = panic_update_log(label, pan_)
                if newf_:
                    panic_new.append(newf_)
                pan_state[label] = pan_
            except Exception as e_:
                print("panic scan failed:", label, repr(e_))
        data.append((label, spot, calls, puts))
        state[label] = {w.contractSymbol: int(w.volume) for w in calls + puts}
        arch[label] = {
            "spot": round(float(spot), 2), "exp": exp,
            "calls": [{"k": w.strike, "oi": int(w.openInterest), "v": int(w.volume)} for w in calls],
            "puts":  [{"k": w.strike, "oi": int(w.openInterest), "v": int(w.volume)} for w in puts],
            "ems": ems, "pin": d.get("pin"), "gam": d.get("gam"),
            "flow": d.get("flow"),
        }

        shown, day_start, _ = split_session(px)
        vwap = session_vwap(shown.iloc[day_start:])
        chips = []
        up = sorted((c for c in calls if c.strike >= spot), key=lambda w: w.strike)
        dn = sorted((p for p in puts if p.strike <= spot), key=lambda w: -w.strike)
        for arrow, w, cls in [("▲", up[0] if up else None, "c"), ("▼", dn[0] if dn else None, "p")]:
            if w is None:
                chips.append(f'<span class="chip {cls}">{arrow} clear</span>')
            else:
                hot = " hot" if w.volume > w.openInterest else ""
                chips.append(f'<span class="chip {cls}{hot}">{arrow} {fmt_strike(w.strike)} '
                             f'{100*(w.strike/spot-1):+.2f}%</span>')
        if vwap is not None:
            side = "above" if spot >= vwap.iloc[-1] else "below"
            chips.append(f'<span class="chip v">VWAP {vwap.iloc[-1]:.2f} · px {side}</span>')
        em_bits = []
        if ems.get("day"):   em_bits.append(f"d ±{ems['day'][1]:.0f}")
        if ems.get("week"):  em_bits.append(f"w ±{ems['week'][1]:.0f}")
        if em_bits:
            chips.append(f'<span class="chip e">EM {" · ".join(em_bits)}</span>')
        if opex_day and d.get("pin"):
            chips.append(f'<span class="chip e">pin {fmt_strike(d["pin"])} · OPEX</span>')
        fl = d.get("flow")
        if fl and fl[0] is not None and (fl[0] + fl[1]) > 0:
            chips.append(f'<span class="chip e" title="$ premium traded on the front expiry so far '
                         f'(volume × mid, 15-min-delayed). UNSIGNED — traded is not the same as '
                         f'bought. Archived every build; it earns a verdict only if its extremes '
                         f'test out.">flow C ${fl[0]/1e6:.1f}M · P ${fl[1]/1e6:.1f}M</span>')

        def tr(kind, cls, w):
            dist = 100*(w.strike/spot-1)
            voi = (w.volume/w.openInterest) if w.openInterest else float("inf")
            hot = ' class=hot' if w.volume > w.openInterest else ''
            dv = w.volume - prev_state.get(label, {}).get(w.contractSymbol, w.volume)
            dvs = f' <span class=dv>+{fmt_k(dv)}</span>' if dv > 0 else ''
            return (f'<tr{hot}><td><b class={cls}>{kind} {fmt_strike(w.strike)}</b>'
                    f'<div class=m>{tos_symbol(w.contractSymbol)}</div></td>'
                    f'<td>{dist:+.2f}%</td><td>{fmt_k(w.openInterest)}</td>'
                    f'<td>{fmt_k(w.volume)}{dvs}</td><td>{voi:.1f}×</td></tr>')
        rows_sorted = sorted([("C", "c", c) for c in calls] + [("P", "p", p) for p in puts],
                             key=lambda t: abs(t[2].strike/spot - 1))
        trs = "".join(tr(k, cl, w) for k, cl, w in rows_sorted)
        fut_btn = (f'<button class=tb data-t=o data-l={label}>24h · futures</button>'
                   if FUT.get(label) else '')
        tabs = (f'<div class=tw>'
                f'<button class="tb on" data-t=d data-l={label}>Today · 0DTE</button>'
                f'{fut_btn}'
                f'<button class=tb data-t=w data-l={label}>Week · Fri exp</button>'
                f'<button class=tb data-t=m data-l={label}>3mo · monthly</button>'
                f'<div class=chart id="ch-{label}"></div>'
                f'<noscript><img src="{label}.png?v={v}" alt="{label} today"></noscript>'
                f'</div>')
        read_data[label] = {
            "spot": float(spot), "calls": calls, "puts": puts,
            "dem": (ems["day"][0] - ems["day"][1], ems["day"][0] + ems["day"][1]) if ems.get("day") else None,
            "pin": d.get("pin") if opex_day else None,
            "vwap": float(vwap.iloc[-1]) if vwap is not None else None,
        }
        if label in ("SPX", "QQQ") and d.get("px_d") is not None and len(d["px_d"]) > 50:
            idx_dailies[label] = d["px_d"]
        if label == "SPX" and d.get("px_d") is not None and len(d["px_d"]) > 50:
            spx_daily["px"] = d["px_d"]
            spx_daily["px5"] = px
            spx_daily["hz"] = d.get("hz") or {}
            spx_daily["spot"] = float(spot)
            spx_daily["broke"] = band_broke
            spx_daily["dem_ae"] = ems.get("day")
            try:
                shown_, ds_, pc_ = split_session(px)
                spx_daily["ret"] = 100 * (float(spot) / float(pc_) - 1) if pc_ else None
                if (pc_ is not None and len(shown_) > ds_
                        and shown_.index[ds_].date() == now_et.date()):
                    spx_daily["gap"] = 100 * (float(shown_.Open.iloc[ds_]) / float(pc_) - 1)
                    tb_ = shown_.iloc[ds_:]
                    hi_, lo_, cl_, op_ = (float(tb_.High.max()), float(tb_.Low.min()),
                                          float(tb_.Close.iloc[-1]), float(tb_.Open.iloc[0]))
                    clv_ = (cl_ - lo_) / max(hi_ - lo_, 1e-9)
                    spx_daily["fade"] = bool(spx_daily["gap"] > 0.25 and cl_ < op_ and clv_ < 0.3)
                dcl = d["px_d"].Close
                spx_daily["at_hi"] = bool(dcl.iloc[-1] >= dcl.rolling(20).max().iloc[-1] - 1e-9)
            except Exception:
                pass
        gw = 0.03 if label in STOCK_SET else 0.015
        (scards if label in STOCK_SET else cards).append(
            f"""<section id="{label}"><h2>{label} <em id="px-{label}">{spot:.2f}</em>
<button class=pbtn id="pb-{label}" data-l={label}>＋ position</button>
<span class=al id="al-{label}"></span>
<span class=gd id="gd-{label}">{dists_text(read_data[label])}</span></h2>
<div class=pform id="pf-{label}" style="display:none">
<div class=plan id="pl-{label}"></div>
<select id="ps-{label}"><option value=C>Call</option><option value=P>Put</option></select>
<input id="pk-{label}" type=number step=any placeholder="strike">
<input id="pp-{label}" type=number step=any placeholder="entry $ (opt)">
<select id="pe-{label}" title="expiration — picks which band and walls your position is judged against">{"".join(
    [f'<option value="{exp}">exp {exp}</option>'] +
    ([f'<option value="{hz["wk"]["exp"]}">Fri {hz["wk"]["exp"]}</option>']
     if hz.get("wk") and hz["wk"]["exp"] != exp else []) +
    ([f'<option value="{hz["mo"]["exp"]}">mo {hz["mo"]["exp"]}</option>']
     if hz.get("mo") and hz["mo"]["exp"] not in (exp, (hz.get("wk") or {}).get("exp")) else []))}</select>
<button class=psave data-l={label}>track</button><button class=pclear data-l={label}>clear</button>
</div>
{gauge_strip(read_data[label], label, W=gw)}
{tabs}
<details class=more><summary>levels · flow · exp {exp}</summary>
<div class=chips>{"".join(chips)}</div>
<table><tr><th>Wall</th><th>Dist</th><th>OI</th><th>Vol (Δ10m)</th><th>V/OI</th></tr>{trs}</table>
<div class=m style="font-size:11px;margin:4px 0 6px">OI = yesterday's book (OCC refresh ~6am ET) — structure, not live positioning, and no dealer-side guess is made from it. ⚡ vol&gt;OI marks strikes being rebuilt <i>today</i>. Wall hold-rates get their audit Sep 22.</div><a class=btn href="OI_Walls_{label}.txt">ToS study text</a></details></section>""")
        # scanner row from this ticker's own data (indexes included for comparison)
        try:
            hz_ = d.get("hz") or {}
            rlz5_ = None
            if d.get("px_d") is not None and len(d["px_d"]) > 7:
                dcl_ = d["px_d"].Close
                try:
                    if dcl_.index[-1].date() >= now_et.date():
                        dcl_ = dcl_.iloc[:-1]
                except Exception:
                    pass
                rlz5_ = float(dcl_.pct_change().abs().iloc[-5:].mean() * 100)
            scan_rows.append({
                "lab": label, "card": True, "spot": float(spot),
                "emd": 100 * ems["day"][1] / float(spot) if ems.get("day") else None,
                "emw": 100 * hz_["wk"]["em"] / float(spot) if hz_.get("wk") else None,
                "emm": 100 * hz_["mo"]["em"] / float(spot) if hz_.get("mo") else None,
                "spr": d.get("spr"), "vol": d.get("fvol"), "rlz5": rlz5_,
                "earn": d.get("earn"),
                "moexp": hz_["mo"]["exp"] if hz_.get("mo") else None,
            })
            hzp[label] = {
                "d": scan_rows[-1]["emd"], "w": scan_rows[-1]["emw"], "m": scan_rows[-1]["emm"],
                "fe": exp,
                "we": hz_["wk"]["exp"] if hz_.get("wk") else None,
                "me": hz_["mo"]["exp"] if hz_.get("mo") else None,
            }
            for b_ in (d.get("big0") or []):
                bigs.append(dict(b_, lab=label))
            for hk_ in ("tm", "wk", "mo"):
                for b_ in (hz_.get(hk_, {}).get("big") or []):
                    bigs.append(dict(b_, lab=label))
        except Exception as e:
            print("scan row failed:", label, repr(e))

    # light scanner pass for names without full cards
    for ssym in SCAN_ONLY:
        def _scan_one(sym_=ssym):
            t_ = yf.Ticker(sym_)
            pxd_ = t_.history(period="3mo", interval="1d")
            if pxd_ is None or len(pxd_) < 8:
                return None
            spot_ = float(pxd_.Close.iloc[-1])
            exp_ = t_.options[0]
            ch_ = t_.option_chain(exp_)
            ems_ = expected_moves(t_, spot_, ch_)
            hz_ = horizon_chains(t_, spot_, now_et.date())
            dcl_ = pxd_.Close
            try:
                if dcl_.index[-1].date() >= now_et.date():
                    dcl_ = dcl_.iloc[:-1]
            except Exception:
                pass
            return {
                "lab": sym_, "card": False, "spot": spot_,
                "emd": 100 * ems_["day"][1] / spot_ if ems_.get("day") else None,
                "emw": 100 * hz_["wk"]["em"] / spot_ if hz_.get("wk") else None,
                "emm": 100 * hz_["mo"]["em"] / spot_ if hz_.get("mo") else None,
                "spr": atm_spread_pct(ch_, spot_), "vol": front_volume(ch_),
                "rlz5": float(dcl_.pct_change().abs().iloc[-5:].mean() * 100),
                "earn": next_earnings(t_),
                "moexp": hz_["mo"]["exp"] if hz_.get("mo") else None,
                "big": ([dict(b_, lab=sym_) for b_ in big_premium(ch_, spot_, exp_)]
                        + [dict(b_, lab=sym_) for hk_ in ("tm", "wk", "mo")
                           for b_ in (hz_.get(hk_, {}).get("big") or [])]),
            }
        row = _with_timeout(_scan_one, 40)
        if row:
            bigs.extend(row.pop("big", []))
            scan_rows.append(row)

    vix_note = ""
    vh = _with_timeout(lambda: yf.Ticker("^VIX").history(period="1y")["Close"], 30)
    try:
        if vh is not None and len(vh) > 100:
            vr = 100 * float((vh.iloc[-1] > vh.iloc[:-1]).mean())
            lab = "calm" if vr < 25 else ("elevated" if vr < 75 else "stressed")
            vix_note = f" · VIX {vh.iloc[-1]:.1f} (rank {vr:.0f} · {lab})"
    except Exception:
        pass
    json.dump(state, open("docs/state.json", "w"))
    # daily signal archive: first build of the day = post-OI-refresh baseline,
    # {date}.json = latest (end-of-day state after the final intraday build)
    os.makedirs("data/archive", exist_ok=True)
    day_key = now_et.strftime("%Y-%m-%d")
    top_bigs = {}
    for b_ in bigs:
        key_ = (b_["lab"], b_["cp"], b_["k"], b_["exp"])
        if key_ not in top_bigs or b_["prem"] > top_bigs[key_]["prem"]:
            top_bigs[key_] = b_
    bigs = sorted(top_bigs.values(), key=lambda z: -z["prem"])[:12]
    snap = json.dumps({"ts": now_utc.isoformat(), "tickers": arch, "big": bigs},
                      default=lambda o: o.item() if hasattr(o, "item") else str(o))
    open(f"data/archive/{day_key}.json", "w").write(snap)
    opening = f"data/archive/{day_key}_open.json"
    # the "morning map" snapshot = first build from 7am ET on (post-OI-refresh);
    # midnight/overnight builds must not claim it — their bands describe the
    # coming session from stale evening quotes
    if not os.path.exists(opening) and now_et.hour >= 7:
        open(opening, "w").write(snap)
    # ----- synthesis inputs for the plain-English read -----
    regime_dist = None
    try:
        pxd = spx_daily.get("px")
        if pxd is not None:
            regime_dist = 100 * (pxd.Close.iloc[-1] / pxd.Close.rolling(50).mean().iloc[-1] - 1)
    except Exception:
        pass
    prem_rank, v1 = None, None
    try:
        day_em = (arch.get("SPX", {}).get("ems") or {}).get("day")
        v1 = _with_timeout(lambda: yf.Ticker("^VIX1D").history(period="3y")["Close"], 30)
        if v1 is not None and len(v1) > 200 and day_em:
            anchor_, e_ = day_em
            em_pct = e_ / anchor_ * 100
            prem_rank = 100 * float((em_pct > (v1 / (252 ** 0.5))).mean())
    except Exception:
        pass
    # how long has the current premium bucket persisted? (regime vs daily news)
    prem_streak = 1
    try:
        if prem_rank is not None and v1 is not None and len(v1) > 300:
            vv_ = v1.reset_index(drop=True)
            end_ = len(vv_) - 1
            try:  # exclude today's partial value from the walk-back
                if v1.index[-1].date() >= now_et.date():
                    end_ -= 1
            except Exception:
                pass
            def _bkt(r_):
                return 0 if r_ < 15 else (2 if r_ > 80 else 1)
            b0 = _bkt(prem_rank)
            for k in range(end_, max(end_ - 40, 250), -1):
                lo_ = max(0, k - 756)
                rk_ = 100 * float((vv_[k] > vv_[lo_:k + 1]).mean())
                if _bkt(rk_) == b0:
                    prem_streak += 1
                else:
                    break
    except Exception:
        pass
    # term structure: 1-day vol above 30-day = market prices today riskier than the month
    term = None
    try:
        if v1 is not None and len(v1) and vh is not None and len(vh):
            t1, t30 = float(v1.iloc[-1]), float(vh.iloc[-1])
            if t1 == t1 and t30 == t30 and t30 > 0:
                term = {"v1": t1, "v30": t30, "inv": t1 > t30}
    except Exception:
        pass
    # realized-vs-implied: is today's straddle priced below what the tape actually moved?
    rlz = None
    try:
        day_em = (arch.get("SPX", {}).get("ems") or {}).get("day")
        pxd = spx_daily.get("px")
        if day_em and pxd is not None and len(pxd) > 7:
            anchor_, e_ = day_em
            dcl = pxd.Close
            try:  # drop today's partial bar so "yesterday" really is yesterday
                if dcl.index[-1].date() >= now_et.date():
                    dcl = dcl.iloc[:-1]
            except Exception:
                pass
            rets = dcl.pct_change().abs() * 100
            if len(rets.dropna()) >= 5:
                rlz = {"em": float(e_ / anchor_ * 100),
                       "y": float(rets.iloc[-1]),
                       "avg5": float(rets.iloc[-5:].mean())}
    except Exception:
        pass
    def _third_friday(y_, m_):
        d0 = dt.date(y_, m_, 15)
        while d0.weekday() != 4:
            d0 += dt.timedelta(days=1)
        return d0
    is_opex = now_et.date() == _third_friday(now_et.year, now_et.month)
    # overnight / opening-gap context (SPX): futures when cash is closed, gap when open
    on_txt, on_stats = None, None
    try:
        mopen = now_et.weekday() < 5 and 570 <= now_et.hour * 60 + now_et.minute < 960
        if mopen:
            g_ = spx_daily.get("gap")
            if g_ is not None and abs(g_) >= 0.3:
                f_, k_ = gap_rates(g_)
                if f_:
                    cont = ("down-gaps this size kept probing — median another 1.0% below the "
                            "open before the day's low (73% went at least 0.5% further), and only "
                            "~40% of gap-day lows form in the first 45 minutes"
                            if g_ < 0 else
                            "up-gaps this size kept running — median another 0.8% above the "
                            "open before the day's high (70% went at least 0.5% further)")
                    shelves = ""
                    try:
                        r0_ = read_data.get("SPX") or {}
                        s0_ = r0_.get("spot")
                        if s0_ and g_ < 0:
                            lv_ = []
                            if r0_.get("dem"):
                                lv_.append(f"{r0_['dem'][0]:,.0f} (day-band edge)")
                            lv_ += [f"{fmt_strike(w.strike)}P wall"
                                    for w in (r0_.get("puts") or []) if w.strike < s0_][:2]
                            if lv_:
                                shelves = (" The mapped shelves below right now: " +
                                           ", ".join(lv_) + " — the planner flips to "
                                           "“at the level” when price reaches one.")
                        elif s0_ and g_ > 0:
                            lv_ = []
                            if r0_.get("dem"):
                                lv_.append(f"{r0_['dem'][1]:,.0f} (day-band edge)")
                            lv_ += [f"{fmt_strike(w.strike)}C wall"
                                    for w in (r0_.get("calls") or []) if w.strike > s0_][:2]
                            if lv_:
                                shelves = " The mapped ceilings above right now: " + ", ".join(lv_) + "."
                    except Exception:
                        pass
                    on_txt = (f"<b>This morning's gap:</b> opened {g_:+.2f}% vs yesterday's close. "
                              f"“Gaps always fill” is folklore — gaps this size fully filled "
                              f"same-day only {f_}% of the time, and the day closed in the gap's "
                              f"direction {k_}% of the time. And {cont} — chasing the first "
                              f"bounce at the open has been the expensive habit.{shelves}")
        else:
            fpx = futs.get("ES=F")
            if fpx is not None and len(fpx):
                es = fpx.Close
                ref_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
                if now_et.hour < 16:
                    ref_t -= dt.timedelta(days=1)
                while ref_t.weekday() >= 5:
                    ref_t -= dt.timedelta(days=1)
                es_ref = float(es.asof(ref_t))
                seg = es[es.index > ref_t]
                if es_ref == es_ref and es_ref > 0 and len(seg):
                    onr = 100 * (float(seg.iloc[-1]) / es_ref - 1)
                    ohi = 100 * (float(seg.max()) / es_ref - 1)
                    olo = 100 * (float(seg.min()) / es_ref - 1)
                    on_stats = (onr, olo, ohi)
                    on_txt = (f"<b>Overnight (futures):</b> ES {onr:+.2f}% since the 4pm close "
                              f"(range {olo:+.2f}% … {ohi:+.2f}%) — the 24h tab on each chart "
                              f"shows it against the same walls. ")
                    f_, k_ = gap_rates(onr)
                    if f_:
                        on_txt += (f"If that holds to the open: “gaps always fill” is "
                                   f"folklore — openings this size fully filled same-day only "
                                   f"{f_}% of the time and closed in the gap's direction {k_}%.")
                    else:
                        on_txt += "So far it's inside the noise band (moves under 0.3% carry no lean)."
    except Exception:
        pass
    pulse = _with_timeout(breadth_pulse, 45) or ""
    breadth_vcls = None
    try:
        breadth_vcls = json.load(open("data/breadth.json"))["vcls"]
    except Exception:
        pass
    try:
        my_json = json.dumps(json.load(open("data/mystats.json")))
    except Exception:
        my_json = "null"
    ext_txt = None
    try:
        mopen2 = now_et.weekday() < 5 and 570 <= now_et.hour * 60 + now_et.minute < 960
        if mopen2 and spx_daily.get("broke") and spx_daily.get("dem_ae"):
            a_, e_ = spx_daily["dem_ae"]
            ext_txt = (f"<b class=w>Day band broken.</b> Once the band breaks, price has reached "
                       f"1.5× it about 1 day in 3 and 2× about 1 in 10 (median break runs ~1.3×) — "
                       f"the 1.5× cloud ring sits at {a_-1.5*e_:,.0f} / {a_+1.5*e_:,.0f} on the "
                       f"charts. Past the 2× ring ({a_-2*e_:,.0f} / {a_+2*e_:,.0f}) is rare-day territory.")
    except Exception:
        ext_txt = None
    prep = None
    closed_now = not (now_et.weekday() < 5 and 570 <= now_et.hour * 60 + now_et.minute < 960)
    if closed_now:
        try:
            nxt = now_et.date() + dt.timedelta(days=1) if now_et.hour >= 16 else now_et.date()
            while nxt.weekday() >= 5:
                nxt += dt.timedelta(days=1)
            inval = None
            r0 = read_data.get("SPX") or {}
            if r0:
                s0 = r0["spot"]
                dem = r0.get("dem")
                up = min((c.strike for c in r0["calls"] if c.strike >= s0), default=None)
                dn = max((p.strike for p in r0["puts"] if p.strike <= s0), default=None)
                bits = []
                if dem:
                    a15 = (dem[0] + dem[1]) / 2
                    e15 = (dem[1] - dem[0]) / 2
                    bits.append(f"outside {dem[0]:,.0f}–{dem[1]:,.0f} (the priced band) → gap rules "
                                f"apply, and meaningful gaps mostly don't fill; past 1.5× the band "
                                f"({a15-1.5*e15:,.0f} / {a15+1.5*e15:,.0f}) → only ~1 break in 3 "
                                f"travels that far — real-news territory")
                if up and dn:
                    bits.append(f"between the {fmt_strike(dn)}P/{fmt_strike(up)}C walls → "
                                f"the map mostly carries into the open")
                if bits:
                    inval = ("<b>Overnight checklist</b> — before the open, see where ES (→SPX) sits: "
                             + "; ".join(bits) + ".")
            prep = {"next": f"{nxt:%a %b %-d}", "oi_stale": now_et.hour >= 16,
                    "autopsy": session_autopsy(spx_daily, now_et,
                                               (read_data.get("SPX") or {}).get("vwap")),
                    "inval": inval}
        except Exception:
            prep = None
    # ---- panic-dip signal section + push (fires only on fresh completed-bar flushes)
    panic_push(panic_new, now_et)
    pan_sec = ""
    try:
        plog_all = json.load(open("data/panic_log.json"))
    except Exception:
        plog_all = []
    try:
        act = {l: p for l, p in pan_state.items()
               if p.get("last") is not None and p["last"] < 15}
        opens = [e for e in plog_all if e.get("status") == "open"]
        if act or opens:
            done = [e for e in plog_all if e.get("status") == "done" and e.get("ret") is not None]
            bits = [f"<b>{l}</b> RSI₂ {p['last']:g} ({p['strength']}/3 cells)"
                    for l, p in act.items()]
            if not bits and opens:
                bits = [f"tracking {len(opens)} open signal{'s' if len(opens) > 1 else ''} "
                        f"({', '.join(sorted({e['label'] for e in opens}))})"]
            rl = ""
            if done:
                wins = sum(1 for e in done if e["ret"] > 0)
                rl = (f" Paper record: {wins}–{len(done) - wins}, "
                      f"avg {sum(e['ret'] for e in done) / len(done):+.2f}%/trade.")
            pan_sec = ('<div class=pansec>🟢 <b>Panic-dip signal · 30m</b> — ' + ", ".join(bits) +
                       ". The one survivor of the 167-config study: deep 30-minute flush → long lean, "
                       "exit RSI&gt;70 or ~2 days. Post-haircut Sharpe ≈1.0; the profit lives in the "
                       "scariest few signals, so it only works taking every one. Paper-tracked, "
                       "not advice — the record earns or loses its seat at the Sep 22 audit." + rl +
                       ' Arrows on the Week tab mark fires.</div>')
    except Exception as e_:
        print("pan_sec failed:", repr(e_))
        pan_sec = ""
    # ---- the ladder: ranked support/ceiling zones for SPX + QQQ, every build ----
    ladder_sec = ""
    try:
        gap_now = None
        if closed_now and on_stats:
            gap_now = on_stats[0]
        elif spx_daily.get("ret") is not None:
            gap_now = spx_daily["ret"]
        up_open = " open" if (gap_now is not None and gap_now > 0.3) else ""
        blocks = []
        for label in ("SPX", "QQQ"):
            r = read_data.get(label)
            sm_ = series_map.get(label)
            if not r or not sm_:
                continue
            s = r["spot"]
            spy_pack = series_map.get("SPY", {}).get("lv") if label == "SPX" else None
            spy_spot = (read_data.get("SPY") or {}).get("spot") if label == "SPX" else None
            common = dict(lv_tabs=sm_.get("lv") or {}, dem=r.get("dem"),
                          day_bars=(sm_.get("d") or {}).get("c"),
                          daily_px=idx_dailies.get(label), bigs=bigs,
                          spy_pack=spy_pack, spy_spot=spy_spot)
            dn_t = ladder_tiers(label, s, -1, **common)
            up_t = ladder_tiers(label, s, +1, **common)
            if not dn_t and not up_t:
                continue
            mark = {"px": s, "lbl": "now"}
            if closed_now:
                fq_ = futs.get(FUT.get(label))
                if fq_ is not None and len(fq_) and fscale.get(label):
                    eqv_ = float(fq_.Close.iloc[-1]) * fscale[label]
                    pr_ = "ES" if label != "QQQ" else "NQ"
                    mark = {"px": eqv_, "lbl": f"{pr_}→{label} {100*(eqv_/s-1):+.2f}% ≈"}
            dn_mark = mark if mark["px"] <= s else None
            up_mark = mark if mark["px"] > s else None
            b_ = [f'<h3 class=lh>{label} <span class=m>below — if it breaks down</span></h3>']
            if dn_t:
                b_.append(ladder_table(label, s, -1, dn_t, mark=dn_mark))
            else:
                b_.append('<div class=m>no mapped structure below (thin chains)</div>')
            if up_t:
                b_.append(f'<details class=ladup{up_open}><summary>{label} above — '
                          f'where rips stall</summary>'
                          f'{ladder_table(label, s, +1, up_t, mark=up_mark)}</details>')
            blocks.append("".join(b_))
        if blocks:
            if closed_now and on_stats:
                lnote = (f"ES {on_stats[0]:+.2f}% overnight (range {on_stats[1]:+.2f}% … "
                         f"{on_stats[2]:+.2f}%) — the → row shows where futures put each "
                         f"index on its ladder right now.")
            elif gap_now is not None:
                lnote = (f"SPX {gap_now:+.2f}% on the day — rebuilt every ~10 min; "
                         f"the → row tracks live price between builds.")
            else:
                lnote = "Rebuilt every build; the → row tracks live price."
            ladder_sec = (
                '<h2 id=ladder>Ladder <span>ranked shelves — where a move should catch or stall</span></h2>'
                f'<div class=lnote>{lnote}</div>' + "".join(blocks) +
                '<p class=m style="font-size:11.5px;margin:8px 0 0">Rank = how much independent '
                'structure stacks in a zone (walls sized by open interest, band edges, extension '
                'rings, 50/200-day averages, session extremes, biggest-premium strikes, SPY strikes '
                'mapped onto SPX) — evidence <i>density</i>, not a measured hold rate. Measured '
                'context: price finishes inside the day band ~3 days in 4; once a band breaks, '
                '~1 in 3 breaks reach the 1.5× ring and ~1 in 10 the 2× ring. Per-wall hold rates '
                'get their formal audit when the archive matures (Sep 22). A ladder says where '
                'a move has reasons to pause — never which way it goes.</p>')
    except Exception as e_:
        print("ladder failed:", repr(e_))
        ladder_sec = ""
    mline = plain_read(read_data, regime_dist, breadth_vcls, prem_rank, vix_note, spx_daily,
                       rlz=rlz, is_opex=is_opex, on_txt=on_txt, term=term, prep=prep,
                       ext_txt=ext_txt, prem_streak=prem_streak)
    alike_html = similar_days(spx_daily.get("px5"), spx_daily.get("spot"),
                              float(vh.iloc[-1]) if vh is not None and len(vh) else None,
                              now_et)
    lv = {}
    for label, r in read_data.items():
        s = r["spot"]
        wd = 0.03 if label in STOCK_SET else 0.015
        lv[label] = {
            "spot": s, "lo": s * (1 - wd), "hi": s * (1 + wd),
            "up": min((cw.strike for cw in r["calls"] if cw.strike >= s), default=None),
            "dn": max((pw.strike for pw in r["puts"] if pw.strike <= s), default=None),
        }
    lv_json = json.dumps(lv)
    open("docs/index.html", "w").write(f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=theme-color content="#0d0d0f">
<link rel=manifest href=manifest.json><link rel=apple-touch-icon href=icon.png>
<link rel=icon href=icon.png><title>Levels</title><style>
body{{background:#0d0d0f;color:#e8e8ea;font:15px -apple-system,system-ui,sans-serif;margin:0;padding:14px;max-width:760px;margin:auto}}
h1{{font-size:19px;margin:0 0 2px}}h2{{font-size:17px;margin:30px 0 8px}}h2 span{{color:{GRAY};font-weight:400;font-size:13px}}
img{{width:100%;border-radius:8px;margin-top:8px}}
table{{width:100%;border-collapse:collapse;margin:10px 0 8px;font-size:13.5px}}
td,th{{padding:6px 6px;border-bottom:1px solid #26262b;text-align:right;white-space:nowrap}}
td:first-child,th:first-child{{text-align:left}}th{{color:{GRAY};font-weight:500;font-size:12px}}
.c{{color:{PURPLE}}}.p{{color:{YELLOW}}}.m{{color:{GRAY};font-family:monospace;font-size:10.5px;font-weight:400}}
tr.hot td,tr.hot td b{{color:#fff;font-weight:600}}tr.hot td .m{{color:#9a9aa2}}
.dv{{color:{TEAL};font-size:11.5px;font-weight:600}}
.chips{{display:flex;gap:6px;flex-wrap:wrap}}
.chip{{border:1px solid #2c2c33;background:#15151a;border-radius:8px;padding:5px 9px;font-size:12.5px;white-space:nowrap}}
.chip.c{{color:{PURPLE}}}.chip.p{{color:{YELLOW}}}.chip.v{{color:{BLUE}}}.chip.e{{color:#9a9aa2}}
.chip.hot{{color:#fff;border-color:#4a4a55}}
.btn{{display:inline-block;background:#1c1c22;color:#e8e8ea;padding:8px 14px;border-radius:8px;text-decoration:none;font-size:13px}}
.u{{color:{GRAY};font-size:12px;margin:2px 0 10px}}
.tw input{{display:none}}
.tw label,.tw .tb{{display:inline-block;background:#15151a;border:1px solid #2c2c33;border-radius:8px;padding:5px 12px;font-size:12.5px;color:#9a9aa2;margin:8px 6px 0 0;cursor:pointer;font-family:inherit}}
.tw input:checked+label,.tw .tb.on{{color:#fff;border-color:#4a4a55;background:#1c1c22}}
.tw img{{display:none}}
.tw input.t0:checked~img.i0,.tw input.t1:checked~img.i1,.tw input.t2:checked~img.i2,.tw input.t3:checked~img.i3{{display:block}}
.chart{{height:330px;margin-top:8px;border-radius:8px;overflow:hidden;background:#101014;position:relative}}
.covr{{position:absolute;top:6px;left:8px;z-index:6;pointer-events:none;font-size:10.5px;
line-height:1.55;color:#c9c9cf;background:rgba(13,13,15,.62);border-radius:6px;padding:3px 8px;
font-variant-numeric:tabular-nums}}
.covr b{{color:#3fd0a4;font-weight:600}}
.tw noscript img{{display:block}}
.chip.good{{color:{TEAL};border-color:#2a4a42}}.chip.warn{{color:{RED};border-color:#4a2a2a}}.chip.mixed{{color:#9a9aa2}}
.ladder{{display:flex;gap:10px;margin:12px 0 2px}}
.lcol{{flex:1;text-decoration:none;color:#e8e8ea;min-width:0}}
.lh{{font-size:13px;color:{GRAY};margin-bottom:4px}}.lh b{{color:#e8e8ea;font-size:14px}}
.lt{{position:relative;height:210px;background:#121216;border:1px solid #1e1e24;border-radius:8px;overflow:hidden}}
.lw{{position:absolute;left:8%;right:34%}}.lw i{{display:block;height:3px;border-radius:2px}}
.lw.c i{{background:{PURPLE}}}.lw.p i{{background:{YELLOW}}}.lw.hot i{{box-shadow:0 0 0 1px #fff;height:4px}}
.ll{{position:absolute;right:5%;transform:translateY(-45%);font-size:11.5px}}
.ll.c{{color:{PURPLE}}}.ll.p{{color:{YELLOW}}}.ll.hot{{color:#fff;font-weight:700}}
.lspot{{position:absolute;left:0;right:0;border-top:1px dashed {TEAL};opacity:.75}}
.leg{{color:{GRAY};font-size:12px;margin:8px 0 0}}
.leg i{{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin:0 3px}}
.mline{{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 4px}}
.say{{background:#101416;border:1px solid #1e1e24;border-radius:12px;padding:12px 14px;margin:10px 0 6px;
font-size:14.5px;line-height:1.65;color:#d8d8dc}}
.say p{{margin:0 0 9px}}.say p:last-child{{margin:0}}
.say b{{color:#fff}}.say b.w{{color:{RED}}}.say i{{font-style:italic}}
.say .rc{{color:{GRAY};font-size:12px}}
.say .key{{color:{GRAY};font-size:12px;line-height:1.55;border-top:1px solid #1e1e24;padding-top:8px}}
.say .kc{{color:{PURPLE}}}.say .kp{{color:{YELLOW}}}
.say .hzt{{color:{TEAL};font-weight:600}}
h1 .hdr{{color:{GRAY};font-weight:400;font-size:12px;float:right;margin-top:6px}}
h2{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
h2 em{{font-style:normal;color:#e8e8ea}}h2 .gd{{font-weight:400;margin-left:auto;font-size:13.5px}}
h2 .gd b.warn{{color:{RED}}}
details.more{{margin:6px 0 0}}details.more summary{{color:{GRAY};font-size:12.5px;cursor:pointer;padding:4px 0}}
.gtr{{position:relative;flex:1;height:26px;background:#121216;border:1px solid #1e1e24;border-radius:6px}}
.gt{{position:absolute;top:5px;bottom:5px;width:2px;border-radius:1px;transform:translateX(-50%)}}
.gt.c{{background:{PURPLE}}}.gt.p{{background:{YELLOW}}}.gt.h{{width:4px}}
.gt.g{{background:#8b8b93;top:9px;bottom:9px}}
.gt.gm{{background:#fff;top:3px;bottom:3px}}
.gt.pn{{background:#55555e;width:7px;height:7px;top:50%;bottom:auto;transform:translate(-50%,-50%) rotate(45deg);border-radius:1px}}
.gt.vw{{background:{BLUE};top:8px;bottom:8px}}
.gt.px{{background:{TEAL};width:9px;height:9px;top:50%;bottom:auto;border-radius:50%;transform:translate(-50%,-50%);box-shadow:0 0 0 2px #0d0d0f}}
.arow{{display:flex;align-items:center;gap:8px;margin:7px 0;font-size:12px}}
.alab{{width:72px;color:#9a9aa2;flex:none}}
.astr{{position:relative;flex:1;height:16px;background:#15151a;border:1px solid #26262b;border-radius:8px;overflow:hidden}}
.az{{position:absolute;top:0;bottom:0;width:1px;background:#3a3a44}}
.ab{{position:absolute;top:4px;bottom:4px;background:#2e2e3a;border-radius:4px}}
.astr .gt{{top:2px;bottom:2px}}
.gt.am{{background:{TEAL};width:3px}}
.gt.aa{{background:#8b8b93;width:2px;opacity:.85}}
.aval{{flex:none;min-width:116px;text-align:right;color:#e8e8ea}}
.aval.rv{{min-width:132px;font-size:11px}}
.ab.dd{{background:rgba(239,83,80,.30)}}
.ab.uu{{background:rgba(63,208,164,.22)}}
.gt.bd{{background:{RED};width:2px}}
.gt.bu{{background:{TEAL};width:2px}}
.gt.kc{{background:{PURPLE};width:3px}}.gt.kp{{background:{YELLOW};width:3px}}
.gt.be{{background:#fff;width:2px;opacity:.9;top:8px;bottom:8px}}
#posbar .gtr{{height:22px;margin:7px 0 5px}}
#posbar .pb b.w{{color:{RED}}}
#alrt{{cursor:pointer}}
#alrt.on{{color:#0d0d0f;background:{TEAL};border-color:{TEAL};font-weight:600}}
#abar .ab-n{{border:1px solid #2a4a42;background:#0f1a16;border-radius:10px;padding:8px 11px;
margin:8px 0;font-size:13px;color:#d9f5ec;animation:abin .25s ease-out}}
#abar .ab-n b{{color:{TEAL}}}
@keyframes abin{{from{{opacity:0;transform:translateY(-4px)}}to{{opacity:1}}}}
em,#live,.gd,.aval,#posbar .pb,.chip{{font-variant-numeric:tabular-nums}}
.gd{{font-size:11.5px;color:#9a9aa2;white-space:nowrap}}
.gcap{{color:{GRAY};font-size:11px;margin:4px 0 0}}
.gcap .sw{{display:inline-block;width:9px;height:3px;vertical-align:middle;margin:0 3px 0 6px}}
details.lg{{margin:18px 0}}details.lg summary{{color:{GRAY};font-size:12.5px;cursor:pointer}}
.pbtn{{background:#15151a;border:1px solid #2c2c33;border-radius:8px;color:#9a9aa2;font-size:11.5px;padding:3px 9px;cursor:pointer;font-family:inherit}}
.pbtn.set{{color:{TEAL};border-color:#2a4a42;font-weight:600}}
.pform{{display:flex;gap:6px;margin:8px 0;flex-wrap:wrap}}
.pform select,.pform input{{background:#15151a;border:1px solid #2c2c33;border-radius:8px;color:#e8e8ea;padding:6px 8px;font-size:13px;width:92px;font-family:inherit}}
.pform select{{width:70px}}
.pform button{{background:#1c1c22;border:1px solid #2c2c33;border-radius:8px;color:#e8e8ea;font-size:12.5px;padding:6px 12px;cursor:pointer;font-family:inherit}}
#posbar .pb{{border:1px solid #2a4a42;background:#101416;border-radius:10px;padding:9px 11px;margin:8px 0;font-size:13.5px;line-height:1.55}}
#posbar .pb.w{{border-color:#4a2a2a;background:#141012}}
#posbar .pb b{{color:{TEAL}}}#posbar .pb.w b{{color:{RED}}}
#posbar .pb .sm{{color:#9a9aa2;font-size:12px}}
#posbar .pb a{{color:#e8e8ea;text-decoration:none}}
.plan{{width:100%;font-size:12.5px;color:#9a9aa2;line-height:1.6;background:#101416;border:1px solid #1e1e24;border-radius:8px;padding:8px 10px}}
.plan b{{color:#e8e8ea}}.plan .c{{color:{PURPLE}}}.plan .p{{color:{YELLOW}}}.plan .tm{{color:{TEAL}}}
.plan .oknow{{color:{TEAL}}}.plan b.w{{color:{RED}}}
.my{{color:#c9a5ff;font-weight:600}}
.al{{font-size:11.5px;color:{TEAL}}}
.lh{{font-size:14px;margin:14px 0 2px}}.lh .m{{font-weight:400}}
.lnote{{color:{GRAY};font-size:12.5px;margin:2px 0 4px}}
table.lad{{margin:4px 0 6px}}
table.lad td{{border-bottom:1px solid #1c1c21;padding:5px 6px;text-align:left;white-space:normal;vertical-align:top}}
td.lrk{{width:22px;text-align:center;font-weight:700;color:#9a9aa2;background:#17171b;border-radius:6px;font-size:12.5px}}
td.lrk.l1{{color:#0d0d0f;background:{YELLOW}}}
td.lrk.l2{{color:#0d0d0f;background:#8f8f97}}
.lz{{font-weight:600;white-space:nowrap}}
.lstr{{color:{YELLOW};font-size:8.5px;letter-spacing:1.6px;margin-top:1px}}
.lbits{{color:#9a9aa2;font-size:11.5px;line-height:1.45}}
tr.lair td{{color:#5c5c64;font-size:11px;text-align:center;font-style:italic;border-bottom:none;padding:2px 6px}}
tr.lmk td{{color:{TEAL};font-weight:700;text-align:center;border-bottom:none;padding:2px 6px}}
details.ladup{{margin:2px 0 10px}}
details.ladup summary{{color:{GRAY};font-size:12.5px;cursor:pointer}}
.pansec{{background:#0f1a16;border:1px solid #1e3a30;border-radius:8px;padding:9px 11px;margin:10px 0 4px;font-size:13px;line-height:1.55;color:#c9d4cf}}
.pansec b{{color:{TEAL}}}
</style></head><body>
<h1>Levels <span class=hdr id=hdr>levels {now_et.strftime('%-I:%M %p ET')} · refresh ~10 min</span></h1>
{mline}
{pan_sec}
{ladder_sec}
{alike_html}
<div class=mline><span class="chip e" id=live style="display:none"></span>
<button class="chip e" id=alrt title="get tapped when price reaches a mapped level (wall, band edge, your strike/break-even) while the page is open. For pushes with the app CLOSED — including overnight futures — install the free ntfy app and subscribe to topic levels-drk-56c5e740 (tap this button for the steps).">🔔 alerts off</button></div>
<div id=abar></div>
<div id=posbar></div>
{"".join(cards)}
{scanner_html(scan_rows, (json.loads(my_json) or {}).get("by_und") if my_json != "null" else None)}
{big_html(bigs)}
{"".join(scards)}
<details class=lg><summary>breadth — S&P internals (tap)</summary>{breadth_section(v, pulse)}</details>
<details class=lg><summary>legend — how to read every element</summary>
<p class=leg><i style="background:{PURPLE}"></i>call wall <i style="background:{YELLOW}"></i>put wall
<i style="background:{BLUE}"></i>VWAP · <b style="color:#fff">white</b> = today's vol &gt; OI (fresh flow) · bar length = OI · OI resets pre-market ·
EM = expected move (ATM straddle: d = today from prev close, w = by Friday from last Fri close) ·
1.5×/2×EM = extension zones: once the day band breaks, moves reached 1.5× ~1 in 3 days and 2× ~1 in 10 (2×EM lines appear only after a break) ·
pin = max pain (where option sellers most want price to close) ·
<span class=dv>+Δ</span> = vol added since last build ·
breadth verdict: swing-horizon context, not a day-trade trigger (weak breadth often bounces short-term) ·
3mo chart dashes = 20d/50d MAs — the regime filter: long edge lives above the 50d ·
charts rendered with <a href="https://www.tradingview.com/lightweight-charts/" style="color:#6b6b73">TradingView Lightweight Charts™</a></p></details>
<script src="lw.js?v={v}"></script>
<script id=SD type=application/json>{json.dumps(series_map)}</script>
<script>
const LV = {lv_json};
const PREM = {json.dumps(prem_rank)};
const MY = {my_json};
const FSC = {json.dumps(fscale)};
const HZP = {json.dumps(hzp)};
const WSYMS = {{"^GSPC": "SPX", "SPY": "SPY", "QQQ": "QQQ", "NVDA": "NVDA", "TSLA": "TSLA",
               "AAPL": "AAPL", "META": "META", "ES=F": "ES", "NQ=F": "NQ"}};
const WFUT = {{ES: ["SPX", "SPY"], NQ: ["QQQ"]}};
const BUILT = {json.dumps(now_et.strftime('%-I:%M %p ET'))};
const PAGE_TS = {int(now_utc.timestamp())};
const SD = JSON.parse(document.getElementById("SD").textContent);
// series arrive packed as [t,o,h,l,c] / [t,v] — expand once
for (const lab in SD) for (const tf of ["d", "w", "m", "o"]) {{
  const S = SD[lab][tf]; if (!S) continue;
  S.c = (S.c || []).map(a => ({{time: a[0], open: a[1], high: a[2], low: a[3], close: a[4]}}));
  S.v = (S.v || []).map(a => ({{time: a[0], value: a[1]}}));
}}
const CH = {{}};
const LAST = {{}};
const POS = JSON.parse(localStorage.getItem("oiwalls_pos") || "{{}}");
const OPTS = {{
  layout: {{background: {{color: "#101014"}}, textColor: "#6b6b73", fontSize: 11,
           attributionLogo: false}},
  grid: {{vertLines: {{color: "#1a1a20"}}, horzLines: {{color: "#1a1a20"}}}},
  crosshair: {{mode: 0}},
  rightPriceScale: {{borderColor: "#26262b"}},
  timeScale: {{borderColor: "#26262b", timeVisible: true, secondsVisible: false}},
  localization: {{locale: "en-US"}},
}};
// ---------- probability cloud + OI profile: zones as fields, strikes as bars ----------
function cloudPrim(lab) {{
  const view = {{
    zOrder() {{ return "bottom"; }},
    renderer() {{
      return {{
        draw(target) {{
          target.useBitmapCoordinateSpace(scope => {{
            try {{
              const o = CH[lab]; if (!o) return;
              const ctx = scope.context, W = scope.bitmapSize.width, vr = scope.verticalPixelRatio;
              const Y = p => {{ const y = o.cs.priceToCoordinate(p); return y == null ? null : y * vr; }};
              const tf = o.tf;
              const g = SD[lab].lv[tf] || [];
              const tag = (tf === "d" || tf === "o") ? "dEM" : (tf === "w" ? "wEM" : "mEM");
              const band = g.filter(l => l.t.indexOf(tag) >= 0).map(l => l.p).sort((a, b) => a - b);
              if (band.length >= 2) {{
                const lo = band[0], hi = band[band.length - 1];
                const c0 = (lo + hi) / 2, e = (hi - lo) / 2;
                const zone = (pLo, pHi, fill) => {{
                  const y1 = Y(pHi), y2 = Y(pLo);
                  if (y1 == null || y2 == null) return;
                  ctx.fillStyle = fill;
                  ctx.fillRect(0, Math.min(y1, y2), W, Math.abs(y2 - y1));
                }};
                const edge = (p, col) => {{
                  const y = Y(p); if (y == null) return;
                  ctx.fillStyle = col;
                  ctx.fillRect(0, y - 0.5 * vr, W, 1 * vr);
                }};
                // density = probability price stays inside: darkest core, fading rings
                zone(lo, hi, "rgba(139,139,147,0.10)");
                if (tf === "d" || tf === "o") {{
                  zone(c0 + e, c0 + 1.5 * e, "rgba(224,168,0,0.055)");
                  zone(c0 - 1.5 * e, c0 - e, "rgba(224,168,0,0.055)");
                  zone(c0 + 1.5 * e, c0 + 2 * e, "rgba(239,83,80,0.045)");
                  zone(c0 - 2 * e, c0 - 1.5 * e, "rgba(239,83,80,0.045)");
                  edge(c0 + 1.5 * e, "rgba(224,168,0,0.35)");
                  edge(c0 - 1.5 * e, "rgba(224,168,0,0.35)");
                  edge(c0 + 2 * e, "rgba(239,83,80,0.40)");
                  edge(c0 - 2 * e, "rgba(239,83,80,0.40)");
                }}
              }}
              // OI profile fused onto the right edge: bar length = open interest
              const walls = g.filter(l => (l.k === "c" || l.k === "p" || l.k === "b") && l.oi);
              const mx = Math.max(1, ...walls.map(w => w.oi));
              for (const w of walls) {{
                const y = Y(w.p); if (y == null) continue;
                const len = Math.max(6, (w.oi / mx) * 64) * scope.horizontalPixelRatio;
                ctx.fillStyle = w.k === "c" ? "rgba(162,89,255,0.50)"
                              : (w.k === "p" ? "rgba(224,168,0,0.50)" : "rgba(216,216,222,0.55)");
                ctx.fillRect(W - len, y - 2 * vr, len, 4 * vr);
              }}
            }} catch (e) {{}}
          }});
        }}
      }};
    }}
  }};
  return {{ paneViews() {{ return [view]; }}, updateAllViews() {{}} }};
}}
function mkChart(lab) {{
  const el = document.getElementById("ch-" + lab);
  if (!el || !window.LightweightCharts) return;
  const ch = LightweightCharts.createChart(el, OPTS);
  const cs = ch.addCandlestickSeries({{upColor: "#26a69a", downColor: "#ef5350",
    wickUpColor: "#26a69a", wickDownColor: "#ef5350", borderVisible: false}});
  try {{ cs.attachPrimitive(cloudPrim(lab)); }} catch (e) {{}}
  const vw = ch.addLineSeries({{color: "#4ea3ff", lineWidth: 2, priceLineVisible: false,
    lastValueVisible: false, crosshairMarkerVisible: false}});
  const ov = document.createElement("div");
  ov.className = "covr";
  el.appendChild(ov);
  CH[lab] = {{ch, cs, vw, ov, tf: "d", lines: []}};
  setTF(lab, "d");
  new ResizeObserver(() => ch.applyOptions({{width: el.clientWidth}})).observe(el);
}}
function updOverlay(lab) {{
  const o = CH[lab]; if (!o || !o.ov) return;
  const px = LAST[lab] || LV[lab].spot;
  const g = SD[lab].lv[o.tf] || [];
  const L1 = [];
  if (o.tf === "d" || o.tf === "o") {{
    const pin = g.find(l => l.t === "pin");
    if (pin) L1.push("pin " + pin.p);
    const fl = null;
    if (fl) L1.push("flip " + fl.p.toFixed(0));
  }}
  const up = g.filter(l => (l.k === "c" || l.k === "b") && l.p >= px).sort((a, b) => a.p - b.p)[0];
  const dn = g.filter(l => (l.k === "p" || l.k === "b") && l.p <= px).sort((a, b) => b.p - a.p)[0];
  const L2 = [];
  const fp2 = v => (Math.round(v * 100) / 100).toLocaleString();
  if (dn) L2.push(`▼${{fp2(dn.p)}} −${{((1 - dn.p / px) * 100).toFixed(2)}}%`);
  if (up) L2.push(`▲${{fp2(up.p)}} +${{((up.p / px - 1) * 100).toFixed(2)}}%`);
  const p = POS[lab];
  if (p) L2.push(`<b>you: ${{p.k}}${{p.side}}</b>`);
  o.ov.innerHTML = L1.join(" · ") + (L2.length ? "<br>" + L2.join(" · ") : "");
}}
function updMarkers(lab) {{
  const o = CH[lab]; if (!o) return;
  if (o.tf !== "d") {{
    if (o.tf === "w" && SD[lab].ps && SD[lab].ps.length) {{
      // panic-dip fires (30m study survivor): teal arrows under the flush bars
      const pm = SD[lab].ps.map(p => ({{time: p[0], position: "belowBar",
        color: p[1] >= 3 ? "#3fd0a4" : "#2e8f77", shape: "arrowUp",
        text: "dip " + p[1] + "/3"}}));
      try {{ o.cs.setMarkers(pm.slice(-12)); }} catch (e) {{}}
    }} else {{ try {{ o.cs.setMarkers([]); }} catch (e) {{}} }}
    return;
  }}
  const S = SD[lab].d; if (!S || !S.c.length) return;
  const vs = SD[lab].vs && SD[lab].vs.d;
  const bars = vs ? S.c.filter(b => b.time >= vs) : S.c;
  const mk = [];
  for (const w of (SD[lab].lv.d || [])) {{
    if (w.k !== "c" && w.k !== "p" && w.k !== "b") continue;
    const hit = bars.find(b => b.low <= w.p && b.high >= w.p);
    if (!hit) continue;
    const above = w.k === "c" || (w.k === "b" && w.p >= hit.close);
    mk.push({{time: hit.time, position: above ? "aboveBar" : "belowBar",
      color: w.k === "b" ? "#d8d8de" : (w.k === "c" ? "#a259ff" : "#e0a800"), shape: "circle",
      text: (w.k === "b" ? "C+P " : (w.k === "c" ? "C" : "P")) + (Math.round(w.p * 100) / 100)}});
  }}
  mk.sort((a, b) => a.time - b.time);
  try {{ o.cs.setMarkers(mk.slice(0, 10)); }} catch (e) {{}}
}}
function setTF(lab, tf) {{
  const o = CH[lab]; if (!o) return;
  const S = SD[lab][tf] || {{c: [], v: []}};
  o.tf = tf;
  o.cs.setData(S.c);
  o.vw.setData(tf === "d" ? S.v : []);
  o.lines.forEach(l => o.cs.removePriceLine(l.ln || l));
  o.lines = [];
  const COLS = {{c: "#a259ff", p: "#e0a800", g: "#8b8b93", b: "#d8d8de"}};
  for (const l of (SD[lab].lv[tf] || [])) {{
    const ln = o.cs.createPriceLine({{price: l.p, color: l.h ? "#ffffff" : COLS[l.k],
      lineWidth: (l.h || l.m) ? 2 : 1, lineStyle: l.k === "g" ? 3 : (l.h ? 0 : 2),
      axisLabelVisible: true, title: l.t}});
    o.lines.push({{ln, p: l.p, k: l.k, t: l.t, vis: true}});
  }}
  relabel(lab);
  o.ch.applyOptions({{timeScale: {{timeVisible: tf !== "m", secondsVisible: false}}}});
  const V = SD[lab].vs && SD[lab].vs[tf];
  if (V && S.c.length > 5)
    o.ch.timeScale().setVisibleRange({{from: V, to: S.c[S.c.length - 1].time + 600}});
  else
    o.ch.timeScale().fitContent();
  updMarkers(lab);
  updOverlay(lab);
  document.querySelectorAll(`.tb[data-l=${{lab}}]`).forEach(b =>
    b.classList.toggle("on", b.dataset.t === tf));
}}
document.addEventListener("click", e => {{
  if (e.target.classList && e.target.classList.contains("tb"))
    setTF(e.target.dataset.l, e.target.dataset.t);
}});
Object.keys(SD).forEach(mkChart);

// ---------- position mode ----------
const PLINES = {{}};
function posSave() {{ localStorage.setItem("oiwalls_pos", JSON.stringify(POS)); }}
function relabel(lab) {{
  // visual hierarchy: only the actionable levels get axis labels. Nearest wall
  // each side always; everything else only when within 0.4% of price; the
  // extension ladder only once price is actually outside the day band.
  const o = CH[lab]; if (!o || !o.lines || !o.lines.length) return;
  const px = LAST[lab] || (LV[lab] && LV[lab].spot);
  if (!px) return;
  const dem = demBounds(lab);
  const outside = dem && (px > dem[1] || px < dem[0]);
  let upW = null, dnW = null;
  for (const m of o.lines) {{
    if ((m.k === "c" || m.k === "b") && m.p >= px && (!upW || m.p < upW.p)) upW = m;
    if ((m.k === "p" || m.k === "b") && m.p <= px && (!dnW || m.p > dnW.p)) dnW = m;
  }}
  for (const m of o.lines) {{
    const near = Math.abs(m.p / px - 1) < 0.004;
    let show;
    if (m === upW || m === dnW) show = true;
    else if (m.t.indexOf("×EM") >= 0) show = outside && near;
    else show = near;
    if (show !== m.vis) {{
      m.vis = show;
      try {{ m.ln.applyOptions({{axisLabelVisible: show}}); }} catch (e) {{}}
    }}
  }}
}}
function bandOf(lab, lvk, tag) {{
  const g = (SD[lab].lv[lvk] || []).filter(l => l.t === tag).map(l => l.p).sort((a, b) => a - b);
  return g.length >= 2 ? [g[0], g[1]] : null;
}}
function demBounds(lab) {{ return bandOf(lab, "d", "dEM"); }}
function wallsBetween(lab, a, b, lvk) {{
  const lo = Math.min(a, b), hi = Math.max(a, b);
  return (SD[lab].lv[lvk || "d"] || []).filter(l => (l.k === "c" || l.k === "p" || l.k === "b") && l.p > lo && l.p < hi);
}}
function posCtx(lab, p) {{
  // which horizon this position actually lives on — band, walls set and label
  const Z = HZP[lab] || {{}};
  const e = p.exp || Z.fe;
  if (!e || (Z.fe && e <= Z.fe))
    return {{h: "d", band: demBounds(lab), lvk: "d", word: "today's", exp: e || Z.fe}};
  if (Z.we && e <= Z.we)
    return {{h: "w", band: bandOf(lab, "w", "wEM"), lvk: "w", word: "the week's", exp: e}};
  return {{h: "m", band: bandOf(lab, "m", "mEM") || bandOf(lab, "m", "wEM"),
           lvk: "m", word: "the monthly", exp: e}};
}}
function dteOf(exp) {{
  if (!exp) return null;
  return Math.max(0, Math.round((Date.parse(exp + "T16:00:00-04:00") - Date.now()) / 864e5));
}}
function drawPos(lab) {{
  const o = CH[lab]; if (!o) return;
  (PLINES[lab] || []).forEach(l => o.cs.removePriceLine(l));
  PLINES[lab] = [];
  const p = POS[lab]; if (!p) return;
  PLINES[lab].push(o.cs.createPriceLine({{price: p.k, color: "#3fd0a4", lineWidth: 2,
    lineStyle: 0, axisLabelVisible: true, title: "YOUR " + p.k + p.side}}));
  if (p.be) PLINES[lab].push(o.cs.createPriceLine({{price: p.be, color: "#3fd0a4",
    lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "b/e"}}));
}}
function etMinNow() {{ return Math.floor(etEpoch() % 86400 / 60); }}
// ---------- level-touch alerts: get tapped when price reaches a mapped level ----------
let ALERTS = localStorage.getItem("oiwalls_alerts") === "1";
const ACOOL = {{}};   // "LAB|price-bucket" -> last fired ms (persisted: reopening the
                      // app or the ~10-min auto-refresh must NOT replay old alerts)
try {{
  const s0 = JSON.parse(localStorage.getItem("oiw_acool") || "{{}}");
  for (const k in s0) if (Date.now() - s0[k] < 1200000) ACOOL[k] = s0[k];
}} catch (e) {{}}
function saveCool() {{
  try {{ localStorage.setItem("oiw_acool", JSON.stringify(ACOOL)); }} catch (e) {{}}
}}
// arm-up: for the first moments after open/resume, the tape's current position is
// BASELINE — mark levels it's already at as seen, fire only for what happens next
let ALERT_ARM = Date.now() + 45000;
function alertUi() {{
  const b = document.getElementById("alrt");
  if (b) {{ b.textContent = ALERTS ? "🔔 alerts on" : "🔔 alerts off"; b.classList.toggle("on", ALERTS); }}
}}
function fireAlert(lab, msg) {{
  const bar = document.getElementById("abar");
  if (bar) {{
    const div = document.createElement("div");
    div.className = "ab-n";
    div.innerHTML = `<b>${{lab}}</b> ${{msg}} <span class=m>· ${{new Date().toLocaleTimeString([], {{hour: "2-digit", minute: "2-digit"}})}}</span>`;
    bar.prepend(div);
    while (bar.children.length > 3) bar.removeChild(bar.lastChild);
    setTimeout(() => div.remove(), 240000);
  }}
  try {{ navigator.vibrate && navigator.vibrate([120, 60, 120]); }} catch (e) {{}}
  try {{
    if (typeof Notification !== "undefined" && Notification.permission === "granted")
      new Notification("Levels — " + lab, {{body: msg.replace(/<[^>]+>/g, "")}});
  }} catch (e) {{}}
}}
function levelAlerts(lab, was, px) {{
  if (!ALERTS || px == null || !cashOpen()) return;
  const silent = Date.now() < ALERT_ARM;
  if (was == null) was = px;             // first poll after open: baseline pass
  if (was === px && !silent) return;     // no movement -> nothing new to say
  const lvls = [];
  for (const l of (SD[lab].lv.d || [])) lvls.push({{p: l.p, t: l.t}});
  const p = POS[lab];
  if (p) {{
    lvls.push({{p: p.k, t: "YOUR STRIKE"}});
    if (p.be) lvls.push({{p: p.be, t: "your break-even"}});
  }}
  const lo = Math.min(was, px), hi = Math.max(was, px);
  let dirty = false;
  for (const l of lvls) {{
    const near = Math.abs(px / l.p - 1) <= 0.0006;
    if (!(near || (l.p >= lo && l.p <= hi))) continue;
    // bucketed key: tiny build-to-build drift of a band edge maps to the same
    // cooldown slot instead of minting a "new" level that re-alerts
    const q = l.p >= 2000 ? 5 : 0.5;
    const bk = Math.round(l.p / q) * q;
    const key = lab + "|" + bk;
    const last = Math.max(ACOOL[key] || 0,                    // adjacent buckets too:
                          ACOOL[lab + "|" + (bk - q)] || 0,   // a level drifting across
                          ACOOL[lab + "|" + (bk + q)] || 0);  // a boundary stays cooled
    if (Date.now() - last < 1200000) continue;
    ACOOL[key] = Date.now();
    dirty = true;
    if (!silent) {{
      const down = px < was;
      const ar = down ? "↓" : "↑";
      const crossedL = l.p >= lo && l.p <= hi && was !== px;
      const L = `<b>${{l.t}} ${{l.p.toLocaleString()}}</b>`;
      const move = crossedL ? (down ? `broke below ${{L}}` : `reclaimed ${{L}}`)
                 : px >= l.p ? (down ? `testing ${{L}} from above` : `holding above ${{L}}`)
                             : (down ? `sliding under ${{L}}` : `testing ${{L}} from below`);
      fireAlert(lab, `${{ar}} ${{move}} (px ${{px.toFixed(2)}}) — check the planner`);
    }}
  }}
  if (dirty) saveCool();
}}
// ---- native Web Push: the installed Home-Screen app gets its own push channel,
// ---- and tapping a notification opens THE APP (not Safari). iOS 16.4+.
const VAPID_PUB = "BHUYbUoSV1Z_9DjYDZOtXPEuUGD5DNV0iL0h7sYyJ1DqxA4bmuPgPxjIByGmQ3DoHasZzme8j_AEii3uFEg4ujY";
const REG_DROP = "https://ntfy.sh/levels-drk-56c5e740-reg";
function b64uK(s) {{
  const p = s.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(p + "=".repeat((4 - p.length % 4) % 4)), c => c.charCodeAt(0));
}}
async function postSub(sub) {{
  try {{
    await fetch(REG_DROP, {{method: "POST",
      body: JSON.stringify({{sub: sub.toJSON(), t: Date.now(),
                             ua: navigator.userAgent.slice(0, 60)}})}});
  }} catch (e) {{}}
}}
async function enableNativePush() {{
  try {{
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return false;
    const reg = await navigator.serviceWorker.register("sw.js");
    await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub)
      sub = await reg.pushManager.subscribe({{userVisibleOnly: true,
                                              applicationServerKey: b64uK(VAPID_PUB)}});
    await postSub(sub);
    localStorage.setItem("oiw_webpush", "1");
    return true;
  }} catch (e) {{ return false; }}
}}
// pre-register the worker at load so the 🔔 tap's subscribe stays inside iOS's
// user-activation window; if already enrolled, quietly re-announce the
// subscription (the free dead-drop only retains ~12h — regular opens refresh it)
if ("serviceWorker" in navigator) {{
  navigator.serviceWorker.register("sw.js").then(r => {{
    if (localStorage.getItem("oiw_webpush") === "1" && r.pushManager)
      r.pushManager.getSubscription().then(s => {{ if (s) postSub(s); }}).catch(() => {{}});
  }}).catch(() => {{}});
}}
document.addEventListener("click", e => {{
  if (e.target && e.target.id === "alrt") {{
    ALERTS = !ALERTS;
    localStorage.setItem("oiwalls_alerts", ALERTS ? "1" : "0");
    alertUi();
    if (ALERTS) {{
      try {{ navigator.vibrate && navigator.vibrate(80); }} catch (er) {{}}
      try {{
        if (typeof Notification !== "undefined" && Notification.permission === "default")
          Notification.requestPermission();
      }} catch (er) {{}}
      enableNativePush().then(ok => {{
        if (ok) {{
          fireAlert("alerts", "on — and <b>native pushes armed</b>: closed-app alerts now come " +
            "from Levels itself and tapping one opens the app. Quiet hours built in: no buzzes " +
            "8pm–7am ET — overnight touches log silently and arrive as one 7am recap. " +
            "(ntfy keeps working as a backup — delete or mute it if you only want these.)");
        }} else {{
          fireAlert("alerts", "on — you'll get tapped when price reaches a wall, band edge, or your " +
            "strike/b-e while the page is open. <b>For native closed-app alerts</b> open Levels from " +
            "the Home-Screen icon and tap 🔔 there (iOS only allows push for the installed app). " +
            "Backup channel: the free <b>ntfy</b> app, topic <b>levels-drk-56c5e740</b>.");
        }}
      }});
    }}
  }}
}});
alertUi();
function cockpitStrip(lab, p, px, ctx) {{
  const pts = [px, p.k];
  if (p.be) pts.push(p.be);
  const dem = ctx ? ctx.band : demBounds(lab);
  if (dem) pts.push(dem[0], dem[1]);
  let lo = Math.min(...pts), hi = Math.max(...pts);
  const pad = Math.max((hi - lo) * 0.18, px * 0.0015);
  lo -= pad; hi += pad;
  const P = x => Math.max(1.5, Math.min(98.5, (x - lo) / (hi - lo) * 100));
  let t = "";
  const bandWord = ctx ? ctx.word : "today's";
  if (dem) t += `<i class="gt g" style="left:${{P(dem[0]).toFixed(1)}}%" title="${{bandWord}} band ${{dem[0].toFixed(0)}}"></i>` +
                `<i class="gt g" style="left:${{P(dem[1]).toFixed(1)}}%" title="${{bandWord}} band ${{dem[1].toFixed(0)}}"></i>`;
  for (const w of wallsBetween(lab, lo, hi, ctx && ctx.lvk))
    t += `<i class="gt ${{w.k}}" style="left:${{P(w.p).toFixed(1)}}%" title="${{w.t}} ${{w.p}}"></i>`;
  t += `<i class="gt ${{p.side === "C" ? "kc" : "kp"}}" style="left:${{P(p.k).toFixed(1)}}%" title="your strike ${{p.k}}"></i>`;
  if (p.be) t += `<i class="gt be" style="left:${{P(p.be).toFixed(1)}}%" title="break-even ${{p.be.toFixed(2)}}"></i>`;
  t += `<i class="gt px" style="left:${{P(px).toFixed(1)}}%" title="price ${{px.toFixed(2)}}"></i>`;
  return `<div class=gtr>${{t}}</div>`;
}}
function renderPos() {{
  const bar = document.getElementById("posbar");
  let html = "";
  for (const [lab, p] of Object.entries(POS)) {{
    if (!LV[lab]) continue;
    const px = LAST[lab] || LV[lab].spot;
    const need = (p.k / px - 1) * 100;
    const good = p.side === "C" ? need <= 0 : need >= 0;
    const dir = p.side === "C" ? 1 : -1;
    const needStr = good ? "ITM by " + Math.abs(need).toFixed(2) + "%"
                         : Math.abs(need).toFixed(2) + "% to strike";
    const ctx = posCtx(lab, p);
    const dte = dteOf(ctx.exp);
    const bits = [];
    const dem = ctx.band;
    if (dem) {{
      const inside = p.k >= dem[0] && p.k <= dem[1];
      bits.push(inside ? `strike inside ${{ctx.word}} priced band`
                       : `strike OUTSIDE ${{ctx.word}} priced band (~4 in 5 expiries stay inside)`);
    }}
    const wb = wallsBetween(lab, px, p.k, ctx.lvk);
    if (wb.length) bits.push(wb.length + " wall" + (wb.length > 1 ? "s" : "") + " in path: " +
      wb.slice(0, 3).map(w => (w.k === "c" ? "C" : "P") + w.p).join(", "));
    else bits.push("no walls in path");
    let beStr = "";
    if (p.be) {{
      const bneed = (p.be / px - 1) * 100 * dir;
      beStr = " · b/e-at-exp " + p.be.toFixed(2) + " (" + (bneed <= 0 ? "past it" :
              bneed.toFixed(2) + "% away") + ")";
    }}
    // live clock: what the trade still needs vs what its own horizon has to give
    let clock = "";
    if (cashOpen()) {{
      const mLeft = Math.max(0, 960 - etMinNow());
      let c = `⏱ ${{Math.floor(mLeft / 60)}}h${{String(mLeft % 60).padStart(2, "0")}} to the close`;
      if (ctx.exp && dte != null)
        c += ctx.h === "d" ? ` · <b>expires today</b>` : ` · expires ${{ctx.exp}} (${{dte}}d)`;
      const target = p.be || p.k;
      const needPct = (target - px) / px * 100 * dir;   // + = still needs a move in your direction
      if (dem && needPct > 0) {{
        const room = p.side === "C" ? (dem[1] - px) / px * 100 : (px - dem[0]) / px * 100;
        c += ` · needs ${{needPct.toFixed(2)}}% ${{p.side === "C" ? "up" : "down"}}; ${{ctx.word}} band has ${{Math.max(room, 0).toFixed(2)}}% that way`;
        if (ctx.h === "d" && needPct > Math.max(room, 0))
          c += ` — <b class=w>more than the day usually gives; holding for it is an overnight bet, and your losers were the held ones</b>`;
        if (ctx.h !== "d" && needPct > Math.max(room, 0))
          c += ` — <b class=w>more than the market has priced for the entire life of your option (~4 in 5 stay inside)</b>`;
      }}
      if (dte != null && dte <= 5 && ctx.h !== "d" && MY && MY.exp_n)
        c += ` · <span class=my>your ${{MY.exp_n}} rides-to-zero were median 4 DTE at entry — this is that zone</span>`;
      if (mLeft <= 40) {{
        const agp = p.side === "C" ? "opened >0.5% against a call on ~4% of days (calm tape: 2.6%)"
                                   : "opened >0.5% against a put on ~4% of days (calm tape: 2.5%)";
        c += `<br><b>Closing decision:</b> overnight ${{agp}} — the typical overnight isn't the killer; holding losers extra days is. If this is red right now, your own rule is: decide today.`;
      }}
      clock = `<br><span class=sm>${{c}}</span>`;
    }}
    let ageLine = "";
    if (p.d) {{
      const age = Math.max(0, Math.round((Date.now() - Date.parse(p.d)) / 864e5));
      const dayN = age + 1;
      ageLine = dayN >= 3 ? `<b class=w>day ${{dayN}} of this trade</b> — ` :
                (dayN === 2 ? `<b>day 2 of this trade</b> — ` : "");
    }}
    const warn = !good && (!dem || p.k < dem[0] || p.k > dem[1]);
    let myline = "";
    if (MY) myline = `<br><span class=sm>${{ageLine}}<span class=my>your pattern:</span> winners sold in 1–2 days, losers held 3+ · ${{MY.exp_n}} positions ridden to $0 (−$${{MY.exp_burn.toLocaleString()}}). Decide by day 2.</span>`;
    const expTag = ctx.exp ? ` <span class=sm>${{ctx.h === "d" ? "0DTE" : ctx.exp}}</span>` : "";
    html += `<div class="pb${{warn ? " w" : ""}}"><a href="#${{lab}}"><b>${{lab}} ${{p.k}}${{p.side}}</b></a>${{expTag}}
 · px ${{px.toFixed(2)}} · <b>${{needStr}}</b>${{beStr}}
${{cockpitStrip(lab, p, px, ctx)}}<span class=sm>${{bits.join(" · ")}}</span>${{clock}}${{myline}}</div>`;
  }}
  bar.innerHTML = html;
  for (const [lab] of Object.entries(LV)) {{
    const btn = document.getElementById("pb-" + lab);
    if (btn) {{
      const p = POS[lab];
      btn.textContent = p ? p.k + p.side + " ✎" : "＋ position";
      btn.classList.toggle("set", !!p);
    }}
  }}
}}
document.addEventListener("click", e => {{
  const t = e.target;
  if (t.classList.contains("pbtn")) {{
    const f = document.getElementById("pf-" + t.dataset.l);
    f.style.display = f.style.display === "none" ? "flex" : "none";
    renderPlans();
    const p = POS[t.dataset.l];
    if (p) {{
      document.getElementById("ps-" + t.dataset.l).value = p.side;
      document.getElementById("pk-" + t.dataset.l).value = p.k;
      if (p.prem) document.getElementById("pp-" + t.dataset.l).value = p.prem;
      const pe = document.getElementById("pe-" + t.dataset.l);
      if (pe && p.exp) {{
        for (const o of pe.options) if (o.value === p.exp) {{ pe.value = p.exp; break; }}
      }}
    }}
  }}
  if (t.classList.contains("psave")) {{
    const lab = t.dataset.l;
    const side = document.getElementById("ps-" + lab).value;
    const k = parseFloat(document.getElementById("pk-" + lab).value);
    const prem = parseFloat(document.getElementById("pp-" + lab).value);
    if (!isFinite(k) || k <= 0) return;
    const prev = POS[lab];
    const p = {{side, k, d: (prev && prev.d && prev.side === side && prev.k === k)
                          ? prev.d : new Date().toISOString().slice(0, 10)}};
    const pe = document.getElementById("pe-" + lab);
    if (pe && pe.value) p.exp = pe.value;
    if (isFinite(prem) && prem > 0) {{ p.prem = prem; p.be = side === "C" ? k + prem : k - prem; }}
    POS[lab] = p; posSave(); drawPos(lab); renderPos();
    document.getElementById("pf-" + lab).style.display = "none";
  }}
  if (t.classList.contains("pclear")) {{
    const lab = t.dataset.l;
    delete POS[lab]; posSave(); drawPos(lab); renderPos();
    document.getElementById("pf-" + lab).style.display = "none";
  }}
}});
function refLevels(lab) {{
  const out = [];
  for (const l of (SD[lab].lv.d || [])) {{
    if (l.k === "c" || l.k === "p") out.push({{p: l.p, n: (l.k === "c" ? "C-wall " : "P-wall ") + l.p}});
    if (l.t === "dEM") out.push({{p: l.p, n: "dEM " + l.p.toFixed(0)}});
  }}
  const vwp = (SD[lab].d.v || []).length ? SD[lab].d.v[SD[lab].d.v.length - 1].value : null;
  if (vwp) out.push({{p: vwp, n: "VWAP " + vwp.toFixed(2)}});
  return out;
}}
function inc(lab) {{ return lab === "SPX" ? 5 : 1; }}
function sidePlan(lab, side) {{
  const px = LAST[lab] || LV[lab].spot;
  const walls = (SD[lab].lv.d || []).filter(l => l.k === "c" || l.k === "p");
  const dem = demBounds(lab);
  const vw = (SD[lab].d.v || []).length ? SD[lab].d.v[SD[lab].d.v.length - 1].value : null;
  const firstUp = walls.filter(w => w.k === "c" && w.p > px).map(w => w.p).sort((a, b) => a - b)[0]
                  ?? (dem ? dem[1] : null);
  const firstDn = walls.filter(w => w.k === "p" && w.p < px).map(w => w.p).sort((a, b) => b - a)[0]
                  ?? (dem ? dem[0] : null);
  const cap = side === "C" ? firstUp : firstDn;
  const supC = [firstDn, (vw && vw < px) ? vw : null].filter(x => x != null);
  const supP = [firstUp, (vw && vw > px) ? vw : null].filter(x => x != null);
  const sup = side === "C" ? (supC.length ? Math.max(...supC) : null)
                           : (supP.length ? Math.min(...supP) : null);
  const room = cap != null ? Math.abs(cap / px - 1) * 100 : null;
  const supD = sup != null ? Math.abs(sup / px - 1) * 100 : null;
  const nm = side === "C" ? "CALLS" : "PUTS";
  if (room != null && room < 0.15)
    return {{st: "no", txt: `<b>${{nm}}: no play</b> — the first ${{side === "C" ? "ceiling" : "floor"}} is only ${{room.toFixed(2)}}% away. Not enough room to pay for the ticket.`}};
  if (supD != null && supD <= 0.12)
    return {{st: "now", txt: `<b class=oknow>${{nm}}: a play exists here</b> — price is sitting on a level (${{sup.toFixed(0)}}), so your out is defined right ${{side === "C" ? "below" : "above"}} it; room to ${{cap ? cap.toFixed(0) : "the range edge"}} (${{room != null ? room.toFixed(2) : "?"}}%).`}};
  if (supD != null)
    return {{st: "wait", txt: `<b>${{nm}}: not yet</b> — mid-air here, nothing to lean risk against. The entry worth waiting for is ${{sup.toFixed(0)}} (${{supD.toFixed(2)}}% away).`}};
  return {{st: "no", txt: `<b>${{nm}}: no play</b> — no reference levels in reach.`}};
}}
function planFor(lab) {{
  const a = sidePlan(lab, "C"), b = sidePlan(lab, "P");
  const L = [];
  if (a.st !== "now" && b.st !== "now")
    L.push(`<b class=w>No clean play on either side right now.</b> Standing aside is a position — most hours look exactly like this, and forcing it is how accounts leak.`);
  L.push(a.txt); L.push(b.txt);
  const IDX = {{SPX: 1, SPY: 1, QQQ: 1}};
  if (b.st !== "never" && MY && MY.put_pnl < -50000 && IDX[lab])
    L.push(`<span class=my>Your history:</span> ${{MY.put_n}} put buys, ${{MY.put_win}}% won, $${{Math.abs(MY.put_pnl).toLocaleString()}} lost — puts are your single most expensive habit. Your three worst trades ever happened in one 48-hour window (Jul 30–31, 2026): $${{(MY.worst3_cost||0).toLocaleString()}} into index puts at your +$${{(MY.peak26||0).toLocaleString()}} peak → −$${{(MY.worst3_burn||0).toLocaleString()}}. The year ended +$${{(MY.final26||0).toLocaleString()}}.`);
  const H = MY && MY.by_und && MY.by_und[lab];
  if (H && Math.abs(H.pnl) >= 5000)
    L.push(`<span class=my>Your ${{lab}} record:</span> ${{H.n}} closed positions, ${{H.pnl > 0 ? "+" : "−"}}$${{Math.abs(H.pnl).toLocaleString()}} lifetime.` +
           (IDX[lab] ? "" : (H.pnl > 0
             ? " Single names are your green column — index options were the killers. Same rules here: at a level, out fast, sized for a zero."
             : " This name has cost you — a famous tape isn't automatically your tape.")));
  if (PREM != null && PREM < 15)
    L.push(`Premium is near record cheap — tempting, but days this cheap usually stay quiet (straddle buyers lost ~83%). Cheap is not a reason.`);
  if (MY && PREM != null && PREM < 25)
    L.push(`<span class=my>Your history on calm-vol days:</span> $${{Math.abs(MY.calm_pnl).toLocaleString()}} lost across 145 trades. Your only net-green regime was elevated vol (+$${{MY.hot_pnl.toLocaleString()}} in ${{MY.hot_n}}) — and even that was two big hits doing all the work.`);
  if (PREM != null && PREM > 80)
    L.push(`Premium is expensive today — you need a bigger move than usual just to break even.`);
  const Z = HZP[lab];
  if (Z && Z.d != null) {{
    const rows = [`0DTE: costs ±${{Z.d.toFixed(2)}}% — moves that big happen ~1 day in 4. On stall days the bleed is front-loaded: with no movement, ~a third of the time value is gone by 1pm and over half by 3pm.`];
    if (Z.w != null) rows.push(`Friday (${{Z.we}}): ±${{Z.w.toFixed(2)}}% — weeks out-move their pricing only ~17% of the time, but the bleed is slower and you get exits.`);
    if (Z.m != null) rows.push(`Monthly (${{Z.me}}): ±${{Z.m.toFixed(2)}}% — months out-move it ~16% of the time. Most time to be right, most premium at risk.`);
    L.push(`<b>Choosing the contract</b> — every expiry loses to its own pricing ~4 times in 5; the choice is how fast it bleeds and how many exits you get:<br>· ` + rows.join(`<br>· `));
    if (MY && MY.exp_burn)
      L.push(`<span class=my>Your short-dated record:</span> ${{MY.exp_n}} contracts ridden to zero, −$${{MY.exp_burn.toLocaleString()}}, median 4 DTE at entry — the fast bleed is the one you've already paid for. If you buy short-dated, the exit plan matters more than the entry.`);
  }}
  L.push(`<span style="font-size:11px">Levels and timing only — direction is yours; nothing here knows it. Size ≤1–2%.</span>`);
  return L.join("<br>");
}}
function renderPlans() {{
  for (const lab of Object.keys(LV)) {{
    const f = document.getElementById("pf-" + lab);
    const pl = document.getElementById("pl-" + lab);
    if (f && pl && f.style.display !== "none") pl.innerHTML = planFor(lab);
    const al = document.getElementById("al-" + lab);
    if (al) {{
      const px = LAST[lab] || LV[lab].spot;
      const refs = refLevels(lab);
      if (refs.length) {{
        const near = refs.reduce((a, b) => Math.abs(b.p - px) < Math.abs(a.p - px) ? b : a);
        al.textContent = Math.abs(near.p / px - 1) * 100 <= 0.12 ? "● at " + near.n : "";
      }}
    }}
  }}
}}
Object.keys(POS).forEach(drawPos);
renderPos();
renderPlans();
const fp = x => (x >= 0 ? "+" : "") + x.toFixed(2) + "%";
function cashOpen() {{
  const p = new Intl.DateTimeFormat("en-US", {{timeZone: "America/New_York", hour12: false,
    hour: "2-digit", minute: "2-digit", weekday: "short"}}).formatToParts(new Date());
  const g = k => (p.find(x => x.type === k) || {{}}).value;
  const m = (+g("hour")) * 60 + (+g("minute"));
  return !["Sat", "Sun"].includes(g("weekday")) && m >= 570 && m < 960;
}}
function etEpoch() {{  // epoch shifted so chart's UTC display reads as ET wall time
  const p = new Intl.DateTimeFormat("en-US", {{timeZone: "America/New_York", hourCycle: "h23",
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
    minute: "2-digit", second: "2-digit"}}).formatToParts(new Date());
  const g = k => +(p.find(x => x.type === k) || {{}}).value;
  return Date.UTC(g("year"), g("month") - 1, g("day"), g("hour") % 24, g("minute"), g("second")) / 1000;
}}
function tick(lab, tfKey, px, bucket, at) {{
  // extend the chart between rebuilds: update the current candle, or open a new
  // one when the clock crosses a bucket boundary — the day chart stays live.
  // `at` (optional): ET-shifted epoch of the sample, for replaying history.
  const S = SD[lab] && SD[lab][tfKey]; if (!S || !S.c.length) return;
  const t = Math.floor((at || etEpoch()) / bucket) * bucket;
  let lb = S.c[S.c.length - 1];
  if (t > lb.time) {{ lb = {{time: t, open: px, high: px, low: px, close: px}}; S.c.push(lb); }}
  else if (t === lb.time) {{ lb.close = px; lb.high = Math.max(lb.high, px); lb.low = Math.min(lb.low, px); }}
  else return;  // older than the last candle — ignore
  const o = CH[lab];
  if (o && o.tf === tfKey) o.cs.update(lb);
}}
const REPL = {{}};  // label -> newest history epoch already replayed
function replay(d) {{
  // fill the gap between the static build and now from the feed's rolling history
  if (!d.h) return;
  const off = etEpoch() - Math.floor(Date.now() / 1000);
  const FUTL2 = {{ES: ["SPX", "SPY"], NQ: ["QQQ"]}};
  for (const [src, samples] of Object.entries(d.h)) {{
    if (!Array.isArray(samples)) continue;
    for (const [ts, px] of samples) {{
      if (ts <= (REPL[src] || 0)) continue;
      const at = ts + off;
      const m = Math.floor(at % 86400 / 60);
      if (LV[src]) {{
        if (m >= 570 && m < 960) tick(src, "d", px, 300, at);
      }} else if (FUTL2[src]) {{
        for (const lab of FUTL2[src])
          if (FSC[lab]) tick(lab, "o", px * FSC[lab], 900, at);
      }}
    }}
    if (samples.length) REPL[src] = samples[samples.length - 1][0];
  }}
}}
// ---------- direct streaming: Yahoo's own websocket (unofficial — every failure
// falls back silently to the 30s GitHub feed; ticks reuse the same pipeline) ----------
let WS = null, wsLast = 0, wsDirty = {{}};
function b64u8(s) {{ return Uint8Array.from(atob(s), c => c.charCodeAt(0)); }}
function pvar(u, i) {{
  let v = 0, s = 0;
  for (;;) {{ const b = u[i++]; v += (b & 127) * Math.pow(2, s); if (!(b & 128)) break; s += 7;
    if (s > 63 || i > u.length) break; }}
  return [v, i];
}}
function pdec(u) {{  // tolerant protobuf walk: id (f1 str), price (f2 float32), hours (f7 varint)
  let i = 0; const out = {{}};
  while (i < u.length) {{
    let key; [key, i] = pvar(u, i);
    const f = key >> 3, w = key & 7;
    if (w === 0) {{ let v; [v, i] = pvar(u, i); if (f === 7) out.hours = v; }}
    else if (w === 2) {{
      let len; [len, i] = pvar(u, i);
      if (f === 1) out.id = new TextDecoder().decode(u.slice(i, i + len));
      i += len;
    }}
    else if (w === 5) {{
      if (i + 4 > u.length) break;
      const dv = new DataView(u.buffer, u.byteOffset + i, 4);
      if (f === 2) out.price = dv.getFloat32(0, true);
      i += 4;
    }}
    else if (w === 1) {{ i += 8; }}
    else break;
  }}
  return out;
}}
function applyTick(lab, px) {{
  const prev = LAST[lab] || (LV[lab] && LV[lab].spot);
  if (prev && Math.abs(px / prev - 1) > 0.1) return;   // reject garbage decodes
  const pe = document.getElementById("px-" + lab);
  if (pe) pe.textContent = px.toFixed(2);
  const dot = document.getElementById("dot-" + lab);
  if (dot && LV[lab]) dot.style.left =
    Math.max(1.5, Math.min(98.5, (px - LV[lab].lo) / (LV[lab].hi - LV[lab].lo) * 100)) + "%";
  if (cashOpen()) tick(lab, "d", px, 300);
  levelAlerts(lab, LAST[lab], px);
  LAST[lab] = px;
  wsDirty[lab] = 1;
}}
function wsHandle(m) {{
  if (!m || !m.id || !isFinite(m.price) || m.price <= 0) return;
  const lab = WSYMS[m.id];
  if (!lab) return;
  wsLast = Date.now();
  if (WFUT[lab]) {{
    for (const l2 of WFUT[lab]) if (FSC[l2]) {{
      tick(l2, "o", m.price * FSC[l2], 900);
      wsDirty[l2] = 1;
    }}
    return;
  }}
  if (LV[lab]) applyTick(lab, m.price);
}}
function wsStart() {{
  if (!("WebSocket" in window)) return;
  try {{ if (WS) WS.close(); }} catch (e) {{}}
  try {{
    WS = new WebSocket("wss://streamer.finance.yahoo.com/?version=2");
    WS.onopen = () => WS.send(JSON.stringify({{subscribe: Object.keys(WSYMS)}}));
    WS.onmessage = ev => {{
      try {{
        let raw = ev.data;
        if (typeof raw === "string" && raw[0] === "{{") {{
          const j = JSON.parse(raw);
          raw = j.message || j.data || "";
        }}
        if (typeof raw === "string" && raw) wsHandle(pdec(b64u8(raw)));
      }} catch (e) {{}}
    }};
    WS.onclose = () => {{ WS = null; setTimeout(wsStart, 20000); }};
    WS.onerror = () => {{ try {{ WS.close(); }} catch (e) {{}} }};
  }} catch (e) {{}}
}}
function streaming() {{ return Date.now() - wsLast < 12000; }}
setInterval(() => {{  // throttled flush of the heavier widgets at 1s
  const labs = Object.keys(wsDirty);
  if (!labs.length) return;
  wsDirty = {{}};
  for (const lab of labs) {{ updOverlay(lab); updMarkers(lab); relabel(lab); }}
  renderPos(); renderPlans();
  if (streaming()) {{
    const b = document.getElementById("live");
    if (b) {{
      b.style.display = ""; b.style.color = "#3fd0a4";
      b.textContent = "● streaming · tick-level";
      const hd = document.getElementById("hdr");
      if (hd) hd.textContent = `levels ${{BUILT}} · price streaming`;
    }}
  }}
}}, 1000);
wsStart();
document.addEventListener("visibilitychange", () => {{
  if (!document.hidden && !streaming()) wsStart();
}});
function ladderMark(lab, px, ref) {{
  // keep the ladder's "→ now" row glued to live price, hopping tiers as it moves
  const mk = document.getElementById("lmk-" + lab);
  if (!mk || !px || !LV[lab] || !LV[lab].spot) return;
  const dn = document.getElementById("lad-" + lab), up = document.getElementById("ladu-" + lab);
  const tgt = (px <= LV[lab].spot || !up) ? (dn || up) : up;
  if (!tgt) return;
  mk.dataset.p = px;
  let txt = "→ now " + (LV[lab].spot >= 2000 ? px.toFixed(0) : px.toFixed(2));
  if (ref) {{
    const pc = 100 * (px / ref - 1);
    txt += " (" + (pc >= 0 ? "+" : "") + pc.toFixed(2) + "% vs close)";
  }}
  mk.firstElementChild.textContent = txt + " ←";
  const desc = tgt === dn;   // downside tbody sorts price-descending, upside ascending
  let before = null;
  for (const r of tgt.children) {{
    if (r === mk || !r.dataset.p) continue;
    const rp = parseFloat(r.dataset.p);
    if ((desc && rp < px) || (!desc && rp > px)) {{ before = r; break; }}
  }}
  tgt.insertBefore(mk, before);
}}
async function livePoll() {{
  try {{
    let d = null;
    try {{
      // raw + cache-bust query = fresh from origin, no 60/hr API cap -> 30s polling
      const r2 = await fetch("https://raw.githubusercontent.com/zoyuka/oi-walls/live/live.json?t=" + Date.now(),
                             {{cache: "no-store"}});
      if (r2.ok) d = await r2.json();
    }} catch (e) {{}}
    if (!d || !d.ts) {{
      const r = await fetch("https://api.github.com/repos/zoyuka/oi-walls/contents/live.json?ref=live",
                            {{headers: {{Accept: "application/vnd.github.raw+json"}}, cache: "no-store"}});
      if (r.ok) d = await r.json();
    }}
    const b = document.getElementById("live");
    if (!d.ts) return;
    const age = (Date.now() - Date.parse(d.ts)) / 1000;
    const hd = document.getElementById("hdr");
    if (age > 240) {{
      b.style.display = "none";
      if (hd) hd.textContent = `levels ${{BUILT}} · refresh ~10 min`;
      return;
    }}
    const strm = streaming();
    b.style.display = "";
    b.style.color = "#3fd0a4";
    const tD = new Date(d.ts);
    if (!strm) {{
      b.textContent = "● live " + String(tD.getHours()).padStart(2, "0") + ":" + String(tD.getMinutes()).padStart(2, "0");
      if (hd) hd.textContent = `levels ${{BUILT}} · price live`;
    }}
    replay(d);  // backfill candles between the static build and now
    for (const [lab, q] of Object.entries(d)) {{
      if (lab === "ts" || !LV[lab] || !q.px) continue;
      const L = LV[lab];
      const px = (strm && LAST[lab]) ? LAST[lab] : q.px;  // never overwrite ticks with staler polls
      const gd = document.getElementById("gd-" + lab);
      if (gd) gd.innerHTML = (L.dn ? "▼" + fp((L.dn / px - 1) * 100) : "▼—") + " " +
                             (L.up ? "▲" + fp((L.up / px - 1) * 100) : "▲—") +
                             "";
      if (cashOpen()) ladderMark(lab, px);
      if (strm && LAST[lab]) continue;
      const pe = document.getElementById("px-" + lab);
      if (pe) pe.textContent = px.toFixed(2);
      const dot = document.getElementById("dot-" + lab);
      if (dot) dot.style.left = Math.max(1.5, Math.min(98.5, (px - L.lo) / (L.hi - L.lo) * 100)) + "%";
      if (cashOpen()) tick(lab, "d", px, 300);
      levelAlerts(lab, LAST[lab], px);
      LAST[lab] = px;
      updMarkers(lab);
      updOverlay(lab);
      relabel(lab);
    }}
    // futures: keep the 24h tab's last candle live, show overnight drift when cash is closed
    const FUTL = {{SPX: "ES", SPY: "ES", QQQ: "NQ"}};
    const co = cashOpen();
    for (const [lab, fl] of Object.entries(FUTL)) {{
      const q = d[fl];
      if (!q || !q.px || !FSC[lab]) continue;
      const eq = q.px * FSC[lab];
      tick(lab, "o", eq, 900);
      if (!co) ladderMark(lab, eq, LV[lab] && LV[lab].spot);
      if (!co && lab === "SPX" && LV.SPX && LV.SPX.spot) {{
        const pct = 100 * (eq / LV.SPX.spot - 1);
        b.textContent += ` · overnight ES→SPX ${{eq.toFixed(0)}} (${{pct >= 0 ? "+" : ""}}${{pct.toFixed(2)}}%)`;
      }}
    }}
    renderPos();
    renderPlans();
  }} catch (e) {{}}
}}
livePoll(); setInterval(livePoll, 30000);

// ---------- build freshness: never let a cached page sit stale ----------
let lastTouch = 0;
["pointerdown", "touchstart", "wheel"].forEach(ev =>
  addEventListener(ev, () => lastTouch = Date.now(), {{passive: true}}));
function doReload() {{
  try {{
    sessionStorage.setItem("oiw_scroll", String(scrollY));
    const tabs = {{}};
    for (const l of Object.keys(CH)) tabs[l] = CH[l].tf;
    sessionStorage.setItem("oiw_tabs", JSON.stringify(tabs));
    sessionStorage.setItem("oiw_rl", String(Date.now()));
  }} catch (e) {{}}
  location.replace(location.pathname + "?v=" + Date.now());
}}
async function freshCheck() {{
  try {{
    const r = await fetch("state.json?v=" + Date.now(), {{cache: "no-store"}});
    if (!r.ok) return;
    const s = await r.json();
    if (!s._ts) return;
    const newer = (Date.parse(s._ts) - PAGE_TS * 1000) / 1000;
    const guard = +(sessionStorage.getItem("oiw_rl") || 0);
    if (newer > 60 && Date.now() - guard > 300000) {{
      if (document.hidden || Date.now() - lastTouch > 8000) doReload();
      // else: user is mid-interaction — the next check catches it
    }}
  }} catch (e) {{}}
}}
try {{
  const sc = +(sessionStorage.getItem("oiw_scroll") || 0);
  if (sc) {{ scrollTo(0, sc); sessionStorage.removeItem("oiw_scroll"); }}
  const tb = JSON.parse(sessionStorage.getItem("oiw_tabs") || "{{}}");
  for (const [l, tf] of Object.entries(tb)) if (CH[l] && tf !== "d") setTF(l, tf);
  sessionStorage.removeItem("oiw_tabs");
}} catch (e) {{}}
freshCheck(); setInterval(freshCheck, 90000);
document.addEventListener("visibilitychange", () => {{ if (!document.hidden) {{
  ALERT_ARM = Date.now() + 20000;   // resume from background: re-baseline, don't replay
  freshCheck(); livePoll();
}} }});
</script>
</body></html>""")
    print("site built:", now_et.strftime('%H:%M ET'))

def horizon_chains(t, spot, today):
    """Walls + straddle EM for tomorrow's, Friday's and the monthly expiry."""
    out = {}
    try:
        opts = [dt.date.fromisoformat(d_) for d_ in t.options]
        def pack(exp_date, key):
            try:
                ch = t.option_chain(exp_date.isoformat())
                cw, pw = walls(ch, spot)
                out[key] = {"exp": exp_date.isoformat(), "calls": cw, "puts": pw,
                            "em": float(straddle_mid(ch, spot)),
                            "big": big_premium(ch, spot, exp_date.isoformat())}
            except Exception:
                pass
        tom = next((d_ for d_ in opts if d_ > today), None)
        if tom: pack(tom, "tm")
        fri = next((d_ for d_ in opts if d_.weekday() == 4 and d_ >= today), None)
        if fri: pack(fri, "wk")
        # monthly = third Friday
        def third_fri(y, m):
            d_ = dt.date(y, m, 15)
            while d_.weekday() != 4: d_ = d_ + dt.timedelta(days=1)
            return d_
        mth = third_fri(today.year, today.month)
        if mth <= today + dt.timedelta(days=4):
            nm = today.month % 12 + 1
            mth = third_fri(today.year + (1 if nm == 1 else 0), nm)
        mo = min(opts, key=lambda d_: abs((d_ - mth).days)) if opts else None
        if mo and abs((mo - mth).days) <= 3: pack(mo, "mo")
    except Exception:
        pass
    return out

def yahoo_fetch(sym):
    t = yf.Ticker(sym)
    # max history Yahoo serves per interval: 5m→60d, 30m→60d, 1d→decades.
    # We take a week of 5m, two months of 30m, two years of daily — each tab
    # opens zoomed to the familiar window and pans back through the rest.
    px = t.history(period="5d", interval="5m")
    if px.empty: px = t.history(period="1mo", interval="15m")
    if px.empty: px = t.history(period="60d", interval="30m")
    spot = px["Close"].iloc[-1]
    # front expiry = next session's, not today's dead chain: after ~4:15pm ET the
    # expired 0DTE can still be listed with zeroed quotes, which would poison the
    # straddle EM, walls, pin and gamma on evening prep builds
    now_e = dt.datetime.now(ZoneInfo("America/New_York"))
    cutoff = (now_e.date() if now_e.hour * 60 + now_e.minute < 975
              else now_e.date() + dt.timedelta(days=1))
    live_exps = [e for e in t.options if dt.date.fromisoformat(e) >= cutoff]
    exp = live_exps[0] if live_exps else t.options[0]
    ch0 = t.option_chain(exp)
    calls, puts = walls(ch0, spot)
    ems = expected_moves(t, spot, ch0)
    hz = horizon_chains(t, spot, dt.datetime.now(ZoneInfo("America/New_York")).date())
    try:
        px_w = t.history(period="60d", interval="30m")
    except Exception:
        px_w = None
    try:
        px_d = t.history(period="2y", interval="1d")
    except Exception:
        px_d = None
    close_et = dt.datetime.now(ZoneInfo("America/New_York")).replace(hour=16, minute=0, second=0)
    T = max((close_et - dt.datetime.now(ZoneInfo("America/New_York"))).total_seconds(), 0) / 86400 / 365
    return {"px": px, "px_w": px_w, "px_d": px_d, "exp": exp, "calls": calls,
            "puts": puts, "ems": ems, "pin": max_pain(ch0, spot),
            "gam": gamma_profile(ch0, spot, T), "hz": hz,
            "spr": atm_spread_pct(ch0, spot), "fvol": front_volume(ch0),
            "flow": chain_flow(ch0), "big0": big_premium(ch0, spot, exp),
            "earn": _with_timeout(lambda: next_earnings(t), 15)}

if __name__ == "__main__":
    build(yahoo_fetch)
