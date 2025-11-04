import os
import time
import math
import requests
import traceback
import numpy as np
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# === Настройки ===
CAPITAL_API = "https://api-capital.backend-capital.com"
CST_TOKEN = os.getenv("CST_TOKEN")
X_SECURITY_TOKEN = os.getenv("X_SECURITY_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = {
    "OIL_BRENT": "BZ=F",
    "NATURAL_GAS": "NG=F",
    "GOLD": "GC=F"
}

INTERVAL = "1m"
PERIOD = "1d"
RISK_SHARE = 0.25
SL_MULT = 2.0
TP_MULT = 3.0

# === Отправка сообщений в Telegram ===
def send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text}
        )
    except Exception as e:
        print("Ошибка отправки в Telegram:", e)

# === Получение данных с Yahoo ===
def get_data_yahoo(ticker):
    try:
        df = yf.download(ticker, period=PERIOD, interval=INTERVAL, progress=False)
        if df.empty:
            send_message(f"⚠️ {ticker}: нет данных истории из Yahoo.")
            return None
        return df
    except Exception as e:
        send_message(f"❌ Ошибка загрузки {ticker}: {e}")
        return None

# === Построение торгового сигнала ===
def build_signal(df):
    close = df["Close"]
    ema_fast = EMAIndicator(close, 9).ema_indicator()
    ema_slow = EMAIndicator(close, 21).ema_indicator()
    macd = MACD(close).macd_diff()
    rsi = RSIIndicator(close, 14).rsi()

    if ema_fast.iloc[-1] > ema_slow.iloc[-1] and macd.iloc[-1] > 0 and rsi.iloc[-1] < 70:
        return "BUY"
    elif ema_fast.iloc[-1] < ema_slow.iloc[-1] and macd.iloc[-1] < 0 and rsi.iloc[-1] > 30:
        return "SELL"
    return "HOLD"

# === Расчёт стопов ===
def compute_sl_tp(last_price, direction):
    atr = last_price * 0.003  # примерная волатильность
    if direction == "BUY":
        return last_price - atr * SL_MULT, last_price + atr * TP_MULT
    else:
        return last_price + atr * SL_MULT, last_price - atr * TP_MULT

# === Отправка ордера в Capital ===
def place_order(epic, direction, size, sl, tp):
    try:
        headers = {
            "X-CST": CST_TOKEN,
            "X-SECURITY-TOKEN": X_SECURITY_TOKEN,
            "Content-Type": "application/json"
        }
        data = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "guaranteedStop": False,
            "forceOpen": True,
            "limitLevel": tp,
            "stopLevel": sl,
            "orderType": "MARKET"
        }
        r = requests.post(f"{CAPITAL_API}/api/v1/positions", headers=headers, json=data)
        if r.status_code == 200 or r.status_code == 201:
            send_message(f"✅ Ордер {direction} размещён по {epic}\nSL={sl:.2f}, TP={tp:.2f}")
        else:
            send_message(f"⚠️ Ошибка ордера: {r.text}")
    except Exception as e:
        send_message(f"❌ Ошибка отправки ордера: {e}")

# === Основной цикл ===
def main():
    while True:
        for name, ticker in SYMBOLS.items():
            try:
                df = get_data_yahoo(ticker)
                if df is None:
                    continue

                signal = build_signal(df)
                last_price = float(df["Close"].iloc[-1])
                sl, tp = compute_sl_tp(last_price, signal)

                send_message(f"{name}: цена {last_price:.2f}, сигнал {signal}")

                if signal in ["BUY", "SELL"]:
                    # Здесь можно указать свой EPIC для каждого актива:
                    epic_map = {
                        "OIL_BRENT": "OIL_BRENT",
                        "NATURAL_GAS": "NATURAL_GAS",
                        "GOLD": "GOLD"
                    }
                    place_order(epic_map[name], signal, 1, sl, tp)

            except Exception as e:
                send_message(f"🔥 Ошибка цикла: {e}\n{traceback.format_exc()}")

        time.sleep(60)  # проверка каждую минуту

if __name__ == "__main__":
    send_message("🚀 TraderKing запущен и работает в live-режиме.")
    main()
