import logging
import asyncio
import aiosqlite
import os
import html
import json
import random
import re
import tempfile
import time
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.types import BufferedInputFile, FSInputFile, LabeledPrice, PreCheckoutQuery
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp
from aiohttp import web

# ========================================================
API_TOKEN = os.getenv('BOT_TOKEN')
# ========================================================

if not API_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
DB_NAME = "messages_cache.db"
db_conn: aiosqlite.Connection | None = None

# media_type -> (метод отправки, имя аргумента с файлом)
MEDIA_SENDERS = {
    "photo": (bot.send_photo, "photo"),
    "video": (bot.send_video, "video"),
    "audio": (bot.send_audio, "audio"),
    "document": (bot.send_document, "document"),
    "animation": (bot.send_animation, "animation"),
    "voice": (bot.send_voice, "voice"),
    "video_note": (bot.send_video_note, "video_note"),
    "sticker": (bot.send_sticker, "sticker"),
}
# Эти типы поддерживают подпись (caption)
CAPTION_TYPES = {"photo", "video", "audio", "document", "animation"}

CAPTION_LIMIT = 1024
MAX_TEXT_LEN = 3000


async def init_db():
    global db_conn
    db_conn = await aiosqlite.connect(DB_NAME)

    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER,
            chat_id INTEGER,
            sender_name TEXT,
            username TEXT,
            text_content TEXT,
            file_id TEXT,
            media_type TEXT,
            connection_id TEXT,
            PRIMARY KEY (message_id, chat_id)
        )
    ''')

    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS connections (
            connection_id TEXT PRIMARY KEY,
            owner_user_id INTEGER
        )
    ''')

    # Одноразовые медиа, которые уже были отправлены владельцу (защита от дублей)
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS one_time_sent (
            message_id INTEGER,
            chat_id INTEGER,
            PRIMARY KEY (message_id, chat_id)
        )
    ''')

    # Счёт дуэлей: побед владельца и собеседника в каждом чате
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS duels (
            connection_id TEXT,
            chat_id INTEGER,
            owner_wins INTEGER DEFAULT 0,
            opp_wins INTEGER DEFAULT 0,
            PRIMARY KEY (connection_id, chat_id)
        )
    ''')

    # Служебные значения (например, последняя объявленная версия бота)
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # Заглушённые чаты (.mute): для них не сохраняем сообщения и не шлём уведомления
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS muted_chats (
            connection_id TEXT,
            chat_id INTEGER,
            PRIMARY KEY (connection_id, chat_id)
        )
    ''')

    # Включённые стили оформления для каждого владельца
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS formats (
            connection_id TEXT PRIMARY KEY,
            styles TEXT
        )
    ''')

    # Включённый режим стилизации (.tsundere / .kawaii / .yandere) для каждого владельца
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS style_modes (
            connection_id TEXT PRIMARY KEY,
            mode TEXT
        )
    ''')

    # Игровой баланс (звёзды) для дуэлей .dicep
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0
        )
    ''')

    # Дуэли на звёзды: хранятся в БД, чтобы ставки не терялись при перезапуске
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS stars_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player1 INTEGER,
            player1_name TEXT,
            player2 INTEGER,
            player2_name TEXT,
            bet INTEGER,
            status TEXT,
            created INTEGER
        )
    ''')

    await db_conn.commit()
    logging.info("База данных SQLite успешно запущена!")


@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    # Ссылка-приглашение в дуэль на звёзды: /start join_N
    if command.args and command.args.startswith("join_"):
        return await stars_join_from_start(message, command.args)

    welcome_text = (
        "👋 Привет!\n\n"
        "Я бот для сохранения удалённых сообщений из <b>Telegram</b>.\n\n"
        "1️⃣ Подключите меня: <b>Профиль ➔ Редактировать профиль ➔ внизу «Автоматизация чатов» ➔ напишите Owhedsavebot</b>.\n"
        "2️⃣ Все удалённые сообщения и медиафайлы из ваших чатов будут приходить прямо сюда!\n\n"
        "Список команд: /help\n\n"
        "Мой ТГК @rebfa\n"
        "поддержать: <code>UQDT1kkEirbQOmUlfvc9AX5LaKZUDteRw44uRQg_xOtpAOjc</code>"
    )
    await message.answer(welcome_text, parse_mode="HTML")


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, parse_mode=None)


# ---------- .nsfw: картинка 18+ под спойлером в чат с собеседником ----------

def _first(d, *path):
    """Достаёт значение по пути из словарей/списков, иначе None."""
    for key in path:
        try:
            d = d[key]
        except (KeyError, IndexError, TypeError):
            return None
    return d


def _nekosapi_url(d):
    items = d if isinstance(d, list) else d.get("items") or d.get("data") or []
    return _first(items, 0, "url") or _first(items, 0, "image_url")


# Несколько независимых источников: если один недоступен (DNS, блокировка), берётся следующий
NSFW_SOURCES = [
    ("https://api.waifu.pics/nsfw/waifu", lambda d: d.get("url")),
    ("https://api.waifu.pics/nsfw/neko", lambda d: d.get("url")),
    ("https://api.waifu.im/search?is_nsfw=true&limit=1", lambda d: _first(d, "images", 0, "url")),
    ("https://nekobot.xyz/api/image?type=hentai", lambda d: d.get("message")),
    ("https://nekobot.xyz/api/image?type=hass", lambda d: d.get("message")),
    ("https://purrbot.site/api/img/nsfw/neko/img", lambda d: d.get("link")),
    ("https://purrbot.site/api/img/nsfw/neko/gif", lambda d: d.get("link")),
    ("https://api.nekosapi.com/v4/images/random?rating=explicit&limit=1", _nekosapi_url),
]

NSFW_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SaverBot/1.0)"}


async def fetch_nsfw_url() -> str | None:
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout, headers=NSFW_HEADERS) as session:
        for api_url, extract in random.sample(NSFW_SOURCES, len(NSFW_SOURCES)):
            try:
                async with session.get(api_url) as resp:
                    if resp.status != 200:
                        logging.info(f"[NSFW] {api_url}: HTTP {resp.status}")
                        continue
                    data = await resp.json(content_type=None)
                url = extract(data)
                if isinstance(url, str) and url.startswith("http"):
                    return url
            except Exception as e:
                logging.warning(f"[NSFW] Источник {api_url} не ответил: {e}")
    return None


async def send_nsfw(message: types.Message, owner_id: int):
    conn_id = message.business_connection_id
    chat_id = message.chat.id

    url = await fetch_nsfw_url()
    if not url:
        await try_delete_message(conn_id, message.message_id)
        await notify_owner(owner_id, "🔞 Не удалось получить картинку: все источники недоступны, попробуйте позже.")
        return

    is_anim = url.lower().endswith((".gif", ".mp4"))
    sender = bot.send_animation if is_anim else bot.send_photo
    arg = "animation" if is_anim else "photo"

    try:
        # Сначала по ссылке — Telegram сам скачает файл
        await sender(chat_id=chat_id, business_connection_id=conn_id, has_spoiler=True, **{arg: url})
    except Exception as e:
        logging.info(f"[NSFW] Отправка по ссылке не удалась ({e}), скачиваю файл сам")
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout, headers=NSFW_HEADERS) as session:
                async with session.get(url) as resp:
                    data = await resp.read()
            upload = BufferedInputFile(data, filename="img.gif" if is_anim else "img.jpg")
            await sender(chat_id=chat_id, business_connection_id=conn_id, has_spoiler=True, **{arg: upload})
        except Exception as e2:
            logging.error(f"[NSFW] Не удалось отправить {url}: {e2}")
            await notify_owner(owner_id, f"❌ Не удалось отправить картинку: {e2}")

    await try_delete_message(conn_id, message.message_id)  # убираем сообщение-команду


def get_owner_chat_id(conn_info: types.BusinessConnection) -> int:
    """ID чата, куда слать уведомления (user_chat_id надёжнее user.id)."""
    return getattr(conn_info, "user_chat_id", None) or conn_info.user.id


async def ensure_connection_owner(connection_id: str) -> int | None:
    if not connection_id:
        return None

    async with db_conn.execute(
        "SELECT owner_user_id FROM connections WHERE connection_id = ?",
        (connection_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row and row[0]:
        return row[0]

    try:
        conn_info = await bot.get_business_connection(business_connection_id=connection_id)
        if conn_info and conn_info.user:
            owner_id = get_owner_chat_id(conn_info)
            await db_conn.execute(
                "INSERT OR REPLACE INTO connections (connection_id, owner_user_id) VALUES (?, ?)",
                (connection_id, owner_id)
            )
            await db_conn.commit()
            logging.info(f"[БИЗНЕС] Связь {connection_id} подтянута из API для пользователя {owner_id}")
            return owner_id
    except Exception as e:
        logging.error(f"Не удалось получить бизнес-подключение {connection_id}: {e}")

    return None


def extract_content(message: types.Message):
    """Возвращает (текст, file_id, тип медиа)."""
    content = message.text or message.caption or ""
    file_id = None
    media_type = None

    if message.photo:
        file_id = message.photo[-1].file_id
        media_type = "photo"
        content = content or "[Фотография]"
    elif message.voice:
        file_id = message.voice.file_id
        media_type = "voice"
        content = content or "[Голосовое сообщение]"
    elif message.audio:
        file_id = message.audio.file_id
        media_type = "audio"
        content = content or f"[Аудиозапись: {message.audio.title or 'без названия'}]"
    elif message.video_note:
        file_id = message.video_note.file_id
        media_type = "video_note"
        content = content or "[Видеосообщение (кружочек)]"
    elif message.video:
        file_id = message.video.file_id
        media_type = "video"
        content = content or "[Видео]"
    elif message.animation:
        file_id = message.animation.file_id
        media_type = "animation"
        content = content or "[GIF]"
    elif message.sticker:
        file_id = message.sticker.file_id
        media_type = "sticker"
        content = content or f"[Стикер: {message.sticker.emoji or ''}]"
    elif message.document:
        file_id = message.document.file_id
        media_type = "document"
        content = content or f"[Документ: {message.document.file_name or 'файл'}]"

    return content, file_id, media_type


async def save_message(message: types.Message):
    content, file_id, media_type = extract_content(message)

    sender = message.from_user.full_name if message.from_user else "Собеседник"
    username = (
        f"@{message.from_user.username}"
        if message.from_user and message.from_user.username
        else "@нет_юзернейма"
    )
    conn_id = message.business_connection_id

    if conn_id:
        await ensure_connection_owner(conn_id)

    await db_conn.execute(
        "INSERT OR REPLACE INTO messages "
        "(message_id, chat_id, sender_name, username, text_content, file_id, media_type, connection_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (message.message_id, message.chat.id, sender, username, content, file_id, media_type, conn_id)
    )
    await db_conn.commit()
    logging.info(f"[БАЗА] Сохранено сообщение ID {message.message_id} ({media_type or 'текст'})")


# 1. Подключение/отключение бота к Telegram Business
@dp.business_connection()
async def handle_business_connection(connection: types.BusinessConnection):
    try:
        if connection.is_enabled:
            owner_id = get_owner_chat_id(connection)
            await db_conn.execute(
                "INSERT OR REPLACE INTO connections (connection_id, owner_user_id) VALUES (?, ?)",
                (connection.id, owner_id)
            )
            await db_conn.commit()
            logging.info(f"[БИЗНЕС] Связь {connection.id} закреплена за пользователем {owner_id}")
        else:
            await db_conn.execute("DELETE FROM connections WHERE connection_id = ?", (connection.id,))
            await db_conn.commit()
            logging.info(f"[БИЗНЕС] Связь {connection.id} была удалена.")
    except Exception as e:
        logging.error(f"Ошибка в handle_business_connection: {e}")


# ---------- Режимы стилизации: .tsundere, .kawaii и .yandere ----------

TSUNDERE_PREFIXES = [
    "Х-хмф! Мне вообще-то это совсем не нужно, но",
    "Д-дурак! Ладно, слушай:",
    "Т-ты не подумай, что мне не всё равно, просто",
    "Ц-цк... ну раз уж ты так просишь,",
    "Н-не смотри на меня так! Короче,",
    "Ф-фу, как же ты достал... ладно,",
    "Э-это не потому, что я переживаю! Просто",
    "Т-тебе повезло, что я в хорошем настроении, поэтому",
    "О-отвернись! Я не краснею! Так вот:",
    "Т-ты вообще слушаешь?! Ладно, повторяю:",
    "М-мне нечем больше заняться, вот и говорю:",
    "Х-хватит так пялиться! В общем,",
    "Не думай, что я для тебя старалась, но",
    "У-урод... ну ладно, слушай:",
]
TSUNDERE_SUFFIXES = [
    "— и это совсем не потому, что ты мне нравишься! 😤",
    "Б-бака!",
    "Хмф! Не задавай лишних вопросов!",
    "И не думай, что это ради тебя! 💢",
    "...и вообще, отстань! 😳",
    "— и не вздумай меня благодарить! 😠",
    "Д-дурак, ты всё не так понял! 💢",
    "Т-только не воображай лишнего! 😳",
    "...н-но мне всё равно, понял?! 😤",
    "Хмф, можешь не отвечать! (но лучше ответь...) 🙄",
    "Это просто так совпало, ясно?! 💢",
    "...и не смей смеяться! 😖",
]

KAWAII_PREFIXES = [
    "Ой, ну я такое не умею, но",
    "Ня~ слушай,",
    "Ааа, только не смейся надо мной, но",
    "Э-этоо... ну...",
    "Уву~ а можно я скажу?",
    "Кья~ я так стесняюсь, но",
    "Мяу~ знаешь что?",
    "Ой-ой, я кажется ошиблась, но",
    "Пуф~ только не ругай меня, но",
    "Ня-ня~ можно тебе кое-что сказать?",
    "Ииии, я так волнуюсь, но",
    "Ура-ура~ слушай,",
    "Ну пожалуйста, не злись, но",
    "Хмм~ дай-ка подумаю... ага!",
]
KAWAII_SUFFIXES = [
    "хихи~ 🥺👉👈",
    "ты же не будешь ругаться? 💗",
    "уву~ ✨",
    "ня~ 🎀",
    "я старалась~ 🥺💕",
    "мяу~ 🐱💖",
    "ты ведь не обидишься? 🥹",
    "кья~ 💞",
    "пожалуйста-пожалуйста~ 🌸",
    "ня-ня-ня~ 🍓",
    "я такая молодец, да? ✨🥺",
    "хихи, обнимашки~ 🤗💗",
]

YANDERE_PREFIXES = [
    "Любимый, послушай меня внимательно:",
    "Ты ведь никому не расскажешь... но",
    "Ах, как я рада тебя видеть~ 💕 Кстати,",
    "Я так долго ждала, чтобы сказать:",
    "Не отводи взгляд, я всё вижу. Так вот:",
    "Только между нами, милый:",
    "Ты же знаешь, что я всегда рядом? Так вот,",
    "Тссс... не перебивай меня, дорогой:",
    "Я весь день думала только о тебе, и вот что:",
    "Мы ведь с тобой навсегда, правда? Поэтому",
    "Не волнуйся, я обо всём позабочусь. Слушай:",
    "Ты ведь мой, да? Тогда слушай:",
]
YANDERE_SUFFIXES = [
    "...и ты ведь больше ни с кем так не разговариваешь, правда? 🔪💕",
    "Ты только мой, запомни это~ 💗",
    "Я ведь знаю, где ты сейчас находишься~ 👁️💕",
    "Никому тебя не отдам. Никому. ♥",
    "...а с кем ты вчера переписывался? Шучу~ 🔪",
    "Улыбнись мне, пожалуйста~ ты же не хочешь меня расстроить? 🖤",
    "Я так тебя люблю, что готова на всё~ 💘",
    "Мы будем вместе вечно. ВЕЧНО. 💞",
    "Не заставляй меня ревновать, милый~ 🥀",
    "...я ведь всегда за тобой присматриваю 👁️♥",
]


def _soften_first_letter(text: str) -> str:
    """Делает первую букву строчной, но не трогает аббревиатуры (ОК, ТГ и т.п.)."""
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        return text[0].lower() + text[1:]
    return text


def tsundere_style(text: str) -> str:
    body = _soften_first_letter(text.strip())
    # Заикание на первом слове: "ты" -> "т-ты"
    first, sep, rest = body.partition(" ")
    if first and first[0].isalpha() and random.random() < 0.5:
        body = f"{first[0]}-{first}{sep}{rest}"
    return f"{random.choice(TSUNDERE_PREFIXES)} {body} {random.choice(TSUNDERE_SUFFIXES)}"


def kawaii_style(text: str) -> str:
    body = _soften_first_letter(text.strip())
    # Заменяем завершающую точку на тильду
    if body.endswith("."):
        body = body[:-1] + "~"
    elif body.endswith("!"):
        body = body + "~"
    return f"{random.choice(KAWAII_PREFIXES)} {body} {random.choice(KAWAII_SUFFIXES)}"


def yandere_style(text: str) -> str:
    body = _soften_first_letter(text.strip())
    # Завершающая точка превращается в сердечко
    if body.endswith("."):
        body = body[:-1] + " ♥"
    elif body.endswith("!"):
        body = body + " ♥"
    return f"{random.choice(YANDERE_PREFIXES)} {body} {random.choice(YANDERE_SUFFIXES)}"


STYLE_COMMANDS = {
    ".tsundere": tsundere_style,
    ".kawaii": kawaii_style,
    ".yandere": yandere_style,
}


async def handle_style_command(message: types.Message) -> bool:
    """Если владелец написал '.tsundere текст', '.kawaii текст' или '.yandere текст' — редактируем сообщение."""
    text = message.text
    conn_id = message.business_connection_id
    if not text or not conn_id:
        return False

    parts = text.split(None, 1)
    command = parts[0].lower()
    if command not in STYLE_COMMANDS:
        return False

    owner_id = await ensure_connection_owner(conn_id)
    if not message.from_user or message.from_user.id != owner_id:
        return False  # команды работают только у владельца аккаунта

    if len(parts) < 2:
        await toggle_style_mode(message, owner_id, command)  # без текста — включить/выключить режим
        return True

    new_text = STYLE_COMMANDS[command](parts[1])[:4096]
    try:
        await bot.edit_message_text(
            text=new_text,
            business_connection_id=conn_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
        logging.info(f"[СТИЛЬ] {command} применён к сообщению {message.message_id}")
    except Exception as e:
        logging.error(f"Не удалось изменить сообщение командой {command}: {e}")
    return True


STYLE_MODE_CACHE: dict[str, str | None] = {}


async def get_style_mode(conn_id: str) -> str | None:
    if conn_id in STYLE_MODE_CACHE:
        return STYLE_MODE_CACHE[conn_id]
    async with db_conn.execute(
        "SELECT mode FROM style_modes WHERE connection_id = ?", (conn_id,)
    ) as cursor:
        row = await cursor.fetchone()
    mode = row[0] if row and row[0] in STYLE_COMMANDS else None
    STYLE_MODE_CACHE[conn_id] = mode
    return mode


async def set_style_mode(conn_id: str, mode: str | None):
    STYLE_MODE_CACHE[conn_id] = mode
    if mode:
        await db_conn.execute(
            "INSERT OR REPLACE INTO style_modes (connection_id, mode) VALUES (?, ?)", (conn_id, mode)
        )
    else:
        await db_conn.execute("DELETE FROM style_modes WHERE connection_id = ?", (conn_id,))
    await db_conn.commit()


async def toggle_style_mode(message: types.Message, owner_id: int, command: str):
    """.tsundere / .kawaii / .yandere без текста: включает режим, повторно — выключает. Режимы взаимоисключающие."""
    conn_id = message.business_connection_id
    current = await get_style_mode(conn_id)
    label = command[1:]

    if current == command:
        await set_style_mode(conn_id, None)
        note = f"❌ Режим {label} выключен."
    else:
        await set_style_mode(conn_id, command)
        note = f"✅ Режим {label} включён: все ваши сообщения будут оформляться в этом стиле."
        if current:
            note += f"\n(Режим {current[1:]} выключен.)"
        note += f"\nЧтобы выключить, напишите {command} ещё раз."

    await try_delete_message(conn_id, message.message_id)
    try:
        await bot.send_message(chat_id=owner_id, text=note)
    except Exception as e:
        logging.info(f"[РЕЖИМ] Не удалось отправить подтверждение владельцу: {e}")


async def style_text_for_mode(message: types.Message) -> str | None:
    """Если включён режим, возвращает переписанный текст сообщения владельца, иначе None."""
    conn_id = message.business_connection_id
    text = message.text
    if not conn_id or not text or not message.from_user:
        return None

    mode = await get_style_mode(conn_id)
    if not mode:
        return None

    owner_id = await ensure_connection_owner(conn_id)
    if message.from_user.id != owner_id:
        return None

    stripped = text.strip()
    if len(stripped) < 2 or stripped.startswith((".", "/")):
        return None  # команды и одиночные символы (ходы в игре) не трогаем
    if message.entities:
        return None  # ссылки, упоминания и т.п. сломались бы при переписывании

    return STYLE_COMMANDS[mode](stripped)[:4096]


# ---------- .help и крестики-нолики (.ttt) ----------

HELP_TEXT = (
    "✨ Команды (работают в бизнес-чатах, только у владельца):\n\n"
    ".tsundere — включить/выключить режим цундере (или .tsundere текст — один раз)\n"
    ".kawaii — включить/выключить пикми-режим (или .kawaii текст — один раз)\n"
    ".yandere — включить/выключить режим яндере (или .yandere текст — один раз)\n"
    ".ttt — крестики-нолики с собеседником (ходы цифрами 1–9)\n"
    ".ttt stop — остановить игру\n"
    ".neko — случайная картинка с кошкодевочкой\n"
    ".duel — дуэль с собеседником, победителя решает случай\n"
    ".dice — бросить по кубику: у кого больше, тот победил\n"
    ".text — расшифровать голосовое или кружок (ответом на него)\n"
    ".video ссылка — скачать видео (TikTok, YouTube и др.) и отправить в чат\n"
    ".mute — не давать собеседнику писать (его сообщения удаляются)\n"
    ".unmute — снять запрет\n"
    ".song название или строчка — найти песню\n"
    ".spam число текст — отправить текст несколько раз (до 30)\n"
    ".love — анимация любви\n"
    ".gif — сделать гифку из фото, видео или кружка (ответом на него)\n"
    ".bold — включить/выключить жирный шрифт\n"
    ".italic — включить/выключить курсив\n"
    ".monospace — включить/выключить моноширинный шрифт\n"
    ".underline — включить/выключить подчёркивание\n"
    ".help — этот список\n\n"
    "⭐️ Команды в личке с ботом (дуэль на звёзды):\n\n"
    ".dicep ставка — создать дуэль на кубиках (по умолчанию 50 ⭐️)\n"
    ".bal — игровой баланс\n"
    ".cancel — отменить свои игры без соперника\n"
    ".withdraw — вывод (пока недоступен)"
)

# Активные игры: chat_id -> состояние (хранятся в памяти, при перезапуске бота сбрасываются)
GAMES: dict[tuple, dict] = {}


def game_key(message: types.Message) -> tuple:
    """Ключ партии: (бизнес-подключение владельца, чат). Партии разных владельцев не пересекаются."""
    return (message.business_connection_id, message.chat.id)

WIN_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
MARKS = {"X": "❌", "O": "⭕"}


def check_winner(board: list[str]) -> str | None:
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None


def render_game(game: dict, result: str | None = None, note: str | None = None) -> str:
    rows = []
    for r in range(3):
        row = ""
        for c in range(3):
            i = r * 3 + c
            row += MARKS.get(game["board"][i], NUM_EMOJI[i])
        rows.append(row)

    names = {"X": game["x_name"], "O": game["o_name"]}
    text = (
        f"🎮 Крестики-нолики\n"
        f"{MARKS['X']} {names['X']}  vs  {MARKS['O']} {names['O']}\n\n"
        + "\n".join(rows) + "\n\n"
    )
    if note:
        text += note
    elif result == "draw":
        text += "🤝 Ничья!"
    elif result in MARKS:
        text += f"🏆 Победил {MARKS[result]} {names[result]}!"
    else:
        text += f"Ход: {MARKS[game['turn']]} {names[game['turn']]}\nОтправьте цифру от 1 до 9"
    return text


async def update_board(game: dict, chat_id: int, text: str):
    """Редактирует сообщение с доской; если не вышло — отправляет новое."""
    try:
        await bot.edit_message_text(
            text=text,
            business_connection_id=game["conn_id"],
            chat_id=chat_id,
            message_id=game["msg_id"],
        )
    except Exception as e:
        logging.warning(f"[ИГРА] Не удалось изменить доску ({e}), отправляю новое сообщение")
        sent = await bot.send_message(
            chat_id=chat_id, text=text, business_connection_id=game["conn_id"]
        )
        game["msg_id"] = sent.message_id


async def start_ttt(message: types.Message, owner_id: int):
    chat_id = message.chat.id
    if chat_id == owner_id:
        return  # с самим собой играть нельзя

    chat = message.chat
    opponent_name = getattr(chat, "full_name", None) or chat.first_name or "Собеседник"
    game = {
        "board": [""] * 9,
        "turn": "X",
        "x_id": owner_id,
        "o_id": chat_id,
        "x_name": message.from_user.full_name,
        "o_name": opponent_name,
        "msg_id": message.message_id,  # команда .ttt превращается в доску
        "conn_id": message.business_connection_id,
    }
    GAMES[game_key(message)] = game
    await update_board(game, chat_id, render_game(game))
    logging.info(f"[ИГРА] Начата партия в чате {chat_id}")


async def stop_ttt(message: types.Message):
    game = GAMES.pop(game_key(message), None)
    if game:
        await update_board(game, message.chat.id, render_game(game, note="🛑 Игра остановлена"))
    # Само сообщение-команду убираем, чтобы не мусорило в чате
    await try_delete_message(message.business_connection_id, message.message_id)


async def try_delete_message(conn_id: str, message_id: int) -> bool:
    try:
        await bot.delete_business_messages(business_connection_id=conn_id, message_ids=[message_id])
        return True
    except Exception as e:
        logging.info(f"[УДАЛЕНИЕ СООБЩЕНИЯ] Не удалось удалить {message_id}: {e}")
        return False


async def handle_ttt_move(message: types.Message) -> bool:
    game = GAMES.get(game_key(message))
    if not game or not message.text or message.business_connection_id != game["conn_id"]:
        return False

    move = message.text.strip()
    if len(move) != 1 or move not in "123456789":
        return False

    sender_id = message.from_user.id if message.from_user else None
    if sender_id == game["x_id"]:
        mark = "X"
    elif sender_id == game["o_id"]:
        mark = "O"
    else:
        return False

    idx = int(move) - 1
    if mark != game["turn"] or game["board"][idx]:
        return False  # не ваш ход или клетка занята — обычное сообщение

    game["board"][idx] = mark
    result = check_winner(game["board"])
    game["turn"] = "O" if mark == "X" else "X"

    await update_board(game, message.chat.id, render_game(game, result=result))
    await try_delete_message(message.business_connection_id, message.message_id)

    if result:
        GAMES.pop(game_key(message), None)
    return True


# ---------- Расшифровка (.text) и поиск песен (.song) ----------

# Распознавание речи: любой OpenAI-совместимый сервис (по умолчанию Groq, есть бесплатный лимит)
STT_API_KEY = os.getenv("STT_API_KEY")
STT_API_URL = os.getenv("STT_API_URL", "https://api.groq.com/openai/v1/audio/transcriptions")
STT_MODEL = os.getenv("STT_MODEL", "whisper-large-v3-turbo")
# Токен Genius нужен для поиска песни по строчке из текста (https://genius.com/api-clients)
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN")


async def notify_owner(owner_id: int, text: str):
    """Отправляет результат владельцу в личку с ботом (собеседник его не увидит)."""
    try:
        for i in range(0, len(text), 4000):
            await bot.send_message(chat_id=owner_id, text=text[i:i + 4000])
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение владельцу {owner_id}: {e}")


async def transcribe(file_id: str, media_type: str) -> str:
    if not STT_API_KEY:
        raise RuntimeError("не задана переменная окружения STT_API_KEY")

    tg_file = await bot.get_file(file_id)  # Bot API отдаёт файлы до 20 МБ
    data = (await bot.download_file(tg_file.file_path)).read()

    is_video = media_type == "video_note"
    form = aiohttp.FormData()
    form.add_field(
        "file", data,
        filename="audio.mp4" if is_video else "audio.ogg",
        content_type="video/mp4" if is_video else "audio/ogg",
    )
    form.add_field("model", STT_MODEL)
    form.add_field("response_format", "json")

    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            STT_API_URL, data=form, headers={"Authorization": f"Bearer {STT_API_KEY}"}
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"сервис распознавания ответил {resp.status}: {(await resp.text())[:200]}")
            result = await resp.json(content_type=None)
    return (result.get("text") or "").strip()


async def find_voice_target(message: types.Message):
    """Ищем голосовое/кружок: сначала в сообщении, на которое ответили, затем последнее в чате."""
    reply = message.reply_to_message
    if reply:
        _, file_id, media_type = extract_content(reply)
        if file_id and media_type in ("voice", "video_note"):
            return file_id, media_type

    async with db_conn.execute(
        "SELECT file_id, media_type FROM messages "
        "WHERE chat_id = ? AND media_type IN ('voice', 'video_note') "
        "ORDER BY message_id DESC LIMIT 1",
        (message.chat.id,)
    ) as cursor:
        row = await cursor.fetchone()
    return (row[0], row[1]) if row else None


async def handle_text_command(message: types.Message, owner_id: int):
    conn_id = message.business_connection_id
    chat = message.chat
    chat_name = getattr(chat, "full_name", None) or chat.first_name or "собеседник"

    target = await find_voice_target(message)
    await try_delete_message(conn_id, message.message_id)

    if not target:
        await notify_owner(owner_id, "🎙 Не нашёл голосового или кружка. Ответьте командой .text на нужное сообщение.")
        return

    try:
        text = await transcribe(*target)
    except Exception as e:
        logging.error(f"[TEXT] Ошибка расшифровки: {e}")
        await notify_owner(owner_id, f"❌ Не удалось расшифровать: {e}")
        return

    await notify_owner(owner_id, f"📝 Расшифровка (чат с {chat_name}):\n\n{text or '[речь не распознана]'}")


async def search_genius(query: str) -> list[tuple[str, str, str]]:
    if not GENIUS_TOKEN:
        return []
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.genius.com/search",
                params={"q": query},
                headers={"Authorization": f"Bearer {GENIUS_TOKEN}"},
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
        hits = data.get("response", {}).get("hits", [])[:5]
        return [
            (h["result"]["primary_artist"]["name"], h["result"]["title"], h["result"]["url"])
            for h in hits
        ]
    except Exception as e:
        logging.warning(f"[SONG] Genius не ответил: {e}")
        return []


async def search_deezer(query: str) -> list[dict]:
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.deezer.com/search", params={"q": query, "limit": 5}
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
        return data.get("data", [])[:5]
    except Exception as e:
        logging.warning(f"[SONG] Deezer не ответил: {e}")
        return []


async def handle_song_command(message: types.Message, owner_id: int, args: str):
    conn_id = message.business_connection_id
    query = args.strip()
    reply = message.reply_to_message

    # Без текста команды берём запрос из сообщения, на которое ответили
    if not query and reply:
        query = (reply.text or reply.caption or "").strip()
        if not query:
            _, file_id, media_type = extract_content(reply)
            if file_id and media_type in ("voice", "video_note"):
                try:
                    query = await transcribe(file_id, media_type)  # ищем по произнесённой строчке
                except Exception as e:
                    await try_delete_message(conn_id, message.message_id)
                    await notify_owner(owner_id, f"❌ Не удалось расшифровать голосовое для поиска: {e}")
                    return

    if not query:
        await try_delete_message(conn_id, message.message_id)
        await notify_owner(owner_id, "🎵 Напишите название или строчку из песни: .song название")
        return

    genius, deezer = await asyncio.gather(search_genius(query), search_deezer(query))

    lines = [f"🎵 Поиск: {query[:200]}"]
    if genius:
        lines.append("\n📖 Genius (в том числе по тексту):")
        for i, (artist, title, url) in enumerate(genius, 1):
            lines.append(f"{i}. {artist} — {title}\n{url}")
    if deezer:
        lines.append("\n🎧 Deezer:")
        for i, t in enumerate(deezer, 1):
            lines.append(f"{i}. {t['artist']['name']} — {t['title']}\n{t['link']}")
    if not genius and not deezer:
        lines.append("\nНичего не найдено 😔")
    if not GENIUS_TOKEN:
        logging.info("[SONG] GENIUS_TOKEN не задан — поиск по строчке из текста недоступен")
    result_text = "\n".join(lines)[:4000]

    chat_id = message.chat.id

    # Результат уходит в чат, где написана команда: сообщение-команда превращается в результат
    try:
        await bot.edit_message_text(
            text=result_text, business_connection_id=conn_id,
            chat_id=chat_id, message_id=message.message_id,
        )
    except Exception as e:
        logging.info(f"[SONG] Не удалось изменить сообщение ({e}), отправляю новое")
        try:
            await bot.send_message(chat_id=chat_id, text=result_text, business_connection_id=conn_id)
            await try_delete_message(conn_id, message.message_id)
        except Exception as e2:
            logging.error(f"[SONG] Не удалось отправить результат в чат: {e2}")
            await notify_owner(owner_id, result_text)
            return

    # Отрывок первого трека (30 сек от Deezer) тоже в чат
    if deezer and deezer[0].get("preview"):
        top = deezer[0]
        try:
            await bot.send_audio(
                chat_id=chat_id, business_connection_id=conn_id, audio=top["preview"],
                title=top["title"], performer=top["artist"]["name"],
            )
        except Exception as e:
            logging.info(f"[SONG] Не удалось отправить отрывок: {e}")


# ---------- Запрет собеседнику писать (.mute / .unmute) ----------

MUTE_WARNED: set = set()  # чтобы не повторять предупреждение о правах

# Тексты, которые видит собеседник в чате (можно поменять)
MUTE_NOTICE = "🔇 Вам ограничили возможность писать в этом чате. Ваши сообщения будут удаляться."
UNMUTE_NOTICE = "🔊 Ограничение снято — теперь вы снова можете писать."


async def is_muted(conn_id: str | None, chat_id: int) -> bool:
    if not conn_id:
        return False
    async with db_conn.execute(
        "SELECT 1 FROM muted_chats WHERE connection_id = ? AND chat_id = ?",
        (conn_id, chat_id)
    ) as cursor:
        return await cursor.fetchone() is not None


async def delete_if_muted(message: types.Message) -> bool:
    """В заглушённом чате удаляем всё, что пишет собеседник. Сообщения владельца не трогаем."""
    conn_id = message.business_connection_id
    if not conn_id or not message.from_user:
        return False
    if not await is_muted(conn_id, message.chat.id):
        return False

    owner_id = await ensure_connection_owner(conn_id)
    if message.from_user.id == owner_id:
        return False

    deleted = await try_delete_message(conn_id, message.message_id)
    logging.info(f"[MUTE] Сообщение {message.message_id} из заглушённого чата {message.chat.id}: удалено={deleted}")

    if not deleted and owner_id and (conn_id, message.chat.id) not in MUTE_WARNED:
        MUTE_WARNED.add((conn_id, message.chat.id))
        await notify_owner(
            owner_id,
            "⚠️ Не получилось удалить сообщение заглушённого собеседника. "
            "Откройте Настройки → Telegram Business → Чат-боты и дайте боту право удалять все сообщения."
        )
    return True


async def set_mute(message: types.Message, owner_id: int, muted: bool):
    conn_id = message.business_connection_id
    chat = message.chat
    name = getattr(chat, "full_name", None) or chat.first_name or "собеседник"

    if chat.id == owner_id:
        await try_delete_message(conn_id, message.message_id)
        await notify_owner(owner_id, "🔇 Эту команду нужно писать в чате с собеседником.")
        return

    already = await is_muted(conn_id, chat.id)

    if muted:
        await db_conn.execute(
            "INSERT OR IGNORE INTO muted_chats (connection_id, chat_id) VALUES (?, ?)",
            (conn_id, chat.id)
        )
        note = (
            f"🔇 {name} уже заглушён." if already else
            f"🔇 {name} заглушён: все его сообщения будут удаляться. Снять запрет: .unmute"
        )
    else:
        await db_conn.execute(
            "DELETE FROM muted_chats WHERE connection_id = ? AND chat_id = ?", (conn_id, chat.id)
        )
        MUTE_WARNED.discard((conn_id, chat.id))
        note = (
            f"🔊 {name} снова может писать." if already else
            f"🔊 {name} и так не был заглушён."
        )
    await db_conn.commit()

    # Предупреждаем собеседника прямо в чате: команда превращается в уведомление
    changed = (muted and not already) or (not muted and already)
    if changed:
        ctx = {"conn_id": conn_id, "msg_id": message.message_id}
        await update_board(ctx, chat.id, MUTE_NOTICE if muted else UNMUTE_NOTICE)
        if ctx["msg_id"] != message.message_id:
            await try_delete_message(conn_id, message.message_id)  # правка не вышла — убираем команду
    else:
        await try_delete_message(conn_id, message.message_id)
    await notify_owner(owner_id, note)


# ---------- Скачивание видео по ссылке (.video) ----------

VIDEO_MAX_BYTES = 49 * 1024 * 1024   # лимит Telegram на загрузку файлов ботами — 50 МБ
VIDEO_MAX_DURATION = 15 * 60         # не качаем ролики длиннее 15 минут
_video_semaphore = None


def get_video_semaphore() -> asyncio.Semaphore:
    """Качаем по одному видео за раз, чтобы не перегружать бесплатный сервер."""
    global _video_semaphore
    if _video_semaphore is None:
        _video_semaphore = asyncio.Semaphore(1)
    return _video_semaphore


def _download_video_sync(url: str, tmpdir: str) -> dict:
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("не установлен yt-dlp: добавьте строку yt-dlp в requirements.txt")

    opts = {
        # Только готовые файлы без склейки (на сервере нет ffmpeg), в пределах лимита Telegram
        "format": "b[ext=mp4][filesize<48M]/b[ext=mp4][filesize_approx<48M]/b[filesize<48M]/b[ext=mp4]/b",
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "restrictfilenames": True,
        "socket_timeout": 20,
        "max_filesize": VIDEO_MAX_BYTES,
    }

    proxy = os.getenv("VIDEO_PROXY")  # необязательно: http://user:pass@host:port
    if proxy:
        opts["proxy"] = proxy
    logging.info(f"[VIDEO] yt-dlp версия {yt_dlp.version.__version__}")

    cookies = os.getenv("YTDLP_COOKIES")  # необязательно: cookies в формате Netscape (для YouTube/Instagram)
    if cookies:
        cookie_path = os.path.join(tmpdir, "cookies.txt")
        with open(cookie_path, "w", encoding="utf-8") as f:
            f.write(cookies)
        opts["cookiefile"] = cookie_path

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if (info.get("duration") or 0) > VIDEO_MAX_DURATION:
            raise RuntimeError("видео длиннее 15 минут")
        info = ydl.process_ie_result(info, download=True)
        requested = info.get("requested_downloads") or [{}]
        filepath = requested[0].get("filepath") or ydl.prepare_filename(info)

    if not filepath or not os.path.exists(filepath):
        raise RuntimeError("видео слишком большое (лимит Telegram для ботов — 50 МБ) или недоступно")
    if os.path.getsize(filepath) > VIDEO_MAX_BYTES:
        raise RuntimeError("видео больше 50 МБ — Telegram не позволяет ботам отправлять такие файлы")

    return {
        "path": filepath,
        "title": info.get("title") or "",
        "duration": int(info.get("duration") or 0),
        "width": info.get("width"),
        "height": info.get("height"),
    }


def is_tiktok_url(url: str) -> bool:
    return "tiktok.com" in url.lower()


async def download_tiktok_fallback(url: str, tmpdir: str) -> dict:
    """Запасной способ для TikTok через сторонний сервис tikwm (если yt-dlp заблокирован)."""
    timeout = aiohttp.ClientTimeout(total=60)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    }
    proxy = os.getenv("VIDEO_PROXY") or None

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        data = None
        status = None
        body = ""
        for _ in range(2):  # сервис ограничивает частоту запросов — одна повторная попытка
            async with session.post(
                "https://www.tikwm.com/api/", data={"url": url, "hd": 1}, proxy=proxy
            ) as resp:
                status = resp.status
                body = await resp.text()
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("code") == 0:
                break
            await asyncio.sleep(1.5)

        if not isinstance(data, dict):
            raise RuntimeError(f"tikwm не вернул данные (HTTP {status}, ответ: {body[:100]!r})")

        info = data.get("data") if isinstance(data.get("data"), dict) else {}
        video_url = info.get("play") or info.get("hdplay")
        if data.get("code") != 0 or not video_url:
            raise RuntimeError(data.get("msg") or "в ответе нет видео (возможно, это пост с фото)")
        if video_url.startswith("/"):
            video_url = "https://www.tikwm.com" + video_url

        path = os.path.join(tmpdir, "tiktok.mp4")
        size = 0
        async with session.get(video_url, proxy=proxy) as resp:
            if resp.status != 200:
                raise RuntimeError(f"не удалось скачать файл ({resp.status})")
            with open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > VIDEO_MAX_BYTES:
                        raise RuntimeError("видео больше 50 МБ")
                    f.write(chunk)

    return {
        "path": path,
        "title": info.get("title") or "",
        "duration": int(info.get("duration") or 0),
        "width": None,
        "height": None,
    }


async def handle_video_command(message: types.Message, owner_id: int, args: str):
    conn_id = message.business_connection_id
    chat_id = message.chat.id

    source = args
    reply = message.reply_to_message
    if not re.search(r"https?://\S+", source) and reply:
        source = reply.text or reply.caption or ""

    match = re.search(r"https?://\S+", source)
    if not match:
        await try_delete_message(conn_id, message.message_id)
        await notify_owner(owner_id, "🎬 Укажите ссылку: .video https://... (или ответьте командой на сообщение со ссылкой)")
        return
    url = match.group(0).rstrip(").,]>")

    # Команда превращается в статус, пока видео скачивается
    try:
        await bot.edit_message_text(
            text="⏳ Скачиваю видео…", business_connection_id=conn_id,
            chat_id=chat_id, message_id=message.message_id,
        )
    except Exception as e:
        logging.info(f"[VIDEO] Не удалось показать статус: {e}")

    try:
        async with get_video_semaphore():
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    result = await asyncio.to_thread(_download_video_sync, url, tmpdir)
                except Exception as first_error:
                    if not is_tiktok_url(url):
                        raise
                    logging.info(f"[VIDEO] yt-dlp не справился с TikTok, пробую запасной способ: {first_error}")
                    try:
                        result = await download_tiktok_fallback(url, tmpdir)
                    except Exception as second_error:
                        raise RuntimeError(f"{first_error}\n\nЗапасной способ тоже не сработал: {second_error}")

                video = FSInputFile(result["path"])
                caption = result["title"][:200] or None
                try:
                    await bot.send_video(
                        chat_id=chat_id, business_connection_id=conn_id, video=video,
                        caption=caption, duration=result["duration"] or None,
                        width=result["width"], height=result["height"],
                        supports_streaming=True,
                    )
                except Exception as e:
                    logging.info(f"[VIDEO] Не вышло как видео ({e}), отправляю файлом")
                    await bot.send_document(
                        chat_id=chat_id, business_connection_id=conn_id,
                        document=FSInputFile(result["path"]), caption=caption,
                    )
        logging.info(f"[VIDEO] Отправлено видео из {url}")
    except Exception as e:
        logging.error(f"[VIDEO] Ошибка: {e}")
        await notify_owner(owner_id, f"❌ Не удалось скачать видео:\n{str(e)[:600]}\n\n{url}")

    await try_delete_message(conn_id, message.message_id)  # убираем статус/команду


# ---------- Дуэль (.duel) ----------

DUEL_RUNNING: set = set()


def hp_bar(hp: int) -> str:
    filled = round(max(hp, 0) / 10)
    return "█" * filled + "░" * (10 - filled)


async def run_duel(message: types.Message):
    key = game_key(message)
    if key in DUEL_RUNNING:
        return  # в этом чате дуэль уже идёт
    DUEL_RUNNING.add(key)

    try:
        conn_id = message.business_connection_id
        chat = message.chat
        names = [
            message.from_user.full_name,
            getattr(chat, "full_name", None) or chat.first_name or "Соперник",
        ]
        hp = [100, 100]
        ctx = {"conn_id": conn_id, "msg_id": message.message_id}  # команда превращается в поле боя

        def frame(extra: str) -> str:
            return (
                f"⚔️ Дуэль!\n{names[0]} vs {names[1]}\n\n"
                f"{names[0]}\n{hp_bar(hp[0])} {hp[0]} HP\n"
                f"{names[1]}\n{hp_bar(hp[1])} {hp[1]} HP\n\n{extra}"
            )

        await update_board(ctx, chat.id, frame("Бойцы выходят на арену... 🔫"))

        attacker = random.randint(0, 1)
        while min(hp) > 0:
            await asyncio.sleep(1.5)
            defender = 1 - attacker
            roll = random.random()
            if roll < 0.10:
                dmg = 0
                event = f"💨 {names[attacker]} промахнулся!"
            else:
                dmg = random.randint(8, 35)
                if roll > 0.85:
                    dmg = int(dmg * 1.5)
                    event = f"💥 КРИТ! {names[attacker]} наносит {dmg} урона"
                else:
                    event = f"🗡 {names[attacker]} наносит {dmg} урона"
            hp[defender] = max(0, hp[defender] - dmg)
            await update_board(ctx, chat.id, frame(event))
            attacker = defender

        winner = 0 if hp[0] > 0 else 1

        # Обновляем общий счёт между владельцем и собеседником
        column = "owner_wins" if winner == 0 else "opp_wins"
        await db_conn.execute(
            "INSERT OR IGNORE INTO duels (connection_id, chat_id) VALUES (?, ?)",
            (conn_id, chat.id)
        )
        await db_conn.execute(
            f"UPDATE duels SET {column} = {column} + 1 WHERE connection_id = ? AND chat_id = ?",
            (conn_id, chat.id)
        )
        await db_conn.commit()
        async with db_conn.execute(
            "SELECT owner_wins, opp_wins FROM duels WHERE connection_id = ? AND chat_id = ?",
            (conn_id, chat.id)
        ) as cursor:
            row = await cursor.fetchone()
        ow, pw = row if row else (0, 0)

        await update_board(
            ctx, chat.id,
            frame(f"🏆 Победитель: {names[winner]}!\n\n📊 Общий счёт: {names[0]} {ow} : {pw} {names[1]}")
        )
        logging.info(f"[ДУЭЛЬ] Чат {chat.id}: победил {names[winner]}")
    finally:
        DUEL_RUNNING.discard(key)


# ---------- Кубики (.dice) ----------

DICE_RUNNING: set = set()
DICE_ANIMATION_DELAY = 4.0   # столько идёт анимация кубика в Telegram
DICE_MAX_ROUNDS = 5          # при ничьей перебрасываем, но не бесконечно


async def run_dice(message: types.Message):
    """.dice — каждый из двоих бросает кубик, у кого больше — тот победил."""
    key = game_key(message)
    if key in DICE_RUNNING:
        return  # в этом чате бросок уже идёт
    DICE_RUNNING.add(key)

    try:
        conn_id = message.business_connection_id
        chat = message.chat
        names = [
            message.from_user.full_name,
            getattr(chat, "full_name", None) or chat.first_name or "Соперник",
        ]
        ctx = {"conn_id": conn_id, "msg_id": message.message_id}  # команда превращается в табло

        header = (
            f"🎲 Кубики!\n"
            f"1-й кубик — {names[0]}\n"
            f"2-й кубик — {names[1]}\n\n"
        )
        await update_board(ctx, chat.id, header + "Бросаем...")

        values = (0, 0)
        for attempt in range(1, DICE_MAX_ROUNDS + 1):
            first = await bot.send_dice(chat_id=chat.id, business_connection_id=conn_id, emoji="🎲")
            second = await bot.send_dice(chat_id=chat.id, business_connection_id=conn_id, emoji="🎲")
            values = (first.dice.value, second.dice.value)

            await asyncio.sleep(DICE_ANIMATION_DELAY)  # даём анимации закончиться, чтобы не спойлерить результат

            if values[0] != values[1]:
                break
            if attempt < DICE_MAX_ROUNDS:
                await update_board(ctx, chat.id, header + f"{values[0]} : {values[1]} — ничья! Бросаем ещё раз...")
                await asyncio.sleep(1.5)

        score = f"{names[0]}: {values[0]}\n{names[1]}: {values[1]}\n\n"
        if values[0] > values[1]:
            result = f"🏆 Победил {names[0]}!"
        elif values[1] > values[0]:
            result = f"🏆 Победил {names[1]}!"
        else:
            result = "🤝 Ничья!"
        await update_board(ctx, chat.id, header + score + result)
        logging.info(f"[КУБИКИ] Чат {chat.id}: {values[0]} : {values[1]}")
    except Exception as e:
        logging.error(f"[КУБИКИ] Ошибка: {e}")
    finally:
        DICE_RUNNING.discard(key)


# ---------- Авто-форматирование: .bold .italic .monospace .underline ----------

FORMAT_COMMANDS = {
    ".bold": "bold",
    ".italic": "italic",
    ".monospace": "monospace",
    ".underline": "underline",
}
FORMAT_LABELS = {
    "bold": "Жирный",
    "italic": "Курсив",
    "monospace": "Моноширинный",
    "underline": "Подчёркнутый",
}
FORMAT_ENTITY = {"bold": "bold", "italic": "italic", "monospace": "code", "underline": "underline"}
FORMAT_CACHE: dict[str, set] = {}


async def get_styles(conn_id: str) -> set:
    if conn_id in FORMAT_CACHE:
        return FORMAT_CACHE[conn_id]
    async with db_conn.execute(
        "SELECT styles FROM formats WHERE connection_id = ?", (conn_id,)
    ) as cursor:
        row = await cursor.fetchone()
    styles = {x for x in (row[0] if row and row[0] else "").split(",") if x in FORMAT_ENTITY}
    FORMAT_CACHE[conn_id] = styles
    return styles


async def save_styles(conn_id: str, styles: set):
    FORMAT_CACHE[conn_id] = styles
    await db_conn.execute(
        "INSERT OR REPLACE INTO formats (connection_id, styles) VALUES (?, ?)",
        (conn_id, ",".join(sorted(styles)))
    )
    await db_conn.commit()


async def toggle_format(message: types.Message, owner_id: int, style: str):
    conn_id = message.business_connection_id
    styles = set(await get_styles(conn_id))
    if style in styles:
        styles.discard(style)
        enabled = False
    else:
        styles.add(style)
        enabled = True
    await save_styles(conn_id, styles)

    # Команду убираем из чата, а подтверждение шлём владельцу в личку с ботом
    await try_delete_message(conn_id, message.message_id)

    active = ", ".join(FORMAT_LABELS[x] for x in sorted(styles)) or "нет"
    note = f"{'✅ Включено' if enabled else '❌ Выключено'}: {FORMAT_LABELS[style]}\nАктивные стили: {active}"
    if "monospace" in styles and len(styles) > 1:
        note += "\n⚠️ Моноширинный нельзя сочетать с другими стилями — пока он включён, применяется только он."
    try:
        await bot.send_message(chat_id=owner_id, text=note)
    except Exception as e:
        logging.info(f"[ФОРМАТ] Не удалось отправить подтверждение владельцу: {e}")


async def apply_auto_format(message: types.Message, text_override: str | None = None):
    """Оформляет каждое сообщение владельца включёнными стилями."""
    conn_id = message.business_connection_id
    if not conn_id or not message.from_user:
        return

    text = text_override or message.text or message.caption
    if not text or not text.strip():
        return

    styles = await get_styles(conn_id)
    if not styles and text_override is None:
        return

    owner_id = await ensure_connection_owner(conn_id)
    if message.from_user.id != owner_id:
        return

    is_caption = message.text is None
    existing = [] if text_override else list((message.caption_entities if is_caption else message.entities) or [])

    # Код/pre нельзя совмещать с другими стилями — не трогаем такие сообщения
    if any(e.type in ("code", "pre") for e in existing):
        return

    if "monospace" in styles:
        if existing:
            return  # моноширинный не совмещается со ссылками, упоминаниями и т.п.
        entity_types = ["code"]
    else:
        entity_types = [FORMAT_ENTITY[x] for x in sorted(styles)]

    length = len(text.encode("utf-16-le")) // 2  # Telegram считает в UTF-16
    new_entities = [types.MessageEntity(type=t, offset=0, length=length) for t in entity_types]
    entities = existing + new_entities

    try:
        if is_caption:
            await bot.edit_message_caption(
                caption=text, caption_entities=entities,
                business_connection_id=conn_id, chat_id=message.chat.id,
                message_id=message.message_id,
            )
        else:
            await bot.edit_message_text(
                text=text, entities=entities or None,
                business_connection_id=conn_id, chat_id=message.chat.id,
                message_id=message.message_id,
            )
    except Exception as e:
        logging.error(f"[ФОРМАТ] Не удалось оформить сообщение {message.message_id}: {e}")


# Только SFW-источники картинок
NEKO_APIS = [
    ("https://nekos.best/api/v2/neko", lambda d: d["results"][0]["url"]),
    ("https://api.waifu.pics/sfw/neko", lambda d: d["url"]),
]


async def fetch_neko_url() -> str | None:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for api_url, extract in random.sample(NEKO_APIS, len(NEKO_APIS)):
            try:
                async with session.get(api_url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
                    return extract(data)
            except Exception as e:
                logging.warning(f"[NEKO] Источник {api_url} не ответил: {e}")
    return None


async def send_neko(message: types.Message):
    conn_id = message.business_connection_id
    chat_id = message.chat.id

    url = await fetch_neko_url()
    if not url:
        logging.error("[NEKO] Не удалось получить картинку ни из одного источника")
        return

    is_gif = url.lower().endswith(".gif")
    sender = bot.send_animation if is_gif else bot.send_photo
    arg = "animation" if is_gif else "photo"

    try:
        # Пробуем отправить по ссылке — Telegram сам скачает файл
        await sender(chat_id=chat_id, business_connection_id=conn_id, **{arg: url})
    except Exception as e:
        logging.info(f"[NEKO] Отправка по ссылке не удалась ({e}), скачиваю файл сам")
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                data = await resp.read()
        upload = BufferedInputFile(data, filename="neko.gif" if is_gif else "neko.jpg")
        await sender(chat_id=chat_id, business_connection_id=conn_id, **{arg: upload})

    # Убираем сообщение-команду
    await try_delete_message(conn_id, message.message_id)


SPAM_MAX = 30
SPAM_DELAY = 0.4


async def handle_spam_command(message: types.Message, owner_id: int, args: str):
    """.spam число текст — отправляет текст указанное число раз."""
    conn_id = message.business_connection_id
    chat_id = message.chat.id
    parts = args.split(None, 1)

    if len(parts) < 2 or not parts[0].isdigit() or int(parts[0]) < 1:
        await try_delete_message(conn_id, message.message_id)
        await notify_owner(owner_id, f"📨 Формат: .spam число текст (например, .spam 5 привет). Максимум {SPAM_MAX}.")
        return

    count = min(int(parts[0]), SPAM_MAX)
    spam_text = parts[1]

    await try_delete_message(conn_id, message.message_id)
    for _ in range(count):
        try:
            await bot.send_message(chat_id=chat_id, text=spam_text, business_connection_id=conn_id, parse_mode=None)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(chat_id=chat_id, text=spam_text, business_connection_id=conn_id, parse_mode=None)
            except Exception as e2:
                logging.error(f"[SPAM] Остановлено: {e2}")
                break
        except Exception as e:
            logging.error(f"[SPAM] Остановлено: {e}")
            break
        await asyncio.sleep(SPAM_DELAY)


LOVE_FRAMES = [
    "🤍",
    "🤍🤍",
    "💗💗💗",
    "💗 💖 💗",
    "💖💖💖💖💖",
    "💘 💞 💘 💞 💘",
    "💕💕💕💕💕💕💕",
    "❤️‍🔥❤️‍🔥❤️‍🔥❤️‍🔥❤️‍🔥",
    "💖 Я тебя люблю 💖",
    "💞💖💘 Я тебя люблю 💘💖💞",
    "💖💘💞💓💗 Я тебя люблю 💗💓💞💘💖",
    "❤️ Я тебя люблю ❤️",
]
LOVE_DELAY = 0.5


async def handle_love_command(message: types.Message):
    """.love — анимация сердечек: редактируем команду кадр за кадром."""
    conn_id = message.business_connection_id
    chat_id = message.chat.id
    for frame in LOVE_FRAMES:
        try:
            await bot.edit_message_text(
                text=frame,
                business_connection_id=conn_id,
                chat_id=chat_id,
                message_id=message.message_id,
            )
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            logging.error(f"[LOVE] Остановлено: {e}")
            return
        await asyncio.sleep(LOVE_DELAY)


def _photo_to_gif_sync(data: bytes) -> bytes:
    """Фото -> GIF: плавное приближение к центру (Pillow)."""
    import io
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((512, 512))
    w, h = img.size
    frames = []
    steps = 20
    for i in range(steps):
        scale = 1 + 0.2 * i / (steps - 1)
        cw, ch = w / scale, h / scale
        left, top = (w - cw) / 2, (h - ch) / 2
        frame = img.crop((int(left), int(top), int(left + cw), int(top + ch))).resize((w, h), Image.LANCZOS)
        frames.append(frame.quantize(colors=128))
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=70, loop=0)
    return out.getvalue()


async def handle_gif_command(message: types.Message, owner_id: int):
    """.gif — ответом на фото/видео/кружок делает гифку и отправляет в чат."""
    conn_id = message.business_connection_id
    chat_id = message.chat.id
    reply = message.reply_to_message
    file_id = media_type = None
    if reply:
        _, file_id, media_type = extract_content(reply)

    await try_delete_message(conn_id, message.message_id)

    if media_type not in ("photo", "video", "video_note", "animation"):
        await notify_owner(owner_id, "🎞 Ответьте командой .gif на фото, видео или кружок.")
        return

    try:
        tg_file = await bot.get_file(file_id)  # лимит Bot API — 20 МБ
        data = (await bot.download_file(tg_file.file_path)).read()
        if media_type == "photo":
            gif = await asyncio.to_thread(_photo_to_gif_sync, data)
            upload = BufferedInputFile(gif, filename="anim.gif")
        else:
            upload = BufferedInputFile(data, filename="anim.mp4")
        await bot.send_animation(chat_id=chat_id, animation=upload, business_connection_id=conn_id)
    except Exception as e:
        logging.error(f"[GIF] Ошибка: {e}")
        await notify_owner(owner_id, f"❌ Не удалось сделать гифку: {e}")


async def handle_help_and_games(message: types.Message) -> bool:
    text = message.text
    conn_id = message.business_connection_id
    if not text or not conn_id:
        return False

    # Ходы в игре (от владельца и собеседника)
    if game_key(message) in GAMES and await handle_ttt_move(message):
        return True

    parts = text.split()
    if not parts or parts[0].lower() not in (".help", ".ttt", ".neko", ".nsfw", ".duel", ".dice", ".text", ".song", ".video", ".mute", ".unmute", ".spam", ".love", ".gif", *FORMAT_COMMANDS):
        return False

    owner_id = await ensure_connection_owner(conn_id)
    if not message.from_user or message.from_user.id != owner_id:
        return False  # команды только у владельца

    command = parts[0].lower()
    if command == ".help":
        try:
            await bot.edit_message_text(
                text=HELP_TEXT,
                business_connection_id=conn_id,
                chat_id=message.chat.id,
                message_id=message.message_id,
            )
        except Exception as e:
            logging.error(f"Не удалось показать .help: {e}")
        return True

    if command == ".neko":
        await send_neko(message)
        return True

    if command == ".nsfw":
        if message.chat.id != owner_id:
            await send_nsfw(message, owner_id)
        else:
            await try_delete_message(conn_id, message.message_id)
            await notify_owner(owner_id, "🔞 Команду .nsfw нужно писать в чате с собеседником.")
        return True

    if command == ".duel":
        if message.chat.id != owner_id:
            await run_duel(message)
        return True

    if command == ".dice":
        if message.chat.id != owner_id:
            await run_dice(message)
        else:
            await try_delete_message(conn_id, message.message_id)
            await notify_owner(owner_id, "🎲 Команду .dice нужно писать в чате с собеседником.")
        return True

    if command == ".text":
        await handle_text_command(message, owner_id)
        return True

    if command == ".song":
        args = text.split(None, 1)[1] if len(parts) > 1 else ""
        await handle_song_command(message, owner_id, args)
        return True

    if command == ".video":
        args = text.split(None, 1)[1] if len(parts) > 1 else ""
        await handle_video_command(message, owner_id, args)
        return True

    if command == ".gif":
        await handle_gif_command(message, owner_id)
        return True

    if command == ".love":
        await handle_love_command(message)
        return True

    if command == ".spam":
        args = text.split(None, 1)[1] if len(parts) > 1 else ""
        await handle_spam_command(message, owner_id, args)
        return True

    if command in (".mute", ".unmute"):
        await set_mute(message, owner_id, muted=(command == ".mute"))
        return True

    if command in FORMAT_COMMANDS:
        if len(parts) == 1:  # переключатель — только когда сообщение состоит из одной команды
            await toggle_format(message, owner_id, FORMAT_COMMANDS[command])
            return True
        return False

    # .ttt
    if len(parts) > 1 and parts[1].lower() in ("stop", "end", "стоп"):
        await stop_ttt(message)
    else:
        await start_ttt(message, owner_id)
    return True


# 2. Входящие бизнес-сообщения
@dp.business_message()
async def handle_business_message(message: types.Message):
    try:
        if await delete_if_muted(message):
            return
    except Exception as e:
        logging.error(f"Ошибка в delete_if_muted: {e}")

    try:
        if await handle_style_command(message):
            return
    except Exception as e:
        logging.error(f"Ошибка в handle_style_command: {e}")

    try:
        if await handle_help_and_games(message):
            return
    except Exception as e:
        logging.error(f"Ошибка в handle_help_and_games: {e}")

    try:
        styled = await style_text_for_mode(message)
        await apply_auto_format(message, text_override=styled)
    except Exception as e:
        logging.error(f"Ошибка в apply_auto_format: {e}")

    logging.info(
        f"[ВХОДЯЩЕЕ] id={message.message_id} type={message.content_type} "
        f"reply={'да' if message.reply_to_message else 'нет'}"
    )
    try:
        await save_message(message)
    except Exception as e:
        logging.error(f"Ошибка в handle_business_message: {e}")

    try:
        await forward_one_time_media(message)
    except Exception as e:
        logging.error(f"Ошибка в forward_one_time_media: {e}")


# 2.1 Редактирование сообщений: обновляем кэш и сообщаем владельцу, что собеседник изменил сообщение
EDIT_PART_LIMIT = 1500  # максимум символов для «было» и «стало»


async def notify_edit(message: types.Message, old_row: tuple):
    conn_id = message.business_connection_id
    owner_id = await ensure_connection_owner(conn_id) if conn_id else None
    if not owner_id:
        return
    # Свои правки (в том числе команды бота) владельцу показывать не нужно
    if message.from_user and message.from_user.id == owner_id:
        return

    sender_name, username, old_text, old_file_id, old_media = old_row
    new_text, new_file_id, _ = extract_content(message)
    old_text, new_text = old_text or "", new_text or ""

    media_changed = bool(old_file_id and new_file_id and old_file_id != new_file_id)
    if old_text == new_text and not media_changed:
        return  # изменилось только форматирование или ничего существенного

    notification = (
        f"✏️ <b>Сообщение изменено!</b>\n\n"
        f"👤 <b>Отправитель:</b> {html.escape(sender_name or '')} ({html.escape(username or '')})\n\n"
        f"📄 <b>Было:</b> {html.escape(old_text[:EDIT_PART_LIMIT])}\n\n"
        f"📝 <b>Стало:</b> {html.escape(new_text[:EDIT_PART_LIMIT])}"
    )
    if media_changed:
        notification += "\n\n🖼 Медиафайл тоже был заменён (ниже прежняя версия)."

    try:
        if media_changed:
            await send_deleted_notice(owner_id, notification, old_file_id, old_media)
        else:
            await bot.send_message(chat_id=owner_id, text=notification[:4000], parse_mode="HTML")
    except Exception as e:
        logging.error(f"[ПРАВКА] Не удалось отправить уведомление владельцу {owner_id}: {e}")


@dp.edited_business_message()
async def handle_edited_business_message(message: types.Message):
    try:
        if await delete_if_muted(message):
            return

        async with db_conn.execute(
            "SELECT sender_name, username, text_content, file_id, media_type "
            "FROM messages WHERE message_id = ? AND chat_id = ?",
            (message.message_id, message.chat.id)
        ) as cursor:
            old_row = await cursor.fetchone()

        await save_message(message)  # обновляем кэш, чтобы при удалении был актуальный текст

        if old_row:
            await notify_edit(message, old_row)
        else:
            logging.info(f"[ПРАВКА] Старой версии сообщения {message.message_id} нет в базе")
    except Exception as e:
        logging.error(f"Ошибка в handle_edited_business_message: {e}")


async def send_deleted_notice(target_id: int, text: str, file_id: str | None, media_type: str | None):
    if not file_id or media_type not in MEDIA_SENDERS:
        await bot.send_message(chat_id=target_id, text=text, parse_mode="HTML")
        return

    method, arg = MEDIA_SENDERS[media_type]

    if media_type in CAPTION_TYPES and len(text) <= CAPTION_LIMIT:
        await method(chat_id=target_id, **{arg: file_id}, caption=text, parse_mode="HTML")
    else:
        # Подпись не поддерживается или слишком длинная — шлём текст отдельно
        await bot.send_message(chat_id=target_id, text=text, parse_mode="HTML")
        await method(chat_id=target_id, **{arg: file_id})


ONE_TIME_EXT = {"photo": "jpg", "video": "mp4", "voice": "ogg", "video_note": "mp4"}


async def send_one_time_notice(target_id: int, text: str, file_id: str, media_type: str):
    """
    Telegram не даёт переотправить file_id одноразового медиа (SelfDestructingPhoto и т.п.),
    поэтому скачиваем файл и загружаем его заново как обычный.
    """
    tg_file = await bot.get_file(file_id)
    data = await bot.download_file(tg_file.file_path)
    upload = BufferedInputFile(data.read(), filename=f"one_time.{ONE_TIME_EXT[media_type]}")

    method, arg = MEDIA_SENDERS[media_type]
    if media_type in CAPTION_TYPES and len(text) <= CAPTION_LIMIT:
        await method(chat_id=target_id, **{arg: upload}, caption=text, parse_mode="HTML")
    else:
        await bot.send_message(chat_id=target_id, text=text, parse_mode="HTML")
        await method(chat_id=target_id, **{arg: upload})


ONE_TIME_TYPES = {"photo", "video", "voice", "video_note"}
ONLY_OWNER_REPLIES = True  # True: только когда отвечаете вы; False: на любой ответ в чате


async def forward_one_time_media(message: types.Message):
    """
    Если сообщение — ответ на медиа, которое бот не получил обычным путём
    (одноразовое), берём медиа из reply_to_message и отправляем владельцу в ЛС.
    """
    reply = message.reply_to_message
    conn_id = message.business_connection_id
    if not reply or not conn_id:
        return

    content, file_id, media_type = extract_content(reply)
    logging.info(
        f"[ОТВЕТ] reply_to={reply.message_id} media_type={media_type} "
        f"has_file={bool(file_id)} content_type={reply.content_type}"
    )
    if not file_id or media_type not in ONE_TIME_TYPES:
        return

    chat_id = message.chat.id

    # Обычное медиа бот получил как обычное сообщение и сохранил в базу.
    # Одноразовое до бота не доходит, его в базе нет — только его и пересылаем.
    async with db_conn.execute(
        "SELECT 1 FROM messages WHERE message_id = ? AND chat_id = ? AND file_id IS NOT NULL",
        (reply.message_id, chat_id)
    ) as cursor:
        if await cursor.fetchone():
            return

    # Реагируем только на ответы самого владельца (иначе будет спам копиями)
    if ONLY_OWNER_REPLIES:
        owner_check = await ensure_connection_owner(conn_id)
        if not message.from_user or message.from_user.id != owner_check:
            return

    # Уже отправляли это медиа раньше
    async with db_conn.execute(
        "SELECT 1 FROM one_time_sent WHERE message_id = ? AND chat_id = ?",
        (reply.message_id, chat_id)
    ) as cursor:
        if await cursor.fetchone():
            return

    owner_id = await ensure_connection_owner(conn_id)
    if not owner_id:
        return

    sender = reply.from_user.full_name if reply.from_user else "Собеседник"
    username = (
        f"@{reply.from_user.username}"
        if reply.from_user and reply.from_user.username
        else "@нет_юзернейма"
    )
    notification = (
        f"🔥 <b>Одноразовое сообщение!</b>\n\n"
        f"👤 <b>Отправитель:</b> {html.escape(sender)} ({html.escape(username)})\n"
        f"📝 <b>Описание:</b> {html.escape(content[:MAX_TEXT_LEN])}"
    )

    await send_one_time_notice(owner_id, notification, file_id, media_type)

    await db_conn.execute(
        "INSERT OR IGNORE INTO one_time_sent (message_id, chat_id) VALUES (?, ?)",
        (reply.message_id, chat_id)
    )
    await db_conn.commit()
    logging.info(f"[ОДНОРАЗОВОЕ] Медиа {reply.message_id} отправлено пользователю {owner_id}")


# 3. Удаление сообщений
@dp.deleted_business_messages()
async def handle_deleted_business_messages(event: types.BusinessMessagesDeleted):
    logging.info(f"[УДАЛЕНИЕ] Обнаружено событие удаления ID: {event.message_ids}")
    try:
        conn_id = event.business_connection_id
        owner_user_id = await ensure_connection_owner(conn_id) if conn_id else None

        for msg_id in set(event.message_ids):
            async with db_conn.execute(
                "SELECT sender_name, username, text_content, file_id, media_type, connection_id "
                "FROM messages WHERE message_id = ? AND chat_id = ?",
                (msg_id, event.chat.id)
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                logging.warning(f"[БАЗА] Сообщение ID {msg_id} не найдено в базе данных")
                continue

            sender_name, username, text_content, file_id, media_type, saved_conn_id = row

            target_user_id = owner_user_id
            if not target_user_id and saved_conn_id:
                target_user_id = await ensure_connection_owner(saved_conn_id)

            if not target_user_id:
                logging.warning(f"[ОШИБКА] Не удалось найти владельца аккаунта для сообщения {msg_id}")
                continue

            text = (text_content or "")[:MAX_TEXT_LEN]
            notification = (
                f"⚠️ <b>Удалено сообщение!</b>\n\n"
                f"👤 <b>Отправитель:</b> {html.escape(sender_name or '')} ({html.escape(username or '')})\n"
                f"📝 <b>Текст/Описание:</b> {html.escape(text)}"
            )

            try:
                await send_deleted_notice(target_user_id, notification, file_id, media_type)
            except Exception as send_error:
                logging.error(f"Не удалось отправить логи пользователю {target_user_id}: {send_error}")
                continue  # не удаляем из кэша, если отправка не удалась

            await db_conn.execute(
                "DELETE FROM messages WHERE message_id = ? AND chat_id = ?",
                (msg_id, event.chat.id)
            )
            await db_conn.commit()
    except Exception as e:
        logging.error(f"Ошибка при обработке удаления: {e}")


# ---------- Дуэль на звёзды (.dicep, .bal, .cancel, .withdraw) ----------

STARS_MIN_BET = 1
STARS_MAX_BET = 10000
STARS_COMMISSION = 0.10
STARS_P2_TIMEOUT = 300  # сек: сколько соперник может не оплачивать счёт

stars_router = Router()
_BG_TASKS: set = set()  # чтобы фоновые задачи не собрал сборщик мусора


def spawn(coro):
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


# --- баланс (через общее соединение db_conn, списание атомарное) ---

async def get_balance(user_id: int) -> int:
    async with db_conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def add_balance(user_id: int, amount: int):
    await db_conn.execute(
        "INSERT INTO users (user_id, balance) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance",
        (user_id, amount),
    )
    await db_conn.commit()


async def try_deduct(user_id: int, amount: int) -> bool:
    """Списывает только если хватает денег. Баланс не может уйти в минус."""
    cur = await db_conn.execute(
        "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
        (amount, user_id, amount),
    )
    await db_conn.commit()
    return cur.rowcount > 0


# --- игры хранятся в БД (а не в памяти), чтобы ставки не терялись при перезапуске ---

GAME_COLS = "id, player1, player1_name, player2, player2_name, bet, status"


async def get_game(game_id: int) -> dict | None:
    async with db_conn.execute(f"SELECT {GAME_COLS} FROM stars_games WHERE id = ?", (game_id,)) as cur:
        row = await cur.fetchone()
    return dict(zip(GAME_COLS.split(", "), row)) if row else None


async def create_game(user_id: int, name: str, bet: int, status: str) -> int:
    cur = await db_conn.execute(
        "INSERT INTO stars_games (player1, player1_name, bet, status, created) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, bet, status, int(time.time())),
    )
    await db_conn.commit()
    return cur.lastrowid


async def invite_link(game_id: int) -> str:
    me = await bot.me()
    return f"https://t.me/{me.username}?start=join_{game_id}"


async def send_invite(chat_id: int, game_id: int):
    link = await invite_link(game_id)
    await bot.send_message(
        chat_id,
        f"🎲 Игра #{game_id} активирована!\n\n🔗 <b>Ссылка для соперника:</b>\n<code>{link}</code>",
        parse_mode="HTML",
    )


# --- команды ---

@stars_router.message(F.chat.type == "private", F.text == ".bal")
async def cmd_bal(message: types.Message):
    balance = await get_balance(message.from_user.id)
    await message.answer(f"💳 <b>Ваш игровой баланс:</b> {balance} ⭐️", parse_mode="HTML")


@stars_router.message(F.chat.type == "private", F.text == ".withdraw")
async def cmd_withdraw(message: types.Message):
    await message.answer("⚠️ Вывод пока недоступен. Баланс можно использовать только для ставок.")


@stars_router.message(F.chat.type == "private", F.text.regexp(r"^\.dicep(\s|$)"))
async def cmd_dicep(message: types.Message):
    user = message.from_user
    parts = message.text.split()
    bet = 50
    if len(parts) > 1:
        if not parts[1].isdigit():
            return await message.answer("❌ Ставка должна быть числом: .dicep 50")
        bet = int(parts[1])
    if not STARS_MIN_BET <= bet <= STARS_MAX_BET:
        return await message.answer(f"❌ Ставка от {STARS_MIN_BET} до {STARS_MAX_BET} ⭐️")

    if await try_deduct(user.id, bet):
        game_id = await create_game(user.id, user.full_name, bet, "ready_for_p2")
        await send_invite(message.chat.id, game_id)
    else:
        game_id = await create_game(user.id, user.full_name, bet, "waiting_p1_payment")
        await bot.send_invoice(
            chat_id=message.chat.id,
            title=f"Создание игры #{game_id}",
            description=f"Оплатите ставку {bet} ⭐️",
            payload=f"p1_pay_{game_id}",
            currency="XTR",
            prices=[LabeledPrice(label=f"Ставка в игре #{game_id}", amount=bet)],
        )


@stars_router.message(F.chat.type == "private", F.text == ".cancel")
async def cmd_cancel(message: types.Message):
    """Отмена своих игр, в которых ещё нет соперника; ставка возвращается на баланс."""
    user_id = message.from_user.id
    async with db_conn.execute(
        "SELECT id, bet, status FROM stars_games "
        "WHERE player1 = ? AND status IN ('ready_for_p2', 'waiting_p1_payment')", (user_id,)
    ) as cur:
        games = await cur.fetchall()

    if not games:
        return await message.answer("У вас нет игр, которые можно отменить.")

    for game_id, bet, status in games:
        cur = await db_conn.execute("DELETE FROM stars_games WHERE id = ? AND status = ?", (game_id, status))
        await db_conn.commit()
        if cur.rowcount and status == "ready_for_p2":
            await add_balance(user_id, bet)
    await message.answer("✅ Игры отменены, ставки возвращены на баланс.")


# --- присоединение соперника (вызывается из /start join_N) ---

async def stars_join_from_start(message: types.Message, args: str):
    user = message.from_user
    try:
        game_id = int(args.split("_", 1)[1])
    except (IndexError, ValueError):
        return await message.answer("❌ Неверная ссылка.")

    game = await get_game(game_id)
    if not game:
        return await message.answer("❌ Игра не найдена.")
    if game["player1"] == user.id:
        return await message.answer("❌ Нельзя играть против себя!")

    # Атомарно занимаем место соперника: два человека не смогут зайти одновременно
    cur = await db_conn.execute(
        "UPDATE stars_games SET player2 = ?, player2_name = ?, status = 'waiting_p2_payment' "
        "WHERE id = ? AND status = 'ready_for_p2' AND player1 != ?",
        (user.id, user.full_name, game_id, user.id),
    )
    await db_conn.commit()
    if cur.rowcount == 0:
        return await message.answer("❌ В игре уже есть соперник или она недоступна.")

    if await try_deduct(user.id, game["bet"]):
        await db_conn.execute("UPDATE stars_games SET status = 'rolling' WHERE id = ?", (game_id,))
        await db_conn.commit()
        await start_rolling(game_id)
    else:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title=f"Принять вызов #{game_id}",
            description=f"Оплатите {game['bet']} ⭐️",
            payload=f"p2_pay_{game_id}",
            currency="XTR",
            prices=[LabeledPrice(label=f"Вход в игру #{game_id}", amount=game["bet"])],
        )
        spawn(p2_payment_timeout(game_id, user.id))


async def p2_payment_timeout(game_id: int, p2_id: int):
    """Если соперник не оплатил — освобождаем место, игра снова открыта."""
    await asyncio.sleep(STARS_P2_TIMEOUT)
    await db_conn.execute(
        "UPDATE stars_games SET player2 = NULL, player2_name = NULL, status = 'ready_for_p2' "
        "WHERE id = ? AND status = 'waiting_p2_payment' AND player2 = ?",
        (game_id, p2_id),
    )
    await db_conn.commit()


async def start_rolling(game_id: int):
    game = await get_game(game_id)
    if not game:
        return
    try:
        await bot.send_message(game["player1"], f"🔥 {game['player2_name']} принял вызов!")
    except Exception as e:
        logging.info(f"[STARS] Не удалось написать игроку 1: {e}")
    try:
        await bot.send_message(game["player2"], "🚀 Игра начинается...")
    except Exception as e:
        logging.info(f"[STARS] Не удалось написать игроку 2: {e}")
    spawn(run_game_logic(game_id))


# --- оплата звёздами ---

@stars_router.pre_checkout_query()
async def stars_pre_checkout(query: PreCheckoutQuery):
    kind, _, gid = query.invoice_payload.rpartition("_")
    game = await get_game(int(gid)) if gid.isdigit() else None
    ok = False
    if game:
        if kind == "p1_pay":
            ok = game["status"] == "waiting_p1_payment" and game["player1"] == query.from_user.id
        elif kind == "p2_pay":
            ok = game["status"] == "waiting_p2_payment" and game["player2"] == query.from_user.id
    await query.answer(ok=ok, error_message=None if ok else "Игра больше недоступна, создайте новую.")


@stars_router.message(F.successful_payment)
async def stars_payment(message: types.Message):
    pay = message.successful_payment
    user_id = message.from_user.id
    kind, _, gid = pay.invoice_payload.rpartition("_")
    game_id = int(gid) if gid.isdigit() else None
    handled = False

    if game_id is not None and kind == "p1_pay":
        cur = await db_conn.execute(
            "UPDATE stars_games SET status = 'ready_for_p2' "
            "WHERE id = ? AND player1 = ? AND status = 'waiting_p1_payment'", (game_id, user_id)
        )
        await db_conn.commit()
        if cur.rowcount:
            handled = True
            await send_invite(message.chat.id, game_id)
    elif game_id is not None and kind == "p2_pay":
        cur = await db_conn.execute(
            "UPDATE stars_games SET status = 'rolling' "
            "WHERE id = ? AND player2 = ? AND status = 'waiting_p2_payment'", (game_id, user_id)
        )
        await db_conn.commit()
        if cur.rowcount:
            handled = True
            await start_rolling(game_id)

    if not handled:
        # Игра успела стать недоступной — деньги не пропадают, а попадают на баланс
        await add_balance(user_id, pay.total_amount)
        await message.answer(f"⚠️ Игра уже недоступна. {pay.total_amount} ⭐️ зачислены на игровой баланс.")


# --- сама игра ---

async def roll_pair(roller: int, watcher: int) -> int:
    await bot.send_message(roller, "🎲 Ваш ход...")
    total = 0
    for _ in range(2):
        d = await bot.send_dice(chat_id=roller, emoji="🎲")
        try:
            await bot.forward_message(chat_id=watcher, from_chat_id=roller, message_id=d.message_id)
        except Exception as e:
            logging.info(f"[STARS] Не удалось показать кубик сопернику: {e}")
        total += d.dice.value
        await asyncio.sleep(4)
    return total


async def safe_send(chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text)
    except Exception as e:
        logging.info(f"[STARS] Не удалось написать {chat_id}: {e}")


async def run_game_logic(game_id: int):
    game = await get_game(game_id)
    if not game or game["status"] != "rolling":
        return
    p1, p2, bet = game["player1"], game["player2"], game["bet"]

    try:
        s1 = await roll_pair(p1, p2)
        s2 = await roll_pair(p2, p1)
    except Exception as e:
        # Сбой до результата — возвращаем обе ставки
        logging.error(f"[STARS] Игра {game_id} прервана: {e}")
        cur = await db_conn.execute("DELETE FROM stars_games WHERE id = ? AND status = 'rolling'", (game_id,))
        await db_conn.commit()
        if cur.rowcount:
            await add_balance(p1, bet)
            await add_balance(p2, bet)
            await safe_send(p1, "⚠️ Игра прервана из-за ошибки. Ставка возвращена на баланс.")
            await safe_send(p2, "⚠️ Игра прервана из-за ошибки. Ставка возвращена на баланс.")
        return

    # Сначала закрываем игру: повторно выплатить её уже не получится
    cur = await db_conn.execute("DELETE FROM stars_games WHERE id = ? AND status = 'rolling'", (game_id,))
    await db_conn.commit()
    if not cur.rowcount:
        return

    total_bank = bet * 2
    prize = total_bank - int(total_bank * STARS_COMMISSION)

    if s1 > s2:
        await add_balance(p1, prize)
        await safe_send(p1, f"🏆 Вы выиграли {prize} ⭐️ ({s1} vs {s2})")
        await safe_send(p2, f"📉 Вы проиграли ({s2} vs {s1})")
    elif s2 > s1:
        await add_balance(p2, prize)
        await safe_send(p2, f"🏆 Вы выиграли {prize} ⭐️ ({s2} vs {s1})")
        await safe_send(p1, f"📉 Вы проиграли ({s1} vs {s2})")
    else:
        await add_balance(p1, bet)
        await add_balance(p2, bet)
        await safe_send(p1, f"🤝 Ничья ({s1}={s2}). Ставка возвращена.")
        await safe_send(p2, f"🤝 Ничья ({s2}={s1}). Ставка возвращена.")


async def stars_recover():
    """Вызывать при старте: ставки зависших игр возвращаются, ничего не теряется."""
    async with db_conn.execute("SELECT id, player1, player2, bet FROM stars_games WHERE status = 'rolling'") as cur:
        rows = await cur.fetchall()
    for game_id, p1, p2, bet in rows:
        await add_balance(p1, bet)
        if p2:
            await add_balance(p2, bet)
        await db_conn.execute("DELETE FROM stars_games WHERE id = ?", (game_id,))
    await db_conn.execute("DELETE FROM stars_games WHERE status = 'waiting_p1_payment'")
    await db_conn.execute(
        "UPDATE stars_games SET player2 = NULL, player2_name = NULL, status = 'ready_for_p2' "
        "WHERE status = 'waiting_p2_payment'"
    )
    await db_conn.commit()
    if rows:
        logging.info(f"[STARS] Возвращены ставки в {len(rows)} прерванных играх")


# --- админка ---

ADMIN_ID = 8478742975  # ваш Telegram ID


@stars_router.message(
    F.chat.type == "private",
    F.from_user.id == ADMIN_ID,
    F.text.regexp(r"^\.give(\s+\d+)?\s+-?\d+$"),
)
async def cmd_give(message: types.Message):
    """.give сумма — себе; .give ID сумма — другому пользователю (отрицательная сумма списывает)."""
    nums = [int(x) for x in message.text.split()[1:]]
    target, amount = (ADMIN_ID, nums[0]) if len(nums) == 1 else (nums[0], nums[1])

    if amount >= 0:
        await add_balance(target, amount)
    elif not await try_deduct(target, -amount):
        return await message.answer("❌ У пользователя недостаточно средств для списания.")

    await message.answer(f"✅ Баланс {target}: {await get_balance(target)} ⭐️")


dp.include_router(stars_router)


NOTIFY_STATUS = True  # писать пользователям бота о запуске и остановке

# ---------- Список изменений ----------
# При каждом обновлении добавляйте НОВУЮ запись В НАЧАЛО списка (с новой версией).
# После запуска бот сам пришлёт её всем пользователям.
CHANGELOG = [
    ("2.0", [
        "🔪 Новый режим .yandere — включается и выключается как .tsundere и .kawaii",
        "🎭 Добавлено больше фраз для .tsundere и .kawaii",
        "🐞 Исправлено: бот больше не пересылает вам обычные фото и видео при ответе на них — только одноразовые",
    ]),
    ("1.9", [
        "⭐️ Дуэль на звёзды в личке с ботом: .dicep ставка, .bal, .cancel",
    ]),
    ("1.8", [
        "🎲 Добавлена команда .dice — каждый бросает кубик, у кого больше, тот победил",
    ]),
    ("1.7", [
        "🎭 .tsundere и .kawaii теперь включают и выключают режим: все ваши сообщения оформляются в выбранном стиле",
    ]),
    ("1.6", [
        "✏️ Бот сообщает, когда собеседник изменил сообщение: показывает, что было и что стало",
    ]),
    ("1.5", [
        "🔞 Добавлена команда .nsfw — картинка 18+ под спойлером в чат с собеседником",
    ]),
    ("1.4", [
        "🎞 Добавлена команда .gif — гифка из фото, видео или кружка",
    ]),
    ("1.3", [
        "💖 Добавлена команда .love — анимация любви",
        "⚡ .spam стал быстрее",
    ]),
    ("1.1", [
        "➕ Добавлена команда .spam",
    ]),
    ("1.2.0", [
        "👋 Новое приветствие: обновлена инструкция по подключению",
        "📋 Команда /help в личке с ботом показывает список команд",
        "🔕 Бот больше не пишет об отключении",
    ]),
    ("1.1.0", [
        "🔇 .mute теперь предупреждает собеседника в чате, а .unmute сообщает, что ограничение снято",
        "📣 После каждого обновления бот присылает список изменений",
    ]),
    ("1.0.0", [
        "💾 Сохранение удалённых сообщений и медиа из бизнес-чатов",
        "🔥 Пересылка одноразовых фото, видео и голосовых при ответе на них",
        "📋 Список всех команд: .help",
    ]),
]
CURRENT_VERSION = CHANGELOG[0][0]


def format_changelog() -> str:
    version, notes = CHANGELOG[0]
    return f"🆕 Бот обновлён до версии {version}\n\nЧто нового:\n" + "\n".join(f"• {n}" for n in notes)


async def get_meta(key: str) -> str | None:
    async with db_conn.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def set_meta(key: str, value: str):
    await db_conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    await db_conn.commit()


async def notify_all_owners(text: str):
    """Отправляет сообщение всем, кто подключил бота (в личку с ботом, а не в чаты с собеседниками)."""
    if not NOTIFY_STATUS or db_conn is None:
        return
    try:
        async with db_conn.execute("SELECT DISTINCT owner_user_id FROM connections") as cursor:
            rows = await cursor.fetchall()
    except Exception as e:
        logging.error(f"Не удалось получить список владельцев: {e}")
        return

    for (owner_id,) in rows:
        try:
            await bot.send_message(chat_id=owner_id, text=text)
        except Exception as e:
            logging.info(f"[СТАТУС] Не удалось написать {owner_id}: {e}")


async def handle_web_request(request):
    return web.Response(text="Бот стабильно активен!")


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get('/', handle_web_request)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    return runner


async def main():
    await init_db()
    await stars_recover()  # вернуть ставки из игр, прерванных перезапуском
    runner = await start_web_server()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        if await get_meta("announced_version") != CURRENT_VERSION:
            # Первый запуск после обновления — присылаем список изменений
            await notify_all_owners(format_changelog())
            await set_meta("announced_version", CURRENT_VERSION)
        else:
            await notify_all_owners("✅ Бот снова в сети. Техработы завершены.")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=False,  # сессию закроем сами, после уведомления об остановке
        )
    finally:
        await runner.cleanup()
        if db_conn:
            await db_conn.close()
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
