import os
import telebot
import psycopg2
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = telebot.TeleBot(TOKEN)
user_data = {}


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("Создать заявку"))
    return markup


@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(
        message.chat.id,
        "Привет. Нажмите 'Создать заявку'",
        reply_markup=main_menu()
    )


@bot.message_handler(commands=['id'])
def get_id(message):
    bot.send_message(message.chat.id, f"Ваш Telegram ID: {message.chat.id}")


@bot.message_handler(func=lambda message: message.text == "Создать заявку")
def create_order(message):
    user_data[message.chat.id] = {}
    msg = bot.send_message(message.chat.id, "Введите вид услуги:")
    bot.register_next_step_handler(msg, get_service)


def get_service(message):
    user_data[message.chat.id]["service_type"] = message.text
    msg = bot.send_message(message.chat.id, "Введите цену в USDT:")
    bot.register_next_step_handler(msg, get_price)


def get_price(message):
    try:
        user_data[message.chat.id]["price"] = int(message.text)
    except ValueError:
        msg = bot.send_message(message.chat.id, "Введите цену числом, например 288")
        bot.register_next_step_handler(msg, get_price)
        return

    msg = bot.send_message(message.chat.id, "Введите контакт клиента (@username или номер):")
    bot.register_next_step_handler(msg, get_contact)


def get_contact(message):
    user_data[message.chat.id]["contact_text"] = message.text
    msg = bot.send_message(message.chat.id, "Формат: Incall или Outcall?")
    bot.register_next_step_handler(msg, get_format)


def get_format(message):
    user_data[message.chat.id]["incall_outcall"] = message.text
    msg = bot.send_message(message.chat.id, "Время от:")
    bot.register_next_step_handler(msg, get_time_from)


def get_time_from(message):
    user_data[message.chat.id]["time_from"] = message.text
    msg = bot.send_message(message.chat.id, "Время до:")
    bot.register_next_step_handler(msg, get_time_to)


def get_time_to(message):
    user_data[message.chat.id]["time_to"] = message.text
    msg = bot.send_message(message.chat.id, "Профиль:")
    bot.register_next_step_handler(msg, get_profile)


def get_profile(message):
    data = user_data[message.chat.id]
    data["profile_name"] = message.text

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO orders (
            service_type,
            price,
            client_telegram_id,
            client_username,
            contact_text,
            incall_outcall,
            time_from,
            time_to,
            profile_name,
            status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'NEW')
        RETURNING id
    """, (
        data["service_type"],
        data["price"],
        message.chat.id,
        message.from_user.username,
        data["contact_text"],
        data["incall_outcall"],
        data["time_from"],
        data["time_to"],
        data["profile_name"]
    ))

    order_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    send_order_to_masters(order_id, data)

    bot.send_message(message.chat.id, f"Заявка #{order_id} создана и отправлена мастерам.")
    user_data.pop(message.chat.id, None)


def send_order_to_masters(order_id, data):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT telegram_id
        FROM masters
        WHERE is_active = TRUE AND is_online = TRUE
    """)
    masters = cur.fetchall()

    text = f"""🆕 Новая заявка #{order_id}

Вид услуги: {data['service_type']}
Цена: {data['price']} USDT
Клиент: {data['contact_text']}
Формат: {data['incall_outcall']}
Время: {data['time_from']}-{data['time_to']}
Профиль: {data['profile_name']}
"""

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Принять", callback_data=f"accept_{order_id}"))

    for master in masters:
        telegram_id = master[0]
        try:
            bot.send_message(telegram_id, text, reply_markup=kb)
        except Exception as e:
            print(f"Не удалось отправить мастеру {telegram_id}: {e}")

    cur.close()
    conn.close()


@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_"))
def accept_order(call):
    order_id = int(call.data.split("_")[1])
    master_id = call.from_user.id

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT status FROM orders WHERE id = %s", (order_id,))
    row = cur.fetchone()

    if not row:
        bot.answer_callback_query(call.id, "Заказ не найден")
        cur.close()
        conn.close()
        return

    status = row[0]

    if status != "NEW":
        bot.answer_callback_query(call.id, "Заказ уже забрал другой мастер")
        cur.close()
        conn.close()
        return

    cur.execute("""
        UPDATE orders
        SET status = 'ASSIGNED', master_telegram_id = %s
        WHERE id = %s AND status = 'NEW'
    """, (master_id, order_id))

    if cur.rowcount == 1:
        conn.commit()
        bot.answer_callback_query(call.id, "Заказ ваш")
        bot.send_message(master_id, f"✅ Ты взял заказ #{order_id}")
    else:
        conn.rollback()
        bot.answer_callback_query(call.id, "Заказ уже забрал другой мастер")

    cur.close()
    conn.close()


print("Bot started...")
bot.infinity_polling()
