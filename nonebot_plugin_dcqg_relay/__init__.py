import re
import sqlite3
from typing import Union

from nonebot.plugin import PluginMetadata
from nonebot.adapters.qq import Bot as qq_Bot
from nonebot.adapters.discord import Bot as dc_Bot
from nonebot import on, logger, require, on_regex, get_driver, on_command
from nonebot.adapters.qq import GuildMessageEvent as qq_GuildMessageEvent
from nonebot.adapters.qq import MessageDeleteEvent as qq_MessageDeleteEvent
from nonebot.adapters.discord import MessageCreateEvent as dc_MessageCreateEvent
from nonebot.adapters.discord import MessageDeleteEvent as dc_MessageDeleteEvent

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store

from .init_db import init_db
from .config import Link, Config, plugin_config
from .utils import (
    check_messages,
    create_dc_to_qq,
    create_qq_to_dc,
    delete_dc_to_qq,
    delete_qq_to_dc,
)

__plugin_meta__ = PluginMetadata(
    name="sync_message_to_discord",
    description="",
    usage="",
    config=Config,
)


driver = get_driver()
channel_links: list[Link] = plugin_config.smd_channel_links
discord_proxy = plugin_config.discord_proxy
unmatch_beginning = plugin_config.smd_unmatch_beginning

cache_dir = store.get_cache_dir("sync_message_to_discord")
data_dir = store.get_data_dir("sync_message_to_discord")
database_file = store.get_data_file("sync_message_to_discord", "msgid.db")
config_file = store.get_config_file("sync_message_to_discord", "webhook.json")

commit_db_m = on_command("commit_db", priority=0, block=True)
unmatcher = on_regex(
    rf'\A *?[{re.escape("".join(unmatch_beginning))}].*', priority=1, block=True
)
matcher = on(rule=check_messages, priority=2, block=False)


@driver.on_bot_connect
async def get_dc_bot(bot: dc_Bot):
    global dc_bot
    dc_bot = bot


@driver.on_bot_connect
async def get_qq_bot(bot: qq_Bot):
    global qq_bot
    qq_bot = bot


@driver.on_startup
async def connect_db():
    global conn
    conn = (
        (await init_db(database_file))
        if not database_file.exists()
        else sqlite3.connect(database_file)
    )


@driver.on_shutdown
async def close_db():
    conn.commit()
    conn.close()


@scheduler.scheduled_job("cron", minute="*/30", id="commit_db")
@commit_db_m.handle()
async def commit_db():
    await close_db()
    await connect_db()
    await commit_db_m.finish("commit!")


@unmatcher.handle()
async def unmatcher_pass():
    pass


@matcher.handle()
async def create_message(
    bot: Union[qq_Bot, dc_Bot],
    event: Union[qq_GuildMessageEvent, dc_MessageCreateEvent],
):
    logger.debug("create_handle")
    if isinstance(bot, qq_Bot) and isinstance(event, qq_GuildMessageEvent):
        await create_qq_to_dc(bot, event, dc_bot, conn)
    elif isinstance(bot, dc_Bot) and isinstance(event, dc_MessageCreateEvent):
        await create_dc_to_qq(bot, event, qq_bot, conn)
    else:
        logger.error("bot type and event type not match")


@matcher.handle()
async def delete_message(
    bot: Union[qq_Bot, dc_Bot],
    event: Union[qq_MessageDeleteEvent, dc_MessageDeleteEvent],
):
    if isinstance(bot, qq_Bot) and isinstance(event, qq_MessageDeleteEvent):
        await delete_qq_to_dc(event, dc_bot, conn)
    elif isinstance(bot, dc_Bot) and isinstance(event, dc_MessageDeleteEvent):
        await delete_dc_to_qq(event, qq_bot, conn)
    else:
        logger.error("bot type and event type not match")
