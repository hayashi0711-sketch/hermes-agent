"""modal_hub/tests/test_ntfy_client.py — store 非依存 ntfy 送信クライアントの単体テスト。

設計書: docs/hh-agent/03_Architecture.md §14（S-11: 衝突通知・store 非依存）。
実 ntfy.sh へは接続せず、`ntfy_client.httpx.Client` を fake に差し替えて
検証する（test_notifier.py と同じ注入パターン）。

最低限のカバレッジ（タスク指示）:
- `send_skill_conflict` が通知本文に SKILL.md 相当の長文を含まないこと
- 非 ASCII Title/Tags を送信前に拒否すること
- `modal` を import していないこと（sys.modules から `modal` を除去した
  状態で import・実行できること）
"""

from __future__ import annotations

import importlib
import json
import sys

import httpx
import pytest

from modal_hub.services import ntfy_client
from modal_hub.tests.conftest import TEST_NTFY_TOKEN, TEST_NTFY_TOPIC

_CONFLICT_EVENT = {
    "event": "skill_conflict",
    "name": "my-skill",
    "winner": "win-abc123",
    "winner_sha8": "12345678",
    "loser_sha8": "87654321",
}


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.request = None


class FakeClient:
    """httpx.Client の fake。script を順に消費し、呼び出しを記録する。

    script の要素: 整数は応答 status に、Exception はそのまま raise に
    変換する。実 httpx と同じく 4xx/5xx の応答では raise しない
    （非 2xx/3xx は応答として返り、`send_via_ntfy` 側がリトライする）。
    """

    def __init__(self, script, sink, **kwargs) -> None:
        self._script = script
        self._sink = sink
        self._kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def post(self, url, *, headers, content):
        outcome = self._script.pop(0) if self._script else 200
        self._sink.append({
            "url": url,
            "headers": headers,
            "content": content,
            "timeout": self._kwargs.get("timeout"),
        })
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


@pytest.fixture()
def fake_ntfy(monkeypatch):
    sink, script = [], []

    def _client_factory(**kwargs):
        return FakeClient(script, sink, **kwargs)

    monkeypatch.setattr(ntfy_client.httpx, "Client", _client_factory)
    monkeypatch.setattr(ntfy_client.time, "sleep", lambda _seconds: None)
    return {"sink": sink, "script": script}


@pytest.fixture()
def ntfy_env(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", TEST_NTFY_TOPIC)
    monkeypatch.setenv("NTFY_TOKEN", TEST_NTFY_TOKEN)


# ---------------------------------------------------------------------------
# send_skill_conflict（S-11）
# ---------------------------------------------------------------------------


def test_send_skill_conflict_body_never_contains_skill_content(fake_ntfy, ntfy_env):
    long_skill_md = "---\nname: x\n---\n" + ("# 危険なスキル本文\n" * 100)
    result = ntfy_client.send_skill_conflict({
        **_CONFLICT_EVENT,
        # 万一呼び出し側が混ぜても本文には載らない（構造的担保）
        "content": long_skill_md,
        "diff": "--- a/x\n+++ b/x\n-dangerous\n",
    })
    assert result == "sent"

    (call,) = fake_ntfy["sink"]
    body = json.loads(call["content"].decode("utf-8"))
    assert set(body) == {"event", "name", "winner", "winner_sha8", "loser_sha8"}
    assert body == {
        "event": "skill_conflict",
        "name": "my-skill",
        "winner": "win-abc123",
        "winner_sha8": "12345678",
        "loser_sha8": "87654321",
    }
    blob = call["content"].decode("utf-8")
    assert "危険なスキル本文" not in blob
    assert "--- a/x" not in blob


def test_send_skill_conflict_url_and_headers(fake_ntfy, ntfy_env):
    assert ntfy_client.send_skill_conflict(dict(_CONFLICT_EVENT)) == "sent"
    (call,) = fake_ntfy["sink"]
    assert call["url"] == f"{ntfy_client.NTFY_BASE_URL}/{TEST_NTFY_TOPIC}"
    # Title / Tags ヘッダは ASCII（httpx が ascii でエンコードするため）
    assert call["headers"]["Title"] == ntfy_client.CONFLICT_TITLE
    assert call["headers"]["Title"].isascii()
    assert call["headers"]["Tags"].isascii()
    assert call["headers"]["Authorization"] == f"Bearer {TEST_NTFY_TOKEN}"
    assert call["timeout"] == ntfy_client.HTTP_TIMEOUT_SECONDS


def test_send_skill_conflict_requires_all_fields(fake_ntfy, ntfy_env):
    with pytest.raises(ValueError):
        ntfy_client.send_skill_conflict({"name": "my-skill"})


def test_send_skill_conflict_without_topic_fails_without_sending(fake_ntfy, monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    assert ntfy_client.send_skill_conflict(dict(_CONFLICT_EVENT)) == "failed"
    assert fake_ntfy["sink"] == []


def test_send_skill_conflict_maps_retry_exhaustion_to_failed(fake_ntfy, ntfy_env):
    fake_ntfy["script"].extend([500, 503, 502])
    assert ntfy_client.send_skill_conflict(dict(_CONFLICT_EVENT)) == "failed"
    assert len(fake_ntfy["sink"]) == ntfy_client.MAX_ATTEMPTS == 3


def test_send_skill_conflict_recovers_on_later_attempt(fake_ntfy, ntfy_env):
    fake_ntfy["script"].extend([500, 200])
    assert ntfy_client.send_skill_conflict(dict(_CONFLICT_EVENT)) == "sent"
    assert len(fake_ntfy["sink"]) == 2


# ---------------------------------------------------------------------------
# send_via_ntfy（低レベル。notifier._send_with_retries と同一挙動の踏襲）
# ---------------------------------------------------------------------------


def test_send_via_ntfy_rejects_non_ascii_title_without_sending(fake_ntfy):
    ok = ntfy_client.send_via_ntfy("topic-x", None, "承認待ちのタイトル", "message")
    assert ok is False
    assert fake_ntfy["sink"] == []


def test_send_via_ntfy_rejects_non_ascii_tags_without_sending(fake_ntfy):
    ok = ntfy_client.send_via_ntfy("topic-x", None, "Title", "message", tags=["警告"])
    assert ok is False
    assert fake_ntfy["sink"] == []


def test_send_via_ntfy_rejects_non_ascii_token_without_sending(fake_ntfy):
    ok = ntfy_client.send_via_ntfy("topic-x", "tk_日本語トークン", "Title", "message")
    assert ok is False
    assert fake_ntfy["sink"] == []


def test_send_via_ntfy_omits_authorization_when_token_is_none(fake_ntfy):
    assert ntfy_client.send_via_ntfy("topic-x", None, "Title", "message") is True
    (call,) = fake_ntfy["sink"]
    assert "Authorization" not in call["headers"]


def test_send_via_ntfy_sends_bearer_when_token_given(fake_ntfy):
    assert ntfy_client.send_via_ntfy("topic-x", "tk_secret_123456", "Title", "message") is True
    (call,) = fake_ntfy["sink"]
    assert call["headers"]["Authorization"] == "Bearer tk_secret_123456"


def test_send_via_ntfy_retries_up_to_three_times_then_fails(fake_ntfy):
    fake_ntfy["script"].extend([500, 503, 502])
    assert ntfy_client.send_via_ntfy("topic-x", None, "Title", "message") is False
    assert len(fake_ntfy["sink"]) == 3


def test_send_via_ntfy_retries_on_network_error(fake_ntfy):
    # httpx.HTTPError 系（接続失敗等）もリトライ対象（notifier と同じ）
    fake_ntfy["script"].extend([httpx.ConnectError("boom"), 200])
    assert ntfy_client.send_via_ntfy("topic-x", None, "Title", "message") is True
    assert len(fake_ntfy["sink"]) == 2


def test_send_via_ntfy_treats_3xx_as_success(fake_ntfy):
    fake_ntfy["script"].extend([302, 200])  # 3xx で成功扱い（notifier と同じ）
    assert ntfy_client.send_via_ntfy("topic-x", None, "Title", "message") is True
    assert len(fake_ntfy["sink"]) == 1


# ---------------------------------------------------------------------------
# store 非依存（modal を import しないこと。必須要件）
# ---------------------------------------------------------------------------


def test_ntfy_client_imports_and_runs_without_modal(monkeypatch, fake_ntfy, ntfy_env):
    """`modal` を import 不能にした状態でも import・実行できること。

    `sys.modules["modal"] = None` を差し込むと、そのセッションで `import
    modal` は ImportError になる（モジュール再実行のハードブロック）。
    この状態で ntfy_client を素の状態から import し、`send_skill_conflict`
    が例外なく `"sent"` を返せれば、ntfy_client が modal に依存しない
    ことの証明になる。
    """
    monkeypatch.setitem(sys.modules, "modal", None)  # import をハードブロック
    # 既に import 済みなら破棄して素の状態から import し直す
    monkeypatch.delitem(sys.modules, "modal_hub.services.ntfy_client", raising=False)
    fresh = importlib.import_module("modal_hub.services.ntfy_client")

    assert fresh.send_skill_conflict(dict(_CONFLICT_EVENT)) == "sent"
    (call,) = fake_ntfy["sink"]
    assert json.loads(call["content"].decode("utf-8"))["event"] == "skill_conflict"


def test_ntfy_client_module_imports_are_modal_free():
    """モジュールが読む import 先に modal が含まれないことの静的確認。"""
    import ast
    from pathlib import Path

    source = Path(ntfy_client.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.append(node.module)
    joined = "\n".join(imported_names)
    assert "modal" not in joined, f"ntfy_client.py が modal を import している: {joined}"
