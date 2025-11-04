import os, time, json, math, asyncio, traceback
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
import yfinance as yf

# ========= ENV =========
CAPITAL_BASE_URL = os.environ.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")
CAPITAL_USERNAME  = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_PASSWORD  = os.environ.get("CAPITAL_PASSWORD", "")
CAPITAL_API_KEY   = os.environ.get("CAPITAL_API_KEY", "")

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")

EPIC_GOLD         = os.environ.get("EPIC_GOLD", "")
EPIC_OIL_BRENT    = os.environ.get("EPIC_OIL_BRENT", "")
EPIC_NATURAL_GAS  = os.environ.get("EPIC_NATURAL_GAS", "")

CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))
HISTORY_PERIOD     = os.environ.get("HISTORY_PERIOD", "3d")
HISTORY_INTERVAL   = os.environ.get("HISTORY_INTERVAL", "1m")

RISK_SHARE      = float(os.environ.get("RISK_SHARE", "0.25"))
LEVERAGE        = float(os.environ.get("LEVERAGE", "20"))
SL_ATR_MULT     = float(os.environ.get("SL_ATR_MULT", "1.0"))
TP_ATR_MULT     = float(os.environ.get("TP_ATR_MULT", "1.2"))
MAX_SIZE        = int(os.environ.get("MAX_SIZE", "50"))
MIN_SIZE        = int(os.environ.get("MIN_SIZE", "1"))

# маппинг Yahoo тикеров для сигналов
YF_TICKERS = {
    "GOLD": "GC=F",          # COMEX Gold Futures
    "OIL_BRENT": "BZ=F",     # Brent
    "GAS": "NG=F",           # Natural Gas
}

# EPIC маппинг для сделок
EPIC = {
    "GOLD": EPIC_GOLD,
    "OIL_BRENT": EPIC_OIL_BRENT,
    "GAS": EPIC_NATURAL_GAS,
}

# ======= utils =======
def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg: str):
    print(f"[{now_utc()}] {msg}", flush=True)

def tg(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    except Exception as e:
        print(f"TG error: {e}", flush=True)

def safe_req(method: str, url: str, retries=3, **kwargs):
    for i in range(retries):
        try:
            r = requests.request(method, url, timeout=20, **kwargs)
            return r
        except Exception as e:
            if i == retries - 1:
                return None
            time.sleep(1.5)

# ===== Capital.com auth/session =====
_session = requests.Session()
CST = None
XST = None

def cap_headers():
    h = {
        "Accept": "application/json; charset=UTF-8",
        "Content-Type": "application/json; charset=UTF-8",
        "X-IG-API-KEY": CAPITAL_API_KEY
    }
    if CST: h["CST"] = CST
    if XST: h["X-SECURITY-TOKEN"] = XST
    return h

def capital_login() -> bool:
    global CST, XST
    # если уже есть токены — проверим аккаунт
    if CST and XST:
        r = safe_req("GET", f"{CAPITAL_BASE_URL}/api/v1/accounts", headers=cap_headers())
        if r and r.status_code == 200:
            return True
        # иначе сбрасываем и логинимся заново
        CST = XST = None

    # логин
    payload = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_PASSWORD}
    r = safe_req("POST", f"{CAPITAL_BASE_URL}/api/v1/session", headers=cap_headers(), data=json.dumps(payload))
    if not r:
        log("Capital login: no response")
        return False
    if r.status_code not in (200, 201):
        log(f"Capital login failed: {r.text}")
        return False

    # токены берутся из заголовков ответа
    CST = r.headers.get("CST")
    XST = r.headers.get("X-SECURITY-TOKEN")
    if not CST or not XST:
        log("Capital login: tokens missing in headers")
        return False

    # быстрая проверка
    test = safe_req("GET", f"{CAPITAL_BASE_URL}/api/v1/accounts", headers=cap_headers())
    if test and test.status_code == 200:
        acc = test.json()
        log(f"Login test status: 200 (balance {acc.get('accountInfo', {}).get('balance')})")
        return True
    log(f"Login test failed: {None if not test else test.text}")
    return False

# ====== Prices & orders ======
def cap_price(epic: str):
    """
    Получить последнюю котировку bid/offer/mid по EPIC из Capital.
    Если 401 — перелогин.
    """
    url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}?max=1"
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
    mid = (bid+ask)/2 if (bid and ask) else (bid or ask)
    return {"bid": bid, "ask": ask, "mid": mid}

def cap_open_position(epic: str, direction: str, size: float, stop_level: float, limit_level: float):
    """
    Открыть позицию с TP/SL. Используем v1.
    """
    url = f"{CAPITAL_BASE_URL}/api/v1/positions"
    body = {
        "direction": direction.upper(),           # "BUY" / "SELL"
        "epic": epic,
        "size": size,
        "guaranteedStop": False,
        "forceOpen": True,
        "orderType": "MARKET",
        "limitLevel": round(limit_level, 3),
        "stopLevel": round(stop_level, 3)
    }
    r = safe_req("POST", url, headers=cap_headers(), data=json.dumps(body))
    if r and r.status_code == 401:
        if not capital_login():
            return False, "auth_failed"
        r = safe_req("POST", url, headers=cap_headers(), data=json.dumps(body))

    if not r:
        return False, "no_response"
    if r.status_code not in (200, 201):
        return False, r.text
    return True, r.json()

# ===== TA helpers =====
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def rsi(series: pd.Series, period=14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -1*delta.clip(upper=0.0)
    roll_up = up.ewm(span=period, adjust=False).mean()
    roll_down = down.ewm(span=period, adjust=False).mean()
    rs = roll_up / (roll_down + 1e-12)
    return 100 - (100 / (1 + rs))

def macd(series: pd.Series, fast=12, slow=26, signal=9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def bollinger(series: pd.Series, period=20, mult=2.0):
    ma = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)
    upper = ma + mult*std
    lower = ma - mult*std
    return ma, upper, lower

def atr(df: pd.DataFrame, period=14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    prev_c = c.shift(1)
    tr = pd.concat([
        (h - l),
        (h - prev_c).abs(),
        (l - prev_c).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# ===== Strategy (Aggressive mode) =====
def get_signal(df: pd.DataFrame) -> str:
    """
    Возвращает 'BUY' / 'SELL' / 'HOLD' (агрессивная версия)
    """
    df = df.copy().dropna()
    if len(df) < 60:
        return "HOLD"

    close = df["Close"]
    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    macd_line, macd_sig, macd_hist = macd(close, 12, 26, 9)
    rsi14 = rsi(close, 14)

    # последние значения (как числа)
    e20_1 = float(ema20.iloc[-1])
    e50_1 = float(ema50.iloc[-1])
    e20_2 = float(ema20.iloc[-2])
    e50_2 = float(ema50.iloc[-2])
    hist_1 = float(macd_hist.iloc[-1])
    hist_2 = float(macd_hist.iloc[-2])
    rsi_1 = float(rsi14.iloc[-1])
    price_1 = float(close.iloc[-1])
    price_2 = float(close.iloc[-2])

    # кроссы
    bull_cross = (e20_2 <= e50_2) and (e20_1 > e50_1)
    bear_cross = (e20_2 >= e50_2) and (e20_1 < e50_1)

    # направление
    up_slope = e20_1 > e20_2
    down_slope = e20_1 < e20_2

    # Упрощённая логика: если EMA20 > EMA50 и MACD растёт — BUY
    if (bull_cross or (e20_1 > e50_1 and hist_1 > hist_2)) and (rsi_1 < 75):
        return "BUY"

    # Если EMA20 < EMA50 и MACD падает — SELL
    if (bear_cross or (e20_1 < e50_1 and hist_1 < hist_2)) and (rsi_1 > 25):
        return "SELL"

    return "HOLD"


# ===== Position sizing & TP/SL (Aggressive) =====
def compute_position_params(balance: float, atr_value: float, last_price: float, direction: str):
    """
    Агрессивная версия: 35-40% баланса, короткий TP, чуть шире SL, плюс трейлинг-стоп.
    """
    if atr_value is None or math.isnan(atr_value) or atr_value <= 0:
        atr_value = last_price * 0.004  # 0.4%

    # риск 35% от баланса
    notional = max(1.0, balance * 0.35)
    size = int(max(MIN_SIZE, min(MAX_SIZE, round(notional / max(1e-6, last_price)))))

    sl_dist = 1.3 * atr_value   # шире стоп
    tp_dist = 0.8 * atr_value   # ближе тейк

    if direction == "BUY":
        stop_level = last_price - sl_dist
        limit_level = last_price + tp_dist
    else:
        stop_level = last_price + sl_dist
        limit_level = last_price - tp_dist

    # трейлинг-стоп (опционально)
    trailing_stop = 0.6 * atr_value

    return size, stop_level, limit_level

    # последние значения
    e20_1, e50_1 = ema20.iloc[-1], ema50.iloc[-1]
    e20_2, e50_2 = ema20.iloc[-2], ema50.iloc[-2]
    hist_1, hist_2 = macd_hist.iloc[-1], macd_hist.iloc[-2]
    rsi_1 = rsi14.iloc[-1]
    price_1 = close.iloc[-1]
    bb_u1, bb_l1 = bb_up.iloc[-1], bb_low.iloc[-1]

    # кроссы
    bull_cross = (e20_2 <= e50_2) and (e20_1 > e50_1)
    bear_cross = (e20_2 >= e50_2) and (e20_1 < e50_1)

    # ранний вход: возврат внутрь полос после касания
    reentry_long  = (price_1 > bb_l1) and (close.iloc[-2] < bb_low.iloc[-2])
    reentry_short = (price_1 < bb_u1) and (close.iloc[-2] > bb_up.iloc[-2])

    # фильтры
    up_slope   = e20_1 > e20_2 and e50_1 >= e50_2
    down_slope = e20_1 < e20_2 and e50_1 <= e50_2

    # — BUY:
    if (bull_cross or reentry_long) and up_slope and (hist_1 > 0) and (rsi_1 < 68):
        return "BUY"

    # — SELL:
    if (bear_cross or reentry_short) and down_slope and (hist_1 < 0) and (rsi_1 > 32):
        return "SELL"

    return "HOLD"

# ===== Position sizing & TP/SL =====
def compute_position_params(balance: float, atr_value: float, last_price: float, direction: str):
    """
    size: по риску ~25% баланса (env RISK_SHARE), ограничено [MIN_SIZE..MAX_SIZE].
    SL/TP: отступы по ATR.
    """
    if atr_value is None or math.isnan(atr_value) or atr_value <= 0:
        atr_value = last_price * 0.003  # fallback ≈0.3%

    notional = max(1.0, balance * RISK_SHARE)
    size = int(max(MIN_SIZE, min(MAX_SIZE, round(notional / max(1e-6, last_price)))))

    sl_dist = SL_ATR_MULT * atr_value
    tp_dist = TP_ATR_MULT * atr_value

    if direction == "BUY":
        stop_level  = last_price - sl_dist
        limit_level = last_price + tp_dist
    else:
        stop_level  = last_price + sl_dist
        limit_level = last_price - tp_dist

    return size, stop_level, limit_level

# ===== Yahoo history loader =====
def load_history(symbol_name: str) -> pd.DataFrame | None:
    yf_ticker = YF_TICKERS.get(symbol_name)
    if not yf_ticker:
        return None
    df = yf.download(yf_ticker, period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False, auto_adjust=False)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    df = df.dropna()
    # для однозначности — обычные столбцы
    df = df[['Open','High','Low','Close','Volume']].copy()
    return df

# ===== Main cycle =====
SYMBOLS = ["GOLD", "OIL_BRENT"]  # добавь "GAS" если нужно

async def process_symbol(symbol_name: str):
    try:
        df = load_history(symbol_name)
        if df is None or df.empty:
            log(f"{symbol_name}: no history")
            return

        signal = get_signal(df)
        last = float(df["Close"].iloc[-1])

        # ATR для TP/SL
        atr14 = float(atr(df, 14).iloc[-1])

        # цена с Capital
        epic = EPIC.get(symbol_name) or ""
        if not epic:
            log(f"{symbol_name}: EPIC is empty — skip")
            return

        p = cap_price(epic)
        if not p:
            log(f"{symbol_name}: no price from Capital")
            return

        mid = p["mid"] or last

        # баланс (упростим: берём доступный с /accounts)
        acc = safe_req("GET", f"{CAPITAL_BASE_URL}/api/v1/accounts", headers=cap_headers())
        balance = 100.0
        if acc and acc.status_code == 200:
            j = acc.json()
            balance = float(j.get("accountInfo", {}).get("available", 100.0) or 100.0)

        # решение
        if signal in ("BUY","SELL"):
            size, sl, tp = compute_position_params(balance, atr14, mid, signal)
            ok, resp = cap_open_position(epic, signal, size, sl, tp)
            if ok:
                msg = f"✅ {symbol_name}: {signal} открыта @ {mid:.3f} (size {size}) | SL={sl:.3f} TP={tp:.3f}"
                log(msg); tg(msg)
            else:
                msg = f"❌ {symbol_name}: ошибка открытия | {resp}"
                log(msg); tg(msg)
        else:
            log(f"{symbol_name}: signal=HOLD")
    except Exception as e:
        log(f"🔥 {symbol_name} loop error: {e}\n{traceback.format_exc()}")

async def main_loop():
    log(f"🤖 TraderKing v5 запущен. 24/7. Интервал: {CHECK_INTERVAL_SEC}s. Торговля: ВКЛ")
    tg("TraderKing запущен (1m). Авто TP/SL включены.")
    # первичный логин
    if not capital_login():
        log("Capital login failed at start"); tg("❌ Capital login failed на старте")
    while True:
        for sym in SYMBOLS:
            if not (CST and XST):
                capital_login()
            await process_symbol(sym)
        log("=== CYCLE DONE ===")
        await asyncio.sleep(CHECK_INTERVAL_SEC)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
