"""modal_hub/core/canonical.py — H-H Agent Canonical JSON とペイロードハッシュ（Phase 1a）。

設計上の位置づけ:
    - 親設計書 docs/hh-agent/03_Architecture.md §4.3・§9
    - 実装契約   docs/hh-agent/05_Phase1a_Spec.md §3「Canonical JSON とハッシュ」

このモジュールの役割は 1 つだけ: ``payload_sha256`` / ``payload_raw_sha256`` の元になる
バイト列を、フック（Windows）と Hub（Linux）で **完全に同一の実装** から生成することである。
正はこのファイル。``hh_hooks/canonical.py`` は ``scripts/sync_hook_modules.py``
（本タスクの所有範囲外）が生成する複製であり、手書きしてはならない（Phase1a spec §5.3）。

== 標準ライブラリのみ ==

``json`` / ``hashlib`` / ``unicodedata`` のみを import する。このファイルは
``hh_hooks/`` へ複製され、ツール呼び出しごとに新規起動する短命プロセス内で
import されるため、起動コストを最小に保つ（親設計書 §4.4・§8.2 の 200ms 予算。
本プロジェクトの他モジュールで重い import が予算を食い潰した前例があるため、
標準ライブラリの中でも軽量なものだけを選んでいる）。

== 絶対原則: フェイルクローズ ==

このモジュールは承認ゲートの「比較の基準」を作る場所である。あいまいな入力を
best-effort で何らかのハッシュに変換してはならない。判定に迷う型・値は必ず
例外を送出し、呼び出し側（hh_hooks/tool_gate.py）のフェイルクローズに委ねる。
ここで黙って何かに丸める実装は、「検証は通るが実は違うものを承認した」という
セキュリティホールを作る。

== 05_Phase1a_Spec.md §3 の規定（そのまま実装。変更していない） ==

    def canonical_json(obj) -> bytes:
        return json.dumps(
            _normalize(obj),
            sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

``_normalize()`` の規則（§3 の表を一字一句実装する）:

    - str            : Unicode NFC 正規化
    - int / bool / None : そのまま
    - float          : 禁止。含まれていたら例外を投げる
    - list           : 順序を保持したまま各要素を正規化
    - dict           : キーが str でなければ例外を投げる。キーは NFC 正規化してソート

``PathStr``（``context.cwd`` / ``targets[].path`` / ``targets[].realpath``）の
``os.path.realpath()`` 正規化は、§3 が明示するとおり **呼び出し側（フックのローカル
Windows 環境）の責務**であり、このモジュールはそれを行わない・行えない
（Hub は Linux 上にあり、Windows パスの realpath を評価できない）。このモジュールが
受け取る時点で、PathStr 宣言フィールドは既に正規化済みの文字列であることを前提とする。
このモジュールは全ての str に対して一律に NFC 正規化のみを追加で適用する
（§3 の ``Str`` 規則そのものであり、既に realpath 済みの文字列に NFC を掛けても
実体パスの意味は変わらない — realpath の出力はパス区切りと大文字小文字の話であり、
NFC は Unicode 結合文字の等価性の話で直交する）。

== このモジュールが実装時に追加した安全策（§3 を変更するものではなく、
   §3 が要求する「フェイルクローズ」を具体化したもの。BLOCKED にはせず開示する） ==

**巨大な整数の扱い**: §3 の型テーブルは int を「そのまま」通すとしか書いていない。
一方 05_Phase1a_Spec.md §3 は同じ節で ``identity`` を「必ず文字列で持つ（数値に
してはならない）」と明記し、理由として実測値 ``st_dev = 17735206716449772873``
（約 2^63.9）が JavaScript の ``Number.MAX_SAFE_INTEGER`` (2^53-1) を超え、PWA 側で
丸められて照合が壊れることを挙げている。つまり **§3 の設計は「識別子になり得る巨大な
整数は最初から文字列として運ばれる」ことを前提にしており、canonical_json() の入力に
生の巨大整数が現れること自体が契約違反（呼び出し側のバグ）である**。

「int はそのまま」という記述を文字どおり実装すると、この契約違反を検出できないまま
``json.dumps`` が任意精度の Python int をそのまま巨大な JSON 数値として書き出し、
一見正しく見えるハッシュを生成してしまう（ハッシュ自体は決定的だが、この payload が
万一どこかで JSON として再パースされれば値が丸められる、という上位の脅威を
黙って通過させることになる）。ユーザー指示の hard rule 3
（「あいまいなハッシュ計算は best-effort で作らず、必ず例外を投げる」）に従い、
JavaScript の安全整数範囲 ``[-(2**53-1), 2**53-1]`` を外れる int は
``_normalize()`` の時点で ``CanonicalizationError`` を送出する。§3 の値・
フィールド名・アルゴリズムは一切変更していない — 通常範囲の int は従来どおり
「そのまま」通す。変更したのは「範囲外の int を渡された場合に何をするか」という、
§3 が明記していない空白部分のみである。

**NFC 正規化によるキー衝突**: §3 が dict の非文字列キーを禁止する理由として挙げている
「``{True:'a', 1:'b'}`` が 1 キーに潰れる」という事故と同じ形の問題が、実は文字列キー
同士でも起こりうる — 2 つの異なる生キーが NFC 正規化後に同一文字列になった場合、
Python の dict 構築時に一方が黙って消える。§3 はこのケースに明示的に言及していないが、
「非文字列キーの型強制による衝突」を安全性クリティカルな欠陥として名指ししている
以上、同じ性質を持つ「NFC 正規化による衝突」も同様に扱うのが一貫している。
検出したら ``CanonicalizationError`` を送出する（黙って一方を消さない）。

== ハッシュは 2 本 ==

``payload_sha256`` は ``canonical_json()`` の出力（＝上記の正規化を経た決定的な形）を
ハッシュしたもの。``payload_raw_sha256`` は正規化を一切行わず、§3 が明示する式
``json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",",":"))``
をそのままハッシュしたもの。NFC 正規化は単射ではないため、canonical ハッシュだけでは
「異なる 2 つの生文字列が同じ正規化形に落ちる」ケースを見分けられない
（§3 に明記されたとおり）。``claim`` 時はサーバが両方を照合することでこれを塞ぐ。

**両方を必ずセットで計算させる**: ``hh_hooks/tool_gate.py``（本タスクの所有範囲外・
変更禁止）は既に ``canonical_json()`` と、§3 の raw 式をインラインで書いた独自コードの
2 箇所から両方のハッシュを個別に計算している。既存呼び出し元のインターフェースは
変更できないため、そちらの「片方だけ計算し忘れる」経路を塞ぐことはできない。
その代わり、このモジュール自身が公開する高レベル API ``payload_hashes()`` は
2 本のハッシュを**1 回の呼び出しで常にペアで返す**設計にし、将来の呼び出し元
（新しいエンドポイント実装等）がこのモジュールを正しく使う限り、片方だけを
計算してしまうことを構造的に防ぐ。``raw_json()`` / ``canonical_json()`` を
個別に公開しているのは、既存の ``hh_hooks/tool_gate.py`` の呼び出し形
（``hashlib.sha256(canonical_mod.canonical_json(payload)).hexdigest()``）との
互換性を保つためであり、新規コードは ``payload_hashes()`` を使うことを推奨する。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import NamedTuple

__all__ = [
    "CanonicalizationError",
    "JS_MAX_SAFE_INTEGER",
    "PayloadHashes",
    "canonical_json",
    "raw_json",
    "payload_hashes",
]


class CanonicalizationError(ValueError):
    """canonical_json() の入力があいまい・非決定的・契約違反である場合に送出する。

    呼び出し側（hh_hooks/tool_gate.py）はこの例外を捕捉して deny（フェイルクローズ）
    する前提。ここで例外を握りつぶし何らかのハッシュへ落とすことは絶対にしない
    （hard rule 3）。
    """


# JavaScript の Number.MAX_SAFE_INTEGER。これを超える int を canonical_json() の
# 入力に含めることは §3 の設計契約違反（識別子は文字列で運ぶ）なので拒否する。
# 詳細はモジュール docstring「このモジュールが実装時に追加した安全策」を参照。
JS_MAX_SAFE_INTEGER = 2**53 - 1


class PayloadHashes(NamedTuple):
    """payload_sha256 と payload_raw_sha256 を常にペアで持つための戻り値型。"""

    payload_sha256: str
    payload_raw_sha256: str


def _normalize_str(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _normalize_int(value: int) -> int:
    # bool は Python 上 int のサブクラスだが、_normalize() の呼び出し側で
    # 先に bool 判定して分岐しているため、ここに来るのは真の int のみ。
    if value > JS_MAX_SAFE_INTEGER or value < -JS_MAX_SAFE_INTEGER:
        raise CanonicalizationError(
            "canonical_json(): int が JavaScript の安全整数範囲 "
            f"(±{JS_MAX_SAFE_INTEGER}) を超えている (value={value!r})。"
            "05_Phase1a_Spec.md §3 の設計上、st_dev のような巨大な整数は "
            "文字列として運ぶ契約になっている（例: identity = f'{st_dev}:{st_ino}'）。"
            "呼び出し側で文字列化してから渡すこと。"
        )
    return value


def _normalize_list(value: list) -> list:
    return [_normalize(item) for item in value]


def _normalize_dict(value: dict) -> dict:
    normalized: dict = {}
    # 元のキーごとに正規化後キーを対応付け、衝突を検出してから返す。
    # dict 内包表記で直接組み立てると衝突時に片方が黙って消えるため、
    # 明示的なループで検出する。
    seen_normalized_to_original: dict = {}
    for raw_key, raw_value in value.items():
        if type(raw_key) is not str:  # noqa: E721 — bool/int/None 等の派生を含め型そのものを見る
            raise CanonicalizationError(
                "canonical_json(): dict のキーは str でなければならない "
                f"(key={raw_key!r}, type={type(raw_key).__name__})。"
                "05_Phase1a_Spec.md §3: 非文字列キーは json.dumps によって暗黙に "
                "文字列化され、異なるキーが 1 個に潰れうる（実測: "
                "json.dumps({True:'a', 1:'b'}) はキー1個に潰れる）ため禁止する。"
            )
        normalized_key = _normalize_str(raw_key)
        if normalized_key in seen_normalized_to_original and seen_normalized_to_original[normalized_key] != raw_key:
            raise CanonicalizationError(
                "canonical_json(): 異なる2つのキーが Unicode NFC 正規化後に同一文字列 "
                f"{normalized_key!r} へ衝突した (元キー: {seen_normalized_to_original[normalized_key]!r} と "
                f"{raw_key!r})。NFC 正規化は単射ではないため、この衝突を黙って片方だけ "
                "残すとハッシュが一意にならない。"
            )
        seen_normalized_to_original[normalized_key] = raw_key
        normalized[normalized_key] = _normalize(raw_value)
    return normalized


def _normalize(obj):
    """05_Phase1a_Spec.md §3 の _normalize() 規則を実装する。

    型ごとの分岐順序に注意: bool は Python では int のサブクラスなので、
    isinstance(obj, int) より先に isinstance(obj, bool) を判定する
    （§3 の表では bool と int は同じ「そのまま」規則だが、int だけに掛ける
    巨大整数チェックを bool に誤爆させないため）。
    """
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, str):
        return _normalize_str(obj)
    if isinstance(obj, int):
        return _normalize_int(obj)
    if isinstance(obj, float):
        raise CanonicalizationError(
            "canonical_json(): float は禁止（05_Phase1a_Spec.md §3）。"
            "表現の揺れでハッシュが一致しなくなるため、値を送る側が文字列化するか "
            "整数化してから渡すこと。"
        )
    if isinstance(obj, list):
        return _normalize_list(obj)
    if isinstance(obj, dict):
        return _normalize_dict(obj)
    raise CanonicalizationError(
        f"canonical_json(): 未対応の型 {type(obj).__name__!r} は正規化できない "
        "（05_Phase1a_Spec.md §3 が規定するのは str/int/bool/None/float/list/dict のみ）。"
        "best-effort での変換はしない（hard rule 3: フェイルクローズ）。"
    )


def canonical_json(obj) -> bytes:
    """05_Phase1a_Spec.md §3 の canonical_json() をそのまま実装する。

    フックとサーバ (Windows / Linux) の両方で bit-for-bit 同一の出力になることを
    要求される。sort_keys=True・separators=(",", ":")・ensure_ascii=False・
    明示的な UTF-8 エンコードにより、意味のない空白・キー順序・エスケープ方式の
    揺れを排除する。

    戻り値は **bytes**（str ではない）。呼び出し元 (hh_hooks/tool_gate.py) は
    ``hashlib.sha256(canonical_json(payload)).hexdigest()`` の形でそのまま
    ハッシュに渡すため、str を返すと ``hashlib`` が TypeError で落ちる。
    """
    normalized = _normalize(obj)
    return json.dumps(
        normalized,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def raw_json(obj) -> bytes:
    """05_Phase1a_Spec.md §3 の payload_raw_sha256 の元になる「生バイト」を返す。

    正規化を一切行わない。§3 に明記された式をそのまま実装する:

        json.dumps(payload, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")

    NFC 正規化・float 禁止・非文字列キー禁止・巨大整数拒否のいずれも適用しない
    （それらは canonical_json() 側の規則であり、raw 側は意図的に「正規化前の
    生の姿」をハッシュするための独立した経路である）。float や巨大な int が
    含まれていても json.dumps の標準的な変換規則に従うだけで例外にはしない。
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def payload_hashes(payload) -> PayloadHashes:
    """payload_sha256 と payload_raw_sha256 を1回の呼び出しで両方計算して返す。

    このモジュールのモジュール docstring「両方を必ずセットで計算させる」を参照。
    新規に canonical ハッシュを必要とするコードはこの関数を使うこと。
    片方だけを取り出して使う場合も、必ずこの関数を経由してから
    ``result.payload_sha256`` / ``result.payload_raw_sha256`` のように参照すれば、
    「もう片方の計算を書き忘れる」という経路自体が発生しない。
    """
    payload_sha256 = hashlib.sha256(canonical_json(payload)).hexdigest()
    payload_raw_sha256 = hashlib.sha256(raw_json(payload)).hexdigest()
    return PayloadHashes(payload_sha256=payload_sha256, payload_raw_sha256=payload_raw_sha256)
