import os
import time
import json
import math
import asyncio
import traceback
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands

# ========= ENVIRONMENT =========
CAPITAL_API_KEY = os.getenv("CAPITAL_API_KEY", "")
CAPITAL_API_PASSWORD = os.getenv("CAPITAL_API_PASSWORD", "")
CAPITAL_USERNAME = os.getenv("CAPITAL_USERNAME", "")
CAPITAL_BASE_URL = os.getenv("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com/api/v1")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "300"))  # каждые 5 минут
HISTORY_PERIOD = os.getenv("HISTORY_PERIOD", "3mo")
HISTORY_INTERVAL = os.getenv("HISTORY_INTERVAL", "1h")

LEVERAGE = float(os.getenv("LEVERAGE", "20"))
POSITION_FRACTION = float(os.getenv("POSITION_FRACTION", "0.25"))
SL_PCT = float(os.getenv("SL_PCT", "0.006"))   # 0.6%
TP_MULT = float(os.getenv("TP_MULT", "2.0"))   # Take Profit = 2×SL

# ========= SYMBOLS =========
SYMBOLS = {
    "GOLD": {"epic": "CS.D.GC.FMIP.IP", "yahoo": "GC=F"},
    "OIL_BRENT": {"epic": "CC.D.BRENT.CFM.IP", "yahoo": "BZ=F"},
    "GAS": {"epic": "CS.D.NG.FMIP.IP", "yahoo": "NG=F"},
}

# ========= TELEGRAM =========
def telegram_send(msg):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
        except Exception as e:
            print(f"⚠️ Ошибка Telegram: {e}")

# ========= CAPITAL API =========
def capital_headers():
    return {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "Content-Type": "application/json"
    }

def capital_login():
    url = f"{CAPITAL_BASE_URL}/session"
    data = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD}
    try:
        r = requests.post(url, json=data, headers=capital_headers())
        if r.status_code == 200:
            print("✅ Capital login OK")
            return True
        else:
            print(f"⚠️ Capital login failed: {r.text}")
            return False
    except Exception as e:
        print(f"🔥 Ошибка Capital login: {e}")
        return False

# ========= INDICATORS =========
def get_signal(df: pd.DataFrame) -> str:
    try:
        df['Close'] = df['Close'].squeeze()
        df = df.dropna(subset=['Close'])
        if df.empty:
            raise ValueError("Пустой DataFrame")

        df['rsi'] = RSIIndicator(close=df['Close'], window=14).rsi()
        df['ema_fast'] = EMAIndicator(close=df['Close'], window=12).ema_indicator()
        df['ema_slow'] = EMAIndicator(close=df['Close'], window=26).ema_indicator()
        macd = MACD(close=df['Close'])
        df['macd'] = macd.macd()
        df['macd_signal'] = macd.macd_signal()
        bb = BollingerBands(close=df['Close'], window=20, window_dev=2)
        df['bb_high'] = bb.bollinger_hband()
        df['bb_low'] = bb.bollinger_lband()
        df = df.dropna()

        latest = df.iloc[-1]
        signal = "HOLD"

        if (
            latest['ema_fast'] > latest['ema_slow']
            and latest['rsi'] < 70
            and latest['macd'] > latest['macd_signal']
            and latest['Close'] <= latest['bb_low']
        ):
            signal = "BUY"

        elif (
            latest['ema_fast'] < latest['ema_slow']
            and latest['rsi'] > 30
            and latest['macd'] < latest['macd_signal']
            and latest['Close'] >= latest['bb_high']
        ):
            signal = "SELL"

        return signal
    except Exception as e:
        print(f"⚠️ Ошибка get_signal(): {e}")
        return "HOLD"

# ========= CAPITAL ORDER EXECUTION =========
def place_order(epic, direction, size, price):
    url = f"{CAPITAL_BASE_URL}/positions"
    sl = price * (1 - SL_PCT) if direction == "BUY" else price * (1 + SL_PCT)
    tp = price * (1 + SL_PCT * TP_MULT) if direction == "BUY" else price * (1 - SL_PCT * TP_MULT)

    payload = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "limitLevel": round(tp, 2),
        "stopLevel": round(sl, 2),
        "orderType": "MARKET",
        "guaranteedStop": False,
        "forceOpen": True
    }

    try:
        r = requests.post(url, headers=capital_headers(), json=payload)
        if r.status_code == 200:
            telegram_send(f"✅ {epic}: {direction} открыта @ {price}\nTP={round(tp,2)}, SL={round(sl,2)}")
        else:
            telegram_send(f"❌ {epic}: ошибка ордера\n{r.text}")
    except Exception as e:
        telegram_send(f"🔥 Ошибка при открытии {epic}: {e}")

# ========= MAIN LOOP =========
async def main_loop():
    telegram_send("🤖 TraderKing Pro v4.1 запущен. Работа 24/7")

    if not capital_login():
        telegram_send("🚫 Ошибка авторизации Capital.")
        return

    while True:
        for symbol, meta in SYMBOLS.items():
            try:
                print(f"Проверка {symbol}...")
                df = yf.download(meta["yahoo"], period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False)
                if df is None or df.empty:
                    telegram_send(f"⚠️ {symbol}: нет данных с Yahoo Finance.")
                    continue

                signal = get_signal(df)
                if len(df["Close"]) == 0:
                    continue

                price = float(df["Close"].iloc[-1].item())

                if signal in ["BUY", "SELL"]:
                    size = round(POSITION_FRACTION, 2)
                    place_order(meta["epic"], signal, size, price)
                else:
                    print(f"{symbol} => HOLD")

            except Exception as e:
                telegram_send(f"⚠️ Ошибка {symbol}: {e}")

        print("=== Цикл завершён ===")
        await asyncio.sleep(CHECK_INTERVAL_SEC)

# ========= START =========
if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except Exception as e:
        print(f"🔥 Глобальная ошибка: {e}")
        telegram_send(f"🔥 Глобальная ошибка: {e}")
