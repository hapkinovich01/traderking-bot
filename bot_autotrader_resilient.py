import os, time, json, math, asyncio, traceback
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
import yfinance as yf

# ========= ENV =========
CAPITAL_API_KEY     = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_API_PASSWORD= os.environ.get("CAPITAL_API_PASSWORD", "")
CAPITAL_USERNAME    = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_BASE_URL    = os.environ.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")

TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL_SEC  = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))  # 5m
HISTORY_PERIOD      = os.environ.get("HISTORY_PERIOD", "3mo")
HISTORY_INTERVAL    = os.environ.get("HISTORY_INTERVAL", "1h")

LEVERAGE            = float(os.environ.get("LEVERAGE", "20"))
POSITION_FRACTION   = float(os.environ.get("POSITION_FRACTION", "0.25"))
SL_PCT              = float(os.environ.get("SL_PCT", "0.006"))    # 0.6%
TP_MULT             = float(os.environ.get("TP_MULT", "2.0"))     # TP = 2*SL
TRADE_ENABLED       = os.environ.get("TRADE_ENABLED", "true").lower() == "true"

# !!! замени EPIC под свои рабочие (эти — шаблон; у тебя они уже найдены) !!!
SYMBOLS = {
    "Gold":  {"epic": "GOLD",        "yf": "GC=F", "min_size": 0.1},
    "Brent": {"epic": "OIL_BRENT",   "yf": "BZ=F", "min_size": 1.0},
    "Gas":   {"epic": "NATURALGAS", "yf": "NG=F", "min_size": 1.0},
}

TOKENS = {"CST": "", "X-SECURITY-TOKEN": ""}

# ========= UTILS =========
def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg):
    print(f"[{utcnow()}] {msg}", flush=True)

def tg(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: 
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    except Exception as e:
        log(f"⚠️ Telegram send error: {e}")

def cap_headers():
    h = {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "Accept": "application/json"
    }
    if TOKENS["CST"]:
        h["CST"] = TOKENS["CST"]
    if TOKENS["X-SECURITY-TOKEN"]:
        h["X-SECURITY-TOKEN"] = TOKENS["X-SECURITY-TOKEN"]
    return h

def safe_req(method, url, retries=3, **kwargs):
    for i in range(retries):
        try:
            return requests.request(method, url, timeout=25, **kwargs)
        except Exception as e:
            if i == retries - 1:
                log(f"🔥 HTTP fail {method} {url}: {e}")
            time.sleep(1)
    return None

# ========= CAPITAL =========
def capital_login():
    url = f"{CAPITAL_BASE_URL}/api/v1/session"
    headers = {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "Accept": "application/json"
    }
    data = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD}
    r = safe_req("POST", url, headers=headers, json=data)
    if not r:
        log("🔥 Capital login: no response"); return False
    if r.status_code != 200:
        log(f"🔥 Capital login error {r.status_code}: {r.text}"); return False
    TOKENS["CST"] = r.headers.get("CST","")
    TOKENS["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN","")
    ok = bool(TOKENS["CST"] and TOKENS["X-SECURITY-TOKEN"])
    log("✅ Capital login OK" if ok else "🔥 Capital login missing tokens")
    return ok

def capital_price(epic: str):
    """Возвращает {'bid','ask','mid'} или None"""
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
    mid = (bid + ask)/2 if (bid and ask) else (bid or ask or 0)
    return {"bid": bid, "ask": ask, "mid": mid}

def capital_open(epic: str, direction: str, size: float, stop: float, limit: float):
    if not TRADE_ENABLED:
        log(f"ℹ️ Trade disabled. Skip open {epic} {direction} {size}")
        return True, {"dealReference": "dry-run"}
    url = f"{CAPITAL_BASE_URL}/api/v1/positions"
    payload = {
        "epic": epic,
        "direction": direction.upper(),   # "BUY" / "SELL"
        "size": round(size, 2),
        "orderType": "MARKET",
        "guaranteedStop": False,
        "stopLevel": stop,
        "limitLevel": limit
    }
    r = safe_req("POST", url, headers=cap_headers(), json=payload)
    if r and r.status_code == 401:
        if not capital_login(): 
            return False, {"error":"auth"}
        r = safe_req("POST", url, headers=cap_headers(), json=payload)
    if not r:
        return False, {"error":"no_response"}
    if r.status_code not in (200,201):
        try: j = r.json()
        except: j = {"text": r.text}
        return False, j
    try: j = r.json()
    except: j = {}
    return True, j

# ========= YF HELPERS =========
def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ["_".join([str(x) for x in tup if x]).strip() for tup in df.columns]
    return df

def _get_close(df: pd.DataFrame) -> pd.Series | None:
    """Возвращает 1D Series 'Close' при любых колонках"""
    if df is None or df.empty:
        return None
    df = _flatten_columns(df.copy())
    candidates = [c for c in df.columns if c.lower() == "close"]
    if not candidates:
        # На всякий — попробуем последние цены (последняя колонка)
        s = df.iloc[:, -1]
    else:
        s = df[candidates[0]]
    # привести к 1D float
    s = pd.Series(np.asarray(s).reshape(-1,), index=s.index).astype(float)
    return s

def yf_download_tolerant(ticker: str, period: str, interval: str) -> pd.DataFrame:
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False, timeout=15)
        # Иногда Yahoo возвращает Series; приведём к DataFrame
        if isinstance(df, pd.Series):
            df = df.to_frame()
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception as e:
        log(f"⚠️ Yahoo timeout/error for {ticker}: {e}")
        return pd.DataFrame()

# ========= INDICATORS (без внешних lib) =========
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=series.index).ewm(alpha=1/period, adjust=False).mean()
    roll_down = pd.Series(down, index=series.index).ewm(alpha=1/period, adjust=False).mean()
    rs = roll_up / (roll_down.replace(0, np.nan))
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)

def macd(series: pd.Series, fast=12, slow=26, signal=9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def bollinger(series: pd.Series, window=20, n_std=2):
    ma = series.rolling(window).mean()
    sd = series.rolling(window).std(ddof=0)
    upper = ma + n_std*sd
    lower = ma - n_std*sd
    return ma, upper, lower

# ========= STRATEGY =========
def build_signal(close: pd.Series):
    """Возвращает ('BUY'|'SELL'|'HOLD', dict метрик)"""
    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    r = rsi(close, 14)
    macd_line, macd_sig, macd_hist = macd(close)
    bb_ma, bb_up, bb_lo = bollinger(close, 20, 2)

    last = close.iloc[-1]
    e20, e50 = float(ema20.iloc[-1]), float(ema50.iloc[-1])
    r_last = float(r.iloc[-1])
    m_line, m_sig = float(macd_line.iloc[-1]), float(macd_sig.iloc[-1])

    # Простая логика: EMA20/50 пересечения + RSI фильтр
    if e20 > e50 and r_last > 55 and m_line > m_sig:
        decision = "BUY"
    elif e20 < e50 and r_last < 45 and m_line < m_sig:
        decision = "SELL"
    else:
        decision = "HOLD"

    meta = {
        "price": float(last),
        "ema20": e20, "ema50": e50,
        "rsi": r_last,
        "macd": m_line, "macd_sig": m_sig
    }
    return decision, meta

def compute_levels(direction: str, price: float):
    # SL/TP в абсолютных ценовых единицах (на mid)
    if direction == "BUY":
        sl = price * (1 - SL_PCT)
        tp = price * (1 + SL_PCT * TP_MULT)
    else:
        sl = price * (1 + SL_PCT)
        tp = price * (1 - SL_PCT * TP_MULT)
    return round(sl, 2), round(tp, 2)

def position_size(balance_usd: float, price: float, min_size: float):
    # Простейшая оценка: сколько контрактов тянем при заданном плече
    usd_for_pos = balance_usd * POSITION_FRACTION
    notional = usd_for_pos * LEVERAGE
    size = max(min_size, (notional / max(price, 1e-6)))
    # округлим до кратности min_size
    steps = max(1, int(round(size / min_size)))
    return round(steps * min_size, 2)

# ========= MAIN LOOP =========
async def main_loop():
    # Логинимся в Capital один раз в начале
    capital_login()

    while True:
        try:
            log("⏳ New cycle started")
            for name, meta in SYMBOLS.items():
                epic = meta["epic"]; yf_ticker = meta["yf"]; min_sz = meta["min_size"]

                log(f"🔎 Checking {name} ({epic}/{yf_ticker}) ...")

                # 1) Capital mid-price (если есть)
                price_info = capital_price(epic)
                cap_mid = price_info["mid"] if price_info else None
                if not cap_mid or cap_mid <= 0:
                    log(f"⚠️ {name}: no Capital price, fallback to Yahoo")

                # 2) Yahoo history (с таймаутом)
                df = yf_download_tolerant(yf_ticker, period=HISTORY_PERIOD, interval=HISTORY_INTERVAL)
                if df.empty:
                    log(f"⚠️ {name}: no Yahoo data — skip")
                    continue

                close = _get_close(df)
                if close is None or close.dropna().shape[0] < 60:
                    log(f"⚠️ {name}: insufficient 'Close' — skip")
                    continue

                # Work price
                work_price = float(cap_mid or close.iloc[-1])

                # 3) Сигнал
                signal, ind = build_signal(close)
                log(f"ℹ️ {name} => {signal} | P={work_price:.2f} | EMA20={ind['ema20']:.2f} EMA50={ind['ema50']:.2f} | RSI={ind['rsi']:.1f}")

                # 4) Торговля
                if signal == "HOLD":
                    continue

                # Баланс (грубая оценка через /session)
                # Если хочется точнее — сделай отдельный вызов аккаунта /accounts
                balance = 1000.0  # безопасный дефолт, если не получится получить
                try:
                    sess = safe_req("POST", f"{CAPITAL_BASE_URL}/api/v1/session", headers={"X-CAP-API-KEY": CAPITAL_API_KEY, "Accept":"application/json"},
                                    json={"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD})
                    if sess and sess.status_code == 200:
                        j = sess.json()
                        bal = j.get("accountInfo", {}).get("available", 0.0)
                        if bal: balance = float(bal)
                except: pass

                size = position_size(balance, work_price, min_sz)
                sl, tp = compute_levels(signal, work_price)

                ok, resp = capital_open(epic, signal, size, sl, tp)
                if ok:
                    ref = resp.get("dealReference", "?")
                    msg = f"✅ OPEN {name} {signal} size={size} @ {work_price:.2f} | SL={sl} TP={tp} ref={ref}"
                    log(msg); tg(msg)
                else:
                    log(f"❌ OPEN fail {name}: {resp}")
                    tg(f"❌ Не удалось открыть позицию {name}: {resp}")

            log("✅ Cycle complete")
        except Exception as e:
            log(f"🔥 MAIN LOOP error: {e}\n{traceback.format_exc()}")
        finally:
            await asyncio.sleep(CHECK_INTERVAL_SEC)

# ========= ENTRY =========
if __name__ == "__main__":
    log("🤖 TraderKing v2.1 starting (Render).")
    tg(f"🤖 TraderKing v2.1 запущен. Автоторговля: {'ВКЛ' if TRADE_ENABLED else 'ВЫКЛ'}. Интервал: {CHECK_INTERVAL_SEC//60}м.")
    asyncio.run(main_loop())
