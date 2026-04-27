# spond-mcp

A local-first **Model Context Protocol** server for [Spond](https://www.spond.com/)
that wraps the unofficial [`Olen/Spond`](https://github.com/Olen/Spond) Python
library and exposes a curated set of tools, resources, and prompts to
autonomous agents (Claude Desktop, Claude Code, MCP Inspector, etc.).

> **⚠️ Unofficial API.** Spond does not publish a public API. The underlying
> `spond` package is reverse-engineered and may break without warning. Use this
> server only for personal automations on accounts you own and have permission
> to operate.

## Features

- 13 typed MCP tools across profile, groups, members, events, attendance,
  messages, posts, and Spond Club transactions.
- 5 MCP resources (`spond://profile`, `spond://groups`,
  `spond://events/upcoming`, `spond://schedule/today`, `spond://schedule/week`).
- 3 MCP prompts for weekly schedule summaries, attendance decisions, and
  message drafting.
- Read-only by default. All side effects are gated behind two layers: an
  environment policy flag *and* an explicit `confirm: true` argument.
- Compact, agent-friendly summaries with optional `include_raw` for diagnostics.
- Lazy `aiohttp` session management with clean shutdown.
- Short-TTL cache for low-risk reads, with `refresh: true` to bypass.
- Pydantic-validated inputs and structured error envelopes.
- 48 unit tests with fully fake Spond clients — no real credentials needed
  to run CI.

## Installation

```bash
git clone <this repo>
cd spond_Mcp
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This installs `spond>=1.2.0`, the `mcp` SDK, Pydantic, and the dev tools
(`pytest`, `pytest-asyncio`, `ruff`).

## Configuration

Copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `SPOND_USERNAME` | _(unset)_ | Spond account email. |
| `SPOND_PASSWORD` | _(unset)_ | Spond account password. Stored as a Pydantic `SecretStr` and never logged. |
| `SPOND_CLUB_ID` | _(unset)_ | Optional Spond Club id for `spond_list_club_transactions`. |
| `SPOND_MCP_READ_ONLY` | `true` | Master switch. While `true`, every side-effecting tool is refused. |
| `SPOND_MCP_ALLOW_MESSAGES` | `false` | Allow `spond_send_message`. Has no effect unless `SPOND_MCP_READ_ONLY=false`. |
| `SPOND_MCP_ALLOW_ATTENDANCE_CHANGES` | `false` | Allow `spond_change_event_response`. Has no effect unless `SPOND_MCP_READ_ONLY=false`. |
| `SPOND_MCP_MAX_EVENTS` | `100` | Hard cap applied to `spond_list_events.max_events`. |
| `SPOND_MCP_TIMEZONE` | `UTC` | Reserved for future schedule formatting. Internally everything stays in UTC. |
| `SPOND_MCP_CACHE_TTL_SECONDS` | `60` | TTL for cached profile/groups/events/posts reads. Set to `0` to disable. |

`SPOND_MCP_LOG_LEVEL` can be set (e.g. `DEBUG`) to increase server log
verbosity. Logs go to stderr; credentials, tokens, message bodies, and
attendance payloads are never logged.

## Running the server

```bash
python -m spond_mcp.server
```

The server speaks **stdio**, which is what Claude Desktop and Claude Code
expect. To poke at it manually, use the official MCP inspector:

```bash
npx @modelcontextprotocol/inspector python -m spond_mcp.server
```

## Claude Desktop / Claude Code configuration

Add the following to `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or the equivalent on your platform. For Claude Code, add it to the
`mcpServers` block of your project or user `settings.json`.

```json
{
  "mcpServers": {
    "spond": {
      "command": "python",
      "args": ["-m", "spond_mcp.server"],
      "env": {
        "SPOND_USERNAME": "you@example.com",
        "SPOND_PASSWORD": "••••••••",
        "SPOND_MCP_READ_ONLY": "true"
      }
    }
  }
}
```

If you use a virtual environment, point `command` at its Python:

```json
"command": "/path/to/spond_Mcp/.venv/bin/python"
```

To enable side effects, flip the relevant env flags **and** continue to pass
`confirm: true` from the agent at call time:

```json
"env": {
  "SPOND_USERNAME": "you@example.com",
  "SPOND_PASSWORD": "••••••••",
  "SPOND_MCP_READ_ONLY": "false",
  "SPOND_MCP_ALLOW_MESSAGES": "true",
  "SPOND_MCP_ALLOW_ATTENDANCE_CHANGES": "true"
}
```

## Example prompts to try

Once the server is wired up, ask Claude:

- *“What Spond events do we have this weekend?”*
- *“List my Spond groups.”*
- *“Who has accepted Saturday's match?”*
- *“Draft a message to the U12 team coach, but don't send it yet.”*
- *“Summarize my Spond schedule for the next 7 days.”*

The included prompts (`summarize_weekly_spond_schedule`,
`prepare_attendance_decision`, `draft_spond_message`) are good starting points
in clients that surface MCP prompts.

## Tools

| Tool | Purpose | Side-effecting? |
| --- | --- | --- |
| `spond_get_profile` | Authenticated user's profile summary. | No |
| `spond_list_groups` | All accessible groups, optionally with members. | No |
| `spond_get_group` | One group by id, with members. | No |
| `spond_find_person` | Match a member or guardian by id, profile id, email, or full name. | No |
| `spond_list_events` | List events with date / group / subgroup filters. | No |
| `spond_get_event` | Single event with attendance summary. | No |
| `spond_summarize_schedule` | Day-grouped schedule between two ISO datetimes. | No |
| `spond_get_event_attendance_report` | XLSX export, returned as metadata or written to a temp file. | No |
| `spond_change_event_response` | Set a member's response to *accepted* / *declined* / *unanswered*. | **Yes** |
| `spond_list_messages` | Recent chats with truncated previews. | No |
| `spond_send_message` | Send a Spond chat message. | **Yes** |
| `spond_list_posts` | Group wall posts (graceful error if the installed library lacks `get_posts`). | No |
| `spond_list_club_transactions` | Spond Club finance transactions. | No |

All tools accept `include_raw: true` (read-only tools only) for diagnostic
access to the raw upstream payload, and read tools accept `refresh: true` to
bypass the cache.

### Side-effect gating in detail

Side-effecting tools require **both**:

1. The relevant env policy flag is set:
   - `SPOND_MCP_READ_ONLY=false` **and**
   - `SPOND_MCP_ALLOW_MESSAGES=true` (for sending messages), **or**
   - `SPOND_MCP_ALLOW_ATTENDANCE_CHANGES=true` (for response changes).
2. The agent passes `confirm: true` in the tool call.

If either layer is missing, the tool returns a `spond_policy_denied` error
without contacting Spond.

## Security notes

- Credentials live in environment variables and never appear in logs or
  error messages.
- Outgoing message bodies are not echoed in tool responses.
- Message previews are truncated to 160 characters by default. Pass
  `include_raw: true` to see full bodies.
- Tool descriptions are factual; they contain no instructions to the agent.
  Prompt-injection vectors are limited to the upstream Spond data itself —
  treat that data as untrusted when forwarding it back to the model.
- The server does **not** expose a generic raw-API tool. New endpoints must
  be added explicitly with their own schema.
- The XLSX attendance export is never returned as inline content; it is
  either summarized as metadata or written to a temp file path.

## Development

```bash
ruff check .
pytest
```

The test suite uses fakes for `spond.spond.Spond` and `spond.club.SpondClub`,
so no real account is needed.

Project layout:

```
src/spond_mcp/
  __init__.py
  client.py     # SpondClientManager: lazy auth, caching, error wrapping
  config.py     # Pydantic-settings env config
  errors.py     # Structured error hierarchy
  schemas.py    # Pydantic summaries + mappers
  server.py     # FastMCP server: tools, resources, prompts, entrypoint
tests/
  conftest.py
  test_client_manager.py
  test_config.py
  test_schemas.py
  test_server_meta.py
  test_server_tools.py
```

## Troubleshooting

**`spond_auth_error: Spond credentials are not configured.`**
Set `SPOND_USERNAME` and `SPOND_PASSWORD` in the server's environment.

**`spond_auth_error: Spond authentication failed.`**
The upstream login responded without a `loginToken`. Verify the credentials
in the Spond mobile app, then restart the MCP server (the client manager
caches a single auth attempt per process).

**`spond_unsupported: ... does not expose get_posts()`**
Upgrade `spond` to ≥ 1.2.0. Older versions do not implement wall posts.

**`spond_upstream_error: Request failed with status 4xx/5xx`**
The unofficial API has moved or rate-limited you. Retry with backoff. If the
error persists, check the [Olen/Spond](https://github.com/Olen/Spond/issues)
issue tracker — the wrapper sometimes needs to follow upstream changes.

**Tools are listed but always return `spond_policy_denied`**
The defaults are read-only on purpose. Either keep using the read tools, or
flip `SPOND_MCP_READ_ONLY` to `false` *and* the matching `ALLOW_*` flag, then
pass `confirm: true` at call time.

## License

MIT. The `spond` library it wraps is also MIT-licensed but maintained
independently; please do not file Spond-API issues against this repo.
