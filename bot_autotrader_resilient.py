import os
import time
import json
import math
import asyncio
import traceback
from datetime import datetime, timezone
import requests
import pandas as pd
import numpy as np
import yfinance as yf
import os, requests

url = "https://api-capital.backend-capital.com/api/v1/session"
payload = {
    "identifier": os.environ.get("CAPITAL_USERNAME"),
    "password": os.environ.get("CAPITAL_API_PASSWORD")
}
headers = {
    "X-CAP-API-KEY": os.environ.get("CAPITAL_API_KEY"),
    "Content-Type": "application/json"
}

r = requests.post(url, json=payload, headers=headers)
print("Login test status:", r.status_code, r.text)
exit()
# ==========================
# ENV CONFIG
# ==========================

CAPITAL_API_KEY = os.environ.get("CAPITAL_API_KEY", "")
CAPITAL_API_PASSWORD = os.environ.get("CAPITAL_API_PASSWORD", "")
CAPITAL_USERNAME = os.environ.get("CAPITAL_USERNAME", "")
CAPITAL_BASE_URL = os.environ.get("CAPITAL_BASE_URL", "https://api-capital.backend-capital.com")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))
HISTORY_PERIOD = os.environ.get("HISTORY_PERIOD", "3mo")
HISTORY_INTERVAL = os.environ.get("HISTORY_INTERVAL", "1h")

LEVERAGE = float(os.environ.get("LEVERAGE", "20"))
POSITION_FRACTION = float(os.environ.get("POSITION_FRACTION", "0.25"))
SL_PCT = float(os.environ.get("SL_PCT", "0.005"))   # 0.5% стоп
TP_PCT = float(os.environ.get("TP_PCT", "0.010"))   # 1% тейк

SYMBOLS = {
    "GOLD": {"epic": "IX.D.GC.FEB25.IP", "yf": "GC=F"},
    "OIL_BRENT": {"epic": "IX.D.BRENT.F25.IP", "yf": "BZ=F"},
    "NATGAS": {"epic": "IX.D.NATGAS.F25.IP", "yf": "NG=F"},
}

# ==========================
# GLOBAL STATE
# ==========================

ACTIVE_POSITIONS = {}

# ==========================
# HELPERS
# ==========================

def log(msg: str):
    """Лог в консоль и Telegram"""
    text = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(text, flush=True)
    try:
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                timeout=10
            )
    except Exception:
        pass


def capital_headers():
    return {
        "X-SECURITY-TOKEN": os.environ.get("X-SECURITY-TOKEN", ""),
        "CST": os.environ.get("CST", ""),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def capital_login():
    """Авторизация в Capital"""
    try:
        r = requests.post(
            f"{CAPITAL_BASE_URL}/api/v1/session",
            json={"identifier": CAPITAL_USERNAME, "password": CAPITAL_API_PASSWORD},
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            os.environ["X-SECURITY-TOKEN"] = data["securityToken"]
            os.environ["CST"] = data["clientSessionId"]
            log("✅ Capital login OK")
            return True
        else:
            log(f"🔥 Capital login failed: {r.text}")
            return False
    except Exception as e:
        log(f"🔥 Capital login exception: {e}")
        return False


def get_yahoo_data(symbol):
    """Получение данных с Yahoo"""
    try:
        df = yf.download(symbol, period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False, timeout=10)
        if df is None or df.empty:
            return None
        df = df[["Close"]].dropna()
        df["EMA20"] = df["Close"].ewm(span=20).mean()
        df["Signal"] = np.where(df["Close"] > df["EMA20"], "BUY", "SELL")
        return df
    except Exception as e:
        log(f"⚠️ Yahoo data error for {symbol}: {e}")
        return None


def capital_order(epic, direction, size, stop_loss=None, take_profit=None):
    """Открытие позиции"""
    try:
        payload = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "orderType": "MARKET",
            "guaranteedStop": False,
        }

        # Добавляем SL/TP если заданы
        if stop_loss and take_profit:
            payload["stopLevel"] = stop_loss
            payload["limitLevel"] = take_profit

        r = requests.post(
            f"{CAPITAL_BASE_URL}/api/v1/positions",
            headers=capital_headers(),
            json=payload,
            timeout=10
        )

        if r.status_code == 200:
            return True
        else:
            log(f"❌ Order fail: {r.text}")
            return False
    except Exception as e:
        log(f"❌ Exception in order: {e}")
        return False


def close_position(epic, direction):
    """Закрытие позиции"""
    try:
        opposite = "SELL" if direction == "BUY" else "BUY"
        r = requests.post(
            f"{CAPITAL_BASE_URL}/api/v1/positions/otc",
            headers=capital_headers(),
            json={
                "epic": epic,
                "direction": opposite,
                "size": 1.0,
                "orderType": "MARKET",
                "guaranteedStop": False
            },
            timeout=10
        )
        if r.status_code == 200:
            log(f"✅ Позиция {epic} закрыта ({opposite})")
            return True
        else:
            log(f"⚠️ Ошибка при закрытии {epic}: {r.text}")
            return False
    except Exception as e:
        log(f"❌ Exception close_position: {e}")
        return False


# ==========================
# MAIN LOGIC
# ==========================

async def process_symbol(symbol_name, data):
    epic = data["epic"]
    yf_symbol = data["yf"]

    df = get_yahoo_data(yf_symbol)
    if df is None:
        log(f"⚠️ {symbol_name}: нет данных с Yahoo")
        return

    signal = df["Signal"].iloc[-1]
    price = df["Close"].iloc[-1]
    log(f"{symbol_name}: сигнал {signal} при цене {price}")

    current = ACTIVE_POSITIONS.get(symbol_name)

    # TP и SL уровни
    if signal == "BUY":
        sl = price * (1 - SL_PCT)
        tp = price * (1 + TP_PCT)
    else:
        sl = price * (1 + SL_PCT)
        tp = price * (1 - TP_PCT)

    # Закрытие старой позиции при смене сигнала
    if current and current != signal:
        log(f"🔁 {symbol_name}: сигнал изменился {current} → {signal}, закрываю...")
        close_position(epic, current)
        ACTIVE_POSITIONS.pop(symbol_name, None)

    # Если позиции нет — открываем
    if symbol_name not in ACTIVE_POSITIONS:
        success = capital_order(epic, signal, size=1.0, stop_loss=sl, take_profit=tp)
        if success:
            ACTIVE_POSITIONS[symbol_name] = signal
            log(f"✅ {symbol_name}: {signal} открыта. SL={round(sl,2)} TP={round(tp,2)}")
        else:
            log(f"❌ {symbol_name}: не удалось открыть позицию")


async def main_loop():
    log("🤖 TraderKing v3 запущен. Авто TP/SL + закрытие при смене сигнала. Работа 24/7.")

    while True:
        try:
            if not await capital_login():
                await asyncio.sleep(60)
                continue

            for symbol_name, data in SYMBOLS.items():
                await process_symbol(symbol_name, data)

            log("=== 🔁 Цикл завершён, жду следующий ===")
            await asyncio.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            log(f"⚠️ Ошибка цикла: {e}")
            traceback.print_exc()
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main_loop())
