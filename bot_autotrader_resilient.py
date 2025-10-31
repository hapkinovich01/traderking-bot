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

CHECK_INTERVAL_SEC  = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))         # цикл проверки, сек
HISTORY_PERIOD      = os.environ.get("HISTORY_PERIOD", "3mo")
HISTORY_INTERVAL    = os.environ.get("HISTORY_INTERVAL", "1h")

LEVERAGE            = float(os.environ.get("LEVERAGE", "20"))
POSITION_FRACTION   = float(os.environ.get("POSITION_FRACTION", "0.25"))      # доля доступного
SL_PCT              = float(os.environ.get("SL_PCT", "0.006"))                 # 0.6% в distance
TP_MULT             = float(os.environ.get("TP_MULT", "1.6"))                  # TP = SL * мультипликатор
TRADE_ENABLED       = os.environ.get("TRADE_ENABLED", "true").lower() == "true"

# EPIC и тикеры Yahoo
SYMBOLS = {
    "GOLD": {
        "epic": os.environ.get("EPIC_GOLD", "GOLD"),       # подставь свой точный EPIC
        "yahoo": "GC=F",
        "min_size": 0.1
    },
    "OIL_BRENT": {
        "epic": os.environ.get("EPIC_BRENT", "OIL_BRENT"), # подставь свой точный EPIC
        "yahoo": "BZ=F",
        "min_size": 1.0
    },
    "GAS": {
        "epic": os.environ.get("EPIC_GAS", "NATGAS"),      # подставь свой точный EPIC
        "yahoo": "NG=F",
        "min_size": 1.0
    },
}

TOKENS = {"CST":"", "X-SECURITY-TOKEN":""}

# ========= УТИЛЫ =========
def ts() -> str:
    return datetime.now(timezone.utc).strftime("[%Y-%m-%d %H:%M:%S UTC]")

def log(msg: str):
    print(f"{ts()} {msg}", flush=True)

def tg(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    except Exception as e:
        log(f"⚠️ Telegram error: {e}")

def cap_headers():
    return {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "CST": TOKENS.get("CST",""),
        "X-SECURITY-TOKEN": TOKENS.get("X-SECURITY-TOKEN",""),
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

def safe_req(method, url, retries=2, **kwargs):
    for i in range(retries+1):
        try:
            r = requests.request(method, url, timeout=25, **kwargs)
            return r
        except Exception as e:
            if i==retries:
                log(f"❌ HTTP fail {method} {url}: {e}")
                return None
            time.sleep(1.2)
    return None

# ========= CAPITAL AUTH / PRICE / ORDERS =========
def capital_login() -> bool:
    """Логин; сохраняем CST и X-SECURITY-TOKEN."""
    url = f"{CAPITAL_BASE_URL}/api/v1/session"
    payload = {
        "identifier": CAPITAL_USERNAME,
        "password":   CAPITAL_API_PASSWORD
    }
    headers = {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    r = safe_req("POST", url, json=payload, headers=headers)
    if not r:
        log("🔥 Capital login exception: network")
        return False
    if r.status_code != 200:
        log(f"🔥 Capital login failed: {r.text}")
        return False
    cs = r.headers.get("CST","")
    xt = r.headers.get("X-SECURITY-TOKEN","")
    if not cs or not xt:
        log(f"🔥 Capital login: tokens missing, headers={dict(r.headers)}")
        return False
    TOKENS["CST"] = cs
    TOKENS["X-SECURITY-TOKEN"] = xt
    log("✅ Capital login OK")
    tg("✅ TraderKing авторизовался в Capital")
    return True

def capital_price(epic: str):
    """Последняя цена (mid) из Capital. Возвращает float или None."""
    url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
    r = safe_req("GET", url, headers=cap_headers())
    if r and r.status_code == 401:
        # истёкла сессия
        if not capital_login():
            return None
        r = safe_req("GET", url, headers=cap_headers())
    if not r or r.status_code != 200:
        return None
    try:
        arr = r.json().get("prices") or []
        if not arr:
            return None
        p = arr[-1]
        bid = float(p.get("bid",0) or 0)
        ask = float(p.get("offer",0) or 0)
        if bid and ask:
            return (bid+ask)/2.0
        return float(p.get("midPrice",0) or 0)
    except Exception:
        return None

def account_available_usd() -> float:
    """Сколько доступно средств. Нужен для размера позиции."""
    url = f"{CAPITAL_BASE_URL}/api/v1/accounts"
    r = safe_req("GET", url, headers=cap_headers())
    if r and r.status_code == 401:
        if not capital_login():
            return 0.0
        r = safe_req("GET", url, headers=cap_headers())
    if not r or r.status_code != 200:
        return 0.0
    try:
        data = r.json()
        # формат может отличаться; берём первое поле available
        if isinstance(data, dict) and "accountInfo" in data:
            return float((data["accountInfo"].get("available",0) or 0))
        if isinstance(data, dict) and "accounts" in data:
            # массив аккаунтов
            accs = data["accounts"]
            if accs:
                bal = accs[0].get("balance",{})
                return float((bal.get("available",0) or 0))
        return 0.0
    except Exception:
        return 0.0

def round_size(symbol: str, raw_size: float) -> float:
    min_sz = SYMBOLS[symbol].get("min_size", 1.0)
    # Округлим до шага min_size
    steps = max(1, int(round(raw_size / min_sz)))
    return max(min_sz, steps * min_sz)

def open_market_order(symbol: str, direction: str, price: float) -> bool:
    """Открыть сделку (с TP/SL через distance). Если брокер не примет — повтор без TP/SL."""
    epic = SYMBOLS[symbol]["epic"]
    avail = account_available_usd()
    if avail <= 0:
        log(f"⚠️ {symbol}: нет доступных средств (available={avail})")
        tg(f"⚠️ {symbol}: нет доступных средств")
        return False

    # простой риск-менеджмент: размер из доли баланса и рычага
    # здесь "size" — контрактный; логика подбирается под ваш инструмент
    raw_size = (avail * POSITION_FRACTION * LEVERAGE) / max(price, 1e-9)
    size = round_size(symbol, raw_size)

    sl_dist  = round(price * SL_PCT, 2)
    tp_dist  = round(price * SL_PCT * TP_MULT, 2)

    base_payload = {
        "epic": epic,
        "direction": direction,        # "BUY" / "SELL"
        "size": size,
        "orderType": "MARKET",
        "timeInForce": "FILL_OR_KILL",
        "guaranteedStop": False,
        "forceOpen": True,
        "currencyCode": "USD",
    }

    # Попытка №1: c TP/SL distance
    p1 = dict(base_payload)
    p1["stopLossDistance"]   = sl_dist
    p1["takeProfitDistance"] = tp_dist

    url = f"{CAPITAL_BASE_URL}/api/v1/positions/otc"
    r = safe_req("POST", url, json=p1, headers=cap_headers())
    if r and r.status_code == 401:
        if not capital_login():
            return False
        r = safe_req("POST", url, json=p1, headers=cap_headers())

    if r and r.status_code in (200, 201):
        try:
            ref = r.json().get("dealReference","")
        except Exception:
            ref = ""
        log(f"✅ OPEN OK: {symbol} {direction}; size={size}; ref={ref}")
        tg(f"✅ {symbol}: {direction} открыта @ {price:.3f} | size={size}")
        return True

    # Если брокер не принял distance — повтор без TP/SL
    if not r or r.status_code >= 400:
        log(f"⚠️ OPEN with TP/SL failed ({symbol}): {r.text if r else 'no response'}; retry w/o TP/SL")
        r2 = safe_req("POST", url, json=base_payload, headers=cap_headers())
        if r2 and r2.status_code == 401:
            if not capital_login():
                return False
            r2 = safe_req("POST", url, json=base_payload, headers=cap_headers())

        if r2 and r2.status_code in (200,201):
            log(f"✅ OPEN OK (no TP/SL): {symbol} {direction}; size={size}")
            tg(f"✅ {symbol}: {direction} открыта @ {price:.3f} (без TP/SL)")
            return True
        else:
            log(f"❌ OPEN FAIL ({symbol}): {r2.text if r2 else 'no response'}")
            tg(f"❌ {symbol}: ошибка открытия сделки\n{r2.text if r2 else 'no response'}")
            return False

# ========= DATA / INDICATORS / SIGNALS =========
def clean_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Привести данные к виду с одним столбцом Close(float) без мультииндекса."""
    if df is None or df.empty:
        return pd.DataFrame()
    # Сбрасываем multiindex колонок, если есть
    if isinstance(df.columns, pd.MultiIndex):
        # берём Close из первого уровня
        if ("Close" in df.columns.get_level_values(0)):
            df = df["Close"].copy()
        else:
            # берём любой последний столбец
            df = df.droplevel(0, axis=1)
    # Если это Series -> в DataFrame
    if isinstance(df, pd.Series):
        df = df.to_frame(name="Close")
    # Если в наборе есть столбец Close
    if "Close" in df.columns:
        df = df[["Close"]].copy()
    else:
        # иногда yfinance называет 'Adj Close'
        if "Adj Close" in df.columns:
            df = df[["Adj Close"]].rename(columns={"Adj Close":"Close"})
        elif len(df.columns)==1:
            df = df.rename(columns={df.columns[0]:"Close"})
        else:
            # нет понятного Close
            return pd.DataFrame()
    # приведение типа
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df = df.dropna().copy()
    return df

def get_history_from_yahoo(yf_ticker: str) -> pd.DataFrame:
    try:
        df = yf.download(yf_ticker, period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False)
        return clean_ohlc(df)
    except Exception:
        return pd.DataFrame()

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """EMA20/EMA50 + RSI14; NaN правильно обрабатываются."""
    if df is None or df.empty:
        return pd.DataFrame()
    s = df["Close"].astype(float)
    df["ema20"] = s.ewm(span=20, adjust=False).mean()
    df["ema50"] = s.ewm(span=50, adjust=False).mean()
    # RSI
    delta = s.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=s.index).rolling(14, min_periods=14).mean()
    roll_down = pd.Series(down, index=s.index).rolling(14, min_periods=14).mean()
    rs = roll_up / (roll_down + 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df

def get_signal(df: pd.DataFrame) -> str:
    """BUY / SELL / HOLD по кроссоверу EMA и фильтру RSI."""
    if df is None or df.empty:
        return "HOLD"
    df = df.dropna().copy()
    if len(df) < 60:
        return "HOLD"
    last = df.iloc[-1]
    prev = df.iloc[-2]
    ema20, ema50, rsi = float(last["ema20"]), float(last["ema50"]), float(last["rsi"])
    p_ema20, p_ema50 = float(prev["ema20"]), float(prev["ema50"])

    # кроссовер
    crossed_up   = (p_ema20 <= p_ema50) and (ema20 > ema50)
    crossed_down = (p_ema20 >= p_ema50) and (ema20 < ema50)

    if crossed_up and rsi < 65:
        return "BUY"
    if crossed_down and rsi > 35:
        return "SELL"
    return "HOLD"

# ========= ОСНОВНОЙ ЦИКЛ =========
async def process_symbol(name: str):
    meta = SYMBOLS[name]
    epic = meta["epic"]
    yf_ticker = meta["yahoo"]

    # 1) Цена с Capital
    price_cap = capital_price(epic)
    if price_cap:
        price = price_cap
        price_src = "Capital"
    else:
        # 2) Фолбэк на Yahoo (последняя close)
        dfp = get_history_from_yahoo(yf_ticker)
        if dfp.empty:
            log(f"⚠️ {name}: нет цены для открытия сделки")
            tg(f"⚠️ {name}: нет цены для открытия сделки")
            return
        price = float(dfp["Close"].iloc[-1])
        price_src = "Yahoo"

    # История для сигналов (только Yahoo — стабильнее)
    df_raw = get_history_from_yahoo(yf_ticker)
    if df_raw.empty:
        log(f"⚠️ {name}: история не получена (Yahoo)")
        return
    df_ind = compute_indicators(df_raw)
    sig = get_signal(df_ind)

    log(f"🔎 {name}: {price_src} price={price:.4f} | signal={sig}")
    if sig == "HOLD":
        return

    if not TRADE_ENABLED:
        tg(f"ℹ️ {name}: сигнал {sig}, но автоторговля выключена")
        return

    ok = open_market_order(name, "BUY" if sig=="BUY" else "SELL", price)
    if not ok:
        return

async def main_loop():
    log(f"🤖 TraderKing v5 запущен. 24/7. Интервал: {CHECK_INTERVAL_SEC}с. "
        f"Торговля: {'ВКЛ' if TRADE_ENABLED else 'ВЫКЛ'}")
    tg(f"🤖 TraderKing v5 запущен. Авто ТP/SL • Работа 24/7.\nИнтервал: {CHECK_INTERVAL_SEC}с.")

    # первичный логин
    if not capital_login():
        log("⚠️ Capital login не удался — будет повтор перед каждым запросом")
    while True:
        try:
            for name in SYMBOLS.keys():
                await process_symbol(name)
            log("=== CYCLE DONE ===")
        except Exception as e:
            log(f"🔥 Loop error: {e}\n{traceback.format_exc()}")
            tg(f"⚠️ Ошибка цикла: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SEC)

if __name__ == "__main__":
    asyncio.run(main_loop())
