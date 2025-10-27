import os
import time
import json
import math
import asyncio
from datetime import datetime, timezone

import requests
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands

# ===============================
# ENV / CONFIG (Render Environment Variables)
# ===============================

CAPITAL_API_KEY = os.environ["CAPITAL_API_KEY"]
CAPITAL_USERNAME = os.environ["CAPITAL_USERNAME"]
CAPITAL_API_PASSWORD = os.environ["CAPITAL_API_PASSWORD"]
CAPITAL_BASE_URL = os.environ["CAPITAL_BASE_URL"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TRADE_ENABLED = os.environ.get("TRADE_ENABLED", "True").lower() == "true"

# === Strategy / Risk Management ===
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))  # 5 min
HISTORY_PERIOD = os.environ.get("HISTORY_PERIOD", "5d")
HISTORY_INTERVAL = os.environ.get("HISTORY_INTERVAL", "1h")
LEVERAGE = float(os.environ.get("LEVERAGE", "20"))  # 1:20
POSITION_FRACTION = float(os.environ.get("POSITION_FRACTION", "0.25"))  # 25% balance
SL_PCT = float(os.environ.get("SL_PCT", "0.006"))  # Stop loss
TP_MULT = float(os.environ.get("TP_MULT", "2.0"))  # Take profit = 2x SL

SYMBOLS = {
    "Gold":  {"yf": "GC=F", "query": "gold"},
    "Brent": {"yf": "BZ=F", "query": "brent"},
    "Gas":   {"yf": "NG=F", "query": "natural gas"},
}

TOKENS = {"CST": "", "X-SECURITY-TOKEN": ""}
LAST_SIGNAL = {name: None for name in SYMBOLS.keys()}

# ===========================
#   UTILS
# ===========================
def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg: str):
    print(f"[{ts()}] {msg}", flush=True)

def tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
    except Exception as e:
        log(f"Telegram error: {e}")

def safe_req(method, url, retries=3, **kwargs):
    for i in range(retries):
        try:
            r = requests.request(method, url, timeout=25, **kwargs)
            # pass through 401 to let caller relogin
            if r.status_code in (200, 201) or r.status_code == 401:
                return r
        except Exception as e:
            log(f"NET err {e} [{i+1}/{retries}] -> {url}")
        time.sleep(2 + i)
    return None

# ===========================
#   CAPITAL API
# ===========================
def cap_headers():
    return {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "CST": TOKENS["CST"],
        "X-SECURITY-TOKEN": TOKENS["X-SECURITY-TOKEN"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def capital_login() -> bool:
    url = f"{CAPITAL_BASE_URL}/api/v1/session"
    data = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD, "encryptedPassword": False}
    r = safe_req("POST", url, headers={"X-CAP-API-KEY": CAPITAL_API_KEY, "Accept": "application/json", "Content-Type": "application/json"}, json=data)
    if not r or r.status_code != 200:
        txt = r.text if r else "no response"
        log(f"Login FAIL {getattr(r, 'status_code', '??')}: {txt}")
        tg(f"❌ Capital login failed: {txt}")
        return False
    TOKENS["CST"] = r.headers.get("CST", "")
    TOKENS["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")
    ok = bool(TOKENS["CST"] and TOKENS["X-SECURITY-TOKEN"])
    if ok:
        log("✅ Capital login OK")
    else:
        log("⚠️ Capital login response without tokens")
    return ok

def capital_accounts():
    url = f"{CAPITAL_BASE_URL}/api/v1/accounts"
    r = safe_req("GET", url, headers=cap_headers())
    if r and r.status_code == 401:
        if not capital_login():
            return None
        r = safe_req("GET", url, headers=cap_headers())
    if not r or r.status_code != 200:
        return None
    return r.json()

def capital_available_balance():
    info = capital_accounts()
    if not info:
        return None
    try:
        for acc in info.get("accounts", []):
            if acc.get("preferred", False) and acc.get("accountType") in ("CFD", "SPREADBET", "CFD_ACCOUNT"):
                bal = acc.get("balance", {})
                return float(bal.get("available", 0.0))
        # fallback older schema
        return float(info.get("accountInfo", {}).get("available", 0.0))
    except Exception:
        return None

def capital_search_epic(query: str):
    url = f"{CAPITAL_BASE_URL}/api/v1/markets"
    r = safe_req("GET", url, headers=cap_headers(), params={"search": query})
    if r and r.status_code == 401:
        if not capital_login():
            return None, None
        r = safe_req("GET", url, headers=cap_headers(), params={"search": query})
    if not r or r.status_code != 200:
        return None, None
    mkts = r.json().get("markets", [])
    # prefer commodities & tradeable
    for m in mkts:
        if m.get("instrumentType") == "COMMODITIES":
            return m.get("epic"), m.get("instrumentName")
    if mkts:
        m = mkts[0]
        return m.get("epic"), m.get("instrumentName")
    return None, None

def capital_price(epic: str):
    url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
    r = safe_req("GET", url, headers=cap_headers())
    if r and r.status_code == 401:
        if not capital_login():
            return None
        r = safe_req("GET", url, headers=cap_headers())
    if not r or r.status_code != 200:
        return None
    arr = r.json().get("prices") or []
    if not arr:
        return None
    p = arr[-1]
    bid = float(p.get("bid", 0) or 0)
    ask = float(p.get("offer", 0) or 0)
    mid = (bid + ask) / 2 if bid and ask else (bid or ask or 0)
    return {"bid": bid, "ask": ask, "mid": mid}

def capital_positions():
    url = f"{CAPITAL_BASE_URL}/api/v1/positions"
    r = safe_req("GET", url, headers=cap_headers())
    if r and r.status_code == 401:
        if not capital_login():
            return []
        r = safe_req("GET", url, headers=cap_headers())
    if not r or r.status_code != 200:
        return []
    try:
        return r.json().get("positions", [])
    except Exception:
        return []

def find_position(epic: str):
    for p in capital_positions():
        try:
            if p.get("market", {}).get("epic") == epic:
                # IG/Capital style: size + direction on position
                size = float(p.get("position", {}).get("size", 0))
                direction = (p.get("position", {}).get("direction", "") or "").upper()
                deal_id = p.get("position", {}).get("dealId") or p.get("dealId")
                return {"size": size, "direction": direction, "dealId": deal_id}
        except Exception:
            continue
    return None

def capital_place_order(epic: str, side: str, size: float, stop_dist: float, limit_dist: float):
    url = f"{CAPITAL_BASE_URL}/api/v1/positions"
    payload = {
        "epic": epic,
        "direction": side.upper(),        # BUY / SELL
        "size": float(size),
        "orderType": "MARKET",
        "timeInForce": "FILL_OR_KILL",
        "stopDistance": round(stop_dist, 2),
        "limitDistance": round(limit_dist, 2),
        "guaranteedStop": False,
    }
    r = safe_req("POST", url, headers=cap_headers(), data=json.dumps(payload))
    if r and r.status_code == 401:
        if not capital_login():
            return None
        r = safe_req("POST", url, headers=cap_headers(), data=json.dumps(payload))
    return r

def capital_close_by_opposite(epic: str, current_pos: dict):
    """Close by sending opposite market order with same size (netted)."""
    if not current_pos:
        return True
    side = "SELL" if current_pos["direction"] == "BUY" else "BUY"
    size = current_pos["size"]
    # small protective distances
    stop_dist, limit_dist = 1.0, 1.0
    r = capital_place_order(epic, side, size, stop_dist, limit_dist)
    return bool(r and r.status_code in (200, 201))

# ===========================
#   DATA & SIGNALS
# ===========================
def fetch_df(yf_ticker: str) -> pd.DataFrame:
    df = yf.download(yf_ticker, period="1mo", interval="1d, auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise RuntimeError("empty yfinance data")
    if isinstance(df["Close"], pd.DataFrame):
        df["Close"] = df["Close"].iloc[:, 0]
    return df.dropna()

def indicators(df: pd.DataFrame) -> dict:
    close = df["Close"].squeeze()  # 👈 делает массив одномерным

    rsi = RSIIndicator(close).rsi()
    ema20 = EMAIndicator(close, 20).ema_indicator()
    ema50 = EMAIndicator(close, 50).ema_indicator()
    macd = MACD(close)
    bb = BollingerBands(close)
    last = -1
    return {
        "close": float(close.iloc[last]),
        "rsi": float(rsi.iloc[last]),
        "ema20": float(ema20.iloc[last]),
        "ema50": float(ema50.iloc[last]),
        "macd": float(macd.macd().iloc[last]),
        "macd_signal": float(macd.macd_signal().iloc[last]),
        "bb_up": float(bb.bollinger_hband().iloc[last]),
        "bb_lo": float(bb.bollinger_lband().iloc[last]),
    }

def decide(ind: dict) -> str:
    buy = ind["rsi"] < 30 and ind["ema20"] > ind["ema50"] and ind["macd"] > ind["macd_signal"]
    sell = ind["rsi"] > 70 and ind["ema20"] < ind["ema50"] and ind["macd"] < ind["macd_signal"]
    if buy:  return "BUY"
    if sell: return "SELL"
    return "HOLD"

def calc_order(px_mid: float, available: float):
    target_notional = max(0.0, available) * POSITION_FRACTION * LEVERAGE
    size = max(0.1, round(target_notional / max(px_mid, 1e-6), 2))
    stop_dist  = max(0.01, px_mid * SL_PCT)
    limit_dist = stop_dist * TP_MULT
    return size, stop_dist, limit_dist

# Auto-exit logic by RSI/EMA cross
def exit_needed(signal: str, pos_dir: str, ind: dict) -> bool:
    # If long but EMA20 fell below EMA50 or RSI crossed down from > 70 -> exit
    if pos_dir == "BUY":
        if ind["ema20"] < ind["ema50"] or ind["rsi"] > 70 and signal != "BUY":
            return True
    # If short but EMA20 rose above EMA50 or RSI crossed up from < 30 -> exit
    if pos_dir == "SELL":
        if ind["ema20"] > ind["ema50"] or ind["rsi"] < 30 and signal != "SELL":
            return True
    return False

# ===========================
#   MAIN CYCLE
# ===========================
def process_one(name: str, s: dict):
    # 1) indicators by Yahoo
    df = fetch_df(s["yf"])
    ind = indicators(df)
    signal = decide(ind)

    # 2) epic & price from Capital
    epic, instr_name = capital_search_epic(s["query"])
    if not epic:
        log(f"{name}: EPIC not found")
        return

    px = capital_price(epic)
    if not px or not px["mid"]:
        log(f"{name}: no price from Capital")
        return

    # 3) manage positions
    current = find_position(epic)
    if current:
        # Check if need exit based on EMA/RSI
        if exit_needed(signal, current["direction"], ind):
            if TRADE_ENABLED:
                ok = capital_close_by_opposite(epic, current)
                if ok:
                    tg(f"✖️ EXIT {name} @ {px['mid']:.4f} (rule: EMA/RSI)")
                else:
                    tg(f"❌ EXIT failed {name}")
            else:
                tg(f"ℹ️ Would EXIT {name}, but TRADE_ENABLED=False")

    # 4) act on new signals (avoid duplicate)
    prev = LAST_SIGNAL.get(name)
    if prev != signal:
        LAST_SIGNAL[name] = signal
        tg(f"{'🟢' if signal=='BUY' else '🔴' if signal=='SELL' else '⚪'} {signal} {name} @ {px['mid']:.4f}")

    if TRADE_ENABLED and signal in ("BUY", "SELL"):
        # open only if no position in same direction
        if not current or current["direction"] != signal:
            available = capital_available_balance() or 1000.0
            size, stop_d, limit_d = calc_order(px["mid"], available)
            r = capital_place_order(epic, signal, size, stop_d, limit_d)
            if r and r.status_code in (200, 201):
                tg(f"✅ Order {signal} {name} size {size} placed.")
            else:
                code = getattr(r, "status_code", "no_resp")
                body = getattr(r, "text", "")
                tg(f"❌ Order {signal} {name} failed: {code} {body}")
        else:
            log(f"{name}: already in {signal}, skip open")

    log(f"{name}: signal={signal} px={px['mid']:.4f} rsi={ind['rsi']:.1f} ema20={ind['ema20']:.2f} ema50={ind['ema50']:.2f}")

async def main_loop():
    tg("🤖 TraderKing запущен (Render). Автоторговля: ВКЛ. Интервал: 5м.")
    if not capital_login():
        await asyncio.sleep(10)
        capital_login()

    while True:
        started = time.time()
        try:
            for name, s in SYMBOLS.items():
                try:
                    await asyncio.to_thread(process_one, name, s)
                except Exception as e:
                    tg(f"⚠️ Ошибка {name}: {e}")
        except Exception as e:
            log(f"Main loop err: {e}")
        # keep cadence at CHECK_INTERVAL_SEC from start
        sleep_left = max(5, CHECK_INTERVAL_SEC - (time.time() - started))
        await asyncio.sleep(sleep_left)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        log("Stopped by user")
