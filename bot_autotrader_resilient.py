import math
import numpy as np
import pandas as pd

# =======================
# AGGRESSIVE STRATEGY v2
# =======================

def get_signal(df: pd.DataFrame) -> str:
    """
    Агрессивная стратегия входов:
    EMA20/EMA50 + MACD + RSI + динамическая фильтрация
    Возвращает BUY / SELL / HOLD
    """
    df = df.copy().dropna()
    if len(df) < 60:
        return "HOLD"

    close = df["Close"]
    ema20 = close.ewm(span=20).mean()
    ema50 = close.ewm(span=50).mean()

    # MACD
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd_line = ema12 - ema26
    macd_signal = macd_line.ewm(span=9).mean()
    macd_hist = macd_line - macd_signal

    # RSI
    delta = close.diff()
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(14).mean()
    avg_loss = pd.Series(loss).rolling(14).mean()
    rs = avg_gain / avg_loss
    rsi14 = 100 - (100 / (1 + rs))

    # значения последних свечей
    e20_now = float(ema20.iloc[-1])
    e50_now = float(ema50.iloc[-1])
    e20_prev = float(ema20.iloc[-2])
    e50_prev = float(ema50.iloc[-2])
    macd_now = float(macd_hist.iloc[-1])
    macd_prev = float(macd_hist.iloc[-2])
    rsi_now = float(rsi14.iloc[-1])
    price_now = float(close.iloc[-1])

    # сигналы EMA
    bull_cross = (e20_prev <= e50_prev) and (e20_now > e50_now)
    bear_cross = (e20_prev >= e50_prev) and (e20_now < e50_now)

    # агрессивная логика
    if (bull_cross or (e20_now > e50_now and macd_now > macd_prev)) and (rsi_now < 74):
        return "BUY"

    if (bear_cross or (e20_now < e50_now and macd_now < macd_prev)) and (rsi_now > 25):
        return "SELL"

    return "HOLD"


# =======================
# POSITION & RISK LOGIC
# =======================
def compute_position_params(balance: float, atr_value: float, last_price: float, direction: str):
    """
    Агрессивная версия:
    - 35–40% от баланса
    - короткий тейк-профит, широкий стоп
    - трейлинг-стоп на основе ATR
    """
    if atr_value is None or math.isnan(atr_value) or atr_value <= 0:
        atr_value = last_price * 0.004  # запасной вариант

    # риск: 35% от баланса
    notional = max(1.0, balance * 0.35)
    size = int(max(1, min(50, round(notional / max(1e-6, last_price)))))

    # SL / TP
    sl_mult = 1.4
    tp_mult = 0.9
    sl_dist = sl_mult * atr_value
    tp_dist = tp_mult * atr_value

    if direction == "BUY":
        stop_level = last_price - sl_dist
        limit_level = last_price + tp_dist
    else:
        stop_level = last_price + sl_dist
        limit_level = last_price - tp_dist

    # трейлинг стоп (динамический)
    trailing_stop = 0.6 * atr_value

    return {
        "size": size,
        "stop_level": stop_level,
        "limit_level": limit_level,
        "trailing_stop": trailing_stop
    }
