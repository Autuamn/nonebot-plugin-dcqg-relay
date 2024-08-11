import asyncio
import re
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
from nonebot_plugin_orm import get_session
from sqlalchemy import select

from .config import LinkWithWebhook
from .model import MsgID
from .qq_emoji_dict import qq_emoji_dict
from .utils import get_dc_member_name, get_file_bytes


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
    link: LinkWithWebhook,
) -> list[Embed]:
    """处理 QQ 转 discord 中的回复部分"""
    guild_id, channel_id = link.dc_guild_id, link.dc_channel_id

    author = ""
    timestamp = f"<t:{int(reply.timestamp.timestamp())}:R>" if reply.timestamp else ""

    async with get_session() as session:
        if reference_id := await session.scalar(
            select(MsgID.dcid).filter(MsgID.qqid == reference.message_id).limit(1)
        ):
            dc_message = await dc_bot.get_channel_message(
                channel_id=channel_id, message_id=reference_id
            )
            if reply.author.id == (await bot.me()).id:
                name, _ = await get_dc_member_name(
                    dc_bot, guild_id, dc_message.author.id
                )
                author = EmbedAuthor(
                    name=name + f"(@{dc_message.author.username})",
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
            logger.warning(f"send_to_discord() error: {e}, retry {try_times}")
            if try_times == 3:
                raise e
            try_times += 1
            await asyncio.sleep(5)
    return send


async def create_qq_to_dc(
    bot: qq_Bot,
    event: qq_GuildMessageEvent,
    dc_bot: dc_Bot,
    link: LinkWithWebhook,
):
    """QQ 消息转发到 discord"""
    logger.debug("into create_qq_to_dc()")
    text, img_list = await build_dc_message(bot, event)

    if (reply := event.reply) and (reference := event.message_reference):
        embeds = await build_dc_embeds(bot, dc_bot, reference, reply, link)
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
            logger.warning(f"create_qq_to_dc() error: {e}, retry {try_times}")
            if try_times == 3:
                raise e
            try_times += 1
            await asyncio.sleep(5)

    async with get_session() as session:
        session.add(MsgID(dcid=send.id, qqid=event.id))
        await session.commit()
    logger.debug("finish create_qq_to_dc()")


async def delete_qq_to_dc(
    event: qq_MessageDeleteEvent,
    dc_bot: dc_Bot,
    link: LinkWithWebhook,
    just_delete: list,
):
    logger.debug("into delete_qq_to_dc()")
    if (id := event.message.id) in just_delete:
        just_delete.remove(id)
        return
    try_times = 1
    while True:
        try:
            async with get_session() as session:
                if msgids := await session.scalars(
                    select(MsgID).filter(MsgID.qqid == event.message.id)
                ):
                    for msgid in msgids:
                        await dc_bot.delete_message(
                            message_id=msgid.dcid, channel_id=link.dc_channel_id
                        )
                        just_delete.append(msgid.dcid)
                        await session.delete(msgid)
                    await session.commit()
            logger.debug("finish delete_qq_to_dc()")
            break
        except (UnboundLocalError, TypeError, NameError) as e:
            logger.warning(f"delete_qq_to_dc() error: {e}, retry {try_times}")
            if try_times == 3:
                raise e
            try_times += 1
            await asyncio.sleep(5)
