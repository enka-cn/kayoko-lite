"""Example extension used to exercise the M1 extension contract."""

from __future__ import annotations

import os

from qqagent.sdk import ExtensionAPI, ToolContext


def setup(api: ExtensionAPI) -> None:
    api.log.info("example extension loading (config=%s)", api.config)
    api.prompts.add(
        "示例扩展已加载。你可以调用 example__echo 测试扩展工具，"
        "或调用 example__last_echo 读取上次写入的扩展存储。",
        priority=50,
    )

    @api.hooks.on("message.received", priority=100)
    async def on_message(payload):
        api.log.debug(
            "example saw message %s from %s",
            payload.get("message_id"),
            payload.get("user_id"),
        )
        return payload

    @api.tools.register(
        name="echo",
        description="返回传入的文本，并把文本写入扩展存储。用于测试扩展工具链路。",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要回显的文本"}
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    async def echo(ctx: ToolContext, args):
        await ctx.storage.set("last_echo", args["text"])
        return f"echo: {args['text']}"

    @api.tools.register(
        name="last_echo",
        description="读取 example__echo 最近一次写入的文本。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def last_echo(ctx: ToolContext, args):
        value = await ctx.storage.get("last_echo", "")
        return value or "(empty)"

    @api.tools.register(
        name="env_probe",
        description="返回 host 进程可见的环境变量名。用于验证密钥没有传入 host。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def env_probe(ctx: ToolContext, args):
        return {"keys": sorted(os.environ)}
