# TraderKing (Render 24/7)

Автоторговый бот для Gold, Brent, Natural Gas. Работает на Render как **Background Worker**.

## Что делает
- Анализ каждые 5 минут (RSI, EMA20/EMA50, MACD, Bollinger).
- Плечо 1:20, размер позиции 25% от доступного баланса.
- SL ≈ 0.6% от цены, TP = 2×SL.
- Автовыход из позиции по пересечению EMA/RSI.
- Короткие сигналы в Telegram.

## Файлы
- `bot_autotrader_resilient.py` — основной код.
- `requirements.txt` — зависимости.

## Render: создание сервиса
1. Создайте репозиторий на GitHub и загрузите оба файла.
2. На https://render.com → **New +** → **Background Worker** → выберите ваш репозиторий.
3. Настройки:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot_autotrader_resilient.py`
   - **Region:** ближайший к вам.
4. **Environment Variables**:
   - `CAPITAL_API_KEY` — ваш ключ Capital
   - `CAPITAL_USERNAME` — ваш email
   - `CAPITAL_API_PASSWORD` — пароль
   - `CAPITAL_BASE_URL` — `https://api-capital.backend-capital.com`
   - `TELEGRAM_TOKEN` — токен Telegram-бота
   - `TELEGRAM_CHAT_ID` — ваш chat_id
   - (опц.) `TRADE_ENABLED` — `True`/`False`
   - (опц.) `LEVERAGE` — по умолчанию 20
   - (опц.) `POSITION_FRACTION` — по умолчанию 0.25
   - (опц.) `SL_PCT` — по умолчанию 0.006
   - (опц.) `TP_MULT` — по умолчанию 2.0

5. Нажмите **Create Worker** → **Deploy**. Логи в разделе **Logs**.

## Примечания
- Если API вернёт 401, бот сам перелогинится.
- Минимальные стоп-дистанции у инструментов отличаются — проверьте в Capital.com. Если ордер отклоняется, увеличьте `SL_PCT`.
- Чтобы отключить реальные сделки, установите `TRADE_ENABLED=False`.
