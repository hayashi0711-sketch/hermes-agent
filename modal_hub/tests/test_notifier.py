"""`modal_hub/services/notifier.py` — ntfy.sh 通知。

親設計書 §5.2「通知経路には権限を載せない」・§4.3「通知の送達保証」、
Phase1a spec §1.2 step 7 / §1.3 の `notify_state` 語彙。

**最重要**: 通知本文にコマンド本文・パス・差分を一切載せないこと。
本設計で秘密が外部へ漏れないことを担保しているのは redaction ではなく
この構造である（spec §10.3 末尾）。
"""

from __future__ import annotations

import json
import logging

import pytest

from modal_hub.core import store
from modal_hub.services import notifier
from modal_hub.tests.conftest import TEST_NTFY_TOKEN, TEST_NTFY_TOPIC


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.request = None


class FakeClient:
    """`httpx.Client` のコンテキストマネージャ模倣。"""

    def __init__(self, script, sink, **kwargs) -> None:
        self._script = script
        self._sink = sink
        self.timeout = kwargs.get("timeout")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def post(self, url, headers=None, content=None):
        self._sink.append({"url": url, "headers": headers or {}, "content": content})
        outcome = self._script.pop(0) if self._script else 200
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


@pytest.fixture()
def ntfy(monkeypatch, secret_env, fake_dict):
    """httpx をモックし、送信内容を記録する。`time.sleep` も潰す。"""
    sink: list[dict] = []
    script: list = []

    monkeypatch.setattr(
        notifier.httpx, "Client", lambda **kwargs: FakeClient(script, sink, **kwargs)
    )
    monkeypatch.setattr(notifier.time, "sleep", lambda _s: None)
    return {"sink": sink, "script": script}


APPROVAL_ID = "11111111-1111-4111-8111-111111111111"


# ===========================================================================
# 通知本文に権限も機密も載せない（親設計書 §5.2 — 最重要）
# ===========================================================================


def test_body_contains_only_approval_id_and_risk(ntfy) -> None:
    notifier.send_approval_request(APPROVAL_ID, "HIGH")
    (call,) = ntfy["sink"]
    body = json.loads(call["content"].decode("utf-8"))
    assert body == {"approval_id": APPROVAL_ID, "risk": "HIGH"}


def test_build_body_never_accepts_a_payload() -> None:
    """シグネチャに payload/command/path を受け取る余地が無いこと。"""
    import inspect

    assert list(inspect.signature(notifier._build_body).parameters) == ["approval_id", "risk"]
    assert list(inspect.signature(notifier.send_approval_request).parameters) == ["approval_id", "risk"]


def test_notification_never_carries_the_command_text(ntfy) -> None:
    """ntfy 運営者・通知ログ・ロック画面に露出する経路を作らない。"""
    notifier.send_approval_request(APPROVAL_ID, "HIGH")
    (call,) = ntfy["sink"]
    blob = json.dumps({"h": {k: v for k, v in call["headers"].items() if k != "Authorization"},
                       "c": call["content"].decode("utf-8")}, ensure_ascii=False)
    for forbidden in ("rm -rf", "git push", "command", "payload", "diff", "cwd", "path"):
        assert forbidden not in blob, f"通知に {forbidden!r} が載っている"


def test_title_is_a_fixed_string_with_no_interpolation() -> None:
    assert "{" not in notifier.NOTIFICATION_TITLE and "%" not in notifier.NOTIFICATION_TITLE


def test_title_is_ascii_only() -> None:
    """**2026-08-11 障害の回帰防止**.

    httpx は HTTP ヘッダ値を ascii でエンコードする。``NOTIFICATION_TITLE`` に
    CJK 等の非 ASCII 文字が混ざっていると ``client.post`` の中で
    ``UnicodeEncodeError`` が送出され、``except httpx.HTTPError`` で捕捉されず
    ``send_approval_request`` まで素のまま伝播する。タイトルを ASCII 固定する
    ことでこの抜け道を塞ぐ。コメント（``notifier.py`` 103 行周辺）に同じ理由が
    日本語で記録されている。
    """
    try:
        notifier.NOTIFICATION_TITLE.encode("ascii")
    except UnicodeEncodeError as exc:
        pytest.fail(
            f"NOTIFICATION_TITLE contains non-ASCII characters "
            f"(httpx will raise UnicodeEncodeError on send): {exc!r}"
        )


def test_send_approval_request_does_not_raise_unicode_encode_error(ntfy) -> None:
    """**2026-08-11 障害の回帰防止**.

    httpx の実際の header エンコードが ascii で失敗しないこと、および
    ``send_approval_request`` が ``UnicodeEncodeError`` をそのまま投げないこと
    （戻り値は ``"sent"`` か ``"failed"`` のみという 05 §1.2 step 7 の
    fail-closed 約束）。
    """
    state = notifier.send_approval_request(APPROVAL_ID, "HIGH")
    assert state in ("sent", "failed")
    (call,) = ntfy["sink"]
    notifier.httpx.Request(
        "POST", "https://ntfy.sh/x", headers=call["headers"]
    )


def test_topic_is_treated_as_a_secret_and_never_logged(ntfy, caplog) -> None:
    """§5.2: トピック名自体を秘密として扱う（Secret に格納・ログに書かない）。"""
    ntfy["script"].extend([500, 500, 500])
    with caplog.at_level(logging.DEBUG, logger="hh_agent.notifier"):
        notifier.send_approval_request(APPROVAL_ID, "HIGH")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert TEST_NTFY_TOPIC not in logged
    assert TEST_NTFY_TOKEN not in logged


def test_token_is_sent_as_a_bearer_header_not_in_the_url(ntfy) -> None:
    notifier.send_approval_request(APPROVAL_ID, "HIGH")
    (call,) = ntfy["sink"]
    assert call["headers"]["Authorization"] == f"Bearer {TEST_NTFY_TOKEN}"
    assert TEST_NTFY_TOKEN not in call["url"]
    assert call["url"] == f"{notifier.NTFY_BASE_URL}/{TEST_NTFY_TOPIC}"


def test_non_ascii_token_fails_closed_without_http_send(
    ntfy, monkeypatch, caplog
) -> None:
    token = "non-ascii-token-秘密"
    monkeypatch.setenv(notifier.config.NTFY_TOKEN, token)

    with caplog.at_level(logging.ERROR, logger="hh_agent.notifier"):
        state = notifier.send_approval_request(APPROVAL_ID, "HIGH")

    assert state == "failed"
    assert ntfy["sink"] == []
    record = store.get(store.notify_key(APPROVAL_ID))
    assert record["state"] == "failed"
    assert record["attempts"] == notifier.MAX_ATTEMPTS
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "Authorization credential rejected as non-ASCII" in logged
    assert token not in logged


@pytest.mark.parametrize("set_empty_string", [False, True])
def test_no_authorization_header_when_token_is_unset(ntfy, monkeypatch, set_empty_string) -> None:
    """NTFY_TOKEN は任意（2026-08-11 決定: 公開トピック運用では空）。

    空の Bearer トークンを送るのではなく、Authorization ヘッダそのものを
    省く。03_Architecture.md §5.2「ntfy には権限を一切載せない」は
    公開トピックモードでも成り立たなければならない。実際の障害
    （2026-08-11 Modal 再デプロイ）は Secret のキー自体が ``NTFY_TOKEN=""``
    （空文字列）で設定されていたケースなので、変数が存在しないケースだけで
    なく空文字列のケースも別途固定する（Codex レビュー指摘）。
    """
    if set_empty_string:
        monkeypatch.setenv(notifier.config.NTFY_TOKEN, "")
    else:
        monkeypatch.delenv(notifier.config.NTFY_TOKEN, raising=False)
    notifier.send_approval_request(APPROVAL_ID, "HIGH")
    (call,) = ntfy["sink"]
    assert "Authorization" not in call["headers"]


# ===========================================================================
# 送達保証（§4.3: 最大 3 回・指数バックオフ）
# ===========================================================================


def test_single_success_sends_once(ntfy) -> None:
    assert notifier.send_approval_request(APPROVAL_ID, "HIGH") == "sent"
    assert len(ntfy["sink"]) == 1


def test_retries_up_to_three_times_then_fails(ntfy) -> None:
    ntfy["script"].extend([500, 503, 502])
    assert notifier.send_approval_request(APPROVAL_ID, "HIGH") == "failed"
    assert len(ntfy["sink"]) == notifier.MAX_ATTEMPTS == 3


def test_recovers_on_a_later_attempt(ntfy) -> None:
    ntfy["script"].extend([500, 200])
    assert notifier.send_approval_request(APPROVAL_ID, "HIGH") == "sent"
    assert len(ntfy["sink"]) == 2


def test_network_errors_are_retried(ntfy) -> None:
    import httpx

    ntfy["script"].extend([httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), 200])
    assert notifier.send_approval_request(APPROVAL_ID, "HIGH") == "sent"


def test_backoff_is_bounded(ntfy, monkeypatch) -> None:
    """§1.1 共通規約: 初回 0.5 秒・以後 2 倍・上限 4 秒。"""
    slept: list[float] = []
    monkeypatch.setattr(notifier.time, "sleep", slept.append)
    ntfy["script"].extend([500, 500, 500])
    notifier.send_approval_request(APPROVAL_ID, "HIGH")
    assert notifier.INITIAL_BACKOFF_SECONDS == 0.5
    assert notifier.MAX_BACKOFF_SECONDS == 4.0
    assert all(0 <= s <= notifier.MAX_BACKOFF_SECONDS * 1.5 for s in slept), slept


def test_http_timeout_is_explicit(ntfy) -> None:
    """タイムアウト無しの HTTP 呼び出しはコンテナごとハングする。"""
    assert notifier.HTTP_TIMEOUT_SECONDS > 0


# ===========================================================================
# `notify:` の write-once 意味論（§1.2「更新という語に引きずられないこと」）
# ===========================================================================


def test_notify_state_is_written_once(ntfy) -> None:
    ntfy["script"].extend([500, 500, 500])
    assert notifier.send_approval_request(APPROVAL_ID, "HIGH") == "failed"
    record = store.get(store.notify_key(APPROVAL_ID))
    assert record["state"] == "failed"
    assert record["attempts"] == 3


def test_failed_state_is_sticky(ntfy) -> None:
    """§1.2: 3 回の送信がすべて失敗して `failed` が書かれたら以後変わらない。

    仕様どおりの挙動（フェイルクローズ）。poll が `notify_failed` を返し
    エージェントは deny する。復旧はユーザーがコマンドを実行し直すこと。
    """
    ntfy["script"].extend([500, 500, 500])
    notifier.send_approval_request(APPROVAL_ID, "HIGH")
    ntfy["script"].append(200)
    notifier.send_approval_request(APPROVAL_ID, "HIGH")
    assert store.get(store.notify_key(APPROVAL_ID))["state"] == "failed"


def test_already_sent_is_a_noop(ntfy) -> None:
    notifier.send_approval_request(APPROVAL_ID, "HIGH")
    assert len(ntfy["sink"]) == 1
    assert notifier.send_approval_request(APPROVAL_ID, "HIGH") == "sent"
    assert len(ntfy["sink"]) == 1, "既に sent なのに再送した"


def test_notifier_uses_only_put_if_absent(ntfy, fake_dict) -> None:
    """store に overwrite が無い以上、`notify:` も 1 回勝負でなければならない。"""
    notifier.send_approval_request(APPROVAL_ID, "HIGH")
    notify_puts = [(k, skip) for k, skip in fake_dict.put_calls if k.startswith("notify:")]
    assert notify_puts and all(skip is True for _k, skip in notify_puts)


@pytest.mark.parametrize("state", ["pending", "queued", "", "SENT", None])
def test_invalid_notify_state_is_rejected(ntfy, state) -> None:
    """`notify_state` の語彙は閉じている（§1.3）。typo を黙って書かせない。"""
    with pytest.raises(notifier.NotifyError):
        notifier._record_notify_state(APPROVAL_ID, state, 1)


def test_returned_state_uses_the_shared_vocabulary(ntfy) -> None:
    """§1.2: `notify_failed` のような別名を作らない。"""
    assert notifier.send_approval_request(APPROVAL_ID, "HIGH") in ("sent", "failed")


# ===========================================================================
# Secret 不在はそのまま伝播させる（NotifyError に変換しない）
# ===========================================================================


def test_missing_secret_propagates_as_secret_missing_error(monkeypatch, fake_dict) -> None:
    from modal_hub.core import config

    monkeypatch.delenv(config.NTFY_TOPIC, raising=False)
    monkeypatch.delenv(config.NTFY_TOKEN, raising=False)
    with pytest.raises(config.SecretMissingError):
        notifier.send_approval_request(APPROVAL_ID, "HIGH")
