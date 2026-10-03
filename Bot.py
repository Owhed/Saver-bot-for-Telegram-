import logging
import asyncio
import aiosqlite
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

# ========================================================
# ⚠️ ВСТАВЬТЕ СВОЙ ТОКЕН БОТА НИЖЕ
API_TOKEN = '8848306379:AAGAgwUsiXXFIBC5Bfh08G8OTtufxEHAx0s'
# ========================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
DB_NAME = "messages_cache.db"
db_conn = None


async def init_db():
    global db_conn
    db_conn = await aiosqlite.connect(DB_NAME)
    
    # Таблица для хранения сообщений
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER, 
            chat_id INTEGER, 
            sender_name TEXT,
            username TEXT, 
            text_content TEXT, 
            file_id TEXT, 
            media_type TEXT,
            owner_user_id INTEGER,
            PRIMARY KEY (message_id, chat_id)
        )
    ''')
    
    # Таблица для связывания business_connection_id с Telegram ID владельца
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS connections (
            connection_id TEXT PRIMARY KEY,
            user_id INTEGER
        )
    ''')
    
    await db_conn.commit()
    logging.info("База данных SQLite успешно запущена!")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    welcome_text = (
        "Привет я на вайбкодил это с помощью Gemeni и он находится на слабом хосте,так что не надейтесь на быстродействие"
    )
    await message.answer(welcome_text)


# Сохранение подключения Telegram Business
@dp.business_connection()
async def handle_business_connection(connection: types.BusinessConnection):
    try:
        await db_conn.execute(
            "INSERT OR REPLACE INTO connections (connection_id, user_id) VALUES (?, ?)",
            (connection.id, connection.user.id)
        )
        await db_conn.commit()
        logging.info(f"[ПОДКЛЮЧЕНИЕ] Связь {connection.id} закреплена за пользователем {connection.user.id}")
    except Exception as e:
        logging.error(f"Ошибка в handle_business_connection: {e}")


@dp.business_message()
async def handle_business_message(message: types.Message):
    try:
        # Находим, кому принадлежит это бизнес-подключение
        owner_user_id = None
        if message.business_connection_id:
            async with db_conn.execute(
                "SELECT user_id FROM connections WHERE connection_id = ?",
                (message.business_connection_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    owner_user_id = row[0]

        # Если не нашли по connection_id, берем ID отправителя как запасной вариант
        if not owner_user_id and message.from_user:
            owner_user_id = message.from_user.id

        content = message.text or message.caption or ""
        file_id = None
        media_type = None

        if message.photo:
            file_id = message.photo[-1].file_id
            media_type = "photo"
            if not content:
                content = "[Фотография]"
        elif message.voice:
            file_id = message.voice.file_id
            media_type = "voice"
            content = "[Голосовое сообщение]"
        elif message.video:
            file_id = message.video.file_id
            media_type = "video"
            if not content:
                content = "[Видео]"
        elif message.sticker:
            content = f"[Стикер: {message.sticker.emoji or ''}]"

        sender = message.from_user.full_name if message.from_user else "Собеседник"
        username = f"@{message.from_user.username}" if message.from_user and message.from_user.username else "@нет_юзернейма"

        await db_conn.execute(
            "INSERT OR REPLACE INTO messages (message_id, chat_id, sender_name, username, text_content, file_id, media_type, owner_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (message.message_id, message.chat.id, sender, username, content, file_id, media_type, owner_user_id)
        )
        await db_conn.commit()
        logging.info(f"[БАЗА] Сохранено сообщение ID {message.message_id} для пользователя {owner_user_id}")
    except Exception as e:
        logging.error(f"Ошибка в handle_business_message: {e}")


@dp.deleted_business_messages()
async def handle_deleted_business_messages(event: types.BusinessMessagesDeleted):
    logging.info(f"[УДАЛЕНИЕ] Обнаружено событие удаления ID: {event.message_ids}")
    try:
        # Определяем владельца по connection_id
        owner_user_id = None
        if event.business_connection_id:
            async with db_conn.execute(
                "SELECT user_id FROM connections WHERE connection_id = ?",
                (event.business_connection_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    owner_user_id = row[0]

        for msg_id in event.message_ids:
            async with db_conn.execute(
                "SELECT sender_name, username, text_content, file_id, media_type, owner_user_id FROM messages WHERE message_id = ? AND chat_id = ?",
                (msg_id, event.chat.id)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    sender_name, username, text_content, file_id, media_type, saved_owner_id = row
                    
                    target_user_id = owner_user_id or saved_owner_id
                    
                    if not target_user_id:
                        logging.warning(f"[ОШИБКА] Не удалось определить владельца для сообщения {msg_id}")
                        continue

                    notification = (
                        f"⚠️ <b>Удалено сообщение!</b>\n\n"
                        f"👤 <b>Отправитель:</b> {sender_name} ({username})\n"
                        f"📝 <b>Текст:</b> {text_content}"
                    )

                    # Отправляем в ЛС конкретному владельцу
                    if media_type == "photo" and file_id:
                        try:
                            await bot.send_photo(
                                chat_id=target_user_id, 
                                photo=file_id, 
                                caption=notification, 
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logging.error(f"Не удалось отправить фото пользователю {target_user_id}: {e}")
                            await bot.send_message(
                                chat_id=target_user_id, 
                                text=notification, 
                                parse_mode="HTML"
                            )
                    else:
                        await bot.send_message(
                            chat_id=target_user_id, 
                            text=notification, 
                            parse_mode="HTML"
                        )

                    await db_conn.execute("DELETE FROM messages WHERE message_id = ? AND chat_id = ?", (msg_id, event.chat.id))
                    await db_conn.commit()
                else:
                    logging.warning(f"[БАЗА] Сообщение ID {msg_id} не найдено")
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
    
    # Удаляем вебхук и сбрасываем зависшие обновления
    await bot.delete_webhook(drop_pending_updates=True)
    
    try:
        await dp.start_polling(bot)
    finally:
        if db_conn:
            await db_conn.close()


if __name__ == '__main__':
    asyncio.run(main())
