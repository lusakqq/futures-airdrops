# -*- coding: utf-8 -*-
"""
Бот №2: быстрый шорт на фьючерсах Bitget (USDT-M) в момент открытия торгов.

Что делает:
  1. Вы вводите монету, например DOGS/USDT.
  2. Бот в быстром цикле спрашивает у Bitget: «контракт DOGSUSDT уже торгуется?»
  3. Как только торги открылись, бот отправляет РЫНОЧНЫЙ ордер на продажу (шорт)
     на заданную сумму с заданным плечом и СРАЗУ ВНУТРИ ЭТОГО ЖЕ ОРДЕРА
     прикрепляет стоп-лосс (Bitget называет это preset stop loss).

Три режима (переменная MODE в файле .env):
  dry  - ничего не покупает и не продаёт, ключи не нужны. Только показывает, что СДЕЛАЛ БЫ.
  demo - торгует на демо-счёте Bitget (виртуальные деньги, нужны ДЕМО-ключи).
  live - торгует реальными деньгами. Включайте только после проверки в dry и demo.
"""

import os                      # чтение переменных окружения (настроек)
import re                      # регулярные выражения для поиска текста в ошибках
import sys                     # аварийный выход из программы
import time                    # паузы и замер времени

import ccxt                    # библиотека для работы с биржами
from dotenv import load_dotenv  # загрузка настроек из файла .env


# ------------------------------------------------------------------
# 1. НАСТРОЙКИ
# ------------------------------------------------------------------

# папка программы: рядом с .exe (если собрано в exe) или рядом со скриптом
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))  # читаем файл .env из этой папки

MODE = os.getenv("MODE", "dry").strip().lower()                 # режим: dry / demo / live
MARGIN_USDT = float(os.getenv("MARGIN_USDT", "10"))              # сколько СВОИХ денег (маржи) ставим в сделку, в USDT
LEVERAGE = int(os.getenv("LEVERAGE", "3"))                       # кредитное плечо (3 = позиция в 3 раза больше маржи)
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "15"))          # стоп-лосс: на сколько % выше цены входа закрыть убыток
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0"))       # тейк-профит: на сколько % ниже входа забрать прибыль (0 = не ставить)
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "0.2"))           # как часто спрашивать биржу «торги открылись?» (в секундах)
FIRE_RETRY_SECONDS = float(os.getenv("FIRE_RETRY_SECONDS", "20"))  # сколько секунд повторять попытку открыть ордер, если биржа отказывает
MAX_WAIT_HOURS = float(os.getenv("MAX_WAIT_HOURS", "12"))        # через сколько часов ожидания бот сам остановится

API_KEY = os.getenv("BITGET_API_KEY", "")                        # API-ключ Bitget
API_SECRET = os.getenv("BITGET_API_SECRET", "")                  # секретный ключ Bitget
API_PASSPHRASE = os.getenv("BITGET_API_PASSPHRASE", "")          # пароль (passphrase), который вы придумали при создании ключа

PRODUCT_TYPE = "USDT-FUTURES"   # так Bitget называет рынок фьючерсов USDT-M
LIVE_STATUSES = {"normal"}      # статус контракта, при котором он торгуется


# ------------------------------------------------------------------
# 2. ПРОВЕРКА НАСТРОЕК (защита от опасных ошибок)
# ------------------------------------------------------------------

def check_settings():
    """Проверяем, что настройки разумные, ДО того как начнём что-то делать."""
    if MODE not in ("dry", "demo", "live"):                                   # неизвестный режим
        sys.exit("MODE должен быть dry, demo или live")                       # выходим с объяснением
    if MODE != "dry" and not (API_KEY and API_SECRET and API_PASSPHRASE):     # для торговли нужны все три части ключа
        sys.exit("Для режима demo/live заполните BITGET_API_KEY, BITGET_API_SECRET, BITGET_API_PASSPHRASE в .env")
    if not 1 <= LEVERAGE <= 10:                                               # плечо выше 10 для новичка с $200 слишком опасно
        sys.exit("LEVERAGE должно быть от 1 до 10 (для листингов советуем 2-3)")
    if MARGIN_USDT <= 0:                                                      # сумма должна быть положительной
        sys.exit("MARGIN_USDT должен быть больше 0")
    # Примерная цена ликвидации шорта на изолированной марже: вход * (1 + 1/плечо).
    # Стоп-лосс должен сработать ЗАМЕТНО раньше ликвидации, иначе он бесполезен.
    liq_pct = 100.0 / LEVERAGE                                                # например при 3х ликвидация примерно на +33%
    if STOP_LOSS_PCT <= 0 or STOP_LOSS_PCT >= liq_pct * 0.8:                  # оставляем запас 20% до ликвидации
        sys.exit(f"STOP_LOSS_PCT={STOP_LOSS_PCT}% слишком близко к ликвидации (~{liq_pct:.0f}% при плече {LEVERAGE}x). "
                 f"Уменьшите стоп или плечо.")


# ------------------------------------------------------------------
# 3. ПОДКЛЮЧЕНИЕ К BITGET
# ------------------------------------------------------------------

def make_exchange():
    """Создаём объект биржи Bitget в библиотеке ccxt."""
    exchange = ccxt.bitget({
        "apiKey": API_KEY,                     # ключ (в режиме dry может быть пустым)
        "secret": API_SECRET,                  # секрет
        "password": API_PASSPHRASE,            # в ccxt passphrase Bitget передаётся как password
        "enableRateLimit": True,               # ccxt сам не даст превысить лимиты запросов биржи
        "timeout": 5000,                       # ждём ответ биржи не дольше 5 секунд
        "options": {
            "defaultType": "swap",             # работаем с бессрочными фьючерсами (swap)
            "fetchMarkets": {"types": ["swap"]},  # загружаем только фьючерсы, так быстрее
            "adjustForTimeDifference": True,   # подстраиваемся под часы биржи (иначе бывают ошибки подписи)
        },
    })
    if MODE == "demo":                         # демо-режим Bitget
        exchange.set_sandbox_mode(True)        # ccxt добавит заголовок paptrading=1, ордера пойдут на демо-счёт
    return exchange                            # возвращаем готовый объект


def parse_symbol(text):
    """Превращаем то, что ввёл пользователь ('DOGS/USDT', 'dogsusdt', 'DOGS'), в нужные форматы."""
    t = text.strip().upper()                   # убираем пробелы, делаем заглавные буквы
    t = t.split(":")[0]                        # отрезаем ':USDT', если ввели полный формат ccxt
    t = t.replace("/", "").replace("-", "")    # убираем слэш и дефис: DOGS/USDT -> DOGSUSDT
    if t.endswith("USDT"):                     # если в конце уже есть USDT
        base = t[:-4]                          # базовая монета = всё, что до USDT
    else:
        base = t                               # иначе пользователь ввёл только монету
    if not base:                               # пустой ввод
        sys.exit("Не понял название монеты")
    exchange_id = base + "USDT"                # так символ называется в API Bitget: DOGSUSDT
    ccxt_symbol = f"{base}/USDT:USDT"          # так символ называется в ccxt для USDT-M фьючерсов
    return base, exchange_id, ccxt_symbol      # возвращаем все три варианта


# ------------------------------------------------------------------
# 4. ОЖИДАНИЕ ОТКРЫТИЯ ТОРГОВ
# ------------------------------------------------------------------

def get_contract_status(exchange, exchange_id):
    """Спрашиваем у Bitget состояние одного контракта. Возвращаем статус или None, если контракта ещё нет."""
    try:
        resp = exchange.publicMixGetV2MixMarketContracts({   # публичный запрос, ключи не нужны
            "productType": PRODUCT_TYPE,                       # рынок USDT-M
            "symbol": exchange_id,                             # конкретный контракт, например DOGSUSDT
        })
    except ccxt.NetworkError:                                  # кратковременный сбой связи
        return None                                            # просто спросим ещё раз в следующем круге
    except ccxt.BadSymbol:                                     # биржа говорит «такого символа нет»
        return None                                            # значит контракт ещё не создан
    except ccxt.ExchangeError:                                 # другие ответы-ошибки биржи (часто тоже «нет символа»)
        return None                                            # считаем, что контракта пока нет
    data = resp.get("data") or []                              # список контрактов в ответе
    for item in data:                                          # перебираем (обычно там один элемент)
        if item.get("symbol") == exchange_id:                  # нашли наш контракт
            return (item.get("symbolStatus") or item.get("status") or "").lower()  # возвращаем статус: listed / normal / ...
    return None                                                # контракта нет в ответе


def prepare_position_settings(exchange, ccxt_symbol):
    """Заранее ставим изолированную маржу, плечо и режим одной позиции, пока торги ещё не начались."""
    exchange.load_markets(reload=True)                        # перезагружаем список рынков, чтобы ccxt узнал о новой монете
    if ccxt_symbol not in exchange.markets:                   # ccxt ещё не видит символ
        return False                                          # попробуем ещё раз позже
    if MODE == "dry":                                         # в режиме dry ничего на бирже не меняем
        return True
    try:
        exchange.set_position_mode(False, ccxt_symbol)        # режим «одна позиция на монету» (one-way), проще для новичка
    except Exception as e:                                    # если есть открытые позиции, биржа может отказать
        print(f"  (режим позиции не изменён: {e})")           # не страшно, просто сообщаем
    try:
        exchange.set_margin_mode("isolated", ccxt_symbol)     # ИЗОЛИРОВАННАЯ маржа: рискуем только деньгами этой сделки
        exchange.set_leverage(LEVERAGE, ccxt_symbol)          # ставим плечо
        print(f"  Изолированная маржа и плечо {LEVERAGE}x установлены")
        return True                                           # всё готово
    except Exception as e:                                    # до открытия торгов биржа иногда не даёт менять настройки
        print(f"  Не удалось заранее выставить маржу/плечо ({e}), повторю перед входом")
        return False                                          # повторим позже


def wait_for_listing(exchange, exchange_id, ccxt_symbol):
    """Крутимся в цикле, пока контракт не начнёт торговаться."""
    print(f"Жду открытия фьючерса {exchange_id}. Опрос каждые {POLL_SECONDS} сек. Ctrl+C - остановить.")
    started = time.time()                                     # запоминаем время старта
    prepared = False                                          # настройки маржи/плеча ещё не выставлены
    last_status = "нет"                                       # последний увиденный статус (для вывода на экран)
    while True:                                               # бесконечный цикл, пока не выйдем через return
        status = get_contract_status(exchange, exchange_id)   # спрашиваем биржу
        shown = status or "нет контракта"                     # что показать на экране
        if shown != last_status:                              # печатаем только когда статус изменился
            print(f"  {time.strftime('%H:%M:%S')} статус: {shown}")
            last_status = shown
        if status is not None and not prepared:               # контракт появился, но настройки ещё не сделаны
            prepared = prepare_position_settings(exchange, ccxt_symbol)  # пробуем подготовиться заранее
        if status in LIVE_STATUSES:                           # торги открыты!
            return prepared                                   # выходим из цикла и сообщаем, готовы ли настройки
        if time.time() - started > MAX_WAIT_HOURS * 3600:     # ждём слишком долго
            sys.exit("Время ожидания вышло, торги так и не открылись")
        time.sleep(POLL_SECONDS)                              # короткая пауза перед следующим вопросом


# ------------------------------------------------------------------
# 5. РАСЧЁТ РАЗМЕРА ПОЗИЦИИ И СТОПА
# ------------------------------------------------------------------

def get_current_price(exchange, ccxt_symbol):
    """Берём текущую цену. В первые секунды тикер может быть пустым, тогда смотрим стакан."""
    ticker = exchange.fetch_ticker(ccxt_symbol)              # последний тикер
    price = ticker.get("bid") or ticker.get("last")          # для шорта важна цена покупателей (bid)
    if not price:                                            # тикер ещё пустой
        book = exchange.fetch_order_book(ccxt_symbol, 5)     # берём 5 уровней стакана
        if book["bids"]:                                     # есть покупатели
            price = book["bids"][0][0]                       # лучшая цена покупателя
    return price                                             # может быть None, если рынка ещё нет


def build_order(exchange, ccxt_symbol, price):
    """Считаем количество монет, цену стоп-лосса и тейк-профита."""
    market = exchange.market(ccxt_symbol)                    # параметры контракта (шаг цены, минимальный объём)
    notional = MARGIN_USDT * LEVERAGE                        # размер позиции в долларах: $10 * 3 = $30
    contract_size = market.get("contractSize") or 1          # у USDT-M Bitget 1 контракт = 1 монета
    amount = notional / price / contract_size                # сколько монет продаём
    amount = float(exchange.amount_to_precision(ccxt_symbol, amount))  # округляем по правилам биржи
    min_amount = (market.get("limits", {}).get("amount", {}) or {}).get("min")  # минимальный объём
    if min_amount and amount < min_amount:                   # слишком маленький ордер биржа не примет
        raise ValueError(f"Объём {amount} меньше минимального {min_amount}. Увеличьте MARGIN_USDT.")
    sl_price = price * (1 + STOP_LOSS_PCT / 100)             # для шорта стоп ВЫШЕ цены входа
    sl_price = float(exchange.price_to_precision(ccxt_symbol, sl_price))  # округляем по шагу цены
    tp_price = None                                          # тейк-профит по умолчанию не ставим
    if TAKE_PROFIT_PCT > 0:                                  # если пользователь задал тейк-профит
        tp_price = price * (1 - TAKE_PROFIT_PCT / 100)       # для шорта тейк НИЖЕ цены входа
        tp_price = float(exchange.price_to_precision(ccxt_symbol, tp_price))
    return amount, sl_price, tp_price                        # возвращаем рассчитанные значения


# ------------------------------------------------------------------
# 6. ОТКРЫТИЕ ШОРТА
# ------------------------------------------------------------------

SL_ERROR_HINT = re.compile(r"stop|loss|preset|tpsl|trigger", re.I)  # слова, по которым узнаём ошибку именно в стоп-лоссе


def has_short_position(exchange, ccxt_symbol):
    """Проверяем, есть ли уже открытый шорт (нужно после сетевой ошибки, чтобы не открыть дважды)."""
    try:
        positions = exchange.fetch_positions([ccxt_symbol])  # запрашиваем позиции по монете
    except Exception:
        return False                                         # не смогли проверить, считаем что позиции нет
    for p in positions:                                      # перебираем позиции
        if p.get("side") == "short" and (p.get("contracts") or 0) > 0:  # нашли открытый шорт
            return True
    return False


def open_short(exchange, ccxt_symbol, prepared):
    """Открываем рыночный шорт со встроенным стоп-лоссом. Повторяем, пока биржа не примет ордер."""
    deadline = time.time() + FIRE_RETRY_SECONDS              # до какого момента пытаемся
    attach_sl = True                                         # пытаемся прикрепить стоп прямо к ордеру
    while time.time() < deadline:                            # повторяем попытки
        try:
            if not prepared and MODE != "dry":               # если плечо ещё не выставлено
                prepared = prepare_position_settings(exchange, ccxt_symbol)  # пробуем ещё раз
            price = get_current_price(exchange, ccxt_symbol) # текущая цена
            if not price:                                    # цены ещё нет
                time.sleep(0.1)                              # ждём 0.1 сек
                continue                                     # и пробуем снова
            amount, sl_price, tp_price = build_order(exchange, ccxt_symbol, price)  # считаем ордер
            params = {
                "marginMode": "isolated",                    # изолированная маржа
                "hedged": False,                             # режим одной позиции (one-way)
            }
            if attach_sl:                                    # прикрепляем стоп к ордеру
                params["stopLoss"] = {"triggerPrice": sl_price}      # ccxt превратит это в presetStopLossPrice
                if tp_price:                                         # и тейк, если задан
                    params["takeProfit"] = {"triggerPrice": tp_price}  # ccxt превратит это в presetStopSurplusPrice
            print(f"\n>>> ШОРТ {amount} {ccxt_symbol} по рынку (~{price}), плечо {LEVERAGE}x, "
                  f"стоп {sl_price}" + (f", тейк {tp_price}" if tp_price else ""))
            if MODE == "dry":                                # режим проверки
                req = exchange.create_order_request(ccxt_symbol, "market", "sell", amount, None, params)  # собираем запрос без отправки
                print("    [DRY] Ордер НЕ отправлен. Запрос, который ушёл бы на биржу:")
                print(f"    {req}")
                return None                                  # в dry на этом всё
            t0 = time.time()                                 # засекаем время отправки
            order = exchange.create_order(ccxt_symbol, "market", "sell", amount, None, params)  # ОТПРАВЛЯЕМ ОРДЕР
            print(f"    Ордер принят за {(time.time() - t0) * 1000:.0f} мс, id={order.get('id')}")
            if not attach_sl:                                # стоп не прикрепился к ордеру
                place_separate_stop(exchange, ccxt_symbol, amount)  # ставим его отдельно, сразу же
            return order                                     # успех, выходим
        except ccxt.NetworkError as e:                       # сеть подвела: ордер МОГ пройти
            print(f"    Сетевая ошибка: {e}")
            if MODE != "dry" and has_short_position(exchange, ccxt_symbol):  # проверяем, не открылась ли позиция
                print("    Позиция всё-таки открылась. Проверьте стоп-лосс в приложении!")
                return None                                  # второй раз не открываем
        except ValueError as e:                              # наша проверка (слишком маленький объём)
            sys.exit(str(e))                                 # повторять бессмысленно
        except ccxt.InsufficientFunds as e:                  # не хватает денег на фьючерсном счёте
            sys.exit(f"Недостаточно средств на фьючерсном счёте: {e}")
        except ccxt.ExchangeError as e:                      # биржа отказала (часто: торги ещё не начались)
            print(f"    Биржа отказала: {e}")
            if attach_sl and SL_ERROR_HINT.search(str(e)):  # отказ из-за цены стопа (цена успела улететь)
                print("    Похоже, проблема в стоп-лоссе. Открою без него и сразу поставлю стоп отдельно.")
                attach_sl = False                            # следующая попытка без встроенного стопа
        time.sleep(0.1)                                      # маленькая пауза перед повтором
    print("Не удалось открыть позицию за отведённое время.")  # время вышло
    return None


def place_separate_stop(exchange, ccxt_symbol, amount):
    """Запасной план: ставим стоп-лосс на ВСЮ позицию отдельным ордером от реальной цены входа."""
    try:
        entry = None                                         # цена входа
        for p in exchange.fetch_positions([ccxt_symbol]):    # ищем нашу позицию
            if p.get("side") == "short":
                entry = p.get("entryPrice")                  # средняя цена входа
        if not entry:                                        # позицию не нашли
            raise RuntimeError("не нашёл позицию")
        sl_price = float(exchange.price_to_precision(ccxt_symbol, entry * (1 + STOP_LOSS_PCT / 100)))  # стоп от реального входа
        exchange.create_order(ccxt_symbol, "market", "buy", amount, None, {
            "stopLossPrice": sl_price,                       # стоп на позицию (Bitget: pos_loss)
            "marginMode": "isolated",
            "hedged": False,
        })
        print(f"    Стоп-лосс поставлен отдельно на {sl_price}")
    except Exception as e:                                   # не получилось поставить стоп
        print(f"    !!! СТОП НЕ ПОСТАВЛЕН ({e}). Закрываю позицию, чтобы не остаться без защиты.")
        exchange.create_order(ccxt_symbol, "market", "buy", amount, None, {"reduceOnly": True, "marginMode": "isolated"})  # аварийно закрываем


def show_position(exchange, ccxt_symbol):
    """Показываем открытую позицию: вход, ликвидация, нереализованная прибыль."""
    time.sleep(0.5)                                          # даём бирже полсекунды обновить данные
    for p in exchange.fetch_positions([ccxt_symbol]):        # запрашиваем позиции
        if (p.get("contracts") or 0) > 0:                    # позиция открыта
            print(f"\nПозиция: {p.get('side')} {p.get('contracts')} | вход {p.get('entryPrice')} | "
                  f"ликвидация {p.get('liquidationPrice')} | PnL {p.get('unrealizedPnl')}")
    print("Проверьте в приложении Bitget, что стоп-лосс виден у позиции (TP/SL).")


# ------------------------------------------------------------------
# 7. ЗАПУСК
# ------------------------------------------------------------------

def main():
    check_settings()                                         # проверяем настройки
    print(f"Режим: {MODE.upper()} | маржа ${MARGIN_USDT} | плечо {LEVERAGE}x | "
          f"позиция ~${MARGIN_USDT * LEVERAGE} | стоп +{STOP_LOSS_PCT}%")
    if MODE == "live":                                       # реальные деньги: просим подтверждение
        if input("Торговля РЕАЛЬНЫМИ деньгами. Введите YES для продолжения: ").strip() != "YES":
            sys.exit("Отменено")
    text = sys.argv[1] if len(sys.argv) > 1 else input("Монета (например DOGS/USDT): ")  # монету можно передать в командной строке
    base, exchange_id, ccxt_symbol = parse_symbol(text)      # разбираем название
    exchange = make_exchange()                               # подключаемся к Bitget
    prepared = wait_for_listing(exchange, exchange_id, ccxt_symbol)  # ждём открытия торгов
    order = open_short(exchange, ccxt_symbol, prepared)      # открываем шорт
    if order and MODE != "dry":                              # если ордер реально отправлен
        show_position(exchange, ccxt_symbol)                 # показываем позицию


if __name__ == "__main__":       # этот блок выполняется, только если файл запущен напрямую
    try:
        main()                   # запускаем бота
    except KeyboardInterrupt:    # нажали Ctrl+C
        print("\nОстановлено пользователем")
    except SystemExit as e:      # бот остановился с сообщением
        if isinstance(e.code, str):
            print(e.code)            # показываем причину
    finally:
        if getattr(sys, "frozen", False):          # запущено двойным щелчком по .exe
            try:
                input("\nНажмите Enter, чтобы закрыть окно")  # чтобы окно не закрылось сразу
            except EOFError:
                pass
