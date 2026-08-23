# Hermes Profile共有設計（Linux/Windows①/Windows②間）

**作成日**: 2026-08-23（9セッション目）
**背景**: H-H-Agentの土台であるHermesは複数環境（Linux＝Modal上のhh-agent-dashboard、Windows①、Windows②）で利用している。Corpus2Skillの記憶は既に外部記憶として共有されているが、Profile（`config.yaml`・`SOUL.md`・skills・cron・MCP接続などのエージェント設定一式）は環境ごとに完全に独立しており、共有手段がなかった。特にAgentic OS HubのProfile Agent機能（`hermes_cli/web_routers/profiles.py`経由でLinux側に作成される）をWindows①・Windows②でも使えるようにしたい、という要望が発端。

## 調査結果：既存機能で解決できる

Hermes本体に「Profile Distributions」という、まさにこの用途向けの標準機能が既にある
（`website/docs/user-guide/profile-distributions.md`参照）。新規のsync基盤を自作する必要はない。

- **Distribution（gitリポジトリ）**: Profileをgitリポジトリとしてパッケージ化する仕組み。
  `SOUL.md`・`config.yaml`・`skills/`・`cron/`・`mcp.json`が「配布物」としてリポジトリに載る。
  `memories/`・`sessions/`・`state.db*`・`auth.json`・`.env`・`logs/`は**installerが強制的に除外**する
  （author側が誤ってコミットしても、install/update時にコピーされない設計）。
- 公式ドキュメントに本件とほぼ同じユースケースが「Personal: sync one agent across machines」として
  明記されている: 1台目で`git init && git push`、2台目以降で`hermes profile install <repo> --alias`。
  以後の更新は author側で`git push`、installer側で`hermes profile update <name>`のみ。

この設計により、SQLite（`state.db`）の複数環境同時書き込み衝突という当初懸念していたリスクは
**そもそも発生しない**（history系ファイルは配布物に含まれず、各環境が自分のstate.dbを持ち続ける）。
Corpus2Skill（会話の記憶）とProfile（人格・スキル・設定）は最初から別レイヤーとして設計されている、
という理解でよい。

## 採用する構成

### 役割分担

- **Author（発信側）＝ Linux（Modal, `hh-agent-dashboard-home` Volume）**: Agentic OS HubのProfile Agent
  機能で作成・編集されるProfileの実体はここにある。共有したいProfile Agentが決まったら、ここから
  Distributionを起こす。
- **Installer（受信側）＝ Windows①・Windows②**: `hermes profile install`で最初に取得し、以後は
  `hermes profile update`で追随する。

### リポジトリ方針

- Profile 1つ＝GitHubリポジトリ1つ（Distribution機構の設計上、リポジトリのルート＝Profileディレクトリ
  である必要があるため、1エージェント1リポジトリが必須。複数エージェントを1リポジトリにまとめる方式は
  ツールの標準機能では不可）。
- 命名: `hermes-profile-<agent名>`（例: `hermes-profile-researcher`）。既存の
  `hayashi0711-sketch/hermes-agent`フォーク運用と同じアカウント配下、**Private**リポジトリ。
- 全Profile Agentを共有する必要はない。共有したいものだけ選んで個別にDistribution化する
  （YAGNI。使わないエージェントまで自動でリポジトリ化しない）。

### なぜModal側でgit操作をしないか

Linux側（Modal）でそのまま`git push`しようとすると、GitHub認証情報をModal Secretとして新規に
持たせる必要がある。これは「課金・外部サービス連携が絡む設定変更」に該当し、この種の作業は
Codex経由（または明示的なユーザー確認）が必要という運用ルールがある。加えてそもそも、
Modal Volumeの中身をローカルへ落とす手段（`modal volume get`）をユーザー自身が既にデプロイ作業で
使っており、追加のインフラなしで完結する。そのため、**Author側の作業は「Modal Volumeから
ユーザーのWindows機へ一旦pullし、そこでgit操作する」**方式を採用する。Windows機は既に
GitHub認証（`myfork`運用等）を持っているため、新規の認証情報を用意する必要がない。

## v1runbook（手動、初回公開）

Linux上のProfile Agent「`researcher`」を共有したい場合の例（ユーザー自身のターミナルで実行）:

```powershell
# 1. Modal VolumeからProfileディレクトリをローカルへ取得
modal volume get hh-agent-dashboard-home profiles/researcher ./hermes-profile-researcher

# 2. .gitignore を作成（profile-distributions.md の Step 3 の内容をそのまま使う。
#    特に .env / auth.json / state.db* / memories/ / sessions/ / logs/ を必ず除外）

# 3. distribution.yaml を作成（最低限 name のみ必須）
#    name: researcher
#    version: 1.0.0

# 4. git初期化してプッシュ（git statusで秘密情報が混入していないか必ず確認してから）
cd ./hermes-profile-researcher
git init
git add .
git status   # ← 秘密情報が無いことを目視確認してからcommit
git commit -m "v1.0.0"
git remote add origin git@github.com:hayashi0711-sketch/hermes-profile-researcher.git
git push -u origin main
```

Windows①・Windows②それぞれで:

```powershell
hermes profile install github.com/hayashi0711-sketch/hermes-profile-researcher --alias
# .env.EXAMPLE をコピーして .env を作成し、必要なAPIキーを入力
```

## v1runbook（更新時）

Linux側でProfile Agentを編集したら、上記1〜4を繰り返して`git push`（`version`を上げて`git tag`する）。
Windows側は:

```powershell
hermes profile update researcher
```

`config.yaml`はinstaller側の変更が保持される（`--force-config`で配布側にリセット可能）。
`memories/`・`sessions/`・`.env`は一切上書きされない。

## 将来の自動化候補（今回はスコープ外）

- Agentic OS HubのProfile Agent管理画面（`ProfileAgentPanel.jsx`）に「配布用に公開/更新」ボタンを
  追加し、Hub backend（Linux側）から直接pushする案。ただし前述のとおりModal SecretへのGitHub認証情報
  追加が必要になるため、実装するならCodex経由での認証設定作業として別途着手する。
- Windows側の`hermes profile update`を定期実行するタスクスケジューラ登録（自動追随）。現状は
  「Windows側で使うたびに手動update」で十分という前提（利用頻度が低ければ自動化のメリットが薄い）。

## 未決事項

- どのProfile Agentを実際に共有するか（全部ではなく選択制）は次回以降ユーザーが個別に判断する。
- リポジトリの実際の作成（`git remote add origin`が指す先）はユーザー自身のGitHubアカウント操作の
  ため、Claude Codeはこの設計の実行（Modal操作・GitHubリポジトリ作成）を代行しない
  （Modal deploy系コマンドと同様、外部への可視な操作はユーザーのターミナルで行う運用を踏襲）。
