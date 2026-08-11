# H-H Agent Phase 1b 詳細仕様（実装契約）

- **最終更新**: 2026-08-11
- **親設計書**: `docs/hh-agent/03_Architecture.md`（食い違う場合は親設計書が優先。特に §4.7 の隔離・昇格要件 D-16 は本書でも変更しない。**例外**: §4.7 item2 の `hh skill promote` コマンド名について、本書 §4.2 の注記を参照）
- **版**: v7（Codex 設計レビュー 6 巡目。v6 で V3-02 は RESOLVED 確定済み。V3-01 は「相対パスの `skills.external_dirs` エントリを cwd 基準で解決しており、Hermes 自身の `get_external_skills_dirs()` が行う『相対パスは cwd でなく `HERMES_HOME` を基準に解決する』（`agent/skill_utils.py` 実装コメントで確認済み）と食い違うため、Hermes が実際に見るルートより少なく候補を拾ってしまう」という指摘が残り、§4.2 のチェック2をHermesと同一の正規化・解決手順（スカラー→リスト正規化・`strip()`・`~`/環境変数展開・相対パスは `HERMES_HOME` 基準）へ揃えた。旧 Critical 6 件・v3 由来 Critical 1 件（V3-01・V3-02）は全て解消。Medium/Low の残課題は個人単一開発者運用の許容バックログとして残す）
- **位置づけ**: 親設計書 §11「Phase 1b 着手前に必須」の 4 件（11〜14）を確定させたもの。**ここに書かれていることは実装者が変更してよい判断ではない。** 不足を見つけたら BLOCKED として報告すること。

### v2 から変更した設計（Codex 2 巡目レビュー起点）

| 変更 | 理由 |
|---|---|
| 状態 `ambiguous` を廃止し `submitting` に統合 | v2 は `pending → ambiguous` を図示しながら、実際の遷移先を `submitting` と書いており矛盾していた（N-01）。**`submitting` 自体を「投入結果不明の耐久状態」として扱う**（§2.1） |
| 抽出条件①②③の preflight を**投入前**（§2.2）に確定的に移動 | v2 は §3.1 で「Batch 呼び出し前に preflight」と言いながら、§2.3 で「succeeded 結果を受けてから判定」とも書いており、両立不能だった（N-02）。**preflight は投入前に完結させ、不合格のエントリはそもそも Batch に入れない** |
| 除外ルートを設定ファイル駆動・解決不能なら fail-closed に変更 | v2 は Vault パスをハードコードしており、他アカウント/OS/Vault 移動で保護が黙って外れる（N-03） |
| `git diff` の取得から `--text` の言及を削除 | `--text` はバイナリを強制的にテキスト扱いする逆効果のオプションだった（N-04）。デフォルトの `git diff HEAD` が出す `Binary files ... differ` 行をそのまま使えばよい |
| publish アウトボックスの選定条件に `extracted == true` を追加 | `extracted: false`（非抽出）のエントリまで publish 対象に紛れ込んでいた（N-05） |
| `/api/skills/publish` を「create-or-match-only」に変更（無条件上書き禁止） | 別エントリの取り違えで新しい内容が古い内容に上書きされうる余地があった（N-06） |
| `queue_entry_id` に `turn_id` を混ぜる | 同一 `session_id` の再開・複数回終了イベントが、最初の 1 件だけを記録して以降を黙って捨てていた（M-01） |
| `hh skill promote` を Phase 1b の非目標と明記 | 親設計書が要求するコマンド名だが、この段階のコードベースには `hh` という統合 CLI 自体が存在しない（M-10。詳細は §4.2 の注記） |

---

## 0. 前提として変更しないもの

### 0.1 §4.7 の再掲・強調

- Distiller の実行場所は**ローカル PC のみ**（D-17）。Modal 上では動かさない。
- 出力先は隔離領域 `~/.hh-agent/skills_quarantine/<name>/SKILL.md`。**Hermes が探索するどのディレクトリにも直接書かない**（D-16）。
- 昇格は `skill promote` の**人間の明示操作のみ**。自動昇格を実装しない。
- Obsidian への書き込み経路を実装しない（D-12）。`modal_hub/tests/test_phase1b_guards.py` がこれを機械的に担保する。
- ジャーナル・Distiller 系は**フェイルオープン**（承認ゲートとは逆）。安全性に影響しないため。

### 0.2 データフロー方針（D-16 の機密漏洩側への対応）

**外部（Anthropic Batch API・Modal）へ送るあらゆるデータは、送信前に以下を通過しなければならない。**

1. **除外ルート判定（設定駆動・fail-closed。N-03 対応）**:
   - 除外ルート一覧は `~/.hh-agent/config.json` の新規フィールド `excluded_roots: string[]` から読む。
   - **初回セットアップ時**（`hh_distill.py` 初回実行、または `session_end_distill.py` が `config.json` に `excluded_roots` を見つけられなかった時）、Obsidian Vault のパスを自動検出できない場合は**その場で追加しない**。かわりに `~/.hh-agent/distill_queue/enqueue_errors.log`（§1.2）に「`excluded_roots` 未設定」を記録し、**設定が存在しない限りキュー登録そのものを拒否する**（fail-closed。「除外ルートが分からない ＝ 何でも許可」を絶対にしない）。
   - 運用手順としては、Phase 1b 導入時に林さんが `hh_hooks/INSTALL.md` の追記手順で `~/.hh-agent/config.json` へ `excluded_roots` を明示的に書く（例: `["C:\\Users\\Haruki\\Documents\\Obsidian Vault", "C:\\Users\\Haruki\\.hh-agent"]`）。**設定ファイルが存在し `excluded_roots` キーがあれば、値が空配列でも「意図的に除外なし」として扱ってよい**（明示された空は許可、キー自体が無いのは拒否）。
   - 判定は `cwd` を `os.path.realpath()` した結果が、設定済みいずれかのルートを `realpath()` したものの配下（一致 or 子孫）であるかどうかで行う。除外対象なら**キュー登録自体をスキップ**（記録も残さない。除外ルート配下で作業していたという事実自体を漏らさない）。
2. **サイズ上限**: `git diff HEAD` は **200 KB** を超えたら切り詰め、`"...[truncated N bytes]"` を末尾に付記して `truncated: true` をキューエントリに記録する。バイナリファイルは `git diff HEAD` のデフォルト出力が自動的に `Binary files a/... and b/... differ` の 1 行に要約する（**`--text`/`-a` は使わない** — それらは逆にバイナリを無理にテキスト展開してしまう。N-04 対応）。
3. **redaction**: 既存 `modal_hub/core/redact.py`（Phase 1a で実装済み）を**再利用**し、以下の**すべて**に適用する（v2 では `git diff` にしか適用しておらず、実際にモデルへ渡す会話内容が素通りになっていた。C-06 残課題対応）:
   - `git diff` の内容（キューへ書く直前・Batch リクエスト組み立て直前の両方）
   - `SessionDB.get_messages()` から取得したメッセージ本文・ツール結果（Batch リクエストへ含める直前）
   - ジャーナルのエラーメッセージ
   - 既存スキルの `name`/`description`（§3.2）
4. **会話内容の追加の上限**: `get_messages()` の結果は**メッセージ単位で** 1 件あたり 20 KB を超える `content`/`tool_calls` 結果を切り詰める（1 回の巨大なツール出力がトークン予算・redaction コストを食い潰さないようにする）。
5. **同意の扱い**: このプロジェクトは単一ユーザー・ローカル運用であり、専用の同意 UI は作らない。**`hh_distill.py run` を手動実行すること自体が、その時点で `pending/` にあるセッションを Anthropic Batch API と Modal Volume へ送ることへの同意とみなす**（§1.4 で自動実行しないと定めているため、実行判断は常にユーザーの手元にある）。
6. この方針は SKILL.md 生成・Batch 投入・Volume publish のいずれの経路でも省略できない。**quarantine 経由の Hermes 活性化は防げても、モデルがセッション内容から秘密を言い換えて出力する経路までは redaction で完全には防げない**。最終防波堤は promote 時の人間によるレビュー（§4.2）。

### 0.3 用語

- **キューエントリ**: 1 セッションの 1 終了イベント分の抽出候補。状態は `pending`/`submitting`/`submitted`/`completed`/`failed` の 5 つ（`ambiguous` は廃止。§2.1）。
- **queue_entry_id**: セッション＋終了イベントを指す不透明な識別子（§1.3）。ファイル名・Anthropic `custom_id` の両方に使う。

---

## 1. 起動契機（§11 項目11）

**二段階に分離する。** Batch API は最大 24 時間かかりうるため、セッション終了処理をブロックしない。

### 1.1 段階1: キュー登録（自動・`on_session_end` フック）

新規 `hh_hooks/session_end_distill.py` を Hermes の `on_session_end` フックとして登録する。

- 受け取るペイロード: `session_id` / `task_id` / `turn_id` / `completed` / `interrupted` / `model` / `platform` / `reason`。
- 処理（**すべて同期・軽量。`git diff` はここで取らない**）:
  1. `~/.hh-agent/config.json` を読み、`excluded_roots` キーが無ければ §0.2 のとおり登録を拒否して終了（診断ログへ記録）。
  2. `queue_entry_id`（§1.3）を計算。
  3. `~/.hh-agent/distill_queue/{pending,submitting,submitted,completed,failed}/<queue_entry_id>.json` の**いずれかに既に存在すれば何もしない**（同一終了イベントの二重キューを防ぐ。**同一 `session_id` でも `turn_id` が違えば別エントリになる** — M-01 対応）。
  4. 存在しなければ、cwd の realpath を §0.2 の除外ルートと照合。除外対象ならキュー登録自体をスキップ。
  5. 除外対象でなければ `pending/<queue_entry_id>.json` を**排他的ファイル作成**（`os.O_CREAT | os.O_EXCL`）で書く:
     ```json
     {
       "queue_entry_id": "...",
       "session_id": "...",
       "turn_id": "...",
       "queued_at": "<ISO8601>",
       "completed": true,
       "interrupted": false,
       "cwd": "<realpath>"
     }
     ```
     `git_diff` はここでは含めない。§2.2 の投入処理が**その場で** cwd から取得する。
- 例外は握りつぶすが、**§1.2 の診断ログには記録してから** exit 0 する。
- LLM 呼び出し・Batch API 投入・`git diff` 取得・抽出条件判定はこの段階で**一切行わない**。

### 1.2 診断ログ

- `~/.hh-agent/distill_queue/enqueue_errors.log`（追記のみ・最大 1 MB。超過したら古い方から切り詰める）。
- 1 行 1 JSON: `{"at": "...", "queue_entry_id": "...", "error": "<例外の型名とメッセージ。redact.py 適用済み>"}`。

### 1.3 `queue_entry_id` の定義

```python
import hashlib, json
_key = json.dumps([session_id, turn_id or ""], separators=(",", ":"))
queue_entry_id = "s" + hashlib.sha256(_key.encode("utf-8")).hexdigest()[:32]
```

- **`session_id`/`turn_id` を JSON 配列としてエンコードしてからハッシュする**（単純な `f"{session_id}:{turn_id}"` 文字列結合は、`session_id` 自体に `:` が含まれた場合に `("a:b", "c")` と `("a", "b:c")` が同一ハッシュへ衝突しうる。V3-03 対応。JSON の文字列エスケープにより、配列としてのタプル表現は入力に対して単射になる）。
- `turn_id` を混ぜることで、同一セッションが中断→再開→別の `turn_id` で終了、という複数の終了イベントをそれぞれ別エントリとして扱える（M-01 対応）。
- 正規表現 `^[a-zA-Z0-9_-]{1,64}$`（Anthropic `custom_id` 制約と同一）に**常に一致する**ことをテストで固定する。
- 元の `session_id`/`turn_id` はファイル**内容**にのみ保持し、パスにも `custom_id` にも使わない。
- **すべてのファイル操作**は、対象パスの `Path(...).resolve()` が対応する状態ディレクトリ直下（直接の子。孫階層を許さない）であることを確認してから行う。`resolve()` 前後のパス文字列が一致することを追加のガードにする（シンボリックリンク・reparse point を弾く）。

### 1.4 段階2: Batch 投入・回収（手動・`scripts/hh_distill.py`）

- コマンド: `python scripts/hh_distill.py run`
- **Phase 1b では自動実行（cron / Windows タスクスケジューラ）を組まない。** 課金を伴う Batch API 投入はユーザーの明示操作でのみ発生させる。
- `run` は「ローカル preflight →（合格分のみ）投入」(§2.2)と「投入済み分の回収」(§2.3)を1回のコマンドで行う。

---

## 2. Batch API の完了回収とリトライ（§11 項目12）

**Anthropic の実際の状態モデル（公式ドキュメントで確認済み）:**

- バッチ全体の `processing_status`: `in_progress` → (`canceling` →) `ended`。**それ以外の値は存在しない。**
- 個別リクエストの `result.type`（`ended` になって初めて確定）: `succeeded` / `errored` / `canceled` / `expired`。
- `request_counts`（`processing`/`succeeded`/`errored`/`canceled`/`expired`）でバッチ全体の内訳を確認できる。
- `custom_id`: `^[a-zA-Z0-9_-]{1,64}$`。§1.3 の `queue_entry_id` がそのまま使える。
- 上限: 1 バッチ 100,000 リクエストまたは 256 MB。結果は作成から 29 日で取得不能になる。

### 2.1 キューエントリの状態機械

```
pending → (ローカル preflight 不合格) → completed（extracted: false）
pending → submitting → submitted → completed
                                  → failed
submitting → (投入結果が最終確認できない) → submitting のまま残留（手動 resolve 待ち）
failed → (手動 retry) → pending
```

**`ambiguous` という別状態は存在しない。`submitting` 自体が「投入結果が確定するまでの耐久状態」であり、確定できないまま放置されたエントリは `hh_distill.py status` が「要手動確認」として表示し、`hh_distill.py resolve-submitting <queue_entry_id>` で解決する**（§2.2 手順4）。

| 状態 | 遷移条件 |
|---|---|
| `pending` | `on_session_end` が作成、または `retry` で差し戻された |
| `submitting` | `hh_distill.py run` が preflight 合格エントリについて**Anthropic API 呼び出し直前**に書く耐久状態。応答を受け取れるまで、あるいは手動 resolve されるまでここに留まる |
| `submitted` | API 呼び出しが成功し `batch_id` を確認できた |
| `completed` | preflight 不合格で即終了した場合、**または** Batch 結果を回収して処理が完了した場合。`extracted: true/false` を記録する |
| `failed` | 個別 `custom_id` の結果が `errored`/`canceled`/`expired`、または SKILL.md 生成中の例外 |

### 2.2 preflight → 投入（`hh_distill.py run` の前半）

0. **選定除外（V3-02 対応）**: `submitting/` に現存する全マニフェスト（`_manifest_*.json`）が列挙する `queue_entry_id` の集合を求め、この回の投入対象選定から**除外する**。理由: それらは「投入結果が未確定のまま前回の `run` を終えたエントリ」であり、`pending/` に見えていても新しいマニフェストへ二重に取り込んではならない（同一エントリが 2 つのマニフェスト経由で 2 回投入・2 回課金される事故を防ぐ）。
1. **ローカル preflight（§3.1 の条件①②③）を、上記で除外されなかった `pending/` の全エントリに対して、Batch リクエストを組み立てる前に実行する。** 各エントリについて `hh_hooks/journal.py` が書いたジャーナルを読み、条件①②③のいずれかを満たさなければ、**Anthropic API を一切呼ばずに** `completed/` へ直接移動し `extracted: false`・`reason: "condition_1_unmet"` 等を記録する。
2. preflight に合格したエントリについてのみ、この時点で `git diff` を取得（§0.2 のサイズ上限・redaction を適用）。**最大 100 リクエスト**を 1 チャンクとする（256 MB 上限に対する安全マージン。実際のバイト数もチャンクごとに積算し、80 MB を超えたら 100 件未満でもチャンクを区切る）。
3. チャンクごとに投入前の**耐久マニフェスト**を書く: `submitting/_manifest_<manifest_id>.json`（`manifest_id` は `uuid4().hex`）に、このチャンクへ含める全 `queue_entry_id`・各リクエストの `sha256(canonical_json(request))`・**`api_call_attempted: false`** を書く。続けて、対象の各 `pending/<queue_entry_id>.json` を `submitting/<queue_entry_id>.json` へ `os.replace()` で移動し、`manifest_id` を書き込む。**これが「送信意図の耐久記録」であり、この時点では Anthropic API はまだ一度も呼ばれていないことが `api_call_attempted: false` によって確定している。**
4. `client.messages.batches.create()` を呼ぶ**直前**に、マニフェストの `api_call_attempted` を `true` に書き換えて `fsync` する（この書き込みが完了して初めて「API 呼び出しが行われた可能性がある」区間に入る。V3-02 対応: これにより「ステップ3の途中でクラッシュ＝未送信」と「ステップ4の呼び出し中にクラッシュ＝送信結果不明」を後から確実に区別できる）。その後 `create()` を呼ぶ。
   - **成功**（`batch_id` を含む応答を受け取れた） → マニフェスト内の全エントリに `batch_id` を追記し `submitted/` へ移動。マニフェストファイルは削除してよい（`submitted/` 側のファイルが正）。
   - **明確な失敗**（4xx 等、API がリクエストを受理しなかったことが応答から確定できる） → マニフェスト対象の全エントリを `pending/` へ戻す（まだ課金されていないことが確定しているため）。マニフェストファイルを削除。
   - **不明**（タイムアウト・接続断・プロセスがここでクラッシュ） → **エントリは `submitting/` のまま、マニフェストファイルも残す（`api_call_attempted: true` のまま）。自動では何もしない。**
5. `api_call_attempted: true` のまま `submitting/` に残ったエントリは `hh_distill.py status` で「要手動確認」として表示される。`hh_distill.py resolve-submitting <manifest_id>` が `client.messages.batches.list()` を呼び、`created_at` がマニフェスト作成時刻の前後数分以内のバッチを列挙し、**バッチの `request_counts` 合計とマニフェストのエントリ数を突き合わせて**候補を絞り込む。ユーザーが目視で該当バッチを確定できたら、マニフェスト内の全エントリへ `batch_id` を付けて `submitted/` へ手動遷移。特定できなければ `pending/` へ差し戻す（**二重投入のリスクを人間の確認に委ね、自動リトライはしない**）。
6. **再起動時の回復（V3-02 対応。`api_call_attempted` を最初に見る）**: `hh_distill.py run` は起動時に必ず `submitting/_manifest_*.json` を読み、まず `api_call_attempted` を確認する。
   - **`api_call_attempted == false`**（ステップ3の途中、API 呼び出しに到達する前にクラッシュしたことが確定している） → マニフェストが列挙する全 `queue_entry_id` を、現在どのディレクトリにあっても**無条件に `pending/` へ戻し**、マニフェストを削除する。**API は一度も呼ばれていないため、この操作に曖昧さは無い。**
   - **`api_call_attempted == true`**（呼び出しが行われた可能性がある） → マニフェストが列挙する `queue_entry_id` を**1 件ずつ**現在の所在ディレクトリと照合する:
     - `submitted/` にある（＝手順4の成功パスまで完了した） → 完了とみなす。
     - まだ `submitting/` にある（＝結果が確定していない） → 「要手動確認」対象として残す（ステップ5参照）。
   - マニフェストは、**`api_call_attempted == true` の場合に限り、列挙された全エントリが `submitting/` から居なくなった時点でのみ**削除する。1 件でも `submitting/` に残っていれば、マニフェストは残し続け、（ステップ0により）新しいマニフェストへの二重取り込みを防ぎ続ける。

### 2.3 回収（`hh_distill.py run` の後半）

**このフェーズはもう抽出条件①②③を判定しない（preflight は §2.2 で完結済み）。ここで行うのは④（novelty）の結果反映と SKILL.md の保存のみ。**

1. `submitted/` 内のファイルが持つ `batch_id` を重複排除して列挙。
2. 各 `batch_id` を `batches.retrieve()`。`processing_status != "ended"` なものはスキップ。
3. `processing_status == "ended"` のバッチは `request_counts` を記録した上で `batches.results()` で個別結果をストリーム取得する。
4. 各結果について:
   - `custom_id` が `submitted/` に存在しない（既に処理済み・重複配信・別バッチの結果が紛れた等） → ログに記録して**無視**（例外を投げてストリーム処理全体を止めない。C-04 対応）。
   - `custom_id` が `submitted/` に存在する場合、`result.type` に応じて:
     - `succeeded` → `services/skill_distiller.py` の novelty 判定・SKILL.md 生成へ渡す（§3.2）。
       - 生成成功 → `services/skill_quarantine.py` で隔離保存 → `completed/`、`extracted: true`。
       - `duplicate`/`not_extractable` 判定 → `completed/`、`extracted: false`、理由を記録。
       - 隔離保存中に例外 → `failed/`、例外内容を記録。
     - `errored`/`canceled`/`expired` → `failed/`、`result.type` とエラー内容を記録。
5. ストリーム処理が途中で例外を投げて中断した場合、**既に `completed/`/`failed/` へ移動済みのエントリの状態はそのまま確定**とし（各エントリの移動は 1 件ずつ完結するため、中断は「まだ処理していないエントリが `submitted/` に残る」以上の影響を持たない）、次回 `run` で残りを再開する。
6. `request_counts` の合計と、`submitted/` 側でそのバッチを参照するエントリ数が一致しないまま処理が終わった場合、警告を `status` に出す（自動修復はしない。人間が調べる対象として可視化するだけ）。
7. `submitted/` に残ったまま 29 日（結果取得可能期限）を超えたエントリは、次回 `run` 実行時に `failed/` へ強制移動し `reason: "result_expired"` を記録する。

### 2.4 リトライエラーのタクソノミー（M-06 対応）

`batches.retrieve()`/`batches.results()` 呼び出し時のエラーを以下のように分類する:

| エラー種別 | 扱い |
|---|---|
| 認証エラー（401/403） | **即座に `run` 全体を中断**し、`status` にエラー表示。エントリの状態は変更しない（再試行しても解決しないため、ユーザーの資格情報修正を待つ） |
| レート制限（429） | このバッチの処理をスキップし、次の `batch_id` へ進む。エントリの状態は変更しない（次回 `run` で再試行） |
| 404（バッチが見つからない） | 該当バッチを参照する全エントリを `failed/` へ、`reason: "batch_not_found"` |
| サーバエラー（5xx）・ネットワークタイムアウト | このバッチの処理をスキップ。エントリの状態は変更しない（次回 `run` で再試行） |
| 結果ストリームの JSON パースエラー・不正な UTF-8 | 該当行をログに記録してスキップ（他の行の処理は継続）。ストリーム全体が壊れている場合のみバッチ処理を中断し、エントリの状態は変更しない |
| ローカルディスク書き込み失敗（`failed/`/`completed/` への移動自体が失敗） | `run` はエラーで終了。**この場合のみ**エントリが `submitted/` に残ったまま矛盾した中間状態になりうるため、次回起動時に §2.2 手順6と同様の整合性チェックを行う |

### 2.5 リトライ（キューエントリ単位）

- **自動リトライはしない。** Batch API はコストを伴うため、失敗の再送はユーザーの明示操作。
- `python scripts/hh_distill.py retry <session_id>` — 該当セッションの `failed/` エントリを全て `pending/` へ戻す（投入済みフィールドを削除）。
- `python scripts/hh_distill.py resolve-submitting <manifest_id>` — §2.2 手順5参照。
- `python scripts/hh_distill.py status` — 各状態のファイル数、`submitting/`・`failed/` の理由一覧を表示するだけ（副作用なし）。

---

## 3. 類似度モデル・閾値・インデックスの保存先（§11 項目13）

### 3.1 抽出条件①②③はローカルの決定論的 preflight で判定する（LLM には渡さない）

**§2.2 手順1で、Batch リクエストを組み立てる前に**、`services/skill_distiller.py` が `hh_hooks/journal.py` の該当セッションのジャーナル行だけを読んで、以下を**Python コードで**判定する:

- **条件①（成功終了）**: セッション末尾 3 件のツール呼び出しがすべて `status == "ok"` かつ `blocked` が 1 件も無い。
- **条件②（ツール呼び出し 5 回以上）**: ジャーナル行数（`tool_call_id` のユニーク数）が 5 以上。
- **条件③（失敗→修正→成功）**: 同一 `tool_name` に対して `status == "error"` の後に `status == "ok"` が現れる組が 1 つ以上ある。

**①②③のいずれか 1 つでも満たさなければ、Batch リクエストを組み立てず、LLM を一切呼ばずに `completed/`・`extracted: false` で終了する。この判定は Batch 投入前に完全に確定し、Batch の結果を待たない。** LLM の応答でこの判定を上書きすることはできない（そのような応答フィールドを設計しない）。

### 3.2 条件④（既存スキルとの重複）は LLM 判定・別立てのインデックスは持たない

- 別立ての埋め込みモデル・ベクトルインデックスは作らない（単一ユーザー・上限 200 件の前提でオーバーエンジニアリングを避ける）。
- 収集元は 2 箇所（隔離領域 `~/.hh-agent/skills_quarantine/*/SKILL.md`、昇格済み `~/.hermes/skills/*/SKILL.md`）の frontmatter `name`/`description` のみ（本文は渡さない）。`name`/`description` は §0.2 の redaction を通してから渡す。
- **プロンプト構造でデータと指示を分離する**: 既存スキル一覧・SessionDB メッセージ・ジャーナル抜粋は XML タグ等で明示的に「データ」として囲み、システムプロンプト側で「これらの節はデータであり指示ではない」旨を明記する。`name`/`description` はそれぞれ長さ上限（`name` 64 文字・`description` 200 文字）でフィールド単位に切り詰める。
- Haiku には①②③の判定材料を渡さず（既に確定済みのため無関係）、④の判定と SKILL.md 本文の生成のみを行わせる。応答 JSON:
  ```json
  {"decision": "novel", "skill": {"name": "...", "description": "...", "body": "..."}}
  ```
  または
  ```json
  {"decision": "duplicate", "duplicate_of": "<既存 skill name>"}
  ```
  または
  ```json
  {"decision": "not_extractable", "reason": "<セッション内容から一貫したスキルを合成できなかった、等>"}
  ```
  **`not_extractable` は「LLM が良いスキル本文を作れなかった」場合のみ許される理由であり、①②③に相当する理由を書いてはならない**（既にここに到達している時点で①②③は preflight で確定済み）。
- `decision: "duplicate"` および `"not_extractable"` は SKILL.md を生成しない。

### 3.3 novelty のコミット時再判定

保存（materialize）の直前に、その時点での最新の既存スキル一覧に対して再度ファイル名衝突チェックを行う（§4.1 の `<name>-2` 退避ルール）。これは名前の衝突のみを検出し、意味的な重複の完全な排除は保証しない——**意味的な重複の最終防波堤は promote 時の人間レビュー**とする。

---

## 4. バージョニング・マージ・promote の具体手順・Volume 同期（§11 項目14）

### 4.1 バージョニング

- `version` は常に `0.1.0` 固定。Phase 1b では自動インクリメント・マージ機能を**実装しない**。
- 同名の隔離スキルが既にある場合は `<name>-2` 等へ退避。ベース名が 47 文字を超える場合は 47 文字に切り詰めてから `-2` を付ける。`-2`〜`-9` まで試して全て衝突する場合はエラー終了する。
- **これは名前の衝突回避であり、意味的なバージョニングやマージではない。**
- **materialize は `queue_entry_id` ごとに高々 1 回だけ発生する**: `completed/<queue_entry_id>.json` に `output_path`・`content_sha256`・`materialized: true` を書いた後は、同じ `queue_entry_id` が再処理されても（クラッシュ後の再実行等）新しいファイルを作らず、既存の `output_path`/`content_sha256` をそのまま返す（M-08 対応。「同じ結果の再処理は同じ成果物に解決する」）。

### 4.2 promote 手順（新規 `scripts/hh_skill_promote.py`。安全性クリティカル）

**このスクリプトが、Phase 1b における唯一の promote 実装である。**

> **`hh skill promote` について（M-10）**: 親設計書 §4.7 item2 はコマンド名を `hh skill promote <name>` としているが、このコードベースには現時点で統合 CLI `hh` が存在しない（`hh auth login` 等も同様に未実装、`08_Handoff_Note.md` 落とし穴24参照）。**Phase 1b では `python scripts/hh_skill_promote.py <name>` を暫定の正式コマンドとする。** 将来 `hh` CLI 統合基盤を作る際は、このスクリプトの中核関数を呼び出すサブコマンドとして配線し、別実装を作らない。親設計書 §4.7 item2 はこの注記をもって読み替える。

1. `python scripts/hh_skill_promote.py <name> [--force]`
2. `<name>` を `^[a-z0-9][a-z0-9-]{1,48}$` で検証（CLI 境界で最初に行う）。
3. `~/.hh-agent/skills_quarantine/` を `resolve()` した隔離ルート配下に `<name>/SKILL.md` が存在するか確認。**シンボリックリンク・reparse point・ハードリンクを含む経路なら拒否**（`resolve()` 前後のパス文字列比較、および `os.stat().st_nlink > 1` でのハードリンク検出）。ファイルは `O_NOFOLLOW` 相当（POSIX では `os.open(..., os.O_NOFOLLOW)`。Windows では `resolve()` 前後比較がこれを代替する）で開く。
4. ファイルを**1 回だけ**バイト列として読み込み、その場でメモリ上に保持する。`sha256` ダイジェストを計算する。
5. 読み込んだバイト列を、制御文字・ANSI/OSC エスケープシーケンスをリテラル表記に変換した上で**全文表示**する。ダイジェストの先頭 12 文字も併記する。
6. 確認プロンプトは対象名・ダイジェスト・ライセンス表記を明示する: `Promote 'skill-name' (sha256:abcd1234..., license: MIT) to ~/.hermes/skills/skill-name/? This may reproduce code/text from your session; confirm you have the right to license it MIT. [y/N]`（L-03 対応）。**非対話実行（TTY が無い）では即座にエラー終了する。**
7. `y` が入力されたら、以下の**クラッシュから復旧可能な**手順でインストールする。**ステージング領域は Hermes が実際に探索するどのルートの配下でもないことを、設定を読んで確認してから使う**（V3-01 対応。2 巡目の指摘: `~/.hermes/skills/` だけをハードコードで避けても、`config.yaml` の `skills.external_dirs`（`agent/skill_utils.py` の `get_all_skills_dirs()` が返す。ローカル固定パス＋設定ファイル由来の任意個の追加ルート）が `~/.hh-agent/` を包含する設定にされていれば同じ脅威が再現するため、ハードコード除外では不十分）:
   a. **起動時**（`--force` の有無によらず、`<name>` を書き込む前）に 2 つのチェックを両方行う（V3-01 の 2 回目の指摘対応: `agent.skill_utils.get_all_skills_dirs()` は `skills.external_dirs` のうち**現に存在するディレクトリしか返さない**ため、「設定はされているがまだ存在しない、かつ `promote_staging` と同じ場所を指す」パスを見逃す）。
      - **チェック1（既存ルート）**: `agent.skill_utils.get_all_skills_dirs()` を呼び、返された各ルートを `resolve()` する。
      - **チェック2（設定上の宣言。存在有無を問わない）**: `hermes_constants.get_config_path()` で `config.yaml` の場所を取得し、存在すれば `yaml.safe_load()` で読み、`skills.external_dirs` を取り出す。**値の正規化とパス解決規則は `agent/skill_utils.py:get_external_skills_dirs()` と完全に同じ手順を踏む**（存在チェックで足切りする最後の一歩だけを行わない、という差分のみを持たせる。手順が違えば Hermes が実際に見るルートより少なく候補を拾ってしまい、この安全策自体が抜け穴になる——v6 レビューで実際に指摘された）:
        1. `external_dirs` が文字列 1 個なら 1 要素のリストとして扱う（Hermes 同様、スカラー指定を許容する）。リストでも文字列でもなければ空扱い。
        2. 各エントリを `str(entry).strip()` し、空文字ならスキップ。
        3. `os.path.expanduser(os.path.expandvars(entry))` で `~` と環境変数を展開する。
        4. **相対パスなら `hermes_constants.get_hermes_home()` を基準に解決し、絶対パスならそのまま解決する**（`Path(expanded)` が `is_absolute()` でなければ `(get_hermes_home() / Path(expanded)).resolve()`、絶対なら `Path(expanded).resolve()`。**cwd を基準にしない** — ここを誤ると、`skills.external_dirs` に相対パスで宣言されたエントリが Hermes の実際の解決結果と一致しなくなり、チェック自体が無意味になる）。
        5. **`is_dir()` によるフィルタは行わない**（ここが `get_all_skills_dirs()` との唯一の意図的な差分。存在しないパスも候補として残す）。
        いずれかの手順で `config.yaml` の読み取り・パースに失敗した場合はフェイルクローズ（後述）。
      - **判定**: `~/.hh-agent/promote_staging/` を `resolve()` した結果が、チェック1・チェック2いずれかの候補ルートのいずれかと**一致するか、その配下、またはその祖先**（＝ステージング領域の方がスキャンルートを内包してしまう倒錯したケースも含む）であれば、**ファイルを一切書かずに即座にエラー終了する**。`config.yaml` の読み取り・パースが失敗した場合も**フェイルクローズ**（「読めなかったから安全」ではなく「読めなかったので拒否」）。
   b. 上記チェックを通過して初めて、`~/.hh-agent/promote_staging/<name>/SKILL.md` へ完全に書き込み、`os.replace()` で確定させる。手順4のダイジェストと再読込したダイジェストが一致することを確認し、不一致なら中止する。**`~/.hh-agent/` と `~/.hermes/` が異なるドライブ/ファイルシステムにある環境では、最終手順の `os.replace()` がアトミックにならないため、その場でエラー終了する**（Phase 1b は単一ユーザー・単一マシン運用の前提でこの制約を許容する）。
   c. `~/.hermes/skills/<name>/` が存在しない場合 → `os.replace(staging_dir, name)` で探索ツリーの外から内へ一発配置して終了。
   d. `~/.hermes/skills/<name>/` が存在し `--force` が無い場合 → 拒否して終了（staging を削除）。
   e. `--force` の場合 → `os.replace(name, backup)`（`backup` = `~/.hh-agent/promote_backups/<name>.bak.<timestamp>/`。探索ツリー外）を実行した**直後に** `os.replace(staging_dir, name)` を実行する。**この 2 手順の間でクラッシュした場合の回復**: 次回 `hh_skill_promote.py` 実行時（対象名を問わず起動のたび）に、`~/.hermes/skills/<name>/` が存在せず、かつ `~/.hh-agent/promote_backups/<name>.bak.*/` と `~/.hh-agent/promote_staging/<name>/` が両方存在する組を検出したら、`os.replace(staging_dir, name)` だけを実行して回復する（起動時セルフヒール。**回復が完了するまでの間、`<name>` は探索ツリーのどこにも存在しない** — 「拒否された昇格が一時的にでも発見可能になる」という V3-01 の脅威モデルを完全に断つ）。
8. ローカル `~/.hh-agent/promote_log.jsonl` に 1 行追記: `{"name", "promoted_at", "distilled_from_session_id", "source_digest", "destination", "forced": bool, "backup_path": "..."|null, "license_confirmed": true}`。**監査 Volume（Modal）には書かない。**

### 4.3 Volume とローカルの同期方式

**一方向・ベストエフォート・非同期。ローカルの隔離領域が正。**

- 隔離保存が成功した直後（§2.3 の `extracted: true` 確定時のみ。N-05 対応）、`publish_status: "pending"` をそのキューエントリに記録する。`extracted: false` のエントリには `publish_status` を一切付与しない。
- `hh_distill.py run` は毎回の実行末尾で、**`extracted == true` かつ `publish_status == "pending"`**（この 2 条件を両方満たす場合のみ。N-05 対応）のエントリを対象に `POST /api/skills/publish` を呼ぶ:
  - 成功 → `publish_status: "published"`。
  - 失敗 → `publish_attempts` をインクリメント。**5 回失敗したら `publish_status: "abandoned"`**（`status` コマンドで可視化。それ以上自動リトライしない）。
- **promote 操作は Volume を一切参照しない。** ローカルの隔離領域のみで完結する。
- **Volume → ローカルの取り込み（pull）は実装しない。**

---

## 5. 新規エンドポイント `POST /api/skills/publish`

- ファイル: `modal_hub/routers/skills.py`（新規）
- **これは「読み取り専用の複製」ではなく、認証済みの永続書き込みエンドポイントである。** 承認ゲートと同等の警戒で実装する。
- **認証**: エージェントトークンに `scopes: list[str]` フィールドを追加する（`modal_hub/core/security.py` の拡張）。既存 Phase 1a トークンには `scopes` が無いため、**欠落時は `{"request","poll","claim","complete"}` の従来固定セットとみなし、`publish` は含めない**。`publish` スコープを持つトークンのみがこのエンドポイントを呼べる。Distiller ローカルワーカー用のトークンは `issue_agent_token()` を呼ぶ際に明示的に `scopes=["publish"]` を指定して発行する。
- **リクエスト**: `{"name": "<kebab-case>", "skill_md": "<全文>", "content_sha256": "<送信側で計算したダイジェスト>"}`。本文サイズ上限 **64 KB**。
- **検証**:
  1. `name` は `^[a-z0-9][a-z0-9-]{1,48}$`。
  2. `skill_md` の frontmatter をパースし、`name` フィールドがリクエストの `name` と一致することを確認（不一致は 400）。
  3. `content_sha256` が実際の本文のダイジェストと一致することを確認（不一致は 400）。
  4. `modal_hub/core/redact.py` を本文に適用し、redaction 前後で差分が出たら**保存を拒否**する。
- **書き込みセマンティクス（create-or-match-only。N-06 対応。無条件上書きをしない）**:
  - Volume に `skills_quarantine/<name>/SKILL.md` が**存在しない** → 新規作成。
  - **存在し、既存の `content_sha256` がリクエストと一致** → 何もしない（`200 {"status": "ok", "unchanged": true}`。同一内容の再送は idempotent）。
  - **存在し、既存の `content_sha256` がリクエストと異なる** → `409 {"error": {"code": "SKILL_ALREADY_PUBLISHED_WITH_DIFFERENT_CONTENT"}}` を返す。**このエンドポイントは同名スキルの内容を書き換える手段を提供しない**（ローカルの `<name>-2` 衝突回避ルールと対称的に、リモート側も「同名で内容が違う」状態を黙って解決しない）。呼び出し元（`hh_distill.py`）はこの 409 を受けたら `publish_status: "failed_conflict"` として記録し、自動リトライしない。
- **レート制限**: トークンごとに「publish 20 件/時」。
- **保存先**: Volume 上 `skills_quarantine/<name>/SKILL.md`。書き込み後 `volume.commit()`。
- **監査**: 成功・拒否いずれも `services/audit.py` の既存方式で記録する（新種イベント `skill_published` / `skill_publish_rejected`）。

---

## 6. ファイル所有権の追記（`04_Task_Allocation.md` Phase 1b 表の補足）

| ファイル | 所有者 | 理由 |
|---|---|---|
| `hh_hooks/session_end_distill.py` | Sonnet 5 | フック新設・`journal.py` と同系統 |
| `scripts/hh_distill.py` | MiniMax | `skill_distiller.py` 呼び出しの薄いラッパ。状態機械（§2）は変更しない |
| `scripts/hh_skill_promote.py` | **Sonnet 5** | 安全性クリティカル（D-16、TOCTOU・クラッシュ回復） |
| `modal_hub/routers/skills.py` | **Sonnet 5** | 新規 Hub エンドポイント・トークンスコープ拡張 |
| `modal_hub/core/security.py`（`scopes` 拡張分） | **Sonnet 5** | 既存ファイルの安全性クリティカルな拡張 |

---

## 7. 着手前チェックリスト

- [ ] `modal_hub/tests/test_phase1b_guards.py` の該当ガードを実装着手と同時に更新・削除する。
- [ ] 親設計書 §8.1 の4項目に加え、本書が追加した不変条件（§1.3 の `queue_entry_id` 正規表現固定、§2.2 の preflight が Batch 投入前に完結すること、§3.1 の①②③が LLM 応答で上書きされないこと、§4.2 の promote ダイジェスト一致とクラッシュ回復、§5 の create-or-match-only）を `modal_hub/tests/test_distiller.py` 等に必ず含める。
- [ ] 本書 §0〜5 で確定した状態機械・データフロー方針・ファイルパス・エンドポイント契約は実装者が変更しない。BLOCKED で報告する。
- [ ] Anthropic Batch API の実際の挙動は実装時に SDK のバージョンで再確認し、齟齬があれば本書を改訂してから進める。
