import os
import time
import json
import math
import traceback
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ==================== НАСТРОЙКИ ====================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

CAPITAL_BASE_URL = os.getenv("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")
CAPITAL_API_KEY = os.getenv("CAPITAL_API_KEY", "")
CAPITAL_USERNAME = os.getenv("CAPITAL_USERNAME", "")
CAPITAL_PASSWORD = os.getenv("CAPITAL_PASSWORD", "")

EPIC_GOLD  = os.getenv("EPIC_GOLD",  "")
EPIC_BRENT = os.getenv("EPIC_BRENT", "")
EPIC_GAS   = os.getenv("EPIC_GAS",   "")

INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "60"))
YF_PERIOD  = os.getenv("YF_PERIOD", "1d")
YF_INTERVAL = os.getenv("YF_INTERVAL", "1m")
LEVERAGE = float(os.getenv("LEVERAGE", "20"))
RISK_FRACTION = float(os.getenv("RISK_FRACTION", "0.25"))
ATR_WINDOW = int(os.getenv("ATR_WINDOW", "14"))
TP_ATR = float(os.getenv("TP_ATR", "1.8"))
SL_ATR = float(os.getenv("SL_ATR", "1.2"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "10"))
MIN_BARS = 100

SYMBOLS = {
    "GC=F":  {"name": "GOLD",      "epic": EPIC_GOLD},
    "CL=F":  {"name": "OIL_WTI",   "epic": EPIC_BRENT},
    "NG=F":  {"name": "GAS",       "epic": EPIC_GAS},
}

session_tokens = {"CST": "", "XST": ""}
last_signal_ts: Dict[str, float] = {}

# ==================== Утилиты ====================

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def tg_send(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=15
        )
    except Exception:
        pass

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ==================== CAPITAL API ====================

def capital_login() -> bool:
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/session"
        headers = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Accept": "application/json"}
        data = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_PASSWORD}
        r = requests.post(url, headers=headers, json=data, timeout=15)
        if r.status_code == 200:
            session_tokens["CST"] = r.headers.get("CST", "")
            session_tokens["XST"] = r.headers.get("X-SECURITY-TOKEN", "")
            log("✅ Capital login OK")
            return True
        log(f"❌ Capital login failed: {r.text}")
        return False
    except Exception as e:
        log(f"❌ Capital login error: {e}")
        return False

def capital_headers():
    return {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "CST": session_tokens["CST"],
        "X-SECURITY-TOKEN": session_tokens["XST"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def capital_get_bid_ask(epic: str) -> Tuple[Optional[float], Optional[float]]:
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
        r = requests.get(url, headers=capital_headers(), timeout=10)
        if r.status_code == 200:
            js = r.json()
            if "prices" in js and js["prices"]:
                last = js["prices"][-1]
                return float(last.get("bid")), float(last.get("ask"))
    except Exception:
        pass
    return None, None

def capital_get_balance() -> Optional[float]:
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/accounts"
        r = requests.get(url, headers=capital_headers(), timeout=10)
        if r.status_code == 200:
            for acc in r.json().get("accounts", []):
                if acc.get("preferred"):
                    return float(acc.get("balance", {}).get("available", 0))
    except Exception:
        pass
    return None

def capital_place_market(epic: str, direction: str, size: float, stop_distance: float, limit_distance: float):
    bid, ask = capital_get_bid_ask(epic)
    if not bid or not ask:
        return False, "no price"
    entry = ask if direction == "BUY" else bid
    stop_level = entry - stop_distance if direction == "BUY" else entry + stop_distance
    limit_level = entry + limit_distance if direction == "BUY" else entry - limit_distance
    payload = {
        "epic": epic,
        "direction": direction,
        "size": round(size, 2),
        "orderType": "MARKET",
        "stopLevel": round(stop_level, 2),
        "limitLevel": round(limit_level, 2),
        "forceOpen": True,
    }
    try:
        url = f"{CAPITAL_BASE_URL}/api/v2/positions"
        r = requests.post(url, headers=capital_headers(), json=payload, timeout=15)
        if r.status_code in (200, 201):
            return True, "OK"
    except Exception as e:
        return False, str(e)
    return False, r.text if 'r' in locals() else "unknown"

# ==================== Индикаторы ====================

def indicators(df):
    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    ema20 = c.ewm(span=20).mean()
    ema50 = c.ewm(span=50).mean()
    delta = c.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    rs = up.ewm(span=14).mean() / down.ewm(span=14).mean()
    rsi = 100 - (100 / (1 + rs))
    ema12 = c.ewm(span=12).mean()
    ema26 = c.ewm(span=26).mean()
    macd = ema12 - ema26
    macd_sig = macd.ewm(span=9).mean()
    macd_hist = macd - macd_sig
    atr = pd.concat([
        (h - l),
        (h - c.shift()).abs(),
        (l - c.shift()).abs()
    ], axis=1).max(axis=1).rolling(14).mean()
    return ema20, ema50, rsi, macd_hist, atr

# ==================== Получение данных ====================

def get_yf(symbol: str, epic: str):
    for period in ["1d", "5d", "1mo"]:
        try:
            df = yf.download(symbol, period=period, interval=YF_INTERVAL, progress=False)
            if isinstance(df, pd.DataFrame) and len(df) > 50:
                return df
        except Exception:
            pass
    log(f"⚠️ {symbol}: нет данных с Yahoo, fallback → Capital")
    try:
        bid, ask = capital_get_bid_ask(epic)
        if bid and ask:
            price = (bid + ask) / 2
            return pd.DataFrame({
                "Open": [price],
                "High": [price],
                "Low": [price],
                "Close": [price],
                "Volume": [0]
            })
    except Exception as e:
        log(f"⚠️ fallback Capital error: {e}")
    return None

# ==================== Сигнал ====================

def signal(df, ema20, ema50, rsi, macd_hist, atr):
    c = float(df["Close"].iloc[-1])
    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])
    r = float(rsi.iloc[-1])
    macd_now = float(macd_hist.iloc[-1])
    macd_prev = float(macd_hist.iloc[-2])
    atr_val = float(atr.iloc[-1])

    # BUY сигнал
    if (e20 > e50) and (r > 55) and (macd_prev <= 0 and macd_now > 0):
        return "BUY", SL_ATR * atr_val, TP_ATR * atr_val

    # SELL сигнал
    elif (e20 < e50) and (r < 45) and (macd_prev >= 0 and macd_now < 0):
        return "SELL", SL_ATR * atr_val, TP_ATR * atr_val

    # Если сигналов нет
    else:
        return None, None, None

# ==================== Основной цикл ====================

def main():
    log("🚀 TraderKing PRO v5 Live запущен.")
    tg_send("🤖 TraderKing запущен (Yahoo + Capital fallback).")

    if CAPITAL_API_KEY and CAPITAL_USERNAME:
        capital_login()

    while True:
        try:
            for sym, meta in SYMBOLS.items():
                df = get_yf(sym, meta["epic"])
                if df is None:
                    continue
                ema20, ema50, rsi, macd_hist, atr = indicators(df)
                s, sl, tp = signal(df, ema20, ema50, rsi, macd_hist, atr)
                if not s:
                    continue
                price = df["Close"].iloc[-1]
                msg = f"🔔 {meta['name']} {s} @ {price:.2f}\nSL={sl:.2f} TP={tp:.2f}"
                tg_send(msg)
                log(msg)
                if meta["epic"]:
                    bal = capital_get_balance() or 1000
                    size = round((bal * RISK_FRACTION * LEVERAGE) / (price * 10), 2)
                    ok, info = capital_place_market(meta["epic"], s, size, sl, tp)
                    log(f"Capital: {ok} {info}")
            log("…cycle complete…")
        except Exception as e:
            log(f"⚠️ Ошибка цикла: {e}")
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
