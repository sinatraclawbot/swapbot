import os
import telebot
import psycopg2
from flask import Flask, request
from group_worker import create_order_group

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

def get_conn():
    return psycopg2.connect(DATABASE_URL)


# --- КНОПКИ ---
def main_menu():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Создать заявку")
    return markup


# --- START ---
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(
        message.chat.id,
        "Привет. Нажмите 'Создать заявку'",
        reply_markup=main_menu()
    )


# --- СОЗДАНИЕ ЗАЯВКИ (пример) ---
@bot.message_handler(func=lambda m: m.text == "Создать заявку")
def create_order(message):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("INSERT INTO orders(status) VALUES('new') RETURNING id")
    order_id = cur.fetchone()[0]
    conn.commit()

    cur.close()
    conn.close()

    bot.send_message(message.chat.id, f"Заявка #{order_id} создана")

    # создаем группу
    try:
        invite_link = create_order_group(order_id)
        bot.send_message(message.chat.id, f"Чат создан: {invite_link}")
    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка создания чата: {e}")


# --- WEBHOOK ---
@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def webhook():
    json_str = request.get_data().decode('UTF-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200


@app.route("/")
def index():
    return "Bot is running"


# --- ЗАПУСК ---
if __name__ == "__main__":
    bot.remove_webhook()

    bot.set_webhook(
        url=f"https://swapbot.onrender.com/{BOT_TOKEN}"
    )

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
