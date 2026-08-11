"""scripts/hh_skill_promote.py — `skill promote` の唯一の実装（Phase 1b、安全性クリティカル）。

設計上の位置づけ:
    - 実装契約   docs/hh-agent/07_Phase1b_Spec.md §4.2
    - 担当表     docs/hh-agent/04_Task_Allocation.md Phase 1b 表
      「promote CLI。全文表示・TTY確認必須・非対話は拒否」

**このスクリプトが、Phase 1b における唯一の promote 実装である**（§4.2
冒頭）。`hh skill promote <name>` という統合 CLI コマンド名は親設計書
§4.7 item2 の指定だが、`hh` という統合 CLI 自体がこの段階のコードベースに
存在しないため、`python scripts/hh_skill_promote.py <name>` を暫定の
正式コマンドとする（§4.2 の M-10 注記どおり）。

== 使い方 ==

    python scripts/hh_skill_promote.py <name> [--force]

== 安全性の骨格（§4.2 手順どおり、変更しない） ==

    1. `<name>` を CLI 境界で `^[a-z0-9][a-z0-9-]{1,48}$` 検証。
    2. 隔離領域の `<name>/SKILL.md` を symlink・reparse point・
       ハードリンクを拒否した上で読む（1回だけバイト列として）。
    3. sha256 ダイジェストを計算し、制御文字・ANSI/OSC エスケープを
       リテラル表記に変換した全文とともに表示する。
    4. TTY が無ければ即座にエラー終了（非対話拒否）。確認プロンプトに
       `y` が入力されて初めて次へ進む。
    5. 起動時（`--force` の有無によらず）に、Hermes が実際にスキャンする
       ディレクトリ（既存ルート + `config.yaml` 宣言ルート、未存在も含む）
       とステージング領域が重ならないことを確認する（V3-01 対応）。
    6. ステージング領域へ完全に書き込み、ダイジェスト一致を確認してから
       `~/.hermes/skills/<name>/` へ `os.replace()` で一発配置する。
       `--force` かつ既存の場合は「退避 → 配置」の2手順に分け、両者の間
       でクラッシュしても次回起動時にセルフヒールで回復できるようにする。

== このファイルが独自に決めた設計判断 ==

1. **`distilled_from_session_id` の取得元**: 監査ログ
   （`~/.hh-agent/promote_log.jsonl`）が要求するこのフィールドの由来元は
   §4.2 に明記が無い。隔離済み SKILL.md の frontmatter に
   `distilled_from_session_id` キーがあればそれを使い、無ければ `null` を
   記録する（`services/skill_distiller.py`、MiniMax 所有、は生成する
   SKILL.md の frontmatter にこのキーを含めること — 含めなくても
   promote 自体は失敗せず `null` になるだけなので破壊的ではないが、
   監査の追跡可能性が落ちる）。
2. **クロスファイルシステムの検出**: `~/.hh-agent/` と `~/.hermes/` が
   別ドライブにある場合の最終 `os.replace()` 失敗は、事前に `st_dev` を
   比較して予測するのではなく、実際に `os.replace()` を試みて送出された
   `OSError` を捕捉し、分かりやすいメッセージへ変換する形にした（Windows
   のクロスドライブ判定を自前で作るより、OS 自身の判定を信頼する方が
   確実なため）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modal_hub.services import skill_quarantine  # noqa: E402

_HH_AGENT_HOME_ENV = "USERPROFILE"


class PromoteError(RuntimeError):
    """promote に失敗した。呼び出し元（人間）へそのままメッセージを表示する。

    メッセージにトークン・署名鍵の類は決して含めない（本スクリプトはそもそも
    それらを扱わないため、この制約は自然に満たされる）。
    """


def hh_agent_home() -> Path:
    userprofile = os.environ.get(_HH_AGENT_HOME_ENV)
    base = Path(userprofile) if userprofile else Path.home()
    return base / ".hh-agent"


def _promote_staging_root(base: Optional[Path] = None) -> Path:
    return (base or hh_agent_home()) / "promote_staging"


def _promote_backups_root(base: Optional[Path] = None) -> Path:
    return (base or hh_agent_home()) / "promote_backups"


def _promote_log_path(base: Optional[Path] = None) -> Path:
    return (base or hh_agent_home()) / "promote_log.jsonl"


def _hermes_skills_root() -> Path:
    import hermes_constants

    return hermes_constants.get_skills_dir()


# ---------------------------------------------------------------------------
# 手順1: name の CLI 境界検証
# ---------------------------------------------------------------------------


def validate_name(name: str) -> str:
    if not skill_quarantine.NAME_RE.match(name):
        raise PromoteError(
            f"invalid skill name {name!r}: must match ^[a-z0-9][a-z0-9-]{{1,48}}$"
        )
    return name


# ---------------------------------------------------------------------------
# 手順2〜4: 隔離領域からの読み取り・表示
# ---------------------------------------------------------------------------


def read_quarantined_skill(name: str, *, base: Optional[Path] = None) -> tuple[bytes, str]:
    """`<name>/SKILL.md` を1回だけバイト列として読む。symlink・reparse
    point・ハードリンクを拒否する。

    Returns:
        `(content_bytes, sha256_hex)`。
    """
    root = skill_quarantine.quarantine_root(base)
    expected = root.resolve() / name / "SKILL.md"
    candidate = root / name / "SKILL.md"

    if not candidate.is_file():
        raise PromoteError(f"no quarantined skill found for {name!r} ({candidate})")

    resolved = candidate.resolve()
    if resolved != expected:
        raise PromoteError(
            f"{name!r} resolves outside the expected quarantine path "
            "(symlink or reparse point?), refusing"
        )

    # 2026-08-11 Codex レビュー Medium 指摘の修正: 旧実装は
    # `resolved.stat()`（1回目のパス解決）→ `resolved.read_bytes()`
    # （2回目のパス解決）という別々の呼び出しだったため、その間に
    # ファイルが差し替えられると st_nlink チェックと実際に読む内容が
    # 食い違いうる（TOCTOU）。**1回だけ open して同じファイルディスクリプタ
    # に対して fstat と read の両方を行う**ことで、この2手順間の窓を閉じる
    # （`os.fstat(fd)` はパスを再解決しない — 開いた実体そのものを見る）。
    # `candidate.resolve()` から `open()` までの窓は Windows に
    # `O_NOFOLLOW` 相当が無いため構造的に閉じられず、残存リスクとして
    # 許容する（本ファイル冒頭の docstring と同じ扱い）。
    try:
        with open(resolved, "rb") as f:
            st = os.fstat(f.fileno())
            if getattr(st, "st_nlink", 1) > 1:
                raise PromoteError(f"{name!r}'s SKILL.md has st_nlink > 1 (hard link?), refusing")
            content_bytes = f.read()
    except OSError as exc:
        raise PromoteError(f"failed to open/read quarantined SKILL.md: {type(exc).__name__}") from exc

    digest = hashlib.sha256(content_bytes).hexdigest()
    return content_bytes, digest


def _escape_control_and_ansi(text: str) -> str:
    """制御文字・ESC（ANSI/OSC の起点）をリテラル表記へ変換する。

    改行・タブは表示上そのまま残す。ESC（0x1b）を `\\x1b` へ変換することで、
    以降に続く CSI/OSC シーケンスも端末には制御コードとして解釈されず、
    そのままテキストとして見える（§4.2 手順5「ANSI/OSC エスケープ
    シーケンスをリテラル表記に変換」）。
    """
    out: List[str] = []
    for ch in text:
        cp = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
        elif cp < 0x20 or cp == 0x7F:
            out.append(f"\\x{cp:02x}")
        else:
            out.append(ch)
    return "".join(out)


def display_for_confirmation(name: str, content_bytes: bytes, digest: str) -> None:
    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = content_bytes.decode("utf-8", errors="replace")
    print("=" * 70)
    print(f"SKILL.md for {name!r} (sha256:{digest})")
    print("=" * 70)
    print(_escape_control_and_ansi(text))
    print("=" * 70)


def confirm_or_abort(name: str, digest: str) -> None:
    """§4.2 手順6。非対話（TTY 無し）は即座にエラー終了する。"""
    if not sys.stdin.isatty():
        raise PromoteError(
            "non-interactive execution (no TTY) is not permitted for skill promote"
        )
    prompt = (
        f"Promote '{name}' (sha256:{digest[:12]}..., license: MIT) to "
        f"~/.hermes/skills/{name}/? This may reproduce code/text from your "
        "session; confirm you have the right to license it MIT. [y/N] "
    )
    answer = input(prompt).strip().lower()
    if answer != "y":
        raise PromoteError("promotion cancelled by operator")


# ---------------------------------------------------------------------------
# 手順7a: Hermes の実スキャン対象との重複チェック（V3-01）
# ---------------------------------------------------------------------------


def _existing_hermes_scan_dirs() -> List[Path]:
    import agent.skill_utils as skill_utils

    return list(skill_utils.get_all_skills_dirs())


def _declared_hermes_scan_dirs_including_nonexistent() -> List[Path]:
    """`config.yaml` の `skills.external_dirs` を、Hermes 自身の
    `agent/skill_utils.py:get_external_skills_dirs()` と同じ正規化・解決
    手順で列挙する。**`is_dir()` によるフィルタだけを行わない**点が唯一の
    意図的な差分（§4.2 手順7a チェック2）。
    """
    import hermes_constants

    config_path = hermes_constants.get_config_path()
    if not config_path.is_file():
        return []

    try:
        import yaml

        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — フェイルクローズ（下記 raise）
        raise PromoteError(
            f"failed to read/parse config.yaml (fail-closed): {type(exc).__name__}"
        ) from exc

    if not isinstance(parsed, dict):
        raise PromoteError("config.yaml did not parse to a mapping (fail-closed)")

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


def assert_staging_root_is_safe(*, base: Optional[Path] = None) -> None:
    """ステージング領域が Hermes の実スキャン対象と重ならないことを確認する。

    重なっていれば（一致・配下・祖先いずれも）`PromoteError`（何も書かずに
    中止）。`config.yaml` の読み取り失敗はここまでに `PromoteError` として
    伝播済み（フェイルクローズ）。

    2026-08-11 Codex レビュー Medium 指摘の修正: `_existing_hermes_scan_dirs()`
    （`agent.skill_utils.get_all_skills_dirs()` をそのまま返す）の戻り値は
    必ずしも `.resolve()` 済みとは限らない（`get_skills_dir()` 自体は
    未 resolve）。symlink/junction 経由でローカルスキルルートが物理的には
    ステージングと同一なのに文字列としては異なる、という抜け穴を防ぐため、
    ここで**候補側も**明示的に `.resolve()` してから比較する（既に
    resolve 済みの値に対しては no-op）。
    """
    staging_root = _promote_staging_root(base).resolve()
    raw_candidates = _existing_hermes_scan_dirs() + _declared_hermes_scan_dirs_including_nonexistent()
    candidates = [c.resolve() for c in raw_candidates]
    for candidate in candidates:
        if _same_or_ancestor_or_descendant(staging_root, candidate):
            raise PromoteError(
                f"promote_staging root {staging_root} overlaps a Hermes-scanned "
                f"skills directory ({candidate}); refusing to write anything"
            )


# ---------------------------------------------------------------------------
# セルフヒール（§4.2 手順7e、対象名を問わず起動のたび）
# ---------------------------------------------------------------------------


def self_heal_orphaned_promotions(*, base: Optional[Path] = None) -> List[str]:
    """`退避 → 配置` の間でクラッシュした過去の `--force` 実行を回復する。

    `promote_staging/<name>/` が残っていて `~/.hermes/skills/<name>/` が
    存在せず、かつ `promote_backups/<name>.bak.*/` が存在する場合のみ
    `os.replace(staging, target)` で回復する。`~/.hermes/skills/<name>/`
    が既に存在する場合は触らない（誰かが既に埋めた可能性があり、推測で
    上書きしない）。

    Returns:
        回復した名前のリスト。
    """
    staging_root = _promote_staging_root(base)
    backups_root = _promote_backups_root(base)
    hermes_skills_root = _hermes_skills_root()

    healed: List[str] = []
    if not staging_root.is_dir():
        return healed

    for staging_entry in sorted(staging_root.iterdir()):
        if not staging_entry.is_dir():
            continue
        name = staging_entry.name
        target = hermes_skills_root / name
        if target.exists():
            continue
        if not backups_root.is_dir():
            continue
        has_backup = any(
            p.is_dir() and p.name.startswith(f"{name}.bak.") for p in backups_root.iterdir()
        )
        if not has_backup:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_entry, target)
        healed.append(name)
    return healed


# ---------------------------------------------------------------------------
# 手順7b〜e: ステージング書き込み → 配置
# ---------------------------------------------------------------------------


def _write_staging(name: str, content_bytes: bytes, expected_digest: str, *, base: Optional[Path] = None) -> Path:
    staging_root = _promote_staging_root(base)
    staging_root.mkdir(parents=True, exist_ok=True)
    final_dir = staging_root / name

    tmp_dir = staging_root / f".tmp-{name}-{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True)
    tmp_file = tmp_dir / "SKILL.md"
    with open(tmp_file, "wb") as f:
        f.write(content_bytes)
        f.flush()
        os.fsync(f.fileno())

    if final_dir.exists():
        # 前回の未完了ステージングが残っている。古い内容を信用せず置き換える。
        _rm_dir_tree(final_dir)
    os.replace(tmp_dir, final_dir)

    verify_bytes = (final_dir / "SKILL.md").read_bytes()
    if hashlib.sha256(verify_bytes).hexdigest() != expected_digest:
        raise PromoteError(
            "post-write verification failed: staged SKILL.md does not match "
            "the digest confirmed by the operator"
        )
    return final_dir


def _rm_dir_tree(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file():
            child.unlink()
        else:
            child.rmdir()
    path.rmdir()


def install_staged_skill(name: str, staged_dir: Path, *, force: bool) -> Optional[str]:
    """ステージング済みディレクトリを `~/.hermes/skills/<name>/` へ配置する。

    Returns:
        `--force` で既存を退避した場合はそのバックアップパス文字列、
        新規配置の場合は None。
    """
    hermes_skills_root = _hermes_skills_root()
    hermes_skills_root.mkdir(parents=True, exist_ok=True)
    target = hermes_skills_root / name

    if not target.exists():
        _atomic_replace_or_explain(staged_dir, target)
        return None

    if not force:
        _rm_dir_tree(staged_dir)
        raise PromoteError(f"~/.hermes/skills/{name}/ already exists; pass --force to overwrite")

    backups_root = _promote_backups_root()
    backups_root.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    backup_dir = backups_root / f"{name}.bak.{timestamp}"

    _atomic_replace_or_explain(target, backup_dir)
    _atomic_replace_or_explain(staged_dir, target)
    return str(backup_dir)


def _atomic_replace_or_explain(src: Path, dst: Path) -> None:
    try:
        os.replace(src, dst)
    except OSError as exc:
        raise PromoteError(
            f"os.replace({src} -> {dst}) failed ({type(exc).__name__}: {exc}); "
            "if ~/.hh-agent and ~/.hermes live on different drives/filesystems, "
            "this replace cannot be atomic and promote must be run with both "
            "directories on the same volume"
        ) from exc


# ---------------------------------------------------------------------------
# 手順8: promote_log.jsonl
# ---------------------------------------------------------------------------


def _extract_distilled_from_session_id(content_bytes: bytes) -> Optional[str]:
    text = content_bytes.decode("utf-8", errors="replace")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        import yaml

        data = yaml.safe_load(text[3:end])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("distilled_from_session_id")
    return value if isinstance(value, str) else None


def append_promote_log(
    *,
    name: str,
    digest: str,
    content_bytes: bytes,
    destination: Path,
    forced: bool,
    backup_path: Optional[str],
    base: Optional[Path] = None,
) -> None:
    record = {
        "name": name,
        "promoted_at": time.time(),
        "distilled_from_session_id": _extract_distilled_from_session_id(content_bytes),
        "source_digest": digest,
        "destination": str(destination),
        "forced": forced,
        "backup_path": backup_path,
        "license_confirmed": True,
    }
    log_path = _promote_log_path(base)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def run_promote(name: str, *, force: bool) -> None:
    # 2026-08-11 Codex レビュー Critical 指摘の修正: 旧実装は
    # self_heal_orphaned_promotions() を安全ゲート（scan-root 重複チェック・
    # TTY 確認）より**先に**実行していた。セルフヒールは実際に
    # `~/.hermes/skills/` へファイルを移動しうる操作であり、非対話実行や
    # config.yaml がその後不安全な状態に変わっていた場合でも、拒否される
    # より前にこの書き込みが起きてしまっていた。scan-root の重複チェックは
    # 個々の promote 対象に依存しない構造的な安全性なので、他の何よりも
    # 先に行う。
    assert_staging_root_is_safe()

    healed = self_heal_orphaned_promotions()
    for healed_name in healed:
        print(f"[hh_skill_promote] self-healed orphaned promotion: {healed_name}")

    validated_name = validate_name(name)
    content_bytes, digest = read_quarantined_skill(validated_name)

    display_for_confirmation(validated_name, content_bytes, digest)
    confirm_or_abort(validated_name, digest)

    staged_dir = _write_staging(validated_name, content_bytes, digest)
    backup_path = install_staged_skill(validated_name, staged_dir, force=force)

    destination = _hermes_skills_root() / validated_name
    append_promote_log(
        name=validated_name,
        digest=digest,
        content_bytes=content_bytes,
        destination=destination,
        forced=force,
        backup_path=backup_path,
    )
    print(f"[hh_skill_promote] promoted '{validated_name}' to {destination}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hh_skill_promote")
    parser.add_argument("name")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    try:
        run_promote(args.name, force=args.force)
    except PromoteError as exc:
        print(f"[hh_skill_promote] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
