import os
import time
import math
import json
import requests
import traceback
import numpy as np
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from dotenv import load_dotenv

load_dotenv()

# === Настройки ===
CAPITAL_API = "https://api-capital.backend-capital.com"
CST_TOKEN = os.getenv("CST_TOKEN")
X_SECURITY_TOKEN = os.getenv("X_SECURITY_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

RISK_SHARE = 0.25
SL_MULT = 2.0
TP_MULT = 3.0
INTERVAL = "1m"
PERIOD = "1d"

# === Символы для анализа Yahoo ===
SYMBOLS = {
    "GOLD": "GC=F",
    "OIL_BRENT": "BZ=F",
    "NATURAL_GAS": "NG=F"
}

# === Отправка сообщений в Telegram ===
def send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text}
        )
    except:
        pass

# === Получение EPIC-кодов Capital ===
def get_epic(symbol_name):
    try:
        headers = {
            "X-CST": CST_TOKEN,
            "X-SECURITY-TOKEN": X_SECURITY_TOKEN
        }
        r = requests.get(f"{CAPITAL_API}/api/v1/markets?searchTerm={symbol_name}", headers=headers)
        data = r.json()
        if "markets" in data and len(data["markets"]) > 0:
            epic = data["markets"][0]["epic"]
            return epic
        else:
            send_message(f"⚠️ Не найден EPIC для {symbol_name}")
            return None
    except Exception as e:
        send_message(f"❌ Ошибка EPIC: {e}")
        return None

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

# === Генерация торгового сигнала ===
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
    atr = last_price * 0.0025  # волатильность ~0.25%
    if direction == "BUY":
        sl = last_price - atr * SL_MULT
        tp = last_price + atr * TP_MULT
    else:
        sl = last_price + atr * SL_MULT
        tp = last_price - atr * TP_MULT
    return sl, tp

# === Получение баланса Capital ===
def get_balance():
    try:
        headers = {
            "X-CST": CST_TOKEN,
            "X-SECURITY-TOKEN": X_SECURITY_TOKEN
        }
        r = requests.get(f"{CAPITAL_API}/api/v1/accounts", headers=headers)
        data = r.json()
        return float(data["balance"]["available"])
    except Exception as e:
        send_message(f"⚠️ Не удалось получить баланс: {e}")
        return 0.0

# === Размещение ордера ===
def place_order(epic, direction, size, sl, tp):
    try:
        headers = {
            "X-CST": CST_TOKEN,
            "X-SECURITY-TOKEN": X_SECURITY_TOKEN,
            "Content-Type": "application/json"
        }
        payload = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "orderType": "MARKET",
            "limitLevel": tp,
            "stopLevel": sl,
            "guaranteedStop": False,
            "forceOpen": True
        }

        r = requests.post(f"{CAPITAL_API}/api/v1/positions", headers=headers, json=payload)
        if r.status_code in [200, 201]:
            send_message(f"✅ Ордер {direction} по {epic} создан.\nSL={sl:.2f}, TP={tp:.2f}")
        else:
            send_message(f"⚠️ Ошибка ордера {epic}: {r.text}")
    except Exception as e:
        send_message(f"❌ Ошибка размещения ордера: {e}")

# === Основной цикл ===
def main():
    send_message("🚀 TraderKing LIVE запущен!")

    epic_cache = {}
    while True:
        balance = get_balance()
        if balance <= 0:
            send_message("⚠️ Баланс недоступен или равен 0.")
            time.sleep(60)
            continue

        for name, ticker in SYMBOLS.items():
            try:
                if name not in epic_cache:
                    epic_cache[name] = get_epic(name)

                epic = epic_cache.get(name)
                if not epic:
                    continue

                df = get_data_yahoo(ticker)
                if df is None:
                    continue

                signal = build_signal(df)
                last_price = float(df["Close"].iloc[-1])
                sl, tp = compute_sl_tp(last_price, signal)

                send_message(f"{name}: {signal} @ {last_price:.2f}")

                if signal in ["BUY", "SELL"]:
                    size = max(1, round(balance * RISK_SHARE / last_price))
                    place_order(epic, signal, size, sl, tp)

            except Exception as e:
                send_message(f"🔥 Ошибка цикла для {name}: {e}\n{traceback.format_exc()}")

        time.sleep(60)  # цикл 1 минута

if __name__ == "__main__":
    main()
