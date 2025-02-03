import re
from typing import Union, Optional

from nonebot import get_driver, logger, on, on_regex, require
from nonebot.params import Depends
from nonebot.adapters.discord import (
    Bot as dc_Bot,
    MessageCreateEvent as dc_MessageCreateEvent,
    MessageDeleteEvent as dc_MessageDeleteEvent,
)
from nonebot.adapters.qq import (
    Bot as qq_Bot,
    GuildMessageEvent as qq_GuildMessageEvent,
    MessageDeleteEvent as qq_MessageDeleteEvent,
)
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_orm")

from .config import Config, LinkWithWebhook, plugin_config
from .dc_to_qq import create_dc_to_qq, delete_dc_to_qq
from .qq_to_dc import create_qq_to_dc, delete_qq_to_dc
from .utils import check_messages, get_link, get_webhooks

__plugin_meta__ = PluginMetadata(
    name="QQ频道-Discord 互通",
    description="在QQ频道与 Discord 之间同步消息的 nonebot2 插件",
    usage="",
    type="application",
    homepage="https://github.com/Autuamn/nonebot-plugin-dcqg-relay",
    config=Config,
    supported_adapters={"~qq", "~discord"},
)


driver = get_driver()

discord_proxy = plugin_config.discord_proxy
unmatch_beginning = plugin_config.dcqg_relay_unmatch_beginning

just_delete = []

unmatcher = on_regex(
    rf"\A *?[{re.escape(''.join(unmatch_beginning))}].*", priority=1, block=True
)
matcher = on(rule=check_messages, priority=2, block=False)


@driver.on_bot_connect
async def prepare_webhooks(bot: dc_Bot):
    logger.info("prepare webhooks: start")
    failed = await get_webhooks(bot)
    logger.info("prepare webhooks: done")
    if failed:
        logger.error(
            f"{len(failed)} channels failed to get or create webhook: {failed}"
        )


@driver.on_bot_connect
async def get_dc_bot(bot: dc_Bot):
    global dc_bot
    dc_bot = bot


@driver.on_bot_connect
async def get_qq_bot(bot: qq_Bot):
    global qq_bot
    qq_bot = bot


@unmatcher.handle()
async def unmatcher_pass():
    pass


@matcher.handle()
async def create_message(
    bot: Union[qq_Bot, dc_Bot],
    event: Union[qq_GuildMessageEvent, dc_MessageCreateEvent],
    link: Optional[LinkWithWebhook] = Depends(get_link),
):
    logger.debug("into create_message()")
    if link:
        if isinstance(bot, qq_Bot) and isinstance(event, qq_GuildMessageEvent):
            await create_qq_to_dc(bot, event, dc_bot, link)
        elif isinstance(bot, dc_Bot) and isinstance(event, dc_MessageCreateEvent):
            await create_dc_to_qq(bot, event, qq_bot, link)
        else:
            logger.error("bot type and event type not match")


@matcher.handle()
async def delete_message(
    bot: Union[qq_Bot, dc_Bot],
    event: Union[qq_MessageDeleteEvent, dc_MessageDeleteEvent],
    link: Optional[LinkWithWebhook] = Depends(get_link),
):
    logger.debug("into delete_message()")
    if link:
        if isinstance(bot, qq_Bot) and isinstance(event, qq_MessageDeleteEvent):
            await delete_qq_to_dc(event, dc_bot, link, just_delete)
        elif isinstance(bot, dc_Bot) and isinstance(event, dc_MessageDeleteEvent):
            await delete_dc_to_qq(event, qq_bot, link, just_delete)
        else:
            logger.error("bot type and event type not match")
