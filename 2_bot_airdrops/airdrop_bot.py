# -*- coding: utf-8 -*-
"""
Бот №1: оповещения ТОЛЬКО об аирдропах и клейме токенов в ваш Telegram-канал.

Источники (только официальные):
  1. Bitget, официальные анонсы через публичный API биржи (бесплатно, ключ не нужен).
     Это главный источник: вы торгуете на Bitget, и время раздачи/листинга там указано точно.
  2. CoinMarketCap Airdrops API (по желанию). ВНИМАНИЕ: этот раздел API платный
     (тариф Startup и выше). На бесплатном ключе CMC он не работает.

Каждый пост: имя токена, дата и время раздачи в вашем часовом поясе, ссылка на источник,
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
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")                        # ID канала, например -1001234567890
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))                    # ваш часовой пояс
CHECK_MINUTES = float(os.getenv("CHECK_MINUTES", "10"))                  # как часто проверять источники
LOOKBACK_HOURS = float(os.getenv("LOOKBACK_HOURS", "24"))               # при первом запуске брать анонсы не старше N часов
CMC_API_KEY = os.getenv("CMC_API_KEY", "")                               # ключ CoinMarketCap (платный тариф), можно оставить пустым
BITGET_ANN_TYPES = [t.strip() for t in os.getenv("BITGET_ANN_TYPES", "coin_listings,latest_news").split(",") if t.strip()]

# Слова, по которым анонс считается аирдропом/клеймом (в заголовке или описании)
INCLUDE = re.compile(os.getenv("INCLUDE_REGEX", r"airdrop|claim|token distribution|distribut(e|ion) of|hodler"), re.I)
# Слова, по которым анонс отбрасываем (конкурсы, акции, розыгрыши - это не раздача токена)
EXCLUDE = re.compile(os.getenv("EXCLUDE_REGEX", r"competition|carnival|giveaway|lucky draw|trading challenge|campaign|delist"), re.I)

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
    return sorted(found)                                         # по порядку появления в тексте


def pick_claim_time(text):
    """Выбираем время, которое стоит рядом со словами про раздачу/клейм. Если таких нет, берём первое."""
    times = find_event_times(text)                               # все найденные времена
    if not times:
        return None                                              # времени в тексте нет
    keys = [m.start() for m in re.finditer(r"airdrop|claim|distribut|credited|snapshot", text, re.I)]  # где стоят ключевые слова
    if keys:
        # берём время, ближайшее к ключевому слову
        return min(times, key=lambda t: min(abs(t[0] - k) for k in keys))[1]
    return times[0][1]                                           # иначе первое время


def find_symbol(text):
    """Ищем тикер монеты: обычно он в скобках, например 'Dogs (DOGS)'."""
    m = re.search(r"\(([A-Z0-9]{2,15})\)", text)                 # тикер в скобках
    if m and m[1] not in ("UTC", "USDT", "USDC", "BTC", "ETH", "BGB"):  # отбрасываем то, что не является новой монетой
        return m[1]
    return None


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
            title = a.get("annTitle") or ""                      # заголовок
            desc = a.get("annDesc") or ""                        # краткое описание
            text = f"{title} {desc}"
            if not INCLUDE.search(text) or EXCLUDE.search(text):  # СТРОГИЙ фильтр: только аирдроп/клейм
                continue
            url = a.get("annUrl") or ""                          # ссылка на анонс
            published = datetime.fromtimestamp(int(a.get("cTime", 0)) / 1000, tz=timezone.utc)  # когда опубликован
            events.append({
                "id": f"bitget:{a.get('annId') or url}",         # уникальный ID для защиты от повторов
                "source": "Bitget",
                "title": title,
                "symbol": find_symbol(text),
                "name": None,
                "url": url,
                "published": published,
                "text": text,                                    # по нему позже ищем время
                "logo": None,
            })
    return events


# ------------------------------------------------------------------
# ИСТОЧНИК 2: COINMARKETCAP AIRDROPS (ПЛАТНЫЙ ТАРИФ, ПО ЖЕЛАНИЮ)
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
# КАРТИНКА: ЛОГОТИП ИЛИ КАРТОЧКА
# ------------------------------------------------------------------

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


def load_font(size):
    """Шрифт для карточки. Если DejaVu не найден, берём встроенный."""
    for path in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "C:/Windows/Fonts/arialbd.ttf", "/Library/Fonts/Arial Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def make_card(symbol, when_text, logo_bytes=None):
    """Рисуем картинку 1080x608: логотип (если есть), тикер и время раздачи."""
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
    d.text((x, 150), "AIRDROP / CLAIM", font=load_font(48), fill=(0, 200, 170))
    d.text((x, 230), symbol or "TOKEN", font=load_font(110), fill=(255, 255, 255))
    d.text((x, 390), when_text, font=load_font(40), fill=(200, 210, 225))
    buf = io.BytesIO()
    img.save(buf, "PNG")                                         # сохраняем в память
    return buf.getvalue()


# ------------------------------------------------------------------
# ОТПРАВКА В TELEGRAM
# ------------------------------------------------------------------

def format_post(ev, event_time):
    """Собираем текст поста (HTML-разметка Telegram)."""
    name = ev["symbol"] or "?"                                   # тикер
    if ev.get("name"):
        name += f" ({ev['name']})"                               # полное имя, если известно
    lines = [f"🪂 <b>АИРДРОП / КЛЕЙМ: {escape(ev['symbol'] or ev['title'][:40])}</b>", ""]
    lines.append(f"🪙 <b>Токен:</b> {escape(name)}")
    if event_time:                                               # нашли точное время
        local = event_time.astimezone(TZ)                        # переводим в ваш пояс
        lines.append(f"📅 <b>Дата и время:</b> {local:%d.%m.%Y %H:%M} ({TZ.key})")
    else:                                                        # времени в анонсе нет
        lines.append("📅 <b>Дата и время:</b> не указаны в анонсе, смотрите по ссылке")
    lines.append(f"📰 {escape(ev['title'][:200])}")
    lines.append(f"🔗 <a href=\"{escape(ev['url'])}\">Источник: {ev['source']}</a>")
    return "\n".join(lines)


def send_photo(photo_bytes, caption):
    """Отправляем картинку с подписью в канал."""
    r = HTTP.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                  data={"chat_id": CHANNEL_ID, "caption": caption[:1024], "parse_mode": "HTML"},  # подпись не длиннее 1024 символов
                  files={"photo": ("airdrop.png", photo_bytes)}, timeout=30)
    if not r.json().get("ok"):                                   # Telegram вернул ошибку
        raise RuntimeError(r.text)


def post_event(ev, dry=False):
    """Готовим картинку и пост, отправляем."""
    event_time = ev.get("event_time")                            # у CMC время уже есть
    if event_time is None and ev["text"]:                        # у Bitget ищем время в тексте
        event_time = pick_claim_time(ev["text"])
        if event_time is None and ev["url"]:                     # в кратком описании нет — читаем полный анонс
            event_time = pick_claim_time(page_text(ev["url"]))
    logo_url = ev.get("logo") or coingecko_logo(ev["symbol"])    # ссылка на логотип
    logo_bytes = None
    if logo_url:
        try:
            logo_bytes = HTTP.get(logo_url, timeout=10).content  # скачиваем логотип
        except requests.RequestException:
            pass
    when = f"{event_time.astimezone(TZ):%d.%m.%Y %H:%M} {TZ.key}" if event_time else "time: see announcement"
    photo = make_card(ev["symbol"], when, logo_bytes)            # картинка: логотип + инфографика
    caption = format_post(ev, event_time)
    if dry:                                                      # режим проверки: печатаем, не отправляем
        print(caption, "\n---")
        return photo
    send_photo(photo, caption)
    return photo


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


def run_once(sent, dry=False):
    """Одна проверка всех источников."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)  # старые анонсы не шлём
    events = fetch_bitget() + fetch_cmc()                        # собираем события из всех источников
    unique = {e["id"]: e for e in events}                        # один анонс может быть в двух разделах, убираем дубли
    new = [e for e in unique.values() if e["id"] not in sent and e["published"] >= cutoff]
    for ev in sorted(new, key=lambda e: e["published"]):         # от старых к новым
        try:
            post_event(ev, dry=dry)
            if not dry:                                          # в режиме проверки ничего не запоминаем
                sent.add(ev["id"])                               # запоминаем, чтобы не отправить второй раз
                save_sent(sent)
            time.sleep(1)                                        # не спамим Telegram
        except Exception as e:
            print(f"Не удалось отправить {ev['id']}: {e}")
    print(f"{datetime.now(TZ):%H:%M} проверено, новых: {len(new)}")


SAMPLE_EVENT = {                                                 # пример события для проверки (--test)
    "id": "test", "source": "Bitget", "symbol": "DOGS", "name": "Dogs", "logo": None,
    "title": "TEST: Dogs (DOGS) airdrop claim opens on October 5, 2026, 12:00 (UTC)",
    "url": "https://www.bitget.com/support/sections/5955813039257", "published": datetime.now(timezone.utc),
    "text": "TEST: Dogs (DOGS) airdrop claim opens on October 5, 2026, 12:00 (UTC)",
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
    finally:
        if getattr(sys, "frozen", False):          # запущено двойным щелчком по .exe
            try:
                input("\nНажмите Enter, чтобы закрыть окно")  # чтобы окно не закрылось сразу
            except EOFError:
                pass
