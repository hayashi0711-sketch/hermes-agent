"""Configuration loader for Modal Secrets.

Modal's `Secret.from_name("hh-agent-secret")` injects the secret values into
the container's environment at function-call time (not at import time). This
module is therefore lazy: every accessor reads ``os.environ`` on demand.

**Contract:**

- All secret keys defined in design doc §6 (Phase 1a spec) are exposed as
  module-level constants and as named accessors. The named accessors are
  the public API; the constants exist so callers don't pass magic strings.
- Real values must never appear in logs, tracebacks, or ``__repr__``. Use
  :func:`safe_repr` for any diagnostic output. A debug ``print(secret)``
  is a defect, not a convenience.
- Missing required secrets raise :class:`SecretMissingError`. This is a
  fail-closed signal: routers can translate it to 500 and let the upstream
  client retry. We do not silently substitute empty strings.
- Optional secrets (e.g. rotation-prev key, provider keys for later phases)
  return ``None`` when unset so callers can branch explicitly.

This module owns no other file's behavior. It is read-only from the
environment's perspective: nothing here mutates ``os.environ``, opens a
network connection, or touches storage.
"""

from __future__ import annotations

import os
from typing import Final, Optional

# ---------------------------------------------------------------------------
# Secret key constants
# ---------------------------------------------------------------------------
# Centralized so the rest of the codebase never hard-codes the strings.
# Adding a new secret: append the constant, the accessor, the doc line in
# the table below, and the ALL_SECRET_KEYS tuple if it is required (not
# optional).

HH_AGENT_TOKEN_SIGNING_KEY: Final = "HH_AGENT_TOKEN_SIGNING_KEY"
HH_AGENT_TOKEN_SIGNING_KEY_PREV: Final = "HH_AGENT_TOKEN_SIGNING_KEY_PREV"
HH_PWA_SESSION_KEY: Final = "HH_PWA_SESSION_KEY"
HH_PAIRING_CODE: Final = "HH_PAIRING_CODE"
NTFY_TOPIC: Final = "NTFY_TOPIC"
NTFY_TOKEN: Final = "NTFY_TOKEN"
ANTHROPIC_API_KEY: Final = "ANTHROPIC_API_KEY"
C2S_API_KEY: Final = "C2S_API_KEY"
# Agentic_OS ヘッドレスディスパッチ（.agentic_os_headless_dispatch_task.md）。
# 既存のエージェントトークン機構（security.verify_agent_token）を再利用するが、
# 署名鍵の名前空間だけは既存（HH_AGENT_TOKEN_SIGNING_KEY）と衝突しない新規名にする。
AGENTIC_OS_DISPATCH_KEY: Final = "AGENTIC_OS_DISPATCH_KEY"
# 任意: 一時 HERMES_HOME の config.yaml へ model.default として書くモデル名
# （未設定なら hermes 側の既定モデル解決に委ねる。routers/dispatch.py の
# _prepare_hermes_home 参照）。
AGENTIC_OS_DISPATCH_MODEL: Final = "AGENTIC_OS_DISPATCH_MODEL"

# Required at function-call time. The container fails closed if any of
# these is unset when the function runs.
_REQUIRED_KEYS: Final = (
    HH_AGENT_TOKEN_SIGNING_KEY,
    HH_PWA_SESSION_KEY,
    HH_PAIRING_CODE,
    NTFY_TOPIC,
)

# Optional. The container starts even if these are unset; the calling code
# branches on ``None``.
#
# NTFY_TOKEN (2026-08-11 決定, 08_Handoff_Note.md): ntfy 認証は「公開トピック
# ＋32文字ランダム名」で先行させ、NTFY_TOKEN は空のまま運用する。ntfy には
# 権限もコマンド本文も載せない設計（03_Architecture.md §5.2）なので、トピック
# 名が漏れても「承認待ちがある」以上の情報は漏れない。空を「未設定」として
# fail-close する _REQUIRED_KEYS に含めていたのは実装時の誤りで、この決定と
# 矛盾し Hub 起動そのものを拒否させていた（2026-08-11 の Modal 再デプロイで発覚）。
_OPTIONAL_KEYS: Final = (
    HH_AGENT_TOKEN_SIGNING_KEY_PREV,
    NTFY_TOKEN,
    ANTHROPIC_API_KEY,
    C2S_API_KEY,
    AGENTIC_OS_DISPATCH_KEY,
    AGENTIC_OS_DISPATCH_MODEL,
)

# Public aggregate, mainly for startup self-diagnostics (see design doc §4.4
# "起動時にフックが実際に登録されたことを自己診断する" — the equivalent check
# for the hub is "are the secrets we expect actually present?").
ALL_SECRET_KEYS: Final = _REQUIRED_KEYS + _OPTIONAL_KEYS


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SecretMissingError(RuntimeError):
    """A required secret is not set in the container environment.

    Distinct from an invalid value: this signals a deployment / secret
    configuration defect, not a client error. Callers should map it to
    HTTP 500 (server-side misconfiguration), not 401 (auth failure).
    """


# ---------------------------------------------------------------------------
# Low-level accessors
# ---------------------------------------------------------------------------


def get_secret(name: str) -> str:
    """Read a required secret. Raises :class:`SecretMissingError` if unset.

    The check is on the *non-empty* string: ``HH_PAIRING_CODE=""`` is
    treated the same as missing, because empty values are never meaningful
    for these keys and would otherwise propagate as opaque 401s downstream.
    """
    value = os.environ.get(name, "")
    if not value:
        raise SecretMissingError(f"Required secret not set: {name}")
    return value


def get_optional(name: str) -> Optional[str]:
    """Read an optional secret. Returns ``None`` if unset or empty."""
    value = os.environ.get(name, "")
    return value or None


def all_required_present() -> bool:
    """Return True iff every required secret is set and non-empty.

    Intended for the container's startup self-diagnostic: if this returns
    False the container is misconfigured and routers should refuse traffic
    rather than serve 500s under load.
    """
    return all(bool(os.environ.get(k, "")) for k in _REQUIRED_KEYS)


# ---------------------------------------------------------------------------
# Redaction helper
# ---------------------------------------------------------------------------


def safe_repr(value: str, keep: int = 4) -> str:
    """Return a redacted representation of a secret for diagnostic output.

    Keeps the first ``keep`` characters (default 4) so two distinct values
    are still distinguishable in logs, and replaces the rest with ``*``.
    Values shorter than ``keep`` are fully masked.

    >>> safe_repr("sk-abcdefghijklmnop")
    'sk-a***************'
    >>> safe_repr("abcd", keep=4)
    '****'
    """
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


# ---------------------------------------------------------------------------
# Domain accessors
# ---------------------------------------------------------------------------
# One function per secret. Centralizing the key name in the function body
# (rather than scattering it through the codebase) makes the surface of
# "things that read secrets" auditable in one place. The public surface is
# the function names; the constants are kept for tests and the self-diagnostic.


def agent_token_signing_key() -> str:
    """Primary HMAC signing key for agent tokens (hha1.<payload>.<sig>)."""
    return get_secret(HH_AGENT_TOKEN_SIGNING_KEY)


def agent_token_signing_key_prev() -> Optional[str]:
    """Previous signing key, present during a 24h rotation window.

    Token verification tries the primary first, then this. If absent, only
    the primary is consulted (the normal state outside a rotation).
    """
    return get_optional(HH_AGENT_TOKEN_SIGNING_KEY_PREV)


def pwa_session_key() -> str:
    """HMAC key for PWA session cookies, CSRF tokens, and WS tickets."""
    return get_secret(HH_PWA_SESSION_KEY)


def pairing_code() -> str:
    """Initial PWA pairing code. Single-use; consumed by the first pair.

    After a successful pair the secret should be rotated by the operator
    so that the next `hh pwa pair` invocation produces a fresh code. The
    container does not enforce this — the spec leaves rotation to the
    operator workflow.
    """
    return get_secret(HH_PAIRING_CODE)


def ntfy_topic() -> str:
    """ntfy.sh topic name. Treated as a secret: the topic string itself
    is the bearer of the notification channel."""
    return get_secret(NTFY_TOPIC)


def ntfy_token() -> Optional[str]:
    """ntfy.sh access token used as the Bearer credential on POST, if any.

    Optional (2026-08-11 決定): 公開トピック運用では空のまま。呼び出し側
    （services/notifier.py）は None のとき Authorization ヘッダを一切
    付けない -- 03_Architecture.md §5.2「ntfy には権限を一切載せない」を
    公開トピックモードでも満たすため。
    """
    return get_optional(NTFY_TOKEN)


def anthropic_api_key() -> Optional[str]:
    """Anthropic API key for the Skill Distiller (Phase 1b).

    Returns None if unset. Callers must check before using; the Distiller
    will surface a clear "missing key" error rather than a generic 500.
    """
    return get_optional(ANTHROPIC_API_KEY)


def c2s_api_key() -> Optional[str]:
    """Corpus2Skill MCP API key for the read-only memory bridge (Phase 1b+)."""
    return get_optional(C2S_API_KEY)


def agentic_os_dispatch_key() -> Optional[str]:
    """Agentic_OS ヘッドレスディスパッチ用エージェントトークンの HMAC 署名鍵。

    Optional（2026-08-20 追加）: 未設定でも Hub は起動するが、
    `POST /api/dispatch/headless` のトークン検証は 401 で fail-closed になる。
    既存デプロイの起動を壊さないよう _REQUIRED_KEYS には加えない
    （_verify_required_secrets は起動を HubStartupError で止めるため）。
    """
    return get_optional(AGENTIC_OS_DISPATCH_KEY)


def agentic_os_dispatch_model() -> Optional[str]:
    """一時 HERMES_HOME の config.yaml に書く推論モデル名（任意）。

    Optional: 未設定なら hermes 側の既定モデル解決（プロバイダー既定）に委ねる。
    推論モデルを固定したい場合はこの Secret にモデル名
    （例: anthropic/claude-sonnet-4-6）を設定する。
    """
    return get_optional(AGENTIC_OS_DISPATCH_MODEL)


# ---------------------------------------------------------------------------
# __all__
# ---------------------------------------------------------------------------

__all__ = [
    # Constants
    "HH_AGENT_TOKEN_SIGNING_KEY",
    "HH_AGENT_TOKEN_SIGNING_KEY_PREV",
    "HH_PWA_SESSION_KEY",
    "HH_PAIRING_CODE",
    "NTFY_TOPIC",
    "NTFY_TOKEN",
    "ANTHROPIC_API_KEY",
    "C2S_API_KEY",
    "AGENTIC_OS_DISPATCH_KEY",
    "AGENTIC_OS_DISPATCH_MODEL",
    "ALL_SECRET_KEYS",
    # Errors
    "SecretMissingError",
    # Low-level
    "get_secret",
    "get_optional",
    "all_required_present",
    "safe_repr",
    # Domain accessors
    "agent_token_signing_key",
    "agent_token_signing_key_prev",
    "pwa_session_key",
    "pairing_code",
    "ntfy_topic",
    "ntfy_token",
    "anthropic_api_key",
    "c2s_api_key",
    "agentic_os_dispatch_key",
    "agentic_os_dispatch_model",
]
