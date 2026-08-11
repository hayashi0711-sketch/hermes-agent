"""PWA の機械的検査 — `06_PWA_Design.md` §6「禁止事項（テストで機械的に検査される）」
および `05_Phase1a_Spec.md` §9.2 が名指しで要求するテスト。

    1. innerHTML / insertAdjacentHTML / outerHTML — 例外なし禁止
    2. eval / new Function / setTimeout(文字列)
    3. 外部ホストへの参照（CDN・Web フォント・外部画像・外部アイコン）
    4. インライン <script> / <style>
    5. href / src へのユーザー由来値の埋め込み
    6. 背景の点滅・明滅アニメーション

**コメントと文字列リテラルを剥がしてから走査する。** 「`innerHTML` 禁止」と
書いた説明コメント自体が検査に引っかかると、実装者はコメントを消すという
無意味な対処に追い込まれ、検査の信頼性が落ちる。
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.usefixtures("pwa_dir")


# ---------------------------------------------------------------------------
# コメント除去（正規表現による近似。JS の正規表現リテラル内の `//` を誤検出
# しうるが、本 PWA は正規表現リテラルを使わないため実用上問題ない）
# ---------------------------------------------------------------------------


def strip_js_comments(source: str) -> str:
    source = re.sub(r"/\*[\s\S]*?\*/", "", source)
    source = re.sub(r"(?m)^\s*//.*$", "", source)
    source = re.sub(r"(?<![:\w])//[^\n\"']*$", "", source, flags=re.MULTILINE)
    return source


def strip_css_comments(source: str) -> str:
    return re.sub(r"/\*[\s\S]*?\*/", "", source)


def strip_html_comments(source: str) -> str:
    return re.sub(r"<!--[\s\S]*?-->", "", source)


@pytest.fixture(scope="module")
def js_files(pwa_dir):
    files = sorted(pwa_dir.glob("*.js"))
    assert files, f"{pwa_dir} に .js が無い（検査対象が空だとテストが常に緑になる）"
    return {p.name: strip_js_comments(p.read_text(encoding="utf-8")) for p in files}


@pytest.fixture(scope="module")
def html_source(pwa_dir):
    path = pwa_dir / "index.html"
    assert path.is_file()
    return strip_html_comments(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def css_source(pwa_dir):
    path = pwa_dir / "style.css"
    assert path.is_file()
    return strip_css_comments(path.read_text(encoding="utf-8"))


# ===========================================================================
# 1. innerHTML / insertAdjacentHTML / outerHTML（例外なし禁止）
# ===========================================================================

_HTML_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


def test_no_html_injection_sinks(js_files) -> None:
    """§9.2: コマンド全文・パス・差分は「攻撃者が内容を決められる文字列」。

    エージェントが実行しようとしているコマンドは、そのエージェントを騙した
    誰かが書いたかもしれない。テキストの挿入は `textContent` のみ。
    """
    offenders = [
        f"{name}:{sink}"
        for name, source in js_files.items()
        for sink in _HTML_SINKS
        if sink in source
    ]
    assert offenders == [], f"HTML 注入シンクが使われている: {offenders}"


def test_text_is_inserted_via_textcontent(js_files) -> None:
    """禁止するだけでなく、代替手段が実際に使われていることを確認する。"""
    assert any("textContent" in source for source in js_files.values())


def test_elements_are_built_with_createelement(js_files) -> None:
    """§9.2: 差分の色付けは行ごとに createElement してクラスを付ける。"""
    assert any("createElement" in source for source in js_files.values())


# ===========================================================================
# 2. eval / new Function / setTimeout(文字列)
# ===========================================================================


def test_no_dynamic_code_evaluation(js_files) -> None:
    offenders = []
    for name, source in js_files.items():
        if re.search(r"\beval\s*\(", source):
            offenders.append(f"{name}:eval")
        if re.search(r"\bnew\s+Function\s*\(", source):
            offenders.append(f"{name}:new Function")
        if re.search(r"\bsetTimeout\s*\(\s*[\"'`]", source):
            offenders.append(f"{name}:setTimeout(string)")
        if re.search(r"\bsetInterval\s*\(\s*[\"'`]", source):
            offenders.append(f"{name}:setInterval(string)")
    assert offenders == [], f"動的コード評価が使われている: {offenders}"


# ===========================================================================
# 3. 外部ホストへの参照
# ===========================================================================

# XML 名前空間 URI はネットワーク参照ではない（SVG の `xmlns` 等）。
_XMLNS_ALLOWED = ("http://www.w3.org/",)


def external_refs(source: str) -> list[str]:
    return [
        url
        for url in re.findall(r"https?://[^\s\"'()<>]+", source)
        if not url.startswith(_XMLNS_ALLOWED)
    ]


def test_no_external_host_references_in_any_pwa_asset(pwa_dir) -> None:
    """§4.5: CDN・フォント・画像すべてインライン。外部ホストへ一切要求しない。"""
    offenders = {}
    for path in sorted(pwa_dir.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".js":
            text = strip_js_comments(text)
        elif path.suffix == ".css":
            text = strip_css_comments(text)
        elif path.suffix in (".html", ".htm"):
            text = strip_html_comments(text)
        refs = external_refs(text)
        if refs:
            offenders[path.name] = refs
    assert offenders == {}, f"外部ホストへの参照がある: {offenders}"


def test_no_web_font_imports(css_source) -> None:
    """フォントはシステムフォントスタックのみ（06 §2。CSP 違反にもなる）。"""
    assert "@font-face" not in css_source
    assert "@import" not in css_source


def test_html_only_loads_local_assets(html_source) -> None:
    for attr, value in re.findall(r'\b(src|href)\s*=\s*"([^"]*)"', html_source):
        assert not value.startswith(("http://", "https://", "//")), f"{attr}={value}"


# ===========================================================================
# 4. インライン <script> / <style>
# ===========================================================================


def test_no_inline_script_or_style_blocks(html_source) -> None:
    """CSP に `'unsafe-inline'` を入れないため、インラインを書けない。"""
    inline_scripts = [
        m for m in re.findall(r"<script\b([^>]*)>([\s\S]*?)</script>", html_source) if m[1].strip()
    ]
    assert inline_scripts == [], "インライン <script> がある"
    inline_styles = [m for m in re.findall(r"<style\b[^>]*>([\s\S]*?)</style>", html_source) if m.strip()]
    assert inline_styles == [], "インライン <style> がある"


def test_no_inline_event_handler_attributes(html_source) -> None:
    """`onclick="..."` も CSP の `script-src 'self'` では実行できない。"""
    handlers = re.findall(r"\bon[a-z]+\s*=\s*[\"']", html_source)
    assert handlers == [], f"インラインイベントハンドラがある: {handlers}"


# ===========================================================================
# 5. href / src へのユーザー由来値の埋め込み
# ===========================================================================


def _dynamic_href_assignments(source: str) -> list[str]:
    hits = []
    for match in re.finditer(r"\.(href|src)\s*=\s*([^;\n]+)", source):
        if not re.fullmatch(r"[\"'][^\"']*[\"']", match.group(2).strip()):
            hits.append(match.group(0).strip())
    for match in re.finditer(r"setAttribute\(\s*[\"'](href|src)[\"']\s*,\s*([^)]+)\)", source):
        if not re.fullmatch(r"[\"'][^\"']*[\"']", match.group(2).strip()):
            hits.append(match.group(0).strip())
    return hits


def test_no_reachable_user_data_reaches_href_or_src(js_files) -> None:
    """§9.2 / 06 §6-5: `href`/`src` にユーザー由来の値を入れない。

    `href`/`src` への代入・`setAttribute` はすべてリテラル文字列でなければ
    ならない。例外は無い（BUG-8 の修正で、かつて例外扱いだった死んだ
    ヘルパー `safeSetHref` 自体が削除された）。
    """
    offenders = []
    for name, source in js_files.items():
        offenders += [f"{name}:{hit}" for hit in _dynamic_href_assignments(source)]
    assert offenders == [], f"href/src へ動的な値が到達しうる: {offenders}"


def test_safe_set_href_helper_is_not_reintroduced(js_files) -> None:
    """BUG-8（修正済み）の回帰ガード。

    `app.js` にはかつて `safeSetHref()` という、どこからも呼ばれていない
    死んだヘルパーが存在した。その許可リストは `^https?://` を通しており、
    06 §6 が禁止する「外部ホストへの参照」と「href/src へのユーザー由来値の
    埋め込み」を同時に破る実装だった（呼び出し側が無かったため実害はなかった
    が、将来誰かが呼び出せば即座に両方を破る）。ヘルパーは削除済み。この
    テストは、同じ名前・同種の許可リストを持つヘルパーが将来再導入されて
    いないことを確認する回帰ガードであり、以前のように「ヘルパーが無ければ
    何も検査せず通す」形にはしない。
    """
    for name, source in js_files.items():
        assert "safeSetHref" not in source, (
            f"{name}: safeSetHref が再導入されている。href/src へのユーザー由来値"
            "の埋め込みを許す許可リストを持ち込んでいないか確認すること（BUG-8）。"
        )


# ===========================================================================
# 6. 背景の点滅・明滅アニメーション（光過敏性発作の回避。06 §3.1）
# ===========================================================================


def test_no_blinking_or_flashing_animation(css_source) -> None:
    offenders = []
    if re.search(r"\banimation\b[^;]*\binfinite\b", css_source):
        offenders.append("animation: ... infinite")
    for name in re.findall(r"@keyframes\s+([\w-]+)", css_source):
        if re.search(r"blink|flash|pulse|strobe", name, re.IGNORECASE):
            offenders.append(f"@keyframes {name}")
    assert offenders == [], f"点滅アニメーションがある: {offenders}"


# ===========================================================================
# Service Worker（spec §9.3 / 06 §5）
# ===========================================================================


def test_service_worker_never_caches_api_responses(pwa_dir) -> None:
    """§9.3: `/api/*` の応答を絶対にキャッシュしない。

    承認内容がオフラインキャッシュに残ると、認証を失った端末からも見える。
    """
    sw = strip_js_comments((pwa_dir / "sw.js").read_text(encoding="utf-8"))
    assert "/api/" in sw, "SW が /api/ を明示的に除外していない（意図が読めない）"
    # `cache.put` / `cache.add` の対象に /api/ が混じっていないこと。
    for match in re.finditer(r"\bcache\.(put|add|addAll)\s*\(([^)]*)\)", sw):
        assert "/api/" not in match.group(2), f"SW が API 応答をキャッシュしている: {match.group(0)}"


def test_service_worker_does_not_implement_approval_logic(pwa_dir) -> None:
    """§9.3: 承認処理を SW 内に実装しない。"""
    sw = strip_js_comments((pwa_dir / "sw.js").read_text(encoding="utf-8"))
    for forbidden in ("/api/approval/respond", "approve", "reject"):
        assert forbidden not in sw, f"SW に承認ロジックの痕跡がある: {forbidden}"


def test_service_worker_caches_only_the_static_shell(pwa_dir) -> None:
    """§9.3 / 06 §5: キャッシュ対象は index.html / app.js / style.css / manifest のみ。"""
    sw = strip_js_comments((pwa_dir / "sw.js").read_text(encoding="utf-8"))
    cached = set(re.findall(r"[\"']\.?/?((?:index\.html|app\.js|style\.css|manifest\.webmanifest))[\"']", sw))
    assert cached, "SW のキャッシュ対象が読み取れない"
    assert cached <= {"index.html", "app.js", "style.css", "manifest.webmanifest"}


# ===========================================================================
# 06 §1.1: 色だけに意味を持たせない
# ===========================================================================


def test_risk_is_conveyed_by_more_than_colour(pwa_dir) -> None:
    """§1.1: アイコンとラベル文字は省略してはならない。"""
    blob = "\n".join(
        (pwa_dir / name).read_text(encoding="utf-8") for name in ("app.js", "index.html")
    )
    for label in ("HIGH", "MEDIUM", "LOW"):
        assert label in blob, f"リスクラベル文字 {label} が画面に出ていない"


def test_colour_tokens_are_defined_as_css_variables(css_source) -> None:
    """06 §2: `:root` に定義し、値をハードコードしない。"""
    for token in (
        "--risk-high-bg",
        "--risk-med-bg",
        "--risk-low-bg",
        "--surface",
        "--deny-bg",
        "--approve-edge",
    ):
        assert token in css_source, f"カラートークン {token} が未定義"


def test_dark_mode_is_the_default_and_light_is_an_override(css_source) -> None:
    """06 §2: ダークモードが既定。ライトは `prefers-color-scheme: light` で上書き。"""
    assert "prefers-color-scheme: light" in css_source.replace("prefers-color-scheme:light", "prefers-color-scheme: light")
    assert "prefers-color-scheme: dark" not in css_source.replace("prefers-color-scheme:dark", "prefers-color-scheme: dark")
