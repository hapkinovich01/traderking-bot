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

# ==== Параметры через ENV (обязательные для телеграма) ====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# ==== Capital (реальная торговля) - укажи, если хочешь торговать ====
CAPITAL_BASE_URL = os.getenv("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")
CAPITAL_API_KEY = os.getenv("CAPITAL_API_KEY", "")
CAPITAL_USERNAME = os.getenv("CAPITAL_USERNAME", "")
CAPITAL_PASSWORD = os.getenv("CAPITAL_PASSWORD", "")

# EPIC'и для реальной торговли в Capital (дай свои, иначе торги не будут отправляться)
EPIC_GOLD  = os.getenv("EPIC_GOLD",  "")     # пример: "GOLD"
EPIC_BRENT = os.getenv("EPIC_BRENT", "")     # пример: "OIL_BRENT"
EPIC_GAS   = os.getenv("EPIC_GAS",   "")     # пример: "NATURAL_GAS"

# ==== Настройки стратегии (можешь менять ENVами) ====
INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "60"))     # пауза между циклами
YF_PERIOD  = os.getenv("YF_PERIOD", "1d")                            # период загрузки свечей
YF_INTERVAL = os.getenv("YF_INTERVAL", "1m")                         # интервал свечей
LEVERAGE = float(os.getenv("LEVERAGE", "20"))                        # кредитное плечо
RISK_FRACTION = float(os.getenv("RISK_FRACTION", "0.25"))            # доля баланса на сделку (0.25 = 25%)
ATR_WINDOW = int(os.getenv("ATR_WINDOW", "14"))
TP_ATR = float(os.getenv("TP_ATR", "1.8"))                           # тейк по ATR
SL_ATR = float(os.getenv("SL_ATR", "1.2"))                           # стоп по ATR
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "10"))          # защита от “переприходов”

# Лимит минимального числа свечей, чтобы индикаторы были валидны
MIN_BARS = 120

# Карта Yahoo -> имя + EPIC
SYMBOLS = {
    "GC=F":  {"name": "GOLD",      "epic": EPIC_GOLD},
    "BZ=F":  {"name": "OIL_BRENT", "epic": EPIC_BRENT},   # Brent crude на Yahoo
    "NG=F":  {"name": "GAS",       "epic": EPIC_GAS},
}

session_tokens = {"CST": "", "XST": ""}  # Capital CST/X-SECURITY-TOKEN
last_signal_ts: Dict[str, float] = {}    # защита от частых входов
open_positions: Dict[str, Dict] = {}     # локальный реестр открытых позиций

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

# ==================== Capital: авторизация и ордера ====================
def capital_login() -> bool:
    if not (CAPITAL_API_KEY and CAPITAL_USERNAME and CAPITAL_PASSWORD):
        log("⚠️ Capital: ключи/логин не заданы — торги отключены, будут только сигналы.")
        return False
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/session"
        headers = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Accept": "application/json"}
        data = {"identifier": CAPITAL_USERNAME, "password": CAPITAL_PASSWORD}
        r = requests.post(url, headers=headers, json=data, timeout=20)
        ok = r.status_code == 200
        if ok:
            session_tokens["CST"] = r.headers.get("CST", "")
            session_tokens["XST"] = r.headers.get("X-SECURITY-TOKEN", "")
            log(f"✅ Capital login OK. CST/XST получены.")
            tg_send("✅ Авторизация Capital прошла успешно.")
        else:
            log(f"❌ Capital login failed: {r.status_code} {r.text}")
            tg_send(f"❌ Ошибка авторизации Capital: {r.text}")
        return ok
    except Exception as e:
        log(f"❌ Capital login exception: {e}")
        tg_send(f"❌ Ошибка авторизации Capital: {e}")
        return False

def capital_headers():
    return {
        "X-CAP-API-KEY": CAPITAL_API_KEY,
        "CST": session_tokens["CST"],
        "X-SECURITY-TOKEN": session_tokens["XST"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def capital_get_balance() -> Optional[float]:
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/accounts"
        r = requests.get(url, headers=capital_headers(), timeout=20)
        if r.status_code == 200:
            js = r.json()
            # Ищем текущий счёт (preferred true)
            for acc in js.get("accounts", []):
                if acc.get("preferred"):
                    bal = acc.get("balance", {}).get("available", 0.0)
                    return float(bal)
        else:
            log(f"⚠️ get_balance {r.status_code} {r.text}")
    except Exception as e:
        log(f"⚠️ get_balance exception: {e}")
    return None

def capital_place_market(epic: str, direction: str, size: float,
                         stop_distance: float, limit_distance: float) -> Tuple[bool, str]:
    """
    direction: "BUY" или "SELL"
    distance — абсолютные цены (не проценты): для Capital v1/v2 используются price levels.
    Мы дадим в заявке stopLevel/limitLevel как конкретные цены (не distance).
    """
    try:
        # Получим текущую цену, чтобы выставить stop/limit уровнями
        bid, ask = capital_get_bid_ask(epic)
        if bid is None or ask is None:
            return False, "no price"

        entry = ask if direction == "BUY" else bid
        stop_level  = entry - stop_distance if direction == "BUY" else entry + stop_distance
        limit_level = entry + limit_distance if direction == "BUY" else entry - limit_distance

        # API v2/positions (некоторым аккаунтам доступен v1; оставим v2 основным)
        url = f"{CAPITAL_BASE_URL}/api/v2/positions"
        payload = {
            "epic": epic,
            "direction": direction,
            "size": round(size, 2),
            "orderType": "MARKET",
            "guaranteedStop": False,
            "stopLevel": round(stop_level, 2),
            "limitLevel": round(limit_level, 2),
            "forceOpen": True,
            "level": None,
            "currencyCode": "USD",
        }
        r = requests.post(url, headers=capital_headers(), data=json.dumps(payload), timeout=25)
        if r.status_code in (200, 201):
            deal_ref = r.json().get("dealReference", "n/a")
            return True, f"OK dealRef={deal_ref}"
        # fallback на v1 (если v2 вернул 404/405)
        if r.status_code in (404, 405):
            url1 = f"{CAPITAL_BASE_URL}/api/v1/positions/otc"
            r1 = requests.post(url1, headers=capital_headers(), data=json.dumps(payload), timeout=25)
            if r1.status_code in (200, 201):
                deal_ref = r1.json().get("dealReference", "n/a")
                return True, f"OK dealRef={deal_ref}"
            return False, r1.text
        return False, r.text
    except Exception as e:
        return False, f"exception: {e}"

def capital_get_bid_ask(epic: str) -> Tuple[Optional[float], Optional[float]]:
    try:
        url = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
        r = requests.get(url, headers=capital_headers(), timeout=15)
        if r.status_code == 200:
            js = r.json()
            # берём последний snapshot
            if "prices" in js and js["prices"]:
                last = js["prices"][-1]
                bid = last.get("bid")
                ask = last.get("ask")
                return (float(bid) if bid is not None else None,
                        float(ask) if ask is not None else None)
        return (None, None)
    except Exception:
        return (None, None)

# ==================== Индикаторы ====================
def compute_indicators(df: pd.DataFrame) -> Dict[str, pd.Series]:
    """
    df: DataFrame с колонками ['Open','High','Low','Close','Volume']
    Возвращает словарь Series одинаковой длины.
    """
    close = pd.Series(df["Close"].astype(float).values, index=df.index)
    high  = pd.Series(df["High"].astype(float).values, index=df.index)
    low   = pd.Series(df["Low"].astype(float).values, index=df.index)

    # EMA20 / EMA50
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    # RSI(14)
    delta = close.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=df.index).ewm(span=14, adjust=False).mean()
    roll_down = pd.Series(down, index=df.index).ewm(span=14, adjust=False).mean()
    rs = roll_up / (roll_down.replace(0, np.nan))
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(method="bfill").clip(0, 100)

    # MACD(12,26,9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal

    # Bollinger Bands (20,2)
    ma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std(ddof=0)
    bb_up = ma20 + 2 * std20
    bb_dn = ma20 - 2 * std20

    # Stochastic (14,3,3)
    ll14 = low.rolling(14).min()
    hh14 = high.rolling(14).max()
    stoch_k = (close - ll14) / (hh14 - ll14 + 1e-9) * 100.0
    stoch_k = stoch_k.rolling(3).mean()
    stoch_d = stoch_k.rolling(3).mean()

    # ATR(14)
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(ATR_WINDOW).mean()

    return {
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "macd": macd,
        "macd_signal": signal,
        "macd_hist": hist,
        "bb_up": bb_up,
        "bb_dn": bb_dn,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "atr": atr,
    }

# ==================== Сигналы (агрессивная логика) ====================
def build_signal(df: pd.DataFrame, ind: Dict[str, pd.Series]) -> Optional[Dict]:
    """
    Возвращает сигнал: {side, sl_abs, tp_abs} или None
    side: "BUY"/"SELL"
    sl_abs/tp_abs — абсолютные расстояния (цена), чтобы подставить в Capital stop/limit level.
    """
    if len(df) < MIN_BARS:
        return None

    close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])

    ema20, ema50 = ind["ema20"].iloc[-1], ind["ema50"].iloc[-1]
    ema20_prev, ema50_prev = ind["ema20"].iloc[-2], ind["ema50"].iloc[-2]
    rsi = float(ind["rsi"].iloc[-1])
    macd_hist = float(ind["macd_hist"].iloc[-1])
    macd_hist_prev = float(ind["macd_hist"].iloc[-2])
    bb_up, bb_dn = float(ind["bb_up"].iloc[-1]), float(ind["bb_dn"].iloc[-1])
    st_k, st_d = float(ind["stoch_k"].iloc[-1]), float(ind["stoch_d"].iloc[-1])
    atr = float(ind["atr"].iloc[-1])

    if not np.isfinite([ema20, ema50, rsi, macd_hist, macd_hist_prev, bb_up, bb_dn, st_k, st_d, atr]).all():
        return None

    # Тренд: вверх/вниз
    trend_up = ema20 > ema50
    crossed_up = ema20_prev <= ema50_prev and ema20 > ema50
    crossed_dn = ema20_prev >= ema50_prev and ema20 < ema50

    # Условия входа (агрессивно):
    long_ok = (
        trend_up
        and crossed_up
        and rsi > 52
        and macd_hist_prev <= 0 < macd_hist
        and close > bb_up  # импульсный пробой
        and st_k > st_d
    )
    short_ok = (
        (not trend_up)
        and crossed_dn
        and rsi < 48
        and macd_hist_prev >= 0 > macd_hist
        and close < bb_dn
        and st_k < st_d
    )

    if atr <= 0 or not np.isfinite(atr):
        return None

    sl_abs = SL_ATR * atr
    tp_abs = TP_ATR * atr

    if long_ok:
        return {"side": "BUY", "sl_abs": sl_abs, "tp_abs": tp_abs}
    if short_ok:
        return {"side": "SELL", "sl_abs": sl_abs, "tp_abs": tp_abs}
    return None

# ==================== Данные Yahoo ====================
def get_yf(symbol: str) -> Optional[pd.DataFrame]:
    """
    Надёжная загрузка с авто-повтором и “ужатием” периода, чтобы не висло.
    """
    for period in [YF_PERIOD, "3h", "1h"]:
        try:
            df = yf.download(symbol, period=period, interval=YF_INTERVAL, progress=False, auto_adjust=True, threads=False)
            if isinstance(df, pd.DataFrame) and len(df) and {"Open","High","Low","Close","Volume"}.issubset(df.columns):
                # фиксим мультииндекс (на всякий случай)
                df = df.reset_index().set_index("Datetime" if "Datetime" in df.columns else "Date")
                df = df.dropna().copy()
                return df
        except Exception:
            time.sleep(2)
    return None

# ==================== Размер позиции ====================
def compute_size(balance_usd: float, price: float, sl_abs: float) -> float:
    """
    Агрессивный риск: доля баланса * плечо, ограничиваем SL так, чтобы риск на сделку ~ RISK_FRACTION*баланс.
    """
    if balance_usd is None or balance_usd <= 0:
        balance_usd = 1000.0  # дефолт на случай, если баланс не получили
    risk_usd = balance_usd * RISK_FRACTION  # допускаем агрессивно
    if sl_abs <= 0:
        sl_abs = price * 0.005  # защитный минимум
    units = (risk_usd * LEVERAGE) / sl_abs
    # округлим, чтобы не было 0
    return max(0.01, round(units, 2))

# ==================== Основной цикл ====================
def process_symbol(yf_symbol: str, meta: Dict):
    name = meta["name"]
    epic = meta["epic"]

    log(f"→ Checking {name} ({yf_symbol})")
    df = get_yf(yf_symbol)
    if df is None or len(df) < MIN_BARS:
        log(f"⚠️ {name}: нет данных Yahoo (bars={0 if df is None else len(df)})")
        return

    ind = compute_indicators(df)
    sig = build_signal(df, ind)

    # вывод в Render-лог о состоянии
    close = float(df["Close"].iloc[-1])
    ema20 = float(ind["ema20"].iloc[-1])
    ema50 = float(ind["ema50"].iloc[-1])
    rsi = float(ind["rsi"].iloc[-1])
    macd_hist = float(ind["macd_hist"].iloc[-1])
    atr = float(ind["atr"].iloc[-1])
    log(f"{name} close={close:.2f} | EMA20={ema20:.2f} EMA50={ema50:.2f} | RSI={rsi:.1f} | MACDh={macd_hist:.3f} | ATR={atr:.3f}")

    if not sig:
        return

    # анти-спам: cooldown
    now_ts = time.time()
    if name in last_signal_ts and (now_ts - last_signal_ts[name]) < (COOLDOWN_MINUTES * 60):
        log(f"⏳ {name}: cooldown, пропуск входа.")
        return

    side = sig["side"]
    sl_abs = sig["sl_abs"]
    tp_abs = sig["tp_abs"]

    msg = f"🔔 {name}: {side}\nPrice: {close:.2f}\nSL≈{SL_ATR}×ATR ({sl_abs:.2f})\nTP≈{TP_ATR}×ATR ({tp_abs:.2f})\n{now_utc_iso()}"
    tg_send(msg)
    log(msg)

    # Если EPIC не задан — только сигнал
    if not epic:
        log(f"ℹ️ {name}: EPIC не задан — сделка НЕ отправлена в Capital.")
        last_signal_ts[name] = now_ts
        return

    # Торговля в Capital
    if not session_tokens["CST"] or not session_tokens["XST"]:
        if not capital_login():
            log("❌ Не удалось авторизоваться в Capital — только сигнал.")
            last_signal_ts[name] = now_ts
            return

    bal = capital_get_balance()
    size = compute_size(bal if bal is not None else 1000, close, sl_abs)

    ok, info = capital_place_market(epic=epic, direction="BUY" if side=="BUY" else "SELL",
                                    size=size, stop_distance=sl_abs, limit_distance=tp_abs)
    if ok:
        open_positions[name] = {
            "side": side,
            "size": size,
            "entry_price": close,
            "sl_abs": sl_abs,
            "tp_abs": tp_abs,
            "time": now_utc_iso(),
        }
        msg2 = f"✅ {name}: ордер отправлен в Capital ({side}) size={size}, SL≈{sl_abs:.2f}, TP≈{tp_abs:.2f}\n{info}"
        tg_send(msg2)
        log(msg2)
        last_signal_ts[name] = now_ts
    else:
        # если токен протух — пробуем логин и один повтор
        if "invalid" in info.lower() or "token" in info.lower():
            log("⚠️ Токен мог протухнуть — переавторизация…")
            if capital_login():
                ok2, info2 = capital_place_market(epic=epic, direction="BUY" if side=="BUY" else "SELL",
                                                  size=size, stop_distance=sl_abs, limit_distance=tp_abs)
                if ok2:
                    tg_send(f"✅ Повтор: {name} ордер отправлен. {info2}")
                    log(f"✅ Повтор отправки ок: {info2}")
                    last_signal_ts[name] = now_ts
                    return
                else:
                    tg_send(f"❌ {name}: повтор тоже неудачен: {info2}")
                    log(f"❌ {name}: повтор неудачен: {info2}")
            else:
                tg_send(f"❌ {name}: не удалось переавторизоваться Capital.")
                log(f"❌ {name}: не удалось переавторизоваться Capital.")
        else:
            tg_send(f"❌ {name}: ошибка открытия сделки\n{info}")
            log(f"❌ {name}: ошибка открытия сделки: {info}")

def main():
    log("🚀 TraderKing PRO v4 запущен.")
    tg_send("🤖 TraderKing запущен. Стратегия: EMA x RSI x MACD x BB x Stoch. SL/TP по ATR. Агрессивная.")

    # одноразовая попытка логина (если хотим торговать)
    if any([EPIC_GOLD, EPIC_BRENT, EPIC_GAS]) and CAPITAL_API_KEY and CAPITAL_USERNAME and CAPITAL_PASSWORD:
        capital_login()

    while True:
        try:
            for yf_symbol, meta in SYMBOLS.items():
                process_symbol(yf_symbol, meta)
                time.sleep(1.0)  # мини-пауза между инструментами
        except Exception as e:
            err = "".join(traceback.format_exception_only(type(e), e)).strip()
            log(f"⚠️ Ошибка цикла: {err}")
            tg_send(f"⚠️ Ошибка цикла: {err}")
        finally:
            # жизненный пульс для Render — чтобы было видно, что сервис жив
            log("…cycle complete …")
            time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
