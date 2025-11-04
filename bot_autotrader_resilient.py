import os, time, json, math, asyncio, traceback
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
import yfinance as yfimport os
import requests

def get_capital_tokens():
    """
    Авторизация на Capital.com и получение новых CST / X-SECURITY-TOKEN
    """
    base_url = "https://api-capital.backend-capital.com"
    account_type = os.getenv("CAPITAL_ACCOUNT_TYPE", "LIVE").lower()
    if account_type == "demo":
        base_url = "https://demo-api-capital.backend-capital.com"

    email = os.getenv("CAPITAL_EMAIL")
    password = os.getenv("CAPITAL_PASSWORD")

    if not email or not password:
        raise ValueError("❌ Не заданы CAPITAL_EMAIL и CAPITAL_PASSWORD в .env!")

    headers = {"Content-Type": "application/json"}
    payload = {"identifier": email, "password": password}

    try:
        response = requests.post(f"{base_url}/api/v1/session", headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        # Извлекаем токены
        cst = response.headers.get("CST")
        x_security_token = response.headers.get("X-SECURITY-TOKEN")

        if not cst or not x_security_token:
            raise ValueError("⚠️ Не удалось получить токены из ответа Capital.com")

        # Обновляем в переменных среды (если бот перезапускается)
        os.environ["CST"] = cst
        os.environ["X_SECURITY_TOKEN"] = x_security_token

        print(f"✅ CST и X-SECURITY-TOKEN обновлены: {account_type.upper()}")
        return cst, x_security_token

    except Exception as e:
        print(f"❌ Ошибка авторизации Capital: {e}")
        return None, None
# ========= ENV =========
CAPITAL_BASE_URL = os.environ.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")
CST            = os.environ.get("CST", "")
XST            = os.environ.get("X_SECURITY_TOKEN", "")
ACCOUNT_ID     = os.environ.get("CAPITAL_ACCOUNT_ID", "")  # можно оставить пустым — возьмём currentAccountId из /accounts

EPIC_GOLD      = os.environ.get("EPIC_GOLD", "")
EPIC_BRENT     = os.environ.get("EPIC_OIL_BRENT", "")
EPIC_GAS       = os.environ.get("EPIC_NATURAL_GAS", "")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))   # 1m-скальпинг
HISTORY_PERIOD     = os.environ.get("HISTORY_PERIOD", "5d")
HISTORY_INTERVAL   = os.environ.get("HISTORY_INTERVAL", "1m")

# риск/размер
RISK_SHARE     = float(os.environ.get("POSITION_FRACTION", "0.25"))   # 25% от баланса в номинал
LEVERAGE       = float(os.environ.get("LEVERAGE", "20"))

# ATR-параметры и множители TP/SL (агрессивнее = короче)
ATR_LEN        = int(os.environ.get("ATR_LEN", "14"))
SL_ATR_MULT    = float(os.environ.get("SL_ATR_MULT", "1.2"))
TP_ATR_MULT    = float(os.environ.get("TP_ATR_MULT", "1.8"))

# индикаторы
RSI_LEN        = 14
EMA_FAST       = 20
EMA_SLOW       = 50
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIG       = 9
BB_LEN         = 20
BB_STD         = 2
STO_LEN        = 14
STO_K          = 3
STO_D          = 3

# Yahoo тикеры (для фолбэка и истории)
YF = {
    "GOLD":      "GC=F",
    "OIL_BRENT": "BZ=F",
    "GAS":       "NG=F",
}

SYMBOLS = {
    "GOLD":      {"epic": EPIC_GOLD,  "yf": YF["GOLD"]},
    "OIL_BRENT": {"epic": EPIC_BRENT, "yf": YF["OIL_BRENT"]},
    "NATURAL_GAS":{"epic": EPIC_GAS,  "yf": YF["GAS"]},
}

session = requests.Session()
session.headers.update({
    "Content-Type": "application/json",
    "Accept": "application/json",
})

# ========= Утилиты =========
def log(s): print(s, flush=True)

def tg(msg):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "disable_web_page_preview": True},
            timeout=10
        )
    except Exception:
        pass

def cap_headers():
    return {
        "CST": CST,
        "X-SECURITY-TOKEN": XST,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def capital_get(path):
    try:
        r = session.get(CAPITAL_BASE_URL + path, headers=cap_headers(), timeout=12)
        return r
    except Exception as e:
        log(f"[capital_get] exception: {e}")
        return None

def capital_post(path, payload):
    try:
        r = session.post(CAPITAL_BASE_URL + path, headers=cap_headers(), data=json.dumps(payload), timeout=12)
        return r
    except Exception as e:
        log(f"[capital_post] exception: {e}")
        return None

def is_token_error(resp_json):
    if not isinstance(resp_json, dict): return False
    code = str(resp_json.get("errorCode", "")).lower()
    return any(x in code for x in [
        "invalid.session.token", "null.client.token", "auth", "unauthorised", "unauthorized"
    ])

def capital_login_test():
    r = capital_get("/api/v1/accounts")
    if not r:
        return False, "no_response"
    if r.status_code == 200:
        try:
            data = r.json()
            # выберем текущий аккаунт
            acc = data.get("currentAccountId") or ""
            if not acc and data.get("accounts"):
                acc = str(data["accounts"][0].get("accountId"))
            return True, acc
        except Exception:
            return False, "bad_json"
    else:
        try:
            j = r.json()
        except Exception:
            j = {}
        return False, j.get("errorCode") or f"HTTP{r.status_code}"

def capital_price(epic: str):
    """bid/offer/mid из Capital; None если нет"""
    if not epic:
        return None
    r = capital_get(f"/api/v1/prices/{epic}")
    if not r:
        return None
    if r.status_code == 200:
        try:
            prices = r.json().get("prices") or []
            if not prices: return None
            p = prices[-1]
            bid = float(p.get("bid", 0) or 0)
            ask = float(p.get("offer", 0) or 0)
            if bid <= 0 and ask <= 0:
                return None
            mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else (bid if bid > 0 else ask)
            return {"bid": bid, "ask": ask, "mid": mid}
        except Exception:
            return None
    else:
        return None

def capital_open_market(epic: str, direction: str, size: float, stop_level: float, limit_level: float, force_open=True):
    """
    Открытие MARKET позиции с TP/SL.
    Для Capital.com v1 обычно хватает полей ниже. Если у тебя была «рабочая» версия — эта схема совместима.
    """
    payload = {
        "epic": epic,
        "direction": direction.upper(),  # "BUY" | "SELL"
        "size": float(size),
        "orderType": "MARKET",
        "guaranteedStop": False,
        "forceOpen": bool(force_open),
        "stopLevel": float(stop_level),
        "limitLevel": float(limit_level),
    }
    r = capital_post("/api/v1/positions", payload)
    if not r:
        return False, {"error": "no_response"}
    try:
        j = r.json()
    except Exception:
        j = {}
    if r.status_code in (200, 201):
        return True, j
    # обработка токена отдельно, чтобы видеть это явно в логах
    if is_token_error(j):
        return False, {"error": "token", **j}
    return False, j

def yahoo_history(yf_ticker: str, period=HISTORY_PERIOD, interval=HISTORY_INTERVAL):
    try:
        df = yf.download(yf_ticker, period=period, interval=interval, progress=False, auto_adjust=True)
        if isinstance(df, pd.DataFrame) and not df.empty and "Close" in df.columns:
            # убедимся в монотонности индекса
            df = df.sort_index()
            return df
    except Exception as e:
        log(f"[yahoo_history] {yf_ticker} ex: {e}")
    return None

# ========= Индикаторы =========
def ema(series: pd.Series, length: int):
    return series.ewm(span=length, adjust=False).mean()

def rsi(series: pd.Series, length=14):
    delta = series.diff()
    up = (delta.clip(lower=0)).ewm(alpha=1/length, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/length, adjust=False).mean()
    rs = up / (down.replace(0, np.nan))
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)

def macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def bollinger(series: pd.Series, length=20, std=2):
    ma = series.rolling(length).mean()
    sd = series.rolling(length).std()
    upper = ma + std * sd
    lower = ma - std * sd
    return upper, ma, lower

def atr(high: pd.Series, low: pd.Series, close: pd.Series, length=14):
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, n=14, k=3, d=3):
    lowest = low.rolling(n).min()
    highest = high.rolling(n).max()
    k_line = 100 * ((close - lowest) / (highest - lowest)).clip(0, 1)
    k_smooth = k_line.rolling(k).mean()
    d_line = k_smooth.rolling(d).mean()
    return k_smooth, d_line

# ========= Сигнал (агрессивно на 1m) =========
def build_signal(df: pd.DataFrame):
    """Возвращает BUY/SELL/HOLD + значения индикаторов и ATR."""
    close = df["Close"].astype(float)
    high  = df["High"].astype(float)
    low   = df["Low"].astype(float)

    ema20 = ema(close, EMA_FAST)
    ema50 = ema(close, EMA_SLOW)
    rsi14 = rsi(close, RSI_LEN)
    macd_l, macd_s, macd_h = macd(close, MACD_FAST, MACD_SLOW, MACD_SIG)
    bb_u, bb_m, bb_l = bollinger(close, BB_LEN, BB_STD)
    atrv = atr(high, low, close, ATR_LEN)
    k, d = stochastic(high, low, close, STO_LEN, STO_K, STO_D)

    # берём последние два значения, всегда как float
    e20_1, e50_1 = float(ema20.iloc[-1]), float(ema50.iloc[-1])
    e20_2, e50_2 = float(ema20.iloc[-2]), float(ema50.iloc[-2])
    r1 = float(rsi14.iloc[-1])
    hist_1, hist_2 = float(macd_h.iloc[-1]), float(macd_h.iloc[-2])
    k1, d1 = float(k.iloc[-1]), float(d.iloc[-1])

    # Кроссы EMAs
    bull_cross = (e20_2 <= e50_2) and (e20_1 > e50_1)
    bear_cross = (e20_2 >= e50_2) and (e20_1 < e50_1)

    buy = (bull_cross or e20_1 > e50_1) and (r1 > 52) and (hist_1 > hist_2) and (k1 > d1) and (k1 < 85)
    sell = (bear_cross or e20_1 < e50_1) and (r1 < 48) and (hist_1 < hist_2) and (k1 < d1) and (k1 > 15)

    if buy and not sell:
        sig = "BUY"
    elif sell and not buy:
        sig = "SELL"
    else:
        sig = "HOLD"

    return sig, float(close.iloc[-1]), float(atrv.iloc[-1] if not np.isnan(atrv.iloc[-1]) else max(0.003*float(close.iloc[-1]), 0.01))

# ========= Размер/TP/SL =========
def compute_position_params(balance: float, atr_value: float, last_price: float, direction: str):
    # номинал от баланса
    notion = max(1.0, balance * RISK_SHARE)
    size = max(1, int(round(notion / max(last_price, 1e-6))))
    size = min(size, 50)

    sl_dist = SL_ATR_MULT * atr_value
    tp_dist = TP_ATR_MULT * atr_value

    if direction == "BUY":
        stop_level = last_price - sl_dist
        limit_level = last_price + tp_dist
    else:
        stop_level = last_price + sl_dist
        limit_level = last_price - tp_dist

    return size, stop_level, limit_level

def capital_balance():
    r = capital_get("/api/v1/accounts")
    if not r or r.status_code != 200:
        return None
    try:
        j = r.json()
        acc = None
        # пытаемся взять currentAccountId
        cur_id = j.get("currentAccountId")
        for a in j.get("accounts", []):
            if str(a.get("accountId")) == str(cur_id):
                acc = a
                break
        if not acc and j.get("accounts"):
            acc = j["accounts"][0]
        if not acc:
            return None
        bal = float(acc.get("balance", {}).get("balance", acc.get("balance", 0)) or 0)
        avail = float(acc.get("balance", {}).get("available", acc.get("available", 0)) or 0)
        return {"balance": bal, "available": avail}
    except Exception:
        return None

# ========= Процесс символа =========
def process_symbol(name: str, meta: dict):
    epic = meta.get("epic", "")
    yf_ticker = meta.get("yf", "")

    # 1) история — всегда из Yahoo (надёжнее и быстрее)
    df = yahoo_history(yf_ticker, period=HISTORY_PERIOD, interval=HISTORY_INTERVAL)
    if df is None:
        tg(f"⚠️ {name}: нет данных истории из Yahoo.")
        log(f"[{name}] no yahoo history")
        return

    signal, last_price, atr_val = build_signal(df)

    # 2) берём актуальную цену: сперва из Capital, иначе Yahoo
    price_cap = capital_price(epic) if epic else None
    price = price_cap["mid"] if price_cap else float(df["Close"].iloc[-1])

    log(f"[{name}] price={price:.5f} | signal={signal}")

    if signal == "HOLD":
        return

    # 3) баланс
    bal = capital_balance()
    if not bal:
        tg(f"⚠️ {name}: не удалось получить баланс.")
        return

    size, sl, tp = compute_position_params(bal["available"], atr_val, price, signal)

    ok, resp = capital_open_market(epic, signal, size, sl, tp)
    if ok:
        dealref = resp.get("dealReference") or resp.get("dealReferenceId") or "?"
        tg(f"✅ {name}: {signal} открыта @ {price:.5f} | size={size} | SL={sl:.5f} | TP={tp:.5f} | deal={dealref}")
        log(f"OPEN OK [{name}] {signal} size={size} sl={sl} tp={tp} -> {dealref}")
    else:
        if resp.get("error") == "token" or is_token_error(resp):
            tg("❗️Ошибка токена Capital (CST/X-SECURITY). Обнови токены в Render и перезапусти.")
        else:
            tg(f"❌ {name}: ошибка открытия сделки\n{json.dumps(resp, ensure_ascii=False)}")
        log(f"OPEN FAIL [{name}] {resp}")

# ========= MAIN LOOP =========
async def main_loop():
    ok, info = capital_login_test()
    if ok:
        tg("🤖 TraderKing запущен (Render). Авторизация в Capital OK.")
        log("Capital login OK")
    else:
        tg(f"❗️ Capital login failed: {info}")
        log(f"Capital login failed: {info}")

    while True:
        try:
            for name in ["GOLD", "OIL_BRENT", "NATURAL_GAS"]:
                meta = SYMBOLS.get(name, {})
                if not meta.get("yf"):
                    continue
                process_symbol(name, meta)
            log("=== CYCLE DONE ===")
        except Exception as e:
            tg(f"🔥 Ошибка цикла: {e}")
            traceback.print_exc()

        await asyncio.sleep(CHECK_INTERVAL_SEC)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
