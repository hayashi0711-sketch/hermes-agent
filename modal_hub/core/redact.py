"""modal_hub/core/redact.py — シークレット様パターンの redaction（Phase1a spec §10.3）。

設計上の位置づけ:
    - 実装契約 docs/hh-agent/05_Phase1a_Spec.md §10.3「Redaction」

このファイルは `routers/approval_gate.py`（PWA へ返す `summary`/`detail`）と
`services/audit.py`（監査レコードの `payload_redacted`/`detail`）の両方から
使われる共有実装であり、Phase1a spec §10.3 が要求する「実装は
`modal_hub/core/redact.py` の1か所に置く」を満たす。

**このファイルは `05_Phase1a_Spec.md §10.3` の所有ファイル一覧には明記されて
いないが、`routers/approval_gate.py` と `services/audit.py`（いずれも本タスク
の所有ファイル）が §10.3 の要件を満たすために必須のため、本タスクの範囲内で
新規作成した。** `core/canonical.py` に許可されている「無ければ作る」の扱いを
同じ理由（既存ファイルが無く、自分の所有ファイルが動くために必要）で redact.py
にも適用した。他の担当が既に同名ファイルを計画していた場合は要調整
（実装完了報告の「REQUESTS TO OTHER OWNERS」で明示する）。

== パターンについて ==

`REQUESTS_PATTERNS` は Phase1a spec §10.3 のコードブロックを一字一句そのまま
書き写したものである。Markdown の**表**に正規表現を書くとパイプ `|` の
エスケープ（`\\|`）が交替演算子ではなくリテラルとして実装されてしまう事故が
v1 で起きているため（05 冒頭「v1から破棄した設計」表）、本ファイルはコード
ブロック（このファイル自身）を正としてそのまま使う。

== 「最後の砦にしない」という位置づけ ==

Phase1a spec §10.3 が明記するとおり、正規表現による秘密検出は必ず取りこぼす。
本設計で秘密が外部へ漏れないことを担保しているのは redaction ではなく、
ntfy 通知にペイロードを一切載せないという構造である（親設計書 §5.2）。
本モジュールは「認証済みの PWA 画面と監査ログでの露出を減らす」ための
二次的な措置として実装する。
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# パターン定義（Phase1a spec §10.3 のコードブロックをそのまま転記）
# ---------------------------------------------------------------------------

REDACTION_PATTERNS: list[tuple[str, str]] = [
    ("anthropic", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai", r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    ("github", r"gh[pousr]_[A-Za-z0-9]{20,}"),
    ("aws", r"AKIA[0-9A-Z]{16}"),
    ("slack", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("google", r"AIza[0-9A-Za-z_-]{35}"),
    ("bearer", r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"),
    (
        "private_key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    ),
    ("conn_string", r"(?i)\b[a-z][a-z0-9+.-]*://[^:@/\s]+:[^@/\s]+@"),
    (
        "generic",
        r"(?i)(?:api[_-]?key|token|secret|password|passwd|pwd)\s*[=:]\s*\S{8,}",
    ),
]

_COMPILED: list[tuple[str, "re.Pattern[str]"]] = [
    (name, re.compile(pattern)) for name, pattern in REDACTION_PATTERNS
]


def redact_text(value: str) -> str:
    """1つの文字列に全パターンを順番に適用し、マッチ箇所を `<REDACTED:kind>` に置換する。

    Raises:
        TypeError: `value` が str でない場合（呼び出し側の型不正。黙って
            str() 変換したりしない — ユーザー指示 hard rule 3）。
    """
    if not isinstance(value, str):
        raise TypeError(f"redact_text は str を期待したが {type(value).__name__} だった")
    out = value
    for name, compiled in _COMPILED:
        out = compiled.sub(f"<REDACTED:{name}>", out)
    return out


def redact_value(value: Any) -> Any:
    """dict/list/tuple を再帰的に辿り、すべての str 値へ :func:`redact_text` を適用する。

    Phase1a spec §10.3「適用対象は『表示・保存されるすべての自由文フィールド』」
    に従い、ネストした構造（`payload` のような入れ子 dict）もまとめて処理できる
    ようにする。str/dict/list/tuple 以外の型（int/bool/None 等）はそのまま返す。
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v) for v in value)
    return value


__all__ = ["REDACTION_PATTERNS", "redact_text", "redact_value"]
