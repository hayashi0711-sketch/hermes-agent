"""Bot Gateway 常駐Modal App — Telegram等プラットフォームとのリレー接続を維持する。

`gateway.run.start_gateway()` は Nous Research の中継サーバーへ WebSocket で
常時接続し続ける長寿命コルーチンで、リクエスト駆動のサーバーレス実行とは
相性が悪い（docs/hh-agent/09_Telegram_Bot_Upstream_Merge_Design.md §デプロイ方式）。
そのためこの App は独立させ、`min_containers=1` の常駐 Function として動かす。

既存 `hh-agent-hub`（modal_hub/main.py）・`hh-agent-dashboard`
（modal_dashboard/app.py）とは疎結合（R-2以来の方針）: Volume名・Secret名は
値として再定義するのみで、モジュールをまたいだ import はしない。
"""

from __future__ import annotations

from pathlib import Path

import modal

from gateway.run import start_gateway
from hermes_cli.telegram_managed_bot import TelegramBotSetupResult, auto_setup_telegram_bot_result

_DASHBOARD_VOLUME_NAME = "hh-agent-dashboard-home"
_DASHBOARD_MOUNT_PATH = "/opt/data"
_HUB_SECRET_NAME = "hh-agent-secret"
# NCAM連携用(2026-09-05新規)。modal_dashboard/app.pyの同名定数・同じコメント参照。
_NCAM_SECRET_NAME = "ncam-daemon-secret"

app = modal.App("hh-agent-gateway")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DASHBOARD_DOCKERFILE_PATH = Path(__file__).resolve().parent.parent / "modal_dashboard" / "Dockerfile"
# NCAMパス解決・バージョンpinの方針はmodal_dashboard/app.pyの同名定数・コメント参照
# (存在確認を import 時に行わない理由も同様: add_local_dir は遅延評価されるため
# ../NCAM を持たない環境でのテストimportを壊さない)。
_NCAM_REPO_ROOT = _REPO_ROOT.parent / "NCAM"

image = (
    modal.Image.from_dockerfile(_DASHBOARD_DOCKERFILE_PATH, context_dir=_REPO_ROOT)
    # Image.pip_install()はこのイメージのvenv(pip自体を含まない)では失敗する
    # ("No module named pip", 2026-09-05実機確認)。modal_dashboard/app.pyの
    # 同名コメント参照 -- uv pip installを使う。
    .run_commands(
        "uv pip install "
        "duckdb==1.5.5 pytz==2026.3.post1 numpy==2.4.3 mcp==2.0.0 "
        "fastapi==0.133.1 'uvicorn[standard]==0.41.0' pydantic==2.13.4"
    )
    .add_local_dir(str(_NCAM_REPO_ROOT / "ncam"), remote_path="/opt/ncam-pkg/ncam", copy=True)
    .add_local_dir(str(_NCAM_REPO_ROOT / "ncam_hooks"), remote_path="/opt/ncam-pkg/ncam_hooks", copy=True)
    .env({"PYTHONPATH": "/opt/ncam-pkg"})
)


async def run_telegram_gateway_forever() -> None:
    """Bot Gatewayを起動し、プロセスが生きている限り接続を維持する。

    `start_gateway()` が False を返す、または例外を投げた場合は、コンテナが
    "生きているのにBotだけ死んでいる" サイレント障害を避けるため、そのまま
    伝播させる（コンテナが再起動され、Modal側のリトライに委ねる）。
    """
    await start_gateway()


@app.function(
    image=image,
    volumes={_DASHBOARD_MOUNT_PATH: modal.Volume.from_name(_DASHBOARD_VOLUME_NAME, create_if_missing=True)},
    secrets=[
        modal.Secret.from_name(_HUB_SECRET_NAME),
        modal.Secret.from_name(_NCAM_SECRET_NAME),  # ncam-memory hooks/MCPが実際のBot応答生成中に発火するため
    ],
    min_containers=1,
    timeout=86400,
)
async def run_gateway() -> None:
    await run_telegram_gateway_forever()


def pair_telegram_cli() -> TelegramBotSetupResult:
    """Telegramペアリングを開始し、ディープリンク表示→ユーザーの承認待ちポーリング→
    トークン取得までを1回のリモート実行内で完結させる。

    2026-08-23修正: 当初はディープリンクを返すだけで、ユーザーが承認した後の
    トークン取得・`.env`への保存を一切していなかった（設計・実装計画の抜け）。
    ユーザーがTelegram側で「Create a BOT」を押しても何も起きないように見える
    バグの原因だった。`auto_setup_telegram_bot_result()`（`hermes setup`の
    Telegram自動セットアップが実際に使っている関数）へ差し替え、ペアリング作成
    →ディープリンク表示→最大180秒のポーリング→トークン取得までを1呼び出しで
    行うようにした。
    """
    result = auto_setup_telegram_bot_result()
    if result is None:
        # オンボーディングAPIへ到達できない、またはユーザーが確認しないまま
        # タイムアウトした場合。None.token で AttributeError になる暗黙の失敗を
        # 避け、明示的に失敗させる（feedback_silent_empty_fallback_hides_bugs）。
        raise RuntimeError(
            "Telegram pairing did not complete: onboarding API unreachable, "
            "the request was rejected, or the 180-second approval window expired "
            "(re-run this command to get a fresh link)"
        )
    return result


@app.function(
    image=image,
    volumes={_DASHBOARD_MOUNT_PATH: modal.Volume.from_name(_DASHBOARD_VOLUME_NAME, create_if_missing=True)},
    secrets=[modal.Secret.from_name(_HUB_SECRET_NAME)],
    timeout=240,
)
def _pair_telegram_remote() -> dict:
    from hermes_cli.config import save_env_value

    result = pair_telegram_cli()
    save_env_value("TELEGRAM_BOT_TOKEN", result.token)
    allowed_user = None
    if result.owner_user_id:
        allowed_user = str(result.owner_user_id)
        save_env_value("TELEGRAM_ALLOWED_USERS", allowed_user)
    return {
        "bot_username": result.bot_username,
        "allowed_user": allowed_user,
    }


@app.local_entrypoint()
def pair_telegram() -> None:
    """`modal run modal_hub/gateway_app.py::pair_telegram` で1回だけ手動実行する。
    表示されたディープリンクをTelegramアプリで開いて「Create a BOT」で承認すると、
    このコマンド自身が承認完了を検知してトークンを`.env`へ保存する（最大180秒待機）。
    保存後、常駐App（run_gateway）が新しいトークンを読み込むには再デプロイ
    （`modal deploy modal_hub/gateway_app.py`）が必要（既存コンテナはVolumeの
    変更を自動では読み直さないため）。
    """
    result = _pair_telegram_remote.remote()
    print(f"✓ Telegram Bot作成完了: @{result['bot_username']}")
    if result["allowed_user"]:
        print(f"✓ 許可ユーザーを設定: {result['allowed_user']}")
    else:
        print("⚠️ 許可ユーザーIDを自動検出できませんでした。誰でもこのBotを使える状態です。")
    print("次: `modal deploy modal_hub/gateway_app.py` を再実行し、常駐Appへ新しいトークンを反映してください。")
