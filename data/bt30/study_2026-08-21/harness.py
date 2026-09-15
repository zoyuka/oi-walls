"""Shared backtest harness for the 30-minute strategy study. DO NOT MODIFY.

Conventions every strategy must obey (they are enforced here):
- A signal for bar t may use data up to and including bar t's CLOSE only.
- The position implied at bar t is held during bar t+1: pnl uses close[t]->close[t+1]
  with pos shifted one bar. One-bar delay = structural no-lookahead.
- pos: +1 long, -1 short, 0 flat (fractions allowed, |pos| <= 1).
- Costs: cost_bps per side charged on |delta pos| (default 1.5bp; a 3bp
  sensitivity rerun is included automatically in results).
- Split: IS = first 70% of bars, OOS = final 30%. Tune on IS ONLY; OOS is
  touched once, to report. run() computes both from the same pos series.
"""
import json
import numpy as np
import pandas as pd

ET = "America/New_York"

def load(path):
    df = pd.read_csv(path)
    tcol = df.columns[0]
    df[tcol] = pd.to_datetime(df[tcol], utc=True)
    df = df.set_index(tcol).tz_convert(ET)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df

def bars_per_year(df):
    by_day = df.groupby(df.index.date).size()
    return float(np.median(by_day)) * 252.0

def _metrics(rets, pos, bpy):
    eq = (1 + rets).cumprod()
    total = eq.iloc[-1] - 1 if len(eq) else 0.0
    years = len(rets) / bpy if bpy else 1
    cagr = (1 + total) ** (1 / max(years, 1e-9)) - 1 if total > -1 else -1
    vol = rets.std() * np.sqrt(bpy)
    sharpe = (rets.mean() * bpy) / (vol + 1e-12)
    dd = float((eq / eq.cummax() - 1).min()) if len(eq) else 0.0
    # trade extraction: contiguous nonzero position runs
    p = pos.fillna(0).values
    trades, cur = [], None
    for i in range(len(p)):
        if p[i] != 0 and cur is None:
            cur = i
        elif cur is not None and (p[i] == 0 or np.sign(p[i]) != np.sign(p[cur])):
            trades.append((cur, i))
            cur = i if p[i] != 0 else None
    if cur is not None:
        trades.append((cur, len(p)))
    tr = []
    rv = rets.values
    for a, b in trades:
        seg = rv[a + 1:b + 1]  # pos at bar t earns t+1's return (shift already applied upstream)
        if len(seg):
            tr.append(float(np.prod(1 + seg) - 1))
    tr = np.array(tr) if tr else np.array([0.0])
    return {
        "total_ret": round(float(total) * 100, 2),
        "cagr": round(float(cagr) * 100, 2),
        "sharpe": round(float(sharpe), 2),
        "max_dd": round(dd * 100, 2),
        "n_trades": int(len(trades)),
        "hit": round(float((tr > 0).mean()) * 100, 1),
        "avg_trade": round(float(tr.mean()) * 100, 3),
        "exposure": round(float((pos != 0).mean()) * 100, 1),
        "bars": int(len(rets)),
    }

def _strategy_rets(df, pos, cost_bps):
    c = df.Close
    bar_ret = c.pct_change().fillna(0)
    held = pos.shift(1).fillna(0)          # the one-bar execution delay
    gross = held * bar_ret
    turns = pos.diff().abs().fillna(pos.abs())
    costs = turns * (cost_bps / 1e4)
    return gross - costs.shift(1).fillna(0)

def run(df, pos, name="strategy", cost_bps=1.5):
    """df: OHLCV frame from load(). pos: pd.Series indexed like df, in [-1, 1]."""
    pos = pos.reindex(df.index).fillna(0).clip(-1, 1)
    if pos.abs().max() > 1.0001:
        raise ValueError("pos out of range")
    bpy = bars_per_year(df)
    n = len(df)
    cut = int(n * 0.70)
    rets = _strategy_rets(df, pos, cost_bps)
    rets3 = _strategy_rets(df, pos, 3.0)
    out = {
        "name": name,
        "is": _metrics(rets.iloc[:cut], pos.iloc[:cut], bpy),
        "oos": _metrics(rets.iloc[cut:], pos.iloc[cut:], bpy),
        "full": _metrics(rets, pos, bpy),
        "full_3bp": _metrics(rets3, pos, bpy),
        "p_boot": p_boot(df, pos, rets, bpy),
    }
    return out

def p_boot(df, pos, rets, bpy, n_iter=300, seed=7):
    """Placebo test: same trade lengths/directions at random times.
    p = fraction of placebos with full-sample Sharpe >= actual."""
    rng = np.random.default_rng(seed)
    p = pos.fillna(0).values
    segs, cur = [], None
    for i in range(len(p)):
        if p[i] != 0 and cur is None:
            cur = i
        elif cur is not None and (p[i] == 0 or np.sign(p[i]) != np.sign(p[cur])):
            segs.append((i - cur, np.sign(p[cur])))
            cur = i if p[i] != 0 else None
    if cur is not None:
        segs.append((len(p) - cur, np.sign(p[cur])))
    if not segs:
        return 1.0
    bar = df.Close.pct_change().fillna(0).values
    act = rets.mean() / (rets.std() + 1e-12)
    hits = 0
    n = len(p)
    for _ in range(n_iter):
        q = np.zeros(n)
        for ln, sg in segs:
            a = rng.integers(0, max(n - ln, 1))
            q[a:a + ln] = sg
        rr = np.roll(q, 1) * bar
        rr[0] = 0
        s = rr.mean() / (rr.std() + 1e-12)
        if s >= act:
            hits += 1
    return round(hits / n_iter, 3)

def buy_hold(df, cost_bps=1.5):
    pos = pd.Series(1.0, index=df.index)
    return run(df, pos, "buy&hold", cost_bps)

def save_results(family, configs, path_dir="/tmp/bt30"):
    """configs: list of dicts (every config tested, not just winners)."""
    with open(f"{path_dir}/results_{family}.json", "w") as f:
        json.dump({"family": family, "n_tested": len(configs), "configs": configs}, f, indent=1)
    print(f"saved {len(configs)} configs -> results_{family}.json")
