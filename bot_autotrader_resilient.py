import os
import time
import json
import math
import traceback
from datetime import datetime, timezone

import requests
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands

# ========= ENV / CONFIG =========
CAPITAL_API_KEY       = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_USERNAME      = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_API_PASSWORD  = os.environ.get("CAPITAL_API_PASSWORD", "")
CAPITAL_BASE_URL      = os.environ.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")

TELEGRAM_BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
TRADE_ENABLED         = os.environ.get("TRADE_ENABLED", "false").lower() == "true"

CHECK_INTERVAL_SEC    = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))
HISTORY_PERIOD        = os.environ.get("HISTORY_PERIOD", "1mo")
HISTORY_INTERVAL      = os.environ.get("HISTORY_INTERVAL", "1h")
LEVERAGE              = float(os.environ.get("LEVERAGE", "20"))
POSITION_FRACTION     = float(os.environ.get("POSITION_FRACTION", "0.25"))
SL_PCT                = float(os.environ.get("SL_PCT", "0.006"))
TP_MULT               = float(os.environ.get("TP_MULT", "2.0"))

# === EPIC codes (точные) ===
SYMBOLS = {
    "Gold":  {"epic": "CS.D.GC.FWM3.IP", "yf": "GC=F", "query": "gold"},
    "Brent": {"epic": "CC.D.LCO.UME.IP", "yf": "BZ=F", "query": "brent"},
    "Gas":   {"epic": "CC.D.NG.UME.IP",  "yf": "NG=F", "query": "natural gas"},
}

TOKENS = {"CST": "", "X-SECURITY-TOKEN": ""}


# ========= UTILS =========
def now_s():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg):
    print(f"[{now_s()}] {msg}", flush=True)

def tgsend(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15
        )
    except Exception as e:
        log(f"⚠️ Telegram error: {e}")

def cap_headers():
    base = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Accept": "application/json"}
    if TOKENS["CST"] and TOKENS["X-SECURITY-TOKEN"]:
        base["CST"] = TOKENS["CST"]
        base["X-SECURITY-TOKEN"] = TOKENS["X-SECURITY-TOKEN"]
    return base

def safe_req(method, url, retries=2, **kwargs):
    for i in range(retries + 1):
        try:
            r = requests.request(method, url, timeout=25, **kwargs)
            return r
        except Exception as e:
            if i == retries:
                log(f"❌ HTTP error for {url}: {e}")
                return None
            time.sleep(1.5)


# ========= CAPITAL API =========
def capital_login():
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/session"
        payload = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD}
        headers = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Content-Type": "application/json"}
        r = safe_req("POST", url, json=payload, headers=headers)
        if not r:
            log("🔥 Capital login request failed (no response)")
            return False

        log(f"🔍 Capital login status: {r.status_code}")
        if "CST" in r.headers and "X-SECURITY-TOKEN" in r.headers and r.status_code == 200:
            TOKENS["CST"] = r.headers["CST"]
            TOKENS["X-SECURITY-TOKEN"] = r.headers["X-SECURITY-TOKEN"]
            log("✅ Capital login OK")
            return True
        else:
            log(f"❌ Login failed: {r.text}")
            return False
    except Exception as e:
        log(f"🔥 Capital login exception: {e}")
        return False


def capital_price(epic):
    url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
    r = safe_req("GET", url, headers=cap_headers())
    if not r or r.status_code != 200:
        return None
    try:
        arr = r.json().get("prices", [])
        if not arr:
            return None
        p = arr[-1]
        bid = float(p.get("bid", 0) or 0)
        ask = float(p.get("offer", 0) or 0)
        mid = (bid + ask) / 2 if bid and ask else (bid or ask)
        return {"bid": bid, "ask": ask, "mid": mid}
    except Exception as e:
        log(f"🔥 price parse error: {e}")
        return None


def capital_order(epic, direction, size):
    """direction: 'BUY' или 'SELL'"""
    if not TRADE_ENABLED:
        log(f"⚙️ Trade disabled, skip {direction}")
        return False
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/positions"
        body = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "orderType": "MARKET",
            "forceOpen": True,
            "guaranteedStop": False,
            "stopDistance": SL_PCT,
            "limitDistance": SL_PCT * TP_MULT,
            "currencyCode": "USD",
        }
        r = safe_req("POST", url, headers=cap_headers(), json=body)
        if r and r.status_code in (200, 201):
            log(f"✅ OPEN OK: {epic} {direction} size={size}")
            tgsend(f"✅ Открыта позиция {direction} {epic} size={size}")
            return True
        else:
            log(f"❌ Ошибка открытия {epic}: {r.text if r else 'нет ответа'}")
            tgsend(f"❌ Не удалось открыть позицию {direction} {epic}")
            return False
    except Exception as e:
        log(f"🔥 capital_order exception: {e}")
        return False


# ========= STRATEGY =========
def get_signal(df):
    """Возвращает BUY, SELL или HOLD"""
    if len(df) < 50:
        return "HOLD"

    close = df["Close"].astype(float)
    ema_fast = EMAIndicator(close, window=10).ema_indicator()
    ema_slow = EMAIndicator(close, window=30).ema_indicator()
    rsi = RSIIndicator(close, window=14).rsi()

    if ema_fast.iloc[-1] > ema_slow.iloc[-1] and rsi.iloc[-1] < 70:
        return "BUY"
    elif ema_fast.iloc[-1] < ema_slow.iloc[-1] and rsi.iloc[-1] > 30:
        return "SELL"
    else:
        return "HOLD"


# ========= MAIN LOOP =========
def main_loop():
    log("🤖 TraderKing started (Render).")
    tgsend("🤖 TraderKing запущен (Render). Автоторговля: ВКЛ. Интервал 5м.")

    if not capital_login():
        tgsend("❌ Не удалось войти в Capital API.")
        return

    while True:
        try:
            for name, meta in SYMBOLS.items():
                epic = meta["epic"]
                yf_ticker = meta["yf"]
                log(f"🔍 Checking {name} ({epic}/{yf_ticker}) ...")

                price = capital_price(epic)
                if not price:
                    log(f"⚠️ No price from Capital for {name}, fallback to Yahoo")
                    try:
                        df_yf = yf.download(yf_ticker, period="5d", interval="1h", progress=False)
                        last_close = float(df_yf["Close"].iloc[-1])
                        price = {"mid": last_close}
                    except Exception:
                        log(f"❌ No fallback price for {name}")
                        continue

                df = yf.download(yf_ticker, period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False)
                df = df.dropna()

                signal = get_signal(df)
                log(f"{name} => {signal}")

                if signal in ("BUY", "SELL"):
                    direction = signal
                    capital_order(epic, direction, 1)

            log("=== CYCLE DONE ===")
            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            log(f"🔥 MAIN LOOP error: {e}\n{traceback.format_exc()}")
            time.sleep(10)


if __name__ == "__main__":
    main_loop()
