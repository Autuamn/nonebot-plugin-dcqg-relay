import asyncio
import re
from typing import Optional, Union

import aiohttp
from nonebot import logger
from nonebot.adapters.discord import (
    Bot as dc_Bot,
    MessageCreateEvent as dc_MessageCreateEvent,
    MessageDeleteEvent as dc_MessageDeleteEvent,
)
from nonebot.adapters.discord.api import UNSET, Missing, Webhook
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
    logger.debug("check_messages")
    if isinstance(event, dc_MessageCreateEvent):
        return not (
            re.match(r".*? \[ID:\d*?\]$", event.author.username)
            and event.author.bot is True
        )
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
    if isinstance(event, qq_GuildMessageEvent):
        return next(
            (
                link
                for link in with_webhook_links
                if link.qq_channel_id == event.channel_id
            ),
            None,
        )
    elif isinstance(event, dc_MessageCreateEvent):
        if not (
            re.match(r".*? \[ID:\d*?\]$", event.author.username)
            and event.author.bot is True
        ):
            return next(
                (
                    link
                    for link in with_webhook_links
                    if link.dc_channel_id == event.channel_id
                ),
                None,
            )
    elif isinstance(event, qq_MessageDeleteEvent):
        return next(
            (
                link
                for link in with_webhook_links
                if link.qq_channel_id == event.message.channel_id
            ),
            None,
        )
    elif isinstance(event, dc_MessageDeleteEvent):
        return next(
            (
                link
                for link in with_webhook_links
                if link.dc_channel_id == event.channel_id
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
) -> Optional[LinkWithWebhook]:
    if link.webhook_id and link.webhook_token:
        return LinkWithWebhook(**link.model_dump())
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
        if bot_webhook:
            return await build_link(link, bot_webhook)
    except Exception as e:
        logger.error(
            f"get webhook error, Discord channel id: {link.dc_channel_id}, error: {e}"
        )
    try:
        create_webhook = await bot.create_webhook(
            channel_id=link.dc_channel_id, name=str(link.dc_channel_id)
        )
        return await build_link(link, create_webhook)
    except Exception as e:
        logger.error(
            f"create webhook error, Discord channel id: {link.dc_channel_id}, "
            + f"error: {e}"
        )
    logger.error(
        f"failed to get or create webhook, Discord channel id: {link.dc_channel_id}"
    )


async def build_link(
    link: LinkWithoutWebhook, webhook: Webhook
) -> Optional[LinkWithWebhook]:
    if webhook and webhook.token:
        return LinkWithWebhook(
            webhook_id=webhook.id,
            webhook_token=webhook.token,
            **link.model_dump(exclude_none=True),
        )


async def get_webhooks(bot: dc_Bot):
    global with_webhook_links
    task = [get_webhook(bot, link) for link in without_webhook_links]
    webhooks = await asyncio.gather(*task)
    with_webhook_links.extend(link for link in webhooks if link)
