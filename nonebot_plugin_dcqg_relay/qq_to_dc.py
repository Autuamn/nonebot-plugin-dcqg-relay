import asyncio
import re
from sqlite3 import Connection
from typing import Optional

import filetype
from nonebot import logger
from nonebot.adapters.discord import Bot as dc_Bot
from nonebot.adapters.discord.api import UNSET, Embed, EmbedAuthor, File, MessageGet
from nonebot.adapters.discord.exception import NetworkError
from nonebot.adapters.qq import (
    Bot as qq_Bot,
    GuildMessageEvent as qq_GuildMessageEvent,
    MessageDeleteEvent as qq_MessageDeleteEvent,
)
from nonebot.adapters.qq.models import Message as qq_Message, MessageReference

from .config import Link, plugin_config
from .qq_emoji_dict import qq_emoji_dict
from .utils import get_dc_member_name, get_file_bytes

channel_links: list[Link] = plugin_config.smd_channel_links


async def get_qq_member_name(bot: qq_Bot, guild_id: str, user_id: str) -> str:
    member = await bot.get_member(guild_id=guild_id, user_id=user_id)
    return member.nick or (member.user.username if member.user else "") or ""


async def get_dc_member_avatar(bot: dc_Bot, guild_id: int, user_id: int) -> str:
    member = await bot.get_guild_member(guild_id=guild_id, user_id=user_id)
    if member.avatar is not UNSET and (avatar := member.avatar):
        return (
            f"https://cdn.discordapp.com/guilds/{guild_id}/users/{user_id}/avatars/{avatar}."
            + ("gif" if re.match(r"^a_.*", avatar) else "webp")
        )
    elif (user := member.user) and user is not UNSET and user.avatar:
        return f"https://cdn.discordapp.com/avatars/{user_id}/{user.avatar}." + (
            "gif" if re.match(r"^a_.*", user.avatar) else "webp"
        )
    else:
        return ""


async def build_dc_file(url: str) -> File:
    """获取图片文件，用于发送到 Discord"""
    img_bytes = await get_file_bytes(url)
    match = filetype.match(img_bytes)
    kind = match.extension if match else "dat"
    return File(content=img_bytes, filename=f"{url.split('/')[-1]!s}.{kind}")


async def build_dc_embeds(
    bot: qq_Bot,
    dc_bot: dc_Bot,
    reference: MessageReference,
    reply: qq_Message,
    conn: Connection,
    channel_link: Link,
) -> list[Embed]:
    """处理 QQ 转 discord 中的回复部分"""
    guild_id, channel_id = channel_link.dc_guild_id, channel_link.dc_channel_id

    author = ""
    timestamp = f"<t:{int(reply.timestamp.timestamp())}:R>" if reply.timestamp else ""

    if db_select := conn.execute(
        f"SELECT DCID FROM ID WHERE QQID LIKE ('%{reference.message_id}%')"
    ).fetchone():
        reference_id = db_select[0]
        dc_message = await dc_bot.get_channel_message(
            channel_id=channel_id, message_id=reference_id
        )
        if reply.author.id == (await bot.me()).id:
            author = EmbedAuthor(
                name=await get_dc_member_name(dc_bot, guild_id, dc_message.author.id)
                + f"(@{dc_message.author.username})",
                icon_url=await get_dc_member_avatar(
                    dc_bot, guild_id, dc_message.author.id
                ),
            )
            timestamp = f"<t:{int(dc_message.timestamp.timestamp())}:R>"

        description = (
            f"{dc_message.content}\n\n"
            + timestamp
            + f"[[ ↑ ]](https://discord.com/channels/{guild_id}/{channel_id}/{reference_id})"
        )
    else:
        description = f"{reply.content}\n\n" + timestamp + "[ ? ]"

    if not author:
        member = await bot.get_member(guild_id=reply.guild_id, user_id=reply.author.id)
        author = EmbedAuthor(
            name=(member.nick or (member.user.username if member.user else "") or "")
            + f"[ID:{reply.author.id}]",
            icon_url=(member.user.avatar if member.user else "") or "",
        )

    embeds = [
        Embed(
            author=author,
            description=description,
        )
    ]
    return embeds


async def build_dc_message(
    bot: qq_Bot, event: qq_GuildMessageEvent
) -> tuple[str, list[str]]:
    """获取 QQ 消息，用于发送到 discord"""
    text = ""
    img_list: list[str] = []
    for msg in event.get_message():
        if msg.type == "text":
            # 文本
            text += (
                str(msg.data["text"])
                .replace("@everyone", "@.everyone")
                .replace("@here", "@.here")
            )
        elif msg.type == "emoji":
            # 表情
            text += (
                f"[{qq_emoji_dict.get(msg.data['id'], 'QQemojiID:' + msg.data['id'])}]"
            )
        elif msg.type == "mention_user":
            # @人
            text += (
                f"@{await get_qq_member_name(bot, event.guild_id, msg.data['user_id'])}"
                + f"[ID:{msg.data['user_id']}]"
                + " "
            )
        elif msg.type == "image":
            # 图片
            img_list.append(msg.data["url"])
    return text, img_list


async def send_to_discord(
    bot: dc_Bot,
    webhook_id: int,
    token: str,
    text: Optional[str],
    img_list: Optional[list[str]],
    embed: Optional[list[Embed]],
    username: Optional[str],
    avatar_url: Optional[str],
) -> MessageGet:
    """用 webhook 发送到 discord"""
    logger.debug("send_to_discord")

    if img_list:
        get_img_tasks = [build_dc_file(img) for img in img_list]
        files = await asyncio.gather(*get_img_tasks)
    else:
        files = None

    try_times = 1
    while True:
        try:
            send = await bot.execute_webhook(
                webhook_id=webhook_id,
                token=token,
                content=text or "",
                files=files,
                embeds=embed,
                username=username,
                avatar_url=avatar_url,
                wait=True,
            )
            break
        except NetworkError as e:
            logger.warning(f"retry {try_times}")
            if try_times == 3:
                raise e
            try_times += 1
            await asyncio.sleep(5)

    logger.debug("send")
    return send


async def create_qq_to_dc(
    bot: qq_Bot, event: qq_GuildMessageEvent, dc_bot: dc_Bot, conn: Connection
):
    """QQ 消息转发到 discord"""

    text, img_list = await build_dc_message(bot, event)
    link = next(
        link for link in channel_links if link.qq_channel_id == event.channel_id
    )

    if (reply := event.reply) and (reference := event.message_reference):
        embeds = await build_dc_embeds(bot, dc_bot, reference, reply, conn, link)
    else:
        embeds = None

    username = f"{event.author.username} [ID:{event.author.id}]"
    avatar = event.author.avatar

    try_times = 1
    while True:
        try:
            send = await send_to_discord(
                dc_bot,
                link.webhook_id,
                link.webhook_token,
                text,
                img_list,
                embeds,
                username,
                avatar,
            )
            break
        except NameError as e:
            logger.warning(f"retry {try_times}")
            if try_times == 3:
                raise e
            try_times += 1
            await asyncio.sleep(5)

    if send:
        conn.execute(f'INSERT INTO ID (DCID, QQID) VALUES ({send.id}, "{event.id}")')


async def delete_qq_to_dc(
    event: qq_MessageDeleteEvent, dc_bot: dc_Bot, conn: Connection, just_delete: list
):
    if (id := event.message.id) in just_delete:
        just_delete.remove(id)
        return
    try_times = 1
    while True:
        try:
            db_selected = conn.execute(
                f"SELECT DCID FROM ID WHERE QQID LIKE ('%{event.message.id}%')"
            )
            for msgids in db_selected:
                for msgid in msgids:
                    channel_id = next(
                        link.dc_channel_id
                        for link in channel_links
                        if link.qq_channel_id == event.message.channel_id
                    )
                    await dc_bot.delete_message(message_id=msgid, channel_id=channel_id)
                    just_delete.append(msgid)
                    conn.execute(f"DELETE FROM ID WHERE DCID={msgid}")
            break
        except UnboundLocalError or TypeError or NameError as e:
            logger.warning(f"retry {try_times}")
            if try_times == 3:
                raise e
            try_times += 1
            await asyncio.sleep(5)
