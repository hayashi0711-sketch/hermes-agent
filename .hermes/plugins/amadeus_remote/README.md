# AmadeuS-Remote Memory Provider

Hermes `MemoryProvider` plugin that bridges the Modal-hosted Hermes
(Phase1c dashboard) onto **AmadeuS** — the unified memory gateway that
routes every query and write across NCAM and Corpus2Skill — via the
already-deployed AmadeuS-Remote Modal service
(`https://hayashi0711--amadeus-remote-serve.modal.run`).

This plugin **complements** the existing
[`../corpus2skill/`](../corpus2skill/) provider in this repository: the
corpus2skill plugin is deliberately left in place and untouched. The two
plugins can coexist; actually switching `memory.provider` on the live
Volume is a separate task.

Design context: `docs/hh-agent/03_Architecture.md` §13 (M-05/M-06/M-07)
in this repo (written for the corpus2skill plugin; the placement, hook
set and tool-surface rules apply unchanged), read together with the
backend side in the AmadeuS repo — `doc/12_Phase5_Architecture.md`
(D-50〜D-62), the deployed `services/modal/app.py`, and the Windows-native
`amadeus_hooks/` package (behavioral parity target for the automatic
hooks).

## What it does

- **Prefetch** (every turn, synchronous): queries AmadeuS
  (`GET /api/query` — one call that fans out across NCAM and
  Corpus2Skill server-side) and injects a formatted context block
  (`<amadeus-context>`, one bullet per evidence item, same rendering as
  the Windows-native `amadeus_prefetch` hook).
- **Sync** (every turn, background thread): writes the user and assistant
  turn content as journal entries (`POST /api/write`,
  `write_type=journal`, `apply=true`). Runs off the main thread per the
  `MemoryProvider` threading contract — never blocks the agent's turn
  loop, and failures are logged, not raised.
- **Session end** (session boundary, synchronous): records one episode
  entry (`write_type=episode`, `role=system`, `subject=session_end`,
  `namespace=hh-agent-dashboard`, `apply=true`) summarizing the session —
  behavior parity with the Windows-native `amadeus_session_end` hook.
- **Tool**: exposes exactly one agent-callable tool, `amadeus_query`
  (wraps `GET /api/query`). **No write-capable tool is ever exposed** —
  the agent cannot trigger a journal/episode write through this plugin.

## Transport

Plain HTTPS REST via the Python standard library (`urllib.request`) — no
MCP protocol client, no third-party HTTP dependency. This Hermes runs
with `HERMES_DISABLE_LAZY_INSTALLS=1` (sealed venv), which is exactly why
AmadeuS-Remote exposes a REST fallback alongside its MCP endpoint.
Auth is a fixed Bearer token:

```
Authorization: Bearer <AMADEUS_REMOTE_API_KEY>
```

## Installation

This plugin lives at `./.hermes/plugins/amadeus_remote/` in this
repository — a Hermes **Project Provider**. It only activates when
`HERMES_ENABLE_PROJECT_PLUGINS=1` is set (Hermes' project-plugin opt-in;
see `hermes_cli/plugins.py`). For the Modal-hosted dashboard container
this is handled via `.dockerignore` (the directory is shipped into the
image) and the container's `HERMES_HOME` configuration.

For a separate Windows-native Hermes install (not a checkout of this
repo), copy this directory as-is to that install's **User Provider**
location:

```
%HERMES_HOME%\plugins\amadeus_remote\
```

Then activate it like any other memory provider:

```bash
hermes config set memory.provider amadeus_remote
# or: hermes memory setup   # interactive picker
```

## Configuration

`hermes memory setup` only prompts for the API key — this follows the
"minimal config schema" guidance in the memory-provider-plugin developer
guide (see the corpus2skill plugin and the Supermemory provider for the
same pattern).

| Key | Where it lives | Required | Default |
|---|---|---|---|
| `api_key` | `.env` as `AMADEUS_REMOTE_API_KEY` (secret) | Yes | — |
| `base_url` | `$HERMES_HOME/amadeus_remote.json` | No | `https://hayashi0711--amadeus-remote-serve.modal.run` |

The API key is the same `AMADEUS_REMOTE_API_KEY` that protects the
deployed AmadeuS-Remote service (Modal Secret `amadeus-remote-secret`).
Never commit the real value.

Manual setup, equivalent to the wizard. Write to the **active profile's**
`.env` — for the default profile that's `~/.hermes/.env`, but for a named
profile it's `~/.hermes/profiles/<profile>/.env` (or wherever `$HERMES_HOME`
resolves to for that profile):

```bash
echo "AMADEUS_REMOTE_API_KEY=your-bearer-token" >> "$HERMES_HOME/.env"
hermes config set memory.provider amadeus_remote
```

To point at a different backend (e.g. a local AmadeuS-Remote dev server),
write `$HERMES_HOME/amadeus_remote.json` directly:

```json
{ "base_url": "http://localhost:8000" }
```

## Failure behavior

- `is_available()` never makes a network call — it only checks that
  `AMADEUS_REMOTE_API_KEY` is set.
- If the AmadeuS-Remote backend is unreachable or errors, `prefetch()`
  returns an empty context (logged, not raised) rather than blocking or
  breaking the turn.
- `sync_turn()` failures are logged from its background thread and never
  propagate — a lost journal write does not interrupt the conversation.
- `on_session_end()` failures are logged and never propagate — a lost
  session-end episode does not interrupt shutdown.
- `amadeus_query` (the one agent-callable tool) surfaces failures to the
  agent as a tool error, since that is a foreground, agent-initiated
  call rather than an automatic background hook.

## Endpoints used

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/query?question=...&per_device_limit=...&snippet_chars=...` | Unified recall across NCAM + Corpus2Skill (prefetch + `amadeus_query` tool) |
| POST | `/api/write` | Journal writes `{write_type: journal, ..., apply: true}` (`sync_turn` only) and the session-end episode `{write_type: episode, role: system, subject: session_end, namespace: hh-agent-dashboard, apply: true}` (`on_session_end` only) — never agent-callable |
| GET | `/health` | Warm/cold diagnostic probe (prefetch only, once after a content-less prefetch; unauthenticated) |

`prefetch()` runs under a **total 7.0s deadline** (per-attempt cap 4.0s,
one retry for connection-class errors such as timeout / 502-504) so a
Modal cold start can never stall a turn past MemoryManager's 8s
external-prefetch abandonment window. Explicit tool calls use the 15s
default timeout instead.
