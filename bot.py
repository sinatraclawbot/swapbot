import telebot
import os

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "Привет. Нажмите 'Создать заявку'")

@bot.message_handler(func=lambda message: message.text == "Создать заявку")
def order(message):
    bot.send_message(message.chat.id, "Введите вид услуги:")

bot.polling()
