import os
import json
import time
import asyncio
from datetime import datetime, timezone

import requests
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD

# ====================== CONFIG =========================
CAPITAL_API_KEY = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_API_PASSWORD = os.environ.get("CAPITAL_API_PASSWORD", "")
CAPITAL_USERNAME = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_BASE_URL = os.environ.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

LEVERAGE = float(os.environ.get("LEVERAGE", "20"))
POSITION_FRACTION = float(os.environ.get("POSITION_FRACTION", "0.25"))
SL_PCT = float(os.environ.get("SL_PCT", "0.006"))
TP_MULT = float(os.environ.get("TP_MULT", "2.0"))
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))
HISTORY_PERIOD = os.environ.get("HISTORY_PERIOD", "1mo")
HISTORY_INTERVAL = os.environ.get("HISTORY_INTERVAL", "1h")
TRADE_ENABLED = os.environ.get("TRADE_ENABLED", "true").lower() == "true"

SYMBOLS = {
    "Gold": {"yf": "GC=F", "query": "gold"},
    "Brent": {"yf": "BZ=F", "query": "brent"},
    "Gas": {"yf": "NG=F", "query": "natural gas"},
}

TOKENS = {"CST": "", "X-SECURITY-TOKEN": ""}
LAST_SIGNAL = {k: None for k in SYMBOLS.keys()}


# ====================== HELPERS =========================
def log(msg: str):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def tg(text: str):
    """Отправка уведомления в Telegram"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        log(f"⚠️ Telegram send error: {e}")


def safe_req(method: str, url: str, **kwargs):
    """Безопасный запрос с повтором"""
    for _ in range(3):
        try:
            r = requests.request(method, url, timeout=10, **kwargs)
            return r
        except Exception as e:
            log(f"⚠️ Request error: {e}")
            time.sleep(3)
    return None


def cap_headers():
    return {"CST": TOKENS["CST"], "X-SECURITY-TOKEN": TOKENS["X-SECURITY-TOKEN"]}


# ====================== CAPITAL API =========================
def capital_login():
    url = f"{CAPITAL_BASE_URL}/api/v1/session"
    payload = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD}
    headers = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Content-Type": "application/json"}

    r = safe_req("POST", url, json=payload, headers=headers)
    if not r or r.status_code != 200:
        log(f"❌ Capital login failed ({r.status_code if r else 'no response'})")
        return False

    TOKENS["CST"] = r.headers.get("CST", "")
    TOKENS["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")
    log("✅ Capital login OK")
    return True


def capital_price(epic: str):
    url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
    r = safe_req("GET", url, headers=cap_headers())
    if not r or r.status_code != 200:
        log(f"⚠️ no price from Capital for {epic}")
        return None
    try:
        prices = r.json().get("prices", [])
        if not prices:
            return None
        p = prices[-1]
        bid = float(p.get("bid", 0))
        ask = float(p.get("offer", 0))
        return (bid + ask) / 2
    except Exception as e:
        log(f"⚠️ price parse error: {e}")
        return None


# ====================== DATA CLEANING =========================
def close_series_1d(df: pd.DataFrame) -> pd.Series:
    """Принудительно приводит колонку Close к одномерной серии"""
    col = "Close" if "Close" in df.columns else "Adj Close"
    if col not in df.columns:
        raise ValueError("No Close or Adj Close in dataframe")

    s = df[col]

    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]

    if hasattr(s.values, "ndim") and s.values.ndim == 2:
        s = pd.Series(s.values.reshape(-1), index=df.index, name=col)

    s = pd.to_numeric(s, errors="coerce").dropna()
    return s


# ====================== INDICATORS =========================
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = close_series_1d(df)
    out = pd.DataFrame(index=close.index)
    out["Close"] = close

    out["EMA20"] = EMAIndicator(out["Close"], window=20).ema_indicator()
    out["EMA50"] = EMAIndicator(out["Close"], window=50).ema_indicator()
    out["RSI"] = RSIIndicator(out["Close"], window=14).rsi()
    macd = MACD(out["Close"])
    out["MACD"] = macd.macd()
    out["MACD_signal"] = macd.macd_signal()
    return out.dropna()


# ====================== STRATEGY =========================
def decide(df: pd.DataFrame) -> str:
    last = df.iloc[-1]
    if last["EMA20"] > last["EMA50"] and last["RSI"] < 70 and last["MACD"] > last["MACD_signal"]:
        return "BUY"
    elif last["EMA20"] < last["EMA50"] and last["RSI"] > 30 and last["MACD"] < last["MACD_signal"]:
        return "SELL"
    else:
        return "HOLD"


# ====================== MAIN LOOP =========================
async def main_loop():
    log("🤖 TraderKing started (Render).")
    tg(f"🤖 TraderKing запущен (Render). Автоторговля: {'ВКЛ' if TRADE_ENABLED else 'ВЫКЛ'}. Интервал: {CHECK_INTERVAL_SEC//60}м.")

    if not capital_login():
        tg("❌ Ошибка входа в Capital")
        return

    while True:
        try:
            for name, meta in SYMBOLS.items():
                log(f"🔍 Checking {name}...")
                price = capital_price(meta["yf"])

                # если нет данных от Capital — fallback на Yahoo
                if not price:
                    log(f"⚠️ {name}: no Capital price, fallback to Yahoo")
                    df = yf.download(meta["yf"], period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False, auto_adjust=True)
                    if df.empty:
                        tg(f"⚠️ Нет данных Yahoo для {name}")
                        continue
                    price = float(close_series_1d(df).iloc[-1])

                log(f"✅ {name} Price: {price}")

                # Загружаем историю
                df = yf.download(meta["yf"], period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False, auto_adjust=True)
                if df.empty:
                    tg(f"⚠️ {name}: пустые данные истории")
                    continue

                df = calc_indicators(df)
                signal = decide(df)
                tg(f"{name} Price: {price:.2f}\nSignal: {signal}\nRSI: {df['RSI'].iloc[-1]:.2f}")
                log(f"{name} => {signal}")

            log("=== CYCLE DONE ===")
            await asyncio.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            log(f"🔥 MAIN LOOP error: {e}")
            tg(f"🔥 Ошибка цикла: {e}")
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main_loop())
