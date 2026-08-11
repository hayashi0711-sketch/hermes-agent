# hh_hooks/tool_gate.py — インストール手順（Phase 1a）

このファイルは `hh_hooks/tool_gate.py` を Claude Code (`PreToolUse`) と Hermes
(`pre_tool_call`) の両方へ登録するための手順を記す。**設計上の正**は
`docs/hh-agent/03_Architecture.md` §4.4 と `docs/hh-agent/05_Phase1a_Spec.md`。
このファイルと食い違う記述があれば設計書側が優先する。

> 本ファイル自体は `.claude/settings.json` / `%HERMES_HOME%\config.yaml` を自動生成しない。
> これらは各利用者のローカル設定ファイルであり、`hh_hooks/tool_gate.py` の
> 所有範囲外（本タスクは手順を文書化するところまでが責務）。下記の内容を
> 手動でマージすること。

---

## 0. 前提: 生成モジュールを同期する

`hh_hooks/risk.py` と `hh_hooks/risk_rules.yaml` は手書きしてはならない生成物。
`tool_gate.py` を使う前に必ず次を実行する（内容が変わったら再実行が必要）:

```powershell
python scripts\sync_hook_modules.py
```

成功すると次が作られる:

- `hh_hooks/risk.py`（`modal_hub/core/risk.py` の複製。先頭に
  `# GENERATED FILE - DO NOT EDIT`）
- `hh_hooks/risk_rules.yaml`（`modal_hub/core/risk_rules.yaml` の複製。
  `risk.py` が「自分と同じディレクトリの兄弟ファイル」として実行時に読み込むため必須。
  05_Phase1a_Spec.md / 03_Architecture.md には明記の無い追加の同期対象だが、
  無いと `risk.py` が `FileNotFoundError` で必ず落ちることを実機で確認済み）
- `hh_hooks/canonical.py`（`modal_hub/core/canonical.py` があれば複製。
  **本タスク実行時点 (2026-08-11) では `modal_hub/core/canonical.py` がまだ
  存在しない**。下記「既知の未解決課題」を必ず読むこと）

`scripts/sync_hook_modules.py` は標準ライブラリのみで書かれており、単体で
`python scripts\sync_hook_modules.py` として実行できる（他のスクリプトから
呼ぶ必要はない）。

---

## 1. ローカル状態ファイル

Phase1a spec に明記が無く、実装時に確定した配置（`tool_gate.py` docstring
「既知のギャップ」参照）。**すべて `%USERPROFILE%\.hh-agent\` 配下。**
`agent_token.json`/`config.json`/`bypass`系は `tool_gate.py`（承認フロー）が、
`distill_token.json` は `hh_distill.py`（Skill Distiller の publish フェーズ）
が読む — 読み手が異なる点に注意。

| ファイル | 内容 | 作成者 |
|---|---|---|
| `config.json` | `{"hub_url": "https://<workspace>--hh-agent-hub-fastapi-app.modal.run"}` | 手動、または環境変数 `HH_AGENT_HUB_URL` で代替可（そちらが優先） |
| `agent_token.json` | `{"token": "hha1.<payload>.<sig>"}`（親設計書 §6.1 のトークン形式） | `hh auth login`（**未実装**。手動発行する場合は Hub 側の発行ロジックと一致させること） |
| `distill_token.json` | `{"token": "hha1.<payload>.<sig>"}`（`scopes: ["publish"]` のみで発行） | **未実装**。`hh_distill.py run` の publish フェーズ専用（07_Phase1b_Spec.md §5）。`agent_token.json` とは別ファイル・最小権限で分離する。手動発行する場合は `modal_hub/core/security.issue_agent_token(..., scopes=["publish"])` を呼び、**戻り値の文字列を自分で `{"token": "<戻り値>"}` として保存する**（関数はトークン文字列を返すだけで、JSON ファイルの作成もストアへの副作用以外の書き込みも行わない） |
| `bypass` | `{"enabled_at": <epoch>, "reason": "...", "sig": "<hmac-sha256 hex>"}` | `hh bypass enable`（**未実装**）。署名対象は `f"{enabled_at}|{reason}"` を `local.key` の内容で HMAC-SHA256 したもの（下記「既知の未解決課題」参照） |
| `local.key` | バイパス署名用の対称鍵（バイト列） | `hh bypass enable` が初回生成（**未実装**） |
| `bypass_audit.log` | バイパス使用のローカル監査（追記のみ・JSON Lines） | `tool_gate.py` が自動追記 |
| `tool_gate.log` | 非機密の診断ログ（MEDIUM 通知の成否、内部エラー等） | `tool_gate.py` が自動追記（ベストエフォート。書けなくても処理は止めない） |

**このディレクトリにトークン・署名鍵の実値を絶対に Git や Obsidian へコミットしないこと。**

---

## 2. Claude Code 側: `.claude/settings.json`

`PreToolUse` フックとして登録する。**`timeout` は 200（秒）を必ず指定する**
（親設計書 D-13: フック内部デッドライン 170 秒より 30 秒長い外側の予算）。

### 2.1 推奨設定（規範。まずこれをコピーする）

**`matcher` を省略するか `".*"` にして全ツールをゲートすること。** これが
唯一の安全な設定であり、以下のどちらでもよい:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/hh_hooks/tool_gate.py\"",
            "timeout": 200
          }
        ]
      }
    ]
  }
}
```

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "python \"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/hh_hooks/tool_gate.py\"",
            "timeout": 200
          }
        ]
      }
    ]
  }
}
```

**理由（05_Phase1a_Spec.md §5.2「未知のツールの扱い（安全性クリティカル）」）**:
`risk.classify()` は未知のツール名（＝ MCP ツール・カスタムツールを含む、
`tool_aliases` に列挙されていない全て）を自動的に `HIGH`/`unknown_tool` へ
格上げして拒否する。**しかしこれはフックが実際に呼ばれた場合の話であり、
`matcher` で対象を絞ると、絞り込みに一致しないツールはフック自体を通らず
Claude Code 側の判断だけで実行される。** つまり `matcher` を絞ることは
`unknown_tool` のフェイルクローズ規則そのものを無効化する。**MCP ツールは
まさにこの「未知のツール」の典型例であり、gate されないまま実行できてしまう
ことは Phase1a の受け入れ条件（未知のツールは常に拒否）に違反する。**

- コマンドパスは絶対パスで指定する（相対パスは Claude Code の起動時カレント
  ディレクトリ次第で解決できないことがある）。
- 実行する `python` が `hh_hooks/risk.py`（生成コピー）の依存先である PyYAML を
  解決できるインタプリタであることを確認する（下記「既知の未解決課題」参照）。

### 2.2 非推奨・非適合の例（**コピーしないこと**）

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

**⚠️ 安全ではない・仕様非適合。** `matcher` をツール名の限定リストにすると、
それ以外のツール（MCP ツール・カスタムツール等）は `tool_gate.py` を一切
経由せずに実行される。上記 2.1 の理由により、**この設定は 05_Phase1a_Spec.md
§5.2 が要求する「未知のツールは常に HIGH として拒否する」という規則を
実質的に無効化する。承認ゲートの一部がまるごと迂回できる状態であり、
この設定を「デフォルトの推奨構成」として案内してはならない。** 記載するのは
「限定的な matcher がなぜ危険か」を示す反面教師としてのみである。

---

## 3. Hermes 側: `%HERMES_HOME%\config.yaml`

> **訂正（2026-08-11・4セッション目、実装検証で判明）**: 本節は当初「リポジトリ
> 直下の `cli-config.yaml`」と書いていたが誤り。実際の起動ランチャー
> `hh_hermes.py` → `hermes_cli.main.main()` が使う設定ローダー
> （`hermes_cli/config.py` の `load_config()`）は **`%HERMES_HOME%\config.yaml`
> しか読まない**（プロジェクトローカルへのフォールバックは無い）。
> `./cli-config.yaml` を読むのは `cli.py` トップレベルの `load_cli_config()`
> という別のレガシー経路で、`hh_hermes.py` 経由の起動では一切参照されない。
> `%HERMES_HOME%` は既定で `%LOCALAPPDATA%\hermes`（環境変数 `HERMES_HOME` で
> 上書き可）。**この PC では既に Hermes が日常利用されており、
> `HERMES_HOME` 環境変数が本番の `C:\Users\Haruki\AppData\Local\hermes` を
> 指している** — つまり下記の設定はその本番 `config.yaml` に追記することになる。
> 触る前に必ずタイムスタンプ付きでバックアップすること。実際に登録されたかは
> 目視確認ではなく `hh_hooks/startup_guard.py` の `diagnose_pretool_hooks()`
> を直接呼んで `registered_count` を見て確認する。

**`hooks:` は「イベント名をキーとする辞書」でなければならない。リストで書くと
`_parse_hooks_block()`（`agent/shell_hooks.py:353`）が警告もエラーも出さず
0 件登録で通過する** — つまり承認ゲートが存在しないまま起動してしまう
（親設計書 D-07 / 既知の落とし穴 17）。

```yaml
hooks_auto_accept: true          # ★D-20。非対話起動では必須（下記参照）
hooks:                            # ★辞書。トップレベルは「イベント名: [エントリ...]」
  pre_tool_call:
    - command: python C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/hh_hooks/tool_gate.py
      matcher: ".*"
      fail_closed: true           # ★D-15。無いと障害時に全部素通りする
      timeout: 200                # ★D-13。170秒の内部デッドラインより30秒長く取る
```

### 3.1 許可リスト（D-20。ここを外すと承認ゲートが黙って無効になる）

Hermes のシェルフックは `(event, command)` の組ごとに初回使用時の同意を要求し、
`~/.hermes/shell-hooks-allowlist.json` に記録する。**許可リストに無く、かつ
非対話（TTY 無し）で `accept_hooks` も指定されていない場合、フックは登録されず、
警告ログを出すだけで素通りする。** 上記 YAML の `hooks_auto_accept: true` は
これを防ぐために必須。代替手段（いずれか一つでよい）:

- `%HERMES_HOME%\config.yaml` に `hooks_auto_accept: true` を書く（上記の推奨方法）
- 環境変数 `HERMES_ACCEPT_HOOKS=1` を設定する
- `--accept-hooks` を付けて `hermes` を起動する
- `~/.hermes/shell-hooks-allowlist.json` を事前に用意する

### 3.2 起動時の自己診断（**必須。実装済み — D-20**）

**これは推奨事項ではない。** 親設計書 §4.4 と §8.2 は次を**要求**している:

> 起動時の自己診断でフックが実際に登録されたことを確認し、未登録なら
> エージェントを起動させない（D-20）。

理由（親設計書「既知の落とし穴」18. および D-20）: Hermes のシェルフックは
`(event, command)` の組ごとに初回使用時の同意を要求し、`hooks_auto_accept`
等が無い非対話起動では**警告ログを出すだけでフックが 0 件登録のまま起動が
成功する**。加えて `hooks:` をリストで書く等の設定ミスも同様に**警告すら
出さず**フックを 0 件にする（D-07・既知の落とし穴 17）。**どちらの事故も
「設定ファイルを書いた」ことと「フックが実際に有効である」ことの間に
検証されない断絶がある。** 承認ゲートが存在しないまま Hermes / Claude Code
が全ツール呼び出しを素通りさせる状態は、このプロジェクトが防ごうとしている
最悪の失敗そのものである。

**自己診断が確認しなければならない具体的な項目（Hermes 起動時）**:

1. `pre_tool_call` イベントに対してフックエントリが実際に **1 件以上** 登録
   されていること（`_parse_hooks_block()` を通した後の登録数を見る。
   `%HERMES_HOME%\config.yaml` を目視するだけでは、リスト形式で書いてしまった等の
   誤りを検出できない）。
2. 登録されたエントリの `matcher` が全ツールをカバーしていること
   （`".*"` 相当。§2.1 と同じ理由で、限定的な matcher は「登録はされて
   いるが実質ゲートされないツールがある」状態を作る）。
3. 登録されたエントリの `fail_closed` が `true` として**実際に有効**に
   なっていること（設定ファイルに書いてあるかではなく、パース後の
   実効値を確認する。D-15: 既定は fail-open）。
4. 上記 1〜3 のいずれかを満たさない場合、**エージェントを起動させない**
   （警告ログを出して起動を継続する、という選択肢は明示的に禁止される）。

**実装（2026-08-11）**: `hh_hooks/startup_guard.py` が Hermes の実効設定を
`hermes_cli.config.load_config()` で読み、`agent.shell_hooks.register_from_config()`
の戻り値（宣言ではなく実際に登録されたフック）を診断する。登録済み
`pre_tool_call` が 0 件、代表的な固定名と実行時生成のランダム名を含むプローブの
いずれかが未カバー、またはプローブに一致する登録エントリの
`fail_closed` が実効値で `false` の場合は、stderr に
`[HH-AGENT] STARTUP BLOCKED (D-20): ...` を表示して exit code 1 で停止する。

Hermes は `python -m hermes_cli.main` や `./hermes` を直接使わず、リポジトリ
ルートから次の専用ランチャーで起動すること。自己診断を通過した場合にだけ
実際の Hermes CLI エントリポイントへ処理を渡す。

```powershell
python hh_hermes.py
```

サブコマンドやオプションも通常どおり後ろへ付けられる。なお、自己診断自身も
許可リスト条件を通った**実登録結果**を見るため、§3.1 の
`hooks_auto_accept: true` 等はいずれか一つ必須である。

手動の補助確認（専用ランチャーによる起動ブロックの代替ではない）:

```powershell
hermes hooks test pre_tool_call
```
（Hermes 側にこのようなデバッグコマンドが無い場合は、意図的に危険なコマンドを
1 回叩いてフックが本当に発火する＝stderr に `[HH-AGENT]` が出るか、承認要求が
飛ぶかを目視確認する。これはフック本体の追加確認であり、自動化された起動
ブロックの代替にはならない。）

---

## 4. 動作確認（手動）

`tool_gate.py` は stdin から JSON を読み、exit code と stdout/stderr で結果を返す
単体のスクリプトなので、Claude Code / Hermes を経由せず直接テストできる。

```powershell
# LOW リスク: 即座に allow (exit 0, 出力なし)
'{"hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{"file_path":"C:/x"},"session_id":"s1","cwd":"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent"}' | python hh_hooks\tool_gate.py
echo $LASTEXITCODE   # 0 を期待

# 未知のツール: 即座に deny (exit 2)
'{"hook_event_name":"PreToolUse","tool_name":"SomeWeirdMcpTool","tool_input":{},"session_id":"s1","cwd":"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent"}' | python hh_hooks\tool_gate.py
echo $LASTEXITCODE   # 2 を期待

# HIGH (Write と .env): Phase1a では常に deny (下記「既知の未解決課題」参照)
'{"hook_event_name":"PreToolUse","tool_name":"Write","tool_input":{"file_path":"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/.env"},"session_id":"s1","cwd":"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent"}' | python hh_hooks\tool_gate.py
echo $LASTEXITCODE   # 2 を期待

# HIGH (Bash の git push --force): canonical.py が無い間は常に deny
'{"hook_event_name":"pre_tool_call","tool_name":"Bash","tool_input":{"command":"git push --force origin main"},"session_id":"s1","cwd":"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent"}' | python hh_hooks\tool_gate.py
echo $LASTEXITCODE   # 2 を期待
```

Hub 到達不能時に deny されることの確認（Phase1a の受け入れ条件そのもの）:

```powershell
$env:HH_AGENT_HUB_URL = "https://127.0.0.1:1"   # 到達不能な URL
# ... 上記の Bash HIGH コマンドを実行 ...         # canonical.py が実装済みの前提で exit 2 を期待
Remove-Item Env:\HH_AGENT_HUB_URL
```

---

## 5. 既知の未解決課題（実装者向け・司令塔への報告事項）

`hh_hooks/tool_gate.py` の冒頭 docstring に詳細を記載済み。ここでは
インストール作業に直接影響するものだけ要約する。

1. **`modal_hub/core/canonical.py` が未実装**（`04_Task_Allocation.md` の
   どちらの担当表にも所有者が無い）。これが無い間、HIGH リスクの Bash
   コマンドは **常に deny される**（Hub には一切接続しない・フェイルクローズ）。
   Phase 1a の受け入れ条件（スマホで `rm -rf` を承認/却下できる）は
   canonical.py が実装され `scripts/sync_hook_modules.py` を再実行するまで
   検証できない。
2. **Write/Edit/NotebookEdit の HIGH リスクは Phase1a では常に deny**
   （承認フローへ進まない）。TOCTOU の open→fstat 再検証は「実際にファイルを
   開く主体」（Claude Code / Hermes 組み込みの書き込みツール、いずれも変更
   禁止）でしか行えず、`PreToolUse`/`pre_tool_call` フックという短命プロセス
   の構造上、実行不可能と判断した。親設計書 §4.3 が明示的に許容する縮退。
3. **MEDIUM リスクの通知は既存の `POST /api/approval/request` を
   `risk="MEDIUM"` として 1 回・200ms タイムアウトで叩くだけ**（poll/claim は
   行わない）。専用の通知エンドポイントは Phase1a spec に定義が無いための
   実装判断。
4. **性能: LOW リスクパスは実測で 200ms を超える**（この Windows/Python
   3.14 環境で median ≈ 240ms、8 回計測）。原因は `hh_hooks/tool_gate.py`
   自身ではなく、`hh_hooks/risk.py`（生成コピー。手を加えられない）が
   モジュール読み込み時に無条件で `from tools.approval import
   detect_dangerous_command` を実行しており、**この import 単体で
   約 215〜220ms かかる**（`tools.approval` の推移的 import グラフが重い。
   `python -c "from tools.approval import detect_dangerous_command"` 単体で
   実測）。LOW リスク（読み取り専用ツール）ですら risk.py を import した
   時点でこのコストを払ってしまうため、tool_gate.py 側の遅延 import 等では
   回避できない。200ms 予算を満たすには risk.py 側で
   `detect_dangerous_command` の import を「shell カテゴリの判定時のみ」に
   遅延させる変更が必要だが、risk.py は本タスクの所有範囲外（生成コピーの
   元ファイルであり書き換え禁止）。司令塔判断を仰ぐ。
5. **エージェントトークンの自動更新は未実装**（Phase1a spec に更新用
   エンドポイントが無いため）。期限切れなら Hub に接続せず deny する。
6. **`hh auth login` / `hh bypass enable` は未実装**（本タスクの所有範囲外）。
   上記セクション 1 のトークンファイル・バイパスファイルは今のところ手動で
   用意する必要がある。起動時自己診断は `python hh_hermes.py` で実装済み。

---

## 6. Phase 1b: Skill Distiller のインストール

**設計上の正**は `docs/hh-agent/07_Phase1b_Spec.md`。本節は同書 §0.2・§1.1〜
§1.3 で確定した `hh_hooks/journal.py`（`post_tool_call`）と
`hh_hooks/session_end_distill.py`（`on_session_end`）の 2 フックを
Claude Code / Hermes へ登録する手順、`%USERPROFILE%\.hh-agent\config.json`
への `excluded_roots` 追記、および Phase 1b の手動 CLI の位置づけを記す。
このファイルと食い違う記述があれば設計書側が優先する。

### 6.0 前提: 追加の外部依存は無い

`journal.py` は標準ライブラリのみで書かれている（`hashlib`/`json`/`os`/
`sys`/`time`/`pathlib`/`typing` のみ import）。`session_end_distill.py` も
同様に標準ライブラリのみが必須だが、診断ログの redaction のために
`modal_hub.core.redact` を**失敗を許容する形で**（`try/except`）読み込む。
`modal_hub/core/redact.py` 自体も標準ライブラリのみ（`re`/`typing`）なので、
`tool_gate.py` が要求する PyYAML のような追加インストールは Phase 1b の
どちらのフックにも不要。したがって `scripts/sync_hook_modules.py` のような
事前同期ステップも要らない。

### 6.1 ローカル状態ファイル（追加分）

Phase1a の `%USERPROFILE%\.hh-agent\` 配下に、Phase 1b が新たに読み書きする
ファイル/ディレクトリを追加する。

| ファイル/ディレクトリ | 内容 | 作成者 |
|---|---|---|
| `config.json` の `excluded_roots` キー | 除外ルート文字列配列（§6.4） | 手動（本節で追記） |
| `journal\<sha256(session_id)先頭40桁>.jsonl` | セッションごとのツール呼び出しジャーナル | `journal.py` が自動作成・追記 |
| `distill_queue\{pending,submitting,submitted,completed,failed}\<queue_entry_id>.json` | キューエントリ | `session_end_distill.py`（pending のみ）／`scripts/hh_distill.py`（他状態、§6.5 参照） |
| `distill_queue\enqueue_errors.log` | キュー登録失敗の診断ログ（追記のみ・最大 1MB） | `session_end_distill.py` が自動追記 |

### 6.2 Claude Code 側: `.claude/settings.json`

`PostToolUse`（`journal.py`）と `SessionEnd`（`session_end_distill.py`）を
追記する。**Phase1a の `PreToolUse` と同じ理由で、`PostToolUse` の
`matcher` は省略するか `".*"` にして全ツールをカバーすること。** MCP
ツールの呼び出し結果もジャーナルに残らなければ、抽出条件②「ツール呼び出し
5 回以上」の判定材料が漏れる（journal.py が読めないツールは無かったことに
なる）。`SessionEnd` はツール名に紐づくイベントではないため `matcher` の
概念自体が無い。

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/hh_hooks/journal.py\"",
            "timeout": 200
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/hh_hooks/session_end_distill.py\"",
            "timeout": 200
          }
        ]
      }
    ]
  }
}
```

Phase1a の `PreToolUse` 登録と併存させる場合は、既存の `hooks` オブジェクト
へ `PostToolUse`/`SessionEnd` のキーを追加するだけでよい（`PreToolUse` は
そのまま残す）。

**`fail_closed` 相当の指定は不要（Phase1a との意図的な違い）**:
`tool_gate.py`（`PreToolUse`）は承認ゲートなのでフェイルクローズが絶対
原則だったが、`journal.py`/`session_end_distill.py` はどちらもフェイル
オープン設計（内部で例外をすべて握りつぶし常に exit 0 を返す。ジャーナル
やキュー登録が失敗しても、ツール実行やセッション終了そのものは絶対に
止めない）。`.claude/settings.json` にはそもそも `fail_closed` という設定
項目が無いためこの違いは §6.3（Hermes 側）でのみ設定として現れるが、
「Phase1a のノートを見て念のため足しておく」といった追記は不要、という
点をここで明記しておく。

### 6.3 Hermes 側: `%HERMES_HOME%\config.yaml`

§3 と同じ `hooks:` 辞書（トップレベルは「イベント名: [エントリ...]」）へ
`post_tool_call` と `on_session_end` を追加する。

```yaml
hooks_auto_accept: true
hooks:
  pre_tool_call:
    - command: python C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/hh_hooks/tool_gate.py
      matcher: ".*"
      fail_closed: true
      timeout: 200
  post_tool_call:
    - command: python C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/hh_hooks/journal.py
      matcher: ".*"
      timeout: 200
  on_session_end:
    - command: python C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent/hh_hooks/session_end_distill.py
      timeout: 200
```

**`fail_closed: true` を書かないこと（Phase1a との意図的な違い）。** §3 の
`tool_gate.py` は承認ゲートなのでフェイルクローズが絶対原則（書き忘れると
事故的にフェイルオープンへ後退する D-15 の対象）だったが、
`journal.py`/`session_end_distill.py` は設計上フェイルオープンが正しい
挙動であり、ここに `fail_closed: true` を書いてしまうと逆に「ジャーナルが
書けなかっただけでツール実行やセッション終了そのものを止める」という
意図しない副作用を持たせてしまう。**Phase1a のセクション 3 を見て反射的に
`fail_closed: true` を足さないこと。**

`hooks:` をリストで書くと `_parse_hooks_block()` が警告なしで 0 件登録に
なる落とし穴は §3 と同じ（辞書のトップレベルキーとして
`post_tool_call`/`on_session_end` を追加する形を必ず守る）。§3.2 の起動時
自己診断（`startup_guard.py`）は現時点で `pre_tool_call` のみを検査対象と
しており、`post_tool_call`/`on_session_end` の登録有無は自己診断の対象外
（フェイルオープンのフックであり D-20 の起動ブロック対象にする必要が無い
という設計判断）。登録されているかどうかは §6.6 の stdin 直叩きで手動
確認する。

### 6.4 `excluded_roots` の追記（`%USERPROFILE%\.hh-agent\config.json`）

`session_end_distill.py` は起動のたびに `config.json` を読み、
`excluded_roots` キーが**存在しなければ、その場でキュー登録そのものを
拒否する**（fail-closed。07_Phase1b_Spec.md §0.2 item1）。既存の
`config.json`（Phase1a で `hub_url` を書いたのと同じファイル）へ、個人の
ノート・日記など秘匿性の高い情報を置いているディレクトリを列挙する:

```json
{
  "hub_url": "https://<workspace>--hh-agent-hub-fastapi-app.modal.run",
  "excluded_roots": [
    "C:\\Users\\<username>\\Documents\\MyNotes",
    "C:\\Users\\<username>\\Documents\\MyJournal"
  ]
}
```

（上記の `MyNotes`/`MyJournal` はプレースホルダ。実際には自分の環境で
個人的なメモ・日記類を保存しているディレクトリの実パスに置き換えること。）

- キー自体が無い場合と、値が空配列 `[]` である場合は意味が異なる。
  **空配列は「意図的に除外なしとして許可する」という明示指定として扱われる**
  が、**キーが存在しないこと自体がキュー登録拒否の理由になる**
  （07_Phase1b_Spec.md §0.2）。「何も除外したくない」場合でも、キー自体は
  `"excluded_roots": []` として明示的に書くこと。
- 判定は `cwd` を `os.path.realpath()` した結果が、ここに列挙したパスの
  いずれかを `realpath()` したものと一致するか、その配下（子孫）である
  かどうかで行う（Windows の大文字小文字非区別に対応して正規化してから
  比較される）。
- 除外ルート配下でセッションが終了した場合、キューへは**何も記録されず、
  診断ログにも残らない**（除外ルート配下で作業していたという事実自体を
  漏らさない設計）。
- `excluded_roots` が無い、または `config.json` 自体の読み込み/パースに
  失敗した場合は、`%USERPROFILE%\.hh-agent\distill_queue\enqueue_errors.log`
  に `{"at": ..., "queue_entry_id": ..., "error": "registration refused: ..."}`
  という行が追記される。セッション終了処理自体は失敗しない（フェイル
  オープン）。

### 6.5 手動 CLI（フックとしては登録しない）

以下はフックとして常時登録するものではなく、必要なときに手動で実行する
コマンドラインツール。

- `python scripts/hh_skill_promote.py <name> [--force]` — 隔離領域
  （`~/.hh-agent/skills_quarantine/<name>/`）にある `SKILL.md` を
  `~/.hermes/skills/<name>/` へ人間の確認付きで昇格する、Phase 1b における
  唯一の promote 実装（07_Phase1b_Spec.md §4.2）。TTY が無い非対話実行では
  即座に拒否される。
- `scripts/hh_distill.py`（想定コマンド例: `python scripts/hh_distill.py
  run`。07_Phase1b_Spec.md §1.4/§2）は本タスク実行時点 (2026-08-11) では
  まだ実装されていない（MiniMax 所有・実装中）ため、確認できる CLI 引数
  仕様は無い。

### 6.6 動作確認（stdin 直叩き）

`journal.py`/`session_end_distill.py` はどちらも stdin から JSON を読み、
常に exit 0 で終わる単体スクリプトなので、Claude Code / Hermes を経由せず
直接テストできる。**§4 と同じ理由（PowerShell の `'...' | python` パターン
は JSON を壊すことがある）で、ここでも Bash/Git Bash 経由での実行を
推奨する。**

```bash
# journal.py: ジャーナル行が1行追記されることを確認
echo '{"hook_event_name":"post_tool_call","tool_name":"Read","session_id":"s1","cwd":"C:/x","extra":{"status":"ok","duration_ms":12,"tool_call_id":"tc1"}}' | python hh_hooks/journal.py
echo $?   # 0 を期待（フェイルオープン。常に0）
ls "$USERPROFILE/.hh-agent/journal/"   # sha256("s1") 先頭40桁.jsonl が作られていることを確認

# session_end_distill.py: excluded_roots 未設定なら enqueue_errors.log に記録され、pending/ には何も作られない
echo '{"session_id":"s1","turn_id":"t1","completed":true,"interrupted":false,"cwd":"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent"}' | python hh_hooks/session_end_distill.py
echo $?   # 0 を期待（fail-closedで拒否した場合もフック自体はフェイルオープンなので0）
cat "$USERPROFILE/.hh-agent/distill_queue/enqueue_errors.log"   # "excluded_roots キーが無い" 等の行を確認

# config.json に excluded_roots を追記した後、cwd が除外ルート配下でなければ pending/ に1件作られることを確認
echo '{"session_id":"s2","turn_id":"t1","completed":true,"interrupted":false,"cwd":"C:/Users/Haruki/Projects/Hermes-Hyper-Agent_HHAgent"}' | python hh_hooks/session_end_distill.py
ls "$USERPROFILE/.hh-agent/distill_queue/pending/"   # <queue_entry_id>.json が1件作られていることを確認
```
