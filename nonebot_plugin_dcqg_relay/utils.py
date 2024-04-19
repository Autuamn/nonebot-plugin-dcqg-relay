from pathlib import Path
import re
import sqlite3
from typing import Optional, Union

import aiohttp
from nonebot import logger
from nonebot.adapters.discord import (
    Bot as dc_Bot,
    MessageCreateEvent as dc_MessageCreateEvent,
    MessageDeleteEvent as dc_MessageDeleteEvent,
)
from nonebot.adapters.discord.api import UNSET
from nonebot.adapters.discord.exception import ActionFailed
from nonebot.adapters.qq import (
    Bot as qq_Bot,
    GuildMessageEvent as qq_GuildMessageEvent,
    MessageDeleteEvent as qq_MessageDeleteEvent,
)

from .config import Link, plugin_config

channel_links: list[Link] = plugin_config.smd_channel_links
discord_proxy = plugin_config.discord_proxy


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
            event.message.guild_id == link.qq_guild_id
            and event.message.channel_id == link.qq_channel_id
            for link in channel_links
        )
    elif isinstance(event, dc_MessageDeleteEvent):
        return any(
            event.guild_id == link.dc_guild_id
            and event.channel_id == link.dc_channel_id
            for link in channel_links
        )


async def init_db(dbpath: Path):
    conn = sqlite3.connect(dbpath)
    conn.execute(
        """CREATE TABLE ID (
            DCID    INT     NOT NULL,
            QQID    TEXT    NOT NULL
        );"""
    )
    return conn


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


async def get_file_bytes(url: str, proxy: Optional[str] = None) -> bytes:
    async with aiohttp.ClientSession().get(url, proxy=proxy) as response:
        return await response.read()
