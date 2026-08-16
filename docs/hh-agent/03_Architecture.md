# Hermes-Hyper-Agent (H-H Agent) 設計書（実装の唯一の正）

- **版**: v3（2026-08-10 Codex 設計レビュー 1 巡目 16 件 ＋ 2 巡目の指摘を反映）
- **最終更新**: 2026-08-10
- **体制**: 設計・司令塔＝Claude Code **Opus 5** ／ コーディング＝Claude Code **Sonnet 5（30%）** + **MiniMax M3（70%）** ／ コード・設計レビュー＝**Codex**
- **原指示書**: `docs/Hermes-Hyper-Agent_HHAgent.md`（参考資料。**本設計書と食い違う場合は本設計書が優先**）
- **作業フォルダ**: `C:\Users\Haruki\Projects\Hermes-Hyper-Agent_HHAgent`（Hermes-Agent のクローン。ここを直接改造する）
- **Obsidian（プロジェクト記憶の正）**: `Obsidian Vault\Projects\H-H-Agent`
- **参照許可フォルダ**（流用時は**必ず本プロジェクトへコピーしてから**使う。原本を書き換えない）:
  `2HD-Code` / `3LLM_Max` / `AutoLLM-Evolver` / `Corpus2Skill` / `GEM Project` / `MindStack` / `PersonaClone`

---

## 0. 一行要約

成熟した OSS エージェント CLI **Hermes-Agent** を土台に、**①スマホからのリモート承認ゲート**、**②Modal クラウド上の常駐コーディングエージェント**、**③実行ログからの SKILL.md 自動抽出（Skill Distiller）** の3機能を**疎結合アドオンとして増設**した、自己進化型クロスプラットフォーム AI エージェント。

---

## 1. 原指示書からの変更点（確定事項 D-01〜D-15）

原指示書のブループリントをそのまま実装してはならない。以下はユーザー承認済みの司令塔判断。

| ID | 原指示書 | 変更後 | 理由 |
|---|---|---|---|
| **D-01** | `hh-agent/` を新規作成 | **Hermes クローン本体（本フォルダ）を土台とし、`modal_hub/` `hh_hooks/` `mobile_app/` を疎結合アドオンとして増設** | Hermes は承認システム・SKILL.md 体系・シェルフック機構・WebSocket ゲートウェイ・React Web UI を既に備える。新規作成は既存資産を捨てて劣化版を作る行為 |
| **D-02** | クラウドエージェントを自作 | **Hermes CLI を Modal コンテナで実行。UI は Hermes 自身の `serve`/dashboard バックエンドを丸ごとホストする（静的 dist だけの配信は不可）** | §4.6 参照。`web/` の ChatPage は `/api/pty` 等の複数バックエンド API に依存しており、静的配信だけでは動かない |
| **D-03** | `services/corpus2skill.py` を新規実装し「Corpus2Skill」と呼ぶ | **新規 `services/skill_distiller.py`（実行ログ→SKILL.md）として実装。既存 Corpus2Skill（Obsidian→階層Markdown記憶）は MCP 経由の参照専用として温存** | 両者は名前が同じだけの別物。既存の稼働中記憶基盤を改造するリスクを負わない |
| **D-04** | PWA + Web Push (VAPID) | **通知配信＝ntfy.sh、承認操作＝PWA 画面。ただし ntfy には権限を一切載せない（§5）** | iOS の PWA Web Push はホーム画面追加必須かつ不安定。ntfy は iOS/Android ネイティブアプリがあり即日確実に動く |
| **D-05** | Modal 上に Qwen 2.5/3.5 を常設 | **Phase 1 では作らない。`TaskRouter` のインターフェースのみ定義し `QwenBackend` は `NotImplementedError`** | GPU 常駐コスト回避。Phase 2 で `3LLM_Max/modal_app.py` をコピー流用して差し込む |
| **D-06** | 危険操作の判定基準の記述なし | **ルールベース＋リスク3段階（HIGH/MEDIUM/LOW）。HIGH のみスマホ承認。判定ルールは YAML で外出し** | 全 Bash 承認は1タスクで数十回鳴り実用に耐えない |
| **D-07** | `hooks/claude_code_interceptor.py` が全てをフックする | **単一のフックスクリプトで Claude Code と Hermes の両方を賄う。両者は同一のワイヤプロトコル（JSON stdin / `{"decision":"block"}` / exit 2）を採用しているため** | Hermes の `agent/shell_hooks.py` は Claude-Code 互換のシェルフック機構。`%HERMES_HOME%\config.yaml` の `hooks:` に登録するだけで Hermes 本体を一切改変せずに全ツール呼び出しへ介入できる |
| **D-08** | STEP 3 で OpenAI Realtime 音声ゲートウェイを実装 | **Phase 2 へ後回し。Phase 1 では `/api/voice/*` のルータ枠と Function スキーマ定義のみ予約（501 を返す）** | ユーザー確定の Phase 1 スコープに音声は含まない |
| **D-09** | 記述なし | **Modal は単一 `@modal.asgi_app()` + FastAPI。`@modal.web_endpoint` を関数ごとに複数配置しない** | Modal 1.x で `web_endpoint` は非推奨。コールドスタート重複を回避。2HD-Code / 3LLM_MAX / Corpus2Skill / JARVIS 全てと同じ流儀 |
| **D-10** | 認証の記述なし | **クライアント種別ごとに別資格情報。エージェント＝Bearer、PWA＝HttpOnly Cookie ＋ 単回 WS チケット。通知経路には権限を載せない** | 単一共有キーだと、エージェントが実行する不審コードがキーを奪えば偽承認要求を大量生成できる（通知疲れ攻撃） |
| **D-11** | 記述なし | **承認状態は `modal.Dict` に**不変レコード＋書き込み1回限りの決定キー**として保持（compare-and-set は使わない）。監査は1イベント1ファイルの不変 JSON** | `modal.Dict` に「既存値が X なら更新」という CAS API は存在しない。`put(skip_if_exists=True)` のみが原子的。共有 JSONL への複数コンテナ追記は last-write-wins で行が消える |
| **D-12** | `ObsidianBrainGate` が Modal から Obsidian Vault を読み書き | **Modal からは Obsidian に一切アクセスしない。読み取り＝既存 Corpus2Skill MCP 経由／書き込み＝ローカルの Claude Code (Opus 5) が直接行う** | ローカル PC の Obsidian Vault はクラウドから見えない。既存ユーザールール「Obsidian が唯一の正」とも整合 |
| **D-13** | 承認タイムアウト 180 秒 | **ユーザーが操作できる猶予＝150 秒。フック側の内部デッドライン＝170 秒。ホスト側フックタイムアウト＝200 秒** | 「180 秒待機＋ポーリング遅延＋コールドスタート＋リトライ」は 200 秒を超える。超えるとフックが強制終了され、`deny` を返す前に殺される |
| **D-14** | 記述なし | **クラウドエージェントの Hermes は `env_type` を絶対に `"modal"` にしない** | Hermes の `_should_skip_container_guards()` は `env_type in ("singularity","modal","daytona","vercel_sandbox")` のとき危険コマンド承認を**丸ごとスキップ**する（`tools/approval.py:3402`）。Modal 上で動かすからといって `"modal"` を指定すると承認ゲートが無言で無効化される |
| **D-15** | 記述なし | **Hermes のシェルフックは既定で fail-open。`fail_closed: true` を必ず明示する** | `agent/shell_hooks.py` の既定では、フックのクラッシュ・タイムアウト・パース失敗は「警告ログを出して素通り」。承認ゲートでこれをやると障害時に全部通る |
| **D-16** | 抽出したスキルを `~/.hermes/skills/` に保存する（v2） | **抽出物は隔離領域に置き、Hermes が探索するディレクトリには絶対に直接書かない。人間が明示的に promote したものだけが有効化される** | Hermes はスキルディレクトリを**自動スキャン**する（`tools/skills_tool.py:719`）。抽出元は「エージェントが読んだツール出力」であり、プロンプトインジェクションを含みうる。自動配置は「注入された指示が次回以降の全セッションへ自動注入される」永続的バックドアを作る |
| **D-17** | Skill Distiller を Modal 上で動かす（v2） | **Phase 1b の Distiller は**ローカルで動かす**。Modal へは生成済み SKILL.md を publish するだけ** | Hermes の SessionDB は `~/.hermes/state.db`（`hermes_state.py:334`）でローカルにあり、Modal Volume の外。SQLite を Volume に置くと複数コンテナの last-writer-wins で壊れ、open file があると Volume の reload も失敗する。同期経路を作るより、DB のある場所で動かす方が正しい |
| **D-18** | クラウドエージェントの UI に `hermes serve` を使う（v2） | **ブラウザ UI が要るなら `dashboard`。`serve` は使えない** | `serve` は明示的に headless で「no UI build, no SPA mount」（`hermes_cli/main.py:10277`） |
| **D-19** | Scale-to-Zero か常時起動かを保留（v2） | **Scale-to-Zero（`min_containers=0`）で確定。課金ゼロを優先する**（ユーザー確定 2026-08-10） | 通知 SLO は warm 1 秒 / cold 10 秒の 2 本立てとする |
| **D-20** | 記述なし | **シェルフックの許可リスト登録を明示的に行い、起動時にフックが実際に登録されたことを自己診断する** | Hermes のシェルフックは `(event, command)` ごとに初回同意が要る。非対話起動で未許可だと**登録されず警告ログだけ出して素通り**する。Modal 上は TTY が無いため確実に踏む |

---

## 2. 全体アーキテクチャ

```
┌─────────────────────── ローカル PC (Windows) ───────────────────────┐
│  VS Code + Claude Code                     Hermes CLI (ローカル)     │
│      │ PreToolUse hook                          │ pre_tool_call hook │
│      │ (.claude/settings.json)                  │ (%HERMES_HOME%\config.yaml) │
│      └──────────────┬───────────────────────────┘                    │
│                     ▼                                                 │
│        hh_hooks/tool_gate.py  ← 単一スクリプトが両方を処理            │
│            ①ローカルでリスク判定（HIGH 以外は Hub 往復ゼロ）          │
│            ②HIGH のみ Hub へ                                         │
└─────────────────────┼────────────────────────────────────────────────┘
                      │ HTTPS (agent Bearer)
                      ▼
┌─────────────────────── Modal: hh-agent-hub ─────────────────────────┐
│  @modal.asgi_app()  FastAPI                                          │
│                                                                      │
│  routers/approval_gate.py                                            │
│      modal.Dict "hh-agent-approvals"                                 │
│        req:<id>       不変の要求レコード（作成後は書き換えない）      │
│        decision:<id>  put(skip_if_exists=True) の1回勝負             │
│        lease:<id>     put(skip_if_exists=True) の実行権1回勝負       │
│           │ 通知（opaque ID のみ。権限を載せない）                    │
│           ▼  ntfy.sh ──push──▶ スマホ ntfy アプリ ──tap──▶ PWA       │
│                                                                      │
│  routers/cloud_agent.py   Hermes serve バックエンドを丸ごとホスト     │
│                           （single-container / session affinity）    │
│  routers/voice_gateway.py Phase 2（Phase 1 は 501）                  │
│                                                                      │
│  core/router.py  TaskRouter → Claude / MiniMax M3 / Codex / (Qwen)   │
│  services/skill_distiller.py  SessionDB → SKILL.md パッケージ        │
│  services/memory_bridge.py    既存 Corpus2Skill MCP（読み取り専用）   │
│                                                                      │
│  Volume: hh-agent-store   /mnt/hh_store                              │
│    ├── skills/<name>/SKILL.md   抽出されたスキル（ディレクトリ形式）  │
│    └── audit/<YYYY-MM>/<id>.<seq>.json  1イベント1ファイルの不変監査  │
└──────────────────────────────────────────────────────────────────────┘
```

### 記憶の関心分離（絶対原則）

| 置き場 | 入るもの | 入れてはならないもの | 書く主体 |
|---|---|---|---|
| **`~/.hermes/skills/<name>/SKILL.md` + Volume `hh-agent-store/skills/`** | 汎用スキル・再利用パターン・自己改善ルール | プロジェクト固有の仕様 | Skill Distiller（自動） |
| **Obsidian `Projects/H-H-Agent/`** | 設計判断・アーキテクチャ決定・ロードマップ・進捗 | 汎用スキル・実行ログ・生ログ | ローカルの Claude Code のみ |
| **既存 Corpus2Skill（Modal `corpus2skill`）** | Obsidian 由来の階層 Markdown 記憶 | H-H Agent の実行ログ | 既存の `sync.bat`（変更なし） |

**Skill Distiller の出力が Obsidian に書き込まれることは絶対にあってはならない。** Modal 側から Obsidian へのパスは物理的に存在しない（D-12）ため、この分離はアーキテクチャで担保される。加えて §8.1 でパス検証テストを課す。

---

## 3. リポジトリ構成（増設分のみ）

```
Hermes-Hyper-Agent_HHAgent/          ← Hermes クローン本体（上流追従可能に保つ）
├── modal_hub/                       ★新規
│   ├── main.py                      Modal App + FastAPI (@asgi_app)
│   ├── routers/
│   │   ├── approval_gate.py
│   │   ├── cloud_agent.py
│   │   └── voice_gateway.py         Phase 2 予約（501）
│   ├── services/
│   │   ├── skill_distiller.py
│   │   ├── session_reader.py        Hermes SessionDB からの履歴取得
│   │   ├── memory_bridge.py         既存 Corpus2Skill MCP 読み取り
│   │   ├── notifier.py              ntfy.sh 送信
│   │   └── audit.py                 不変監査ログ
│   ├── core/
│   │   ├── config.py
│   │   ├── router.py                TaskRouter（Qwen はスタブ）
│   │   ├── risk.py                  リスク3段階分類器（フックと共有）
│   │   ├── risk_rules.yaml
│   │   ├── security.py              資格情報検証 / 署名 / レート制限
│   │   └── store.py                 modal.Dict / Volume アクセス層
│   └── tests/                       Modal 非依存で回る pytest
├── hh_hooks/                        ★新規（`hooks/` は使わない。※後述）
│   ├── tool_gate.py                 Claude Code / Hermes 共通フック本体（pre_tool_call）
│   ├── journal.py                   Distiller 用ジャーナル記録（post_tool_call）
│   ├── risk.py                      core/risk.py と同一実装（symlink 不可のため生成コピー）
│   └── INSTALL.md                   両方への登録手順
├── mobile_app/                      ★新規
│   └── pwa_approval/
│       ├── index.html / app.js / style.css
│       ├── manifest.webmanifest
│       └── sw.js                    オフライン表示のみ
└── docs/hh-agent/                   ★新規（上流 docs/ と衝突させない）
    ├── 03_Architecture.md           ← 本書
    └── 04_Task_Allocation.md
```

**`hooks/` ではなく `hh_hooks/` を使う理由**: Hermes は `~/.hermes/hooks/` を独自のイベントフック探索パス（`HOOK.yaml` + `handler.py`）として使っており、リポジトリ直下の `hooks/` という名前は将来の上流追加や規約と衝突しうる。名前空間を分ける。

**既存 Hermes ファイルへの変更はゼロ。** 統合はすべて設定ファイル（`%HERMES_HOME%\config.yaml` の `hooks:` ブロック、`.claude/settings.json`）経由で行う。`cli.py` や `tools/approval.py` を編集してはならない。

**訂正（2026-08-11・4セッション目、実装検証で判明）**: 本書は当初 Hermes 側の設定先を「リポジトリ直下の `cli-config.yaml`」としていたが誤り。実際の起動ランチャー `hh_hermes.py` → `hermes_cli.main.main()` が使う設定ローダー（`hermes_cli/config.py` の `load_config()`）は `%HERMES_HOME%\config.yaml` のみを読み、プロジェクトローカルへのフォールバックは無い。`./cli-config.yaml` を読むのは `cli.py` トップレベルの `load_cli_config()` という別のレガシー経路で、`hh_hermes.py` 経由の起動では一切参照されない。以降の記述はすべて `%HERMES_HOME%\config.yaml` を指す。

---

## 4. コンポーネント設計

### 4.1 Modal Hub (`modal_hub/main.py`, `core/config.py`)

```python
app = modal.App("hh-agent-hub")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("fastapi", "uvicorn", "pydantic", "httpx", "websockets",
                 "pyyaml", "gitpython", "anthropic")
)

@app.function(image=image,
              secrets=[modal.Secret.from_name("hh-agent-secret")],
              volumes={"/mnt/hh_store": store_volume},
              min_containers=0, scaledown_window=300)
@modal.asgi_app()
def fastapi_app():
    ...
```

- **型注釈の注意（D-16 の修正版）**: FastAPI のハンドラで**解決できない遅延型注釈を使わない**。`from fastapi import Request` などの型はモジュールスコープで import する。`from __future__ import annotations` そのものは禁止事項ではない（Hermes の `tui_gateway/ws.py` は使用している）。3LLM_MAX で起きた全リクエスト 422 は「遅延評価された `Request` 型がモジュール名前空間に無く、FastAPI がクエリパラメータと誤認した」ケース。**起動時に全ルートを検証する pytest を必ず置く**（§8.1）。
- **コールドスタートと SLO（D-19・ユーザー確定 2026-08-10）**: **`min_containers=0`（Scale-to-Zero）で確定。課金ゼロを優先する。** したがって「通知 1 秒以内」は **warm パス限定の SLO** とし、cold パスは 10 秒以内を別 SLO とする（§8.3）。この 2 本立てを受け入れ条件とし、cold で 1 秒を満たさないことを不具合として扱わない。

### 4.2 リスク分類器 (`core/risk.py`, `core/risk_rules.yaml`)

**ツール種別ごとに正規化してから判定する。** シェルコマンド専用の検出器へ `Write`/`Edit` の dict をそのまま渡してはならない。

```python
def classify(tool_name: str, tool_input: dict) -> Risk:
    """Risk(level, rule_id, reason, normalized_target) を返す。"""
```

| ツール | 正規化 | 判定 |
|---|---|---|
| `Bash` / `terminal` | `tool_input["command"]` を取り出す | Hermes の `detect_dangerous_command()` を一次判定に使う ＋ 自前ルール |
| `Write` / `Edit` / `NotebookEdit` / `file_write` | 対象パスと差分を取り出す | パスルール（`.env`、`secrets/`、`.git/`、`settings.json` 等）で判定 |
| **上記以外で副作用のありうる未知のツール** | — | **既定で HIGH に格上げ（escalate）** |
| 明示的に読み取り専用と分かっているツール | — | LOW |

**Hermes 既存検出器の呼び出し契約（Codex 検証済み）**:

```python
is_dangerous, pattern_key, description = detect_dangerous_command(command)
#  ↑ 必ず 3 要素にアンパックする。
#    戻り値はタプルなので、(False, None, None) も truthy。
#    `if detect_dangerous_command(cmd):` と書くと全コマンドが HIGH になる。
```

`detect_dangerous_command()` は Hermes 承認システムの**一部**にすぎない（hardline 判定・ユーザー deny ルール・tirith は別経路）。「Hermes の承認システム全体を再利用している」とは主張しない。あくまで**優秀な正規表現検出器の再利用**であり、最終的なポリシーは `risk_rules.yaml` が持つ。

| レベル | 挙動 | 例 |
|---|---|---|
| **HIGH** | スマホ承認必須。応答まで**ブロック** | `rm -rf`、`git push`（全般）、シークレット/`.env` の読み書き、課金 API 呼び出し、`DROP TABLE`、外部への `curl -X POST`、`sudo`、**未知の副作用ツール** |
| **MEDIUM** | 実行は許可し、**通知のみ**（承認待ちしない） | 通常の `git commit`、`npm install`、`pip install`、単一ファイル削除 |
| **LOW** | 素通り。ログのみ | 読み取り系、`ls`、`grep`、テスト実行 |

**偽陽性は許容し、偽陰性を潰す。** `echo "rm -rf"` が HIGH になるのは「通知が1回増える」だけで安全側に倒れる。

### 4.3 承認ゲート (`routers/approval_gate.py`)

#### データモデル

`modal.Dict` には compare-and-set API が存在しない。原子性が保証されるのは `put(key, value, skip_if_exists=True)` だけ。したがって **レコードを書き換えず、キーの新規作成1回勝負で決着させる**（D-11）。

**承認レコード系**

| キー | 内容 | 書き込み |
|---|---|---|
| `req:<approval_id>` | **不変**。要求の全内容 | 作成時 1 回のみ |
| `decision:<approval_id>` | `{"decision":"approved"\|"rejected", "at":float, "by":"pwa"}` | `put(skip_if_exists=True)` で **1 回だけ成功**。2 人目・2 回目は必ず負ける |
| `lease:<approval_id>` | `{"claimed_at":float, "claimant":str}` | `put(skip_if_exists=True)`。**実行権も 1 回だけ** |
| `idem:<subject>:<idempotency_key>` | `approval_id` | `put(skip_if_exists=True)`。リトライの重複要求を吸収。**キーはトークン subject で名前空間を切る**（他クライアントのキーと衝突させない） |
| `notify:<approval_id>` | `{"sent_at":float, "attempts":int}` | 通知の送達状態（後述の P2 対策） |

**認証補助状態系**（v2 で「Dict は 3 種のみ」と書いたのは誤り。これらにも原子的な永続ストアが要る）

| キー | 内容 |
|---|---|
| `wsticket:<ticket_id>` | 単回 WS チケットの使用済みマーク（`put(skip_if_exists=True)` で消費） |
| `pairing:<code_hash>` | ペアリングコードの使用済みマーク（同上） |
| `rate:<subject>:<hour_bucket>` | レート制限カウンタ |
| `revoked:agent:<token_id>` | エージェントトークンの失効 |
| `revoked:pwa:<session_id>` | PWA セッションの失効・ログアウト |

#### 所有権の照合（必須）

`poll` / `claim` / `complete` **および `request` の idempotent 再利用**は、**リクエストのトークン subject が `req:<id>` の `source` / `session_id` / `workspace_id` と一致することを必ず確認する**。一致しなければ 404（存在を漏らさないため 403 ではなく 404）。

これが無いと:

- 漏洩した／別セッションのエージェントトークンで**他エージェントの approved な lease を先取りして DoS できる**（承認は通ったのに実行権を奪われ、正規のエージェントは永久に実行できない）
- 偽の完了監査を書き込める
- `idem:` を subject で名前空間化していても、照合が無ければ**同じ idempotency key を送るだけで他セッションの `approval_id` を引ける**

「クライアントごとに別トークン」はオブジェクト単位の照合とセットで初めて分離になる。**トークンを分けただけでは分離されていない。**

#### 保持期間と GC

| 対象 | 保持 | 削除方法 |
|---|---|---|
| `req:` / `decision:` / `lease:` / `notify:` | 決着から 7 日 | 2 段階 GC（下記） |
| `idem:` | 24 時間（tool-call ID の再利用を将来にわたり禁止しないため） | 同上 |
| `wsticket:` / `pairing:` | 1 時間 | 同上 |
| `rate:` | 2 時間 | 同上 |
| 監査ファイル | 12 か月 | 月次ディレクトリごとアーカイブ |

**2 段階 GC**: `gc:index:<day>` に当日作成した approval_id のリストを追記し、GC はこのインデックスだけを見て削除対象を決める。

- **`modal.Dict` の全走査と `len()` を使ってはならない。** `len()` は高コストで、Dict は最大 10 万件。
- **`/api/approval/pending` も全走査で実装してはならない。** 保留中の approval_id を保持する `pending:index` キーを別に持ち、そこから引く。
- GC は Modal の scheduled function（1 日 1 回）で回す。

#### タイムアウトの監査記録

`timeout` は純導出であり「誰も書き込まない状態」なので、放っておくと監査に残らない。一方 `poll` のたびに書くと重複する。
→ **`decision:<id>` と同じ write-once 機構を使う**: 最初に timeout を観測した者が `decision:<id>` に `{"decision":"timeout","by":"system"}` を `put(skip_if_exists=True)` で書き込み、**成功した者だけが監査を 1 行書く**。これで「必ず 1 回だけ記録される」。

`req:<id>` の中身：

```python
{
  "approval_id": str,          # UUID4
  "idempotency_key": str,      # クライアント生成。リトライで同一値
  "source": "claude_code" | "cloud_agent",   # サーバーが資格情報から確定（自己申告を信じない）
  "session_id": str,           # 同上
  "tool_name": str,
  "payload": {...},            # 実行される内容そのもの（コマンド全文 / パス / 差分）
  "payload_sha256": str,       # payload の正規化 JSON のハッシュ
  "cwd": str,
  "workspace_id": str,
  "base_revision": str | None, # git HEAD（取得できた場合）
  "risk": "HIGH", "rule_id": str, "reason": str,
  "created_at": float,
  "grace_deadline": float,     # created_at + 150（ユーザーが操作できる期限）
}
```

**`status` はレコードに持たない。時刻と `decision:` キーの有無から純関数で導出する**：

```python
def status_of(req, decision, lease, now) -> str:
    if decision is None:
        return "pending" if now <= req["grace_deadline"] else "timeout"

    # 遅延して書き込まれた決定は無効。コンテナ停止や競合で
    # grace_deadline を過ぎてから decision: が入りうる。
    if decision["at"] > req["grace_deadline"]:
        return "timeout"

    if decision["decision"] != "approved":
        return decision["decision"]    # rejected | timeout

    if lease is not None:
        return "claimed"

    # ★承認は無期限に有効ではない。claim されないまま期限を過ぎたら失効。
    if now > req["claim_deadline"]:
        return "timeout"
    return "approved"
```

**`claim_deadline`（`req:` に不変で持つ）= `created_at + 180`。** これが無いと「承認したがフックが落ちて claim されなかった」承認が、24 時間有効な Bearer トークンで**数時間後に再利用できてしまう**。`claim` エンドポイント側でも `now <= claim_deadline` を再確認する（`status_of` の結果だけに依存しない）。

これで「読み取り時にレコードを書き換える」副作用が消え、どのコンテナが応答しても答えが一致する。バックグラウンドタスクによる期限切れ処理も不要になる（Modal のコンテナは途中で消えるため、そもそも当てにできない）。

**期限直前の競合**: `decision:` の書き込みが `grace_deadline` を越えていた場合、サーバーは書き込みを受理せず `timeout` を返す。承認は「猶予内に到達した書き込み1回」のみが有効。

#### 単回実行権（TOCTOU 対策）

`approved` は「実行してよい」ではなく「実行権を1回取得できる」を意味する。

```
pending ──approve──▶ approved ──claim lease (1回勝負)──▶ claimed ──▶ 実行
   │                     │                                    └─▶ consumed / failed（監査）
   ├──reject───────▶ rejected
   └──150s 無応答──▶ timeout（＝拒否扱い）
```

**実行直前の再検証（必須）**

lease 取得後、実行主体は承認時と実行時の状態が一致することを確認する。**`payload_sha256` / `cwd` / `workspace_id` / `base_revision` の 4 項目だけでは不十分**である — これらは「実行予定の入力」と「作業ディレクトリの識別子」を表すにすぎず、**対象ファイルの実体が差し替えられたことを検出できない**。

具体的な破れ方: `workspace/output.txt` への書き込みを承認した 150 秒の待機中に、別プロセスが同パスを機密ファイルへの symlink に差し替える。4 項目はすべて一致したまま、承認していないファイルが上書きされる。

したがって、`Write` / `Edit` など**ファイルを対象とする操作**では、承認要求の作成時と実行の直前の両方で次を取得・照合する:

| 項目 | 内容 |
|---|---|
| 解決済み実パス | `os.path.realpath()`（symlink を辿った先） |
| `lstat` 識別子 | `st_dev` + `st_ino`（Windows では `nFileIndex` 相当）。**`lstat` であって `stat` ではない**（リンク自体を見る） |
| 対象内容ハッシュ | 上書き前のファイル内容の SHA-256（preimage） |

さらに「照合してから開く」の間に差し替えられる隙を塞ぐ。**`O_NOFOLLOW` は Windows に無いため使わない**（`FILE_FLAG_OPEN_REPARSE_POINT` は ctypes での `CreateFileW` 直呼びが必要になり移植性が悪い）。代わりに **open してから `os.fstat(fd)` で「実際に開いたファイル」の識別子を取り、照合済みの値と突き合わせる**:

```python
fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
try:
    actual = os.fstat(fd)
    if (actual.st_dev, actual.st_ino) != approved_identity:
        raise Mismatch()          # 開いた先が承認時と違う → 中止
    # ここから先は fd に対してのみ書く。path を再度使わない。
finally:
    os.close(fd)
```

**開いた後の fd に対してのみ操作し、パス文字列を二度と使わない。** これで「検証したファイル」と「書き込むファイル」が同一であることが保証される。`O_NOFOLLOW` と違い、symlink 自体は辿るが、辿った先が承認時と同じ実体であることを確認するので目的は達成される。

1 項目でも一致しなければ実行を中止し `mismatch` として監査に記録する。

**この節は実装難度が高い。** Phase 1a の時点で完全な実装が困難な場合は、**`Write`/`Edit` の HIGH 判定を「承認しても実行しない（deny のみ）」に落とす**ことを許容する。「検証が甘いまま実行する」より「実行できない」方が安全側であり、`Bash` の承認は先に価値を出せる。この判断は実装時に BLOCKED として報告し、司令塔が決める。

#### エンドポイント

| メソッド | パス | 呼び手 | 認証 | 説明 |
|---|---|---|---|---|
| GET | `/health` | 任意 | なし | 疎通のみ。内部情報を返さない |
| POST | `/api/approval/request` | エージェント | Agent Bearer | 要求登録＋通知。**`idempotency_key` 必須**。`approval_id` と `grace_remaining_seconds` を即返す（ブロックしない） |
| GET | `/api/approval/poll?id=` | エージェント | Agent Bearer | 導出 status を返す。5 秒間隔＝ハートビート |
| POST | `/api/approval/claim` | エージェント | Agent Bearer | 実行権の取得（1回勝負）。成功時のみ実行してよい |
| POST | `/api/approval/complete` | エージェント | Agent Bearer | `consumed` / `failed` / `mismatch` を監査に記録 |
| GET | `/api/approval/pending` | PWA | PWA Cookie | 保留中一覧 |
| POST | `/api/approval/respond` | PWA | PWA Cookie ＋ CSRF トークン | `{approval_id, decision}` |
| WS | `/ws/approval?ticket=` | PWA | 単回 WS チケット | 状態変化の push |
| POST | `/api/pwa/pair` | PWA | ペアリングコード | HttpOnly Cookie を発行（初回のみ） |

**ロングポーリングを採用しない理由**: Modal の ASGI コンテナで 150 秒ブロックする接続を張ると、コールドスタート／スケールダウンとタイムアウトが噛み合って無言のハングを作る。5 秒ポーリングなら 1 承認あたり最大 30 リクエストで済み、状態はすべて `modal.Dict` にあるのでどのコンテナが応答しても正しい。

#### 通知の送達保証

`req:` の作成と ntfy 送信は別事象であり、両者の間で落ちうる。

- 通知の送達状態は `notify:<approval_id>` に持つ（`req:` とは分ける）。
- **`request` がリトライで既存の `approval_id` を返す場合でも、`notify:` が未成功なら再送する。** v2 の設計だと「登録は成功・通知は失敗」の後にリトライしても同じ ID を返すだけで、**二度と通知されず必ずタイムアウトする**穴があった。
- 送信は最大 3 回・指数バックオフ。`notify:` に試行回数を記録し、成功・全失敗のいずれも監査に残す。
- **全失敗した場合はエージェントの `poll` に `notify_failed: true` を含めて返す。** エージェントは即座に deny して「通知が届いていないので却下した」と報告する。ユーザーが気づけないまま 150 秒待たせない。

**WebSocket は「速くするための最適化」であって「正しさの前提」にしない。** 複数コンテナに分散すると、コンテナ A の WS はコンテナ B での状態変化を知れない（Modal に分散 pub/sub は無い）。したがって **PWA は WS 接続中でも 10 秒間隔の低頻度ポーリングで必ず状態を突き合わせ、カウントダウンはクライアント側の `grace_deadline` から自前で描画する**。WS が届かなくても画面は正しく期限切れになる。

### 4.4 共通ツールゲート (`hh_hooks/tool_gate.py`)

**1 本のスクリプトが Claude Code と Hermes の両方のフックとして動く。** 両者は同一のワイヤプロトコルを持つ（Codex 検証済み）。

| | Claude Code | Hermes |
|---|---|---|
| 登録先 | `.claude/settings.json` の `hooks.PreToolUse` | `%HERMES_HOME%\config.yaml` の `hooks:` ブロック |
| イベント名 | `PreToolUse` | `pre_tool_call` |
| stdin | `{tool_name, tool_input, session_id, cwd, ...}` | 同形 ＋ `extra` |
| ブロック方法 | `{"hookSpecificOutput":{"permissionDecision":"deny",...}}` または exit 2 | `{"decision":"block","reason":"..."}` または exit 2 |
| 障害時の既定 | — | **fail-open（要注意）** |

スクリプトは `hook_event_name` を見て出力形式を切り替える。**ブロックは常に「exit 2 ＋ stderr に理由 ＋ stdout に両形式で通る JSON」の三重掛けで行う**（どちらのホストでも確実に止まるように）。

**処理フロー**:

1. `tool_name` が読み取り専用と既知のもの → 即 allow（Hub 往復ゼロ）
2. `risk.classify()` でローカル判定。LOW → allow、MEDIUM → allow ＋ fire-and-forget 通知
3. HIGH → `/api/approval/request`（`idempotency_key` はツール呼び出し ID から決定的に生成）→ 5 秒間隔で `/api/approval/poll`
4. `approved` → `/api/approval/claim` → 成功したら allow ／ 失敗（他が取得済み）なら deny
5. `rejected` / `timeout` / `mismatch` → deny（理由を明記）

**タイムバジェット（D-13）**:

| 区間 | 予算 | 時計 |
|---|---|---|
| ユーザーが操作できる猶予（`grace_deadline`） | 150 秒 | サーバーの wall clock |
| フック内部デッドライン（これを過ぎたら必ず deny JSON を返して終了） | 170 秒 | **フックの `time.monotonic()`** |
| ホスト側フックタイムアウト（`.claude/settings.json` の `timeout`） | 200 秒 | ホスト |

内部デッドラインをホストタイムアウトより 30 秒短くすることで、「deny を返す前にフックが強制終了される」事故を防ぐ。

**時計に関する規則（4 つの時計が混在するため、曖昧にすると必ず壊れる）**:

1. **フック側のローカル待機は `time.monotonic()` で測る。** wall clock を使うと NTP 同期やスリープ復帰で予算が飛ぶ。
2. **`urllib.request` の全呼び出しに明示的な `timeout=` を渡し、その値を「残り予算」でクランプする。** これを怠ると、内部デッドラインのループが 170 秒を判定していても、最後の HTTP 要求が返らないまま 200 秒を超えてプロセスが殺され、deny JSON を返せない（内部デッドラインが機能しない）。
3. **サーバーは絶対時刻の `grace_deadline` ではなく、相対値 `grace_remaining_seconds` を返す。** PWA のカウントダウンは端末時計に依存させない。
4. **猶予 150 秒の起点は「サーバーが `req:` を登録した瞬間」**。コールドスタートで数秒かかった分はフック側の 170 秒予算から差し引かれるので、フックは自分の monotonic 予算とサーバーの残り秒数の**小さい方**で待つ。
5. **バイパスファイルの TTL 判定に未来の mtime を受け入れない。** 現在時刻より未来の mtime を持つファイルは無効として扱う（時計変更による無期限バイパスを防ぐ）。

**MEDIUM の通知を fire-and-forget にしてはならない**: フックは短命プロセスであり、投げっぱなしの非同期送信はプロセス終了で破棄されうる。MEDIUM は「200ms のタイムアウトを付けた同期送信を 1 回試み、失敗したら**明示的に諦めてローカルログに残す**」とする。黙って消えるのが最悪。

**Claude Code 側の必須設定**:

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash|Write|Edit|NotebookEdit",
      "hooks": [{
        "type": "command",
        "command": "python \"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/hh_hooks/tool_gate.py\"",
        "timeout": 200
      }]
    }]
  }
}
```

**Hermes 側の必須設定（`%HERMES_HOME%\config.yaml`。既定は `C:\Users\<user>\AppData\Local\hermes\config.yaml`）**:

**`hooks:` は「イベント名をキーとする辞書」である。リストで書いてはならない**（`agent/shell_hooks.py:353`）。`_parse_hooks_block()` は `isinstance(hooks_cfg, dict)` でない入力に対して**エラーも警告も出さず空リストを返す**。つまりリスト形式で書くと**フックが 0 件登録され、何の異常も表示されないまま承認ゲートが存在しない状態**になる。

```yaml
hooks_auto_accept: true          # ★下記「許可リスト」参照。非対話起動では必須
hooks:                           # ★辞書。トップレベルは「イベント名: [エントリ...]」
  pre_tool_call:
    - command: python C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/hh_hooks/tool_gate.py
      matcher: ".*"              # matcher は pre_tool_call / post_tool_call でのみ有効
      fail_closed: true          # ★D-15。書かないと障害時に全部素通りする
      timeout: 200
  post_tool_call:
    - command: python C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/hh_hooks/journal.py
      timeout: 5                 # Distiller 用。fail_closed は付けない（フェイルオープンでよい）
```

エントリのフィールドは `command`（必須・文字列）／`matcher`（正規表現文字列）／`timeout`（int）／`fail_closed`（bool。`failClosed` も可）。**不正なエントリは警告ログを出してスキップされるだけで、起動は成功する。**

**許可リスト（D-20。ここを外すと承認ゲートが黙って無効になる）**

Hermes のシェルフックは `(event, command)` の組ごとに**初回使用時の同意**を要求し、`~/.hermes/shell-hooks-allowlist.json` に記録する。**許可リストに無く、かつ非対話（TTY 無し）で `accept_hooks` も指定されていない場合、フックは登録されず、警告ログを出すだけで素通りする**（`agent/shell_hooks.py`）。

Modal 上のクラウドエージェントは当然 TTY を持たないため、この経路を踏むと**承認ゲートが存在しないまま Hermes が動く**。防ぐには次のいずれかを必ず行う:

- `%HERMES_HOME%\config.yaml` に `hooks_auto_accept: true` を書く、または
- `HERMES_ACCEPT_HOOKS=1` を設定する、または
- `--accept-hooks` を付けて起動する、または
- `~/.hermes/shell-hooks-allowlist.json` をイメージに焼き込む

**さらに、起動時の自己診断でフックが実際に登録されたことを確認し、未登録ならエージェントを起動させない**（§8.2 の検証項目）。「設定を書いたから大丈夫」で済ませない。

**フェイルセーフ**: Hub 到達不能・不正レスポンス・内部デッドライン超過はすべて **deny（フェイルクローズ）**。

**緊急脱出口**: Hub 障害でローカル作業が完全停止するのを防ぐバイパスを用意するが、**環境変数では行わない**（環境変数はエージェント自身が設定でき、プロンプトインジェクションによる権限昇格経路になる。Hermes が `HERMES_YOLO_MODE` をモジュール import 時に凍結しているのと同じ理由）。代わりに:

- バイパスは**ローカルの保護ファイル** `%USERPROFILE%\.hh-agent\bypass` の存在で判定する。
- **TTL 30 分**。ファイルのタイムスタンプが古ければ無効。
- 有効時は毎回 stderr に警告を出し、**Hub とは独立したローカル監査ファイル**にバイパス使用を記録する。

**性能要件**: フックは全ツール呼び出しごとに Python プロセスを起動する。**標準ライブラリのみで書き**（HTTP は `urllib.request`。`requests` / `httpx` を import しない）、起動〜allow 返却の時間を §8.2 で計測する。

**2026-08-11 改訂（実測に基づく）**: 当初の「一律 200ms 未満」は達成不能であることが実測で判明した。

| 経路 | 予算 | 根拠 |
|---|---|---|
| **非シェル系の allow**（`Read` / `Grep` / `Glob` / `Write` / `Edit` / 未知ツール） | **200ms 未満** | Hermes の検出器を読み込まないため到達可能 |
| **シェル系の allow**（`Bash` 等） | **300ms 未満** | Hermes `tools.approval` の import 単体で実測 **215〜220ms**。これが下限 |

実測値: LOW リスク経路の中央値 **240ms**（8 回、最小 227ms）。内訳の支配項は `from tools.approval import detect_dangerous_command` の import コスト。

**対処（確定）**: `risk.py` の `tools.approval` import を**モジュール直下からシェル分岐の中へ遅延させる**。非シェル系のツール呼び出しはこの import を一切踏まなくなる。実運用のツール呼び出しは読み取り系が多数を占めるため、これで大半の呼び出しが 200ms 予算に収まる。

**やってはいけない対処**: Hermes の危険コマンド検出ロジックを `hh_hooks/` 側へ複製・自前実装して時間を稼ぐこと。**Hermes 本体との判定のズレを生む**。`sync_hook_modules.py` と `test_hook_module_sync.py` は、まさにその複製ズレを防ぐために存在する。速度のために検出の一致性を捨てる取引はしない。

### 4.5 モバイル承認 PWA (`mobile_app/pwa_approval/`)

- 単一ページ。フレームワークなし。Modal から静的配信。**外部ホストへのリクエストを一切行わない**（CDN・フォント・画像すべてインライン）。
- **通知経路（権限を載せない）**: Hub → ntfy.sh（本文は「承認待ち 1 件」＋ **opaque な approval_id のみ**）→ スマホ ntfy アプリ → タップで PWA を開く → **PWA 側で認証済み Cookie を確認**して初めて内容を表示。ntfy の URL を見ただけでは承認できない。
- **画面要素**: 実行予定の内容（等幅・折り返し）／差分（追加緑・削除赤）／リスクバッジ／**クライアント側で計算する残り秒数のカウントダウン**／「承認」[緑・大]／「却下」[赤・大]。
- WS が繋がっていても 10 秒ポーリングで状態を突き合わせる（§4.3）。
- ダークモード対応（`prefers-color-scheme`）。ボタン最小 56px 高。
- `Referrer-Policy: no-referrer` を応答ヘッダに付与。
- **iOS 実機での確認事項**（自動テスト不能・ユーザー依頼タスク）: ntfy 通知の到達時間、Safari での表示・操作、Cookie がアプリ間遷移で保持されるか。

### 4.6 Modal クラウドコーディングエージェント (`routers/cloud_agent.py`)

**Phase 1c。2026-08-13 に PoC①②とも実機で通過し、`docs/hh-agent/08_Phase1c_Spec.md` に実装契約を確定した。以下は確定済みの方針（詳細・根拠は同ファイル参照）。**

- **実行体**: Hermes CLI 本体。Modal Image に本リポジトリを焼き込む。
- **UI**: **`web/` の静的 dist だけを配信する方式は成立しない**（Codex 検証済み）。`web/src/pages/ChatPage.tsx` の主チャットは JSON-RPC 直結ではなく `/api/pty` で `hermes --tui` を起動する xterm 画面であり、SPA は REST API・WS チケット・`/api/ws`・`/api/pub`・`/api/events` にも依存する。さらに `tui_gateway/server.py` のセッションは**プロセス内 dict** で保持される。
  → **Hermes 自身の `dashboard`（`hermes_cli.web_server:app`）をそのまま丸ごと ASGI としてホストする。** その前段に H-H Agent の認証は置かない（`08_Phase1c_Spec.md` §2.3。脅威モデルが異なるため Hermes 本体の `_SESSION_TOKEN` のみで完結させる）。
  **`serve` は使えない**（D-18）。`serve` は明示的に headless で「no UI build, no SPA mount」（`hermes_cli/main.py:10277`）。ブラウザ UI が必要なら `dashboard`、または「別途ビルドした SPA ＋ `serve`」の 2 択であり、`serve` 単体でブラウザ UI は出ない。
- **セッションアフィニティ: `max_containers=1` による単一コンテナ固定で解決する**（2026-08-13 改訂。旧「1 セッション = 1 Modal Sandbox」方式は撤回）。
  セッション状態がプロセス内 dict にある、という制約自体は元の指摘のとおり正しい。だが本プロジェクトは個人単一ユーザー運用で同時アクセスは実質 1 系統のため、**コンテナを常に 1 個に固定すればそもそも複数コンテナ間のルーティング問題自体が発生しない**。これにより Sandbox 生成・URL/WS プロキシ・再接続・Sandbox 単位認証・スケールダウン復旧という 5 項目の個別設計が丸ごと不要になった。加えて `max_containers=1` は「Modal Volume への複数コンテナ同時書き込みで行が消える」（既知の落とし穴）と Hermes 自体の単一プロセス前提（PID 管理・SQLite WAL・多重起動検出）を両方構造的に回避する副次効果も持つ。詳細は `08_Phase1c_Spec.md` §2.2。
  （**`min_containers` はセッションアフィニティではない**という旧記述の指摘自体は引き続き正しい。`max_containers=1` は「warm container 数」ではなく「上限」を固定する設定であり、これが単一コンテナを保証する）
- **承認の合流（D-14、最重要。変更なし）**: `%HERMES_HOME%\config.yaml` の `pre_tool_call` フック（§4.4）を、Phase1c ではコンテナ初回起動時にシーディングして有効化する（HERMES_HOME が空の新規 Volume から始まるため。詳細は `08_Phase1c_Spec.md` §3）。**`set_approval_callback()` は使わない**（スレッドローカルであり、実行ワーカースレッドでは `None` になる。またコールバックには redact 済みコマンドしか渡らず、cwd・差分・session_id を受け取れない）。
- **D-14 の再掲（最重要）**: Hermes を `env_type="modal"` で起動してはならない。承認ガードが丸ごとスキップされる。

**PoC の合否条件（2026-08-13 実機で両方通過。詳細数値は `08_Phase1c_Spec.md` §0）**:

1. Hermes の依存関係一式を載せた Modal Image が **5GB / ビルド 10 分**以内に収まるか → **通過**（実測 632MB / 75.93 秒）
2. `dashboard` バックエンド（`hermes_cli.web_server:app`）が Modal の ASGI 上で起動し、`/api/pty` が機能するか → **通過**（`/api/pty` の WebSocket で実際に `hermes --tui` が起動し ANSI 出力を確認）

両方通過したため Phase 1c は Phase 2 送りにせず前進する。本実装の詳細は `docs/hh-agent/08_Phase1c_Spec.md` を参照。

### 4.7 Skill Distiller (`services/skill_distiller.py`, `services/session_reader.py`)

```python
class SkillDistiller:
    def extract_skill_from_session(
        self, session_id: str, git_diff: str
    ) -> Optional[SkillDraft]: ...
    def save_skill(self, skill_name: str, content: str) -> Path: ...
```

- **入力（Codex 検証済みの修正 ＋ 追加調査で確定）**: `sessions/<id>.jsonl` は**存在しない**。Hermes は per-session JSONL を廃止し **SQLite の `SessionDB`（`hermes_state.py`）** に移行している。trajectory 保存は既定で無効、有効にしても `trajectory_samples.jsonl` / `failed_trajectories.jsonl` への集約追記であり、セッション単位ではない。

  入力は **2 系統**を組み合わせる:

  **(1) 会話・ツール列 → `SessionDB`**（`services/session_reader.py`）。確定済みの API とスキーマ:

  | 用途 | API |
  |---|---|
  | セッション情報 | `SessionDB.get_session(session_id) -> dict \| None` |
  | メッセージ列（挿入順） | `SessionDB.get_messages(session_id, limit=None, after_id=None) -> list[dict]` |

  `messages` テーブルの利用カラム（`hermes_state_common.py:266`）: `id` / `role` / `content` / `tool_call_id` / `tool_calls` / `tool_name` / `finish_reason` / `timestamp` / `active`。**並びは `timestamp` ではなく AUTOINCREMENT `id`**（Hermes 自身が WSL2 のクロック巻き戻しを理由にそうしている）。既定では `active = 1` の行のみ返る（rewind でソフト削除された行は除外される）。

  **(2) ツールの成否 → `post_tool_call` フックが書く Distiller ジャーナル**

  `messages` テーブルには**ツール実行の成否を表すカラムが無い**（`role='tool'` 行の本文から推測するしかない）。抽出条件③「失敗 → 修正 → 成功」の判定を本文の推測に頼ると精度が出ない。

  → Hermes の **`post_tool_call` シェルフック**を使う。このイベントの `extra` には `status`（`"ok" \| "error" \| "blocked"`）・`error_type`・`error_message`・`duration_ms`・`tool_call_id`・`turn_id` が**そのまま載っている**（`agent/shell_hooks.py`）。これを `hh_hooks/journal.py` が `~/.hh-agent/journal/<session_id>.jsonl` へ 1 行 1 イベントで追記する。`tool_call_id` で SessionDB のメッセージ行と突き合わせる。

  ジャーナル書き込みは `fail_closed: false`（既定のまま）でよい。**スキル抽出は失敗しても安全性に影響しないため、承認ゲートとは逆にフェイルオープンで正しい。**
#### 実行場所（D-17）

**Phase 1b の Distiller はローカル PC で動かす。** Modal 上では動かさない。

`SessionDB` の実体は `~/.hermes/state.db`（`hermes_state.py:334`）でローカルにあり、Modal Volume `/mnt/hh_store` の外にある。SQLite ファイルを Volume に置く案は採らない — 複数コンテナからの更新が Volume の last-writer-wins で壊れ、open file がある状態では Volume の reload も失敗する。**DB がある場所で動かすのが正しい。** Modal へは生成済みの SKILL.md を publish するだけ（`POST /api/skills/publish`）。

- **抽出条件（すべて満たすときのみ抽出。緩めるとゴミが溜まる）**:
  1. セッションが成功終了している
  2. ツール呼び出しが 5 回以上
  3. 「失敗 → 修正 → 成功」の遷移を含む
  4. 既存スキルとの類似度が閾値未満

  **①③の判定に `sessions` テーブルの `end_reason` を使ってはならない。** `end_reason` は圧縮・モデル切替・切断も表すため「成功」を意味しない（`hermes_state_common.py:207`）。判定は **`hh_hooks/journal.py` が書く `status` フィールドを唯一の根拠**とする:

  - **①成功終了** = 「セッション末尾 3 件のツール呼び出しがすべて `status == "ok"`」かつ「`blocked` が 1 件も無い」
  - **③失敗→修正→成功** = 「同一 `tool_name` に対して `status == "error"` の後に `status == "ok"` が現れる組が 1 つ以上ある」

  この 2 つの定義は**設計上の確定事項**であり、実装者が変えてよいヒューリスティックではない。変更したい場合は本設計書を改訂する。
- **抽出 LLM**: `claude-haiku-4-5` ＋ **Message Batches API**（即時性不要、50% 割引）。Corpus2Skill と同じ流儀。
- **出力形式**: Hermes のスキルは**ディレクトリ内の `SKILL.md`** が必須（`tools/skills_tool.py`）。`skills/<name>.md` は自動発見されない。

#### 隔離と昇格（D-16。この節は安全性要件であり、省略も簡略化もしてはならない）

Hermes は**スキルディレクトリを自動スキャンして発見したスキルを有効化する**（`tools/skills_tool.py:719`）。一方 Distiller の入力は「エージェントがセッション中に読んだツール出力」であり、**プロンプトインジェクションを含みうる**。抽出物を Hermes の探索パスへ直接書くと、注入された指示が次回以降の全セッションへ自動注入される**永続的バックドア**になる。

したがって:

1. **Distiller は Hermes が探索するどのディレクトリにも書かない。** 出力先は隔離領域 `~/.hh-agent/skills_quarantine/<name>/SKILL.md`（およびその Volume 上の写し）。
2. **昇格は人間の明示操作のみ。** `hh skill promote <name>` が隔離領域から `~/.hermes/skills/<name>/` へコピーする。**自動昇格の実装を作らない。** **注記（2026-08-11・`07_Phase1b_Spec.md` §4.2）**: 統合 CLI `hh` は本コードベースに未実装のため、Phase 1b では `python scripts/hh_skill_promote.py <name>` を暫定の正式コマンドとする。将来 `hh` CLI ができた際は同スクリプトの中核関数を呼び出す配線にし、別実装を作らない。
3. `promote` は実行前に SKILL.md 本文を全文表示し、確認を求める。
4. **`skill_name` の検証**: `^[a-z0-9][a-z0-9-]{1,48}$` に完全一致しないものは拒否。加えて保存前に `Path(dest).resolve()` が隔離ルート配下であることを確認する（`../` によるパストラバーサル対策。regex だけに頼らない）。
5. **原子的書き込み**: 同一ファイルシステム上の一時ファイルへ書いてから `os.replace()` でリネームする。書きかけの `SKILL.md` が探索されうる状態を作らない。
6. **同名衝突**: 既存の隔離スキルと同名なら上書きせず `<name>-2` 等へ退避し、衝突を監査に記録する。類似度判定と保存の間の TOCTOU を避けるため、判定と保存は単一プロセス内で連続して行う。
7. **総数上限**: 隔離領域のスキル数が 200 を超えたら新規抽出を停止し、警告する（無制限増加の防止）。
  ```markdown
  ---
  name: <kebab-case>
  description: "<1行>"
  version: 0.1.0
  author: H-H Agent Skill Distiller
  license: MIT
  platforms: [linux, macos, windows]
  metadata:
    hermes:
      tags: [...]
      related_skills: []
    hh_agent:
      distilled_from: <session_id>
      distilled_at: <ISO8601>
  ---
  ```
- **受け入れテスト**: **昇格後に** Hermes の実パーサ（`tools/skills_tool.py` の探索経路）へ読ませてスキル一覧に現れることを確認する。ファイルが存在することを確認するだけのテストは不合格。
  加えて**逆方向のテストが必須**: **昇格前の隔離領域に置かれたスキルが、Hermes のスキル一覧に一切現れないこと**を確認する（D-16 の担保）。
- **Obsidian への書き込み経路を実装しない**（D-12・絶対原則）。

### 4.8 TaskRouter (`core/router.py`)

| タスク種別 | ルーティング先 | Phase 1 |
|---|---|---|
| 高度な概念設計・複雑なロジック | Claude (Anthropic API) / MiniMax M3 | ✅ |
| 決定論的なコード生成・レビュー | OpenAI Codex | ✅ |
| プライベート／低遅延／オフライン | Modal ホストの Qwen | ⛔ `NotImplementedError`。**黙って別バックエンドにフォールバックしない** |

**フォールバック禁止の理由**: 「プライベート指定なのに黙って外部 API に送る」は情報漏洩そのもの。未実装は明示的に失敗させる。

### 4.9 Memory Bridge (`services/memory_bridge.py`)

既存 Corpus2Skill の MCP サーバー（Modal `corpus2skill`）を**読み取り専用**で叩くクライアント。`get_memory_index` / `read_memory_skill` / `search_memory` のみ。`add_new_memory` は呼ばない。

---

## 5. セキュリティ

### 5.1 資格情報（クライアント種別ごとに分離）

| クライアント | 資格情報 | スコープ |
|---|---|---|
| エージェント（Claude Code フック / クラウド Hermes） | `Authorization: Bearer <agent_token>`。**クライアントごとに個別発行**、短命（24h）、`source` と `session_id` はトークンに紐づく | `request` / `poll` / `claim` / `complete` のみ。`respond` は**呼べない** |
| PWA | 初回ペアリングコードで交換した **HttpOnly / Secure / SameSite=Strict Cookie**。WS は単回・TTL 30 秒のチケットをクエリで渡す（ブラウザの WebSocket API は任意ヘッダを付けられないため） | `pending` / `respond` / `ws` のみ |
| ntfy 通知 | **権限なし**。opaque な approval_id を運ぶだけ | — |

- `source` / `session_id` は**サーバーがトークンから確定する**。リクエストボディの自己申告を信じない。
- `respond` には CSRF トークン（Cookie とは別に発行、ボディで送る）を要求し、`Origin` ヘッダを検証する。
- 比較はすべて `hmac.compare_digest`（定数時間）。
- **レート制限**: エージェントトークンごとに「HIGH 承認要求 20 件/時」。超過分は拒否し監査に記録する。通知疲れによる誤承認を誘発する攻撃を防ぐ。

### 5.2 通知

- **ntfy 認証（2026-08-11 改訂・ユーザー確定）**: 公開トピック＋32 文字以上のランダム文字列で先行する。`NTFY_TOKEN` は**任意**（`core/config.py` の `_OPTIONAL_KEYS`）で、未設定時は Authorization ヘッダそのものを付けない（`services/notifier.py`）。ntfy には権限もコマンド本文も載せない設計のため、トピック名が漏れても「承認待ちがある」以上の情報は漏れない。**トピック名自体は秘密として扱う**（Secret に格納。ログ・Obsidian・Git に書かない）。将来アクセストークン付きの ntfy 認証へ切り替える場合は `NTFY_TOKEN` を設定するだけで有効化される（コード変更不要）。
- 通知本文は「承認待ち 1 件」＋ approval_id ＋ リスクレベルのみ。**コマンド本文を通知に載せない**（ntfy 運営者・通知ログ・ロック画面に露出するため）。内容は認証後の PWA でのみ表示する。
- PWA 表示時にシークレット様パターン（`sk-`, `ghp_`, `AKIA` 等）をマスクする。

### 5.3 監査（`services/audit.py`）

- **1 イベント 1 ファイルの不変 JSON**: `audit/<YYYY-MM>/<approval_id>.<seq>.json`。共有 JSONL への複数コンテナ追記は Modal Volume の last-write-wins で行が消えるため禁止（D-11）。書き込み後は必ず `volume.commit()`。
- 記録対象: 要求・通知送信（成否）・決定・lease 取得・実行完了/失敗/mismatch・タイムアウト・認証失敗・レート制限拒否・バイパス使用。
- **承認の監査書き込みに失敗したらフェイルクローズ**（`claim` を成功させない）。証跡なしで危険操作が走る状態を作らない。

### 5.4 その他

- Secret の実値はコード・Obsidian・Git に一切書かない。**Modal Secret の作成は Codex 経由で行う**（グローバルルール）。
- Hub の資格情報を、エージェントが実行する非信頼コマンドの子プロセス環境へ継承させない。

---

## 6. Modal リソース

**既存アプリと同居する。既存リソースには一切触れない。**

| 種別 | 名前 | 備考 |
|---|---|---|
| App | `hh-agent-hub` | 新規 |
| Volume | `hh-agent-store` | 新規。マウント先 `/mnt/hh_store` |
| Dict | `hh-agent-approvals` | 新規 |
| Secret | `hh-agent-secret` | 新規 |
| GPU | **なし** | Phase 1 は CPU のみ。GPU 指定を書いてはならない |

### Secret `hh-agent-secret` のキー

| キー | 用途 |
|---|---|
| `HH_AGENT_TOKEN_SIGNING_KEY` | エージェントトークンの署名鍵 |
| `HH_PWA_SESSION_KEY` | PWA Cookie / CSRF / WS チケットの署名鍵 |
| `HH_PAIRING_CODE` | PWA 初回ペアリング用（使い切り。ペアリング後に無効化） |
| `NTFY_TOPIC` | ntfy トピック名（秘密扱い） |
| `NTFY_TOKEN` | ntfy アクセストークン（**任意**。2026-08-11 決定で公開トピック運用のため空。§5.2 参照） |
| `ANTHROPIC_API_KEY` | Skill Distiller（Batch API） |
| `C2S_API_KEY` | 既存 Corpus2Skill 参照用 |

### 絶対に触れてはならない既存リソース

Volume `models_cache` / `hf-cache` / `2hd-code-evolved` / `jarvis-memory` / `c2s-skills-store`、
Secret `vllm-api-key` / `3llm-max-secret` / `jarvis-secret` / `c2s-secret`、
App `multi-ai-coder-agent` / `3llm-max` / `jarvis-backend` / `corpus2skill`。

---

## 7. フェーズ計画

| Phase | 内容 | 完了条件 | 見積 |
|---|---|---|---|
| **1a** | 承認ゲート（Hub + PWA + ntfy + 共通ツールゲート） | VS Code の Claude Code で `rm -rf` を試み、スマホに通知が届き（warm 1 秒 / cold 10 秒以内）、「却下」で処理が止まる。Hub 停止時に deny されることも確認 | 3〜4 日 |
| **1b** | Skill Distiller | 模擬セッションから SKILL.md が生成され、**Hermes の実パーサでスキル一覧に現れる**こと。Obsidian へ漏洩していないことをパス検証テストで証明 | 2〜3 日 |
| **1c** | Modal クラウドエージェント | PoC 通過（§4.6）が前提。Web UI からタスク投入 → ストリーミング → 危険操作でスマホ承認 → 完了 | 5〜8 日（PoC 次第で Phase 2 送り） |
| **2** | OpenAI Realtime 音声 / Qwen バックエンド / マルチセッション | — | 未着手 |

**1a だけで日常的な価値が出る**（外出先から VS Code のエージェントを承認できる）ため、1a を確実に完成させてから 1b に進む。

---

## 8. テスト計画

### 8.1 単体（Modal 非依存で回る `pytest`）

- **FastAPI 起動時ルート検証**: 全ルートが正しくスキーマ解決されること（422 事故の再発防止）
- `core/risk.py`:
  - HIGH/MEDIUM/LOW の分類。**偽陰性を重点的に**（`rm  -rf`、`rm -fr`、`git push -f`、改行分割、`$(...)` 経由）
  - `detect_dangerous_command()` の戻り値を**タプルとしてアンパックしていること**（`(False, None, None)` を truthy 判定していないことの回帰テスト）
  - `Write`/`Edit` の dict 入力が正しく正規化されること
  - **未知のツール名が HIGH に格上げされること**
- `core/security.py`: 署名検証、TTL 切れ、単回使用、定数時間比較、レート制限、`source` の自己申告が無視されること
- `approval_gate`:
  - `status_of()` が純関数であること（同じ入力で必ず同じ出力、副作用なし）
  - `decision:` の二重書き込みが 2 回目に必ず失敗すること
  - `lease:` の二重取得が失敗すること
  - `grace_deadline` 超過後の `decision` 書き込みが拒否されること
  - `idempotency_key` が同一なら同じ `approval_id` を返すこと
  - `payload_sha256` / `cwd` / `base_revision` の不一致で `mismatch` になること
  - **`decision.at > grace_deadline` の遅延決定が `timeout` になること**
  - **`claim_deadline` 超過後の `claim` が失敗すること**（承認の無期限再利用の防止）
  - **別 subject のトークンで `poll` / `claim` / `complete` / idempotent `request` が 404 になること**
  - **対象ファイルが symlink に差し替えられた場合に `mismatch` になること**（realpath + `lstat` 識別子 + 内容ハッシュ）
- `skill_distiller`:
  - 抽出条件 4 つの境界
  - YAML frontmatter の妥当性
  - **出力パスに Obsidian Vault が含まれないことのパス検証テスト**
  - **`<name>/SKILL.md` のディレクトリ形式で保存されること**
- `audit`: 監査書き込み失敗時に `claim` が成功しないこと

### 8.2 統合

- ツールゲート → Hub → 承認 → claim → 実行 の一気通貫（ntfy はモック）
- Hub 到達不能時に deny になること（フェイルクローズ）
- Hermes 側フックで `fail_closed: true` が効いていること
- **`%HERMES_HOME%\config.yaml` の `hooks:` を実際に `_parse_hooks_block()` へ通し、登録件数が期待どおりであること**（辞書形式の誤りは警告すら出ないため、目視確認では検出できない）
- **Hermes 起動時の自己診断でフックが未登録ならサービスが起動しないこと**（D-20）
- **Hermes を `env_type="modal"` で起動していないことの検証**（D-14）
- フックのプロセス起動〜allow 返却が、**非シェル系 200ms 未満 / シェル系 300ms 未満**であること（§4.3 の 2026-08-11 改訂を参照）
- **昇格後の SKILL.md が Hermes のスキル一覧に現れること**
- **昇格前（隔離領域）の SKILL.md が Hermes のスキル一覧に一切現れないこと**（D-16 の担保）

### 8.3 ユーザー実機確認（自動化不能・必ずユーザーに依頼）

1. スマホ ntfy への通知到達時間（**warm 1 秒以内 / cold 10 秒以内**の 2 つの SLO で計測）
2. iOS Safari で PWA 承認画面が表示・操作でき、ntfy アプリからの遷移で Cookie が保持されること
3. 「却下」時にローカルの Claude Code が実際に止まること

---

## 9. 既知の落とし穴（実装者は必読）

1. **Hermes は `env_type` が `"modal"` / `"singularity"` / `"daytona"` / `"vercel_sandbox"` のとき危険コマンド承認を丸ごとスキップする**（`tools/approval.py:3402`）。Modal 上で動かすからといって `env_type="modal"` にしない（D-14）。
2. **Hermes のシェルフックは既定で fail-open。** `fail_closed: true` を明示しないと、フックが落ちた瞬間に全部素通りになる（D-15）。
3. **`set_approval_callback()` はスレッドローカル**（`tools/terminal_tool.py`）。起動スレッドで登録しても実行ワーカースレッドでは `None`。またコールバックが受け取るのは redact 済みコマンドと説明だけで、cwd・差分・session_id は渡らない。
4. **`detect_dangerous_command()` はタプルを返す。** `(False, None, None)` も truthy。
5. **Hermes のスキルはディレクトリ内の `SKILL.md`。** `skills/<name>.md` は発見されない。
6. **Hermes は per-session JSONL を持たない。** 履歴は SQLite の `SessionDB`。
7. **`modal.Dict` に compare-and-set は無い。** 原子的なのは `put(skip_if_exists=True)` だけ。
8. **Modal Volume への複数コンテナ同時追記は行が消える。** 1 イベント 1 ファイルにする。
9. **FastAPI ハンドラで解決できない遅延型注釈を使うと全リクエストが 422 になる。** 型はモジュールスコープで import する（`from __future__ import annotations` 自体は禁止ではない）。
10. **Modal の Secret 更新は `modal app stop` → `modal deploy` し直さないと反映されない。**
11. **Codex はテスト/ベンチを実行しない。** 静的レビューを「動作確認済み」の根拠にしない。実行検証は Claude Code 側が行う。
12. **Codex はネットワーク障害でも exit 0 を返す。** 完了確認はファイル更新時刻や `git log` など実体で行う。
13. **Codex のサンドボックスは workdir 外への書き込みと外部通信を拒否する。** Obsidian 更新を Codex に投げない。
14. **PowerShell/cmd の日本語リテラルは文字化けする。** スクリプトが読むファイルは ASCII のみで書く。日本語パスを扱う処理は Python の `subprocess.run(list)` でラップする。
15. **黙って空を返す実装は原因を隠す。** 想定外のレスポンス形状は例外を投げる。
16. **`git push` は Codex 経由**（グローバルルール）。Claude Code が直接 push しない。
17. **`hooks:` は辞書。リストで書くとフック 0 件になり、警告すら出ない**（`agent/shell_hooks.py:353`）。
18. **シェルフックは許可リスト未登録だと黙って登録されない。** 非対話起動では `hooks_auto_accept: true` 等が必須。登録されたことを起動時に自己診断する（D-20）。
19. **抽出したスキルを Hermes の探索ディレクトリへ直接書かない。** 自動スキャンで有効化され、プロンプトインジェクション由来の指示が永続化する（D-16）。
20. **`hermes serve` は headless で SPA を配信しない。** ブラウザ UI が要るなら `dashboard`（D-18）。
21. **`min_containers` はセッションアフィニティではない。** warm コンテナ数の指定にすぎない。
22. **`modal.Dict` の `len()` と全走査を使わない。** 高コストで、上限 10 万件。一覧はインデックスキーから引く。
23. **承認は無期限に有効ではない。** `claim_deadline` を持たせないと、承認済みで claim されなかったものが後から再利用できる。
24. **`payload_sha256` + `cwd` + `HEAD` では symlink 差し替えを検出できない。** realpath + `lstat` 識別子 + 内容ハッシュが要る。

---

## 11. 未仕様事項（実装着手前に埋めること。実装者に推測させない）

**Phase 1a 分（下記 1〜10）は `docs/hh-agent/05_Phase1a_Spec.md` で確定済み**（2026-08-11）。実装者は同ファイルを実装契約として読むこと。Phase 1b / 1c 分は未確定であり、着手前に司令塔が埋める。実装者がこれらに遭遇したら BLOCKED として報告すること。

**Phase 1a 分 — ✅ `05_Phase1a_Spec.md` にて確定**

1. `/request` `/poll` `/respond` `/claim` `/complete` の完全なスキーマ・HTTP ステータス・リトライ可能エラー → §1
2. 正式な状態遷移表（decision 期限、claim 期限、lease 保持者のクラッシュ、complete 重複） → §2
3. canonical JSON 定義とハッシュを計算する主体 → §3
4. `workspace_id` の定義（Git 管理外・symlink・UNC） → §4
5. `risk_rules.yaml` スキーマ、ツール名エイリアス、read-only allowlist、MCP/カスタムツールの扱い → §5
6. エージェントトークンの発行・更新・保存・失効・ローテーション、子プロセス継承の**残存リスク受容** → §6
7. ペアリング・Cookie・CSRF・WS チケット → §7
8. バイパスファイルの作成・削除・署名・**残存リスク受容** → §8
9. PWA の CSP / Origin / **`innerHTML` 禁止の XSS 要件** → §9
10. 監査の ID 採番・原子的作成・redaction・失敗時の順序保証 → §10

**Phase 1b 分（下記 11〜14）は `docs/hh-agent/07_Phase1b_Spec.md` で確定済み**（2026-08-11）。実装者は同ファイルを実装契約として読むこと。

11. ✅ Distiller の起動契機（手動 / セッション終了時 / 定期） → §1
12. ✅ Batch API の完了回収とリトライ、失敗時の扱い → §2
13. ✅ 類似度モデル・閾値・インデックスの保存先 → §3
14. ✅ 同名スキルのバージョニング・マージ・promote の具体手順、Volume とローカルの同期方式 → §4

**Phase 1c 着手前に必須**

15. `dashboard` を Modal ASGI へ統合する具体的方式、sticky routing、再接続、スケールダウン復旧。
16. Sandbox のライフサイクル・プロキシ・認証（§4.6 の 5 項目）。

**Corpus2Skill Memory Provider 連携着手前に必須**

17. 下記 §13 参照。

---

## 12. 実装担当の分割方針

**タスク単位ではなくファイル単位で所有者を決める**（並列実装時の衝突ゼロを実測済みの方式）。詳細は `docs/hh-agent/04_Task_Allocation.md`。

| 担当 | 比率 | 担当領域の性格 |
|---|---|---|
| **Claude Code Sonnet 5** | 30% | セキュリティ・状態機械・既存 Hermes との接続部（`core/security.py`, `core/risk.py`, `routers/approval_gate.py`, `services/audit.py`, `hh_hooks/*`）＝ 間違うと安全性が壊れる箇所 |
| **MiniMax M3** | 70% | UI・定型実装・テスト（`mobile_app/pwa_approval/*`, `services/skill_distiller.py`, `services/notifier.py`, `services/memory_bridge.py`, `core/config.py`, `core/store.py`, `modal_hub/tests/*`） |
| **Codex** | — | 全コードのレビュー（`codex exec review --uncommitted`）＋ 設計書レビュー ＋ GitHub push ＋ Modal Secret 作成 |

---

## 13. Corpus2Skill Memory Provider プラグイン（設計案・未着手・2026-08-15）

**目的**: Windowsネイティブ Hermes と Modal 上の Hermes（Phase1c dashboard）を別々にチャット利用したとき、一方が知っていてもう一方が知らないという記憶の差異が生じる。Hermes公式の「Memory Provider」プラグイン機構（`agent/memory_provider.py` の `MemoryProvider` ABC）を使い、Corpus2Skill自身をバックエンドとするプロバイダを実装してこれを解消する。バックエンド側（Lane B の新設）の設計は `Corpus2Skill/doc/03_Architecture.md` §12 を参照（両ファイルは対で読むこと）。

### M-05: プラグインの配置先は「Bundled」ではなく「Project Provider」

Hermesのプラグイン発見順位（Bundled → User → Project → Package）のうち、リポジトリ直下 `plugins/memory/<name>/` は **「新規プロバイダには閉じている」**（公式ドキュメント上の制約）。このリポジトリはNousResearch本家からのupstream同期（592コミットマージの実績あり）を継続する運用のため、本家が予約している領域には触れない。

**採用: `./.hermes/plugins/corpus2skill/`（Project Provider、`HERMES_ENABLE_PROJECT_PLUGINS=1` が必要）。** git管理下に置け、Codexレビューの対象になり、Modal イメージへは既存の `add_local_dir` 系の仕組みでそのまま含められる。D-01（Hermes本体を無改変のまま疎結合アドオンを増設する）にも合致する。

Windows側（ネイティブHermesアプリ、このリポジトリのクローンではない別インストール）には、同じプラグインフォルダを `$HERMES_HOME\plugins\corpus2skill\`（User Provider）へ手動コピーする。コード本体は1つ、配置経路だけが2通り。

### M-06: プラグインが実装するフック

| フック | 用途 | 頻度 | 遅延許容度 |
|---|---|---|---|
| `is_available()` | `CORPUS2SKILL_API_KEY` の存在確認のみ（ネットワーク呼び出し禁止、契約どおり） | 起動時 | — |
| `initialize(session_id, **kwargs)` | api_key・base_url・session_id を保持 | 起動時 | — |
| `prefetch(query, *, session_id="")` | Corpus2Skillの `search_memory`（Lane A）＋ `journal_recall`（Lane B）を呼び、結果を合成してコンテキストへ注入 | 毎ターン | 同期・軽量（両方ともLLM不使用の安価な検索） |
| `sync_turn(user, assistant, *, session_id="", messages=None)` | `journal_write`（Lane B）へ書き込み | 毎ターン | **非同期必須**（公式契約どおりデーモンスレッド） |
| `on_session_end(messages)` | v1では no-op（Lane B→Lane Aの昇格は見送り、`Corpus2Skill/doc/03_Architecture.md` §12の未確定事項D参照） | セッション終了時 | — |
| `get_tool_schemas()` / `handle_tool_call()` | 明示ツール `corpus2skill_search(query, limit)` を1つだけ公開（自動prefetchとは別に、エージェントが能動的に検索したい場合用） | エージェントのツール呼び出し時 | — |

### M-07: risk_rules.yaml への影響

**`prefetch`・`sync_turn`・`on_session_end` はHermesのフレームワークが自動的に呼ぶものであり、エージェントのツール呼び出し（tool-calling）としては現れない。** よって `risk_rules.yaml` の分類対象には**ならない**（BUG-5と混同しないこと）。

**`get_tool_schemas()` で公開する `corpus2skill_search` のみ**、エージェントが明示的に呼び出せるツールとして現れるため、`modal_hub/core/risk_rules.yaml` と `hh_hooks/risk_rules.yaml` の両方（BUG-5修正時と同じ2箇所、`scripts/sync_hook_modules.py`で再生成）に `read` カテゴリとして追加する。**書き込み系ツール（`journal_write`・`add_new_memory`相当）はエージェント呼び出し可能なツールとして一切公開しない**（get_tool_schemasに含めない＝存在しないのと同じ扱い）。

### 未確定事項（実装着手前に埋める）

| # | 内容 |
|---|---|
| E | Modal dashboard コンテナの `HERMES_HOME`（Volume `hh-agent-dashboard-home`）に `HERMES_ENABLE_PROJECT_PLUGINS=1` を設定する具体的な箇所（Dockerfile環境変数か、起動スクリプトか） |
| F | Windows側への手動コピー手順を一度きりの手順書にするか、簡易インストールスクリプトにするか |

**E・Fとも実装をブロックしない。** プラグイン本体の実装・テストはローカルのfake経由で先行できる。

---

## 14. スキル同期レーン（Lane C・設計案・**CLEARED FOR IMPLEMENTATION**・2026-08-16）

**CLEARED FOR IMPLEMENTATION**（2026-08-16、Codex 設計レビュー計 9 巡を経て確定。Phase 1b の 7 巡を上回る回数だが、この機能は 2 つの信頼境界（Windows／Modal）をまたぐ署名・レプリケーション設計であり、Phase 1b より扱う攻撃面が広いため妥当な巡数と判断する）。9 巡目時点で Critical 0 件・High 0 件。残った Medium 2 件のうち 1 件は本節内で修正済み、もう 1 件（`sync_pull()` の分類フェーズがロック取得前に行われることに起因する理論上の極小レース窓）は明示的にバックログとして受容し、根拠を S-10 手順0 に記載済み（個人・低頻度運用で実害確率が無視できるほど小さく、当たっても静かな破損ではなく衝突検出・通知に帰着するため）。バックエンド側（Corpus2Skill の Lane C 新設）の設計は `Corpus2Skill/doc/03_Architecture.md` §13 を参照（**両ファイルは対で読むこと**。§13 も同じ 9 巡のレビューを経て整合を確認済み）。

**実装フェーズへの引き継ぎ**: 次のフェーズはメイン実装 = DeepSeek、サブ実装 = MiniMax M3、レビュー = Codex の体制で進める（本節末尾「並列委任のための依存順序」を参照）。実装担当者は本節の S-05〜S-14・新規/変更ファイル表をそのまま実装契約として扱ってよい。

### 14.0 解こうとしている問題

Hermes の「使うたびに賢くなる」体験には**2本の独立した学習経路**がある。

| 経路 | 実体 | 2インスタンス間の共有状況 |
|---|---|---|
| 会話レベルの記憶 | Corpus2Skill Memory Provider プラグイン（§13・Lane A/B） | **解決済み**（2026-08-16 デプロイ済み） |
| スキル（手続き的知識） | Skill Distiller → 隔離 → `hh_skill_promote.py` で昇格（§4.7・D-16） | **未解決。これが本節の対象** |

昇格の**配置先**は `scripts/hh_skill_promote.py:_hermes_skills_root()`（実体は `hermes_constants.get_skills_dir()`）であり、**これは呼び出し元インスタンス自身のローカルなスキルディレクトリを返す**。（`skill_quarantine.py:140-147` の `_existing_hermes_scan_dirs()` は配置先ではなく、「隔離／ステージング領域が Hermes のスキャン対象と重なっていないか」を検査するための別物である。2026-08-16 Codex レビュー Low 指摘で用語を分離した。）したがって Windows ネイティブ Hermes（`%HERMES_HOME%` = `C:\Users\Haruki\AppData\Local\hermes`）で昇格したスキルは Modal ダッシュボード（Volume `hh-agent-dashboard-home`）のスキル一覧に一切現れず、逆も同じ。使い込むほど 2 インスタンスの「賢さ」が乖離していく。

**運用上の非対称性（2026-08-16 第5巡 Codex レビュー High 指摘で発見。S-08・S-08b で詳述）**: 上記「逆も同じ」はプロトコル上（受信側の検証ロジック上）は正しいが、**実際の運用としては Windows→Modal が主経路になる**。署名鍵 `HH_AGENT_TOKEN_SIGNING_KEY` は Windows にしか存在しない（C-3・S-06c）ため、Modal 発の promote（quarantine の確認・署名）は必ず Windows 側で行う必要がある。**当初案（2026-08-16 第6巡 Codex レビュー確定時点）は人間本人が `modal shell` で署名鍵を持つコンテナへ直接アタッチする運用だったが、ユーザーがこの運用変更を明示的に却下した**（2026-08-16「１の運用変更は了承しない」）。代わりに、Hub（`modal_hub`）が新設する読み取り専用エンドポイント（S-08b）を Windows 側から叩き、確認・署名は従来どおり Windows のローカル TTY で完結させる「リモート確認・署名」方式に差し替えた。**エージェントによる代行は不可**（C-3・D-16。TTY 確認は必ず人間本人が Windows 上で行う。2026-08-16 第6巡 Codex レビュー High 指摘。この禁止は差し替え後も変わらず有効）。プロトコルは origin を区別しないが、**v1 の実運用としては（署名鍵の所在ゆえに）非対称であることを認識した上で読むこと**。

`services/memory_bridge.py` はこの穴を塞がない。あれは Distiller の novelty 判定を補助するための**読み取り専用**サイドチャネル（D-03）で、`connect()` は未接続のまま常に例外を投げる骨格である。**Lane C の実装で `memory_bridge.py` を書き換えて書き込み機能を足してはならない** — 「読み取り専用であること」自体がテストで固定されている契約であり、Lane C は別モジュール（`services/skill_sync.py`）として新設する。

**08_Phase1c_Spec.md §1 の非スコープ「ローカル Hermes とのスキル同期（HERMES_HOME は完全独立）」との関係**: あれは Phase 1c の範囲の宣言であり、本節がその制約を**スキル1本に限って**解除する。`HERMES_HOME` を丸ごと同期する話ではない（`config.yaml`・`state.db`・`.env` は引き続き完全独立）。同期するのは `<name>/SKILL.md` だけである。

### S-05: 同期の単位と対象は「promote を通ったもの」だけ

- 同期単位は **`<name>/SKILL.md` 1ファイル**。スキルディレクトリ内の付随ファイル（`scripts/`・`assets/` 等）は v1 の対象外。Distiller が生成するのは SKILL.md 単体（§4.7「出力形式」）であり、v1 の同期対象は Distiller 由来のスキルに限られるため、これで漏れは出ない。
- **`~/.hermes/skills/` を丸ごと走査して同期する実装を作らない。** そこには Hermes 同梱スキル・手動導入スキル・upstream 同期で入ったスキルが混在し、**来歴（provenance）が不明なもの**が同期経路に載る。Lane C に載せてよいのは「`hh_skill_promote.py` の全文表示・sha256 提示・TTY 確認を通過した」ものだけであり、**その証明は署名付き promote receipt の検証に一元化する**（S-06b・S-10 手順3。`~/.hh-agent/promote_log.jsonl` は改ざん検知の無い単なる JSONL であり、それ自体を来歴の証明として使ってはならない — 2026-08-16 第9巡 Codex レビュー Medium 指摘で訂正: この段落は初期の要約が残ったままで、後段 S-06(1)・S-10 手順3・S-14 の「`promote_log.jsonl` の記載を根拠にしない」という確定済みルールと矛盾していた）。

### S-06: なぜ受信側の再確認なしで自動反映してよいのか（安全性の論理・この節が本設計の核心）

ユーザー確定事項（2026-08-16）: **受信側で改めて人間の確認は求めない。** 表面上これは D-16／D-20（抽出スキルを Hermes の探索ディレクトリへ直接書かない・プロンプトインジェクションの永続化を防ぐ）と矛盾して見えるため、なぜ安全かを明示する。

D-16 が禁じているのは「**未確認の**コンテンツが Hermes の探索パスへ自動流入すること」である。Distiller の入力はエージェントがセッション中に読んだツール出力であり、注入された指示を含みうる — だから隔離し、人間が全文を読むまで探索パスへ出さない。**Lane C を流れるコンテンツは、定義上どこかのインスタンスで既にその全文確認を1回通過している。** よって Lane C は新規の無確認書き込み経路ではなく、**既に人間が承認済みのコンテンツの複製経路**である。人間が「このスキルを使う」と決めた対象が、その人が使う2台目の Hermes にも現れる、というだけのことに過ぎない。

**ただしこの論理は、以下の条件（条件0 と 1〜5 の計 6 つ）がすべて実装で守られている限りでのみ成立する。1つでも崩れると論理ごと崩れるため、実装者はこれらを弱めてはならない。**

**条件0（2026-08-16 Codex レビュー Critical 指摘を受けて新設。S-06b で詳述）: 「人間が確認済みである」ことは、Corpus2Skill への到達経路ではなく `HH_AGENT_TOKEN_SIGNING_KEY` による署名（promote receipt）で証明する。** 旧版の論証は「Lane C に書けるのは正規の promote フローだけ」を暗黙の前提にしていたが、その前提を支えていたのは Corpus2Skill の**固定 Bearer 1本だけ**だった。この Bearer は Memory Provider プラグインとして**両インスタンスの Hermes に配置され、毎ターンの journal 書き込みで使われている**（§13）。すなわち「エージェントが触れる場所に置かれた、広く配布された資格情報」であり、これ1本で「スキャンディレクトリへの自動書き込み」を守るのは**強度が釣り合わない**。漏えい時に悪意ある SKILL.md を両インスタンスへ自動配布できてしまう。**Corpus2Skill を信頼境界の外へ出し、輸送層としてのみ扱う**のが正しい設計である（S-06b）。

1. **push できるのは「promote receipt を持つ内容」だけである。** 経路は 2 つあり、**どちらも receipt を必須とする**:
   - **即時 push**（S-08）: promote 成功直後の同一プロセス内。送信するバイト列は `read_quarantined_skill()` が返し、人間へ表示し digest を提示した**まさにその `content_bytes`** をそのまま使う（ファイルを読み直さない＝表示と送信の間の TOCTOU を作らない）。
   - **reconcile push**（S-10）: 即時 push がネットワーク断等で失敗した分を後から埋める。**後からローカルを走査する経路なので、`promote_log.jsonl` の記載を人間確認の証明として使ってはならない**（あれは改ざん検知の無い単なる JSONL である。2026-08-16 Codex レビュー High 指摘）。reconcile push は `~/.hh-agent/promote_receipts/<name>/current` が指す receipt（S-06b「保存と検証」の版管理形式）を検証し、**現在のローカル本文のダイジェストと receipt が一致するものだけ**を送る。一致しなければ送らず `skipped(no-valid-receipt)` を監査に残す。
2. **Lane C はエージェントが呼べるツール面に一切現れない。** Corpus2Skill 側は Lane C を **MCP ツールとして公開せず REST のみ**とする（§13 M-07 の「書き込み系ツールを `get_tool_schemas()` に含めない」より一段強い措置。journal_write は MCP に存在するが Lane C は存在自体させない）。書き込みは**読み取り用の既存 Bearer では行えない** — Lane C への書き込み（push / events-ack）は専用の `C2S_SKILL_WRITE_KEY` を要求する（S-09・`Corpus2Skill/doc/03_Architecture.md` §13 S-04）。**Memory Provider プラグインが持つ `CORPUS2SKILL_API_KEY` では Lane C に書き込めない**（2026-08-16 第2巡 Codex レビュー High 指摘: 初版は節によって記述が食い違っていた）。書き込み鍵の供給元は **`.hh-signing.env`（Windows。S-06c の唯一の正）**／`hh-agent-secret` 相当の Modal Secret（Modal 側同期 Function）に限り、**エージェントが動くプロセスの環境には置かない**（S-06b の C-3 と同じ理由）。未設定なら push を試みず、その旨を警告して終了する（黙って読み取り鍵にフォールバックしない）。**エージェントが自分自身の恒久スキルを書き換える経路を作らないこと**が D-20 の核心であり、Lane C はこれを構造的に不可能にする。
3. **受信側は Lane C の内容を無検証で信用しない。** 「信頼済みコンテンツの複製」という主張は**転送の完全性が保たれている限り**で成立するので、完全性は受信側で独立に再検証する（S-10 手順4）。**その検証の中核が promote receipt の検証であり、これに落ちたものは何があっても書き込まない。** 検証に落ちたものは書き込まずスキップし、監査に残したうえで ntfy 通知を出す（S-06b。receipt 不一致は「単なる転送事故」ではなく「Lane C に正規でない書き込みが起きた」ことを意味するため、黙ってスキップするだけでは足りない）。
4. **隔離（未 promote）は同期しない。** Hub の `POST /api/skills/publish` が書く `skills_quarantine/<name>/SKILL.md`（`routers/skills.py:_skill_rel_path`）と Lane C を同じ名前空間・同じストアに混ぜない。「未確認」と「確認済み」が並ぶと、どちらの信頼レベルの物か判断する責務が読み手側へ移り、いずれ取り違える。
5. **同期を止められる手段が常に存在する。** 事故・侵害が疑われたときに「まず止める」ができない自動配布経路を作らない（S-12 の緊急停止スイッチ・denylist）。

### S-06b: promote receipt（署名付き来歴証跡・2026-08-16 新設、同日 第2巡レビューで全面改訂）

**目的**: 「この SKILL.md は promote フローを通った」ことを、Corpus2Skill を信頼せずに受信側だけで検証できるようにする。これにより Corpus2Skill は**信頼境界の外の輸送層**になり、その資格情報が漏えいしても悪意あるスキルを配布できなくなる。

#### 署名鍵をどこに置くか（C-3 との衝突。この項が S-06b の設計上いちばん重要）

**2026-08-16 第2巡 Codex レビュー Critical**: 初版は「Modal ダッシュボードの ASGI コンテナに `hh-agent-secret` をアタッチして receipt を検証する」としていた。これは **`modal_dashboard/app.py` が明記する C-3（`hh-agent-secret` は `refresh_dashboard_agent_token` にのみアタッチし、モデルが書いたコマンドを実行する `dashboard_server` には決してアタッチしない）を逆転させる**ものだった。HMAC は対称鍵であり、**検証できる者は署名もできる**。エージェントが動くコンテナに鍵を置けば、エージェントは任意の本文に正規の receipt を作れてしまい、D-20 の「無確認の永続化経路」が完成する。**初版の設計は誤りであり、撤回する。**

したがって:

- **`dashboard_server`（エージェントが動くコンテナ）には署名鍵を絶対にアタッチしない。C-3 をそのまま維持する。**
- **Modal 側の receipt 検証とローカル配置は、`hh-agent-secret` を持つ別 Modal Function `sync_dashboard_skills` で行う**（`refresh_dashboard_agent_token` が既に確立し、レビュー済みの前例と同じ形。同じ Volume `hh-agent-dashboard-home` を `/opt/data` にマウントする）。エージェントが動くプロセスは**鍵にも検証ロジックにも触れない**。
- この変更に伴い、**S-10 の「ASGI プロセス内デーモンスレッド」案は撤回する**（確定事項 G への影響は S-10 の実行タイミング表に記載。定期実行という要件自体は `schedule=modal.Period(hours=8)` で満たす）。
- **Windows ネイティブ側の限界（残存リスク R-1・ユーザー判断済み＝受容。下記参照）**: Windows では Hermes エージェントも同期スクリプトも**同じユーザー権限**で動くため、プロンプトインジェクションを受けたエージェントは原理的に `.hh-signing.env`（署名鍵・書き込み鍵の格納先。S-06c）を読み、任意の本文に正規の receipt を作って push できる。**この 1 点において、Windows 側では S-06 の安全性の論証は成立しない。** 2026-08-16 第3巡 Codex レビュー Critical 指摘。正確に切り分けると:

  - **新しく生まれるリスクではない部分**: 侵害された Windows エージェントは、Lane C が無くても `%HERMES_HOME%\skills\<name>\SKILL.md` を直接書ける（同一ユーザー権限のファイル書き込み）。**そのインスタンス自身への永続化**は Lane C とは無関係に元から可能である。
  - **Lane C が新しく作るリスク（これが本質）**: **横展開（lateral movement）**。侵害された Windows インスタンスが、**もう一方の Modal インスタンスへ恒久スキルを自動配布できるようになる**。1 台の侵害が 2 台に広がる経路は、Lane C 以前には存在しなかった。
  - **Modal 側は正しく分離できている**（C-3 遵守。エージェントが動く `dashboard_server` は署名鍵も書き込み鍵も持たない）。したがって**逆方向（Modal 侵害 → Windows へ配布）は成立しない**。リスクは非対称である。
  - **v1 で取れる緩和**: (a) 受信側は pull で**新規インストール・更新が起きるたびに ntfy 通知を出す**（横展開が起きたら人間に見える。S-11）、(b) 書き込み鍵と署名鍵を専用ファイル（`.hh-signing.env`、ACL を現ユーザーのみに制限。S-06c が唯一の正）へ分離し、一般の `.hh-secret.env`（NTFY 等）とは別に、**Hermes の作業ディレクトリ配下に置かない**、(c) 将来 Ed25519 等の非対称署名へ移行すれば検証側には公開鍵だけを置けるが、**署名側（Windows）の秘密鍵をエージェントから隔離する問題は非対称化では解決しない**（別ユーザー・別サービスとしての分離が必要）。
  - **根本解決には Windows 側で「エージェントとは別の権限で動く署名サービス」が要る**（TTY 確認済みのダイジェストだけを署名する常駐サービス等）。これは個人 PC 1 台の構成に対しては大がかりであり、**v1 のスコープを超える。**
  - **解消済み（未確定事項 O・ユーザー回答 2026-08-16。全文は本節末尾「第2巡レビューで新たに生じた未確定事項」表を参照）**: 選択肢 **(1)** 横展開リスクを受け入れて v1 を進める（緩和 (a)(b) はそのまま実施する）を採用した。選択肢 (2)（Windows 発のスキルに限り受信側の人間確認を必須にする）・(3)（署名サービスを作るまで Lane C 全体を保留）は不採用。判断理由（林さん）: 個人利用・低頻度運用であり、Windows 機自体が乗っ取られるリスクは他の攻撃経路と比べて特別高くはない。**したがって以下 S-06b・S-10・S-11 の記述はすべて「Windows 発の push も Modal 側の受信側は追加の人間確認なしで自動反映する」ことを前提として書かれている**（2026-08-16 第4巡 Codex レビュー Medium 指摘で文言を訂正: 直前の段落が明言するとおり、Windows 側では S-06 の安全性の論証そのものは成立していない。ここで「そのまま適用する」のは**その論証の正しさではなく、条件 0〜5 が定める自動反映という挙動**である。安全性が証明されているから適用するのではなく、**証明されていないことを認識した上でなお、挙動としては origin による部分的な例外を設けない**、という受容の選択である）。

#### 鍵と `key_id`

- **鍵**: 既存の `HH_AGENT_TOKEN_SIGNING_KEY`（Modal Secret `hh-agent-secret`。ローカル PC では `.hh-signing.env`。S-06c が唯一の正）を流用する。**新しい鍵を作らない**（§3.4 が `issue_agent_token()` の複製を禁じたのと同じ理由）。
- **ただしローテーション契約はエージェントトークンと共有しない**（2026-08-16 第2巡 Codex レビュー High 指摘）。エージェントトークンは TTL 24h なので「primary + prev の 2 世代」で足りるが、**receipt はスキルが存在する限り検証され続ける**。2 回目のローテーションで既存 receipt が一斉に検証不能になり、インスタンスを作り直すと既存スキルが全件同期できなくなる。
- したがって receipt には **`key_id`**（鍵の sha256 の先頭 8 桁）を含め、**検証鍵は「その `key_id` の receipt が 1 つでも残っている限り保持する」**。運用手順は 2 つのいずれか: (a) 旧鍵を検証専用として保持し続ける、(b) ローテーション時に `scripts/hh_skill_sync.py --resign` で全 receipt を新鍵で署名し直してから旧鍵を捨てる。**どちらを採るかは実装時にユーザーへ確認する**（未確定事項 N）。

**`--resign` の安全契約（2026-08-16 第3巡 Codex レビュー High 指摘。これが無いと `--resign` は「人間確認を完全に迂回できる非対話の署名オラクル」になる）**:

- 再署名してよいのは、**旧鍵で検証に成功する既存 receipt が存在し、かつその receipt が署名しているタプル（`name`・`content_sha256`・`origin_instance`・`promoted_at_ms`・`promotion_seq`・`distilled_from_session_id`）が、再署名対象の内容と完全に一致する**場合だけである。
- **署名対象のタプルを一切変更しない**（`key_id` だけが変わる）。新しい内容・新しい seq を `--resign` で作れてはならない。
- 旧 receipt が存在しない／検証に失敗する／内容が一致しない場合は**必ず拒否**し、その name を一覧で人間に示す（人間が再 promote するしかない）。
- `--resign` は TTY を要求しない（バッチ処理のため）が、**上記の制約により「既にある確認済みの事実を別の鍵で言い直す」以上のことはできない。**

#### 署名対象（canonical 表現を固定する）

```
hhskill1|<key_id>|<name>|<content_sha256>|<origin_instance>|<promoted_at_ms>|<promotion_seq>|<distilled_from_session_id or "">
```

`receipt = <key_id>.<base64url(hmac_sha256(SIGNING_KEY, 上記のUTF-8バイト列))>`

- **`promoted_at_ms` は整数ミリ秒**（2026-08-16 第2巡 Codex レビュー Medium 指摘）。float をそのまま文字列化すると、JSON 往復での整数/小数・指数表記の差で**同じ時刻でも HMAC 入力が変わり検証に落ちる**。署名対象に浮動小数点数を含めない。
- **`distilled_from_session_id` も署名対象に含める**（同 Medium 指摘。監査来歴の一部なのに未署名だと偽装できる）。`null` の場合は空文字として署名する。
- **`promotion_seq`**: origin ごとに単調増加する整数（`~/.hh-agent/promote_seq.json` で管理）。リプレイ対策（下記）。
- 実装は `modal_hub/core/security.py` の `_hmac_sha256()` / `_b64url_encode()` / `constant_time_equals()` を再利用する（HMAC を手書きしない）。

#### リプレイ（ダウングレード）対策

**2026-08-16 第2巡 Codex レビュー High 指摘**: CAS だけではリプレイを防げない。読み書き両方の資格情報と過去の正規 receipt を持つ攻撃者は、`list` で現在の `revision` を取得してそれを `base_revision` に指定すれば、**古い正規署名版を「正しい CAS 更新」として再 push できる**。

対策: **`promotion_seq` を署名対象に含め、受信側は `(name, origin_instance)` ごとに「これまで受理した最大の `promotion_seq`」**未満**の receipt を拒否する。** 古い版の再 push は seq が巻き戻るため受信側で落ちる。**`accepted_seq` と厳密に等しい（`==`）receipt は拒否しない**（2026-08-16 第6巡 Codex レビュー High 指摘で訂正: 旧文は「以下」＝`<=` と書いており、S-12 が約束する「ローカルで削除しても次の pull で復活する」という挙動と矛盾していた——削除後に再 pull すると、同一内容・同一 seq の receipt が `accepted_seq` と等しくなり `<=` 判定では拒否されてしまう。同じ seq の再受理は「同じ既知の内容をもう一度見ただけ」であり、ダウングレードでも新規リプレイでもないため安全に許容できる。危険なのは`accepted_seq` を**下回る**（＝真に過去の）版だけである）。

**永続化の契約（2026-08-16 第3巡 Codex レビュー High 指摘。初版は保存先も復旧も未定義だった）**:

| 値 | 保存先 | コミット点 |
|---|---|---|
| 自分が発行した次の seq（origin ごと） | `~/.hh-agent/promote_seq.json`（**`{name: {origin_instance: seq}}`**・原子的書き込み。**2026-08-16 S-08b 差し替え**: 従来の `{name: seq}` は「1 インスタンス＝1 origin」前提だったが、S-08b により Windows 1 台が自分自身の origin と Modal 発の origin という 2 つの origin の seq を管理するため、origin をキーの内側へ 1 階層追加した。**移行**: 旧スキーマ（value が int）を検出したら、その値を自分自身の `instance_id` 配下へ 1 回だけ書き直してから続行する（フェイルクローズより先に自動移行を試み、移行自体が失敗した場合のみ下記のフェイルクローズへ倒す）。**移行は必ず `promote_lock` を保持した状態で、`allocate_promotion_seq()` の内部で読み取り直後に行う**（2026-08-16 第2巡 Codex レビュー Medium 指摘: 原子的なファイル置換それ自体は read-modify-write の直列化を保証しない。ローカル promote と remote-promote がほぼ同時に起動すると、一方が移行・採番した直後をもう一方が旧スキーマの読み取り結果で上書きしうる。`allocate_promotion_seq()` は `run_promote()`／`run_remote_promote()` のどちらから呼ばれても既に `promote_lock` 区間の内側でしか実行されない〈S-08 pseudocode・S-08b pseudocode〉ため、この制約を満たすのに新しいロックは不要。ロック外で移行を先読みする実装を書かないことだけを禁則として明記する）） | **receipt を書く前に採番して永続化する**（採番だけして落ちても seq が飛ぶだけで安全側。逆順にすると同じ seq を 2 回使う） |
| 受理済みの最大 seq（`(name, origin)` ごと） | `~/.hh-agent/skill_sync_state.json` の `accepted_seq: {"<origin>": <int>}` | **pull を配置し終えた後**に更新する |

- **`promote_seq.json` が欠損・巻き戻った場合**: 自動で 0 から振り直さない（過去 seq の再利用は受信側で拒否され、**その name が二度と同期できなくなる**）。**フェイルクローズで停止し、人間へ「`--repair-seq` を実行せよ」と促す**。
  - **`--repair-seq [--origin <instance_id>]` は `GET /api/skills/list` が返す `origin_seq_watermarks[<対象 origin_instance>]`（`Corpus2Skill/doc/03_Architecture.md` §13 S-01・S-04 検証8）を各 name について読み、その値 +1 から再開する**（2026-08-16 第4巡 Codex レビュー High 指摘: 当初「`list` から自分の origin の現在 seq を読む」としていたが、`list`／`meta.json` が返すのは CAS に勝った現在版 1 件の `promotion_seq` だけであり、現在版が他 origin のものである場合は自 origin の過去最大値を取得できなかった。`origin_seq_watermark` は CAS の勝敗と無関係に origin ごとの観測最大値を保持するため、この問題を構造的に解消する）。**`--origin` 省略時は自分自身の `instance_id` を対象にする**（2026-08-16 S-08b 追加。Windows が Modal 発 origin の seq を修復する場合は `--origin <Modal の instance_id>` を明示する）。
  - 該当 name が `origin_seq_watermarks` に存在しない場合（一度も push したことがない）は 0 から開始する。
- **`skill_sync_state.json` の `accepted_seq` が欠損した場合**: その name を「初回」として扱い、S-10 手順3 の整合性検証を経て衝突扱いにする（**推測で受理しない**）。
- **サーバー側の seq 検査は advisory に留める**（同 High 指摘）。サーバーは署名を検証できないため、書き込み鍵を持つ攻撃者が**巨大な seq を持つ偽 receipt** を送ると、正規の push が永久に拒否される DoS になりうる。したがって **サーバーは seq の逆行をイベントとして記録するが、それだけを理由に正規クライアントを恒久ロックアウトしない**（人間が `--repair-seq` で回復できる）。**真正性の根拠は常にクライアント側の署名検証であり、サーバーの持つ seq ではない。**（`Corpus2Skill/doc/03_Architecture.md` §13 S-04 検証8 と同一の結論に揃えてある。2026-08-16 第4巡 Codex レビューまでは、Corpus2Skill 側だけが「過去最大値以下は 400」という旧記述のまま残っており、この節と正面から矛盾していた。今回修正済み。）

#### 保存と検証

- **receipt は「現在版 1 件」ではなく digest 単位で版管理する**（2026-08-16 第3巡 Codex レビュー High 指摘。1 件だけだと「衝突で退避したローカル版の receipt」と「pull した現在版の receipt」を同時に持てず、どちらかを必ず失う）:
  - `~/.hh-agent/promote_receipts/<name>/<content_sha8>-<receipt_sha8>.json`（`{"name","content_sha256","origin_instance","promoted_at_ms","promotion_seq","distilled_from_session_id","key_id","receipt"}`）を原子的に書く。**ファイル名は本文のダイジェストだけでなく receipt 文字列自体のダイジェスト（`receipt_sha8` = `sha256(receipt)` の先頭 8 桁）も含める**（2026-08-16 第6巡 Codex レビュー Medium 指摘で訂正: 旧文はファイル名を `<sha8>`（本文のみ）としており、「同じ内容には同じファイル」という不変性の主張と、「同一本文が異なる origin/seq/session で複数回 promote された場合は別ファイルとして保存する」という自己修復側（S-10）の要求が両立しなかった。`content_sha8` と `receipt_sha8` の組をキーにすれば、**同じ内容・同じ receipt は必ず同じファイル名**（不変・冪等）になり、**同じ内容でも異なる receipt は必ず異なるファイル名**になるため、事後的な衝突検出や `-2` サフィックスの規律を持ち込む必要がなくなる）。**`distilled_from_session_id` を含める**（署名対象タプルの一部であり、これが無いと再検証・`--resign`（下記）の際に canonical 表現を再構成できない。2026-08-16 第4巡 Codex レビュー High 指摘）。
  - `~/.hh-agent/promote_receipts/<name>/current` に現在版のファイル名 `<content_sha8>-<receipt_sha8>`（原子的差し替え）を書く（2026-08-16 第7巡 Codex レビュー Medium 指摘で訂正: 旧文は `<sha8>` とだけ書いており、本文が同じで receipt が異なる複数バージョンを一意に指し示せなかった。ファイル名の完全な2部構成をそのまま書く）。reconcile push はこれを見る。
  - `promote_backups/<name>.bak.<ts>/` へ退避・複製するときは、**対応する receipt も同じバックアップへ複製する**（退避した版を後から再 promote できるように）。
  - pull 成功時は、検証済みのリモート receipt を保存して `current` を差し替える（**これを保存しないと、Corpus2Skill 側が失われたときにローカル正本から再 push できなくなる**）。
- pull 側は `hmac.compare_digest` による定数時間比較で照合する。**照合できないものは、他のすべての検証を通っていても絶対に書き込まない。**
- **検証に失敗したことを知れるのは受信側クライアントだけである**（サーバーは鍵を持たないので判定できない・2026-08-16 第2巡 Codex レビュー High 指摘）。したがって**署名検証失敗の通知はクライアント側のローカル永続アウトボックス**（`~/.hh-agent/skill_sync_outbox.jsonl`）で扱い、ntfy 送信に成功するまで残す。**サーバーの `_events/` に「receipt 検証失敗」を記録させる設計は誤りなので撤回する**（サーバーが判定できないことをサーバーの責務にしない）。サーバー側 `_events/` が扱うのは「形式不正・CAS 409・上限超過・`promotion_seq` 逆行」など**サーバーが自力で判定できるものだけ**。

#### この設計が防げないこと（残存リスク・正直に記載する）

- **ローカル PC の署名鍵そのものが漏えいした場合**は防げない（その時点で承認ゲート自体が破られており、Lane C だけを守っても意味がない）。
- **Windows 側ではエージェントと同期スクリプトが同一権限**である（上記）。
- **人間が確認した内容が実は悪意あるものだった場合**: これは Lane C の問題ではなく promote の問題であり、Lane C は「確認された物が確認されたとおりに複製される」ことまでしか保証しない。

### S-06c: 鍵と Secret の配置表（2026-08-16 第3巡 Codex レビュー High 指摘で新設）

**どの鍵がどのプロセスに存在するかを 1 か所で固定する。** これが崩れると S-06 の論証も C-3 も同時に壊れるため、実装時はこの表を唯一の正とする。

| 鍵 | 用途 | Windows ネイティブ | Modal `dashboard_server`（**エージェントが動く**） | Modal `sync_dashboard_skills`（新規・**エージェントは動かない**） | Modal `refresh_dashboard_agent_token`（既存） |
|---|---|---|---|---|---|
| `CORPUS2SKILL_API_KEY`（Lane A/B/C **読み取り**） | Memory Provider・Lane C の list/pull | あり（`$HERMES_HOME\.env`） | **あり**（`corpus2skill-secret`。既存・変更しない） | あり | なし |
| `C2S_SKILL_WRITE_KEY`（Lane C **書き込み**） | push・events ack | あり（`.hh-signing.env`） | **絶対に置かない** | あり（**新規 Secret `c2s-skill-write-secret`**） | なし |
| `HH_AGENT_TOKEN_SIGNING_KEY`（receipt 署名・検証） | promote receipt | あり（`.hh-signing.env`） | **絶対に置かない（C-3）** | あり（`hh-agent-secret`） | あり（既存） |
| `NTFY_TOPIC` / `NTFY_TOKEN` | 衝突通知 | あり（`.hh-secret.env`） | 不要 | あり | 不要 |

- **`C2S_SKILL_WRITE_KEY` を既存の `corpus2skill-secret` へ追加してはならない。** その Secret は `dashboard_server`（エージェントが動くコンテナ）にアタッチされている（`modal_dashboard/app.py:98`）ため、書き込み鍵が即座にエージェントの手に入る。**必ず別 Secret `c2s-skill-write-secret` を新設し、`sync_dashboard_skills` にだけアタッチする。**
- Windows 側は `.hh-secret.env` をそのまま使わず、**署名鍵と書き込み鍵だけを `.hh-signing.env` へ分離**し、ファイル ACL を現ユーザーのみに絞る（残存リスク R-1 の緩和 (b)。同一ユーザー権限である以上これは緩和であって解決ではない）。
- **Secret の作成・更新は Codex 経由で行う**（§5.4・グローバルルール）。作成後は `modal app stop` → `modal deploy`（既知の落とし穴: Secret 更新は再デプロイしないと反映されない）。

### S-07: 輸送経路に Corpus2Skill を選ぶ理由（不採用案を明記する）

| 案 | 判断 | 理由 |
|---|---|---|
| Hub の `POST /api/skills/publish` を拡張する | **不採用** | あれは**隔離（未確認）**の publish 先であり信頼レベルが違う（S-06-4）。加えて create-or-match-only（同名・別内容は 409、`routers/skills.py`）で**不変**のため、再 promote による内容更新を運べない。 |
| 同期専用の Modal App / Volume を新設する | **不採用** | 運用対象（デプロイ・Secret・監視）が1つ増える。得るものが「置き場所が独立する」だけで割に合わない。 |
| **Corpus2Skill に新レーンを増設する** | **採用** | 両インスタンスに**既に配布済みで動作実績のあるクライアント**（`.hermes/plugins/corpus2skill/__init__.py` の stdlib urllib REST クライアント）と長命 Bearer がある。Volume の commit/reload 規律・パス封じ込め（`volumes.safe_join`）・REST フォールバックのパターンが確立済み。増えるのは Volume 上の1ディレクトリと3エンドポイントだけ。 |

**依存の向きに関する注記（D-03 の位置づけ更新）**: これまで Corpus2Skill は H-H-Agent から見て「参照専用のサイドチャネル」だった。Lane C は書き込みを伴うため、この位置づけを更新する。ただし依存は**ソフトのまま**とする:

- **唯一の正はローカルの `~/.hermes/skills/`** であり、Corpus2Skill は輸送層に過ぎない。Corpus2Skill が消えてもローカルのスキルは1つも失われない。
- push 失敗は promote を失敗させない（S-08）。pull 失敗は Hermes の起動をブロックしない（S-10）。

### S-08: PUSH 側（`scripts/hh_skill_promote.py` へのフック）

**既存の promote の安全チェック（symlink/reparse point 拒否・`os.fstat` による TOCTOU 対策・原子的書き込み・scan-root 重複チェック・TTY 確認）は一切変更しない。push はローカル promote が完全に成功した後の純粋な追加処理である。**

**Modal コンテナ上での promote・push の「実行」は v1 では発生しない（設計上の重大な非対称性・2026-08-16 第5巡 Codex レビュー High 指摘で発見）。** `run_promote()`（S-10 疑似コード）は TTY 確認（`confirm_or_abort()`）を必須の人間ゲートとして持つが、`modal_dashboard/app.py` の `dashboard_server`（エージェントが動くチャット UI コンテナ）には TTY が無く（§4.4 で既知）、かつ C-3 により署名鍵・書き込み鍵のどちらも持たない。したがって**チャット UI 経由で Modal 側のスキルを promote → push する経路は存在しない**。これは Lane C 固有の欠陥ではなく、§4.7 の promote 機構そのものが元々 TTY 前提であることに起因する、より広い制約である。**2026-08-16 S-08b 訂正**: 上記は「Modal コンテナの中で署名・push が**実行される**ことはない」という意味であり、「Modal 発（origin_instance が Modal インスタンスの）promote・push が発生しない」という意味ではない——後者は Windows が S-08b の経路で**代理実行**するため、v1 でも普通に発生する。「実行場所」（Modal では起きない）と「`origin_instance`」（Modal のままでよい）を混同しないこと。

- **v1 で許容する実際の運用（2026-08-16 ユーザー却下により全面差し替え。旧版は「人間が `modal shell` で署名鍵を持つコンテナへ直接アタッチし対話的に promote する」運用だったが、ユーザーがこれを明示的に却下した——「１の運用変更は了承しない」）**: Modal 側で Distiller が生成した隔離候補を promote したい場合、**人間（林さん本人）は `modal shell` に一切触れない。** 代わりに **Windows 側から Hub の読み取り専用エンドポイント（`GET /api/skills/quarantine`）を叩き、既存の Windows ローカル TTY 確認・署名フローをそのまま再利用する**。詳細な設計は **S-08b** を参照（本節はこれ以上詳述せず S-08b に一元化する）。
- **したがって v1 の実運用は「Windows 発→Modal」が主経路であり、「Modal 発」の確認・署名も（S-08b により）Windows 側で行われる**（この点で非対称。署名鍵が Windows にしか無いことがそのまま構造的な理由になる。14.0 の「逆も同じ」という記述は、この運用上の非対称性を踏まえて読むこと）。**チャット UI 上での確認操作としては引き続き提供しない**（S-13(g)。提供すると事実上 TTY 相当の確認 UI をエージェント面に持ち込むことになり D-16 の趣旨に反する）。将来ダッシュボードにブラウザ経由の承認 UI（Hub の既存 ntfy／承認フローと同種のもの。D-11 のパターンを流用できる）を足せば TTY 相当の確認をチャット UI からも行えるが、**これは v1 のスコープ外**（S-13 に追記）。
- コードは `origin_instance` を特定の値にハードコードしない（将来 Modal 発の実運用が増えても構造的に対応できるようにする）。v1 で制約されるのは**運用（人間がどうやって promote するか）**であり、**プロトコル（受信側の検証ロジック）ではない**。**S-08b の `run_remote_promote()` はこの原則を、`origin_instance` を明示引数として受け取るという実装レベルの規律にまで落とし込む**（2026-08-16 第4巡 Codex レビュー Medium 指摘で訂正: 当初この文は「応答値をそのまま使う」としていたが、S-08b が第3巡で確定させた「署名対象は接続先設定〈`remote_sources.json`〉の固定値を使い、quarantine 応答の自己申告 `origin_instance` は表示専用として扱う」という契約と矛盾していた。正は S-08b であり、応答値をそのまま署名対象に転記する実装を書かないこと）。

- **配置**: HTTP・JSON・タイムアウト処理は新規 `modal_hub/services/skill_sync.py` に置く。`hh_skill_promote.py` は「promote の唯一の実装」であり安全性クリティカルなので、ネットワークコードを持ち込まない。同ファイルへの追加は `run_promote()` 末尾（`append_promote_log()` の**後**）の1呼び出しと `try/except` のみ。
- **フェイルオープン**: push の失敗（ネットワーク断・401・5xx・タイムアウト・サイズ超過・redact 差分）は promote を失敗させない。stderr に1行警告を出し、**終了コードは 0 のまま**。理由: この時点でローカル promote は完全に成功しており、非 0 を返すと人間が「promote が失敗した」と誤解して再実行し、`--force` での上書きを促す羽目になる（安全側の挙動が危険側の操作を誘発する）。取りこぼしは S-10 の reconcile が拾う。
- **送信ペイロード**:
  ```json
  {"name": "<kebab-case>", "skill_md": "<UTF-8 text>", "content_sha256": "<hex64>",
   "promoted_at_ms": 1755300000123, "origin_instance": "<instance_id>",
   "distilled_from_session_id": "<id>|null",
   "promotion_seq": 7,                    // S-06b。origin ごとに単調増加。リプレイ対策
   "receipt": "<key_id>.<base64url hmac>", // S-06b。必須。形式不正はサーバが 400
   "base_revision": 0}                    // S-11 の CAS。新規は 0、更新は既知の revision
  ```
  `promoted_at_ms` は receipt・`promote_log.jsonl`・push ペイロードの**三者すべてで同一の値を使う**（`time.time()` を複数回呼ばない。整数ミリ秒で持つ理由は S-06b の canonical 表現）。監査ログとペイロードで値が食い違うと、衝突時に何が起きたのか後から追えなくなる。

  **値を生成する箇所は1か所に固定する**（2026-08-16 第8巡 Codex レビュー Medium 指摘で訂正: 旧文は「`append_promote_log()` が書き込んだ record dict を返すのでそこから取る」としていたが、S-10 の疑似コードでは `write_receipt()`（署名。手順10）が `append_promote_log()`（手順11）より**先**に実行される。receipt は署名対象に `promoted_at_ms` を含む（S-06b canonical 表現）ため、`append_promote_log()` が値を生成してから返す形では receipt の署名対象と `promote_log.jsonl` の記録が異なる値になりうる）。正しい順序は: `run_promote()` が `allocate_promotion_seq()`（手順9）の直前に `promoted_at_ms = int(time.time() * 1000)` を**1回だけ**生成し、`write_receipt()`（手順10）・`append_promote_log()`（手順11。引数として渡す。**生成させない**）・`push_to_lane_c()`（手順12）の3か所すべてにこの同じ値を明示的に渡す。`append_promote_log()` の返り値は record dict のままでよいが、**`promoted_at_ms` の出どころとしては扱わない**（他のフィールド、例えば `synced_at` の生成には引き続き使ってよい）。

  **ただし `promoted_at_ms` は順序判定には使わない**（確定事項 I・2026-08-16）。クライアント申告のタイムスタンプであり、Windows と Modal コンテナの時計ずれで大小が逆転しうるため、**新旧の権威はサーバーが採番する単調増加 `revision`** とする（S-11）。`promoted_at_ms` は監査・署名対象・人間が読むときの手がかりとしてのみ使う。
- **サーバー応答の記録**（2026-08-16 第4巡 Codex レビュー Medium 指摘で契約を訂正: 旧文は成功応答にも `conflict`（bool）・`loser_sha256` を含めるとしていたが、`Corpus2Skill/doc/03_Architecture.md` §13 S-04 は「CAS が成功した通常の更新はイベントを記録せず、応答は `revision`・`received_at`・置き換えられた版の `content_sha256` を返すだけ」としており、両者は成功応答の形が食い違っていた）:
  - **成功（HTTP 200・`base_revision` 一致）**: `{"revision": <int>, "received_at": <str>, "replaced_content_sha256": <hex64|null>}`。**`conflict` フィールドは含めない**（成功はそもそも衝突ではない）。同期スクリプトは `revision` を `~/.hh-agent/skill_sync_state.json` の当該 name のウォーターマークとして原子的に記録する（S-10 手順3）。
  - **CAS 不一致（HTTP 409）**: `{"error": {...}, "conflict": true, "current_revision": <int>}`。`conflict: true` を見たら即座に ntfy 通知を出す（S-11）。
  - **形式不正・上限超過（HTTP 400）**: `{"error": {...}}`。これらは CAS の意味での「衝突」ではないため `conflict` フィールドは付けない。**同期スクリプトの即時応答での検知はベストエフォートに留め、通知の正本は次回 `GET /api/skills/list` の `events` から拾う**（S-11 が既に定めているとおり、応答そのものが失われても `_events/` に残るのが本来の durable な経路であり、同期応答の `conflict` フィールドはあくまで即時通知の高速パスに過ぎない）。
  - **`promotion_seq` 逆行は 400 の理由にならない**（advisory・`Corpus2Skill/doc/03_Architecture.md` §13 S-04 検証8と同一の結論。2026-08-16 第5巡 Codex レビュー High 指摘で訂正: 旧文はこれも 400 応答の一つとして扱っており、S-06b の「advisory に留める」という決定と矛盾していた）。**CAS(7) が成功すれば、`promotion_seq` が逆行していても応答は HTTP 200（`conflict` フィールドなし）である。** 逆行はサーバー側の `_events/` にだけ記録され、次回 `list()` の `events` 経由で拾われる（S-11）。
- **応答が失われた場合**（タイムアウト・プロセス強制終了）: promote は成功のままとし、ウォーターマークは**次回 pull 時に `GET /api/skills/list` の `revision` から埋める**。このとき「サーバー側は更新済みだがローカルは知らない」という状態になるが、**これは通常の成功であり `_events/` には何も記録されない**（CAS が成功した通常の更新はイベントを記録しない・S-11）。回復は S-11 の衝突通知に頼るのではなく、**次回 `list()` で `local.sha == remote.sha` と判定され、S-10 手順3 の通常の分類フローがウォーターマークを補記する**ことで自然に収束する（2026-08-16 第6巡 Codex レビュー Low 指摘で訂正: 旧文は「S-11 の durable な衝突イベントに頼る」としていたが、これは通常の成功パスであり衝突ではないため、そもそもイベントは作られない。応答喪失からの回復手段は「通知」ではなく「次回同期時の通常の差分判定」である）。
- **サイズ上限（2026-08-16 Codex レビュー Medium 指摘で分離）**: `skill_md` は **UTF-8 バイト列で 64KB 以下**。これとは別に JSON ボディ全体の上限を **256KB** とする。既存 `routers/skills.py` の `MAX_BODY_BYTES = 64KB` は「JSON ボディ全体」に掛かっており、そこへ meta と receipt が加わる Lane C の形でそのまま流用すると、**正当な 64KB の SKILL.md が拒否される**。加えて JSON エンコードは `ensure_ascii=False`（UTF-8 のまま送る）に固定する — `ensure_ascii=True` だと日本語1文字が `\uXXXX` の 6 バイトへ膨らみ、同じ本文でも上限に当たるかどうかがエンコーダ設定次第で変わる。
- **`origin_instance` の決め方**: 新規 `~/.hh-agent/instance_id.json`（`{"instance_id": "..."}`）。存在しなければ初回に `<platform>-<uuid4 hex 8桁>` で生成する。環境変数 `HH_AGENT_INSTANCE_ID` があればそれを優先。**ホスト名を使わない**（Modal コンテナのホスト名は起動ごとに変わり、かつ個人環境名を外部サービスへ残したくない）。Modal 側は `USERPROFILE=/opt/data` により `/opt/data/.hh-agent/instance_id.json` として Volume 上に永続するので、同じコードで一意性が保てる。**この値の用途は監査とループ抑止だけで、認可には一切使わない**（クライアントの自己申告値だから — §5.1「`source`/`session_id` はサーバーがトークンから確定する。ボディの自己申告を信じない」と同じ理由）。
- **秘密の混入防止**: 送信前に `modal_hub/core/redact.py` の `redact_text()` を適用し、**差分が出たら送信しない**（redact 後の本文を送るのではなく拒否する。書き換えた本文を配ると2インスタンスで内容が食い違う）。`POST /api/skills/publish` の検証4と同じ規律を Lane C にも適用する。
- 上限を超えた場合は送らず警告のみ（promote は成功のまま）。

**再送キューを作らない。** push に失敗しても、次回の pull 実行時に「ローカルにあるが Lane C に無い／Lane C の方が古い」ことが差分判定で自明に検出できる（S-10 手順3）。専用のアウトボックス機構を新設せず、reconcile が構造的に埋める。

### S-08b: Modal 発 quarantine スキルの確認・署名 —「Windows からのリモート確認・署名」方式（`modal shell` 運用は不採用・2026-08-16 ユーザー却下により全面差し替え）

**経緯**: S-08 が当初挙げていた代替案（「人間本人が `modal shell` で `hh-agent-secret` を持つコンテナへ直接アタッチし、その中で対話的に `hh_skill_promote.py` を実行する」）を、**ユーザーが明示的に却下した**（2026-08-16「１の運用変更は了承しない」）。理由は運用側の判断であり、設計原理（C-3・D-16 いずれも）そのものへの異議ではない。したがって以後、**この節の内容が S-08 の「v1 で許容する実際の運用」を完全に置き換える。`modal shell` を人間が対話的に使う運用は設計から削除する。**

**方式の要点**: 確認・署名という人間ゲートそのものは Windows 側から一切動かさない（TTY 確認は引き続き Windows ローカルで行う。C-3・D-16 を毫も緩めない）。変わるのは「確認対象のコンテンツをどこから読むか」だけである — ローカル quarantine の代わりに、リモートの quarantine を読み取り専用 API 経由で読む。

**アーキテクチャ訂正（2026-08-16 第3巡 Codex レビュー High 指摘。実コードを確認して判明）**: 当初案は「quarantine は Volume `hh-agent-dashboard-home` 上にあり `dashboard_server` から読める」という誤った前提で書かれていた。**実際には quarantine（`skills_quarantine/<name>/SKILL.md`）は Hub（`modal_hub`、Modal App `hh-agent-hub`）が所有する別 Volume `hh-agent-store`（マウント先 `/mnt/hh_store`）上にあり、既存の `POST /api/skills/publish`（`modal_hub/routers/skills.py`）が書き込む**。`dashboard_server`（Modal App `hh-agent-dashboard`）はこの Volume を一切マウントしていない。したがって新設の読み取り専用エンドポイントは `dashboard_server` にではなく **Hub（`modal_hub`）に追加する**。これは C-3 の適用対象を変えない — C-3 が禁じるのは「エージェントが動く `dashboard_server`」への署名鍵・書き込み鍵の配置であり、Hub はもともと `hh-agent-secret`（署名鍵含む Secret 一式）を中核で保持する既存の信頼された制御プレーンである（`refresh_dashboard_agent_token` が既に同じ位置づけで運用中）。

1. **Hub 側: 読み取り専用エンドポイントの新設**
   - `GET /api/skills/quarantine`（`modal_hub/routers/skills.py`。既存 `POST /api/skills/publish` の隣に追加）。
   - **認可は新しい Secret を作らず、Hub が既に持つ Agent Bearer + scopes 機構（`modal_hub/core/security.py`）を再利用する**（2026-08-16 第3巡 Codex レビューで訂正: 当初案は新規 Modal Secret `hh-quarantine-read-secret` を作る設計だったが、Hub には既に `scopes: list[str]` を持つエージェントトークン基盤があり（既存 `SCOPE_PUBLISH = "publish"` と同型）、これに `SCOPE_QUARANTINE_READ = "quarantine_read"` を追加するだけで用が足りる。新しい Secret・新しい鍵配布経路を増やさない方が、既存の「鍵ごとに用途を分離する」規律（S-04・S-06c）にも合致する）。Windows 側は `scripts/hh_issue_agent_token.py` を拡張し、既存の `agent_token.json`／`distill_token.json` と同じ発行パターンで **`quarantine_read_token.json`（`scopes=["quarantine_read"]`）** を Windows 常駐の `HH_AGENT_TOKEN_SIGNING_KEY`（Windows にしか無い。S-06c）で自己署名・発行する。新しい Modal Secret 作成は不要であり、**Codex 経由の Secret 作成手順も本節では発生しない**。
   - **C-3 は堅持される**: Hub はそもそも `dashboard_server` ではない。この変更で `dashboard_server` の Secret 構成には一切手を入れない（S-06c の鍵配置表に変更なし）。
   - **origin_instance の記録（追加要件）**: 既存の `_publish_skill_core`（`POST /api/skills/publish`）は現在 `identity.sub`（トークンの持ち主）を publish イベントの監査ログにのみ記録しており、**quarantine エントリ自体には呼び出し元インスタンスを識別する情報が永続化されていない**。S-08c の消し込み判定・本節の応答の両方に必要なため、`_publish_skill_core` に**純粋な追加**として: リクエストボディに任意項目 `origin_instance`（自己申告。呼び出し元＝ Distiller クライアントが自分の `~/.hh-agent/instance_id.json` の値を渡す）を許容し、`skill_md` と同じ `atomic_write_file` 呼び出しの流れで `skills_quarantine/<name>/meta.json`（`{"origin_instance", "published_at"}`）を `write_json()` で書く。**この `origin_instance` は署名されない自己申告値である**（distilled_from_session_id と同格の監査・表示専用の情報であり、S-08c の消し込み判定では**信用しない**——信用するのは常に S-06b の検証済み promote receipt に署名された `origin_instance` の方である）。**受け付ける前に型・形式を検証する**（2026-08-16 第5巡 Codex レビュー Low 指摘）: `origin_instance` は `null` または `^[a-z0-9][a-z0-9._-]{0,63}$`（S-04・S-10 が他の場所で使う `origin_instance` の正規表現と同一）に限定し、不一致は `null` として扱う（表示専用の非信頼値であっても、型・長さ・文字種を無検証で監査ログや応答へ流さないという S-04 の規律をここでも適用する）。`published_at` はクライアント申告を使わずサーバー側で `time.time()` から生成する（`meta.json` に書く時刻はサーバー時刻とし、クライアントの自己申告時刻は使わない）。`meta.json` が読めない場合（欠損・破損・symlink 拒否・valid JSON だが object でない・フィールド型不正のいずれも含む）は一律 `origin_instance: null`・`published_at: null` として扱う（部分的に信用しない・全部信用しないのどちらかであり、フィールド単位で信頼度を混在させない）。
     - **`meta.json` を書く分岐を固定する**（2026-08-16 第4巡 Codex レビュー Medium 指摘: `_publish_skill_core` には「予約獲得後の新規書き込み」「同一内容・ファイルも一致する即時 return」「予約あり・ファイル無しの自己修復」「異なる内容の 409」の 4 分岐があり、どこで `meta.json` を書くかを定めないと、409 になる要求が既存 metadata を上書きしたり、自己申告 origin_instance が書き換わり続けて remote-promote が不安定になりうる）。**`meta.json` を書くのは「予約獲得後の新規書き込み」分岐だけ**（＝その name への最初の書き込みが確定した瞬間のみ）とする。それ以外の分岐（即時 return・自己修復・409）では `meta.json` に一切触れない——**一度書かれた `meta.json` は、対応する quarantine エントリが存在する限り不変**であり、後続の同一内容再送がどんな `origin_instance` を自己申告しても上書きされない（最初の書き手の申告だけを信じる、という単純な規律にする）。
   - **MCP ツールとしては公開しない**（S-06-2 と同じ原則）。
   - レスポンス: `{"skills": [{"name", "content", "content_sha256", "origin_instance", "distilled_from_session_id", "published_at"}]}`（`origin_instance` はエントリごと。`meta.json` が存在しない場合は `null` を返す——古い quarantine エントリや `meta.json` の読み取りに失敗した場合を含む。上記のとおり自己申告値であり、Windows 側はこれを**確認画面の表示・接続先ラベルとしてのみ**使い、署名・消し込み判定の根拠には使わない）。**消し込み済みエントリの除外**（S-08c 参照）: 各エントリについて `store.get_quarantine_resolved(name)` を引き、返り値が現在の `content_sha256` と一致する場合はそのエントリを応答に含めない（既に Windows 側で確認・署名・Lane C へ push 済みであり、人間が再度確認する必要がないため）。
   - **読み取りの安全チェック**（2026-08-16 第2〜4巡 Codex レビュー Medium 指摘の集約: 認証だけでは quarantine 内のパスが symlink 等へ差し替えられた場合の意図しない読み取りを防げず、最終ファイルだけでなく祖先ディレクトリの差し替え・`meta.json` も同様に考慮する必要がある）: `name` を `skill_quarantine.NAME_RE` で検証した上で、quarantine root を `os.open(root, os.O_DIRECTORY | os.O_NOFOLLOW)` で開き、そこから `dir_fd` 相対で `<name>` ディレクトリを `O_NOFOLLOW` 付きで開く（`openat` 相当。存在確認してから開く二段階にしない）。**`SKILL.md`・`meta.json` の両方をこの `<name>` の dir fd から同じ規律（`O_NOFOLLOW`・`os.fstat()` で通常ファイル確認）で開く**（`meta.json` だけ安全チェックの対象から漏らさない）。本文は `fstat` のサイズで先に上限超過を検出し、EOF または 64KB+1 バイトまでのループで bounded read する（一括 `read()` に頼らない）。`meta.json` の読み取りに失敗した場合（symlink 拒否・サイズ超過・破損 JSON 等）は例外にせず `origin_instance: null` として扱う（S-04 の「黙って空を返さない」は本文の整合性検証の話であり、ここは付随メタデータの取得失敗——本文自体の安全性には影響しないため、null で返す方が全体を 500 にするより実用的）。一覧件数上限は 200 件（§4.7 の隔離領域 200 件上限と同じ資源）。
   - **`GET /api/skills/quarantine` は読むだけで、いかなる書き込みも行わない**（消し込みは S-08c が別経路で行う）。

2. **Windows 側: 新しい実行モード**
   - `scripts/hh_skill_promote.py --remote <source>`（`<source>` は接続先設定を選ぶキー。例 `modal-dashboard`。**この文字列は接続先ラベルであり、receipt に書き込む `origin_instance` の値そのものではない**）。
   - 接続先設定（Hub の URL・`quarantine_read_token.json` のパス）は `~/.hh-agent/remote_sources.json` を正本とする。ファイル ACL は現ユーザーのみに絞る。
   - **署名する `origin_instance` は、応答の自己申告フィールドをそのまま信用しない**（2026-08-16 第3巡 Codex レビューでの整理: 上記のとおり quarantine メタデータの `origin_instance` は署名されない自己申告値であり、これを無検証で promote receipt の署名対象に転記すると、Lane C の書き込み鍵とは無関係の「Hub の publish scope を持つ何か」が任意の `origin_instance` を詐称できてしまう）。したがって Windows 側は、`--remote <source>` の**接続先設定自体に対応する `origin_instance` を固定値として `remote_sources.json` に保存しておき**（初回セットアップ時に、対象 Modal インスタンスの `~/.hh-agent/instance_id.json` の値を人間が一度だけ確認して書き込む。以後は自動）、**エンドポイント応答の `origin_instance` フィールドは表示・監査用にのみ使い、署名対象には接続先設定の固定値を使う**。両者が食い違う場合（quarantine メタデータの申告値と接続先設定の固定値が一致しない）は **警告を表示して署名を中断する**（設定ミス・詐称のいずれであっても人間の判断を挟む）。

3. **確認・署名は既存の Windows ローカル promote フローをそのまま再利用する**
   - `display_for_confirmation()` / `confirm_or_abort()` / `install_confirmed_skill()` / `allocate_promotion_seq()` / `write_receipt()` / `append_promote_log()` / `push_to_lane_c()` は**すべて既存の関数をそのまま呼ぶ**。
   - **`allocate_promotion_seq()`・`write_receipt()`・`push_to_lane_c()` は `origin_instance` を明示引数として受け取るよう署名を広げる**（`run_promote()` は自分の `instance_id` を渡し、`run_remote_promote()` は接続先設定の固定値〈上記〉を渡す）。
   - `promote_seq.json` の**スキーマを `{name: seq}` から `{name: {origin_instance: seq}}` へ拡張する**。**この移行は `allocate_promotion_seq()` の内部で、`promote_lock` を保持した状態で読み取り直後に 1 回だけ行う**（2026-08-16 第2巡 Codex レビュー Medium 指摘: 原子的なファイル置換それ自体は read-modify-write の直列化を保証しない。`allocate_promotion_seq()` は `run_promote()`／`run_remote_promote()` のどちらから呼ばれても既に `promote_lock` 区間の内側でしか実行されないため、ロック外で移行を先読みする実装さえ書かなければ、これで新しいロックなしに直列化される）。`--repair-seq [--origin <instance_id>]` は**同じ `promote_lock` を writer として取得してから** `promote_seq.json` を更新する（2026-08-16 第3巡 Codex レビュー Medium 指摘: 当初 `allocate_promotion_seq()` にだけロック要件を明記しており、同じファイルを書き換える `--repair-seq` が対象から漏れていた）。省略時は自分自身の `instance_id` を対象にする。
   - **push 結果、Windows 自身の `~/.hermes/skills/<name>/` にもこの内容がインストールされる**（`install_confirmed_skill()` を経由するため。意図した挙動——Lane C の目的は最終的に両インスタンスが同じ内容を持つことであり、S-10 の定期 pull で後追いされるはずの内容を確認と同時に前倒ししているだけ）。**Modal 側インスタンス自身のスキル一覧には影響しない**（Modal は自分自身の pull（S-10・`sync_dashboard_skills`）で改めて Lane C から取得する）。
   - 応答（`content`）は無検証で信用しない: 受信直後に `sha256(content) == content_sha256` を再検証する。

4. **push 経路は S-08 と完全に同一**（新しい push 実装を作らない）。

**擬似コード**（S-10 §「リファクタ後の呼び出し順」の直後に位置づける）:

```
run_remote_promote(source: str, target_name: str | None = None):   # 新規・人間の対話経路
    cfg = load_remote_source_config(source)                # Hub URL・quarantine_read_token・固定 origin_instance
    resp = fetch_quarantine_list(cfg)                       # GET /api/skills/quarantine（Hub）
    for entry in resp["skills"]:
        if target_name and entry["name"] != target_name:
            continue
        name, content = entry["name"], entry["content"]
        digest = sha256(content)
        assert digest == entry["content_sha256"]            # 応答を無検証で信用しない
        if entry.get("origin_instance") not in (None, cfg.origin_instance):
            abort(f"quarantine の自己申告 origin_instance が接続先設定と食い違う: {name}")  # ★警告して中断
        display_for_confirmation(name, content, digest)     # 全文表示（run_promote と同一関数）
        confirm_or_abort(name, digest)                       # TTY 確認（同一関数。★ロックは保持しない）
        with promote_lock(timeout=60):                       # run_promote と同じロック
            assert_staging_root_is_safe()
            self_heal_orphaned_promotions()
            install_confirmed_skill(name, content, digest,
                                    force=False, provenance=f"remote-promote:{cfg.origin_instance}")
            promoted_at_ms = int(time.time() * 1000)
            seq = allocate_promotion_seq(name, origin_instance=cfg.origin_instance)      # ★接続先設定の固定値
            receipt = write_receipt(name, content, digest, seq, promoted_at_ms,
                                    origin_instance=cfg.origin_instance)                  # ★同上
            record = append_promote_log(provenance=f"remote-promote:{cfg.origin_instance}",
                                        promoted_at_ms=promoted_at_ms)
        push_to_lane_c(promoted_at_ms=promoted_at_ms, receipt=receipt,
                       origin_instance=cfg.origin_instance,                              # ★push実行者(Windows)ではない
                       distilled_from_session_id=entry.get("distilled_from_session_id"))
```

**この設計が満たす条件**: C-3・D-16・S-06 条件1〜5を**すべてそのまま満たす**。`modal shell` という運用固有の要素だけが消え、プロトコル・安全性の論証（S-06）は一切変更されない。

### S-08c: Modal 側 quarantine の自動消し込み（`sync_dashboard_skills` の拡張・新しい HTTP エンドポイントは増設しない）

**要件**: S-08b で Windows がリモート確認・署名して Lane C へ push した Modal 発スキルは、Hub の quarantine（`skills_quarantine/<name>/SKILL.md`、Volume `hh-agent-store`）に「未 promote」のまま残り続けてはならない。しかし **`dashboard_server` にも Hub にも新しい HTTP 書き込みエンドポイントを追加しない**——`sync_dashboard_skills` が Hub の Volume を直接マウントし、Hub の既存ストレージ層（`modal_hub/core/store.py`）を**同一プロセス内 import で**再利用する（2026-08-16 第3巡 Codex レビュー High 指摘を受けた設計訂正: 当初「`dashboard_server` の Volume 上で claim する」「Distiller をロックに参加させる」という案は、そもそも quarantine が別 Volume 上にあるという誤った前提の上に組み立てられていたため撤回する。正しい実装対象は Hub の Volume `hh-agent-store` である）。

**アクセス方法**: `refresh_dashboard_agent_token`（`modal_dashboard/app.py`）が `hh-agent-secret` を持ちながら `modal_hub.core.store` を import して Hub の `modal.Dict` を直接操作している、**既に確立済みの前例と同じパターン**を踏襲する。`sync_dashboard_skills` の `@app.function` 定義に **Hub の Volume `hh-agent-store`（`modal.Volume.from_name("hh-agent-store")`）を追加でマウントし**（両 App が同じ Modal Environment にデプロイされていること・マウント先が `/mnt/hh_store` であることを前提とする）、`modal_hub.core.store` の既存ヘルパー（`read_json`・`atomic_write_file`）と新設ヘルパー（下記）をそのまま呼ぶ。**新しい Secret は不要**（Volume マウントは Secret とは独立の Modal リソース参照であり、`sync_dashboard_skills` は既に `hh-agent-secret` を保持している）。

**設計の根本方針（2026-08-16 第5巡 Codex レビュー High 指摘を受けた全面再設計）**: 当初案（複製 → quarantine 本体を削除 → 予約解放）は、**削除の順序をどう入れ替えても閉じられない TOCTOU が残ることが判明した**。`_publish_skill_core` の自己修復書き込み（`atomic_write_file()`）は「予約が自分の内容と一致する」ことを確認した**後**に実際の書き込みを行うが、その確認から書き込み完了までの間に**任意の遅延**（プロセススケジューリング・ネットワーク遅延・GC 停止など）が起こりうる。したがって「削除 → 予約解放」の**後**に、その削除より前に確認を終えていた**古い**自己修復書き込みが**今ごろ完了する**という順序が理論上あり得ることが、既存コード（`skills.py:_publish_skill_core`）の構造上排除できない——`予約確認`と`書き込み実行`が単一の原子操作になっていないためである。この種のレースは「手順の順序を入れ替える」だけでは閉じられない（消し込み側がどんな順序を選んでも、Hub 側の書き込みが「今ごろ完了する」タイミング自体を制御できないため）。

**したがって、quarantine 本体（`skills_quarantine/<name>/SKILL.md`）そのものには一切書き込み・削除を行わない設計に変更する。** 消し込みは「複製して、既に処理済みであることを別の場所に記録する」だけであり、Hub の既存 publish 経路が**将来いつ何を書いても**、消し込み側の記録と衝突しない:

1. `store.store_volume().reload()` を呼ぶ。
2. quarantine の `<name>` について、**S-08b と同じ安全な読み取り規律（`dir_fd` 相対の `O_NOFOLLOW`・`fstat`・bounded read）で** `SKILL.md`（および `meta.json`、存在すれば）を読む（2026-08-16 第4巡 Codex レビュー Medium 指摘: cleaner 側の読み取りが `store.read_file()`〈symlink を辿る `Path.read_bytes()` の薄いラッパー〉のままでは、GET エンドポイントだけを安全にしても cleaner 側に同じパス攻撃面が残る。この安全な読み取りロジックは S-08b の実装と共通化し、複製しない）。digest を計算し、手順3 で確認済みの `content_sha256` と一致するか確認する。
   - **一致しない場合**: 何もしない（quarantine の内容が Lane C 上の版より新しい・別の再 distillation が起きている）。次周期へ持ち越す。
   - **一致する場合**: 次へ進む。
3. 本文（と `meta.json`）を、**新規かつ一意なパス** `skills_quarantine_promoted/<name>.<ts>/` へ `atomic_write_file()` で複製する（このパスは消し込みごとに一意なので、他の書き手と衝突しようがない——複製は常に安全）。
4. **「この name はこの digest まで消し込み済み」という記録を、`modal.Dict` の新しいキー空間（quarantine 本体とは別。書き手は `sync_dashboard_skills` だけ）へ書く**: `store.mark_quarantine_resolved(name, content_sha256)`（新設ヘルパー。内部は単純な `dict[key] = value` の無条件上書き——このキー空間を書くアクターは `sync_dashboard_skills` 唯一なので `put_if_absent` のような排他は不要）。
5. `store_volume().commit()` を呼ぶ。

**quarantine 本体は消し込み後も物理的に残り続ける。** これは意図した挙動である。`GET /api/skills/quarantine`（S-08b）は一覧を返す際、各エントリについて `store.get_quarantine_resolved(name)` を引き、**その値が現在の `content_sha256` と一致する場合はそのエントリを応答から除外する**（既に消し込み済みで見せる必要がないため）。Distiller が同じ `name` に**異なる**内容を再 distillation して自己修復以外の経路で更新すれば（現行 `_publish_skill_core` の create-or-match-only の制約内で可能な範囲——これは本デルタが変更する範囲外の既存の挙動である）、その新しい `content_sha256` は解決済みマーカーと一致しなくなるため、一覧に自然に再出現する。

**この設計が閉じるもの・閉じないもの**: quarantine 本体への書き込み・削除アクターは `_publish_skill_core` だけのまま変わらないため、**Hub 側の既存コードには一切手を入れずに済む**（`origin_instance` sidecar の追加を除く。S-08b）。消し込み側はその既存コードの動作タイミングに一切依存しない（読むだけ・自分専用の別キーへ書くだけ）ため、上記で説明した「遅延書き込み」の影響を受けない。**代わりに、quarantine 本体は無期限に残り続ける**（v1 で許容する——SKILL.md は数 KB でストレージ圧迫は実質ゼロであり、S-12 が Lane C 自体について既に採用している「削除より安全側」の設計判断と同じ理由による）。

- 例外（I/O エラー等）は捕捉し、次周期で再試行する。
- **量産される quarantine 全体の走査はしない**。あくまで「今回 Lane C 上で観測された name」だけを対象にする。

**Corpus2Skill 側への影響: なし。**

### S-09: Corpus2Skill 側 Lane C（要約。詳細は `Corpus2Skill/doc/03_Architecture.md` §13）

- ストレージ: 既存 Volume `c2s-skills-store` に `skills_shared/<name>/versions/<revision>-<sha8>.md`（**不変**）+ `meta.json`（コミット点）。新規 Volume は作らない。
- REST のみ 4 エンドポイント: `POST /api/skills/push`（CAS・`base_revision` 不一致は 409）・`GET /api/skills/list`（skills + 未 ACK 衝突イベント）・`GET /api/skills/pull?name=`・`POST /api/skills/events/ack`。**削除系は作らない**（S-12）。
- name ごとに単調増加する `revision` をサーバーが採番し、それが唯一の順序権威（S-11）。
- 名前の正規表現は `skill_quarantine.NAME_RE` と**完全に同一**の `^[a-z0-9][a-z0-9-]{1,48}\Z`（**`$` ではなく `\Z`**。`$` は末尾の改行 1 個の直前にもマッチし `"name
"` を誤って受理する — 2026-08-11 に実機確認済みの既知の罠）。
- **Corpus2Skill は receipt の中身を検証できないし、する必要もない**（署名鍵を持たないため）。サーバーは receipt を**不透明な文字列として保存・返却するだけ**であり、正当性の判断は受信側インスタンスが行う（S-06b。これが「Corpus2Skill を信頼境界の外に置く」ということの実装上の意味）。ただし長さ上限・文字種（base64url）の形式検証は行う。
- **MCP ツールとしては公開しない**（S-06-2）。
- 認証は 2 段: **読み取りは既存の固定 Bearer、書き込み（push / events-ack）は新規の `C2S_SKILL_WRITE_KEY`**（2026-08-16 Codex レビュー Critical 指摘への多層防御。既存 Bearer は Memory Provider として両インスタンスに常駐し毎ターン使われる「広く配布された鍵」であり、スキル書き込みの防御には強度が足りない）。**Modal Secret への追加は Codex 経由で行う**（§5.4・グローバルルール）。

  （2026-08-16 第4巡 Codex レビュー Medium 指摘: 上記「名前の正規表現」の重複記載を削除した。旧文はこのすぐ上に `^[a-z0-9][a-z0-9-]{1,48}$`（`\Z` ではなく `$` を使う誤った旧版）をもう一度記載しており、この節の直前の箇条書きにある正しい `\Z` 版と矛盾していた。正は 1 つ上の箇条書きの `\Z` 版のみである。）

### S-10: PULL 側（新規 `scripts/hh_skill_sync.py`）

CLI: `python scripts/hh_skill_sync.py [--pull] [--reconcile] [--dry-run]`。（`--forget` は v1 では作らない。S-12）

**手順（順序は安全性要件であり、実装者が入れ替えてよいものではない）**

0. **緊急停止スイッチの確認とプロセス間ロックの取得**（2026-08-16 Codex レビュー High 指摘で新設）。
   - `~/.hh-agent/skill_sync_disabled` が存在する、または環境変数 `HH_SKILL_SYNC_DISABLED=1` なら、**何もせず終了する**（S-12 の緊急封じ込め）。
   - **`promote` と `sync` は同じ資源を共有する** — `promote_staging/<name>`、配置先 `~/.hermes/skills/<name>/`、`promote_backups/`、`promote_log.jsonl`、`skill_sync_state.json`。12h のスケジュールタスクは人間が対話的に promote している最中にも発火しうる。**排他しないと、`_write_staging()` の検証後から `install_staged_skill()` までの間に別プロセスが同じ staging パスを差し替え、「人間が確認したバイト列」「実際に配置されたバイト列」「監査に記録した digest」が食い違う。**
   - したがって `~/.hh-agent/locks/skill_promote.lock` を**両者が共有する単一のプロセス間ロック**とし、`hh_skill_promote.py` と `hh_skill_sync.py` の両方が取得する。ロック下で実行する区間は **assert → self-heal → staging → install → promote_log → state 更新** の全体（`_write_staging()` と `install_staged_skill()` の間でロックを手放さない）。
   - **ロックを人間の確認中に保持しない**（2026-08-16 第2巡 Codex レビュー High 指摘）。`hh_skill_promote.py` は全文表示と TTY 確認の**前ではなく後**にロックを取り、`_write_staging()` → `install_staged_skill()` → `promote_log` → state 更新 の書き込み区間だけを保持する。**確認に何十分かかってもロックは占有されない。** ただしロック取得後、書き込みを始める前に `assert_staging_root_is_safe()`・**`self_heal_orphaned_promotions()`**・「隔離領域の SKILL.md ダイジェストが確認時と同一か」を**再実行・再検査する**（確認中に環境が変わっていないことを保証する。ここを省くと確認と書き込みの間に窓ができる）。**`self_heal_orphaned_promotions()` をここでも呼ぶ理由**（2026-08-16 第8巡 Codex レビュー Medium 指摘で追加): `run_promote()` は TTY 確認**前**（ロック取得前）にも自己修復を1回行う（人間へ表示する内容を汚さないため）が、その後・ロック取得までの間に**別プロセス（`hh_skill_sync.py` の同期）がクラッシュして新たな孤児を残す**窓が存在する。ロック取得後にもう一度呼ぶことで、この窓で生まれた孤児も commit 前に確実に回収する。`self_heal_orphaned_promotions()` はべき等な設計（存在しない孤児に対しては何もしない）なので、2回呼んでも副作用は増えない。
   - **stale 判定を経過時間だけで行わない**（同 High 指摘。旧案の「30 分超で奪取」は、人間が長時間確認している最中の promote を sync が破壊しうる）。ロックファイルには **所有者 nonce（uuid4）・PID・開始時刻・heartbeat 時刻**を書き、保持側は 30 秒ごとに heartbeat を更新する。奪取してよいのは **heartbeat が 5 分以上更新されておらず、かつ記録された PID が生存していない**場合に限る。奪取時は所有者 nonce を照合してから置き換え、必ずログに残す。**この方式（ローカルファイル・O_EXCL・PID 生存確認）が有効なのは Windows ネイティブ側だけ**——同一 OS 上の単一ファイルシステムであり、`promote` と `sync` は同じマシン上の別プロセスとして動くため、`O_EXCL` と PID 生存確認がそのまま使える。
   - **Modal 側にも排他機構を置く**（2026-08-16 第7巡 Codex レビュー High 指摘で新設。**2026-08-16 のユーザー却下を受けた S-08b/S-08c への差し替えにより、このロックが排他すべき対象が変わった**）。当初は「人間が `modal shell` で対話的に promote する経路」と `sync_dashboard_skills` の定期実行の競合を防ぐためのものだったが、**S-08b により Modal 側で人間が対話的に promote する経路自体が無くなった**（Modal 発スキルの確認・署名は必ず Windows から行い、Modal へは通常の pull 経路でしか配置されない・S-08b）。したがって Modal 側で `~/.hermes/skills/`・`promote_backups/`・`promote_log.jsonl`・`skill_sync_state.json`（Volume `hh-agent-dashboard-home`）へ書き込むアクターは**`sync_dashboard_skills` 1 つだけ**になった。**このロックの対象範囲から quarantine は外れる**（2026-08-16 第3巡 Codex レビュー High 指摘で訂正: quarantine〈`skills_quarantine/`〉は実際には**別の Volume `hh-agent-store`**〈Hub 所有〉上にあり、このロックが守る `hh-agent-dashboard-home` とは無関係。quarantine の消し込みは S-08c が Hub の既存 `POST /api/skills/publish` の create-or-match-only 排他機構〈`modal.Dict.put_if_absent`〉をそのまま再利用する別経路であり、このロックへの参加は不要かつ的外れだった。当初「Distiller もこのロックに参加させる」としていた記述は撤回する）。
   - このロック自体は残す。理由: `sync_dashboard_skills` は `max_containers=1` で多重起動を構造的に避けているが、コンテナ再起動・再スケジュールの端境期に短時間の重複実行がゼロとは断定できない防御的な意味がある。**排他が守るべき資源の性質（Modal Volume 上のファイル作成は複数コンテナ間で原子的ではない）自体は変わっていないため、機構は Corpus2Skill 側 S-01・従来の Modal 側排他と同じ結論（`modal.Dict` の `put(..., skip_if_exists=True)`、stale ロックの自動奪取は実装しない）をそのまま維持する。**
     - キーは `skill_promote_lock:hh-agent-dashboard-home`（変更なし）。値に取得者 nonce・時刻を入れ、`try/finally` で削除する（削除は自分の nonce と一致するときだけ）。
     - **ロックが取れなければ、`sync_dashboard_skills` は「今回はスキップ、次の周期に回す」で正常終了する**（ノンブロッキング。待つ相手＝対話的な人間がもう存在しないため、Windows 側のようなタイムアウト付き待機は不要になった）。
     - **stale ロックの自動奪取は実装しない**（Corpus2Skill 側 S-01 と同じ理由: `modal.Dict.pop` に条件付き削除がなく、「古いか確認してから消す」が原子的な compare-and-delete にならないため、複数の奪取者が同時に奪取判断をして二重 writer を生みうる）。異常終了でロックが残った場合は人間が Modal CLI で手動で消す（手順を README に書く）。
     - **Windows 側のローカルロックと Modal 側の `modal.Dict` ロックは別の名前空間であり、互いに関知しない**（Windows のプロセスと Modal のコンテナが同じ資源を同時に触ることはそもそも無い——Windows は `%HERMES_HOME%`、Modal は Volume 上の別パスである）。
   - **解放は所有者 nonce が一致するときだけ行う**（他人のロックを解放しない）。解放は `try/finally` で行い、`BaseException`（`KeyboardInterrupt` を含む）でも必ず実行する。
   - **`sync` 側はノンブロッキング**で取りに行き、取れなければ「今回はスキップ」して正常終了する（人間の promote を待たせない・スケジュールタスクを溜めない）。**`promote` 側はタイムアウト付き（既定 60 秒）で待ち**、取れなければ「同期処理が実行中です」と明示して失敗する。無期限に待たない。
   - 同一プロセス内での再入を想定しない（`sync` は `install_confirmed_skill()` を呼ぶ側でだけロックを取り、その内側で再取得しない）。
   - **既知の限界（バックログ・v1 では受容する。2026-08-16 第9巡 Codex レビュー Medium 指摘）**: 本項冒頭は「ロック下で実行する区間は assert → self-heal → staging → install → promote_log → state 更新の全体」としているが、`sync_pull()`（S-10 疑似コード）は S-10 手順3（整合性検証・分類判定）を**ロック取得より前**に行い、`push_to_lane_c()`・一部の state 更新をロックの**外**で行う（`run_promote()` 側も `push_to_lane_c()` はロック外・S-08）。したがって「分類判定の材料になった `skill_sync_state.json` の内容」と「実際にロックを取ってから書き込む内容」の間には、理論上ごく短い窓が残る（例: 分類直後にもう一方のプロセスが同じ name の state を書き換える）。**v1 でこれを完全に閉じるには分類フェーズ自体をロック内に取り込む必要があり、それは「ロックを短時間だけ持つ」という S-10 の設計方針（人間の確認中や HTTP 通信中にロックを占有しない）と衝突する。** 個人・低頻度運用でこの窓に実際に当たる確率は無視できるほど小さく、**当たった場合の結果も「衝突として検出され通知される」（S-10 手順3 の衝突分類・S-11）であり、静かなデータ破損にはならない**ため、v1 はこの限界を受容し実装を複雑化させない。将来 3 台以上の同時運用が増えるなど実害の兆候が出れば、分類フェーズもロック内に含める設計へ拡張する。

1. **`assert_staging_root_is_safe()` を呼ぶ。** 個々の同期対象に依存しない構造的な安全性なので、対象の取得より先に行う（`run_promote()` が 2026-08-11 Codex レビュー Critical で確定させた順序と同じ理由）。続けて `self_heal_orphaned_promotions()`。**この2つの順序は `run_promote()` と完全に同一でなければならない**（`install_confirmed_skill()` 切り出しリファクタでこの順序が崩れていないことをテストで固定する・S-14）。
2. `GET /api/skills/list` → `{"skills": [{name, content_sha256, revision, received_at, promoted_at_ms, promotion_seq, origin_instance, origin_seq_watermarks}], "events": [...], "next_cursor": ...}`（本文を含まない軽量な一覧。全件の本文を毎回落とさない）。**`origin_seq_watermarks: {"<origin_instance>": <int>}`** は `--repair-seq`（S-06b）が使う（`Corpus2Skill/doc/03_Architecture.md` §13 S-03。2026-08-16 第5巡 Codex レビュー Medium 指摘: この応答例に抜けていた）。`events` はページングされる（1 回 50 件上限）。扱いは S-11。
3. **整合性検証フェーズ →（通過したものだけ）分類フェーズ**の 2 段で処理する（2026-08-16 第3巡 Codex レビュー High 指摘: 1 枚の表に正常系と異常系を混ぜたため、`local.sha == remote.sha` かつ `remote.revision < state.lane_c_revision` のように**複数行に該当して優先順位が決まらない**ケースがあった）。

   **フェーズ A: 整合性検証（先に行う。1 つでも該当したら、その name は分類フェーズへ進めず「整合性異常」として通知し、ローカルへ書かない）**

   | 検査 | 異常とみなす条件 |
   |---|---|
   | 型・範囲 | `revision`／`promotion_seq` が整数でない・負数・bool・異常に大きい、`skill_sync_state.json` のスキーマ不正 |
   | revision の逆行 | `remote.revision < state.lane_c_revision` |
   | CAS 不変条件の破れ | `remote.revision == state.lane_c_revision` かつ `remote.sha != state.content_sha256`（同じ revision なら同じ内容のはず） |
   | **watermark との矛盾**（2026-08-16 第6巡 Codex レビュー High 指摘で新設） | `remote.promotion_seq < origin_seq_watermarks[remote.origin_instance]`（`list` 応答に含まれる。S-09・`Corpus2Skill/doc/03_Architecture.md` §13 S-04 検証8） |

   **watermark との矛盾チェックが必要な理由**: `accepted_seq` によるリプレイ拒否（S-06b）は「このローカルインスタンスが**過去に一度でも見た**最大の seq」を基準にするため、**そのローカルインスタンスがその name をまだ一度も見たことがない場合（初回 pull・`--repair-seq` 直後・将来の3台目以降の新規インスタンス）には効かない**。サーバーは seq の逆行を advisory 扱いで受理し続けるため、書き込み鍵を持つ攻撃者が古い正規署名版を CAS で `current` に戻すと、**初めてその name を見る受信者はそれを「初回の正規な pull」として無条件に受け入れてしまう**。`origin_seq_watermarks` は CAS の勝敗と無関係にサーバーが観測した「その origin から一度でも来た最大の `promotion_seq`」を保持しているため、**現在 `current` になっている版の seq がこの watermark を下回っていれば、それは何か（ロールバック・リプレイ・レース）が起きた証拠になる** — 初回 pull であっても、これを整合性異常としてブロックし通知する。

   状態ファイルのスキーマが壊れている場合は全エントリを破棄し、全 name を「初回」として扱う（下表の最終行）。**ただし「初回」扱いになった場合も上記の watermark との矛盾チェックは省略しない**（初回だからこそ最も脆弱な経路であるため）。

   **フェーズ B: 分類。時計を跨いだ大小比較を一切しない**（確定事項 I）。判定材料は「ダイジェスト」と「サーバー採番の単調増加 `revision`」だけであり、**ローカル時刻とリモート時刻を比べる式を書かない。`received_at` も比較に使わない**（監査表示専用。時刻は逆行しうるが `revision` は逆行しない・S-11）。

   ローカル側の状態は `~/.hh-agent/skill_sync_state.json`（`{name: {"content_sha256": ..., "lane_c_revision": ...}}`、原子的書き込み）に持つ。これは「このローカル内容は Lane C のどの版に対応しているか」を表すウォーターマークで、**push 成功時（応答の `revision`）と pull 成功時（取得した `revision`）の両方で更新する**。

   | 状況 | 判定 |
   |---|---|
   | `local.sha == remote.sha` | 何もしない（**本文は書き換えない**が下記「メタデータの自己修復」を行い、ウォーターマークも補記する） |
   | リモートにのみ存在 | **pull** |
   | ローカルにのみ存在 | **push**（`base_revision = 0`。下記の来歴条件を満たす場合のみ） |
   | sha 不一致・ローカル内容は最後の同期時点から変わっていない（`state.content_sha256 == local.sha`） | **pull**（リモートだけが進んだ） |
   | sha 不一致・ローカルが同期後に再 promote された（`state.content_sha256 != local.sha`）かつ `remote.revision == state.lane_c_revision` | **push**（ローカルだけが進んだ。`base_revision = state.lane_c_revision`） |
   | sha 不一致・ローカルも変わり、かつ `remote.revision > state.lane_c_revision`（＝双方が最後の同期以降に動いた） | **衝突**。下記の解決手順へ |
   | `skill_sync_state.json` に当該 name が無い（初回・状態ファイル欠損・破損） | ローカルにも実体があり sha 不一致なら**衝突として扱う**（安全側。推測でどちらかを消さない）。リモートにのみ存在するなら pull |
   | `local.sha == remote.sha` かつ `remote.revision > state.lane_c_revision`（内容は同じで revision だけ進んだ） | **リモートの内容は変わっていない。** 書き込みは行わず、**メタデータの自己修復**（下記）を行った上でウォーターマークだけ `remote.revision` へ更新する |

   **メタデータの自己修復（`local.sha == remote.sha` の全分岐で必須。2026-08-16 第4巡 Codex レビュー High 指摘で新設。2026-08-16 第5巡 Codex レビュー High 指摘で判定条件を訂正）**: `sync_pull()` は「本文配置 → receipt 保存 → `accepted_seq` 更新」の順で書く（下記疑似コード）。**本文配置の直後・receipt/state 保存の前にクラッシュすると**、次回実行時には `local.sha == remote.sha` に該当し、本文がすでに正しいという理由だけで何もしない実装だと、**`promote_receipts/<name>/current` が古い版を指したまま**（reconcile push が来歴を証明できず永久に `skipped(no-valid-receipt)` になる）・**`accepted_seq[origin]` が更新されないまま**（リプレイ防止の巻き戻り検査がこの分の seq を見ていないため、本来防げるはずの古い seq の再受理をこの name だけ見逃す）という 2 つの不整合が残る。

   **判定は `content_sha256` の一致だけで行ってはならない**（第5巡指摘: 同一バイト列が異なる origin・異なる `promotion_seq` で複数回 promote されるケースがあり得るため、「本文が一致する＝receipt も一致する」は成り立たない。content_sha256 だけを見て「もう保存済み」と誤判定すると、より新しい `promotion_seq` を持つ remote の receipt を取りこぼし、`accepted_seq` が古いまま据え置かれる）。したがって `local.sha == remote.sha` に該当した場合は必ず:

   1. **`accepted_seq` の更新に使う `origin_instance`／`promotion_seq`／`distilled_from_session_id` は、必ず remote の receipt が実際に署名しているタプルから取り出す。pull 応答が別送りしてくる同名フィールド（`remote.origin_instance` 等、C2S_SKILL_WRITE_KEY だけを持つ攻撃者が signature とは独立に細工できる自己申告値）をそのまま信用しない**（2026-08-16 第7巡 Codex レビュー High 指摘で訂正: 旧文は「receipt 文字列が完全一致すれば再検証しない」という高速パスを持ち、その上で `accepted_seq` の更新には remote 応答の `remote.promotion_seq`／`remote.origin_instance` フィールドをそのまま使っていた。しかし Corpus2Skill はこれらのフィールドを receipt とは無関係に不透明な自己申告値として保存・返却するだけであり（`Corpus2Skill/doc/03_Architecture.md` §13 S-01）、receipt 文字列自体が既知のものと一致していても、**同じ HTTP 応答に同梱された `promotion_seq`／`origin_instance` が signature の対象と一致している保証はない**。この高速パスは「receipt は本物だが、その脇に不正に水増しされた `promotion_seq` を付けて `accepted_seq` を先読みで押し上げる」攻撃を通してしまう。したがって**高速パスは廃止し、`local.sha == remote.sha` に該当した場合は毎回**:
   2. **remote receipt を通常の pull 検証（S-10 手順4。signature の再計算・canonical タプルの再構成を含む）に必ずかける**（HMAC の再計算は安価であり、「文字列が一致するから省略する」という最適化を行う理由がない）。canonical タプルは remote 応答の `content_sha256`／`origin_instance`／`promoted_at_ms`／`promotion_seq`／`distilled_from_session_id` から再構成し、receipt の signature と照合する。**この検証を通過して初めて、その中の `origin_instance`／`promotion_seq` を信用してよい**（検証を通れば、それらは署名時点の値そのものであることが保証される）。
      - **検証に成功**した場合: `promote_receipts/<name>/current` が指す receipt 文字列と remote の receipt 文字列が一致するなら `promote_receipts/` への書き込みは不要（既に同じものが保存済み）。一致しなければ `promote_receipts/<name>/<content_sha8>-<receipt_sha8>.json`（版管理形式・S-06b。ファイル名に receipt 自体のダイジェストも含むため、同じ content_sha256 でも receipt の中身が違えば自然に別ファイルになる）として保存し `current` を差し替える（本文は書き換えない。書き込むのはメタデータだけ）。**いずれの場合も**、検証済みの `origin_instance`／`promotion_seq` を使って手順3へ進む。
      - **検証に失敗**した場合: `promote_receipts/`・`current`・`accepted_seq` のいずれも更新せず、この自己修復全体を中断する（本文は既にローカルにあり D-16 的に無害だが、検証できないメタデータで `accepted_seq` を進めない）。
   3. `accepted_seq[<検証済み origin_instance>] < <検証済み promotion_seq>` なら `accepted_seq[<検証済み origin_instance>]` を `<検証済み promotion_seq>` まで進める。**この自己修復も `skill_sync_state.json` と `promote_receipts/` を書くため S-10 手順0 の共有資源に含まれ、`sync_pull()` と同じノンブロッキングの `promote_lock` の下で行う**（取れなければ今回はスキップし次回に回す。本文を書かない軽量な操作なのでブロッキング待ちにする必要はない）。

   **ファイル名衝突について（2026-08-16 第6巡 Codex レビューで解消）**: `promote_receipts/<name>/<content_sha8>-<receipt_sha8>.json`（S-06b「保存と検証」で定義）はファイル名に receipt 自体のダイジェストも含むため、同一本文が異なる receipt（異なる origin/seq/session）で複数回保存されても**自然に別ファイルになる**。Corpus2Skill 側の `versions/<revision>-<sha8>.md`（衝突時に `-2` サフィックスへ退避する規律・`Corpus2Skill/doc/03_Architecture.md` §13 S-01）とは異なる仕組みだが、目的（同名衝突での上書き事故を防ぐ）は同じであり、こちらは事後検出でなく命名規則そのもので衝突を構造的に回避している。

   **衝突（双方が動いた）の解決手順は一意に定める**（2026-08-16 Codex レビュー High 指摘。旧版は「衝突」と分類するだけで、ローカル版にはサーバー `revision` が無いため LWW が適用できず、実装者が判断に迷う状態だった）:

   1. **まず ntfy 通知を出す**（S-11。解決より先に人間へ知らせる）。
   2. **ローカル版を失わないことを最優先する。** ローカル版を `~/.hh-agent/promote_backups/<name>.bak.<ts>/` へ複製してから次へ進む（配置の入れ替えではなく複製。この時点ではまだローカルは正常な状態なので、退避ではなく写しを取る）。
   3. **リモート版を pull して配置する**（＝リモート優先）。理由: ローカル版は `promote_receipts/<name>/<content_sha8>-<receipt_sha8>.json`（版管理形式・S-06b）と `promote_backups/` に残っており人間がいつでも復元・再 promote できるのに対し、**自動 push でローカルを勝たせると、もう一方のインスタンスで人間が確認したリモート版が Lane C 上で上書きされ**、そちらのインスタンスには写しが残らない。**復元可能な側を犠牲にする**のが正しい非対称性である。
   4. `promote_log.jsonl` に `provenance="sync-conflict"`、勝者・敗者の digest、退避先パス、通知結果を記録する。
   5. **衝突時に自動 push はしない**（`base_revision` 不一致のまま強制 push すると、通知を見た人間が状況を確認する前に相手側を上書きしてしまう）。ローカル版を採用したい場合は、人間が退避から戻して改めて `hh_skill_promote.py` を実行する（＝もう一度全文を確認する）。

   **push 候補には追加の必須条件がある**: `~/.hh-agent/promote_receipts/<name>/current` が指す receipt（版管理形式・S-06b）が存在し、現在のローカル本文の sha256 と一致して検証できること（S-06b）。これが S-05 の「来歴が確認できるものだけを送る」を担保する。**`promote_log.jsonl` の記載を根拠にしない**（改ざん検知の無い JSONL であり、人間確認の証明にはならない・2026-08-16 Codex レビュー High 指摘）。receipt を検証できないもの（手書きスキル・Hermes 同梱スキル・promote 後に手編集されたスキル）は**黙って無視せず** `skipped(no-valid-receipt)` として監査に残す（設計原則: 黙って飛ばす実装は原因を隠す）。

   **`skill_sync_state.json` は補助的なヒントであって真実ではない。** 実体の真実は常にディスク上の SKILL.md のダイジェストと receipt であり、状態ファイルが壊れていた・消えていた場合は上表のとおり安全側（衝突扱い＝通知して人間に見せる）へ倒す。状態ファイルを唯一の根拠にして書き込み・削除を決める実装にしない。
4. pull 候補について**受信側検証**（S-06-3）を全部通してから書き込む。1つでも落ちたら書き込まずスキップ＋監査:
   - `NAME_RE` 一致 / 本文が UTF-8 として妥当 / UTF-8 バイト列で 64KB 以下
   - `skill_quarantine.parse_frontmatter_name()` の結果がリクエストの `name` と一致
   - 実測 sha256 が `content_sha256` と一致
   - **promote receipt が検証できる（S-06b）。これが最重要であり、他がすべて通ってもこれに落ちたら書き込まない。** receipt 不一致は ntfy 通知の対象（正規でない書き込みが Lane C に起きたことを意味する）
   - `origin_instance` が `^[a-z0-9][a-z0-9._-]{0,63}$`、`distilled_from_session_id` が `null` または 128 文字以下の `^[A-Za-z0-9_-]+$`、`promoted_at_ms`／`promotion_seq` が非負整数（型・長さ・文字種を無検証で監査ログや通知本文へ流さない）
   - `redact_text()` を適用して差分ゼロ
5. **書き込みに新しい素朴なファイル書き込みを一切作らない。** `hh_skill_promote.py` が既に持つ経路をそのまま再利用する:
   - `run_promote()` の後半を **`install_confirmed_skill(name, content_bytes, digest, *, force, provenance) -> dict`** として切り出す（純粋なリファクタ。内部は `_write_staging()` → `install_staged_skill()` の2段のみで、既存の順序・内容・例外は一切変えない。**`append_promote_log()` はここに含めない** — 下記疑似コードのとおり、`append_promote_log()` は呼び出し元（`run_promote()`／`sync_pull()`）がそれぞれ自分の `provenance`・`promoted_at_ms`・（sync 側は）`synced_at` を渡して個別に呼ぶ。2026-08-16 第8巡 Codex レビュー Medium 指摘で訂正: 旧文はここで「`append_promote_log()` の順序・内容も変えない」と書いており、それを `install_confirmed_skill()` の内部ステップであるかのように読めたため、下記疑似コードとの食い違いを解消した）。
   - `run_promote()` は「読む → 全文表示 → TTY 確認 → `install_confirmed_skill(provenance="local-promote")` → S-08 の push」になり、pull は「HTTP 取得 → 手順4の検証 → `install_confirmed_skill(provenance="sync-pull:<origin_instance>")`」になる。
   - **`confirm_or_abort()` は pull 経路から呼ばれないが、それは「引数で確認を飛ばせるようにする」形で実現してはならない。** `--yes` / `--non-interactive` のようなフラグを `hh_skill_promote.py` に**絶対に追加しない**（そのフラグが存在した瞬間、人間の promote 経路でも全文確認を飛ばせるようになり、D-16 の唯一の人間ゲートが無効化できてしまう）。確認は `run_promote()` の中にだけ残る構造にする。**これは設計上の確定事項である。**
   - **リファクタ後の呼び出し順を疑似コードで固定する**（2026-08-16 第3巡 Codex レビュー High 指摘。「どこがロック・再 assert・self-heal・再ダイジェスト検査を所有するか」が一意でなかった）:

     ```
     run_promote(name, force):                      # 人間の対話経路
         assert_staging_root_is_safe()              # 1. 構造的安全性（対象に依存しない）
         self_heal_orphaned_promotions()            # 2. 過去のクラッシュ回復
         validate_name(name)
         content, digest = read_quarantined_skill(name)   # 3. 1回だけ読む
         display_for_confirmation(name, content, digest)  # 4. 全文表示
         confirm_or_abort(name, digest)                   # 5. TTY 確認（★ロックは保持しない）
         with promote_lock(timeout=60):                   # 6. ここで初めてロック
             assert_staging_root_is_safe()                # 7. 確認中に環境が変わっていないか再検査
             self_heal_orphaned_promotions()              # 7b. 確認中に他プロセスが残した孤児を回収（第8巡指摘で追加）
             recheck_quarantined_digest(name, digest)     # 8. 隔離本文が差し替わっていないか再検査
             install_confirmed_skill(name, content, digest,
                                     force=force, provenance="local-promote")
             promoted_at_ms = int(time.time() * 1000)     # 8b. 3箇所で使う値を1回だけ生成（第8巡指摘）
             seq = allocate_promotion_seq(name, origin_instance=self_instance_id)  # 9. 採番→永続化（receipt より前。★S-08b: origin明示）
             receipt = write_receipt(name, content, digest, seq, promoted_at_ms,
                                     origin_instance=self_instance_id)  # 10. receipt（S-06b。★S-08b: origin明示）
             record = append_promote_log(provenance="local-promote",
                                         promoted_at_ms=promoted_at_ms)  # 11. 監査（値は生成しない・受け取る）
         push_to_lane_c(promoted_at_ms=promoted_at_ms, receipt=receipt,
                        origin_instance=self_instance_id, ...)  # 12. ロック外。失敗しても promote は成功（S-08。★S-08b: origin明示）

     install_confirmed_skill(...):                    # 人間経路と sync 経路の共通部分
         # 前提: 呼び出し側が既にロックを保持し、assert 済みであること（自分では取らない）
         staged = _write_staging(name, content, digest)
         backup = install_staged_skill(name, staged, force=force)
         return {"backup_path": backup, ...}

     sync_pull(name, remote):                         # 同期経路
         # フェーズA/B の判定（手順3）と受信側検証（手順4）を済ませてから:
         with promote_lock(nonblocking=True) as got:   # 取れなければ今回はスキップ
             if not got: return "skipped(locked)"
             assert_staging_root_is_safe()
             self_heal_orphaned_promotions()
             install_confirmed_skill(name, content, digest,
                                     force=True, provenance=f"sync-pull:{remote.origin}")
             save_verified_receipt(name, remote)        # S-06b（current を差し替え）
             append_promote_log(...); update_sync_state(...)
     ```

     **`self_heal_orphaned_promotions()` は必ず `assert_staging_root_is_safe()` の後**（2026-08-11 Codex レビュー Critical で確定した順序）。**`install_confirmed_skill()` は自分ではロックを取らない**（再入を作らない・S-10 手順0）。

     **2026-08-16 S-08b 追記**: 上記疑似コードの `allocate_promotion_seq(name)`・`write_receipt(...)`・`push_to_lane_c(...)` は、S-08b の `run_remote_promote()` からも呼ばれるため、**`origin_instance` を明示引数として受け取るようシグネチャを拡張する**（`run_promote()` は自分自身の `instance_id` を渡し、`run_remote_promote()` は接続先設定〈`remote_sources.json`〉の固定値を渡す——**リモートの読み取り専用エンドポイント応答が返す `origin_instance` はそのまま渡さない**〈2026-08-16 第5巡 Codex レビュー Medium 指摘で訂正: 旧文はここだけ「応答が返す値を渡す」という第3巡以前の古い契約のまま残っており、S-08b が確定させた「応答値は表示専用、署名は接続先設定の固定値」という契約と矛盾していた〉。暗黙に「自分自身の `instance_id.json`」を読む実装のままにしない）。
   - pull は既存ローカルスキルの上書きを伴いうるため `install_staged_skill(..., force=True)` 相当の経路を通る。既存は `~/.hh-agent/promote_backups/<name>.bak.<ts>/` へ退避されてから配置されるので、負けた版も復旧できる。
6. `append_promote_log()` へ `provenance`（`"local-promote"` / `"sync-pull:<origin_instance>"`）と `synced_at` を含む1行を追記する。これが S-06 の「複製経路である」ことの監査上の証跡になる。既存 record へのフィールド追加は後方互換（JSONL 追記のみで既存行を書き換えない）。`license_confirmed` は元 promote の確認を継承する意味で `true` のまま残し、`provenance` で区別できるようにする。

**実行タイミング**

| インスタンス | 方式 |
|---|---|
| Windows ネイティブ | 既存 `HH-Agent-TokenRefresh` と**同じパターン**の新規スケジュールタスク `HH-Agent-SkillSync`（**12h 毎**・確定事項 K、`scripts/register_skill_sync_task.ps1`）。**既存タスクへ相乗りしない** — トークン更新は安全性クリティカルで、スキル同期の失敗（ネットワーク断等）に巻き込みたくない。加えて任意の手動実行。 |
| Modal ダッシュボード | **`hh-agent-secret` を持つ別 Modal Function `sync_dashboard_skills`（`schedule=modal.Period(hours=8)`）** が Volume `hh-agent-dashboard-home` を `/opt/data` にマウントして pull・検証・配置を行い、`volume.commit()` する。`refresh_dashboard_agent_token` と同じ形（S-06b）。 |

**確定事項 G（常駐での定期チェックまで実装する）との関係 — 実装形態を変更した（2026-08-16 第2巡 Codex レビュー Critical 指摘）**

初版は「ダッシュボードの ASGI プロセス内にデーモンスレッドを常駐させる」設計だった。これは **C-3（エージェントが動くコンテナに署名鍵を置かない）と両立しない**ため撤回する（S-06b）。**「起動時1回だけで終わらせず定期的にチェックする」というユーザー確定の意図は、別 Function の `modal.Period(hours=8)` で満たす。**

- **`modal_dashboard/app.py` のブートストラップから pull を呼ばない**（呼ぶと鍵が要る）。ダッシュボードコンテナは Lane C を一切知らない。
- **Volume の可視性について正直に書く**: `sync_dashboard_skills` が書いた内容は、既に起動中のダッシュボードコンテナには `reload()` するまで見えない。しかしダッシュボード Function は `min_containers=0`・`scaledown_window=300`（`08_Phase1c_Spec.md §2.2`）で**アイドル 5 分でコンテナが落ちる**ため、次にアクセスしたときのコールドスタートで新しいスキルが見える。個人利用のペースではこれで十分であり、**動いているコンテナへ横から差し込む仕組み（reload の強制・プロセス間通知）は作らない**（開いているファイルがあると Volume の reload 自体が失敗しうるため、壊れやすい方向へ複雑さを足すことになる）。
- 長時間連続でチャットし続けた場合のみ、新しいスキルの反映がそのセッション中は起きない。**これは既知の制約として受け入れる**（回避したければダッシュボードのタブを閉じて開き直せばよい）。
- `sync_dashboard_skills` は `max_containers=1` で定義し、同 Function の多重起動による Volume 競合を構造的に避ける。1 回の実行にタイムアウト（合計 5 分）を設ける。例外は捕捉してログに出し、**次の周期で再試行する**（1 回の失敗で以後の同期が永久に止まらないこと）。


**Hermes の起動フックは使わない。** Hermes 本体を無改変に保つ D-01 に反するうえ、`hermes` 起動のたびにネットワーク待ちが入る。**「起動時1回」は自動実行としては存在しない**（2026-08-16 第4巡 Codex レビュー Medium 指摘で訂正: 旧文はここで「定期タスク＋起動時1回」と書いており、Modal 側のブートストラップからは pull を呼ばないという直前の記述（S-10 実行タイミング表・上記）と矛盾していた）。実行されるのは**定期実行のみ**（Windows: `HH-Agent-SkillSync` の 12h スケジュールタスク＋人間による任意の手動実行。Modal: `sync_dashboard_skills` の `modal.Period(hours=8)`）であり、これで個人利用のペースには十分（v1 スコープ (b)）。

### S-11: コンフリクト処理 = **`revision` + CAS による直列化** + ntfy 通知（確定事項 H・I）

同じスキル名が2インスタンスで別内容に promote されるケース（個人利用なので稀）は、マージロジックを作らない。

**用語の訂正（2026-08-16 Codex レビュー High 指摘）**: 当初この節は「last-write-wins（サーバー受信時刻基準）」としていたが、実際に採用するのは**時刻ではなく単調増加 `revision` に対する CAS** である。「後から書いた方が勝つ」という結果は同じだが、**勝敗を時計で決めないこと**が要点なので、以後 LWW という語は使わない。どちらが新しいかは常に `revision` が決める。

- **順序の権威は Corpus2Skill サーバーが採番する単調増加の `revision`**（確定事項 I の趣旨を、より強い形で実装する）。クライアント申告の `promoted_at` は使わない。理由: Windows と Modal コンテナで時計がずれていると `promoted_at` の大小が逆転し、**新しい方が古い方に静かに上書きされる**。順序を 1 か所（Corpus2Skill）でだけ決めれば、この失敗モードは構造的に存在しなくなる。
  - **`received_at`（サーバー時刻）ではなく `revision`（整数カウンタ）を権威にする**理由（2026-08-16 Codex レビュー High 指摘）: サーバー側の `time.time()` も NTP 同期・コンテナ再起動で逆行しうるうえ、同一秒内の連続 push で同値になる。**全順序を保証する必要があるものに壁掛け時計を使わない。** `received_at` は監査・人間向け表示専用に残す。
  - `revision` は name ごとに `meta.json` 内で単調増加させる（`Corpus2Skill/doc/03_Architecture.md` §13 S-04）。**両設計書で同一の結論であること。**
- **更新は CAS（compare-and-swap）で行う**: push は `base_revision`（クライアントが知っている最新 revision）を必ず添える。
  - **D-11「compare-and-set は使わない」と矛盾しないことの確認**: あれは **Hub の `modal.Dict` に CAS API が存在しない**という制約に基づく決定であり、承認状態の話である。ここで CAS を行うのは **Corpus2Skill の Volume 上のファイル**であり、`meta.json` の read-modify-write を name 単位のロック下で行うことで実現する（`Corpus2Skill/doc/03_Architecture.md` §13 S-01）。**別のストレージ・別の制約であり、D-11 を破っていない。** 実装者はこの CAS を `modal.Dict` で実装しようとしないこと。サーバー側の現在値と一致しなければ **409 を返し、書き込まない**。一致すれば `revision + 1` を採番する。これにより「知らないうちに他方が更新していた」ケースが静かな上書きではなく明示的な 409 になる。
- **タイブレーク**: CAS により同一 `revision` への同時 push は片方だけが成功するため、原理的にタイブレークは不要になる。それでも実装上 `revision` が同値で比較される箇所（例: 状態ファイル復旧時）では `content_sha256` の辞書順が小さい方を勝ちとし、判定を決定的にする（振動防止）。
- **負けた版は消えない**: pull 側は `~/.hh-agent/promote_backups/<name>.bak.<ts>/` に退避される（衝突時は退避ではなく複製・S-10 手順3）。push 側は Corpus2Skill の `skills_shared/<name>/versions/<revision>-<sha8>.md` に旧版が不変ファイルとして残る（**10 世代**でローテート・確定事項 J）。加えてローカルには `promote_receipts/<name>/<content_sha8>-<receipt_sha8>.json`（版管理形式・S-06b）が残るため、人間はいつでも元の版を復元して再 promote できる。
- **衝突は必ず ntfy 通知を1本出す（確定事項 H・2026-08-16）。黙って上書きする実装は不可。** 「後から書いた方が勝つ」方式の唯一の実害は「起きたことに気づけない」ことなので、そこだけを潰す。
  - **「通知すべき事象」を 3 つに区別する**（2026-08-16 第2巡 Codex レビュー High 指摘。初版は通常の更新まで `conflict` 扱いになっており、再 promote のたびに通知が出る設計になっていた）:

    | 事象 | 判定者 | 通知 | サーバー `_events/` |
    |---|---|---|---|
    | **通常の CAS 更新**（`base_revision` 一致） | サーバー | **出さない** | 記録しない |
    | **CAS 409**（`base_revision` 不一致）・形式不正・`promotion_seq` 逆行・件数上限超過 | サーバー | 出す | **記録する** |
    | **署名（receipt）検証失敗**・S-10 手順3 の整合性異常 | **クライアントのみ**（サーバーは鍵を持たず判定できない・S-06b） | 出す | **記録しない**（クライアント側アウトボックスで扱う） |

  - **サーバーが判定できる事象は durable に持つ**（レスポンスが失われても消えないように）: `skills_shared/_events/<event_id>.json`（1 イベント 1 ファイルの不変 JSON。共有 JSONL への複数コンテナ追記を避けるのは §5.3 の監査と同じ理由）。`GET /api/skills/list` が**未 ACK のイベントをページングして**返し、同期スクリプトが通知してから `POST /api/skills/events/ack` で ACK する。
  - **イベントは無制限に溜めない**（同 High 指摘）: `list` が返すイベントは 1 回あたり最大 50 件＋カーソル、未 ACK の総数上限は 500 件（超過分は同種イベントを集約して 1 件にまとめる）、ACK 済みは 30 日で GC する。**イベントが溜まって `list` が肥大化し同期全体が止まる**という失敗モードを作らない。
  - **クライアントのみが判定できる事象は、クライアント側の永続アウトボックスで持つ**: `~/.hh-agent/skill_sync_outbox.jsonl` に記録し、**ntfy 送信に成功するまで消さない**（次回起動時に再送）。同一内容の再通知は `event_id`（内容のハッシュ）で重複排除する。
    **対象は「クライアントが判定するすべての通知事象」**（2026-08-16 第3巡 Codex レビュー High 指摘。初版は署名検証失敗だけを挙げ、双方変更の衝突は `promote_log` に残すだけで通知の再送対象になっていなかった）: 署名検証失敗・整合性異常（S-10 フェーズ A）・**双方変更の衝突**・pull による新規インストール／更新（残存リスク R-1 の緩和 (a)）。**通知に失敗したまま処理を先へ進める場合でも、アウトボックスには必ず残す。**
  - **`list` のイベントは全ページ読み切ってから ACK する**: `next_cursor` が `null` になるまで取得し、通知に成功したものだけを ACK する（途中のページで打ち切って ACK すると、未読のイベントが未 ACK のまま溜まり続ける）。
  - **イベントの集約と不変性の両立**: `_events/<event_id>.json` は不変のまま保ち、**集約は「集約レコードを新規に 1 件書き、集約された個々のイベントを ACK 済みへ移す」**という形で行う（既存ファイルの `occurrences` を書き換えない・同 High 指摘）。異なる `(type, name)` の種類自体が 500 件を超えた場合は、**最古のものから ACK 済みへ移し、「N 件のイベントを取りこぼした」という 1 件の集約レコードを残す**（黙って捨てない）。
  - **ACK は「通知が送れた」ことを条件にする。** ntfy 送信に失敗したイベントを ACK しない（次回また通知を試みる）。
  - **ACK 権限は push 権限と分けない場合の限界を明記する**: 書き込み鍵を持つ攻撃者は自分が起こしたイベントを ACK して隠せる。v1 はこれを**受け入れる**（個人利用・鍵が漏れている時点で他の防御が主）。ただしクライアント側アウトボックスの通知は攻撃者が消せないため、**署名検証失敗だけは必ず人間に届く**。
  - 送信は既存 `modal_hub/services/notifier.py` の ntfy 送信部を流用するが、**そのまま import してはならない**（2026-08-16 第2巡 Codex レビュー Medium 指摘）。`notifier.py` はモジュール読み込み時に `modal_hub.core.store` を import し、`store.py` は**モジュールトップで `modal` を import する**。Windows のスケジュールタスクから動く `hh_skill_sync.py` を `modal` パッケージに依存させたくない。
    → **`_send_with_retries()` 相当の HTTP 送信部を `modal_hub/services/ntfy_client.py`（store 非依存・新規）へ切り出し**、`notifier.py`（承認通知・store 依存）と `skill_sync.py`（store 非依存）の両方がそれを使う。切り出しは純粋なリファクタとし、承認通知側の挙動（リトライ回数・バックオフ・`NTFY_TOKEN` 省略時の Authorization ヘッダ非付与）を一切変えない。
    → 追加する公開関数は `send_skill_conflict(event) -> str` 1 つ。**`Title` / `Tags` ヘッダは ASCII 固定であることを送信前に検証する**（既存 `_send_with_retries()` が `Authorization` について行っている ASCII 検査と同じ理由: 非 ASCII が混ざると HTTP ヘッダとして壊れる。§9 の「レスポンスヘッダに日本語を入れない」と同種の地雷）。
  - **`send_approval_request()` と違い、`store`（Modal Dict）への `notify:<id>` 状態記録は行わない。** この関数はローカル PC のスケジュールタスクからも呼ばれ、そこには Hub の Dict が無いため。冪等性・証跡は `promote_log.jsonl` 側の 1 行で担保する。
  - **通知本文に SKILL.md 本文・差分を絶対に載せない**（§5.2 の「コマンド本文を通知に載せない」と同じ理由: ntfy 運営者・通知ログ・ロック画面に露出する）。載せるのは `{"event":"skill_conflict","name":"<name>","winner":"<origin_instance>","winner_sha8":"...","loser_sha8":"..."}` まで。スキル名はケバブケースの識別子であり本文ではないため、通知を実用的にする最小限として許容する（この逸脱は意図的であり、ここに記録しておく）。
  - **通知の失敗は同期を失敗させない**（フェイルオープン。ただし stderr と `promote_log.jsonl` には必ず残す）。通知が飛ばなかったことを黙殺しない。
  - **`NTFY_TOPIC` / `NTFY_TOKEN` の供給元を明示する**（2026-08-16 Codex レビュー High 指摘。既存 `config.ntfy_topic()` は `os.environ` からしか読まないため、Windows スケジュールタスクから起動した `hh_skill_sync.py` の環境には存在しない）。**`scripts/hh_issue_agent_token.py:_load_signing_key()` と同じ方式**でリポジトリ直下 `.hh-secret.env` から読み、`os.environ` へ注入してから `notifier` を呼ぶ。この読み取りヘルパーは 2 スクリプトで重複させず共通化する。`NTFY_TOPIC` が取得できない場合は通知を諦めるが、**その事実を stderr と監査に必ず残す**（トピック名自体は秘密として扱い、ログに出さない・§5.2）。Modal 側は既存どおり Secret 経由で環境変数に入る。
- **衝突の記録先は判定者によって異なる**（2026-08-16 第7巡 Codex レビュー Medium 指摘で訂正: 旧文は「`promote_log.jsonl` と Corpus2Skill 側の `meta.json` の両方に 1 行残す」としていたが、`meta.json` は現在版 1 件を指すコミット点であり衝突ログを追記する場所ではない。またこの書き方は、クライアントだけが判定できる事象（署名検証失敗・整合性異常。S-06b）までサーバー側に記録されるかのように読めてしまう）:
  - **双方変更の衝突**（S-10 手順3。ローカル・リモート双方が動いた場合）は、ローカルの `promote_log.jsonl`（`provenance="sync-conflict"`, 勝者・敗者の digest、`notify_state`）にのみ記録する。**Corpus2Skill 側には何も書かない** — この衝突はクライアントが pull 後に検出するものであり、サーバーは関与していない（サーバー側の `revision` は既に確定した単一の値を返しただけである）。
  - **サーバーが判定できる事象**（CAS 409・形式不正・`promotion_seq` 逆行・上限超過）は Corpus2Skill 側の `_events/`（`meta.json` ではない）に記録される（S-03・S-04）。ローカルはこれを次回 `list()` で受け取り、`promote_log.jsonl` にも記録する。
  - **クライアントだけが判定できる事象**（署名検証失敗・整合性異常・watermark 矛盾）はローカルの `promote_log.jsonl` と `~/.hh-agent/skill_sync_outbox.jsonl` にのみ記録され、Corpus2Skill 側には一切残らない（サーバーは判定できないため。S-06b・S-11）。

### S-12: 削除は同期しない（v1 で許容する既知の挙動・**先送りであって放棄ではない**）

ローカルでスキルディレクトリを消しても Lane C からは消えないため、次の pull で**復活する**。ユーザー確定（2026-08-16）: **v1 はこの挙動を許容する。**

- **削除の自動伝播を作らない理由**: 片方の事故（Volume の巻き戻し・手違いの `rm`・pull 中のクラッシュ）が、もう片方の正常なスキルまで巻き添えで消す経路になる。復元不能な操作を自動化しない。
- **v1 で tombstone / `--forget` を作らない理由**: 「復活する」実害が実際に出るかは運用してみないと分からず、先回りして削除経路を作ると、その経路自体が新しい事故の原因になる。Lane C の設計で最も危険な機能は削除である。
**ただし「通常の削除同期を作らない」ことと「事故が起きても止められない」ことは別問題である**（2026-08-16 Codex レビュー Medium 指摘）。悪意ある、または後から危険と判明したスキルを封じ込める手段は v1 に必ず入れる。ローカルで消しても pull で復活する以上、これが無いと**事故時に手が無い**:

- **緊急停止スイッチ**: `~/.hh-agent/skill_sync_disabled` ファイルの存在、または `HH_SKILL_SYNC_DISABLED=1` で、そのインスタンスの pull/push を丸ごと止める（S-10 手順0）。ファイル 1 個で止まることが重要（ネットワークもデプロイも不要）。
- **denylist**: `~/.hh-agent/skill_sync_denylist.json`（`{"names": [...], "content_sha256": [...]}`）に載る name / digest は **pull しない・push しない**。復活を止めつつ、Lane C 側を触らずにローカルだけで完結できる。
- **サーバー側の隔離（人間専用・同期クライアントからは呼べない）**: 有効な署名を持つ危険なスキルは Corpus2Skill 上に残り続けるため、**denylist を持たない新しいインスタンスを立てると再流入する**（2026-08-16 第2巡 Codex レビュー High 指摘）。したがって `skills_shared/<name>/` を `skills_quarantined/<name>/` へ退避する**運用手順**（**この `skills_quarantined` も Corpus2Skill 側 `_RESERVED_TOP_LEVEL_NAMES` へ必ず追加する** — 追加しないと `/recompile` の LLM 命名が同名カテゴリを作った瞬間に隔離したスキルが消える。§13 S-02 が指摘したのとまったく同じ穴を、隔離ディレクトリ自身で繰り返さないこと。2026-08-16 第3巡 Codex レビュー High 指摘）（Modal CLI もしくは Codex 経由の一度きりの操作）を手順書に含める。**この操作を同期クライアントから呼べる API にはしない**（削除経路を作らないという S-12 の方針を守るため。人間が Modal 側で直接行う）。
- **手順書**: 侵害が疑われるときの順序を設計書に明記する。**「まず止める・次に消す・次に鍵を替える・最後に原因究明」であり、逆順にしない。**
  1. 両インスタンスで緊急停止スイッチを立てる（`skill_sync_disabled`）。
  2. ローカルの当該スキルを削除し、denylist に digest を追加する。
  3. **漏えいの種別ごとに鍵をローテートする**（初版は Corpus2Skill の Bearer しか挙げていなかった。同 High 指摘）:

     | 漏えいしたもの | 影響 | 対応 |
     |---|---|---|
     | `CORPUS2SKILL_API_KEY`（読み取り。両インスタンスのプラグインに常駐） | Lane C の内容が読まれる。**書き込みは不可** | 鍵をローテートし、両インスタンスのプラグイン設定を更新 |
     | `C2S_SKILL_WRITE_KEY`（書き込み） | 不正な push・イベントの ACK 隠蔽が可能。ただし**署名検証で配布は阻止される** | 鍵をローテート（Codex 経由で Modal Secret 更新 → `modal app stop` → `deploy`）。アウトボックスの通知履歴を確認 |
     | `HH_AGENT_TOKEN_SIGNING_KEY`（署名鍵） | **最悪。任意の内容に正規 receipt を作れる＝承認ゲートごと破られている** | 署名鍵と Hub のトークンをすべてローテートし、`key_id` の切り替え（S-06b）を行い、**旧 key_id の receipt をすべて無効化**する。既存の同期済みスキルは全件、人間が再確認して再 promote する |
  4. Corpus2Skill 上の当該スキルを `skills_quarantined/` へ退避する（上記）。
  5. 原因が判明してから同期を再開する。

- **先送りであって忘れてよい話ではない。** 実際に「消したのに戻ってくる」が不便になった時点で、`scripts/hh_skill_sync.py --forget <name>`（Lane C 側に `deleted_at` の tombstone を立て実体を削除、pull 側は tombstone のある name を復活させない、より新しい push が来たら tombstone は上書きされる）を別途設計する。その際に必要になる変更は **(1) `meta.json` への `deleted_at` フィールド追加、(2) `/api/skills/forget` エンドポイント新設、(3) pull 側 受信検証（S-10 手順4）への tombstone 判定追加**の 3 点であり、v1 のストレージ形式とは前方互換に足せる（`meta.json` は未知フィールドを無視する読み方にしておくこと）。**v1 の実装時点で `deleted_at` を「使わないが置いておく」形で入れない** — 使われないフィールドは半端な実装を誘発する。

### S-13: v1 のスコープ外（明記。混同しないこと）

| # | 対象外 | 理由 |
|---|---|---|
| (a) | ローカル SessionDB（`~/.hermes/state.db`）の生履歴の同期 | 会話レベルの記憶は §13 の Memory Provider で解決済み。SQLite を Volume に置かない理由は §4.7 D-17 のとおり（複数コンテナからの更新が last-writer-wins で壊れる）。 |
| (b) | リアルタイム push / 購読 | **12h/8h の定期 pull（＋ Windows は人間による任意の手動実行）で個人利用のペースには十分**（2026-08-16 第5巡 Codex レビュー Medium 指摘で訂正: 旧文の「起動時 + 定期」は誤り。S-10 実行タイミング表のとおり自動の起動時 pull は存在しない — Modal はブートストラップから pull を呼ばず、Windows も Hermes 起動フックは使わない）。 |
| (c) | Lane B → Lane A の自動昇格 | **会話記憶側の別の未解決課題**（`Corpus2Skill/doc/03_Architecture.md` §12 未確定事項 D）。Lane C は「人間が確認済みのスキルの複製」であって昇格の自動化ではない。**この2つを混同しないこと。** |
| (d) | 隔離（未 promote）スキルの同期 | S-06-4。 |
| (e) | SKILL.md 以外の同梱ファイル | S-05。 |
| (f) | 3インスタンス目以降 | 設計上は N でも動くが、検証は2インスタンスで行う。 |
| (g) | Modal ダッシュボードのチャット UI からの promote（＝ブラウザ経由の TTY 相当確認 UI）／エージェント（Claude Code・Codex 含む）による Modal 側 promote の代行 | S-08b。C-3 により `dashboard_server` は署名鍵を持てないため構造的に不可能。**v1 では Windows 側からのリモート確認・署名（S-08b。新設の読み取り専用エンドポイント経由で quarantine を取得し、既存の Windows ローカル TTY 確認・署名フローを再利用する）で代替する**（2026-08-16 ユーザーが `modal shell` を人間が対話的に操作する運用案を却下したため全面差し替え。プロトコルは制約しない・運用のみの制約という位置づけは変わらない）。**エージェントによる代行は D-16 の人間ゲートを無効化するため明示的に禁止する**（2026-08-16 第6巡 Codex レビュー High 指摘。この禁止は差し替え後も変わらず有効）。2026-08-16 第5巡 Codex レビュー High 指摘で発見。 |

### S-14: テスト計画（設計段階で確定させておく要求）

通常のケースに加え、**以下は「無いことを固定する」テストであり省略してはならない**（§13 の `test_get_tool_schemas_excludes_write_tools` と同じ意図）:

- `hh_skill_promote.py` の argparse が `--yes` / `--non-interactive` / `--no-confirm` の類を**受け付けないこと**（S-10 手順5）。
- 受信側検証（S-10 手順4）のどれか1つでも落ちた場合、`~/.hermes/skills/<name>/` に**一切ファイルが現れないこと**（逆方向テスト。§4.7 の受け入れテストと同じ形式）。
- **receipt を持たない**手書きスキル・Hermes 同梱スキルが reconcile push の対象に**ならないこと**（`promote_log.jsonl` の有無ではなく receipt 検証が根拠であること）。
- push が例外を投げても `run_promote()` は成功し終了コード 0 であること（フェイルオープン）。逆に、promote 本体が例外で終わったときに push が**呼ばれないこと**。
- **衝突を検出したのに ntfy 通知を出さない経路が無いこと**（確定事項 H）。衝突の全分岐で `send_skill_conflict()` が呼ばれること、かつ通知が失敗しても同期自体は成功し `promote_log.jsonl` に `notify_state="failed"` が残ること。
- **通知本文に SKILL.md 本文が含まれないこと**（本文の一部文字列が通知ペイロードに現れないことを assert する。S-11）。
- **ローカル時刻とリモート時刻を比較する式が無いこと**（確定事項 I）。時計を大きくずらした環境（ローカルを未来／過去へ）でも S-10 手順3 の分類結果が変わらないこと — これが「順序の権威はサーバーが採番する単調増加 `revision` であり、`received_at`（サーバー受信時刻）は監査・表示専用で判定に使わない」（S-11）の実質的な受け入れテストである（2026-08-16 第5巡 Codex レビュー Low 指摘で文言を訂正: 旧文は「サーバー受信時刻を権威にする」と書いており、`revision` こそが権威であるという S-11 の結論と矛盾していた）。
- `revision` が同値で比較される復旧経路のタイブレークが決定的であること（振動しないこと）。
- **整合性異常の分類テスト**: `remote.revision < state.lane_c_revision`（巻き戻し）、同一 `revision` で異なる digest、`revision` が負数/bool/巨大値、`skill_sync_state.json` のスキーマ不正、**`remote.promotion_seq < origin_seq_watermarks[remote.origin_instance]`（watermark との矛盾。初回 pull・`skill_sync_state.json` 欠損後の「初回」扱いの場合も含む。2026-08-16 第7巡 Codex レビュー Medium 指摘で追加 — 第6巡で新設した検査が S-14 に反映されていなかった）** — いずれも**ローカルへ書かず通知すること**（S-10 手順3）。
- **鍵ローテーションのテスト**: `key_id` が異なる 2 世代以上前の鍵で署名された receipt でも、その鍵を保持していれば検証が通ること。保持していない場合は書き込まれず通知されること（S-06b）。
- **リプレイのテスト**: `accepted_seq` を**下回る** `promotion_seq` を持つ過去の正規 receipt を再 push しても受信側で拒否されること（S-06b）。**`accepted_seq` と等しい `promotion_seq`（同一内容の再 pull）は拒否されず、`skill_sync_state.json` に書き込み直せること**（S-12 の削除復活と矛盾しないことの回帰テスト。2026-08-16 第6巡 Codex レビュー High 指摘で追加）。
- **通常更新で衝突通知が出ないこと**（`base_revision` 一致の再 promote を 10 回繰り返しても通知が 0 件であること。S-11。過剰通知は通知疲れを生み、本当の警告を見落とさせる）。
- **クライアント側アウトボックスの永続性**: 署名検証失敗を検出したが ntfy 送信に失敗した場合、次回実行時に再通知されること（S-11）。
- **`ntfy_client.py` が `modal` を import せずに動作すること**（`modal` を import 不可にした状態で `hh_skill_sync.py` の通知経路が動くテスト。S-11）。`Title`/`Tags` に非 ASCII が来たら送信前に弾くこと。
- `skill_sync_state.json` が欠損・破損している場合に、sha 不一致のスキルが**衝突として扱われる**（＝黙って上書きされない）こと。
- Modal 側 `sync_dashboard_skills` が、1 周期で例外を投げても**次の周期で再試行すること**（1 回の失敗で以後の同期が永久に止まらないこと）。
- **敵対的認証テスト（S-06b・最重要）**: receipt が無い／別の鍵で署名された／`name`・`content_sha256`・`origin_instance`・`promoted_at_ms`・`promotion_seq`・`distilled_from_session_id` のいずれかを改変した push を pull したとき、**`~/.hermes/skills/` に一切書き込まれず、ntfy 通知が出ること**。「Corpus2Skill の Bearer を持つ攻撃者」を模したテストとして書く。
- 鍵ローテーション中（`HH_AGENT_TOKEN_SIGNING_KEY_PREV` のみで検証できる receipt）でも pull が成功すること。
- **`assert_staging_root_is_safe()` が `self_heal_orphaned_promotions()` より先に呼ばれることの回帰テスト**（呼び出し順序を spy で固定する。`install_confirmed_skill()` 切り出しリファクタで壊れやすい箇所・S-10 手順1）。
- **promote と sync の競合テスト**: バリアで両者を同時に走らせ、`_write_staging()` 後・`install_staged_skill()` 前に他方が割り込んでも、配置されたバイト列＝人間が確認したバイト列＝監査 digest が一致すること。ロックが取れない側がスキップまたは明示エラーになり、**ハングしないこと**（タイムアウトを持つテストで固定する）。
- **`accepted_seq` 改ざんメタデータ攻撃のテスト**（2026-08-16 第8巡 Codex レビュー Medium 指摘で追加）: 本物の（過去に正規に検証済みの）receipt 文字列はそのままに、同梱の `origin_instance`／`promotion_seq` フィールドだけを（署名鍵を持たない攻撃者を模して）水増しした pull 応答を注入し、**`accepted_seq` が水増しされた値まで進まないこと**（自己修復・S-10 が必ず signature を再検証し、検証済みの値だけを使うことの回帰テスト）。
- **Modal 側クロスコンテナロックの実機テスト**（同 Medium 指摘。**2026-08-16 S-08b/S-08c への差し替えにより対象を更新**）: このロックが守るのは `hh-agent-dashboard-home`（`~/.hermes/skills/`・`promote_backups/`・`promote_log.jsonl`・`skill_sync_state.json`）だけであり quarantine は含まれない（S-10 手順0）。`sync_dashboard_skills` の**多重実行そのもの**を模したプロセスを同時に走らせ、`modal.Dict` の排他が実際に機能して二重 writer が発生しないことを確認する（実 Modal 必須。`Corpus2Skill/doc/03_Architecture.md` §13 S-01 の受け入れ条件と対）。
- **S-08c quarantine 消し込みのテスト**（2026-08-16 第5巡 Codex レビュー High 指摘を受けて「quarantine 本体には触れず、複製と別キーへの解決マーカー記録だけを行う」非破壊方式に全面再設計）: (1) 消し込み対象の digest が Lane C 上の検証済み版と一致しない場合、複製・解決マーカー記録のいずれも行われず次周期へ持ち越されること。(2) 消し込み後も `skills_quarantine/<name>/SKILL.md` 自体は変更・削除されず物理的に残り続けること（quarantine 本体に触れないことの回帰テスト——旧「複製→削除→予約解放」設計からの意図的な離脱を固定する）。(3) `store.get_quarantine_resolved(name)` が消し込み後に正しい `content_sha256` を返し、`GET /api/skills/quarantine` の応答からそのエントリが除外されること。(4) Distiller が同じ `name` に**異なる**内容を公開した場合（既存 `_publish_skill_core` の制約内で発生しうるケースを模す）、新しい `content_sha256` が解決マーカーと一致しないため、一覧に再出現すること。(5) install 済みだが解決マーカー記録だけ失敗した状態を模したとき、次周期でも消し込みが再試行されること。(6) Lane C 応答のトップレベル `origin_instance` を自インスタンス値に偽装し、かつ検証済み receipt が存在しない（または receipt 内の署名済み `origin_instance` が別の値）候補を注入しても、消し込みが実行されないこと。(7) `sync_dashboard_skills` の quarantine 読み取りが `store.read_file()`（symlink を辿る）ではなく S-08b と共通の安全な読み取りヘルパーを使っていること（symlink を模した quarantine エントリで実際に拒否されることを含む）。
- **`quarantine_read` スコープの認可テスト**（2026-08-16 S-08b 新設。新規 Secret は使わず既存 Agent Bearer + scopes を再利用する設計に伴う回帰テスト）: `scopes` に `quarantine_read` を含まないトークンで `GET /api/skills/quarantine` を叩くと 403 になること。正しいスコープのトークンでも応答に `HH_AGENT_TOKEN_SIGNING_KEY`／`C2S_SKILL_WRITE_KEY` のいずれも含まれないこと（C-3 の実装レベルでの回帰テスト）。
- **`GET /api/skills/quarantine` の安全な読み取りテスト**（2026-08-16 第2〜4巡 Codex レビュー Medium 指摘で追加）: quarantine の `<name>` ディレクトリ自体が symlink に差し替えられているケース・`SKILL.md` 自体が symlink のケース・**`meta.json` 自体が symlink のケース**の全てで、`dir_fd` 相対の `O_NOFOLLOW` オープンにより読み取りが拒否されること（最終ファイルだけでなく祖先ディレクトリの差し替え・`meta.json` の漏れも防ぐことの回帰テスト）。64KB を超える本文が全量読み込みではなく `fstat` サイズ判定＋bounded read の時点で弾かれること。`meta.json` が欠損・破損・symlink のいずれでも `origin_instance: null` を返し例外にならないこと。201 件目以降が一覧に含まれないこと。
- **`meta.json` 書き込み分岐の固定テスト**（2026-08-16 第4巡 Codex レビュー Medium 指摘で追加）: 「予約獲得後の新規書き込み」分岐でのみ `meta.json` が書かれること。同一内容の冪等な再送・自己修復分岐・409 の各分岐では `meta.json` が新規作成も上書きもされないこと（最初の申告だけが残り続けることの回帰テスト）。
- **quarantine メタデータの `origin_instance` を署名対象に無検証で使わないことの回帰テスト**（2026-08-16 S-08b 新設）: `GET /api/skills/quarantine` の応答に含まれる自己申告 `origin_instance` を、接続先設定（`remote_sources.json`）の固定値と異なる値に細工しても、`run_remote_promote()` が中断し署名・push が行われないこと（応答の自己申告フィールドを署名対象へ無検証で転記しないことの回帰テスト）。
- **`promote_seq.json` の origin 別スキーマ回帰テスト**（2026-08-16 S-08b 新設）: 同一 `name` に対して 2 つの異なる `origin_instance` で `allocate_promotion_seq()` を呼んでも、互いの seq が独立して単調増加し、干渉しないこと。
- **`promote_seq.json` 旧スキーマ移行の直列化テスト**（2026-08-16 第2・3巡 Codex レビュー Medium 指摘で追加）: 旧スキーマ（`{name: seq}`）が残っている状態で `run_promote()` と `run_remote_promote()` をバリアでほぼ同時に起動しても、`promote_lock` の外で移行が先読みされず、移行後の値が両呼び出しの採番を反映して単調増加していること。`--repair-seq` も同じ `promote_lock` を取得してから書き込むこと（取得せずに書き込む実装がないことの回帰テスト）。
- **CAS の 409 テスト**: 古い `base_revision` を添えた push が書き込まれずに 409 になること（ダウングレード試行）。
- **衝突イベントの durable 性テスト**: **サーバー側がイベントを記録する事象（CAS 409・形式不正・`promotion_seq` 逆行・上限超過のいずれか）を発生させた push**でレスポンスを取りこぼした（例外を注入）後の次回 `list` で未 ACK イベントが返り、通知が出て ACK されること。通知失敗時は ACK されず次回再通知されること。**このテストを通常の CAS 成功（`base_revision` 一致）に対して実装してはならない**（2026-08-16 第7巡 Codex レビュー Low 指摘で明記: 通常成功はイベントを一切生成しないため、そのケースにこの durable 性テストを適用すると存在しないイベントを待つテストになり成立しない。通常成功の応答喪失からの回復は S-08「応答が失われた場合」が定める、次回 `list()` の `local.sha == remote.sha` 分類による自然な収束であり、通知やイベントには依存しない）。
- **緊急停止スイッチと denylist のテスト**: スイッチ有効時に pull も push も一切行われないこと。denylist の name／digest が pull されないこと（S-12）。
- 上記すべてが**実 Corpus2Skill へ接続せず**に回ること（fake クライアント注入・fake notifier 注入）。
- **ただし「複数コンテナからの同時 push が直列化されること」だけは fake では検証できない**（2026-08-16 第2巡 Codex レビュー Medium 指摘）。これは実 Modal 上での統合テストとして別枠で必須にする（`Corpus2Skill/doc/03_Architecture.md` §13 S-01 の排他機構の受け入れ条件）。ローカルの単体テストで代用したことにしない。

- **秘密配置の受け入れテスト（S-06c）**: `dashboard_server` の環境に `HH_AGENT_TOKEN_SIGNING_KEY` と `C2S_SKILL_WRITE_KEY` が**存在しないこと**を、デプロイ構成（`modal_dashboard/app.py` の `secrets=[...]`）に対する静的テストで固定する。C-3 を将来のリファクタで踏み抜かないための回帰テスト。
- **Modal 発 quarantine のリモート確認・署名 運用受け入れ確認（手動・自動テスト化しない。2026-08-16 第3巡 S-08b/S-08c への差し替えにより対象を更新）**: S-08b が定める「Windows 側から Hub の `GET /api/skills/quarantine` を叩き、既存のローカル TTY 確認・署名フローで promote する」経路は、**実際に人間が一度手を動かして** (1) Windows から `quarantine_read_token.json` で新設エンドポイントを叩いて Modal 側 quarantine 一覧（内容・digest）が正しく取得できること、(2) `hh_skill_promote.py --remote` が正常に全文表示・確認・署名・push まで到達し、push ペイロードの `origin_instance` が接続先設定（`remote_sources.json`）の固定値と一致していること、(3) 次回 `sync_dashboard_skills` 実行時に Hub 側 quarantine エントリが `skills_quarantine_promoted/` へ自動的に消し込まれ、かつ同じ name へ新しい内容で再度 publish できること（予約解放の確認・S-08c）、を確認する。これは自動テストで代替できない（人間の TTY 操作そのものが検証対象のため）。実施したことと結果を `promote_log.jsonl` に残る `provenance` が `remote-promote:<origin_instance>` になっている記録で確認できることをもって完了とする。**Critical/High をゼロにするための必須条件ではない**（実装完了後、初めて Modal 発スキルをリモート確認・署名する際に一度確認すればよい運用確認であり、設計の妥当性そのものを左右しない）。
- **鍵の許可／拒否マトリクス**（2026-08-16 第4巡 Codex レビュー Medium 指摘で訂正: 旧文は「書き込み鍵で list/pull ができること」としていたが、これは `Corpus2Skill/doc/03_Architecture.md` §13 S-04 が定める「読み取りは既存 Bearer のみ、書き込みは `C2S_SKILL_WRITE_KEY` のみ」という**互いに交換不可の 2 資格情報**設計と矛盾していた）: **読み取り鍵で push／events-ack を試みると 401/403（書き込みへ昇格できない）、書き込み鍵で list／pull を試みても 401/403（読み取りへは流用できない）**、いずれの鍵も未設定なら押さずに警告終了すること。
- **`--resign` の安全契約**: 旧鍵の有効な receipt が無い／内容が一致しない対象を再署名しようとすると**拒否されること**（署名オラクル化の防止・S-06b）。
- **`promotion_seq` の異常系**: `promote_seq.json` の欠損・巻き戻り、偽の巨大 seq を受け取った場合、いずれも**自動で振り直さずフェイルクローズし、人間の `--repair-seq` を促すこと**。巨大 seq で正規クライアントが恒久ロックアウトされないこと。
- **ロックの異常系**: heartbeat が生きているロックを奪取しないこと、旧所有者が復帰しても他人のロックを解放しないこと、ロック取得後の再 assert・再ダイジェスト検査が実行されること（S-10 手順0・疑似コード）。
- **イベントのページング**: 51 件目以降が `next_cursor` で取得できること、501 種類目で集約レコードが作られ**黙って捨てられないこと**、全ページ読了前に ACK しないこと（S-11）。

### 新規・変更ファイル

| ファイル | 区分 | 内容 |
|---|---|---|
| `modal_hub/services/skill_sync.py` | 新規 | Lane C の HTTP クライアント（**push / list / pull / events-ack のみ。削除系の関数は作らない**・S-12）・差分判定・受信側検証・promote receipt の生成と検証（S-06b）。`memory_bridge.py` は変更しない。 |
| `scripts/hh_skill_sync.py` | 新規 | pull / reconcile / dry-run の CLI（`--forget` は v1 に入れない・S-12）。`~/.hh-agent/skill_sync_state.json` の管理もここ。 |
| `modal_hub/services/notifier.py` | 変更（リファクタ） | 送信部を `ntfy_client.py` へ委譲する。**既存 `send_approval_request()` の外部挙動は変えない**（リトライ・バックオフ・store への状態記録すべて維持）。 |
| `scripts/register_skill_sync_task.ps1` | 新規 | `HH-Agent-SkillSync`（12h）。`register_token_refresh_task.ps1` と同型。 |
| `scripts/hh_skill_promote.py` | 変更 | `install_confirmed_skill()` の切り出し（純粋なリファクタ）、`append_promote_log()` が record を返す、`run_promote()` 末尾に push 1呼び出し。**加えて `--remote <source>` モード（S-08b・`run_remote_promote()`）を新設**——確認・署名は既存関数（`display_for_confirmation()`・`confirm_or_abort()`・`install_confirmed_skill()`・`write_receipt()`・`push_to_lane_c()`）を再利用し、新しい確認・署名ロジックは増やさない。`allocate_promotion_seq()`・`write_receipt()`・`push_to_lane_c()` は `origin_instance` を明示引数に取るようシグネチャを拡張する。**安全チェック（TTY 確認・symlink 拒否・原子的書き込み等）は1つも触らない。** |
| `modal_dashboard/app.py` | 変更 | **`hh-agent-secret` を持つ新 Function `sync_dashboard_skills`（`modal.Period(hours=8)`・`max_containers=1`）の追加**（S-06b）。**加えて、S-08c の quarantine 消し込みのため、この Function に Hub の Volume `hh-agent-store`（`modal.Volume.from_name("hh-agent-store")`）を追加マウントし、`modal_hub.core.store` の既存ヘルパーを in-process import して使う**（`refresh_dashboard_agent_token` が `modal_hub.core.store` を import している既存前例と同型・2026-08-16 第3巡 Codex レビューで訂正）。`dashboard_server` には一切手を入れない（C-3 は変更なし）。 |
| `modal_hub/routers/skills.py` | 変更 | **既存 `POST /api/skills/publish`（`_publish_skill_core`）に、リクエストボディの任意項目 `origin_instance` を受け取り `skills_quarantine/<name>/meta.json` へ `write_json()` で書く処理を追加**（「予約獲得後の新規書き込み」分岐だけで書く。既存の `put_if_absent` 排他・イベント記録・レスポンス形式は一切変更しない）。**新設 `GET /api/skills/quarantine`（S-08b）を追加**——`SCOPE_QUARANTINE_READ` を要求し、quarantine 一覧を返す（安全な読み取り実装は S-08b 参照）。 |
| `modal_hub/core/security.py` | 変更（最小） | 新規スコープ定数 `SCOPE_QUARANTINE_READ = "quarantine_read"` の追加のみ（既存 `SCOPE_PUBLISH` と同型）。新しい Secret・新しい認証方式は追加しない。 |
| `modal_hub/core/store.py` | 変更（最小） | **新規ヘルパー `mark_quarantine_resolved(name, content_sha256)` / `get_quarantine_resolved(name) -> str \| None`**（`modal.Dict` の新しいキー空間 `quarantine_resolved:<name>` への単純な読み書き。書き手は `sync_dashboard_skills` のみなので `put_if_absent` は使わない無条件上書き。S-08c 参照）。**加えて S-08b／S-08c 双方が使う共通の安全な読み取りヘルパー**（`dir_fd` 相対の `O_NOFOLLOW`・`fstat`・bounded read を quarantine の `<name>` 配下〈`SKILL.md`・`meta.json`〉に適用する関数）をここに追加し、`routers/skills.py` の新設エンドポイントと `modal_dashboard/app.py` の `sync_dashboard_skills` の両方から呼ぶ（実装を複製しない。2026-08-16 第5巡 Codex レビュー Medium 指摘への対応）。既存ヘルパーの挙動は一切変更しない。 |
| `modal_hub/tests/test_skills_router.py` | 変更 | 既存 `_publish_skill_core` のテストスイート。**`origin_instance` sidecar の書き込み分岐・`GET /api/skills/quarantine`・`quarantine_read` スコープ・symlink／上限テスト・消し込み済みエントリの除外テストを追加**。 |
| `modal_hub/tests/test_store.py` | 変更 | **`mark_quarantine_resolved`／`get_quarantine_resolved`・共通の安全な読み取りヘルパーの単体テストを追加**（2026-08-16 第5巡 Codex レビュー Low 指摘: 既存 Volume I/O テストの置き場所であり、新設ヘルパーもここに追加するのが自然）。 |
| `~/.hh-agent/remote_sources.json` | 新規（設定ファイル・コードではない） | `--remote <source>` が参照する接続先設定（Hub URL・`quarantine_read_token.json` のパス・固定 `origin_instance`）（S-08b）。ファイル ACL は現ユーザーのみに絞る。 |
| `modal_hub/services/ntfy_client.py` | 新規 | `notifier.py` から切り出す store 非依存の ntfy 送信部（S-11）。 |
| `scripts/hh_issue_agent_token.py` | 変更 | `.hh-secret.env`（`NTFY_TOPIC`/`NTFY_TOKEN` 等）読み取りヘルパーの共通化（`hh_skill_sync.py` が再利用する・S-11）。**加えて、既存の `agent_token.json`／`distill_token.json` と同じ発行パターンで `quarantine_read_token.json`（`scopes=["quarantine_read"]`）を発行する処理を追加**（S-08b。新規 Secret は使わず、Windows 常駐の `HH_AGENT_TOKEN_SIGNING_KEY` で自己署名する既存の仕組みをそのまま再利用）。**`.hh-signing.env`（署名鍵・書き込み鍵。S-06c）は別ファイル・別ヘルパーで読む** — ACL を現ユーザーのみに絞った専用ファイルを一般の secret ローダーと混在させない。既存のトークン発行ロジック（agent_token/distill_token）は変更しない。 |
| `modal_hub/tests/test_skill_sync.py` | 新規 | S-14。 |
| `Corpus2Skill/services/modal/{app.py,mcp_server.py,models.py,compiler.py,config.py}` | 変更 | §13 側の担当。`compiler.py` は `_RESERVED_TOP_LEVEL_NAMES` への追加のみ、`config.py` は `JOURNAL_DIR`／`SKILLS_SHARED_DIR` 定数の集約（詳細は Corpus2Skill 設計書 S-02）。 |

**並列委任のための依存順序（実装フェーズ引き継ぎメモ・2026-08-16）**: 上表はファイル単位で担当を分けられる粒度になっているが、無条件に並列化できるわけではない。`skill_sync.py`・`ntfy_client.py` の2つは他の複数ファイルから import される**土台ライブラリ**であり、この2つの公開関数シグネチャ（`push`/`list`/`pull`/`events_ack`・受信側検証関数・`send_skill_conflict()` 等）が先に確定していないと、それに依存する側は書き始められない。したがって実装フェーズは:

1. **第1弾（先行・土台）**: `modal_hub/services/skill_sync.py`・`modal_hub/services/ntfy_client.py` の2ファイル。ここは公開インターフェースを決める作業なので分割せず1担当（メイン＝DeepSeek想定）で先に着手する。
2. **第2弾（並列可）**: 第1弾のインターフェースが決まった後、以下は互いにファイルが重ならず並列委任できる:
   - `scripts/hh_skill_sync.py`（`skill_sync.py`・`ntfy_client.py` に依存するが、他の第2弾ファイルには依存しない）
   - `scripts/hh_skill_promote.py` の変更（`run_promote()` の最小変更・`--remote` モード新設）＋`modal_dashboard/app.py` の `sync_dashboard_skills` 追加・拡張（この2つは同じ「promote/pull 実行経路」なので同一担当が望ましい。`hh_skill_promote.py` 側は安全チェックに触れないため差分は小さい）
   - `modal_hub/routers/skills.py`（`origin_instance` sidecar 追加・`GET /api/skills/quarantine` 新設）＋`modal_hub/core/security.py`（`SCOPE_QUARANTINE_READ` 追加）（S-08b。既存 `_publish_skill_core` を担当するため、この2ファイルは同一担当が望ましい）
   - `modal_hub/services/notifier.py` のリファクタ（`ntfy_client.py` への委譲のみ。外部挙動不変がテストで固定されるため他ファイルとの衝突リスクが低い）
   - `scripts/register_skill_sync_task.ps1`・`scripts/hh_issue_agent_token.py` の変更（`quarantine_read_token.json` 発行の追加を含む。どちらも他ファイルと独立）
3. **`modal_hub/tests/test_skill_sync.py`（S-14）は第1弾・第2弾のどちらか一方に寄せず、各担当が自分の変更に対応するテストケースをその場で追加する**（テストファイル単位で担当を分けると「無いことを固定するテスト」の抜け漏れ対象がぼやけるため）。最終的な一本化・重複排除はレビュー担当（Codex）が行う。
4. Corpus2Skill 側（`app.py`・`mcp_server.py`・`models.py`・`compiler.py`・`config.py`）は別リポジトリであり、上記のいずれとも並行して着手できる。ただし `revision`・`receipt`・`promotion_seq` のフィールド名・型は本表の H-H-Agent 側ペイロード定義（S-08）と一致させること。

### 未確定事項 → **すべて解消済み**（ユーザー回答・2026-08-16）

| # | 内容 | 確定した回答 | 反映先 |
|---|---|---|---|
| G | Modal 側 pull を起動時1回に留めるか、常駐の定期チェックまで入れるか | **常駐（定期チェック）まで入れる。** ただし実装形態は「ASGI プロセス内デーモンスレッド」ではなく**別 Function の `modal.Period(hours=8)`** に変更した — 前者は C-3（エージェントが動くコンテナに署名鍵を置かない）と両立しないことが第2巡レビューで判明したため（S-06b）。**定期実行するというユーザー確定の趣旨は満たしている** | S-06b・S-10 実行タイミング表 |
| H | 衝突（上書き発生）時に ntfy 通知を出すか | **出す。** 既存 `notifier.py`（Phase 1a 承認通知と同じ ntfy 経路）を流用。黙って上書きする挙動は不可 | S-11・S-14・新規/変更ファイル表 |
| I | `promoted_at`（クライアント申告）とサーバー受信時刻のどちらを順序の権威にするか | **「クライアント時刻を使わない」というユーザー判断を採用**。ただし 2026-08-16 Codex レビューで「サーバー時刻も逆行・同値化しうる」と指摘されたため、**サーバーが採番する単調増加 `revision` + CAS** という、同じ趣旨をより強く満たす形に精緻化した（`received_at` は監査表示専用）。両設計書で同一の結論に揃えている | S-08・S-10 手順3・S-11・Corpus2Skill §13 S-01/S-04 |
| J | Lane C の保持上限・履歴世代数 | **警告閾値 2,000 件＋ハード上限 10,000 件／1GB の 2 段構成**（200 件は隔離領域＝抽出候補の暴走防止バルブから目的違いの数字を借用していただけと判明したため差し替え）。**警告閾値**（2,000 件）到達時は push を拒否せず警告ログ＋イベント記録のみで受け入れ続ける。**ハード上限**（10,000 件／1GB。2026-08-16 第3巡 Codex レビュー High 指摘で新設）超過分は保存前に 400 で拒否する。履歴は 10 世代を維持 | Corpus2Skill §13 S-04 |
| K | `HH-Agent-SkillSync` の実行間隔 | **12h を維持**（`HH-Agent-TokenRefresh` と同間隔で運用上わかりやすい） | S-10 実行タイミング表 |

**J の補足（数字の根拠を借用しない）**: §4.7 の「隔離領域 200 スキル上限」は、**LLM が無制限にスキル候補を生成し続けるのを止める安全弁**である。Lane C に流れるのは人間が 1 件ずつ確認して昇格させたものだけで、生成速度の上限は人間の確認速度そのものであり、暴走の性質がまったく違う。SKILL.md は数 KB でストレージ圧迫も実質ゼロ。したがって**警告閾値**は「運用を止める閾値」ではなく「明らかにおかしいことが起きたと気づくための閾値」として 2,000 件に置き、到達しても push は受け入れ続け、警告ログだけを出す。同じ数字を目的の違う場所へ機械的に流用しない。**ただし「実質無制限」を文字どおり無制限にすると、書き込み鍵が漏れた場合に共有 Volume を枯渇させ Lane A/B まで巻き添えにする**（2026-08-16 第3巡 Codex レビュー High 指摘）。そのため正常運用では絶対に到達しない値（10,000 件／1GB）にハード上限を別途置き、こちらだけは超過分を拒否する最終防壁とした。両者の役割は異なる: 警告閾値＝気づくため、ハード上限＝止めるため。

### 第2巡レビューで新たに生じた未確定事項

| # | 内容 | ブロックする範囲 |
|---|---|---|
| N | 署名鍵ローテーション時の receipt 扱い: (a) 旧鍵を検証専用として保持し続ける / (b) `--resign` で全 receipt を再署名してから旧鍵を捨てる（S-06b） | ローテーション運用手順のみ。`key_id` を receipt に含める実装は両案共通なので先行できる |
| **O** | ⚠️ **残存リスク R-1（Windows は同一ユーザー権限のため、侵害されたエージェントが receipt を偽造して Modal 側へ横展開できる）を受け入れるか**（S-06b）。選択肢: (1) 受け入れて v1 を進める（緩和のみ実施）／(2) Windows 発のスキルに限り受信側の人間確認を必須にする（＝「確認なしで自動反映」の部分的撤回）／(3) 署名サービスを作るまで Lane C 全体を保留 | **解消済み（ユーザー回答・2026-08-16）: (1) を採用。リスクを受け入れて v1 を進める。** 判断理由（林さん）: 個人利用・低頻度運用であり、Windows 機自体が乗っ取られるリスクは他の攻撃経路と比べて特別高くはないという判断。緩和策（S-06b の署名・promotion_seq によるリプレイ対策等）はそのまま維持する |

**実装後に実機で確認する項目**（設計をブロックしない）: (1) `sync_dashboard_skills` が書いた内容が、ダッシュボードコンテナのコールドスタート後に実際にスキル一覧へ現れるか、(2) 複数コンテナからの同時 push が排他機構で直列化されるか（実 Modal 必須・S-14）、(3) 衝突時の ntfy 通知が実機で届くか。

---

### 14.1 2026-08-16 デルタ: Modal 発 quarantine の promote 方式差し替え（`modal shell` 運用の却下と S-08b/S-08c への確定）

本デルタ（S-08b・S-08c の新設、および S-08 冒頭の非対称性の記述・S-10 手順0 の Modal 側ロックの記述・S-13(g)・S-14 の該当テスト・「新規・変更ファイル」表の更新）は、当初案（人間が `modal shell` で対話的に promote する運用）をユーザーが明示的に却下した（2026-08-16「１の運用変更は了承しない」）ことを受けた差し替えである。**このデルタは Codex レビュー計9巡で確定（2026-08-16）。**（本節の以前の版は「2巡で確定」としていたが、これは初期の楽観的な集計であり誤り——実際には第3巡でアーキテクチャ上の誤り〈quarantine の実体は `dashboard_server` ではなく Hub 所有の別 Volume 上にある〉が発覚して設計を訂正、第5巡で消し込み機構の TOCTOU が発覚して非破壊方式へ全面再設計、第6巡で watermark 矛盾チェックが必要と判明、第7巡で receipt 検証の高速パスが署名なしフィールドを信用してしまう抜け穴が見つかる、といった実質的な指摘が9巡目まで続いた。各指摘への対応は S-08b・S-08c・S-10 手順0 の本文中に「2026-08-16 第N巡 Codex レビュー」として個別に記録されている。9巡目で Critical/High 0 件に到達し、それ以降の指摘はない）。レビュー対象はこのデルタ（S-08b・S-08c と、それに伴う S-08 冒頭・S-10 手順0・S-13(g)・S-14・新規/変更ファイル表の更新箇所）に限定し、既存の §14 全体（S-01〜S-14 の他の部分）は同日の別の 9 巡レビューで確定済みのまま変更していない（両者はたまたま同じ巡数になったが、別系統のレビューである）。**このデルタの追加によって §14 冒頭の「CLEARED FOR IMPLEMENTATION」マーカーを取り下げる必要はない**（新規追加分のみを対象にレビューし、Critical/High 0 件で確定したため）。
