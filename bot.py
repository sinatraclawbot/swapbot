import telebot
import os

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)


@bot.message_handler(commands=['start'])
def start(message):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn = telebot.types.KeyboardButton("Создать заявку")
    markup.add(btn)
    bot.send_message(message.chat.id, "Привет. Нажмите 'Создать заявку'", reply_markup=markup)


@bot.message_handler(commands=['id'])
def get_id(message):
    bot.send_message(message.chat.id, f"Ваш Telegram ID: {message.chat.id}")


@bot.message_handler(func=lambda message: message.text == "Создать заявку")
def create_order(message):
    bot.send_message(message.chat.id, "Введите вид услуги:")


print("Bot started...")
bot.polling()
