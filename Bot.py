import logging
import asyncio
import aiosqlite
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

# ========================================================
API_TOKEN = '8848306379:AAGAgwUsiXXFIBC5Bfh08G8OTtufxEHAx0s'
# ========================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
DB_NAME = "messages_cache.db"
db_conn = None

pvp_games = {}

async def init_db():
    global db_conn
    db_conn = await aiosqlite.connect(DB_NAME)
    await db_conn.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER, chat_id INTEGER, sender_name TEXT,
            username TEXT, text_content TEXT, file_id TEXT, media_type TEXT,
            PRIMARY KEY (message_id, chat_id)
        )
    ''')
    await db_conn.commit()
    logging.info("База данных SQLite успешно запущена!")

def get_ttt_keyboard(board):
    builder = InlineKeyboardBuilder()
    for i in range(9):
        builder.button(text=board[i], callback_data=f"ttt_{i}")
    builder.adjust(3)
    return builder.as_markup()

def check_winner(b):
    lines = [(0,1,2), (3,4,5), (6,7,8), (0,3,6), (1,4,7), (2,5,8), (0,4,8), (2,4,6)]
    for x, y, z in lines:
        if b[x] == b[y] == b[z] and b[x] != '⬜':
            return b[x]
    if '⬜' not in b:
        return 'Ничья'
    return None

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    welcome_text = (
        "Привет! Это бот для Telegram Business.\n\n"
        "🛠 Доступные команды в чатах:\n"
        "ℹ️ .help — Показать меню помощи.\n"
        "🎮 .ttt — Запустить Крестики-Нолики с собеседником."
    )
    await message.answer(welcome_text)

@dp.business_message()
async def handle_business_message(message: types.Message):
    try:
        content = message.text or message.caption or ""
        clean_content = content.strip().lower()
        
        # Обработка команд игры
        if clean_content == ".help":
            help_text = "🛠 Список команд:\n\n🎮 .ttt — Запустить совместную игру в Крестики-Нолики."
            await bot.send_message(
                chat_id=message.chat.id, 
                text=help_text, 
                business_connection_id=message.business_connection_id
            )
            return

        elif clean_content == ".ttt":
            user_id = message.from_user.id
            pvp_games[message.chat.id] = {
                "board": ['⬜'] * 9,
                "player_x": user_id,
                "player_o": None,
                "turn": user_id,
                "x_name": message.from_user.first_name,
                "o_name": "Ожидание соперника..."
            }
            
            await bot.send_message(
                chat_id=message.chat.id,
                text=f"🎮 Игра Крестики-Нолики (PvP)!\n\n❌ Ходит: {message.from_user.first_name}\n⭕ Ожидание второго игрока...",
                reply_markup=get_ttt_keyboard(['⬜'] * 9),
                business_connection_id=message.business_connection_id
            )
            return

        # Сохранение входящих файлов и сообщений в базу
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
        elif message.sticker:
            content = f"[Стикер: {message.sticker.emoji or ''}]"

        sender = message.from_user.full_name if message.from_user else "Собеседник"
        username = f"@{message.from_user.username}" if message.from_user and message.from_user.username else "@нет_юзернейма"

        await db_conn.execute(
            "INSERT OR REPLACE INTO messages (message_id, chat_id, sender_name, username, text_content, file_id, media_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message.message_id, message.chat.id, sender, username, content, file_id, media_type)
        )
        await db_conn.commit()
        logging.info(f"[БАЗА] Сохранено сообщение ID {message.message_id} от {username}")
    except Exception as e:
        logging.error(f"Ошибка в handle_business_message: {e}")

@dp.callback_query(lambda c: c.data.startswith('ttt_'))
async def process_ttt_move(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    clicker_id = callback_query.from_user.id
    clicker_name = callback_query.from_user.first_name

    if chat_id not in pvp_games:
        await callback_query.answer("Игра завершена. Напишите .ttt заново.", show_alert=True)
        return

    game = pvp_games[chat_id]
    board = game["board"]
    index = int(callback_query.data.split('_')[1])

    # Автоматическое присоединение второго игрока
    if game["player_o"] is None and clicker_id != game["player_x"]:
        game["player_o"] = clicker_id
        game["o_name"] = clicker_name

    # Проверка, участвует ли игрок
    if clicker_id != game["player_x"] and clicker_id != game["player_o"]:
        await callback_query.answer("Вы не участвуете в этой игре!", show_alert=True)
        return

    # Проверка очередности хода
    if clicker_id != game["turn"]:
        await callback_query.answer("Сейчас не ваш ход!", show_alert=True)
        return

    # Проверка, свободна ли клетка
    if board[index] != '⬜':
        await callback_query.answer("Эта клетка уже занята!", show_alert=True)
        return

    # Ход
    if clicker_id == game["player_x"]:
        board[index] = '❌'
        game["turn"] = game["player_o"]
        next_turn_name = game["o_name"]
    else:
        board[index] = '⭕'
        game["turn"] = game["player_x"]
        next_turn_name = game["x_name"]

    await callback_query.answer()

    winner = check_winner(board)
    if winner:
        if winner == '❌':
            text = f"🎉 Игра окончена! Победил: {game['x_name']} ❌"
        elif winner == '⭕':
            text = f"🎉 Игра окончена! Победил: {game['o_name']} ⭕"
        else:
            text = "🤝 Игра окончена! Ничья!"
        
        try:
            await bot.edit_message_text(text=text, chat_id=chat_id, message_id=callback_query.message.message_id, reply_markup=get_ttt_keyboard(board))
        except Exception:
            pass
        del pvp_games[chat_id]
        return

    status_text = f"🎮 Крестики-Нолики (PvP)!\n\n❌ Игрок 1: {game['x_name']}\n⭕ Игрок 2: {game['o_name']}\n\n👉 Сейчас очередь: {next_turn_name}"
    try:
        await bot.edit_message_text(text=status_text, chat_id=chat_id, message_id=callback_query.message.message_id, reply_markup=get_ttt_keyboard(board))
    except Exception:
        pass

@dp.deleted_business_messages()
async def handle_deleted_business_messages(event: types.BusinessMessagesDeleted):
    logging.info(f"[УДАЛЕНИЕ] Обнаружено событие удаления ID: {event.message_ids}")
    try:
        for msg_id in event.message_ids:
            async with db_conn.execute(
                "SELECT sender_name, username, text_content, file_id, media_type FROM messages WHERE message_id = ? AND chat_id = ?",
                (msg_id, event.chat.id)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    sender_name, username, text_content, file_id, media_type = row
                    notification = f"⚠️ Удалено сообщение!\n\n👤 Отправитель: {sender_name} ({username})\n📝 Текст: {text_content}"
                    
                    if media_type == "photo" and file_id:
                        try:
                            await bot.send_photo(
                                chat_id=event.chat.id, 
                                photo=file_id, 
                                caption=notification, 
                                business_connection_id=event.business_connection_id
                            )
                        except Exception as e:
                            logging.error(f"Не удалось отправить фото: {e}")
                            await bot.send_message(
                                chat_id=event.chat.id, 
                                text=notification, 
                                business_connection_id=event.business_connection_id
                            )
                    else:
                        await bot.send_message(
                            chat_id=event.chat.id, 
                            text=notification, 
                            business_connection_id=event.business_connection_id
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
    try:
        await dp.start_polling(bot)
    finally:
        if db_conn:
            await db_conn.close()

if __name__ == '__main__':
    asyncio.run(main())
