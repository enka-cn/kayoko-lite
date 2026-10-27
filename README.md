# qqagent

Minimal, extension-first QQ group agent for OneBot v11. M1 focuses on text
replies, L1 context, tool-based sending, and a small extension host.

```
QQ  ←→  SnowLuma / NapCat / LLBot  ←→  qqagent core  ←→  OpenAI-compatible LLM
        (OneBot v11 endpoint)          prompts/context/tools
```

The core is deliberately small. Images, stickers, poke, L2/L3 memory, search
and other features are extensions added later.

## M1 features

- OneBot v11: forward WebSocket events + HTTP actions, reconnect/backoff
- SQLite message archive, unread tracking, L1 context window
- Trigger policy: mention, reply-to-self, keyword, always, per-group overrides
- OpenAI-compatible LLM with a tool loop; the only built-in output tool is
  `send_message` (silence is a valid action)
- Per-chat send queue: serialization, rate limits, human-like delay, splitting,
  dedupe, self-message archiving
- YAML config with `${ENV_VAR}` substitution, JSON Schema and VS Code support
- Shared extension host process: JSON-RPC, capability broker, permissions,
  rlimits, timeouts, crash restart, circuit breaker
- Structured text/JSON logs; no web UI in M1

Not in M1: L2/L3 memory, images, stickers, poke, web UI, bwrap/per-process
isolation, hot reload.

## Quick start

Requires Python 3.12+ and a running OneBot v11 endpoint.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp config.example.yaml config.yaml
export LLM_API_KEY=...
export SNOWLUMA_WS_TOKEN=...
export SNOWLUMA_HTTP_TOKEN=...
.venv/bin/qqagent config check -c config.yaml
.venv/bin/qqagent run -c config.yaml
```

## SnowLuma on Linux

SnowLuma is a third-party OneBot v11 runtime with its own source-available,
non-commercial license. It is not bundled here. A compatible compose file is in
[`deploy/snowluma-compose.yml`](deploy/snowluma-compose.yml):

```bash
cd deploy
docker compose -f snowluma-compose.yml up -d
```

Then:

1. SSH tunnel `6081` (noVNC), `5099` (SnowLuma WebUI), `3000` (OneBot HTTP),
   and `3001` (OneBot WebSocket) to your machine.
2. Open noVNC and scan the QQ login QR code.
3. In the SnowLuma WebUI, enable the OneBot HTTP server on `3000` and the
   forward WebSocket server on `3001`; set tokens and message format `array`.
4. Put those tokens in `config.yaml` (`ws_token`, `http_token`).

All ports are bound to loopback in the compose file. Never expose the OneBot
endpoint or WebUI to the public internet.

NapCat, LLBot and other OneBot v11 endpoints also work; the agent does not use
SnowLuma-specific APIs.

## Configuration

`config.yaml` is the desired state; runtime state (messages, unread, extension
KV) lives in SQLite under `data/`. Edit YAML in VS Code with schema support:

```json
// .vscode/settings.json (already included)
{
  "yaml.schemas": { "./config.schema.json": ["config.yaml"] }
}
```

Generate the schema after changing config models:

```bash
.venv/bin/qqagent schema -o config.schema.json
```

Key sections:

- `onebot`: endpoint URLs, separate WS/HTTP tokens
- `llm`: OpenAI-compatible base URL, key, model, tool rounds
- `trigger`: mention/keyword/always, allowed groups, cooldowns
- `limits`: per-chat rate limits, split length, dedupe window
- `extensions`: paths, enabled ids, shared host/rlimit/timeout settings

## Commands

```bash
qqagent run -c config.yaml
qqagent config check -c config.yaml     # validate, print secrets masked
qqagent schema -o config.schema.json
qqagent extensions list -c config.yaml
```

## Extensions

Architecture and M1 trade-offs: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

See [`extensions/README.md`](extensions/README.md) and the sample in
[`extensions/example`](extensions/example). The sample is disabled by default;
enable it with:

```yaml
extensions:
  enabled: [example]
```

The shared host is a trust domain, not a sandbox. Read
[SECURITY.md](SECURITY.md) before installing third-party extensions.

## Tests

```bash
.venv/bin/pytest -q
```

The suite uses fake OneBot HTTP/WebSocket servers and a fake LLM; it does not
need a real QQ account. Extension tests spawn a real host process and cover
hooks, tools, storage, permissions, environment scrubbing, crash restart and
hard timeouts.

## Roadmap

- M2: image/sticker/poke extensions, quote handling, L2 rolling summaries
- M3: probability-based participation, L2/L3 memory consolidation, search
- M4: admin UI, Docker packaging, per-process/bwrap isolation

## License

MIT. SnowLuma, NapCat, LLBot and QQ have their own licenses; this project does
not bundle them.
