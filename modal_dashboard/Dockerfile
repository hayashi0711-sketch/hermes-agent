# Phase 1c dashboard image -- Hermes `dashboard` (FastAPI + /api/pty
# PTY-over-WS chat) on Modal. Trimmed from the production Dockerfile
# (repo root): dropped s6-overlay process supervision, the custom
# SQLite build, Playwright/Chromium, the Matrix (python-olm) build
# toolchain, the Photon sidecar, and the messaging/hindsight/bedrock/
# azure-identity extras. Single ASGI process only -- Modal owns the
# container lifecycle (docs/hh-agent/08_Phase1c_Spec.md §4).
#
# Node is pinned to 22, NOT the production Dockerfile's 26. This is an
# intentional mismatch (spec §4.3) -- do not "fix" it without reading
# that section first.

FROM node:22-bookworm-slim AS node_source

FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get -o Acquire::Retries=3 update && \
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    ca-certificates curl git procps && \
    rm -rf /var/lib/apt/lists/*

COPY --from=node_source /usr/local/bin/node /usr/local/bin/
COPY --from=node_source /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN pip install --no-cache-dir uv==0.11.6

WORKDIR /opt/hermes

# ---------- Layer-cached npm installs ----------
COPY package.json package-lock.json ./
COPY web/package.json web/
COPY ui-tui/package.json ui-tui/
COPY ui-tui/packages/hermes-ink/ ui-tui/packages/hermes-ink/
COPY apps/shared/ apps/shared/
ENV npm_config_install_links=false
RUN npm install --prefer-offline --no-audit --fetch-retries=5 && npm cache clean --force

# ---------- Layer-cached Python deps (web + anthropic only) ----------
COPY pyproject.toml uv.lock ./
RUN touch ./README.md
RUN uv sync --frozen --no-install-project --extra web --extra anthropic --extra pty

# ---------- Frontend build ----------
COPY web/ web/
COPY ui-tui/ ui-tui/
COPY apps/shared/ apps/shared/
RUN cd web && npm run build && cd ../ui-tui && npm run build

# ---------- Source ----------
COPY . .
RUN uv pip install --no-cache-dir --no-deps -e "."

ENV HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist
ENV HERMES_TUI_DIR=/opt/hermes/ui-tui
ENV HERMES_HOME=/opt/data
# USERPROFILE, not HOME: hh_hooks/tool_gate.py's _hh_agent_home() checks
# USERPROFILE first (Windows-style var, unused by anything else on Linux)
# and only falls back to Path.home() if it's unset. Setting HOME=/opt/data
# instead was tried first and broke deploys: the moment the container
# process starts, Python/uv/the toolchain write a $HOME/.cache directory
# BEFORE Modal mounts the Volume, so Modal's mount then fails with "cannot
# mount volume on non-empty path" (all containers crash-looped with this
# exact error on a real deploy). USERPROFILE has no such side effect on
# Linux -- nothing else reads it -- so it seeds the same persistent path
# for agent_token.json/config.yaml (docs/hh-agent/08_Phase1c_Spec.md §3.3)
# without anything writing into the mount point pre-mount.
ENV USERPROFILE=/opt/data
# Lets Hermes discover Project Provider plugins from the repo-local
# ./.hermes/plugins/ directory (docs/hh-agent/03_Architecture.md §13 M-05:
# Corpus2Skill Memory Provider plugin, ./.hermes/plugins/corpus2skill/).
# Without this, Hermes's plugin loader skips the Project Provider tier
# entirely and the memory-provider plugin silently never loads in the
# Modal dashboard container. `.hermes/` itself is not excluded by
# .dockerignore, so `COPY . .` above already ships the plugin directory
# into the image -- this ENV is the only missing wiring.
ENV HERMES_ENABLE_PROJECT_PLUGINS=1
ENV HERMES_DISABLE_LAZY_INSTALLS=1
ENV PATH="/opt/hermes/.venv/bin:${PATH}"
# Do NOT `mkdir -p /opt/data` here: Modal mounts the hh-agent-dashboard-home
# Volume at this exact path, and refuses to mount onto a path that already
# has a filesystem entry baked into the image ("cannot mount volume on
# non-empty path"). Modal creates the mount point itself, so this directory
# must not exist in the image at all.
