"""Hermes Agent プロファイル・設定のModal移行App — WindowsPCからエクスポートした
バックアップ一式(hermes-full-backup.tar.gz)を、Modal上のLinuxコンテナの
HERMES_HOME(/opt/data)へ取り込む一回限り実行用のモジュール。

`modal_hub/gateway_app.py` と同様、既存 App(hh-agent-hub・hh-agent-dashboard)とは
疎結合(R-2以来の方針): Dockerfile・Volume名は値として再定義するのみで、
モジュールをまたいだ import はしない。

本モジュールにAPIキー等のシークレット値は一切ハードコードしない
(扱うのはロジックのみ。値はバックアップ内のファイルにのみ存在する)。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import modal

_MIGRATION_VOLUME_NAME = "hermes-migration-data"
_MOUNT_PATH = "/opt/data"
_BACKUP_ARCHIVE_NAME = "hermes-full-backup.tar.gz"
_HOME_BACKUP_DIR = "home-backup"
_PROFILES_DIR = "profiles"
_EXTRACT_ROOT = "/tmp/migration_extract"

# 稼働中のhh-agent-dashboard/hh-agent-gatewayが実際にマウントしているVolume。
# config_versionマイグレーション(run_config_migration)専用にこちらへも接続する
# (hermes-migration-dataとは別物。取り違えると本番データを一切migrateしない
# ままになるので、名前をmodal_dashboard/app.pyの_DASHBOARD_VOLUME_NAMEと
# 揃えてある)。
_LIVE_DASHBOARD_VOLUME_NAME = "hh-agent-dashboard-home"

app = modal.App("hermes-migration")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE_PATH = _REPO_ROOT / "modal_dashboard" / "Dockerfile"

image = modal.Image.from_dockerfile(_DOCKERFILE_PATH, context_dir=_REPO_ROOT)

volume = modal.Volume.from_name(_MIGRATION_VOLUME_NAME, create_if_missing=True)
live_dashboard_volume = modal.Volume.from_name(_LIVE_DASHBOARD_VOLUME_NAME, create_if_missing=False)


def _extract_archive(archive_path: Path, extract_root: Path) -> None:
    """バックアップtar.gzをextract_rootへ展開する。

    Modalのwarm containerで再実行されたときに前回の展開分が残らないよう、
    展開前にextract_rootを丸ごと削除してから作り直す。また
    ``filter="data"`` で安全な展開(パス・symlink等の攻撃パターンを遮断)を行う。
    """
    shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(archive_path), "r:gz") as archive:
        archive.extractall(str(extract_root), filter="data")
    print(f"展開完了: {archive_path} -> {extract_root}")


# home-backup直下からコピーしてよい既知の項目のみ(それ以外は無差別コピーしない)
_ALLOWED_HOME_ITEMS = {".env", "config.yaml", "auth.json", "memories", "skills"}


def _copy_home_backup(home_backup_dir: Path, dest_root: Path) -> None:
    """home-backup直下の設定一式をdest_root直下へコピーする。

    コピー対象は許可リスト _ALLOWED_HOME_ITEMS の5項目だけに限定する。
    許可リスト外の項目はコピーせず、名前をprintでログするだけに留める。
    """
    if not home_backup_dir.is_dir():
        print(f"スキップ: {home_backup_dir} が存在しません")
        return
    for item in sorted(home_backup_dir.iterdir()):
        if item.name not in _ALLOWED_HOME_ITEMS:
            print(f"スキップ(許可リスト外): {item.name}")
            continue
        dest = dest_root / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)
        print(f"コピー: {item.name} -> {dest}")


def _import_profiles(profiles_dir: Path) -> list[str]:
    """profiles/内の各*.tar.gzを`hermes profile import`で取り込む。

    1つのプロファイルが失敗しても残りは続行し、最後に成功/失敗件数と
    失敗したプロファイル名一覧をprintする(例外は握りつぶさず、内容を報告する)。
    """
    if not profiles_dir.is_dir():
        print(f"スキップ: {profiles_dir} が存在しません")
        return []
    archives = sorted(profiles_dir.glob("*.tar.gz"))
    succeeded: list[str] = []
    failed: list[str] = []
    for archive in archives:
        print(f"==> import開始: {archive.name}")
        try:
            result = subprocess.run(
                ["hermes", "profile", "import", str(archive)],
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception as exc:  # noqa: BLE001 — 1件の失敗で移行全体を止めない
            failed.append(archive.name)
            print(f"!! import失敗: {archive.name} ({exc!r})")
            continue
        succeeded.append(archive.name)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
    print(f"プロファイルimportサマリ: 成功 {len(succeeded)}件 / 失敗 {len(failed)}件")
    if failed:
        print(f"失敗したプロファイル: {', '.join(failed)}")
    return failed


def _normalize_windows_paths() -> None:
    """設定ファイル内に残留するWindows絶対パスを/opt/data基準へ書き換える。

    path_fix.pyは移行に必須のため、importできない場合はそのまま失敗させる
    (fail-fast。スキップして移行が半端な状態になることを防ぐ)。
    """
    from hermes_migration.path_fix import normalize_windows_paths

    changed = normalize_windows_paths(
        root=Path(_MOUNT_PATH),
        old_prefixes=[
            r"C:\Users\Haruki\AppData\Local\hermes",
            "C:/Users/Haruki/AppData/Local/hermes",
            "/c/Users/Haruki/AppData/Local/hermes",
        ],
        new_base=_MOUNT_PATH,
    )
    if changed:
        print(f"パス書き換え完了: {len(changed)}件")
        for path in changed:
            print(f"  変更: {path}")
    else:
        print("パス書き換え: 変更対象なし")


def _chmod_credentials() -> None:
    """.env と auth.json を0600にし、他ユーザーから読み取れないようにする。"""
    for name in (".env", "auth.json"):
        path = Path(_MOUNT_PATH) / name
        try:
            os.chmod(path, 0o600)
        except FileNotFoundError:
            continue
        print(f"chmod 0600: {path}")


def _print_profile_list() -> None:
    """import後のプロファイル一覧を確認用に出力する。"""
    result = subprocess.run(
        ["hermes", "profile", "list"], capture_output=True, text=True
    )
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)


@app.function(image=image, volumes={_MOUNT_PATH: volume}, timeout=1800)
def run_import() -> None:
    """バックアップtar.gzを展開し、home-backupと各プロファイルを/opt/dataへ取り込む。

    手順: 展開 -> home-backupコピー -> プロファイルimport -> Windowsパス正規化
    -> 資格情報のchmod -> プロファイル一覧確認 -> Volume永続化。
    """
    backup_archive = Path(_MOUNT_PATH) / _BACKUP_ARCHIVE_NAME
    if not backup_archive.is_file():
        raise FileNotFoundError(
            f"バックアップが見つかりません: {backup_archive} "
            f"(先に {_MIGRATION_VOLUME_NAME} Volumeへアップロードしてください)"
        )

    extract_root = Path(_EXTRACT_ROOT)
    _extract_archive(backup_archive, extract_root)

    _copy_home_backup(extract_root / _HOME_BACKUP_DIR, Path(_MOUNT_PATH))

    failed = _import_profiles(extract_root / _PROFILES_DIR)

    _normalize_windows_paths()

    _chmod_credentials()

    _print_profile_list()

    volume.commit()
    print(f"run_import完了 (失敗プロファイル: {failed if failed else 'なし'})")


@app.function(image=image, volumes={_MOUNT_PATH: live_dashboard_volume}, timeout=300)
def run_config_migration() -> None:
    """稼働中のhh-agent-dashboard-home上のconfig.yamlを最新スキーマへ移行する。

    modal_dashboard/はDockerイメージのs6-overlayエントリポイントを経由せず
    Modalが直接ASGIアプリを起動するため(README「Process supervision: none」)、
    本番Dockerfileのブート時マイグレーション(scripts/docker_config_migrate.py)
    が実質一度も走っていない。config_versionが上流マージのたびに取り残される
    のはこのため。`hermes.exe`/ダッシュボードの起動を待たず、このFunctionを
    直接叩いて手動でマイグレーションを適用する。
    """
    import os
    import sys

    os.environ["HERMES_HOME"] = _MOUNT_PATH
    repo_root = Path("/opt/hermes")
    for _dir in (repo_root, repo_root / "scripts"):
        if str(_dir) not in sys.path:
            sys.path.insert(0, str(_dir))

    from hermes_cli.config import check_config_version

    before, latest = check_config_version()
    print(f"移行前: config_version={before}, latest={latest}")
    if before >= latest:
        print("既に最新のため何もしません")
        return

    import docker_config_migrate  # noqa: PLC0415 — sys.path挿入後に必要

    exit_code = docker_config_migrate.main()
    if exit_code != 0:
        raise RuntimeError(f"docker_config_migrate.main()がexit code {exit_code}を返しました")

    after, _ = check_config_version()
    print(f"移行後: config_version={after}")
    if after < latest:
        raise RuntimeError(f"マイグレーション後もconfig_versionが古いまま(after={after}, latest={latest})")

    live_dashboard_volume.commit()
    print("run_config_migration完了")
