"""modal_hub/services/skill_quarantine.py — 隔離保存・materialize（Phase 1b、安全性クリティカル）。

設計上の位置づけ:
    - 実装契約   docs/hh-agent/07_Phase1b_Spec.md §0.1（D-16: Hermes が探索する
      どのディレクトリにも直接書かない）、§4.1（バージョニング・名前衝突・
      materialize の高々 1 回性）
    - 担当表     docs/hh-agent/04_Task_Allocation.md Phase 1b 表
      「隔離保存・skill_name 検証・パス封じ込め・原子的書き込み
      （安全性クリティカル。D-16）」

== このモジュールの役割 ==

生成された SKILL.md 本文を `~/.hh-agent/skills_quarantine/<name>/SKILL.md`
へ保存する。**Hermes が探索するどのディレクトリ（`~/.hermes/skills/` および
`config.yaml` の `skills.external_dirs`）にも絶対に書かない** — 隔離領域は
Hermes の探索対象の外にあることが D-16 の核心であり、`scripts/
hh_skill_promote.py`（別ファイル・同一所有者）の人間確認を経て初めて
Hermes 側へ移動する。

== materialize の高々 1 回性（§4.1・M-08）とクラッシュ回復 ==

`hh_distill.py`（MiniMax 所有・段階2の状態機械）はクラッシュ後の再実行で
同じ `queue_entry_id` を再処理しうる。呼び出し側の状態機械だけに冪等性を
委ねると「呼び出し側が正しく `completed/<id>.json` を先にチェックする」と
いう規律に依存することになるため、**このモジュール自体が
`queue_entry_id` をキーに冪等性を担保する**（安全性クリティカルな責務を
外部の規律に依存させない）。

2 段階の耐久記録を使う（`07_Phase1b_Spec.md` 全体で繰り返される
「意図の耐久記録 → 実行 → 確定記録」パターンと同型）:

    1. `.materializing/<queue_entry_id>.json`（排他作成）に、これから
       使う `name` を書く。これが「どの名前を使うと決めたか」の耐久記録。
    2. `<name>/SKILL.md` を原子的に書く（temp + fsync + os.replace）。
    3. `.materialized/<queue_entry_id>.json`（確定記録）を書いてから
       `.materializing/<queue_entry_id>.json` を削除する。

再実行時:
    - `.materialized/<id>.json` があれば、それをそのまま返す（新規書き込み
      なし。これが最も一般的な「正常に完了済み」の再呼び出し）。
    - 無いが `.materializing/<id>.json` がある（ステップ 2〜3 の間で
      クラッシュした）場合、そこに記録された `name` の
      `<name>/SKILL.md` を読み、ダイジェストが一致すれば「実は完了して
      いた」として確定記録を書いて回復する。一致しなければ（部分書き込み
      の疑いがある）安全側に倒して例外にする — 人間の確認が必要な状態
      であり、自動では推測しない。

== 名前衝突（§4.1） ==

**新しく保存しようとしている内容の方が `<name>-2` へ退避する**（既存の
隔離済みスキルを上書きしない）。ベース名が 47 文字を超える場合は 47 文字に
切り詰めてから `-2` を付ける。`-2`〜`-9` まで試して全て衝突していたら
`QuarantineError`。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

_HH_AGENT_HOME_ENV = "USERPROFILE"

#: `promote` の CLI 境界検証（07_Phase1b_Spec.md §4.2 手順2）と完全に同じ
#: 規則。`scripts/hh_skill_promote.py`（同一所有者）はこの定数を import
#: して使うこと — 手書き複製で規則が食い違うと、quarantine には保存
#: できたのに promote では拒否される（またはその逆の）分かりにくい不整合
#: を生む。
#: `\Z`（文字列末尾そのもの）を使う。`$` は Python の正規表現では末尾の
#: 改行1個の直前にもマッチするため、`.match("valid-name\n")` が意図せず
#: 通ってしまう（2026-08-11 Codex レビュー Low 指摘・実機で確認済み）。
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}\Z")

_MAX_BASE_NAME_LEN_BEFORE_SUFFIX = 47
_MAX_COLLISION_SUFFIX = 9


class QuarantineError(RuntimeError):
    """隔離保存に失敗した。呼び出し側（`hh_distill.py`）はこの例外を
    `failed/` への遷移として扱うこと（07_Phase1b_Spec.md §2.3 手順4）。
    """


#: `queue_entry_id` はホスト（`hh_distill.py`）から渡される値だが、
#: 07_Phase1b_Spec.md §1.3 の規則（Anthropic `custom_id` 制約と同一）に
#: 常に一致する前提でパス構成要素として使っている。呼び出し側の実装が
#: 誤って規則外の値を渡した場合にパストラバーサルへ悪用されないよう、
#: このモジュール自身でも検証する（2026-08-11 Codex レビュー Medium 指摘）。
QUEUE_ENTRY_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}\Z")


def hh_agent_home() -> Path:
    userprofile = os.environ.get(_HH_AGENT_HOME_ENV)
    base = Path(userprofile) if userprofile else Path.home()
    return base / ".hh-agent"


def quarantine_root(base: Optional[Path] = None) -> Path:
    return (base or hh_agent_home()) / "skills_quarantine"


def _materializing_dir(base: Optional[Path] = None) -> Path:
    return quarantine_root(base) / ".materializing"


def _materialized_dir(base: Optional[Path] = None) -> Path:
    return quarantine_root(base) / ".materialized"


def _validate_queue_entry_id(queue_entry_id: str) -> str:
    if not isinstance(queue_entry_id, str) or not QUEUE_ENTRY_ID_RE.match(queue_entry_id):
        raise QuarantineError(f"invalid queue_entry_id: {queue_entry_id!r}")
    return queue_entry_id


# ---------------------------------------------------------------------------
# D-16: 隔離領域が Hermes の実スキャン対象と重ならないことの確認
# ---------------------------------------------------------------------------
#
# `scripts/hh_skill_promote.py`（同一所有者）の `assert_staging_root_is_safe()`
# と完全に同じアルゴリズムを、`promote_staging/` の代わりに `skills_quarantine/`
# へ適用する。2026-08-11 Codex レビュー Critical 指摘: 旧実装は
# `materialize()` がこのチェックを一切行っておらず、`quarantine_root` が
# 誤って（または攻撃的に）`skills.external_dirs` に含まれていた場合、
# 隔離のつもりの保存が実際には Hermes の自動スキャン対象になってしまう
# （D-16 の核心を破る）。
#
# `_existing_hermes_scan_dirs()`/`_declared_hermes_scan_dirs_including_
# nonexistent()` はモジュール関数として分離してある（monkeypatch でテスト
# 隔離できるように — 実 Hermes 環境の `config.yaml` を単体テストが読みに
# 行ってしまうと、実行環境依存でテストが不安定になる）。


def _existing_hermes_scan_dirs() -> List[Path]:
    import agent.skill_utils as skill_utils

    return list(skill_utils.get_all_skills_dirs())


def _declared_hermes_scan_dirs_including_nonexistent() -> List[Path]:
    """`hh_skill_promote.py` の同名関数と同一の正規化・解決手順。"""
    import hermes_constants

    config_path = hermes_constants.get_config_path()
    if not config_path.is_file():
        return []

    try:
        import yaml

        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — フェイルクローズ（下記 raise）
        raise QuarantineError(
            f"failed to read/parse config.yaml (fail-closed): {type(exc).__name__}"
        ) from exc

    if not isinstance(parsed, dict):
        raise QuarantineError("config.yaml did not parse to a mapping (fail-closed)")

    skills_cfg = parsed.get("skills")
    if not isinstance(skills_cfg, dict):
        return []

    raw_dirs = skills_cfg.get("external_dirs")
    if raw_dirs is None:
        return []
    if isinstance(raw_dirs, str):
        raw_dirs = [raw_dirs]
    if not isinstance(raw_dirs, list):
        return []

    hermes_home = hermes_constants.get_hermes_home()
    out: List[Path] = []
    for entry in raw_dirs:
        entry = str(entry).strip()
        if not entry:
            continue
        expanded = os.path.expanduser(os.path.expandvars(entry))
        p = Path(expanded)
        p = p.resolve() if p.is_absolute() else (hermes_home / p).resolve()
        out.append(p)
    return out


def _same_or_ancestor_or_descendant(a: Path, b: Path) -> bool:
    a_parts = tuple(os.path.normcase(p) for p in a.parts)
    b_parts = tuple(os.path.normcase(p) for p in b.parts)
    return a_parts[: len(b_parts)] == b_parts or b_parts[: len(a_parts)] == a_parts


def assert_quarantine_root_is_safe(*, base: Optional[Path] = None) -> None:
    """隔離領域が Hermes の実スキャン対象と重ならないことを確認する。

    重なっていれば（一致・配下・祖先いずれも）`QuarantineError`。
    `config.yaml` の読み取り失敗はフェイルクローズで伝播する。
    """
    root = quarantine_root(base).resolve()
    candidates = (
        _existing_hermes_scan_dirs() + _declared_hermes_scan_dirs_including_nonexistent()
    )
    for candidate in candidates:
        if _same_or_ancestor_or_descendant(root, candidate):
            raise QuarantineError(
                f"quarantine root {root} overlaps a Hermes-scanned skills "
                f"directory ({candidate}); refusing to materialize anything"
            )


@dataclass(frozen=True)
class MaterializeResult:
    name: str
    output_path: str
    content_sha256: str


def validate_skill_name(name: str) -> str:
    """`^[a-z0-9][a-z0-9-]{1,48}$` を満たすことを確認して返す。

    満たさなければ `QuarantineError`（呼び出し側は promote 前の段階で
    弾かれたことを意味し、`failed/` へ倒す）。
    """
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise QuarantineError(f"invalid skill name: {name!r}")
    return name


def _resolve_within(root: Path, relative_dir_name: str) -> Path:
    """`root` 直下の `relative_dir_name` ディレクトリを返す。

    `resolve()` した結果の親が `root` の `resolve()` と一致しない場合
    （symlink・reparse point・`../` を含む名前等）は拒否する。`root` 自体が
    symlink/junction の場合も拒否する（2026-08-11 Codex レビュー Critical
    指摘: `root` が置き換えられていると、子の resolve() 一致チェックは
    「置き換え先の中で一致している」だけになり素通りしてしまう）。

    完全な TOCTOU 除去（このチェックと実際の書き込みの間で `root` や
    `relative_dir_name` が置き換えられる競合）は Windows で `O_NOFOLLOW`
    相当が無いため構造的に閉じられない。単一ユーザー・ローカル運用という
    前提のもとで許容する残存リスクとして記録する。
    """
    if root.exists() and root.is_symlink():
        raise QuarantineError(f"{root} itself is a symlink/junction, refusing")
    candidate = root / relative_dir_name
    if candidate.exists() and candidate.is_symlink():
        raise QuarantineError(f"{candidate} is a symlink/junction, refusing")
    if candidate.resolve().parent != root.resolve():
        raise QuarantineError(
            f"{relative_dir_name!r} resolves outside the quarantine root"
        )
    return candidate


def parse_frontmatter_name(skill_md: str) -> Optional[str]:
    """SKILL.md の YAML frontmatter から `name` フィールドを取り出す。

    frontmatter が無い・パースできない・`name` が文字列でない場合は
    None を返す（呼び出し側が「不一致」として扱えるようにするため、
    ここでは例外にしない）。
    """
    if not skill_md.startswith("---"):
        return None
    end = skill_md.find("\n---", 3)
    if end == -1:
        return None
    frontmatter_text = skill_md[3:end]
    try:
        import yaml

        data = yaml.safe_load(frontmatter_text)
    except Exception:  # noqa: BLE001 — 壊れた frontmatter は「無い」と同じ扱い
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    return name if isinstance(name, str) else None


def _atomic_write_text(target: Path, content: str) -> None:
    """`content` を UTF-8 バイト列として原子的に書き込む。

    **必ずバイナリモードで書く。** テキストモード（`open(..., "w")`）は
    Windows で改行を `\\n` → `\\r\\n` へ無言変換し、`materialize()` が
    書き込み**前**の文字列から計算した `content_sha256` と、書き込み
    **後**にバイト列として読み直した（`Path.read_bytes()`）内容のダイジェスト
    が食い違う実バグを生んだ（2026-08-11 テストで発覚。`Path.read_text()`
    は読み込み側でも同じ変換を行って戻すため、書き込み・検証の両方が
    テキストモードだと症状が相殺されて見えなくなる — `hh_skill_promote.py`
    の `read_bytes()` 経由でのみ顕在化した）。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    content_bytes = content.encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".tmp.", suffix=".part"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _reservation_marker_path(root: Path, base: Optional[Path], candidate_name: str) -> Path:
    return _materializing_dir(base) / f"name-{candidate_name}.reserved"


def _reserve_and_find_available_name(root: Path, base_name: str, *, base: Optional[Path]) -> str:
    """§4.1: 衝突していなければ `base_name` そのもの、衝突していれば
    `<base_name>-2`〜`-9` を順に試す。全滅なら `QuarantineError`。

    2026-08-11 Codex レビュー Medium 指摘の修正: 旧実装は「空いているか
    確認 → 後で intent ファイルを書く」という check-then-act だったため、
    2 つの異なる `queue_entry_id` が同時に同じ空き名前を選び、両方が
    同じ `<name>/SKILL.md` を上書きし合う競合があった。ここでは候補ごとに
    `name-<candidate>.reserved` を排他作成し、**予約そのものを原子化**
    する（`os.open(..., O_CREAT|O_EXCL)` 相当を `open(path, "xb")` で行う。
    "xb" は既存ファイルがあれば `FileExistsError` を送出する）。
    """
    truncated = base_name[:_MAX_BASE_NAME_LEN_BEFORE_SUFFIX]
    candidates = [base_name] + [f"{truncated}-{i}" for i in range(2, _MAX_COLLISION_SUFFIX + 1)]

    _materializing_dir(base).mkdir(parents=True, exist_ok=True)

    for candidate in candidates:
        try:
            skill_file = _resolve_within(root, candidate) / "SKILL.md"
        except QuarantineError:
            continue  # 名前として使えない ＝ 空きが無いのと同義
        if skill_file.is_file():
            continue  # 既に確定済みの別スキルがある

        marker = _reservation_marker_path(root, base, candidate)
        try:
            with open(marker, "xb"):
                pass
        except FileExistsError:
            continue  # 他の queue_entry_id が同時に予約した
        return candidate

    raise QuarantineError(
        f"no available quarantine slot for {base_name!r} after "
        f"-2..-{_MAX_COLLISION_SUFFIX} collisions"
    )


def _release_reservation(root: Path, base: Optional[Path], name: str) -> None:
    """確定後は `SKILL.md` の存在自体が「予約済み」を意味するため、予約
    マーカーは削除してよい（クリーンアップの体裁を保つだけで、消し忘れても
    衝突判定に影響しない — `_reserve_and_find_available_name()` は先に
    `SKILL.md` の有無を見るため）。"""
    marker = _reservation_marker_path(root, base, name)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def materialize(
    queue_entry_id: str,
    base_name: str,
    skill_md_content: str,
    *,
    base: Optional[Path] = None,
) -> MaterializeResult:
    """SKILL.md を隔離領域へ保存する。`queue_entry_id` ごとに高々1回だけ
    実際の書き込みが発生する（本ファイル docstring 参照）。

    Raises:
        QuarantineError: `queue_entry_id`/名前が不正、隔離領域が Hermes
            スキャン対象と重なっている（D-16）、衝突枠が枯渇、または
            過去のクラッシュ状態から安全に回復できない場合。
    """
    _validate_queue_entry_id(queue_entry_id)
    if not isinstance(skill_md_content, str) or not skill_md_content.strip():
        raise QuarantineError("skill_md_content must be a non-empty string")

    root = quarantine_root(base)
    root.mkdir(parents=True, exist_ok=True)
    _materializing_dir(base).mkdir(parents=True, exist_ok=True)
    _materialized_dir(base).mkdir(parents=True, exist_ok=True)

    # D-16: 隔離領域そのものが Hermes の実スキャン対象と重なっていないかを
    # 毎回確認する（config.yaml は実行中に書き換わりうるため、キャッシュ
    # せず呼び出しごとに確認する。2026-08-11 Codex レビュー Critical 指摘）。
    assert_quarantine_root_is_safe(base=base)

    materialized_path = _materialized_dir(base) / f"{queue_entry_id}.json"
    existing = _read_json(materialized_path)
    if existing is not None:
        return MaterializeResult(
            name=existing["name"],
            output_path=existing["output_path"],
            content_sha256=existing["content_sha256"],
        )

    content_sha256 = _content_sha256(skill_md_content)
    intent_path = _materializing_dir(base) / f"{queue_entry_id}.json"
    intent = _read_json(intent_path)

    if intent is not None:
        # ステップ2〜3の間でクラッシュした可能性のある再実行。
        resolved_name = validate_skill_name(intent["name"])
        skill_file = _resolve_within(root, resolved_name) / "SKILL.md"
        if skill_file.is_file():
            on_disk = skill_file.read_text(encoding="utf-8")
            digest = _content_sha256(on_disk)
            if digest == content_sha256:
                # 実際には完了していた。確定記録を書いて回復する。
                return _finalize(
                    queue_entry_id, resolved_name, skill_file, content_sha256, base=base
                )
            raise QuarantineError(
                f"queue_entry_id={queue_entry_id!r} has an inconsistent "
                f".materializing record (name={resolved_name!r} on-disk "
                f"digest={digest!r} != expected {content_sha256!r}); "
                "manual inspection required before retrying"
            )
        # 2026-08-11 Codex レビュー Medium 指摘の修正: 旧実装はここで
        # 無条件に QuarantineError を送出していたが、「意図の耐久記録
        # （intent）は書けたが SKILL.md 本体を書く前にクラッシュした」
        # という完全に無害な中断ケースまで永続的な失敗として扱っていた。
        # `resolved_name` は既にこの queue_entry_id 用に予約済みの名前
        # なので、そのまま書き込みを再開してよい（下の書き込み処理へ合流）。
    else:
        base_name_validated = validate_skill_name(base_name)
        resolved_name = _reserve_and_find_available_name(root, base_name_validated, base=base)

        # 意図の耐久記録（排他作成。"xb" は OS レベルで O_CREAT|O_EXCL 相当の
        # 原子性を持つ。バイナリモード固定で Windows の改行変換を避ける）。
        intent_bytes = json.dumps({"name": resolved_name, "started_at": time.time()}).encode("utf-8")
        try:
            with open(intent_path, "xb") as f:
                f.write(intent_bytes)
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            raise QuarantineError(
                f"concurrent materialize detected for queue_entry_id={queue_entry_id!r}"
            ) from None

    skill_dir = _resolve_within(root, resolved_name)
    skill_file = skill_dir / "SKILL.md"
    _atomic_write_text(skill_file, skill_md_content)

    # 書き込み直後に読み戻して一致を確認する（4.2 手順 7b と同じ規律）。
    verify = skill_file.read_text(encoding="utf-8")
    if _content_sha256(verify) != content_sha256:
        raise QuarantineError(
            "post-write verification failed: on-disk content does not match "
            "the intended SKILL.md content"
        )

    _release_reservation(root, base, resolved_name)
    return _finalize(queue_entry_id, resolved_name, skill_file, content_sha256, base=base)


def _finalize(
    queue_entry_id: str,
    name: str,
    skill_file: Path,
    content_sha256: str,
    *,
    base: Optional[Path],
) -> MaterializeResult:
    record = {
        "name": name,
        "output_path": str(skill_file),
        "content_sha256": content_sha256,
        "materialized": True,
        "materialized_at": time.time(),
    }
    materialized_path = _materialized_dir(base) / f"{queue_entry_id}.json"
    _atomic_write_text(materialized_path, json.dumps(record, ensure_ascii=False))

    intent_path = _materializing_dir(base) / f"{queue_entry_id}.json"
    try:
        intent_path.unlink()
    except FileNotFoundError:
        pass

    return MaterializeResult(
        name=name, output_path=str(skill_file), content_sha256=content_sha256
    )


def list_quarantined_skill_headers(base: Optional[Path] = None) -> List[Tuple[str, str]]:
    """隔離領域にある各スキルの `(name, description)` を列挙する。

    §3.2「収集元は隔離領域と昇格済みの frontmatter name/description のみ」
    の隔離領域側を提供する（昇格済み側は `scripts/hh_skill_promote.py` の
    対象である `~/.hermes/skills/` を直接読む形になるため、この関数の
    範囲外 — 別の探索ロジックが既に `agent/skill_utils.py` に存在する）。

    frontmatter が壊れているエントリは黙ってスキップする（一覧生成が
    1 件の壊れたファイルで全滅しないように。読み取り専用の集計であり、
    フェイルクローズの対象ではない）。
    """
    root = quarantine_root(base)
    if not root.is_dir():
        return []
    out: List[Tuple[str, str]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end == -1:
            continue
        try:
            import yaml

            data = yaml.safe_load(text[3:end])
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        description = data.get("description")
        if isinstance(name, str) and isinstance(description, str):
            out.append((name, description))
    return out


__all__ = [
    "QuarantineError",
    "MaterializeResult",
    "NAME_RE",
    "QUEUE_ENTRY_ID_RE",
    "hh_agent_home",
    "quarantine_root",
    "validate_skill_name",
    "assert_quarantine_root_is_safe",
    "parse_frontmatter_name",
    "materialize",
    "list_quarantined_skill_headers",
]
