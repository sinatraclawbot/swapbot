import os
import asyncio
import psycopg2
from telethon import TelegramClient
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest, SendMessageRequest

api_id = int(os.getenv("TG_API_ID"))
api_hash = os.getenv("TG_API_HASH")
DATABASE_URL = os.getenv("DATABASE_URL")

SESSION_NAME = "/opt/render/project/src/swapbot_session"


def get_conn():
    return psycopg2.connect(DATABASE_URL)


async def create_group_async(order_id):
    async with TelegramClient(SESSION_NAME, api_id, api_hash) as client:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                service_type,
                price,
                client_username,
                contact_text,
                incall_outcall,
                time_from,
                time_to,
                profile_name,
                master_telegram_id,
                payment_status,
                order_status
            FROM orders
            WHERE id = %s
            """,
            (order_id,),
        )

        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            raise ValueError(f"Order #{order_id} not found")

        (
            service_type,
            price,
            client_username,
            contact_text,
            incall_outcall,
            time_from,
            time_to,
            profile_name,
            master_telegram_id,
            payment_status,
            order_status,
        ) = row

        title = f"Order #{order_id}"

        result = await client(
            CreateChannelRequest(
                title=title,
                about=f"Private order chat #{order_id}",
                megagroup=True,
            )
        )

        channel = result.chats[0]

        invite = await client(ExportChatInviteRequest(channel))
        invite_link = invite.link

        master_label = f"`{master_telegram_id}`" if master_telegram_id else "—"
        client_label = f"@{client_username}" if client_username else contact_text

        group_message = f"""📦 Заказ #{order_id}

Услуга: {service_type}
Цена: {price} USDT
Клиент: {client_label}
Контакт: {contact_text}
Формат: {incall_outcall}
Время: {time_from}-{time_to}
Профиль: {profile_name}
Мастер ID: {master_label}

Статус: {order_status}
Оплата: {payment_status}
"""

        await client(
            SendMessageRequest(
                peer=channel,
                message=group_message,
            )
        )

        cur.execute(
            """
            UPDATE orders
            SET invite_link = %s,
                tg_group_title = %s,
                tg_group_id = %s,
                order_status = 'IN_CHAT'
            WHERE id = %s
            """,
            (invite_link, title, channel.id, order_id),
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
