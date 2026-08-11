"""親設計書 §9「既知の落とし穴」24 項目の回帰テスト。

§9 は「実装者は必読」と銘打たれた既知バグの一覧である。1 項目 1 テスト
以上を置き、**踏み直したら赤くなる**ようにする。既に他ファイルで詳細に
検証している項目はここでは所在を示すポインタ 1 本に留め、重複した検証は
書かない（テストの二重メンテを避ける）。

Hermes 本体（`tools/approval.py` / `agent/shell_hooks.py`）を実際に import
して契約を確認する項目があるため、このファイルはリポジトリルートからの
実行を前提とする。
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def hh_sources() -> dict[str, str]:
    """H-H Agent が所有する Python ソース（テスト自身は除く）。"""
    out = {}
    for base in ("modal_hub", "hh_hooks"):
        for path in sorted((REPO_ROOT / base).rglob("*.py")):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            out[str(path.relative_to(REPO_ROOT)).replace("\\", "/")] = path.read_text(encoding="utf-8")
    return out


def code_only(source: str) -> str:
    """docstring と `#` コメントを除いたコード本文。

    §9 の各項目は解説として docstring に必ず登場するので、素の文字列検索は
    ほぼ確実に誤検出する。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body.pop(0)
    return ast.unparse(tree)


# ===========================================================================
# 1. env_type が "modal" だと危険コマンド承認が丸ごとスキップされる（D-14）
# ===========================================================================


def test_hermes_skips_container_guards_for_modal_env_type() -> None:
    """この落とし穴が**実在する**ことを Hermes 本体の関数で確認する。

    仮定が変わった（Hermes 側が挙動を変えた）ことにも気付けるようにする。
    """
    from tools.approval import _should_skip_container_guards

    for env_type in ("modal", "singularity", "daytona", "vercel_sandbox"):
        assert _should_skip_container_guards(env_type) is True, env_type
    assert _should_skip_container_guards("local") is False


def test_no_hh_agent_code_sets_env_type_to_a_guard_skipping_value() -> None:
    """D-14: Modal 上で動かすからといって `env_type="modal"` にしない。

    承認ガードが無言で丸ごと無効化される。
    """
    dangerous = ("modal", "singularity", "daytona", "vercel_sandbox")
    offenders = []
    for rel, source in hh_sources().items():
        body = code_only(source)
        for match in re.finditer(r"env_type\s*=\s*[\"']([^\"']+)[\"']", body):
            if match.group(1) in dangerous:
                offenders.append(f"{rel}: {match.group(0)}")
        for match in re.finditer(r"[\"']env_type[\"']\s*:\s*[\"']([^\"']+)[\"']", body):
            if match.group(1) in dangerous:
                offenders.append(f"{rel}: {match.group(0)}")
    assert offenders == [], f"承認ガードを無効化する env_type を設定している: {offenders}"


# ===========================================================================
# 2 / 17 / 18. Hermes シェルフックの設定（D-15 / D-20）
# ===========================================================================


def install_yaml_blocks() -> list[str]:
    text = (REPO_ROOT / "hh_hooks" / "INSTALL.md").read_text(encoding="utf-8")
    return re.findall(r"```ya?ml\n([\s\S]*?)```", text)


def test_install_doc_documents_fail_closed_true() -> None:
    """§9-2 / D-15: Hermes のシェルフックは既定で fail-open。

    `fail_closed: true` を明示しないと、フックが落ちた瞬間に全部素通りになる。
    """
    blocks = install_yaml_blocks()
    assert blocks, "INSTALL.md に YAML ブロックが無い"
    pre_tool_blocks = [b for b in blocks if "pre_tool_call" in b]
    assert pre_tool_blocks, "pre_tool_call の設定例が無い"
    for block in pre_tool_blocks:
        assert re.search(r"fail_closed:\s*true", block), f"fail_closed: true が無い:\n{block}"


def test_install_doc_documents_hooks_auto_accept() -> None:
    """§9-18 / D-20: 許可リスト未登録だと**警告ログだけ出して素通り**する。

    Modal 上は TTY が無いため確実に踏む。
    """
    blocks = [b for b in install_yaml_blocks() if "pre_tool_call" in b]
    assert any(re.search(r"hooks_auto_accept:\s*true", b) for b in blocks)


def test_install_doc_hook_timeout_exceeds_the_internal_deadline() -> None:
    """D-13: ホストタイムアウト 200 秒 > 内部デッドライン 170 秒。"""
    blocks = [b for b in install_yaml_blocks() if "pre_tool_call" in b]
    timeouts = [int(m) for b in blocks for m in re.findall(r"timeout:\s*(\d+)", b)]
    assert 200 in timeouts, f"pre_tool_call の timeout が 200 でない: {timeouts}"


def test_documented_hooks_block_is_a_dict_and_parses_to_the_expected_count() -> None:
    """§9-17: `hooks:` は**辞書**。リストで書くとフック 0 件になり警告すら出ない。

    `_parse_hooks_block()` は `isinstance(hooks_cfg, dict)` でない入力に対して
    エラーも警告も出さず空リストを返す。目視確認では検出できないので、
    INSTALL.md に書かれた設定を **実際にパーサへ通す**。
    """
    import yaml

    from agent.shell_hooks import _parse_hooks_block

    blocks = [b for b in install_yaml_blocks() if "pre_tool_call" in b]
    assert blocks
    parsed_any = False
    for block in blocks:
        config = yaml.safe_load(block)
        if not isinstance(config, dict) or "hooks" not in config:
            continue
        hooks_cfg = config["hooks"]
        assert isinstance(hooks_cfg, dict), "hooks: がリストで書かれている（フック 0 件になる）"
        specs = _parse_hooks_block(hooks_cfg)
        assert len(specs) >= 1, "設定を通したのにフックが 0 件登録された"
        parsed_any = True
    assert parsed_any, "INSTALL.md の hooks 設定をパーサへ通せなかった"


def test_list_shaped_hooks_block_silently_yields_zero_hooks() -> None:
    """落とし穴 17 そのものの再現。**警告すら出ない**ことを固定する。"""
    from agent.shell_hooks import _parse_hooks_block

    as_list = [{"event": "pre_tool_call", "command": "python tool_gate.py"}]
    assert _parse_hooks_block(as_list) == []


# ===========================================================================
# 3. set_approval_callback() はスレッドローカル
# ===========================================================================


def test_no_hh_agent_code_relies_on_set_approval_callback() -> None:
    """§9-3: 起動スレッドで登録しても実行ワーカースレッドでは `None`。

    さらにコールバックには redact 済みコマンドしか渡らず、cwd・差分・
    session_id を受け取れない。承認は必ずシェルフック経由で行う。
    """
    offenders = [rel for rel, src in hh_sources().items() if "set_approval_callback" in code_only(src)]
    assert offenders == [], f"set_approval_callback に依存している: {offenders}"


# ===========================================================================
# 4. detect_dangerous_command() はタプルを返す
# ===========================================================================


def test_hermes_detector_really_returns_a_three_tuple() -> None:
    from tools.approval import detect_dangerous_command

    result = detect_dangerous_command("echo hello")
    assert isinstance(result, tuple) and len(result) == 3
    assert result[0] is False
    assert bool(result) is True, "(False, None, None) は truthy。真偽判定してはいけない"


def test_detector_is_never_used_in_a_boolean_context() -> None:
    """`if detect_dangerous_command(cmd):` と書くと全コマンドが HIGH になる。"""
    for rel, source in hh_sources().items():
        body = code_only(source)
        assert not re.search(r"if\s+detect_dangerous_command\s*\(", body), rel
        assert not re.search(r"if\s+\w*detect_dangerous\w*\s*\(\s*\w+\s*\)\s*:", body), rel


# 詳細な回帰は test_risk.py::test_detector_returns_three_tuple_and_false_is_not_truthy。


# ===========================================================================
# 5 / 6 / 19. Phase 1b の落とし穴（Phase 1a では作らないことの確認）
# ===========================================================================


def test_no_code_writes_directly_into_hermes_scanned_skill_directory() -> None:
    """§9-5 / §9-19 / D-16: Hermes はスキルディレクトリを自動スキャンする。

    抽出物を探索パスへ**直接**書くと、注入された指示が次回以降の全セッション
    へ自動注入される**永続的バックドア**になる。

    Phase 1a はスキルを一切扱わないので、当初は `"SKILL.md"` という文字列
    自体の不在をもって検証していた。**Phase 1b（`services/skill_quarantine.py`
    ／`routers/skills.py`）はこの文字列を正当に扱う** —
    隔離領域 `~/.hh-agent/skills_quarantine/` と Volume 上の同名パスは、
    どちらも Hermes が探索するディレクトリの**外**にある（D-16 が要求する
    まさにその隔離）。したがって不変条件として意味を持つのは
    `".hermes/skills"`（Hermes が実際にスキャンする探索パスそのもの）への
    直接参照が無いことであり、`"SKILL.md"` という語の存在ではない。
    `scripts/hh_skill_promote.py`（本テストのスコープ外。`hh_sources()` は
    `modal_hub`/`hh_hooks` のみを見る）が human-confirmed の唯一の書き込み
    経路であることは `test_skill_promote.py` 側で担保する。
    """
    for rel, source in hh_sources().items():
        body = code_only(source)
        assert ".hermes/skills" not in body, rel


def test_no_phase1a_code_reads_per_session_jsonl() -> None:
    """§9-6: Hermes は per-session JSONL を持たない。履歴は SQLite の SessionDB。"""
    for rel, source in hh_sources().items():
        body = code_only(source)
        assert "sessions/" not in body, rel


def test_no_phase1a_code_touches_the_obsidian_vault() -> None:
    """親設計書 §2「絶対原則」/ D-12: Modal から Obsidian へのパスは存在しない。"""
    for rel, source in hh_sources().items():
        body = code_only(source)
        for marker in ("Obsidian", "obsidian", "マイドライブ", "H-H-Agent/"):
            assert marker not in body, f"{rel} が Obsidian を参照している: {marker}"


# ===========================================================================
# 7. modal.Dict に compare-and-set は無い
# ===========================================================================


def test_no_compare_and_set_usage() -> None:
    """原子的なのは `put(skip_if_exists=True)` だけ。"""
    for rel, source in hh_sources().items():
        body = code_only(source)
        for marker in ("compare_and_set", "compare_and_swap", ".cas(", "check_and_set"):
            assert marker not in body, f"{rel} に {marker}"


def test_every_dict_put_in_the_store_uses_skip_if_exists() -> None:
    from modal_hub.core import store

    tree = ast.parse(inspect.getsource(store))
    puts = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "put"
    ]
    assert puts, "store.py に put 呼び出しが無い（テスト側の想定違い）"
    for call in puts:
        kwargs = {kw.arg for kw in call.keywords}
        assert "skip_if_exists" in kwargs, ast.unparse(call)


# 詳細は test_store.py。


# ===========================================================================
# 8. Volume への複数コンテナ同時追記は行が消える
# ===========================================================================


def test_no_append_mode_writes_to_the_volume() -> None:
    """D-11 / §5.3: 1 イベント 1 ファイルの不変 JSON。共有 JSONL への追記は禁止。

    ローカルのバイパス監査（`~/.hh-agent/bypass_audit.log`）は Hub とは
    独立した単一ホスト上のファイルなので対象外。
    """
    for rel, source in hh_sources().items():
        if rel.startswith("hh_hooks/"):
            continue  # ローカルファイルのみ扱う
        body = code_only(source)
        assert not re.search(r"open\([^)]*[\"']a[b+]*[\"']", body), f"{rel} が追記モードで開いている"
        assert ".jsonl" not in body, rel


# ===========================================================================
# 9. FastAPI の遅延型注釈 → 全リクエスト 422
# ===========================================================================


def test_fastapi_reflected_types_are_imported_at_module_scope() -> None:
    """`Request` / `WebSocket` はハンドラ本体ではなくモジュールスコープで import。"""
    from modal_hub.routers import approval_gate

    tree = ast.parse(inspect.getsource(approval_gate))
    module_level = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert {"Request", "WebSocket"} <= module_level


# ルート解決の実地検証は test_routes_and_app.py。


# ===========================================================================
# 10. Modal Secret の更新は再デプロイが要る
# ===========================================================================


def test_secrets_are_read_per_call_not_cached_at_import() -> None:
    """import 時にキャッシュすると、再デプロイしても古い値が残り続ける。"""
    from modal_hub.core import config

    tree = ast.parse(inspect.getsource(config))
    module_assignments = [
        ast.unparse(n)
        for n in tree.body
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and "environ" in ast.unparse(n)
    ]
    assert module_assignments == [], f"モジュールスコープで環境変数を読んでいる: {module_assignments}"
    assert not hasattr(config, "_CACHE")


# ===========================================================================
# 14. PowerShell/cmd の日本語リテラルは文字化けする
# ===========================================================================


def test_every_text_file_operation_specifies_an_encoding() -> None:
    """ruff PLW1514 と同趣旨。Windows では既定が locale 依存で内容が壊れる。"""
    offenders = []
    for rel, source in hh_sources().items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name not in ("open", "read_text", "write_text"):
                continue
            rendered = ast.unparse(node)
            if "encoding" in rendered or '"wb"' in rendered or '"rb"' in rendered or "'b'" in rendered:
                continue
            if name == "open" and len(node.args) > 1 and "b" in ast.unparse(node.args[1]):
                continue
            offenders.append(f"{rel}: {rendered[:90]}")
    assert offenders == [], f"encoding 未指定のテキスト I/O: {offenders}"


def test_tool_gate_forces_utf8_on_the_wire_protocol() -> None:
    """cp932 のまま block JSON を書くと、拒否が相手に伝わらない。"""
    source = (REPO_ROOT / "hh_hooks" / "tool_gate.py").read_text(encoding="utf-8")
    assert 'reconfigure(encoding="utf-8"' in source


def test_all_json_dumps_on_the_wire_use_ensure_ascii_false() -> None:
    """日本語の理由文字列がエスケープで膨れないようにする（表示崩れ防止）。"""
    from hh_hooks import tool_gate  # noqa: F401  — パッケージ import 可否も兼ねる確認

    source = (REPO_ROOT / "hh_hooks" / "tool_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "dumps"
        ):
            assert "ensure_ascii=False" in ast.unparse(node), ast.unparse(node)


# ===========================================================================
# 15. 黙って空を返す実装は原因を隠す
# ===========================================================================


def test_no_swallowed_exception_returns_a_success_looking_value() -> None:
    """例外を握りつぶして「成功に見える値」を返す経路が無いこと。

    フェイルクローズの観点では、`None` / `[]` / `{}` を返すこと自体は
    問題ではない — 呼び出し側がそれを **deny 側** として扱うなら正しい
    （例: `_load_generated_module` の `None` は「モジュールが無い → deny」、
    `_check_bypass` の `None` は「バイパス無効 → ゲートを通す」）。
    禁止したいのは **True / "ok" / 空でない成功値** を返して素通りさせる形。
    """
    offenders = []
    for rel, source in hh_sources().items():
        tree = ast.parse(source)
        for handler in [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]:
            for node in ast.walk(handler):
                if not isinstance(node, ast.Return) or node.value is None:
                    continue
                rendered = ast.unparse(node.value)
                if rendered in ("True", '"ok"', "'ok'", '"allow"', "'allow'", '"sent"', "'sent'"):
                    offenders.append(f"{rel}: {ast.unparse(node)}")
    assert offenders == [], f"例外を握りつぶして成功値を返している: {offenders}"


def test_audit_logs_when_a_volume_write_is_swallowed() -> None:
    from modal_hub.services import audit

    tree = ast.parse(inspect.getsource(audit))
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "audit.py に except ハンドラが無い（テスト側の想定違い）"
    for handler in handlers:
        body = ast.unparse(handler)
        assert "logger" in body or "log" in body, body


# ===========================================================================
# 16. git push は Codex 経由（グローバルルール）
# ===========================================================================


def test_no_hh_agent_code_invokes_git_push() -> None:
    for rel, source in hh_sources().items():
        body = code_only(source)
        assert "git push" not in body and '"push"' not in body, rel


def test_git_push_is_classified_high_so_the_rule_is_enforced_at_runtime() -> None:
    """ルールをドキュメントだけに置かず、ゲートで実際に止める。"""
    from modal_hub.core import risk

    assert risk.classify("Bash", {"command": "git push origin main"}).level == "HIGH"


# ===========================================================================
# 20 / 21. Phase 1c の落とし穴（Phase 1a に痕跡が無いこと）
# ===========================================================================


def test_no_reliance_on_hermes_serve_for_a_browser_ui() -> None:
    """§9-20 / D-18: `serve` は headless で SPA を配信しない。"""
    for rel, source in hh_sources().items():
        body = code_only(source)
        assert "hermes serve" not in body, rel


# min_containers / session affinity は test_routes_and_app.py。


# ===========================================================================
# 22. modal.Dict の len() と全走査
# ===========================================================================


def test_no_len_or_iteration_over_the_approvals_dict() -> None:
    """一覧はインデックスキーから引く（`pending:index`）。

    対象は `modal.Dict` ハンドルに対する操作のみ。素の Python dict への
    `.items()` は無関係なので、`_approvals_dict()` の戻り値を受ける変数
    （store.py の `d`）とハンドル取得式そのものを見る。
    """
    from modal_hub.core import store

    tree = ast.parse(inspect.getsource(store))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = ast.unparse(node.func.value)
            if target in ("d", "_approvals_dict()"):
                assert node.func.attr in ("contains", "put", "pop"), ast.unparse(node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
            if ast.unparse(node) in ("len(d)", "len(_approvals_dict())"):
                offenders.append(ast.unparse(node))
        if isinstance(node, ast.For) and ast.unparse(node.iter) in ("d", "_approvals_dict()"):
            offenders.append(ast.unparse(node.iter))
    assert offenders == [], f"modal.Dict を走査している: {offenders}"


def test_routers_never_grab_a_raw_modal_dict_for_state_keys() -> None:
    """`approval_gate.py` の生 `modal.Dict` アクセスは index キー専用。

    ファイル冒頭 docstring が宣言している「`pending:index` / `gc:index:` に
    限る」が守られているか、`overwrite` の呼び出し先キーで確認する。
    """
    from modal_hub.routers import approval_gate

    tree = ast.parse(inspect.getsource(approval_gate))
    # `modal.Dict.from_name(...)` が現れてよいのは `overwrite` の中だけ。
    raw_dict_users = {
        f.name
        for f in ast.walk(tree)
        if isinstance(f, ast.FunctionDef) and "modal.Dict.from_name" in ast.unparse(f)
    }
    assert raw_dict_users <= {"overwrite"}, f"生 modal.Dict を触る関数: {raw_dict_users}"

    # `overwrite` が呼ばれるのは _index_add / _index_remove の中だけ。
    callers = {
        f.name
        for f in ast.walk(tree)
        if isinstance(f, ast.FunctionDef)
        and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "overwrite"
            for n in ast.walk(f)
        )
    }
    assert callers <= {"_index_add", "_index_remove", "overwrite"}, callers


def test_pending_listing_goes_through_an_index_key() -> None:
    from modal_hub.core import store
    from modal_hub.routers import approval_gate

    source = inspect.getsource(approval_gate._list_pending_items)
    assert "PREFIX_PENDING_INDEX" in source
    assert store.PREFIX_PENDING_INDEX == "pending:index"


# ===========================================================================
# 23 / 24. claim_deadline と symlink 差し替え
# ===========================================================================


def test_claim_deadline_exists_on_every_request_record() -> None:
    """§9-23: 承認は無期限に有効ではない。"""
    from modal_hub.tests.conftest import make_req

    req = make_req()
    assert "claim_deadline" in req and req["claim_deadline"] > req["grace_deadline"]


def test_verification_compares_more_than_the_four_weak_fields() -> None:
    """§9-24: `payload_sha256` + `cwd` + `HEAD` では symlink 差し替えを検出できない。

    realpath + lstat 識別子 + 内容ハッシュが要る。
    """
    from modal_hub.routers import approval_gate

    assert set(approval_gate._TARGET_KEYS) >= {"realpath", "identity", "preimage_sha256"}


# 実シナリオは test_approval_state_machine.py / test_approval_gate_http.py。


# ===========================================================================
# 横断: 秘密をログへ書かない
# ===========================================================================


def test_no_logging_call_interpolates_a_token_or_key() -> None:
    offenders = []
    for rel, source in hh_sources().items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in ("debug", "info", "warning", "error", "exception", "critical"):
                continue
            rendered = ast.unparse(node)
            if re.search(r"\b(token|signing_key|session_key|cookie_value|local_key|secret)\b", rendered):
                if "safe_repr" in rendered or "type(" in rendered:
                    continue
                offenders.append(f"{rel}: {rendered[:110]}")
    assert offenders == [], f"秘密をログへ書いている疑い: {offenders}"


def test_ntfy_topic_is_only_logged_through_safe_repr() -> None:
    from modal_hub.services import notifier

    tree = ast.parse(inspect.getsource(notifier))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("info", "warning", "error", "exception")
        ):
            rendered = ast.unparse(node)
            if "topic" in rendered:
                assert "safe_repr" in rendered, rendered


@pytest.mark.parametrize("base", ["modal_hub", "hh_hooks"])
def test_no_secret_material_is_committed(base: str) -> None:
    """§5.4: Secret の実値はコード・Git に一切書かない。"""
    patterns = [
        r"sk-ant-[A-Za-z0-9_-]{20,}",
        r"gh[pousr]_[A-Za-z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    ]
    for path in sorted((REPO_ROOT / base).rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in patterns:
            assert not re.search(pattern, text), f"{path} に秘密らしき値がある"
