"""Prompt assembly for the stateless group-chat turn."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qqagent.config import Config

from .events import MessageEvent

TOOL_INSTRUCTIONS = """\
你可以使用 send_message 工具发言：
- 调用一次发送一条消息，可以连续调用多次。
- 不调用任何工具就表示这次保持沉默。
- send_message 只对当前会话生效，不能发到别的群或私聊。
- text 是消息正文；reply_to 可选，填要引用的消息 id。
"""


class PromptBuilder:
    def __init__(self, config: Config) -> None:
        self.config = config
        try:
            self.tz = ZoneInfo(config.agent.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            self.tz = ZoneInfo("UTC")

    def _system_prompt(self, event: MessageEvent, fragments: list[str]) -> str:
        persona = self.config.persona
        system = persona.system_prompt.replace("{name}", persona.name)
        if event.chat_type == "group":
            group_name = str(event.raw.get("group_name") or "")
            label = f"{group_name}（群号 {event.group_id}）" if group_name else str(event.group_id)
            system += f"\n\n当前群：{label}"
        else:
            system += f"\n\n当前正在和 {event.nickname} 私聊。"
        if fragments:
            system += "\n\n扩展提供的补充说明：\n" + "\n".join(
                f"- {fragment}" for fragment in fragments if str(fragment).strip()
            )
        system += "\n\n" + TOOL_INSTRUCTIONS.strip()
        return system

    def build(
        self, event: MessageEvent, context_text: str, fragments: list[str] | None = None
    ) -> list[dict[str, str]]:
        now = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M")
        context = context_text.strip() or "（暂无聊天记录）"
        user = (
            "以下是最近的聊天记录（按时间顺序）：\n"
            f"{context}\n\n"
            f"当前时间：{now}。\n"
            "请判断你是否需要发言。需要发言就调用 send_message；"
            "不需要发言就不要调用任何工具。不要重复别人刚说过的话。"
        )
        return [
            {"role": "system", "content": self._system_prompt(event, fragments or [])},
            {"role": "user", "content": user},
        ]
