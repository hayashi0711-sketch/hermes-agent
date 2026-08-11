"""H-H Agent Phase 1a unit test suite (Modal 非依存で回る pytest)。

親設計書 docs/hh-agent/03_Architecture.md §8.1 の単体テスト項目と、
§9「既知の落とし穴」24 項目の回帰テストをここに置く。

**Modal のランタイムには一切依存しない。** `modal.Dict` / `modal.Volume` へ
実際に接続するコードパスは、すべて conftest.py の `FakeStore` / `FakeModalDict`
に差し替えてから実行する。ネットワークが無い環境（CI・オフライン）でも
全テストが緑になることが要件。
"""
