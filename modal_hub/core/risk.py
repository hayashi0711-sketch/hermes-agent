"""modal_hub/core/risk.py — H-H Agent リスク3段階分類器（Phase 1a）。

設計上の位置づけ:
    - 親設計書 docs/hh-agent/03_Architecture.md §4.2「リスク分類器」
    - 実装契約   docs/hh-agent/05_Phase1a_Spec.md §5「risk_rules.yaml スキーマとツール名の正規化」

このモジュールの唯一の公開関数は ``classify(tool_name, tool_input) -> Risk`` である。
ツール種別ごとに tool_input を正規化してから判定する。シェルコマンド専用の検出器へ
Write/Edit の dict をそのまま渡してはならない（親設計書 §4.2 の絶対原則）。

正はこのファイル。``hh_hooks/risk.py`` は ``scripts/sync_hook_modules.py``（本タスクの
所有範囲外）が生成する複製であり、手書きしてはならない（Phase1a spec §5.3）。

== 既知の危険な誤り（このモジュールで踏んではならない） ==

Hermes の ``tools.approval.detect_dangerous_command()`` は **3要素タプル** を返す。
``(False, None, None)`` も Python的には truthy であるため、
``if detect_dangerous_command(cmd):`` と書くと全コマンドが HIGH になる致命的バグになる。
このモジュールでは必ず3変数へアンパックする（下記 ``_classify_shell`` 参照）。

== フェイルクローズの方針 ==

本モジュールは承認パスの一部であり、判定に迷う場合は必ず安全側（より高いリスク）へ倒す。

    - ``tool_aliases`` に列挙されていない未知のツール名 → HIGH（``unknown_tool``。
      Phase1a spec §5.2 で明示的に規定された挙動であり、例外ではない）。
    - shell カテゴリなのに ``tool_input["command"]`` が無い/文字列でない、
      write/edit/notebook カテゴリなのに既知のパスキーが1つも見つからない、
      ``tool_name``/``tool_input`` の型がそもそも不正——これらは「未知のツール」とは異なり
      「既知のはずのツールの形が壊れている」ケースなので、黙って握りつぶさず例外を送出する
      （ユーザー指示の hard rule 3「Unexpected shapes must raise, never silently return
      empty」）。呼び出し側（hh_hooks/tool_gate.py、本タスクの所有範囲外）が
      フェイルクローズ（deny）として扱うことを前提とする。

== このモジュールが独自に埋めた判断（BLOCKED にはせず、報告で開示する） ==

1. **Write/Edit/NotebookEdit の対象パスキー名**: 親設計書・Phase1a spec のどちらにも
   tool_input の正確なキー名は明記されていない。Claude Code 組み込みツールは
   ``file_path``、Hermes 内蔵の file_operations.py 系ツールは ``path``、
   Claude Code の NotebookEdit は ``notebook_path`` を使う（本リポジトリの
   ``tools/file_operations.py`` 実測）。これらを優先順に探索し、どれも見つからなければ
   例外を送出する（上記の「未知の形」扱い）。
2. **``applies_to: [shell]`` を持つ ``path_pattern`` ルール（``secret_path``）の適用方法**:
   ``path_pattern`` は ``(^|/)...$`` の形で「パスの終端」を要求するアンカー付き正規表現
   であり、シェルコマンド全体の文字列にそのまま re.search を掛けても
   （例: ``"cat .env"`` は ``.env`` の直前が空白でありスラッシュにも先頭にもならないため）
   ほぼ確実にマッチしない。これでは ``applies_to`` に ``shell`` を含めた意図（§5.1 の
   secret_path ルール）が実質的に無効化され、偽陰性を積極的に作ってしまう
   （親設計書 §4.2「偽陰性は許容しない」に反する）。そのためコマンド文字列を
   shlex 相当でトークン化し、各トークンに対して ``path_pattern`` を適用する
   （トークン境界が ``^``/``$`` の役割を果たす）。トークン化に失敗した場合は
   空白区切りへフォールバックする（判定を諦めて無条件で通すより安全側）。
   この2点は正規表現マッチングの厳密な仕様がスペックに存在しない領域での実装判断であり、
   REQUESTS TO OTHER OWNERS として報告する。
3. **Hermes 検出器がヒットした場合の rule_id**: ``detect_dangerous_command()`` が返す
   ``pattern_key``/``description`` は自然文の説明であり、値の種類が無数に存在しうる。
   Phase1a spec §1.2 は ``reason_code`` を「``rule_id`` と同じ語彙」と定義し、サーバ側は
   ``rule_id`` から表示文言を引く設計になっている（＝ ``rule_id`` は閉じた小さな語彙集合
   であることが前提）。そのため Hermes 検出器がヒットしたケースは固定の
   ``rule_id="hermes_dangerous_command"`` に統一し、Hermes 側の自然文説明は
   ``reason``（ローカル用途。ワイヤーには乗らない）にのみ残す。
"""

from __future__ import annotations

import dataclasses
import functools
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Iterable, Optional

import yaml

# ---------------------------------------------------------------------------
# Hermes 本体の危険コマンド検出器を再利用する（親設計書 §4.2）。
#
# tools/approval.py はリポジトリルート直下の "tools" パッケージにある。
# このファイルは modal_hub/core/risk.py として存在するが、Phase1a spec §5.3 の
# 「生成コピー」により hh_hooks/risk.py としても配置される（配置の深さが変わる）。
# 固定の parents[N] に依存すると複製先で壊れるため、"tools/approval.py" と
# "hermes_cli/" が両方存在するディレクトリを上方探索してリポジトリルートを特定する。
#
# 2026-08-11 改訂（実測に基づく。親設計書 §4.3 参照）: 実際の `from tools.approval
# import detect_dangerous_command` は下の `_get_detect_dangerous_command()` へ
# 遅延させ、shell カテゴリの分類が実際に必要になった時点で初めて import する。
# `tools.approval` の import 単体が実測 215〜220ms かかり、これが Read/Grep 等の
# 非シェル系 allow 経路（200ms 未満の予算）を直撃していたため。
#
# 2026-08-11 追記（Modal デプロイで発覚・修正）: `_find_repo_root()` と
# `sys.path` の設定も上記 import と合わせて `_get_detect_dangerous_command()` の
# 中へ遅延させる。05_Phase1a_Spec.md §1.2「サーバはパス等を再計算しない
# （Windows/Linux でパス正規化の結果が一致しないため）」の通り、Hub
# （modal_hub/routers/approval_gate.py）は risk.classify() を一度も呼ばず、
# `_load_rules()` のみを使う（表示文言の rule_id 引き当て）。したがって Hub 環境
# には Hermes 本体（tools/approval.py・hermes_cli/）が存在せず、これらをモジュール
# import 時に即座に探索すると Hub の起動そのものが `RuntimeError` でクラッシュする
# （実際に modal deploy 後に発生）。shell カテゴリの分類は tool_gate.py を持つ
# ローカル環境でのみ呼ばれるため、探索自体も遅延させて Hub 側の import を無傷にする。
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "tools" / "approval.py").is_file() and (candidate / "hermes_cli").is_dir():
            return candidate
    raise RuntimeError(
        "Hermes リポジトリルートを特定できない"
        f"（tools/approval.py と hermes_cli/ を持つ祖先ディレクトリが無い）: start={start}"
    )


@functools.lru_cache(maxsize=1)
def _get_detect_dangerous_command():
    """Hermes 本体の危険コマンド検出器を遅延 import して返す（2026-08-11 改訂）。

    非シェル系ツール（Read/Grep/Glob/Write/Edit/未知ツール）は shell カテゴリの
    分類に到達しないため、この import のコスト（実測 215〜220ms）を一切踏まない。
    shell カテゴリの分類が実際に呼ばれた時点で初めて `_find_repo_root()` を実行し
    `sys.path` を設定してから import する。以降は sys.modules キャッシュとこの関数
    自身の lru_cache により1プロセス内では一度しか実行しない。

    フェイルクローズ: リポジトリルートの特定・import のいずれが失敗しても、この関数は
    例外をそのまま呼び出し元（`_classify_shell` → `classify`）へ伝播させる。握りつぶして
    LOW 等へフォールバックしてはならない。`classify()` の他の例外パスと同様、呼び出し側
    （hh_hooks/tool_gate.py、本タスクの所有範囲外）がこれをフェイルクローズ
    （deny）として扱うことを前提とする。
    """
    repo_root = _find_repo_root(Path(__file__).resolve())
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from tools.approval import detect_dangerous_command

    return detect_dangerous_command


# ---------------------------------------------------------------------------
# 公開データ型
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Risk:
    """classify() の戻り値。

    親設計書 §4.2: ``Risk(level, rule_id, reason, normalized_target)``。

    Attributes:
        level: "HIGH" | "MEDIUM" | "LOW"。
        rule_id: マッチしたルールの id、または "unknown_tool" / "read_only" / "no_match"。
        reason: 人間向けの理由文字列（読み取り専用・未マッチのときは空文字列）。
        normalized_target: 判定に使った正規化後の対象
            （shell はコマンド文字列そのもの、write/edit/notebook は
            os.path.realpath() 済みかつ区切り文字を "/" に統一したパス）。
            "unknown_tool" / "read_only" のように正規化の前段で判定が確定した場合は
            None のままでよい（Phase1a spec §5.2 の疑似コードは normalized_target を
            渡さない3引数呼び出しであり、本フィールドにはデフォルト値を持たせる）。
    """

    level: str
    rule_id: str
    reason: str
    normalized_target: Optional[str] = None


# ---------------------------------------------------------------------------
# risk_rules.yaml の読み込み
# ---------------------------------------------------------------------------

_RULES_PATH = Path(__file__).resolve().with_name("risk_rules.yaml")

_REQUIRED_TOP_LEVEL_KEYS = ("version", "tool_aliases", "read_only_categories", "high", "medium")


@functools.lru_cache(maxsize=1)
def _load_rules() -> dict:
    """risk_rules.yaml を読み込み、最低限のスキーマ検証をして返す。

    プロセス内で1度だけ読み込む（``functools.lru_cache``）。テストで別内容の
    risk_rules.yaml を差し替えたい場合は ``_load_rules.cache_clear()`` を呼ぶこと。
    """
    with open(_RULES_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"risk_rules.yaml の内容が不正: dict を期待したが {type(data)!r} だった")
    for key in _REQUIRED_TOP_LEVEL_KEYS:
        if key not in data:
            raise ValueError(f"risk_rules.yaml に必須キー '{key}' が無い")
    if not isinstance(data["tool_aliases"], dict):
        raise ValueError("risk_rules.yaml: tool_aliases は dict でなければならない")
    if not isinstance(data["read_only_categories"], list):
        raise ValueError("risk_rules.yaml: read_only_categories は list でなければならない")
    for level in ("high", "medium"):
        if not isinstance(data[level], list):
            raise ValueError(f"risk_rules.yaml: '{level}' は list でなければならない")
        for rule in data[level]:
            if not isinstance(rule, dict) or "id" not in rule or "applies_to" not in rule:
                raise ValueError(f"risk_rules.yaml: '{level}' 内のルールに id/applies_to が無い: {rule!r}")
            if "pattern" not in rule and "path_pattern" not in rule:
                raise ValueError(
                    f"risk_rules.yaml: ルール '{rule.get('id')}' に pattern も path_pattern も無い"
                )
            # "reason" は必須ではない（Phase1a spec §5.1 の pkg_install ルールに reason が
            # 無いことを確認済み）。未指定は classify() 側で空文字列として扱う。
    return data


@functools.lru_cache(maxsize=1)
def _alias_index() -> dict:
    """生ツール名 → カテゴリ名 のフラットな索引を作る（大文字小文字を区別する完全一致）。"""
    rules = _load_rules()
    index: dict = {}
    for category, names in rules["tool_aliases"].items():
        for name in names:
            index[name] = category
    return index


def _alias_lookup(tool_name: str) -> Optional[str]:
    return _alias_index().get(tool_name)


def category_of(tool_name: str) -> Optional[str]:
    """tool_name のカテゴリ（"shell" / "write" / "edit" / "notebook" / 読み取り系など）を返す公開API。

    ``risk_rules.yaml`` の ``tool_aliases`` に列挙されていない未知のツール名は
    ``None`` を返す（``classify()`` がこの場合に HIGH/"unknown_tool" へ格上げする
    のと同じ判定材料）。実装は ``_alias_lookup()`` への薄いラッパーであり、
    呼び出し側（例: hh_hooks/tool_gate.py）が private 関数を直接呼ばずに済むように
    公開する。``_alias_lookup`` 自体はモジュール内部からの利用のため残す。
    """
    return _alias_lookup(tool_name)


def _iter_candidate_rules(category: str) -> Iterable[tuple]:
    """category に適用可能なルールを high → medium の順・各リスト内は先頭からの順序で yield する。"""
    rules = _load_rules()
    for level_key, level_name in (("high", "HIGH"), ("medium", "MEDIUM")):
        for rule in rules[level_key]:
            if category in rule["applies_to"]:
                yield level_name, rule


# ---------------------------------------------------------------------------
# shell カテゴリ
# ---------------------------------------------------------------------------


def _tokenize_shell_command(command: str) -> list:
    """コマンド文字列をトークン化する。パース不能な引用符構造は空白区切りへフォールバックする。

    path_pattern（``(^|/)...$`` 形式）はトークン境界を前提とするため、判定不能を理由に
    トークン化そのものを諦める（＝path_pattern 判定を丸ごとスキップする）よりは、
    多少不正確でも空白区切りにフォールバックしてマッチを試みる方が安全側である。

    2026-08-11 CRITICAL 修正（BUG-5）: 以前は ``shlex.split(command, posix=True)`` を
    使っていたが、POSIX モードは引用符の外側にあるバックスラッシュを**エスケープ文字と
    して消費する**。Windows のコマンド文字列（PowerShell / cmd.exe）はパス区切りに
    ``\\`` を使うため、引用符の無い Windows パスがバックスラッシュごと破壊されていた:

        ``Get-Content C:\\Users\\Haruki\\.ssh\\id_rsa``
        → posix=True では ``['Get-Content', 'C:UsersHaruki.sshid_rsa']``
        → secret_path の path_pattern が絶対にマッチしなくなる（致命的な偽陰性）。

    修正: ``shlex.split(command, posix=False)`` を使う。non-POSIX モードは引用符の
    外側でも中でもバックスラッシュを特別扱いしない（＝そのまま残す）ため、引用符の
    有無によらず Windows パスのバックスラッシュが保存される。ただし non-POSIX モードは
    引用符文字そのものをトークンから剥がさない（例: ``"C:\\a b"`` → トークン文字列に
    引用符が残る）ため、先頭・末尾が対応する引用符（``"`` または ``'``）であれば
    手動で1組だけ剥がす。

    このトークン自体は POSIX パス（``~/.ssh/id_rsa`` 等）にバックスラッシュが含まれない
    ため無変化で、既存の POSIX 形式の判定を回帰させない。Windows パスの ``\\`` → ``/``
    正規化は呼び出し側（``_classify_shell``）でトークンとは別に行う（このトークン自体は
    元の綴りを保つ）。
    """
    try:
        raw_tokens = shlex.split(command, posix=False)
    except ValueError:
        return command.split()

    tokens = []
    for tok in raw_tokens:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
            tok = tok[1:-1]
        tokens.append(tok)
    return tokens


def _classify_shell(command: str) -> Risk:
    # Hermes 既存検出器の呼び出し契約（Codex 検証済み・親設計書 §4.2）:
    # 必ず3要素にアンパックする。(False, None, None) は truthy なので
    # `if detect_dangerous_command(command):` は絶対に書かない。
    # 2026-08-11 改訂: import 自体も遅延させたのでここで初めて取得する。
    detect_dangerous_command = _get_detect_dangerous_command()
    is_dangerous, pattern_key, description = detect_dangerous_command(command)
    if is_dangerous:
        # rule_id は固定の "hermes_dangerous_command" にする（pattern_key/description を
        # そのまま rule_id へ流用しない）。Phase1a spec §1.2 は reason_code を
        # 「rule_id と同じ語彙」と定義し、サーバは rule_id から表示文言を引く設計
        # （＝ rule_id は閉じた小さな語彙集合であることが前提）。Hermes の
        # detect_dangerous_command() が返す pattern_key/description は自然文の説明
        # （例: "git force push (rewrites remote history)"）であり無数のバリエーションを
        # 持ちうるため、これをそのまま rule_id にすると reason_code のクローズド語彙という
        # 前提を壊す。Hermes 側の詳細情報は reason（ローカル用途のみ・ワイヤーには
        # 乗らない）に残す。
        detail = description or pattern_key or "詳細不明"
        return Risk(
            level="HIGH",
            rule_id="hermes_dangerous_command",
            reason=f"Hermes 危険コマンド検出器がマッチ: {detail}",
            normalized_target=command,
        )

    tokens = _tokenize_shell_command(command)
    # BUG-5 対策: path_pattern はトークンの元の綴り（POSIX パスならそのまま）と、
    # "\" を "/" に置換した Windows 正規化形の両方に対して照合する。
    # 置換はトークンごとに独立して行うため、バックスラッシュを含まない POSIX トークン
    # （例: "~/.ssh/id_rsa"）は無変化であり、既存の POSIX 判定を回帰させない。
    # set() で重複除去することで、バックスラッシュの無いトークンは1回しか照合しない。
    token_variants = [{token, token.replace("\\", "/")} for token in tokens]
    for level, rule in _iter_candidate_rules("shell"):
        if "pattern" in rule:
            if re.search(rule["pattern"], command, re.IGNORECASE):
                return Risk(level, rule["id"], rule.get("reason", ""), command)
        else:  # "path_pattern"
            if any(
                re.search(rule["path_pattern"], variant, re.IGNORECASE)
                for variants in token_variants
                for variant in variants
            ):
                return Risk(level, rule["id"], rule.get("reason", ""), command)

    return Risk("LOW", "no_match", "", command)


# ---------------------------------------------------------------------------
# write / edit / notebook カテゴリ
# ---------------------------------------------------------------------------

# tool_input からファイル対象パスを取り出す際に探索するキー名（優先順）。
# 上部docstring「独自に埋めた判断 1」を参照。
_PATH_INPUT_KEYS = ("file_path", "path", "notebook_path")


def _extract_target_path(tool_name: str, tool_input: dict) -> str:
    for key in _PATH_INPUT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise ValueError(
        f"ツール {tool_name!r} の tool_input に既知のパスキー {_PATH_INPUT_KEYS} の"
        f"いずれも見つからない（想定外の形）: keys={sorted(tool_input.keys())}"
    )


def _normalize_path_target(raw_path: str) -> str:
    """Phase1a spec §3 の PathStr 正規化: realpath → '\\' を '/' に置換。大文字小文字は変換しない。"""
    return os.path.realpath(raw_path).replace("\\", "/")


def _classify_path_target(category: str, tool_name: str, tool_input: dict) -> Risk:
    raw_path = _extract_target_path(tool_name, tool_input)
    normalized = _normalize_path_target(raw_path)

    for level, rule in _iter_candidate_rules(category):
        if "path_pattern" not in rule:
            # write/edit/notebook に適用される "pattern"（コマンド文字列用）ルールは
            # risk_rules.yaml のスキーマ上ありえない組み合わせ。設定ミスとして扱う。
            raise ValueError(
                f"risk_rules.yaml: ルール '{rule['id']}' は category={category!r} に適用されるが "
                "path_pattern を持たない（pattern はコマンド文字列専用）"
            )
        if re.search(rule["path_pattern"], normalized, re.IGNORECASE):
            return Risk(level, rule["id"], rule.get("reason", ""), normalized)

    return Risk("LOW", "no_match", "", normalized)


# ---------------------------------------------------------------------------
# 公開エントリポイント
# ---------------------------------------------------------------------------


def classify(tool_name: str, tool_input: dict) -> Risk:
    """ツール種別ごとに正規化してから HIGH/MEDIUM/LOW を判定する。

    Args:
        tool_name: Claude Code / Hermes から渡される生のツール名（例: "Bash", "Write"）。
        tool_input: そのツール呼び出しの引数 dict。

    Returns:
        Risk。

    Raises:
        TypeError: tool_name / tool_input の型がそもそも不正な場合。
        ValueError: 既知のツールカテゴリなのに想定した形（shell の command、
            write/edit/notebook のパスキー、risk_rules.yaml 自体のスキーマ）が
            崩れている場合。フェイルクローズの前提として、ここでは決して
            握りつぶさず例外を送出する。
    """
    if not isinstance(tool_name, str) or not tool_name:
        raise TypeError(f"tool_name は非空の str でなければならない: {tool_name!r}")
    if not isinstance(tool_input, dict):
        raise TypeError(f"tool_input は dict でなければならない: {type(tool_input)!r}")

    rules = _load_rules()
    category = _alias_lookup(tool_name)

    # Phase1a spec §5.2: 未知のツールは既定で HIGH に格上げする。
    # MCP ツール・カスタムツールはすべてここに落ちる。これは意図した挙動であり、
    # 自動学習・自動追加は行わない。
    if category is None:
        return Risk("HIGH", "unknown_tool", f"未知のツール {tool_name} は副作用ありとみなす")

    if category in rules["read_only_categories"]:
        return Risk("LOW", "read_only", "")

    if category == "shell":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError(
                f"shell 系ツール {tool_name!r} の tool_input に command(str) が無い: "
                f"keys={sorted(tool_input.keys())}"
            )
        return _classify_shell(command)

    if category in ("write", "edit", "notebook"):
        return _classify_path_target(category, tool_name, tool_input)

    # tool_aliases に category は定義されているが、このモジュールが判定方法を
    # 知らないカテゴリ（risk_rules.yaml の設定ミス）。黙って LOW にはしない。
    raise ValueError(
        f"risk_rules.yaml: tool_aliases に未対応のカテゴリ '{category}' が定義されている"
        "（対応済み: read, shell, write, edit, notebook）"
    )
