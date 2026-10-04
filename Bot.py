import logging
import asyncio
import aiosqlite
import os
import html
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

# ========================================================
API_TOKEN = os.getenv('BOT_TOKEN', '8848306379:AAGAgwUsiXXFIBC5Bfh08G8OTtufxEHAx0s')
# ========================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
DB_NAME = "messages_cache.db"
db_conn = None


async def init_db():
    global db_conn
    db_conn = await aiosqlite.connect(DB_NAME)
    
    # Таблица сохраненных сообщений
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
    
    # Таблица связей: connection_id -> Telegram ID владельца
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS connections (
            connection_id TEXT PRIMARY KEY,
            owner_user_id INTEGER
        )
    ''')
    
    await db_conn.commit()
    logging.info("База данных SQLite успешно запущена!")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    welcome_text = (
        "👋 Привет!\n\n"
        "Я бот для сохранения удалённых сообщений из **Telegram Business**.\n\n"
        "1️⃣ Подключите меня в **Настройки ➔ Telegram Business ➔ Чат-боты**.\n"
        "2️⃣ Все удалённые сообщения и медиафайлы из ваших бизнес-чатов будут приходить прямо сюда!"
    )
    await message.answer(welcome_text, parse_mode="Markdown")


# Вспомогательная функция для гарантированного получения и сохранения владельца
async def ensure_connection_owner(connection_id: str) -> int | None:
    if not connection_id:
        return None

    # Проверяем в локальной БД
    async with db_conn.execute(
        "SELECT owner_user_id FROM connections WHERE connection_id = ?",
        (connection_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if row and row[0]:
            return row[0]

    # Если в БД нет, запрашиваем напрямую у Telegram API
    try:
        conn_info = await bot.get_business_connection(business_connection_id=connection_id)
        if conn_info and conn_info.user:
            owner_id = conn_info.user.id
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


# 1. Обработка подключения/отключения бота к Telegram Business
@dp.business_connection()
async def handle_business_connection(connection: types.BusinessConnection):
    try:
        if connection.is_enabled:
            await db_conn.execute(
                "INSERT OR REPLACE INTO connections (connection_id, owner_user_id) VALUES (?, ?)",
                (connection.id, connection.user.id)
            )
            await db_conn.commit()
            logging.info(f"[БИЗНЕС] Связь {connection.id} успешно закреплена за пользователем {connection.user.id}")
        else:
            await db_conn.execute("DELETE FROM connections WHERE connection_id = ?", (connection.id,))
            await db_conn.commit()
            logging.info(f"[БИЗНЕС] Связь {connection.id} была удалена.")
    except Exception as e:
        logging.error(f"Ошибка в handle_business_connection: {e}")


# 2. Обработка входящих бизнес-сообщений
@dp.business_message()
async def handle_business_message(message: types.Message):
    try:
        content = message.text or message.caption or ""
        file_id = None
        media_type = None

        if message.photo:
            file_id = message.photo[-1].file_id
            media_type = "photo"
            if not content: content = "[Фотография]"
        elif message.voice:
            file_id = message.voice.file_id
            media_type = "voice"
            if not content: content = "[Голосовое сообщение]"
        elif message.audio:
            file_id = message.audio.file_id
            media_type = "audio"
            if not content: content = f"[Аудиозапись: {message.audio.title or 'без названия'}]"
        elif message.video_note:
            file_id = message.video_note.file_id
            media_type = "video_note"
            if not content: content = "[Видеосообщение (кружочек)]"
        elif message.video:
            file_id = message.video.file_id
            media_type = "video"
            if not content: content = "[Видео]"
        elif message.sticker:
            file_id = message.sticker.file_id
            media_type = "sticker"
            if not content: content = f"[Стикер: {message.sticker.emoji or ''}]"
        elif message.document:
            file_id = message.document.file_id
            media_type = "document"
            if not content: content = f"[Документ: {message.document.file_name or 'файл'}]"

        sender = message.from_user.full_name if message.from_user else "Собеседник"
        username = f"@{message.from_user.username}" if message.from_user and message.from_user.username else "@нет_юзернейма"
        conn_id = message.business_connection_id

        # Автоматически проверяем и привязываем владельца при каждом сообщении
        if conn_id:
            await ensure_connection_owner(conn_id)

        await db_conn.execute(
            "INSERT OR REPLACE INTO messages (message_id, chat_id, sender_name, username, text_content, file_id, media_type, connection_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (message.message_id, message.chat.id, sender, username, content, file_id, media_type, conn_id)
        )
        await db_conn.commit()
        logging.info(f"[БАЗА] Сохранено сообщение ID {message.message_id} ({media_type or 'текст'})")
    except Exception as e:
        logging.error(f"Ошибка в handle_business_message: {e}")


# 3. Обработка удаления сообщений
@dp.deleted_business_messages()
async def handle_deleted_business_messages(event: types.BusinessMessagesDeleted):
    logging.info(f"[УДАЛЕНИЕ] Обнаружено событие удаления ID: {event.message_ids}")
    try:
        conn_id = event.business_connection_id
        owner_user_id = await ensure_connection_owner(conn_id) if conn_id else None

        unique_message_ids = set(event.message_ids)

        for msg_id in unique_message_ids:
            async with db_conn.execute(
                "SELECT sender_name, username, text_content, file_id, media_type, connection_id FROM messages WHERE message_id = ? AND chat_id = ?",
                (msg_id, event.chat.id)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    sender_name, username, text_content, file_id, media_type, saved_conn_id = row
                    
                    target_user_id = owner_user_id
                    if not target_user_id and saved_conn_id:
                        target_user_id = await ensure_connection_owner(saved_conn_id)

                    if not target_user_id:
                        logging.warning(f"[ОШИБКА] Не удалось найти владельца аккаунта для сообщения {msg_id}")
                        continue

                    # Удаляем из БД сразу после нахождения
                    await db_conn.execute("DELETE FROM messages WHERE message_id = ? AND chat_id = ?", (msg_id, event.chat.id))
                    await db_conn.commit()

                    notification = (
                        f"⚠️ <b>Удалено сообщение!</b>\n\n"
                        f"👤 <b>Отправитель:</b> {html.quote(sender_name)} ({html.quote(username)})\n"
                        f"📝 <b>Текст/Описание:</b> {html.quote(text_content)}"
                    )

                    try:
                        if media_type == "photo" and file_id:
                            await bot.send_photo(chat_id=target_user_id, photo=file_id, caption=notification, parse_mode="HTML")
                        elif media_type == "video" and file_id:
                            await bot.send_video(chat_id=target_user_id, video=file_id, caption=notification, parse_mode="HTML")
                        elif media_type == "audio" and file_id:
                            await bot.send_audio(chat_id=target_user_id, audio=file_id, caption=notification, parse_mode="HTML")
                        elif media_type == "document" and file_id:
                            await bot.send_document(chat_id=target_user_id, document=file_id, caption=notification, parse_mode="HTML")
                        elif media_type == "video_note" and file_id:
                            await bot.send_message(chat_id=target_user_id, text=notification, parse_mode="HTML")
                            await bot.send_video_note(chat_id=target_user_id, video_note=file_id)
                        elif media_type == "voice" and file_id:
                            await bot.send_message(chat_id=target_user_id, text=notification, parse_mode="HTML")
                            await bot.send_voice(chat_id=target_user_id, voice=file_id)
                        elif media_type == "sticker" and file_id:
                            await bot.send_message(chat_id=target_user_id, text=notification, parse_mode="HTML")
                            await bot.send_sticker(chat_id=target_user_id, sticker=file_id)
                        else:
                            await bot.send_message(chat_id=target_user_id, text=notification, parse_mode="HTML")
                    except Exception as send_error:
                        logging.error(f"Не удалось отправить логи пользователю {target_user_id}: {send_error}")

                else:
                    logging.warning(f"[БАЗА] Сообщение ID {msg_id} не найдено в базе данных")
    except Exception as e:
        logging.error(f"Ошибка при обработке удаления: {e}")


async def handle_web_request(request):
    return web.Response(text="Бот стабильно активен!")


async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_web_request)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()


async def main():
    await init_db()
    await start_web_server()
    
    # Отключаем вебхуки и делаем паузу для сброса старых сессий
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.sleep(2)
    
    try:
        await dp.start_polling(
            bot, 
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_skip_unhandled=True
        )
    finally:
        if db_conn:
            await db_conn.close()


if __name__ == '__main__':
    asyncio.run(main())

if __name__ == '__main__':
    asyncio.run(main())
