from typing import Optional

from pydantic import BaseModel
from nonebot import get_plugin_config


class Link(BaseModel):
    qq_guild_id: str
    dc_guild_id: int
    qq_channel_id: str
    dc_channel_id: int


class LinkWithoutWebhook(Link):
    webhook_id: Optional[int] = None
    webhook_token: Optional[str] = None


class LinkWithWebhook(Link):
    webhook_id: int
    webhook_token: str


class Config(BaseModel):
    dcqg_relay_channel_links: list[LinkWithoutWebhook] = []
    """子频道绑定"""
    dcqg_relay_unmatch_beginning: list[str] = ["/"]
    """不转发的消息开头"""
    discord_proxy: Optional[str] = None


plugin_config = get_plugin_config(Config)
