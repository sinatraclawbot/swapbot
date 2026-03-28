import os
import asyncio
import psycopg2
from telethon import TelegramClient
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest, SendMessageRequest

api_id = int(os.getenv("TG_API_ID"))
api_hash = os.getenv("TG_API_HASH")
DATABASE_URL = os.getenv("DATABASE_URL")

SESSION_NAME = "swapbot_session"


def get_conn():
    return psycopg2.connect(DATABASE_URL)


async def create_group_async(order_id):
    async with TelegramClient(SESSION_NAME, api_id, api_hash) as client:

        title = f"Order #{order_id}"

        # Создаем группу
        result = await client(CreateChannelRequest(
            title=title,
            about="Chat between client and master",
            megagroup=True
        ))

        channel = result.chats[0]

        # Создаем invite link
        invite = await client(ExportChatInviteRequest(channel))
        invite_link = invite.link

        # Отправляем первое сообщение в группу
        await client(SendMessageRequest(
            peer=channel,
            message=f"""
📦 Заказ #{order_id}

Статус: ASSIGNED
Оплата: UNPAID

Используйте кнопки ниже:
💰 Оплачено
✅ Завершить
⚠️ Спор
"""
        ))

        # Сохраняем данные в БД
        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE orders
            SET invite_link = %s,
                tg_group_title = %s,
                tg_group_id = %s,
                order_status = 'IN_CHAT'
            WHERE id = %s
            """,
            (invite_link, title, channel.id, order_id)
        )

        conn.commit()
        cur.close()
        conn.close()

        return invite_link


def create_order_group(order_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        return loop.run_until_complete(create_group_async(order_id))
    finally:
        loop.close()
    try:
        return loop.run_until_complete(create_group_async(order_id))
    finally:
        loop.close()
