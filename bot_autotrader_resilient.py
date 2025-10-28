import os
import time
import json
import requests
import asyncio
import traceback
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD

# =================== ENV CONFIG ===================
CAPITAL_API_KEY = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_API_PASSWORD = os.environ.get("CAPITAL_API_PASSWORD", "")
CAPITAL_USERNAME = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_BASE_URL = os.environ.get("CAPITAL_BASE_URL")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TRADE_ENABLED = os.environ.get("TRADE_ENABLED", "true").lower() == "true"
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", 300))
HISTORY_PERIOD = os.environ.get("HISTORY_PERIOD", "5d")
HISTORY_INTERVAL = os.environ.get("HISTORY_INTERVAL", "1h")
POSITION_FRACTION = float(os.environ.get("POSITION_FRACTION", "0.25"))
LEVERAGE = float(os.environ.get("LEVERAGE", "20"))
SL_PCT = float(os.environ.get("SL_PCT", "0.006"))
TP_MULT = float(os.environ.get("TP_MULT", "2.0"))

# EPIC-коды Capital.com
SYMBOLS = {
    "Gold": {"epic": "GOLD", "yahoo": "GC=F"},
    "Brent": {"epic": "OIL_BRENT", "yahoo": "BZ=F"},
    "Gas": {"epic": "NATURALGAS", "yahoo": "NG=F"}
}

# ===================================================

def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S UTC}] {msg}", flush=True)

def send_message(text):
    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                timeout=15
            )
            if r.status_code == 200:
                return
        except Exception as e:
            log(f"⚠️ Telegram send fail: {e}")
            time.sleep(3)

# =================== CAPITAL API ===================

def capital_login():
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/session"
        r = requests.post(url, json={
            "identifier": CAPITAL_USERNAME,
            "password": CAPITAL_API_PASSWORD
        }, headers={"X-CAP-API-KEY": CAPITAL_API_KEY})
        if r.status_code == 200:
            data = r.json()
            TOKENS["CST"] = data["CST"]
            TOKENS["X-SECURITY-TOKEN"] = data["X-SECURITY-TOKEN"]
            log("✅ Capital login OK")
            return True
        else:
            log(f"❌ Capital login failed: {r.text}")
            return False
    except Exception as e:
        log(f"🔥 Capital login exception: {e}")
        return False

TOKENS = {"CST": "", "X-SECURITY-TOKEN": ""}

def cap_headers():
    return {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "CST": TOKENS["CST"],
        "X-SECURITY-TOKEN": TOKENS["X-SECURITY-TOKEN"]
    }

def capital_price(epic):
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
        r = requests.get(url, headers=cap_headers(), timeout=10)
        if r.status_code != 200:
            return None
        prices = r.json().get("prices", [])
        if not prices:
            return None
        p = prices[-1]
        bid = float(p.get("bid", 0) or 0)
        ask = float(p.get("offer", 0) or 0)
        return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
    except Exception:
        return None

# =================== MARKET ANALYSIS ===================

def compute_indicators(df):
    if df is None or df.empty:
        return None
    df["Close"] = df["Close"].astype(float).squeeze()
    rsi = RSIIndicator(df["Close"]).rsi().iloc[-1]
    ema20 = EMAIndicator(df["Close"], 20).ema_indicator().iloc[-1]
    ema50 = EMAIndicator(df["Close"], 50).ema_indicator().iloc[-1]
    macd = MACD(df["Close"])
    macd_val = macd.macd().iloc[-1]
    macd_sig = macd.macd_signal().iloc[-1]
    return {"rsi": rsi, "ema20": ema20, "ema50": ema50, "macd": macd_val, "macd_sig": macd_sig}

def get_signal(ind):
    if ind is None:
        return "HOLD"
    if ind["rsi"] < 35 and ind["ema20"] > ind["ema50"]:
        return "BUY"
    elif ind["rsi"] > 65 and ind["ema20"] < ind["ema50"]:
        return "SELL"
    return "HOLD"

# =================== MAIN LOOP ===================

async def check_symbol(name, meta):
    try:
        log(f"🔍 Checking {name} ({meta['yahoo']}) ...")
        price = capital_price(meta["epic"])
        if not price:
            log(f"⚠️ No price from Capital for {name}, fallback to Yahoo")
            df = yf.download(meta["yahoo"], period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False)
            if df is None or df.empty:
                log(f"❌ No data for {name} from Yahoo either")
                return
            price = {"mid": float(df["Close"].iloc[-1])}

        df = yf.download(meta["yahoo"], period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False)
        ind = compute_indicators(df)
        signal = get_signal(ind)
        send_message(f"📊 {name}: Price {price['mid']:.2f} | RSI {ind['rsi']:.2f} | Signal: {signal}")
        log(f"{name} => {signal}")

    except Exception as e:
        err = f"⚠️ Ошибка {name}: {type(e).__name__}: {e}"
        log(err)
        send_message(err)

async def main_loop():
    try:
        send_message("🤖 TraderKing запущен (Render). Автоторговля активна.")
        if not capital_login():
            send_message("❌ Ошибка входа в Capital API!")
            return

        while True:
            for s, meta in SYMBOLS.items():
                await check_symbol(s, meta)
            log("=== CYCLE DONE ===")
            await asyncio.sleep(CHECK_INTERVAL_SEC)
    except Exception as e:
        msg = f"🔥 MAIN LOOP error: {type(e).__name__}: {e}"
        log(msg)
        send_message(msg)
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main_loop())
