r"""modal_hub/tests/test_skill_sync.py — Lane C クライアント（skill_sync.py）の単体テスト。

設計書: docs/hh-agent/03_Architecture.md §14（S-06b / S-08 / S-10 / S-11）。
実 Corpus2Skill へは接続せず、`skill_sync._urlopen` を fake に差し替えて
検証する（タスク指示: fake な HTTP クライアント/レスポンスを注入する形）。

最低限のカバレッジ:
- receipt 生成→検証の往復と、署名対象の各フィールド改変の検出
- receipt 形式の正規表現チェック（`^[0-9a-f]{8}\.[A-Za-z0-9_-]{43}\Z`）
- 分類関数の表（noop/pull/push/conflict/metadata_repair）の代表ケース
- フェーズ A の整合性異常（revision の逆行・型不正・watermark 矛盾）で
  分類フェーズへ進まないこと
- `ensure_ascii=False` でのエンコード・64KB/256KB 上限の送信前チェック
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error

import pytest

from modal_hub.services import skill_sync
from modal_hub.services.skill_sync import (
    IntegrityAnomalyError,
    LocalSkillState,
    PushResult,
    RemoteSkillState,
    SyncValidationError,
)

# ---------------------------------------------------------------------------
# 共通データ
# ---------------------------------------------------------------------------

_SKILL_MD = (
    "---\n"
    "name: my-skill\n"
    "description: sample skill for lane C tests\n"
    "---\n"
    "# My Skill\n"
    "Handles things.\n"
)
_DIGEST = hashlib.sha256(_SKILL_MD.encode("utf-8")).hexdigest()

_KEY = b"k" * 32
_KEY_ID = hashlib.sha256(_KEY).hexdigest()[:8]
_KEY_PREV = b"p" * 32
_KEY_PREV_ID = hashlib.sha256(_KEY_PREV).hexdigest()[:8]

BASE_URL = "https://c2s.example"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_receipt(
    name: str = "my-skill",
    content: str = _SKILL_MD,
    digest: str = _DIGEST,
    seq: int = 7,
    promoted_at_ms: int = 1_755_300_000_123,
    origin_instance: str = "win-abc123",
    distilled_from_session_id: str | None = "sess_1",
    signing_key: bytes = _KEY,
    key_id: str = _KEY_ID,
) -> str:
    return skill_sync.write_receipt(
        name, content, digest, seq, promoted_at_ms, origin_instance,
        distilled_from_session_id, signing_key=signing_key, key_id=key_id,
    )


# ---------------------------------------------------------------------------
# fake HTTP
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _http_error(status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://fake", status, "err", {}, io.BytesIO(body))


class FakeUrlopen:
    """skill_sync._urlopen の fake。script を順に消費し、呼び出しを記録する。

    script の要素: (status, body_bytes) は応答に、Exception はそのまま raise
    に変換する。4xx/5xx は本物の urllib と同じく HTTPError として投げる。
    """

    def __init__(self) -> None:
        self.calls: list = []  # [(req, timeout)]
        self.script: list = []

    def __call__(self, req, timeout=None):
        self.calls.append((req, timeout))
        outcome = self.script.pop(0) if self.script else (200, b"{}")
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome
        if status >= 400:
            raise _http_error(status, body)
        return _FakeHTTPResponse(status, body)


@pytest.fixture()
def fake_http(monkeypatch):
    fake = FakeUrlopen()
    monkeypatch.setattr(skill_sync, "_urlopen", fake)
    return fake


# ---------------------------------------------------------------------------
# promote receipt（S-06b）
# ---------------------------------------------------------------------------


def test_receipt_roundtrip_and_tamper_detection():
    receipt = _make_receipt()
    assert skill_sync.is_valid_receipt_format(receipt)
    assert skill_sync.verify_receipt(
        receipt, "my-skill", _DIGEST, "win-abc123",
        1_755_300_000_123, 7, "sess_1",
        verify_keys={_KEY_ID: _KEY},
    )

    # 署名対象タプルのいずれかを 1 つでも変えると検証に失敗する
    tampered = [
        ("name", "other-name", _DIGEST, "win-abc123", 1_755_300_000_123, 7, "sess_1"),
        ("sha", "my-skill", "0" * 64, "win-abc123", 1_755_300_000_123, 7, "sess_1"),
        ("origin", "my-skill", _DIGEST, "win-OTHER", 1_755_300_000_123, 7, "sess_1"),
        ("time", "my-skill", _DIGEST, "win-abc123", 1_755_300_000_124, 7, "sess_1"),
        ("seq", "my-skill", _DIGEST, "win-abc123", 1_755_300_000_123, 8, "sess_1"),
        ("session", "my-skill", _DIGEST, "win-abc123", 1_755_300_000_123, 7, "sess_2"),
    ]
    for label, n, s, o, t, q, d in tampered:
        assert not skill_sync.verify_receipt(
            receipt, n, s, o, t, q, d, verify_keys={_KEY_ID: _KEY}
        ), f"tampered field should fail verification: {label}"


def test_receipt_null_session_is_equivalent_to_empty_string_in_signature():
    receipt = _make_receipt(distilled_from_session_id=None)
    # None と "" は同じ canonical 表現になる（canonical 末尾が `or ""`）
    assert skill_sync.verify_receipt(
        receipt, "my-skill", _DIGEST, "win-abc123",
        1_755_300_000_123, 7, "",
        verify_keys={_KEY_ID: _KEY},
    )


def test_receipt_format_regex():
    receipt = _make_receipt()
    assert skill_sync.is_valid_receipt_format(receipt)
    bad = [
        "",
        "123",                                     # ドットなし
        "abcdefgh.ABC",                            # 署名部が短すぎ
        "12345678." + "A" * 42,                    # 42 文字（43 でない）
        "12345678." + "A" * 44,                    # 44 文字
        "ABCDEF12." + "A" * 43,                    # key_id に大文字
        "1234567." + "A" * 43,                     # key_id が 7 桁
        "12345678." + "A" * 43 + "=",              # パディング付き base64
        "12345678." + "+" * 43,                    # base64url 外の文字
        "12345678." + "a" * 43 + "\n",             # 末尾改行（\Z で拒否）
    ]
    for value in bad:
        assert not skill_sync.is_valid_receipt_format(value), f"should be invalid: {value!r}"
        # 形式不正は verify も False（例外を投げない）
        assert not skill_sync.verify_receipt(
            value, "my-skill", _DIGEST, "win-abc123",
            1_755_300_000_123, 7, "sess_1",
            verify_keys={_KEY_ID: _KEY},
        ), f"verify should reject invalid format: {value!r}"


def test_receipt_wrong_key_fails_and_rotation_allows_old_key():
    receipt = _make_receipt(signing_key=_KEY, key_id=_KEY_ID)
    # 鍵が違えば検証できない（key_id から引いた鍵で署名照合するため）
    assert not skill_sync.verify_receipt(
        receipt, "my-skill", _DIGEST, "win-abc123",
        1_755_300_000_123, 7, "sess_1",
        verify_keys={_KEY_PREV_ID: _KEY_PREV},
    )
    # 旧世代の鍵で署名した receipt は、verify_keys に旧 key_id を残している
    # 限り検証できる（鍵の世代交代）
    old_receipt = _make_receipt(
        seq=3, signing_key=_KEY_PREV, key_id=_KEY_PREV_ID,
    )
    assert skill_sync.verify_receipt(
        old_receipt, "my-skill", _DIGEST, "win-abc123",
        1_755_300_000_123, 3, "sess_1",
        verify_keys={_KEY_ID: _KEY, _KEY_PREV_ID: _KEY_PREV},
    )
    # 旧鍵を保持していなければ検証できない
    assert not skill_sync.verify_receipt(
        old_receipt, "my-skill", _DIGEST, "win-abc123",
        1_755_300_000_123, 3, "sess_1",
        verify_keys={_KEY_ID: _KEY},
    )


def test_receipt_signature_uses_security_helpers():
    """HMAC が security._hmac_sha256 / _b64url_encode と同じ値を生むこと。"""
    from modal_hub.core import security

    receipt = _make_receipt()
    key_id, sig_b64 = receipt.split(".", 1)
    assert key_id == _KEY_ID
    canonical = (
        "hhskill1|" + key_id + "|my-skill|" + _DIGEST + "|win-abc123|"
        "1755300000123|7|sess_1"
    ).encode("utf-8")
    assert sig_b64 == security._b64url_encode(security._hmac_sha256(_KEY, canonical))


def test_write_receipt_rejects_float_timestamp():
    with pytest.raises(ValueError):
        _make_receipt(promoted_at_ms=1_755_300_000_123.0)


def test_write_receipt_rejects_bool_and_negative_seq():
    with pytest.raises(ValueError):
        _make_receipt(seq=True)
    with pytest.raises(ValueError):
        _make_receipt(seq=-1)


def test_write_receipt_rejects_key_id_mismatch():
    with pytest.raises(ValueError):
        _make_receipt(key_id=_KEY_PREV_ID)  # key_id が鍵の sha256 先頭 8 桁でない


def test_write_receipt_rejects_digest_mismatch():
    with pytest.raises(ValueError):
        _make_receipt(digest="0" * 64)  # 実測 sha256 と一致しない


def test_write_receipt_computes_digest_when_not_given():
    receipt = skill_sync.write_receipt(
        "my-skill", _SKILL_MD, None, 7, 1_755_300_000_123, "win-abc123", "sess_1",
        signing_key=_KEY, key_id=_KEY_ID,
    )
    assert skill_sync.verify_receipt(
        receipt, "my-skill", _DIGEST, "win-abc123",
        1_755_300_000_123, 7, "sess_1",
        verify_keys={_KEY_ID: _KEY},
    )


def test_derive_key_id():
    assert skill_sync.derive_key_id(_KEY) == hashlib.sha256(_KEY).hexdigest()[:8]


def test_verify_receipt_rejects_float_and_wrong_type_fields():
    receipt = _make_receipt()
    assert not skill_sync.verify_receipt(
        receipt, "my-skill", _DIGEST, "win-abc123",
        1_755_300_000_123.0, 7, "sess_1",
        verify_keys={_KEY_ID: _KEY},
    )
    assert not skill_sync.verify_receipt(
        receipt, "my-skill", _DIGEST, "win-abc123",
        1_755_300_000_123, 7, 123,  # distilled_from_session_id が非文字列
        verify_keys={_KEY_ID: _KEY},
    )


# ---------------------------------------------------------------------------
# HTTP クライアント（push / list / pull / ack。S-08 / S-10）
# ---------------------------------------------------------------------------


def _push_args(receipt: str = None, skill_md: str = _SKILL_MD, **overrides):
    args = dict(
        name="my-skill",
        skill_md=skill_md,
        content_sha256=_sha256_hex(skill_md),
        promoted_at_ms=1_755_300_000_123,
        origin_instance="win-abc123",
        distilled_from_session_id="sess_1",
        promotion_seq=7,
        receipt=receipt or _make_receipt(),
        base_revision=0,
        base_url=BASE_URL,
        write_key="wk",
    )
    args.update(overrides)
    return args


def test_push_skill_success(fake_http):
    fake_http.script.append(
        (200, json.dumps({
            "revision": 9,
            "received_at": "2026-08-16T00:00:00Z",
            "replaced_content_sha256": None,
        }).encode("utf-8"))
    )
    result = skill_sync.push_skill(**_push_args())
    assert isinstance(result, PushResult)
    assert result.sent and not result.conflict
    assert result.revision == 9
    assert result.received_at == "2026-08-16T00:00:00Z"
    assert result.replaced_content_sha256 is None

    (req, timeout) = fake_http.calls[0]
    assert req.get_method() == "POST"
    assert req.full_url == f"{BASE_URL}/api/skills/push"
    assert req.headers["Authorization"] == "Bearer wk"
    body = json.loads(req.data.decode("utf-8"))
    assert body["name"] == "my-skill"
    assert body["skill_md"] == _SKILL_MD
    assert body["base_revision"] == 0
    assert body["receipt"] == _make_receipt()
    assert timeout == skill_sync.DEFAULT_TIMEOUT_SECONDS


def test_push_skill_encodes_utf8_without_ascii_escaping(fake_http):
    md = _SKILL_MD + "日本語のスキル説明です。\n"
    fake_http.script.append((200, json.dumps({"revision": 1}).encode("utf-8")))
    result = skill_sync.push_skill(**_push_args(skill_md=md))
    assert result.sent
    (req, _) = fake_http.calls[0]
    raw = req.data
    # ensure_ascii=False なので日本語が \uXXXX エスケープされず生の UTF-8 で載る
    assert "日本語のスキル説明です。".encode("utf-8") in raw
    assert b"\\u65e5" not in raw
    # ボディの JSON としての等価性は維持される
    assert json.loads(raw.decode("utf-8"))["skill_md"] == md


def test_push_skill_cas_conflict_returns_result_not_exception(fake_http):
    fake_http.script.append(
        (409, json.dumps({
            "error": {"code": "CAS_CONFLICT"},
            "conflict": True,
            "current_revision": 4,
        }).encode("utf-8"))
    )
    result = skill_sync.push_skill(**_push_args())
    assert not result.sent
    assert result.conflict
    assert result.current_revision == 4
    # error は 409 応答のパース結果全体（S-08 の契約どおり）
    assert result.error == {
        "error": {"code": "CAS_CONFLICT"},
        "conflict": True,
        "current_revision": 4,
    }


def test_push_skill_400_rejected(fake_http):
    fake_http.script.append(
        (400, json.dumps({"error": {"code": "BAD_REQUEST"}}).encode("utf-8"))
    )
    result = skill_sync.push_skill(**_push_args())
    assert not result.sent
    assert result.reason == "rejected"
    # error は 400 応答のパース結果全体（S-08 の契約どおり）
    assert result.error == {"error": {"code": "BAD_REQUEST"}}


def test_push_skill_redact_diff_does_not_send(fake_http):
    md = _SKILL_MD + "sk-ant-" + "a" * 24 + "\n"  # REDACTION_PATTERN に一致
    result = skill_sync.push_skill(**_push_args(skill_md=md))
    assert not result.sent
    assert result.reason == "redact_diff"
    assert fake_http.calls == []  # 何も送っていない


def test_push_skill_rejects_oversized_skill_md_without_sending(fake_http):
    big = "x" * (64 * 1024 + 1)
    result = skill_sync.push_skill(**_push_args(skill_md=big))
    assert not result.sent
    assert result.reason == "size_limit"
    assert fake_http.calls == []


def test_push_skill_rejects_oversized_json_body_without_sending(fake_http, monkeypatch):
    # 第 1 の上限（64KB）を緩めて第 2 の上限（JSON 全体 256KB）を踏む
    monkeypatch.setattr(skill_sync, "MAX_SKILL_MD_BYTES", 2**20)
    big = "y" * (256 * 1024 + 1)
    result = skill_sync.push_skill(**_push_args(skill_md=big))
    assert not result.sent
    assert result.reason == "size_limit"
    assert fake_http.calls == []


def test_list_skills_requests_page(fake_http):
    fake_http.script.append(
        (200, json.dumps({
            "skills": [{"name": "my-skill"}],
            "events": [],
            "next_cursor": None,
        }).encode("utf-8"))
    )
    page = skill_sync.list_skills(base_url=BASE_URL, read_key="rk")
    assert page["skills"] == [{"name": "my-skill"}]
    assert page["next_cursor"] is None
    (req, timeout) = fake_http.calls[0]
    assert req.get_method() == "GET"
    assert req.full_url == f"{BASE_URL}/api/skills/list"
    assert req.headers["Authorization"] == "Bearer rk"
    assert timeout == skill_sync.DEFAULT_TIMEOUT_SECONDS


def test_list_all_skills_reads_every_page(fake_http):
    p1 = {"skills": [{"name": "a"}], "events": [{"id": "e1"}], "next_cursor": "c1"}
    p2 = {"skills": [{"name": "b"}], "events": [], "next_cursor": None}
    fake_http.script.append((200, json.dumps(p1).encode("utf-8")))
    fake_http.script.append((200, json.dumps(p2).encode("utf-8")))
    all_pages = skill_sync.list_all_skills(base_url=BASE_URL, read_key="rk")
    assert [s["name"] for s in all_pages["skills"]] == ["a", "b"]
    assert [e["id"] for e in all_pages["events"]] == ["e1"]
    assert all_pages["next_cursor"] is None
    assert len(fake_http.calls) == 2
    assert "cursor=c1" in fake_http.calls[1][0].full_url


def test_pull_skill(fake_http):
    fake_http.script.append(
        (200, json.dumps({
            "name": "my-skill",
            "content": _SKILL_MD,
            "content_sha256": _DIGEST,
            "revision": 5,
            "receipt": _make_receipt(),
            "origin_instance": "win-abc123",
            "promoted_at_ms": 1_755_300_000_123,
            "promotion_seq": 7,
            "distilled_from_session_id": "sess_1",
        }).encode("utf-8"))
    )
    pulled = skill_sync.pull_skill("my-skill", base_url=BASE_URL, read_key="rk")
    assert pulled["name"] == "my-skill"
    assert pulled["content"] == _SKILL_MD
    (req, _) = fake_http.calls[0]
    assert req.get_method() == "GET"
    assert req.full_url == f"{BASE_URL}/api/skills/pull?name=my-skill"
    assert req.headers["Authorization"] == "Bearer rk"


def test_ack_events(fake_http):
    fake_http.script.append((200, b"{}"))
    result = skill_sync.ack_events(["e1", "e2"], base_url=BASE_URL, write_key="wk")
    assert result is None
    (req, _) = fake_http.calls[0]
    assert req.get_method() == "POST"
    assert req.full_url == f"{BASE_URL}/api/skills/events/ack"
    assert json.loads(req.data.decode("utf-8")) == {"event_ids": ["e1", "e2"]}
    assert req.headers["Authorization"] == "Bearer wk"


def test_transport_error_raises_lane_c_api_error(fake_http):
    fake_http.script.append(urllib.error.URLError("boom"))
    with pytest.raises(skill_sync.LaneCApiError):
        skill_sync.list_skills(base_url=BASE_URL, read_key="rk")


def test_http_calls_always_carry_a_timeout(fake_http):
    skill_sync.list_skills(base_url=BASE_URL, read_key="rk")
    (_, timeout) = fake_http.calls[0]
    assert timeout is not None and timeout > 0


def test_missing_credential_is_fail_closed(fake_http):
    with pytest.raises(skill_sync.LaneCApiError):
        skill_sync.push_skill(**_push_args(write_key=""))
    assert fake_http.calls == []


# ---------------------------------------------------------------------------
# 受信側検証（S-10 手順4）
# ---------------------------------------------------------------------------


def _pulled(verify_keys=None, sign=False, **overrides):
    """pull 応答の fixture。`sign=True` のときだけ receipt を override 値に
    合わせて再署名する（署名対象と一致する応答を組み立てたいテスト用。
    欠陥注入テストはデフォルトの receipt のまま override する）。"""
    data = {
        "name": "my-skill",
        "content": _SKILL_MD,
        "content_sha256": _DIGEST,
        "revision": 5,
        "receipt": _make_receipt(),
        "origin_instance": "win-abc123",
        "promoted_at_ms": 1_755_300_000_123,
        "promotion_seq": 7,
        "distilled_from_session_id": "sess_1",
        "received_at": "2026-08-16T00:00:00Z",
    }
    data.update(overrides)
    if sign:
        data["receipt"] = _make_receipt(
            name=data["name"],
            content=data["content"],
            digest=data["content_sha256"],
            seq=data["promotion_seq"],
            promoted_at_ms=data["promoted_at_ms"],
            origin_instance=data["origin_instance"],
            distilled_from_session_id=data["distilled_from_session_id"],
        )
    keys = verify_keys if verify_keys is not None else {_KEY_ID: _KEY}
    return data, keys


def test_validate_pulled_skill_accepts_valid_pull():
    data, keys = _pulled()
    pulled = skill_sync.validate_pulled_skill(data, verify_keys=keys)
    assert pulled.name == "my-skill"
    assert pulled.content == _SKILL_MD
    assert pulled.revision == 5
    assert pulled.received_at == "2026-08-16T00:00:00Z"


def test_validate_pulled_skill_accepts_skill_md_content_key():
    """サーバー契約が skill_md キーで本文を返してきても受け付ける。"""
    data, keys = _pulled()
    data["skill_md"] = data.pop("content")
    pulled = skill_sync.validate_pulled_skill(data, verify_keys=keys)
    assert pulled.content == _SKILL_MD


def test_validate_pulled_skill_accepts_null_session():
    # receipt を null session で署名し直した応答は受理される
    data, keys = _pulled(sign=True, distilled_from_session_id=None)
    pulled = skill_sync.validate_pulled_skill(data, verify_keys=keys)
    assert pulled.distilled_from_session_id is None


@pytest.mark.parametrize(
    "override,label",
    [
        ({"name": "BAD_NAME"}, "NAME_RE 不一致（大文字）"),
        ({"name": "other-name"}, "frontmatter name 不一致"),
        ({"content": _SKILL_MD + "junk"}, "sha256 不一致"),
        ({"content_sha256": "0" * 64}, "content_sha256 不一致"),
        ({"receipt": "1234abcd." + "A" * 43}, "receipt 形式不正"),
        ({"origin_instance": "Bad-Origin"}, "origin_instance 形式不正"),
        ({"promoted_at_ms": -1}, "promoted_at_ms 負数"),
        ({"promoted_at_ms": 1.5}, "promoted_at_ms float"),
        ({"promotion_seq": -3}, "promotion_seq 負数"),
        ({"distilled_from_session_id": "bad session!"}, "session 文字種"),
        ({"distilled_from_session_id": "x" * 129}, "session 128 文字超"),
        ({"revision": "5"}, "revision 型不正"),
        ({"revision": -1}, "revision 負数"),
    ],
)
def test_validate_pulled_skill_rejects_each_failure(override, label):
    data, keys = _pulled(**override)
    with pytest.raises(SyncValidationError):
        skill_sync.validate_pulled_skill(data, verify_keys=keys)


def test_validate_pulled_skill_rejects_oversized_content():
    data, keys = _pulled(content="x" * (64 * 1024 + 1))
    with pytest.raises(SyncValidationError):
        skill_sync.validate_pulled_skill(data, verify_keys=keys)


def test_validate_pulled_skill_rejects_redact_diff():
    """sha も receipt も通っても redact 差分が出れば落とす（防衛の最終線）。"""
    content = _SKILL_MD + "sk-ant-" + "a" * 24 + "\n"
    digest = _sha256_hex(content)
    receipt = _make_receipt(content=content, digest=digest)
    data, keys = _pulled(
        content=content, content_sha256=digest, receipt=receipt,
    )
    with pytest.raises(SyncValidationError):
        skill_sync.validate_pulled_skill(data, verify_keys=keys)


def test_validate_pulled_skill_rejects_receipt_signed_by_unknown_key():
    """他が全部通っても receipt が未知の鍵なら書き込まない（最重要チェック）。"""
    data, _ = _pulled()
    with pytest.raises(SyncValidationError):
        skill_sync.validate_pulled_skill(data, verify_keys={_KEY_PREV_ID: _KEY_PREV})


def test_validate_pulled_skill_accepts_previous_generation_key():
    data, _ = _pulled()
    pulled = skill_sync.validate_pulled_skill(
        data, verify_keys={_KEY_ID: _KEY, _KEY_PREV_ID: _KEY_PREV}
    )
    assert pulled.name == "my-skill"


# ---------------------------------------------------------------------------
# 差分判定（S-10 手順3）
# ---------------------------------------------------------------------------

_STATE = {"content_sha256": _DIGEST, "lane_c_revision": 5}


def _remote(**overrides) -> RemoteSkillState:
    kwargs = dict(
        name="my-skill",
        revision=5,
        content_sha256=_DIGEST,
        origin_instance="win-abc123",
        promotion_seq=7,
        origin_seq_watermarks={"win-abc123": 7},
    )
    kwargs.update(overrides)
    return RemoteSkillState(**kwargs)


def _local(**overrides) -> LocalSkillState:
    kwargs = dict(exists=True, content_sha256=_DIGEST)
    kwargs.update(overrides)
    return LocalSkillState(**kwargs)


def test_classify_representative_cases():
    """設計書 1225〜1234 行目の表の代表ケース（フェーズ B）。"""
    local_sha = _DIGEST
    remote_sha = "a" * 64

    cases = [
        # 状況 → 期待判定
        # (local, remote, state)
        # 1. 内容が同一・revision 同期済み
        (_local(), _remote(), _STATE, "noop"),
        # 2. 内容が同一・revision だけ進んだ（クラッシュ復旧）
        (_local(), _remote(revision=9), _STATE, "metadata_repair"),
        # 3. 内容が同一・状態ファイル欠損（ウォーターマーク補記）
        (_local(), _remote(revision=9), None, "metadata_repair"),
        # 4. リモートにのみ存在
        (_local(exists=False), _remote(), _STATE, "pull"),
        # 5. ローカルにのみ存在
        (_local(), None, _STATE, "push"),
        # 6. sha 不一致・ローカルは最後の同期から不変
        (_local(content_sha256=local_sha), _remote(content_sha256=remote_sha, revision=9),
         {"content_sha256": local_sha, "lane_c_revision": 5}, "pull"),
        # 7. sha 不一致・ローカルが再 promote 済み・リモートは同期点のまま
        (_local(content_sha256=remote_sha), _remote(content_sha256=local_sha),
         {"content_sha256": local_sha, "lane_c_revision": 5}, "push"),
        # 8. sha 不一致・双方が進んだ
        (_local(content_sha256=remote_sha), _remote(content_sha256=local_sha, revision=9),
         {"content_sha256": local_sha, "lane_c_revision": 5}, "conflict"),
        # 9. 状態なし（初回）・sha 不一致 → 安全側で衝突
        (_local(content_sha256=remote_sha), _remote(content_sha256=local_sha, revision=9),
         None, "conflict"),
        # 10. どちらにも存在しない
        (_local(exists=False), None, None, "noop"),
    ]
    for local, remote, state, expected in cases:
        assert (
            skill_sync.classify_sync_action("my-skill", local, remote, state) == expected
        ), f"case {local} / {remote} / {state} -> expected {expected}"


@pytest.mark.parametrize(
    "remote_kwargs,state",
    [
        # 型不正（bool は int なので明示的に弾かれる必要がある）
        ({"revision": -1}, _STATE),
        ({"revision": True}, _STATE),
        ({"revision": 2**63}, _STATE),          # 異常に大きい
        ({"promotion_seq": -1}, _STATE),
        ({"promotion_seq": True}, _STATE),
        ({"promotion_seq": 2**63}, _STATE),
        # revision の巻き戻り
        ({"revision": 3}, _STATE),
        # CAS 不変条件の破れ（同一 revision で内容が異なる）
        ({"revision": 5, "content_sha256": "0" * 64}, _STATE),
        # watermark との矛盾（origin の promotion_seq が過去に遡っている）
        ({"promotion_seq": 2}, _STATE),
    ],
)
def test_phase_a_anomaly_never_reaches_classification(remote_kwargs, state):
    """フェーズ A の整合性異常で分類フェーズ（フェーズ B）へ進まない。"""
    remote = _remote(**remote_kwargs)
    with pytest.raises(IntegrityAnomalyError):
        skill_sync.classify_sync_action("my-skill", _local(), remote, state)


def test_check_integrity_returns_none_for_sane_remote():
    assert skill_sync.check_integrity(_remote(), _STATE) is None
    # watermark に対象 origin が無ければ比較不能なので異常にしない
    assert skill_sync.check_integrity(
        _remote(origin_instance="win-other"),
        _STATE,
    ) is None


def test_classify_does_not_compare_local_and_remote_times():
    """ローカル時刻とリモート時刻の比較が無いことの構造的検証。

    確定事項 I: 判定材料はダイジェストと `revision` のみ。分類の入力型
    `LocalSkillState` / `RemoteSkillState` には時刻フィールド
    （received_at / promoted_at_ms）が存在しない——比較する式を書くこと
    自体が構造的に不可能になっている。
    """
    local_fields = set(LocalSkillState.__dataclass_fields__)
    remote_fields = set(RemoteSkillState.__dataclass_fields__)
    assert local_fields == {"exists", "content_sha256"}
    assert remote_fields == {
        "name", "revision", "content_sha256", "origin_instance",
        "promotion_seq", "origin_seq_watermarks",
    }


# ---------------------------------------------------------------------------
# S-11 通知本文の構造的担保（SKILL.md 本文が一切載らないこと。wave4 追加）
# ---------------------------------------------------------------------------

from modal_hub.services import ntfy_client  # noqa: E402


def test_send_skill_sync_event_body_whitelist():
    """`send_skill_sync_event()` の本文は event/name/reason の 3 フィールドのみ。

    呼び出し側が event に `content` / `diff`（SKILL.md 本文・差分）を混ぜて
    も本文には漏れない（S-11 の構造的担保）。reason は上限へ切り詰める。
    """
    event = {
        "event": "skill_sync_validation_failed",
        "name": "my-skill",
        "reason": "r" * 300,  # SYNC_EVENT_REASON_MAX を超える
        "content": _SKILL_MD,  # 混入しても載らない
        "diff": "sk-ant-" + "a" * 24,
    }
    body = ntfy_client._build_sync_event_body(event)
    payload = json.loads(body)
    assert set(payload) == {"event", "name", "reason"}
    assert len(payload["reason"]) == ntfy_client.SYNC_EVENT_REASON_MAX
    assert _SKILL_MD not in body
    assert "sk-ant-" not in body


def test_send_skill_sync_event_requires_event_and_name():
    with pytest.raises(ValueError):
        ntfy_client._build_sync_event_body({"name": "x"})  # event 欠落
    with pytest.raises(ValueError):
        ntfy_client._build_sync_event_body({"event": "e", "name": ""})  # name 空
    with pytest.raises(ValueError):
        ntfy_client._build_sync_event_body({"event": "", "name": "x"})  # event 空


def test_send_skill_sync_event_fails_without_topic(monkeypatch):
    """NTFY_TOPIC が取得できない場合は送信を諦めて "failed"（HTTP に出ない）。"""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    assert ntfy_client.send_skill_sync_event(
        {"event": "skill_sync_validation_failed", "name": "x"}
    ) == "failed"


def test_sync_event_title_and_tags_are_ascii():
    """タイトル・タグは ASCII 固定（httpx のヘッダーエンコード制約）。"""
    assert ntfy_client.SYNC_EVENT_TITLE.isascii()
    assert all(t.isascii() for t in ntfy_client.SYNC_EVENT_TAGS)


def test_conflict_body_never_leaks_content():
    """`send_skill_conflict()` の本文も content/diff を決して載せない。"""
    event = {
        "name": "s",
        "winner": "win-1",
        "winner_sha8": "a" * 8,
        "loser_sha8": "b" * 8,
        "content": _SKILL_MD,
        "diff": "sk-ant-" + "a" * 24,
    }
    body = ntfy_client._build_conflict_body(event)
    payload = json.loads(body)
    assert set(payload) == {"event", "name", "winner", "winner_sha8", "loser_sha8"}
    assert _SKILL_MD not in body
    assert "sk-ant-" not in body


# ---------------------------------------------------------------------------
# modal_dashboard/app.py の sync_dashboard_skills 静的な構成テスト（wave4 追加）
#
# 実 Modal 環境（デプロイ・Volume/Secret 実体）は使わず、app.py のソースを
# ast で読み、`@app.function(...)` のデコレータ引数（volumes / secrets /
# max_containers / schedule）を評価する。モジュール import を伴わないため
# `modal` パッケージや Docker ビルドが無い環境でも動く。
# ---------------------------------------------------------------------------

import ast  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_APP_ROOT = _Path(__file__).resolve().parents[2]
_APP_PATH = _APP_ROOT / "modal_dashboard" / "app.py"

#: app.py 内で参照される定数（モジュール import せずに評価するための写し）
_APP_CONSTS = {
    "_DASHBOARD_VOLUME_NAME": "hh-agent-dashboard-home",
    "_DASHBOARD_MOUNT_PATH": "/opt/data",
    "_DASHBOARD_SECRET_NAME": "hh-agent-dashboard-secret",
    "_HUB_SECRET_NAME": "hh-agent-secret",
    "_CORPUS2SKILL_SECRET_NAME": "corpus2skill-secret",
    "_NCAM_SECRET_NAME": "ncam-daemon-secret",
    "_DASHBOARD_PORT": 8000,
}

#: modal_hub/core/store.py の定数（同じく写し。値は store.py の定義と一致させる）
_STORE_CONSTS = {
    "VOLUME_MOUNT_PATH": "/mnt/hh_store",
    "STORE_VOLUME_NAME": "hh-agent-store",
}


def _eval_expr(node):
    """`@app.function(...)` のキーワード引数で使われる式だけを評価する小型評価器。

    対応: リテラル・既知の定数名・`modal.Volume.from_name(...)` /
    `modal.Secret.from_name(...)` / `modal.Period(hours=N)` /
    `store.<定数>`。それ以外の式は明示的に AssertionError（静的分析が
    黙って誤った値を返さないようにする）。
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        # volumes= / secrets= のリテラル dict（キーは文字列・定数・
        # store.<定数> 参照のいずれか）。** 展開は使わない。
        return {
            _eval_expr(key): _eval_expr(value)
            for key, value in zip(node.keys, node.values)
            if key is not None
        }
    if isinstance(node, ast.List):
        return [_eval_expr(elt) for elt in node.elts]
    if isinstance(node, ast.Name) and node.id in _APP_CONSTS:
        return _APP_CONSTS[node.id]
    if isinstance(node, ast.Name):
        # 既知の定数表に無い裸の名前参照（例: `image=image`、モジュール
        # レベルの `modal.Image` オブジェクトを指す）。このテストは
        # volumes/secrets/max_containers/schedule だけを検証し image の
        # 中身は見ないため、参照先を評価する必要はない——同じ名前を指す
        # 参照同士を比較・照合できるよう、シンボリックな印だけを返す
        # （黙って無視せず、想定外の別の式に化けないよう明示的にタグ付けする）。
        return ("name_ref", node.id)
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == "store" and node.attr in _STORE_CONSTS:
            return _STORE_CONSTS[node.attr]
        raise AssertionError(f"未対応の属性参照: {ast.dump(node)}")
    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "from_name"
            and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "modal"
            and func.value.attr in ("Volume", "Secret")
        ):
            kind = "volume" if func.value.attr == "Volume" else "secret"
            args = [_eval_expr(a) for a in node.args]
            kwargs = {kw.arg: _eval_expr(kw.value) for kw in node.keywords if kw.arg}
            return {kind: {"name": args[0], **kwargs}}
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "modal"
            and func.attr == "Period"
        ):
            return {"period": {kw.arg: _eval_expr(kw.value) for kw in node.keywords if kw.arg}}
        raise AssertionError(f"未対応の呼び出し: {ast.dump(node)}")
    raise AssertionError(f"未対応の式: {ast.dump(node)}")


def _function_kwargs(app_ast, name: str) -> dict:
    """FunctionDef `name` の `@app.function(...)` キーワード引数を評価する。"""
    for node in app_ast.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Attribute)
                    and deco.func.attr == "function"
                ):
                    return {kw.arg: _eval_expr(kw.value) for kw in deco.keywords if kw.arg}
    raise AssertionError(f"app.py に @app.function の {name} が見つからない")


def _app_ast():
    return ast.parse(_APP_PATH.read_text(encoding="utf-8"))


def test_sync_dashboard_skills_volumes_secrets_and_schedule():
    """sync_dashboard_skills の静的構成（wave4 タスク指示 §2 のとおり）。

    - volumes: _DASHBOARD_MOUNT_PATH（dashboard Volume）に加えて
      Hub Volume（hh-agent-store）をマウントする
    - secrets: dashboard / hh-agent-secret / corpus2skill / ncam-daemon の 4 つ
      (ncam-daemon-secretは2026-09-05追加。sync_dashboard_skills自体は
      hooks/MCPを起動しないため機能的には不要だが、
      test_sync_dashboard_skills_diff_vs_dashboard_serverが検証する
      「syncはdashboard_serverの秘密を包含する」不変条件を満たすために揃える)
    - max_containers=1・schedule=modal.Period(hours=8)
    """
    cfg = _function_kwargs(_app_ast(), "sync_dashboard_skills")
    assert cfg["volumes"] == {
        "/opt/data": {"volume": {"name": "hh-agent-dashboard-home", "create_if_missing": True}},
        "/mnt/hh_store": {"volume": {"name": "hh-agent-store", "create_if_missing": True}},
    }
    assert [s["secret"]["name"] for s in cfg["secrets"]] == [
        "hh-agent-dashboard-secret",
        "hh-agent-secret",
        "corpus2skill-secret",
        "ncam-daemon-secret",
    ]
    assert cfg["max_containers"] == 1
    assert cfg["schedule"] == {"period": {"hours": 8}}


def test_sync_dashboard_skills_hub_mount_references_store_constant():
    """Hub Volume のマウント先キーは store.VOLUME_MOUNT_PATH 定数を参照する。

    タスク指示「マウント先は modal_hub/core/store.py の VOLUME_MOUNT_PATH
    定数の値と一致させること」— 文字列リテラルの写しでなく定数参照である
    ことを構造的に検証する（ずれの余地をなくす）。
    """
    app_ast = _app_ast()
    func = next(n for n in app_ast.body if isinstance(n, ast.FunctionDef) and n.name == "sync_dashboard_skills")
    deco = next(
        d for d in func.decorator_list
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "function"
    )
    volumes_call = next(kw.value for kw in deco.keywords if kw.arg == "volumes")
    keys = [k for k in volumes_call.keys if k is not None]
    assert any(
        isinstance(k, ast.Attribute)
        and isinstance(k.value, ast.Name)
        and k.value.id == "store"
        and k.attr == "VOLUME_MOUNT_PATH"
        for k in keys
    )


def test_sync_dashboard_skills_diff_vs_dashboard_server():
    """sync_dashboard_skills と dashboard_server の構成差分（C-3 の規約維持）。

    - hh-agent-secret は sync_dashboard_skills には付き、dashboard_server
      には付かない（dashboard_server は未検証のモデル生成コマンドを実行する
      ため Hub ルート資格情報を持たせない — C-3）
    - sync は dashboard_server の秘密を包含し、Hub Volume を追加マウントする
    - どちらも max_containers=1（dashboard_server は min_containers=0）
    """
    sync_cfg = _function_kwargs(_app_ast(), "sync_dashboard_skills")
    server_cfg = _function_kwargs(_app_ast(), "dashboard_server")
    sync_secrets = [s["secret"]["name"] for s in sync_cfg["secrets"]]
    server_secrets = [s["secret"]["name"] for s in server_cfg["secrets"]]
    assert "hh-agent-secret" in sync_secrets
    assert "hh-agent-secret" not in server_secrets
    assert set(server_secrets) <= set(sync_secrets)
    assert "/mnt/hh_store" in sync_cfg["volumes"]
    assert "/mnt/hh_store" not in server_cfg["volumes"]
    assert sync_cfg["max_containers"] == 1 and server_cfg["max_containers"] == 1
    assert sync_cfg["schedule"] == {"period": {"hours": 8}}
