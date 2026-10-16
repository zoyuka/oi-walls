#!/usr/bin/env python3
"""One-shot intraday history collector for the 30-minute strategy study.
Yahoo caps: 30m -> 60 days, 60m -> 730 days. Runs in Actions where Yahoo works.
"""
import os
import yfinance as yf

os.makedirs("data/bt30", exist_ok=True)
SYMS = ["SPY", "QQQ", "^GSPC", "ES=F", "NQ=F", "^VIX"]

def fname(sym):
    return sym.replace("^", "").replace("=F", "_F")

for sym in SYMS:
    for iv, per in (("30m", "60d"), ("60m", "730d")):
        try:
            df = yf.Ticker(sym).history(period=per, interval=iv)
            df.to_csv(f"data/bt30/{fname(sym)}_{iv}.csv")
            print(sym, iv, "rows:", len(df))
        except Exception as e:
            print(sym, iv, "FAILED:", e)

for sym in ("SPY", "QQQ", "^GSPC", "^VIX"):
    df = yf.Ticker(sym).history(period="max", interval="1d")
    df.to_csv(f"data/bt30/{fname(sym)}_1d.csv")
    print(sym, "1d rows:", len(df))
