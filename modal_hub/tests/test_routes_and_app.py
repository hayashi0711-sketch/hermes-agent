"""FastAPI 起動時ルート検証 — 親設計書 §8.1 の最初の項目・§9 落とし穴 9。

    「全ルートが正しくスキーマ解決されること（422 事故の再発防止）」

3LLM_MAX で起きた「全リクエスト 422」は、遅延評価された `Request` 型が
モジュール名前空間に無く、FastAPI がそれを**クエリパラメータと誤認**した
ケース。`from __future__ import annotations` 自体は禁止ではないが、
FastAPI がリフレクションする型はモジュールスコープで import されていな
ければならない。

このテストは「アプリを実際に組み立て、全ルートの dependant を解決し、
`Request` / `WebSocket` がクエリパラメータへ落ちていないこと」を見る。
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute

import modal_hub.main as hub_main
from modal_hub.routers import approval_gate


def iter_api_routes(app: FastAPI):
    """`include_router` 後のラッパーも含めて全 APIRoute を辿る。"""
    seen: list = []
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        if isinstance(route, (APIRoute, APIWebSocketRoute)):
            seen.append(route)
            continue
        for attr in ("routes", "original_router"):
            child = getattr(route, attr, None)
            if child is None:
                continue
            stack.extend(getattr(child, "routes", child) or [])
    return seen


@pytest.fixture()
def app(secret_env) -> FastAPI:
    """`main._build_fastapi()` が組み立てるものそのもの。

    `_include_approval_router` が承認ルータを載せるので、ここで重ねて
    `include_router` してはならない（operation ID が重複し、
    「本番と違う構成を検証している」状態になる）。

    DEFECT 3 修正後、`_build_fastapi()` は必須シークレットが揃っていないと
    `HubStartupError` を送出するため、`secret_env`（conftest.py）に依存する
    ようにした。これに伴い `scope="module"` から関数スコープへ変更した
    （`secret_env` は `monkeypatch` を使う都合上、関数スコープでなければ
    pytest の ScopeMismatch になる）。
    """
    built = hub_main._build_fastapi()
    paths = {r.path for r in iter_api_routes(built)}
    assert "/api/approval/request" in paths, "承認ルータが main に載っていない"
    assert approval_gate.router is not None
    return built


def test_the_hub_app_builds_without_a_modal_runtime(secret_env) -> None:
    """コンテナ外（テスト環境）でもアプリの組み立てが完走すること。"""
    assert isinstance(hub_main._build_fastapi(), FastAPI)


def test_openapi_schema_resolves_for_every_route(app: FastAPI) -> None:
    """スキーマ生成が通ること＝全ルートの型注釈が解決できること。"""
    schema = app.openapi()
    assert isinstance(schema, dict) and schema.get("paths") is not None


def test_no_handler_leaks_request_or_websocket_into_query_params(app: FastAPI) -> None:
    """§9 落とし穴 9 の直接的な回帰テスト。

    `Request` がクエリパラメータと誤認されると、その値が必須クエリとして
    要求され **全リクエストが 422** になる。
    """
    offenders = []
    for route in iter_api_routes(app):
        for param in route.dependant.query_params:
            if param.name in ("request", "websocket", "ws", "req"):
                offenders.append(f"{route.path}:{param.name}")
    assert offenders == [], f"Request/WebSocket がクエリパラメータへ落ちている: {offenders}"


def test_no_handler_has_an_unresolved_body_or_query_type(app: FastAPI) -> None:
    routes = iter_api_routes(app)
    assert routes, "ルートが 1 つも収集できていない（テスト側の走査バグ）"
    for route in routes:
        for param in list(route.dependant.query_params) + list(route.dependant.path_params):
            assert not isinstance(param.field_info.annotation, str), (
                f"{route.path}:{param.name} の型注釈が str のまま解決されていない"
            )


def test_all_phase1a_endpoints_are_registered(app: FastAPI) -> None:
    """親設計書 §4.3 のエンドポイント表 ＋ spec §1.6 の detail。"""
    registered = {(r.path, m) for r in iter_api_routes(app) if isinstance(r, APIRoute) for m in r.methods}
    for path, method in [
        ("/health", "GET"),
        ("/api/approval/request", "POST"),
        ("/api/approval/poll", "GET"),
        ("/api/approval/claim", "POST"),
        ("/api/approval/complete", "POST"),
        ("/api/approval/pending", "GET"),
        ("/api/approval/detail", "GET"),
        ("/api/approval/respond", "POST"),
        ("/api/pwa/pair", "POST"),
        ("/api/pwa/ws-ticket", "POST"),
        ("/api/pwa/logout", "POST"),
    ]:
        assert (path, method) in registered, f"{method} {path} が未登録"

    ws_paths = {r.path for r in iter_api_routes(app) if isinstance(r, APIWebSocketRoute)}
    assert "/ws/approval" in ws_paths


def test_health_returns_no_internal_information(secret_env) -> None:
    """§4.3: 「疎通のみ。内部情報を返さない」。"""
    from fastapi.testclient import TestClient

    with TestClient(hub_main._build_fastapi()) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_api_docs_are_disabled(secret_env) -> None:
    """`GET /docs` でルート一覧を漏らさない。"""
    app = hub_main._build_fastapi()
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None


def test_approval_router_is_actually_included_by_main() -> None:
    """`_include_approval_router` が黙って握りつぶさず実際に載せていること。

    親設計書 §9 落とし穴 15「黙って空を返す実装は原因を隠す」。承認ルータが
    載らないままデプロイされると、承認ゲートが存在しない Hub が立つ。

    DEFECT 3 修正で `_include_approval_router` は成功時に `None` を返し、
    失敗時は例外を送出する契約に変わった（旧: 成否を bool で返し、失敗時は
    警告ログのみで握りつぶしていた）。ここでは「例外を送出せず、実際に
    ルートが登録されること」を確認する。
    """
    app = FastAPI()
    hub_main._include_approval_router(app)  # 例外を投げないこと自体が確認事項
    paths = {r.path for r in iter_api_routes(app)}
    assert "/api/approval/request" in paths


def test_include_approval_router_raises_when_the_module_fails_to_import(monkeypatch) -> None:
    """DEFECT 3: import 失敗を黙ってログするだけの旧挙動へ戻さないこと。

    `from modal_hub.routers import approval_gate` は、パッケージ
    `modal_hub.routers` が既に `approval_gate` 属性を持っていれば
    （このテストモジュール自身が冒頭で import 済みなので持っている）、
    `sys.modules` を見ずにその属性をそのまま返してしまう。真に import
    失敗を再現するには、パッケージ側のキャッシュ済み属性も同時に消す
    必要がある。`sys.modules[name] = None` は CPython の import 機構に
    おいて、その名前をあらためて import しようとした際に確実に
    `ImportError` を送出させる標準的な手法（`None` は「import 済みだが
    失敗した」を表す番兵値）。
    """
    import modal_hub.routers as routers_pkg

    monkeypatch.delattr(routers_pkg, "approval_gate", raising=False)
    monkeypatch.setitem(sys.modules, "modal_hub.routers.approval_gate", None)
    app = FastAPI()
    with pytest.raises(hub_main.HubStartupError):
        hub_main._include_approval_router(app)


def test_include_approval_router_raises_when_router_attribute_is_missing(monkeypatch) -> None:
    """モジュールは import できても `router` を公開していない異常系。"""
    import modal_hub.routers as routers_pkg

    fake_module = types.ModuleType("modal_hub.routers.approval_gate")
    monkeypatch.delattr(routers_pkg, "approval_gate", raising=False)
    monkeypatch.setitem(sys.modules, "modal_hub.routers.approval_gate", fake_module)
    app = FastAPI()
    with pytest.raises(hub_main.HubStartupError):
        hub_main._include_approval_router(app)


def _dummy_endpoint() -> dict:
    return {"ok": True}


def test_verify_required_routes_raises_when_a_route_is_missing() -> None:
    """DEFECT 3 の防御的二重チェック: 必須ルートが1つでも欠けたら起動を止める。

    実際の `approval_gate.router` は `include_router()` を経由すると内部
    ラッパー（`_IncludedRouter` 相当。FastAPI 0.139 系の実装詳細）に包まれ
    `app.router.routes` を直接いじっても中身へは届かない。そのため
    `_verify_required_routes()` 単体の契約（「必須パス/メソッドが1つでも
    欠けたら HubStartupError」）を、素の `add_api_route` で手作りしたルート
    集合を使って直接検証する。
    """
    app = FastAPI()
    for path, method in hub_main._REQUIRED_APPROVAL_ROUTES:
        if path == "/api/approval/claim":
            continue  # 意図的に1件だけ欠落させる
        app.add_api_route(path, _dummy_endpoint, methods=[method])
    with pytest.raises(hub_main.HubStartupError):
        hub_main._verify_required_routes(app)


def test_verify_required_routes_passes_when_everything_is_registered() -> None:
    app = FastAPI()
    hub_main._include_approval_router(app)
    hub_main._include_skills_router(app)  # Phase 1b: 必須集合に skills も入った
    hub_main._verify_required_routes(app)  # 例外を投げないこと


def test_verify_required_secrets_raises_when_one_is_missing(secret_env, monkeypatch) -> None:
    """DEFECT 3: config.all_required_present() が起動時に実際に呼ばれること。"""
    from modal_hub.core import config

    monkeypatch.delenv(config.NTFY_TOPIC, raising=False)
    with pytest.raises(hub_main.HubStartupError):
        hub_main._verify_required_secrets()


def test_verify_required_secrets_passes_without_ntfy_token(secret_env, monkeypatch) -> None:
    """NTFY_TOKEN は任意（2026-08-11 決定）。不在でも起動を拒否してはならない。

    以前は _REQUIRED_KEYS に入っており、公開トピック運用（NTFY_TOKEN 空）で
    Hub が毎回 HubStartupError を返していた（2026-08-11 Modal 再デプロイで発覚）。
    """
    from modal_hub.core import config

    monkeypatch.delenv(config.NTFY_TOKEN, raising=False)
    hub_main._verify_required_secrets()  # 例外を投げないこと


def test_verify_required_secrets_passes_when_everything_is_set(secret_env) -> None:
    hub_main._verify_required_secrets()  # 例外を投げないこと


def test_build_fastapi_raises_without_required_secrets(monkeypatch) -> None:
    """DEFECT 3 の結合テスト: シークレット欠如で `_build_fastapi()` 全体が落ちること。

    実際の Modal コンテナでは Secret 未設定のままデプロイされた場合に相当
    する。`/health` が黙って ok を返し続ける事故を防ぐには、この時点で
    起動そのものを失敗させる必要がある。
    """
    from modal_hub.core import config

    for key in config.ALL_SECRET_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(hub_main.HubStartupError):
        hub_main._build_fastapi()


def test_health_reflects_reality_when_approval_routes_are_absent() -> None:
    """DEFECT 3: `/health` は承認ルートが無ければ ok を返してはならない。

    `_approval_routes_present()` を直接検証する。統合された `_build_fastapi()`
    はこの状態になる前に `HubStartupError` で落ちるため（test_build_fastapi_
    raises_without_required_secrets 等）、ここでは health の判定ロジックその
    ものを承認ルータ無しの素の FastAPI で検証する。
    """
    bare_app = FastAPI()
    assert hub_main._approval_routes_present(bare_app) is False

    wired_app = FastAPI()
    hub_main._include_approval_router(wired_app)
    assert hub_main._approval_routes_present(wired_app) is True


# ===========================================================================
# Modal の使い方（D-09 / §9 落とし穴 21 / D-19）
# ===========================================================================


def test_only_one_asgi_entrypoint_and_no_web_endpoint() -> None:
    """D-09: 単一 `@modal.asgi_app()` + FastAPI。`@modal.web_endpoint` を並べない。"""
    source = inspect.getsource(hub_main)
    tree = ast.parse(source)
    decorators = [
        ast.unparse(d)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for d in node.decorator_list
    ]
    assert sum("asgi_app" in d for d in decorators) == 1
    assert not any("web_endpoint" in d for d in decorators)


def test_scale_to_zero_and_no_gpu() -> None:
    """D-19: `min_containers=0`。親設計書 §6: Phase 1 は GPU 指定を書かない。"""
    source = inspect.getsource(hub_main)
    assert "min_containers=0" in source
    assert "gpu=" not in source


def test_min_containers_is_not_used_as_session_affinity() -> None:
    """§9 落とし穴 21: `min_containers` は warm コンテナ数の指定にすぎない。

    Phase 1a はセッションアフィニティを必要としない（cloud_agent は 1c）。
    """
    source = inspect.getsource(hub_main)
    assert "session_affinity" not in source
    assert "sticky" not in source.lower()


def test_resource_names_match_the_design_doc() -> None:
    source = inspect.getsource(hub_main)
    assert '"hh-agent-hub"' in source
    assert '"hh-agent-store"' in source
    assert '"hh-agent-secret"' in source
    assert "/mnt/hh_store" in source


def test_forbidden_existing_resources_are_never_referenced() -> None:
    """親設計書 §6「絶対に触れてはならない既存リソース」。

    この検査の実体は「他プロジェクトの Modal リソース識別子
    （`modal.Dict.from_name(...)` 等に渡す文字列）を H-H Agent 側のコードへ
    直接書かないこと」——誤って本番の他プロジェクトのリソースへ接続・
    作成してしまう事故（過去に実際に発生済み。`08_Handoff_Note.md` 参照）
    を防ぐための機械チェックである。

    `services/memory_bridge.py` は例外: 親設計書 §4.7・行635 が「既存
    Corpus2Skill の MCP サーバー（Modal `corpus2skill`）を読み取り専用で
    叩くクライアント」と明記しており、`corpus2skill` という語をこの1
    ファイルだけは正当に参照する（D-03: 既存 Corpus2Skill は MCP 経由の
    参照専用として温存する、という設計そのものがこの参照を要求している）。
    それでも `c2s-skills-store`/`c2s-secret` のような**実際の Modal
    リソース識別子**は `memory_bridge.py` を含めどのファイルでも禁止のまま
    とする（`connect()` が未実装であることも合わせて、実リソースへは
    一切触れていない）。
    """
    forbidden = [
        "models_cache",
        "hf-cache",
        "2hd-code-evolved",
        "jarvis-memory",
        "c2s-skills-store",
        "vllm-api-key",
        "3llm-max-secret",
        "jarvis-secret",
        "c2s-secret",
        "multi-ai-coder-agent",
        "3llm-max",
        "jarvis-backend",
        "corpus2skill",
    ]
    memory_bridge_exempt = {"corpus2skill"}
    root = Path(inspect.getfile(hub_main)).resolve().parents[1]
    for path in sorted((root / "modal_hub").rglob("*.py")) + sorted((root / "hh_hooks").glob("*.py")):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        applicable = forbidden
        if path.name == "memory_bridge.py":
            applicable = [name for name in forbidden if name not in memory_bridge_exempt]
        hits = [name for name in applicable if name in text]
        assert hits == [], f"{path.name} が既存リソース {hits} を参照している"


# ===========================================================================
# PWA 応答ヘッダ（spec §9.1）
# ===========================================================================


def test_csp_has_no_unsafe_inline_and_no_external_hosts() -> None:
    csp = hub_main._CSP_VALUE
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    assert "http://" not in csp and "https://" not in csp
    for directive in (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ):
        assert directive in csp, f"CSP に {directive!r} が無い"


def test_security_headers_are_applied_to_the_pwa_surface(secret_env) -> None:
    from fastapi.testclient import TestClient

    app = hub_main._build_fastapi()
    with TestClient(app) as client:
        resp = client.get("/")
    if resp.status_code == 404:
        pytest.skip("PWA ディレクトリが無い環境")
    assert resp.headers["Content-Security-Policy"] == hub_main._CSP_VALUE
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cross-Origin-Opener-Policy"] == "same-origin"


# ===========================================================================
# DEFECT 2: Cache-Control: no-store on responses carrying private state
# ===========================================================================


def test_private_api_responses_carry_cache_control_no_store(secret_env) -> None:
    """承認・ペアリング等 `/api/*` はステータスに関係なく no-store を返す。

    401（未認証）応答であっても、応答本文が将来変わりうる／中身がまだ
    無いことを共有キャッシュに学習させてはならないため、エラー応答にも
    等しく適用されることを確認する。
    """
    from fastapi.testclient import TestClient

    app = hub_main._build_fastapi()
    with TestClient(app) as client:
        resp = client.get("/api/approval/pending")
    assert resp.headers.get("cache-control") == "no-store"


def test_static_assets_are_not_forced_to_no_store(secret_env, pwa_dir: Path) -> None:
    """静的アセットは DEFECT 2 の対象外（`/api/` 配下ではない）。"""
    if not pwa_dir.is_dir():
        pytest.skip("PWA ディレクトリが無い環境")
    from fastapi.testclient import TestClient

    app = hub_main._build_fastapi()
    with TestClient(app) as client:
        resp = client.get("/static/app.js")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") != "no-store"


def test_health_is_not_forced_to_no_store(secret_env) -> None:
    from fastapi.testclient import TestClient

    app = hub_main._build_fastapi()
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.headers.get("cache-control") != "no-store"


# ===========================================================================
# DEFECT 1: every asset URL referenced by index.html / manifest.webmanifest
# must actually resolve through the ASGI app (regression for the
# "/app.js" vs "/static/app.js" mismatch that made the PWA unloadable).
# ===========================================================================

_HTML_ASSET_URL_RE = re.compile(r'''(?:href|src)=["']([^"']+)["']''')


def _extract_index_html_asset_urls(html: str) -> list[str]:
    """`href=`/`src=` の値を全て集め、フラグメントリンクと外部/data: を除く。"""
    urls = []
    for value in _HTML_ASSET_URL_RE.findall(html):
        if value.startswith("#"):
            continue  # ページ内アンカー（詳細画面への切替）
        if value.startswith(("http://", "https://", "data:")):
            continue  # CSP により実際には存在しないはずの外部参照
        urls.append(value)
    return urls


def _extract_manifest_asset_urls(manifest_obj: dict) -> list[str]:
    """manifest.webmanifest が参照する取得可能な（data: でない）URL を集める。"""
    urls = []
    start_url = manifest_obj.get("start_url")
    if isinstance(start_url, str) and not start_url.startswith("data:"):
        urls.append(start_url)
    for icon in manifest_obj.get("icons", []):
        src = icon.get("src") if isinstance(icon, dict) else None
        if isinstance(src, str) and not src.startswith("data:"):
            urls.append(src)
    return urls


def _resolve_pwa_url(value: str) -> str:
    """PWA のページ (`/` で配信) からの相対 URL をサイトルート相対の絶対パスへ解決する。"""
    if value.startswith("/"):
        return value
    if value in (".", "./"):
        return "/"
    return "/" + value.lstrip("./")


def test_every_asset_url_referenced_by_index_html_and_manifest_resolves_to_200(
    secret_env, pwa_dir: Path
) -> None:
    """DEFECT 1 の直接の回帰テスト。

    修正前は index.html が `style.css` / `app.js` をサイトルート相対で
    参照していたが、main.py はそれらを `/static/` 配下にしかマウントして
    いなかったため、スマホは静的な HTML の殻しか読み込めず、承認 UI が
    一切初期化されなかった（すべてのタイムアウトが deny になる）。

    このテストは実際のディスク上の index.html / manifest.webmanifest を
    パースして参照される全 URL を抽出し、実際に組み立てた ASGI アプリへ
    `TestClient` で問い合わせて 200 が返ることを確認する。将来また
    パスの不一致を持ち込んだら、このテストが失敗して検知する。
    """
    if not pwa_dir.is_dir():
        pytest.skip("PWA ディレクトリが無い環境")
    from fastapi.testclient import TestClient

    index_html = (pwa_dir / "index.html").read_text(encoding="utf-8")
    manifest_obj = json.loads((pwa_dir / "manifest.webmanifest").read_text(encoding="utf-8"))

    urls = set(_extract_index_html_asset_urls(index_html)) | set(
        _extract_manifest_asset_urls(manifest_obj)
    )
    assert urls, "アセット URL が1件も抽出できなかった（パーサ側の不具合の疑い）"

    app = hub_main._build_fastapi()
    with TestClient(app) as client:
        for raw_url in sorted(urls):
            resolved = _resolve_pwa_url(raw_url)
            resp = client.get(resolved)
            assert resp.status_code == 200, (
                f"{raw_url!r}（解決後 {resolved!r}）が {resp.status_code} を返した"
            )
