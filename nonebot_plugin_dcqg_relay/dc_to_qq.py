import asyncio
import io
import re
from typing import Optional

import filetype
from nonebot import logger
from nonebot.adapters.discord import (
    Bot as dc_Bot,
    MessageCreateEvent as dc_MessageCreateEvent,
    MessageDeleteEvent as dc_MessageDeleteEvent,
)
from nonebot.adapters.discord.api import UNSET
from nonebot.adapters.qq import (
    Bot as qq_Bot,
    Message as qq_SegmentMessage,
    MessageSegment as qq_MessageSegment,
)
from nonebot.adapters.qq.exception import AuditException
from nonebot.adapters.qq.models import Message as qq_Message
from nonebot_plugin_orm import get_session
from sqlalchemy import select
from PIL import Image

from .config import LinkWithWebhook, plugin_config
from .model import MsgID
from .utils import get_dc_member_name, get_file_bytes

discord_proxy = plugin_config.discord_proxy


async def get_qq_img(url: str, proxy: Optional[str]) -> io.BytesIO:
    img_bytes = await get_file_bytes(url, proxy)
    match = filetype.match(img_bytes)
    kind = match.extension if match else "webp"
    if kind == "webp":
        with Image.open(io.BytesIO(img_bytes)) as img:
            output = io.BytesIO()
            img.save(output, format="PNG")
    else:
        output = io.BytesIO(img_bytes)
    return output


async def get_dc_channel_name(bot: dc_Bot, guild_id: int, channel_id: int) -> str:
    channels = await bot.get_guild_channels(guild_id=guild_id)
    channel = next(channel for channel in channels if channel.id == channel_id)
    return (
        channel.name
        if channel.name is not UNSET and channel.name is not None
        else "(error:无名频道)"
    )


async def get_dc_role_name(bot: dc_Bot, guild_id: int, role_id: int) -> str:
    roles = await bot.get_guild_roles(guild_id=guild_id)
    role = next(role for role in roles if role.id == role_id)
    return role.name


async def build_qq_message(
    bot: dc_Bot, event: dc_MessageCreateEvent
) -> tuple[qq_SegmentMessage, list[str]]:
    qq_message = qq_SegmentMessage(
        qq_MessageSegment.text(
            (
                event.member.nick
                if event.member is not UNSET
                and event.member.nick is not UNSET
                and event.member.nick
                else event.author.global_name or ""
            )
            + f"(@{event.author.username}):\n"
        )
    )
    img_list: list[str] = []
    message = (
        event
        if event.content
        else (
            await bot.get_channel_message(
                channel_id=event.channel_id, message_id=event.message_id
            )
        )
    )
    content = message.content
    text_begin = 0
    for embed in re.finditer(
        r"<(?P<type>(@!|@&|@|#|/|:|a:|t:))(?P<param>.+?)>",
        content,
    ):
        if content := content[text_begin : embed.pos + embed.start()]:
            qq_message += qq_MessageSegment.text(content)
        text_begin = embed.pos + embed.end()
        if embed.group("type") in ("@!", "@"):
            nick, username = await get_dc_member_name(
                bot, event.guild_id, int(embed.group("param"))
            )
            qq_message += qq_MessageSegment.text("@" + nick + f"({username})")
        elif embed.group("type") == "@&":
            qq_message += qq_MessageSegment.text(
                "@"
                + (
                    await get_dc_role_name(
                        bot, event.guild_id, int(embed.group("param"))
                    )
                    if event.guild_id is not UNSET
                    else f"(error:未知用户组)({embed.group('param')})"
                )
            )
        elif embed.group("type") == "#":
            qq_message += qq_MessageSegment.text(
                "#"
                + (
                    await get_dc_channel_name(
                        bot, event.guild_id, int(embed.group("param"))
                    )
                    if event.guild_id is not UNSET
                    else f"(error:未知频道)({embed.group('param')})"
                )
            )
        elif embed.group("type") == "/":
            pass
        elif embed.group("type") in (":", "a:"):
            if len(cut := embed.group("param").split(":")) == 2:
                if not cut[1]:
                    qq_message += qq_MessageSegment.text(cut[0])
                else:
                    img_list.append(
                        "https://cdn.discordapp.com/emojis/"
                        + cut[1]
                        + "."
                        + ("gif" if embed.group("type") == "a:" else "webp")
                    )
            else:
                qq_message += qq_MessageSegment.text(embed.group())
        else:
            if embed.group().isdigit():
                qq_message += qq_MessageSegment.text(f"<t:{embed.group('param')}>")
            else:
                qq_message += qq_MessageSegment.text(embed.group())
    if content := content[text_begin:]:
        qq_message += qq_MessageSegment.text(content)

    if message.mention_everyone:
        qq_message += qq_MessageSegment.mention_everyone()

    if attachments := message.attachments:
        for attachment in attachments:
            if attachment.content_type is not UNSET and re.match(
                r"image/(gif|jpeg|png|webp)", attachment.content_type, 0
            ):
                img_list.append(attachment.url)
            else:
                pass
    return qq_message, img_list


async def create_dc_to_qq(
    bot: dc_Bot, event: dc_MessageCreateEvent, qq_bot: qq_Bot, link: LinkWithWebhook
):
    """discord 消息转发到 QQ"""
    logger.debug("into create_dc_to_qq()")
    message, img_list = await build_qq_message(bot, event)
    if img_list:
        get_img_tasks = [get_qq_img(img, discord_proxy) for img in img_list]
        img_data_list = await asyncio.gather(*get_img_tasks)
    else:
        img_data_list = ["0"]

    async with get_session() as session:
        if (
            event.referenced_message is not UNSET
            and event.referenced_message is not None
            and (
                reference := await session.scalar(
                    select(MsgID.qqid)
                    .filter(MsgID.dcid == event.referenced_message.id)
                    .limit(1)
                )
            )
        ):
            message += qq_MessageSegment.reference(reference)

    sends: list = []
    for i, img_data in enumerate(img_data_list):
        try_times = 1
        while True:
            try:
                if isinstance(img_data, io.BytesIO):
                    message = (
                        message.append(qq_MessageSegment.file_image(img_data))
                        if i == 0
                        else qq_SegmentMessage(qq_MessageSegment.file_image(img_data))
                    )
                sends.append(await qq_bot.send_to_channel(link.qq_channel_id, message))
                break
            except AuditException as e:
                try_times = 1
                while True:
                    if id := (await e.get_audit_result()).message_id:
                        sends.append(id)
                        break
                    else:
                        logger.warning(f"get_audit_result retry {try_times}")
                        if try_times >= 5:
                            logger.warning(
                                "message audit fail: "
                                + f"[audit_id:{e.audit_id}, dc_message_id: {event.id}]"
                            )
                            return
                        try_times += 1
                        await asyncio.sleep(1)
                break
            except NameError as e:
                logger.warning(f"create_dc_to_qq() error {e}, retry {try_times}")
                if try_times >= 3:
                    raise e
                try_times += 1
                await asyncio.sleep(5)

    async with get_session() as session:
        for send in sends:
            session.add(
                MsgID(
                    dcid=event.id,
                    qqid=send.id if isinstance(send, qq_Message) else send,
                )
            )
        await session.commit()
    logger.debug("finish create_dc_to_qq()")


async def delete_dc_to_qq(
    event: dc_MessageDeleteEvent,
    qq_bot: qq_Bot,
    link: LinkWithWebhook,
    just_delete: list,
):
    logger.debug("into delete_dc_to_qq()")
    if (id := event.id) in just_delete:
        just_delete.remove(id)
        return
    try_times = 1
    while True:
        try:
            async with get_session() as session:
                if msgids := await session.scalars(
                    select(MsgID).filter(MsgID.dcid == event.id)
                ):
                    for msgid in msgids:
                        await qq_bot.delete_message(
                            message_id=msgid.qqid, channel_id=link.qq_channel_id
                        )
                        just_delete.append(msgid.qqid)
                        await session.delete(msgid)
                    await session.commit()
            logger.debug("finish delete_dc_to_qq()")
            break
        except (UnboundLocalError, TypeError, NameError) as e:
            logger.warning(f"delete_dc_to_qq() error: {e}, retry {try_times}")
            if try_times == 3:
                raise e
            try_times += 1
            await asyncio.sleep(5)
