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

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Final, List, Optional

import modal

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
# __all__
# ---------------------------------------------------------------------------

__all__ = [
    # Resource names
    "APPROVALS_DICT_NAME",
    "STORE_VOLUME_NAME",
    "VOLUME_MOUNT_PATH",
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
    # Resource handles
    "store_volume",
    # Dict operations
    "put_if_absent",
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
]
