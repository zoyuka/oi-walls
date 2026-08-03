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
from zoneinfo import ZoneInfo
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BAND, N = 0.04, 3
PURPLE, YELLOW, GREEN, RED = "#a259ff", "#e0a800", "#26a69a", "#ef5350"
BLUE, TEAL, GRAY = "#4ea3ff", "#3fd0a4", "#6b6b73"
TICKERS = [("^SPX", "SPX"), ("SPY", "SPY"), ("QQQ", "QQQ")]

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
        prev_close = closes.iloc[-2] if len(closes) > 1 else spot
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
        return {"long": net(spot) > 0, "flip": flip, "peak": peak}
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
    em_out = []
    for tag, lv in (em or []):
        if lo < lv < hi:
            ax.axhline(lv, color="#8b8b93", linewidth=1.0, linestyle=(0, (5, 2, 1, 2)), zorder=1)
            ax.annotate(f"{tag} {lv:,.0f}", xy=(0.012, lv), xycoords=("axes fraction", "data"),
                        color="#9a9aa2", fontsize=8.5, va="bottom", zorder=5, bbox=MASKG)
        else:
            em_out.append((tag, lv))

    ws = sorted([("C", c, PURPLE) for c in calls] + [("P", p, YELLOW) for p in puts],
                key=lambda t: t[1].strike)
    maxoi = {"C": max(c.openInterest for c in calls), "P": max(p.openInterest for p in puts)}
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

def build(fetch):
    """fetch(sym) -> dict(px, px_w, px_d, exp, calls, puts, ems, pin, gflip)."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
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
    cards, data = [], []
    for sym, label in TICKERS:
        d = fetch(sym)
        px, exp, calls, puts, ems = d["px"], d["exp"], d["calls"], d["puts"], d["ems"]
        spot = px["Close"].iloc[-1]
        open(f"docs/OI_Walls_{label}.txt", "w").write(study_text(label, exp, calls, puts))
        em_levels = []
        for tag, key in (("dEM", "day"), ("wEM", "week")):
            if ems.get(key):
                a, e = ems[key]
                em_levels += [(tag, a - e), (tag, a + e)]
        wk_levels = [t for t in em_levels if t[0] == "wEM"]
        chart(f"docs/{label}.png", px, spot, calls, puts, em_levels, "day")
        if d.get("px_w") is not None and len(d["px_w"]):
            chart(f"docs/{label}_w.png", d["px_w"], spot, calls, puts, wk_levels, "week")
        if d.get("px_d") is not None and len(d["px_d"]):
            chart(f"docs/{label}_d.png", d["px_d"], spot, calls, puts, wk_levels, "daily")
        data.append((label, spot, calls, puts))
        state[label] = {w.contractSymbol: int(w.volume) for w in calls + puts}

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
        if d.get("pin"):
            chips.append(f'<span class="chip e">pin {fmt_strike(d["pin"])}</span>')
        g = d.get("gam")
        if g:
            if g["long"]:
                bits = ["γ long (dampens)"]
                if g.get("peak"): bits.append(f'magnet ~{g["peak"]:,.0f}')
            else:
                bits = ["γ short (amplifies)"]
                if g.get("flip"): bits.append(f'flip ~{g["flip"]:,.0f}')
            chips.append(f'<span class="chip e">{" · ".join(bits)}</span>')

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
        tabs = (f'<div class=tw>'
                f'<input type=radio name=t{label} id={label}-0 class=t0 checked><label for="{label}-0">Today 5m</label>'
                f'<input type=radio name=t{label} id={label}-1 class=t1><label for="{label}-1">Week 15m</label>'
                f'<input type=radio name=t{label} id={label}-2 class=t2><label for="{label}-2">3mo daily</label>'
                f'<img class=i0 src="{label}.png?v={v}" alt="{label} today">'
                f'<img class=i1 src="{label}_w.png?v={v}" alt="{label} week">'
                f'<img class=i2 src="{label}_d.png?v={v}" alt="{label} 3 months">'
                f'</div>')
        cards.append(f"""<section id="{label}"><h2>{label} <span>{spot:.2f} · exp {exp}</span></h2>
<div class=chips>{"".join(chips)}</div>
{tabs}
<table><tr><th>Wall</th><th>Dist</th><th>OI</th><th>Vol (Δ10m)</th><th>V/OI</th></tr>{trs}</table>
<a class=btn href="OI_Walls_{label}.txt">ToS study text</a></section>""")

    json.dump(state, open("docs/state.json", "w"))
    open("docs/index.html", "w").write(f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>OI Walls</title><style>
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
.tw label{{display:inline-block;background:#15151a;border:1px solid #2c2c33;border-radius:8px;padding:5px 12px;font-size:12.5px;color:#9a9aa2;margin:8px 6px 0 0;cursor:pointer}}
.tw input:checked+label{{color:#fff;border-color:#4a4a55;background:#1c1c22}}
.tw img{{display:none}}
.tw input.t0:checked~img.i0,.tw input.t1:checked~img.i1,.tw input.t2:checked~img.i2{{display:block}}
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
</style></head><body>
<h1>OI Walls</h1>
<p class=u>{now_et.strftime('%-I:%M %p ET')} · auto-updates every 10 min (mkt hrs) · quotes ~15 min delayed</p>
{ladder_html(data)}
<p class=leg><i style="background:{PURPLE}"></i>call wall <i style="background:{YELLOW}"></i>put wall
<i style="background:{BLUE}"></i>VWAP · <b style="color:#fff">white</b> = today's vol &gt; OI (fresh flow) · bar length = OI · OI resets pre-market ·
EM = expected move (ATM straddle: d = today from prev close, w = by Friday from last Fri close) ·
pin = max pain · γ = est. dealer gamma, 0DTE (rough): long = moves dampened, price gravitates to magnet strike into 4pm; short = moves amplified below flip · <span class=dv>+Δ</span> = vol added since last build</p>
{"".join(cards)}</body></html>""")
    print("site built:", now_et.strftime('%H:%M ET'))

def yahoo_fetch(sym):
    t = yf.Ticker(sym)
    px = t.history(period="2d", interval="5m")
    if px.empty: px = t.history(period="4d", interval="15m")
    if px.empty: px = t.history(period="5d", interval="15m")
    spot = px["Close"].iloc[-1]
    exp = t.options[0]
    ch0 = t.option_chain(exp)
    calls, puts = walls(ch0, spot)
    ems = expected_moves(t, spot, ch0)
    try:
        px_w = t.history(period="5d", interval="15m")
    except Exception:
        px_w = None
    try:
        px_d = t.history(period="3mo", interval="1d")
    except Exception:
        px_d = None
    close_et = dt.datetime.now(ZoneInfo("America/New_York")).replace(hour=16, minute=0, second=0)
    T = max((close_et - dt.datetime.now(ZoneInfo("America/New_York"))).total_seconds(), 0) / 86400 / 365
    return {"px": px, "px_w": px_w, "px_d": px_d, "exp": exp, "calls": calls,
            "puts": puts, "ems": ems, "pin": max_pain(ch0, spot),
            "gam": gamma_profile(ch0, spot, T)}

if __name__ == "__main__":
    build(yahoo_fetch)
