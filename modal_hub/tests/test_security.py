"""`modal_hub/core/security.py` — 親設計書 §8.1「core/security.py」の全項目。

    署名検証、TTL 切れ、単回使用、定数時間比較、レート制限、
    `source` の自己申告が無視されること

加えて Phase1a spec §6（トークン）、§7（ペアリング / Cookie / CSRF / WS
チケット）、§11（肯定リスト方式。失効の墓標を使わない）を検証する。
"""

from __future__ import annotations

import base64
import hmac
import json
import time

import pytest

from modal_hub.core import security, store as store_keys
from modal_hub.tests.conftest import (
    TEST_AGENT_SIGNING_KEY,
    TEST_AGENT_SIGNING_KEY_PREV,
    TEST_PWA_SESSION_KEY,
    WS_ID,
    WS_ID_OTHER,
    FakeStore,
)

KEY = TEST_AGENT_SIGNING_KEY.encode("utf-8")
KEY_PREV = TEST_AGENT_SIGNING_KEY_PREV.encode("utf-8")
PWA_KEY = TEST_PWA_SESSION_KEY.encode("utf-8")

NOW = 1_786_000_000.0


def issue(store: FakeStore, **kw) -> str:
    params = dict(
        sub="claude_code:desktop-haruki",
        source="claude_code",
        session_id="sess-1",
        workspace_id=WS_ID,
        signing_key=KEY,
        now=NOW,
    )
    params.update(kw)
    return security.issue_agent_token(store, **params)


def _decode_payload(token: str) -> dict:
    seg = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


# ===========================================================================
# エージェントトークン: 形式・署名
# ===========================================================================


def test_issued_token_has_hha1_prefix_and_three_segments(fake_store) -> None:
    token = issue(fake_store)
    parts = token.split(".")
    assert len(parts) == 3
    assert parts[0] == "hha1"


def test_round_trip_verify_returns_identity_from_token_not_body(fake_store) -> None:
    token = issue(fake_store)
    identity = security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW + 1)
    assert identity.source == "claude_code"
    assert identity.session_id == "sess-1"
    assert identity.workspace_id == WS_ID
    assert identity.sub == "claude_code:desktop-haruki"


def test_tampered_payload_fails_signature(fake_store) -> None:
    """payload を書き換えたトークンは署名検証で落ちる（改ざん検出）。"""
    token = issue(fake_store)
    header, payload_b64, sig = token.split(".")
    payload = _decode_payload(token)
    payload["workspace_id"] = WS_ID_OTHER  # 他ワークスペースへの昇格を試みる
    forged_payload = (
        base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    forged = f"{header}.{forged_payload}.{sig}"
    with pytest.raises(security.InvalidCredentialError):
        security.verify_agent_token(fake_store, forged, signing_key=KEY, now=NOW + 1)


def test_signature_from_wrong_key_rejected(fake_store) -> None:
    token = issue(fake_store)
    with pytest.raises(security.InvalidCredentialError):
        security.verify_agent_token(fake_store, token, signing_key=b"another-key-entirely", now=NOW + 1)


@pytest.mark.parametrize(
    "token",
    ["", "not-a-token", "hha1.onlytwo", "hha1.a.b.c", "xxxx.payload.sig", "hha1..sig"],
)
def test_malformed_tokens_rejected(fake_store, token: str) -> None:
    with pytest.raises(security.SecurityError):
        security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW)


def test_verification_without_any_key_fails_closed(fake_store) -> None:
    token = issue(fake_store)
    with pytest.raises(security.InvalidCredentialError):
        security.verify_agent_token(fake_store, token, signing_key=b"", signing_key_prev=None, now=NOW)


# ===========================================================================
# 鍵ローテーション（spec §6.2「検証は両方で試す。発行は新しい方のみ」）
# ===========================================================================


def test_token_signed_with_prev_key_still_verifies(fake_store) -> None:
    token = issue(fake_store, signing_key=KEY_PREV)
    identity = security.verify_agent_token(
        fake_store, token, signing_key=KEY, signing_key_prev=KEY_PREV, now=NOW + 1
    )
    assert identity.sub == "claude_code:desktop-haruki"


def test_token_signed_with_prev_key_fails_after_prev_removed(fake_store) -> None:
    token = issue(fake_store, signing_key=KEY_PREV)
    with pytest.raises(security.InvalidCredentialError):
        security.verify_agent_token(fake_store, token, signing_key=KEY, signing_key_prev=None, now=NOW + 1)


def test_issue_signature_has_no_prev_key_parameter() -> None:
    """発行は現行鍵のみ。旧鍵で誤発行できないようシグネチャで防ぐ（spec §6.2）。"""
    import inspect

    params = inspect.signature(security.issue_agent_token).parameters
    assert "signing_key" in params
    assert "signing_key_prev" not in params


# ===========================================================================
# TTL 切れ（spec §6.1「有効期限 24 時間」・§11「判定はストア側の exp」）
# ===========================================================================


def test_token_ttl_is_24h(fake_store) -> None:
    token = issue(fake_store)
    payload = _decode_payload(token)
    assert payload["exp"] - payload["iat"] == security.AGENT_TOKEN_TTL_SECONDS == 24 * 3600


def test_expired_token_rejected(fake_store) -> None:
    token = issue(fake_store)
    with pytest.raises(security.ExpiredCredentialError):
        security.verify_agent_token(
            fake_store, token, signing_key=KEY, now=NOW + security.AGENT_TOKEN_TTL_SECONDS + 1
        )


def test_expiry_is_decided_by_store_record_not_token_payload(fake_store) -> None:
    """spec §11: 有効性の一次情報源は常にストア側の `exp`。

    トークン payload の `exp` がまだ先でも、ストアの `exp` を過ぎていれば無効。
    """
    token = issue(fake_store)
    tid = _decode_payload(token)["tid"]
    record = fake_store.data[store_keys.agent_session_key(tid)]
    record["exp"] = NOW - 1  # ストア側だけを期限切れにする
    with pytest.raises(security.ExpiredCredentialError):
        security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW)


# ===========================================================================
# 肯定リスト（spec §11。失効の墓標を使わない）
# ===========================================================================


def test_revoke_deletes_the_allowlist_key_and_writes_no_tombstone(fake_store) -> None:
    token = issue(fake_store)
    tid = _decode_payload(token)["tid"]

    security.revoke_agent_session(fake_store, tid)

    assert store_keys.agent_session_key(tid) not in fake_store.data
    assert not any(k.startswith("revoked:") for k in fake_store.data), (
        "失効の墓標（revoked:）が書かれている。spec §11 が明示的に破棄した設計。"
    )


def test_revoked_token_is_rejected_even_though_signature_is_valid(fake_store) -> None:
    token = issue(fake_store)
    security.revoke_agent_session(fake_store, _decode_payload(token)["tid"])
    with pytest.raises(security.UnknownCredentialError):
        security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW + 1)


def test_store_record_disappearing_fails_closed(fake_store) -> None:
    """ストアの TTL でレコードが消えた場合も「無効」に倒れる（フェイルクローズ）。"""
    token = issue(fake_store)
    fake_store.data.clear()
    with pytest.raises(security.UnknownCredentialError):
        security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW + 1)


def test_record_payload_divergence_fails_closed(fake_store) -> None:
    """発行時に書いた 2 つのコピーが食い違ったら実装バグの兆候 → 拒否。"""
    token = issue(fake_store)
    tid = _decode_payload(token)["tid"]
    fake_store.data[store_keys.agent_session_key(tid)]["workspace_id"] = WS_ID_OTHER
    with pytest.raises(security.InvalidCredentialError):
        security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW + 1)


# ===========================================================================
# `source` の自己申告を信じない（§8.1 明示項目 / spec §6.1）
# ===========================================================================


def test_source_and_session_come_from_token_only(fake_store) -> None:
    """`verify_agent_token` はリクエストボディを一切受け取らない。

    ボディ由来の値を採用する経路が存在しないことをシグネチャで担保する。
    """
    import inspect

    params = set(inspect.signature(security.verify_agent_token).parameters)
    assert params == {"store", "token", "signing_key", "signing_key_prev", "now"}
    assert "source" not in params and "session_id" not in params and "body" not in params


def test_invalid_source_claim_in_token_is_rejected(fake_store) -> None:
    """署名済みでも `source` が許可語彙外なら拒否（閉じた語彙）。"""
    payload = {
        "tid": "t1",
        "sub": "x",
        "source": "attacker_supplied",
        "session_id": "s",
        "workspace_id": WS_ID,
        "iat": int(NOW),
        "exp": int(NOW) + 3600,
    }
    payload_b64 = (
        base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    sig = hmac.new(KEY, f"hha1.{payload_b64}".encode("ascii"), "sha256").digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    fake_store.put_if_absent(store_keys.agent_session_key("t1"), dict(payload, exp=payload["exp"]))
    with pytest.raises(security.InvalidCredentialError):
        security.verify_agent_token(fake_store, f"hha1.{payload_b64}.{sig_b64}", signing_key=KEY, now=NOW)


def test_issue_rejects_unknown_source(fake_store) -> None:
    with pytest.raises(ValueError):
        issue(fake_store, source="pwa")


def test_issue_rejects_malformed_workspace_id(fake_store) -> None:
    for bad in ("", "short", "z" * 64, "A" * 63):
        with pytest.raises(ValueError):
            issue(fake_store, workspace_id=bad)


# ===========================================================================
# 定数時間比較（§8.1 明示項目）
# ===========================================================================


def test_constant_time_equals_uses_compare_digest(monkeypatch) -> None:
    seen: list[tuple] = []
    real = hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", spy)
    assert security.constant_time_equals("abc", "abc") is True
    assert security.constant_time_equals("abc", "abd") is False
    assert security.constant_time_equals("abc", "abcd") is False
    assert len(seen) == 3


def test_security_module_never_compares_secrets_with_plain_equality() -> None:
    """`==` による秘密比較がソース上に残っていないことを機械的に検査する。

    `hmac.compare_digest` を使う方針は「使っている箇所がある」ことでは担保
    できない。**署名・ダイジェスト変数に対する `==` が 1 つも無い**ことを
    見る。
    """
    import inspect
    import re

    source = inspect.getsource(security)
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"\b(sig|signature|digest|expected|provided|mac)\w*\s*==", line)
        and not line.strip().startswith("#")
    ]
    assert offenders == [], f"秘密由来の値を == で比較している: {offenders}"


# ===========================================================================
# PWA セッション Cookie（spec §7.2 / §11）
# ===========================================================================


def test_pwa_session_round_trip(fake_store) -> None:
    cookie, sid = security.issue_pwa_session(
        fake_store, device_name="iPhone", session_key=PWA_KEY, now=NOW
    )
    identity = security.verify_pwa_session(fake_store, cookie, session_key=PWA_KEY, now=NOW + 10)
    assert identity.session_id == sid
    assert identity.device_name == "iPhone"


def test_pwa_session_ttl_is_30_days(fake_store) -> None:
    assert security.PWA_SESSION_TTL_SECONDS == 30 * 24 * 3600
    cookie, sid = security.issue_pwa_session(fake_store, device_name="iPhone", session_key=PWA_KEY, now=NOW)
    record = fake_store.data[store_keys.pwa_session_key(sid)]
    assert record["exp"] == NOW + security.PWA_SESSION_TTL_SECONDS


def test_pwa_cookie_signature_is_verified(fake_store) -> None:
    cookie, sid = security.issue_pwa_session(fake_store, device_name="iPhone", session_key=PWA_KEY, now=NOW)
    forged = sid + ".AAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    with pytest.raises(security.InvalidCredentialError):
        security.verify_pwa_session(fake_store, forged, session_key=PWA_KEY, now=NOW)


def test_logout_deletes_session_so_stolen_cookie_stops_working(fake_store) -> None:
    """spec §7.2/§11: `Max-Age` はヒント。盗まれた Cookie を止めるのは削除。"""
    cookie, sid = security.issue_pwa_session(fake_store, device_name="iPhone", session_key=PWA_KEY, now=NOW)
    security.logout_pwa_session(fake_store, sid)
    with pytest.raises(security.UnknownCredentialError):
        security.verify_pwa_session(fake_store, cookie, session_key=PWA_KEY, now=NOW + 10)
    assert not any(k.startswith("revoked:") for k in fake_store.data)


def test_expired_pwa_session_rejected(fake_store) -> None:
    cookie, _sid = security.issue_pwa_session(fake_store, device_name="iPhone", session_key=PWA_KEY, now=NOW)
    with pytest.raises(security.ExpiredCredentialError):
        security.verify_pwa_session(
            fake_store, cookie, session_key=PWA_KEY, now=NOW + security.PWA_SESSION_TTL_SECONDS + 1
        )


# ===========================================================================
# CSRF（spec §7.3）
# ===========================================================================


def test_csrf_token_round_trip() -> None:
    token = security.issue_csrf_token("sid-1", session_key=PWA_KEY, now=NOW)
    security.verify_csrf_token("sid-1", token, session_key=PWA_KEY, now=NOW)  # 例外なし


def test_csrf_token_is_bound_to_the_session() -> None:
    token = security.issue_csrf_token("sid-1", session_key=PWA_KEY, now=NOW)
    with pytest.raises(security.InvalidCsrfError):
        security.verify_csrf_token("sid-2", token, session_key=PWA_KEY, now=NOW)


def test_csrf_token_survives_one_hour_bucket_but_not_three() -> None:
    token = security.issue_csrf_token("sid-1", session_key=PWA_KEY, now=NOW)
    security.verify_csrf_token("sid-1", token, session_key=PWA_KEY, now=NOW + 3600)
    with pytest.raises(security.InvalidCsrfError):
        security.verify_csrf_token("sid-1", token, session_key=PWA_KEY, now=NOW + 3 * 3600)


@pytest.mark.parametrize("bad", ["", None, "deadbeef"])
def test_missing_or_wrong_csrf_rejected(bad) -> None:
    with pytest.raises(security.InvalidCsrfError):
        security.verify_csrf_token("sid-1", bad, session_key=PWA_KEY, now=NOW)


def test_origin_must_match_exactly_and_referer_is_not_used() -> None:
    security.verify_origin("https://hub.example.com", "https://hub.example.com")
    for bad in (None, "", "http://hub.example.com", "https://hub.example.com.evil", "https://evil"):
        with pytest.raises(security.InvalidCsrfError):
            security.verify_origin(bad, "https://hub.example.com")


# ===========================================================================
# WS チケット: 単回使用（§8.1 明示項目 / spec §7.4）
# ===========================================================================


def test_ws_ticket_is_single_use(fake_store) -> None:
    ticket = security.issue_ws_ticket("sid-1", session_key=PWA_KEY, now=NOW)
    identity = security.consume_ws_ticket(fake_store, ticket, session_key=PWA_KEY, now=NOW + 1)
    assert identity.pwa_session_id == "sid-1"
    with pytest.raises(security.ReplayedTicketError):
        security.consume_ws_ticket(fake_store, ticket, session_key=PWA_KEY, now=NOW + 2)


def test_ws_ticket_ttl_is_30_seconds(fake_store) -> None:
    assert security.WS_TICKET_TTL_SECONDS == 30
    ticket = security.issue_ws_ticket("sid-1", session_key=PWA_KEY, now=NOW)
    with pytest.raises(security.ExpiredCredentialError):
        security.consume_ws_ticket(fake_store, ticket, session_key=PWA_KEY, now=NOW + 31)


def test_ws_ticket_issue_writes_nothing_to_the_store(fake_store) -> None:
    """発行時に `wsticket:` を先出しで書くと、正規の初回接続が必ず弾かれる。

    §7.1 の「同一キーの作成と skip_if_exists 消費」で起きた論理バグと同型。
    """
    security.issue_ws_ticket("sid-1", session_key=PWA_KEY, now=NOW)
    assert fake_store.data == {}


def test_forged_ws_ticket_rejected(fake_store) -> None:
    ticket = security.issue_ws_ticket("sid-1", session_key=PWA_KEY, now=NOW)
    payload_b64 = ticket.split(".")[0]
    with pytest.raises(security.InvalidCredentialError):
        security.consume_ws_ticket(fake_store, f"{payload_b64}.AAAA", session_key=PWA_KEY, now=NOW)


def test_expired_ws_ticket_is_not_consumed(fake_store) -> None:
    """期限切れチケットは消費マークを残さない（消費前に TTL を見る）。"""
    ticket = security.issue_ws_ticket("sid-1", session_key=PWA_KEY, now=NOW)
    with pytest.raises(security.ExpiredCredentialError):
        security.consume_ws_ticket(fake_store, ticket, session_key=PWA_KEY, now=NOW + 999)
    assert fake_store.data == {}


# ===========================================================================
# ペアリング（spec §7.1。v1 の単一キー設計が生んだ「必ず 409」バグの回帰）
# ===========================================================================


def test_first_pairing_succeeds(fake_store) -> None:
    """v1 の単一キー `pairing:<hash>` 設計では**正規の初回ペアリングが必ず 409**。

    オファーと使用済みマーカーが別キーになっていることの回帰テスト。
    """
    code = security.create_pairing_offer(fake_store, now=NOW)
    security.verify_and_consume_pairing_code(fake_store, code, now=NOW + 1)  # 例外が出なければ成功


def test_pairing_code_cannot_be_reused(fake_store) -> None:
    """単回使用そのものは成立している（2 回目は必ず何らかの SecurityError）。"""
    code = security.create_pairing_offer(fake_store, now=NOW)
    security.verify_and_consume_pairing_code(fake_store, code, now=NOW + 1)
    with pytest.raises(security.SecurityError):
        security.verify_and_consume_pairing_code(fake_store, code, now=NOW + 2)


def test_pairing_used_marker_is_written(fake_store) -> None:
    code = security.create_pairing_offer(fake_store, now=NOW)
    security.verify_and_consume_pairing_code(fake_store, code, now=NOW + 1)
    used = store_keys.pairing_used_key(security.hash_pairing_code(code))
    assert used in fake_store.data


def test_reused_pairing_code_reports_consumed_not_invalid(fake_store) -> None:
    """BUG-4 の回帰テスト（修正済み）。

    `verify_and_consume_pairing_code` は判定順序を「used マーカー確認 →
    offer 存在確認 → used 消費」に変更した。これにより再利用されたコードは
    offer が既に削除されていても `PairingCodeConsumedError`
    （409 PAIRING_CONSUMED）を正しく返す。
    """
    code = security.create_pairing_offer(fake_store, now=NOW)
    security.verify_and_consume_pairing_code(fake_store, code, now=NOW + 1)
    with pytest.raises(security.PairingCodeConsumedError):
        security.verify_and_consume_pairing_code(fake_store, code, now=NOW + 2)


def test_pairing_offer_expires_after_5_minutes(fake_store) -> None:
    assert security.PAIRING_OFFER_TTL_SECONDS == 300
    code = security.create_pairing_offer(fake_store, now=NOW)
    with pytest.raises(security.PairingCodeInvalidError):
        security.verify_and_consume_pairing_code(fake_store, code, now=NOW + 301)


def test_unknown_pairing_code_rejected(fake_store) -> None:
    with pytest.raises(security.PairingCodeInvalidError):
        security.verify_and_consume_pairing_code(fake_store, "00000000", now=NOW)


def test_pairing_code_shape(fake_store) -> None:
    code = security.create_pairing_offer(fake_store, now=NOW)
    assert len(code) == security.PAIRING_CODE_LENGTH == 8
    assert code.isdigit()


def test_pairing_store_keys_never_contain_the_raw_code(fake_store) -> None:
    """コードそのものをキーに使わない（ストアのキー一覧から総当たりされない）。"""
    code = security.create_pairing_offer(fake_store, now=NOW)
    assert all(code not in key for key in fake_store.data)


# ===========================================================================
# レート制限（§8.1 明示項目 / 親設計書 §5.1 / spec §7.1）
# ===========================================================================


def test_rate_limit_allows_exactly_limit_then_rejects(fake_store) -> None:
    for _ in range(20):
        security.check_rate_limit(fake_store, "sub-a", limit=20, window_seconds=3600, now=NOW)
    with pytest.raises(security.RateLimitExceededError):
        security.check_rate_limit(fake_store, "sub-a", limit=20, window_seconds=3600, now=NOW)


def test_rate_limit_is_per_subject(fake_store) -> None:
    for _ in range(20):
        security.check_rate_limit(fake_store, "sub-a", limit=20, window_seconds=3600, now=NOW)
    security.check_rate_limit(fake_store, "sub-b", limit=20, window_seconds=3600, now=NOW)


def test_rate_limit_window_rolls_over(fake_store) -> None:
    for _ in range(20):
        security.check_rate_limit(fake_store, "sub-a", limit=20, window_seconds=3600, now=NOW)
    security.check_rate_limit(fake_store, "sub-a", limit=20, window_seconds=3600, now=NOW + 3600)


def test_rate_limit_retry_after_points_at_next_window_boundary(fake_store) -> None:
    now = 3600.0 * 100 + 900  # バケット境界から 900 秒後
    for _ in range(3):
        security.check_rate_limit(fake_store, "sub-a", limit=3, window_seconds=3600, now=now)
    with pytest.raises(security.RateLimitExceededError) as exc:
        security.check_rate_limit(fake_store, "sub-a", limit=3, window_seconds=3600, now=now)
    assert exc.value.retry_after_seconds == pytest.approx(3600 - 900)


def test_rate_limit_rejects_nonpositive_limit(fake_store) -> None:
    for bad in (0, -1):
        with pytest.raises(ValueError):
            security.check_rate_limit(fake_store, "s", limit=bad, window_seconds=60, now=NOW)


# ===========================================================================
# フェイルクローズの形（None を返さず必ず例外にする）
# ===========================================================================


@pytest.mark.parametrize(
    "name",
    [
        "verify_agent_token",
        "verify_pwa_session",
        "consume_ws_ticket",
        "verify_csrf_token",
        "verify_origin",
        "verify_and_consume_pairing_code",
        "check_rate_limit",
    ],
)
def test_verifiers_signal_failure_by_exception_not_return_value(name: str) -> None:
    """「無効を意味する None/False を返す」実装は呼び出し側の判定漏れを誘発する。

    全ての検証系関数の例外が単一の基底 `SecurityError` に属することを確認し、
    ルータ側が 1 つの except で漏れなく拒否できることを担保する。
    """
    fn = getattr(security, name)
    assert callable(fn)
    for exc_name in (
        "InvalidCredentialError",
        "ExpiredCredentialError",
        "UnknownCredentialError",
        "ReplayedTicketError",
        "InvalidCsrfError",
        "PairingCodeInvalidError",
        "PairingCodeConsumedError",
        "RateLimitExceededError",
    ):
        assert issubclass(getattr(security, exc_name), security.SecurityError)


def test_store_errors_are_not_swallowed_as_invalid_credentials(fake_store) -> None:
    """インフラ障害を「資格情報が無効」に読み替えない（原因を隠さない）。"""

    class BrokenStore(FakeStore):
        def get(self, key):
            raise RuntimeError("store outage")

    token = issue(fake_store)
    broken = BrokenStore()
    with pytest.raises(RuntimeError, match="store outage"):
        security.verify_agent_token(broken, token, signing_key=KEY, now=NOW + 1)


# ===========================================================================
# scopes（Phase1b, 07_Phase1b_Spec.md §5）
# ===========================================================================


def test_omitting_scopes_defaults_to_legacy_set(fake_store) -> None:
    token = issue(fake_store)
    identity = security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW + 1)
    assert identity.scopes == frozenset({"request", "poll", "claim", "complete"})
    assert identity.has_scope("request") is True
    assert identity.has_scope("publish") is False


def test_explicit_scopes_round_trip(fake_store) -> None:
    token = issue(fake_store, scopes=["publish"])
    identity = security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW + 1)
    assert identity.scopes == frozenset({"publish"})


def test_require_scope_passes_when_present(fake_store) -> None:
    token = issue(fake_store, scopes=["publish"])
    identity = security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW + 1)
    security.require_scope(identity, "publish")  # 例外なし


def test_require_scope_raises_when_absent(fake_store) -> None:
    token = issue(fake_store)  # レガシーデフォルト(publish を含まない)
    identity = security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW + 1)
    with pytest.raises(security.InsufficientScopeError):
        security.require_scope(identity, "publish")


def test_insufficient_scope_error_is_a_security_error() -> None:
    assert issubclass(security.InsufficientScopeError, security.SecurityError)


def test_record_missing_scopes_key_is_treated_as_legacy(fake_store) -> None:
    """本物の Phase1a 発行済みトークン（`scopes` キーを持たない古いレコード）
    を模して、ストアのレコードから直接 `scopes` を消しても検証が壊れず
    レガシーデフォルトへフォールバックすることを確認する。
    """
    token = issue(fake_store)
    payload = _decode_payload(token)
    record_key = store_keys.agent_session_key(payload["tid"])
    record = fake_store.get(record_key)
    del record["scopes"]  # Phase1a 時点の実レコードを模す
    fake_store.data[record_key] = record

    identity = security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW + 1)
    assert identity.scopes == frozenset({"request", "poll", "claim", "complete"})


def test_payload_scopes_must_match_record_scopes(fake_store) -> None:
    """トークン payload の scopes とストアレコードの scopes が食い違う場合は
    拒否する（`sub`/`source` 等の既存フィールドと同じ整合性チェック）。
    """
    token = issue(fake_store, scopes=["publish"])
    payload = _decode_payload(token)
    record_key = store_keys.agent_session_key(payload["tid"])
    record = fake_store.get(record_key)
    record["scopes"] = ["request", "poll", "claim", "complete"]  # 改ざんを模す
    fake_store.data[record_key] = record

    with pytest.raises(security.InvalidCredentialError):
        security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW + 1)


def test_non_list_scopes_in_payload_is_rejected(fake_store) -> None:
    token = issue(fake_store)
    payload = _decode_payload(token)
    record_key = store_keys.agent_session_key(payload["tid"])
    record = fake_store.get(record_key)
    record["scopes"] = "publish"  # リストでない不正な形
    fake_store.data[record_key] = record

    with pytest.raises(security.InvalidCredentialError):
        security.verify_agent_token(fake_store, token, signing_key=KEY, now=NOW + 1)
