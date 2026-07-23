import os
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
ADMIN_TELEGRAM_ID_RAW = os.getenv("ADMIN_TELEGRAM_ID")
ADMIN_TELEGRAM_ID = int(ADMIN_TELEGRAM_ID_RAW) if ADMIN_TELEGRAM_ID_RAW else None

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
COMMISSION_RATE = Decimal("0.30")


def log(*args):
    print(*args, flush=True)


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def ensure_balance_schema():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            ALTER TABLE masters
            ADD COLUMN IF NOT EXISTS balance NUMERIC(14, 2) NOT NULL DEFAULT 0
            """
        )
        cur.execute(
            """
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS paid_amount NUMERIC(14, 2),
            ADD COLUMN IF NOT EXISTS commission_amount NUMERIC(14, 2),
            ADD COLUMN IF NOT EXISTS master_balance_after NUMERIC(14, 2)
            """
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def notify_admin(text: str):
    if not ADMIN_CHANNEL_ID:
        log("ADMIN_CHANNEL_ID NOT SET")
        return
    try:
        bot.send_message(ADMIN_CHANNEL_ID, text)
        log("ADMIN NOTIFIED", text[:80])
    except Exception as e:
        log("ADMIN NOTIFY ERROR", repr(e))


def is_admin(user_id: int):
    return ADMIN_TELEGRAM_ID is not None and user_id == ADMIN_TELEGRAM_ID


def parse_admin_balance_command(message, command_name):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Access denied")
        return None

    parts = message.text.split()
    if len(parts) != 3:
        bot.send_message(message.chat.id, f"Usage: /{command_name} MASTER_ID AMOUNT")
        return None

    try:
        master_id = int(parts[1])
        amount = Decimal(parts[2].replace(",", ".")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except (ValueError, InvalidOperation):
        bot.send_message(message.chat.id, "MASTER_ID or AMOUNT is invalid")
        return None

    return master_id, amount


def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("Create Date"))
    markup.add(KeyboardButton("Wallet"))
    return markup


def send_main_menu(chat_id: int, text: str):
    bot.send_message(chat_id, text, reply_markup=main_menu())


def order_group_keyboard(order_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💰 Paid", callback_data=f"paid_{order_id}"))
    kb.add(InlineKeyboardButton("✅ Done", callback_data=f"done_{order_id}"))
    kb.add(InlineKeyboardButton("⚠️ Dispute", callback_data=f"dispute_{order_id}"))
    return kb


def build_group_status_text(
    order_id: int,
    order_status: str,
    payment_status: str,
    paid_amount=None,
    commission=None,
    master_balance=None,
):
    payment_details = ""
    if paid_amount is not None:
        payment_details = f"""
Total paid by client: {paid_amount} USDT
Master fee (30%): {commission} USDT
Master balance: {master_balance} USDT
"""

    return f"""📦 Date Request #{order_id}

Order status: {order_status}
Payment status: {payment_status}
{payment_details}

Use buttons below:"""


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
    try:
        send_main_menu(message.chat.id, "Welcome. Tap 'Create Date'")
    except Exception as e:
        log("START ERROR", repr(e))


@bot.message_handler(commands=["id"])
def get_id(message):
    try:
        bot.send_message(message.chat.id, f"Your Telegram ID: {message.chat.id}")
        bot.send_message(message.chat.id, "Menu:", reply_markup=main_menu())
    except Exception as e:
        log("ID ERROR", repr(e))


@bot.message_handler(commands=["balance"])
@bot.message_handler(func=lambda message: message.text == "Wallet")
def get_balance(message):
    try:
        parts = message.text.split()
        target_id = message.from_user.id
        if len(parts) == 2:
            if not is_admin(message.from_user.id):
                bot.send_message(message.chat.id, "Access denied")
                return
            target_id = int(parts[1])

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                m.balance,
                COALESCE(SUM(o.paid_amount), 0) AS total_paid
            FROM masters m
            LEFT JOIN orders o
              ON o.master_telegram_id = m.telegram_id
             AND o.payment_status = 'PAID'
            WHERE m.telegram_id = %s
            GROUP BY m.telegram_id, m.balance
            """,
            (target_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            bot.send_message(message.chat.id, "Master account not found")
            return
        balance, total_paid = row
        if target_id == message.from_user.id:
            bot.send_message(
                message.chat.id,
                f"💼 Wallet\nBalance: {balance} USDT\nTotal paid: {total_paid} USDT",
            )
        else:
            bot.send_message(
                message.chat.id,
                f"Master {target_id}\nBalance: {balance} USDT\nTotal paid: {total_paid} USDT",
            )
    except Exception as e:
        log("BALANCE ERROR", repr(e))
        notify_admin(f"❌ BALANCE ERROR: {repr(e)}")


@bot.message_handler(commands=["setbalance"])
def set_master_balance(message):
    parsed = parse_admin_balance_command(message, "setbalance")
    if not parsed:
        return
    master_id, amount = parsed

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE masters SET balance = %s WHERE telegram_id = %s RETURNING balance",
            (amount, master_id),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            bot.send_message(message.chat.id, "Master account not found")
            return
        conn.commit()
        bot.send_message(message.chat.id, f"Master {master_id} balance set to {row[0]} USDT")
    finally:
        cur.close()
        conn.close()


@bot.message_handler(commands=["addbalance"])
def add_master_balance(message):
    parsed = parse_admin_balance_command(message, "addbalance")
    if not parsed:
        return
    master_id, amount = parsed

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE masters
            SET balance = balance + %s
            WHERE telegram_id = %s
            RETURNING balance
            """,
            (amount, master_id),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            bot.send_message(message.chat.id, "Master account not found")
            return
        conn.commit()
        bot.send_message(message.chat.id, f"Master {master_id} balance: {row[0]} USDT")
    finally:
        cur.close()
        conn.close()


@bot.message_handler(commands=["balances"])
def list_master_balances(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Access denied")
        return

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT telegram_id, balance, is_active, is_online
            FROM masters
            ORDER BY telegram_id
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not rows:
        bot.send_message(message.chat.id, "No masters found")
        return

    lines = ["💼 Master balances:"]
    for telegram_id, balance, is_active, is_online in rows:
        status = "active" if is_active else "inactive"
        online = "online" if is_online else "offline"
        lines.append(f"{telegram_id}: {balance} USDT — {status}, {online}")

    chunk = ""
    for line in lines:
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > 3500:
            bot.send_message(message.chat.id, chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        bot.send_message(message.chat.id, chunk)


@bot.message_handler(func=lambda message: message.text == "Create Date")
def create_order(message):
    user_data[message.chat.id] = {}
    msg = bot.send_message(message.chat.id, "Enter contact:")
    bot.register_next_step_handler(msg, get_contact)


def get_contact(message):
    user_data[message.chat.id]["contact_text"] = message.text
    bot.send_message(
        message.chat.id,
        "Select date type:",
        reply_markup=date_type_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("dt_"))
def select_date_type(call):
    try:
        date_type = call.data.replace("dt_", "")
        user_data[call.from_user.id]["date_type"] = date_type

        msg = bot.send_message(call.from_user.id, "Enter price (USDT):")
        bot.register_next_step_handler(msg, get_price)
        bot.answer_callback_query(call.id)
    except Exception as e:
        log("DATE TYPE ERROR", repr(e))
        notify_admin(f"❌ DATE TYPE ERROR: {repr(e)}")


def get_price(message):
    try:
        user_data[message.chat.id]["price"] = int(message.text)
    except ValueError:
        msg = bot.send_message(message.chat.id, "Enter price as a number, for example 288")
        bot.register_next_step_handler(msg, get_price)
        return

    bot.send_message(
        message.chat.id,
        "Select format:",
        reply_markup=format_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("fmt_"))
def select_format(call):
    try:
        fmt = call.data.replace("fmt_", "")
        user_data[call.from_user.id]["format_type"] = fmt

        msg = bot.send_message(call.from_user.id, "Enter time from:")
        bot.register_next_step_handler(msg, get_time_from)
        bot.answer_callback_query(call.id)
    except Exception as e:
        log("FORMAT ERROR", repr(e))
        notify_admin(f"❌ FORMAT ERROR: {repr(e)}")


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
        data["profile_name"] = message.text.strip()

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
            f"Date request #{order_id} created and sent.",
        )

        notify_admin(f"""🆕 New Date Request #{order_id}

Client TG ID: {message.chat.id}
Client username: @{message.from_user.username if message.from_user.username else 'none'}
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
        WHERE is_active = TRUE
          AND is_online = TRUE
        """
    )
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
    try:
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
            bot.answer_callback_query(call.id, "This request was already taken")
            notify_admin(f"⚠️ Accept attempt for already taken request #{order_id}")
            return

        client_id = row[0]
        conn.commit()
        cur.close()
        conn.close()

        notify_admin(f"✅ Accept clicked for request #{order_id}\nMaster TG ID: {master_id}\nClient TG ID: {client_id}")

        invite_link, group_chat_id = create_order_group(order_id)
        group_chat_id = int(f"-100{group_chat_id}")

        notify_admin(f"""✅ Group created for Date Request #{order_id}

Master TG ID: {master_id}
Client TG ID: {client_id}
Group ID: {group_chat_id}
Invite: {invite_link}
""")

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
            notify_admin(f"📨 Status card sent to group for request #{order_id}")
        except Exception as e:
            log("SEND STATUS CARD TO GROUP ERROR", repr(e))
            notify_admin(f"❌ Could not send status card to group for request #{order_id}: {repr(e)}")

        bot.answer_callback_query(call.id, "Accepted")

    except Exception as e:
        log("ACCEPT ERROR", repr(e))
        notify_admin(f"❌ ACCEPT ERROR: {repr(e)}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("paid_"))
def mark_paid(call):
    try:
        order_id = int(call.data.split("_")[1])
        master_id = call.from_user.id

        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT payment_status, master_telegram_id
            FROM orders
            WHERE id = %s
            """,
            (order_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            bot.answer_callback_query(call.id, "Request not found")
            return

        payment_status, assigned_master_id = row
        if assigned_master_id != master_id:
            bot.answer_callback_query(call.id, "Only the assigned master can mark Paid")
            return

        if payment_status == "PAID":
            bot.answer_callback_query(call.id, "Payment was already recorded")
            return

        msg = bot.send_message(
            master_id,
            f"Enter the total amount paid by the client for request #{order_id} (USDT):",
        )
        bot.register_next_step_handler(
            msg,
            save_paid_amount,
            order_id,
            call.message.chat.id,
        )
        bot.answer_callback_query(call.id, "Enter the total amount in private chat")

    except Exception as e:
        log("PAID ERROR", repr(e))
        notify_admin(f"❌ PAID ERROR: {repr(e)}")


def save_paid_amount(message, order_id, source_group_id):
    try:
        raw_amount = message.text.strip().replace(",", ".")
        paid_amount = Decimal(raw_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if paid_amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, AttributeError):
        msg = bot.send_message(message.chat.id, "Enter a positive amount, for example 288")
        bot.register_next_step_handler(msg, save_paid_amount, order_id, source_group_id)
        return

    master_id = message.from_user.id
    commission = (paid_amount * COMMISSION_RATE).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT payment_status, master_telegram_id, tg_group_id, order_status
            FROM orders
            WHERE id = %s
            FOR UPDATE
            """,
            (order_id,),
        )
        row = cur.fetchone()

        if not row:
            conn.rollback()
            bot.send_message(master_id, "Request not found")
            return

        payment_status, assigned_master_id, group_chat_id, order_status = row
        if assigned_master_id != master_id:
            conn.rollback()
            bot.send_message(master_id, "You are not the assigned master for this request")
            return

        if payment_status == "PAID":
            conn.rollback()
            bot.send_message(master_id, "Payment was already recorded; balance was not charged again")
            return

        cur.execute(
            """
            UPDATE masters
            SET balance = balance - %s
            WHERE telegram_id = %s
              AND is_active = TRUE
            RETURNING balance
            """,
            (commission, master_id),
        )
        master_row = cur.fetchone()
        if not master_row:
            conn.rollback()
            bot.send_message(master_id, "Active master account not found")
            return

        master_balance = master_row[0]
        cur.execute(
            """
            UPDATE orders
            SET payment_status = 'PAID',
                paid_amount = %s,
                commission_amount = %s,
                master_balance_after = %s
            WHERE id = %s
            """,
            (paid_amount, commission, master_balance, order_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    if group_chat_id and not str(group_chat_id).startswith("-100"):
        group_chat_id = int(f"-100{group_chat_id}")

    status_text = build_group_status_text(
        order_id,
        order_status or "IN_CHAT",
        "PAID",
    )

    bot.send_message(
        group_chat_id or source_group_id,
        status_text,
        reply_markup=order_group_keyboard(order_id),
    )
    bot.send_message(
        master_id,
        f"Payment saved. Fee charged: {commission} USDT. Balance: {master_balance} USDT",
    )

    notify_admin(
        f"💰 Date request #{order_id}: marked as PAID\n"
        f"Total: {paid_amount} USDT\n"
        f"Master fee (30%): {commission} USDT\n"
        f"Master balance: {master_balance} USDT"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("done_"))
def mark_done(call):
    try:
        order_id = int(call.data.split("_")[1])

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

        bot.answer_callback_query(call.id, "Request completed")

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

    except Exception as e:
        log("DONE ERROR", repr(e))
        notify_admin(f"❌ DONE ERROR: {repr(e)}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("dispute_"))
def mark_dispute(call):
    try:
        order_id = int(call.data.split("_")[1])

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

        bot.answer_callback_query(call.id, "Dispute opened")

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

    except Exception as e:
        log("DISPUTE ERROR", repr(e))
        notify_admin(f"❌ DISPUTE ERROR: {repr(e)}")


@app.route("/", methods=["GET"])
def index():
    return "Bot is running", 200


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        log("WEBHOOK ERROR", repr(e))
        notify_admin(f"❌ WEBHOOK ERROR: {repr(e)}")
    return "OK", 200


def setup_webhook(max_attempts=12, retry_delay=10):
    for attempt in range(1, max_attempts + 1):
        try:
            bot.remove_webhook()
            bot.set_webhook(url=WEBHOOK_URL)
            log("WEBHOOK SET", f"attempt {attempt}")
            notify_admin("✅ Swapbot restarted and webhook is set")
            return
        except Exception as e:
            log(
                "SET WEBHOOK ERROR",
                f"attempt {attempt}/{max_attempts}",
                repr(e),
            )
            if attempt < max_attempts:
                time.sleep(retry_delay)

    notify_admin("❌ Could not set webhook after repeated attempts")


ensure_balance_schema()
threading.Thread(target=setup_webhook, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
