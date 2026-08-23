---
date: 2026-08-23
tags: [design, hh-agent, hermes-upstream, telegram-bot]
status: approved (pending spec doc review)
---

# 09 — Hermes上流マージ + Telegram Bot活性化 設計書

体制: 設計・司令塔=Claude Code Sonnet5、コーディング=DeepSeek、レビュー=Sonnet5代行（Codex 401認証エラーのため今回は不使用）。

## 背景

上流 `nousresearch/hermes-agent`（origin）がローカルフォーク（`myfork`、H-H-Agent向けModal統合済み）より1152コミット進んでいる。ユーザーから「Linux版のHermes Agentをソフトアップデートしたい、Bot機能を含むアップデートがあるはず」と依頼された。

調査の結果:
- 試験マージ（`git merge-tree`）でテキスト衝突ゼロを確認。クリーンマージ見込み。
- 「Bot機能」は2系統存在する:
  1. `apps/desktop/src/plugins/hermes-bots/` — Windowsデスクトップアプリ専用のグループチャット内マルチAIボットUI。Linux/Modal側（H-H-Agentが動かしている本体）には無関係。
  2. `tools/bot_mode_dm.py`・`tools/bot_relay.py`（新規追加）+ `gateway/relay/`・`hermes_cli/telegram_managed_bot.py`（既存強化） — サーバー側のBot Gateway機能。Pythonのみで完結するためLinux/Modal側でも動作可能。**今回はこちらを指す。**
- Hermesの「Bot Gateway」は自前でTelegram/Discord/Slack APIを叩くのではなく、**Nous Research運営のリレーサーバーへHermes側がWebSocketで常時接続し、実際のプラットフォーム通信はNous側が代行する**アーキテクチャ（`gateway/relay/adapter.py`のdocstringで確認）。ペアリングはBotFatherでの手動トークン取得ではなく、Hermes組み込みのQR/ディープリンク方式（ユーザーがTelegramアプリで1回承認するだけ）。
- Hermes組み込みの`gateway/scale_to_zero.py`はFly.io Machines APIに直結した実装で、Modalには移植できない（環境変数`HERMES_SCALE_TO_ZERO`が未設定なら単に発火しないだけで無害）。

## スコープ

1. 上流1152コミットをH-H-Agentのローカル30コミット（Agentic OS連携: dispatch/approval token発行・`hh-agent-dispatch`分離等）にマージ。
2. Telegramプラットフォームで実際にBotを活性化（ペアリング完了まで）。

Discord/Slack等の他プラットフォームは対象外（必要になれば別途）。

## 設計判断

### マージ方式
新規ブランチ`merge-hermes-upstream`で`git merge origin/main`を実行 → 既存pytestスイート（現状1037 passed/5 skipped）を全実行 → Modal依存箇所（`modal_hub/main.py`のImage定義、`approval_token.py`/`dispatch_token.py`の循環インポート回避、`modal_hub/dispatch_app.py`）を個別に差分確認 → 問題なければmainへマージ。

### デプロイ方式（3案比較・A案採用）

| 案 | 内容 | 判断 |
|---|---|---|
| **A（採用）** | 新規Modal Function、`min_containers=1`で`gateway.run`（`start_gateway()`）を常駐実行。既存`hh-agent-dashboard-home` Volumeでペアリング状態・state.dbを永続化 | 小さいが継続課金あり。ComfyUI等の既存`min_containers`パターンを踏襲 |
| B | Modalのscale-to-zero request駆動モデルに載せる | Hermes組み込みscale-to-zeroはFly専用。作り直しは工数大でYAGNI、不採用 |
| C | ユーザーのWindows PCでスケジュール実行 | 新規Modal課金ゼロだが可用性がPC電源に依存し、「Hubは常時稼働」という既存方針と矛盾。不採用 |

### 外部サービス接続（課金リスク）に関する運用
Telegramペアリング（Nousの管理Botとの1回限りのディープリンク承認）は新規外部サービス接続に該当し、CLAUDE.mdルール上は本来Codex経由。Codex 401エラーのため今回は「Sonnet5がコマンド手順を提示し、ユーザー自身がTelegramアプリ上で承認する」形で進める（Claude Code自身はAPIキー発行やトークン取得を代行しない）。

## コンポーネント

1. **`modal_hub/gateway_app.py`（新規）**: 独立Modal App。`gateway.run.start_gateway()`をラップする`@app.function(min_containers=1)`。既存`hh-agent-dashboard-home` Volumeをマウント（`HERMES_HOME`を共有し、Web UIで作成済みのプロファイル・設定と同じ状態を参照できるようにする）。
2. **Secret**: 既存`hh-agent-secret`を流用（追加の秘密情報は現時点で不要、ペアリングトークン自体はVolume上のファイルに保存される）。
3. **ペアリング手順（1回限り、コード不要）**: デプロイ後、`modal run modal_hub/gateway_app.py::pair_telegram`のような一時エントリポイントでディープリンク/QRを出力 → ユーザーがTelegramアプリで承認 → 以降は常駐プロセスがそのまま応答する。

## テスト

- マージ後: 既存pytest全件（回帰ゼロ確認）
- 新規`gateway_app.py`: `modal run`でのローカルスモークテスト（`start_gateway()`が例外なく起動し、Volume上に想定パスが作られるか）
- 実機: Telegramでのペアリング完了 + 実際のメッセージ往復確認（ユーザー実施）

## 残存リスク・次回以降の課題

- Discord/Slack等の追加プラットフォームは今回スコープ外
- 常駐コンテナのコスト実測は未実施（デプロイ後に確認）
- Profile（Hermes CLIの`profiles`機能）をLinux(Modal)/Windows①/Windows②の3環境で共有する仕組みは別件として設計待ち（Corpus2Skillの記憶共有とは別軸、現状は各`HERMES_HOME`のローカルSQLiteで完全に分離されている）
