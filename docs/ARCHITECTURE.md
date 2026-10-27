# Architecture (M1)

## Process model

```
core process (trusted)
├── OneBotClient          forward WS events + HTTP actions
├── Store                 SQLite: chats, messages, extension KV
├── ContextBuilder        L1 window: recent messages + unread
├── PromptBuilder         persona + rules + fragments + context
├── AgentLoop             OpenAI-compatible tool loop
├── SendQueue             per-chat serial, rate limit, split, dedupe
├── ToolRegistry          send_message + extension tools
├── TriggerPolicy         mention / keyword / always / cooldown
└── ExtensionManager
     └── shared host process (third-party extensions)
          ├── SDK: hooks, tools, prompts, storage, capabilities
          └── JSON-RPC over length-prefixed stdio frames
```

The core owns all credentials and all OneBot actions. The host never sees the
OneBot tokens or the LLM API key.

## Main path

```
OneBot WS event
  → normalize_event
  → dedupe + store message
  → trigger policy
  → per-chat worker
      → context.build        (L1: recent + unread)
      → prompt.build         (extension hook may add fragments)
      → LLM tool loop
          → send_message     (core tool; silence is allowed)
          → extension tools  (JSON-RPC to host)
      → mark processed
      → repeat while sends happened and unread remain (max 4 turns)
  → SendQueue → OneBot HTTP
```

Messages that do not trigger are marked processed immediately, so they do not
cost tokens, but they remain in SQLite and can appear in a later context
window.

## Extension contract

- Manifest: `extension.toml` with `[extension]`, `[permissions]`, `[config]`.
- Entrypoint: `setup(api)` in `__init__.py`.
- Hooks: `message.received`, `notice.received`, `trigger.should_respond`,
  `prompt.build`.
- Tools: registered with a JSON Schema and exposed as
  `<extension_id>__<tool_name>`.
- Prompt fragments: static text included in the system prompt.
- Capabilities: `storage.*`, `message.send_text`, read-only `onebot.call`.
- Tool/hook timeouts, crash restart and a circuit breaker are enforced by the
  core; see `SECURITY.md` for the trust boundary.

## Memory roadmap

M1 implements only L1:

| Layer | Coverage | Update rate | Storage | Retrieval |
|---|---|---|---|---|
| L1 immediate | 100% raw messages | every message | `messages` | always, token-budgeted |
| L2 short-term | high | rolling summary every N messages/hours | future extension table | active chats |
| L3 long-term | low (salient facts) | daily/weekly consolidation | future extension table | on demand by user/topic |

L2/L3 will be an extension using `prompt.build` for injection and tools for
on-demand reads. The core does not need to change.

## Deliberate M1 trade-offs

- **OneBot v11, not Satori**: the QQ endpoint ecosystem speaks OneBot and the
  app's group-chat semantics (poke, forward, member info) map directly.
- **Tool-based sending**: the model can stay silent, send multiple messages,
  and extensions can add tools without changing the core.
- **Shared extension host**: low memory (~one Python process for all
  third-party extensions), but third-party extensions are one trust domain.
  `per_process` and bwrap are future modes.
- **YAML + SQLite**: YAML is desired state (edit in VS Code with schema);
  SQLite is observed runtime state. No write-back from runtime to YAML.
- **No web UI in M1**: logs plus `config.yaml` are the operational interface.

## Future work

- L2 rolling summaries and L3 fact consolidation
- image/sticker/poke extensions
- probability-based participation
- per-process or bwrap isolation for untrusted extensions
- admin UI and Docker packaging
