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
ADMIN_CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID")

WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}/webhook/{BOT_TOKEN}"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)
user_data = {}


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def notify_admin(text: str):
    if ADMIN_CHANNEL_ID:
        bot.send_message(ADMIN_CHANNEL_ID, text)


def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("Create Date"))
    return markup


def order_group_keyboard(order_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💰 Paid", callback_data=f"paid_{order_id}"))
    kb.add(InlineKeyboardButton("✅ Done", callback_data=f"done_{order_id}"))
    kb.add(InlineKeyboardButton("⚠️ Dispute", callback_data=f"dispute_{order_id}"))
    return kb


def build_group_status_text(order_id, order_status, payment_status):
    return f"""📦 Date Request #{order_id}

Order status: {order_status}
Payment status: {payment_status}
"""


def date_type_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    buttons = [
        "Lap Dance",
        "Erotic Massage",
        "Tantra Massage",
        "Sugar Date",
        "Romantic Meeting",
        "Watch Adult Content Together",
        "4-Hand Massage",
        "Domina",
        "Champagne Date",
        "Private Video Call",
        "Other",
    ]
    for b in buttons:
        kb.add(InlineKeyboardButton(b, callback_data=f"dt_{b}"))
    return kb


def format_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Incall", callback_data="fmt_Incall"))
    kb.add(InlineKeyboardButton("Outcall", callback_data="fmt_Outcall"))
    return kb


@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "Welcome", reply_markup=main_menu())


@bot.message_handler(commands=["id"])
def get_id(message):
    bot.send_message(message.chat.id, f"Your Telegram ID: {message.chat.id}")
    bot.send_message(message.chat.id, "Menu:", reply_markup=main_menu())


@bot.message_handler(func=lambda message: message.text == "Create Date")
def create_order(message):
    user_data[message.chat.id] = {}
    msg = bot.send_message(message.chat.id, "Enter contact:")
    bot.register_next_step_handler(msg, get_contact)


def get_contact(message):
    user_data[message.chat.id]["contact_text"] = message.text
    bot.send_message(message.chat.id, "Select date type:", reply_markup=date_type_keyboard())


@bot.callback_query_handler(func=lambda call: call.data.startswith("dt_"))
def select_date_type(call):
    date_type = call.data.replace("dt_", "")
    user_data[call.from_user.id]["date_type"] = date_type
    msg = bot.send_message(call.from_user.id, "Enter price (USDT):")
    bot.register_next_step_handler(msg, get_price)
    bot.answer_callback_query(call.id)


def get_price(message):
    try:
        user_data[message.chat.id]["price"] = int(message.text)
    except:
        msg = bot.send_message(message.chat.id, "Enter number:")
        bot.register_next_step_handler(msg, get_price)
        return

    bot.send_message(message.chat.id, "Select format:", reply_markup=format_keyboard())


@bot.callback_query_handler(func=lambda call: call.data.startswith("fmt_"))
def select_format(call):
    fmt = call.data.replace("fmt_", "")
    user_data[call.from_user.id]["format_type"] = fmt
    msg = bot.send_message(call.from_user.id, "Enter time from:")
    bot.register_next_step_handler(msg, get_time_from)
    bot.answer_callback_query(call.id)


def get_time_from(message):
    user_data[message.chat.id]["time_from"] = message.text
    msg = bot.send_message(message.chat.id, "Enter time to:")
    bot.register_next_step_handler(msg, get_time_to)


def get_time_to(message):
    user_data[message.chat.id]["time_to"] = message.text
    msg = bot.send_message(message.chat.id, "Enter profile:")
    bot.register_next_step_handler(msg, save_order)


def save_order(message):
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
            contact_text,
            incall_outcall,
            time_from,
            time_to,
            profile_name,
            status,
            order_status,
            payment_status
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'NEW','NEW','UNPAID')
        RETURNING id
        """,
        (
            data["date_type"],
            data["price"],
            message.chat.id,
            data["contact_text"],
            data["format_type"],
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

    bot.send_message(message.chat.id, f"Date request #{order_id} created", reply_markup=main_menu())
    user_data.pop(message.chat.id, None)

    notify_admin(f"New Date Request #{order_id}")


def send_order_to_masters(order_id, data):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT telegram_id FROM masters WHERE is_active = TRUE AND is_online = TRUE")
    masters = cur.fetchall()

    text = f"""🆕 New Date Request #{order_id}

Contact: {data['contact_text']}
Date type: {data['date_type']}
Price: {data['price']} USDT
Format: {data['format_type']}
Time: {data['time_from']}-{data['time_to']}
Profile: {data['profile_name']}
"""

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Accept", callback_data=f"accept_{order_id}"))

    for master in masters:
        try:
            bot.send_message(master[0], text, reply_markup=kb)
        except:
            pass

    cur.close()
    conn.close()


@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_"))
def accept_order(call):
    order_id = int(call.data.split("_")[1])
    master_id = call.from_user.id

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE orders
        SET status='ASSIGNED', order_status='ASSIGNED', master_telegram_id=%s
        WHERE id=%s AND status='NEW'
        RETURNING client_telegram_id
        """,
        (master_id, order_id),
    )

    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if not row:
        bot.answer_callback_query(call.id, "Already taken")
        return

    client_id = row[0]

    invite_link, group_chat_id = create_order_group(order_id)
    group_chat_id = int(f"-100{group_chat_id}")

    bot.send_message(client_id, f"Chat link:\n{invite_link}", reply_markup=main_menu())
    bot.send_message(master_id, f"Chat link:\n{invite_link}")

    bot.send_message(
        group_chat_id,
        build_group_status_text(order_id, "IN_CHAT", "UNPAID"),
        reply_markup=order_group_keyboard(order_id),
    )

    bot.answer_callback_query(call.id, "Accepted")


@bot.callback_query_handler(func=lambda call: call.data.startswith("paid_"))
def paid(call):
    order_id = int(call.data.split("_")[1])
    bot.edit_message_text(
        build_group_status_text(order_id, "IN_CHAT", "PAID"),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=order_group_keyboard(order_id),
    )
    bot.answer_callback_query(call.id, "Paid")


@bot.callback_query_handler(func=lambda call: call.data.startswith("done_"))
def done(call):
    order_id = int(call.data.split("_")[1])
    bot.edit_message_text(
        build_group_status_text(order_id, "DONE", "PAID"),
        call.message.chat.id,
        call.message.message_id,
    )
    bot.answer_callback_query(call.id, "Done")


@bot.callback_query_handler(func=lambda call: call.data.startswith("dispute_"))
def dispute(call):
    order_id = int(call.data.split("_")[1])
    bot.edit_message_text(
        build_group_status_text(order_id, "DISPUTE", "DISPUTE"),
        call.message.chat.id,
        call.message.message_id,
    )
    bot.answer_callback_query(call.id, "Dispute")


@app.route("/", methods=["GET"])
def index():
    return "Bot is running", 200


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200


def setup_webhook():
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)


setup_webhook()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
