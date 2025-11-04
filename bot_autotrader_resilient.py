import os
import time
import json
import math
import asyncio
import logging
import requests
import numpy as np
import pandas as pd
import yfinance as yf

from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.volatility import BollingerBands, AverageTrueRange

# ==========================
# НАСТРОЙКИ
# ==========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CAPITAL_API_KEY = os.getenv("CAPITAL_API_KEY", "")
CAPITAL_USERNAME = os.getenv("CAPITAL_USERNAME", "")
CAPITAL_API_PASSWORD = os.getenv("CAPITAL_API_PASSWORD", "")
CAPITAL_BASE_URL = os.getenv("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")

# EPIC-и (замени на твои из live)
EPIC_GOLD = os.getenv("EPIC_GOLD", "GOLD")
EPIC_BRENT = os.getenv("EPIC_BRENT", "OIL_BRENT")

# Yahoo тикеры
YF_GOLD = os.getenv("YF_GOLD", "GC=F")
YF_BRENT = os.getenv("YF_BRENT", "BZ=F")

# Торговые параметры
LEVERAGE = float(os.getenv("LEVERAGE", "20"))
RISK_BALANCE_FRACTION = float(os.getenv("RISK_BALANCE_FRACTION", "0.25"))
MAX_CONCURRENT_POS = int(os.getenv("MAX_CONCURRENT_POS", "2"))

# Индикаторы
EMA_FAST, EMA_SLOW = 20, 50
RSI_LEN = 14
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
BB_LEN, BB_STD = 20, 2
ATR_LEN = 14
STO_K, STO_D, STO_SMOOTH = 14, 3, 3
ADX_LEN = 14

# ATR SL/TP
SL_ATR_MULT, TP_ATR_MULT = 1.8, 1.2

# Интервал и цикл
BAR_INTERVAL = os.getenv("BAR_INTERVAL", "1m")
LOOKBACK_BARS = 600
SLEEP_SECONDS = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("TraderKing")

TOKENS = {"CST": "", "X-SECURITY-TOKEN": ""}

# ==========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================

def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except:
        pass

def capital_headers():
    return {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "CST": TOKENS["CST"],
        "X-SECURITY-TOKEN": TOKENS["X-SECURITY-TOKEN"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def capital_login():
    url = f"{CAPITAL_BASE_URL}/api/v1/session"
    data = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD}
    headers = {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    r = requests.post(url, headers=headers, json=data, timeout=20)
    if r.status_code != 200:
        raise Exception(f"Login error: {r.text}")
    TOKENS["CST"] = r.headers.get("CST", "")
    TOKENS["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")
    tg("✅ <b>Capital авторизация успешна</b>")
    log.info("Login OK")

def capital_get_account():
    r = requests.get(f"{CAPITAL_BASE_URL}/api/v1/accounts", headers=capital_headers(), timeout=15)
    if r.status_code == 401:
        capital_login()
        r = requests.get(f"{CAPITAL_BASE_URL}/api/v1/accounts", headers=capital_headers(), timeout=15)
    return r.json()

def capital_market_details(epic):
    r = requests.get(f"{CAPITAL_BASE_URL}/api/v1/markets/{epic}", headers=capital_headers(), timeout=15)
    if r.status_code == 401:
        capital_login()
        r = requests.get(f"{CAPITAL_BASE_URL}/api/v1/markets/{epic}", headers=capital_headers(), timeout=15)
    return r.json()

def capital_current_price(epic):
    r = requests.get(f"{CAPITAL_BASE_URL}/api/v1/markets/{epic}", headers=capital_headers(), timeout=15)
    if r.status_code == 401:
        capital_login()
        r = requests.get(f"{CAPITAL_BASE_URL}/api/v1/markets/{epic}", headers=capital_headers(), timeout=15)
    snap = r.json().get("snapshot", {})
    bid, offer = float(snap.get("bid", "nan")), float(snap.get("offer", "nan"))
    return (bid + offer) / 2 if not np.isnan(bid) and not np.isnan(offer) else np.nan

def capital_open_positions():
    r = requests.get(f"{CAPITAL_BASE_URL}/api/v1/positions", headers=capital_headers(), timeout=15)
    if r.status_code == 401:
        capital_login()
        r = requests.get(f"{CAPITAL_BASE_URL}/api/v1/positions", headers=capital_headers(), timeout=15)
    return r.json().get("positions", [])

def capital_open_market(epic, direction, size, sl, tp):
    payload = {
        "epic": epic,
        "direction": direction,
        "size": str(size),
        "orderType": "MARKET",
        "forceOpen": True,
        "guaranteedStop": False,
        "stopLevel": float(sl),
        "limitLevel": float(tp),
    }
    r = requests.post(f"{CAPITAL_BASE_URL}/api/v1/positions", headers=capital_headers(), data=json.dumps(payload))
    if r.status_code == 401:
        capital_login()
        r = requests.post(f"{CAPITAL_BASE_URL}/api/v1/positions", headers=capital_headers(), data=json.dumps(payload))
    if r.status_code not in (200, 201):
        raise Exception(r.text)
    return r.json()

def fetch_bars(ticker):
    df = yf.download(ticker, interval=BAR_INTERVAL, period="7d", auto_adjust=True, progress=False)
    df = df.dropna().tail(LOOKBACK_BARS)
    for c in ["Open", "High", "Low", "Close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna()
    return df

def compute_indicators(df):
    close, high, low = df["Close"], df["High"], df["Low"]
    ema20, ema50 = EMAIndicator(close, 20).ema_indicator(), EMAIndicator(close, 50).ema_indicator()
    rsi = RSIIndicator(close, 14).rsi()
    macd_i = MACD(close)
    macd, macd_sig, macd_hist = macd_i.macd(), macd_i.macd_signal(), macd_i.macd_diff()
    bb = BollingerBands(close)
    bb_high, bb_low = bb.bollinger_hband(), bb.bollinger_lband()
    atr = AverageTrueRange(high, low, close).average_true_range()
    sto = StochasticOscillator(high, low, close)
    stoch_k, stoch_d = sto.stoch(), sto.stoch_signal()
    adx = ADXIndicator(high, low, close).adx()
    df = pd.DataFrame({
        "close": close,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "macd": macd,
        "macd_sig": macd_sig,
        "macd_hist": macd_hist,
        "bb_high": bb_high,
        "bb_low": bb_low,
        "atr": atr,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "adx": adx,
    }).dropna()
    return df

def signal(row_prev, row):
    buy = row["ema20"] > row["ema50"] and row["macd"] > row["macd_sig"] and row["rsi"] > 55 and row["adx"] > 20
    sell = row["ema20"] < row["ema50"] and row["macd"] < row["macd_sig"] and row["rsi"] < 45 and row["adx"] > 20
    if buy and not sell:
        return "BUY"
    elif sell and not buy:
        return "SELL"
    else:
        return "HOLD"

def trade(epic, ticker, name):
    df = fetch_bars(ticker)
    ind = compute_indicators(df)
    if len(ind) < 5:
        return
    row_prev, row = ind.iloc[-2], ind.iloc[-1]
    sig = signal(row_prev, row)
    if sig == "HOLD":
        return
    acc = capital_get_account()
    balance = float(acc.get("accounts", [{}])[0].get("balance", {}).get("available", 0.0))
    price = capital_current_price(epic)
    atr = row["atr"]
    sl = price - atr * SL_ATR_MULT if sig == "BUY" else price + atr * SL_ATR_MULT
    tp = price + atr * TP_ATR_MULT if sig == "BUY" else price - atr * TP_ATR_MULT
    size = max(0.1, round((balance * RISK_BALANCE_FRACTION * LEVERAGE) / price, 2))
    try:
        resp = capital_open_market(epic, sig, size, sl, tp)
        msg = f"✅ <b>{name}</b> {sig}\nЦена: {price:.2f}\nSL: {sl:.2f} | TP: {tp:.2f}\nRSI: {row['rsi']:.1f}"
        tg(msg)
        log.info(f"{name}: {sig} исполнен {resp}")
    except Exception as e:
        tg(f"❌ {name}: ошибка {e}")

async def main():
    tg("🚀 <b>TraderKing LIVE запущен (GOLD + BRENT)</b>")
    capital_login()
    while True:
        try:
            trade(EPIC_GOLD, YF_GOLD, "GOLD")
            trade(EPIC_BRENT, YF_BRENT, "BRENT")
        except Exception as e:
            tg(f"⚠️ Ошибка цикла: {e}")
            log.exception(e)
        time.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
