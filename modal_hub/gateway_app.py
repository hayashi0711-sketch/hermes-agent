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
from hermes_cli.telegram_managed_bot import TelegramPairing, create_pairing

_DASHBOARD_VOLUME_NAME = "hh-agent-dashboard-home"
_DASHBOARD_MOUNT_PATH = "/opt/data"
_HUB_SECRET_NAME = "hh-agent-secret"

app = modal.App("hh-agent-gateway")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DASHBOARD_DOCKERFILE_PATH = Path(__file__).resolve().parent.parent / "modal_dashboard" / "Dockerfile"

image = modal.Image.from_dockerfile(_DASHBOARD_DOCKERFILE_PATH, context_dir=_REPO_ROOT)


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
    secrets=[modal.Secret.from_name(_HUB_SECRET_NAME)],
    min_containers=1,
    timeout=86400,
)
async def run_gateway() -> None:
    await run_telegram_gateway_forever()


def pair_telegram_cli() -> str:
    """Telegramペアリングを開始し、ユーザーがTelegramアプリで承認するための
    ディープリンクを返す。ユーザーが自分でリンクを開いて承認するまで、
    このコマンドはコード側では何もトークンを保持・送信しない
    （docs/hh-agent/09_Telegram_Bot_Upstream_Merge_Design.md
    「外部サービス接続（課金リスク）に関する運用」節）。
    """
    pairing: TelegramPairing | None = create_pairing()
    if pairing is None:
        # create_pairing はネットワーク失敗・API拒否時に None を返す仕様。
        # None.deep_link で AttributeError になる暗黙の失敗を避け、明示的に
        # 失敗させる（feedback_silent_empty_fallback_hides_bugs）。
        raise RuntimeError(
            "Telegram pairing could not be started: onboarding API unreachable "
            "or rejected the request"
        )
    return pairing.deep_link


@app.function(
    image=image,
    volumes={_DASHBOARD_MOUNT_PATH: modal.Volume.from_name(_DASHBOARD_VOLUME_NAME, create_if_missing=True)},
    secrets=[modal.Secret.from_name(_HUB_SECRET_NAME)],
)
def _pair_telegram_remote() -> str:
    return pair_telegram_cli()


@app.local_entrypoint()
def pair_telegram() -> None:
    """`modal run modal_hub/gateway_app.py::pair_telegram` で1回だけ手動実行する。
    出力されたディープリンクをユーザーがTelegramアプリで開いて承認する。
    """
    link = _pair_telegram_remote.remote()
    print(f"Telegramで以下のリンクを開いて承認してください:\n{link}")
