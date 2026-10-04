import logging
import asyncio
import aiosqlite
import os
import html
import random
from aiogram import Bot, Dispatcher, types
from aiogram.types import BufferedInputFile
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
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

    await db_conn.commit()
    logging.info("База данных SQLite успешно запущена!")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    welcome_text = (
        "👋 Привет!\n\n"
        "Я бот для сохранения удалённых сообщений из <b>Telegram Business</b>.\n\n"
        "1️⃣ Подключите меня в <b>Настройки ➔ Telegram Business ➔ Чат-боты</b>.\n"
        "2️⃣ Все удалённые сообщения и медиафайлы из ваших бизнес-чатов будут приходить прямо сюда!\n\n"
        "✨ <b>Команды в бизнес-чатах:</b>\n"
        "<code>.tsundere текст</code> — сообщение в стиле цундере\n"
        "<code>.kawaii текст</code> — пикми-режим\n\n"
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


# 2. Входящие бизнес-сообщения
@dp.business_message()
async def handle_business_message(message: types.Message):
    try:
        if await handle_style_command(message):
            return
    except Exception as e:
        logging.error(f"Ошибка в handle_style_command: {e}")

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
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )
    finally:
        await runner.cleanup()
        if db_conn:
            await db_conn.close()


if __name__ == '__main__':
    asyncio.run(main())
