import os
import time
import json
import math
import traceback
import requests
import pandas as pd
from datetime import datetime, timezone

# ---------- ПАРАМЕТРЫ И ОКРУЖЕНИЕ ----------
CAPITAL_BASE_URL = os.getenv("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")
CAPITAL_USERNAME = os.getenv("CAPITAL_USERNAME")
CAPITAL_PASSWORD = os.getenv("CAPITAL_PASSWORD")
CAPITAL_API_KEY = os.getenv("CAPITAL_API_KEY")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# Эпики можно переопределить через .env
EPIC_GOLD        = os.getenv("EPIC_GOLD", "GOLD")
EPIC_OIL_BRENT   = os.getenv("EPIC_OIL_BRENT", "OIL_BRENT")
EPIC_NATURAL_GAS = os.getenv("EPIC_NATURAL_GAS", "NATURAL_GAS")

# Таймфрейм для Capital (5 минут)
RESOLUTION = "MINUTE_5"  # неизменно, по запросу пользователя
CANDLES_LIMIT = 300      # исторических свечей для индикаторов

# Управление риском
LEVERAGE = float(os.getenv("LEVERAGE", "20"))         # плечо (информативно)
RISK_SHARE = float(os.getenv("RISK_SHARE", "0.25"))   # 25% от баланса на сделку (по запросу)
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
TP_ATR_MULT = float(os.getenv("TP_ATR_MULT", "1.8"))  # TP = 1.8 * ATR
SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.2"))  # SL = 1.2 * ATR

SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "60")) # пауза между циклами
ALLOW_MULTIPLE_POSITIONS = os.getenv("ALLOW_MULTIPLE_POSITIONS", "true").lower() == "true"

# ---------- ПОЛЕЗНОЕ ----------
def send_telegram(text: str):
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("⚠️ TELEGRAM не настроен (нет токена/чата). Сообщение:", text)
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    except Exception as e:
        print("❌ Ошибка отправки в Telegram:", e)

def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# ---------- CAPITAL API КЛИЕНТ С АВТОПЕРЕЛОГИНОМ ----------
class CapitalAPI:
    def __init__(self, base_url, username, password, api_key):
        self.base = base_url.rstrip("/")
        self.u = username
        self.p = password
        self.k = api_key
        self.s = requests.Session()
        self.cst = None
        self.xst = None
        self.last_login = 0

    def login(self) -> bool:
        url = f"{self.base}/api/v1/session"
        headers = {"X-CAP-API-KEY": self.k, "Content-Type": "application/json"}
        body = {"identifier": self.u, "password": self.p}
        try:
            r = self.s.post(url, headers=headers, data=json.dumps(body), timeout=30)
            if r.status_code == 200:
                self.cst = r.headers.get("CST")
                self.xst = r.headers.get("X-SECURITY-TOKEN")
                self.last_login = time.time()
                print("✅ [Capital] Авторизация успешна")
                send_telegram("✅ TraderKing: авторизация Capital успешна")
                return True
            else:
                print(f"❌ [Capital] Логин {r.status_code}: {r.text}")
                send_telegram(f"❌ TraderKing: ошибка логина Capital {r.status_code}: {r.text}")
                return False
        except Exception as e:
            print("🔥 [Capital] Исключение логина:", e)
            send_telegram(f"🔥 TraderKing: исключение логина Capital: {e}")
            return False

    def ensure(self):
        if not self.cst or (time.time() - self.last_login > 3300):
            print("🔁 [Capital] Обновление токена...")
            self.login()

    def _headers(self, json_ct=False):
        h = {
            "CST": self.cst or "",
            "X-SECURITY-TOKEN": self.xst or "",
            "X-CAP-API-KEY": self.k
        }
        if json_ct:
            h["Content-Type"] = "application/json"
        return h

    def get(self, endpoint, params=None):
        self.ensure()
        url = f"{self.base}{endpoint}"
        try:
            r = self.s.get(url, headers=self._headers(), params=params, timeout=30)
            if r.status_code == 401 or "error.null.client.token" in r.text:
                self.login()
                r = self.s.get(url, headers=self._headers(), params=params, timeout=30)
            return r
        except Exception as e:
            print("❌ GET error:", e); return None

    def post(self, endpoint, data):
        self.ensure()
        url = f"{self.base}{endpoint}"
        try:
            r = self.s.post(url, headers=self._headers(json_ct=True), data=json.dumps(data), timeout=30)
            if r.status_code == 401 or "error.null.client.token" in r.text:
                self.login()
                r = self.s.post(url, headers=self._headers(json_ct=True), data=json.dumps(data), timeout=30)
            return r
        except Exception as e:
            print("❌ POST error:", e); return None

    def accounts(self):
        return self.get("/api/v1/accounts")

    def prices(self, epic, resolution=RESOLUTION, maxsize=CANDLES_LIMIT):
        # API Capital: /api/v1/prices/{epic}?resolution=MINUTE_5&max=...
        return self.get(f"/api/v1/prices/{epic}", params={"resolution": resolution, "max": maxsize})

    def market_details(self, epic):
        return self.get(f"/api/v1/markets/{epic}")

    def open_position(self, epic, direction, size, stop_level=None, limit_level=None, currency="USD"):
        payload = {
            "epic": epic,
            "direction": direction,     # BUY / SELL
            "size": size,               # кол-во контрактов (инт)
            "orderType": "MARKET",
            "timeInForce": "FILL_OR_KILL",
            "guaranteedStop": False,
            "currencyCode": currency
        }
        # Лучше задавать уровнями (не distance), так стабильнее
        if stop_level is not None:
            payload["stopLevel"] = float(stop_level)
        if limit_level is not None:
            payload["limitLevel"] = float(limit_level)

        r = self.post("/api/v1/positions", payload)
        return r

    def open_positions(self):
        return self.get("/api/v1/positions")

# ---------- ИНДИКАТОРЫ (без внешних либ, чтобы не ловить 1-D ошибки) ----------
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(span=period, adjust=False).mean()
    roll_down = down.ewm(span=period, adjust=False).mean()
    rs = roll_up / (roll_down.replace(0, 1e-9))
    r = 100 - (100 / (1 + rs))
    return r

def macd(series, fast=12, slow=26, signal=9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def bollinger(series, period=20, std=2.0):
    ma = series.rolling(period).mean()
    sd = series.rolling(period).std(ddof=0)
    upper = ma + std * sd
    lower = ma - std * sd
    return ma, upper, lower

def atr(df, period=14):
    high = df["high"]; low = df["low"]; close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def stochastic(df, k_period=14, d_period=3, smooth_k=3):
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    k = 100 * (df["close"] - low_min) / ((high_max - low_min).replace(0, 1e-9))
    k = k.rolling(smooth_k).mean()
    d = k.rolling(d_period).mean()
    return k, d

def compute_indicators(df):
    close = df["close"]
    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    rsi14 = rsi(close, 14)
    macd_line, macd_signal, macd_hist = macd(close, 12, 26, 9)
    bb_mid, bb_up, bb_dn = bollinger(close, 20, 2.0)
    atr14 = atr(df, ATR_PERIOD)
    k, d = stochastic(df, 14, 3, 3)
    out = df.copy()
    out["ema20"] = ema20
    out["ema50"] = ema50
    out["rsi"] = rsi14
    out["macd"] = macd_line
    out["macd_signal"] = macd_signal
    out["macd_hist"] = macd_hist
    out["bb_mid"] = bb_mid
    out["bb_up"] = bb_up
    out["bb_dn"] = bb_dn
    out["atr"] = atr14
    out["stoch_k"] = k
    out["stoch_d"] = d
    return out

# ---------- ЛОГИКА СИГНАЛОВ ----------
def trade_signal(row):
    """Возвращает 'BUY' / 'SELL' / None по набору условий."""
    # Условия входа BUY:
    buy = (
        row["ema20"] > row["ema50"] and
        row["rsi"] > 55 and
        row["macd_hist"] > 0 and
        row["stoch_k"] > row["stoch_d"] and
        row["close"] > row["bb_mid"]
    )
    # Условия входа SELL:
    sell = (
        row["ema20"] < row["ema50"] and
        row["rsi"] < 45 and
        row["macd_hist"] < 0 and
        row["stoch_k"] < row["stoch_d"] and
        row["close"] < row["bb_mid"]
    )
    if buy: return "BUY"
    if sell: return "SELL"
    return None

# ---------- ЦЕНЫ ИЗ CAPITAL ----------
def capital_candles(api: CapitalAPI, epic: str):
    r = api.prices(epic)
    if not r or r.status_code != 200:
        return None, f"prices {epic} status {getattr(r,'status_code',None)}: {getattr(r,'text',None)}"
    data = r.json()
    # Формат: {"prices":[{"openPrice":{"bid":...,"ask":...}, "closePrice":..., "highPrice":..., "lowPrice":..., "snapshotTimeUTC":"..."}]}
    rows = []
    for p in data.get("prices", []):
        try:
            o = (p["openPrice"]["bid"] + p["openPrice"]["ask"]) / 2.0
            h = (p["highPrice"]["bid"] + p["highPrice"]["ask"]) / 2.0
            l = (p["lowPrice"]["bid"] + p["lowPrice"]["ask"]) / 2.0
            c = (p["closePrice"]["bid"] + p["closePrice"]["ask"]) / 2.0
            t = p.get("snapshotTimeUTC") or p.get("snapshotTime")
            rows.append({"time": t, "open": o, "high": h, "low": l, "close": c})
        except Exception:
            continue
    if not rows:
        return None, "no candles"
    df = pd.DataFrame(rows)
    # Убираем NaN/дубликаты и гарантируем 1D-формат
    df = df.dropna().reset_index(drop=True)
    for col in ["open","high","low","close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    df = df.dropna()
    return df, None

# ---------- РАЗМЕР СДЕЛКИ / TP / SL ----------
def compute_position_params(balance, atr_value, last_price, direction):
    """
    size: приблизительно из риска. Берём 25% баланса как номинал, делим на last_price.
    Это даёт число контрактов "на глаз" (ограничиваем минимум 1 и максимум 50).
    SL/TP: уровнями от текущей цены по ATR.
    """
    if atr_value is None or math.isnan(atr_value) or atr_value <= 0:
        atr_value = last_price * 0.003  # запасной вариант ~0.3%

    # объём
    notional = max(1.0, balance * RISK_SHARE)      # 25% от баланса
    size = int(max(1, min(50, round(notional / max(1e-6, last_price)))))

    # уровни
    sl_dist = SL_ATR_MULT * atr_value
    tp_dist = TP_ATR_MULT * atr_value

    if direction == "BUY":
        stop_level = last_price - sl_dist
        limit_level = last_price + tp_dist
    else:
        stop_level = last_price + sl_dist
        limit_level = last_price - tp_dist

    return size, stop_level, limit_level

# ---------- ОСНОВНОЙ ЦИКЛ ----------
def run():
    # Проверки окружения
    missing = [k for k in ["CAPITAL_USERNAME","CAPITAL_PASSWORD","CAPITAL_API_KEY","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"] if not os.getenv(k)]
    if missing:
        print("❌ Не заданы переменные окружения:", ", ".join(missing))
        print("   Создай .env (пример ниже в конце ответа) или выставь их в Render.")
        return

    api = CapitalAPI(CAPITAL_BASE_URL, CAPITAL_USERNAME, CAPITAL_PASSWORD, CAPITAL_API_KEY)
    if not api.login():
        print("⛔ Остановлено: логин не прошёл.")
        return

    send_telegram("🚀 TraderKing Pro v6 запущен (5m, авто-TP/SL, множественные позиции).")

    symbols = [
        ("GOLD", EPIC_GOLD),
        ("OIL_BRENT", EPIC_OIL_BRENT),
        ("NATURAL_GAS", EPIC_NATURAL_GAS),
    ]

    while True:
        cycle_errors = []
        try:
            # баланс
            acc = api.accounts()
            if not acc or acc.status_code != 200:
                cycle_errors.append(f"accounts:{getattr(acc,'status_code',None)}")
                raise RuntimeError("accounts_failed")
            acc_json = acc.json()
            # в разных ответах структура может отличаться; пытаемся вытащить available/balance
            balance = (
                acc_json.get("accountInfo", {}).get("balance", 0.0)
                or acc_json.get("balance", {}).get("balance", 0.0)
                or 0.0
            )

            for name, epic in symbols:
                try:
                    df, err = capital_candles(api, epic)
                    if err or df is None or len(df) < 60:
                        print(f"ℹ️ {name}: нет достаточных свечей ({err})")
                        continue

                    ind = compute_indicators(df)
                    last = ind.iloc[-1]

                    signal = trade_signal(last)
                    last_price = float(last["close"])
                    atr_val = float(last["atr"]) if pd.notna(last["atr"]) else None

                    if not signal:
                        print(f"[{name}] {ts()} — HOLD (ema20={last['ema20']:.2f}, ema50={last['ema50']:.2f}, rsi={last['rsi']:.1f})")
                        continue

                    direction = "BUY" if signal == "BUY" else "SELL"
                    size, stop_level, limit_level = compute_position_params(balance, atr_val, last_price, direction)

                    # Открываем позицию
                    resp = api.open_position(epic=epic, direction=direction, size=size,
                                             stop_level=stop_level, limit_level=limit_level, currency="USD")
                    if resp is None:
                        send_telegram(f"❌ {name}: нет ответа при открытии позиции")
                        continue

                    if resp.status_code in (200, 201):
                        txt = (f"✅ {name}: ОТКРЫТА {direction}\n"
                               f"size={size}\n"
                               f"price≈{last_price:.2f}\n"
                               f"SL={stop_level:.2f}\nTP={limit_level:.2f}")
                        print(txt); send_telegram(txt)
                    else:
                        print(f"❌ {name}: ошибка открытия {resp.status_code} {resp.text}")
                        send_telegram(f"❌ {name}: ошибка открытия сделки\n{resp.text}")

                    # Множественные позиции разрешены: ничего не закрываем здесь.
                    # Закрытие/переворот можно добавлять отдельной логикой, если потребуется.

                except Exception as e_sym:
                    print(f"🔥 Исключение по {name}:", e_sym)
                    traceback.print_exc()
                    send_telegram(f"🔥 Ошибка по {name}: {e_sym}")

            print(f"⏳ Пауза {SLEEP_SECONDS}s...\n")
            time.sleep(SLEEP_SECONDS)

        except Exception as e:
            print("⚠️ Ошибка цикла:", e, "| details:", cycle_errors)
            traceback.print_exc()
            send_telegram(f"⚠️ Ошибка основного цикла: {e}")
            time.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    run()
