"""Storage layer for hh-agent-hub: ``modal.Dict`` accessors + Volume I/O.

**Design contract (Phase 1a spec §1.2, §1.4, §4.3, §10):**

- ``modal.Dict`` has no compare-and-set API. The only atomic primitive is
  ``put(key, value, skip_if_exists=True)``. Every write exposed here goes
  through that. There is **no** ``update``/``set``/``merge``/``overwrite``
  helper — adding one would re-introduce the race that the design doc
  explicitly avoids.
- Revocation is **allowlist-style key deletion** (spec §11). ``delete`` is
  exposed for that single purpose: the absence of a key *is* the
  "this is no longer valid" signal. We do not write negative tombstones.
- Volume writes are atomic: temp file in the same directory, ``fsync``,
  ``os.replace``, then ``volume.commit()``. The store never leaves a
  partially written audit file visible to readers.
- ``modal.Dict`` iteration (``len``, full ``keys()``) is intentionally
  absent. The design doc bans it (high cost, 100k-entry cap). Listing is
  the caller's responsibility, done through dedicated index keys.

**Key prefixes** are defined here as the single source of truth so the
prefix string ``"req:"`` is never duplicated across routers. The
``build_*_key`` helpers turn an opaque ID into a fully qualified key
without exposing the concatenation to callers.

This module owns no business logic. State transitions, validation, and
policy live in ``routers/approval_gate.py`` and ``services/audit.py``;
this file only provides the durable primitives those modules compose.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Final, List, Optional

import modal

from modal_hub.services import skill_quarantine

logger = logging.getLogger("hh_agent.store")

# ---------------------------------------------------------------------------
# Resource names — must match design doc §6 exactly.
# ---------------------------------------------------------------------------

APPROVALS_DICT_NAME: Final = "hh-agent-approvals"
STORE_VOLUME_NAME: Final = "hh-agent-store"
VOLUME_MOUNT_PATH: Final = "/mnt/hh_store"

# Outbox is colocated with the approvals Dict (same atomic store). See
# spec §10.1b — outbox registration uses the same skip_if_exists path,
# which is why it lives in the same Dict rather than a separate one.
OUTBOX_DICT_NAME: Final = APPROVALS_DICT_NAME

# ---------------------------------------------------------------------------
# Key prefixes (design doc §4.3, §10.1b, §11).
# ---------------------------------------------------------------------------

PREFIX_REQ: Final = "req:"
PREFIX_DECISION: Final = "decision:"
PREFIX_LEASE: Final = "lease:"
PREFIX_IDEM: Final = "idem:"
PREFIX_NOTIFY: Final = "notify:"
PREFIX_PAIRING_OFFER: Final = "pairing_offer:"
PREFIX_PAIRING_USED: Final = "pairing_used:"
PREFIX_WSTICKET: Final = "wsticket:"
PREFIX_RATE: Final = "rate:"
PREFIX_AGENT_SESSION: Final = "agent_session:"
PREFIX_PWA_SESSION: Final = "pwa_session:"
PREFIX_OUTBOX: Final = "outbox:"
PREFIX_GC_INDEX: Final = "gc:index:"
PREFIX_PENDING_INDEX: Final = "pending:index"
#: Phase1b (07_Phase1b_Spec.md §5): atomic name reservation for
#: ``POST /api/skills/publish``. See ``skill_publish_key()`` below.
PREFIX_SKILL_PUBLISH: Final = "skill_publish:"

#: S-08c: ``sync_dashboard_skills`` が書く「この name はこの digest まで
#: 消し込み済み」マーカーのキー空間（quarantine 本体の Volume ファイルとは別）。
PREFIX_QUARANTINE_RESOLVED: Final = "quarantine_resolved:"

#: quarantine SKILL.md 本文のサイズ上限（S-08b。`routers/skills.py` の
#: 同名定数と同じ 64KB — publish 側の契約と読み取り側の上限を一致させる）。
MAX_BODY_BYTES: Final = 64 * 1024

# Aggregate for startup diagnostics. Pairs the constant name with the
# value so a typo in one place is caught at import time.
ALL_PREFIXES: Final = (
    PREFIX_REQ,
    PREFIX_DECISION,
    PREFIX_LEASE,
    PREFIX_IDEM,
    PREFIX_NOTIFY,
    PREFIX_PAIRING_OFFER,
    PREFIX_PAIRING_USED,
    PREFIX_WSTICKET,
    PREFIX_RATE,
    PREFIX_AGENT_SESSION,
    PREFIX_PWA_SESSION,
    PREFIX_OUTBOX,
    PREFIX_GC_INDEX,
    PREFIX_PENDING_INDEX,
    PREFIX_SKILL_PUBLISH,
    PREFIX_QUARANTINE_RESOLVED,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StoreError(RuntimeError):
    """Raised on unexpected storage-layer responses.

    Distinct from a normal "key already exists" miss: this signals a
    serialization error, a Modal SDK failure, or a volume write failure
    that the caller cannot recover from by retrying alone.
    """


# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------


def req_key(approval_id: str) -> str:
    return PREFIX_REQ + approval_id


def decision_key(approval_id: str) -> str:
    return PREFIX_DECISION + approval_id


def lease_key(approval_id: str) -> str:
    return PREFIX_LEASE + approval_id


def idem_key(subject: str, idempotency_key: str) -> str:
    """Build the per-subject idempotency key.

    Subject-prefixing is the spec's defense against cross-session
    idempotency-key collisions (spec §1.2 step 2). The caller passes the
    token's ``sub`` claim verbatim; this function must not normalize it.
    """
    return PREFIX_IDEM + subject + ":" + idempotency_key


def notify_key(approval_id: str) -> str:
    return PREFIX_NOTIFY + approval_id


def pairing_offer_key(code_hash: str) -> str:
    return PREFIX_PAIRING_OFFER + code_hash


def pairing_used_key(code_hash: str) -> str:
    return PREFIX_PAIRING_USED + code_hash


def ws_ticket_key(ticket: str) -> str:
    return PREFIX_WSTICKET + ticket


def rate_key(subject: str, hour_bucket: str) -> str:
    return PREFIX_RATE + subject + ":" + hour_bucket


def agent_session_key(token_id: str) -> str:
    return PREFIX_AGENT_SESSION + token_id


def pwa_session_key(session_id: str) -> str:
    return PREFIX_PWA_SESSION + session_id


def outbox_key(event_id: str) -> str:
    return PREFIX_OUTBOX + event_id


def skill_publish_key(name: str) -> str:
    """Phase1b: atomic reservation key for a published skill name.

    ``routers/skills.py`` uses ``put_if_absent(skill_publish_key(name), ...)``
    as the sole arbiter of "who gets to write ``<name>/SKILL.md`` first" —
    the genuinely atomic compare-and-set the Dict provides, instead of a
    read-file-then-write-file sequence (which is not atomic and was the
    subject of a 2026-08-11 Codex review Critical finding: two concurrent
    requests with different content could both observe no existing file
    and both proceed to the unconditional replacing write).
    """
    return PREFIX_SKILL_PUBLISH + name


def quarantine_resolved_key(name: str) -> str:
    """S-08c: 「name はこの digest まで消し込み済み」マーカーのキー。"""
    return PREFIX_QUARANTINE_RESOLVED + name


# ---------------------------------------------------------------------------
# modal.Dict handles (lazy, per-call).
# ---------------------------------------------------------------------------
# Resolving the handle inside the function (rather than at import time)
# matches Modal's lifecycle: the Dict object is bound to the running
# container, and importing it once at module load would mean every
# test or alternate process also grabs a handle. Each accessor calls
# this fresh.


def _approvals_dict() -> "modal.Dict":
    """Return the approvals/outbox Dict handle.

    ``create_if_missing=True`` matches the deployment contract: the
    volume and Dict are created by the first ``modal deploy`` and reused
    thereafter. The flag is a no-op on subsequent deploys.
    """
    return modal.Dict.from_name(APPROVALS_DICT_NAME, create_if_missing=True)


def store_volume() -> "modal.Volume":
    """Return the store Volume handle. Mounted at ``/mnt/hh_store``."""
    return modal.Volume.from_name(STORE_VOLUME_NAME, create_if_missing=True)


# ---------------------------------------------------------------------------
# Write-once primitives
# ---------------------------------------------------------------------------


def put_if_absent(key: str, value: Any) -> bool:
    """Write ``value`` at ``key`` iff ``key`` does not already exist.

    Returns ``True`` if this call performed the write, ``False`` if the
    key was already present (the write was a no-op). The result is the
    sole signal the caller needs to decide who "won" a write-once race
    (e.g. duplicate ``request`` calls with the same idempotency key, or
    two claimants racing to acquire a ``lease:``).

    The implementation is a single call to ``modal.Dict.put(key, value,
    skip_if_exists=True)``. Verified against the installed SDK (modal
    1.5.3): that call's return value **is** the atomic "did I write?"
    signal —

        Dict.put(self, key, value, *, skip_if_exists=False) -> bool
        "Returns True if the key-value pair was added and False if it
         wasn't because the key already existed and `skip_if_exists`
         was set."

    That boolean is decided server-side as part of the same atomic
    operation, so it is the authoritative race result — there is no
    window between "check" and "write" to lose, unlike a separate
    ``contains`` call before or after the ``put``. Do **not** reintroduce
    a ``contains`` check here: a `contains` before `put` is a classic
    TOCTOU gap, and a `contains` after `put` only answers "does the key
    exist now", which is ``True`` for the race loser as well as the
    winner — it cannot tell the two apart. That was the bug this
    function used to have (BUG-7): it made `put_if_absent` return
    ``True`` to every racing caller, so a `lease:` claimant that lost the
    race was told it held the execution right anyway.
    """
    d = _approvals_dict()
    try:
        return d.put(key, value, skip_if_exists=True)
    except Exception as exc:  # pragma: no cover — defensive
        raise StoreError(f"put failed for key {key!r}: {exc}") from exc


def mark_quarantine_resolved(name: str, content_sha256: str) -> None:
    """「この name はこの digest まで消し込み済み」という記録を書く（S-08c）。

    キー空間 `quarantine_resolved:<name>` は quarantine 本体（Volume 上の
    SKILL.md）とは別の `modal.Dict` キー空間。このキー空間の書き手は
    `sync_dashboard_skills` 唯一であり `put_if_absent` のような排他は不要 —
    素朴な無条件上書き（`dict[key] = value` 相当）でよい
    （`modal.Dict.put` は `skip_if_exists=False` がデフォルトで上書き）。
    `skip_if_exists=False` を明示して上書き意図を宣言する。
    """
    d = _approvals_dict()
    d.put(quarantine_resolved_key(name), content_sha256, skip_if_exists=False)


def get_quarantine_resolved(name: str) -> Optional[str]:
    """`mark_quarantine_resolved` で記録した digest を返す。未設定は None。"""
    value = get(quarantine_resolved_key(name))
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Read primitives
# ---------------------------------------------------------------------------


def contains(key: str) -> bool:
    """Return True iff ``key`` is present in the Dict.

    Preferred over catching a KeyError on ``get`` — it makes the call
    site read as a predicate and avoids ambiguity with the "value is
    literally None / False / 0" cases.
    """
    return _approvals_dict().contains(key)


def get(key: str) -> Optional[Any]:
    """Read a single key. Returns ``None`` if missing.

    The contract is "absent or None are indistinguishable to the caller";
    both mean "no decision has been recorded for this approval". Routers
    that need to distinguish "not present" from "explicitly None" must
    use :func:`contains` first.
    """
    d = _approvals_dict()
    if not d.contains(key):
        return None
    return d[key]


def get_many(keys: List[str]) -> dict[str, Any]:
    """Read many keys. Missing keys are simply absent from the result.

    The Dict API does not offer a batched read, so this is a sequential
    loop. For the per-approval lookup pattern (req + decision + lease +
    notify, four reads per poll) this is 4 round-trips to the Modal
    control plane. Acceptable for a 5-second poll cadence; if it ever
    becomes a bottleneck, the bottleneck is the poll cadence, not the
    fan-out.
    """
    d = _approvals_dict()
    out: dict[str, Any] = {}
    for k in keys:
        if d.contains(k):
            out[k] = d[k]
    return out


# ---------------------------------------------------------------------------
# Deletion (allowlist-style revocation only)
# ---------------------------------------------------------------------------


def delete(key: str) -> None:
    """Remove a key. Idempotent: deleting a missing key is a no-op.

    Per spec §11, revocation is implemented by deletion, not by a
    "revoked:" tombstone. The absence of a key is the "this credential
    is no longer valid" signal, so the store's TTL policy cannot
    accidentally re-validate a revoked credential by expiring the
    tombstone.

    Callers that want to distinguish "deleted" from "never existed"
    must call :func:`contains` first; this function does not return a
    status.
    """
    d = _approvals_dict()
    if d.contains(key):
        d.pop(key)


# ---------------------------------------------------------------------------
# Outbox helpers
# ---------------------------------------------------------------------------
# The outbox (spec §10.1b) is a write-once log of audit events that have
# been registered in the atomic store but not yet flushed to the volume.
# The primitives here are deliberately thin: register = put_if_absent on
# the outbox key, consume = delete. The flush job that scans pending
# outbox entries is owned by the scheduled GC function and is out of
# scope for this file (it would require Dict iteration, which the spec
# bans for general use — the GC uses a dedicated index path).


def outbox_register(event_id: str, payload: Any) -> bool:
    """Register an audit event in the outbox. Returns True on first write.

    The ``event_id`` must be deterministic (see spec §10.1 — the audit
    service owns event_id derivation). Re-registering the same event_id
    is a no-op; this is what makes the flush-retry path safe to run
    against the same key repeatedly without producing duplicate
    on-disk records.
    """
    return put_if_absent(outbox_key(event_id), payload)


def outbox_consume(event_id: str) -> None:
    """Remove an outbox entry after it has been flushed to the volume.

    Idempotent. Called by the audit flush path on successful write. On
    volume-write failure, the entry is left in place and the GC job
    retries the flush later.
    """
    delete(outbox_key(event_id))


# ---------------------------------------------------------------------------
# Volume I/O
# ---------------------------------------------------------------------------


def _volume_root() -> Path:
    """Resolve the mounted volume root.

    In a Modal container this is the configured mount path
    (``/mnt/hh_store``). In a local test (no Modal runtime) we fall back
    to a sibling of the repo so unit tests can exercise the file
    primitives without a Modal container.
    """
    if os.path.isdir(VOLUME_MOUNT_PATH):
        return Path(VOLUME_MOUNT_PATH)
    # Test fallback. The repo-root-relative path keeps the local test
    # tree out of source control (covered by the existing .gitignore
    # patterns for ``/mnt/`` if any are added later; for now we use a
    # tempfile.TemporaryDirectory-style sentinel under the repo).
    fallback = Path(__file__).resolve().parents[2] / ".hh-agent-store-test"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def atomic_write_file(rel_path: str, content: bytes) -> None:
    """Write ``content`` to ``<volume>/<rel_path>`` atomically.

    Steps:

    1. Resolve the target path inside the volume mount.
    2. Create parent directories (idempotent).
    3. Write to a temp file in the **same** directory. Same-directory is
       load-bearing: ``os.replace`` is only atomic when source and
       destination are on the same filesystem.
    4. ``fsync`` the temp file so the bytes are durable before the
       rename.
    5. ``os.replace`` into the final path. After this point, any reader
       sees either the old content or the new content — never a
       partially written file.
    6. ``volume.commit()`` so the rename is persisted across container
       restarts.

    On any failure between steps 3 and 5 the temp file is removed
    before re-raising. The final path is never partially written.
    """
    if not isinstance(content, (bytes, bytearray)):
        raise StoreError(
            f"atomic_write_file requires bytes, got {type(content).__name__}"
        )
    base = _volume_root()
    target = base / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)

    # ``delete=False`` (default) and ``dir=target.parent`` together
    # guarantee the same-filesystem invariant for the subsequent
    # os.replace.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=".tmp.",
        suffix=target.suffix or ".part",
    )
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, target)
    except Exception:
        # Best-effort cleanup of the temp file. We do not want to
        # mask the original exception with a cleanup failure, so
        # ``errors="ignore"`` is not used here — failing loudly about
        # the cleanup is preferable to leaking temp files silently.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    # Persist the rename. Modal Volumes buffer writes; without
    # ``commit`` the new file is not visible after a cold start.
    try:
        store_volume().commit()
    except Exception as exc:  # pragma: no cover — Modal-specific path
        raise StoreError(f"volume.commit failed after write: {exc}") from exc


def read_file(rel_path: str) -> Optional[bytes]:
    """Read a file from the volume. Returns ``None`` if missing."""
    full = _volume_root() / rel_path
    if not full.is_file():
        return None
    return full.read_bytes()


def file_exists(rel_path: str) -> bool:
    """True iff a regular file exists at the given relative path."""
    return (_volume_root() / rel_path).is_file()


def list_dir(rel_path: str) -> List[str]:
    """List immediate children of a volume directory.

    Returns names, not full paths. Missing directory returns ``[]``,
    not an error — the call sites that need to distinguish "empty" from
    "absent" can wrap with :func:`file_exists`.
    """
    full = _volume_root() / rel_path
    if not full.is_dir():
        return []
    return [p.name for p in full.iterdir()]


def write_json(rel_path: str, obj: Any) -> None:
    """Atomic JSON write. Object is serialized with stable key order.

    The serialization uses ``sort_keys=True`` and the compact
    separators so two semantically equal objects produce the same
    bytes. This is the on-disk form for audit records; the audit
    service may further canonicalize before hashing (spec §3), but
    bytewise stability here keeps diffs reviewable.
    """
    content = json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    atomic_write_file(rel_path, content)


def read_json(rel_path: str) -> Optional[Any]:
    """Read JSON. Returns ``None`` if the file is missing.

    Parse errors are propagated. A malformed audit record is a defect
    that must be surfaced, not silently swallowed.
    """
    raw = read_file(rel_path)
    if raw is None:
        return None
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Quarantine の安全な読み取り（S-08b/S-08c 共通・symlink 差し替え攻撃への防御）
# ---------------------------------------------------------------------------
#
# 既存の `read_file()` は `Path.is_file()` + `Path.read_bytes()` で symlink を
# 辿ってしまうため、quarantine（`skills_quarantine/<name>/SKILL.md`）の読み取り
# には絶対に使ってはならない。`read_quarantine_entry_safe()` は S-08b の
# `GET /api/skills/quarantine` と S-08c の `sync_dashboard_skills` の両方から
# 呼ばれる共通実装であり、Linux（Modal 本番）では `dir_fd` 相対の
# `O_NOFOLLOW`（openat 相当）で祖先ディレクトリごと差し替えられても辿らない。
# O_NOFOLLOW / dir_fd が使えないプラットフォーム（Windows 等）では
# `os.path.islink()` による代替検証に分岐する。

#: openat 相当の安全な実装が使えるプラットフォームか（本番は Modal Linux
#: コンテナのため常にこちら）。`os.supports_dir_fd` は `os.open` が
#: `dir_fd` 引数を受け付けるかどうかの公式の判定方法。
_HAVE_DIRFD_NOFOLLOW: Final = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
)


def read_quarantine_entry_safe(name: str) -> Optional[dict]:
    """quarantine の1エントリ（<name>/SKILL.md + <name>/meta.json）を安全に読む。

    戻り値: ``{"content": str, "content_sha256": str,
                 "origin_instance": str|None, "published_at": float|None,
                 "distilled_from_session_id": str|None}``
    または None（name が無い・不正な場合）。

    防御の要点（S-08b 実装要件・省略しない）:
        1. `name` を `skill_quarantine.NAME_RE` で検証（不一致は None）。
        2. quarantine root（`skills_quarantine/`、`_volume_root()` 基準）を
           ``os.open(root, O_DIRECTORY | O_NOFOLLOW)`` で開く。
        3. その dir fd を基点に `<name>` サブディレクトリを
           ``os.open(name, O_DIRECTORY | O_NOFOLLOW, dir_fd=root_fd)`` で開く
           （openat 相当。存在確認してから開く2段階にしない）。
        4. `<name>` の dir fd を基点に ``SKILL.md``・``meta.json`` をそれぞれ
           ``os.open(filename, O_RDONLY | O_NOFOLLOW, dir_fd=name_fd)`` で開く。
        5. ``os.fstat(fd)`` で通常ファイルであることとサイズを確認し、一括
           ``read()`` に頼らず EOF または ``MAX_BODY_BYTES``+1 バイトまでの
           境界付きループで読む（超過は破棄し、例外にせずログのみ）。
        6. ``SKILL.md`` が読めない・symlink・サイズ超過・存在しない場合は
           エントリ全体を None として扱う（本文が読めなければエントリ自体が
           無意味なため）。
        7. ``meta.json`` は本文と非対称に扱う: 読めない（symlink 拒否・破損
           JSON・サイズ超過・存在しない・object でない）場合は例外にせず
           ``origin_instance: None, published_at: None`` として扱う（本文の
           安全性には影響しないため 500 にしない）。
        8. ``distilled_from_session_id`` は SKILL.md 本文の YAML frontmatter
           から読み取る（`skill_quarantine.parse_frontmatter_name()` と同じ
           YAML パース方式。壊れていれば None — Hub にサイドカーは不要。
           Distiller が frontmatter に既に埋め込んでいる）。
        9. fd は必ず try/finally で閉じる。
        10. O_NOFOLLOW / dir_fd が使えないプラットフォーム（Windows 等）では
            ``os.path.islink()`` 等の代替手段で「symlink なら拒否する」ことを
            実現する（本番の openat 経路の安全性が最優先であり、Windows 側は
            完全な等価実装ではなくても代替検証が可能な形にする）。

    ``content_sha256`` は読み取った本文から実測する（保存されている値をその
    まま信用しない）。
    """
    if not isinstance(name, str) or not skill_quarantine.NAME_RE.match(name):
        return None

    root = _volume_root() / "skills_quarantine"

    if _HAVE_DIRFD_NOFOLLOW:
        result = _read_entry_openat(root, name)
    else:
        result = _read_entry_fallback(root, name)
    if result is None:
        return None
    content_bytes, meta_bytes = result

    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning(
            "quarantine entry %r: SKILL.md is not valid UTF-8; skipping", name
        )
        return None

    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    origin_instance, published_at = _extract_meta(meta_bytes)
    distilled_from_session_id = _frontmatter_field(content, "distilled_from_session_id")
    return {
        "content": content,
        "content_sha256": content_sha256,
        "origin_instance": origin_instance,
        "published_at": published_at,
        "distilled_from_session_id": distilled_from_session_id,
    }


def _read_entry_openat(root: Path, name: str) -> Optional[tuple[bytes, Optional[bytes]]]:
    """Linux/Modal 本番経路: dir_fd 相対の O_NOFOLLOW で祖先からの差し替えを
    防ぐ（openat 相当）。

    戻り値は ``(content_bytes, meta_bytes)``。meta_bytes は meta.json が
    無い/読めない場合 None（呼び出し側が null フィールドへフォールバック）。
    SKILL.md が読めない場合は全体 None。
    """
    try:
        root_fd = os.open(root, os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        try:
            name_fd = os.open(name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except OSError:
            return None
        try:
            content = _read_regular_file(name_fd, "SKILL.md")
            if content is None:
                return None
            meta = _read_regular_file(name_fd, "meta.json")
            return content, meta
        finally:
            os.close(name_fd)
    finally:
        os.close(root_fd)


def _read_entry_fallback(root: Path, name: str) -> Optional[tuple[bytes, Optional[bytes]]]:
    """O_NOFOLLOW / dir_fd が使えないプラットフォーム（Windows 等）の代替実装。

    openat 相当の完全な TOCTOU 除去はできないが、各段階で ``os.path.islink()``
    により「symlink なら拒否する」ことを実現する（テストが Windows 上でも
    意味を持つように、防御の意図は代替手段で保つ）。
    """
    if os.path.islink(root):
        logger.warning("quarantine root is a symlink; refusing to read %r", name)
        return None
    name_dir = root / name
    if os.path.islink(name_dir):
        return None
    if not name_dir.is_dir():
        return None
    skill_file = name_dir / "SKILL.md"
    if os.path.islink(skill_file) or not skill_file.is_file():
        return None
    content = _read_regular_file(None, str(skill_file))
    if content is None:
        return None
    meta_file = name_dir / "meta.json"
    meta = None
    if not os.path.islink(meta_file) and meta_file.is_file():
        meta = _read_regular_file(None, str(meta_file))
    return content, meta


def _read_regular_file(dir_fd: Optional[int], filename: str) -> Optional[bytes]:
    """`filename` を dir_fd 相対（dir_fd=None ならパス直接）で安全に読む。

    - dir_fd 経由時は O_NOFOLLOW で symlink を拒否する。
    - ``os.fstat`` で通常ファイル・サイズを確認する。
    - 一括 ``read()`` に頼らず ``MAX_BODY_BYTES``+1 バイトまでの境界付きループ
      で読む（fstat 後にファイルが成長した場合の無限読み込みを防ぐ）。

    戻り値は読み取ったバイト列、または None（欠損・symlink・非通常ファイル・
    サイズ超過。サイズ超過は破棄し、例外にせずログのみ）。
    """
    try:
        if dir_fd is not None:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        else:
            fd = os.open(filename, os.O_RDONLY)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > MAX_BODY_BYTES:
            logger.warning(
                "quarantine file %r is %d bytes (> %d); refusing to read",
                filename,
                st.st_size,
                MAX_BODY_BYTES,
            )
            return None
        data = _bounded_read_fd(fd)
        if data is None:
            logger.warning(
                "quarantine file %r grew past %d bytes during read; refusing",
                filename,
                MAX_BODY_BYTES,
            )
        return data
    finally:
        os.close(fd)


def _bounded_read_fd(fd: int) -> Optional[bytes]:
    """EOF または ``MAX_BODY_BYTES``+1 バイトまで境界付きで読む。

    上限を超えたら None を返す（呼び出し側がログして扱う）。空ファイルは
    空バイト列として読める。
    """
    chunks: List[bytes] = []
    total = 0
    while total <= MAX_BODY_BYTES:
        chunk = os.read(fd, min(8192, MAX_BODY_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        chunks.append(chunk)
    if total > MAX_BODY_BYTES:
        return None
    return b"".join(chunks)


def _extract_meta(meta_bytes: Optional[bytes]) -> tuple[Optional[str], Optional[float]]:
    """meta.json を本文と非対称に扱う: 読めなくても例外にしない。

    欠損・破損 JSON・object でない・フィールド型不正のいずれも
    ``(origin_instance=None, published_at=None)`` へフォールバックする
    （付随メタデータの取得失敗であり本文の安全性には影響しないため、
    全体を 500 にしない — S-08b）。
    """
    if meta_bytes is None:
        return None, None
    try:
        obj = json.loads(meta_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("quarantine meta.json is malformed; using null fields")
        return None, None
    if not isinstance(obj, dict):
        logger.warning("quarantine meta.json is not a JSON object; using null fields")
        return None, None
    origin = obj.get("origin_instance")
    origin = origin if isinstance(origin, str) else None
    published = obj.get("published_at")
    if isinstance(published, bool) or not isinstance(published, (int, float)):
        published = None
    else:
        published = float(published)
    return origin, published


def _frontmatter_field(content: str, field: str) -> Optional[str]:
    """SKILL.md の YAML frontmatter から文字列フィールドを取り出す。

    `skill_quarantine.parse_frontmatter_name()` と同じパース方式
    （`---` 検出 → `yaml.safe_load` → dict 確認 → str 確認）。frontmatter が
    無い・壊れている・型が違う場合は None（例外にしない）。
    """
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 3)
    if end == -1:
        return None
    try:
        import yaml

        data = yaml.safe_load(content[3:end])
    except Exception:  # noqa: BLE001 — 壊れた frontmatter は「無い」と同じ扱い
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(field)
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# __all__
# ---------------------------------------------------------------------------

__all__ = [
    # Resource names
    "APPROVALS_DICT_NAME",
    "STORE_VOLUME_NAME",
    "VOLUME_MOUNT_PATH",
    "MAX_BODY_BYTES",
    # Key prefixes
    "PREFIX_REQ",
    "PREFIX_DECISION",
    "PREFIX_LEASE",
    "PREFIX_IDEM",
    "PREFIX_NOTIFY",
    "PREFIX_PAIRING_OFFER",
    "PREFIX_PAIRING_USED",
    "PREFIX_WSTICKET",
    "PREFIX_RATE",
    "PREFIX_AGENT_SESSION",
    "PREFIX_PWA_SESSION",
    "PREFIX_OUTBOX",
    "PREFIX_GC_INDEX",
    "PREFIX_PENDING_INDEX",
    "PREFIX_SKILL_PUBLISH",
    "PREFIX_QUARANTINE_RESOLVED",
    "ALL_PREFIXES",
    # Errors
    "StoreError",
    # Key builders
    "req_key",
    "decision_key",
    "lease_key",
    "idem_key",
    "notify_key",
    "pairing_offer_key",
    "pairing_used_key",
    "ws_ticket_key",
    "rate_key",
    "agent_session_key",
    "pwa_session_key",
    "outbox_key",
    "skill_publish_key",
    "quarantine_resolved_key",
    # Resource handles
    "store_volume",
    # Dict operations
    "put_if_absent",
    "mark_quarantine_resolved",
    "get_quarantine_resolved",
    "contains",
    "get",
    "get_many",
    "delete",
    # Outbox
    "outbox_register",
    "outbox_consume",
    # Volume I/O
    "atomic_write_file",
    "read_file",
    "file_exists",
    "list_dir",
    "write_json",
    "read_json",
    # Quarantine safe read (S-08b/S-08c)
    "read_quarantine_entry_safe",
]
