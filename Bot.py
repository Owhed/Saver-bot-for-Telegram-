import logging
import asyncio
import aiosqlite
import os
import html
import random
import re
import tempfile
from aiogram import Bot, Dispatcher, types
from aiogram.types import BufferedInputFile, FSInputFile
from aiogram.filters import CommandStart
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

    # Включённые стили оформления для каждого владельца
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS formats (
            connection_id TEXT PRIMARY KEY,
            styles TEXT
        )
    ''')

    await db_conn.commit()
    logging.info("База данных SQLite успешно запущена!")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    welcome_text = (
        "👋 Привет!\n\n"
        "Я бот для сохранения удалённых сообщений из <b>Telegram Business</b>.\n\n"
        "1️⃣ Подключите меня в <b>Настройки ➔ Telegram Business ➔ Чат-боты</b>.\n"
        "2️⃣ Все удалённые сообщения и медиафайлы из ваших бизнес-чатов будут приходить прямо сюда!\n\n"
        "Мой ТГК @rebfa\n"
        "поддержать: <code>UQDT1kkEirbQOmUlfvc9AX5LaKZUDteRw44uRQg_xOtpAOjc</code>"
    )
    await message.answer(welcome_text, parse_mode="HTML")


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


# ---------- Режимы стилизации: .tsundere и .kawaii ----------

TSUNDERE_PREFIXES = [
    "Х-хмф! Мне вообще-то это совсем не нужно, но",
    "Д-дурак! Ладно, слушай:",
    "Т-ты не подумай, что мне не всё равно, просто",
    "Ц-цк... ну раз уж ты так просишь,",
    "Н-не смотри на меня так! Короче,",
]
TSUNDERE_SUFFIXES = [
    "— и это совсем не потому, что ты мне нравишься! 😤",
    "Б-баka!",
    "Хмф! Не задавай лишних вопросов!",
    "И не думай, что это ради тебя! 💢",
    "...и вообще, отстань! 😳",
]

KAWAII_PREFIXES = [
    "Ой, ну я такое не умею, но",
    "Ня~ слушай,",
    "Ааа, только не смейся надо мной, но",
    "Э-этоо... ну...",
    "Уву~ а можно я скажу?",
]
KAWAII_SUFFIXES = [
    "хихи~ 🥺👉👈",
    "ты же не будешь ругаться? 💗",
    "уву~ ✨",
    "ня~ 🎀",
    "я старалась~ 🥺💕",
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


STYLE_COMMANDS = {
    ".tsundere": tsundere_style,
    ".kawaii": kawaii_style,
}


async def handle_style_command(message: types.Message) -> bool:
    """Если владелец написал '.tsundere текст' или '.kawaii текст' — редактируем сообщение."""
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
        return True  # команда без текста — ничего не делаем

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


# ---------- .help и крестики-нолики (.ttt) ----------

HELP_TEXT = (
    "✨ Команды (работают в бизнес-чатах, только у владельца):\n\n"
    ".tsundere текст — сообщение в стиле цундере\n"
    ".kawaii текст — пикми-режим\n"
    ".ttt — крестики-нолики с собеседником (ходы цифрами 1–9)\n"
    ".ttt stop — остановить игру\n"
    ".neko — случайная картинка с кошкодевочкой\n"
    ".duel — дуэль с собеседником, победителя решает случай\n"
    ".text — расшифровать голосовое или кружок (ответом на него)\n"
    ".video ссылка — скачать видео (TikTok, YouTube и др.) и отправить в чат\n"
    ".song название или строчка — найти песню\n"
    ".bold — включить/выключить жирный шрифт\n"
    ".italic — включить/выключить курсив\n"
    ".monospace — включить/выключить моноширинный шрифт\n"
    ".underline — включить/выключить подчёркивание\n"
    ".help — этот список"
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


async def try_delete_message(conn_id: str, message_id: int):
    try:
        await bot.delete_business_messages(business_connection_id=conn_id, message_ids=[message_id])
    except Exception as e:
        logging.info(f"[ИГРА] Не удалось удалить сообщение {message_id}: {e}")


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
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get("https://www.tikwm.com/api/", params={"url": url}) as resp:
            data = await resp.json(content_type=None)

        info = data.get("data") or {}
        video_url = info.get("play") or info.get("hdplay")
        if data.get("code") != 0 or not video_url:
            raise RuntimeError(data.get("msg") or "в ответе нет видео (возможно, это пост с фото)")
        if video_url.startswith("/"):
            video_url = "https://www.tikwm.com" + video_url

        path = os.path.join(tmpdir, "tiktok.mp4")
        size = 0
        async with session.get(video_url) as resp:
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


async def apply_auto_format(message: types.Message):
    """Оформляет каждое сообщение владельца включёнными стилями."""
    conn_id = message.business_connection_id
    if not conn_id or not message.from_user:
        return

    text = message.text or message.caption
    if not text or not text.strip():
        return

    styles = await get_styles(conn_id)
    if not styles:
        return

    owner_id = await ensure_connection_owner(conn_id)
    if message.from_user.id != owner_id:
        return

    is_caption = message.text is None
    existing = list((message.caption_entities if is_caption else message.entities) or [])

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
                text=text, entities=entities,
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


async def handle_help_and_games(message: types.Message) -> bool:
    text = message.text
    conn_id = message.business_connection_id
    if not text or not conn_id:
        return False

    # Ходы в игре (от владельца и собеседника)
    if game_key(message) in GAMES and await handle_ttt_move(message):
        return True

    parts = text.split()
    if not parts or parts[0].lower() not in (".help", ".ttt", ".neko", ".duel", ".text", ".song", ".video", *FORMAT_COMMANDS):
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

    if command == ".duel":
        if message.chat.id != owner_id:
            await run_duel(message)
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
        await apply_auto_format(message)
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


# 2.1 Редактирование сообщений — обновляем кэш, чтобы при удалении был актуальный текст
@dp.edited_business_message()
async def handle_edited_business_message(message: types.Message):
    try:
        await save_message(message)
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


NOTIFY_STATUS = True  # писать пользователям бота о запуске и остановке


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
    runner = await start_web_server()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await notify_all_owners("✅ Бот снова в сети. Техработы завершены.")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=False,  # сессию закроем сами, после уведомления об остановке
        )
    finally:
        await notify_all_owners(
            "🔧 Бот ушёл на тех. работы.\n"
            "Пока он выключен, сообщения не сохраняются и команды не работают. Напишу, когда вернусь."
        )
        await runner.cleanup()
        if db_conn:
            await db_conn.close()
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
