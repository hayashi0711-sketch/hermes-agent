"""hh-agent-hub Modal entrypoint.

Phase 1a: the FastAPI surface for the mobile approval gate. This module
owns:

- The Modal ``App``, ``Image``, ``Volume``, and ``Secret`` wiring.
- A single ``@modal.asgi_app()`` entrypoint (design doc §4.1 / D-09).
  ``@modal.web_endpoint`` is intentionally not used: every additional
  endpoint would duplicate the cold-start path, and per-container
  sessions need the single shared ASGI process anyway.
- The ``/health`` liveness endpoint.
- Static serving for the PWA shell at ``mobile_app/pwa_approval/``
  (the files themselves are owned by another agent; this file only
  serves them).
- Response-header hardening for the PWA surface per spec §9.1.

The routers that own the actual approval / cloud-agent / voice APIs are
mounted here, not defined here. ``routers/approval_gate`` is the only
Phase 1a router; ``routers/cloud_agent`` and ``routers/voice_gateway``
arrive in later phases and are intentionally not imported.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

import modal
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from modal_hub.core import config

# ---------------------------------------------------------------------------
# Module-scope FastAPI type imports.
# ---------------------------------------------------------------------------
# Design doc §4.1 / known pitfall #9: lazy type annotations that fail to
# resolve at handler-binding time caused every request to 422 in
# 3LLM_MAX. ``from __future__ import annotations`` is allowed (Hermes's
# own tui_gateway/ws.py uses it) but every type the framework might
# reflect on must be importable at module scope. The imports above are
# the entire set used by this file; add new ones here, not inside
# handler bodies.

logger = logging.getLogger("hh_agent.hub")


# ---------------------------------------------------------------------------
# Modal resources
# ---------------------------------------------------------------------------

app = modal.App("hh-agent-hub")

# Bake both the package and PWA into the image so imports and static serving
# do not depend on deployment-time mounts or source placement.
_LOCAL_PWA_DIR = (
    Path(__file__).resolve().parent.parent / "mobile_app" / "pwa_approval"
)
_CONTAINER_PWA_PATH = "/opt/hh-agent/mobile_app/pwa_approval"

# Hermes CLI 本体と Corpus2Skill プラグインの焼き込み先
# （routers/dispatch.py の POST /api/dispatch/headless がサブプロセスで使う）。
# Hermes はローカルパッケージ（pyproject.toml の name=hermes-agent。PyPI 未公開の
# 内部プロジェクト）のため `pip_install("hermes-agent")` では解決できない。
# modal_dashboard/Dockerfile と同じくリポジトリを焼いて editable install する
# （依存は pyproject.toml の完全ピンが単一の正。setuptools==83.0.0 backend）。
_LOCAL_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTAINER_HERMES_PATH = "/opt/hermes"
_LOCAL_C2S_PLUGIN_DIR = _LOCAL_REPO_ROOT / ".hermes" / "plugins" / "corpus2skill"
_CONTAINER_C2S_PLUGIN_PATH = "/opt/hh-agent/corpus2skill-plugin"

# Image matches design doc §4.1 exactly. ``anthropic`` is included for
# the Phase 1b Skill Distiller's Batch API; the package is small and
# removing it just to shrink the Phase 1a image would force a second
# rebuild the moment Distiller lands.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "fastapi",
        "uvicorn",
        "pydantic",
        "httpx",
        "websockets",
        "pyyaml",
        "gitpython",
        "anthropic",
    )
    .add_local_python_source("modal_hub", copy=True)
    .add_local_dir(
        _LOCAL_PWA_DIR,
        remote_path=_CONTAINER_PWA_PATH,
        copy=True,
    )
    # ヘッドレスディスパッチ用: Hermes CLI 本体を焼いて editable install する
    # （pyproject.toml の依存は全て == 完全ピンなので pip の解決がそのまま正）。
    # ignore は dockerignore 構文。リポジトリ内の秘密ファイル（.env・シークレット
    # バックアップ等）と巨大ディレクトリをイメージから除外する。
    .add_local_dir(
        _LOCAL_REPO_ROOT,
        remote_path=_CONTAINER_HERMES_PATH,
        copy=True,
        # Codexレビュー3巡目指摘（P2、2026-08-20）で修正: FilePatternMatcherは
        # 名前だけのパターン（"node_modules"・".venv"等）をコピー元ルート直下の
        # トップレベルにしか一致させない。`**/`を付けない旧パターンでは
        # `ui-tui/node_modules`のようなネストした依存ツリーや、ネストした
        # `.env`/秘密バックアップがイメージへ焼き込まれてしまっていた。
        # 全パターンを再帰化して修正。
        ignore=[
            "**/.git",
            "**/node_modules",
            "**/__pycache__",
            "**/*.pyc",
            "**/.venv",
            "**/dist",
            "**/.env",
            "**/.env.*",
            "**/*secret*",
            # Codexレビュー中に発覚（2026-08-20、Sonnet5が確認・修正）:
            # `.hh-signing.env`（HH_AGENT_TOKEN_SIGNING_KEY・C2S_SKILL_WRITE_KEY
            # を含む）は上記のどのパターンにも一致せず、修正前は焼き込まれて
            # いた。`.git/info/exclude`・`.gitignore`で保護されている秘密系
            # ファイルは全て明示的に除外する。
            "**/.hh-*.env*",
            "**/.op.env",
            "**/*.pem",
            "**/*.ppk",
            "**/privvy*",
        ],
    )
    .run_commands(["pip install -e /opt/hermes"])
    # Corpus2Skill メモリプラグイン（一時 HERMES_HOME のユーザー層へコピーされる。
    # dispatch.py の _resolve_plugin_source_dir 参照）。
    .add_local_dir(
        _LOCAL_C2S_PLUGIN_DIR,
        remote_path=_CONTAINER_C2S_PLUGIN_PATH,
        copy=True,
    )
)

# Volume name and mount path match the design doc §6. The handle is
# resolved per-call inside store.py, not here, so that other modules
# (store.py, audit.py, approval_gate.py) can call
# ``modal.Volume.from_name(...)`` with the same name and pick up the
# same instance.
_STORE_VOLUME_NAME = "hh-agent-store"
_STORE_MOUNT_PATH = "/mnt/hh_store"

# Secret name from design doc §6. The actual values are injected by
# Modal at function-call time; this file never reads them. The keys it
# expects are documented in modal_hub.core.config.ALL_SECRET_KEYS.
_SECRET_NAME = "hh-agent-secret"


# ---------------------------------------------------------------------------
# FastAPI factory
# ---------------------------------------------------------------------------


class HubStartupError(RuntimeError):
    """Raised when the hub cannot safely serve traffic and must not boot.

    Phase 1a's entire safety model is "every dangerous command blocks on
    the approval gate; the gate is fail-closed". A container that boots
    without a working approval gate inverts that silently: ``/health``
    keeps answering ``{"status": "ok"}`` while every ``/api/approval/*``
    call 404s or 500s, so every dangerous command times out and gets
    denied -- but *for the wrong reason*. The operator believes the gate
    is protecting them when it is not (docs/hh-agent defect report,
    DEFECT 3). Anything that would leave the hub in that state must abort
    container startup instead of logging a warning and continuing (spec
    §9 pitfall #15: "黙って空を返す実装は原因を隠す").
    """


# Endpoints that must exist for the approval gate to actually function.
# Mirrors the endpoint table in docs/hh-agent/03_Architecture.md §4.3 /
# 05_Phase1a_Spec.md §1, trimmed to the subset whose *absence* means
# "every HIGH-risk tool call is denied for a reason the operator can't
# see from /health". Used both to gate startup (_verify_required_routes,
# which raises) and to answer /health truthfully at request time
# (_approval_routes_present, which never raises).
_REQUIRED_APPROVAL_ROUTES: Final[tuple[tuple[str, str], ...]] = (
    ("/api/approval/request", "POST"),
    ("/api/approval/poll", "GET"),
    ("/api/approval/claim", "POST"),
    ("/api/approval/complete", "POST"),
    ("/api/approval/pending", "GET"),
    ("/api/approval/respond", "POST"),
    ("/api/pwa/pair", "POST"),
)


# Phase 1b (07_Phase1b_Spec.md §5): the skills-publish endpoint is not part
# of the approval gate's safety model (a missing publish route does not
# invert fail-closed enforcement the way a missing approval route does),
# but this file's established convention is "never boot a hub that looks
# healthy while a mounted feature silently 404s" -- applied consistently
# here too, in a separate tuple so _REQUIRED_APPROVAL_ROUTES keeps meaning
# exactly what its name and docstring say.
_REQUIRED_SKILLS_ROUTES: Final[tuple[tuple[str, str], ...]] = (
    ("/api/skills/publish", "POST"),
)


# Agentic_OS ヘッドレスディスパッチ（.agentic_os_headless_dispatch_task.md）:
# 承認ゲートの安全性モデルには関与しない（欠落しても fail-closed は倒れない）が、
# このファイルの確立した規約「healthy な Hub がマウント済み機能を静かに 404
# させない」を skills と同じく一貫適用する。別タプルにして
# _REQUIRED_APPROVAL_ROUTES の意味を汚さない。
_REQUIRED_DISPATCH_ROUTES: Final[tuple[tuple[str, str], ...]] = (
    ("/api/dispatch/headless", "POST"),
)

_HTTP_METHODS: Final = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")


def _registered_path_methods(fastapi_app: FastAPI) -> set[tuple[str, str]]:
    """The (path, HTTP method) pairs FastAPI has actually registered.

    Deliberately built from ``fastapi_app.openapi()`` rather than by
    walking ``fastapi_app.routes`` directly. The FastAPI version pinned
    for this project (0.139.x) does not flatten an included router's
    child routes into ``app.routes`` synchronously: ``include_router()``
    appends a private ``_IncludedRouter`` proxy whose own ``.path`` is
    ``None``, and the effective (prefix-applied) paths live one or more
    levels deeper inside private attributes (``original_router``,
    ``include_context.prefix``) whose shape is a FastAPI implementation
    detail, not a documented contract. A check built by walking that
    private structure would be correct today and silently wrong the
    next time FastAPI restructures it -- exactly the kind of "passes
    vacuously" failure DEFECT 3 exists to prevent.

    ``app.openapi()`` is FastAPI's own public, documented reflection of
    the fully resolved route table -- prefixes already applied, however
    many levels of ``include_router`` were used to get there -- and it
    requires no HTTP request or ASGI lifespan to be entered to compute.
    This is exactly the mechanism ``test_openapi_schema_resolves_for_
    every_route`` (spec §8.1 / §9 pitfall #9) already relies on to prove
    every route's types resolve, so it is already a hard requirement
    for this app to boot. Disabling the *served* ``/openapi.json``
    route (this app sets ``openapi_url=None``) does not disable the
    underlying ``.openapi()`` method -- it only stops FastAPI from
    mounting a route that serves the computed schema.

    WebSocket routes (``/ws/approval``) do not appear in the OpenAPI
    paths object (OpenAPI has no websocket operation type); none of
    ``_REQUIRED_APPROVAL_ROUTES`` are WebSocket routes, so this is not
    a gap for this check.
    """
    schema = fastapi_app.openapi()
    found: set[tuple[str, str]] = set()
    for path, operations in schema.get("paths", {}).items():
        if not isinstance(operations, dict):
            continue
        for method in operations:
            upper = method.upper()
            if upper in _HTTP_METHODS:
                found.add((path, upper))
    return found


def _approval_routes_present(fastapi_app: FastAPI) -> bool:
    """True iff every required approval-gate route is registered right now.

    Re-checks the live route table rather than trusting that startup
    succeeded, so ``/health`` reflects reality even if this function's
    assumptions about ``_verify_required_routes`` ever drift.
    """
    registered = _registered_path_methods(fastapi_app)
    return all(pair in registered for pair in _REQUIRED_APPROVAL_ROUTES)


def _skills_routes_present(fastapi_app: FastAPI) -> bool:
    """True iff the Phase 1b skills-publish route is registered right now.

    Mirrors :func:`_approval_routes_present`; kept as a separate function
    (rather than folded into it) so a failure here is distinguishable from
    an approval-gate failure in logs and in ``/health``'s degraded reason,
    should one ever be added.
    """
    registered = _registered_path_methods(fastapi_app)
    return all(pair in registered for pair in _REQUIRED_SKILLS_ROUTES)


def _dispatch_routes_present(fastapi_app: FastAPI) -> bool:
    """True iff the Agentic_OS dispatch route is registered right now.

    Mirrors :func:`_skills_routes_present`; kept separate so a failure here
    is distinguishable in logs and in ``/health``'s degraded reason.
    """
    registered = _registered_path_methods(fastapi_app)
    return all(pair in registered for pair in _REQUIRED_DISPATCH_ROUTES)


def _verify_required_routes(fastapi_app: FastAPI) -> None:
    """Abort startup if a required approval-gate / skills / dispatch route did not register.

    Defense in depth on top of :func:`_include_approval_router` /
    :func:`_include_skills_router` / :func:`_include_dispatch_router`: even
    if ``include_router`` "succeeded", a router that was subtly wrong (e.g.
    decorated the wrong prefix, or a future refactor drops an endpoint)
    would otherwise boot a hub that looks fine at ``/health`` immediately
    after include but is still missing an enforcement endpoint.
    """
    registered = _registered_path_methods(fastapi_app)
    missing = [
        f"{method} {path}"
        for path, method in (
            *_REQUIRED_APPROVAL_ROUTES,
            *_REQUIRED_SKILLS_ROUTES,
            *_REQUIRED_DISPATCH_ROUTES,
        )
        if (path, method) not in registered
    ]
    if missing:
        raise HubStartupError(
            "required routes are missing after include_router; refusing "
            "to boot: " + ", ".join(missing)
        )


def _verify_required_secrets() -> None:
    """Abort startup if any secret the approval gate needs is unset.

    ``modal_hub.core.config.all_required_present()`` is the self-diagnostic
    the config module documents as existing "mainly for startup
    self-diagnostics" (see its docstring) but that nothing previously
    called. Without this, a deploy missing e.g. ``HH_AGENT_TOKEN_SIGNING_KEY``
    boots a hub where every agent/PWA auth check 401s -- again, silently,
    with ``/health`` still saying "ok".
    """
    if not config.all_required_present():
        raise HubStartupError(
            "one or more required secrets are unset (see "
            "modal_hub.core.config for the required key list); refusing "
            "to boot a hub whose approval gate cannot authenticate requests"
        )


def _include_approval_router(fastapi_app: FastAPI) -> None:
    """Wire ``routers.approval_gate.router`` -- or abort startup.

    An earlier version of this function caught ``ImportError`` and logged
    a warning so the container could boot ahead of ``approval_gate.py``
    landing during parallel development. That module is no longer a
    work-in-progress stub owned by a concurrent task; it is Phase 1a's
    only enforcement point, already implemented, and there is nothing
    "genuinely optional" left to protect by swallowing its failure. Any
    failure here -- a missing dependency, a broken import in code this
    file doesn't own, the module exporting no ``router`` -- must abort
    container startup, not log and continue. Do not reintroduce a
    try/except-and-continue around this import (that pattern remains
    legitimate only for routers that are genuinely optional in this
    phase, e.g. the Phase 2 voice gateway that this file intentionally
    does not import at all).
    """
    try:
        from modal_hub.routers import approval_gate
    except ImportError as exc:
        raise HubStartupError(
            "modal_hub.routers.approval_gate failed to import; the "
            "approval gate is mandatory in Phase 1a and the hub must not "
            "serve traffic without it"
        ) from exc

    router = getattr(approval_gate, "router", None)
    if router is None:
        raise HubStartupError(
            "modal_hub.routers.approval_gate imported but exposes no "
            "`router` attribute; the hub must not serve traffic without "
            "the approval API mounted"
        )

    fastapi_app.include_router(router)


def _include_skills_router(fastapi_app: FastAPI) -> None:
    """Wire ``routers.skills.router`` -- or abort startup.

    Same rigor as :func:`_include_approval_router` (see its docstring):
    no catch-and-log fallback. ``modal_hub.routers.skills`` is Phase 1b's
    only server-side surface and there is nothing "genuinely optional"
    about it once Phase 1b work has started.
    """
    try:
        from modal_hub.routers import skills
    except ImportError as exc:
        raise HubStartupError(
            "modal_hub.routers.skills failed to import; the hub must not "
            "serve traffic without it once Phase 1b is wired in"
        ) from exc

    router = getattr(skills, "router", None)
    if router is None:
        raise HubStartupError(
            "modal_hub.routers.skills imported but exposes no `router` "
            "attribute; the hub must not serve traffic without the "
            "skills-publish API mounted"
        )

    fastapi_app.include_router(router)


def _include_dispatch_router(fastapi_app: FastAPI) -> None:
    """Wire ``routers.dispatch.router`` -- or abort startup.

    Same rigor as :func:`_include_skills_router` (see its docstring): no
    catch-and-log fallback. ``modal_hub.routers.dispatch`` is the Agentic_OS
    headless-dispatch surface (.agentic_os_headless_dispatch_task.md) and
    there is nothing "genuinely optional" about it once it is wired in.
    """
    try:
        from modal_hub.routers import dispatch
    except ImportError as exc:
        raise HubStartupError(
            "modal_hub.routers.dispatch failed to import; the hub must not "
            "serve traffic without it once the dispatch API is wired in"
        ) from exc

    router = getattr(dispatch, "router", None)
    if router is None:
        raise HubStartupError(
            "modal_hub.routers.dispatch imported but exposes no `router` "
            "attribute; the hub must not serve traffic without the "
            "dispatch API mounted"
        )

    fastapi_app.include_router(router)


def _mount_pwa(fastapi_app: FastAPI) -> Path | None:
    """Serve the PWA shell from ``mobile_app/pwa_approval/`` if present.

    A deployed Modal Function must contain the image-baked PWA directory;
    its absence is a startup error because the approval UI would otherwise
    return 404 while the API appears healthy. Outside a Modal Function,
    use the repository-relative directory and preserve the silent skip for
    tests or development contexts that intentionally omit the assets.

    Returns the resolved PWA directory, or ``None`` if not mounted.
    """
    running_in_modal_function = not modal.is_local()
    pwa_dir = (
        Path(_CONTAINER_PWA_PATH) if running_in_modal_function else _LOCAL_PWA_DIR
    )
    if not pwa_dir.is_dir():
        if running_in_modal_function:
            raise HubStartupError(
                "image-baked PWA directory is missing at "
                f"{pwa_dir}; refusing to boot a hub whose approval UI would return 404"
            )
        logger.info("PWA directory not found at %s; static serving disabled.", pwa_dir)
        return None

    @fastapi_app.get("/", include_in_schema=False)
    def _root() -> FileResponse:
        return FileResponse(pwa_dir / "index.html")

    @fastapi_app.get("/sw.js", include_in_schema=False)
    def _sw() -> FileResponse:
        return FileResponse(pwa_dir / "sw.js", media_type="application/javascript")

    @fastapi_app.get("/manifest.webmanifest", include_in_schema=False)
    def _manifest() -> FileResponse:
        return FileResponse(
            pwa_dir / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    # Everything else (app.js, style.css, icons, ...) lives under
    # /static so the static-files handler can serve them with
    # accurate MIME types. The CSP middleware below restricts the
    # page's outbound network to 'self' — these paths all qualify.
    fastapi_app.mount(
        "/static",
        StaticFiles(directory=str(pwa_dir)),
        name="pwa_static",
    )

    return pwa_dir


# Header values are constants (not format strings) so they're easy to
# diff in code review and impossible to mutate at request time.
_CSP_VALUE = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


def _install_security_headers(fastapi_app: FastAPI) -> None:
    """Apply PWA security headers per design doc §9.1, plus Cache-Control
    for endpoints that carry private state (DEFECT 2).

    CSP / Referrer-Policy / X-Content-Type-Options / COOP apply to the
    PWA surface only — the API endpoints do not need CSP and the spec
    explicitly carves the two surfaces apart. The middleware matches
    on path prefix: ``/``, ``sw.js``, ``manifest``, and anything under
    ``/static/`` get the hardening; JSON API responses do not.

    Separately, every ``/api/*`` response gets ``Cache-Control: no-store``.
    Pending/detail/poll responses carry attacker-influenced command text,
    file paths, and payloads that are private to the caller; nothing
    under ``/api/`` (approval, pairing, session, WS-ticket) is safe to
    let a shared cache or the browser's back/forward cache retain.
    Static assets and ``/health`` are intentionally excluded — neither
    path is under ``/api/`` so this branch never touches them, and they
    have no private content that needs the header.
    """
    pwa_paths = ("/", "/sw.js", "/manifest.webmanifest")

    @fastapi_app.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path in pwa_paths or path.startswith("/static/"):
            response.headers["Content-Security-Policy"] = _CSP_VALUE
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        elif path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def _build_fastapi() -> FastAPI:
    """Construct the FastAPI app. Called once per container boot."""
    fastapi_app = FastAPI(
        title="hh-agent-hub",
        version="0.1.0",
        # The hub has no public API docs; surface them only if a
        # future operator toggles them on. Keeping them off by default
        # avoids leaking the route inventory to a casual GET /docs.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @fastapi_app.get("/health", include_in_schema=False)
    def health() -> JSONResponse:
        # Spec §4.3: "疎通のみ。内部情報を返さない". No uptime, no
        # version string, no Dict/Volume probe — those are diagnostics
        # for an authenticated operator endpoint, not a public probe.
        #
        # DEFECT 3: this must not claim "ok" unless the approval routes
        # are actually registered on this app right now. Re-checking the
        # live route table (rather than a boolean captured once at build
        # time) means this stays honest even if a future change to
        # _build_fastapi() ever lets a partially-wired app slip through.
        if (
            not _approval_routes_present(fastapi_app)
            or not _skills_routes_present(fastapi_app)
            or not _dispatch_routes_present(fastapi_app)
        ):
            return JSONResponse(status_code=503, content={"status": "degraded"})
        return JSONResponse(status_code=200, content={"status": "ok"})

    # DEFECT 3: every one of these raises HubStartupError (a subclass of
    # RuntimeError) on failure. None of them catch-and-log. A container
    # that can't satisfy all three must not finish booting — Modal will
    # surface the crash instead of serving a hub that looks healthy
    # while its approval gate is absent or unauthenticated.
    _include_approval_router(fastapi_app)
    _include_skills_router(fastapi_app)
    _include_dispatch_router(fastapi_app)
    _mount_pwa(fastapi_app)
    _install_security_headers(fastapi_app)
    _verify_required_routes(fastapi_app)
    _verify_required_secrets()

    return fastapi_app


# ---------------------------------------------------------------------------
# Modal ASGI entrypoint
# ---------------------------------------------------------------------------


@app.function(
    image=image,
    secrets=[modal.Secret.from_name(_SECRET_NAME)],
    volumes={_STORE_MOUNT_PATH: modal.Volume.from_name(
        _STORE_VOLUME_NAME, create_if_missing=True
    )},
    # D-19 / scale-to-zero: cost floor is zero. Warm-path SLO is 1s,
    # cold-path SLO is 10s; the spec treats these as two separate
    # budgets, so a cold start that misses 1s is not a defect.
    min_containers=0,
    scaledown_window=300,
)
@modal.asgi_app()
def fastapi_app() -> FastAPI:
    """Return the ASGI 3.0 app that Modal will serve.

    The function is invoked once per container boot. The returned
    FastAPI instance is what uvicorn (under Modal's ASGI runner) serves
    for every request. ``@modal.asgi_app`` is the **only** decorator
    that produces a public URL; sibling functions declared with
    ``@modal.web_endpoint`` would each spin up their own cold-start
    path, which the design doc explicitly forbids (D-09).
    """
    return _build_fastapi()


# `Any` is imported above to keep the imports section uniform; it is
# also a useful sentinel that ``from __future__ import annotations``
# is honoured by the static checkers that look at module-scope names.
_ = Any


# Agentic_OS ヘッドレスディスパッチのトークン発行経路
# （.agentic_os_issue_dispatch_token_task.md / Agentic_OS/03_Architecture.md §11.0）:
# `dispatch_token.issue_dispatch_token` はこの `app` に @app.function() 登録される
# Modal Function（公開 HTTP エンドポイントではない）。`modal deploy modal_hub.main`
# の際にこの import が @app.function() を実行させ関数を app へ登録する
# （import しないと `modal.Function.from_name("hh-agent-hub",
# "issue_dispatch_token")` が解決できず、Hub Backend からの呼び出しが 404 相当に
# なる）。routers/ の既存ルートは一切変更しない。
from modal_hub import dispatch_token as _dispatch_token  # noqa: F401,E402


# Agentic_OS 承認オラクル用トークン発行経路
# （.agentic_os_issue_approval_token_task.md / Agentic_OS/03_Architecture.md §7 B7）:
# `approval_token.issue_approval_agent_token` はこの `app` に @app.function() 登録
# される Modal Function（公開 HTTP エンドポイントではない）。`modal deploy
# modal_hub.main` の際にこの import が @app.function() を実行させ関数を app へ
# 登録する（import しないと `modal.Function.from_name("hh-agent-hub",
# "issue_approval_agent_token")` が解決できず、Hub Backend からの呼び出しが
# 404 相当になる）。routers/ の既存ルートは一切変更しない。
from modal_hub import approval_token as _approval_token  # noqa: F401,E402
