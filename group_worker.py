import os
import psycopg2
from telethon import TelegramClient
from telethon.tl.functions.messages import CreateChatRequest, ExportChatInviteRequest

API_ID = int(os.getenv("TG_API_ID"))
API_HASH = os.getenv("TG_API_HASH")
DATABASE_URL = os.getenv("DATABASE_URL")

client = TelegramClient("swapbot_session", API_ID, API_HASH)


def get_conn():
    return psycopg2.connect(DATABASE_URL)


async def create_order_group(order_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT client_telegram_id, master_telegram_id
        FROM orders
        WHERE id = %s
    """, (order_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return None

    title = f"Order #{order_id}"

    result = await client(CreateChatRequest(
        users=[],
        title=title
    ))

    chat = result.chats[0]
    chat_id = chat.id

    invite = await client(ExportChatInviteRequest(chat_id))
    invite_link = invite.link

    cur.execute("""
        UPDATE orders
        SET tg_group_id = %s,
            tg_group_title = %s,
            invite_link = %s
        WHERE id = %s
    """, (chat_id, title, invite_link, order_id))

    conn.commit()
    cur.close()
    conn.close()

    return invite_link
