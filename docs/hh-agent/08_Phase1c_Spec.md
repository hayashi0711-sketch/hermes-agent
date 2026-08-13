# H-H Agent Phase 1c 詳細仕様（実装契約）

- **最終更新**: 2026-08-13（同日、Codex全体レビューでC-1/C-2/C-3を検出→§2.2/§2.3をアーキテクチャごと訂正）
- **親設計書**: `docs/hh-agent/03_Architecture.md`（食い違う場合は親設計書が優先。本書は §4.6 の PoC 合否条件を満たした後の実装契約として、§4.6 の一部を本書の内容で更新する。詳細は「§0 親設計書からの変更点」参照）
- **位置づけ**: 親設計書 §4.6「Phase 1c 着手前に必須」の PoC①②を実機で通過させたうえで、本実装に必要な設計判断（HERMES_HOME 永続化・認証・承認ゲート統合・Node 版数・リポジトリ構成）を確定させたもの。**ここに書かれていることは実装者が変更してよい判断ではない。** 不足を見つけたら BLOCKED として報告すること。

---

## 0. 親設計書（03_Architecture.md §4.6）からの変更点

PoC は 2026-08-13 に実機で実施し、両方とも通過した:

1. **PoC①（サイズ/ビルド時間）**: `/opt/hermes` 実測 632MB、Modal 側イメージビルド時間 75.93 秒（予算: 5GB / 10 分）
2. **PoC②（`dashboard` バックエンドが Modal ASGI 上で起動し `/api/pty` が機能するか）**: `hermes_cli.web_server:app` を Starlette `TestClient` でインプロセス検証。`GET /` が 200、`/api/pty` の WebSocket 接続で実際に `hermes --tui`（Node/ui-tui バンドル）が起動し、本物の ANSI 端末初期化シーケンスを受信した

この結果を受けて、親設計書 §4.6 の以下の記述を**本書の内容で置き換える**（`03_Architecture.md` 側もあわせて改訂する）:

- **「1 セッション = 1 Modal Sandbox」方式を撤回**し、**`max_containers=1` による単一コンテナ固定**に置き換える（§2.2）。理由: セッション状態がプロセス内 dict で保持される制約自体は親設計書の指摘どおり正しいが、それが問題になるのは「複数コンテナ間でセッションを正しくルーティングする必要がある」場合に限る。本プロジェクトは個人単一ユーザー運用で同時アクセスは実質 1 系統のため、**コンテナを常に 1 個に固定すれば、そもそも複数コンテナ間のルーティング問題自体が発生しない**。Sandbox 生成・URL/WS プロキシ・再接続・Sandbox 単位認証・スケールダウン復旧という 5 項目の設計を丸ごと不要にできる。
- PoC 合否条件の記述に残っていた `serve` バックエンドという表現は誤り（親設計書内で「`serve` は使えない、`dashboard` を丸ごとホストする」という決定と矛盾していた）。本書では一貫して `dashboard`（`hermes_cli.web_server:app`）を対象とする。
- 親設計書が要求する「承認の合流」（`pre_tool_call` フック必須・`env_type="modal"` 禁止・D-14「最重要」）は**そのまま維持し、本書 §3 で具体化する**。変更なし。

## 0.1 実装後のアーキテクチャ訂正（2026-08-13・同日、実デプロイ＋Codex全体レビューで発覚）

5タスク実装・タスク別レビュー通過・実デプロイまで完了した直後、**Codexの全体横断レビューでCritical指摘3件（C-1〜C-3）が見つかり、§2.2「アーキテクチャ」・§2.3「ダッシュボードアクセス認証」の設計そのものが誤っていたと判明した。**

**誤っていた前提**: `hermes_cli.web_server:app` を `@modal.asgi_app()` でそのまま返せば `hermes dashboard` と等価に動くと想定していたが、実際には `hermes_cli.web_server.start_server()` が行っている以下の配線が**まるごとスキップされる**ことが分かった:

- `app.state.bound_host`/`app.state.bound_port` の設定 → `/api/pty` がこの値を使って「同一プロセス内のゲートウェイ」に接続する URL を組み立てる。値が無いと、Node 製 TUI は**別プロセス**（`python -m tui_gateway.entry`）を新規起動する。**このプロセスは `agent.shell_hooks.register_from_config()` を一度も呼ばず、承認ゲートのフックが 0 件のまま危険コマンドを実行できてしまう**（C-1。ASGI プロセス側の起動時チェックは通っていたのに、実際にツールを実行する場所には承認ゲートが無かった）
- `app.state.auth_required` の設定 → 未設定だとループバック向けの既定動作のままとなり、SPA が未認証の `GET /` にセッショントークンを埋め込んで配信してしまう（C-2）

**訂正した設計**: `hermes_cli.web_server.start_server()` の配線を自前で再現するのではなく、**実際に `hermes dashboard` CLI をサブプロセスとして起動する**方式に変更した。`@modal.asgi_app()` → `@modal.web_server(port=8000)` に切り替え、コンテナ内で `python /opt/hermes/hh_hermes.py dashboard --host 0.0.0.0 --port 8000 --no-open --skip-build` を起動する。

- `hh_hermes.py`（Phase1a・C5 で新設済みの起動ラッパー）を経由するため、**承認ゲートのフック検証（D-14）が、実際にツールを実行するプロセスの中で行われる**（C-1 の根本修正）
- `--host 0.0.0.0` を渡すことで、Hermes 自身の `should_require_auth()` がこれを「公開バインド」と正しく認識し、`app.state.auth_required=True` を自動設定。**認証プロバイダ未登録なら Hermes 自身が起動を拒否する**（フェイルクローズ）。プロバイダは Hermes 標準搭載の `basic`（ユーザー名・パスワード）を `HERMES_DASHBOARD_BASIC_AUTH_USERNAME`/`_PASSWORD`（Modal Secret 経由の環境変数）で有効化する。**独自の固定トークン方式（`HERMES_DASHBOARD_SESSION_TOKEN`・`?token=`URL）は廃止した**（C-2 の根本修正。旧方式はそもそも `start_server()` を経由しないと機能していなかった）
- `hh-agent-secret`（Hub 署名鍵 `HH_AGENT_TOKEN_SIGNING_KEY` を含む）は、ダッシュボードを配信し信頼できないエージェントコマンドを実行するこの関数には**一切アタッチしない**。トークンの初回シーディングは、署名鍵を持つ `refresh_dashboard_agent_token` 関数を `.remote()` で呼び出す方式に変更した（C-3 の根本修正）

§2.2 のコード例・§2.3 の認証方式の記述は、以下の内容で読み替えること（本文はもう更新しない。実装済みコード `modal_dashboard/app.py` が正）。実機で C-1（ログ `[HH-AGENT] startup guard: ... OK.` が実行プロセス内で出力されることを確認）・C-2（未認証 `GET /` → `302 /login`、ログイン後のみ `200`）とも動作確認済み。

---

## 1. スコープ

**対象**: Modal 上でクラウド Hermes（`hermes_cli.web_server:app` = `hermes dashboard` バックエンド）を、林さんが個人で使うブラウザ/スマホからアクセスできる形で稼働させる。

**非スコープ（明示的に対象外）**:
- マルチユーザー対応
- 音声（Phase 2 で別途検討、Hermes 標準機能を有効化するだけで足りる設計は確定済み）
- ローカル Hermes とのスキル同期（HERMES_HOME は完全独立。§2.1）
- 監視・アラート
- GPU・Modal 上でのローカル LLM ホスティング（LLM 推論は Anthropic API を使う。GPU 不要）
- H-H-Agent PWA（信号機型承認画面）とのデザイン統合。承認自体は Phase 1a と同じ PWA 経由で行うが、Phase1c 専用の UI 変更は行わない

---

## 2. アーキテクチャ

### 2.1 Modal リソース

既存の `hh-agent-hub`（Phase1a/1b の承認ゲートアドオン）とは**別の新規 Modal App** `hh-agent-dashboard` を新設する。

| リソース | 名前 | 内容 |
|---|---|---|
| Modal App | `hh-agent-dashboard` | 新規。`hh-agent-hub` とは別 App |
| Image | `modal_dashboard/Dockerfile` を `modal.Image.from_dockerfile()` で読む | §4 参照 |
| Volume | `hh-agent-dashboard-home`（新規） | `HERMES_HOME` を丸ごとマウント。既存 `hh-agent-store`（承認ゲートの Dict/監査ログ用）とは完全に分離 |
| Secret | `hh-agent-dashboard-secret`（新規） | `HERMES_DASHBOARD_SESSION_TOKEN`（固定値、§2.3）・`ANTHROPIC_API_KEY`・`HH_AGENT_HUB_URL` |

**HERMES_HOME は完全に独立したインスタンス**として扱う（ローカル PC の本番 Hermes・その `%HERMES_HOME%` とは一切同期しない）。初回起動時は空の Volume から `config.yaml` 等をゼロから生成する（§3.2 のブートストラップ処理）。

### 2.2 コンテナ同時実行数: `max_containers=1`

> **⚠️ このコード例は 2026-08-13 のCodexレビューで置き換え済み（§0.1参照）。** `@modal.asgi_app()` で `web_server.app` を直接返す方式は、`start_server()` の配線（`bound_host`/`bound_port`/`auth_required`）を丸ごとスキップし C-1・C-2 の原因になった。実装済みの正しいコードは `modal_dashboard/app.py`（`@modal.web_server(port=8000)` + `hh_hermes.py dashboard --host 0.0.0.0` サブプロセス方式）を参照。`max_containers=1` の必要性・理由は変更なし。

```python
# 旧コード例（訂正済み・参考用。実装は modal_dashboard/app.py 参照）
@app.function(
    image=image,
    volumes={"/opt/data": modal.Volume.from_name("hh-agent-dashboard-home", create_if_missing=True)},
    secrets=[modal.Secret.from_name("hh-agent-dashboard-secret")],
    min_containers=0,       # スケールtoゼロ。コスト試算済みでほぼ$0
    max_containers=1,       # ★必須。複数コンテナ同時Volume書き込みでの
                            #   データ消失（既知の落とし穴）と、Hermes自体の
                            #   単一プロセス前提（PID管理・SQLite WAL・
                            #   多重起動検出）の両方を構造的に回避する
    scaledown_window=300,
)
@modal.asgi_app()
def fastapi_app():
    from hermes_cli import web_server
    return web_server.app
```

**`USERPROFILE=/opt/data` を Dockerfile / Modal Function の環境変数として明示的に設定する**（2026-08-13 実デプロイで訂正。当初は `HOME=/opt/data` を指定する設計だったが、実際にデプロイしたところ全コンテナが `cannot mount volume on non-empty path: "/opt/data"` でクラッシュループした。原因は `HOME=/opt/data` を設定すると、コンテナ起動直後に Python/uv 等のツールチェーンが `$HOME/.cache` を作成してしまい、Modal が Volume をマウントする前にそのパスへ実体ができてしまうため、Modal が「マウント先が空でない」として拒否すること。`hh_hooks/tool_gate.py` の `_hh_agent_home()` は `USERPROFILE`（Windows 由来の変数名だが Linux コンテナでも読める）を `Path.home()` より先にチェックするため、`HOME` ではなく `USERPROFILE` だけを `/opt/data` に向ければ、他のツールチェーンには一切影響を与えずに同じ効果（`~/.hh-agent/` = `/opt/data/.hh-agent/`）を得られる。§3.4 のトークン自動更新関数とも同じパスを共有できる点は変更なし（両関数とも同じ Volume を `/opt/data` にマウントするため）。

`max_containers=1` により、複数コンテナ間のセッションルーティング問題自体が発生しない（§0 参照）。コールドスタート SLO は既存 Phase1a の合意（10 秒許容）をそのまま踏襲する。

### 2.3 ダッシュボードアクセス認証

> **⚠️ 2026-08-13訂正（§0.1参照）。** 独自の固定トークン（`HERMES_DASHBOARD_SESSION_TOKEN`・`?token=`URL）方式は**廃止**した。この方式は `start_server()` を経由しないと機能せず（`_resolve_session_token()` が実際に参照されるのは旧 `@modal.asgi_app()` パスのみ）、C-2（未認証アクセスでのトークン漏洩）の原因になった。

Hermes 標準搭載の `dashboard_auth` フレームワークをそのまま使う。H-H-Agent の scope トークンモデル（`issue_agent_token`/`require_scope`）とは**二重化しない**（脅威モデルが異なるため。Phase1a はローカルの危険コマンドをスマホ承認する仕組み、Phase1c はクラウド Hermes 自体へのアクセス制御）。

- 認証プロバイダは Hermes 標準搭載の `basic`（ユーザー名・パスワード、`plugins/dashboard_auth/basic/`）を使う。`HERMES_DASHBOARD_BASIC_AUTH_USERNAME`/`HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`（Modal Secret `hh-agent-dashboard-secret` 経由の環境変数）で有効化される
- `hermes dashboard --host 0.0.0.0 ...` を渡すことで、Hermes 自身の `should_require_auth()` が公開バインドと正しく認識し `app.state.auth_required=True` を自動設定。**認証プロバイダが登録されていなければ Hermes 自身が起動を拒否する**（`start_server()` 自体のフェイルクローズ設計をそのまま享受できる）
- セッションは HttpOnly Cookie（`hermes_session_at`/`hermes_session_rt`）。JS からは読めず、URL にも残らない
- アクセス URL は `https://<modal-url>/`（未認証だと `/login` へリダイレクト）。ログインは `POST /auth/password-login`（body: `{"provider":"basic","username":...,"password":...}`）

---

## 3. 承認ゲート統合（D-14「最重要」— 省略不可）

クラウド Hermes も Phase 1a と同じ `pre_tool_call` フック（`hh_hooks/tool_gate.py`）を有効にして起動する。**`env_type="modal"` で起動することは絶対に禁止**（承認ガードが丸ごとスキップされる、親設計書 D-14）。

### 3.1 config.yaml のブートストラップ・シーディング

HERMES_HOME は空の Volume から始まるため、ローカル Phase1a（既存の `%HERMES_HOME%\config.yaml` に手で追記）と違い、**初回起動時にコンテナ側で `config.yaml` を生成**する必要がある。ASGI アプリ構築前（`fastapi_app()` の中、`web_server.app` を import する前）に以下を保証する:

```yaml
hooks_auto_accept: true          # D-20。非対話起動では必須
hooks:
  pre_tool_call:
    - command: python /opt/hermes/hh_hooks/tool_gate.py
      matcher: ".*"
      fail_closed: true         # D-15。無いと障害時に全部素通りする
      timeout: 200               # D-13
```

既に `config.yaml` が存在する（2 回目以降の起動）場合は上書きしない。存在しない場合のみ新規生成する。

### 3.2 起動時フェイルクローズチェック（新規、`startup_guard.py` 相当）

Phase1a の `hh_hermes.py`（`startup_guard.enforce_or_exit()` を経由する起動ラッパー）は CLI 起動専用で、Phase1c は `hermes_cli.web_server:app` を直接 import する ASGI 構成のため、この経路を素通りする。**`modal_dashboard/app.py` の `fastapi_app()` 内で `hh_hooks.startup_guard.diagnose_pretool_hooks()` 相当を明示的に呼び、`pre_tool_call` フックが実際に登録されていることを確認してから `web_server.app` を返す。** 登録されていなければ `modal_hub/main.py` の `HubStartupError` と同じパターン（起動そのものを失敗させる）で落とす。「設定ファイルを書いた」ことと「フックが実際に有効である」ことの間の断絶は、このプロジェクトが最も警戒している失敗モード（D-07 / 既知の落とし穴 17・20）。

### 3.3 agent_token.json の配置とスコープ

`hh_hooks/tool_gate.py` はトークンファイルを `_hh_agent_home() / "agent_token.json"` という決め打ちパス（`USERPROFILE` 環境変数優先、無ければ `Path.home()`）からしか読めない（環境変数オーバーライド経路が無い）。§2.2 で `USERPROFILE=/opt/data` を設定するため、実体は `/opt/data/.hh-agent/agent_token.json`（Volume 上・永続）になる。

- Hub URL: `HH_AGENT_HUB_URL` 環境変数（Modal Secret 経由）で注入。ファイル書き込み不要
- `agent_token.json`: **存在しない場合のみ**（初回起動時）ASGI 関数側で最小限のブートストラップを行う（§3.1 の config.yaml と同じ「無ければ作る、あれば触らない」方針。理由は下記）。中身はローカル PC の `agent_token.json` とは**別発行**のトークン（Distiller 用 `distill_token.json` を別発行したのと同じ考え方）。スコープは Phase1a の承認フロー4 エンドポイント相当（`request`/`poll`/`claim`/`complete`）のみで、`publish` は含めない
- 実際の継続的な鮮度維持は §3.4 のトークン自動更新 cron が単独で担う。ASGI 関数側が毎回のコールドスタートで無条件にトークンを再発行すると、cron が直前に更新した新しいトークンを古い値で上書きしてしまう恐れがあるため、ASGI 関数側は「無ければ作る」に限定する

### 3.4 トークン自動更新（Modal cron）

`agent_token.json` の TTL は 24h（既存仕様のまま）。ローカルの `HH-Agent-TokenRefresh`（Windows タスクスケジューラ、12h 毎）相当の仕組みが Modal には無いため、**新規に Modal の定期実行機能で自動更新する**。

トークン発行は署名（`HH_AGENT_TOKEN_SIGNING_KEY`）と Hub 側の所有権レコード書き込み（`hh-agent-approvals` Dict への `agent_session:<tid>` 登録）の両方を要する処理であり、**新しい鍵を複製せず、既存の `modal_hub.core.security.issue_agent_token()` をそのまま呼び出して再利用する**（署名ロジックの複製は鍵のずれ・実装ドリフトのリスクを生むため避ける）。そのため、この関数は `hh-agent-dashboard-secret` に加えて既存の `hh-agent-secret`（`HH_AGENT_TOKEN_SIGNING_KEY` を含む）も両方アタッチする:

```python
@app.function(
    image=image,
    volumes={"/opt/data": modal.Volume.from_name("hh-agent-dashboard-home")},
    secrets=[
        modal.Secret.from_name("hh-agent-dashboard-secret"),
        modal.Secret.from_name("hh-agent-secret"),   # HH_AGENT_TOKEN_SIGNING_KEY 用（既存を再利用、複製しない）
    ],
    schedule=modal.Period(hours=8),   # TTL24hに対し8h間隔で十分な余裕
)
def refresh_dashboard_agent_token():
    from modal_hub.core import security
    # security.issue_agent_token(store, sub=..., source="phase1c_dashboard",
    #   scopes=["request", "poll", "claim", "complete"]) を呼び、
    # /opt/data/.hh-agent/agent_token.json を再発行・上書きする
    ...
```

`max_containers=1` の ASGI 関数とは別の Modal Function として定義する（スケジュール実行と ASGI サービングを同じ関数に混ぜない）。

---

## 4. イメージ・ビルド

### 4.1 方式

PoC で検証済みの `modal.Image.from_dockerfile()` 方式を継続する。`modal_dashboard/Dockerfile` を新規の正式ファイルとして repo に追加する。本家 `Dockerfile`（リポジトリ直下）とは完全に別管理とし、意図的な差分を `modal_dashboard/README.md` に明記してドリフトを可視化する。

### 4.2 本家 Dockerfile からの差分（PoC で確定済み）

除外: s6-overlay（Modal がコンテナライフサイクルを管理するため不要）、独自 SQLite ビルド、Playwright/Chromium、Matrix（python-olm）ビルドツールチェーン、Photon サイドカー、`messaging`/`hindsight`/`bedrock`/`azure-identity` エクストラ。

含める: Node 22（本家ピンは 26。**意図的な不一致**、Dockerfile 内コメントで明記する）、Python 3.13 + `uv sync --extra web --extra anthropic --extra pty`、`web/` と `ui-tui/` のフロントエンドビルド、ソース一式。

### 4.3 Node 版数

22 のまま進める。PoC で `ui-tui` のビルド・`hermes --tui` 起動・ANSI 出力までの実動作を確認済み。本家ピン（26）との将来的な非互換リスクは許容する（発生したら Dockerfile 側で個別に上げる）。

---

## 5. リポジトリ構成

```
Hermes-Hyper-Agent_HHAgent/
├─ modal_hub/              # 既存: H-H-Agentアドオン（承認ゲート、Phase1a/1b）
└─ modal_dashboard/        # 新規: Phase1c
   ├─ Dockerfile           # §4。本家Dockerfileとは別管理
   ├─ app.py                # modal.App定義。ASGI関数(§2.2) + token refresh関数(§3.4)
   └─ README.md             # 本家Dockerfileとの差分・意図的な不一致を明記
```

---

## 6. Phase1c 完了条件（実機確認。机上のレビューだけで完了と判定しない）

1. 公開 Modal URL への実デプロイが成功する
2. 実ブラウザから `?token=<固定トークン>` でアクセスし、`/api/pty` 経由で実際に Anthropic API との会話が成立する（PoC はインプロセス TestClient・ANSI 初期化シーケンス受信までだった。実際の会話往復は未検証）
3. コールドスタート時間の実測が 10 秒 SLO 内である
4. Volume 永続化の実証: 設定を書き込む → コンテナを強制再起動 → 再アクセスして設定が残っていることを確認
5. `max_containers=1` が Modal 側の関数設定として実際に効いている
6. **承認ゲート統合の実機確認**: クラウド Hermes に HIGH リスクコマンドを実行させ、スマホに承認画面が表示され、却下で実際に処理が止まることを確認（Phase1a の完了条件と同じパターンをクラウド側でも踏む）
7. トークン自動更新 cron が実際に動作し、24h 超経過後も `agent_token.json` が有効なままであることを確認

---

## 7. 実装体制

| 作業 | 担当 |
|---|---|
| `modal_dashboard/Dockerfile` の正式ファイル化（PoC 版の機械的移植） | DeepSeek / MiniMax M3 |
| `modal_dashboard/app.py`（Modal App/Volume/Secret/`max_containers` 配線、§3.2 の起動時フェイルクローズチェック、§3.4 のトークン自動更新関数） | Sonnet5 直接（`modal_hub/main.py` と同様、安全性クリティカルな配線のため） |
| 実デプロイ・実機検証（§6） | Sonnet5 直接（Codex は既知の制約で Modal へ到達不可） |
| コードレビュー | Codex |

新規アプリの主要デザイン（ダッシュボード UI 自体）は Hermes 本体の既存デザインをそのまま流用するため、今回「新規デザイン」には該当しないと判断している。UI 面で新規デザイン判断が必要になった場合は着手前に林さんへ相談する。
