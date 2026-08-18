import os
import queue
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import telebot
import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_IDLE
from psycopg2.pool import ThreadedConnectionPool
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
FULL_ADMIN_IDS = {5411302547, 1287765735}
if ADMIN_TELEGRAM_ID is not None:
    FULL_ADMIN_IDS.add(ADMIN_TELEGRAM_ID)

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
admin_topups = {}
pending_disputes = {}
group_creation_queue = queue.Queue()
lead_dispatch_queue = queue.Queue()
COMMISSION_RATE = Decimal("0.30")
LOW_BALANCE_THRESHOLD = Decimal("500.00")
LOW_BALANCE_REMINDER_INTERVAL_HOURS = 2
LOW_BALANCE_CHECK_INTERVAL_SECONDS = 300
LEAD_FOLLOWUP_DELAY_HOURS = 8
LEAD_FOLLOWUP_CHECK_INTERVAL_SECONDS = 300
DB_POOL_MIN_CONNECTIONS = 1
DB_POOL_MAX_CONNECTIONS = 12
_db_pool = None
_db_pool_lock = threading.Lock()


def log(*args):
    print(*args, flush=True)


class PooledConnection:
    def __init__(self, pool, connection):
        self._pool = pool
        self._connection = connection
        self._returned = False

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        if self._returned:
            return
        self._returned = True
        close_connection = bool(self._connection.closed)
        if not close_connection:
            try:
                if self._connection.get_transaction_status() != TRANSACTION_STATUS_IDLE:
                    self._connection.rollback()
            except Exception:
                close_connection = True
        self._pool.putconn(self._connection, close=close_connection)


def get_conn():
    global _db_pool
    if _db_pool is None:
        with _db_pool_lock:
            if _db_pool is None:
                _db_pool = ThreadedConnectionPool(
                    DB_POOL_MIN_CONNECTIONS,
                    DB_POOL_MAX_CONNECTIONS,
                    dsn=DATABASE_URL,
                    connect_timeout=5,
                    application_name="swapbot",
                )
    return PooledConnection(_db_pool, _db_pool.getconn())


def ensure_balance_schema():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            ALTER TABLE masters
            ADD COLUMN IF NOT EXISTS balance NUMERIC(14, 2) NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_low_balance_reminder_at TIMESTAMPTZ
            """
        )
        cur.execute(
            """
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS paid_amount NUMERIC(14, 2),
            ADD COLUMN IF NOT EXISTS commission_amount NUMERIC(14, 2),
            ADD COLUMN IF NOT EXISTS master_balance_after NUMERIC(14, 2),
            ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS meeting_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS lead_followup_reminded_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS dispute_comment TEXT,
            ADD COLUMN IF NOT EXISTS dispute_blacklisted BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS dispute_opened_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'Telegram Bot',
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS order_status_history (
                id BIGSERIAL PRIMARY KEY,
                order_id BIGINT NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                payment_status TEXT,
                actor_telegram_id BIGINT,
                actor_name TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_order_status_history_order
            ON order_status_history (order_id, created_at)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id BIGSERIAL PRIMARY KEY,
                actor_telegram_id BIGINT,
                actor_name TEXT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                old_value TEXT,
                new_value TEXT,
                details TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS client_blacklist (
                client_telegram_id BIGINT PRIMARY KEY,
                client_username TEXT,
                reason TEXT NOT NULL,
                order_id BIGINT,
                added_by_telegram_id BIGINT,
                added_by_name TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            INSERT INTO app_settings (setting_key, setting_value)
            VALUES ('statistics_started_at_gift_v1', NOW()::TEXT)
            ON CONFLICT (setting_key) DO NOTHING
            """
        )
        cur.execute(
            """
            INSERT INTO app_settings (setting_key, setting_value)
            VALUES ('lead_reminders_started_at_v1', NOW()::TEXT)
            ON CONFLICT (setting_key) DO NOTHING
            """
        )
        cur.execute(
            """
            INSERT INTO order_status_history (
                order_id, old_status, new_status, payment_status, actor_name
            )
            SELECT id, NULL, COALESCE(order_status, status, 'NEW'), payment_status, 'Migration'
            FROM orders o
            WHERE NOT EXISTS (
                SELECT 1 FROM order_status_history h WHERE h.order_id = o.id
            )
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
    return user_id in FULL_ADMIN_IDS


def actor_name(user):
    if not user:
        return "System"
    username = getattr(user, "username", None)
    full_name = " ".join(
        part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if part
    )
    return f"@{username}" if username else (full_name or str(getattr(user, "id", "Unknown")))


def add_audit(
    cur,
    actor_id,
    actor_display,
    action,
    entity_type,
    entity_id=None,
    old_value=None,
    new_value=None,
    details=None,
):
    cur.execute(
        """
        INSERT INTO audit_log (
            actor_telegram_id, actor_name, action, entity_type,
            entity_id, old_value, new_value, details
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            actor_id,
            actor_display,
            action,
            entity_type,
            str(entity_id) if entity_id is not None else None,
            str(old_value) if old_value is not None else None,
            str(new_value) if new_value is not None else None,
            details,
        ),
    )


def add_status_history(cur, order_id, old_status, new_status, payment_status, user=None):
    cur.execute(
        """
        INSERT INTO order_status_history (
            order_id, old_status, new_status, payment_status,
            actor_telegram_id, actor_name
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            order_id,
            old_status,
            new_status,
            payment_status,
            getattr(user, "id", None),
            actor_name(user),
        ),
    )


def correct_gift_amount(order_id, new_amount, actor_id=None, actor_display="System"):
    new_amount = Decimal(new_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if new_amount <= 0:
        raise ValueError("Gift amount must be greater than zero")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT paid_amount, commission_amount, master_telegram_id,
                   payment_status, order_status
            FROM orders
            WHERE id = %s
            FOR UPDATE
            """,
            (order_id,),
        )
        order = cur.fetchone()
        if not order:
            raise ValueError("Date request not found")

        old_amount, old_commission, master_id, payment_status, order_status = order
        if payment_status not in ("GIFT", "PAID"):
            raise ValueError("Gift has not been recorded for this request")
        if master_id is None:
            raise ValueError("No Swapper is assigned to this request")

        old_amount = Decimal(old_amount or 0).quantize(Decimal("0.01"))
        old_commission = Decimal(old_commission or 0).quantize(Decimal("0.01"))
        new_commission = (new_amount * COMMISSION_RATE).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if (
            actor_id is None
            and old_amount == new_amount
            and old_commission == new_commission
            and payment_status == "GIFT"
        ):
            return {
                "old_amount": old_amount,
                "new_amount": new_amount,
                "old_commission": old_commission,
                "new_commission": new_commission,
                "balance_adjustment": Decimal("0"),
                "new_balance": None,
                "master_id": master_id,
            }
        balance_adjustment = old_commission - new_commission

        cur.execute(
            """
            UPDATE masters
            SET balance = balance + %s
            WHERE telegram_id = %s
            RETURNING balance
            """,
            (balance_adjustment, master_id),
        )
        balance_row = cur.fetchone()
        if not balance_row:
            raise ValueError("Swapper account not found")
        new_balance = balance_row[0]
        old_balance = Decimal(new_balance) - balance_adjustment

        cur.execute(
            """
            UPDATE orders
            SET payment_status = 'GIFT',
                paid_amount = %s,
                commission_amount = %s,
                master_balance_after = %s
            WHERE id = %s
            """,
            (new_amount, new_commission, new_balance, order_id),
        )
        cur.execute(
            """
            INSERT INTO order_status_history (
                order_id, old_status, new_status, payment_status,
                actor_telegram_id, actor_name
            ) VALUES (%s, %s, %s, 'GIFT', %s, %s)
            """,
            (order_id, order_status, order_status, actor_id, actor_display),
        )
        add_audit(
            cur, actor_id, actor_display, "EDIT_GIFT", "order", order_id,
            old_amount, new_amount,
            f"commission={old_commission}->{new_commission}; balance_adjustment={balance_adjustment}",
        )
        add_audit(
            cur, actor_id, actor_display, "GIFT_BALANCE_CORRECTION",
            "master_balance", master_id, old_balance, new_balance,
            f"order_id={order_id}; adjustment={balance_adjustment}",
        )
        conn.commit()
        return {
            "old_amount": old_amount,
            "new_amount": new_amount,
            "old_commission": old_commission,
            "new_commission": new_commission,
            "balance_adjustment": balance_adjustment,
            "new_balance": new_balance,
            "master_id": master_id,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def apply_order_904_duplicate_charge_fix():
    """Refund the duplicate 3000/1000 Gift charge once after #904 is normalized to 1500."""
    correction_key = "order_904_duplicate_charge_refund_v1"
    refund = Decimal("900.00")
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT 1 FROM app_settings WHERE setting_key = %s",
            (correction_key,),
        )
        if cur.fetchone():
            return False

        cur.execute(
            """
            SELECT master_telegram_id, paid_amount, commission_amount, payment_status
            FROM orders
            WHERE id = 904
            FOR UPDATE
            """
        )
        order = cur.fetchone()
        if not order:
            raise ValueError("Date request #904 not found")
        master_id, gift_amount, commission, payment_status = order
        if (
            payment_status != "GIFT"
            or Decimal(gift_amount or 0) != Decimal("1500.00")
            or Decimal(commission or 0) != Decimal("450.00")
        ):
            raise ValueError("Date request #904 is not normalized to Gift 1500 / fee 450")

        cur.execute(
            "SELECT balance FROM masters WHERE telegram_id = %s FOR UPDATE",
            (master_id,),
        )
        balance_row = cur.fetchone()
        if not balance_row:
            raise ValueError("Swapper account for #904 not found")
        old_balance = Decimal(balance_row[0])
        new_balance = old_balance + refund

        cur.execute(
            "UPDATE masters SET balance = %s WHERE telegram_id = %s",
            (new_balance, master_id),
        )
        cur.execute(
            "UPDATE orders SET master_balance_after = %s WHERE id = 904",
            (new_balance,),
        )
        add_audit(
            cur,
            None,
            "System correction requested by admin",
            "DUPLICATE_GIFT_REFUND",
            "master_balance",
            master_id,
            old_balance,
            new_balance,
            "order_id=904; refund=900.00; duplicate Gift entries were 3000 and 1000",
        )
        cur.execute(
            """
            INSERT INTO app_settings (setting_key, setting_value)
            VALUES (%s, NOW()::TEXT)
            """,
            (correction_key,),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def remove_swapper_8649754773_once():
    correction_key = "remove_swapper_8649754773_v1"
    master_id = 8649754773
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT 1 FROM app_settings WHERE setting_key = %s",
            (correction_key,),
        )
        if cur.fetchone():
            return False

        cur.execute(
            """
            SELECT balance, is_active, is_online
            FROM masters
            WHERE telegram_id = %s
            FOR UPDATE
            """,
            (master_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Swapper {master_id} not found")

        balance, was_active, was_online = row
        cur.execute(
            """
            UPDATE masters
            SET is_active = FALSE,
                is_online = FALSE
            WHERE telegram_id = %s
            """,
            (master_id,),
        )
        add_audit(
            cur,
            None,
            "System removal requested by admin",
            "REMOVE_SWAPPER",
            "master",
            master_id,
            f"active={was_active}; online={was_online}; balance={balance}",
            f"active=False; online=False; balance={balance}",
            "Removed from active Swappers; historical orders and financial records preserved",
        )
        cur.execute(
            """
            INSERT INTO app_settings (setting_key, setting_value)
            VALUES (%s, NOW()::TEXT)
            """,
            (correction_key,),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


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


def main_menu(user_id=None):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("Create Date"))
    markup.row(KeyboardButton("Wallet"), KeyboardButton("Statistics"))
    markup.add(KeyboardButton("Lead History"))
    if user_id is not None and is_admin(user_id):
        markup.add(KeyboardButton("Admin Panel"))
    return markup


def send_main_menu(chat_id: int, text: str):
    bot.send_message(chat_id, text, reply_markup=main_menu(chat_id))


def order_group_keyboard(order_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🎁 Gift", callback_data=f"gift_{order_id}"))
    kb.add(InlineKeyboardButton("⚠️ Dispute", callback_data=f"dispute_{order_id}"))
    return kb


def send_and_pin_group_card(chat_id, text, reply_markup):
    message = bot.send_message(chat_id, text, reply_markup=reply_markup)
    try:
        bot.pin_chat_message(
            chat_id,
            message.message_id,
            disable_notification=True,
        )
        log("GROUP STATUS CARD PINNED", chat_id, message.message_id)
    except Exception as e:
        log("PIN GROUP STATUS CARD ERROR", chat_id, repr(e))
        notify_admin(f"❌ Could not pin status card in group {chat_id}: {repr(e)}")
    return message


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
🎁 Total Gift: {paid_amount} USDT
🔄 Swapper fee (30%): {commission} USDT
💼 Swapper balance: {master_balance} USDT
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


def period_keyboard(prefix):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Today", callback_data=f"{prefix}_today"),
        InlineKeyboardButton("7 days", callback_data=f"{prefix}_7d"),
        InlineKeyboardButton("30 days", callback_data=f"{prefix}_30d"),
        InlineKeyboardButton("All time", callback_data=f"{prefix}_all"),
    )
    return kb


def period_condition(period, column="o.created_at"):
    statistics_start = """
        (SELECT setting_value::TIMESTAMPTZ
         FROM app_settings
         WHERE setting_key = 'statistics_started_at_gift_v1')
    """
    if period == "today":
        return f"{column} >= GREATEST(CURRENT_DATE, {statistics_start})", "Today"
    if period == "7d":
        return f"{column} >= GREATEST(NOW() - INTERVAL '7 days', {statistics_start})", "Last 7 days"
    if period == "30d":
        return f"{column} >= GREATEST(NOW() - INTERVAL '30 days', {statistics_start})", "Last 30 days"
    return f"{column} >= {statistics_start}", "All time"


def format_money(value):
    return f"{Decimal(value or 0).quantize(Decimal('0.01'))}"


def master_statistics(master_id, period):
    condition, label = period_condition(period)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE o.master_telegram_id IS NOT NULL),
                COUNT(*) FILTER (WHERE o.payment_status = 'GIFT'),
                COALESCE(SUM(o.paid_amount) FILTER (WHERE o.payment_status = 'GIFT'), 0),
                COALESCE(AVG(o.paid_amount) FILTER (WHERE o.payment_status = 'GIFT'), 0),
                COALESCE(m.balance, 0)
            FROM masters m
            LEFT JOIN orders o
              ON o.master_telegram_id = m.telegram_id
             AND {condition}
            WHERE m.telegram_id = %s
            GROUP BY m.telegram_id, m.balance
            """,
            (master_id,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not row:
        return None, label
    accepted, paid_leads, revenue, average, balance = row
    conversion = (Decimal(paid_leads) * 100 / Decimal(accepted)) if accepted else Decimal("0")
    return {
        "accepted": accepted,
        "paid_leads": paid_leads,
        "conversion": conversion.quantize(Decimal("0.01")),
        "revenue": revenue,
        "average": average,
        "balance": balance,
    }, label


def master_statistics_text(master_id, period):
    stats, label = master_statistics(master_id, period)
    if not stats:
        return "🔄 Swapper account not found"
    return f"""📊 Swapper statistics — {label}

Accepted Date: {stats['accepted']}
🎁 Gift leads: {stats['paid_leads']}
Conversion: {stats['conversion']}%
Revenue: {format_money(stats['revenue'])} USDT
Average check: {format_money(stats['average'])} USDT
Balance/debt: {format_money(stats['balance'])} USDT"""


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
        bot.send_message(message.chat.id, "Menu:", reply_markup=main_menu(message.from_user.id))
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
                COALESCE(SUM(o.paid_amount), 0) AS total_gift
            FROM masters m
            LEFT JOIN orders o
              ON o.master_telegram_id = m.telegram_id
             AND o.payment_status = 'GIFT'
            WHERE m.telegram_id = %s
            GROUP BY m.telegram_id, m.balance
            """,
            (target_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            bot.send_message(message.chat.id, "🔄 Swapper account not found")
            return
        balance, total_gift = row
        if target_id == message.from_user.id:
            bot.send_message(
                message.chat.id,
                f"💼 Wallet\nBalance: {balance} USDT\n🎁 Total Gift: {total_gift} USDT",
            )
        else:
            bot.send_message(
                message.chat.id,
                f"🔄 Swapper {target_id}\n💼 Balance: {balance} USDT\n🎁 Total Gift: {total_gift} USDT",
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
        cur.execute("SELECT balance FROM masters WHERE telegram_id = %s FOR UPDATE", (master_id,))
        current = cur.fetchone()
        if not current:
            conn.rollback()
            bot.send_message(message.chat.id, "🔄 Swapper account not found")
            return
        old_balance = current[0]
        cur.execute(
            "UPDATE masters SET balance = %s WHERE telegram_id = %s RETURNING balance",
            (amount, master_id),
        )
        row = cur.fetchone()
        add_audit(
            cur, message.from_user.id, actor_name(message.from_user),
            "SET_BALANCE", "master_balance", master_id, old_balance, row[0],
        )
        conn.commit()
        bot.send_message(message.chat.id, f"✅ Swapper {master_id} balance set to {row[0]} USDT")
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
        cur.execute("SELECT balance FROM masters WHERE telegram_id = %s FOR UPDATE", (master_id,))
        current = cur.fetchone()
        if not current:
            conn.rollback()
            bot.send_message(message.chat.id, "🔄 Swapper account not found")
            return
        old_balance = current[0]
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
        add_audit(
            cur, message.from_user.id, actor_name(message.from_user),
            "ADD_BALANCE", "master_balance", master_id, old_balance, row[0],
        )
        conn.commit()
        bot.send_message(message.chat.id, f"💼 Swapper {master_id} balance: {row[0]} USDT")
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
            WHERE is_active = TRUE
            ORDER BY telegram_id
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not rows:
        bot.send_message(message.chat.id, "🔄 No Swappers found")
        return

    lines = ["💼🔄 Swapper balances:"]
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


@bot.message_handler(commands=["stats"])
@bot.message_handler(func=lambda message: message.text == "Statistics")
def show_master_statistics(message):
    text = master_statistics_text(message.from_user.id, "all")
    bot.send_message(message.chat.id, text, reply_markup=period_keyboard("mstats"))


@bot.callback_query_handler(func=lambda call: call.data.startswith("mstats_"))
def change_master_statistics_period(call):
    period = call.data.split("_", 1)[1]
    text = master_statistics_text(call.from_user.id, period)
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=period_keyboard("mstats"),
    )
    bot.answer_callback_query(call.id)


def lead_card(order_id, viewer_id, admin_access=False):
    conn = get_conn()
    cur = conn.cursor()
    try:
        ownership = "TRUE" if admin_access else "o.master_telegram_id = %s"
        params = (order_id,) if admin_access else (order_id, viewer_id)
        cur.execute(
            f"""
            SELECT
                o.id,
                COALESCE(o.profile_name, o.contact_text, '—'),
                o.client_username,
                o.created_at,
                COALESCE(o.order_status, o.status, '—'),
                o.price,
                o.meeting_at,
                o.time_from,
                o.time_to,
                o.source,
                o.paid_amount,
                o.payment_status,
                o.master_telegram_id,
                o.dispute_comment,
                o.dispute_blacklisted
            FROM orders o
            WHERE o.id = %s
              AND {ownership}
              AND o.created_at >= (
                  SELECT setting_value::TIMESTAMPTZ FROM app_settings
                  WHERE setting_key = 'statistics_started_at_gift_v1'
              )
            """,
            params,
        )
        row = cur.fetchone()
        if not row:
            return None

        cur.execute(
            """
            SELECT old_status, new_status, payment_status, actor_name, created_at
            FROM order_status_history
            WHERE order_id = %s
            ORDER BY created_at
            """,
            (order_id,),
        )
        history = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    (
        lead_id, client_name, username, created_at, status, price, meeting_at,
        time_from, time_to, source, paid_amount, payment_status, master_id,
        dispute_comment, dispute_blacklisted,
    ) = row
    meeting = meeting_at.strftime("%Y-%m-%d %H:%M") if meeting_at else f"{time_from or '—'}–{time_to or '—'}"
    username_text = f"@{username}" if username else "—"
    history_lines = []
    for old_status, new_status, hist_payment, hist_actor, changed_at in history:
        transition = f"{old_status or '—'} → {new_status}"
        history_lines.append(
            f"{changed_at.strftime('%Y-%m-%d %H:%M')} — {transition}"
            f" ({hist_payment or '—'}, {hist_actor or 'System'})"
        )
    history_text = "\n".join(history_lines) if history_lines else "No history"
    dispute_details = ""
    if dispute_comment:
        blacklist_text = "YES 🚫" if dispute_blacklisted else "NO"
        dispute_details = (
            f"\nDispute reason: {dispute_comment}"
            f"\nClient blacklisted: {blacklist_text}"
        )

    return f"""📋 Lead #{lead_id}

Client: {client_name}
Telegram: {username_text}
Created: {created_at.strftime('%Y-%m-%d %H:%M')}
Status: {status}
Payment: {payment_status or '—'}
Initial price: {format_money(price)} USDT
Final amount: {format_money(paid_amount)} USDT
Meeting: {meeting}
Source: {source or 'Telegram Bot'}
🔄 Swapper ID: {master_id or '—'}
{dispute_details}

Status history:
{history_text}"""


@bot.message_handler(commands=["leads"])
@bot.message_handler(func=lambda message: message.text == "Lead History")
def show_master_leads(message):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT id, created_at, COALESCE(order_status, status, '—'),
                   COALESCE(paid_amount, price, 0)
            FROM orders
            WHERE master_telegram_id = %s
              AND created_at >= (
                  SELECT setting_value::TIMESTAMPTZ FROM app_settings
                  WHERE setting_key = 'statistics_started_at_gift_v1'
              )
            ORDER BY created_at DESC, id DESC
            LIMIT 20
            """,
            (message.from_user.id,),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not rows:
        bot.send_message(message.chat.id, "You have no accepted leads")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for order_id, created_at, status, amount in rows:
        kb.add(
            InlineKeyboardButton(
                f"#{order_id} · {created_at.strftime('%Y-%m-%d')} · {status} · {format_money(amount)}",
                callback_data=f"lead_{order_id}",
            )
        )
    bot.send_message(message.chat.id, "📚 Your latest leads:", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("lead_"))
def open_master_lead(call):
    order_id = int(call.data.split("_", 1)[1])
    text = lead_card(order_id, call.from_user.id)
    if not text:
        bot.answer_callback_query(call.id, "Lead not found or access denied")
        return
    bot.send_message(call.message.chat.id, text)
    bot.answer_callback_query(call.id)


def admin_panel_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("System statistics", callback_data="adm_stats"),
        InlineKeyboardButton("🔄 Swappers", callback_data="adm_masters"),
        InlineKeyboardButton("TOP revenue", callback_data="adm_top_rev"),
        InlineKeyboardButton("TOP Gift leads", callback_data="adm_top_date"),
        InlineKeyboardButton("TOP conversion", callback_data="adm_top_conv"),
        InlineKeyboardButton("Audit log", callback_data="adm_audit"),
    )
    kb.add(InlineKeyboardButton("✏️ Edit Gift", callback_data="adm_edit_gift"))
    kb.add(InlineKeyboardButton("💰 Top Up Swapper Balance", callback_data="adm_topup"))
    return kb


def require_admin_callback(call):
    if is_admin(call.from_user.id):
        return True
    bot.answer_callback_query(call.id, "Access denied")
    return False


@bot.message_handler(commands=["admin"])
@bot.message_handler(func=lambda message: message.text == "Admin Panel")
def show_admin_panel(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Access denied")
        return
    bot.send_message(message.chat.id, "🛠 Admin Panel", reply_markup=admin_panel_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "adm_stats")
def show_admin_statistics(call):
    if not require_admin_callback(call):
        return
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT
                COUNT(*),
                COUNT(*) FILTER (WHERE master_telegram_id IS NOT NULL),
                COALESCE(SUM(paid_amount) FILTER (WHERE payment_status = 'GIFT'), 0),
                COALESCE(AVG(paid_amount) FILTER (WHERE payment_status = 'GIFT'), 0)
            FROM orders
            WHERE created_at >= (
                SELECT setting_value::TIMESTAMPTZ FROM app_settings
                WHERE setting_key = 'statistics_started_at_gift_v1'
            )
            """
        )
        total_leads, dates, revenue, average = cur.fetchone()
        cur.execute(
            "SELECT COALESCE(SUM(GREATEST(-balance, 0)), 0) FROM masters WHERE is_active = TRUE"
        )
        debt = cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()

    text = f"""📈 System statistics

Total leads: {total_leads}
Date assigned: {dates}
Revenue: {format_money(revenue)} USDT
🔄 Swappers debt: {format_money(debt)} USDT
Average check: {format_money(average)} USDT"""
    bot.send_message(call.message.chat.id, text)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_masters")
def show_admin_masters(call):
    if not require_admin_callback(call):
        return
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT telegram_id, balance, is_active, is_online
            FROM masters
            WHERE is_active = TRUE
            ORDER BY telegram_id
            LIMIT 50
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    if not rows:
        bot.send_message(call.message.chat.id, "🔄 No Swappers found")
        bot.answer_callback_query(call.id)
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for master_id, balance, active, online in rows:
        state = "🟢" if active and online else "⚪️"
        kb.add(
            InlineKeyboardButton(
                f"{state} {master_id} · {format_money(balance)} USDT",
                callback_data=f"adm_master_{master_id}",
            )
        )
    bot.send_message(call.message.chat.id, "👥🔄 Swappers:", reply_markup=kb)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_master_"))
def show_admin_master_card(call):
    if not require_admin_callback(call):
        return
    master_id = int(call.data.rsplit("_", 1)[1])
    stats, _ = master_statistics(master_id, "all")
    if not stats:
        bot.answer_callback_query(call.id, "Swapper not found")
        return
    text = f"""🔄👤 Swapper {master_id}

Accepted Date: {stats['accepted']}
🎁 Gift leads: {stats['paid_leads']}
Conversion: {stats['conversion']}%
Revenue: {format_money(stats['revenue'])} USDT
Average check: {format_money(stats['average'])} USDT
Balance/debt: {format_money(stats['balance'])} USDT"""
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Latest leads", callback_data=f"adm_mleads_{master_id}"))
    bot.send_message(call.message.chat.id, text, reply_markup=kb)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_mleads_"))
def show_admin_master_leads(call):
    if not require_admin_callback(call):
        return
    master_id = int(call.data.rsplit("_", 1)[1])
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT id, created_at, COALESCE(order_status, status, '—')
            FROM orders
            WHERE master_telegram_id = %s
              AND created_at >= (
                  SELECT setting_value::TIMESTAMPTZ FROM app_settings
                  WHERE setting_key = 'statistics_started_at_gift_v1'
              )
            ORDER BY created_at DESC, id DESC LIMIT 20
            """,
            (master_id,),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    kb = InlineKeyboardMarkup(row_width=1)
    for order_id, created_at, status in rows:
        kb.add(
            InlineKeyboardButton(
                f"#{order_id} · {created_at.strftime('%Y-%m-%d')} · {status}",
                callback_data=f"adm_lead_{order_id}",
            )
        )
    bot.send_message(call.message.chat.id, f"📚 Leads of Swapper {master_id}:", reply_markup=kb)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_lead_"))
def open_admin_lead(call):
    if not require_admin_callback(call):
        return
    order_id = int(call.data.rsplit("_", 1)[1])
    text = lead_card(order_id, call.from_user.id, admin_access=True)
    bot.send_message(call.message.chat.id, text or "Lead not found")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_top_"))
def show_admin_top(call):
    if not require_admin_callback(call):
        return
    ranking = call.data.rsplit("_", 1)[1]
    order_expression = {
        "rev": "revenue DESC, paid_leads DESC",
        "date": "paid_leads DESC, revenue DESC",
        "conv": "conversion DESC, paid_leads DESC",
    }.get(ranking, "revenue DESC")
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            SELECT * FROM (
                SELECT
                    m.telegram_id,
                    COUNT(o.id) FILTER (WHERE o.payment_status = 'GIFT') AS paid_leads,
                    COALESCE(SUM(o.paid_amount) FILTER (WHERE o.payment_status = 'GIFT'), 0) AS revenue,
                    CASE WHEN COUNT(o.id) = 0 THEN 0
                         ELSE 100.0 * COUNT(o.id) FILTER (WHERE o.payment_status = 'GIFT') / COUNT(o.id)
                    END AS conversion
                FROM masters m
                LEFT JOIN orders o
                  ON o.master_telegram_id = m.telegram_id
                 AND o.created_at >= (
                     SELECT setting_value::TIMESTAMPTZ FROM app_settings
                     WHERE setting_key = 'statistics_started_at_gift_v1'
                 )
                WHERE m.is_active = TRUE
                GROUP BY m.telegram_id
            ) ranked
            ORDER BY {order_expression}
            LIMIT 10
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    titles = {"rev": "revenue", "date": "Gift leads", "conv": "conversion"}
    lines = [f"🏆🔄 TOP Swappers by {titles.get(ranking, 'revenue')}"]
    for index, (master_id, paid_leads, revenue, conversion) in enumerate(rows, start=1):
        lines.append(
            f"{index}. {master_id} — {format_money(revenue)} USDT, "
            f"{paid_leads} Gift leads, {Decimal(conversion or 0).quantize(Decimal('0.01'))}%"
        )
    bot.send_message(call.message.chat.id, "\n".join(lines))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_audit")
def show_admin_audit(call):
    if not require_admin_callback(call):
        return
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT actor_name, action, entity_type, entity_id,
                   old_value, new_value, created_at
            FROM audit_log ORDER BY created_at DESC LIMIT 30
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    lines = ["🧾 Latest audit operations:"]
    for actor, action, entity_type, entity_id, old, new, created_at in rows:
        lines.append(
            f"{created_at.strftime('%Y-%m-%d %H:%M')} · {actor or 'System'} · {action}\n"
            f"{entity_type} {entity_id or ''}: {old or '—'} → {new or '—'}"
        )
    bot.send_message(call.message.chat.id, "\n\n".join(lines) if rows else "Audit log is empty")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_edit_gift")
def start_admin_edit_gift(call):
    if not require_admin_callback(call):
        return
    msg = bot.send_message(call.from_user.id, "✏️ Enter Date request ID:")
    bot.register_next_step_handler(msg, receive_admin_edit_gift_order)
    bot.answer_callback_query(call.id)


def receive_admin_edit_gift_order(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Access denied")
        return
    try:
        order_id = int(message.text.strip().lstrip("#"))
    except (ValueError, AttributeError):
        msg = bot.send_message(message.chat.id, "Enter a valid numeric Date request ID:")
        bot.register_next_step_handler(msg, receive_admin_edit_gift_order)
        return
    msg = bot.send_message(message.chat.id, f"🎁 Enter the correct Gift amount for #{order_id} (USDT):")
    bot.register_next_step_handler(msg, receive_admin_edit_gift_amount, order_id)


def receive_admin_edit_gift_amount(message, order_id):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Access denied")
        return
    try:
        amount = Decimal(message.text.strip().replace(",", "."))
        result = correct_gift_amount(
            order_id,
            amount,
            actor_id=message.from_user.id,
            actor_display=actor_name(message.from_user),
        )
    except (ValueError, InvalidOperation) as e:
        bot.send_message(message.chat.id, f"❌ Gift was not changed: {e}")
        return
    except Exception as e:
        log("EDIT GIFT ERROR", repr(e))
        notify_admin(f"❌ EDIT GIFT ERROR for #{order_id}: {repr(e)}")
        bot.send_message(message.chat.id, "❌ Could not update Gift. Check the admin log.")
        return

    bot.send_message(
        message.chat.id,
        f"""✅ Gift corrected for Date request #{order_id}

🎁 Gift: {format_money(result['old_amount'])} → {format_money(result['new_amount'])} USDT
💸 Fee: {format_money(result['old_commission'])} → {format_money(result['new_commission'])} USDT
💼 Swapper {result['master_id']} balance: {format_money(result['new_balance'])} USDT""",
    )
    notify_admin(
        f"✏️ Gift corrected for Date request #{order_id}\n"
        f"Gift: {format_money(result['old_amount'])} → {format_money(result['new_amount'])} USDT\n"
        f"Swapper: {result['master_id']}"
    )


@bot.callback_query_handler(func=lambda call: call.data == "adm_topup")
def choose_topup_swapper(call):
    if not require_admin_callback(call):
        return
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT telegram_id, balance, is_active
            FROM masters
            WHERE is_active = TRUE
            ORDER BY telegram_id
            LIMIT 50
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not rows:
        bot.send_message(call.message.chat.id, "🔄 No Swappers found")
        bot.answer_callback_query(call.id)
        return

    kb = InlineKeyboardMarkup(row_width=1)
    for master_id, balance, is_active in rows:
        state = "🟢" if is_active else "⚪️"
        kb.add(
            InlineKeyboardButton(
                f"{state} {master_id} · {format_money(balance)} USDT",
                callback_data=f"adm_topup_master_{master_id}",
            )
        )
    bot.send_message(call.message.chat.id, "💰 Choose a Swapper to top up:", reply_markup=kb)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_topup_master_"))
def request_topup_amount(call):
    if not require_admin_callback(call):
        return
    master_id = int(call.data.rsplit("_", 1)[1])
    admin_topups.pop(call.from_user.id, None)
    msg = bot.send_message(
        call.from_user.id,
        f"💵 Enter the top-up amount for Swapper {master_id} (USDT):",
    )
    bot.register_next_step_handler(msg, receive_topup_amount, master_id)
    bot.answer_callback_query(call.id)


def receive_topup_amount(message, master_id):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Access denied")
        return
    try:
        amount = Decimal(message.text.strip().replace(",", ".")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if amount <= 0:
            raise InvalidOperation
    except (InvalidOperation, AttributeError):
        msg = bot.send_message(message.chat.id, "Enter a positive amount, for example 500")
        bot.register_next_step_handler(msg, receive_topup_amount, master_id)
        return

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT balance FROM masters WHERE telegram_id = %s AND is_active = TRUE",
            (master_id,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        bot.send_message(message.chat.id, "🔄 Swapper account not found")
        return

    old_balance = Decimal(row[0])
    new_balance = old_balance + amount
    admin_topups[message.from_user.id] = {
        "master_id": master_id,
        "amount": amount,
    }
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Confirm Top Up", callback_data=f"adm_topup_confirm_{master_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"adm_topup_cancel_{master_id}"),
    )
    bot.send_message(
        message.chat.id,
        f"""💰 Confirm balance top up

🔄 Swapper: {master_id}
💼 Current balance: {format_money(old_balance)} USDT
➕ Top-up amount: {format_money(amount)} USDT
✅ Balance after top up: {format_money(new_balance)} USDT""",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_topup_cancel_"))
def cancel_topup(call):
    if not require_admin_callback(call):
        return
    admin_topups.pop(call.from_user.id, None)
    bot.edit_message_text("Top up cancelled", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_topup_confirm_"))
def confirm_topup(call):
    if not require_admin_callback(call):
        return
    master_id = int(call.data.rsplit("_", 1)[1])
    pending = admin_topups.get(call.from_user.id)
    if not pending or pending["master_id"] != master_id:
        bot.answer_callback_query(call.id, "Top-up request expired. Start again.", show_alert=True)
        return
    amount = pending["amount"]

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT balance FROM masters
            WHERE telegram_id = %s AND is_active = TRUE
            FOR UPDATE
            """,
            (master_id,),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            bot.answer_callback_query(call.id, "Swapper account not found", show_alert=True)
            return
        old_balance = Decimal(row[0])
        new_balance = old_balance + amount
        cur.execute(
            "UPDATE masters SET balance = %s WHERE telegram_id = %s",
            (new_balance, master_id),
        )
        add_audit(
            cur,
            call.from_user.id,
            actor_name(call.from_user),
            "ADMIN_TOP_UP",
            "master_balance",
            master_id,
            old_balance,
            new_balance,
            f"top_up_amount={amount}",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

    admin_topups.pop(call.from_user.id, None)
    bot.edit_message_text(
        f"""✅ Balance topped up

🔄 Swapper: {master_id}
➕ Added: {format_money(amount)} USDT
💼 Balance: {format_money(old_balance)} → {format_money(new_balance)} USDT""",
        call.message.chat.id,
        call.message.message_id,
    )
    bot.answer_callback_query(call.id, "Balance topped up")
    try:
        bot.send_message(
            master_id,
            f"💰 Your balance was topped up by {format_money(amount)} USDT.\n"
            f"💼 New balance: {format_money(new_balance)} USDT",
        )
    except Exception as e:
        log("TOP UP NOTIFICATION ERROR", master_id, repr(e))
    notify_admin(
        f"💰 Swapper balance topped up\n"
        f"Admin: {actor_name(call.from_user)}\n"
        f"Swapper: {master_id}\n"
        f"Amount: {format_money(amount)} USDT\n"
        f"Balance: {format_money(old_balance)} → {format_money(new_balance)} USDT"
    )


@bot.message_handler(func=lambda message: message.text == "Create Date")
def create_order(message):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT reason, order_id
            FROM client_blacklist
            WHERE client_telegram_id = %s
              AND is_active = TRUE
            """,
            (message.from_user.id,),
        )
        blacklist_row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if blacklist_row:
        reason, source_order_id = blacklist_row
        bot.send_message(
            message.chat.id,
            "⛔ You cannot create a new Date Request. Please contact an administrator.",
        )
        notify_admin(
            f"⛔ Blacklisted client tried to create a Date Request\n"
            f"Client TG ID: {message.from_user.id}\n"
            f"Blacklist source: request #{source_order_id or '—'}\n"
            f"Reason: {reason}"
        )
        return
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'NEW', 'NEW', 'NO_GIFT')
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
        add_status_history(cur, order_id, None, "NEW", "NO_GIFT", message.from_user)
        add_audit(
            cur,
            message.from_user.id,
            actor_name(message.from_user),
            "CREATE_LEAD",
            "order",
            order_id,
            None,
            "NEW",
        )
        conn.commit()
        cur.close()
        conn.close()

        lead_dispatch_queue.put((order_id, dict(data)))

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
            notify_admin(f"❌ Could not send request #{order_id} to Swapper {telegram_id}: {repr(e)}")

    notify_admin("📨 Date request sent to Swappers:\n\n" + text)

    cur.close()
    conn.close()


def lead_dispatch_worker():
    while True:
        order_id, data = lead_dispatch_queue.get()
        try:
            send_order_to_masters(order_id, data)
        except Exception as e:
            log("LEAD DISPATCH WORKER ERROR", order_id, repr(e))
            notify_admin(f"❌ Could not dispatch request #{order_id}: {repr(e)}")
        finally:
            lead_dispatch_queue.task_done()


def complete_accepted_order_group(order_id, master_id, client_id):
    try:
        invite_link, group_chat_id = create_order_group(order_id)
        group_chat_id = int(f"-100{group_chat_id}")

        notify_admin(f"""✅ Group created for Date Request #{order_id}

🔄 Swapper TG ID: {master_id}
Client TG ID: {client_id}
Group ID: {group_chat_id}
Invite: {invite_link}
""")

        try:
            send_main_menu(
                client_id,
                f"✅ Swapper accepted date request #{order_id}\nHere is your chat link:\n{invite_link}",
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
            notify_admin(f"❌ Could not send invite to Swapper for request #{order_id}: {repr(e)}")

        try:
            send_and_pin_group_card(
                group_chat_id,
                build_group_status_text(order_id, "IN_CHAT", "NO_GIFT"),
                order_group_keyboard(order_id),
            )
            notify_admin(f"📨 Status card sent to group for request #{order_id}")
        except Exception as e:
            log("SEND STATUS CARD TO GROUP ERROR", repr(e))
            notify_admin(f"❌ Could not send status card to group for request #{order_id}: {repr(e)}")
    except Exception as e:
        log("GROUP CREATION WORKER ERROR", order_id, repr(e))
        notify_admin(f"❌ GROUP CREATION ERROR for request #{order_id}: {repr(e)}")
        try:
            bot.send_message(
                master_id,
                f"❌ Could not create the group for request #{order_id}. The admin has been notified.",
            )
        except Exception as send_error:
            log("GROUP CREATION FAILURE MESSAGE ERROR", order_id, repr(send_error))


def group_creation_worker():
    while True:
        order_id, master_id, client_id = group_creation_queue.get()
        try:
            complete_accepted_order_group(order_id, master_id, client_id)
        finally:
            group_creation_queue.task_done()


@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_"))
def accept_order(call):
    try:
        log("ACCEPT HANDLER FIRED", call.data, call.from_user.id)

        order_id = int(call.data.split("_")[1])
        master_id = call.from_user.id

        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            "SELECT order_status, payment_status FROM orders WHERE id = %s FOR UPDATE",
            (order_id,),
        )
        previous = cur.fetchone()
        if not previous:
            conn.rollback()
            cur.close()
            conn.close()
            bot.answer_callback_query(call.id, "Request not found")
            return
        old_status, old_payment_status = previous

        cur.execute(
            """
            UPDATE orders
            SET status = 'ASSIGNED',
                order_status = 'ASSIGNED',
                master_telegram_id = %s,
                accepted_at = NOW()
            WHERE id = %s
              AND status = 'NEW'
            RETURNING client_telegram_id, payment_status
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

        client_id, payment_status = row
        add_status_history(cur, order_id, "NEW", "ASSIGNED", payment_status, call.from_user)
        add_audit(
            cur, master_id, actor_name(call.from_user), "ACCEPT_LEAD",
            "order", order_id, "NEW", "ASSIGNED",
        )
        conn.commit()
        cur.close()
        conn.close()

        bot.answer_callback_query(call.id, "Accepted — creating the group…")
        try:
            bot.send_message(master_id, f"✅ Request #{order_id} accepted. Creating the group…")
        except Exception as e:
            log("ACCEPT CONFIRMATION MESSAGE ERROR", order_id, repr(e))
        group_creation_queue.put((order_id, master_id, client_id))
        notify_admin(
            f"✅ Swapper accepted request #{order_id}\n"
            f"🔄 Swapper TG ID: {master_id}\nClient TG ID: {client_id}"
        )

    except Exception as e:
        log("ACCEPT ERROR", repr(e))
        notify_admin(f"❌ ACCEPT ERROR: {repr(e)}")


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("gift_") or call.data.startswith("paid_")
)
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
            bot.answer_callback_query(call.id, "Only the assigned Swapper can send Gift")
            return

        if payment_status in ("PAID", "GIFT"):
            bot.answer_callback_query(call.id, "Gift was already recorded")
            return

        msg = bot.send_message(
            master_id,
            f"Enter the total Gift amount for request #{order_id} (USDT):",
        )
        bot.register_next_step_handler(
            msg,
            save_paid_amount,
            order_id,
            call.message.chat.id,
        )
        bot.answer_callback_query(call.id, "Enter the total amount in private chat")

    except Exception as e:
        log("GIFT ERROR", repr(e))
        notify_admin(f"❌ GIFT ERROR: {repr(e)}")


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
            bot.send_message(master_id, "You are not the assigned Swapper for this request")
            return

        if payment_status in ("PAID", "GIFT"):
            conn.rollback()
            bot.send_message(master_id, "Gift was already recorded; balance was not charged again")
            return

        cur.execute(
            "SELECT balance FROM masters WHERE telegram_id = %s FOR UPDATE",
            (master_id,),
        )
        balance_row = cur.fetchone()
        if not balance_row:
            conn.rollback()
            bot.send_message(master_id, "Swapper account not found")
            return
        old_balance = balance_row[0]

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
            bot.send_message(master_id, "Active Swapper account not found")
            return

        master_balance = master_row[0]
        cur.execute(
            """
            UPDATE orders
            SET payment_status = 'GIFT',
                paid_amount = %s,
                commission_amount = %s,
                master_balance_after = %s
            WHERE id = %s
            """,
            (paid_amount, commission, master_balance, order_id),
        )
        add_status_history(
            cur, order_id, order_status, order_status, "GIFT", message.from_user
        )
        add_audit(
            cur, master_id, actor_name(message.from_user), "MARK_GIFT",
            "order", order_id, payment_status, "GIFT",
            f"total={paid_amount}; commission={commission}",
        )
        add_audit(
            cur, master_id, actor_name(message.from_user), "COMMISSION_CHARGED",
            "master_balance", master_id, old_balance, master_balance,
            f"order_id={order_id}; commission={commission}",
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
        "GIFT",
    )

    send_and_pin_group_card(
        group_chat_id or source_group_id,
        status_text,
        order_group_keyboard(order_id),
    )
    bot.send_message(
        master_id,
        f"Payment saved. Fee charged: {commission} USDT. Balance: {master_balance} USDT",
    )

    notify_admin(
        f"🎁 Date request #{order_id}: marked as GIFT\n"
        f"Gift total: {paid_amount} USDT\n"
        f"🔄 Swapper fee (30%): {commission} USDT\n"
        f"💼 Swapper balance: {master_balance} USDT"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("done_"))
def mark_done(call):
    try:
        order_id = int(call.data.split("_")[1])

        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            "SELECT order_status, payment_status FROM orders WHERE id = %s FOR UPDATE",
            (order_id,),
        )
        previous = cur.fetchone()
        if not previous:
            conn.rollback()
            cur.close()
            conn.close()
            bot.answer_callback_query(call.id, "Request not found")
            return
        old_status, old_payment_status = previous

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

        add_status_history(
            cur, order_id, old_status, "DONE", old_payment_status, call.from_user
        )
        add_audit(
            cur, call.from_user.id, actor_name(call.from_user), "MARK_DONE",
            "order", order_id, old_status, "DONE",
        )

        conn.commit()
        cur.close()
        conn.close()

        group_chat_id = row[0] if row else None
        payment_status = row[1] if row else "GIFT"

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


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("dispute_") and call.data[8:].isdigit()
)
def start_dispute(call):
    try:
        order_id = int(call.data.split("_")[1])
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT order_status, payment_status, master_telegram_id
                FROM orders
                WHERE id = %s
                """,
                (order_id,),
            )
            order = cur.fetchone()
        finally:
            cur.close()
            conn.close()
        if not order:
            bot.answer_callback_query(call.id, "Request not found")
            return
        order_status, payment_status, master_id = order
        if call.from_user.id != master_id and not is_admin(call.from_user.id):
            bot.answer_callback_query(
                call.id,
                "Only the assigned Swapper or an admin can open Dispute",
                show_alert=True,
            )
            return
        if payment_status in ("GIFT", "PAID"):
            bot.answer_callback_query(call.id, "Gift was already recorded", show_alert=True)
            return
        if payment_status == "DISPUTE" or order_status == "DISPUTE":
            bot.answer_callback_query(call.id, "Dispute is already open")
            return

        msg = bot.send_message(
            call.from_user.id,
            f"📝 Why did Date Request #{order_id} go to Dispute?\n"
            "Enter a required comment:",
        )
        bot.register_next_step_handler(
            msg,
            receive_dispute_comment,
            order_id,
            call.message.chat.id,
            call.message.message_id,
        )
        bot.answer_callback_query(call.id, "Enter the reason in private chat")
    except Exception as e:
        log("START DISPUTE ERROR", repr(e))
        notify_admin(f"❌ START DISPUTE ERROR: {repr(e)}")


def receive_dispute_comment(message, order_id, source_chat_id, source_message_id):
    comment = (message.text or "").strip()
    if len(comment) < 3:
        msg = bot.send_message(message.chat.id, "Please enter a meaningful comment (at least 3 characters):")
        bot.register_next_step_handler(
            msg,
            receive_dispute_comment,
            order_id,
            source_chat_id,
            source_message_id,
        )
        return
    if len(comment) > 1000:
        msg = bot.send_message(message.chat.id, "Comment is too long. Maximum: 1000 characters.")
        bot.register_next_step_handler(
            msg,
            receive_dispute_comment,
            order_id,
            source_chat_id,
            source_message_id,
        )
        return

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT master_telegram_id, payment_status, order_status FROM orders WHERE id = %s",
            (order_id,),
        )
        order = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not order:
        bot.send_message(message.chat.id, "Request not found")
        return
    master_id, payment_status, order_status = order
    if message.from_user.id != master_id and not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Access denied")
        return
    if payment_status in ("GIFT", "PAID", "DISPUTE") or order_status == "DISPUTE":
        bot.send_message(message.chat.id, "This request can no longer be moved to Dispute")
        return

    pending_disputes[message.from_user.id] = {
        "order_id": order_id,
        "comment": comment,
        "source_chat_id": source_chat_id,
        "source_message_id": source_message_id,
    }
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            "🚫 Add client to blacklist",
            callback_data=f"dsp_bl_yes_{order_id}",
        ),
        InlineKeyboardButton(
            "✅ Dispute without blacklist",
            callback_data=f"dsp_bl_no_{order_id}",
        ),
        InlineKeyboardButton(
            "❌ Cancel Dispute",
            callback_data=f"dsp_cancel_{order_id}",
        ),
    )
    bot.send_message(
        message.chat.id,
        f"""⚠️ Confirm Dispute for Date Request #{order_id}

📝 Reason:
{comment}

Should this client be added to the blacklist?""",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("dsp_cancel_"))
def cancel_dispute(call):
    pending_disputes.pop(call.from_user.id, None)
    bot.edit_message_text("Dispute cancelled", call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("dsp_bl_yes_") or call.data.startswith("dsp_bl_no_")
)
def finalize_dispute(call):
    add_to_blacklist = call.data.startswith("dsp_bl_yes_")
    order_id = int(call.data.rsplit("_", 1)[1])
    pending = pending_disputes.get(call.from_user.id)
    if not pending or pending["order_id"] != order_id:
        bot.answer_callback_query(call.id, "Dispute request expired. Start again.", show_alert=True)
        return

    comment = pending["comment"]
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT order_status, payment_status, tg_group_id,
                   client_telegram_id, client_username, master_telegram_id
            FROM orders
            WHERE id = %s
            FOR UPDATE
            """,
            (order_id,),
        )
        order = cur.fetchone()
        if not order:
            conn.rollback()
            bot.answer_callback_query(call.id, "Request not found", show_alert=True)
            return
        (
            old_status,
            old_payment_status,
            group_chat_id,
            client_id,
            client_username,
            master_id,
        ) = order
        if call.from_user.id != master_id and not is_admin(call.from_user.id):
            conn.rollback()
            bot.answer_callback_query(call.id, "Access denied", show_alert=True)
            return
        if old_payment_status in ("GIFT", "PAID", "DISPUTE") or old_status == "DISPUTE":
            conn.rollback()
            pending_disputes.pop(call.from_user.id, None)
            bot.answer_callback_query(call.id, "Status already changed", show_alert=True)
            return

        cur.execute(
            """
            UPDATE orders
            SET payment_status = 'DISPUTE',
                order_status = 'DISPUTE',
                dispute_comment = %s,
                dispute_blacklisted = %s,
                dispute_opened_at = NOW()
            WHERE id = %s
            """,
            (comment, add_to_blacklist, order_id),
        )
        if add_to_blacklist:
            cur.execute(
                """
                INSERT INTO client_blacklist (
                    client_telegram_id, client_username, reason, order_id,
                    added_by_telegram_id, added_by_name, is_active, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, NOW())
                ON CONFLICT (client_telegram_id) DO UPDATE
                SET client_username = EXCLUDED.client_username,
                    reason = EXCLUDED.reason,
                    order_id = EXCLUDED.order_id,
                    added_by_telegram_id = EXCLUDED.added_by_telegram_id,
                    added_by_name = EXCLUDED.added_by_name,
                    is_active = TRUE,
                    updated_at = NOW()
                """,
                (
                    client_id,
                    client_username,
                    comment,
                    order_id,
                    call.from_user.id,
                    actor_name(call.from_user),
                ),
            )
        add_status_history(
            cur, order_id, old_status, "DISPUTE", "DISPUTE", call.from_user
        )
        add_audit(
            cur,
            call.from_user.id,
            actor_name(call.from_user),
            "OPEN_DISPUTE",
            "order",
            order_id,
            f"{old_status}/{old_payment_status}",
            "DISPUTE/DISPUTE",
            f"comment={comment}; blacklisted={add_to_blacklist}; client_id={client_id}",
        )
        if add_to_blacklist:
            add_audit(
                cur,
                call.from_user.id,
                actor_name(call.from_user),
                "BLACKLIST_CLIENT",
                "client",
                client_id,
                "active=False",
                "active=True",
                f"order_id={order_id}; reason={comment}",
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        log("DISPUTE ERROR", repr(e))
        notify_admin(f"❌ DISPUTE ERROR: {repr(e)}")
        bot.answer_callback_query(call.id, "Could not open Dispute", show_alert=True)
        return
    finally:
        cur.close()
        conn.close()

    pending_disputes.pop(call.from_user.id, None)
    if group_chat_id and not str(group_chat_id).startswith("-100"):
        group_chat_id = int(f"-100{group_chat_id}")
    blacklist_text = "YES 🚫" if add_to_blacklist else "NO"
    dispute_text = (
        build_group_status_text(order_id, "DISPUTE", "DISPUTE")
        + f"\n📝 Dispute reason:\n{comment}\n\n🚫 Client blacklisted: {blacklist_text}"
    )

    bot.edit_message_text(
        f"""✅ Dispute opened for Date Request #{order_id}

📝 Reason:
{comment}

🚫 Client blacklisted: {blacklist_text}""",
        call.message.chat.id,
        call.message.message_id,
    )
    bot.answer_callback_query(call.id, "Dispute opened")
    try:
        bot.edit_message_text(
            chat_id=pending["source_chat_id"],
            message_id=pending["source_message_id"],
            text=dispute_text,
        )
    except Exception as e:
        log("EDIT DISPUTE CARD ERROR", repr(e))
        if group_chat_id:
            try:
                bot.send_message(group_chat_id, dispute_text)
            except Exception as send_error:
                log("SEND DISPUTE CARD TO GROUP ERROR", repr(send_error))

    notify_admin(
        f"⚠️ Date request #{order_id}: dispute opened\n"
        f"By: {actor_name(call.from_user)}\n"
        f"Client TG ID: {client_id}\n"
        f"Reason: {comment}\n"
        f"Client blacklisted: {blacklist_text}"
    )


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


def send_low_balance_reminders():
    while True:
        conn = None
        cur = None
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE masters
                SET last_low_balance_reminder_at = NULL
                WHERE balance >= %s
                  AND last_low_balance_reminder_at IS NOT NULL
                """,
                (LOW_BALANCE_THRESHOLD,),
            )
            cur.execute(
                """
                UPDATE masters
                SET last_low_balance_reminder_at = NOW()
                WHERE telegram_id IN (
                    SELECT telegram_id
                    FROM masters
                    WHERE is_active = TRUE
                      AND balance < %s
                      AND (
                          last_low_balance_reminder_at IS NULL
                          OR last_low_balance_reminder_at <= NOW() - (%s * INTERVAL '1 hour')
                      )
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING telegram_id, balance
                """,
                (LOW_BALANCE_THRESHOLD, LOW_BALANCE_REMINDER_INTERVAL_HOURS),
            )
            swappers = cur.fetchall()
            conn.commit()

            for telegram_id, balance in swappers:
                try:
                    bot.send_message(
                        telegram_id,
                        f"""🚨 Hey, Swapper! Your wallet has started a diet 🥲

💼 Balance: {format_money(balance)} USDT
🎯 Minimum comfortable balance: {format_money(LOW_BALANCE_THRESHOLD)} USDT

Time to feed the wallet before it starts asking other wallets for snacks 🍔💸
Please contact an admin to top up your balance.""",
                    )
                    log("LOW BALANCE REMINDER SENT", telegram_id, format_money(balance))
                except Exception as e:
                    log("LOW BALANCE REMINDER SEND ERROR", telegram_id, repr(e))
        except Exception as e:
            if conn is not None:
                conn.rollback()
            log("LOW BALANCE REMINDER LOOP ERROR", repr(e))
        finally:
            if cur is not None:
                cur.close()
            if conn is not None:
                conn.close()

        time.sleep(LOW_BALANCE_CHECK_INTERVAL_SECONDS)


def send_unresolved_lead_reminders():
    while True:
        conn = None
        cur = None
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE orders
                SET lead_followup_reminded_at = NOW()
                WHERE id IN (
                    SELECT id
                    FROM orders
                    WHERE master_telegram_id IS NOT NULL
                      AND lead_followup_reminded_at IS NULL
                      AND created_at >= (
                          SELECT setting_value::TIMESTAMPTZ
                          FROM app_settings
                          WHERE setting_key = 'lead_reminders_started_at_v1'
                      )
                      AND created_at <= NOW() - (%s * INTERVAL '1 hour')
                      AND COALESCE(payment_status, 'NO_GIFT') NOT IN ('GIFT', 'PAID', 'DISPUTE')
                      AND COALESCE(order_status, status, 'NEW') <> 'DISPUTE'
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, master_telegram_id, invite_link
                """,
                (LEAD_FOLLOWUP_DELAY_HOURS,),
            )
            leads = cur.fetchall()
            conn.commit()

            for order_id, master_id, invite_link in leads:
                group_line = f"\n\ud83d\udc49 Open group: {invite_link}" if invite_link else ""
                try:
                    bot.send_message(
                        master_id,
                        f"""⏰ Hey, Swapper! What happened with Date Request #{order_id}? 👀

It has been 8 hours, but the lead still has no 🎁 Gift and no ⚠️ Dispute.
Please check the chat and update the status — this lead is starting to feel forgotten 🥲{group_line}""",
                    )
                    log("UNRESOLVED LEAD REMINDER SENT", order_id, master_id)
                except Exception as e:
                    log("UNRESOLVED LEAD REMINDER SEND ERROR", order_id, master_id, repr(e))
                    try:
                        cur.execute(
                            "UPDATE orders SET lead_followup_reminded_at = NULL WHERE id = %s",
                            (order_id,),
                        )
                        conn.commit()
                    except Exception as reset_error:
                        conn.rollback()
                        log("UNRESOLVED LEAD REMINDER RESET ERROR", order_id, repr(reset_error))
        except Exception as e:
            if conn is not None:
                conn.rollback()
            log("UNRESOLVED LEAD REMINDER LOOP ERROR", repr(e))
        finally:
            if cur is not None:
                cur.close()
            if conn is not None:
                conn.close()

        time.sleep(LEAD_FOLLOWUP_CHECK_INTERVAL_SECONDS)


ensure_balance_schema()
try:
    correction_904 = correct_gift_amount(
        904,
        Decimal("1500.00"),
        actor_display="System correction requested by admin",
    )
    if correction_904["new_balance"] is not None:
        log("ORDER 904 GIFT CORRECTED", "1500.00 USDT")
except Exception as e:
    log("ORDER 904 GIFT CORRECTION ERROR", repr(e))
    notify_admin(f"❌ ORDER 904 GIFT CORRECTION ERROR: {repr(e)}")
try:
    if apply_order_904_duplicate_charge_fix():
        log("ORDER 904 DUPLICATE GIFT REFUND APPLIED", "900.00 USDT")
except Exception as e:
    log("ORDER 904 DUPLICATE GIFT REFUND ERROR", repr(e))
    notify_admin(f"❌ ORDER 904 DUPLICATE GIFT REFUND ERROR: {repr(e)}")
try:
    if remove_swapper_8649754773_once():
        log("SWAPPER REMOVED", 8649754773)
        notify_admin("✅ Swapper 8649754773 removed from active Swappers")
except Exception as e:
    log("REMOVE SWAPPER 8649754773 ERROR", repr(e))
    notify_admin(f"❌ REMOVE SWAPPER 8649754773 ERROR: {repr(e)}")
threading.Thread(target=setup_webhook, daemon=True).start()
threading.Thread(target=send_low_balance_reminders, daemon=True).start()
threading.Thread(target=send_unresolved_lead_reminders, daemon=True).start()
threading.Thread(target=lead_dispatch_worker, daemon=True).start()
threading.Thread(target=group_creation_worker, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
