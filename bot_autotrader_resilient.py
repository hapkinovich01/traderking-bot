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

# ====================== ENV / CONFIG =========================
CAPITAL_API_KEY = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_API_PASSWORD = os.environ.get("CAPITAL_API_PASSWORD", "")
CAPITAL_USERNAME = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_BASE_URL = os.environ.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Торговые настройки
LEVERAGE = float(os.environ.get("LEVERAGE", "20"))         # 1:20
POSITION_FRACTION = float(os.environ.get("POSITION_FRACTION", "0.25"))  # 25% от баланса
SL_PCT = float(os.environ.get("SL_PCT", "0.006"))          # 0.6% стоп
TP_MULT = float(os.environ.get("TP_MULT", "2.0"))          # тейк = 2*стоп
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))  # 5 минут
HISTORY_PERIOD = os.environ.get("HISTORY_PERIOD", "1mo")
HISTORY_INTERVAL = os.environ.get("HISTORY_INTERVAL", "1h")
TRADE_ENABLED = os.environ.get("TRADE_ENABLED", "true").lower() == "true"

# Символы: Yahoo тикер + Capital epic (мы будем запрашивать цены по epic; если не даст — fallback на Yahoo)
SYMBOLS = {
    "Gold":  {"yf": "GC=F", "epic": "GOLD"},
    "Brent": {"yf": "BZ=F", "epic": "OIL_BRENT"},
    "Gas":   {"yf": "NG=F", "epic": "NATURALGAS"},
}

# Точки/минимальные дистанции стопов у брокера (эвристика; подгоняй по логам «invalid stop/limit distance»)
# stopDistance/limitDistance у Capital обычно указывается В ПУНКТАХ (points).
MARKET_META = {
    "GOLD":   {"POINT_VALUE": 0.01, "MIN_STOP": 30},   # 30 points ~ 0.30$ если шаг 0.01
    "BRENT":  {"POINT_VALUE": 0.01, "MIN_STOP": 30},
    "NATGAS": {"POINT_VALUE": 0.001, "MIN_STOP": 50},
}

TOKENS = {"CST": "", "X-SECURITY-TOKEN": ""}
BALANCE_CACHE = 0.0   # будем обновлять после логина/по мере надобности
OPEN_DEALS = {}       # epic -> list of deals (we cache to simplify closing)

# ====================== UTILITIES =========================
def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str):
    print(f"[{utcnow()}] {msg}", flush=True)

def send_message(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15
        )
    except Exception as e:
        print(f"⚠️ Telegram error: {e}", flush=True)

def safe_req(method: str, url: str, **kwargs):
    # 3 попытки, чтобы сгладить сетевые фейлы
    for i in range(3):
        try:
            r = requests.request(method, url, timeout=15, **kwargs)
            return r
        except Exception as e:
            log(f"⚠️ [{i+1}/3] Request error: {e} -> {url}")
            time.sleep(2)
    return None

def cap_headers():
    return {
        "CST": TOKENS["CST"],
        "X-SECURITY-TOKEN": TOKENS["X-SECURITY-TOKEN"],
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

# ====================== CAPITAL API =========================
def capital_login() -> bool:
    global BALANCE_CACHE
    url = f"{CAPITAL_BASE_URL}/api/v1/session"
    payload = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD}
    headers = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Content-Type": "application/json"}

    r = safe_req("POST", url, json=payload, headers=headers)
    if not r:
        log("❌ Capital login failed: no response")
        tg("❌ Capital: нет ответа при логине")
        return False

    if r.status_code != 200:
        log(f"❌ Capital login failed: {r.status_code} {r.text}")
        tg(f"❌ Capital login failed: {r.status_code} {r.text}")
        return False

    TOKENS["CST"] = r.headers.get("CST", "")
    TOKENS["X-SECURITY-TOKEN"] = r.headers.get("X-SECURITY-TOKEN", "")

    try:
        data = r.json()
        # login ответ содержит баланс аккаунта
        BALANCE_CACHE = float(data.get("accountInfo", {}).get("balance", 0.0))
    except Exception:
        pass

    log("✅ Capital login OK")
    tg("✅ Capital: вход выполнен")
    return True

def capital_price(epic: str):
    url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
    r = safe_req("GET", url, headers=cap_headers())
    if not r or r.status_code != 200:
        log(f"⚠️ no price from Capital for {epic} ({None if not r else r.status_code})")
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

def list_positions():
    url = f"{CAPITAL_BASE_URL}/api/v1/positions"
    r = safe_req("GET", url, headers=cap_headers())
    if not r:
        log("⚠️ positions: no response")
        return []
    if r.status_code != 200:
        log(f"⚠️ positions: {r.status_code} {r.text}")
        return []
    try:
        return r.json().get("positions", [])
    except Exception:
        return []

def _normalize_stop_limit(epic: str, price: float, stop_pct: float, tp_mult: float, direction: str):
    """
    Переводим стоп/тейк в пункты (points) для тела запроса.
    """
    meta = MARKET_META.get(epic, {"POINT_VALUE": 0.01, "MIN_STOP": 30})
    point = meta["POINT_VALUE"]
    min_stop = meta["MIN_STOP"]

    stop_abs = price * stop_pct
    tp_abs = stop_abs * tp_mult

    # distance в points = абсолютное расстояние / стоимость пункта
    stop_distance = max(int(round(stop_abs / point)), min_stop)
    limit_distance = max(int(round(tp_abs / point)), min_stop)

    return stop_distance, limit_distance

def open_position(epic: str, direction: str, size: float, price_ref: float):
    """
    MARKER: некоторые параметры могут не совпасть с требованиями брокера.
    Если сервер вернёт 400 — мы шлём тело ответа в Telegram для быстрой подгонки.
    """
    stop_distance, limit_distance = _normalize_stop_limit(epic, price_ref, SL_PCT, TP_MULT, direction)

    body = {
        "epic": epic,
        "direction": direction.upper(),              # BUY / SELL
        "size": float(round(size, 3)),               # округлим размер
        "orderType": "MARKET",
        # Capital ожидает distance в ПОИНТАХ (points), не в деньгах:
        "stopDistance": stop_distance,
        "limitDistance": limit_distance,
        # Можно добавить "forceOpen": True, если брокер это требует
        "guaranteedStop": False
    }
    url = f"{CAPITAL_BASE_URL}/api/v1/positions"

    r = safe_req("POST", url, headers=cap_headers(), data=json.dumps(body))
    if not r:
        tg(f"❌ Open {epic} {direction}: no response")
        return None

    if r.status_code not in (200, 201):
        tg(f"❌ Open {epic} {direction}: {r.status_code}\n{r.text}\nBody: {json.dumps(body)}")
        log(f"❌ Open {epic} {direction}: {r.status_code} {r.text}")
        return None

    try:
        data = r.json()
    except Exception:
        data = {}
    tg(f"✅ Открыта позиция {epic} {direction} size={body['size']} (SLd={stop_distance}, TPd={limit_distance})")
    log(f"OPEN OK: {epic} {direction} {data}")
    return data

def close_position_by_deal(deal_id: str, direction: str, size: float):
    """
    Пытаемся закрыть через DELETE /positions/{dealId}.
    Если вернёт 404/405 — пробуем fallback через /positions/close.
    """
    url_del = f"{CAPITAL_BASE_URL}/api/v1/positions/{deal_id}"
    r = safe_req("DELETE", url_del, headers=cap_headers(), data=json.dumps({"size": size}))
    if r and r.status_code in (200, 201, 204):
        tg(f"✅ Закрыта позиция {deal_id} size={size}")
        log(f"CLOSE OK: {deal_id}")
        return True

    # Fallback вариант закрытия (если у брокера другая форма)
    url_alt = f"{CAPITAL_BASE_URL}/api/v1/positions/close"
    payload = {"dealId": deal_id, "direction": direction.upper(), "size": float(size)}
    r2 = safe_req("POST", url_alt, headers=cap_headers(), data=json.dumps(payload))
    if r2 and r2.status_code in (200, 201):
        tg(f"✅ Закрыта позиция {deal_id} (alt) size={size}")
        log(f"CLOSE ALT OK: {deal_id}")
        return True

    tg(f"❌ Close failed for {deal_id}\n"
       f"DELETE: {None if not r else f'{r.status_code} {r.text}'}\n"
       f"ALT: {None if not r2 else f'{r2.status_code} {r2.text}'}")
    log(f"❌ CLOSE failed for {deal_id}")
    return False

def close_all_positions_for_epic(epic: str):
    positions = list_positions()
    closed = 0
    for p in positions:
        try:
            deal_id = p.get("position", {}).get("dealId")
            p_epic = p.get("market", {}).get("epic") or p.get("instrument", {}).get("epic")
            size = float(p.get("position", {}).get("size", 0.0))
            direction = p.get("position", {}).get("direction", "BUY")
            if p_epic == epic and size > 0:
                # чтобы закрыть, некоторые брокеры требуют обратное направление
                opp = "SELL" if direction.upper() == "BUY" else "BUY"
                if close_position_by_deal(deal_id, opp, size):
                    closed += 1
        except Exception as e:
            log(f"close_all_positions_for_epic error: {e}")
    return closed

# ====================== DATA / INDICATORS =========================
def close_series_1d(df: pd.DataFrame) -> pd.Series:
    """
    Принудительно приводит колонку цены к одномерной серии (исправляет ValueError: Data must be 1-dimensional)
    """
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

def decide(df: pd.DataFrame) -> str:
    last = df.iloc[-1]
    if last["EMA20"] > last["EMA50"] and last["RSI"] < 70 and last["MACD"] > last["MACD_signal"]:
        return "BUY"
    elif last["EMA20"] < last["EMA50"] and last["RSI"] > 30 and last["MACD"] < last["MACD_signal"]:
        return "SELL"
    else:
        return "HOLD"

# ====================== SIZING / RISK =========================
def fetch_balance():
    """Пробует обновить баланс из /accounts, если доступно. Иначе оставляет кэш."""
    global BALANCE_CACHE
    url = f"{CAPITAL_BASE_URL}/api/v1/accounts"
    r = safe_req("GET", url, headers=cap_headers())
    if r and r.status_code == 200:
        try:
            data = r.json()
            # найдём текущий preferred аккаунт
            for acc in data.get("accounts", []):
                if acc.get("preferred", False):
                    BALANCE_CACHE = float(acc.get("balance", {}).get("balance", BALANCE_CACHE))
                    break
        except Exception:
            pass
    return BALANCE_CACHE

def calc_size(epic: str, price: float) -> float:
    """
    Простая модель: используем долю баланса * плечо / цена.
    Для CFD это даст приблизительное количество контрактов (size).
    """
    balance = max(fetch_balance(), 0.0)
    if balance <= 0:
        # если баланс 0 (новый счёт), просто пробуем минимальный размер 1
        return 1.0
    exposure = balance * POSITION_FRACTION * LEVERAGE
    size = max(exposure / max(price, 1e-6), 1.0)
    # слегка округлим разумно для нефтегаза
    return round(size, 2)

# ====================== MAIN LOOP =========================
async def main_loop():
    log("🤖 TraderKing started (Render).")
    tg(f"🤖 TraderKing запущен. Автоторговля: {'ВКЛ' if TRADE_ENABLED else 'ВЫКЛ'}. Интервал: {CHECK_INTERVAL_SEC//60}м.")

    if not capital_login():
        tg("❌ Ошибка входа в Capital")
        return

    while True:
        try:
            for name, meta in SYMBOLS.items():
                epic = meta["epic"]
                yf_ticker = meta["yf"]

                log(f"🔍 Checking {name} ({epic}/{yf_ticker}) ...")

                # 1) Текущая цена: Capital -> fallback Yahoo
                price = capital_price(epic)
                if price is None:
                    log(f"⚠️ {name}: no Capital price, fallback to Yahoo")
                    hist = yf.download(yf_ticker, period="5d", interval="5m", progress=False, auto_adjust=True)
                    if hist.empty:
                        tg(f"⚠️ Нет данных цены для {name}")
                        continue
                    price = float(close_series_1d(hist).iloc[-1])

                # 2) История для индикаторов
                df = yf.download(yf_ticker, period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False, auto_adjust=True)
                if df.empty:
                    tg(f"⚠️ {name}: пустые исторические данные {HISTORY_PERIOD}/{HISTORY_INTERVAL}")
                    continue

                ind = calc_indicators(df)
                signal = decide(ind)

                last_rsi = ind["RSI"].iloc[-1]
                tg(f"{name}: {price:.2f} | Signal: {signal} | RSI: {last_rsi:.1f}")

                if not TRADE_ENABLED or signal == "HOLD":
                    log(f"{name} => {signal} (no trade)")
                    continue

                # 3) Логика сделок: если приходит обратный сигнал — закрываем все и переворачиваемся
                closed = 0
                if signal in ("BUY", "SELL"):
                    # Закрыть все позиции по этому epic (если есть)
                    closed = close_all_positions_for_epic(epic)

                    # Открыть новую позицию
                    size = calc_size(epic, price)
                    data = open_position(epic, signal, size, price)
                    if data is None:
                        log(f"❌ не удалось открыть позицию {epic} {signal}")
                    else:
                        log(f"✅ Открыта позиция {epic} {signal} size={size}")

                log(f"{name} => {signal}; closed={closed}")

            log("=== CYCLE DONE ===")
            await asyncio.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            log(f"🔥 MAIN LOOP error: {e}")
            tg(f"🔥 Ошибка цикла: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main_loop())
