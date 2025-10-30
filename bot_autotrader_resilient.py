import os
import time
import json
import requests
import traceback
import yfinance as yf
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from datetime import datetime

# ========== CONFIG ==========
CAPITAL_BASE_URL = "https://api-capital.backend-capital.com"
CAPITAL_API_KEY = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_USERNAME = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_PASSWORD = os.environ.get("CAPITAL_API_PASSWORD", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# EPIC-коды (проверь под свой аккаунт!)
EPIC_GOLD = "GOLD"
EPIC_BRENT = "OIL_BRENT"
EPIC_GAS = "NATGAS"

LEVERAGE = float(os.environ.get("LEVERAGE", 20))
POSITION_FRACTION = float(os.environ.get("POSITION_FRACTION", 0.25))
SL_PCT = float(os.environ.get("SL_PCT", 0.006))
TP_MULT = float(os.environ.get("TP_MULT", 2.0))
REFRESH_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SEC", 300))
TIMEFRAME = "1h"

tokens = {}
last_login_time = 0


# ========== SANITIZE TOKENS ==========
def sanitize(value: str) -> str:
    """Удаляет все не-ASCII символы из строки"""
    return ''.join(ch for ch in str(value) if 0 <= ord(ch) < 128)


def cap_headers():
    return {
        "X-CAP-API-KEY": sanitize(CAPITAL_API_KEY),
        "CST": sanitize(tokens.get("CST", "")),
        "X-SECURITY-TOKEN": sanitize(tokens.get("X-SECURITY-TOKEN", "")),
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


# ========== TELEGRAM ==========
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


# ========== LOGIN ==========
def capital_login():
    global tokens, last_login_time
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/session"
        headers = {
            "X-CAP-API-KEY": sanitize(CAPITAL_API_KEY),
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
            print("✅ Авторизация Capital успешна")
            send_telegram("✅ TraderKing авторизовался в Capital")
            return True
        else:
            print(f"❌ Ошибка Capital login: {r.text}")
            send_telegram(f"❌ Ошибка входа Capital: {r.text}")
            return False
    except Exception as e:
        print(f"⚠️ Ошибка Capital login: {e}")
        return False


def ensure_session():
    if time.time() - last_login_time > 1800 or not tokens:
        print("♻️ Обновление сессии...")
        capital_login()


# ========== PRICE FETCH ==========
def get_price(epic):
    ensure_session()
    url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
    r = requests.get(url, headers=cap_headers())
    if r.status_code == 200:
        data = r.json()
        if "prices" in data and data["prices"]:
            return float(data["prices"][-1]["closePrice"]["bid"])
    else:
        print(f"⚠️ Ошибка цены {epic}: {r.text}")
    return None


# ========== SIGNAL GENERATION ==========
def get_signal(symbol):
    df = yf.download(symbol, interval=TIMEFRAME, period="3mo", progress=False)
    if df.empty:
        print(f"⚠️ Нет данных для {symbol}")
        return None

    # ✅ Исправляем форму данных (ошибка 1-dimensional)
    if isinstance(df["Close"], pd.DataFrame):
        df["Close"] = df["Close"].squeeze()

    df["EMA20"] = EMAIndicator(df["Close"], 20).ema_indicator()
    df["EMA50"] = EMAIndicator(df["Close"], 50).ema_indicator()
    df["RSI"] = RSIIndicator(df["Close"], 14).rsi()
    macd = MACD(df["Close"])
    df["MACD"] = macd.macd()
    df["Signal"] = macd.macd_signal()

    last = df.iloc[-1]

    if last["EMA20"] > last["EMA50"] and last["RSI"] < 70 and last["MACD"] > last["Signal"]:
        return "BUY"
    elif last["EMA20"] < last["EMA50"] and last["RSI"] > 30 and last["MACD"] < last["Signal"]:
        return "SELL"
    else:
        return None


# ========== OPEN TRADE ==========
def open_trade(epic, direction):
    ensure_session()
    price = get_price(epic)
    if not price:
        print(f"❌ Нет цены для {epic}")
        send_telegram(f"⚠️ {epic}: нет цены для открытия сделки")
        return

    tp = price * (1 + TP_MULT * SL_PCT if direction == "BUY" else 1 - TP_MULT * SL_PCT)
    sl = price * (1 - SL_PCT if direction == "BUY" else 1 + SL_PCT)

    payload = {
        "epic": epic,
        "direction": direction,
        "size": 1,
        "orderType": "MARKET",
        "limitLevel": round(tp, 2),
        "stopLevel": round(sl, 2),
        "forceOpen": True,
        "guaranteedStop": False,
        "currencyCode": "USD"
    }

    r = requests.post(f"{CAPITAL_BASE_URL}/api/v1/positions/otc", headers=cap_headers(), json=payload)
    if r.status_code == 200:
        print(f"✅ Сделка {direction} по {epic} открыта @ {price}")
        send_telegram(f"✅ {epic}: {direction} открыта @ {price}")
    else:
        print(f"❌ Ошибка открытия {epic}: {r.text}")
        send_telegram(f"❌ {epic}: ошибка открытия сделки\n{r.text}")


# ========== MAIN LOOP ==========
def trade_cycle():
    print(f"🕒 Цикл запущен {datetime.now().strftime('%H:%M:%S')}")
    try:
        for epic, yf_symbol in [(EPIC_GOLD, "GC=F"), (EPIC_BRENT, "BZ=F"), (EPIC_GAS, "NG=F")]:
            signal = get_signal(yf_symbol)
            if not signal:
                print(f"➡️ {yf_symbol}: нет сигнала")
                continue
            print(f"📈 {yf_symbol}: сигнал {signal}")
            open_trade(epic, signal)
    except Exception as e:
        send_telegram(f"⚠️ Ошибка цикла: {e}")
        print(traceback.format_exc())


# ========== START ==========
if __name__ == "__main__":
    print("🤖 TraderKing запущен!")
    send_telegram("🤖 TraderKing запущен на Render")
    capital_login()

    while True:
        trade_cycle()
        print("✅ Цикл завершен, ждем 5 минут...\n")
        time.sleep(REFRESH_INTERVAL)
