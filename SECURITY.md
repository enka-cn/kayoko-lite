# Security model (M1)

## Trust boundary

```
core process (trusted)
  ├─ OneBot transport, SQLite, LLM client, sender
  ├─ capability broker
  └─ shared extension host process (third-party extensions)
```

Third-party extensions run in **one shared host process**. They are treated as
mutually trusted. The capability broker protects the core and its credentials
from accidental misuse, not from malicious extension code.

## What this does protect

- Extensions never receive the OneBot tokens, the LLM API key, or a database
  handle.
- OneBot actions are mediated and restricted to a read-only allowlist unless a
  permission is explicitly declared.
- Outbound messages go through the core send queue (rate limits, dedupe,
  splitting) when the `message_send` capability is granted.
- Extension storage is namespaced by extension id.
- The host is a separate process with:

  - `RLIMIT_AS`, `RLIMIT_NOFILE`, `RLIMIT_NPROC`, `RLIMIT_CORE`
  - a scrubbed environment (no secrets)
  - its own working directory (`data/extension_host`)
  - per-call timeouts; hard timeouts kill and restart the host
  - an RSS watchdog and crash circuit breaker

## What this does **not** protect

- A malicious extension can still read files the host user can read, including
  `config.yaml` and `.env` if they are in the same account.
- A malicious extension can still open raw sockets; the network permission is
  cooperative, not enforced by the kernel.
- Extensions in the shared host can read each other's memory and can spoof
  another extension's id in capability calls.
- There is no `bwrap`, container, or per-extension process in M1.

Therefore: **only install extensions you trust.** The shared host is a fault
isolation and credential-isolation mechanism, not a security sandbox.

## Deployment recommendations

- Run qqagent under a dedicated OS user.
- Keep `config.yaml` and `.env` mode `600` and owned by that user.
- Do not expose the console, SnowLuma WebUI, or OneBot ports publicly; bind to
  loopback and use SSH tunnels.
- Use a secondary QQ account. Third-party QQ protocol endpoints carry account
  risk.
- Pin dependency versions and review extension code before enabling it.

## Future work

- `isolation: per_process`: one host process per third-party extension.
- Linux `bubblewrap` profile: read-only root, private `/tmp`, optional network
  namespace, bind only the extension directory.
- Core-mediated network proxy with domain allowlists and SSRF protection.

## Reporting

Until the project has a dedicated security contact, open a private security
advisory on the repository instead of a public issue.
