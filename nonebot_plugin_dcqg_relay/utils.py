import asyncio
import re
from typing import Optional, Union

import aiohttp
from nonebot import logger
from nonebot.compat import model_dump
from nonebot.adapters.discord import (
    Bot as dc_Bot,
    MessageCreateEvent as dc_MessageCreateEvent,
    MessageDeleteEvent as dc_MessageDeleteEvent,
)
from nonebot.adapters.discord.api import UNSET, Missing
from nonebot.adapters.discord.exception import ActionFailed
from nonebot.adapters.qq import (
    Bot as qq_Bot,
    GuildMessageEvent as qq_GuildMessageEvent,
    MessageDeleteEvent as qq_MessageDeleteEvent,
)

from .config import LinkWithWebhook, LinkWithoutWebhook, plugin_config


without_webhook_links: list[LinkWithoutWebhook] = plugin_config.dcqg_relay_channel_links
with_webhook_links: list[LinkWithWebhook] = []
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
    logger.debug("checked event type")
    if isinstance(event, dc_MessageCreateEvent):
        if not (
            re.match(r".*?\[ID:\d*?\]$", event.author.username)
            and event.author.bot is True
        ):
            return True
        logger.debug("is self relay message")
        return False
    return True


async def get_link(
    bot: Union[qq_Bot, dc_Bot],
    event: Union[
        qq_GuildMessageEvent,
        dc_MessageCreateEvent,
        qq_MessageDeleteEvent,
        dc_MessageDeleteEvent,
    ],
) -> Optional[LinkWithWebhook]:
    """获取 link"""
    logger.debug("into get_link()")
    if isinstance(event, qq_MessageDeleteEvent):
        return await pick_link(event.message.channel_id)
    else:
        return await pick_link(event.channel_id)


async def pick_link(channel_id: Union[int, str]) -> Optional[LinkWithWebhook]:
    return next(
        (
            link
            for link in with_webhook_links
            if link.dc_channel_id == channel_id or link.qq_channel_id == channel_id
        ),
        None,
    )


async def get_dc_member_name(
    bot: dc_Bot, guild_id: Missing[int], user_id: int
) -> tuple[str, str]:
    try:
        if guild_id is not UNSET:
            member = await bot.get_guild_member(guild_id=guild_id, user_id=user_id)
            if (nick := member.nick) and nick is not UNSET:
                return nick, member.user.username if member.user is not UNSET else ""
            elif member.user is not UNSET and (global_name := member.user.global_name):
                return global_name, member.user.username
            else:
                return "", str(user_id)
        else:
            user = await bot.get_user(user_id=user_id)
            return user.global_name or "", user.username
    except ActionFailed as e:
        if e.message == "Unknown User":
            return "(error:未知用户)", str(user_id)
        else:
            raise e


async def get_file_bytes(url: str, proxy: Optional[str] = None) -> bytes:
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, proxy=proxy) as response,
    ):
        return await response.read()


async def get_webhook(
    bot: dc_Bot, link: LinkWithoutWebhook
) -> Union[LinkWithWebhook, int]:
    if link.webhook_id and link.webhook_token:
        return LinkWithWebhook(**model_dump(link))
    try:
        channel_webhooks = await bot.get_channel_webhooks(channel_id=link.dc_channel_id)
        bot_webhook = next(
            (
                webhook
                for webhook in channel_webhooks
                if webhook.application_id == int(bot.self_id)
            ),
            None,
        )
        if bot_webhook and bot_webhook.token:
            return await build_link(link, bot_webhook.id, bot_webhook.token)
    except Exception as e:
        logger.error(
            f"get webhook error, Discord channel id: {link.dc_channel_id}, error: {e}"
        )
    try:
        create_webhook = await bot.create_webhook(
            channel_id=link.dc_channel_id, name=str(link.dc_channel_id)
        )
        if create_webhook.token:
            return await build_link(link, create_webhook.id, create_webhook.token)
    except Exception as e:
        logger.error(
            f"create webhook error, Discord channel id: {link.dc_channel_id}, "
            + f"error: {e}"
        )
    logger.error(
        f"failed to get or create webhook, Discord channel id: {link.dc_channel_id}"
    )
    return link.dc_channel_id


async def build_link(
    link: LinkWithoutWebhook, webhook_id: int, webhook_token: str
) -> LinkWithWebhook:
    return LinkWithWebhook(
        webhook_id=webhook_id,
        webhook_token=webhook_token,
        **model_dump(link, exclude={"webhook_id", "webhook_token"}),
    )


async def get_webhooks(bot: dc_Bot) -> list[int]:
    global with_webhook_links
    task = [get_webhook(bot, link) for link in without_webhook_links]
    links = await asyncio.gather(*task)
    with_webhook_links.extend(
        link for link in links if isinstance(link, LinkWithWebhook)
    )
    return [link for link in links if isinstance(link, int)]
