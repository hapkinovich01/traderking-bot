import os
import time
import json
import requests
import traceback
import numpy as np
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.volatility import BollingerBands
from dotenv import load_dotenv

load_dotenv()

# === Настройки ===
CAPITAL_API = "https://api-capital.backend-capital.com"
CST_TOKEN = os.getenv("CST_TOKEN")
X_SECURITY_TOKEN = os.getenv("X_SECURITY_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = {
    "GOLD": "GC=F",
    "OIL_BRENT": "BZ=F",
    "NATURAL_GAS": "NG=F"
}

INTERVAL = "1m"
PERIOD = "1d"
RISK_SHARE = 0.25
SL_MULT = 2.0
TP_MULT = 3.0

# === Telegram ===
def send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text}
        )
    except:
        pass

# === Получение EPIC Capital ===
def get_epic(symbol_name):
    try:
        headers = {"X-CST": CST_TOKEN, "X-SECURITY-TOKEN": X_SECURITY_TOKEN}
        r = requests.get(f"{CAPITAL_API}/api/v1/markets?searchTerm={symbol_name}", headers=headers)
        data = r.json()
        if "markets" in data and len(data["markets"]) > 0:
            return data["markets"][0]["epic"]
        send_message(f"⚠️ Не найден EPIC для {symbol_name}")
        return None
    except Exception as e:
        send_message(f"❌ Ошибка EPIC: {e}")
        return None

# === Получение данных Yahoo ===
def get_data_yahoo(ticker):
    try:
        df = yf.download(ticker, period=PERIOD, interval=INTERVAL, progress=False)
        if df.empty:
            send_message(f"⚠️ {ticker}: нет данных из Yahoo.")
            return None
        return df
    except Exception as e:
        send_message(f"❌ Ошибка загрузки {ticker}: {e}")
        return None

# === Сигналы (EMA + MACD + RSI + ADX + STOCH + BBANDS) ===
def build_signal(df):
    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    ema_fast = EMAIndicator(close, 9).ema_indicator()
    ema_slow = EMAIndicator(close, 21).ema_indicator()
    macd = MACD(close).macd_diff()
    rsi = RSIIndicator(close, 14).rsi()
    adx = ADXIndicator(high, low, close, 14).adx()
    stoch = StochasticOscillator(high, low, close, 14, 3, 3)
    bb = BollingerBands(close, 20, 2)

    # Последние значения
    ema_fast_val = ema_fast.iloc[-1]
    ema_slow_val = ema_slow.iloc[-1]
    macd_val = macd.iloc[-1]
    rsi_val = rsi.iloc[-1]
    adx_val = adx.iloc[-1]
    stoch_k = stoch.stoch().iloc[-1]
    stoch_d = stoch.stoch_signal().iloc[-1]
    last_close = close.iloc[-1]

    # Сигналы
    strong_trend = adx_val > 25
    bullish_bb = last_close < bb.bollinger_lband().iloc[-1]
    bearish_bb = last_close > bb.bollinger_hband().iloc[-1]

    if (ema_fast_val > ema_slow_val and macd_val > 0 and rsi_val < 70 and stoch_k < 80 and strong_trend) or bullish_bb:
        return "BUY"
    elif (ema_fast_val < ema_slow_val and macd_val < 0 and rsi_val > 30 and stoch_k > 20 and strong_trend) or bearish_bb:
        return "SELL"
    return "HOLD"

# === SL / TP ===
def compute_sl_tp(last_price, direction):
    atr = last_price * 0.0025
    if direction == "BUY":
        return last_price - atr * SL_MULT, last_price + atr * TP_MULT
    else:
        return last_price + atr * SL_MULT, last_price - atr * TP_MULT

# === Получение баланса ===
def get_balance():
    try:
        headers = {"X-CST": CST_TOKEN, "X-SECURITY-TOKEN": X_SECURITY_TOKEN}
        r = requests.get(f"{CAPITAL_API}/api/v1/accounts", headers=headers)
        data = r.json()
        return float(data["balance"]["available"])
    except:
        return 0.0

# === Получение позиций ===
def get_positions():
    try:
        headers = {"X-CST": CST_TOKEN, "X-SECURITY-TOKEN": X_SECURITY_TOKEN}
        r = requests.get(f"{CAPITAL_API}/api/v1/positions", headers=headers)
        data = r.json()
        return data.get("positions", [])
    except:
        return []

# === Закрытие позиции ===
def close_position(deal_id):
    try:
        headers = {
            "X-CST": CST_TOKEN,
            "X-SECURITY-TOKEN": X_SECURITY_TOKEN,
            "Content-Type": "application/json"
        }
        payload = {"dealId": deal_id, "direction": "SELL", "size": 1}
        r = requests.post(f"{CAPITAL_API}/api/v1/positions/otc/close", headers=headers, json=payload)
        if r.status_code == 200:
            send_message(f"❎ Позиция {deal_id} закрыта.")
        else:
            send_message(f"⚠️ Ошибка закрытия {deal_id}: {r.text}")
    except Exception as e:
        send_message(f"❌ Ошибка закрытия: {e}")

# === Размещение ордера ===
def place_order(epic, direction, size, sl, tp):
    try:
        headers = {
            "X-CST": CST_TOKEN,
            "X-SECURITY-TOKEN": X_SECURITY_TOKEN,
            "Content-Type": "application/json"
        }
        payload = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "orderType": "MARKET",
            "limitLevel": tp,
            "stopLevel": sl,
            "forceOpen": True
        }
        r = requests.post(f"{CAPITAL_API}/api/v1/positions", headers=headers, json=payload)
        if r.status_code in [200, 201]:
            send_message(f"✅ Новый ордер {direction} {epic}\nSL={sl:.2f}, TP={tp:.2f}")
        else:
            send_message(f"⚠️ Ошибка ордера {epic}: {r.text}")
    except Exception as e:
        send_message(f"❌ Ошибка ордера: {e}")

# === Главный цикл ===
def main():
    send_message("🚀 TraderKing PRO v3 запущен (Live, Multi-Indikators, Reversal)!")

    epic_cache = {}
    last_signals = {}

    while True:
        balance = get_balance()
        if balance <= 0:
            send_message("⚠️ Баланс недоступен, ожидание 1 мин.")
            time.sleep(60)
            continue

        for name, ticker in SYMBOLS.items():
            try:
                if name not in epic_cache:
                    epic_cache[name] = get_epic(name)

                epic = epic_cache[name]
                if not epic:
                    continue

                df = get_data_yahoo(ticker)
                if df is None:
                    continue

                signal = build_signal(df)
                last_price = float(df["Close"].iloc[-1])
                sl, tp = compute_sl_tp(last_price, signal)

                prev_signal = last_signals.get(name)
                last_signals[name] = signal

                send_message(f"{name}: {signal} @ {last_price:.2f}")

                open_positions = get_positions()
                current_pos = next((p for p in open_positions if p["market"]["epic"] == epic), None)

                # === Реверс / закрытие ===
                if current_pos:
                    dir_open = current_pos["position"]["direction"]
                    deal_id = current_pos["position"]["dealId"]

                    if (dir_open == "BUY" and signal == "SELL") or (dir_open == "SELL" and signal == "BUY"):
                        close_position(deal_id)
                        send_message(f"🔁 Реверс: {dir_open} → {signal}")
                        time.sleep(2)
                        place_order(epic, signal, 1, sl, tp)
                        continue

                # === Новая позиция ===
                if not current_pos and signal in ["BUY", "SELL"]:
                    if signal == prev_signal:
                        continue  # защита от повторных входов
                    size = max(1, round(balance * RISK_SHARE / last_price))
                    place_order(epic, signal, size, sl, tp)

            except Exception as e:
                send_message(f"🔥 Ошибка цикла {name}: {e}\n{traceback.format_exc()}")

        time.sleep(60)

if __name__ == "__main__":
    main()
