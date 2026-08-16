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
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modal_hub.services import skill_quarantine  # noqa: E402
from modal_hub.services import skill_sync  # noqa: E402
from modal_hub.services.skill_sync import ORIGIN_INSTANCE_RE  # noqa: E402
from hh_agent_promote_lock import PromoteLockTimeout, promote_lock  # noqa: E402

_HH_AGENT_HOME_ENV = "USERPROFILE"

#: Lane C 接続設定ファイル（`~/.hh-agent/lane_c_config.json`）。
#: `{"base_url": "...", "read_key": "..."}` — read_key は任意で、未指定なら
#: 環境変数 CORPUS2SKILL_API_KEY にフォールバックする（S-06c の読み取り鍵）。
#: 実データは運用時に人間が用意する（`remote_sources.json` と同じ扱い）。
LANE_C_CONFIG_FILENAME = "lane_c_config.json"

#: 署名鍵・書き込み鍵専用の環境ファイル（`.hh-secret.env` とは別。S-06c）。
#: ACL を現ユーザーのみに絞る想定だが、ACL 設定自体はこのタスクのスコープ外。
SIGNING_ENV_FILENAME = ".hh-signing.env"
SIGNING_KEY_VAR = "HH_AGENT_TOKEN_SIGNING_KEY"
WRITE_KEY_VAR = "C2S_SKILL_WRITE_KEY"


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


def _promote_seq_path(base: Optional[Path] = None) -> Path:
    return (base or hh_agent_home()) / "promote_seq.json"


def _promote_receipts_root(base: Optional[Path] = None) -> Path:
    return (base or hh_agent_home()) / "promote_receipts"


def _atomic_write_json(path: Path, data) -> None:
    """原子的な JSON 書き込み（temp + os.replace。`promote_seq.json` 等）。

    途中で落ちても元のファイルが破損した状態で残らないようにする
    （S-06b: 採番の永続化は原子的であること）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """原子的なテキスト書き込み（temp + os.replace。`current` ポインタ用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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


def install_confirmed_skill(name: str, content_bytes: bytes, digest: str, *, force: bool, provenance: str) -> dict:
    """確認済みの内容をインストールする（run_promote / sync_pull / run_remote_promote 共通）。

    既存の `_write_staging()` → `install_staged_skill()` の2段をそのまま呼ぶ
    だけ。順序・内容・例外は一切変えない。`append_promote_log()` はここに
    含めない（呼び出し元が provenance / promoted_at_ms を渡して個別に呼ぶ）。

    Args:
        provenance: 呼び出し元の来歴ラベル（`"local-promote"` /
            `"remote-promote:<origin>"` 等）。この関数の内部では使わない
            （呼び出し元が `append_promote_log()` に渡すための引数）。
        force: `install_staged_skill()` にそのまま渡す（既存の退避→配置）。

    Returns:
        `{"backup_path": ..., "destination": <Path>}`。run_promote 等が
        後続処理（監査ログ・表示）に使う。
    """
    staged_dir = _write_staging(name, content_bytes, digest)
    backup_path = install_staged_skill(name, staged_dir, force=force)
    return {
        "backup_path": backup_path,
        "destination": _hermes_skills_root() / name,
    }


def recheck_quarantined_digest(name: str, expected_digest: str, *, base: Optional[Path] = None) -> None:
    """ロック取得後、隔離領域の SKILL.md が確認時と差し替わっていないか再検査する。

    S-10 疑似コード手順8。確認（TTY）と書き込みの間に環境が変わっていない
    ことを保証する。差し替わっていれば `PromoteError`（何も書かずに中止）。
    """
    _, current_digest = read_quarantined_skill(name, base=base)
    if current_digest != expected_digest:
        raise PromoteError(
            f"quarantined SKILL.md for {name!r} changed after confirmation "
            "(digest mismatch); aborting without writing anything"
        )


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
    provenance: str,
    promoted_at_ms: int,
    base: Optional[Path] = None,
) -> dict:
    """promote_log.jsonl に1行追記し、書いた record dict を返す。

    S-08: `promoted_at_ms` は呼び出し元が 1 回だけ生成した値を受け取る
    （この関数は生成しない）。receipt・push ペイロード・監査ログの三者で
    同一の値を使うため。既存の `promoted_at`（float 秒）フィールドは
    後方互換のため残す。`provenance` は新設（`"local-promote"` /
    `"sync-pull:<origin>"` / `"remote-promote:<origin>"` 等）。
    """
    record = {
        "name": name,
        "promoted_at": promoted_at_ms / 1000.0,
        "promoted_at_ms": promoted_at_ms,
        "provenance": provenance,
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
    return record


# ---------------------------------------------------------------------------
# Lane C: 鍵・設定・採番・receipt・push（S-06b / S-08 / S-08b）
# ---------------------------------------------------------------------------


def _load_signing_env() -> dict[str, str]:
    """`.hh-signing.env`（REPO_ROOT 直下）から `HH_AGENT_TOKEN_SIGNING_KEY` と
    `C2S_SKILL_WRITE_KEY` を読む。無ければ空 dict（呼び出し側がフェイルオープン/
    フェイルクローズを判断する）。

    `hh_issue_agent_token.py` の `_load_signing_key()` と同じパース方式
    （KEY=VALUE 行形式・コメント行スキップ・両端の引用符を剥がす）を踏襲する
    が、**既存の `.hh-secret.env` ローダーとは混在させない**（別ファイル・
    別関数。S-06c: 署名鍵・書き込み鍵の専用ファイルであり、一般の secret
    と同居させない）。環境変数が設定されていればファイルより優先する。
    """
    env_path = _REPO_ROOT / SIGNING_ENV_FILENAME

    def _read(name: str) -> Optional[str]:
        env_value = os.environ.get(name)
        if env_value is not None:
            return env_value.strip().strip('"').strip("'") or None
        if not env_path.is_file():
            return None
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                stripped = value.strip().strip('"').strip("'")
                return stripped or None
        return None

    out: dict[str, str] = {}
    signing = _read(SIGNING_KEY_VAR)
    write_key = _read(WRITE_KEY_VAR)
    if signing is not None:
        out[SIGNING_KEY_VAR] = signing
    if write_key is not None:
        out[WRITE_KEY_VAR] = write_key
    return out


def _instance_id() -> str:
    """`~/.hh-agent/instance_id.json`（{"instance_id": "..."}）。無ければ
    `<platform>-<uuid4 hex 8桁>` で生成して保存する。環境変数
    `HH_AGENT_INSTANCE_ID` があれば優先。ホスト名は使わない（S-08:
    Modal コンテナのホスト名は起動ごとに変わり、個人環境名を外部サービスへ
    残したくないため）。"""
    env_value = os.environ.get("HH_AGENT_INSTANCE_ID")
    if env_value:
        return env_value.strip()
    path = hh_agent_home() / "instance_id.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            value = data.get("instance_id")
            if isinstance(value, str) and value:
                return value
    value = f"{sys.platform}-{uuid.uuid4().hex[:8]}"
    _atomic_write_json(path, {"instance_id": value})
    return value


def _is_seq_counter(value) -> bool:
    """promotion_seq カウンタの型・範囲検査（非負 int。bool は拒否）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def allocate_promotion_seq(name: str, *, origin_instance: str, base: Optional[Path] = None) -> int:
    """`~/.hh-agent/promote_seq.json`（スキーマ `{name: {origin_instance: seq}}`）から
    次の seq（0始まり、呼ぶたびに+1して返す）を採番して永続化する。

    **呼び出し前に promote_lock を保持していること**（このモジュール自体は
    ロックを取らない。`run_promote()` / `run_remote_promote()` のどちらから
    呼ばれてもロック区間の内側でしか実行されない — S-08 / S-08b 疑似コード）。

    旧スキーマ（`{name: int}`）を検出したら、その値を自分自身の
    `origin_instance` 配下へ 1 回だけ書き直してから続行する（移行）。
    移行は必ずこの関数の内部・読み取り直後に行う（**ロック外で移行を
    先読みする実装を書かない**）。原子的なファイル書き込み
    （temp+os.replace）を使う。

    採番してから永続化する順序は逆にしない（receipt を書く前に永続化する。
    採番だけして落ちても seq が飛ぶだけで安全側。逆順にすると同じ seq を
    2 回使う — S-06b）。欠損・不正値は自動で振り直さず `PromoteError`
    （フェイルクローズ。`--repair-seq` を促す）。
    """
    path = _promote_seq_path(base)
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PromoteError(
                f"promote_seq.json を読み込めない（{type(exc).__name__}）。"
                "自動では振り直さない。--repair-seq を実行して修復すること"
            ) from exc
        if isinstance(loaded, dict):
            data = loaded

    entry = data.get(name)
    if isinstance(entry, int) and not isinstance(entry, bool):
        # 旧スキーマ（{name: int}）からの移行。値を origin 配下へ移し、
        # 続けて採番までを**1 回の原子的書き込み**で行う（この関数の内部・
        # 読み取り直後にのみ発生。ロック外で先読み・先書きしない）。
        entry = {origin_instance: entry}
        data[name] = entry
    elif not isinstance(entry, dict):
        entry = {}
        data[name] = entry
    current = entry.get(origin_instance, 0)
    if not _is_seq_counter(current):
        raise PromoteError(
            f"promote_seq.json の {name!r}/{origin_instance!r} の値が不正（{current!r}）。"
            "自動では振り直さない。--repair-seq を実行して修復すること"
        )
    entry[origin_instance] = current + 1
    _atomic_write_json(path, data)
    return current


def resolve_seq_from_watermark(
    name: str, origin_instance: str, watermark: Optional[int], *, base: Optional[Path] = None
) -> int:
    """`--repair-seq` 用。`origin_seq_watermarks` の watermark を読み、
    watermark + 1 から `promote_seq.json` を上書きする（S-06b）。

    呼び出し側が promote_lock を保持していること（`--repair-seq` は
    promote_lock を writer として取得してから呼ぶ）。該当 name が
    `origin_seq_watermarks` に存在しない場合（一度も push したことがない）
    は 0 から開始する。戻り値: 設定した次の seq。
    """
    if watermark is None:
        next_seq = 0
    elif _is_seq_counter(watermark):
        next_seq = watermark + 1
    else:
        raise PromoteError(f"origin_seq_watermarks の値が不正: {watermark!r}")
    path = _promote_seq_path(base)
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PromoteError(f"promote_seq.json を読み込めない: {type(exc).__name__}") from exc
        if isinstance(loaded, dict):
            data = loaded
    entry = data.get(name)
    if not isinstance(entry, dict):
        entry = {}
        data[name] = entry
    entry[origin_instance] = next_seq
    _atomic_write_json(path, data)
    return next_seq


def write_receipt(
    name: str,
    content_bytes: bytes,
    digest: str,
    seq: int,
    promoted_at_ms: int,
    *,
    origin_instance: str,
    distilled_from_session_id: Optional[str] = None,
    base: Optional[Path] = None,
) -> str:
    """promote receipt を生成して保存する（S-06b）。

    `skill_sync.write_receipt()` をそのまま呼ぶ薄いラッパー。署名鍵・key_id
    は `.hh-signing.env`（または環境変数）から取得する。鍵が無ければ
    `PromoteError`（フェイルクローズ — receipt は promote フローの一部であり、
    push を送るかどうかとは別の話）。

    - 署名対象のタプルは `skill_sync.write_receipt()` と同じ。
      `distilled_from_session_id` 未指定時は本文 frontmatter から抽出する。
    - 保存: `~/.hh-agent/promote_receipts/<name>/<content_sha8>-<receipt_sha8>.json`
      （版管理形式。本文のダイジェストと receipt 自体のダイジェストの両方を
      ファイル名に含む）を原子的に書き、`current` を差し替える
      （reconcile push は current を見る）。
    - 戻り値: receipt 文字列（push ペイロードにそのまま使う）。
    """
    if distilled_from_session_id is None:
        distilled_from_session_id = _extract_distilled_from_session_id(content_bytes)
    env = _load_signing_env()
    signing_key_raw = env.get(SIGNING_KEY_VAR)
    if not signing_key_raw:
        raise PromoteError(
            f"{SIGNING_KEY_VAR} が無い（.hh-signing.env または環境変数）。"
            "receipt を生成できない"
        )
    signing_key = signing_key_raw.encode("utf-8")
    key_id = skill_sync.derive_key_id(signing_key)
    receipt = skill_sync.write_receipt(
        name=name,
        content_bytes_or_sha256=content_bytes,
        digest=digest,
        seq=seq,
        promoted_at_ms=promoted_at_ms,
        origin_instance=origin_instance,
        distilled_from_session_id=distilled_from_session_id,
        signing_key=signing_key,
        key_id=key_id,
    )
    filename = (
        f"{digest[:8]}-{hashlib.sha256(receipt.encode('utf-8')).hexdigest()[:8]}.json"
    )
    record = {
        "name": name,
        "content_sha256": digest,
        "origin_instance": origin_instance,
        "promoted_at_ms": promoted_at_ms,
        "promotion_seq": seq,
        "distilled_from_session_id": distilled_from_session_id,
        "key_id": key_id,
        "receipt": receipt,
    }
    name_dir = _promote_receipts_root(base) / name
    _atomic_write_json(name_dir / filename, record)
    _atomic_write_text(name_dir / "current", filename)
    return receipt


def _read_current_receipt_record(name: str, *, base: Optional[Path] = None) -> Optional[dict]:
    """`promote_receipts/<name>/current` が指す record を読む。無ければ None。"""
    name_dir = _promote_receipts_root(base) / name
    current = name_dir / "current"
    try:
        filename = current.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not filename:
        return None
    try:
        data = json.loads((name_dir / filename).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class LaneCConfig:
    """`lane_c_config.json` の内容（Lane C 接続設定）。"""

    base_url: str
    read_key: Optional[str] = None


def load_lane_c_config(*, base: Optional[Path] = None) -> Optional[LaneCConfig]:
    """`~/.hh-agent/lane_c_config.json` から Lane C 接続設定を読む。

    形式: `{"base_url": "...", "read_key": "..."}`。read_key は任意で、
    未指定なら環境変数 `CORPUS2SKILL_API_KEY` にフォールバックする
    （S-06c の読み取り鍵）。ファイルが無ければ None（呼び出し側が
    フェイルオープン / フェイルクローズを判断する）。壊れた JSON・
    フィールド欠落は `PromoteError`（フェイルクローズ）。

    実データは運用時に人間が用意する設定ファイルであり、このタスクでは
    読み込みロジックと型定義だけを用意する（`remote_sources.json` と同じ扱い）。
    """
    path = (base or hh_agent_home()) / LANE_C_CONFIG_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromoteError(f"lane_c_config.json を読み込めない: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise PromoteError("lane_c_config.json が JSON オブジェクトでない")
    base_url = data.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        raise PromoteError("lane_c_config.json に base_url が無い")
    read_key = data.get("read_key")
    if not isinstance(read_key, str) or not read_key:
        read_key = os.environ.get("CORPUS2SKILL_API_KEY")
    return LaneCConfig(base_url=base_url, read_key=read_key)


def push_to_lane_c(
    *,
    name: str,
    digest: str,
    promoted_at_ms: int,
    receipt: str,
    origin_instance: str,
    distilled_from_session_id: Optional[str],
    content_bytes: bytes,
    base_revision: int = 0,
) -> None:
    """Lane C へ push する薄いラッパー（S-08）。

    `skill_sync.push_skill()` を呼ぶ。**失敗しても例外を外へ伝播させず**
    stderr へ 1 行警告するだけ（フェイルオープン。呼び出し元の
    `run_promote()` の終了コードに影響させない。取りこぼしは S-10 の
    reconcile が拾う）。base_url / read 周りの設定は Lane C 設定
    （`lane_c_config.json`）から取得する。書き込み鍵は `.hh-signing.env` の
    `C2S_SKILL_WRITE_KEY`。設定が無い・上限超過・redact 差分・通信失敗は
    いずれも警告のみで promote 自体は成功のまま。

    `promotion_seq` は引数で受け取らない（S-08b の呼び出し方に合わせた）。
    直前に `write_receipt()` が保存した `promote_receipts/<name>/current` の
    record から取る（署名対象と同じ値が入っている）。
    """
    try:
        cfg = load_lane_c_config()
        if cfg is None:
            print(
                "[hh_skill_promote] WARN: Lane C 設定（lane_c_config.json）が無いため "
                "push をスキップ（promote 自体は成功）",
                file=sys.stderr,
            )
            return
        write_key = _load_signing_env().get(WRITE_KEY_VAR)
        if not write_key:
            print(
                f"[hh_skill_promote] WARN: {WRITE_KEY_VAR} が無い（.hh-signing.env）ため "
                "push をスキップ（promote 自体は成功）",
                file=sys.stderr,
            )
            return
        try:
            skill_md = content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            print(
                f"[hh_skill_promote] WARN: 本文が UTF-8 として decode できないため "
                f"push をスキップ（{type(exc).__name__}。promote 自体は成功）",
                file=sys.stderr,
            )
            return
        record = _read_current_receipt_record(name)
        promotion_seq = record.get("promotion_seq") if record else None
        if not _is_seq_counter(promotion_seq):
            print(
                f"[hh_skill_promote] WARN: promote_receipts/{name}/current から "
                "promotion_seq を取得できないため push をスキップ（promote 自体は成功）",
                file=sys.stderr,
            )
            return
        result = skill_sync.push_skill(
            name=name,
            skill_md=skill_md,
            content_sha256=digest,
            promoted_at_ms=promoted_at_ms,
            origin_instance=origin_instance,
            distilled_from_session_id=distilled_from_session_id,
            promotion_seq=promotion_seq,
            receipt=receipt,
            base_revision=base_revision,
            base_url=cfg.base_url,
            write_key=write_key,
        )
        if result.sent:
            print(
                f"[hh_skill_promote] pushed '{name}' to Lane C "
                f"(revision={result.revision}, seq={promotion_seq})"
            )
        else:
            reason = result.reason or ("conflict" if result.conflict else "rejected")
            print(
                f"[hh_skill_promote] WARN: Lane C push を送らなかった/受理されなかった: "
                f"{reason}（promote 自体は成功）",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — S-08 フェイルオープン
        print(
            f"[hh_skill_promote] WARN: Lane C push に失敗した（promote 自体は成功）: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


@dataclass(frozen=True)
class RemoteSourceConfig:
    """`remote_sources.json` の 1 エントリ（S-08b）。

    - `hub_base_url`: Hub（modal_hub）のベース URL。
    - `quarantine_read_token_path`: `quarantine_read_token.json`
      （scopes=["quarantine_read"]、`{"token": "..."}`）のパス。
    - `origin_instance`: **署名対象の固定 origin_instance**。接続先（対象
      Modal インスタンス）の `instance_id.json` の値を人間が初回セットアップ
      時に一度だけ確認して書き込む。
    """

    hub_base_url: str
    quarantine_read_token_path: Path
    origin_instance: str


def load_remote_source_config(source: str, *, base: Optional[Path] = None) -> RemoteSourceConfig:
    """`~/.hh-agent/remote_sources.json` から 1 エントリを取り出す（S-08b）。

    形式: `{"modal-dashboard": {"hub_base_url": "...",
    "quarantine_read_token_path": "...", "origin_instance": "..."}}`。
    ファイルが無い / source が無い / フィールド不正は `PromoteError`。

    署名する `origin_instance` はこの固定値を使い、エンドポイント応答の
    自己申告値は表示専用として扱う（S-08b: 応答値をそのまま署名対象に
    転記する実装を書かない）。実データは運用時に人間が用意する設定ファイル
    であり、このタスクでは読み込みロジックと型定義だけを用意する。
    """
    path = (base or hh_agent_home()) / "remote_sources.json"
    if not path.is_file():
        raise PromoteError(f"remote_sources.json が無い（{path}）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromoteError(f"remote_sources.json を読み込めない: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise PromoteError("remote_sources.json が JSON オブジェクトでない")
    entry = data.get(source)
    if not isinstance(entry, dict):
        raise PromoteError(f"remote_sources.json に source {source!r} が無い")
    hub_base_url = entry.get("hub_base_url")
    token_path = entry.get("quarantine_read_token_path")
    origin_instance = entry.get("origin_instance")
    if not isinstance(hub_base_url, str) or not hub_base_url:
        raise PromoteError(f"remote source {source!r} の hub_base_url が不正")
    if not isinstance(token_path, str) or not token_path:
        raise PromoteError(f"remote source {source!r} の quarantine_read_token_path が不正")
    if not isinstance(origin_instance, str) or not ORIGIN_INSTANCE_RE.match(origin_instance):
        raise PromoteError(f"remote source {source!r} の origin_instance が形式に一致しない")
    return RemoteSourceConfig(
        hub_base_url=hub_base_url,
        quarantine_read_token_path=Path(token_path),
        origin_instance=origin_instance,
    )


def _read_quarantine_read_token(path: Path) -> str:
    """`quarantine_read_token.json`（`{"token": "..."}`）を読む（S-08b）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromoteError(
            f"quarantine_read_token.json を読めない（{path}）: {type(exc).__name__}"
        ) from exc
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise PromoteError(f"quarantine_read_token.json（{path}）に token が無い")
    return token


def fetch_quarantine_list(cfg: RemoteSourceConfig, *, timeout: float = 10.0) -> dict:
    """Hub の `GET /api/skills/quarantine` を叩く（S-08b）。

    レスポンス: `{"skills": [{"name", "content", "content_sha256",
    "origin_instance", "distilled_from_session_id", "published_at"}]}`。
    timeout は必ず設定する。読み取り専用（いかなる書き込みも行わない）。
    `skill_sync.py` は Corpus2Skill の `/api/skills/*` 用であり、Hub の
    `quarantine` は別サーバー（modal_hub）のためここに置く。
    """
    token = _read_quarantine_read_token(cfg.quarantine_read_token_path)
    url = f"{cfg.hub_base_url.rstrip('/')}/api/skills/quarantine"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise PromoteError(f"GET /api/skills/quarantine が HTTP {exc.code} を返した") from exc
    except (OSError, TimeoutError) as exc:
        raise PromoteError(
            f"GET /api/skills/quarantine に失敗した: {type(exc).__name__}"
        ) from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromoteError("GET /api/skills/quarantine が非 JSON 応答を返した") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("skills"), list):
        raise PromoteError("GET /api/skills/quarantine の応答形式が不正")
    return parsed


def run_remote_promote(source: str, target_name: Optional[str] = None) -> None:
    """`--remote <source>` モード（S-08b 疑似コード 1110〜1137 行目）。

    Windows から Hub の quarantine を読み取り専用 API 経由で読み、既存の
    Windows ローカル TTY 確認・署名フローをそのまま再利用して promote →
    Lane C push する（Modal 側で人間が対話的に promote することはない）。

    - 署名する `origin_instance` は接続先設定（`remote_sources.json`）の
      固定値。quarantine 応答の自己申告 `origin_instance` は表示・監査用のみ
      で、接続先設定と食い違う場合は警告して中断する（署名・push しない）。
    - 応答の `content` は無検証で信用しない（受信直後に sha256 を再検証）。
    - `--yes` / `--non-interactive` / `--no-confirm` のようなフラグは
      **絶対に追加しない**（`confirm_or_abort()` をバイパスする経路を作らない。
      設計上の確定事項）。
    """
    cfg = load_remote_source_config(source)
    resp = fetch_quarantine_list(cfg)
    for entry in resp.get("skills") or []:
        if not isinstance(entry, dict):
            continue
        entry_name = entry.get("name")
        if not isinstance(entry_name, str):
            continue
        if target_name is not None and entry_name != target_name:
            continue
        content = entry.get("content")
        if not isinstance(content, str):
            raise PromoteError(f"quarantine エントリ {entry_name!r} の content が文字列でない")
        content_bytes = content.encode("utf-8")
        digest = hashlib.sha256(content_bytes).hexdigest()
        content_sha256 = entry.get("content_sha256")
        if not isinstance(content_sha256, str) or digest != content_sha256:
            raise PromoteError(
                f"quarantine エントリ {entry_name!r} の sha256 が content_sha256 と一致しない"
                "（応答を無検証で信用しない）"
            )
        if entry.get("origin_instance") not in (None, cfg.origin_instance):
            raise PromoteError(
                f"quarantine の自己申告 origin_instance が接続先設定と食い違う: "
                f"{entry_name!r}（申告 {entry.get('origin_instance')!r} != "
                f"設定 {cfg.origin_instance!r}）。設定ミスまたは詐称の疑いがあるため"
                "署名・push を行わず中断する"
            )
        display_for_confirmation(entry_name, content_bytes, digest)
        confirm_or_abort(entry_name, digest)

        distilled_from_session_id = entry.get("distilled_from_session_id")
        if not isinstance(distilled_from_session_id, str):
            distilled_from_session_id = None  # 型不正は null（監査・表示専用）
        with promote_lock(timeout=60):
            assert_staging_root_is_safe()
            self_heal_orphaned_promotions()
            install_result = install_confirmed_skill(
                entry_name, content_bytes, digest,
                force=False, provenance=f"remote-promote:{cfg.origin_instance}",
            )
            promoted_at_ms = int(time.time() * 1000)
            seq = allocate_promotion_seq(entry_name, origin_instance=cfg.origin_instance)
            receipt = write_receipt(
                entry_name, content_bytes, digest, seq, promoted_at_ms,
                origin_instance=cfg.origin_instance,
                distilled_from_session_id=distilled_from_session_id,
            )
            append_promote_log(
                name=entry_name,
                digest=digest,
                content_bytes=content_bytes,
                destination=install_result["destination"],
                forced=False,
                backup_path=install_result["backup_path"],
                provenance=f"remote-promote:{cfg.origin_instance}",
                promoted_at_ms=promoted_at_ms,
            )
        # ロック外。失敗しても promote は成功のまま（S-08 フェイルオープン）。
        push_to_lane_c(
            name=entry_name,
            digest=digest,
            promoted_at_ms=promoted_at_ms,
            receipt=receipt,
            origin_instance=cfg.origin_instance,
            distilled_from_session_id=distilled_from_session_id,
            content_bytes=content_bytes,
        )
        print(
            f"[hh_skill_promote] remote-promoted '{entry_name}' (source={source!r}) "
            f"to {install_result['destination']}"
        )


def repair_seq_for(*, origin: str, base: Optional[Path] = None) -> int:
    """`--repair-seq [--origin <instance_id>]`（S-06b）。

    Lane C の `GET /api/skills/list`（`skill_sync.list_all_skills()`）が返す
    `origin_seq_watermarks` を各 name について読み、その値 + 1 から
    `promote_seq.json` を書き直す。`--origin` 省略時は自分自身の
    instance_id。該当 name が `origin_seq_watermarks` に存在しない場合
    （一度も push したことがない）は 0 から開始する。

    HTTP の取得はロックの外で行い、書き込みは promote_lock を writer として
    取得してから行う（ロックを HTTP 通信中に占有しない — S-10 手順0）。
    戻り値: 再設定した name 数。
    """
    cfg = load_lane_c_config(base=base)
    if cfg is None:
        raise PromoteError(
            "Lane C 設定（lane_c_config.json）が無いため --repair-seq を実行できない"
        )
    if not cfg.read_key:
        raise PromoteError(
            "Lane C の読み取り鍵が設定されていない"
            "（lane_c_config.json の read_key または環境変数 CORPUS2SKILL_API_KEY）"
        )
    listing = skill_sync.list_all_skills(base_url=cfg.base_url, read_key=cfg.read_key)
    targets: List[tuple] = []
    for entry in listing.get("skills") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        watermarks = entry.get("origin_seq_watermarks")
        watermark = None
        if isinstance(watermarks, dict):
            watermark = watermarks.get(origin)
        targets.append((name, watermark))
    with promote_lock(timeout=60, base=base):
        for name, watermark in targets:
            resolve_seq_from_watermark(name, origin, watermark, base=base)
    print(
        f"[hh_skill_promote] --repair-seq: {len(targets)} 件の seq を "
        f"origin {origin!r} について再設定した"
    )
    return len(targets)


class ResignRefused(PromoteError):
    """`--resign` がこの name の再署名を拒否した（安全契約・S-06b）。"""


def resign_receipts(*, base: Optional[Path] = None) -> int:
    """`--resign`: 全 receipt を新鍵で署名し直す（S-06b「`--resign` の安全契約」）。

    - 再署名してよいのは、**旧鍵（`.hh-signing.env` の
      `HH_AGENT_TOKEN_SIGNING_KEY`）で検証に成功する既存 receipt が存在し、
      かつその receipt が署名しているタプル（name・content_sha256・
      origin_instance・promoted_at_ms・promotion_seq・
      distilled_from_session_id）が再署名対象の内容と完全に一致する**場合
      だけ。署名対象のタプルは一切変更しない（key_id だけが変わる）。
    - 新鍵は環境変数 `HH_AGENT_TOKEN_SIGNING_KEY_NEW` で渡す
      （argv に鍵を出さない。ローテーション時は、新鍵をこの変数で渡して
      実行した後、`.hh-signing.env` の中身を新鍵へ差し替える運用）。
      旧鍵と同一（key_id 一致）なら拒否する。
    - 旧 receipt が存在しない／検証に失敗する／内容が一致しない対象は
      **必ず拒否**し、その name を一覧で人間に示す（人間が再 promote する
      しかない）。拒否が 1 件でもあれば `PromoteError`（失敗終了）。
    - `--resign` は TTY を要求しない（バッチ処理）が、上記の制約により
      「既にある確認済みの事実を別の鍵で言い直す」以上のことはできない。
    - `promote_receipts/` は共有資源のため promote_lock を保持して実行する。

    戻り値: 再署名した receipt 数。
    """
    env = _load_signing_env()
    old_key_raw = env.get(SIGNING_KEY_VAR)
    new_key_raw = os.environ.get("HH_AGENT_TOKEN_SIGNING_KEY_NEW")
    if not old_key_raw:
        raise PromoteError(
            f"--resign には旧鍵が必要: {SIGNING_KEY_VAR} が .hh-signing.env に無い"
        )
    if not new_key_raw:
        raise PromoteError(
            "--resign には新鍵が必要: 環境変数 HH_AGENT_TOKEN_SIGNING_KEY_NEW を設定せよ"
        )
    old_key = old_key_raw.encode("utf-8")
    new_key = new_key_raw.encode("utf-8")
    old_key_id = skill_sync.derive_key_id(old_key)
    new_key_id = skill_sync.derive_key_id(new_key)
    if old_key_id == new_key_id:
        raise PromoteError(
            "--resign: 新旧の鍵が同一（key_id が一致）。ローテーションには別の鍵を指定せよ"
        )

    receipts_root = _promote_receipts_root(base)
    refused: List[str] = []
    resigned = 0
    with promote_lock(timeout=60, base=base):
        if receipts_root.is_dir():
            for name_dir in sorted(receipts_root.iterdir()):
                if not name_dir.is_dir():
                    continue
                try:
                    resigned += _resign_one_name(
                        name_dir, old_key, old_key_id, new_key, new_key_id
                    )
                except ResignRefused as exc:
                    refused.append(f"{name_dir.name}: {exc}")
    if refused:
        raise PromoteError(
            "--resign が拒否した対象（人間が再 promote する必要がある）:\n  "
            + "\n  ".join(refused)
        )
    print(f"[hh_skill_promote] --resign: {resigned} 件の receipt を新鍵で再署名した")
    return resigned


def _resign_one_name(
    name_dir: Path, old_key: bytes, old_key_id: str, new_key: bytes, new_key_id: str
) -> int:
    """1 name 分の receipt をすべて再署名する。

    安全契約（S-06b）を 1 件でも満たさない（旧 receipt 不在・検証失敗・
    内容不一致）場合は `ResignRefused`（その name を拒否して一覧へ載せる）。
    """
    receipt_files = sorted(p for p in name_dir.glob("*.json") if p.is_file())
    if not receipt_files:
        raise ResignRefused("旧 receipt が存在しない")
    count = 0
    for path in receipt_files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResignRefused(
                f"receipt ファイルを読めない: {path.name}（{type(exc).__name__}）"
            ) from exc
        if not isinstance(record, dict):
            raise ResignRefused(f"receipt ファイルの形式が不正: {path.name}")
        name = record.get("name")
        content_sha256 = record.get("content_sha256")
        origin_instance = record.get("origin_instance")
        promoted_at_ms = record.get("promoted_at_ms")
        promotion_seq = record.get("promotion_seq")
        distilled = record.get("distilled_from_session_id")
        receipt = record.get("receipt")
        key_id = record.get("key_id")
        if (
            not isinstance(name, str)
            or name != name_dir.name
            or not isinstance(content_sha256, str)
            or len(content_sha256) != 64
            or not isinstance(origin_instance, str)
            or not isinstance(promoted_at_ms, int)
            or isinstance(promoted_at_ms, bool)
            or not isinstance(promotion_seq, int)
            or isinstance(promotion_seq, bool)
            or (distilled is not None and not isinstance(distilled, str))
            or not isinstance(receipt, str)
            or not isinstance(key_id, str)
        ):
            raise ResignRefused(f"receipt のフィールドが不正: {path.name}")
        # ファイル名（<content_sha8>-<receipt_sha8>.json）と内容の整合。
        # 本文ダイジェストだけでなく receipt 自体のダイジェストも含める
        # （S-06b の版管理形式）。
        expected_name = (
            f"{content_sha256[:8]}-"
            f"{hashlib.sha256(receipt.encode('utf-8')).hexdigest()[:8]}.json"
        )
        if path.name != expected_name:
            raise ResignRefused(f"ファイル名が署名内容と一致しない: {path.name}（改変の疑い）")
        # 旧鍵で検証できること（最重要）。検証できない receipt は再署名しない。
        if not skill_sync.verify_receipt(
            receipt,
            name,
            content_sha256,
            origin_instance,
            promoted_at_ms,
            promotion_seq,
            distilled,
            verify_keys={old_key_id: old_key},
        ):
            raise ResignRefused(
                f"旧鍵で検証できない: {path.name}（旧鍵が違う、または receipt が改変されている）"
            )
        # 署名対象のタプルは一切変更しない（key_id だけが変わる）。
        new_receipt = skill_sync.write_receipt(
            name=name,
            content_bytes_or_sha256=content_sha256,
            digest=content_sha256,
            seq=promotion_seq,
            promoted_at_ms=promoted_at_ms,
            origin_instance=origin_instance,
            distilled_from_session_id=distilled,
            signing_key=new_key,
            key_id=new_key_id,
        )
        new_record = dict(record)
        new_record["key_id"] = new_key_id
        new_record["receipt"] = new_receipt
        new_filename = (
            f"{content_sha256[:8]}-"
            f"{hashlib.sha256(new_receipt.encode('utf-8')).hexdigest()[:8]}.json"
        )
        _atomic_write_json(name_dir / new_filename, new_record)
        # current がこのファイルを指していたら新ファイルへ差し替える。
        current_path = name_dir / "current"
        try:
            current_name = current_path.read_text(encoding="utf-8").strip()
        except OSError:
            current_name = ""
        if current_name == path.name:
            _atomic_write_text(current_path, new_filename)
        count += 1
    return count


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
    #
    # 呼び出し順は S-10 疑似コード（設計書 1272〜1294 行目）に厳密に従う:
    # assert → self-heal → validate → read（1 回だけ）→ display → confirm
    # （ここまではロックを取らない）→ ロック → 再 assert → 再 self-heal →
    # ダイジェスト再検査 → install → promoted_at_ms（1 回だけ生成）→ seq →
    # receipt → log → ロック解放 → push（フェイルオープン）。
    assert_staging_root_is_safe()

    healed = self_heal_orphaned_promotions()
    for healed_name in healed:
        print(f"[hh_skill_promote] self-healed orphaned promotion: {healed_name}")

    validated_name = validate_name(name)
    content_bytes, digest = read_quarantined_skill(validated_name)

    display_for_confirmation(validated_name, content_bytes, digest)
    confirm_or_abort(validated_name, digest)

    with promote_lock(timeout=60):
        # 確認中に環境が変わっていないことを再検査する（S-10 手順0）。
        assert_staging_root_is_safe()
        self_heal_orphaned_promotions()  # べき等。確認中に他プロセスが残した孤児も回収
        recheck_quarantined_digest(validated_name, digest)

        install_result = install_confirmed_skill(
            validated_name, content_bytes, digest,
            force=force, provenance="local-promote",
        )
        # 3 箇所（receipt・promote_log・push）で使う値を 1 回だけ生成する（S-08）。
        promoted_at_ms = int(time.time() * 1000)
        self_instance_id = _instance_id()
        distilled_from_session_id = _extract_distilled_from_session_id(content_bytes)
        seq = allocate_promotion_seq(validated_name, origin_instance=self_instance_id)
        receipt = write_receipt(
            validated_name, content_bytes, digest, seq, promoted_at_ms,
            origin_instance=self_instance_id,
            distilled_from_session_id=distilled_from_session_id,
        )
        append_promote_log(
            name=validated_name,
            digest=digest,
            content_bytes=content_bytes,
            destination=install_result["destination"],
            forced=force,
            backup_path=install_result["backup_path"],
            provenance="local-promote",
            promoted_at_ms=promoted_at_ms,
        )
    # ロック外。失敗しても promote は成功のまま（S-08 フェイルオープン。
    # 取りこぼしは S-10 の reconcile が拾う）。
    push_to_lane_c(
        name=validated_name,
        digest=digest,
        promoted_at_ms=promoted_at_ms,
        receipt=receipt,
        origin_instance=self_instance_id,
        distilled_from_session_id=distilled_from_session_id,
        content_bytes=content_bytes,
    )
    print(f"[hh_skill_promote] promoted '{validated_name}' to {install_result['destination']}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hh_skill_promote")
    parser.add_argument("name", nargs="?", help="対象スキル名（--remote では対象を絞る任意フィルタ）")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--remote",
        metavar="SOURCE",
        help="remote_sources.json の接続先キーでリモート promote（S-08b）",
    )
    parser.add_argument(
        "--repair-seq",
        action="store_true",
        help="promote_seq.json を origin_seq_watermarks + 1 で修復（S-06b）",
    )
    parser.add_argument(
        "--origin",
        metavar="INSTANCE_ID",
        help="--repair-seq の対象 origin（省略時は自分自身の instance_id）",
    )
    parser.add_argument(
        "--resign",
        action="store_true",
        help="全 receipt を新鍵で再署名（安全契約あり・S-06b）",
    )
    args = parser.parse_args(argv)

    try:
        if args.repair_seq:
            if args.force:
                parser.error("--force は通常の promote 専用")
            origin = args.origin or _instance_id()
            repair_seq_for(origin=origin)
        elif args.resign:
            if args.force:
                parser.error("--force は通常の promote 専用")
            resign_receipts()
        elif args.remote is not None:
            if args.force or args.origin is not None:
                parser.error("--force / --origin は通常の promote / --repair-seq 専用")
            run_remote_promote(args.remote, target_name=args.name)
        else:
            if not args.name:
                parser.error("name が必要（--remote / --repair-seq / --resign 以外のモードでは必須）")
            if args.origin is not None:
                parser.error("--origin は --repair-seq 専用")
            run_promote(args.name, force=args.force)
    except PromoteError as exc:
        print(f"[hh_skill_promote] ERROR: {exc}", file=sys.stderr)
        return 1
    except PromoteLockTimeout as exc:
        print(f"[hh_skill_promote] ERROR: {exc}（同期処理が実行中です）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
