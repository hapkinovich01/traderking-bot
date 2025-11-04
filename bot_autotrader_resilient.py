import asyncio
import pandas as pd
import yfinance as yf
import numpy as np
import logging
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands

# ===================== НАСТРОЙКИ =====================
RISK_SHARE = 0.25            # 25% от баланса
HISTORY_PERIOD = "5d"
HISTORY_INTERVAL = "1m"      # агрессивный таймфрейм
SL_ATR_MULT = 2.0
TP_ATR_MULT = 3.0
VOLATILITY_THRESHOLD = 0.15  # фильтр: если ATR < 0.15% от цены — не торгуем

SYMBOLS = {
    "GOLD": "GC=F",
    "OIL_BRENT": "BZ=F",
    "NATURAL_GAS": "NG=F"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =====================================================

def get_signal(df):
    """Комбинация EMA, RSI, MACD, Bollinger, Stochastic"""
    close = df["Close"]

    ema_fast = EMAIndicator(close, window=9).ema_indicator()
    ema_slow = EMAIndicator(close, window=21).ema_indicator()
    rsi = RSIIndicator(close, window=14).rsi()
    macd = MACD(close)
    macd_line, macd_signal = macd.macd(), macd.macd_signal()
    stoch = StochasticOscillator(df["High"], df["Low"], close)
    stoch_k = stoch.stoch()
    stoch_d = stoch.stoch_signal()

    bb = BollingerBands(close, window=20, window_dev=2)
    bb_high = bb.bollinger_hband()
    bb_low = bb.bollinger_lband()

    last_close = close.iloc[-1]

    # BUY сигнал
    if (
        ema_fast.iloc[-1] > ema_slow.iloc[-1] and
        macd_line.iloc[-1] > macd_signal.iloc[-1] and
        rsi.iloc[-1] < 70 and
        stoch_k.iloc[-1] > stoch_d.iloc[-1] and
        last_close > bb_low.iloc[-1]
    ):
        return "BUY"

    # SELL сигнал
    elif (
        ema_fast.iloc[-1] < ema_slow.iloc[-1] and
        macd_line.iloc[-1] < macd_signal.iloc[-1] and
        rsi.iloc[-1] > 30 and
        stoch_k.iloc[-1] < stoch_d.iloc[-1] and
        last_close < bb_high.iloc[-1]
    ):
        return "SELL"

    return "HOLD"


def compute_tp_sl(df, last_price, direction):
    """Автоматический TP/SL по ATR"""
    atr = AverageTrueRange(df["High"], df["Low"], df["Close"], window=14).average_true_range().iloc[-1]
    if direction == "BUY":
        sl = last_price - SL_ATR_MULT * atr
        tp = last_price + TP_ATR_MULT * atr
    else:
        sl = last_price + SL_ATR_MULT * atr
        tp = last_price - TP_ATR_MULT * atr
    return sl, tp, atr


def compute_position(balance, price):
    """Размер позиции от баланса"""
    nominal = balance * RISK_SHARE
    size = max(1, int(nominal / price))
    return size


def volatility_filter(df):
    """Отфильтровывает флэт: ATR < 0.15% от цены"""
    atr = AverageTrueRange(df["High"], df["Low"], df["Close"], window=14).average_true_range().iloc[-1]
    price = df["Close"].iloc[-1]
    volatility = atr / price
    return volatility >= VOLATILITY_THRESHOLD


async def process_symbol(symbol, ticker):
    try:
        df = yf.download(ticker, period=HISTORY_PERIOD, interval=HISTORY_INTERVAL, progress=False)
        if df.empty:
            logging.warning(f"[{symbol}] Нет данных от Yahoo.")
            return

        last_price = float(df["Close"].iloc[-1])
        if not volatility_filter(df):
            logging.info(f"[{symbol}] Рынок во флэте, ATR слишком мал — пропуск.")
            return

        signal = get_signal(df)
        balance = 1000  # тестовый баланс
        logging.info(f"[{symbol}] Цена={last_price:.2f} | Сигнал={signal}")

        if signal in ["BUY", "SELL"]:
            sl, tp, atr = compute_tp_sl(df, last_price, signal)
            size = compute_position(balance, last_price)
            logging.info(f"[{symbol}] {signal} | SL={sl:.2f} | TP={tp:.2f} | ATR={atr:.4f} | Lot={size}")

            # Здесь добавляется реальный запрос к API Capital для торговли
            # execute_trade(signal, size, sl, tp)
        else:
            logging.info(f"[{symbol}] Нет сигнала, ждем следующего цикла.")

    except Exception as e:
        logging.error(f"[{symbol}] Ошибка: {e}")


async def main():
    while True:
        tasks = [process_symbol(symbol, ticker) for symbol, ticker in SYMBOLS.items()]
        await asyncio.gather(*tasks)
        logging.info("=== CYCLE DONE ===")
        await asyncio.sleep(60)  # проверка каждую минуту


if __name__ == "__main__":
    asyncio.run(main())
