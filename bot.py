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


def log(*args):
    print(*args, flush=True)


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def notify_admin(text: str):
    if not ADMIN_CHANNEL_ID:
        log("ADMIN CHANNEL NOT SET")
        return
    try:
        bot.send_message(ADMIN_CHANNEL_ID, text)
    except Exception as e:
        log("ADMIN NOTIFY ERROR", repr(e))


def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("Create Date"))
    return markup


def send_main_menu(chat_id: int, text: str):
    bot.send_message(chat_id, text, reply_markup=main_menu())


def order_group_keyboard(order_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💰 Paid", callback_data=f"paid_{order_id}"))
    kb.add(InlineKeyboardButton("✅ Done", callback_data=f"done_{order_id}"))
    kb.add(InlineKeyboardButton("⚠️ Dispute", callback_data=f"dispute_{order_id}"))
    return kb


def build_group_status_text(order_id: int, order_status: str, payment_status: str):
    return f"""📦 Date Request #{order_id}

Order status: {order_status}
Payment status: {payment_status}

Use buttons below:"""


@bot.message_handler(commands=["start"])
def start(message):
    log("START HANDLER FIRED", message.chat.id, repr(message.text))
    try:
        send_main_menu(message.chat.id, "Welcome. Tap 'Create Date'")
        log("START MESSAGE SENT")
    except Exception as e:
        log("START ERROR", repr(e))


@bot.message_handler(commands=["id"])
def get_id(message):
    log("ID HANDLER FIRED", message.chat.id, repr(message.text))
    try:
        bot.send_message(message.chat.id, f"Your Telegram ID: {message.chat.id}")
        bot.send_message(message.chat.id, "Menu:", reply_markup=main_menu())
        log("ID MESSAGE SENT")
    except Exception as e:
        log("ID ERROR", repr(e))


@bot.message_handler(func=lambda message: message.text == "Create Date")
def create_order(message):
    log("CREATE DATE HANDLER FIRED", message.chat.id)
    user_data[message.chat.id] = {}
    msg = bot.send_message(message.chat.id, "Enter new date:")
    bot.register_next_step_handler(msg, get_new_date)


def get_new_date(message):
    user_data[message.chat.id]["new_date"] = message.text
    msg = bot.send_message(message.chat.id, "Enter contact:")
    bot.register_next_step_handler(msg, get_contact)


def get_contact(message):
    user_data[message.chat.id]["contact_text"] = message.text
    msg = bot.send_message(message.chat.id, "Enter date type:")
    bot.register_next_step_handler(msg, get_date_type)


def get_date_type(message):
    user_data[message.chat.id]["date_type"] = message.text
    msg = bot.send_message(message.chat.id, "Enter price (USDT):")
    bot.register_next_step_handler(msg, get_price)


def get_price(message):
    try:
        user_data[message.chat.id]["price"] = int(message.text)
    except ValueError:
        msg = bot.send_message(message.chat.id, "Enter price as a number, for example 288")
        bot.register_next_step_handler(msg, get_price)
        return

    msg = bot.send_message(message.chat.id, "Enter format:")
    bot.register_next_step_handler(msg, get_format_type)


def get_format_type(message):
    user_data[message.chat.id]["format_type"] = message.text
    msg = bot.send_message(message.chat.id, "Enter time from:")
    bot.register_next_step_handler(msg, get_time_from)


def get_time_from(message):
    user_data[message.chat.id]["time_from"] = message.text
    msg = bot.send_message(message.chat.id, "Enter time to:")
    bot.register_next_step_handler(msg, get_time_to)


def get_time_to(message):
    user_data[message.chat.id]["time_to"] = message.text
    msg = bot.send_message(message.chat.id, "Enter profile:")
    bot.register_next_step_handler(msg, save_order)


def save_order(message):
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
                status,
                order_status,
                payment_status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'NEW', 'NEW', 'UNPAID')
            RETURNING id
            """,
            (
                data["date_type"],
                data["price"],
                message.chat.id,
                message.from_user.username,
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

        user_data.pop(message.chat.id, None)

        send_main_menu(
            message.chat.id,
            f"Date request #{order_id} created and sent to masters.",
        )
        log("ORDER CREATED", order_id)

        notify_admin(f"""🆕 New Date Request #{order_id}

Client TG ID: {message.chat.id}
Client username: @{message.from_user.username if message.from_user.username else 'none'}
New date: {data['new_date']}
Contact: {data['contact_text']}
Date type: {data['date_type']}
Price: {data['price']} USDT
Format: {data['format_type']}
Time: {data['time_from']}-{data['time_to']}
Profile: {data['profile_name']}
""")

    except Exception as e:
        log("SAVE ORDER ERROR", repr(e))
        notify_admin(f"❌ Error creating date request: {repr(e)}")
        user_data.pop(message.chat.id, None)
        try:
            send_main_menu(message.chat.id, f"Error: {e}")
        except Exception as send_err:
            log("SEND ERROR IN SAVE ORDER", repr(send_err))


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

    text = f"""🆕 New Date Request #{order_id}

New date: {data['new_date']}
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
        telegram_id = master[0]
        try:
            bot.send_message(telegram_id, text, reply_markup=kb)
            log("ORDER SENT TO MASTER", telegram_id)
        except Exception as e:
            log("SEND ORDER ERROR TO MASTER", telegram_id, repr(e))
            notify_admin(f"❌ Could not send request #{order_id} to master {telegram_id}: {repr(e)}")

    notify_admin("📨 Date request sent to masters:\n\n" + text)

    cur.close()
    conn.close()


@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_"))
def accept_order(call):
    log("ACCEPT HANDLER FIRED", call.data, call.from_user.id)

    order_id = int(call.data.split("_")[1])
    master_id = call.from_user.id

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE orders
        SET status = 'ASSIGNED',
            order_status = 'ASSIGNED',
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
            bot.answer_callback_query(call.id, "This request was already taken")
        except Exception as e:
            log("CALLBACK ERROR", repr(e))
        return

    client_id = row[0]
    conn.commit()
    cur.close()
    conn.close()

    try:
        invite_link, group_chat_id = create_order_group(order_id)
        group_chat_id = int(f"-100{group_chat_id}")
        log("GROUP CREATED", invite_link, group_chat_id)
        notify_admin(f"""✅ Date request #{order_id} accepted

Master TG ID: {master_id}
Client TG ID: {client_id}
Group ID: {group_chat_id}
Invite: {invite_link}
""")
    except Exception as e:
        log("GROUP CREATE ERROR", repr(e))
        notify_admin(f"❌ Error creating group for date request #{order_id}: {repr(e)}")

        try:
            bot.send_message(master_id, f"❌ Error creating chat for request #{order_id}")
        except Exception as send_err:
            log("SEND TO MASTER ERROR", repr(send_err))

        try:
            send_main_menu(client_id, f"❌ Error creating chat for request #{order_id}")
        except Exception as send_err:
            log("SEND TO CLIENT ERROR", repr(send_err))

        try:
            bot.answer_callback_query(call.id, "Error creating chat")
        except Exception as callback_err:
            log("CALLBACK ERROR", repr(callback_err))

        return

    try:
        send_main_menu(
            client_id,
            f"✅ Master accepted date request #{order_id}\nHere is your chat link:\n{invite_link}",
        )
    except Exception as e:
        log("SEND TO CLIENT ERROR", repr(e))
        notify_admin(f"❌ Could not send invite to client for request #{order_id}: {repr(e)}")

    try:
        bot.send_message(
            master_id,
            f"✅ Date request #{order_id} is yours\nHere is your chat link:\n{invite_link}",
        )
    except Exception as e:
        log("SEND TO MASTER ERROR", repr(e))
        notify_admin(f"❌ Could not send invite to master for request #{order_id}: {repr(e)}")

    try:
        bot.send_message(
            group_chat_id,
            build_group_status_text(order_id, "IN_CHAT", "UNPAID"),
            reply_markup=order_group_keyboard(order_id),
        )
        log("GROUP STATUS CARD SENT", group_chat_id)
        notify_admin(f"📨 Status card sent to group for request #{order_id}")
    except Exception as e:
        log("SEND STATUS CARD TO GROUP ERROR", repr(e))
        notify_admin(f"❌ Could not send status card to group for request #{order_id}: {repr(e)}")

    try:
        bot.answer_callback_query(call.id, "Accepted")
    except Exception as e:
        log("CALLBACK ERROR", repr(e))


@bot.callback_query_handler(func=lambda call: call.data.startswith("paid_"))
def mark_paid(call):
    order_id = int(call.data.split("_")[1])
    log("PAID HANDLER FIRED", order_id)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE orders
        SET payment_status = 'PAID'
        WHERE id = %s
        RETURNING tg_group_id
        """,
        (order_id,),
    )
    row = cur.fetchone()

    conn.commit()
    cur.close()
    conn.close()

    group_chat_id = row[0] if row else None
    if group_chat_id and not str(group_chat_id).startswith("-100"):
        group_chat_id = int(f"-100{group_chat_id}")

    notify_admin(f"💰 Date request #{order_id}: marked as PAID")

    try:
        bot.answer_callback_query(call.id, "Payment marked as paid")
    except Exception as e:
        log("PAID CALLBACK ERROR", repr(e))

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=build_group_status_text(order_id, "IN_CHAT", "PAID"),
            reply_markup=order_group_keyboard(order_id),
        )
    except Exception as e:
        log("EDIT PAID CARD ERROR", repr(e))

    if group_chat_id and group_chat_id != call.message.chat.id:
        try:
            bot.send_message(
                group_chat_id,
                build_group_status_text(order_id, "IN_CHAT", "PAID"),
                reply_markup=order_group_keyboard(order_id),
            )
        except Exception as e:
            log("SEND PAID CARD TO GROUP ERROR", repr(e))


@bot.callback_query_handler(func=lambda call: call.data.startswith("done_"))
def mark_done(call):
    order_id = int(call.data.split("_")[1])
    log("DONE HANDLER FIRED", order_id)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE orders
        SET order_status = 'DONE',
            closed_at = NOW()
        WHERE id = %s
        RETURNING tg_group_id, payment_status
        """,
        (order_id,),
    )
    row = cur.fetchone()

    conn.commit()
    cur.close()
    conn.close()

    group_chat_id = row[0] if row else None
    payment_status = row[1] if row else "PAID"

    if group_chat_id and not str(group_chat_id).startswith("-100"):
        group_chat_id = int(f"-100{group_chat_id}")

    notify_admin(f"✅ Date request #{order_id}: completed")

    try:
        bot.answer_callback_query(call.id, "Request completed")
    except Exception as e:
        log("DONE CALLBACK ERROR", repr(e))

    final_text = build_group_status_text(order_id, "DONE", payment_status)

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=final_text,
        )
    except Exception as e:
        log("EDIT DONE CARD ERROR", repr(e))

    if group_chat_id and group_chat_id != call.message.chat.id:
        try:
            bot.send_message(group_chat_id, final_text)
        except Exception as e:
            log("SEND DONE CARD TO GROUP ERROR", repr(e))


@bot.callback_query_handler(func=lambda call: call.data.startswith("dispute_"))
def mark_dispute(call):
    order_id = int(call.data.split("_")[1])
    log("DISPUTE HANDLER FIRED", order_id)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE orders
        SET payment_status = 'DISPUTE',
            order_status = 'DISPUTE'
        WHERE id = %s
        RETURNING tg_group_id
        """,
        (order_id,),
    )
    row = cur.fetchone()

    conn.commit()
    cur.close()
    conn.close()

    group_chat_id = row[0] if row else None
    if group_chat_id and not str(group_chat_id).startswith("-100"):
        group_chat_id = int(f"-100{group_chat_id}")

    notify_admin(f"⚠️ Date request #{order_id}: dispute opened")

    try:
        bot.answer_callback_query(call.id, "Dispute opened")
    except Exception as e:
        log("DISPUTE CALLBACK ERROR", repr(e))

    dispute_text = build_group_status_text(order_id, "DISPUTE", "DISPUTE")

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=dispute_text,
        )
    except Exception as e:
        log("EDIT DISPUTE CARD ERROR", repr(e))

    if group_chat_id and group_chat_id != call.message.chat.id:
        try:
            bot.send_message(group_chat_id, dispute_text)
        except Exception as e:
            log("SEND DISPUTE CARD TO GROUP ERROR", repr(e))


@app.route("/", methods=["GET"])
def index():
    return "Bot is running", 200


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        json_str = request.get_data().decode("utf-8")
        log("WEBHOOK HIT RAW", json_str)

        update = telebot.types.Update.de_json(json_str)
        log("UPDATE PARSED", type(update).__name__)

        bot.process_new_updates([update])
        log("UPDATE PROCESSED")

    except Exception as e:
        log("WEBHOOK ERROR", repr(e))
        notify_admin(f"❌ WEBHOOK ERROR: {repr(e)}")
    return "OK", 200


def setup_webhook():
    try:
        bot.remove_webhook()
        result = bot.set_webhook(url=WEBHOOK_URL)
        log("WEBHOOK SET", result, WEBHOOK_URL)
        notify_admin("✅ Swapbot restarted and webhook is set")
    except Exception as e:
        log("SET WEBHOOK ERROR", repr(e))


setup_webhook()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    log("BOT WEBHOOK SERVER STARTED", port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
