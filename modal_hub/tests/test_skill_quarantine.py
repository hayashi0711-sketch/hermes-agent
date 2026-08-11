"""modal_hub/tests/test_skill_quarantine.py — skill_quarantine.py の不変条件テスト。

07_Phase1b_Spec.md §4.1（バージョニング・名前衝突・materialize の高々1回性）
と §0.1（D-16: Hermes が探索するどのディレクトリにも直接書かない）を
実ファイルシステム上で検証する。
"""

from __future__ import annotations

import json

import pytest

from modal_hub.services import skill_quarantine as sq


def _skill_md(name: str, description: str = "a test skill") -> str:
    return f"---\nname: {name}\ndescription: {description}\nversion: 0.1.0\n---\n\nBody text.\n"


@pytest.fixture(autouse=True)
def _isolate_hermes_scan_dirs(monkeypatch):
    """`assert_quarantine_root_is_safe()` が実ホームディレクトリの
    `config.yaml` を読みに行かないよう、デフォルトでは「候補ゼロ
    （衝突なし）」に固定する。個別テストで上書きする。
    """
    monkeypatch.setattr(sq, "_existing_hermes_scan_dirs", lambda: [])
    monkeypatch.setattr(sq, "_declared_hermes_scan_dirs_including_nonexistent", lambda: [])


# ---------------------------------------------------------------------------
# validate_skill_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["ok", "ok-name", "a1-b2-c3", "x" * 49],
)
def test_validate_skill_name_accepts_valid(name):
    assert sq.validate_skill_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "Upper", "-leading-dash", "has space", "has_underscore", "x" * 50, "../escape"],
)
def test_validate_skill_name_rejects_invalid(name):
    with pytest.raises(sq.QuarantineError):
        sq.validate_skill_name(name)


# ---------------------------------------------------------------------------
# parse_frontmatter_name
# ---------------------------------------------------------------------------


def test_parse_frontmatter_name_extracts_name():
    assert sq.parse_frontmatter_name(_skill_md("my-skill")) == "my-skill"


def test_parse_frontmatter_name_returns_none_for_no_frontmatter():
    assert sq.parse_frontmatter_name("just some text") is None


def test_parse_frontmatter_name_returns_none_for_broken_yaml():
    assert sq.parse_frontmatter_name("---\n:::not yaml:::\n---\nbody") is None


# ---------------------------------------------------------------------------
# materialize: 新規保存
# ---------------------------------------------------------------------------


def test_materialize_writes_skill_md_under_quarantine_root(tmp_path):
    content = _skill_md("alpha-skill")
    result = sq.materialize("qid-1", "alpha-skill", content, base=tmp_path)

    assert result.name == "alpha-skill"
    output = tmp_path / "skills_quarantine" / "alpha-skill" / "SKILL.md"
    assert output.is_file()
    assert output.read_text(encoding="utf-8") == content
    assert result.output_path == str(output)


def test_materialize_rejects_invalid_base_name(tmp_path):
    with pytest.raises(sq.QuarantineError):
        sq.materialize("qid-1", "Invalid Name", _skill_md("x"), base=tmp_path)


def test_materialize_rejects_empty_content(tmp_path):
    with pytest.raises(sq.QuarantineError):
        sq.materialize("qid-1", "alpha", "   ", base=tmp_path)


# ---------------------------------------------------------------------------
# materialize: 高々1回性（同一 queue_entry_id の再呼び出し）
# ---------------------------------------------------------------------------


def test_materialize_is_idempotent_for_same_queue_entry_id(tmp_path):
    content = _skill_md("beta-skill")
    first = sq.materialize("qid-2", "beta-skill", content, base=tmp_path)
    output = tmp_path / "skills_quarantine" / "beta-skill" / "SKILL.md"
    first_mtime = output.stat().st_mtime_ns

    second = sq.materialize("qid-2", "beta-skill", content, base=tmp_path)

    assert second == first
    assert output.stat().st_mtime_ns == first_mtime  # 再書き込みされていない


# ---------------------------------------------------------------------------
# 名前衝突: 新しい内容の方が -2 へ退避する
# ---------------------------------------------------------------------------


def test_materialize_name_collision_gets_suffix(tmp_path):
    first = sq.materialize("qid-a", "gamma-skill", _skill_md("gamma-skill", "first"), base=tmp_path)
    second = sq.materialize(
        "qid-b", "gamma-skill", _skill_md("gamma-skill", "second"), base=tmp_path
    )

    assert first.name == "gamma-skill"
    assert second.name == "gamma-skill-2"
    # 既存(first)の内容は上書きされていない。
    original = (tmp_path / "skills_quarantine" / "gamma-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "first" in original


def test_materialize_collision_exhaustion_raises(tmp_path):
    base_name = "delta-skill"
    sq.materialize("qid-0", base_name, _skill_md(base_name, "v0"), base=tmp_path)
    for i in range(2, 10):
        sq.materialize(f"qid-{i}", base_name, _skill_md(f"{base_name}-{i}", f"v{i}"), base=tmp_path)

    with pytest.raises(sq.QuarantineError):
        sq.materialize("qid-overflow", base_name, _skill_md(f"{base_name}-x", "overflow"), base=tmp_path)


def test_materialize_truncates_base_name_over_47_chars(tmp_path):
    long_name = "a" * 48  # NAME_RE 上限(49)には収まるが 47 を超える
    content = _skill_md(long_name)
    sq.materialize("qid-1", long_name, content, base=tmp_path)
    sq.materialize("qid-2", long_name, _skill_md(long_name, "second"), base=tmp_path)

    truncated = long_name[:47]
    assert (tmp_path / "skills_quarantine" / f"{truncated}-2" / "SKILL.md").is_file()


# ---------------------------------------------------------------------------
# クラッシュ回復
# ---------------------------------------------------------------------------


def test_materialize_recovers_from_crash_between_write_and_finalize(tmp_path):
    content = _skill_md("epsilon-skill")
    root = sq.quarantine_root(tmp_path)
    (root / "epsilon-skill").mkdir(parents=True)
    (root / "epsilon-skill" / "SKILL.md").write_text(content, encoding="utf-8")

    materializing_dir = tmp_path / "skills_quarantine" / ".materializing"
    materializing_dir.mkdir(parents=True)
    (materializing_dir / "qid-crash.json").write_text(
        json.dumps({"name": "epsilon-skill"}), encoding="utf-8"
    )

    result = sq.materialize("qid-crash", "epsilon-skill", content, base=tmp_path)
    assert result.name == "epsilon-skill"
    # 確定記録が作られ、.materializing は片付けられている。
    assert (tmp_path / "skills_quarantine" / ".materialized" / "qid-crash.json").is_file()
    assert not (materializing_dir / "qid-crash.json").exists()


def test_materialize_refuses_inconsistent_crash_state(tmp_path):
    root = sq.quarantine_root(tmp_path)
    (root / "zeta-skill").mkdir(parents=True)
    (root / "zeta-skill" / "SKILL.md").write_text(_skill_md("zeta-skill", "on-disk"), encoding="utf-8")

    materializing_dir = tmp_path / "skills_quarantine" / ".materializing"
    materializing_dir.mkdir(parents=True)
    (materializing_dir / "qid-mismatch.json").write_text(
        json.dumps({"name": "zeta-skill"}), encoding="utf-8"
    )

    with pytest.raises(sq.QuarantineError):
        sq.materialize("qid-mismatch", "zeta-skill", _skill_md("zeta-skill", "different-content"), base=tmp_path)


# ---------------------------------------------------------------------------
# list_quarantined_skill_headers
# ---------------------------------------------------------------------------


def test_list_quarantined_skill_headers(tmp_path):
    sq.materialize("qid-1", "eta-skill", _skill_md("eta-skill", "desc-eta"), base=tmp_path)
    sq.materialize("qid-2", "theta-skill", _skill_md("theta-skill", "desc-theta"), base=tmp_path)

    headers = sq.list_quarantined_skill_headers(base=tmp_path)
    assert ("eta-skill", "desc-eta") in headers
    assert ("theta-skill", "desc-theta") in headers


def test_list_quarantined_skill_headers_empty_when_no_root(tmp_path):
    assert sq.list_quarantined_skill_headers(base=tmp_path) == []


def test_list_quarantined_skill_headers_skips_hidden_dirs(tmp_path):
    sq.materialize("qid-1", "iota-skill", _skill_md("iota-skill"), base=tmp_path)
    headers = sq.list_quarantined_skill_headers(base=tmp_path)
    names = [n for n, _ in headers]
    assert ".materializing" not in names
    assert ".materialized" not in names


# ---------------------------------------------------------------------------
# queue_entry_id の検証（2026-08-11 Codex レビュー Medium 対応）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "queue_entry_id",
    ["../../escape", "has space", "", "a" * 65, "s" + "/" * 3 + "etc"],
)
def test_materialize_rejects_invalid_queue_entry_id(tmp_path, queue_entry_id):
    with pytest.raises(sq.QuarantineError):
        sq.materialize(queue_entry_id, "kappa-skill", _skill_md("kappa-skill"), base=tmp_path)


# ---------------------------------------------------------------------------
# D-16: 隔離領域が Hermes の実スキャン対象と重ならないこと
# ---------------------------------------------------------------------------


def test_materialize_rejects_when_quarantine_root_overlaps_scan_dir(tmp_path, monkeypatch):
    root = sq.quarantine_root(tmp_path)
    monkeypatch.setattr(sq, "_existing_hermes_scan_dirs", lambda: [root.resolve()])
    with pytest.raises(sq.QuarantineError):
        sq.materialize("qid-1", "lambda-skill", _skill_md("lambda-skill"), base=tmp_path)


def test_materialize_rejects_when_quarantine_root_is_ancestor_of_scan_dir(tmp_path, monkeypatch):
    root = sq.quarantine_root(tmp_path)
    monkeypatch.setattr(
        sq, "_declared_hermes_scan_dirs_including_nonexistent", lambda: [root.resolve() / "nested"]
    )
    with pytest.raises(sq.QuarantineError):
        sq.materialize("qid-1", "mu-skill", _skill_md("mu-skill"), base=tmp_path)


def test_materialize_succeeds_when_no_overlap(tmp_path):
    # autouse フィクスチャが既に候補ゼロにしている。念のため明示的に確認。
    result = sq.materialize("qid-1", "nu-skill", _skill_md("nu-skill"), base=tmp_path)
    assert result.name == "nu-skill"


# ---------------------------------------------------------------------------
# クラッシュ回復: intent だけあって SKILL.md 本体が無い場合は再開できる
# （2026-08-11 Codex レビュー Medium 対応。旧実装は永続的に失敗していた）
# ---------------------------------------------------------------------------


def test_materialize_resumes_after_crash_between_intent_and_write(tmp_path):
    materializing_dir = tmp_path / "skills_quarantine" / ".materializing"
    materializing_dir.mkdir(parents=True)
    (materializing_dir / "qid-resume.json").write_text(
        json.dumps({"name": "xi-skill", "started_at": 0}), encoding="utf-8"
    )
    # SKILL.md 本体はまだ存在しない(クラッシュでintentだけ書けた状態を再現)。

    result = sq.materialize("qid-resume", "xi-skill", _skill_md("xi-skill"), base=tmp_path)

    assert result.name == "xi-skill"
    assert (tmp_path / "skills_quarantine" / "xi-skill" / "SKILL.md").is_file()
    assert (tmp_path / "skills_quarantine" / ".materialized" / "qid-resume.json").is_file()
    assert not (materializing_dir / "qid-resume.json").exists()


# ---------------------------------------------------------------------------
# 衝突予約の原子性（2026-08-11 Codex レビュー Medium 対応）
# ---------------------------------------------------------------------------


def test_reservation_marker_prevents_two_queue_ids_from_taking_the_same_name(tmp_path):
    root = sq.quarantine_root(tmp_path)
    # omicron-skill を先に予約済みの状態を人為的に再現する。
    materializing_dir = root / ".materializing"
    materializing_dir.mkdir(parents=True)
    (materializing_dir / "name-omicron-skill.reserved").touch()

    # _reserve_and_find_available_name は予約済みの候補をスキップして
    # 次の枠(-2)を返すはずである。
    resolved = sq._reserve_and_find_available_name(root, "omicron-skill", base=tmp_path)
    assert resolved == "omicron-skill-2"
