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

from hermes_cli.telegram_managed_bot import TelegramBotSetupResult
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


def test_pair_telegram_cli_returns_setup_result():
    """ペアリング作成→承認待ちポーリング→トークン取得までを行い、結果を返すこと。"""
    fake_result = TelegramBotSetupResult(
        token="123456:ABCdefGHIjklMNOpqrSTUvwxYZ-1234567890",
        bot_username="hermes_abc_bot",
        owner_user_id=987654321,
    )
    with patch(
        "modal_hub.gateway_app.auto_setup_telegram_bot_result", return_value=fake_result
    ) as mock_setup:
        result = pair_telegram_cli()
    mock_setup.assert_called_once()
    assert result is fake_result
    assert result.token == fake_result.token


def test_pair_telegram_cli_raises_when_pairing_unavailable():
    """auto_setup_telegram_bot_result が None を返したら黙らず明示的なエラーにする
    （feedback_silent_empty_fallback_hides_bugs と同種の教訓: オンボーディングAPI
    への到達失敗・承認タイムアウトのどちらでも None が返る仕様のため、
    None.token で AttributeError になるような暗黙の失敗を避ける）。"""
    with patch("modal_hub.gateway_app.auto_setup_telegram_bot_result", return_value=None):
        with pytest.raises(RuntimeError, match="pairing"):
            pair_telegram_cli()
