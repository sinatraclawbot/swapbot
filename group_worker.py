import os
import asyncio
import psycopg2
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    DeleteChannelRequest,
    EditAdminRequest,
    InviteToChannelRequest,
)
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    ExportChatInviteRequest,
    SendMessageRequest,
)
from telethon.tl.types import ChatAdminRights, PeerChannel

TG_API_ID_RAW = os.getenv("TG_API_ID")
TG_API_HASH = os.getenv("TG_API_HASH")
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING")
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # example: Swapdatebot

if not TG_API_ID_RAW:
    raise RuntimeError("TG_API_ID is not set")
if not TG_API_HASH:
    raise RuntimeError("TG_API_HASH is not set")
if not TG_SESSION_STRING:
    raise RuntimeError("TG_SESSION_STRING is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

TG_API_ID = int(TG_API_ID_RAW)


def get_conn():
    return psycopg2.connect(DATABASE_URL)


async def create_group_async(order_id):
    async with TelegramClient(
        StringSession(TG_SESSION_STRING),
        TG_API_ID,
        TG_API_HASH,
    ) as client:
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
                order_status,
                is_returning_client,
                is_blacklisted_contact
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
            is_returning_client,
            is_blacklisted_contact,
        ) = row

        title_prefix = ""
        if is_blacklisted_contact:
            title_prefix += "🚫 "
        if is_returning_client:
            title_prefix += "🔁 "
        title = f"{title_prefix}Date Request #{order_id}"

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
                await client(
                    EditAdminRequest(
                        channel=channel,
                        user_id=bot_entity,
                        admin_rights=ChatAdminRights(pin_messages=True),
                        rank="SwapBot",
                    )
                )
            except Exception as e:
                print("ADD/PROMOTE BOT IN GROUP ERROR:", repr(e), flush=True)

        invite = await client(ExportChatInviteRequest(channel))
        invite_link = invite.link

        client_label = f"@{client_username}" if client_username else str(client_telegram_id)
        master_label = str(master_telegram_id) if master_telegram_id else "—"

        returning_label = "\n🔁 Returning client: YES" if is_returning_client else ""
        blacklist_label = "\n🚫 Blacklisted contact: YES" if is_blacklisted_contact else ""
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
{returning_label}
{blacklist_label}
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
        cur.execute(
            """
            INSERT INTO order_status_history (
                order_id, old_status, new_status, payment_status,
                actor_telegram_id, actor_name
            )
            VALUES (%s, %s, 'IN_CHAT', %s, %s, %s)
            """,
            (
                order_id,
                order_status,
                payment_status,
                master_telegram_id,
                "Telegram account",
            ),
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

async def delete_group_async(order_id, group_chat_id):
    channel_id = abs(int(group_chat_id))
    if str(channel_id).startswith("100"):
        channel_id = int(str(channel_id)[3:])

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT invite_link FROM orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
        invite_link = row[0] if row else None
    finally:
        cur.close()
        conn.close()

    async with TelegramClient(
        StringSession(TG_SESSION_STRING),
        TG_API_ID,
        TG_API_HASH,
    ) as client:
        channel = None
        if invite_link:
            invite_hash = invite_link.rstrip("/").rsplit("/", 1)[-1].lstrip("+")
            invite = await client(CheckChatInviteRequest(invite_hash))
            channel = getattr(invite, "chat", None)
        if channel is None:
            channel = await client.get_input_entity(PeerChannel(channel_id))
        await client(DeleteChannelRequest(channel=channel))


def delete_order_group(order_id, group_chat_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(delete_group_async(order_id, group_chat_id))
    finally:
        loop.close()
