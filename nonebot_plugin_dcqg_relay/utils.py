import io
import re
import asyncio
from sqlite3 import Connection
from typing import Union, Optional

import aiohttp
import filetype
from PIL import Image
from nonebot import logger
from nonebot.adapters.qq import Bot as qq_Bot
from nonebot.adapters.discord import Bot as dc_Bot
from nonebot.adapters.qq.models import MessageReference
from nonebot.adapters.qq.exception import AuditException
from nonebot.adapters.qq import Message as qq_SegmentMessage
from nonebot.adapters.qq.models import Message as qq_Message
from nonebot.adapters.qq import MessageSegment as qq_MessageSegment
from nonebot.adapters.discord.exception import ActionFailed, NetworkError
from nonebot.adapters.qq import GuildMessageEvent as qq_GuildMessageEvent
from nonebot.adapters.qq import MessageDeleteEvent as qq_MessageDeleteEvent
from nonebot.adapters.discord import MessageCreateEvent as dc_MessageCreateEvent
from nonebot.adapters.discord import MessageDeleteEvent as dc_MessageDeleteEvent
from nonebot.adapters.discord.api import UNSET, File, Embed, MessageGet, EmbedAuthor

from .config import Link, plugin_config
from .qq_emoji_dict import qq_emoji_dict

channel_links: list[Link] = plugin_config.smd_channel_links
discord_proxy = plugin_config.discord_proxy
just_delete = []


async def check_messages(
    bot: Union[qq_Bot, dc_Bot],
    event: Union[
        qq_GuildMessageEvent,
        dc_MessageCreateEvent,
        qq_MessageDeleteEvent,
        dc_MessageDeleteEvent,
    ],
) -> bool:
    """检查消息"""
    logger.debug("check_messages")
    if isinstance(event, qq_GuildMessageEvent):
        return any(
            event.guild_id == link.qq_guild_id
            and event.channel_id == link.qq_channel_id
            for link in channel_links
        )
    elif isinstance(event, dc_MessageCreateEvent):
        if not (
            re.match(r".*? \[ID:\d*?\]$", event.author.username)
            and event.author.bot is True
        ):
            return any(
                event.guild_id == link.dc_guild_id
                and event.channel_id == link.dc_channel_id
                for link in channel_links
            )
        else:
            return False
    elif isinstance(event, qq_MessageDeleteEvent):
        return any(
            check_just_delete(event)
            and event.message.guild_id == link.qq_guild_id
            and event.message.channel_id == link.qq_channel_id
            for link in channel_links
        )
    elif isinstance(event, dc_MessageDeleteEvent):
        return any(
            check_just_delete(event)
            and event.guild_id == link.dc_guild_id
            and event.channel_id == link.dc_channel_id
            for link in channel_links
        )


def check_just_delete(
    event: Union[qq_MessageDeleteEvent, dc_MessageDeleteEvent],
) -> bool:
    id = event.message.id if isinstance(event, qq_MessageDeleteEvent) else event.id
    if id in just_delete:
        just_delete.remove(id)
        return False
    return True


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


async def get_qq_member_name(bot: qq_Bot, guild_id: str, user_id: str) -> str:
    member = await bot.get_member(guild_id=guild_id, user_id=user_id)
    return member.nick or (member.user.username if member.user else "") or ""


async def get_dc_member_name(bot: dc_Bot, guild_id: int, user_id: int) -> str:
    try:
        member = await bot.get_guild_member(guild_id=guild_id, user_id=user_id)
        if (nick := member.nick) and nick is not UNSET:
            logger.trace(f"nick: {nick}")
            return nick
        elif member.user is not UNSET and (global_name := member.user.global_name):
            logger.trace(f"global_name: {global_name}")
            return global_name
        else:
            return ""
    except ActionFailed as e:
        if e.message == "Unknown User":
            return f"({user_id})"
        else:
            raise e


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


async def get_dc_role_name(bot: dc_Bot, guild_id: int, role_id: int) -> str:
    roles = await bot.get_guild_roles(guild_id=guild_id)
    role = next(role for role in roles if role.id == role_id)
    return role.name


async def get_dc_channel_name(bot: dc_Bot, guild_id: int, channel_id: int) -> str:
    channels = await bot.get_guild_channels(guild_id=guild_id)
    channel = next(channel for channel in channels if channel.id == channel_id)
    return (
        channel.name
        if channel.name is not UNSET and channel.name is not None
        else "(error:无名频道)"
    )


async def get_dc_file(url: str) -> File:
    """获取图片文件，用于发送到 Discord"""
    img_bytes = await get_file_bytes(url)
    match = filetype.match(img_bytes)
    kind = match.extension if match else "dat"
    return File(content=img_bytes, filename=f"{url.split('/')[-1]!s}.{kind}")


async def get_file_bytes(url: str, proxy: Optional[str] = None) -> bytes:
    async with aiohttp.ClientSession().get(url, proxy=proxy) as response:
        return await response.read()


async def get_qq_message(
    bot: qq_Bot, event: qq_GuildMessageEvent
) -> tuple[str, list[str]]:
    """获取 QQ 消息，用于发送到 discord"""
    logger.debug("get_qq_message")
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
    logger.debug("got")
    return text, img_list


async def get_dc_message(
    bot: dc_Bot, event: dc_MessageCreateEvent
) -> tuple[qq_SegmentMessage, list[str]]:
    qq_message = qq_SegmentMessage(
        qq_MessageSegment.text(
            (
                await get_dc_member_name(bot, event.guild_id, event.author.id)
                if event.guild_id is not UNSET
                else ""
            )
            + f"(@{event.author.username}):\n"
        )
    )
    img_list: list[str] = []
    msg = (
        event.content
        if event.content
        else (
            await bot.get_channel_message(
                channel_id=event.channel_id, message_id=event.message_id
            )
        ).content
    )
    text_begin = 0
    for embed in re.finditer(
        r"<(?P<type>(@!|@&|@|#|/|:|a:|t:))(?P<param>.+?)>",
        msg,
    ):
        if content := msg[text_begin : embed.pos + embed.start()]:
            qq_message += qq_MessageSegment.text(content)
        text_begin = embed.pos + embed.end()
        if embed.group("type") in ("@!", "@"):
            try:
                qq_message += qq_MessageSegment.text(
                    "@"
                    + (await bot.get_user(user_id=int(embed.group("param")))).username
                )
            except ActionFailed as e:
                if e.message == "Unknown User":
                    qq_message += qq_MessageSegment.text(f'@({embed.group("param")})')
                else:
                    raise e
        elif embed.group("type") == "@&":
            qq_message += qq_MessageSegment.text(
                "@"
                + (
                    await get_dc_role_name(
                        bot, event.guild_id, int(embed.group("param"))
                    )
                    if event.guild_id is not UNSET
                    else "(error:未知用户组)"
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
                    else "(error:未知频道)"
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
                qq_message += qq_MessageSegment.text(f'<t:{embed.group("param")}>')
            else:
                qq_message += qq_MessageSegment.text(embed.group())
    if content := msg[text_begin:]:
        qq_message += qq_MessageSegment.text(content)

    if event.mention_everyone:
        qq_message += qq_MessageSegment.mention_everyone()
    if attachments := event.attachments:
        for attachment in attachments:
            if attachment.content_type is not UNSET and re.match(
                r"image/(gif|jpeg|png|webp)", attachment.content_type, 0
            ):
                img_list.append(attachment.url)
            else:
                pass
    return qq_message, img_list


async def get_embeds(
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
        get_img_tasks = [get_dc_file(img) for img in img_list]
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

    text, img_list = await get_qq_message(bot, event)
    link = next(
        link for link in channel_links if link.qq_channel_id == event.channel_id
    )

    if (reply := event.reply) and (reference := event.message_reference):
        embeds = await get_embeds(bot, dc_bot, reference, reply, conn, link)
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


async def create_dc_to_qq(
    bot: dc_Bot, event: dc_MessageCreateEvent, qq_bot: qq_Bot, conn: Connection
):
    """discord 消息转发到 QQ"""
    event.get_message()
    message, img_list = await get_dc_message(bot, event)
    link = next(
        link for link in channel_links if link.dc_channel_id == event.channel_id
    )
    if img_list:
        get_img_tasks = [get_qq_img(img, discord_proxy) for img in img_list]
        img_data_list = await asyncio.gather(*get_img_tasks)
    else:
        img_data_list = ["0"]
    if (
        event.message_reference is not UNSET
        and (message_id := event.message_reference.message_id)
        and message_id is not UNSET
        and (
            db_select := conn.execute(
                f"SELECT QQID FROM ID WHERE DCID LIKE {message_id}"
            ).fetchone()
        )
        and (reference := db_select[0])
    ):
        message += qq_MessageSegment.reference(reference)

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
                send = await qq_bot.send_to_channel(link.qq_channel_id, message)
                break
            except AuditException as e:
                try_times = 1
                while True:
                    if send := (await e.get_audit_result()).message_id:
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
                logger.warning(f"retry {try_times}")
                if try_times >= 3:
                    raise e
                try_times += 1
                await asyncio.sleep(5)

        if send:
            conn.execute(
                "INSERT INTO ID (DCID, QQID) VALUES (?, ?)",
                (event.id, send.id if isinstance(send, qq_Message) else send),
            )


async def delete_qq_to_dc(
    event: qq_MessageDeleteEvent, dc_bot: dc_Bot, conn: Connection
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


async def delete_dc_to_qq(
    event: dc_MessageDeleteEvent, qq_bot: qq_Bot, conn: Connection
):
    if (id := event.id) in just_delete:
        just_delete.remove(id)
        return
    try_times = 1
    while True:
        try:
            db_selected = conn.execute(
                f'SELECT QQID FROM ID WHERE DCID LIKE ("%{event.id}%")'
            )
            for msgids in db_selected:
                for msgid in msgids:
                    channel_id = next(
                        link.qq_channel_id
                        for link in channel_links
                        if link.dc_channel_id == event.channel_id
                    )
                    await qq_bot.delete_message(message_id=msgid, channel_id=channel_id)
                    just_delete.append(msgid)
                    conn.execute(f'DELETE FROM ID WHERE QQID="{msgid}"')
            break
        except UnboundLocalError or TypeError or NameError as e:
            logger.warning(f"retry {try_times}")
            if try_times == 3:
                raise e
            try_times += 1
            await asyncio.sleep(5)
