import os
import json
import time
import math
import asyncio
from datetime import datetime, timezone

import requests
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD

# ==============================
# ⚙️ ENV / CONFIG
# ==============================
CAPITAL_API_KEY       = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_USERNAME      = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_API_PASSWORD  = os.environ.get("CAPITAL_API_PASSWORD", "")
CAPITAL_BASE_URL      = os.environ.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")

TELEGRAM_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")

# Стратегия / риск
CHECK_INTERVAL_SEC    = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))  # каждые 5 минут
HISTORY_PERIOD        = os.environ.get("HISTORY_PERIOD", "1mo")
HISTORY_INTERVAL      = os.environ.get("HISTORY_INTERVAL", "1h")

LEVERAGE              = float(os.environ.get("LEVERAGE", "20"))     # 1:20
POSITION_FRACTION     = float(os.environ.get("POSITION_FRACTION", "0.25"))  # 25% капитала
SL_PCT                = float(os.environ.get("SL_PCT", "0.006"))    # 0.6% стоп
TP_MULT               = float(os.environ.get("TP_MULT", "2.0"))     # тейк = 2x стоп
TRADE_ENABLED         = os.environ.get("TRADE_ENABLED", "True").lower() == "true"

# Символы
SYMBOLS = {
    "Gold":  {"yf": "GC=F", "epic": "CS.D.GC.MONTH1",    "query": "gold"},
    "Brent": {"yf": "BZ=F", "epic": "CS.D.BRENT.MONTH1", "query": "brent"},
    "Gas":   {"yf": "NG=F", "epic": "CS.D.NATGAS.MONTH1","query": "natural gas"},
}

# Держим сессионные токены Capital
TOKENS = {"CST": "", "X-SECURITY-TOKEN": ""}

# Чтобы не спамить — запоминаем последний сигнал/позицию
LAST_SIGNAL = {k: "HOLD" for k in SYMBOLS.keys()}


# ==============================
# 🔧 UTILS
# ==============================
def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg: str):
    print(f"[{ts()}] {msg}", flush=True)

def tg(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log(f"TG> {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=12
        )
    except Exception as e:
        log(f"⚠️ Telegram error: {e}")

def cap_headers():
    return {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "CST": TOKENS.get("CST", ""),
        "X-SECURITY-TOKEN": TOKENS.get("X-SECURITY-TOKEN", ""),
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

def safe_req(method, url, retries=3, **kwargs):
    for i in range(retries):
        try:
            r = requests.request(method, url, timeout=20, **kwargs)
            if r.status_code in (200, 201, 400, 401, 404, 409):
                return r
            log(f"HTTP {r.status_code} on {url}: {r.text}")
        except Exception as e:
            log(f"Req error [{i+1}/{retries}] {method} {url}: {e}")
        time.sleep(1.5)
    return None


# ==============================
# 🔐 CAPITAL AUTH
# ==============================
def capital_login() -> bool:
    url = f"{CAPITAL_BASE_URL}/api/v1/session"
    data = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD}
    base_headers = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Content-Type": "application/json"}

    try:
        r = requests.post(url, headers=base_headers, json=data, timeout=20)
        if r.status_code == 200:
            TOKENS["CST"] = r.headers.get("CST", "")
            TOKENS["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")
            log("✅ Capital login OK")
            return True
        log(f"❌ Capital login failed: {r.status_code} {r.text}")
        return False
    except Exception as e:
        log(f"❌ Capital login exception: {e}")
        return False


# ==============================
# 💰 BALANCE / POSITIONS
# ==============================
def capital_balance():
    """Пытаемся вытащить баланс (несколько возможных эндпоинтов)."""
    for endpoint in ("/api/v1/accounts/me", "/api/v1/accounts"):
        url = f"{CAPITAL_BASE_URL}{endpoint}"
        r = safe_req("GET", url, headers=cap_headers())
        if r and r.status_code == 401:
            if not capital_login():
                return None
            r = safe_req("GET", url, headers=cap_headers())
        if r and r.status_code == 200:
            try:
                data = r.json()
                # разные структуры — попробуем несколько вариантов
                if isinstance(data, dict):
                    ai = data.get("accountInfo") or data.get("account", {}).get("balance") or {}
                    bal = ai.get("available") or ai.get("balance") or 0.0
                    return float(bal)
                elif isinstance(data, list) and data:
                    ai = data[0].get("balance") or {}
                    bal = ai.get("available") or ai.get("balance") or 0.0
                    return float(bal)
            except Exception:
                pass
    return None

def capital_positions():
    """Текущие позиции (для поиска открытых по эпикам)."""
    url = f"{CAPITAL_BASE_URL}/api/v1/positions"
    r = safe_req("GET", url, headers=cap_headers())
    if r and r.status_code == 401:
        if not capital_login():
            return []
        r = safe_req("GET", url, headers=cap_headers())
    if r and r.status_code == 200:
        try:
            data = r.json()
            return data.get("positions") or data  # иногда массив
        except Exception:
            return []
    return []


# ==============================
# 💵 PRICE with FALLBACK
# ==============================
def capital_price(epic: str, name: str):
    """Пробуем /prices, если 401 — перелогиниваемся; иначе — Yahoo fallback."""
    try:
        # Capital price
        url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
        r = safe_req("GET", url, headers=cap_headers())
        if r and r.status_code == 401:
            if not capital_login():
                return None
            r = safe_req("GET", url, headers=cap_headers())

        if r and r.status_code == 200:
            arr = r.json().get("prices") or []
            if arr:
                p = arr[-1]
                bid = float(p.get("bid", 0) or 0)
                ask = float(p.get("offer", 0) or 0)
                mid = (bid + ask) / 2 if (bid and ask) else (bid or ask or 0)
                if mid:
                    log(f"✅ {name} price Capital = {mid}")
                    return {"bid": bid, "ask": ask, "mid": mid}

        # Fallback Yahoo
        yf_ticker = SYMBOLS[name]["yf"]
        log(f"⚠️ {name}: no Capital price (code {r.status_code if r else 'n/a'}). Fallback to Yahoo…")
        df = yf.download(yf_ticker, period="1d", interval="1h", progress=False)
        if not df.empty:
            px = float(df["Close"].iloc[-1])
            log(f"✅ {name} price Yahoo = {px}")
            return {"bid": px, "ask": px, "mid": px}
        log(f"❌ {name}: no price from Yahoo either.")
        return None
    except Exception as e:
        log(f"❌ capital_price({name}) error: {e}")
        return None


# ==============================
# 📊 INDICATORS & SIGNAL
# ==============================
def calc_indicators(df: pd.DataFrame):
    df = df.copy()
    df["EMA20"] = EMAIndicator(df["Close"], window=20).ema_indicator()
    df["EMA50"] = EMAIndicator(df["Close"], window=50).ema_indicator()
    df["RSI"]   = RSIIndicator(df["Close"]).rsi()
    macd        = MACD(df["Close"])
    df["MACD"]  = macd.macd()
    df["SIGN"]  = macd.macd_signal()
    return df

def decide(df: pd.DataFrame) -> str:
    """Сводим EMA/RSI/MACD в простой сигнал BUY/SELL/HOLD."""
    try:
        last = df.iloc[-1]
        ema   = "BUY" if last["EMA20"] > last["EMA50"] else "SELL"
        rsi   = "BUY" if last["RSI"] < 35 else "SELL" if last["RSI"] > 65 else "HOLD"
        macd  = "BUY" if last["MACD"] > last["SIGN"]  else "SELL"
        if ema == "BUY" and rsi == "BUY" and macd == "BUY":
            return "BUY"
        if ema == "SELL" and rsi == "SELL" and macd == "SELL":
            return "SELL"
        return "HOLD"
    except Exception as e:
        log(f"decide() error: {e}")
        return "HOLD"


# ==============================
# 📐 SIZING / SL-TP
# ==============================
def compute_order_params(price_mid: float, balance: float, name: str):
    """Расчёт размера и уровней SL/TP. Размер — в «контрактах» (приближённо)."""
    if price_mid <= 0 or balance is None:
        return None

    risk_cap = balance * POSITION_FRACTION * LEVERAGE  # «рычагом»
    size_raw = risk_cap / price_mid
    # минимальные/нормальные шаги (грубо, чтобы не 0.0001)
    step = 0.1 if name in ("Gold", "Brent", "Gas") else 1.0
    size = max(step, math.floor(size_raw / step) * step)

    sl_abs = price_mid * SL_PCT
    tp_abs = sl_abs * TP_MULT

    return {
        "size": round(size, 2),
        "sl":   round(sl_abs, 2),
        "tp":   round(tp_abs, 2),
    }


# ==============================
# 🟢 OPEN / 🔴 CLOSE via Capital
# ==============================
def capital_open_market(epic: str, direction: str, price_mid: float, params: dict, currency="USD"):
    """
    Создание MARKET позиции с уровнем SL/TP.
    Формат тела — ориентир по публичной схеме Capital/IG (может отличаться у вашего аккаунта).
    Скрипт печатает ответ API — по ним легко подстроить поля при необходимости.
    """
    if not epic or not direction or not params:
        return False, "bad params"

    level_sl = price_mid - params["sl"] if direction == "BUY"  else price_mid + params["sl"]
    level_tp = price_mid + params["tp"] if direction == "BUY"  else price_mid - params["tp"]

    body = {
        "epic": epic,
        "direction": direction,        # BUY / SELL
        "size": params["size"],        # контрактов
        "orderType": "MARKET",
        "guaranteedStop": False,
        "forceOpen": True,
        "stopLevel": round(level_sl, 2),
        "limitLevel": round(level_tp, 2),
        "currencyCode": currency
    }

    url = f"{CAPITAL_BASE_URL}/api/v1/positions/otc"
    r = safe_req("POST", url, headers=cap_headers(), data=json.dumps(body))
    if r and r.status_code in (200, 201):
        log(f"✅ OPEN OK: {r.text}")
        return True, r.text
    else:
        log(f"❌ OPEN ERR [{r.status_code if r else 'no-resp'}]: {r.text if r else ''}")
        return False, r.text if r else "no response"

def capital_close_positions(epic: str):
    """
    Пытаемся закрыть все открытые позиции по epic.
    В разных схемах API закрытие может идти через DELETE /positions/otc/{dealId}
    или POST на специальный endpoint. Ниже — «универсальная» попытка.
    """
    pos = capital_positions()
    closed_ok = 0
    for p in pos:
        try:
            deal_id = p.get("position", {}).get("dealId") or p.get("dealId")
            p_epic  = p.get("market", {}).get("epic") or p.get("epic")
            if not deal_id or p_epic != epic:
                continue
            url = f"{CAPITAL_BASE_URL}/api/v1/positions/otc/{deal_id}"
            r = safe_req("DELETE", url, headers=cap_headers())
            if r and r.status_code in (200, 204):
                closed_ok += 1
            else:
                log(f"❌ CLOSE ERR [{r.status_code if r else 'no-resp'}] {deal_id}: {r.text if r else ''}")
        except Exception as e:
            log(f"close err: {e}")
    return closed_ok


# ==============================
# 🔁 MAIN LOOP
# ==============================
async def main_loop():
    tg(f"🤖 TraderKing запущен (Render). Автоторговля: {'ВКЛ' if TRADE_ENABLED else 'ВЫКЛ'}. Интервал: {CHECK_INTERVAL_SEC//60}м.")
    if not capital_login():
        tg("❌ Не удалось авторизоваться в Capital!")
        return

    while True:
        try:
            balance = capital_balance()
            if balance is None:
                log("⚠️ balance unknown (continue)")

            for name, meta in SYMBOLS.items():
                epic = meta["epic"]
                # 1) Цена
                px = capital_price(epic, name)
                if not px:
                    tg(f"⚠️ {name}: нет цены (Capital/Yahoo) — пропуск")
                    continue

                # 2) История для индикаторов (через Yahoo)
                df = yf.download(meta["yf"], period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False)
                if df.empty or "Close" not in df.columns:
                    tg(f"⚠️ {name}: нет данных истории {HISTORY_PERIOD}/{HISTORY_INTERVAL}")
                    continue

                df = calc_indicators(df)
                signal = decide(df)

                # 3) Отправим краткий отчёт
                last = df.iloc[-1]
                tg(f"{name} | Price: {px['mid']:.2f} | RSI: {last['RSI']:.2f} | "
                   f"EMA20/50: {last['EMA20']:.2f}/{last['EMA50']:.2f} | MACD: {last['MACD']:.2f}/{last['SIGN']:.2f} "
                   f"→ {signal}")

                # 4) Автоторговля
                if not TRADE_ENABLED or signal == "HOLD":
                    LAST_SIGNAL[name] = signal
                    continue

                # Открывать — только если изменился сигнал и нет позиции/или позиция противоположная
                if signal != LAST_SIGNAL[name]:
                    # Закрыть всё по этому epic (если есть)
                    closed = capital_close_positions(epic)  # пробуем закрыть позиции по инструменту
                    if closed:
                        tg(f"🔴 Закрыто позиций по {name}: {closed}")

                    # Рассчитать размер
                    params = compute_order_params(px["mid"], balance, name)
                    if not params:
                        tg(f"⚠️ {name}: не удалось посчитать размер позиции")
                        LAST_SIGNAL[name] = signal
                        continue

                    ok, resp = capital_open_market(epic, signal, px["mid"], params)
                    if ok:
                        tg(f"🟢 Открыт {signal} по {name} | size={params['size']}, SL={params['sl']}, TP={params['tp']}")
                    else:
                        tg(f"❌ Ошибка открытия {signal} по {name}: {resp}")

                    LAST_SIGNAL[name] = signal
                else:
                    # тот же сигнал — ничего не делаем
                    pass

            await asyncio.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            log(f"MAIN LOOP error: {e}")
            time.sleep(3)


# ==============================
# 🚀 RUN
# ==============================
if __name__ == "__main__":
    asyncio.run(main_loop())
