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

CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL_SEC", "300"))
HISTORY_PERIOD = os.getenv("HISTORY_PERIOD", "3mo")
HISTORY_INTERVAL = os.getenv("HISTORY_INTERVAL", "1h")

LEVERAGE = float(os.getenv("LEVERAGE", "20"))
POSITION_FRACTION = float(os.getenv("POSITION_FRACTION", "0.25"))
SL_PCT = float(os.getenv("SL_PCT", "0.006"))   # 0.6%
TP_MULT = float(os.getenv("TP_MULT", "1.5"))   # 1.5x Take Profit


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
            telegram_send("✅ Авторизация в Capital успешна.")
            return True
        else:
            telegram_send(f"🚫 Ошибка входа Capital: {r.text}")
            return False
    except Exception as e:
        telegram_send(f"🔥 Ошибка авторизации Capital: {e}")
        return False


# ========= INDICATORS =========
def get_signal(df: pd.DataFrame) -> str:
    try:
        df['Close'] = df['Close'].squeeze()
        df = df.dropna(subset=['Close'])
        if df.empty:
            return "HOLD"

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

        if (
            latest['ema_fast'] > latest['ema_slow']
            and latest['rsi'] < 70
            and latest['macd'] > latest['macd_signal']
            and latest['Close'] <= latest['bb_low']
        ):
            return "BUY"

        elif (
            latest['ema_fast'] < latest['ema_slow']
            and latest['rsi'] > 30
            and latest['macd'] < latest['macd_signal']
            and latest['Close'] >= latest['bb_high']
        ):
            return "SELL"

        return "HOLD"

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
            telegram_send(f"❌ {epic}: ошибка открытия сделки\n{r.text}")
    except Exception as e:
        telegram_send(f"🔥 Ошибка при открытии {epic}: {e}")


def close_positions(epic):
    url = f"{CAPITAL_BASE_URL}/positions"
    try:
        positions = requests.get(url, headers=capital_headers()).json()
        for pos in positions.get("positions", []):
            if pos["market"]["epic"] == epic:
                deal_id = pos["position"]["dealId"]
                close_url = f"{CAPITAL_BASE_URL}/positions/{deal_id}"
                requests.delete(close_url, headers=capital_headers())
                telegram_send(f"🟡 Закрыта позиция по {epic} (реверс сигнала).")
    except Exception as e:
        telegram_send(f"⚠️ Ошибка при закрытии {epic}: {e}")


# ========= MAIN LOOP =========
async def main_loop():
    telegram_send("🤖 TraderKing Pro v5 запущен. Авто TP/SL + закрытие при смене сигнала.")

    if not capital_login():
        return

    last_signal = {}

    while True:
        for symbol, meta in SYMBOLS.items():
            try:
                df = yf.download(meta["yahoo"], period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False)
                if df.empty:
                    telegram_send(f"⚠️ {symbol}: нет данных с Yahoo Finance.")
                    continue

                signal = get_signal(df)
                price = float(df["Close"].iloc[-1].item())
                size = round(POSITION_FRACTION, 2)

                prev_signal = last_signal.get(symbol, "HOLD")

                if signal != prev_signal:
                    if prev_signal in ["BUY", "SELL"]:
                        close_positions(meta["epic"])
                    if signal in ["BUY", "SELL"]:
                        place_order(meta["epic"], signal, size, price)
                    last_signal[symbol] = signal

                print(f"{symbol}: {signal}")

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
