"""共有フィクスチャ。

方針:

- **Modal に触らない。** `modal.Dict` / `modal.Volume` を必要とする経路は
  すべてこのファイルの fake に差し替える。`modal_hub.core.store` の
  `_approvals_dict()` / `store_volume()` を monkeypatch するのが唯一の入口。
- **security / audit / approval_gate の「本物の実装」を通す。** ストアだけを
  差し替えて、署名検証・状態遷移・write-once 競合はすべて実コードで検証する。
  ロジックをテスト側で再実装しない（再実装するとテストが「実装のコピー」に
  なり、仕様違反を検出できなくなる）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import pytest

# リポジトリルートを import パスへ。modal_hub は repo ルート直下のパッケージ。
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# テスト用の鍵材料（本物の Secret ではない。Modal Secret には一切触れない）
# ---------------------------------------------------------------------------

TEST_AGENT_SIGNING_KEY = "test-agent-signing-key-0123456789"
TEST_AGENT_SIGNING_KEY_PREV = "test-agent-signing-key-PREVIOUS-9876"
TEST_PWA_SESSION_KEY = "test-pwa-session-key-abcdefghijklmnop"
TEST_PAIRING_CODE = "12345678"
TEST_NTFY_TOPIC = "test-topic-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TEST_NTFY_TOKEN = "tk_testtoken_aaaaaaaaaaaaaaaaaaaa"

# 64-hex / 40-hex のダミー。実際の sha256/HEAD ではないが形式は満たす。
WS_ID = "a" * 64
WS_ID_OTHER = "b" * 64
SHA_PAYLOAD = "c" * 64
SHA_PAYLOAD_RAW = "d" * 64
HEAD_REV = "e" * 40


# ---------------------------------------------------------------------------
# Fake modal.Dict（store.py のプリミティブ検証用。SDK の意味論を模倣する）
# ---------------------------------------------------------------------------


_NO_DEFAULT = object()  # pop() のデフォルト未指定センチネル（本物の SDK と同じく "未指定" と None を区別する）


class FakeModalDict:
    """`modal.Dict` の最小模倣。

    重要な意味論（インストール済み modal 1.5.3 の `modal/dict.pyi` で確認済み）:
        - ``put(key, value, *, skip_if_exists=False) -> bool``。
          戻り値は「実際に書き込んだか」の真偽値であり、``skip_if_exists=True``
          かつ既存キーだった場合は **例外を投げず** ``False`` を返す（無条件で
          ``None`` を返す旧実装は契約違反だった。BUG-7 の修正で
          `store.put_if_absent` がこの戻り値をそのまま返すようになったため、
          fake が正しい bool を返さないとテストの証明力が失われる）。
        - ``get(key, default=None) -> Any``。
        - ``contains(key) -> bool`` / ``__getitem__(key)``。
        - ``pop(key, default=<センチネル>) -> Any``。default 省略時はキー欠落で
          ``KeyError``、指定時は default を返す（本物と同じ）。
        - ``len()`` と反復は **意図的に実装しない**（親設計書 §9 落とし穴 22
          「`modal.Dict` の `len()` と全走査を使わない」。実装が誤って使ったら
          TypeError で落ちる ＝ テストが検出する）。

    `on_contains` フックは「1 回目の contains 検査と put の間に他コンテナが
    割り込む」競合を再現するために使う。
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self.on_contains = None  # Optional[Callable[[str, FakeModalDict], None]]
        self.put_calls: list[tuple[str, bool]] = []

    def contains(self, key: str) -> bool:
        if self.on_contains is not None:
            self.on_contains(key, self)
        return key in self._data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def put(self, key: str, value: Any, *, skip_if_exists: bool = False) -> bool:
        self.put_calls.append((key, skip_if_exists))
        if skip_if_exists and key in self._data:
            return False  # SDK は例外を投げず黙って no-op し、False を返す
        self._data[key] = value
        return True

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def pop(self, key: str, default: Any = _NO_DEFAULT) -> Any:
        if default is _NO_DEFAULT:
            return self._data.pop(key)
        return self._data.pop(key, default)

    # __len__ / __iter__ / keys() はあえて未実装（落とし穴 22 の検出用）。


class FakeVolume:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


# ---------------------------------------------------------------------------
# Fake ApprovalStore / AuditStore / CredentialStore（上位モジュール用）
# ---------------------------------------------------------------------------


class FakeStore:
    """`security.CredentialStore` / `audit.AuditStore` /
    `approval_gate.ApprovalStore` の 3 つを同時に満たすインメモリ実装。

    `put_if_absent` は **正しい write-once 意味論**（既存キーなら書かずに
    False）で実装する。実装側（store.py）の欠陥をテストが肩代わりして
    隠さないように、ここは「あるべき挙動」に固定する。
    """

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.files: dict[str, Any] = {}
        self.raw_files: dict[str, bytes] = {}
        self.outbox: dict[str, Any] = {}
        # 障害注入用
        self.fail_outbox_register = False
        self.fail_write_json = False
        self.overwrite_calls: list[str] = []

    # --- Dict 系 ---
    def get(self, key: str) -> Optional[Any]:
        return self.data.get(key)

    def put_if_absent(self, key: str, value: Any) -> bool:
        if key in self.data:
            return False
        self.data[key] = value
        return True

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def overwrite(self, key: str, value: Any) -> None:
        self.overwrite_calls.append(key)
        self.data[key] = value

    # --- outbox ---
    def outbox_register(self, event_id: str, payload: dict[str, Any]) -> bool:
        if self.fail_outbox_register:
            raise RuntimeError("injected outbox failure")
        if event_id in self.outbox:
            return False
        self.outbox[event_id] = payload
        return True

    def outbox_consume(self, event_id: str) -> None:
        self.outbox.pop(event_id, None)

    # --- Volume 系 ---
    def write_json(self, rel_path: str, obj: Any) -> None:
        if self.fail_write_json:
            raise RuntimeError("injected volume failure")
        self.files[rel_path] = obj

    # Phase1b: routers/skills.py の SkillsStore Protocol が要求する生バイト
    # 版（SKILL.md は JSON ではないため write_json とは別系統で持つ）。
    def read_file(self, rel_path: str) -> Optional[Any]:
        return self.raw_files.get(rel_path)

    def atomic_write_file(self, rel_path: str, content: bytes) -> None:
        self.raw_files[rel_path] = content


@pytest.fixture()
def fake_store() -> FakeStore:
    return FakeStore()


@pytest.fixture()
def fake_dict(monkeypatch) -> FakeModalDict:
    """`modal_hub.core.store` のモジュール関数を fake の modal.Dict へ束ねる。"""
    from modal_hub.core import store as store_mod

    d = FakeModalDict()
    vol = FakeVolume()
    monkeypatch.setattr(store_mod, "_approvals_dict", lambda: d)
    monkeypatch.setattr(store_mod, "store_volume", lambda: vol)
    d.volume = vol  # type: ignore[attr-defined]
    return d


# ---------------------------------------------------------------------------
# Secret 環境（config.py 経由で読まれる）
# ---------------------------------------------------------------------------


@pytest.fixture()
def secret_env(monkeypatch):
    from modal_hub.core import config

    monkeypatch.setenv(config.HH_AGENT_TOKEN_SIGNING_KEY, TEST_AGENT_SIGNING_KEY)
    monkeypatch.setenv(config.HH_PWA_SESSION_KEY, TEST_PWA_SESSION_KEY)
    monkeypatch.setenv(config.HH_PAIRING_CODE, TEST_PAIRING_CODE)
    monkeypatch.setenv(config.NTFY_TOPIC, TEST_NTFY_TOPIC)
    monkeypatch.setenv(config.NTFY_TOKEN, TEST_NTFY_TOKEN)
    monkeypatch.delenv(config.HH_AGENT_TOKEN_SIGNING_KEY_PREV, raising=False)
    return config


# ---------------------------------------------------------------------------
# 承認レコードのビルダー（req: の形は親設計書 §4.3 / spec §1.2 手順3）
# ---------------------------------------------------------------------------

GRACE = 150.0
CLAIM_WINDOW = 180.0


def make_req(
    *,
    approval_id: str = "11111111-1111-4111-8111-111111111111",
    created_at: float = 1_000_000.0,
    sub: str = "claude_code:desktop-haruki",
    source: str = "claude_code",
    session_id: str = "sess-1",
    workspace_id: str = WS_ID,
    tool_name: str = "Bash",
    payload: Optional[dict] = None,
    payload_sha256: str = SHA_PAYLOAD,
    payload_raw_sha256: str = SHA_PAYLOAD_RAW,
    cwd: str = "C:/Users/Haruki/Projects/Foo",
    context_workspace_id: str = WS_ID,
    base_revision: Optional[str] = HEAD_REV,
    risk: str = "HIGH",
    rule_id: str = "force_push",
    targets: Optional[list] = None,
) -> dict:
    return {
        "approval_id": approval_id,
        "idempotency_key": "idem-" + "k" * 20,
        "sub": sub,
        "source": source,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "tool_name": tool_name,
        "payload": payload if payload is not None else {"command": "git push --force"},
        "payload_sha256": payload_sha256,
        "payload_raw_sha256": payload_raw_sha256,
        "context": {
            "cwd": cwd,
            "workspace_id": context_workspace_id,
            "base_revision": base_revision,
        },
        "risk": risk,
        "rule_id": rule_id,
        "reason_code": rule_id,
        "targets": targets if targets is not None else [],
        "created_at": created_at,
        "grace_deadline": created_at + GRACE,
        "claim_deadline": created_at + CLAIM_WINDOW,
    }


def make_target(
    path: str = "C:/Users/Haruki/Projects/Foo/out.txt",
    realpath: Optional[str] = None,
    identity: str = "17735206716449772873:562949955562867",
    preimage_sha256: Optional[str] = "f" * 64,
    exists: bool = True,
) -> dict:
    return {
        "path": path,
        "realpath": realpath if realpath is not None else path,
        "identity": identity,
        "preimage_sha256": preimage_sha256,
        "exists": exists,
    }


@pytest.fixture()
def req_record() -> dict:
    return make_req()


# ---------------------------------------------------------------------------
# ソースツリーのパス（機械的検査テスト用）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def pwa_dir(repo_root: Path) -> Path:
    return repo_root / "mobile_app" / "pwa_approval"
