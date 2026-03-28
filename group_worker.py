import os
import asyncio
import psycopg2
from telethon import TelegramClient
from telethon.tl.functions.channels import CreateChannelRequest, InviteToChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest, SendMessageRequest

TG_API_ID = int(os.getenv("TG_API_ID"))
TG_API_HASH = os.getenv("TG_API_HASH")
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # example: Swapdatebot

SESSION_NAME = "/opt/render/project/src/swapbot_session"


def get_conn():
    return psycopg2.connect(DATABASE_URL)


async def create_group_async(order_id):
    async with TelegramClient(SESSION_NAME, TG_API_ID, TG_API_HASH) as client:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                service_type,
                price,
                client_username,
                client_telegram_id,
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
            client_telegram_id,
            contact_text,
            format_type,
            time_from,
            time_to,
            profile_name,
            master_telegram_id,
            payment_status,
            order_status,
        ) = row

        title = f"Date Request #{order_id}"

        result = await client(
            CreateChannelRequest(
                title=title,
                about=f"Private chat for date request #{order_id}",
                megagroup=True,
            )
        )

        channel = result.chats[0]

        if BOT_USERNAME:
            try:
                bot_entity = await client.get_entity(BOT_USERNAME)
                await client(
                    InviteToChannelRequest(
                        channel=channel,
                        users=[bot_entity],
                    )
                )
            except Exception as e:
                print("ADD BOT TO GROUP ERROR:", repr(e), flush=True)

        invite = await client(ExportChatInviteRequest(channel))
        invite_link = invite.link

        client_label = f"@{client_username}" if client_username else str(client_telegram_id)
        master_label = str(master_telegram_id) if master_telegram_id else "—"

        group_message = f"""📦 Date Request #{order_id}

Date type: {service_type}
Price: {price} USDT
Client: {client_label}
Contact: {contact_text}
Format: {format_type}
Time: {time_from}-{time_to}
Profile: {profile_name}
Master ID: {master_label}

Order status: {order_status}
Payment status: {payment_status}
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

        return invite_link, channel.id


def create_order_group(order_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(create_group_async(order_id))
    finally:
        loop.close()
