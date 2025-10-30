import os
import time
import json
import traceback
from datetime import datetime, timezone

import requests
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

# ========= CONFIG =========
CAPITAL_API_KEY       = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_USERNAME      = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_API_PASSWORD  = os.environ.get("CAPITAL_API_PASSWORD", "")
CAPITAL_BASE_URL      = os.environ.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")
TELEGRAM_BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")
TRADE_ENABLED         = os.environ.get("TRADE_ENABLED", "false").lower() == "true"

CHECK_INTERVAL_SEC = 300  # 5 минут
LEVERAGE = 20
POSITION_FRACTION = 0.25
SL_PCT = 0.006
TP_MULT = 2.0

# === EPIC codes ===
SYMBOLS = {
    "Gold":  {"epic": "GOLD", "yf": "GC=F"},
    "Brent": {"epic": "OIL_BRENT", "yf": "BZ=F"},
    "Gas":   {"epic": "NATURALGAS",  "yf": "NG=F"},
}

TOKENS = {"CST": "", "X-SECURITY-TOKEN": ""}


# ========= UTILITIES =========
def now_s():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg):
    print(f"[{now_s()}] {msg}", flush=True)

def tgsend(text):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                timeout=10
            )
        except Exception as e:
            log(f"⚠️ Telegram error: {e}")

def cap_headers():
    h = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Accept": "application/json"}
    if TOKENS["CST"]:
        h["CST"] = TOKENS["CST"]
    if TOKENS["X-SECURITY-TOKEN"]:
        h["X-SECURITY-TOKEN"] = TOKENS["X-SECURITY-TOKEN"]
    return h


# ========= CAPITAL API =========
def capital_login():
    url = f"{CAPITAL_BASE_URL}/api/v1/session"
    payload = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD}
    headers = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        if r.status_code == 200 and "CST" in r.headers:
            TOKENS["CST"] = r.headers["CST"]
            TOKENS["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")
            log("✅ Capital login OK")
            return True
        else:
            log(f"❌ Capital login failed: {r.text}")
            return False
    except Exception as e:
        log(f"🔥 Capital login error: {e}")
        return False


def capital_price(epic):
    url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
    try:
        r = requests.get(url, headers=cap_headers(), timeout=15)
        if r.status_code != 200:
            return None
        j = r.json()
        prices = j.get("prices", [])
        if not prices:
            return None
        p = prices[-1]
        bid = float(p.get("bid", 0))
        ask = float(p.get("offer", 0))
        return (bid + ask) / 2 if bid and ask else bid or ask
    except Exception:
        return None


def capital_order(epic, direction, size):
    if not TRADE_ENABLED:
        log(f"🧩 Simulated trade: {direction} {epic}")
        return
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/positions"
        body = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "orderType": "MARKET",
            "forceOpen": True,
            "currencyCode": "USD",
        }
        r = requests.post(url, headers=cap_headers(), json=body, timeout=15)
        if r.status_code in (200, 201):
            log(f"✅ {direction} executed on {epic}")
            tgsend(f"✅ Сделка {direction} по {epic} открыта")
        else:
            log(f"❌ Order fail: {r.text}")
    except Exception as e:
        log(f"🔥 capital_order error: {e}")


# ========= STRATEGY =========
def clean_df(df):
    """Безопасно вычленяет столбец Close из любого формата."""
    if df is None or df.empty:
        return pd.DataFrame()
    # если мультииндекс — расплющиваем
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ['_'.join(col).strip() for col in df.columns.values]
    # ищем столбец с "close" (в любом регистре)
    candidates = [c for c in df.columns if "close" in c.lower()]
    if not candidates:
        log("⚠️ DataFrame не содержит столбца Close")
        return pd.DataFrame()
    df = df.rename(columns={candidates[0]: "Close"})
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df.dropna(subset=["Close"], inplace=True)
    return df


def get_signal(df):
    """BUY / SELL / HOLD"""
    if df.empty:
        return "HOLD"
    close = df["Close"]
    ema_fast = EMAIndicator(close, 10).ema_indicator()
    ema_slow = EMAIndicator(close, 30).ema_indicator()
    rsi = RSIIndicator(close, 14).rsi()

    if ema_fast.iloc[-1] > ema_slow.iloc[-1] and rsi.iloc[-1] < 70:
        return "BUY"
    elif ema_fast.iloc[-1] < ema_slow.iloc[-1] and rsi.iloc[-1] > 30:
        return "SELL"
    return "HOLD"


# ========= MAIN =========
def main_loop():
    log("🤖 TraderKing started (Render).")
    tgsend("🤖 TraderKing запущен. Автоторговля активна.")

    if not capital_login():
        tgsend("❌ Ошибка авторизации Capital API.")
        return

    while True:
        try:
            for name, meta in SYMBOLS.items():
                epic, yf_ticker = meta["epic"], meta["yf"]
                log(f"🔍 Проверка {name} ({yf_ticker})...")

                try:
                    df_raw = yf.download(yf_ticker, period="3mo", interval="1h", progress=False)
                except Exception as e:
                    log(f"⚠️ Ошибка загрузки {yf_ticker}: {e}")
                    continue

                df = clean_df(df_raw)
                if df.empty:
                    log(f"⚠️ Нет данных по {name}")
                    continue

                signal = get_signal(df)
                log(f"{name} => {signal}")

                if signal in ["BUY", "SELL"]:
                    capital_order(epic, signal, 1)

            log("=== Цикл завершён ===")
            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            log(f"🔥 Loop error: {e}\n{traceback.format_exc()}")
            time.sleep(30)


if __name__ == "__main__":
    main_loop()
