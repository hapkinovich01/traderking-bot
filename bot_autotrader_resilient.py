import os
import time
import json
import traceback
from datetime import datetime
import requests
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD


# ======== CONFIG ========
CAPITAL_BASE_URL = "https://api-capital.backend-capital.com"
CAPITAL_API_KEY = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_USERNAME = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_PASSWORD = os.environ.get("CAPITAL_API_PASSWORD", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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


# ======== UTILS ========
def sanitize(value: str) -> str:
    return ''.join(ch for ch in str(value) if 0 <= ord(ch) < 128)


def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception:
        pass


def cap_headers():
    return {
        "X-CAP-API-KEY": sanitize(CAPITAL_API_KEY),
        "CST": sanitize(tokens.get("CST", "")),
        "X-SECURITY-TOKEN": sanitize(tokens.get("X-SECURITY-TOKEN", "")),
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


# ======== CAPITAL LOGIN ========
def capital_login():
    global tokens, last_login_time
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/session"
        headers = {"X-CAP-API-KEY": sanitize(CAPITAL_API_KEY), "Content-Type": "application/json"}
        data = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_PASSWORD}
        r = requests.post(url, headers=headers, json=data)

        if r.status_code == 200:
            tokens = {
                "CST": r.headers.get("CST", ""),
                "X-SECURITY-TOKEN": r.headers.get("X-SECURITY-TOKEN", "")
            }
            last_login_time = time.time()
            print("✅ Capital login successful")
            send_telegram("✅ TraderKing авторизовался в Capital")
            return True
        else:
            print(f"❌ Login failed: {r.text}")
            send_telegram(f"❌ Ошибка входа: {r.text}")
            return False
    except Exception as e:
        print(f"⚠️ Login exception: {e}")
        send_telegram(f"⚠️ Ошибка авторизации: {e}")
        return False


def ensure_session():
    if time.time() - last_login_time > 1800 or not tokens:
        print("♻️ Обновление сессии...")
        capital_login()


# ======== FETCH OHLC FIX ========
def fetch_ohlc(yf_ticker: str, period="3mo", interval="1h") -> pd.DataFrame:
    df = yf.download(
        tickers=yf_ticker,
        period=period,
        interval=interval,
        group_by="column",
        auto_adjust=False,
        progress=False,
        threads=False
    )

    if df is None or df.empty:
        raise ValueError(f"No data for {yf_ticker}")

    # Убираем multiindex
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ["_".join([str(x) for x in tup if x]) for tup in df.columns.values]

    # Ищем столбец Close
    close_cols = [c for c in df.columns if "Close" in c]
    if not close_cols:
        raise ValueError(f"No Close column in data for {yf_ticker}")

    df["Close"] = df[close_cols[0]]
    if isinstance(df["Close"], pd.DataFrame):
        df["Close"] = df["Close"].squeeze()
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce").dropna()
    return df


# ======== SIGNAL GENERATION ========
def get_signal(yf_ticker):
    df = fetch_ohlc(yf_ticker)
    df["EMA20"] = EMAIndicator(df["Close"], 20).ema_indicator()
    df["EMA50"] = EMAIndicator(df["Close"], 50).ema_indicator()
    df["RSI"] = RSIIndicator(df["Close"], 14).rsi()
    macd = MACD(df["Close"])
    df["MACD"] = macd.macd()
    df["SIGNAL"] = macd.macd_signal()

    last = df.iloc[-1]
    if last["EMA20"] > last["EMA50"] and last["RSI"] < 70 and last["MACD"] > last["SIGNAL"]:
        return "BUY"
    elif last["EMA20"] < last["EMA50"] and last["RSI"] > 30 and last["MACD"] < last["SIGNAL"]:
        return "SELL"
    else:
        return None


# ======== CAPITAL PRICE ========
def get_price(epic):
    ensure_session()
    url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
    r = requests.get(url, headers=cap_headers())
    if r.status_code == 200:
        data = r.json()
        if "prices" in data and data["prices"]:
            return float(data["prices"][-1]["closePrice"]["bid"])
    print(f"⚠️ Нет цены для {epic}: {r.text}")
    return None


# ======== TRADE OPEN ========
def open_trade(epic, direction):
    ensure_session()
    price = get_price(epic)
    if not price:
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
        send_telegram(f"✅ {epic}: {direction} открыта @ {price}")
        print(f"✅ {epic}: {direction} открыта @ {price}")
    else:
        send_telegram(f"❌ {epic}: ошибка открытия сделки\n{r.text}")
        print(f"❌ Ошибка открытия {epic}: {r.text}")


# ======== MAIN LOOP ========
def trade_cycle():
    try:
        print(f"\n🕒 Цикл запущен {datetime.now().strftime('%H:%M:%S')}")

        for epic, yf_symbol in [(EPIC_GOLD, "GC=F"), (EPIC_BRENT, "BZ=F"), (EPIC_GAS, "NG=F")]:
            signal = get_signal(yf_symbol)
            if not signal:
                print(f"➡️ {yf_symbol}: нет сигнала")
                continue
            print(f"📈 {yf_symbol}: сигнал {signal}")
            open_trade(epic, signal)

        print("✅ Цикл завершен")
    except Exception as e:
        send_telegram(f"⚠️ Ошибка цикла: {e}")
        print(traceback.format_exc())


# ======== START ========
if __name__ == "__main__":
    print("🤖 TraderKing запущен!")
    send_telegram("🤖 TraderKing запущен на Render")
    capital_login()

    while True:
        trade_cycle()
        time.sleep(REFRESH_INTERVAL)
