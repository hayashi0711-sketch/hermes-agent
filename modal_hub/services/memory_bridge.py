"""modal_hub/services/memory_bridge.py — Corpus2Skill MCP 読み取り専用クライアント（Phase 1b）。

設計上の位置づけ:
    - 実装契約   docs/hh-agent/03_Architecture.md D-03（Corpus2Skill Motor は
      既存 Corpus2Skill とは別物。既存 Corpus2Skill は MCP 経由の参照専用として
      温存する）、「記憶の関心分離」表（既存 Corpus2Skill は変更しない）
    - 担当表     docs/hh-agent/04_Task_Allocation.md Phase 1b 表
      「Corpus2Skill MCP の読み取りクライアント。add_new_memory を実装しない」

== このモジュールの役割 ==

Skill Distiller の novelty 判定（§3.2）を補助するための**参照専用**サイド
チャネル。既存 Corpus2Skill の記憶を検索できるが、**書き込み関数は一切
持たない**（`add_new_memory` に相当する関数を実装しない — 実装しない
だけでなく、その名前の関数がモジュールに存在しないこと自体をテストで
固定する）。

== ベストエフォート・ソフト依存（D-03） ==

Corpus2Skill は「参照専用のサイドチャネル」であり、Distiller のコア抽出
パイプラインのハード依存にしてはならない。接続できない・検索が失敗する
——いずれの場合も例外を外へ投げず `[]` を返す。呼び出し側は「クライアント
未設定」と「検索結果 0 件」を同一に扱ってよい。

== BLOCKED: 実際の MCP 起動コマンドは未確定 ==

`corpus2skill` MCP サーバーへの実際の接続方法（プロセス起動コマンド・
stdio か別トランスポートか）は、このタスクを実行したサンドボックス環境
からは特定できなかった。`connect()` は現状 `MemoryBridgeUnavailableError`
を送出するだけの骨格であり、`mcp`（プロジェクト依存関係、dev extra に
含まれる `mcp==1.28.1`）を使った実際の stdio クライアント実装は未着手。
人間が実際の起動コマンド（Claude Code の `.mcp.json` 相当の設定を
このプロジェクト用に持つ場合はそれを参照）を確認したうえで `connect()`
の中身を実装する必要がある。**この未接続状態は安全側**（`search_existing_
memory()` は常に `[]` を返すだけで、Distiller のコア機能は一切妨げない）。
"""

from __future__ import annotations

import logging
from typing import List, Optional, Protocol

logger = logging.getLogger("hh_agent.memory_bridge")


class MemoryBridgeUnavailableError(RuntimeError):
    """Corpus2Skill MCP サーバーへ接続できない。

    呼び出し側（`search_existing_memory()`）はこれを「参照情報が無い」
    として扱い、`[]` を返す（D-03: 参照専用サイドチャネル）。
    """


class Corpus2SkillClient(Protocol):
    """本モジュールが要求する最小インターフェース。

    実際の MCP ワイヤ形式を知らないコードでもこの Protocol だけを見れば
    使い方が分かるようにする（`security.py`/`audit.py` が確立した DI
    パターンと同じ意図）。
    """

    def get_memory_index(self, path: str = "") -> str:
        """指定階層の `INDEX.md` 本文を返す。"""
        ...

    def search_memory(self, query: str, limit: int = 10) -> List[dict]:
        """3段階の安価な検索を行い、マッチのリストを返す。"""
        ...


def connect() -> Corpus2SkillClient:
    """実際の `corpus2skill` MCP サーバーへ接続を試みる。

    本ファイル docstring の BLOCKED 注記のとおり、実際の起動コマンドが
    未確定なため常に例外を送出する。

    Raises:
        MemoryBridgeUnavailableError: 常に（実装未着手）。
    """
    raise MemoryBridgeUnavailableError(
        "corpus2skill MCP server launch command is not configured "
        "(see modal_hub/services/memory_bridge.py module docstring, "
        "BLOCKED note)"
    )


def search_existing_memory(
    query: str, *, limit: int = 5, client: Optional[Corpus2SkillClient] = None
) -> List[dict]:
    """既存 Corpus2Skill 記憶を検索する。

    ベストエフォート専用。クライアントが未指定なら `connect()` を試み、
    接続できない・検索が失敗する——いずれの場合も例外を投げず `[]` を
    返す（D-03: このモジュールの検索結果を Distiller のコア抽出条件
    ①②③の判定材料にしてはならない。あくまで novelty 判定の補助情報）。

    Args:
        query: 検索クエリ文字列。
        limit: 返す件数の上限。
        client: 注入するクライアント（テスト用）。省略時は `connect()`
            の戻り値を使う。

    Returns:
        マッチのリスト。取得できなければ空リスト。
    """
    resolved_client = client
    if resolved_client is None:
        try:
            resolved_client = connect()
        except MemoryBridgeUnavailableError:
            return []
        except Exception:  # noqa: BLE001 — ベストエフォート、Distiller を止めない
            logger.warning(
                "memory_bridge: unexpected error connecting to corpus2skill MCP",
                exc_info=True,
            )
            return []

    try:
        return resolved_client.search_memory(query, limit=limit)
    except Exception:  # noqa: BLE001 — ベストエフォート、Distiller を止めない
        logger.warning("memory_bridge: search_memory failed", exc_info=True)
        return []


__all__ = [
    "MemoryBridgeUnavailableError",
    "Corpus2SkillClient",
    "connect",
    "search_existing_memory",
]
