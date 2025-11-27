import os
import requests
import logging
import sqlite3
import json
import base64
import uuid
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = "8573998335:AAENV4S0UhOUAmc3RpzEeFDLuModI36aqhM"  # Получить у @BotFather
GIGACHAT_CLIENT_ID = "019ac450-7c0b-7686-a4ec-e979dd4fa0f5"  # Получить на developers.sber.ru
GIGACHAT_CLIENT_SECRET = "8dc579fc-56ee-49bd-b8cd-a0cd3fe4ae56"  # Получить на developers.sber.ru


class GigaChatService:
    def __init__(self):
        self.access_token = None
        self.token_expires = None

    def get_access_token(self):
        """Получение access token для GigaChat API"""
        try:
            credentials = f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()

            url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'Basic {encoded_credentials}',
                'Accept': 'application/json',
                'RqUID': str(uuid.uuid4())
            }
            data = {'scope': 'GIGACHAT_API_PERS'}

            response = requests.post(url, headers=headers, data=data, verify=False)

            if response.status_code == 200:
                token_data = response.json()
                self.access_token = token_data.get('access_token')
                logger.info("✅ GigaChat token получен успешно")
                return self.access_token
            else:
                logger.error(f"❌ Ошибка получения token: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"❌ Ошибка при получении token: {e}")
            return None

    def send_message(self, text):
        """Отправка сообщения в GigaChat"""
        try:
            if not self.access_token:
                if not self.get_access_token():
                    return None

            url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json'
            }

            data = {
                "model": "GigaChat",
                "messages": [
                    {
                        "role": "system",
                        "content": "Ты - полезный помощник для школьников и студентов. Отвечай кратко, понятно и по делу. Помогай с учебой, объяснением тем, домашними заданиями и организацией времени."
                    },
                    {
                        "role": "user",
                        "content": text
                    }
                ],
                "temperature": 0.7,
                "max_tokens": 1000
            }

            response = requests.post(url, headers=headers, json=data, verify=False)

            if response.status_code == 200:
                result = response.json()
                return result['choices'][0]['message']['content']
            else:
                logger.error(f"❌ Ошибка API: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"❌ Ошибка при запросе к GigaChat: {e}")
            return None


class TelegramBot:
    def __init__(self):
        self.gigachat = GigaChatService()
        self.init_db()

    def init_db(self):
        """Инициализация базы данных SQLite"""
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message TEXT,
                response TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        conn.close()

    def save_conversation(self, user_id, message, response):
        """Сохранение диалога в базу данных"""
        conn = sqlite3.connect('bot_data.db')
        cursor = conn.cursor()

        cursor.execute(
            'INSERT INTO conversations (user_id, message, response) VALUES (?, ?, ?)',
            (user_id, message, response)
        )

        conn.commit()
        conn.close()

    async def start(self, update: Update, context: CallbackContext):
        """Обработчик команды /start"""
        user = update.effective_user
        keyboard = [
            [KeyboardButton("📚 Помощь с учебой"), KeyboardButton("🤖 Задать вопрос")],
            [KeyboardButton("📅 Планирование"), KeyboardButton("ℹ️ О боте")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        await update.message.reply_text(
            f"Привет, {user.first_name}! 👋\n\n"
            "Я твой умный помощник с интеграцией GigaChat! 🤖\n\n"
            "Что я умею:\n"
            "• Отвечать на вопросы по учебе\n"
            "• Помогать с домашними заданиями\n"
            "• Объяснять сложные темы\n"
            "• Помогать с планированием\n\n"
            "Выбери действие ниже или просто напиши свой вопрос!",
            reply_markup=reply_markup
        )

    async def handle_message(self, update: Update, context: CallbackContext):
        """Обработка текстовых сообщений"""
        user_message = update.message.text
        user_id = update.effective_user.id

        # Показываем индикатор набора сообщения
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # Обработка кнопок быстрого доступа
        if user_message == "📚 Помощь с учебой":
            response = "По каким предметам тебе нужна помощь?\n\nНапример:\n• Математика\n• Физика\n• Русский язык\n• История\n• Английский язык\n\nИли задай конкретный вопрос!"
        elif user_message == "🤖 Задать вопрос":
            response = "Задай свой вопрос, и я постараюсь помочь! 🤔"
        elif user_message == "📅 Планирование":
            response = "Я могу помочь с:\n• Планированием учебы\n• Составлением расписания\n• Тайм-менеджментом\n\nЧто именно тебя интересует?"
        elif user_message == "ℹ️ О боте":
            response = (
                "🤖 **Информация о боте:**\n\n"
                "• Использует нейросеть GigaChat для ответов\n"
                "• Помогает с учебой и планированием\n"
                "• Сохраняет историю диалогов\n"
                "• Работает 24/7\n\n"
                "Просто напиши свой вопрос!"
            )
        else:
            # Отправляем запрос в GigaChat
            response = self.gigachat.send_message(user_message)

            if not response:
                response = (
                    "❌ К сожалению, GigaChat временно недоступен.\n\n"
                    "Попробуй:\n"
                    "• Переформулировать вопрос\n"
                    "• Задать вопрос позже\n"
                    "• Использовать кнопки быстрого доступа"
                )
            else:
                # Сохраняем диалог в базу данных
                self.save_conversation(user_id, user_message, response)

        await update.message.reply_text(response)

    async def error_handler(self, update: Update, context: CallbackContext):
        """Обработка ошибок"""
        logger.error(f"Ошибка: {context.error}")

        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Произошла ошибка. Пожалуйста, попробуйте еще раз."
            )

    def run(self):
        """Запуск бота"""
        # Создаем приложение
        application = Application.builder().token(TELEGRAM_TOKEN).build()

        # Добавляем обработчики
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        # Обработчик ошибок
        application.add_error_handler(self.error_handler)

        # Запускаем бота
        logger.info("🤖 Бот запускается...")
        application.run_polling()


def main():
    """Основная функция"""
    # Проверяем наличие необходимых токенов
    if TELEGRAM_TOKEN == "ВАШ_TELEGRAM_ТОКЕН" or GIGACHAT_CLIENT_ID == "ВАШ_CLIENT_ID":
        print("❌ Пожалуйста, настройте токены в коде:")
        print("1. TELEGRAM_TOKEN - получите у @BotFather")
        print("2. GIGACHAT_CLIENT_ID и GIGACHAT_CLIENT_SECRET - получите на developers.sber.ru")
        return

    # Создаем и запускаем бота
    bot = TelegramBot()
    bot.run()


if __name__ == '__main__':
    main()