# Corpus2Skill Memory Provider

Hermes `MemoryProvider` plugin that bridges Windows-native Hermes and the
Modal-hosted Hermes (Phase1c dashboard) onto one shared memory backend: the
existing Corpus2Skill Modal app
(`https://hayashi0711--corpus2skill-serve.modal.run`).

Design contract: `docs/hh-agent/03_Architecture.md` §13 (M-05/M-06/M-07) in
this repo, read together with `Corpus2Skill/doc/03_Architecture.md` §12
(the backend side — Lane A/Lane B).

## What it does

- **Prefetch** (every turn, synchronous): queries Corpus2Skill's Lane A
  (`GET /api/search`, classified long-term memory) and Lane B
  (`GET /api/journal/recall`, raw session journal) and injects a merged,
  formatted context block.
- **Sync** (every turn, background thread): writes the user and assistant
  turn content to Lane B (`POST /api/journal/write`). Runs off the main
  thread per the `MemoryProvider` threading contract — never blocks the
  agent's turn loop, and failures are logged, not raised.
- **Tool**: exposes exactly one agent-callable tool, `corpus2skill_search`
  (wraps `GET /api/search`). **No write-capable tool is ever exposed** —
  the agent cannot call `journal_write` or anything equivalent to
  `add_new_memory` through this plugin.
- **Session end**: no-op in v1. Promoting Lane B entries into Lane A is out
  of scope here — see the unresolved item in Corpus2Skill's own
  architecture doc.

## Transport

Plain HTTPS REST via the Python standard library (`urllib.request`) — no
MCP protocol client. Auth is a fixed Bearer token, the same scheme
Corpus2Skill's MCP server already uses:

```
Authorization: Bearer <CORPUS2SKILL_API_KEY>
```

## Installation

This plugin lives at `./.hermes/plugins/corpus2skill/` in this repository
— a Hermes **Project Provider**. It only activates when
`HERMES_ENABLE_PROJECT_PLUGINS=1` is set (Hermes' project-plugin opt-in;
see `hermes_cli/plugins.py`).

For the Modal-hosted dashboard container, `HERMES_ENABLE_PROJECT_PLUGINS=1`
needs to be set wherever that container's `HERMES_HOME` environment is
configured (Dockerfile or startup script — unresolved item E in
`03_Architecture.md` §13, does not block this plugin's implementation).

For the separate Windows-native Hermes install (not a checkout of this
repo), copy this directory as-is to that install's **User Provider**
location:

```
%HERMES_HOME%\plugins\corpus2skill\
```

(one-time manual copy for now — unresolved item F in `03_Architecture.md`
§13; the plugin code itself does not change between the two locations).

Then activate it like any other memory provider:

```bash
hermes config set memory.provider corpus2skill
# or: hermes memory setup   # interactive picker
```

## Configuration

`hermes memory setup` only prompts for the API key — this follows the
"minimal config schema" guidance in the memory-provider-plugin developer
guide (see the Supermemory provider, `plugins/memory/supermemory/`, for the
same pattern).

| Key | Where it lives | Required | Default |
|---|---|---|---|
| `api_key` | `.env` as `CORPUS2SKILL_API_KEY` (secret) | Yes | — |
| `base_url` | `$HERMES_HOME/corpus2skill.json` | No | `https://hayashi0711--corpus2skill-serve.modal.run` |

Manual setup, equivalent to the wizard. Write to the **active profile's**
`.env` — for the default profile that's `~/.hermes/.env`, but for a named
profile it's `~/.hermes/profiles/<profile>/.env` (or wherever `$HERMES_HOME`
resolves to for that profile). Using the wrong path silently configures the
default profile instead of the one you're running:

```bash
echo "CORPUS2SKILL_API_KEY=your-bearer-token" >> "$HERMES_HOME/.env"
hermes config set memory.provider corpus2skill
```

To point at a different backend (e.g. a local Corpus2Skill dev server),
write `$HERMES_HOME/corpus2skill.json` directly:

```json
{ "base_url": "http://localhost:8000" }
```

## Failure behavior

- `is_available()` never makes a network call — it only checks that
  `CORPUS2SKILL_API_KEY` is set.
- If the Corpus2Skill backend is unreachable or errors, `prefetch()`
  returns an empty context (logged, not raised) rather than blocking or
  breaking the turn.
- `sync_turn()` failures are logged from its background thread and never
  propagate — a lost journal write does not interrupt the conversation.
- `corpus2skill_search` (the one agent-callable tool) surfaces failures to
  the agent as a tool error, since that is a foreground, agent-initiated
  call rather than an automatic background hook.

## Endpoints used

| Method | Path | Lane | Purpose |
|---|---|---|---|
| GET | `/api/search?query=...&limit=...` | A | Classified long-term memory search (prefetch + `corpus2skill_search` tool) |
| GET | `/api/journal/recall?session_id=...&query=...&limit=...` | B | Session journal recall (prefetch only) |
| POST | `/api/journal/write` | B | Journal write, body `{session_id, role, content, turn_index}` (`sync_turn` only, never agent-callable) |
| GET | `/health` | — | Warm/cold diagnostic probe (prefetch only, once after a total prefetch failure) |

`prefetch()` runs under a **total 7.0s deadline** (per-attempt cap 4.0s, one
retry for connection-class errors such as timeout / 502-504) so a Modal cold
start can never stall a turn past MemoryManager's 8s external-prefetch
abandonment window. Explicit tool/journal calls use the 15s default timeout
instead.

These endpoints are being added to Corpus2Skill in parallel with this
plugin and may not exist yet at any given point in time; this plugin
degrades to an empty prefetch / a logged sync failure rather than raising
when they 404 or time out.
