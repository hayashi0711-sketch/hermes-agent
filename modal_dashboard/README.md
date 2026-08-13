# modal_dashboard

Phase 1c: hosts `hermes dashboard` (the FastAPI app in `hermes_cli/web_server.py`,
including `/api/pty`) on Modal as a single scale-to-zero container. Separate
Modal App from `modal_hub/` (the Phase1a/1b approval-gate addon) — see
`docs/hh-agent/08_Phase1c_Spec.md` for the full design.

## Dockerfile vs. the production `Dockerfile` (repo root)

This directory has its own `Dockerfile`, deliberately **not** shared with the
production one. Differences, and why:

| | Production `Dockerfile` | `modal_dashboard/Dockerfile` |
|---|---|---|
| Process supervision | s6-overlay (multi-service) | none — Modal owns the container lifecycle |
| SQLite | custom-built (WAL-reset fix) | Debian's stock `libsqlite3` |
| Playwright/Chromium | installed | not installed (not needed for the dashboard) |
| Matrix (`python-olm`) build toolchain | installed | not installed |
| Photon sidecar | installed | not installed |
| Python extras | `[all]` + `messaging` + `otlp` + `anthropic` + `bedrock` + `azure-identity` + `hindsight` + `matrix` | `[web]` + `[anthropic]` + `[pty]` only |
| Node version | 26 | **22** (intentional mismatch — see `docs/hh-agent/08_Phase1c_Spec.md` §4.3) |

**If you change the production `Dockerfile`, check whether the same change
belongs here too** — there is no automated drift check between the two.

## PoC results (2026-08-13, informed this design)

- Image size: 632MB (`/opt/hermes` disk usage). Budget: 5GB.
- Build time: 75.93s. Budget: 10 minutes.
- `/api/pty` confirmed spawning `hermes --tui` (the Node/`ui-tui` bundle) and
  returning real ANSI terminal-init sequences, via an in-process Starlette
  `TestClient` inside the built image.
