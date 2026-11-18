"""Current AstrBot FunctionTool API; music only, no dependency mutations."""

from typing import Any

from astrbot.api import FunctionTool, logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from .service import UserFacingError


@dataclass
class YouTubeMusicTool(FunctionTool[AstrAgentContext]):
    name: str = "youtube_music"
    description: str = (
        "仅在用户明确请求音乐时搜索、按确认编号或单视频链接入队、查看和控制当前 QQ 会话队列。"
        "搜索后让用户确认候选，不猜测编号；暂停和下一首需要队列管理员权限。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "search",
                        "enqueue",
                        "queue_status",
                        "resume",
                        "pause",
                        "next",
                        "cancel",
                    ],
                },
                "query": {
                    "type": "string",
                    "maxLength": 2048,
                    "description": "搜索词或单个 YouTube 视频链接。",
                },
                "index": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "用户确认的当前搜索候选编号。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }
    )
    plugin: Any = Field(default=None, exclude=True, repr=False)

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        event = context.context.event
        if event is None:
            return "无法确认当前会话，未执行操作。"
        index = kwargs.get("index", 0)
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 10:
            return "候选编号无效。"
        if not isinstance(kwargs.get("action", ""), str) or not isinstance(
            kwargs.get("query", ""), str
        ):
            return "音乐参数无效。"
        try:
            return await self.plugin.ai_music_action(
                event, kwargs.get("action", ""), kwargs.get("query", ""), index
            )
        except UserFacingError as exc:
            return str(exc)
        except Exception as exc:
            logger.error("[YouTubeMusic] 音乐 Tool 失败：%s", type(exc).__name__)
            return "音乐操作未完成，请检查插件状态。"
