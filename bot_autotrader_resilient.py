import os
import time
import json
import asyncio
from datetime import datetime
import pandas as pd
import numpy as np
import requests
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, SMAIndicator, MACD
from ta.volatility import BollingerBands
from ta.trend import CCIIndicator
from dotenv import load_dotenv

# === Загрузка .env ===
dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
    print("✅ .env loaded successfully")
else:
    print("⚠️ .env file not found — using Render environment")

# === Переменные окружения ===
CAPITAL_API_KEY = os.getenv("CAPITAL_API_KEY", "")
CAPITAL_API_PASSWORD = os.getenv("CAPITAL_API_PASSWORD", "")
CAPITAL_USERNAME = os.getenv("CAPITAL_USERNAME", "")
CAPITAL_BASE_URL = os.getenv("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === Торговые параметры ===
CHECK_INTERVAL_SEC = 300
LEVERAGE = 20
POSITION_FRACTION = 0.25
SL_PCT = 0.004
TP_MULT = 1.3

ASSETS = {
    "GOLD": {"epic": "CS.D.GC.FXXGP.IP", "yahoo": "GC=F"},
    "OIL_BRENT": {"epic": "CS.D.BRENT.FXXGP.IP", "yahoo": "BZ=F"},
    "GAS": {"epic": "CS.D.NG.FXXGP.IP", "yahoo": "NG=F"},
}

# === Утилиты ===
def telegram_message(msg: str):
    try:
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception as e:
        print("⚠️ Telegram send failed:", e)

def safe_request(method, url, **kwargs):
    try:
        return requests.request(method, url, timeout=15, **kwargs)
    except Exception as e:
        print("Request error:", e)
        return None

# === CAPITAL ===
def capital_login():
    url = f"{CAPITAL_BASE_URL}/api/v1/session"
    headers = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Content-Type": "application/json"}
    data = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD}
    r = safe_request("POST", url, headers=headers, data=json.dumps(data))
    if not r or r.status_code != 200:
        print("❌ Login failed:", r.text if r else "no response")
        telegram_message("❌ Ошибка авторизации в Capital")
        return None
    print("✅ Capital login OK")
    return {"CST": r.headers["CST"], "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]}

def capital_headers(auth):
    return {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "CST": auth["CST"],
        "X-SECURITY-TOKEN": auth["X-SECURITY-TOKEN"],
        "Content-Type": "application/json",
    }

# === Индикаторы ===
def compute_indicators(df):
    df["RSI"] = RSIIndicator(df["Close"]).rsi()
    df["EMA20"] = EMAIndicator(df["Close"], window=20).ema_indicator()
    df["SMA50"] = SMAIndicator(df["Close"], window=50).sma_indicator()
    macd = MACD(df["Close"])
    df["MACD"] = macd.macd()
    df["MACD_SIGNAL"] = macd.macd_signal()
    bb = BollingerBands(df["Close"])
    df["BB_HIGH"] = bb.bollinger_hband()
    df["BB_LOW"] = bb.bollinger_lband()
    df["CCI"] = CCIIndicator(df["High"], df["Low"], df["Close"]).cci()
    return df

def generate_signal(df):
    latest = df.iloc[-1]
    signal = "HOLD"

    if (
        latest["RSI"] < 30
        and latest["MACD"] > latest["MACD_SIGNAL"]
        and latest["Close"] < latest["BB_LOW"]
        and latest["EMA20"] > latest["SMA50"]
    ):
        signal = "BUY"

    elif (
        latest["RSI"] > 70
        and latest["MACD"] < latest["MACD_SIGNAL"]
        and latest["Close"] > latest["BB_HIGH"]
        and latest["EMA20"] < latest["SMA50"]
    ):
        signal = "SELL"

    return signal

def open_trade(auth, epic, direction, price, sl, tp):
    url = f"{CAPITAL_BASE_URL}/api/v1/positions"
    headers = capital_headers(auth)
    data = {
        "epic": epic,
        "direction": direction,
        "size": 1.0,
        "limitLevel": tp,
        "stopLevel": sl,
        "orderType": "MARKET",
        "guaranteedStop": False,
    }
    r = safe_request("POST", url, headers=headers, data=json.dumps(data))
    if r and r.status_code == 200:
        telegram_message(f"✅ {epic} {direction} @ {price} | SL {sl}, TP {tp}")
    else:
        telegram_message(f"❌ {epic}: ошибка сделки\n{r.text if r else 'no response'}")

# === Основной цикл ===
async def main_loop():
    telegram_message("🤖 TraderKing Pro v4 запущен")
    auth = capital_login()
    if not auth:
        return

    while True:
        for asset, meta in ASSETS.items():
            try:
                df = yf.download(meta["yahoo"], period="3mo", interval="1h", progress=False)
                if df.empty:
                    print(f"⚠️ {asset}: нет данных.")
                    continue

                df = compute_indicators(df)
                signal = generate_signal(df)
                price = float(df["Close"].iloc[-1])

                if signal == "BUY":
                    sl = round(price * (1 - SL_PCT), 2)
                    tp = round(price * (1 + SL_PCT * TP_MULT), 2)
                    open_trade(auth, meta["epic"], "BUY", price, sl, tp)

                elif signal == "SELL":
                    sl = round(price * (1 + SL_PCT), 2)
                    tp = round(price * (1 - SL_PCT * TP_MULT), 2)
                    open_trade(auth, meta["epic"], "SELL", price, sl, tp)

                print(f"{datetime.now().strftime('%H:%M:%S')} | {asset}: {signal}")
            except Exception as e:
                telegram_message(f"⚠️ Ошибка {asset}: {e}")

        print("=== Цикл завершён ===")
        await asyncio.sleep(CHECK_INTERVAL_SEC)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("🛑 TraderKing остановлен вручную.")
