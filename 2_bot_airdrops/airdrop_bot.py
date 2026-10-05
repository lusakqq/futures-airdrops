# -*- coding: utf-8 -*-
"""
Бот №1: оповещения об аирдропах, клейме и новых листингах в ваш Telegram-канал.

Источники (только официальные):
  1. Bitget, официальные анонсы через публичный API биржи (бесплатно, ключ не нужен).
     Это главный источник: вы торгуете на Bitget, и время раздачи/листинга там указано точно.
  2. Официальные анонсы Binance, Bybit, OKX и KuCoin (тоже бесплатно, без ключей).
  3. CoinMarketCap Airdrops API (по желанию). ВНИМАНИЕ: этот раздел API платный
     (тариф Startup и выше). На бесплатном ключе CMC он не работает.

Каждый пост: биржа, имя токена, дата и время раздачи в вашем часовом поясе, ссылка на источник,
и картинка: логотип монеты (с CoinGecko) или сгенерированная карточка-инфографика.
"""

import io                                   # работа с картинкой в памяти
import json                                 # сохранение списка уже отправленных постов
import os                                   # чтение настроек
import re                                   # поиск дат, времени и тикеров в тексте
import sys                                  # аргументы командной строки
import time                                 # паузы между проверками
from datetime import datetime, timedelta, timezone  # работа с датами
from html import escape, unescape           # безопасный текст для Telegram и очистка HTML
from zoneinfo import ZoneInfo               # часовые пояса

import requests                             # HTTP-запросы
from dotenv import load_dotenv              # настройки из файла .env
from PIL import Image, ImageDraw, ImageFont  # рисование картинки-карточки


# ------------------------------------------------------------------
# НАСТРОЙКИ
# ------------------------------------------------------------------

# папка программы: рядом с .exe (если собрано в exe) или рядом со скриптом
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))  # читаем файл .env из этой папки

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")                     # токен от @BotFather
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")                        # ID канала или людей через запятую: -1001234567890,12345678
CHAT_IDS = [c.strip() for c in CHANNEL_ID.split(",") if c.strip()]       # всем этим получателям уходит каждый пост
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Kyiv"))                    # ваш часовой пояс
CHECK_MINUTES = float(os.getenv("CHECK_MINUTES", "10"))                  # как часто проверять источники
LOOKBACK_HOURS = float(os.getenv("LOOKBACK_HOURS", "24"))               # при первом запуске брать анонсы не старше N часов
CMC_API_KEY = os.getenv("CMC_API_KEY", "")                               # ключ CoinMarketCap (платный тариф), можно оставить пустым
BITGET_ANN_TYPES = [t.strip() for t in os.getenv("BITGET_ANN_TYPES", "coin_listings,latest_news").split(",") if t.strip()]

# Слова, по которым анонс считается аирдропом/клеймом (в заголовке или описании)
INCLUDE = re.compile(os.getenv("INCLUDE_REGEX", r"airdrop|claim|token distribution|distribut(e|ion) of|hodler|megadrop|launchpool|launchpad"), re.I)
# Слова, по которым анонс считается новым листингом (LISTINGS=0 в .env выключает листинги)
LISTING = re.compile(os.getenv("LISTING_REGEX", r"will list|to list|listed on|new listing|will launch|launched for|lists |pre-market|perpetual contract"), re.I)
LISTINGS_ON = os.getenv("LISTINGS", "1") != "0"
# Слова, по которым анонс отбрасываем (конкурсы, акции, розыгрыши - это не раздача токена)
EXCLUDE = re.compile(os.getenv("EXCLUDE_REGEX", r"competition|carnival|giveaway|lucky draw|trading challenge|campaign|delist|cashback|quiz|adjust|risk limit|maintenance|"
                                       r"funding rate|tradfi|stock|pre-ipo|tokenized|collateral|quarterly|delivery|on earn|margin will add|extension"), re.I)

STATE_FILE = os.path.join(BASE_DIR, "sent.json")  # файл с уже отправленными постами
HTTP = requests.Session()                                                # одно соединение на все запросы (быстрее)
HTTP.headers["User-Agent"] = "airdrop-alert-bot/1.0"                     # представляемся


# ------------------------------------------------------------------
# ПОИСК ДАТЫ И ВРЕМЕНИ В ТЕКСТЕ АНОНСА
# ------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}  # названия месяцев

# Вариант 1: 2026-10-05 12:00 (UTC+8)
RE_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T,]+(\d{1,2}):(\d{2})(?::\d{2})?\s*\(?\s*UTC\s*([+-]\s*\d{1,2})?", re.I)
# Вариант 2: October 5, 2026, 12:00 PM (UTC+8)  или  Oct 5, 2026 at 12:00 (UTC)
RE_WORDS = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4}),?\s*(?:at\s*)?"
                      r"(\d{1,2}):(\d{2})\s*(am|pm)?\s*\(?\s*UTC\s*([+-]\s*\d{1,2})?", re.I)
# Вариант 3: 12:00 (UTC+8) on October 5, 2026
RE_TIME_FIRST = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)?\s*\(?\s*UTC\s*([+-]\s*\d{1,2})?\)?,?\s*(?:on\s*)?"
                           r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I)

# Вариант 4: 12:00 on October 5, 2026 (UTC)  (так пишет KuCoin)
RE_TIME_DATE_UTC = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)?\s*(?:on\s*)?"
                              r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})"
                              r"\s*\(?\s*UTC\s*([+-]\s*\d{1,2})?", re.I)


def _to_utc(y, mo, d, h, mi, ampm, offset):
    """Собираем дату из кусочков и переводим в UTC."""
    h = int(h)                                                   # часы
    if ampm:                                                     # формат 12 часов
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)         # 12 AM = 0, 1 PM = 13
    off = int(offset.replace(" ", "")) if offset else 0          # смещение часового пояса из анонса (UTC+8 -> 8)
    local = datetime(int(y), int(mo), int(d), h, int(mi), tzinfo=timezone(timedelta(hours=off)))  # время в поясе анонса
    return local.astimezone(timezone.utc)                        # переводим в UTC


def find_event_times(text):
    """Находим ВСЕ даты со временем в тексте. Возвращаем список (позиция в тексте, время UTC)."""
    found = []
    for m in RE_ISO.finditer(text):                              # формат 2026-10-05 12:00
        try:
            found.append((m.start(), _to_utc(m[1], m[2], m[3], m[4], m[5], None, m[6])))
        except ValueError:
            pass                                                 # некорректная дата, пропускаем
    for m in RE_WORDS.finditer(text):                            # формат October 5, 2026, 12:00
        try:
            found.append((m.start(), _to_utc(m[3], MONTHS[m[1][:3].lower()], m[2], m[4], m[5], m[6], m[7])))
        except ValueError:
            pass
    for m in RE_TIME_FIRST.finditer(text):                       # формат 12:00 (UTC) on October 5, 2026
        try:
            found.append((m.start(), _to_utc(m[7], MONTHS[m[5][:3].lower()], m[6], m[1], m[2], m[3], m[4])))
        except ValueError:
            pass
    for m in RE_TIME_DATE_UTC.finditer(text):                    # формат 12:00 on October 5, 2026 (UTC)
        try:
            found.append((m.start(), _to_utc(m[6], MONTHS[m[4][:3].lower()], m[5], m[1], m[2], m[3], m[7])))
        except ValueError:
            pass
    return sorted(found)                                         # по порядку появления в тексте


def pick_claim_time(text):
    """Выбираем время, которое стоит рядом со словами про раздачу/клейм. Если таких нет, берём первое."""
    times = find_event_times(text)                               # все найденные времена
    if not times:
        return None                                              # времени в тексте нет
    keys = [m.start() for m in re.finditer(r"airdrop|claim|distribut|credited|snapshot|trading|list|open", text, re.I)]  # где стоят ключевые слова
    if keys:
        # берём время, ближайшее к ключевому слову
        return min(times, key=lambda t: min(abs(t[0] - k) for k in keys))[1]
    return times[0][1]                                           # иначе первое время


def find_symbol(text):
    """Ищем тикер монеты: обычно он в скобках, например 'Dogs (DOGS)'."""
    skip = ("UTC", "USDT", "USDC", "USD", "EUR", "BTC", "ETH", "BGB", "BNB", "OKB", "EEA")  # это не новая монета
    for pattern in (r"\(([A-Z0-9]{2,15})\)",                   # тикер в скобках: Dogs (DOGS)
                    r"\b([A-Z0-9]{2,15})/(?:USDT|USDC|USD)\b",  # торговая пара: SLX/USDT
                    r"\b([A-Z0-9]{2,15})USDT\b"):                # фьючерс: CTUSDT
        for m in re.finditer(pattern, text):
            if m[1] not in skip:
                return m[1]
    return None


def classify(text):
    """Что за событие: 'airdrop', 'listing' или None (не наше)."""
    if EXCLUDE.search(text):                                     # конкурсы, акции, делистинг
        return None
    if INCLUDE.search(text):
        return "airdrop"
    if LISTINGS_ON and LISTING.search(text):
        return "listing"
    return None


def make_event(source, uid, title, desc, url, published):
    """Собираем событие в общем виде. Возвращаем None, если анонс не про аирдроп/листинг."""
    text = f"{title} {desc}"
    kind = classify(text)
    symbol = find_symbol(text)
    if not kind or not symbol:                                   # без тикера — это не новая монета, а акция/конкурс
        return None
    return {
        "id": f"{source.lower()}:{uid or url}",                  # уникальный ID для защиты от повторов
        "source": source,                                        # биржа
        "kind": kind,
        "title": title,
        "symbol": symbol,
        "name": None,
        "url": url,
        "published": published,
        "text": text,                                            # по нему позже ищем время
        "logo": None,
    }


def ms_to_dt(ms):
    """Миллисекунды с 1970 года -> дата UTC."""
    return datetime.fromtimestamp(int(ms or 0) / 1000, tz=timezone.utc)


# ------------------------------------------------------------------
# ИСТОЧНИК 1: ОФИЦИАЛЬНЫЕ АНОНСЫ BITGET
# ------------------------------------------------------------------

def page_text(url):
    """Пробуем скачать полный текст анонса, чтобы найти точное время. Не получилось — не страшно."""
    try:
        r = HTTP.get(url, timeout=10)                            # скачиваем страницу
        if r.status_code != 200:
            return ""
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S | re.I)  # убираем скрипты
        text = re.sub(r"<[^>]+>", " ", text)                     # убираем HTML-теги
        return re.sub(r"\s+", " ", unescape(text))               # схлопываем пробелы
    except requests.RequestException:
        return ""


def fetch_bitget():
    """Забираем свежие анонсы Bitget и оставляем только аирдропы/клейм."""
    events = []
    for ann_type in BITGET_ANN_TYPES:                            # по каждому разделу анонсов
        try:
            r = HTTP.get("https://api.bitget.com/api/v2/public/annoucements",  # так (с опечаткой) называется адрес в API Bitget
                         params={"annType": ann_type, "language": "en_US"}, timeout=10)
            data = r.json().get("data") or []                    # список анонсов
        except (requests.RequestException, ValueError) as e:
            print(f"Bitget ({ann_type}): ошибка {e}")
            continue
        for a in data:                                           # каждый анонс
            events.append(make_event("Bitget", a.get("annId"), a.get("annTitle") or "", a.get("annDesc") or "",
                                     a.get("annUrl") or "", ms_to_dt(a.get("cTime"))))
    return [e for e in events if e]


# ------------------------------------------------------------------
# ИСТОЧНИК 2: ОФИЦИАЛЬНЫЕ АНОНСЫ BINANCE, BYBIT, OKX, KUCOIN
# ------------------------------------------------------------------

def get_json(name, url, params):
    """GET-запрос к бирже. При ошибке печатаем её и возвращаем None."""
    try:
        return HTTP.get(url, params=params, timeout=10).json()
    except (requests.RequestException, ValueError) as e:
        print(f"{name}: ошибка {e}")
        return None


def fetch_binance():
    """Binance: новые листинги (48), аирдропы и HODLer (128), акции (93)."""
    events = []
    for catalog in (48, 128, 93):
        j = get_json("Binance", "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
                     {"type": 1, "catalogId": catalog, "pageNo": 1, "pageSize": 20})
        try:
            articles = j["data"]["catalogs"][0]["articles"]
        except (TypeError, KeyError, IndexError):
            continue
        for a in articles:
            ev = make_event("Binance", a.get("code"), a.get("title") or "", "",
                            f"https://www.binance.com/en/support/announcement/{a.get('code')}",
                            ms_to_dt(a.get("releaseDate")))
            if ev:
                ev["code"] = a.get("code")                       # по нему берём полный текст анонса
            events.append(ev)
    return [e for e in events if e]


def fetch_bybit():
    """Bybit: все свежие анонсы."""
    j = get_json("Bybit", "https://api.bybit.com/v5/announcements/index", {"locale": "en-US", "limit": 30})
    items = ((j or {}).get("result") or {}).get("list") or []
    events = [make_event("Bybit", a.get("url"), a.get("title") or "", a.get("description") or "",
                         a.get("url") or "", ms_to_dt(a.get("publishTime") or a.get("dateTimestamp")))
              for a in items]
    return [e for e in events if e]


def fetch_okx():
    """OKX: новые листинги и раздел акций (там бывают аирдропы)."""
    events = []
    for ann_type in ("announcements-new-listings", "announcements-latest-events"):
        j = get_json("OKX", "https://www.okx.com/api/v5/support/announcements", {"annType": ann_type})
        try:
            items = j["data"][0]["details"]
        except (TypeError, KeyError, IndexError):
            continue
        for a in items:
            events.append(make_event("OKX", a.get("url"), a.get("title") or "", "", a.get("url") or "",
                                     ms_to_dt(a.get("pTime"))))
    return [e for e in events if e]


def fetch_kucoin():
    """KuCoin: новые листинги и последние анонсы."""
    events = []
    for ann_type in ("new-listings", "latest-announcements"):
        j = get_json("KuCoin", "https://api.kucoin.com/api/v3/announcements",
                     {"annType": ann_type, "lang": "en_US", "pageSize": 20})
        items = ((j or {}).get("data") or {}).get("items") or []
        for a in items:
            events.append(make_event("KuCoin", a.get("annId"), a.get("annTitle") or "", a.get("annDesc") or "",
                                     a.get("annUrl") or "", ms_to_dt(a.get("cTime"))))
    return [e for e in events if e]


def fetch_exchanges():
    """Все биржи сразу. Ошибка одной биржи не мешает остальным."""
    events = []
    for fetch in (fetch_bitget, fetch_binance, fetch_bybit, fetch_okx, fetch_kucoin):
        try:
            events += fetch()
        except Exception as e:
            print(f"{fetch.__name__}: ошибка {e}")
    return events


# ------------------------------------------------------------------
# ИСТОЧНИК 3: COINMARKETCAP AIRDROPS (ПЛАТНЫЙ ТАРИФ, ПО ЖЕЛАНИЮ)
# ------------------------------------------------------------------

def fetch_cmc():
    """Аирдропы CoinMarketCap. Работает, только если в .env указан ключ платного тарифа."""
    if not CMC_API_KEY:                                          # ключа нет — источник выключен
        return []
    try:
        r = HTTP.get("https://pro-api.coinmarketcap.com/v1/cryptocurrency/airdrops",
                     headers={"X-CMC_PRO_API_KEY": CMC_API_KEY},
                     params={"status": "UPCOMING", "limit": 50}, timeout=15)
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"CMC: ошибка {e}")
        return []
    if r.status_code != 200:                                     # например, тариф не позволяет
        print(f"CMC: {body.get('status', {}).get('error_message')}")
        return []
    events = []
    for a in body.get("data") or []:                             # каждый аирдроп
        coin = a.get("coin") or {}                               # монета
        start = a.get("start_date")                              # дата начала раздачи
        start_utc = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None
        events.append({
            "id": f"cmc:{a.get('id')}",
            "source": "CoinMarketCap",
            "kind": "airdrop",
            "title": a.get("project_name") or coin.get("name") or "",
            "symbol": coin.get("symbol"),
            "name": coin.get("name"),
            "url": a.get("link") or f"https://coinmarketcap.com/airdrop/",
            "published": datetime.now(timezone.utc),
            "event_time": start_utc,
            "text": "",
            "logo": f"https://s2.coinmarketcap.com/static/img/coins/128x128/{coin['id']}.png" if coin.get("id") else None,
        })
    return events


# ------------------------------------------------------------------
# АНАЛИЗ: ПАРА, ФЬЮЧЕРСЫ, РАЗМЕР РАЗДАЧИ, УЧАСТНИКИ, РИСК
# ------------------------------------------------------------------

def num(s):
    """'1,234.5' / '20 million' -> число."""
    m = re.match(r"([\d,]+(?:\.\d+)?)\s*(billion|million|bn|b|m|k)?\b", s.strip(), re.I)
    if not m:
        return None
    value = float(m[1].replace(",", ""))
    mult = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "m": 1e6, "k": 1e3}.get((m[2] or "").lower(), 1)
    return value * mult


def full_text(ev):
    """Полный текст анонса: у Binance через их API, у остальных со страницы."""
    if ev["source"] == "Binance" and ev.get("code"):
        j = get_json("Binance", "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query",
                     {"articleCode": ev["code"]})
        body = ((j or {}).get("data") or {}).get("body") or ""
        parts = re.findall(r'"text":"((?:[^"\\]|\\.)*)"', body)     # все кусочки текста из разметки Binance
        return " ".join(p.encode().decode("unicode_escape", "ignore") if "\\u" in p else p for p in parts)
    return page_text(ev["url"]) if ev.get("url") else ""


def market_of(text):
    """Какой рынок: фьючерсы или спот."""
    if re.search(r"perpetual|futures|contract|swap", text, re.I):
        return "futures"
    return "spot"


def futures_info(symbol):
    """Есть ли бессрочные фьючерсы SYMBOL/USDT на биржах. Цена, фандинг, открытый интерес."""
    pair = f"{symbol}USDT"
    out = []
    try:                                                         # Bitget
        d = (get_json("Bitget", "https://api.bitget.com/api/v2/mix/market/ticker",
                      {"symbol": pair, "productType": "USDT-FUTURES"}) or {}).get("data") or []
        if d:
            d = d[0]
            out.append({"ex": "Bitget", "price": float(d["lastPr"]), "funding": float(d.get("fundingRate") or 0),
                        "oi": float(d.get("holdingAmount") or 0) * float(d["lastPr"]),
                        "change": float(d.get("change24h") or 0) * 100})
    except (KeyError, ValueError, TypeError):
        pass
    try:                                                         # Binance
        p = get_json("Binance", "https://fapi.binance.com/fapi/v1/premiumIndex", {"symbol": pair}) or {}
        if p.get("markPrice"):
            oi = get_json("Binance", "https://fapi.binance.com/fapi/v1/openInterest", {"symbol": pair}) or {}
            price = float(p["markPrice"])
            out.append({"ex": "Binance", "price": price, "funding": float(p.get("lastFundingRate") or 0),
                        "oi": float(oi.get("openInterest") or 0) * price, "change": None})
    except (KeyError, ValueError, TypeError):
        pass
    try:                                                         # Bybit
        lst = ((get_json("Bybit", "https://api.bybit.com/v5/market/tickers",
                         {"category": "linear", "symbol": pair}) or {}).get("result") or {}).get("list") or []
        if lst:
            b = lst[0]
            out.append({"ex": "Bybit", "price": float(b["lastPrice"]), "funding": float(b.get("fundingRate") or 0),
                        "oi": float(b.get("openInterestValue") or 0),
                        "change": float(b.get("price24hPcnt") or 0) * 100})
    except (KeyError, ValueError, TypeError):
        pass
    try:                                                         # OKX
        d = (get_json("OKX", "https://www.okx.com/api/v5/market/ticker", {"instId": f"{symbol}-USDT-SWAP"}) or {}).get("data") or []
        if d:
            out.append({"ex": "OKX", "price": float(d[0]["last"]), "funding": None, "oi": None,
                        "change": (float(d[0]["last"]) / float(d[0]["open24h"]) - 1) * 100 if float(d[0]["open24h"] or 0) else None})
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        pass
    return out


def coin_info(symbol, fut):
    """Данные монеты с CoinGecko: капитализация, монеты в обороте и всего, логотип.
    У разных монет бывает одинаковый тикер, поэтому берём только ту, чья цена совпадает с ценой фьючерса.
    Нет фьючерса для проверки — данные не используем (лучше «нет данных», чем чужие цифры)."""
    if not symbol or not fut:
        return {}
    price = fut[0]["price"]
    try:
        r = HTTP.get("https://api.coingecko.com/api/v3/search", params={"query": symbol}, timeout=10)
        ids = [c["id"] for c in r.json().get("coins", []) if (c.get("symbol") or "").upper() == symbol.upper()][:10]
        if not ids:
            return {}
        coins = HTTP.get("https://api.coingecko.com/api/v3/coins/markets",
                         params={"vs_currency": "usd", "ids": ",".join(ids)}, timeout=10).json()
        good = [c for c in coins if c.get("current_price") and abs(c["current_price"] / price - 1) < 0.25]
        return max(good, key=lambda c: c.get("market_cap") or 0) if good else {}
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, ZeroDivisionError):
        return {}


def supply_from_text(text, symbol):
    """Ищем в анонсе: размер раздачи, монеты в обороте, всего монет, число участников."""
    s = re.escape(symbol or "TOKEN")
    found = {}
    # размер раздачи: "Token Rewards: 30,000,000 AVNT", "airdrop of 20 million DOGS", "share 1,000,000 XYZ"
    m = re.search(r"(?:reward|airdrop|distribut|pool|share|allocat|giveaway)[^.:]{0,60}?[:\s]\s*([\d,]+(?:\.\d+)?\s*(?:billion|million|bn|m|k)?)\s*" + s + r"\b",
                  text, re.I)
    if m:
        found["drop"] = num(m[1])
    m = re.search(r"(?:reward|airdrop)[^.]{0,80}?\(?\s*(\d+(?:\.\d+)?)\s*%\s*of\s*(?:the\s*)?(?:total|max|maximum|genesis)", text, re.I)
    if m:
        found["drop_pct_total"] = float(m[1])
    m = re.search(r"circulating supply[^:]{0,40}:\s*([\d,]+(?:\.\d+)?\s*(?:billion|million|bn|m|k)?)", text, re.I)
    if m:
        found["circ"] = num(m[1])
    m = re.search(r"(?:total|max|maximum)\s*(?:token\s*)?supply[^:]{0,20}:\s*([\d,]+(?:\.\d+)?\s*(?:billion|million|bn|m|k)?)", text, re.I)
    if m:
        found["total"] = num(m[1])
    # участники: "150,000 eligible users", "up to 5,000 winners", "2 million participants"
    m = re.search(r"([\d,]+(?:\.\d+)?\s*(?:million|k)?)\+?\s*(?:eligible\s+|qualified\s+|unique\s+)?(users|participants|winners|holders|addresses|wallets)\b",
                  text, re.I)
    if m and num(m[1]) and num(m[1]) >= 10:
        word = {"users": "участников", "participants": "участников", "winners": "победителей", "holders": "холдеров",
                "addresses": "кошельков", "wallets": "кошельков"}[m[2].lower()]
        found["people"] = f"{m[1].strip()} {word}"
    return found


def fmt_money(x):
    """1234567 -> $1.23M"""
    if x is None:
        return "?"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= limit:
            return f"${x / limit:.2f}{suffix}"
    return f"${x:.4g}"


def risk_rating(kind, fut, info, sup):
    """Оценка риска падения по простым правилам (это НЕ прогноз)."""
    score, why = 0, []
    circ = sup.get("circ") or info.get("circulating_supply")
    total = sup.get("total") or info.get("total_supply") or info.get("max_supply")
    drop = sup.get("drop")
    if drop is None and sup.get("drop_pct_total") and total:
        drop = total * sup["drop_pct_total"] / 100
    if drop and circ:
        pct = drop / circ * 100
        if pct >= 10:
            score += 2; why.append(f"раздача = {pct:.0f}% монет в обороте")
        elif pct >= 3:
            score += 1; why.append(f"раздача = {pct:.1f}% монет в обороте")
    if circ and total:
        share = circ / total * 100
        if share <= 20:
            score += 2; why.append(f"в обороте всего {share:.0f}% монет, впереди анлоки")
        elif share <= 40:
            score += 1; why.append(f"в обороте {share:.0f}% монет")
    if kind == "airdrop":
        score += 1; why.append("получатели аирдропа часто сразу продают")
    if fut:
        rates = [f["funding"] for f in fut if f.get("funding") is not None]
        if rates and max(rates) > 0.0005:
            score += 1; why.append("фандинг высокий, перевес лонгов")
        if rates and min(rates) < -0.0005:
            score -= 1; why.append("фандинг отрицательный, шортов уже много")
        changes = [f["change"] for f in fut if f.get("change") is not None]
        if changes and min(changes) < -30:
            score -= 1; why.append("уже упала больше 30% за сутки")
    level = "🔴 высокий" if score >= 4 else "🟡 средний" if score >= 2 else "🟢 низкий"
    return level, why


def analyze(ev, event_time):
    """Собираем всё, что можно измерить, для блока анализа."""
    text = ev.get("text", "")
    if ev.get("symbol") and ev["source"] != "CoinMarketCap":
        text += " " + full_text(ev)                              # полный анонс: там суммы и участники
    sym = ev.get("symbol")
    fut = futures_info(sym) if sym else []
    info = coin_info(sym, fut)
    sup = supply_from_text(text, sym)
    return {"fut": fut, "info": info, "sup": sup, "market": market_of(ev.get("title", "")),
            "risk": risk_rating(ev.get("kind"), fut, info, sup)}


# ------------------------------------------------------------------
# КАРТИНКА: ЛОГОТИП ИЛИ КАРТОЧКА
# ------------------------------------------------------------------

def load_font(size):
    """Шрифт для карточки. Если DejaVu не найден, берём встроенный."""
    for path in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/arialbd.ttf", "/Library/Fonts/Arial Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def make_card(symbol, when_text, logo_bytes=None, header="AIRDROP / CLAIM", risk=""):
    """Рисуем картинку 1080x608: логотип (если есть), пара, время и риск."""
    img = Image.new("RGB", (1080, 608), (17, 24, 39))            # тёмный фон
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1080, 12], fill=(0, 200, 170))            # цветная полоска сверху
    x = 60                                                       # отступ слева для текста
    if logo_bytes:                                               # есть логотип — вставляем слева
        try:
            logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA").resize((220, 220))
            img.paste(logo, (60, 194), logo)
            x = 320
        except Exception:
            pass
    d.text((x, 130), header, font=load_font(40), fill=(0, 200, 170))
    d.text((x, 200), f"{symbol or 'TOKEN'}/USDT", font=load_font(96), fill=(255, 255, 255))
    d.text((x, 350), when_text, font=load_font(38), fill=(200, 210, 225))
    if risk:
        color = {"high": (239, 68, 68), "mid": (234, 179, 8), "low": (34, 197, 94)}[risk]
        label = {"high": "DROP RISK: HIGH", "mid": "DROP RISK: MEDIUM", "low": "DROP RISK: LOW"}[risk]
        d.text((x, 420), label, font=load_font(38), fill=color)
    buf = io.BytesIO()
    img.save(buf, "PNG")                                         # сохраняем в память
    return buf.getvalue()


# ------------------------------------------------------------------
# ОТПРАВКА В TELEGRAM
# ------------------------------------------------------------------

def format_post(ev, event_time, a):
    """Собираем текст поста (HTML-разметка Telegram)."""
    sym = ev["symbol"]
    head = "🚀 <b>НОВЫЙ ЛИСТИНГ" if ev.get("kind") == "listing" else "🪂 <b>АИРДРОП / КЛЕЙМ"
    lines = [f"{head}: {escape(sym or ev['title'][:40])}</b>", ""]
    lines.append(f"🏦 <b>Биржа:</b> {escape(ev['source'])}")
    if sym:
        market = "фьючерсы (бессрочный контракт)" if a["market"] == "futures" else "спот"
        lines.append(f"💱 <b>Пара:</b> {sym}/USDT, {market}")
    if event_time:
        local = event_time.astimezone(TZ)
        word = "Начало торгов" if ev.get("kind") == "listing" else "Раздача / клейм"
        lines.append(f"📅 <b>{word}:</b> {local:%d.%m.%Y %H:%M} ({TZ.key})")
    else:
        lines.append("📅 <b>Время:</b> не указано, смотрите по ссылке")
    fut = a["fut"]
    if fut:
        lines.append("")
        lines.append("📉 <b>Фьючерсы (можно шортить):</b>")
        for f in fut:
            row = f"• {f['ex']}: ${f['price']:.6g}"
            if f.get("funding") is not None:
                row += f", фандинг {f['funding'] * 100:+.3f}%"
            if f.get("oi"):
                row += f", OI {fmt_money(f['oi'])}"
            if f.get("change") is not None:
                row += f", 24ч {f['change']:+.1f}%"
            lines.append(escape(row))
    elif sym:
        lines.append("📉 <b>Фьючерсы:</b> пока нет ни на одной из 4 бирж")
    info, sup = a["info"], a["sup"]
    lines.append("")
    if info.get("market_cap") or info.get("fully_diluted_valuation"):
        lines.append(f"💰 <b>Капа:</b> {fmt_money(info.get('market_cap'))}, FDV {fmt_money(info.get('fully_diluted_valuation'))}")
    if sup.get("drop"):
        lines.append(f"🎁 <b>Размер раздачи:</b> {sup['drop']:,.0f} {sym or ''}".replace(",", " "))
    elif sup.get("drop_pct_total"):
        lines.append(f"🎁 <b>Размер раздачи:</b> {sup['drop_pct_total']}% от всех монет")
    lines.append(f"👥 <b>Участники:</b> {escape(sup['people']) if sup.get('people') else 'нет данных'}")
    level, why = a["risk"]
    lines.append(f"⚠️ <b>Риск падения:</b> {level}")
    for w in why[:3]:
        lines.append(f"  · {escape(w)}")
    lines.append("<i>Оценка по правилам, не прогноз и не совет.</i>")
    lines.append(f"🔗 <a href=\"{escape(ev['url'])}\">Источник: {ev['source']}</a>")
    return "\n".join(lines)


def tg(method, **kw):
    """Запрос к Telegram. Ошибку показываем."""
    r = HTTP.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", timeout=30, **kw)
    if not r.json().get("ok"):
        raise RuntimeError(r.text)


def send_photo_to(chat_id, photo_bytes, caption):
    """Картинка с подписью одному получателю. Подпись к фото не длиннее 1024 символов, остальное шлём следом текстом."""
    if len(caption) <= 1024:
        tg("sendPhoto", data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
           files={"photo": ("airdrop.png", photo_bytes)})
        return
    cut = caption.rfind("\n\n", 0, 1024)                         # режем по пустой строке
    tg("sendPhoto", data={"chat_id": chat_id, "caption": caption[:cut], "parse_mode": "HTML"},
       files={"photo": ("airdrop.png", photo_bytes)})
    tg("sendMessage", data={"chat_id": chat_id, "text": caption[cut:].strip(), "parse_mode": "HTML",
                            "disable_web_page_preview": True})


def send_photo(photo_bytes, caption):
    """Шлём пост всем получателям. Если кому-то не дошло (не нажал Start), остальным всё равно отправляем."""
    errors = []
    for chat_id in CHAT_IDS:
        try:
            send_photo_to(chat_id, photo_bytes, caption)
        except Exception as e:
            print(f"Получатель {chat_id}: не отправлено ({e}). Он должен нажать Start в боте.")
            errors.append(chat_id)
    if errors and len(errors) == len(CHAT_IDS):                  # не дошло никому — это ошибка
        raise RuntimeError("пост не дошёл ни одному получателю")


def post_event(ev, dry=False):
    """Готовим картинку, анализ и пост, отправляем."""
    event_time = ev.get("event_time")                            # у CMC время уже есть
    if event_time is None and ev["text"]:                        # у остальных ищем время в тексте
        event_time = pick_claim_time(ev["text"])
        if event_time is None and ev["url"]:                     # в кратком описании нет — читаем полный анонс
            event_time = pick_claim_time(full_text(ev))
    a = analyze(ev, event_time)
    logo_url = ev.get("logo") or a["info"].get("image") or coingecko_logo(ev["symbol"])
    logo_bytes = None
    if logo_url:
        try:
            logo_bytes = HTTP.get(logo_url, timeout=10).content  # скачиваем логотип
        except requests.RequestException:
            pass
    when = f"{event_time.astimezone(TZ):%d.%m.%Y %H:%M} {TZ.key}" if event_time else "time: see announcement"
    kind = "NEW LISTING" if ev.get("kind") == "listing" else "AIRDROP / CLAIM"
    risk = {"🔴": "high", "🟡": "mid", "🟢": "low"}[a["risk"][0][:1]]
    photo = make_card(ev["symbol"], when, logo_bytes, f"{kind} · {ev['source'].upper()}", risk)
    caption = format_post(ev, event_time, a)
    if dry:                                                      # режим проверки: печатаем, не отправляем
        print(caption, "\n---")
        return photo
    send_photo(photo, caption)
    return photo


def coingecko_logo(symbol):
    """Ищем логотип монеты на CoinGecko (бесплатно, без ключа)."""
    if not symbol:
        return None
    try:
        r = HTTP.get("https://api.coingecko.com/api/v3/search", params={"query": symbol}, timeout=10)
        for c in r.json().get("coins", []):                      # результаты поиска
            if (c.get("symbol") or "").upper() == symbol.upper():  # точное совпадение тикера
                return c.get("large") or c.get("thumb")          # ссылка на логотип
    except (requests.RequestException, ValueError):
        pass
    return None


# ------------------------------------------------------------------
# ГЛАВНЫЙ ЦИКЛ
# ------------------------------------------------------------------

def load_sent():
    """Читаем список уже отправленных событий."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_sent(sent):
    """Сохраняем список отправленных событий."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(sent), f)


def topic_key(ev):
    """Одна монета, одно событие: листинг CT на 5 биржах = один пост."""
    if not ev.get("symbol"):
        return None
    market = market_of(ev.get("title", "")) if ev.get("kind") == "listing" else "drop"
    return f"topic:{ev['kind']}:{market}:{ev['symbol']}"


def run_once(sent, dry=False):
    """Одна проверка всех источников."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)  # старые анонсы не шлём
    events = fetch_exchanges() + fetch_cmc()                     # собираем события из всех источников
    unique = {e["id"]: e for e in events}                        # один анонс может быть в двух разделах, убираем дубли
    new = [e for e in unique.values() if e["id"] not in sent and e["published"] >= cutoff]
    posted = 0
    for ev in sorted(new, key=lambda e: e["published"]):         # от старых к новым
        key = topic_key(ev)
        if key and key in sent:                                  # эту монету уже присылали с другой биржи
            sent.add(ev["id"])
            continue
        try:
            post_event(ev, dry=dry)
            posted += 1
            sent.add(ev["id"])                                   # запоминаем, чтобы не отправить второй раз
            if key:
                sent.add(key)
            if not dry:                                          # в режиме проверки ничего не сохраняем на диск
                save_sent(sent)
            time.sleep(1)                                        # не спамим Telegram
        except Exception as e:
            print(f"Не удалось отправить {ev['id']}: {e}")
    print(f"{datetime.now(TZ):%H:%M} проверено, отправлено: {posted}")


SAMPLE_EVENT = {                                                 # пример события для проверки (--test)
    "id": "test", "source": "Bitget", "kind": "airdrop", "symbol": "CT", "name": "Concrete", "logo": None,
    "title": "ТЕСТ (пример, цифры раздачи выдуманы): Concrete (CT) airdrop",
    "url": "https://www.bitget.com/support/sections/5955813039257", "published": datetime.now(timezone.utc),
    "text": "TEST: Concrete (CT) airdrop. Token Rewards: 20,000,000 CT (2% of total token supply) for 150,000 eligible users. "
            "Claim opens on October 5, 2026, 12:00 (UTC). Circulating Supply upon Listing: 150,000,000 CT. "
            "Total Token Supply: 1,000,000,000 CT",
}


def main():
    dry = "--dry" in sys.argv                                    # --dry: печатать в консоль, а не в Telegram
    if not dry and not (TELEGRAM_TOKEN and CHANNEL_ID):
        sys.exit("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID в .env (или запустите с --dry)")
    if "--test" in sys.argv:                                     # --test: один пробный пост, чтобы проверить токен и канал
        post_event(SAMPLE_EVENT, dry=dry)
        print("Пробный пост отправлен" if not dry else "")
        return
    sent = load_sent()
    while True:
        run_once(sent, dry=dry)
        if "--once" in sys.argv:                                 # --once: одна проверка и выход
            break
        time.sleep(CHECK_MINUTES * 60)                           # ждём до следующей проверки


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Остановлено")
    except SystemExit as e:      # бот остановился с сообщением
        if isinstance(e.code, str):
            print(e.code)            # показываем причину
            if not getattr(sys, "frozen", False):
                sys.exit(1)          # в облаке GitHub запуск должен стать красным, а не зелёным
    finally:
        if getattr(sys, "frozen", False):          # запущено двойным щелчком по .exe
            try:
                input("\nНажмите Enter, чтобы закрыть окно")  # чтобы окно не закрылось сразу
            except EOFError:
                pass
