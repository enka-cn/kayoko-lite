# Extensions (M1)

An extension is a directory with:

```
my-extension/
├── extension.toml
└── __init__.py        # setup(api)
```

Multiple extensions run in **one shared host process**. They are treated as
mutually trusted; see [SECURITY.md](../SECURITY.md).

## Manifest

```toml
[extension]
id = "my_extension"       # letters, digits, '_' or '-'
version = "0.1.0"
api_version = 1
kind = "plugin"           # plugin | skill (documentation only)
enabled = true
priority = 100
description = "..."

[permissions]
onebot = ["get_group_info"]   # read-only actions only
message_send = false          # allow message.send_text capability
storage = true                # allow extension_kv storage
network = []                  # reserved; not available in M1
llm = false                   # reserved; not available in M1
scheduler = false             # reserved; not available in M1

[config]
# arbitrary values, exposed as api.config
```

## Entrypoint

```python
from qqagent.sdk import ExtensionAPI, ToolContext


def setup(api: ExtensionAPI) -> None:
    api.prompts.add("你可以在需要时调用 my_extension__echo。", priority=50)

    @api.hooks.on("message.received", priority=100)
    async def on_message(payload):
        # Return payload to continue, modify it to rewrite the event,
        # or return None to drop the message.
        return payload

    @api.tools.register(
        name="echo",
        description="Return the supplied text.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    async def echo(ctx: ToolContext, args):
        await ctx.storage.set("last", args["text"])
        return f"echo: {args['text']}"
```

The core exposes the tool as `my_extension__echo`. Tool names are namespaced;
duplicate public names are rejected.

## Hooks

| Hook | Payload | Return |
|---|---|---|
| `message.received` | message dict | dict to continue/modify, `None` to drop |
| `notice.received` | notice dict | ignored by M1 core |
| `trigger.should_respond` | `{event, triggered, reason}` | `{"triggered": true}` to force a wake |
| `prompt.build` | `{event, fragments}` | dict with an updated `fragments` list |

Hook order is `priority` ascending, then extension id. A hook returning `None`
stops the chain for that hook.

## Capabilities

`ctx.capabilities.call(name, arguments)` / `ctx.call(...)`:

| Capability | Permission | Arguments |
|---|---|---|
| `storage.get/set/delete` | `storage = true` | `{key, value?}` |
| `message.send_text` | `message_send = true` | `{chat_id, text, reply_to?}` |
| `onebot.call` | action in `permissions.onebot` | `{action, params}`; read-only actions only |

`onebot.call` is restricted to `get_login_info`, `get_group_info`,
`get_group_member_info`, `get_group_member_list`, `get_group_list`,
`get_friend_list`, `get_msg`, and `get_forward_msg`.

## Storage

`ctx.storage` is a per-extension key/value store backed by SQLite:

```python
await ctx.storage.set("key", "value")
await ctx.storage.get("key", "")
await ctx.storage.delete("key")
```

Values are strings. Serialize structured data yourself.

## Failure semantics

- An exception in a hook is logged and the hook chain continues with the
  previous payload.
- An exception in a tool is returned to the model as a tool error.
- A hung hook/tool hits the core timeout. With
  `hard_timeout_kill_host: true`, the whole host is killed and restarted.
- Repeated host crashes trip the circuit breaker and disable extensions until
  the process is restarted.
