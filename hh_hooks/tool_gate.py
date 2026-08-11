"""hh_hooks/tool_gate.py — Claude Code / Hermes 共通ツールゲート（Phase 1a）。

設計上の位置づけ:
    - 親設計書 docs/hh-agent/03_Architecture.md §4.4「共通ツールゲート」
    - 実装契約   docs/hh-agent/05_Phase1a_Spec.md（API 契約・状態遷移・canonical JSON・
      workspace_id・risk_rules.yaml・エージェントトークン・バイパスファイル）

**1 本のスクリプトが Claude Code の ``PreToolUse`` フックと Hermes の
``pre_tool_call`` フックの両方として動く。** 両者は同一のワイヤプロトコル
（stdin に JSON、ブロックは ``{"decision":"block"}`` および/または exit code 2）を
持つため、``hook_event_name`` の値だけを見て挙動を変える必要はない（ブロック時の
出力は常に両ホストで通る形式を同時に出す。詳細は ``_emit_deny()`` を参照）。

== 絶対原則: フェイルクローズ ==

Hub へ到達できない・レスポンス形状が想定外・内部で例外が飛ぶ——このいずれの場合も
必ず deny（拒否）で終わる。「わからないので許可する」という分岐はこのファイルの
どこにも存在してはならない。``main()`` はすべての例外を最上位で捕捉し、
deny 出力へ変換してから終了する。

== 標準ライブラリのみ ==

このファイルが直接 import するのは標準ライブラリのみ（``urllib.request`` 経由の
HTTP・``json``・``hmac``・``hashlib``・``os``・``sys`` 等）。``requests`` /
``httpx`` は import しない。全ツール呼び出しごとに新しい Python プロセスが
起動するため、起動〜allow 返却が 200ms 未満であることが Phase1a の性能要件
（親設計書 §4.4・§8.2）。LOW リスク（読み取り専用ツール）はこの直接 import の
範囲だけで判定が完結し、ネットワーク往復はゼロになる。

**既知の注記**: ``hh_hooks/risk.py``（``scripts/sync_hook_modules.py`` が
``modal_hub/core/risk.py`` から生成する複製・本ファイルの所有範囲外）は内部で
``import yaml`` している。したがって本ファイル自身の import 文は標準ライブラリのみ
だが、フックプロセス全体としては実行環境に PyYAML が入っている必要がある
（Hermes 同梱の Python 環境には通常入っているが、Claude Code 側が別の Python
インタプリタを使う設定の場合は要確認）。これは risk.py 側の既存実装を
「再利用するが書き換えない」という本タスクの制約から生じる話であり、
REQUESTS TO OTHER OWNERS として報告する。

== このファイルが実装時に確定させた設計判断（BLOCKED として開示） ==

Phase1a spec / 親設計書のどちらにも明記が無く、実装のために判断せざるを得なかった
点を列挙する。詳細は各関数の docstring と、タスク完了時の報告本文を参照。

1. **canonical.py 不在時の扱い**: ``modal_hub/core/canonical.py`` は実装時点
   (2026-08-11) でまだ存在しない（``04_Task_Allocation.md`` のどちらの担当表にも
   所有者が記載されていない）。HIGH リスクの shell 承認フローは
   ``payload_sha256``/``payload_raw_sha256`` の計算に canonical hashing を必須と
   するため、canonical モジュールが見つからない場合は **Hub に一切接続せず
   即座に deny する**（フェイルクローズの正しい適用であり、これ自体は判断の
   余地がない。「canonical.py が無い」という状況そのものが未解決の課題）。
2. **MEDIUM リスクの通知**: 05_Phase1a_Spec.md §1 の API 一覧には MEDIUM 用の
   専用エンドポイントが定義されていない。親設計書 §4.4 の「200ms のタイムアウトを
   付けた同期送信を1回試みる」という記述と、既存の
   ``POST /api/approval/request`` が「要求登録＋通知」を担い「即返す
   （ブロックしない）」設計であることを踏まえ、MEDIUM は
   ``risk="MEDIUM"`` として同じ ``/api/approval/request`` を 1 回・200ms
   タイムアウトで叩き、以降の poll/claim は行わずツール実行を許可する
   （承認/却下されても実行は既に進んでいるので結果を待たない）。この解釈が
   誤りであれば新しい専用エンドポイントの新設を提案する。
3. **Write/Edit/NotebookEdit の HIGH 判定は「承認しても実行しない」に degrade
   済み**: 親設計書 §4.3 が明示的に許容している縮退（「実装難度が高い場合は
   deny のみに落としてよい。実装時に BLOCKED として報告し、司令塔が決める」）を
   採用した。理由: TOCTOU 再検証（open→fstat での再照合）は「実際にファイルを
   開いて書き込む主体」が行う必要があるが、その主体は Claude Code 組み込みの
   Write ツール、または Hermes の ``tools/file_operations.py`` であり、
   いずれも本タスクの所有範囲外・変更禁止（「既存 Hermes ファイルへの変更は
   ゼロ」）。``PreToolUse``/``pre_tool_call`` フックは実行前に一度だけ
   呼ばれて終了する短命プロセスであり、実際の open(fd) の瞬間まで
   在席してその fd を検証することは構造的にできない。したがって
   Write/Edit/NotebookEdit が HIGH と判定された場合は Hub に一切接続せず
   ローカルで即座に deny する（Bash の承認フローは完全実装のまま）。
4. **``category`` の取得に risk モジュールの private 関数を使用**: ``Risk`` は
   ``level``/``rule_id``/``reason``/``normalized_target`` のみを公開し、
   どの ``tool_aliases`` カテゴリに属するかを公開しない。HIGH 判定後に
   「shell か write/edit/notebook か」を分岐するために ``risk._alias_lookup()``
   （アンダースコア始まりの内部関数）を直接呼んでいる。risk.py 自体を
   書き換えることは許されていないため、公開 API を新設せず既存の内部関数を
   読み取り専用で利用する形にした。理想的には risk.py 側に
   ``category_of(tool_name) -> Optional[str]`` のような公開 API があるべきで、
   REQUESTS TO OTHER OWNERS として報告する。
5. **エージェントトークンの自動更新は未実装**: 親設計書 §6.2 は「有効期限の
   2 時間前を切っていたらフック起動時に自動更新」と書いているが、
   05_Phase1a_Spec.md §1 の API 一覧にトークン更新用エンドポイントが無い。
   本実装はトークンの ``exp`` をローカルで確認し、期限切れなら Hub に接続せず
   deny する（「``hh auth login`` を再実行してください」という理由文言を返す）。
   自動更新は行わない。
6. **Hub のベース URL / トークンファイルの保存場所**: Phase1a spec に明記が
   無いため、次の場所をローカル設定として採用した（INSTALL.md にも記載）。
     - Hub URL: 環境変数 ``HH_AGENT_HUB_URL``、無ければ
       ``%USERPROFILE%\\.hh-agent\\config.json`` の ``"hub_url"`` キー。
     - エージェントトークン: 親設計書 §6.2 が明記する
       ``%USERPROFILE%\\.hh-agent\\agent_token.json``。中身は
       ``{"token": "hha1.<payload>.<sig>"}`` を採用（トークン文字列そのものの
       形式は §6.1 で確定済みだが、ファイルの JSON 構造は未確定だったため）。
7. **バイパスファイルの署名対象文字列**: §8 は HMAC 署名を要求するが、
   署名対象に何を連結するかは明記が無い。本実装は
   ``f"{enabled_at}|{reason}"`` を HMAC-SHA256 する形式を採用した。
   ``hh bypass enable``（未実装・本タスクの所有範囲外）はこの形式に
   合わせて書く必要がある。
8. **``targets[].path`` と ``targets[].realpath`` の意味的な重複**: これは
   上記 3. の degrade により本ファイルでは未実装のため実害は無いが、
   05_Phase1a_Spec.md §3 は両方を ``PathStr``（＝ 同一の realpath 正規化）と
   宣言しており、字面通り実装すると両フィールドは常に同一値になる。
   Write/Edit の TOCTOU 検証を将来実装する担当者向けに記録として残す。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 生成モジュール (risk.py / canonical.py) の読み込み
#
# hh_hooks/ 配下に scripts/sync_hook_modules.py が複製したファイルが並ぶ前提。
# パッケージ相対 import ではなく sys.path 経由の素朴なモジュール import にする
# （tool_gate.py は `python tool_gate.py` の形で直接実行されるスクリプトであり、
#  パッケージとして import されることを想定しない）。
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

# ---------------------------------------------------------------------------
# 標準入出力を明示的に UTF-8 へ固定する。
#
# 実機検証で判明した重要な既知バグの回避（2026-08-11）: このリポジトリの Windows
# 環境（日本語ロケール、コンソールコードページ cp932）では、Python 3 は
# サブプロセスとしてパイプ経由で起動された場合でも sys.stdin/stdout/stderr の
# デフォルトエンコーディングを locale.getpreferredencoding()（＝ cp932）に
# する。ワイヤプロトコル（stdin の JSON・stdout の block JSON）はどちらも
# 事実上 UTF-8 前提であり（Claude Code / Hermes 双方とも JSON は UTF-8 として
# 送受信する）、cp932 のまま書き出すと deny の block JSON が呼び出し元で
# 正しく UTF-8 デコードできず、パースに失敗しうる——つまり「拒否したつもりが
# 相手に伝わらない」というフェイルクローズの根幹を崩す事故になる。
# reconfigure() は Python 3.7+ で利用可能。失敗しても機能を止めない
# （その場合でも allow 側は空文字列送出のみなので影響が無く、deny 側は
# 後段で errors="replace" のフォールバックにより最低限クラッシュはしない）。
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _load_generated_module(name: str):
    """hh_hooks/<name>.py を import する。無ければ None を返す（例外を投げない）。

    risk.py / canonical.py はどちらも「無ければ deny する」判断をこの関数の
    呼び出し側が行う。ここでは静かに None を返すだけにして、フェイルクローズの
    判断ロジックを一箇所（呼び出し側）に集約する。
    """
    try:
        module = __import__(name)
    except ImportError:
        return None
    return module


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# 親設計書 §4.4「タイムバジェット」(D-13)。
# フック内部デッドライン: これを過ぎたら必ず deny JSON を返して終了する。
INTERNAL_DEADLINE_SECONDS = 170.0

# MEDIUM 通知の同期送信タイムアウト（親設計書 §4.4）。
MEDIUM_NOTIFY_TIMEOUT_SECONDS = 0.2

# HIGH (shell) の poll 間隔（親設計書 §4.3「5秒間隔＝ハートビート」）。
POLL_INTERVAL_SECONDS = 5.0

# 個々の HTTP 呼び出しに許す最大タイムアウト（残り予算でさらにクランプする）。
_HTTP_MAX_TIMEOUT_SECONDS = 10.0

# リトライ方針（05_Phase1a_Spec.md §1.1）: 最大3回、初回0.5秒・以後2倍・上限4秒。
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.5
_RETRY_MAX_DELAY_SECONDS = 4.0

# request の payload / 全体サイズ上限（05_Phase1a_Spec.md §1.2）。
_PAYLOAD_MAX_BYTES = 4096
_REQUEST_MAX_BYTES = 65536

# ローカル状態の保存場所（親設計書 §6.2 / §8。ファイル形式は本ファイルの
# docstring「既知のギャップ 6.」参照）。
_HH_AGENT_HOME_ENV = "USERPROFILE"

# DEFECT 1 の修正: このフックプロセス自身を表す一意な ID。import 時
# （＝プロセス起動時）に一度だけ生成し、以後このプロセスが終了するまで不変。
# ホストがツール呼び出し ID を供給しない場合の `_extract_call_id()` の
# フォールバックとして使う。詳細は `_extract_call_id()` の docstring を参照。
# ここで `uuid` を import レベルで使うのは標準ライブラリのみであり、
# 性能要件（起動〜allow 返却 200ms/300ms 未満）には影響しない。
_PROCESS_INSTANCE_ID = str(uuid.uuid4())


def _hh_agent_home() -> Path:
    """``%USERPROFILE%\\.hh-agent`` を返す（Windows 前提。無ければ Path.home() で代替）。"""
    userprofile = os.environ.get(_HH_AGENT_HOME_ENV)
    base = Path(userprofile) if userprofile else Path.home()
    return base / ".hh-agent"


_BLOCK_MESSAGE_PREFIX = "[HH-AGENT] "


# ---------------------------------------------------------------------------
# stdin / 出力ヘルパー
# ---------------------------------------------------------------------------


def _read_stdin_request() -> dict:
    """stdin から JSON を読み込む。空・非JSON・非dict は例外を投げる（呼び出し側で deny）。"""
    raw = sys.stdin.read()
    if not raw or not raw.strip():
        raise ValueError("stdin が空（フックへの入力 JSON が渡っていない）")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"stdin の JSON はオブジェクトでなければならない: {type(parsed)!r}")
    return parsed


def _emit_allow() -> None:
    """許可。両ホストとも「stdout 空 ＋ exit 0」を素通り（allow）として扱う。

    LOW リスクパスの性能要件（200ms 未満）のため、ここでは一切の I/O を追加しない。
    """
    sys.stdout.write("")
    sys.exit(0)


def _emit_deny(reason: str) -> None:
    """拒否。親設計書 §4.4 の「三重掛け」を実装する:

        exit code 2 ＋ stderr に理由 ＋ stdout に両ホストで通る JSON。

    stdout の JSON は 3 つの形を同時に含める:
        - Claude-Code-style (レガシー): {"decision": "block", "reason": ...}
        - Hermes-canonical:            {"action": "block", "message": ...}
        - Claude Code 新形式:           {"hookSpecificOutput": {...}}
    どの形を解釈するかはホスト側の実装依存であり、余分なキーは無視される
    前提でどちらのホストでも安全に動く。

    理由文字列にはトークン値・署名鍵・バイパスローカルキーの値を絶対に含めない
    （hard rule 4）。呼び出し側は生の例外メッセージをそのまま渡す前に、
    このファイル内で秘密値を扱う箇所（トークン読み込み等）が理由文字列へ
    値そのものを混入させないよう個別に注意している。
    """
    safe_reason = reason if reason else "denied by hh-agent tool gate (no reason given)"
    sys.stderr.write(_BLOCK_MESSAGE_PREFIX + safe_reason + "\n")

    block_json = {
        "decision": "block",
        "reason": safe_reason,
        "action": "block",
        "message": safe_reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": safe_reason,
        },
    }
    sys.stdout.write(json.dumps(block_json, ensure_ascii=False))
    sys.exit(2)


# ---------------------------------------------------------------------------
# PathStr 正規化 / workspace_id / base_revision (05_Phase1a_Spec.md §3・§4)
# ---------------------------------------------------------------------------


def _normalize_pathstr(raw_path: str) -> str:
    """§3 の PathStr 正規化: realpath() → '\\' を '/' に置換。大文字小文字は変換しない。"""
    return os.path.realpath(raw_path).replace("\\", "/")


def _run_git(args: list, cwd: str, timeout_seconds: float = 3.0) -> Optional[str]:
    """git サブコマンドを実行し、成功したら stdout（前後空白除去）を返す。

    失敗（git 未インストール・非リポジトリ・タイムアウト等）はすべて None を返す。
    §4 の規定どおり「git コマンドが失敗 → cwd にフォールバック、base_revision は null」
    をエラーにせず表現するための共通ヘルパー。
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    output = proc.stdout.strip()
    return output or None


def _compute_workspace_id(cwd: str) -> str:
    """05_Phase1a_Spec.md §4 の workspace_id() をそのまま実装する。"""
    root = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd) or cwd
    real = os.path.realpath(root).replace("\\", "/")
    return hashlib.sha256(real.encode("utf-8")).hexdigest()


def _compute_base_revision(cwd: str) -> Optional[str]:
    """§4: git rev-parse HEAD の40桁hex、取得できなければ None。"""
    return _run_git(["rev-parse", "HEAD"], cwd=cwd)


# ---------------------------------------------------------------------------
# エージェントトークン (親設計書 §6.2)
# ---------------------------------------------------------------------------


class AgentTokenError(RuntimeError):
    """トークンが読めない・不正・期限切れの場合に送出する（呼び出し側は deny する）。"""


def _b64url_decode(segment: str) -> bytes:
    import base64

    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _load_agent_token() -> str:
    """%USERPROFILE%\\.hh-agent\\agent_token.json からトークン文字列を読み込む。

    ファイル形式は本ファイルの docstring「既知のギャップ 6.」で確定した
    ``{"token": "hha1.<payload>.<sig>"}``。

    ローカルで ``exp`` を確認し、期限切れなら AgentTokenError を送出する
    （Hub 側の検証を待たずに無駄なリクエストを避けるための事前チェックであり、
    実際の有効性の最終判定は §11 のとおり常に Hub 側の肯定リストが行う）。

    トークン値そのものは決して例外メッセージに含めない（hard rule 4）。
    """
    token_path = _hh_agent_home() / "agent_token.json"
    if not token_path.is_file():
        raise AgentTokenError(f"エージェントトークンが未設定（{token_path} が無い）。'hh auth login' を実行してください")

    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentTokenError(f"エージェントトークンファイルの読み込みに失敗（詳細は伏せる）: {type(exc).__name__}") from exc

    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token.startswith("hha1."):
        raise AgentTokenError("エージェントトークンファイルの形式が不正（hha1.形式ではない）")

    parts = token.split(".")
    if len(parts) != 3:
        raise AgentTokenError("エージェントトークンの形式が不正（3セグメントではない）")

    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception as exc:
        raise AgentTokenError(f"エージェントトークンの payload をデコードできない: {type(exc).__name__}") from exc

    exp = payload.get("exp") if isinstance(payload, dict) else None
    if not isinstance(exp, (int, float)):
        raise AgentTokenError("エージェントトークンの payload に exp が無い")
    if time.time() >= exp:
        raise AgentTokenError("エージェントトークンの有効期限切れ。'hh auth login' を再実行してください")

    return token


# ---------------------------------------------------------------------------
# Hub 設定 (Base URL)
# ---------------------------------------------------------------------------


class HubConfigError(RuntimeError):
    pass


def _load_hub_base_url() -> str:
    """Hub の Base URL を取得する。

    優先順位: 環境変数 ``HH_AGENT_HUB_URL`` > ``%USERPROFILE%\\.hh-agent\\config.json``
    の ``"hub_url"`` キー。どちらも無ければ HubConfigError。
    """
    env_url = os.environ.get("HH_AGENT_HUB_URL")
    if env_url:
        return env_url.rstrip("/")

    config_path = _hh_agent_home() / "config.json"
    if not config_path.is_file():
        raise HubConfigError(
            f"Hub の URL が未設定（環境変数 HH_AGENT_HUB_URL も {config_path} も無い）"
        )
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HubConfigError(f"{config_path} の読み込みに失敗: {type(exc).__name__}") from exc

    hub_url = data.get("hub_url") if isinstance(data, dict) else None
    if not isinstance(hub_url, str) or not hub_url:
        raise HubConfigError(f"{config_path} に 'hub_url' が無い")
    return hub_url.rstrip("/")


# ---------------------------------------------------------------------------
# HTTP (標準ライブラリのみ: urllib.request)
# ---------------------------------------------------------------------------


class HttpOutcome:
    """1回の HTTP 呼び出し（リトライ込み）の結果。

    ``ok`` が False の場合、呼び出し側は必ず deny する
    （フェイルクローズ。Hub 到達不能・リトライ尽き・レスポンス形状不正すべてがここに集約する）。
    """

    __slots__ = ("ok", "status", "body", "error")

    def __init__(self, ok: bool, status: Optional[int], body: Optional[dict], error: Optional[str]):
        self.ok = ok
        self.status = status
        self.body = body
        self.error = error


def _jittered_delay(base: float) -> float:
    jitter = base * random.uniform(-0.1, 0.1)
    return max(0.0, min(_RETRY_MAX_DELAY_SECONDS, base + jitter))


def _http_request(
    method: str,
    url: str,
    body: Optional[dict],
    token: Optional[str],
    timeout_seconds: float,
) -> tuple:
    """1回だけ HTTP リクエストを送る。戻り値は (status_code_or_None, parsed_json_or_None, error_str_or_None)。

    接続エラー・タイムアウトは (None, None, "<説明>") を返す（05_Phase1a_Spec.md §1.1
    「接続エラー/タイムアウト: retryable=true」に対応する形）。
    HTTP レスポンスが返った場合は非2xxでも status/body を返し、呼び出し側で判断する。
    """
    import urllib.error
    import urllib.request

    data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            status = resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"

    parsed: Optional[dict] = None
    if raw:
        try:
            candidate = json.loads(raw.decode("utf-8"))
            if isinstance(candidate, dict):
                parsed = candidate
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
    return status, parsed, None


def _call_hub(
    method: str,
    path: str,
    body: Optional[dict],
    token: Optional[str],
    hub_base_url: str,
    deadline_monotonic: float,
) -> HttpOutcome:
    """05_Phase1a_Spec.md §1.1 のリトライ方針を実装した Hub 呼び出し。

    最大3回、初回0.5秒・以後2倍・上限4秒のジッタ付き指数バックオフ。
    残り時間予算を超えるリトライは行わない。retryable は応答本文の値を正とする
    （HTTP ステータスから推測しない）。応答本文が読めない/形が不正な場合は
    フェイルクローズ側（ok=False）に倒す。
    """
    url = hub_base_url + path
    last_error: Optional[str] = None
    delay = _RETRY_BASE_DELAY_SECONDS

    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0.05:
            return HttpOutcome(False, None, None, "内部デッドライン超過（Hub 呼び出しの残り予算なし）")

        attempt_timeout = max(0.05, min(_HTTP_MAX_TIMEOUT_SECONDS, remaining))
        status, parsed, error = _http_request(method, url, body, token, attempt_timeout)

        if error is not None:
            # 接続エラー / タイムアウト。§1.1 の表では常に retryable=true。
            last_error = error
            retryable = True
        elif status is not None and 200 <= status < 300:
            return HttpOutcome(True, status, parsed, None)
        else:
            # HTTP レスポンスは得られたが非2xx。retryable は本文の値を正とする。
            retryable = bool(parsed and isinstance(parsed.get("error"), dict) and parsed["error"].get("retryable") is True)
            last_error = f"HTTP {status}: {json.dumps(parsed, ensure_ascii=False) if parsed else '(本文なし/不正)'}"
            if not retryable:
                return HttpOutcome(False, status, parsed, last_error)

        if attempt >= _RETRY_MAX_ATTEMPTS:
            break

        remaining_after = deadline_monotonic - time.monotonic()
        wait = _jittered_delay(delay)
        if wait >= remaining_after:
            break
        time.sleep(wait)
        delay = min(_RETRY_MAX_DELAY_SECONDS, delay * 2)

    return HttpOutcome(False, None, None, last_error or "不明なエラー")


# ---------------------------------------------------------------------------
# canonical hashing のロード（無ければ HIGH shell は deny）
# ---------------------------------------------------------------------------


def _require_canonical_module():
    """hh_hooks/canonical.py（生成コピー）を読み込む。無ければ None。

    このモジュールが存在しない場合、HIGH リスク shell の承認フローは
    payload_sha256 を計算できないため、呼び出し側は Hub に接続せず即座に
    deny しなければならない（ファイル docstring の判断 1. を参照）。
    """
    return _load_generated_module("canonical")


# ---------------------------------------------------------------------------
# 冪等キー / claim_attempt_id の決定的導出
# ---------------------------------------------------------------------------


def _extract_call_id(request: dict) -> str:
    """ツール呼び出し固有の ID をベストエフォートで探す。

    Claude Code の PreToolUse stdin には安定版時点でツール呼び出し ID に
    相当するフィールドが含まれない可能性がある一方、Hermes は
    ``extra.tool_call_id`` を持つ（agent/shell_hooks.py docstring 参照）。
    どちらのホストでも確実に得られる保証が無いため、複数のキー名を
    優先順に探索する。

    **DEFECT 1 の修正（2026-08-11）**: ホストが ID を一切供給しない場合、
    以前はここで空文字列 ``""`` を返していた。これは
    ``_derive_idempotency_key()`` / ``_derive_claim_attempt_id()`` が
    「セッション ID・ツール名・cwd・payload ハッシュ」という**コマンド内容のみ**
    から鍵を導出することを意味し、同一セッションで同一コマンドを 2 プロセスが
    同時に実行しようとすると、両方が同じ ``claim_attempt_id`` を送ることになる。
    Hub 側の冪等化（05_Phase1a_Spec.md §1.4 手順4）はこれを「応答喪失からの
    正当な再試行」と見なして**同じ lease を両方へ返し**、両方の hook プロセスが
    allow してしまう——1 回のスマホ承認で 2 回実行される。

    修正: ホストが ID を供給しない場合は ``_PROCESS_INSTANCE_ID``
    （このプロセスの import 時に一度だけ生成された UUID）を返す。
    実行意図をコマンド内容だけから導出することを止め、「このプロセスが
    処理している 1 回の呼び出し」という単位に紐付ける。

    **この設計が成立する前提**: 1 回のフック呼び出し ＝ 1 回の新規 Python
    プロセス起動である（``.claude/settings.json`` / ``cli-config.yaml`` は
    どちらも ``command: python .../tool_gate.py`` を毎回新規起動する。
    本ファイル冒頭の性能要件の記述「全ツール呼び出しごとに Python プロセスが
    起動する」も参照）。したがって:
        - **同一プロセス内の HTTP リトライ間**では ``_PROCESS_INSTANCE_ID`` は
          import 時に 1 度だけ評価されたモジュール変数なので不変 → 安定。
        - **別々のフック呼び出し（別プロセス）間**では、プロセスごとに
          モジュールが再 import され ``uuid.uuid4()`` が再評価される → 別の値。
    2 つの独立したツール呼び出しがたまたま同一プロセスに同居することは
    設計上ない（フックは 1 回呼ばれて終了する短命スクリプト）。

    **受け入れるトレードオフ**: 同じコマンドを後から実行し直すと、以前のように
    同一 approval を再利用せず新しい承認要求が作られる。これは安全側であり、
    最適化してはならない（タスク指示より）。
    """
    for key in ("tool_call_id", "tool_use_id", "id"):
        value = request.get(key)
        if isinstance(value, str) and value:
            return value
    extra = request.get("extra")
    if isinstance(extra, dict):
        for key in ("tool_call_id", "tool_use_id"):
            value = extra.get(key)
            if isinstance(value, str) and value:
                return value
    return _PROCESS_INSTANCE_ID


def _derive_idempotency_key(session_id: str, tool_name: str, cwd: str, payload_raw_sha256: str, call_id: str) -> str:
    """05_Phase1a_Spec.md §1.2: 16..128文字・[A-Za-z0-9._:-]+ を満たす決定的キー。

    sha256 の16進ダイジェスト（64文字、[0-9a-f]のみ）は文字種・長さの両方の
    制約を機械的に満たす。
    """
    seed = f"idem|{session_id}|{tool_name}|{cwd}|{payload_raw_sha256}|{call_id}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _derive_claim_attempt_id(idempotency_key: str) -> str:
    """§1.4: 「1回の実行意図」につき1つ、リトライ間で変えない決定的値。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hh-agent-claim:{idempotency_key}"))


def _validate_claim_response(body: dict) -> Optional[str]:
    """claim レスポンスの成功スキーマを検証する（DEFECT 2 の修正）。

    ``lease_id`` は「このプロセスが実行権を勝ち取った」ことを示す唯一の証拠
    （05_Phase1a_Spec.md §1.4）。以前の実装は ``granted is not True`` だけを
    確認してから ``lease_id`` を取り出し、その値を一切検証せずに allow して
    いた。Hub が ``{"granted": true}`` を lease_id 無しで返す・型が違う値を
    返す・形式が壊れた文字列を返す——いずれの場合も証拠が無いまま実行を
    許可してしまう。

    ここでは §契約どおり **完全一致のスキーマ** のみを成功として扱う:
        - ``granted`` は厳密な bool の ``True``（``"true"`` 文字列や
          ``1`` のような truthy 値を暗黙変換で受理しない。
          Python の ``is`` 比較は ``1 is True`` が False になるため、
          ``bool`` のサブクラス関係があっても int の 1 を誤って通さない）。
        - ``lease_id`` は空でない文字列であり、かつ ``uuid.UUID()`` で
          パースできる（＝ UUID として妥当な形式である）こと。

    どちらか一方でも満たさなければ deny 理由の文字列を返す（fail closed）。
    満たせば None を返す。
    """
    if body.get("granted") is not True:
        return "claim が granted=true（厳密な bool）を返さなかった（想定外のレスポンス形状）"

    lease_id = body.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        return "claim の応答に有効な lease_id（非空の文字列）が無い（想定外のレスポンス形状）"

    try:
        uuid.UUID(lease_id)
    except (ValueError, AttributeError, TypeError):
        return "claim の応答の lease_id が有効な UUID として解釈できない（想定外のレスポンス形状）"

    return None


# ---------------------------------------------------------------------------
# バイパスファイル (05_Phase1a_Spec.md §8)
# ---------------------------------------------------------------------------


def _check_bypass() -> Optional[str]:
    """バイパスが有効なら人間可読な理由文字列を返す。無効/不在なら None。

    署名対象文字列の形式はファイル docstring「既知のギャップ 7.」で確定した
    ``f"{enabled_at}|{reason}"`` の HMAC-SHA256。

    署名鍵（local.key）の値、バイパスファイルの reason 文字列以外の内容は
    ログ・戻り値に含めない。
    """
    import hmac as hmac_mod

    bypass_path = _hh_agent_home() / "bypass"
    key_path = _hh_agent_home() / "local.key"

    if not bypass_path.is_file() or not key_path.is_file():
        return None

    try:
        data = json.loads(bypass_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    enabled_at = data.get("enabled_at")
    reason = data.get("reason")
    sig = data.get("sig")
    if not isinstance(enabled_at, (int, float)) or not isinstance(reason, str) or not isinstance(sig, str):
        return None

    try:
        local_key = key_path.read_bytes()
    except OSError:
        return None
    if not local_key:
        return None

    message = f"{enabled_at}|{reason}".encode("utf-8")
    expected_sig = hmac_mod.new(local_key, message, hashlib.sha256).hexdigest()
    if not hmac_mod.compare_digest(expected_sig, sig):
        return None

    now = time.time()
    # §4.4 規則5: 未来の mtime (ここでは enabled_at) を無効として扱う。
    if enabled_at > now + 60:
        return None
    # §8: TTL 30分。
    if now - enabled_at > 30 * 60:
        return None

    remaining_min = max(0, int((30 * 60 - (now - enabled_at)) / 60))
    return f"expires in {remaining_min} min"


def _log_bypass_usage(tool_name: str, risk_level: str, rule_id: str) -> None:
    """§8: バイパス使用は Hub とは独立したローカル監査ファイルへ毎回追記する。"""
    audit_path = _hh_agent_home() / "bypass_audit.log"
    line = json.dumps(
        {
            "at": time.time(),
            "tool_name": tool_name,
            "risk": risk_level,
            "rule_id": rule_id,
        },
        ensure_ascii=False,
    )
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        # ローカル監査の書き込み失敗でゲート判断そのものを変えない
        # （バイパスは既に「Hub 障害時のローカル脱出口」という位置づけであり、
        # 監査ログの失敗まで拒否理由にすると脱出口として機能しなくなる）。
        pass


# ---------------------------------------------------------------------------
# ローカル診断ログ（秘密値を含まない範囲でのベストエフォート記録）
# ---------------------------------------------------------------------------


def _log_local(message: str) -> None:
    log_path = _hh_agent_home() / "tool_gate.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.time()} {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# MEDIUM ハンドラ
# ---------------------------------------------------------------------------


def _handle_medium(risk_obj, tool_name: str, tool_input: dict, session_id: str, cwd: str, call_id: str) -> None:
    """MEDIUM: 実行は許可し、通知のみ試みる（結果を待たない）。

    ファイル docstring の判断 2. のとおり、専用エンドポイントが Phase1a spec に
    無いため ``POST /api/approval/request`` を risk="MEDIUM" として1回・
    200ms タイムアウトで送るだけに留める。失敗しても実行許可には影響しない
    （「明示的に諦めてローカルログに残す」）。
    """
    try:
        canonical_mod = _require_canonical_module()
        if canonical_mod is None:
            _log_local(f"MEDIUM notify skipped: canonical module missing (tool={tool_name})")
            return

        hub_base_url = _load_hub_base_url()
        token = _load_agent_token()

        payload = {"command": risk_obj.normalized_target} if risk_obj.normalized_target else dict(tool_input)
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(payload_bytes) > _PAYLOAD_MAX_BYTES:
            _log_local(f"MEDIUM notify skipped: payload too large (tool={tool_name})")
            return

        payload_sha256 = hashlib.sha256(canonical_mod.canonical_json(payload)).hexdigest()
        payload_raw_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        idempotency_key = _derive_idempotency_key(session_id, tool_name, cwd, payload_raw_sha256, call_id)

        body = {
            "idempotency_key": idempotency_key,
            "tool_name": tool_name,
            "payload": payload,
            "payload_sha256": payload_sha256,
            "payload_raw_sha256": payload_raw_sha256,
            "context": {
                "cwd": _normalize_pathstr(cwd),
                "workspace_id": _compute_workspace_id(cwd),
                "base_revision": _compute_base_revision(cwd),
            },
            "risk": "MEDIUM",
            "rule_id": risk_obj.rule_id,
            "reason_code": risk_obj.rule_id,
            "targets": [],
        }

        req_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(req_bytes) > _REQUEST_MAX_BYTES:
            _log_local(f"MEDIUM notify skipped: request too large (tool={tool_name})")
            return

        import urllib.error
        import urllib.request as urllib_request

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        }
        req = urllib_request.Request(hub_base_url + "/api/approval/request", data=req_bytes, headers=headers, method="POST")
        try:
            with urllib_request.urlopen(req, timeout=MEDIUM_NOTIFY_TIMEOUT_SECONDS) as resp:
                resp.read()
                _log_local(f"MEDIUM notify sent: tool={tool_name} status={resp.getcode()}")
        except Exception as exc:  # noqa: BLE001 — MEDIUM 通知はベストエフォート。握りつぶすが必ずログする。
            _log_local(f"MEDIUM notify failed: tool={tool_name} error={type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 — MEDIUM は絶対に実行をブロックしない。
        _log_local(f"MEDIUM notify error (non-fatal): tool={tool_name} error={type(exc).__name__}")


# ---------------------------------------------------------------------------
# HIGH: shell の完全フロー (request -> poll -> claim -> complete)
# ---------------------------------------------------------------------------


def _handle_high_shell(
    risk_obj,
    tool_name: str,
    tool_input: dict,
    session_id: str,
    cwd: str,
    call_id: str,
    deadline_monotonic: float,
) -> Optional[str]:
    """HIGH かつ shell カテゴリの完全な承認フロー。

    戻り値: None なら allow してよい。文字列が返れば、それを deny 理由として使う。
    """
    canonical_mod = _require_canonical_module()
    if canonical_mod is None:
        return (
            "canonical hashing モジュール (hh_hooks/canonical.py) が無いため "
            "HIGH リスクの承認要求を送れない。modal_hub/core/canonical.py が実装され "
            "scripts/sync_hook_modules.py が再実行されるまで、この操作は拒否される "
            "(Phase1a既知の未解決課題)"
        )

    try:
        hub_base_url = _load_hub_base_url()
    except HubConfigError as exc:
        return f"Hub 設定エラー: {exc}"

    try:
        token = _load_agent_token()
    except AgentTokenError as exc:
        return f"認証エラー: {exc}"

    command = risk_obj.normalized_target if risk_obj.normalized_target else tool_input.get("command")
    if not isinstance(command, str) or not command:
        return "shell コマンド文字列を取得できない（risk 分類結果が不正）"

    payload = {"command": command}
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(payload_bytes) > _PAYLOAD_MAX_BYTES:
        return "payload が上限 (4KB) を超えている。切り詰めずに HIGH のまま拒否する"

    payload_sha256 = hashlib.sha256(canonical_mod.canonical_json(payload)).hexdigest()
    payload_raw_sha256 = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    def _build_context() -> dict:
        return {
            "cwd": _normalize_pathstr(cwd),
            "workspace_id": _compute_workspace_id(cwd),
            "base_revision": _compute_base_revision(cwd),
        }

    context_at_request = _build_context()

    idempotency_key = _derive_idempotency_key(session_id, tool_name, context_at_request["cwd"], payload_raw_sha256, call_id)
    claim_attempt_id = _derive_claim_attempt_id(idempotency_key)

    request_body = {
        "idempotency_key": idempotency_key,
        "tool_name": tool_name,
        "payload": payload,
        "payload_sha256": payload_sha256,
        "payload_raw_sha256": payload_raw_sha256,
        "context": context_at_request,
        "risk": "HIGH",
        "rule_id": risk_obj.rule_id,
        "reason_code": risk_obj.rule_id,
        "targets": [],
    }
    req_bytes_len = len(json.dumps(request_body, ensure_ascii=False).encode("utf-8"))
    if req_bytes_len > _REQUEST_MAX_BYTES:
        return "request 全体が上限 (64KB) を超えている"

    outcome = _call_hub("POST", "/api/approval/request", request_body, token, hub_base_url, deadline_monotonic)
    if not outcome.ok or not outcome.body:
        return f"承認要求の送信に失敗（フェイルクローズ）: {outcome.error}"

    approval_id = outcome.body.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return "承認要求の応答に approval_id が無い（想定外のレスポンス形状）"

    if outcome.body.get("notify_state") == "failed":
        return "通知の送達に失敗した（ユーザーに気づいてもらえないため拒否する）"

    # --- poll ループ ---
    while True:
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return "内部デッドライン (170秒) を超過した"

        poll_outcome = _call_hub(
            "GET",
            f"/api/approval/poll?id={approval_id}",
            None,
            token,
            hub_base_url,
            deadline_monotonic,
        )
        if not poll_outcome.ok or not poll_outcome.body:
            return f"poll に失敗（フェイルクローズ）: {poll_outcome.error}"

        status = poll_outcome.body.get("status")
        if poll_outcome.body.get("notify_state") == "failed":
            return "通知の送達に失敗した（poll 中に判明。ユーザーに気づいてもらえないため拒否する）"

        if status == "pending":
            remaining = deadline_monotonic - time.monotonic()
            grace_remaining = poll_outcome.body.get("grace_remaining_seconds")
            wait_candidates = [POLL_INTERVAL_SECONDS, max(0.0, remaining - 0.5)]
            if isinstance(grace_remaining, (int, float)):
                wait_candidates.append(max(0.0, float(grace_remaining)))
            wait = min(wait_candidates)
            if wait <= 0:
                return "内部デッドラインまたは猶予時間を超過した"
            time.sleep(wait)
            continue

        if status == "approved":
            break

        # rejected / timeout / claimed（claim 前に claimed が来ることは無いはずだが
        # 想定外の値として同じ扱いにする） はすべて deny。
        return f"承認されなかった (status={status!r})"

    # --- claim ---
    context_at_claim = _build_context()
    claim_body = {
        "approval_id": approval_id,
        "claim_attempt_id": claim_attempt_id,
        "verification": {
            "payload_sha256": payload_sha256,
            "payload_raw_sha256": payload_raw_sha256,
            "context": context_at_claim,
            "targets": [],
        },
    }
    claim_outcome = _call_hub("POST", "/api/approval/claim", claim_body, token, hub_base_url, deadline_monotonic)
    if not claim_outcome.ok or not claim_outcome.body:
        return f"claim に失敗（フェイルクローズ）: {claim_outcome.error}"

    claim_schema_error = _validate_claim_response(claim_outcome.body)
    if claim_schema_error is not None:
        return claim_schema_error

    lease_id = claim_outcome.body.get("lease_id")

    # --- complete (ベストエフォート。ファイル docstring の該当セクション参照:
    #     PreToolUse フックは実行後の成否を観測できないため "consumed" で報告する) ---
    if isinstance(lease_id, str) and lease_id:
        complete_body = {
            "approval_id": approval_id,
            "lease_id": lease_id,
            "outcome": "consumed",
            "detail": "allowed by pre_tool_call/PreToolUse hook (Phase1a: post-execution outcome not observed)",
        }
        complete_outcome = _call_hub(
            "POST", "/api/approval/complete", complete_body, token, hub_base_url, deadline_monotonic
        )
        if not complete_outcome.ok:
            _log_local(f"complete 呼び出し失敗（許可は取り消さない）: {complete_outcome.error}")

    return None  # allow


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def _run(start_monotonic: float) -> None:
    deadline_monotonic = start_monotonic + INTERNAL_DEADLINE_SECONDS

    request = _read_stdin_request()

    tool_name = request.get("tool_name")
    tool_input = request.get("tool_input")
    if tool_input is None:
        tool_input = {}
    session_id = request.get("session_id")
    if not isinstance(session_id, str):
        session_id = ""
    cwd = request.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()

    if not isinstance(tool_name, str) or not tool_name:
        _emit_deny("tool_name が不正または欠落している")
        return
    if not isinstance(tool_input, dict):
        _emit_deny("tool_input が dict ではない")
        return

    call_id = _extract_call_id(request)

    risk_mod = _load_generated_module("risk")
    if risk_mod is None:
        _emit_deny(
            "risk classifier (hh_hooks/risk.py) が見つからない。"
            "scripts/sync_hook_modules.py を実行したか確認してください"
        )
        return

    risk_obj = risk_mod.classify(tool_name, tool_input)

    if risk_obj.level == "LOW":
        _emit_allow()
        return

    if risk_obj.level == "MEDIUM":
        _handle_medium(risk_obj, tool_name, tool_input, session_id, cwd, call_id)
        _emit_allow()
        return

    if risk_obj.level != "HIGH":
        _emit_deny(f"risk.classify() が未知のレベルを返した: {risk_obj.level!r}")
        return

    # --- HIGH ---
    bypass_reason = _check_bypass()
    if bypass_reason is not None:
        sys.stderr.write(
            f"{_BLOCK_MESSAGE_PREFIX}BYPASS ACTIVE - approval gate disabled ({bypass_reason})\n"
        )
        _log_bypass_usage(tool_name, risk_obj.level, risk_obj.rule_id)
        _emit_allow()
        return

    if risk_obj.rule_id == "unknown_tool":
        _emit_deny(f"未知のツール '{tool_name}' は Phase1a では常時拒否する（unknown_tool）")
        return

    category = risk_mod._alias_lookup(tool_name)  # noqa: SLF001 — risk.py に公開APIが無いため。ファイル docstring 判断4参照

    if category == "shell":
        deny_reason = _handle_high_shell(risk_obj, tool_name, tool_input, session_id, cwd, call_id, deadline_monotonic)
        if deny_reason is None:
            _emit_allow()
        else:
            _emit_deny(deny_reason)
        return

    if category in ("write", "edit", "notebook"):
        _emit_deny(
            "Write/Edit/NotebookEdit の HIGH リスク操作は Phase1a では承認フローへ進まず常に拒否する "
            "(TOCTOU 再検証を実行主体側で行えないための既知の縮退。"
            "docs/hh-agent/03_Architecture.md §4.3 参照)"
        )
        return

    _emit_deny(f"未対応のツールカテゴリ '{category}' (risk_rules.yaml の設定ミスの可能性)")


def main() -> None:
    start_monotonic = time.monotonic()
    try:
        _run(start_monotonic)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — フェイルクローズの絶対原則。
        # 例外メッセージに秘密値が含まれないよう、型名のみを含める
        # （トークン・署名鍵を扱う箇所は個別に AgentTokenError 等へ変換済みで、
        #  生の値が例外オブジェクトに残らないようにしている）。
        _emit_deny(f"内部エラーによりフェイルクローズ: {type(exc).__name__}")


if __name__ == "__main__":
    main()
