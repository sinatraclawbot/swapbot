import os
import telebot
import psycopg2
from flask import Flask, request
from telebot.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from group_worker import create_order_group

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is not set")

WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}/webhook/{BOT_TOKEN}"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
user_data = {}


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("Создать заявку"))
    return markup


@bot.message_handler(commands=["start"])
def start(message):
    print("START HANDLER FIRED", message.chat.id, message.text)
    try:
        bot.send_message(
            message.chat.id,
            "Привет. Нажмите 'Создать заявку'",
            reply_markup=main_menu(),
        )
        print("START MESSAGE SENT")
    except Exception as e:
        print("START ERROR:", repr(e))


@bot.message_handler(commands=["id"])
def get_id(message):
    print("ID HANDLER FIRED", message.chat.id, message.text)
    try:
        bot.send_message(message.chat.id, f"Ваш Telegram ID: {message.chat.id}")
        print("ID MESSAGE SENT")
    except Exception as e:
        print("ID ERROR:", repr(e))


@bot.message_handler(func=lambda message: message.text == "Создать заявку")
def create_order(message):
    print("CREATE ORDER HANDLER FIRED", message.chat.id)
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

    msg = bot.send_message(
        message.chat.id,
        "Введите контакт клиента (@username или номер):",
    )
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
    try:
        data = user_data[message.chat.id]
        data["profile_name"] = message.text

        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            """
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
            """,
            (
                data["service_type"],
                data["price"],
                message.chat.id,
                message.from_user.username,
                data["contact_text"],
                data["incall_outcall"],
                data["time_from"],
                data["time_to"],
                data["profile_name"],
            ),
        )

        order_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        send_order_to_masters(order_id, data)

        bot.send_message(
            message.chat.id,
            f"Заявка #{order_id} создана и отправлена мастерам.",
        )
        user_data.pop(message.chat.id, None)

    except Exception as e:
        print("GET_PROFILE ERROR:", repr(e))
        try:
            bot.send_message(message.chat.id, f"Ошибка: {e}")
        except Exception as send_err:
            print("SEND ERROR IN GET_PROFILE:", repr(send_err))


def send_order_to_masters(order_id, data):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT telegram_id
        FROM masters
        WHERE is_active = TRUE AND is_online = TRUE
        """
    )
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
            print(f"ORDER SENT TO MASTER: {telegram_id}")
        except Exception as e:
            print(f"SEND ORDER ERROR TO MASTER {telegram_id}: {repr(e)}")

    cur.close()
    conn.close()


@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_"))
def accept_order(call):
    print("ACCEPT HANDLER FIRED", call.data, call.from_user.id)

    order_id = int(call.data.split("_")[1])
    master_id = call.from_user.id

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE orders
        SET status = 'ASSIGNED',
            master_telegram_id = %s
        WHERE id = %s
          AND status = 'NEW'
        RETURNING client_telegram_id
        """,
        (master_id, order_id),
    )

    row = cur.fetchone()

    if not row:
        conn.rollback()
        cur.close()
        conn.close()
        try:
            bot.answer_callback_query(call.id, "Заказ уже забрал другой мастер")
        except Exception as e:
            print("CALLBACK ERROR:", repr(e))
        return

    client_id = row[0]
    conn.commit()
    cur.close()
    conn.close()

    try:
        invite_link = create_order_group(order_id)
        print("GROUP CREATED:", invite_link)
    except Exception as e:
        print("GROUP CREATE ERROR:", repr(e))

        try:
            bot.send_message(master_id, f"❌ Ошибка создания чата для заказа #{order_id}")
        except Exception as send_err:
            print("SEND TO MASTER ERROR:", repr(send_err))

        try:
            bot.send_message(client_id, f"❌ Ошибка создания чата для заказа #{order_id}")
        except Exception as send_err:
            print("SEND TO CLIENT ERROR:", repr(send_err))

        try:
            bot.answer_callback_query(call.id, "Ошибка создания чата")
        except Exception as callback_err:
            print("CALLBACK ERROR:", repr(callback_err))

        return

    if not invite_link:
        try:
            bot.send_message(master_id, f"❌ Не удалось создать чат для заказа #{order_id}")
        except Exception as send_err:
            print("SEND TO MASTER ERROR:", repr(send_err))

        try:
            bot.send_message(client_id, f"❌ Не удалось создать чат для заказа #{order_id}")
        except Exception as send_err:
            print("SEND TO CLIENT ERROR:", repr(send_err))

        try:
            bot.answer_callback_query(call.id, "Ошибка создания чата")
        except Exception as callback_err:
            print("CALLBACK ERROR:", repr(callback_err))

        return

    try:
        bot.send_message(
            client_id,
            f"✅ Мастер принял заявку #{order_id}\nВот ссылка в чат:\n{invite_link}",
        )
    except Exception as e:
        print("SEND TO CLIENT ERROR:", repr(e))

    try:
        bot.send_message(
            master_id,
            f"✅ Заказ #{order_id} ваш\nВот ссылка в чат:\n{invite_link}",
        )
    except Exception as e:
        print("SEND TO MASTER ERROR:", repr(e))

    try:
        bot.answer_callback_query(call.id, "Заказ ваш")
    except Exception as e:
        print("CALLBACK ERROR:", repr(e))


@app.route("/", methods=["GET"])
def index():
    return "Bot is running", 200


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        json_str = request.get_data().decode("utf-8")
        print("WEBHOOK HIT:", json_str)

        update = telebot.types.Update.de_json(json_str)
        print("UPDATE PARSED")

        bot.process_new_updates([update])
        print("UPDATE PROCESSED")

    except Exception as e:
        print("WEBHOOK ERROR:", repr(e))
    return "OK", 200


def setup_webhook():
    try:
        bot.remove_webhook()
        result = bot.set_webhook(url=WEBHOOK_URL)
        print(f"Webhook set: {result} -> {WEBHOOK_URL}")
    except Exception as e:
        print("SET WEBHOOK ERROR:", repr(e))


setup_webhook()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    print(f"Bot webhook server started on port {port}")
    app.run(host="0.0.0.0", port=port)
