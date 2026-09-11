#!/usr/bin/env python3
"""S&P 500 market-breadth engine (runs in GitHub Actions, once daily after close).

Downloads 1y of daily bars for every S&P 500 constituent and computes the
breadth stack: advance/decline ratio + cumulative A/D line, McClellan
oscillator & summation (ratio-adjusted), % of stocks above 20/50/200-day MAs,
52-week new highs/lows, up-vs-down volume, and SPY vs RSP (equal-weight).

Outputs:
  data/breadth.json           latest snapshot + 1y series (for the dashboard)
  data/constituents.csv       cached ticker list (fallback if Wikipedia breaks)
  docs/breadth_ad.png         SPX vs cumulative A/D line
  docs/breadth_ma.png         % above 20/50/200 DMA
  docs/breadth_mcc.png        McClellan oscillator + summation
  docs/breadth_rsp.png        RSP/SPY ratio (equal-weight participation)
"""
import datetime as dt
import json
import os

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PURPLE, YELLOW, GREEN, RED = "#a259ff", "#e0a800", "#26a69a", "#ef5350"
BLUE, TEAL, GRAY, BG = "#4ea3ff", "#3fd0a4", "#6b6b73", "#0d0d0f"

os.makedirs("data", exist_ok=True)
os.makedirs("docs", exist_ok=True)

# ---------- constituent list (Wikipedia, cached fallback) ----------
def get_constituents():
    import urllib.request
    try:  # 1: Wikipedia (needs a real user-agent)
        req = urllib.request.Request(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0 (breadth-dashboard; contact: repo)"})
        html = urllib.request.urlopen(req, timeout=30).read()
        tables = pd.read_html(html)
        syms = [s.replace(".", "-") for s in tables[0]["Symbol"].tolist()]
        if len(syms) > 400:
            pd.Series(syms, name="symbol").to_csv("data/constituents.csv", index=False)
            return syms
    except Exception as e:
        print("wikipedia failed:", e)
    try:  # 2: datasets/s-and-p-500-companies on GitHub
        df = pd.read_csv("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv")
        syms = [s.replace(".", "-") for s in df["Symbol"].tolist()]
        if len(syms) > 400:
            pd.Series(syms, name="symbol").to_csv("data/constituents.csv", index=False)
            return syms
    except Exception as e:
        print("datahub failed:", e)
    return pd.read_csv("data/constituents.csv")["symbol"].tolist()  # 3: cached seed

syms = get_constituents()
print("constituents:", len(syms))

# ---------- bulk download (chunked) ----------
frames = {}
for i in range(0, len(syms), 100):
    chunk = syms[i:i+100]
    df = yf.download(chunk, period="1y", interval="1d", group_by="ticker",
                     threads=True, progress=False, auto_adjust=True)
    for s in chunk:
        try:
            sub = df[s][["Close", "Volume"]].dropna()
            if len(sub) > 60:
                frames[s] = sub
        except Exception:
            pass
print("with data:", len(frames))
if len(frames) < 350:
    raise SystemExit("too few constituents downloaded — aborting to avoid bad breadth")

closes = pd.DataFrame({s: f["Close"] for s, f in frames.items()}).sort_index()
vols   = pd.DataFrame({s: f["Volume"] for s, f in frames.items()}).sort_index()
chg = closes.diff()
adv = (chg > 0).sum(axis=1)
dec = (chg < 0).sum(axis=1)
upvol = vols.where(chg > 0).sum(axis=1)
dnvol = vols.where(chg < 0).sum(axis=1)
net = adv - dec
ad_line = net.cumsum()

# McClellan (ratio-adjusted): RANA = 1000*(A-D)/(A+D); osc = EMA19-EMA39; summation = cumsum
rana = 1000 * net / (adv + dec).replace(0, np.nan)
osc = rana.ewm(span=19, adjust=False).mean() - rana.ewm(span=39, adjust=False).mean()
summation = osc.cumsum()

pct20  = 100 * (closes > closes.rolling(20).mean()).sum(axis=1) / closes.notna().sum(axis=1)
pct50  = 100 * (closes > closes.rolling(50).mean()).sum(axis=1) / closes.notna().sum(axis=1)
pct200 = 100 * (closes > closes.rolling(200).mean()).sum(axis=1) / closes.notna().sum(axis=1)
nh = (closes >= closes.rolling(252, min_periods=200).max()).sum(axis=1)
nl = (closes <= closes.rolling(252, min_periods=200).min()).sum(axis=1)

idx = yf.download(["^GSPC", "SPY", "RSP"], period="1y", interval="1d",
                  group_by="ticker", progress=False, auto_adjust=True)
spx = idx["^GSPC"]["Close"].dropna()
ratio = (idx["RSP"]["Close"] / idx["SPY"]["Close"]).dropna()

# ---------- divergence + verdict (the thread's framework, computed) ----------
def at_20d_high(s): return s.iloc[-1] >= s.rolling(20).max().iloc[-1] - 1e-9
def at_20d_low(s):  return s.iloc[-1] <= s.rolling(20).min().iloc[-1] + 1e-9
px_hi, px_lo = at_20d_high(spx), at_20d_low(spx)
ad_hi = at_20d_high(ad_line)
breadth_rising = ad_line.iloc[-1] > ad_line.iloc[-6]
nysi_rising = summation.iloc[-1] > summation.iloc[-6]
if px_hi and not ad_hi:
    verdict, vcls = "⚠ price at highs, A/D line NOT — negative divergence", "warn"
elif px_lo and not at_20d_low(ad_line):
    verdict, vcls = "◆ price at lows but breadth holding — positive divergence", "good"
elif breadth_rising and nysi_rising:
    verdict, vcls = "✓ breadth confirming (A/D + McClellan rising)", "good"
elif not breadth_rising and not nysi_rising:
    verdict, vcls = "⚠ breadth deteriorating (A/D + McClellan falling)", "warn"
else:
    verdict, vcls = "◦ breadth mixed", "mixed"

snap = {
    "date": str(closes.index[-1].date()),
    "n": int(closes.notna().sum(axis=1).iloc[-1]),
    "adv": int(adv.iloc[-1]), "dec": int(dec.iloc[-1]),
    "upvol": float(upvol.iloc[-1]), "dnvol": float(dnvol.iloc[-1]),
    "pct20": round(float(pct20.iloc[-1]), 1),
    "pct50": round(float(pct50.iloc[-1]), 1),
    "pct200": round(float(pct200.iloc[-1]), 1),
    "nh": int(nh.iloc[-1]), "nl": int(nl.iloc[-1]),
    "mcc_osc": round(float(osc.iloc[-1]), 1),
    "mcc_sum": round(float(summation.iloc[-1]), 1),
    "nysi_rising": bool(nysi_rising),
    "rsp_spy_20d": round(float((ratio.iloc[-1]/ratio.iloc[-21] - 1) * 100), 2),
    "verdict": verdict, "vcls": vcls,
}
json.dump(snap, open("data/breadth.json", "w"))
print(json.dumps(snap, indent=1))

# ---------- charts (dark theme, phone-first, recessive grids) ----------
def frame(title):
    fig, ax = plt.subplots(figsize=(7.6, 4.2), facecolor=BG)
    ax.set_facecolor(BG)
    ax.tick_params(colors=GRAY, labelsize=9)
    for sp in ax.spines.values(): sp.set_color("#26262b")
    ax.grid(axis="y", color="#1c1c22", linewidth=0.7)
    ax.set_title(title, color="#e8e8ea", fontsize=11, loc="left")
    return fig, ax

def save(fig, name):
    fig.savefig(f"docs/{name}", dpi=140, bbox_inches="tight", facecolor=BG)
    plt.close(fig)

MASK = dict(facecolor=BG, edgecolor="none", pad=1.5)

# 1: SPX vs cumulative A/D line (dual normalized 0-1 on one axis, labeled directly)
fig, ax = frame("SPX (teal) vs cumulative A/D line (purple) — 1y, normalized")
for s, col, lab in [(spx, TEAL, "SPX"), (ad_line, PURPLE, "A/D line")]:
    z = (s - s.min()) / (s.max() - s.min())
    ax.plot(z.index, z.values, color=col, linewidth=1.5)
    ax.annotate(lab, xy=(z.index[-1], z.values[-1]), color=col, fontsize=9,
                ha="right", va="bottom", bbox=MASK)
ax.set_yticks([])
save(fig, "breadth_ad.png")

# 2: % above MAs
fig, ax = frame("% of S&P 500 above 20d (blue) · 50d (teal) · 200d (gray)")
for s, col, lab in [(pct20, BLUE, "20d"), (pct50, TEAL, "50d"), (pct200, GRAY, "200d")]:
    ax.plot(s.index, s.values, color=col, linewidth=1.4)
    ax.annotate(f"{lab} {s.iloc[-1]:.0f}%", xy=(s.index[-1], s.iloc[-1]), color=col,
                fontsize=9, ha="right", va="bottom", bbox=MASK)
ax.axhline(50, color="#26262b", linewidth=0.8)
ax.set_ylim(0, 105)
save(fig, "breadth_ma.png")

# 3: McClellan
fig, ax = frame("McClellan oscillator (bars) + summation (line, scaled)")
ax.bar(osc.index, osc.values, color=[GREEN if v >= 0 else RED for v in osc.values], width=1.0)
z = summation - summation.min()
z = z / (z.max() or 1) * (osc.abs().max() * 2) - osc.abs().max()
ax.plot(z.index, z.values, color=YELLOW, linewidth=1.3)
ax.annotate("summation", xy=(z.index[-1], z.values[-1]), color=YELLOW, fontsize=9,
            ha="right", va="bottom", bbox=MASK)
save(fig, "breadth_mcc.png")

# 4: RSP/SPY
fig, ax = frame("RSP / SPY ratio — rising = rally broadening beyond mega-caps")
ax.plot(ratio.index, ratio.values, color=BLUE, linewidth=1.5)
m50 = ratio.rolling(50).mean()
ax.plot(m50.index, m50.values, color=GRAY, linewidth=1.0, linestyle=(0, (4, 3)))
ax.annotate("50d avg", xy=(m50.index[-1], m50.iloc[-1]), color=GRAY, fontsize=9,
            ha="right", va="top", bbox=MASK)
save(fig, "breadth_rsp.png")
print("breadth charts written")
