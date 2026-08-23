"""gateway_app.py のテスト — Bot Gateway常駐Functionのコアロジック。

方針（test_approval_token.py と同型）:
    - Modal Function のデコレータ自体はテストしない（Modal SDK のローカル
      実行に依存するテストは重く壊れやすいため）。デコレータを剥がした
      コアロジック `run_telegram_gateway_forever()` を、`gateway.run.start_gateway`
      をモックした状態で直接呼び出して検証する。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hermes_cli.telegram_managed_bot import TelegramPairing
from modal_hub.gateway_app import (
    _DASHBOARD_MOUNT_PATH,
    _DASHBOARD_VOLUME_NAME,
    _HUB_SECRET_NAME,
    pair_telegram_cli,
    run_telegram_gateway_forever,
)


def test_volume_and_secret_names_match_dashboard_app():
    """独自定数がハードコードミスなく既存Appと同じ値を指しているか。"""
    assert _DASHBOARD_VOLUME_NAME == "hh-agent-dashboard-home"
    assert _DASHBOARD_MOUNT_PATH == "/opt/data"
    assert _HUB_SECRET_NAME == "hh-agent-secret"


@pytest.mark.asyncio
async def test_run_telegram_gateway_forever_calls_start_gateway():
    """コアロジックが gateway.run.start_gateway を呼び出すことを確認する。"""
    with patch(
        "modal_hub.gateway_app.start_gateway", new_callable=AsyncMock
    ) as mock_start:
        mock_start.return_value = True
        await run_telegram_gateway_forever()
    mock_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_telegram_gateway_forever_propagates_exceptions():
    """start_gateway が例外を投げたら握りつぶさず伝播することを確認する
    （feedback_silent_empty_fallback_hides_bugs と同種の教訓: Botが落ちて
    いるのにコンテナだけ生きているサイレント障害を防ぐ）。"""
    with patch(
        "modal_hub.gateway_app.start_gateway", new_callable=AsyncMock
    ) as mock_start:
        mock_start.side_effect = RuntimeError("relay connection refused")
        with pytest.raises(RuntimeError, match="relay connection refused"):
            await run_telegram_gateway_forever()


def test_pair_telegram_cli_returns_deep_link():
    """ペアリング開始APIを呼び、ユーザーに提示するディープリンク文字列を返すこと。"""
    fake_pairing = TelegramPairing(
        pairing_id="pair-1",
        poll_token="poll-token-1",
        suggested_username="hermes_abc_bot",
        deep_link="https://t.me/newbot/HermesAgentBot/hermes_abc_bot",
        qr_payload="https://t.me/newbot/HermesAgentBot/hermes_abc_bot",
    )
    with patch(
        "modal_hub.gateway_app.create_pairing", return_value=fake_pairing
    ) as mock_pair:
        result = pair_telegram_cli()
    mock_pair.assert_called_once()
    assert isinstance(result, str)
    assert len(result) > 0


def test_pair_telegram_cli_raises_when_pairing_unavailable():
    """create_pairing が None を返したら黙らず明示的なエラーにする
    （feedback_silent_empty_fallback_hides_bugs と同種の教訓: create_pairing は
    ネットワーク失敗やAPI拒否時に None を返す仕様のため、None.deep_link で
    AttributeError になるような暗黙の失敗を避ける）。"""
    with patch("modal_hub.gateway_app.create_pairing", return_value=None):
        with pytest.raises(RuntimeError, match="pairing"):
            pair_telegram_cli()
