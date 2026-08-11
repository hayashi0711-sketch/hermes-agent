# Hermes-Hyper-Agent (H-H Agent) バイブコーディング プロンプトセット
📋 STEP 1｜プロジェクト定義プロンプト
# 役割と目標
あなたは「Hermes-Hyper-Agent (H-H Agent)」を構築するリードAIシステムアーキテクト兼プリンシパルエンジニアです。H-H Agentは、クロスプラットフォームで動作し、自己進化するAIエージェントシステムです。
あなたのミッションは、Modal上に構築された中央オーケストレーションハブのセットアップ、Corpus2Skill理論に基づく動的スキル抽出メカニズムの実装、Obsidianを用いた厳格な記憶の関心分離、および各種クロスプラットフォーム（Windows VS Code、モバイルリモート承認、OpenAI Realtime Voice Gateway、Modalクラウド上のWeb/CLIコーディングエージェント）の統合を行うことです。

# 核心仕様とプロジェクト制約
1. 中央ハブ（Central Hub）: Modalクラウド上のサーバーレスインフラストラクチャ（FastAPI + WebSockets + Modal Apps）。
2. 記憶アーキテクチャ（厳格な関心分離）:
   - Corpus2Skill Motor: 主たる記憶・スキル蓄積エンジン。会話履歴、実行ログ、Git Diffを自動解析し、再利用可能な `SKILL.md` ファイル（.agent/skills/ 配下に保存）を自動抽出・生成する。
   - Obsidian Vault: アプリケーション設計、アーキテクチャの意思決定、プロジェクトロードマップを保持する「アプリ開発プロジェクト専用の記憶装置」としてのみ利用する。汎用スキルや実行ログでObsidianを汚染してはならない。
3. 音声ゲートウェイ（Voice Gateway）: OpenAI Realtime API（WebRTC/WebSocket）をModal WebEndpointと統合し、低遅延でのツール呼び出し（Function Calling）および割り込み（Barge-in）処理を実現する。
4. モバイルリモート承認（Mobile Remote Approval）: VS Code上のClaude Code実行時やModal上のエージェント実行時に、モバイル端末からワンタップで「承認 / 却下」できるプッシュ通知・PWA承認ゲート。
5. Modalクラウド型コーディングエージェント（新規追加）: Modalサーバー上で稼働し、VS Code + Claude Codeと同等の操作感・開発体験（ストリーミング出力、ファイル差分表示、インタラクティブ承認、Terminal統合）を提供するWeb/CLI統合エージェントエンジン。
6. 基幹LLMバックエンド: Modal上にデプロイされた専用Qwen（2.5/3.5）をローカル/プライベートタスク用に配置し、タスクに応じてClaude Code、OpenAI Codex、MiniMax M3へルーティングする。

# プロジェクト構造のブループリント
以下のリポジトリ構成を作成してください：

hh-agent/
├── .agent/
│   └── skills/                  # 自動生成された SKILL.md リポジトリ (Corpus2Skill)
├── modal_hub/
│   ├── main.py                  # Modal App エントリーポイント & FastAPI アプリ
│   ├── routers/
│   │   ├── voice_gateway.py     # OpenAI Realtime API Function Callback ハンドラー
│   │   ├── approval_gate.py     # モバイル承認通知 & Webhook ハンドラー
│   │   └── cloud_agent.py      # Modalクラウド型コーディングエージェント(VS Code+Claude Code風UI/API)
│   ├── services/
│   │   ├── corpus2skill.py      # Corpus2Skill 抽出 & SKILL.md 生成エンジン
│   │   └── obsidian_sync.py     # Obsidian プロジェクト記憶 Vault REST/Git ブリッジ
│   └── core/
│       ├── router.py            # タスクディスパッチャー (Qwen/Claude/Codex/MiniMax)
│       └── config.py            # 環境変数 & Modal 設定
├── hooks/
│   └── claude_code_interceptor.py # VS Code Claude Code 用のローカル CLI フック / インターセプター
└── mobile_app/
    └── pwa_approval/             # リモート承認用軽量 PWA / Web UI

🏛️ STEP 2｜アーキテクチャ設計プロンプト

# タスク: 内部インフラストラクチャ & Modal デプロイ アーキテクチャ構築

1. Modal クラウドハブのセットアップ (`modal_hub/main.py`, `modal_hub/core/config.py`):
   - `"hh-agent-central-hub"` という名前の Modal `App` を定義すること。
   - `fastapi`, `uvicorn`, `pydantic`, `httpx`, `websockets`, `pyyaml`, `gitpython` を含む Python 3.11 の Modal Image を構成すること。
   - メインの FastAPI インスタンスをホストする `@asgi_app()` エントリーポイントを公開すること。

2. Corpus2Skill 記憶エンジン (`modal_hub/services/corpus2skill.py`):
   - `Corpus2SkillEngine` を実装すること:
     - メソッド `extract_skill_from_session(execution_log: dict, git_diff: str) -> Optional[str]`:
       成功したエージェントのタスク実行ログを解析し、再利用可能なパターンを抽象化して Hermes Agent 互換の `SKILL.md` を生成する。
     - メソッド `save_skill(skill_name: str, content: str)`: スキルを `.agent/skills/<skill_name>.md` に保存し、スキルインデックスを更新する。
   - 厳格な分離を強制すること: Corpus2Skill の出力が Obsidian に書き込まれることは**絶対にあってはならない**。

3. Obsidian プロジェクト Brain 同期 (`modal_hub/services/obsidian_sync.py`):
   - `ObsidianBrainGate` を実装すること:
     - メソッド `query_project_context(query: str) -> str`: 設計判断やプロジェクト仕様について Obsidian の Markdown ファイルを検索する。
     - メソッド `record_project_decision(title: str, content: str, tags: List[str])`: アーキテクチャ上の決定事項（`#architecture`, `#decision`）を Vault に追記する。
   - 同期対象は「アプリ開発プロジェクトの仕様・設計メタデータ」のみに制限すること。

4. マルチ LLM タスクルーター (`modal_hub/core/router.py`):
   - `TaskRouter` を実装すること:
     - タスクの性質に応じてリクエストを動的にルーティングする:
       * 高度な概念設計 / 複雑なロジック -> Claude Code / MiniMax M3 API
       * 決定論的なコード自動生成 -> OpenAI Codex
       * プライベート / 低遅延 / オフラインタスク -> Modal ホストの Qwen (2.5/3.5) エンドポイント

🎨 STEP 3｜UI/UX実装プロンプト

# タスク: クロスプラットフォーム音声インターフェース・Modalクラウドエージェント・モバイル承認 UI の実装

1. Modal クラウドコーディングエージェント UI & API (`modal_hub/routers/cloud_agent.py`):
   - Modal 上で稼働し、VS Code + Claude Code の快適な開発体験をクラウド上で再現するインターフェースを構築すること:
     - リアルタイムストリーミング CLI / Web ターミナル（WebSocket 経由での思考プロセス、ファイル変更Diff、ツール呼び出しのインタラクティブ表示）。
     - コマンド実行時の「承認 / 拒否」プロンプトを画面上にインライン表示し、キー入力またはタップで応答可能にする。
     - Claude Code 同等の色分けされたカラーログ、ツリー状の思考表示、ファイル書き換えプレビュー機能を提供する。

2. モバイル承認 PWA / 通知ゲート (`mobile_app/pwa_approval/`, `modal_hub/routers/approval_gate.py`):
   - 軽量なシングルページ PWA UI (`index.html`, `app.js`) を構築すること:
     - 開発環境（VS Code または Modal クラウドエージェント）からの保留中承認リクエストをリアルタイム表示する。
     - 実行予定のシェルコマンド、変更対象のファイル Diff、リスクレベル、タイムスタンプを表示する。
     - 「承認 (Approve)」[緑] と 「却下 (Reject)」[赤] の大型ボタンを配置する。
   - Modal サーバーとの WebSocket 接続により、承認状態を即座に同期する。
   - エンドポイント `/api/approval/respond`: 承認トークンとユーザーの操作結果を受け取り、セッション状態を更新する。

3. OpenAI Realtime 音声ゲートウェイ (`modal_hub/routers/voice_gateway.py`):
   - OpenAI Realtime API 統合用の FastAPI エンドポイントを作成すること:
     - `POST /api/voice/session`: クライアントアプリへ一時的な WebRTC/WebSocket セッション情報を発行する。
     - `POST /api/voice/function-callback`: OpenAI Realtime API からの Function Calling ペイロードを処理する。
   - 以下の Function Calling スキーマを設定すること:
     * `trigger_coding_agent`: 引数 `{"task": "string", "target_files": ["string"]}`
     * `query_obsidian_brain`: 引数 `{"query": "string"}`
     * `request_mobile_approval`: 引数 `{"command": "string", "reason": "string"}`

🔌 STEP 4｜バックエンド・API実装プロンプト

# タスク: ローカル CLI インターセプター & セッション状態の同期メカニズム

1. ローカル Claude Code インターセプターフック (`hooks/claude_code_interceptor.py`):
   - VS Code 上の Claude Code の動作をフックする Python/Node.js スクリプトを作成すること:
     - シェルコマンド実行や重要ファイルの変更操作を検知・割り込みする。
     - 破壊的または影響度の高い操作を検知した場合:
       1. ローカルプロセスを一時停止（Keep-Alive ハートビートを維持しながら待機）する。
       2. Modal 中央ハブの `/api/approval/request` にコマンド/Diff 情報を送信する。
       3. WebSocket またはポーリングで Modal ハブからのモバイル承認結果を待機する。
       4. 「承認」を受け取った場合: プロセスを再開し、コマンドを実行する。
       5. 「却下」を受け取った場合: プロセスを安全に中止し、Claude Code に実行拒否を返す。

2. キープアライブ & デッドロック防止メカニズム:
   - 承認待機中に Claude Code CLI がタイムアウトしないよう、5秒ごとのハートビート Ping を実装すること。
   - 一定時間（デフォルト: 180秒）応答がない場合、安全のために自動的に操作を「却下」する自動タイムアウトを設定すること。

3. エンドツーエンド統合テストルート:
   - 以下のシナリオを疎通テストすること:
     ローカル CLI / Modal クラウドエージェントがタスク開始 -> Modal がスマホへ Push 通知送信 -> ユーザーが「承認」をタップ -> エージェントのブロックが解除されタスク完了 -> Corpus2Skill が実行ログから新しい `SKILL.md` を自動抽出。

🧪 STEP 5｜テスト・デバッグプロンプト

# タスク: 品質保証、サンドボックス検証、および動作テスト

1. Corpus2Skill パイプラインの検証:
   - Git Diff と実行ログを含む模擬セッションデータを用いて `Corpus2SkillEngine` をテストすること。
   - 生成された `.agent/skills/*.md` が正しい YAML Frontmatter と Markdown 構造を持ち、Obsidian ディレクトリ側へ一切漏洩していないことを確認すること。

2. Modal クラウドコーディングエージェントの操作性テスト:
   - Modal 上の Web/CLI インターフェースからタスクを発行し、VS Code + Claude Code と同様のリアルタイムストリーミング出力およびインタラクティブな承認プロンプトがスムーズに動作するか検証すること。

3. OpenAI Realtime 音声 ＆ Barge-In 応答速度テスト:
   - OpenAI Realtime API から Modal WebEndpoint への WebSocket Function Calling をシミュレートすること。
   - ツール呼び出し受信からレスポンス返却までの遅延が 400ms 未満であることを確認すること。
   - 重いバックグラウンド処理が発生した際、音声対話側に即座にプレースホルダー応答（「タスクを受け付けました」等）を返し、無音やタイムアウトを防げているか確認すること。

4. モバイルリモート承認ゲートの負荷・エラーテスト:
   - `hooks/claude_code_interceptor.py` を実行し、`rm -rf` などの危険なコマンド割り込みをシミュレートすること。
   - モバイル PWA が 1秒以内に Push 通知を受信することを確認すること。
   - スマホで「却下」を選択した際、即座にローカル/クラウドのコマンド実行が中断され、リターンコード 1 で終了することを確認すること。

⚠️ 開発上の注意点

要件に基づき、「Hermes-Hyper-Agent (H-H Agent)」の全プロンプトを日本語化し、追加要件である「Modalサーバー上でVS Code + Claude Codeのような操作感・快適性を再現するコーディングエージェント（Web/CLIインターフェース）」の設計・プロンプトを組み込んだ完全な1つのMarkdownドキュメントを作成しました。

---

# Hermes-Hyper-Agent (H-H Agent) バイブコーディング プロンプトセット

## 📋 STEP 1｜プロジェクト定義プロンプト

```markdown
# 役割と目標
あなたは「Hermes-Hyper-Agent (H-H Agent)」を構築するリードAIシステムアーキテクト兼プリンシパルエンジニアです。H-H Agentは、クロスプラットフォームで動作し、自己進化するAIエージェントシステムです。
あなたのミッションは、Modal上に構築された中央オーケストレーションハブのセットアップ、Corpus2Skill理論に基づく動的スキル抽出メカニズムの実装、Obsidianを用いた厳格な記憶の関心分離、および各種クロスプラットフォーム（Windows VS Code、モバイルリモート承認、OpenAI Realtime Voice Gateway、Modalクラウド上のWeb/CLIコーディングエージェント）の統合を行うことです。

# 核心仕様とプロジェクト制約
1. 中央ハブ（Central Hub）: Modalクラウド上のサーバーレスインフラストラクチャ（FastAPI + WebSockets + Modal Apps）。
2. 記憶アーキテクチャ（厳格な関心分離）:
   - Corpus2Skill Motor: 主たる記憶・スキル蓄積エンジン。会話履歴、実行ログ、Git Diffを自動解析し、再利用可能な `SKILL.md` ファイル（.agent/skills/ 配下に保存）を自動抽出・生成する。
   - Obsidian Vault: アプリケーション設計、アーキテクチャの意思決定、プロジェクトロードマップを保持する「アプリ開発プロジェクト専用の記憶装置」としてのみ利用する。汎用スキルや実行ログでObsidianを汚染してはならない。
3. 音声ゲートウェイ（Voice Gateway）: OpenAI Realtime API（WebRTC/WebSocket）をModal WebEndpointと統合し、低遅延でのツール呼び出し（Function Calling）および割り込み（Barge-in）処理を実現する。
4. モバイルリモート承認（Mobile Remote Approval）: VS Code上のClaude Code実行時やModal上のエージェント実行時に、モバイル端末からワンタップで「承認 / 却下」できるプッシュ通知・PWA承認ゲート。
5. Modalクラウド型コーディングエージェント（新規追加）: Modalサーバー上で稼働し、VS Code + Claude Codeと同等の操作感・開発体験（ストリーミング出力、ファイル差分表示、インタラクティブ承認、Terminal統合）を提供するWeb/CLI統合エージェントエンジン。
6. 基幹LLMバックエンド: Modal上にデプロイされた専用Qwen（2.5/3.5）をローカル/プライベートタスク用に配置し、タスクに応じてClaude Code、OpenAI Codex、MiniMax M3へルーティングする。

# プロジェクト構造のブループリント
以下のリポジトリ構成を作成してください：

hh-agent/
├── .agent/
│   └── skills/                  # 自動生成された SKILL.md リポジトリ (Corpus2Skill)
├── modal_hub/
│   ├── main.py                  # Modal App エントリーポイント & FastAPI アプリ
│   ├── routers/
│   │   ├── voice_gateway.py     # OpenAI Realtime API Function Callback ハンドラー
│   │   ├── approval_gate.py     # モバイル承認通知 & Webhook ハンドラー
│   │   └── cloud_agent.py      # Modalクラウド型コーディングエージェント(VS Code+Claude Code風UI/API)
│   ├── services/
│   │   ├── corpus2skill.py      # Corpus2Skill 抽出 & SKILL.md 生成エンジン
│   │   └── obsidian_sync.py     # Obsidian プロジェクト記憶 Vault REST/Git ブリッジ
│   └── core/
│       ├── router.py            # タスクディスパッチャー (Qwen/Claude/Codex/MiniMax)
│       └── config.py            # 環境変数 & Modal 設定
├── hooks/
│   └── claude_code_interceptor.py # VS Code Claude Code 用のローカル CLI フック / インターセプター
└── mobile_app/
    └── pwa_approval/             # リモート承認用軽量 PWA / Web UI

```

---

## 🏛️ STEP 2｜アーキテクチャ設計プロンプト

```markdown
# タスク: 内部インフラストラクチャ & Modal デプロイ アーキテクチャ構築

1. Modal クラウドハブのセットアップ (`modal_hub/main.py`, `modal_hub/core/config.py`):
   - `"hh-agent-central-hub"` という名前の Modal `App` を定義すること。
   - `fastapi`, `uvicorn`, `pydantic`, `httpx`, `websockets`, `pyyaml`, `gitpython` を含む Python 3.11 の Modal Image を構成すること。
   - メインの FastAPI インスタンスをホストする `@asgi_app()` エントリーポイントを公開すること。

2. Corpus2Skill 記憶エンジン (`modal_hub/services/corpus2skill.py`):
   - `Corpus2SkillEngine` を実装すること:
     - メソッド `extract_skill_from_session(execution_log: dict, git_diff: str) -> Optional[str]`:
       成功したエージェントのタスク実行ログを解析し、再利用可能なパターンを抽象化して Hermes Agent 互換の `SKILL.md` を生成する。
     - メソッド `save_skill(skill_name: str, content: str)`: スキルを `.agent/skills/<skill_name>.md` に保存し、スキルインデックスを更新する。
   - 厳格な分離を強制すること: Corpus2Skill の出力が Obsidian に書き込まれることは**絶対にあってはならない**。

3. Obsidian プロジェクト Brain 同期 (`modal_hub/services/obsidian_sync.py`):
   - `ObsidianBrainGate` を実装すること:
     - メソッド `query_project_context(query: str) -> str`: 設計判断やプロジェクト仕様について Obsidian の Markdown ファイルを検索する。
     - メソッド `record_project_decision(title: str, content: str, tags: List[str])`: アーキテクチャ上の決定事項（`#architecture`, `#decision`）を Vault に追記する。
   - 同期対象は「アプリ開発プロジェクトの仕様・設計メタデータ」のみに制限すること。

4. マルチ LLM タスクルーター (`modal_hub/core/router.py`):
   - `TaskRouter` を実装すること:
     - タスクの性質に応じてリクエストを動的にルーティングする:
       * 高度な概念設計 / 複雑なロジック -> Claude Code / MiniMax M3 API
       * 決定論的なコード自動生成 -> OpenAI Codex
       * プライベート / 低遅延 / オフラインタスク -> Modal ホストの Qwen (2.5/3.5) エンドポイント

```

---

## 🎨 STEP 3｜UI/UX実装プロンプト

```markdown
# タスク: クロスプラットフォーム音声インターフェース・Modalクラウドエージェント・モバイル承認 UI の実装

1. Modal クラウドコーディングエージェント UI & API (`modal_hub/routers/cloud_agent.py`):
   - Modal 上で稼働し、VS Code + Claude Code の快適な開発体験をクラウド上で再現するインターフェースを構築すること:
     - リアルタイムストリーミング CLI / Web ターミナル（WebSocket 経由での思考プロセス、ファイル変更Diff、ツール呼び出しのインタラクティブ表示）。
     - コマンド実行時の「承認 / 拒否」プロンプトを画面上にインライン表示し、キー入力またはタップで応答可能にする。
     - Claude Code 同等の色分けされたカラーログ、ツリー状の思考表示、ファイル書き換えプレビュー機能を提供する。

2. モバイル承認 PWA / 通知ゲート (`mobile_app/pwa_approval/`, `modal_hub/routers/approval_gate.py`):
   - 軽量なシングルページ PWA UI (`index.html`, `app.js`) を構築すること:
     - 開発環境（VS Code または Modal クラウドエージェント）からの保留中承認リクエストをリアルタイム表示する。
     - 実行予定のシェルコマンド、変更対象のファイル Diff、リスクレベル、タイムスタンプを表示する。
     - 「承認 (Approve)」[緑] と 「却下 (Reject)」[赤] の大型ボタンを配置する。
   - Modal サーバーとの WebSocket 接続により、承認状態を即座に同期する。
   - エンドポイント `/api/approval/respond`: 承認トークンとユーザーの操作結果を受け取り、セッション状態を更新する。

3. OpenAI Realtime 音声ゲートウェイ (`modal_hub/routers/voice_gateway.py`):
   - OpenAI Realtime API 統合用の FastAPI エンドポイントを作成すること:
     - `POST /api/voice/session`: クライアントアプリへ一時的な WebRTC/WebSocket セッション情報を発行する。
     - `POST /api/voice/function-callback`: OpenAI Realtime API からの Function Calling ペイロードを処理する。
   - 以下の Function Calling スキーマを設定すること:
     * `trigger_coding_agent`: 引数 `{"task": "string", "target_files": ["string"]}`
     * `query_obsidian_brain`: 引数 `{"query": "string"}`
     * `request_mobile_approval`: 引数 `{"command": "string", "reason": "string"}`

```

---

## 🔌 STEP 4｜バックエンド・API実装プロンプト

```markdown
# タスク: ローカル CLI インターセプター & セッション状態の同期メカニズム

1. ローカル Claude Code インターセプターフック (`hooks/claude_code_interceptor.py`):
   - VS Code 上の Claude Code の動作をフックする Python/Node.js スクリプトを作成すること:
     - シェルコマンド実行や重要ファイルの変更操作を検知・割り込みする。
     - 破壊的または影響度の高い操作を検知した場合:
       1. ローカルプロセスを一時停止（Keep-Alive ハートビートを維持しながら待機）する。
       2. Modal 中央ハブの `/api/approval/request` にコマンド/Diff 情報を送信する。
       3. WebSocket またはポーリングで Modal ハブからのモバイル承認結果を待機する。
       4. 「承認」を受け取った場合: プロセスを再開し、コマンドを実行する。
       5. 「却下」を受け取った場合: プロセスを安全に中止し、Claude Code に実行拒否を返す。

2. キープアライブ & デッドロック防止メカニズム:
   - 承認待機中に Claude Code CLI がタイムアウトしないよう、5秒ごとのハートビート Ping を実装すること。
   - 一定時間（デフォルト: 180秒）応答がない場合、安全のために自動的に操作を「却下」する自動タイムアウトを設定すること。

3. エンドツーエンド統合テストルート:
   - 以下のシナリオを疎通テストすること:
     ローカル CLI / Modal クラウドエージェントがタスク開始 -> Modal がスマホへ Push 通知送信 -> ユーザーが「承認」をタップ -> エージェントのブロックが解除されタスク完了 -> Corpus2Skill が実行ログから新しい `SKILL.md` を自動抽出。

```

---

## 🧪 STEP 5｜テスト・デバッグプロンプト

```markdown
# タスク: 品質保証、サンドボックス検証、および動作テスト

1. Corpus2Skill パイプラインの検証:
   - Git Diff と実行ログを含む模擬セッションデータを用いて `Corpus2SkillEngine` をテストすること。
   - 生成された `.agent/skills/*.md` が正しい YAML Frontmatter と Markdown 構造を持ち、Obsidian ディレクトリ側へ一切漏洩していないことを確認すること。

2. Modal クラウドコーディングエージェントの操作性テスト:
   - Modal 上の Web/CLI インターフェースからタスクを発行し、VS Code + Claude Code と同様のリアルタイムストリーミング出力およびインタラクティブな承認プロンプトがスムーズに動作するか検証すること。

3. OpenAI Realtime 音声 ＆ Barge-In 応答速度テスト:
   - OpenAI Realtime API から Modal WebEndpoint への WebSocket Function Calling をシミュレートすること。
   - ツール呼び出し受信からレスポンス返却までの遅延が 400ms 未満であることを確認すること。
   - 重いバックグラウンド処理が発生した際、音声対話側に即座にプレースホルダー応答（「タスクを受け付けました」等）を返し、無音やタイムアウトを防げているか確認すること。

4. モバイルリモート承認ゲートの負荷・エラーテスト:
   - `hooks/claude_code_interceptor.py` を実行し、`rm -rf` などの危険なコマンド割り込みをシミュレートすること。
   - モバイル PWA が 1秒以内に Push 通知を受信することを確認すること。
   - スマホで「却下」を選択した際、即座にローカル/クラウドのコマンド実行が中断され、リターンコード 1 で終了することを確認すること。

```

---

## ⚠️ 開発上の注意点

1. **記憶領域の厳格分離（Corpus2Skill vs Obsidian）**
* **絶対原則:** エージェントの汎用的な手腕・自己改善ルール・アルゴリズム抽出物（Corpus2Skill）はすべて `.agent/skills/` 内の `SKILL.md` として蓄積すること。Obsidian Vault へは「開発中のプロジェクト固有仕様・設計判断」以外は一切書き込まないこと。


2. **Modal 上のクラウドエージェント開発体験の維持**
* Modal サーバー上で動かすコーディングエージェント（`cloud_agent.py`）は、VS Code + Claude Code 特有の「思考プロセスのリアルタイム表示」「カラー表示」「ファイル差分プレビュー」を WebSocket 経由で再現し、クラウド環境からでもバイブコーディングの快適性を損なわない設計にすること。


3. **セッションのデッドロック回避**
* スマホ承認待ちの際、ローカルの Interceptor や Modal 上のエージェントが無応答のまま放置されると内部タイムアウトでセッションが壊れます。Modal と各クライアント間で必ず Keep-Alive Ping を送受信し、スマホ画面にもカウントダウンタイマーを表示させる設計にしてください。


4. **OpenAI Realtime API のコストと応答遅延対策**
* 音声対話中に重いコード生成や Modal 上の Qwen ファインチューニング等を同期実行すると、音声応答が中断されます。重いタスクはすべて Modal の非同期 Background Task (Modal Functions) にオフロードし、音声対話側には即時に「受け付け完了」の音声レスポンスを返してください。

