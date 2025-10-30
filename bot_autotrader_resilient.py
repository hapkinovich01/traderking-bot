import requests
import time
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
import numpy as np
import traceback

# === НАСТРОЙКИ ===
CAPITAL_BASE_URL = "https://api-capital.backend-capital.com"
CAPITAL_API_KEY = "ТВОЙ_API_KEY"
CAPITAL_USERNAME = "ТВОЙ_EMAIL"
CAPITAL_PASSWORD = "ТВОЙ_ПАРОЛЬ"

# === EPIC-КОДЫ ДЛЯ LIVE-СЧЕТА ===
EPIC_GOLD = "GOLD"
EPIC_BRENT = "OIL_BRENT"
EPIC_GAS = "NATGAS"

# === НАСТРОЙКИ ТОРГОВЛИ ===
LEVERAGE = 20
POSITION_SIZE = 0.25  # 25% от баланса
TAKE_PROFIT_PCT = 0.015
STOP_LOSS_PCT = 0.01
TIMEFRAME = "5m"
REFRESH_INTERVAL = 300  # каждые 5 минут

tokens = {}
last_login_time = 0


# === ФУНКЦИЯ ВХОДА ===
def capital_login():
    global tokens, last_login_time
    url = f"{CAPITAL_BASE_URL}/api/v1/session"
    headers = {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    data = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_PASSWORD}
    r = requests.post(url, headers=headers, json=data)

    if r.status_code == 200:
        tokens = {
            "CST": r.headers.get("CST", ""),
            "X-SECURITY-TOKEN": r.headers.get("X-SECURITY-TOKEN", "")
        }
        last_login_time = time.time()
        print(f"✅ Вход выполнен: {CAPITAL_USERNAME}")
    else:
        print(f"❌ Ошибка входа: {r.text}")


def ensure_session():
    if time.time() - last_login_time > 1800 or not tokens:
        print("♻️ Обновляем сессию...")
        capital_login()


# === ПОЛУЧЕНИЕ ЦЕНЫ ===
def get_price(epic):
    ensure_session()
    url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
    headers = {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "CST": tokens.get("CST", ""),
        "X-SECURITY-TOKEN": tokens.get("X-SECURITY-TOKEN", ""),
        "Accept": "application/json"
    }
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        data = r.json()
        prices = data.get("prices", [])
        if prices:
            return float(prices[-1]["closePrice"]["bid"])
    else:
        print(f"⚠️ Ошибка получения цены {epic}: {r.text}")
    return None


# === РАСЧЁТ ИНДИКАТОРОВ ===
def get_signal(symbol):
    import yfinance as yf
    df = yf.download(symbol, interval=TIMEFRAME, period="7d")

    if df.empty:
        print(f"⚠️ Нет данных для {symbol}")
        return None

    df["EMA20"] = EMAIndicator(df["Close"], window=20).ema_indicator()
    df["EMA50"] = EMAIndicator(df["Close"], window=50).ema_indicator()
    df["RSI"] = RSIIndicator(df["Close"], window=14).rsi()
    macd = MACD(df["Close"])
    df["MACD"] = macd.macd()
    df["Signal"] = macd.macd_signal()

    last = df.iloc[-1]

    # Условия для входа
    if last["EMA20"] > last["EMA50"] and last["RSI"] < 70 and last["MACD"] > last["Signal"]:
        return "BUY"
    elif last["EMA20"] < last["EMA50"] and last["RSI"] > 30 and last["MACD"] < last["Signal"]:
        return "SELL"
    else:
        return None


# === ОТКРЫТИЕ СДЕЛКИ ===
def open_position(epic, direction, size):
    ensure_session()
    url = f"{CAPITAL_BASE_URL}/api/v1/positions/otc"
    headers = {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "CST": tokens.get("CST", ""),
        "X-SECURITY-TOKEN": tokens.get("X-SECURITY-TOKEN", ""),
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    price = get_price(epic)
    if not price:
        print(f"❌ Нет цены для {epic}")
        return

    take_profit = price * (1 + TAKE_PROFIT_PCT if direction == "BUY" else 1 - TAKE_PROFIT_PCT)
    stop_loss = price * (1 - STOP_LOSS_PCT if direction == "BUY" else 1 + STOP_LOSS_PCT)

    data = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "orderType": "MARKET",
        "level": None,
        "limitLevel": round(take_profit, 2),
        "stopLevel": round(stop_loss, 2),
        "forceOpen": True,
        "guaranteedStop": False,
        "currencyCode": "USD"
    }

    r = requests.post(url, headers=headers, json=data)
    if r.status_code == 200:
        print(f"✅ Открыта позиция {direction} по {epic} @ {price}")
    else:
        print(f"❌ Ошибка открытия {epic} ({direction}): {r.text}")


# === ГЛАВНЫЙ ЦИКЛ ===
def trade_cycle():
    print("🔁 TraderKing запущен")
    try:
        for epic, symbol in [(EPIC_GOLD, "GC=F"), (EPIC_BRENT, "BZ=F"), (EPIC_GAS, "NG=F")]:
            signal = get_signal(symbol)
            if not signal:
                print(f"➡️ Нет сигнала для {symbol}")
                continue

            print(f"📈 {symbol}: сигнал {signal}")
            open_position(epic, signal, size=1)

    except Exception:
        print(traceback.format_exc())


# === ЗАПУСК ===
if __name__ == "__main__":
    capital_login()
    while True:
        trade_cycle()
        print("🕒 Цикл завершен. Ожидание 5 минут...\n")
        time.sleep(REFRESH_INTERVAL)
