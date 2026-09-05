"""hh-agent-dashboard Modal entrypoint.

Phase 1c: runs the REAL `hermes dashboard` CLI (via hh_hermes.py, the
Phase1a launcher that enforces the pre_tool_call approval-gate hook
before handing off -- D-14/D-20) as a subprocess inside a
`@modal.web_server` container.

2026-08-13 redesign: the original `@modal.asgi_app()` approach (return
`hermes_cli.web_server.app` directly) was reviewed and found to bypass
everything `hermes_cli.web_server.start_server()` normally wires up:
`app.state.bound_host`/`bound_port` (needed for `/api/pty` to attach to
THIS process's in-process gateway instead of spawning an unverified
`tui_gateway.entry` subprocess -- Codex finding C-1) and
`app.state.auth_required` (needed to gate unauthenticated access --
Codex finding C-2). Rather than re-implement that wiring by hand (fragile,
drifts from Hermes's own tested behavior), this launches the real
`hermes dashboard --host 0.0.0.0 ...` subcommand, which already handles
all of it correctly for a non-loopback bind. Separate Modal App from
modal_hub/ (the Phase1a/1b approval-gate addon).

`hh-agent-secret` (containing `HH_AGENT_TOKEN_SIGNING_KEY`, a Hub root
credential) is attached ONLY to `refresh_dashboard_agent_token` -- never
to `dashboard_server`, which runs untrusted model-authored commands
(Codex finding C-3). First-boot token seeding calls the refresh function
remotely instead of signing locally.

See docs/hh-agent/08_Phase1c_Spec.md for the full design.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import fastapi
import modal

from modal_dashboard import bootstrap, token_refresh
from modal_hub.core import store

_DASHBOARD_VOLUME_NAME = "hh-agent-dashboard-home"
_DASHBOARD_MOUNT_PATH = "/opt/data"
_DASHBOARD_SECRET_NAME = "hh-agent-dashboard-secret"
_HUB_SECRET_NAME = "hh-agent-secret"  # ONLY attached to refresh_dashboard_agent_token -- see module docstring, C-3
# Separate, dedicated Secret for the Corpus2Skill Memory Provider plugin
# (docs/hh-agent/03_Architecture.md §13). Deliberately NOT folded into
# _DASHBOARD_SECRET_NAME: Modal's `secret create --force` replaces a
# secret's entire key set rather than merging, and this bundle's current
# live values (ANTHROPIC_API_KEY, basic-auth username/password, Hub URL)
# cannot be read back to safely recreate it with one more key added. A
# separate Secret is purely additive and never touches the existing one.
_CORPUS2SKILL_SECRET_NAME = "corpus2skill-secret"
# NCAM連携用(2026-09-05新規)。ncam-serve.modal.run(既存の常設NCAM daemon)へ
# リモート接続するための認証情報のみ含む。PC版Hermesも同じdaemonへ
# NCAM_DAEMON_URL/NCAM_DAEMON_TOKENという同名のOS環境変数経由で接続している
# (ローカルdaemonは持たない・両者は最初から同じ「単一の脳」を共有する設計。
# Obsidian Projects/NCAM 05_Current_State.md「2026-08-28」節参照)。
_NCAM_SECRET_NAME = "ncam-daemon-secret"
# CF-Hermes-Hub（Cloudflare Durable ObjectsのDiscord Gateway常時接続層）から
# cf_hub_dispatch()を呼ぶための共有シークレットのみ含む（Obsidian Projects/
# CF-Hermes-Hub参照）。他のSecretとは無関係の単一用途。
_CF_HUB_SECRET_NAME = "cf-hub-secret"
_DASHBOARD_PORT = 8000

app = modal.App("hh-agent-dashboard")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE_PATH = Path(__file__).resolve().parent / "Dockerfile"
# NCAMは別リポジトリ(Hermes-Hyper-Agent_HHAgentの兄弟ディレクトリ)。このビルドは
# これまで一貫してこのマシン(Projects/配下にNCAMをcloneしたHaruki氏のPC)から
# しか実行されていないため、機械依存の絶対パスではなく兄弟ディレクトリの相対
# 関係として表す。存在確認は行わない(Codexレビュー指摘: モジュールimport時に
# 即FileNotFoundErrorにすると、../NCAMを持たないクリーンチェックアウト/CIで
# modal_dashboard/tests/test_app.py・modal_hub/tests/test_gateway_app.pyの
# importそのものが壊れる。`add_local_dir`はビルド/デプロイ時まで遅延評価される
# ため、本当に存在しない場合はそのタイミングでModal自身が明確なエラーを出す)。
_NCAM_REPO_ROOT = _REPO_ROOT.parent / "NCAM"

image = (
    modal.Image.from_dockerfile(_DOCKERFILE_PATH, context_dir=_REPO_ROOT)
    # ncam.interfaces.mcp_server の基本依存のみ(NCAM_DAEMON_URL/TOKEN設定時は
    # ローカルdaemonをspawnしないため、pyproject.tomlのoptional-dependencies
    # [embed]/[accel]/[llm](fastembed・onnxruntime・numba等の重い依存)は不要)。
    # バージョンは可能な限りhermes-agent自身のpyproject.tomlの既存exact pinと
    # 一致させ(pydantic/fastapi/uvicorn/mcp/numpy)、依存解決の衝突を避ける
    # (Dependency Pinning Policy: 上限なしの`>=`はレビュー対象、AGENTS.md参照)。
    # anthropicは`--extra anthropic`で既にanthropic==0.87.0が入っているため
    # ここでは追加しない。duckdb/pytzはNCAM固有でhermes-agent側に既存pinが
    # 無いため、NCAM自身の.venvで動作確認済みの版に固定する。
    # Image.pip_install()はこのイメージのvenv(uv syncで作られ、pip自体を
    # 含まない)へ`python -m pip`で実行しようとして失敗する(2026-09-05実機で
    # 確認: "No module named pip")。DockerfileのRUN uv syncと同じ`uv pip
    # install`を使う(WORKDIR /opt/hermesがuv syncと同じく永続しているため
    # VIRTUAL_ENV等を明示しなくても.venvが自動検出される)。
    .run_commands(
        "uv pip install "
        "duckdb==1.5.5 pytz==2026.3.post1 numpy==2.4.3 mcp==2.0.0 "
        "fastapi==0.133.1 'uvicorn[standard]==0.41.0' pydantic==2.13.4"
    )
    # ncam_hooks/ は標準ライブラリのみで完結(json/os/sys/typing)。ncam/ 本体は
    # 上のpip_installした依存だけで動く(mcp_server.pyがncam.daemon.launcherを
    # importするが、実際にdaemonをspawnするのはNCAM_DAEMON_URL/TOKEN未設定時
    # のみ)。pyproject.tomlごとコピーしてeditable installする代わりに、
    # パッケージ本体だけをPYTHONPATH配下へ置く(services/・tests/・.venv/等の
    # Modal daemon自体のデプロイ物一式は不要なので持ち込まない)。
    .add_local_dir(str(_NCAM_REPO_ROOT / "ncam"), remote_path="/opt/ncam-pkg/ncam", copy=True)
    .add_local_dir(str(_NCAM_REPO_ROOT / "ncam_hooks"), remote_path="/opt/ncam-pkg/ncam_hooks", copy=True)
    .env({"PYTHONPATH": "/opt/ncam-pkg"})
)


@app.function(
    image=image,
    volumes={_DASHBOARD_MOUNT_PATH: modal.Volume.from_name(_DASHBOARD_VOLUME_NAME, create_if_missing=True)},
    secrets=[
        modal.Secret.from_name(_DASHBOARD_SECRET_NAME),
        modal.Secret.from_name(_HUB_SECRET_NAME),
    ],
    max_containers=1,
    schedule=modal.Period(hours=8),   # TTL is 24h; 8h interval leaves ample margin
)
def refresh_dashboard_agent_token():
    from modal_hub.core import store

    token_refresh.issue_dashboard_agent_token(Path(_DASHBOARD_MOUNT_PATH), store=store)


@app.function(
    image=image,
    volumes={
        _DASHBOARD_MOUNT_PATH: modal.Volume.from_name(_DASHBOARD_VOLUME_NAME, create_if_missing=True),
        # S-08c: Hub Volume（hh-agent-store）もマウントする。マウント先は
        # store.VOLUME_MOUNT_PATH 定数をそのまま使う（モジュールレベルの
        # 同一定数参照なので食い違う余地がない — タスク指示「マウント先は
        # VOLUME_MOUNT_PATH の値と一致させること」の最強の保証）。
        store.VOLUME_MOUNT_PATH: modal.Volume.from_name("hh-agent-store", create_if_missing=True),
    },
    secrets=[
        modal.Secret.from_name(_DASHBOARD_SECRET_NAME),
        # hh-agent-secret（HH_AGENT_TOKEN_SIGNING_KEY）は refresh
        # 関数のみに付けるという C-3 の規約は dashboard_server に関する
        # ものであり、同期関数には適用されない（dashboard_server は
        # 未検証のモデル生成コマンドを実行するが、sync は決まったコード
        # しか実行しない）。sync_dashboard_skills は Lane C のサーバー
        # イベント ACK（C2S_SKILL_WRITE_KEY）のために Hub Secret を持つ。
        modal.Secret.from_name(_HUB_SECRET_NAME),
        # Lane C の読み取り鍵（CORPUS2SKILL_API_KEY）。dashboard_server が
        # 既に使っている corpus2skill-secret をそのまま流用する（S-04:
        # 読み取りは既存 Bearer。新たな秘密を作らない）。
        modal.Secret.from_name(_CORPUS2SKILL_SECRET_NAME),
        # 機能的には不要（sync_dashboard_skillsはhooks/MCPを起動しない固定
        # コードのみ実行する）だが、既存テスト
        # test_sync_dashboard_skills_diff_vs_dashboard_server が
        # 「syncはdashboard_serverの秘密を包含する」という不変条件を検証して
        # いるため、dashboard_server側に追加したncam-daemon-secretもここへ
        # 揃える。
        modal.Secret.from_name(_NCAM_SECRET_NAME),
    ],
    max_containers=1,
    schedule=modal.Period(hours=8),   # refresh_dashboard_agent_token と同じ周期
    timeout=300,                       # S-10 確定事項 G: 1 回の実行に 5 分タイムアウト
)
def sync_dashboard_skills():
    """S-10（Modal 側 pull）+ S-08c（quarantine 消し込み）を 1 つの Function で行う。

    手順:
      1. pull 部分: scripts/hh_skill_sync の `run_sync(pull=True, reconcile=False)`
         を呼ぶ。Modal 側は push しない（S-08「Modal コンテナ上での promote・
         push の実行は v1 では発生しない」）。USERPROFILE=/opt/data が
         Dockerfile で既に設定済みのため、hh_skill_promote.py /
         hh_skill_sync.py の `~/.hh-agent` 相当パスは自動的に Volume 上の
         `/opt/data/.hh-agent` へ解決される（同期状態・outbox・receipt が
         ダッシュボード Volume に永続化される）。
      2. quarantine 消し込み（S-08c、設計書 1141〜1166 行目の手順 1〜5）:
         新しい HTTP エンドポイントは増設しない。消し込みの対象は
         **今回の pull で観測された name だけ**（消し込み済みを誤って再処理
         しない・観測外の名前には触れない）。実測 content_sha256 が
         Lane C 上の現在の版と一致する場合のみ `skills_quarantine_promoted/
         <name>.<ts>/` へコピーし、mark_quarantine_resolved を呼ぶ。
      3. 最後にダッシュボード Volume も commit する（pull 側の書き込みの
         永続化。run_sync の Hub Volume 側 commit は store.py の
         atomic_write_file が行う）。
    """
    import json
    import logging
    import sys
    import time
    from pathlib import Path as _Path

    logger = logging.getLogger("hh_agent.dashboard.sync")

    repo_root = _Path("/opt/hermes")
    for _dir in (repo_root, repo_root / "scripts"):
        if str(_dir) not in sys.path:
            sys.path.insert(0, str(_dir))
    import hh_skill_sync  # noqa: PLC0415 — 関数内 import（sys.path 挿入後に必要）

    # 手順1: pull 部分（Modal 側は push しない）
    try:
        result = hh_skill_sync.run_sync(pull=True, reconcile=False)
    except Exception as exc:  # noqa: BLE001 — 次の周期で再試行する（S-10 確定事項 G）
        logger.exception("sync_dashboard_skills: pull フェーズ失敗（次回再試行）: %s", exc)
        result = {"observed": [], "remote_sha256": {}}

    # 手順2: quarantine 消し込み（S-08c 手順1〜5）。store は関数内 import
    #（store.py はモジュールトップで modal を import する — この関数の
    #  実行環境では Volumes が既にマウント済みなので安全）。
    from modal_hub.core import store

    try:
        store.store_volume().reload()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sync_dashboard_skills: Hub Volume reload 失敗（消し込みスキップ）: %s", exc)
        return

    observed_names = result.get("observed") or []
    remote_sha256 = result.get("remote_sha256") or {}
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime()) + f"{int(time.time() * 1000) % 1000:03d}"
    for name in observed_names:
        try:
            entry = store.read_quarantine_entry_safe(name)
            if entry is None:
                continue  # quarantine に無い → 何もしない
            # 実測 content_sha256 が Lane C 上の現在の版（今回 pull した or
            # 既にローカルにある版）と一致するか。一致しなければ何も書かず
            # 次周期へ持ち越す。
            lane_c_sha = remote_sha256.get(name)
            if lane_c_sha is None or entry.get("content_sha256") != lane_c_sha:
                continue
            dest_dir = f"skills_quarantine_promoted/{name}.{ts}"
            store.atomic_write_file(f"{dest_dir}/SKILL.md", entry["content"].encode("utf-8"))
            meta = {}
            for meta_key in ("origin_instance", "published_at", "distilled_from_session_id"):
                if entry.get(meta_key) is not None:
                    meta[meta_key] = entry[meta_key]
            if meta:
                store.atomic_write_file(
                    f"{dest_dir}/meta.json",
                    json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"),
                )
            store.mark_quarantine_resolved(name, entry["content_sha256"])
            store.store_volume().commit()
            logger.info("sync_dashboard_skills: %s を quarantine から消し込んだ", name)
        except Exception as exc:  # noqa: BLE001 — 1 つの失敗で以後の同期を止めない
            logger.warning("sync_dashboard_skills: %s の quarantine 消し込み失敗（次回再試行）: %s", name, exc)

    # 手順3: ダッシュボード Volume も commit する（pull 側の書き込みの永続化）
    try:
        modal.Volume.from_name(_DASHBOARD_VOLUME_NAME).commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("sync_dashboard_skills: dashboard Volume commit 失敗: %s", exc)


@app.function(
    image=image,
    volumes={_DASHBOARD_MOUNT_PATH: modal.Volume.from_name(_DASHBOARD_VOLUME_NAME, create_if_missing=True)},
    secrets=[
        modal.Secret.from_name(_DASHBOARD_SECRET_NAME),
        modal.Secret.from_name(_NCAM_SECRET_NAME),  # ncam-memory hooks/MCPが実際のエージェント実行中に発火するため
    ],
    timeout=310,  # profile_oneshot.py 側の既定タイムアウト(300秒)に余裕を持たせる
)
def run_profile_oneshot(profile: str, prompt: str) -> dict:
    """Agentic OS HubのProfile Agent機能から、実プロフィールに対して1回だけ
    プロンプトを実行し応答を返す（R-2の完全ステートレス設計とは無関係な
    別経路。永続Volumeの実プロフィールに対して実行する点が
    hh-agent-dispatchとの違い）。
    """
    from pathlib import Path as _Path

    from modal_dashboard.profile_oneshot import run_profile_oneshot_sync

    return run_profile_oneshot_sync(profile, prompt, _Path(_DASHBOARD_MOUNT_PATH))


@app.function(
    image=image,
    volumes={_DASHBOARD_MOUNT_PATH: modal.Volume.from_name(_DASHBOARD_VOLUME_NAME, create_if_missing=True)},
    secrets=[
        modal.Secret.from_name(_DASHBOARD_SECRET_NAME),
        modal.Secret.from_name(_NCAM_SECRET_NAME),  # ncam-memory hooks/MCPが実際のエージェント実行中に発火するため
        modal.Secret.from_name(_CF_HUB_SECRET_NAME),
    ],
    timeout=70,  # cf_hub_dispatch.run_root_oneshot_syncの既定タイムアウト(60秒)に余裕を持たせる
)
@modal.fastapi_endpoint(method="POST", docs=True)
def cf_hub_dispatch(payload: dict, request: fastapi.Request) -> dict:
    """CF-Hermes-Hub（Cloudflare Durable ObjectsでDiscord Gatewayを維持する
    常時接続層）から、Discordのメンション/DM本文を受け取りHermesの応答を
    返す。設計はObsidian Projects/CF-Hermes-Hub参照。

    認証は共有シークレット（`CF_HUB_SHARED_SECRET`、Bearerヘッダ）のみの
    単純な方式。`/api/dispatch/headless`（modal_hub、Agentic OS Hub向け）の
    ような署名付きトークン発行の仕組みは持たない——呼び出し元がCloudflare
    Worker 1つに限定されており、値の受け渡しもwrangler secretで閉じている
    ため、この規模では単純な共有シークレットで十分と判断した（過剰設計を
    避ける）。

    リクエスト: {"message": str, "channel_id": str, "user_id": str}
    レスポンス: {"response": str}
    """
    import os

    from fastapi import HTTPException

    expected = os.environ.get("CF_HUB_SHARED_SECRET", "")
    auth_header = request.headers.get("authorization", "")
    provided = auth_header[len("Bearer ") :] if auth_header.startswith("Bearer ") else ""
    # 空文字同士の一致で通ってしまわないようにする（Secret未設定時のfail-closed）。
    if not expected or not provided or provided != expected:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=400, detail="message must be a non-empty string")

    from pathlib import Path as _Path

    from modal_dashboard.cf_hub_dispatch import run_root_oneshot_sync

    try:
        result = run_root_oneshot_sync(message, _Path(_DASHBOARD_MOUNT_PATH))
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"response": result["response"]}


def _ensure_agent_token_seeded(hermes_home: Path) -> None:
    """First-boot only: mint a token via a remote call if none exists yet.

    Deliberately does NOT sign locally -- this function runs inside
    dashboard_server(), which never has hh-agent-secret attached (C-3),
    so it has no signing key to sign with even if it wanted to. Calling
    refresh_dashboard_agent_token.remote() delegates issuance to the one
    function that does carry that credential, short-lived and isolated
    from the untrusted agent execution environment.
    """
    token_path = hermes_home / ".hh-agent" / "agent_token.json"
    if token_path.is_file():
        return
    refresh_dashboard_agent_token.remote()


@app.function(
    image=image,
    volumes={_DASHBOARD_MOUNT_PATH: modal.Volume.from_name(_DASHBOARD_VOLUME_NAME, create_if_missing=True)},
    secrets=[
        modal.Secret.from_name(_DASHBOARD_SECRET_NAME),  # NOT hh-agent-secret -- see module docstring, C-3
        modal.Secret.from_name(_CORPUS2SKILL_SECRET_NAME),  # CORPUS2SKILL_API_KEY only, see comment above
        modal.Secret.from_name(_NCAM_SECRET_NAME),  # ncam-memory hooks/MCP、config.yaml側の登録とセットで有効化
    ],
    min_containers=0,       # scale-to-zero -- cost floor is $0 (docs/hh-agent/08_Phase1c_Spec.md §2.2)
    max_containers=1,       # required -- see Global Constraints
    scaledown_window=300,
    timeout=86400,          # persistent interactive WS sessions, not a batch job (fixes I-2)
)
@modal.concurrent(max_inputs=8)  # dashboard needs several simultaneous WS connections per browser tab (fixes I-1)
@modal.web_server(port=_DASHBOARD_PORT, startup_timeout=90)
def dashboard_server():
    hermes_home = Path(_DASHBOARD_MOUNT_PATH)
    bootstrap.seed_config_yaml(hermes_home)
    # Cheap pre-flight check: fail fast with a clear error before even
    # spawning the subprocess. The REAL enforcement -- the one that
    # actually closes C-1 -- is hh_hermes.py's own enforce_or_exit(),
    # which runs inside the process that actually executes tools.
    bootstrap.verify_pretool_hooks_registered()
    _ensure_agent_token_seeded(hermes_home)

    # `--host 0.0.0.0` (not 127.0.0.1) is load-bearing: it's what makes
    # Hermes's own should_require_auth() correctly treat this as a public
    # bind (auth_required=True, refuses to boot without a registered
    # dashboard_auth provider -- HERMES_DASHBOARD_BASIC_AUTH_USERNAME/
    # _PASSWORD in hh-agent-dashboard-secret registers the bundled
    # `basic` provider) and sets app.state.bound_host so
    # _resolve_client_ws_host() substitutes 127.0.0.1 for the wildcard,
    # giving /api/pty a valid loopback URL to attach to this same
    # process instead of spawning a fresh, unverified tui_gateway.entry.
    # `--skip-build`: the web dist is already baked into the image at
    # Docker build time (§4 of the Dockerfile) -- no npm at runtime.
    subprocess.Popen(
        [
            "python", "/opt/hermes/hh_hermes.py", "dashboard",
            "--host", "0.0.0.0",
            "--port", str(_DASHBOARD_PORT),
            "--no-open",
            "--skip-build",
        ],
        cwd="/opt/hermes",
    )
