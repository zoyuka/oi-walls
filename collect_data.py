#!/usr/bin/env python3
"""One-shot data collector (runs in GitHub Actions where Yahoo is reachable).
Writes CSVs under data/ for offline backtesting:
  - daily max-history OHLCV for ^GSPC, ^NDX, SPY, QQQ, ^VIX
  - 5-minute bars (max ~60 days) for ^GSPC, SPY, QQQ
"""
import os
import yfinance as yf

os.makedirs("data", exist_ok=True)

DAILY = ["^GSPC", "^NDX", "SPY", "QQQ", "RSP", "^VIX", "^VIX1D", "^SKEW", "^VVIX",
         "ES=F", "NQ=F", "NVDA", "TSLA", "AAPL", "META"]
INTRA = ["^GSPC", "SPY", "QQQ", "ES=F", "NQ=F"]  # futures 5m includes the overnight session

def fname(sym):
    return sym.replace("^", "").replace("=F", "_F")

for sym in DAILY:
    df = yf.Ticker(sym).history(period="max", interval="1d")
    df.to_csv(f"data/{fname(sym)}_daily.csv")
    print(sym, "daily rows:", len(df))

for sym in INTRA:
    df = yf.Ticker(sym).history(period="60d", interval="5m")
    df.to_csv(f"data/{fname(sym)}_5m.csv")
    print(sym, "5m rows:", len(df))
