import os, time, json, math, asyncio, traceback
from datetime import datetime, timezone
import requests
import pandas as pd
import numpy as np
import yfinance as yf

# === ENVIRONMENT CONFIG ===
CAPITAL_API_KEY = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_API_PASSWORD = os.environ.get("CAPITAL_API_PASSWORD", "")
CAPITAL_USERNAME = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_BASE_URL = os.environ.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))  # каждые 5 минут
LEVERAGE = float(os.environ.get("LEVERAGE", "20"))
POSITION_FRACTION = float(os.environ.get("POSITION_FRACTION", "0.25"))
BASE_SL = float(os.environ.get("BASE_SL", "0.006"))  # базовый 0.6%
BASE_TP_MULT = float(os.environ.get("BASE_TP_MULT", "1.3"))  # базовый множитель TP

# === SYMBOLS ===
SYMBOLS = {
    "GOLD":  {"epic": "CS.D.GC.TODAY.IP", "yf": "GC=F"},
    "BRENT": {"epic": "CC.D.LCO.TODAY.IP", "yf": "BZ=F"},
    "GAS":   {"epic": "CC.D.NG.TODAY.IP", "yf": "NG=F"}
}

# === GLOBAL ===
CAP_SESSION = requests.Session()
TOKENS = {"CST": "", "X-SECURITY-TOKEN": ""}
LAST_LOGIN_TIME = 0
LOGIN_INTERVAL = 3600 * 6  # обновление каждые 6 часов


# === TELEGRAM ===
def send_telegram(text: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception:
        pass


# === CAPITAL AUTH ===
def capital_login():
    global LAST_LOGIN_TIME
    try:
        r = CAP_SESSION.post(
            f"{CAPITAL_BASE_URL}/api/v1/session",
            json={"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD},
            headers={"X-CAP-API-KEY": CAPITAL_API_KEY}
        )
        if r.status_code == 200:
            TOKENS["CST"] = r.headers.get("CST", "")
            TOKENS["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")
            LAST_LOGIN_TIME = time.time()
            send_telegram("✅ TraderKing авторизовался в Capital")
            return True
        else:
            send_telegram(f"❌ Ошибка входа: {r.text}")
            return False
    except Exception as e:
        send_telegram(f"🔥 Capital login exception: {e}")
        return False


def cap_headers():
    return {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "CST": TOKENS["CST"],
        "X-SECURITY-TOKEN": TOKENS["X-SECURITY-TOKEN"],
        "Content-Type": "application/json"
    }


# === ПОЛУЧЕНИЕ ЦЕНЫ (Capital + fallback Yahoo) ===
def get_price(epic, yf_symbol=None):
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
        r = CAP_SESSION.get(url, headers=cap_headers(), timeout=5)
        if r.status_code == 200:
            data = r.json()
            prices = data.get("prices")
            if prices:
                p = prices[-1]
                bid = float(p.get("bid", 0) or 0)
                ask = float(p.get("offer", 0) or 0)
                if bid and ask:
                    return round((bid + ask) / 2, 3)
    except Exception:
        pass

    # fallback Yahoo
    try:
        df = yf.download(yf_symbol, period="1d", interval="1m", progress=False)
        if not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception:
        pass

    send_telegram(f"⚠️ {yf_symbol}: нет цены для открытия сделки")
    return None


# === ВОЛАТИЛЬНОСТЬ (ATR) ===
def get_volatility(symbol):
    df = yf.download(SYMBOLS[symbol]["yf"], period="1mo", interval="1h", progress=False)
    if df.empty:
        return BASE_SL, BASE_TP_MULT

    df["tr"] = df[["High", "Low", "Close"]].apply(
        lambda x: max(x["High"] - x["Low"], abs(x["High"] - x["Close"]), abs(x["Low"] - x["Close"])), axis=1
    )
    atr = df["tr"].rolling(24).mean().iloc[-1]

    # нормализация ATR в %
    avg_price = df["Close"].iloc[-1]
    vol = atr / avg_price

    # адаптация SL и TP
    sl = BASE_SL * (1 + vol * 15)  # чем выше волатильность, тем больше SL
    tp_mult = BASE_TP_MULT * (1 + vol * 10)

    return round(sl, 5), round(tp_mult, 2)


# === ОТКРЫТИЕ СДЕЛКИ ===
def open_trade(epic, direction, size=0.1, symbol=None):
    price = get_price(epic, SYMBOLS[symbol]["yf"])
    if not price:
        send_telegram(f"⚠️ {epic}: нет цены для открытия сделки")
        return False

    sl_dynamic, tp_mult_dynamic = get_volatility(symbol)

    tp = price * (1 + tp_mult_dynamic * sl_dynamic) if direction == "BUY" else price * (1 - tp_mult_dynamic * sl_dynamic)
    sl = price * (1 - sl_dynamic) if direction == "BUY" else price * (1 + sl_dynamic)

    payload = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "orderType": "MARKET",
        "limitLevel": round(tp, 2),
        "stopLevel": round(sl, 2),
        "forceOpen": True,
        "guaranteedStop": False,
        "currencyCode": "USD"
    }

    try:
        r = CAP_SESSION.post(f"{CAPITAL_BASE_URL}/api/v1/positions", headers=cap_headers(), json=payload)
        if r.status_code == 200:
            send_telegram(f"✅ {epic}: {direction} @ {price}\nTP={round(tp,2)} | SL={round(sl,2)} (ATR адаптив)")
            return True
        else:
            send_telegram(f"❌ {epic}: ошибка открытия сделки\n{r.text}")
            return False
    except Exception as e:
        send_telegram(f"🔥 Ошибка открытия сделки: {e}")
        return False


# === ПРОВЕРКА TP / SL ===
def check_and_close_positions():
    try:
        r = CAP_SESSION.get(f"{CAPITAL_BASE_URL}/api/v1/positions", headers=cap_headers())
        if r.status_code != 200:
            return
        data = r.json().get("positions", [])
        for pos in data:
            deal_id = pos.get("position", {}).get("dealId")
            epic = pos.get("market", {}).get("epic")
            direction = pos.get("position", {}).get("direction")
            open_price = float(pos.get("position", {}).get("openLevel", 0))
            current = get_price(epic)
            if not current or not deal_id:
                continue

            # ручная проверка TP / SL
            if direction == "BUY" and (current >= open_price * 1.02 or current <= open_price * 0.99):
                CAP_SESSION.delete(f"{CAPITAL_BASE_URL}/api/v1/positions/{deal_id}", headers=cap_headers())
                send_telegram(f"💰 {epic} BUY закрыта @ {current}")
            elif direction == "SELL" and (current <= open_price * 0.98 or current >= open_price * 1.01):
                CAP_SESSION.delete(f"{CAPITAL_BASE_URL}/api/v1/positions/{deal_id}", headers=cap_headers())
                send_telegram(f"💰 {epic} SELL закрыта @ {current}")
    except Exception as e:
        send_telegram(f"⚠️ Ошибка при проверке позиций: {e}")


# === СИГНАЛ (RSI) ===
def get_signal(symbol):
    df = yf.download(SYMBOLS[symbol]["yf"], period="3mo", interval="1h", progress=False)
    df["rsi"] = df["Close"].diff().rolling(14).mean()
    if df["rsi"].iloc[-1] > 0.5:
        return "BUY"
    elif df["rsi"].iloc[-1] < -0.5:
        return "SELL"
    return "HOLD"


# === ГЛАВНЫЙ ЦИКЛ ===
async def main_loop():
    if not capital_login():
        return
    while True:
        try:
            if time.time() - LAST_LOGIN_TIME > LOGIN_INTERVAL:
                capital_login()

            check_and_close_positions()

            for sym, meta in SYMBOLS.items():
                epic = meta["epic"]
                direction = get_signal(sym)
                send_telegram(f"🔎 {sym} => {direction}")
                if direction in ["BUY", "SELL"]:
                    open_trade(epic, direction, symbol=sym)

            send_telegram("=== Цикл завершён ===")
        except Exception as e:
            send_telegram(f"⚠️ Ошибка цикла: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SEC)


# === START ===
if __name__ == "__main__":
    send_telegram("🤖 TraderKing Adaptive запущен (Render). TP/SL по ATR. Работа 24/7.")
    asyncio.run(main_loop())
