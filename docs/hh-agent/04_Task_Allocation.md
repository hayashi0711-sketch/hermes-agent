# H-H Agent 実装担当表（ファイル単位の所有権）

- **最終更新**: 2026-08-10
- **親設計書**: `docs/hh-agent/03_Architecture.md`（食い違う場合は親設計書が優先）
- **Phase 1a 実装契約**: `docs/hh-agent/05_Phase1a_Spec.md`（API スキーマ・状態遷移表・canonical JSON・トークン・PWA セキュリティ・監査。**Phase 1a の実装者は必ず読む**）

## 分割の原則

**タスク単位ではなくファイル単位で所有者を固定する。** 過去の並列実装で、タスク単位に分けると同じファイルを複数の実装者が触って衝突した。ファイル単位に切り替えたところ 5 体並列で衝突ゼロを達成している。

- 所有者以外は担当外ファイルを**編集しない**。必要があれば「このファイルのここを変えてほしい」と**報告する**。
- インターフェース（関数シグネチャ・データ形状）は本表と親設計書で先に固定し、実装者に決めさせない。
- 実装者が仕様の穴を見つけたら、**推測で埋めずに BLOCKED として報告する。**

## 比率

| 担当 | 比率 | 割り当ての考え方 |
|---|---|---|
| Claude Code **Sonnet 5** | 30% | **間違うと安全性が壊れる箇所**。セキュリティ・状態機械・既存 Hermes との接続部 |
| **MiniMax M3** | 70% | UI・定型実装・薄いラッパ・テスト |
| **Codex** | — | 全コードのレビュー、GitHub push、Modal Secret 作成 |

---

## Phase 1a: モバイル承認ゲート

### Sonnet 5 所有（安全性クリティカル）

| ファイル | 責務 | 依存する契約 |
|---|---|---|
| `modal_hub/core/risk.py` | ツール種別ごとの正規化 → HIGH/MEDIUM/LOW 分類 | `classify(tool_name: str, tool_input: dict) -> Risk`。`detect_dangerous_command()` は必ず 3 要素にアンパックする |
| `modal_hub/core/risk_rules.yaml` | ルール定義 | 設計書 §4.2 の形 |
| `modal_hub/core/security.py` | 資格情報検証・署名・CSRF・WS チケット・レート制限 | `hmac.compare_digest` 必須。`source`/`session_id` はトークンから確定（ボディを信じない） |
| `modal_hub/routers/approval_gate.py` | 承認 API 一式 | `status_of()` は**純関数**（副作用禁止）。`decision:` / `lease:` / `idem:` は `put(skip_if_exists=True)` の 1 回勝負 |
| `modal_hub/services/audit.py` | 不変監査ログ | 1 イベント 1 ファイル。書き込み失敗時は `claim` を成功させない |
| `hh_hooks/tool_gate.py` | Claude Code / Hermes 共通フック | **標準ライブラリのみ**（`urllib.request`）。起動〜allow 返却 200ms 未満。フェイルクローズ |
| `modal_hub/core/canonical.py` | canonical JSON とハッシュ（設計書 §3） | **2026-08-11 追記。当初この表から抜けており担当不在だった。** `approval_gate.py` と同一所有者が持つ（`payload_sha256` の生成と照合が同じ規則である必要があるため） |
| `modal_hub/core/redact.py` | §10.3 の redaction（監査と PWA 応答の両方に適用） | 同上。実装は 1 か所のみ |
| `hh_hooks/risk.py` | `core/risk.py` の生成コピー | 手書きしない。ビルドスクリプトで同期し、差分があればテストで落とす |
| `hh_hooks/risk_rules.yaml` | 同上（**YAML も一緒に同期する**） | **2026-08-11 追記。** 当初どちらの設計書にも書かれておらず、実行時に `FileNotFoundError` で発覚した |
| `scripts/sync_hook_modules.py` | 上記の同期スクリプト | `tool_gate.py` と同一所有者 |
| `hh_hooks/INSTALL.md` | `.claude/settings.json` と `cli-config.yaml` への登録手順 | `timeout: 200` と `fail_closed: true` を必ず記載 |

### MiniMax M3 所有

| ファイル | 責務 | 依存する契約 |
|---|---|---|
| `modal_hub/main.py` | Modal App + FastAPI 骨格、ルータ結線、静的配信 | 単一 `@modal.asgi_app()`。`@modal.web_endpoint` を使わない |
| `modal_hub/core/config.py` | Secret / 環境変数の読み込み | 実値をログに出さない |
| `modal_hub/core/store.py` | `modal.Dict` / Volume アクセス層 | 書き込み系は `put(skip_if_exists=True)` のみ公開。`update` 系メソッドを生やさない |
| `modal_hub/services/notifier.py` | ntfy.sh 送信 | **本文に承認権限・コマンド本文を載せない**。opaque ID とリスクレベルのみ |
| `mobile_app/pwa_approval/index.html` | 承認画面の構造 | 外部ホストへのリクエスト禁止（CDN・フォント・画像すべてインライン） |
| `mobile_app/pwa_approval/app.js` | 一覧取得・WS・カウントダウン・承認/却下 | WS 接続中も 10 秒ポーリングで突き合わせる。カウントダウンは `grace_deadline` からクライアント側で計算 |
| `mobile_app/pwa_approval/style.css` | スタイル | ダークモード対応。ボタン最小 56px 高 |
| `mobile_app/pwa_approval/manifest.webmanifest`, `sw.js` | PWA 化 | SW はオフライン表示のみ。承認処理を SW に入れない |
| `modal_hub/tests/*` | 単体テスト一式 | 設計書 §8.1 の項目を全部埋める |

### Codex 所有

| 作業 | 備考 |
|---|---|
| Modal Secret `hh-agent-secret` の作成 | 実値はコード・Obsidian・Git に書かない |
| 全コードのレビュー | `codex exec review --uncommitted` |
| GitHub push | Claude Code は直接 push しない |

---

## Phase 1b: Skill Distiller

**Distiller はローカルで動く**（D-17）。SessionDB が `~/.hermes/state.db` にあるため。Modal へは生成済み SKILL.md を publish するだけ。**実装契約は `docs/hh-agent/07_Phase1b_Spec.md`。起動契機・Batch 回収・類似度判定・promote 手順はすべて同ファイルで確定済み、実装者は変更しない。**

| ファイル | 所有者 | 責務 |
|---|---|---|
| `modal_hub/services/session_reader.py` | Sonnet 5 | **ローカル実行**。`SessionDB.get_session()` / `get_messages()` の読み取り。`active=1` の扱い、**`timestamp` ではなく `id` 順**を間違えない |
| `hh_hooks/journal.py` | Sonnet 5 | `post_tool_call` の `status`/`error_type`/`duration_ms` をジャーナルへ追記。**フェイルオープンでよい** |
| `hh_hooks/session_end_distill.py` | Sonnet 5 | `on_session_end` フック。キュー登録のみ（`07_Phase1b_Spec.md` §1.1）。Batch 投入はしない・フェイルオープン |
| `modal_hub/services/skill_quarantine.py` | **Sonnet 5** | 隔離保存・`skill_name` 検証・パス封じ込め・原子的書き込み（**安全性クリティカル。D-16**） |
| `scripts/hh_skill_promote.py` | **Sonnet 5** | `promote` CLI。全文表示・TTY 確認必須・非対話は拒否（`07_Phase1b_Spec.md` §4.2） |
| `modal_hub/routers/skills.py` | **Sonnet 5** | 新規 `POST /api/skills/publish`。`publish` スコープのトークン検証（`07_Phase1b_Spec.md` §5） |
| `modal_hub/services/skill_distiller.py` | MiniMax | 抽出条件 4 つの判定（**判定根拠は journal の `status` のみ。`end_reason` を使わない**）、Haiku 4.5 Batch API 呼び出し、SKILL.md 生成、既存スキル一覧との重複判定（`07_Phase1b_Spec.md` §3） |
| `scripts/hh_distill.py` | MiniMax | `run` / `retry` / `status` CLI。状態機械（`pending`→`submitted`→`completed`/`failed`）は `07_Phase1b_Spec.md` §2 のとおり実装し変更しない |
| `modal_hub/services/memory_bridge.py` | MiniMax | Corpus2Skill MCP の読み取りクライアント。`add_new_memory` を実装しない |
| `modal_hub/tests/test_distiller.py` | MiniMax | Obsidian 漏洩パス検証、`<name>/SKILL.md` 形式、**昇格後に Hermes 実パーサで発見されること**、**昇格前は一切発見されないこと**。親設計書 §8.1 の4項目必須 |

---

## Phase 1c: Modal クラウドエージェント

**PoC 通過まで担当を割り当てない。** PoC は Sonnet 5 が実施する（設計書 §4.6 の合否条件 2 つ）。

---

## 実装者への共通指示（プロンプトに必ず含める）

1. 親設計書 `docs/hh-agent/03_Architecture.md` と、Phase 1a なら `docs/hh-agent/05_Phase1a_Spec.md` を**先に読む**。原指示書と食い違ったら設計書が優先。
1b. **`05_Phase1a_Spec.md` に書かれた値・順序・エラーコードを変更しない。** 「こちらの方が自然」と思っても変えない。理由がある場合は BLOCKED として報告する。
2. **自分の所有ファイル以外を編集しない。** 変更が必要なら報告する。
3. **仕様の穴を推測で埋めない。** BLOCKED として報告する。
4. **エラーを握りつぶさない。** 想定外のレスポンス形状は例外を投げる。黙って空を返さない。
5. 承認系はすべて**フェイルクローズ**。Distiller 系は**フェイルオープン**。
6. 長時間実行を起動したら**完了まで在席し、成果物の実体で成否を検証する**。起動直後に「監視待ち」で終了しない。
7. `git push` はしない。commit も指示があるまでしない。
