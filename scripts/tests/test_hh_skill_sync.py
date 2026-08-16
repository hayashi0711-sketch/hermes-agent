"""scripts/tests/test_hh_skill_sync.py — Lane C 同期（S-10/S-11/S-12/S-06b）のテスト。

`.lane_c_wave4_sync_task.md`（テスト節）の S-14 要件に対応する。既存
`test_hh_skill_promote_lane_c.py` の隔離パターン（hh_agent_home / hermes
ルート / スキャン対象の monkeypatch）を踏襲し、HTTP は
`skill_sync._urlopen` の fake、通知は `ntfy_client.send_skill_conflict` /
`send_skill_sync_event` の fake で**実 Lane C・実 ntfy へ一切接続しない**。
`promote_lock` は本物を使うが `base=tmp_path` に置く（実ホームへ触らない）。

カバーする S-14 項目:
- 通常の CAS 成功（衝突でない）で通知が 0 件（10 回繰り返し）
- 衝突の全分岐で send_skill_conflict() 相当が呼ばれ、失敗しても同期自体は
  成功し promote_log.jsonl に記録が残る
- 通知本文に SKILL.md 本文が含まれない（notifier へ渡る event の形で検証）
- 緊急停止スイッチと denylist が機能する（pull/push 双方止まる・
  name/digest が除外される）
- receipt が無い/改変された pull 候補が ~/.hermes/skills/ に一切書き込まれない
- push 候補に promote_receipts/current（来歴確認）が無い場合は送らず
  skipped(no-valid-receipt)
- promote_lock が取れない場合 sync_pull() が "skipped(locked)" を返し
  正常終了する（ハングしない）
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _d in (REPO_ROOT, SCRIPTS_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import hh_agent_promote_lock as lock_mod  # noqa: E402
import hh_skill_promote as promote  # noqa: E402
import hh_skill_sync as sync_mod  # noqa: E402
from modal_hub.services import ntfy_client, skill_sync  # noqa: E402

DUMMY_SIGNING_KEY = "k" * 64  # HMAC の鍵。テスト専用ダミー
DUMMY_WRITE_KEY = "w" * 64

BASE_URL = "https://lane.test"
PROMOTED_AT_MS = 1_755_300_000_123


def _skill_md(name: str, description: str = "d") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n"


def _write_local_skill(env: Path, name: str, content: str) -> None:
    """SKILL.md フィクスチャをバイト列として書く。

    `Path.write_text()` は Windows で `\n` を `\r\n` へ無言変換するため
    （このプロジェクトで既知の罠。`skill_quarantine._atomic_write_text()` の
    docstring参照）、`content` の sha256（テスト内で独立に計算した digest・
    receipt の署名対象）と、実際に書かれたファイルの実測 sha256 が食い違う。
    必ずバイト列で書く。
    """
    (env / name).mkdir(parents=True, exist_ok=True)
    (env / name / "SKILL.md").write_bytes(content.encode("utf-8"))


def _sign(name: str, content: str, *, seq: int = 1, origin: str = "win-test") -> tuple[str, str]:
    """ダミー鍵で receipt を署名する。戻り値: (digest, receipt)。"""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    key = DUMMY_SIGNING_KEY.encode("utf-8")
    key_id = skill_sync.derive_key_id(key)
    receipt = skill_sync.write_receipt(
        name, content, digest, seq, PROMOTED_AT_MS, origin, None,
        signing_key=key, key_id=key_id,
    )
    return digest, receipt


# ---------------------------------------------------------------------------
# fake HTTP（実 Lane C へ接続しない）
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


def _list_response(
    name: str, digest: str, *, revision: int = 5, seq: int = 1,
    origin: str = "win-test", events: list | None = None,
) -> dict:
    return {
        "skills": [{
            "name": name,
            "content_sha256": digest,
            "revision": revision,
            "origin_instance": origin,
            "promotion_seq": seq,
        }],
        "events": events or [],
        "next_cursor": None,
    }


def _pull_response(
    name: str, content: str, digest: str, receipt: str, *,
    revision: int = 5, seq: int = 1, origin: str = "win-test",
) -> dict:
    return {
        "name": name,
        "content": content,
        "content_sha256": digest,
        "revision": revision,
        "receipt": receipt,
        "origin_instance": origin,
        "promoted_at_ms": PROMOTED_AT_MS,
        "promotion_seq": seq,
        "distilled_from_session_id": None,
        "received_at": "2026-08-17T00:00:00Z",
    }


def _list_json(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# 隔離 fixture（実ホーム・実 Hermes スキャン対象へ触らない）
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """`~/.hh-agent` → tmp_path、`~/.hermes/skills` → tmp_path/hermes_skills、
    Hermes スキャン対象 → 空、に固定する。戻り値は hermes ルート。"""
    hermes_root = tmp_path / "hermes_skills"
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    monkeypatch.setattr(promote, "_hermes_skills_root", lambda: hermes_root)
    monkeypatch.setattr(promote, "_existing_hermes_scan_dirs", lambda: [])
    monkeypatch.setattr(promote, "_declared_hermes_scan_dirs_including_nonexistent", lambda: [])
    return hermes_root


@pytest.fixture()
def signing_env(monkeypatch):
    """`.hh-signing.env` 相当: 署名鍵・書き込み鍵をダミーで返し、PREV は無し。"""
    monkeypatch.setattr(
        promote, "_load_signing_env",
        lambda: {promote.SIGNING_KEY_VAR: DUMMY_SIGNING_KEY,
                 promote.WRITE_KEY_VAR: DUMMY_WRITE_KEY},
    )
    monkeypatch.setattr(sync_mod, "_read_signing_env_value", lambda name: None)


@pytest.fixture()
def ntfy(monkeypatch):
    """ntfy 送信関数を記録 fake に差し替える（実 HTTP は行わない）。

    `outcomes` に {"conflict": "failed"} 等を入れると該当種別が失敗する。
    戻り値: sent（送信された event の記録）/ outcomes / failed を保持する。
    """
    state = SimpleNamespace(sent=[], failed=[], outcomes={})

    def _send(kind: str, event: dict) -> str:
        state.sent.append(event)
        if state.outcomes.get(kind) == "failed":
            state.failed.append(event)
            return "failed"
        return "sent"

    monkeypatch.setattr(
        ntfy_client, "send_skill_conflict",
        lambda event: _send("conflict", event),
    )
    monkeypatch.setattr(
        ntfy_client, "send_skill_sync_event",
        lambda event: _send("sync_event", event),
    )
    return state


def _write_config(tmp_path) -> None:
    (tmp_path / promote.LANE_C_CONFIG_FILENAME).write_text(
        json.dumps({"base_url": BASE_URL, "read_key": "rk"}), encoding="utf-8"
    )


def _seed_receipt(tmp_path, name: str, content: str, *, seq: int = 1) -> str:
    """promote_receipts/<name>/ に receipt record + current を保存する（S-06b 形式）。"""
    digest, receipt = _sign(name, content, seq=seq)
    name_dir = tmp_path / "promote_receipts" / name
    name_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{digest[:8]}-{hashlib.sha256(receipt.encode('utf-8')).hexdigest()[:8]}.json"
    record = {
        "name": name,
        "content_sha256": digest,
        "origin_instance": "win-test",
        "promoted_at_ms": PROMOTED_AT_MS,
        "promotion_seq": seq,
        "distilled_from_session_id": None,
        "key_id": skill_sync.derive_key_id(DUMMY_SIGNING_KEY.encode("utf-8")),
        "receipt": receipt,
    }
    (name_dir / filename).write_text(json.dumps(record), encoding="utf-8")
    (name_dir / "current").write_text(filename, encoding="utf-8")
    return receipt


def _write_state(tmp_path, name: str, content_sha256: str, revision: int) -> None:
    (tmp_path / sync_mod.SYNC_STATE_FILENAME).write_text(
        json.dumps({name: {"content_sha256": content_sha256, "lane_c_revision": revision}}),
        encoding="utf-8",
    )


def _promote_log_lines(tmp_path) -> list[dict]:
    path = tmp_path / "promote_log.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# 1a: 緊急停止・denylist（S-12）
# ---------------------------------------------------------------------------


def test_is_sync_disabled_flag_file(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "hh_agent_home", lambda: tmp_path)
    assert not sync_mod.is_sync_disabled(base=tmp_path)
    (tmp_path / sync_mod.DISABLED_FLAG_FILENAME).write_text("", encoding="utf-8")
    assert sync_mod.is_sync_disabled(base=tmp_path)


def test_is_sync_disabled_env_var(monkeypatch):
    monkeypatch.delenv(sync_mod.DISABLED_ENV_VAR, raising=False)
    assert not sync_mod.is_sync_disabled()
    monkeypatch.setenv(sync_mod.DISABLED_ENV_VAR, "1")
    assert sync_mod.is_sync_disabled()


def test_denylist_load_and_deny(tmp_path):
    assert sync_mod.load_denylist(base=tmp_path) == {"names": [], "content_sha256": []}
    (tmp_path / sync_mod.DENYLIST_FILENAME).write_text(
        json.dumps({"names": ["alpha"], "content_sha256": ["0" * 64, 123]}),
        encoding="utf-8",
    )
    denylist = sync_mod.load_denylist(base=tmp_path)
    assert denylist == {"names": ["alpha"], "content_sha256": ["0" * 64]}  # 型不正は弾く
    assert sync_mod.is_denied("alpha", "1" * 64, denylist)
    assert sync_mod.is_denied("beta", "0" * 64, denylist)
    assert not sync_mod.is_denied("beta", "1" * 64, denylist)


def test_denylist_corrupt_file_is_fail_open(tmp_path):
    (tmp_path / sync_mod.DENYLIST_FILENAME).write_text("not json", encoding="utf-8")
    assert sync_mod.load_denylist(base=tmp_path) == {"names": [], "content_sha256": []}


# ---------------------------------------------------------------------------
# 1b/1c: 同期状態・accepted_seq・receipt の記録
# ---------------------------------------------------------------------------


def test_load_sync_state_filters_corrupt(tmp_path):
    (tmp_path / sync_mod.SYNC_STATE_FILENAME).write_text("not json", encoding="utf-8")
    assert sync_mod.load_sync_state(base=tmp_path) == {}
    (tmp_path / sync_mod.SYNC_STATE_FILENAME).write_text(
        json.dumps({
            "ok": {"content_sha256": "a" * 64, "lane_c_revision": 3},
            "bad-rev": {"content_sha256": "b" * 64, "lane_c_revision": -1},
            "bad-sha": {"content_sha256": 123, "lane_c_revision": 1},
            "not-dict": "x",
        }),
        encoding="utf-8",
    )
    assert sync_mod.load_sync_state(base=tmp_path) == {
        "ok": {"content_sha256": "a" * 64, "lane_c_revision": 3}
    }


def test_save_and_load_sync_state_roundtrip(tmp_path):
    sync_mod.save_sync_state({"alpha": {"content_sha256": "a" * 64, "lane_c_revision": 3}}, base=tmp_path)
    assert sync_mod.load_sync_state(base=tmp_path) == {
        "alpha": {"content_sha256": "a" * 64, "lane_c_revision": 3}
    }


def test_update_accepted_seq_keeps_max(tmp_path):
    assert sync_mod.load_accepted_seq("s1", base=tmp_path) == {}
    sync_mod.update_accepted_seq("s1", "win-1", 3, base=tmp_path)
    assert sync_mod.load_accepted_seq("s1", base=tmp_path) == {"win-1": 3}
    sync_mod.update_accepted_seq("s1", "win-1", 1, base=tmp_path)  # 遡りは無視
    sync_mod.update_accepted_seq("s1", "modal-1", 0, base=tmp_path)
    assert sync_mod.load_accepted_seq("s1", base=tmp_path) == {"win-1": 3, "modal-1": 0}
    with pytest.raises(ValueError):
        sync_mod.update_accepted_seq("s1", "win-1", -1, base=tmp_path)


def test_save_verified_receipt_writes_record_and_current_once(tmp_path, monkeypatch):
    content = _skill_md("rec-skill")
    digest, receipt = _sign("rec-skill", content)
    pulled = skill_sync.validate_pulled_skill(
        _pull_response("rec-skill", content, digest, receipt),
        verify_keys={skill_sync.derive_key_id(DUMMY_SIGNING_KEY.encode("utf-8")): DUMMY_SIGNING_KEY.encode("utf-8")},
    )
    writes: list = []
    original_json = promote._atomic_write_json
    original_text = promote._atomic_write_text
    monkeypatch.setattr(
        promote, "_atomic_write_json",
        lambda path, data: (writes.append(f"json:{path.name}"), original_json(path, data))[1],
    )
    monkeypatch.setattr(
        promote, "_atomic_write_text",
        lambda path, text: (writes.append(f"text:{path.name}"), original_text(path, text))[1],
    )

    sync_mod.save_verified_receipt("rec-skill", pulled, base=tmp_path)
    name_dir = tmp_path / "promote_receipts" / "rec-skill"
    current = (name_dir / "current").read_text(encoding="utf-8").strip()
    record = json.loads((name_dir / current).read_text(encoding="utf-8"))
    assert record["content_sha256"] == digest
    assert record["receipt"] == receipt
    assert record["key_id"] == skill_sync.derive_key_id(DUMMY_SIGNING_KEY.encode("utf-8"))
    assert len(writes) == 2  # record（json）+ current（text）

    sync_mod.save_verified_receipt("rec-skill", pulled, base=tmp_path)  # べき等
    assert len(writes) == 2  # 同じ receipt なら書き直さない


def test_load_verify_keys_current_and_prev(tmp_path, monkeypatch):
    monkeypatch.setattr(promote, "_load_signing_env", lambda: {promote.SIGNING_KEY_VAR: DUMMY_SIGNING_KEY})
    monkeypatch.setattr(sync_mod, "_read_signing_env_value", lambda name: "p" * 64)
    keys = sync_mod.load_verify_keys()
    assert set(keys) == {
        skill_sync.derive_key_id(DUMMY_SIGNING_KEY.encode("utf-8")),
        skill_sync.derive_key_id(("p" * 64).encode("utf-8")),
    }


# ---------------------------------------------------------------------------
# 1d: アウトボックス（S-11）
# ---------------------------------------------------------------------------


def test_append_outbox_dedup(tmp_path):
    event = {"event": "skill_sync_validation_failed", "name": "x", "reason": "r"}
    first = sync_mod.append_outbox(event, base=tmp_path)
    second = sync_mod.append_outbox(event, base=tmp_path)
    assert first == second  # 内容ハッシュで同一 ID
    lines = (tmp_path / sync_mod.OUTBOX_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # 重複追記しない


def test_flush_outbox_removes_only_sent(tmp_path, ntfy):
    e_fail = {"event": "skill_sync_validation_failed", "name": "a", "reason": "r1"}
    e_ok = {
        "event": "skill_conflict", "name": "b",
        "winner": "win-1", "winner_sha8": "a" * 8, "loser_sha8": "b" * 8,
    }
    id_fail = sync_mod.append_outbox(e_fail, base=tmp_path)
    sync_mod.append_outbox(e_ok, base=tmp_path)
    ntfy.outcomes["sync_event"] = "failed"  # sync_event 種別だけ失敗させる（1 件目）
    result = sync_mod.flush_outbox(base=tmp_path)
    assert result["attempted"] == 2 and result["sent"] == 1 and result["failed"] == 1
    assert result["results"][id_fail] == "failed"
    # 失敗した行だけが残る（次回再送）
    remaining = (tmp_path / sync_mod.OUTBOX_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(remaining) == 1
    assert json.loads(remaining[0])["event_id"] == id_fail


# ---------------------------------------------------------------------------
# sync_pull（S-10。ロック・配置・state 更新）
# ---------------------------------------------------------------------------


def test_sync_pull_locked_returns_skipped_without_hanging(tmp_path, env, signing_env):
    content = _skill_md("lock-skill")
    digest, receipt = _sign("lock-skill", content)
    pulled = skill_sync.validate_pulled_skill(
        _pull_response("lock-skill", content, digest, receipt),
        verify_keys={skill_sync.derive_key_id(DUMMY_SIGNING_KEY.encode("utf-8")): DUMMY_SIGNING_KEY.encode("utf-8")},
    )
    with lock_mod.promote_lock(base=tmp_path):  # 本物のロックを保持したまま
        assert sync_mod.sync_pull("lock-skill", pulled, base=tmp_path) == "skipped(locked)"
    # 何も書き込まれていない（ハングしない・正常終了）
    assert not (env / "lock-skill").exists()
    assert not (tmp_path / sync_mod.SYNC_STATE_FILENAME).exists()


def test_sync_pull_installs_receipt_state_and_log(tmp_path, env, signing_env):
    content = _skill_md("pull-skill")
    digest, receipt = _sign("pull-skill", content)
    pulled = skill_sync.validate_pulled_skill(
        _pull_response("pull-skill", content, digest, receipt),
        verify_keys={skill_sync.derive_key_id(DUMMY_SIGNING_KEY.encode("utf-8")): DUMMY_SIGNING_KEY.encode("utf-8")},
    )
    state: dict = {}
    assert sync_mod.sync_pull("pull-skill", pulled, base=tmp_path, state=state) == "pulled"
    # 配置
    assert (env / "pull-skill" / "SKILL.md").read_text(encoding="utf-8") == content
    # receipt + current
    current = (tmp_path / "promote_receipts" / "pull-skill" / "current").read_text(encoding="utf-8").strip()
    record = json.loads((tmp_path / "promote_receipts" / "pull-skill" / current).read_text(encoding="utf-8"))
    assert record["receipt"] == receipt
    # accepted_seq
    assert sync_mod.load_accepted_seq("pull-skill", base=tmp_path) == {"win-test": 1}
    # state 更新
    assert state["pull-skill"] == {"content_sha256": digest, "lane_c_revision": 5}
    # 監査（provenance は f"sync-pull:{origin}"）
    log = _promote_log_lines(tmp_path)
    assert log[-1]["provenance"] == "sync-pull:win-test"
    assert log[-1]["name"] == "pull-skill"


# ---------------------------------------------------------------------------
# run_sync E2E（全 S-14 必須項目）
# ---------------------------------------------------------------------------


def test_disabled_flag_stops_everything(tmp_path, env, signing_env, fake_http, ntfy):
    _write_config(tmp_path)
    (tmp_path / sync_mod.DISABLED_FLAG_FILENAME).write_text("", encoding="utf-8")
    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)
    assert result["disabled"] is True
    assert fake_http.calls == []  # HTTP も一切しない
    assert ntfy.sent == []
    assert not (tmp_path / sync_mod.OUTBOX_FILENAME).exists()


def test_no_config_warns_and_returns(tmp_path, env, signing_env, fake_http, ntfy):
    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)
    assert result["config_present"] is False
    assert result["disabled"] is False
    assert fake_http.calls == []


def test_list_failure_retries_next_run(tmp_path, env, signing_env, fake_http, ntfy):
    _write_config(tmp_path)
    fake_http.script.append(urllib.error.URLError("boom"))
    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)
    assert result["list_failed"] is True
    assert ntfy.sent == []


def test_normal_cas_success_zero_notifications_ten_times(tmp_path, env, signing_env, fake_http, ntfy):
    """S-14「通常の CAS 成功で通知が 0 件」の回帰テスト（10 回繰り返し）。

    1 回目: リモートのみ存在 → pull して配置。2〜10 回目: 同一内容・同一
    revision → noop。どの回も通知（サーバーイベント・outbox とも）ゼロ。
    """
    _write_config(tmp_path)
    name = "my-skill"
    content = _skill_md(name)
    digest, receipt = _sign(name, content)
    list_payload = _list_response(name, digest)
    pull_payload = _pull_response(name, content, digest, receipt)

    results = []
    for i in range(10):
        fake_http.script.append((200, _list_json(list_payload)))
        if i == 0:
            fake_http.script.append((200, _list_json(pull_payload)))
        results.append(sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path))

    assert results[0]["pulled"] == [name]
    for i in range(1, 10):
        assert results[i]["noop"] == [name]
    for result in results:
        assert result["notifications"] == {"attempted": 0, "sent": 0, "failed": 0}
    assert ntfy.sent == []  # 10 回通して通知ゼロ
    assert (env / name / "SKILL.md").read_text(encoding="utf-8") == content
    # 1 回目の list+pull + 2〜10 回目の list = 11 リクエスト
    assert len(fake_http.calls) == 11


def test_validation_failure_writes_nothing(tmp_path, env, signing_env, fake_http, ntfy):
    """敵対的認証テスト: receipt が改変された pull 候補は ~/.hermes/skills/ に
    一切書き込まれない（署名検証失敗 → 通知 + スキップ）。"""
    _write_config(tmp_path)
    name = "evil-skill"
    content = _skill_md(name)
    digest, _ = _sign(name, content)
    fake_http.script.append((200, _list_json(_list_response(name, digest))))
    # receipt を別の鍵で署名したことにして返す（検証に落ちる）
    other_key = b"z" * 32
    tampered_receipt = skill_sync.write_receipt(
        name, content, digest, 1, PROMOTED_AT_MS, "win-test", None,
        signing_key=other_key, key_id=skill_sync.derive_key_id(other_key),
    )
    fake_http.script.append((200, _list_json(_pull_response(name, content, digest, tampered_receipt))))

    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)

    assert result["validation_failures"] == [name]
    assert result["pulled"] == []
    assert not (env / name).exists()  # 書き込みゼロ
    assert not (tmp_path / "promote_receipts" / name).exists()
    # 通知イベントが outbox 経由で送られる（本文に SKILL.md 内容は含まれない）
    assert len(ntfy.sent) == 1
    event = ntfy.sent[0]
    assert event["event"] == "skill_sync_validation_failed"
    assert set(event) == {"event", "name", "reason"}
    assert content not in json.dumps(event)


def test_missing_receipt_writes_nothing(tmp_path, env, signing_env, fake_http, ntfy):
    _write_config(tmp_path)
    name = "no-receipt"
    content = _skill_md(name)
    digest, _ = _sign(name, content)
    fake_http.script.append((200, _list_json(_list_response(name, digest))))
    pull = _pull_response(name, content, digest, "")
    pull["receipt"] = None  # receipt 欠落
    fake_http.script.append((200, _list_json(pull)))

    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)

    assert result["validation_failures"] == [name]
    assert not (env / name).exists()
    assert not (tmp_path / "promote_receipts" / name).exists()


def test_conflict_full_flow(tmp_path, env, signing_env, fake_http, ntfy):
    """S-10 衝突解決手順（1248〜1254 行目）: 通知→写し→pull→配置→監査。"""
    _write_config(tmp_path)
    name = "conflict-skill"
    old_content = _skill_md(name, description="old")
    local_content = _skill_md(name, description="local")
    remote_content = _skill_md(name, description="remote")
    old_digest = hashlib.sha256(old_content.encode("utf-8")).hexdigest()
    local_digest = hashlib.sha256(local_content.encode("utf-8")).hexdigest()
    remote_digest, remote_receipt = _sign(name, remote_content, seq=9)
    # ローカル: 同期点（old_digest, rev 5）から進んだ別内容
    _write_local_skill(env, name, local_content)
    _write_state(tmp_path, name, old_digest, 5)
    # リモート: 同期点（rev 5）から進んだ別内容 → conflict
    fake_http.script.append((200, _list_json(_list_response(name, remote_digest, revision=9, seq=9))))
    fake_http.script.append((200, _list_json(_pull_response(name, remote_content, remote_digest, remote_receipt, revision=9, seq=9))))

    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)

    # (1) 通知が先に出る（conflict 専用の 5 フィールド本文）
    assert len(ntfy.sent) == 1
    event = ntfy.sent[0]
    assert set(event) == {"event", "name", "winner", "winner_sha8", "loser_sha8"}
    assert event["winner_sha8"] == remote_digest[:8]
    assert event["loser_sha8"] == local_digest[:8]
    assert "content" not in event and "diff" not in event
    # (2) ローカル版の写しが promote_backups/<name>.conflict-local.*/ に残る
    backups = list((tmp_path / "promote_backups").glob(f"{name}.conflict-local.*"))
    assert backups
    assert (backups[0] / "SKILL.md").read_text(encoding="utf-8") == local_content
    # (3) リモート版が配置される
    assert (env / name / "SKILL.md").read_text(encoding="utf-8") == remote_content
    # (4) promote_log.jsonl に provenance="sync-conflict" で記録（通知結果付き）
    log = _promote_log_lines(tmp_path)
    conflict_line = [line for line in log if line.get("provenance") == "sync-conflict"]
    assert len(conflict_line) == 1
    assert conflict_line[0]["notify_state"] == "sent"
    assert conflict_line[0]["winner_sha8"] == remote_digest[:8]
    assert conflict_line[0]["loser_sha8"] == local_digest[:8]
    assert "backup_path" in conflict_line[0]
    # (5) 自動 push しない
    assert result["conflicts"] == [name]
    assert result["pushed"] == []
    # state はリモート版（rev 9）に更新される
    assert sync_mod.load_sync_state(base=tmp_path)[name] == {
        "content_sha256": remote_digest, "lane_c_revision": 9
    }


def test_conflict_notification_failure_still_succeeds(tmp_path, env, signing_env, fake_http, ntfy):
    """衝突通知の送信に失敗しても同期自体は成功し、promote_log.jsonl に
    notify_state="failed" で記録が残る（S-14）。失敗イベントは outbox に
    残り次回再送される。"""
    _write_config(tmp_path)
    name = "conflict-fail"
    old_content = _skill_md(name, description="old")
    local_content = _skill_md(name, description="local")
    remote_content = _skill_md(name, description="remote")
    old_digest = hashlib.sha256(old_content.encode("utf-8")).hexdigest()
    remote_digest, remote_receipt = _sign(name, remote_content, seq=9)
    _write_local_skill(env, name, local_content)
    _write_state(tmp_path, name, old_digest, 5)
    fake_http.script.append((200, _list_json(_list_response(name, remote_digest, revision=9, seq=9))))
    fake_http.script.append((200, _list_json(_pull_response(name, remote_content, remote_digest, remote_receipt, revision=9, seq=9))))
    ntfy.outcomes["conflict"] = "failed"

    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)  # 例外は出ない

    assert result["conflicts"] == [name]
    assert result["notifications"] == {"attempted": 1, "sent": 0, "failed": 1}
    assert (env / name / "SKILL.md").read_text(encoding="utf-8") == remote_content  # 同期は成功
    log = _promote_log_lines(tmp_path)
    conflict_line = [line for line in log if line.get("provenance") == "sync-conflict"]
    assert conflict_line[0]["notify_state"] == "failed"
    # 送信に失敗したイベントは outbox に残る（次回再送）
    remaining = (tmp_path / sync_mod.OUTBOX_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(remaining) == 1
    assert json.loads(remaining[0])["event"]["event"] == "skill_conflict"


def test_push_without_receipt_skipped(tmp_path, env, signing_env, fake_http, ntfy, monkeypatch):
    """push 候補に promote_receipts/current（来歴確認）が無い場合は送らず
    skipped(no-valid-receipt)。"""
    _write_config(tmp_path)
    name = "push-no-receipt"
    remote_digest = "a" * 64
    local_content = _skill_md(name, description="new-local")
    local_digest = hashlib.sha256(local_content.encode("utf-8")).hexdigest()
    _write_local_skill(env, name, local_content)
    _write_state(tmp_path, name, remote_digest, 5)
    # リモートは同期点のまま（sha=state.sha, rev=5）→ ローカルが進んだ → push 候補
    fake_http.script.append((200, _list_json(_list_response(name, remote_digest, revision=5))))
    pushed: list = []
    monkeypatch.setattr(promote, "push_to_lane_c", lambda **kwargs: pushed.append(kwargs))

    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)

    assert result["skipped_no_valid_receipt"] == [name]
    assert pushed == []  # 送らない
    log = _promote_log_lines(tmp_path)
    assert log[-1]["provenance"] == "sync-push-skipped"
    assert "no-valid-receipt" in log[-1]["reason"]
    assert local_digest != remote_digest  # 前提の確認


def test_push_with_current_receipt_sends_without_resigning(tmp_path, env, signing_env, fake_http, ntfy, monkeypatch):
    """current receipt がローカル digest と一致する場合のみ push し、
    receipt は既存の current から読む（新規に署名し直さない）。"""
    _write_config(tmp_path)
    name = "push-ok"
    remote_digest = "a" * 64
    local_content = _skill_md(name, description="new-local")
    local_digest = hashlib.sha256(local_content.encode("utf-8")).hexdigest()
    _write_local_skill(env, name, local_content)
    _write_state(tmp_path, name, remote_digest, 5)
    stored_receipt = _seed_receipt(tmp_path, name, local_content)  # 来歴（新ローカル版の receipt）
    fake_http.script.append((200, _list_json(_list_response(name, remote_digest, revision=5))))
    pushed: dict = {}
    monkeypatch.setattr(promote, "push_to_lane_c", lambda **kwargs: pushed.update(kwargs))

    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)

    assert result["pushed"] == [name]
    assert pushed["name"] == name
    assert pushed["receipt"] == stored_receipt  # 署名し直していない
    assert pushed["digest"] == local_digest
    assert pushed["base_revision"] == 5  # state の同期点を CAS の base に使う
    # push 後の state は誤って更新しない（応答の revision が返らないため。
    # 次回実行時に metadata_repair が正しい revision へ進める）
    assert sync_mod.load_sync_state(base=tmp_path)[name]["lane_c_revision"] == 5


def test_push_deferred_when_reconcile_false(tmp_path, env, signing_env, fake_http, ntfy, monkeypatch):
    """Modal 側（reconcile=False）では push は実行されず deferred に集計される。"""
    _write_config(tmp_path)
    name = "push-deferred"
    remote_digest = "a" * 64
    local_content = _skill_md(name, description="new-local")
    _write_local_skill(env, name, local_content)
    _write_state(tmp_path, name, remote_digest, 5)
    _seed_receipt(tmp_path, name, local_content)
    fake_http.script.append((200, _list_json(_list_response(name, remote_digest, revision=5))))
    pushed: list = []
    monkeypatch.setattr(promote, "push_to_lane_c", lambda **kwargs: pushed.append(kwargs))

    result = sync_mod.run_sync(pull=True, reconcile=False, base=tmp_path)

    assert result["push_deferred"] == [name]
    assert pushed == []


def test_denylist_blocks_pull_by_name_and_digest(tmp_path, env, signing_env, fake_http, ntfy):
    _write_config(tmp_path)
    name = "denied-skill"
    content = _skill_md(name)
    digest, _ = _sign(name, content)
    (tmp_path / sync_mod.DENYLIST_FILENAME).write_text(
        json.dumps({"names": [name], "content_sha256": []}), encoding="utf-8"
    )
    fake_http.script.append((200, _list_json(_list_response(name, digest))))
    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)
    assert result["denied"] == [name]
    assert result["observed"] == []
    assert len(fake_http.calls) == 1  # list のみ。pull は呼ばれない
    assert not (env / name).exists()

    # digest 指定でも除外される
    other = "digest-denied"
    other_content = _skill_md(other)
    other_digest, _ = _sign(other, other_content)
    (tmp_path / sync_mod.DENYLIST_FILENAME).write_text(
        json.dumps({"names": [], "content_sha256": [other_digest]}), encoding="utf-8"
    )
    fake_http.script.append((200, _list_json(_list_response(other, other_digest))))
    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)
    assert result["denied"] == [other]
    assert not (env / other).exists()


def test_denylist_blocks_push(tmp_path, env, signing_env, fake_http, ntfy, monkeypatch):
    """denylist は pull だけでなく push 候補も止める（S-12）。"""
    _write_config(tmp_path)
    name = "denied-push"
    remote_digest = "a" * 64
    local_content = _skill_md(name, description="new-local")
    _write_local_skill(env, name, local_content)
    _write_state(tmp_path, name, remote_digest, 5)
    _seed_receipt(tmp_path, name, local_content)
    (tmp_path / sync_mod.DENYLIST_FILENAME).write_text(
        json.dumps({"names": [name], "content_sha256": []}), encoding="utf-8"
    )
    fake_http.script.append((200, _list_json(_list_response(name, remote_digest, revision=5))))
    pushed: list = []
    monkeypatch.setattr(promote, "push_to_lane_c", lambda **kwargs: pushed.append(kwargs))

    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)

    assert result["denied"] == [name]
    assert pushed == []


def test_integrity_anomaly_notifies_and_writes_nothing(tmp_path, env, signing_env, fake_http, ntfy):
    """フェーズ A の整合性異常（revision の巻き戻り）→ 通知 + ローカルへ書かない。"""
    _write_config(tmp_path)
    name = "rollback-skill"
    content = _skill_md(name)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    _write_local_skill(env, name, content)
    _write_state(tmp_path, name, digest, 5)
    fake_http.script.append((200, _list_json(_list_response(name, digest, revision=3, seq=1))))  # 5 → 3 に巻き戻り

    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)

    assert result["integrity_anomalies"] == [name]
    assert result["noop"] == [] and result["pulled"] == []
    assert (env / name / "SKILL.md").read_text(encoding="utf-8") == content  # 変更なし
    assert len(ntfy.sent) == 1
    assert ntfy.sent[0]["event"] == "skill_sync_integrity_anomaly"
    assert content not in json.dumps(ntfy.sent[0])


def test_malformed_list_entry_is_integrity_anomaly(tmp_path, env, signing_env, fake_http, ntfy):
    _write_config(tmp_path)
    payload = {
        "skills": [{"name": "bad-entry", "content_sha256": "0" * 64, "revision": -1, "promotion_seq": 1}],
        "events": [],
        "next_cursor": None,
    }
    fake_http.script.append((200, _list_json(payload)))
    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)
    assert result["integrity_anomalies"] == ["bad-entry"]
    assert not (env / "bad-entry").exists()
    assert ntfy.sent[0]["event"] == "skill_sync_integrity_anomaly"


def test_server_events_notified_then_acked(tmp_path, env, signing_env, fake_http, ntfy):
    """サーバーイベントは通知に成功したものだけ ACK する（S-11）。"""
    _write_config(tmp_path)
    # skills を空にする（skills が 1 件でもあると pull が発火して
    # script エントリを消費し、ACK の位置がずれる）
    payload = {"skills": [], "events": [{"event_id": "e1", "name": "alpha", "type": "publish"}], "next_cursor": None}
    fake_http.script.append((200, _list_json(payload)))
    fake_http.script.append((200, b"{}"))  # ack 応答

    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)

    assert len(ntfy.sent) == 1
    assert ntfy.sent[0] == {"event": "skill_sync_server_event", "name": "alpha", "reason": "server event type=publish"}
    assert result["events_seen"] == 1 and result["events_acked"] == 1
    # 2 リクエスト目が ACK
    (req, _) = fake_http.calls[1]
    assert req.get_method() == "POST"
    assert "events/ack" in req.full_url
    assert json.loads(req.data.decode("utf-8")) == {"event_ids": ["e1"]}


def test_server_events_not_acked_on_notification_failure(tmp_path, env, signing_env, fake_http, ntfy):
    """通知失敗時は ACK しない（サーバー側が durable に保持し次回再通知）。"""
    _write_config(tmp_path)
    payload = {"skills": [], "events": [{"event_id": "e1", "type": "publish"}], "next_cursor": None}
    fake_http.script.append((200, _list_json(payload)))
    ntfy.outcomes["sync_event"] = "failed"
    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)
    assert result["events_seen"] == 1 and result["events_acked"] == 0
    assert len(fake_http.calls) == 1  # ACK は送られない


def test_metadata_repair_updates_state_only(tmp_path, env, signing_env, fake_http, ntfy, monkeypatch):
    """内容一致・revision だけ前進 → 本文は書き換えず receipt/seq/state を自己修復。"""
    _write_config(tmp_path)
    name = "repair-skill"
    content = _skill_md(name)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    _write_local_skill(env, name, content)
    _write_state(tmp_path, name, digest, 5)
    _, receipt = _sign(name, content, seq=7)
    fake_http.script.append((200, _list_json(_list_response(name, digest, revision=9, seq=7))))
    fake_http.script.append((200, _list_json(_pull_response(name, content, digest, receipt, revision=9, seq=7))))
    installed: list = []
    monkeypatch.setattr(promote, "install_confirmed_skill", lambda *a, **kw: installed.append(kw))

    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)

    assert result["metadata_repair"] == [name]
    assert installed == []  # 本文は書き換えない
    assert (env / name / "SKILL.md").read_text(encoding="utf-8") == content
    assert sync_mod.load_sync_state(base=tmp_path)[name] == {
        "content_sha256": digest, "lane_c_revision": 9
    }
    assert sync_mod.load_accepted_seq(name, base=tmp_path) == {"win-test": 7}
    current = (tmp_path / "promote_receipts" / name / "current").read_text(encoding="utf-8").strip()
    assert json.loads((tmp_path / "promote_receipts" / name / current).read_text(encoding="utf-8"))["receipt"] == receipt


def test_metadata_repair_validation_failure_updates_nothing(tmp_path, env, signing_env, fake_http, ntfy):
    """自己修復でも pull 検証は必須 — 検証に落ちたら何も更新しない。"""
    _write_config(tmp_path)
    name = "repair-bad"
    content = _skill_md(name)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    _write_local_skill(env, name, content)
    _write_state(tmp_path, name, digest, 5)
    bad_receipt = "12345678." + "A" * 43  # 形式不正
    fake_http.script.append((200, _list_json(_list_response(name, digest, revision=9, seq=7))))
    fake_http.script.append((200, _list_json(_pull_response(name, content, digest, bad_receipt, revision=9, seq=7))))

    result = sync_mod.run_sync(pull=True, reconcile=True, base=tmp_path)

    assert result["metadata_repair"] == []
    assert result["validation_failures"] == [name]
    # state は巻き戻らない
    assert sync_mod.load_sync_state(base=tmp_path)[name]["lane_c_revision"] == 5


def test_dry_run_classifies_only(tmp_path, env, signing_env, fake_http, ntfy):
    """dry_run: 判定結果だけを集計し、書き込み・pull・通知・ロックを一切行わない。"""
    _write_config(tmp_path)
    name = "dry-skill"
    content = _skill_md(name)
    digest, _ = _sign(name, content)
    fake_http.script.append((200, _list_json(_list_response(name, digest))))

    result = sync_mod.run_sync(pull=True, reconcile=True, dry_run=True, base=tmp_path)

    assert result["pulled"] == [name]  # 判定は出る
    assert len(fake_http.calls) == 1  # list のみ（pull は実行しない）
    assert ntfy.sent == []
    assert not (env / name).exists()
    assert not (tmp_path / sync_mod.OUTBOX_FILENAME).exists()
    assert not (tmp_path / "locks").exists()  # ロックも取らない


def test_pull_deferred_when_pull_false(tmp_path, env, signing_env, fake_http, ntfy):
    _write_config(tmp_path)
    name = "defer-pull"
    content = _skill_md(name)
    digest, _ = _sign(name, content)
    fake_http.script.append((200, _list_json(_list_response(name, digest))))
    result = sync_mod.run_sync(pull=False, reconcile=False, base=tmp_path)
    assert result["pull_deferred"] == [name]
    assert result["pulled"] == []
    assert len(fake_http.calls) == 1  # pull は実行しない


# ---------------------------------------------------------------------------
# CLI（argparse 契約: v1 に --forget は無い）
# ---------------------------------------------------------------------------


def test_main_rejects_forget_flag():
    with pytest.raises(SystemExit) as exc:
        sync_mod.main(["--forget"])
    assert exc.value.code == 2
