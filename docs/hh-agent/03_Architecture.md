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

**Phase 1c。着手前に PoC で 2 つの前提を潰す。**

- **実行体**: Hermes CLI 本体。Modal Image に本リポジトリを焼き込む。
- **UI**: **`web/` の静的 dist だけを配信する方式は成立しない**（Codex 検証済み）。`web/src/pages/ChatPage.tsx` の主チャットは JSON-RPC 直結ではなく `/api/pty` で `hermes --tui` を起動する xterm 画面であり、SPA は REST API・WS チケット・`/api/ws`・`/api/pub`・`/api/events` にも依存する。さらに `tui_gateway/server.py` のセッションは**プロセス内 dict** で保持される。
  → **Hermes 自身の `dashboard` をそのまま丸ごとホストする。** その前段に H-H Agent の認証を置く。
  **`serve` は使えない**（D-18）。`serve` は明示的に headless で「no UI build, no SPA mount」（`hermes_cli/main.py:10277`）。ブラウザ UI が必要なら `dashboard`、または「別途ビルドした SPA ＋ `serve`」の 2 択であり、`serve` 単体でブラウザ UI は出ない。
- **セッションアフィニティ必須**: セッション状態がプロセス内にあるため、**1 セッション = 1 コンテナに固定する**。
  **`min_containers` はセッションアフィニティではない**（warm コンテナ数を指定するだけ）。v2 の「`min_containers` とコンテナ ID ベースのルーティング」という記述は Modal の実 API に対応する具体策が無く、成立しない。
  → **1 セッション = 1 Modal Sandbox** 方式を採る。この方式は次をすべて設計しないと動かないため、Phase 1c の設計項目として明示する:
  1. Sandbox の生成とライフサイクル（起動・停止・期限切れ）
  2. Sandbox への URL / WebSocket のプロキシ
  3. 切断からの再接続（Sandbox が生きている間の復帰）
  4. Sandbox 単位の認証（他人のセッションに繋がらないこと）
  5. スケールダウン／クラッシュ時の扱い
- **承認の合流**: `%HERMES_HOME%\config.yaml` の `pre_tool_call` フック（§4.4）を有効にして起動する。**`set_approval_callback()` は使わない**（スレッドローカルであり、実行ワーカースレッドでは `None` になる。またコールバックには redact 済みコマンドしか渡らず、cwd・差分・session_id を受け取れない）。
- **D-14 の再掲（最重要）**: Hermes を `env_type="modal"` で起動してはならない。承認ガードが丸ごとスキップされる。

**PoC の合否条件**:

1. Hermes の依存関係一式を載せた Modal Image が **5GB / ビルド 10 分**以内に収まるか。
2. `serve` バックエンドが Modal の ASGI 上で起動し、`/api/pty` が機能するか。

**どちらか失敗したら Phase 1c を Phase 2 へ送り、Phase 1a/1b だけで一度完成させる。**

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

---

## 12. 実装担当の分割方針

**タスク単位ではなくファイル単位で所有者を決める**（並列実装時の衝突ゼロを実測済みの方式）。詳細は `docs/hh-agent/04_Task_Allocation.md`。

| 担当 | 比率 | 担当領域の性格 |
|---|---|---|
| **Claude Code Sonnet 5** | 30% | セキュリティ・状態機械・既存 Hermes との接続部（`core/security.py`, `core/risk.py`, `routers/approval_gate.py`, `services/audit.py`, `hh_hooks/*`）＝ 間違うと安全性が壊れる箇所 |
| **MiniMax M3** | 70% | UI・定型実装・テスト（`mobile_app/pwa_approval/*`, `services/skill_distiller.py`, `services/notifier.py`, `services/memory_bridge.py`, `core/config.py`, `core/store.py`, `modal_hub/tests/*`） |
| **Codex** | — | 全コードのレビュー（`codex exec review --uncommitted`）＋ 設計書レビュー ＋ GitHub push ＋ Modal Secret 作成 |
