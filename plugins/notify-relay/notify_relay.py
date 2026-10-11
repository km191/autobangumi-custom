"""通知中继：一个由我们自己控制的 Telegram 渠道。

为什么要它：宿主的「重命名完成」单集通知在**重命名时**就发（只有剧名/季/集 + 海报），
和我们的「入库通知」重复且更吵；而宿主没有"只发事件、不发单集"的开关
（通知配置只有总开关 ``enable`` 和渠道 ``enabled``）。

做法：把宿主自带的 telegram 渠道关掉，换成这个插件渠道，然后：

* ``kind == "episode"``（重命名完成）→ **直接丢弃**，不发送（用我们的入库通知代替）；
* ``kind == "event"``（洗版、下载失败、RSS 失败、偏移复核…）→ 照常转发，
  文案就是宿主渲染好的 ``title`` + ``body``（所以洗版通知的排版保持插件里那套）。

凭据放在插件自己的配置里（宿主不会把渠道条目的 token 传给插件）。
"""

from __future__ import annotations

import html
import logging

import httpx
from pydantic import BaseModel, Field

from ab_sdk import Plugin, PluginContext, provider, points
from ab_sdk.notify import (
    NotificationMessage,
    Notifier,
    NotifierFactory,
    NotifierSettings,
)

logger = logging.getLogger(__name__)

PLUGIN_ID = "notify-relay"

# httpx 会把请求 URL 打进 INFO 日志，而 bot token 就在 URL 里 —— 压掉
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class Options(BaseModel):
    telegram_token: str = Field("", title="Telegram Bot Token")
    telegram_chat_id: str = Field("", title="Telegram Chat ID")
    telegram_api: str = Field("https://api.telegram.org", title="Telegram API 基址（测试用）")
    drop_episode: bool = Field(
        True,
        title="丢弃「重命名完成」单集通知",
        description="开启后单集通知不再推送（由入库通知代替）；关掉则恢复成普通渠道。",
    )
    prepend_title: bool = Field(
        True, title="正文前带事件标题", description="例如「洗版完成」另起一行再跟正文。"
    )
    timeout: float = Field(30.0, ge=1, title="请求超时（秒）")


def compose(title: str, body: str, options: Options) -> str:
    """拼一条纯文本消息（Telegram HTML 模式，需转义）。"""
    parts: list[str] = []
    if options.prepend_title and title:
        parts.append(f"<b>{html.escape(title, quote=False)}</b>")
    if body:
        parts.append(html.escape(body, quote=False))
    return "\n".join(parts)


class TelegramRelay(Notifier):
    def __init__(self, settings: NotifierSettings, options: Options) -> None:
        self.settings = settings
        self.options = options
        self.dropped = 0
        self.sent = 0

    async def _post(self, method: str, data: dict) -> bool:
        url = f"{self.options.telegram_api.rstrip('/')}/bot{self.options.telegram_token}/{method}"
        async with httpx.AsyncClient(timeout=self.options.timeout) as client:
            response = await client.post(url, data=data)
        return response.status_code == 200 and response.json().get("ok", False)

    async def send(self, message: NotificationMessage) -> bool:
        # 单集（重命名完成）默认丢弃：我们用自己的入库通知代替
        if message.kind == "episode" and self.options.drop_episode:
            self.dropped += 1
            if self.dropped == 1:
                # 只报一次，作为"确实在按规则丢弃"的证据；之后走 debug
                logger.info(
                    "[%s] 开始丢弃「重命名完成」单集通知（例如 %s），改用入库通知",
                    PLUGIN_ID,
                    message.title,
                )
            else:
                logger.debug("[%s] 丢弃单集通知：%s", PLUGIN_ID, message.title)
            return True
        text = compose(message.title, message.body, self.options)
        if not text.strip():
            return True
        if not (self.options.telegram_token and self.options.telegram_chat_id):
            logger.info("[%s] 未配置 Telegram，消息只留在站内：%s", PLUGIN_ID, message.title)
            return True
        ok = await self._post(
            "sendMessage",
            {
                "chat_id": self.options.telegram_chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        if not ok:
            logger.warning("[%s] 发送失败：%s", PLUGIN_ID, message.title)
        return ok

    async def test(self) -> tuple[bool, str]:
        if not (self.options.telegram_token and self.options.telegram_chat_id):
            return False, "未填写 Telegram Bot Token 或 Chat ID"
        ok = await self._post(
            "sendMessage",
            {"chat_id": self.options.telegram_chat_id, "text": "通知中继测试消息 ✓"},
        )
        return (True, "已发送测试消息") if ok else (False, "发送失败，请检查 Token 与 Chat ID")


class NotifyRelay(Plugin[Options]):
    config_model = Options

    async def setup(self) -> None:
        logger.info(
            "[%s] 已加载：%s ｜ 单集通知 %s ｜ Telegram %s",
            PLUGIN_ID,
            self.config.telegram_api,
            "丢弃" if self.config.drop_episode else "转发",
            "已配置" if self.config.telegram_token else "未配置",
        )

    @provider(points.NOTIFIER, id="relay")
    def relay(self) -> NotifierFactory:
        options = self.config

        def factory(settings: NotifierSettings) -> Notifier:
            return TelegramRelay(settings, options)

        return factory
