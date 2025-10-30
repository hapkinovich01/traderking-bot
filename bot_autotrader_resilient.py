import os
import asyncio
import time
import requests
import traceback
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD

# ==========================
# ⚙️ CONFIGURATION / SETTINGS
# ==========================

CAPITAL_BASE_URL = os.environ.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")
CAPITAL_API_KEY = os.environ.get("CAPITAL_API_KEY")
CAPITAL_USERNAME = os.environ.get("CAPITAL_USERNAME")
CAPITAL_API_PASSWORD = os.environ.get("CAPITAL_API_PASSWORD")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Активы и EPIC-коды Capital.com
SYMBOLS = {
    "Gold": {"epic": "GOLD", "yf": "GC=F"},
    "Brent": {"epic": "OIL_BRENT", "yf": "BZ=F"},
    "Gas": {"epic": "NATURALGAS", "yf": "NG=F"}
}

CHECK_INTERVAL_SEC = 300   # интервал проверки (5 минут)
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
POSITION_FRACTION = 0.25
LEVERAGE = 20
SL_PCT = 0.006
TP_MULT = 2.0

# ==========================
# 🔗 Capital.com API
# ==========================

def capital_headers():
    return {
        "X-SECURITY-TOKEN": os.environ.get("CST", ""),
        "X-SECURITY-ACCESSTOKEN": os.environ.get("X_SECURITY_TOKEN", ""),
        "X-CAP-API-KEY": CAPITAL_API_KEY
    }

def tgsend(msg: str):
    """Отправка уведомлений в Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception as e:
        print(f"[Telegram Error] {e}")

def capital_login():
    """Авторизация в Capital API"""
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/session"
        payload = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD}
        headers = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Content-Type": "application/json"}
        r = requests.post(url, json=payload, headers=headers)
        if r.status_code == 200:
            data = r.json()
            os.environ["CST"] = data.get("CST", "")
            os.environ["X_SECURITY_TOKEN"] = data.get("X-SECURITY-TOKEN", "")
            print("[✅] Capital login OK")
            return True
        else:
            print("[❌] Capital login failed:", r.text)
            return False
    except Exception as e:
        print("[🔥] Capital login exception:", e)
        return False

def capital_get_price(epic):
    """Получение цены с Capital"""
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
        r = requests.get(url, headers=capital_headers())
        if r.status_code == 200:
            data = r.json()
            prices = data.get("prices", [])
            if not prices:
                return None
            p = prices[-1]
            return (p.get("bid", 0) + p.get("offer", 0)) / 2
    except Exception as e:
        print(f"[⚠️] Capital price error: {e}")
    return None

# ==========================
# 📊 Technical indicators
# ==========================

def get_signal(df):
    """Анализирует данные и выдаёт сигнал BUY/SELL/HOLD"""
    df = df.dropna(subset=["Close"])
    df["rsi"] = RSIIndicator(df["Close"]).rsi()
    df["ema_fast"] = EMAIndicator(df["Close"], window=20).ema_indicator()
    df["ema_slow"] = EMAIndicator(df["Close"], window=50).ema_indicator()
    df["macd"] = MACD(df["Close"]).macd()
    df["macd_signal"] = MACD(df["Close"]).macd_signal()

    last = df.iloc[-1]
    if last["rsi"] < RSI_OVERSOLD and last["ema_fast"] > last["ema_slow"]:
        return "BUY"
    elif last["rsi"] > RSI_OVERBOUGHT and last["ema_fast"] < last["ema_slow"]:
        return "SELL"
    return "HOLD"

# ==========================
# 💰 Capital Orders
# ==========================

def capital_order(epic, direction, size):
    """Открытие позиции"""
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/positions"
        payload = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "orderType": "MARKET",
            "currencyCode": "USD",
            "forceOpen": True,
            "guaranteedStop": False
        }
        r = requests.post(url, headers=capital_headers(), json=payload)
        if r.status_code == 200:
            print(f"[✅] {direction} executed on {epic}")
            tgsend(f"✅ {direction} executed on {epic}")
            return True
        else:
            print(f"[❌] Order fail: {r.text}")
            tgsend(f"❌ Order fail: {r.text}")
    except Exception as e:
        print(f"[⚠️] Order exception: {e}")
    return False

# ==========================
# 🔁 MAIN PROCESS
# ==========================

async def process_symbol(symbol):
    """Обрабатывает один актив"""
    try:
        meta = SYMBOLS[symbol]
        epic = meta["epic"]
        yf_ticker = meta["yf"]

        price = capital_get_price(epic)
        if not price:
            print(f"[⚠️] No Capital price for {symbol}, fallback to Yahoo")
            df = yf.download(yf_ticker, period="3mo", interval="1h", progress=False)
            if df.empty:
                print(f"[❌] No Yahoo data for {symbol}")
                return
        else:
            df = yf.download(yf_ticker, period="3mo", interval="1h", progress=False)

        signal = get_signal(df)

        if signal == "BUY":
            capital_order(epic, "BUY", 1.0)
        elif signal == "SELL":
            capital_order(epic, "SELL", 1.0)
        else:
            print(f"[ℹ️] {symbol} => HOLD")

    except Exception as e:
        print(f"[🔥] {symbol} error: {e}")
        await asyncio.sleep(1)

# ==========================
# ♾️ LOOP
# ==========================

async def main_loop():
    while True:
        print("\n=== 🔁 TraderKing cycle started ===")
        for sym in SYMBOLS.keys():
            await process_symbol(sym)
        print("=== ✅ Cycle complete, sleeping... ===\n")
        await asyncio.sleep(CHECK_INTERVAL_SEC)

if __name__ == "__main__":
    asyncio.run(main_loop())
