import logging
import asyncio
import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage

# ========================================================
# ⚠️ ОБЯЗАТЕЛЬНО ЗАПОЛНИТЕ ЭТИ ДВА ПОЛЯ:
API_TOKEN = '8848306379:AAEK3W-tIR456Y9ce7tOzbbIkKhXZVWQ8sY'
MY_TELEGRAM_ID = 8478742975  # Вставьте сюда ваш ID числами (без кавычек)
# ========================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
DB_NAME = "messages_cache.db"
db_conn = None

async def init_db():
    global db_conn
    try:
        db_conn = await aiosqlite.connect(DB_NAME)
        await db_conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER,
                chat_id INTEGER,
                sender_name TEXT,
                text_content TEXT,
                PRIMARY KEY (message_id, chat_id)
            )
        ''')
        await db_conn.commit()
        logging.info("База данных SQLite успешно инициализирована.")
    except Exception as e:
        logging.error(f"Ошибка инициализации БД: {e}")

@dp.business_message()
async def handle_business_message(message: types.Message):
    """Безопасное сохранение сообщений"""
    try:
        content = message.text or message.caption or ""
        if not content:
            if message.photo: content = "[Фотография]"
            elif message.voice: content = "[Голосовое сообщение]"
            elif message.sticker: content = f"[Стикер: {message.sticker.emoji or ''}]"
            else: content = "[Медиафайл/Документ]"

        sender = message.from_user.full_name if message.from_user else "Собеседник"
        
        await db_conn.execute(
            "INSERT OR REPLACE INTO messages (message_id, chat_id, sender_name, text_content) VALUES (?, ?, ?, ?)",
            (message.message_id, message.chat.id, sender, content)
        )
        await db_conn.commit()
        logging.info(f"Сохранено сообщение от {sender}: {content[:30]}...")
    except Exception as e:
        logging.error(f"Ошибка при сохранении сообщения: {e}")

@dp.deleted_business_messages()
async def handle_deleted_business_messages(event: types.BusinessMessagesDeleted):
    """Безопасная обработка удалений с отправкой ЛИЧНО вам"""
    try:
        for msg_id in event.message_ids:
            async with db_conn.execute(
                "SELECT sender_name, text_content FROM messages WHERE message_id = ? AND chat_id = ?",
                (msg_id, event.chat.id)
            ) as cursor:
                row = await cursor.fetchone()
                
                if row:
                    sender_name, text_content = row
                    
                    notification = (
                        f"⚠️ **Удалено сообщение!**\n\n"
                        f"👤 **Отправитель:** {sender_name}\n"
                        f"📝 **Текст:** {text_content}"
                    )
                    
                    # Отправляем напрямую в ваш личный чат с ботом (это самый стабильный способ)
                    await bot.send_message(chat_id=MY_TELEGRAM_ID, text=notification, parse_mode="Markdown")
                    logging.info(f"Уведомление об удалении успешно отправлено пользователю {MY_TELEGRAM_ID}")
                    
                    await db_conn.execute(
                        "DELETE FROM messages WHERE message_id = ? AND chat_id = ?", 
                        (msg_id, event.chat.id)
                    )
                    await db_conn.commit()
    except Exception as e:
        logging.error(f"Ошибка при обработке удаления сообщения: {e}")

async def main():
    await init_db()
    try:
        await dp.start_polling(bot)
    finally:
        if db_conn:
            await db_conn.close()

if __name__ == '__main__':
    asyncio.run(main())