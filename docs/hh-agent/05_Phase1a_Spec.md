# H-H Agent Phase 1a 詳細仕様（実装契約）

- **最終更新**: 2026-08-11
- **親設計書**: `docs/hh-agent/03_Architecture.md`（食い違う場合は親設計書が優先）
- **版**: v2（2026-08-11 Codex 実装契約レビュー 6 件 ＋ 自己点検 4 件 ＋ Windows 実機検証を反映）
- **位置づけ**: 親設計書 §11 が「実装着手前に埋めること」として挙げた Phase 1a 分 10 件を確定させたもの。**ここに書かれていることは実装者が変更してよい判断ではない。** 不足を見つけたら BLOCKED として報告すること。

### v1 から破棄した設計（同じ誤りを繰り返さないために残す）

| 破棄したもの | 理由 |
|---|---|
| 同一キー `pairing:<hash>` の作成と `skip_if_exists` 消費 | **正規の初回ペアリングが必ず 409 で失敗する論理バグ**。オファーと使用済みマーカーを別キーに分けた（§7.1） |
| `revoked:*` による失効の墓標 | 保存先の TTL で墓標が先に消えると**失効済み資格情報が復活する**。肯定リスト方式へ変更（§11） |
| 「lease を焼いて 500」 | 応答喪失でも lease が永久消費され回復不能。同一 `sub` を名乗れる盗難トークンからの DoS も成立する。`claim_attempt_id` による冪等再取得へ変更（§1.4） |
| 監査ファイル名の `rand8` | リトライで**同一事象が重複記録される**。決定的 `event_id` へ変更（§10.1） |
| 表内に正規表現を書く | Markdown のパイプエスケープ `\|` が**リテラルの縦棒として実装される**。コードブロックへ移した（§10.3） |
| クライアントから自由文 `reason` を受け取る | そのまま PWA へ表示され redaction の抜け道になる。`reason_code` に変更（§1.2） |

---

## 1. HTTP API 契約

### 1.1 共通規約

- **Base URL**: Modal が払い出す `https://<workspace>--hh-agent-hub-fastapi-app.modal.run`
- **Content-Type**: `application/json; charset=utf-8`
- **時刻**: 応答に絶対時刻を含めない。**すべて相対秒（`*_remaining_seconds`）で返す**（親設計書 §4.4 の時計規則）。
- **エラー本文**（全エラー共通）:
  ```json
  {"error": {"code": "SNAKE_CASE_CODE", "message": "human readable", "retryable": true}}
  ```
- **`retryable` の判定は応答本文の値を正とする。** クライアントは HTTP ステータスから推測しない。

| HTTP | 意味 | `retryable` |
|---|---|---|
| 200 | 成功 | — |
| 201 | 承認要求を新規作成 | — |
| 400 | スキーマ不正 | false |
| 401 | 認証失敗 | false |
| 404 | 対象が無い、**または所有権が一致しない** | false |
| 409 | write-once キーの衝突（既に決着済み等） | false |
| 413 | ペイロード上限超過 | false |
| 422 | 意味的に不正（期限切れの決定など） | false |
| 429 | レート制限 | **true**（`Retry-After` 秒を尊重） |
| 500 / 502 / 503 / 504 | サーバ側障害 | **true** |
| 接続エラー / タイムアウト | — | **true** |

**リトライ方針**: 最大 3 回、初回 0.5 秒・以後 2 倍・上限 4 秒のジッタ付き指数バックオフ。ただし**残り時間予算を超えるリトライは行わない**。

### 1.2 `POST /api/approval/request`

**認証**: Agent Bearer

**Request**

```json
{
  "idempotency_key": "string, 16..128 chars, [A-Za-z0-9._:-]+",
  "tool_name": "Bash",
  "payload": { "command": "rm -rf ./build" },
  "payload_sha256": "64-hex",
  "payload_raw_sha256": "64-hex",
  "context": {
    "cwd": "C:/Users/Haruki/Projects/Foo",
    "workspace_id": "64-hex",
    "base_revision": "40-hex or null"
  },
  "risk": "HIGH",
  "rule_id": "force_push",
  "reason_code": "force_push",
  "targets": [
    {
      "path": "C:/Users/Haruki/Projects/Foo/out.txt",
      "realpath": "C:/Users/Haruki/Projects/Foo/out.txt",
      "identity": "17735206716449772873:562949955562867",
      "preimage_sha256": "64-hex or null",
      "exists": true
    }
  ]
}
```

- **`payload_sha256` / `payload_raw_sha256` は request で必須**（64 桁 hex）。これが**比較の基準**になる。サーバはこれを**保存するだけで、再計算しない** — Modal は Linux で動いており、Windows のパスに対する `os.path.realpath()` の結果が一致しないため、サーバ側での再計算は恒常的な `MISMATCH` を生む。
- **`identity` は文字列**（§3 の実測理由）。数値にしない。
- **`reason` ではなく `reason_code`（`rule_id` と同じ語彙）を送る。** 自由文の理由をクライアントから受け取らない — 自由文はそのまま PWA へ表示され、redaction の抜け道になる。表示文言はサーバが `rule_id` から引く。
- `targets` は `Write` / `Edit` 等ファイル対象操作のみ。`Bash` では空配列。
- `payload` は 4 KB、`targets` は 32 件、全体で 64 KB を超えたら 413。超えた場合クライアントは**切り詰めずに HIGH のまま deny する**（切り詰めた内容で承認を求めると、ユーザーが見ていない部分が実行される）。

**Response 201 / 200**（200 は idempotent 再利用）

```json
{
  "approval_id": "uuid4",
  "grace_remaining_seconds": 150,
  "claim_remaining_seconds": 180,
  "reused": false,
  "notify_state": "sent" | "pending" | "failed"
}
```

`notify_state` は `/poll` の `notify_state` と**同一の語彙**を使う（`notify_failed` のような別名を作らない）。`"pending"` は「まだ試行中／再試行余地あり」、`"failed"` は「規定回数すべて失敗し、以後再試行しない」。

**サーバ側の処理順序（この順序を守る）**

1. 認証・レート制限。
2. `idem:<subject>:<key>` を**読む**。存在すれば既存の `approval_id` を取得し、**その `req:` の `source`/`session_id`/`workspace_id` が今回の subject と一致するか確認**（不一致は 404）。以降は手順 7 へ（既存を返す）。
3. `approval_id` を採番し、**先に `req:<id>` を作成する**（`created_at`、`grace_deadline = created_at+150`、`claim_deadline = created_at+180` を含む）。
4. **その後で** `idem:<subject>:<key>` を `put(skip_if_exists=True)` する。
   - 失敗（他の並行リクエストが先に作った）→ そちらの `approval_id` を採用し、手順 3 で作った `req:` は孤児として GC に任せる（**利用者からは見えないので害はない**）。
5. `pending:index` と `gc:index:<YYYY-MM-DD>` に追加。
6. 監査 `requested` を outbox 経由で書く。
7. ntfy 送信（`notify:<id>` を更新）。既存再利用の場合も `notify:` が `sent` でなければ送る。
8. 応答。

**`req:` を `idem:` より先に作る理由**: 逆順にすると、`idem:` を作った直後にコンテナが停止した場合、**存在しない `approval_id` を指す idempotency レコードが残り、以後そのキーでのリトライが永久に 404 になる**（回復不能）。先に `req:` を作れば、最悪でも「参照されない `req:` が 1 つ残る」だけで済む。

**再利用時も `notify:<id>` が `sent` でなければ再送する。**

**「更新」という語に引きずられないこと（2026-08-11 確定）**: `store` に `overwrite` / `update` 系の原子操作は**存在しない**（意図的にそう設計している）。`notify:<id>` も `put(skip_if_exists=True)` の**書き込み 1 回勝負**であり、レコードは終端状態（`sent` または `failed`）でのみ書く。「レコードが無い＝未通知」である。

したがって **3 回の送信がすべて失敗して `failed` が書かれたら、その approval_id の通知状態は以後変わらない**（sticky）。これは仕様どおりの挙動である。

| | |
|---|---|
| 帰結 | `poll` が `notify_failed: true` を返し、エージェントは deny する |
| 評価 | **正しい。フェイルクローズしている** |
| 復旧 | ユーザーがコマンドを実行し直す（新しい approval_id が振られる）。コストはゼロ |

**`store.py` に `overwrite()` を生やしてはならない。** 承認状態機械そのものが「書き込み 1 回勝負」であることに依存しており、同じストアに read-then-write の経路を作ることは、この設計が回避するために作られたバグ類型そのものを持ち込む。通知の再送性のために状態機械の安全性を下げる取引はしない。

### 1.3 `GET /api/approval/poll?id=<approval_id>`

**認証**: Agent Bearer ＋ 所有権照合

**Response 200**

```json
{
  "approval_id": "uuid4",
  "status": "pending" | "approved" | "rejected" | "timeout" | "claimed",
  "grace_remaining_seconds": 42,
  "claim_remaining_seconds": 72,
  "notify_state": "sent" | "pending" | "failed",
  "decided_by": "pwa" | "system" | null
}
```

- `notify_state == "failed"` を受け取ったクライアントは**待機をやめて即 deny する**（通知が届いていないのでユーザーは承認しようがない）。`"pending"` では待ち続ける。
- `status` は親設計書 §4.3 の `status_of()` の出力そのもの。**サーバは poll で状態を書き換えない**（唯一の例外はタイムアウトの write-once 記録、§1.7）。

### 1.4 `POST /api/approval/claim`

**認証**: Agent Bearer ＋ 所有権照合

**Request**

```json
{
  "approval_id": "uuid4",
  "claim_attempt_id": "uuid4",
  "verification": {
    "payload_sha256": "64-hex",
    "payload_raw_sha256": "64-hex",
    "context": {
      "cwd": "C:/Users/Haruki/Projects/Foo",
      "workspace_id": "64-hex",
      "base_revision": "40-hex or null"
    },
    "targets": [ ...request と同一形式・同一順序... ]
  }
}
```

**照合する項目（すべて。1 つでも欠けたら 400、1 つでも違ったら 422 `MISMATCH`）**

| 項目 | 照合方法 |
|---|---|
| `payload_sha256` | 文字列一致 |
| `payload_raw_sha256` | 文字列一致 |
| `context.cwd` | 文字列一致 |
| `context.workspace_id` | 文字列一致。**claim 時にフックが再計算した値**であること（トークン内の発行時の値ではない） |
| `context.base_revision` | 文字列一致。両方 `null` なら一致とみなす |
| `targets` | **件数と順序が一致**し、各要素の `path` / `realpath` / `identity` / `preimage_sha256` / `exists` がすべて一致 |

`Bash` のように `targets` が空でも、**`context` の 3 項目は必ず照合する**。コマンド文字列が同じでも、`cwd` や `HEAD` が変われば実行結果はまったく別物になる（例: `git reset --hard` を コミット A で承認した後に HEAD が動く）。

**Response 200**: `{"granted": true, "lease_id": "uuid4"}`

**エラー**

| 条件 | HTTP | code |
|---|---|---|
| `status != "approved"` | 409 | `NOT_APPROVED` |
| `claim_deadline` 超過 | 422 | `CLAIM_EXPIRED` |
| `lease:` が既存 | 409 | `ALREADY_CLAIMED` |
| 検証項目の不一致 | 422 | `MISMATCH`（不一致項目名を `message` に含める） |
| 監査 outbox 登録失敗 | 500 | `AUDIT_FAILED`（`retryable: true`。同一 `claim_attempt_id` でのリトライは安全） |

**`ALREADY_CLAIMED`（`claim_attempt_id` 不一致）を受け取ったクライアントは deny する。** 他者が実行権を取得しているため、自分が実行してはならない。

**`claim_attempt_id` による冪等化（v1 の「lease を焼いて 500」は破棄する）**

v1 は「lease を取った後に監査が失敗したら 500 を返す。lease は焼けるが安全側」としていた。これは**回復不能**である:

- 監査成功後・HTTP 応答到達前にコンテナが停止した場合も lease は消費済みになる。クライアントが正当にリトライしても `ALREADY_CLAIMED` が返り、**自分が取った lease なのか他人が取ったのか区別できない**ため実行できない。
- 「所有者しか焼けないから DoS ではない」という v1 の説明も**誤り**。§6.3 で「同一ユーザー権限のプロセスによるトークン窃取は防げない」と受容している以上、盗んだトークンは同じ `sub` として照合を通る。ユーザーの承認直後に先回りして claim し応答を捨てれば、そのセッションの承認を繰り返し無効化できる。

**修正: lease レコードに `claim_attempt_id` と `lease_id` を保存し、同一 attempt の再試行には同じ lease を返す。**

```json
// lease:<approval_id>
{"lease_id": "uuid4", "claim_attempt_id": "uuid4",
 "claimed_at": 1786000000.0, "claimant_sub": "..."}
```

**処理順序**

1. 所有権を照合（`sub` が `req:` と一致）。
2. 導出状態と期限を確認（§2 の優先順位に従う）。
3. `verification` を `req:` と突き合わせる。**不一致なら監査 `mismatch` を書いてから** 422 を返す。
4. `lease:<id>` を `put(skip_if_exists=True)`。
   - **成功** → 新規取得。手順 5 へ。
   - **失敗（既存）** → 既存レコードを読み、
     - `claim_attempt_id` が**今回と同じ** → **同じ `lease_id` を 200 で返す**（＝応答喪失からの正当な回復）。
     - 異なる → 409 `ALREADY_CLAIMED`。
5. 監査 `claim_granted` を**永続 outbox 経由で**書く（§10.4）。outbox への登録が成功すれば 200 を返す。

**`claim_attempt_id` はクライアントが「1 回の実行意図」につき 1 つ生成し、リトライ間で変えない。** ツール呼び出し ID から決定的に導出してよい。

**監査ストアが落ちている場合**: outbox への登録も失敗したら 500 `AUDIT_FAILED` を返し、クライアントは deny する。この場合 lease は取得済みだが `claim_attempt_id` が一致するため、ストア復旧後のリトライで同じ lease を受け取って実行できる。**焼き切らない。**

### 1.5 `POST /api/approval/complete`

**認証**: Agent Bearer ＋ 所有権照合 ＋ `lease_id` 一致

**Request**: `{"approval_id":"...", "lease_id":"...", "outcome":"consumed"|"failed"|"mismatch", "detail":"string, <=1KB"}`

**Response 200**: `{"recorded": true}`。重複 `complete` は 200（冪等）で `{"recorded": false, "already": true}`。

### 1.6 `GET /api/approval/pending` / `POST /api/approval/respond` / `WS /ws/approval`

**認証**: PWA Cookie（`respond` は ＋ CSRF、WS は ＋ 単回チケット）

`GET /api/approval/pending` → `{"items":[{approval_id, tool_name, risk, rule_id, reason, summary, grace_remaining_seconds}]}`
`summary` は表示用の要約（コマンド先頭 200 文字、redaction 済み）。**全文と差分は個別取得**（`GET /api/approval/detail?id=`）。

`POST /api/approval/respond` → Request `{"approval_id":"...", "decision":"approved"|"rejected", "csrf":"..."}`

| 条件 | HTTP | code |
|---|---|---|
| `decision:` が既存 | 409 | `ALREADY_DECIDED` |
| `grace_deadline` 超過 | 422 | `GRACE_EXPIRED` |
| CSRF 不正 / Origin 不一致 | 403 | `CSRF_FAILED` |

**`decision` レコードには必ず `at`（サーバ時刻）を入れる。** `status_of()` が `at > grace_deadline` を無効化するのに使う。

### 1.7 タイムアウトの一度きり記録

`poll` / `pending` / `respond` のいずれかで「`decision:` が無く、かつ `now > grace_deadline`」を観測した者が、`decision:<id>` に `{"decision":"timeout","at":<grace_deadline>,"by":"system"}` を `put(skip_if_exists=True)` する。**成功した者だけが監査 `timed_out` を 1 行書く。** 失敗した者（他が先に書いた）は何もしない。

`at` に `now` ではなく `grace_deadline` を入れるのは、`status_of()` の `at > grace_deadline` 判定に自分で引っかからないようにするため。

---

## 2. 状態遷移表（正式版）

| 現在 | イベント | 条件 | 次 | 副作用 |
|---|---|---|---|---|
| （無） | `request` | idem 新規 | `pending` | `req:` 作成、通知送信、監査 `requested` |
| （無） | `request` | idem 既存・所有者一致 | 現状維持 | `notify:` 未成功なら再送 |
| （無） | `request` | idem 既存・**所有者不一致** | — | **404**。何も返さない |
| `pending` | `respond(approved)` | `now <= grace_deadline` | `approved` | `decision:` write-once、監査 `approved` |
| `pending` | `respond(rejected)` | 同上 | `rejected` | `decision:` write-once、監査 `rejected` |
| `pending` | `respond(*)` | `now > grace_deadline` | `timeout` | 422 `GRACE_EXPIRED`。§1.7 の write-once も同時に行う |
| `pending` | `claim` | — | 変化なし | **409 `NOT_APPROVED`**（まだ承認されていない） |
| `pending` | `complete` | — | 変化なし | **409 `NOT_CLAIMED`** |
| `pending` | 観測（poll 等） | `now > grace_deadline` | `timeout` | §1.7 の write-once 記録 |
| `approved` | `claim` | `now <= claim_deadline` かつ検証一致 | `claimed` | `lease:` write-once、監査 2 行 |
| `approved` | `claim` | `now > claim_deadline` | `timeout` | 422 `CLAIM_EXPIRED` |
| `approved` | `claim` | 検証不一致 | `approved` のまま | 422 `MISMATCH`、監査 `mismatch` |
| `approved` | 観測 | `now > claim_deadline` | `timeout` | 導出のみ（`status_of()`） |
| `claimed` | `complete` | `lease_id` 一致 | `claimed`（終端） | 監査 `consumed`/`failed`/`mismatch` |
| `claimed` | `claim` | — | — | 409 `ALREADY_CLAIMED` |
| `approved` | `complete` | — | 変化なし | 409 `NOT_CLAIMED`（lease を取らずに完了報告はできない） |
| `claimed` | `respond` | — | 変化なし | 409 `ALREADY_DECIDED` |
| `rejected` | 任意 | — | 変化なし | `respond` → 409 `ALREADY_DECIDED` / `claim` → 409 `NOT_APPROVED` / `complete` → 409 `NOT_CLAIMED` |
| `timeout` | 任意 | — | 変化なし | `respond` → 422 `GRACE_EXPIRED` / `claim` → 422 `CLAIM_EXPIRED` / `complete` → 409 `NOT_CLAIMED` |

**表に無い (状態, イベント) の組は存在しない。** 実装は網羅した `match` / 辞書ディスパッチで書き、未知の組み合わせに落ちたら **500 を返して監査に `unexpected_transition` を記録する**（黙って素通りさせない）。

### 2.1 エラー判定の優先順位（曖昧だと同じ状況で違うコードが返る）

`status_of()` を先に評価すると「期限切れの approved」は `timeout` になるため、`CLAIM_EXPIRED` ではなく `NOT_APPROVED` に落ちてしまう。**判定は必ず次の順序で行い、最初に該当したものを返す。**

**`claim` の判定順序**

1. 所有権不一致 → **404**
2. `lease:` が存在し `claim_attempt_id` 一致 → **200**（冪等な再取得）
3. `lease:` が存在し `claim_attempt_id` 不一致 → **409 `ALREADY_CLAIMED`**
4. `decision:` が無い → **409 `NOT_APPROVED`**
5. `decision.decision != "approved"` → **409 `NOT_APPROVED`**（rejected / timeout）
6. `decision.at > req.grace_deadline` → **422 `GRACE_EXPIRED`**（遅延して入った決定）
7. `now > req.claim_deadline` → **422 `CLAIM_EXPIRED`**
8. `verification` 不一致 → 監査 `mismatch` → **422 `MISMATCH`**
9. 以上を通過 → lease 取得へ

**`respond` の判定順序**

1. CSRF / Origin 不正 → **403 `CSRF_FAILED`**
2. `req:` が無い → **404**
3. `decision:` が既存 → **409 `ALREADY_DECIDED`**（`decision.decision` が `timeout` でも同じ。**再送で 422 と 409 が入れ替わらないようにするため、既存判定を期限判定より先に置く**）
4. `now > req.grace_deadline` → §1.7 の write-once を行ってから **422 `GRACE_EXPIRED`**
5. 以上を通過 → `decision:` 書き込みへ

**`complete` の判定順序**

1. 所有権不一致 → **404**
2. `lease:` が無い → **409 `NOT_CLAIMED`**
3. `lease.lease_id != request.lease_id` → **409 `NOT_CLAIMED`**
4. 同一 `lease_id` で既に `complete` 済み → **200**（冪等。`{"recorded": false, "already": true}`）
5. 以上を通過 → 記録

**§1.4 / §1.6 のエラー表と本節が食い違う場合、本節が優先する。**

**`decision:` が `timeout` として書かれている場合の `status_of()`**: `decision["decision"] == "timeout"` は `"approved"` ではないので、そのまま `timeout` が返る（§`status_of()` の `if decision["decision"] != "approved"` 分岐）。二重に期限判定する必要はない。

**lease 保持者がクラッシュした場合**: lease は解放されない（`claim_deadline` を過ぎれば `status_of()` は `claimed` を返し続けるが、`complete` が来ないだけ）。**意図的にそうしている。** 解放すると「1 回の承認で 2 回実行される」経路ができる。ユーザーは再度操作すれば新しい承認要求が飛ぶ。GC は 7 日後に回収する。

---

## 3. Canonical JSON とハッシュ

`payload_sha256` および `preimage_sha256` の計算は**フックとサーバで完全に同一の実装**でなければならない。実装は `modal_hub/core/canonical.py` の 1 か所に置き、`hh_hooks/` へは生成コピーで配る（親設計書 §3 の `risk.py` と同じ扱い）。

```python
def canonical_json(obj) -> bytes:
    return json.dumps(
        _normalize(obj),
        sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
```

`_normalize()` の規則:

| 型 | 規則 |
|---|---|
| str | **Unicode NFC 正規化** |
| int / bool / None | そのまま |
| **float** | **禁止。含まれていたら例外を投げる**（表現の揺れでハッシュが一致しなくなるため） |
| list | 順序を保持したまま各要素を正規化 |
| dict | **キーが `str` でなければ例外を投げる。** 実測: `json.dumps({True:'a'})` → `{"true": "a"}`、`json.dumps({1:'b'})` → `{"1": "b"}`、さらに `{True:'a', 1:'b'}` は Python 上で**キー 1 個に潰れる**。非文字列キーを許すとハッシュが一意にならない。キーは NFC 正規化してソート |

**「パス文字列」は値を見て判別してはならない。** どのフィールドがパスかは**スキーマで型として宣言する**（`PathStr` 型）。値の見た目（`C:` で始まる等）で判定する実装は、フィールドごとに挙動が食い違う。

| 宣言型 | 正規化 |
|---|---|
| `PathStr` | `os.path.realpath()` → `\` → `/` 置換。**大文字小文字は変換しない** |
| `Str` | NFC 正規化のみ。パス解決を行わない |

`PathStr` として宣言するフィールド: `context.cwd`、`targets[].path`、`targets[].realpath`。それ以外の文字列はすべて `Str`。

**`PathStr` の正規化は必ずフックのローカル環境（Windows）で行う。** サーバ（Modal / Linux）で `os.path.realpath("C:/Users/...")` を評価すると `/root/C:/Users/...` のような別物になるため、サーバ側では**一切パス正規化を行わない**（受け取った文字列をそのまま保存・比較するのみ）。

**UNC・device path（`\\?\C:\...`）・8.3 短縮名は同一対象の別表現である。** これらが混在するとハッシュが一致しない。フックは常に `os.path.realpath()` の出力だけを使い、生の入力綴りをそのまま送らないこと。

**パスの大文字小文字について（Windows 固有・本環境で実測確認済み 2026-08-11）**

Python の `os.path.realpath()` は Windows で、**実在する構成要素は正準な大文字小文字へ変換し、実在しない構成要素は入力の綴りをそのまま残す**。実測:

| 入力 | 出力 |
|---|---|
| `casetest/mixedcase.txt`（実在） | `...\CaseTest\MixedCase.txt` ← 全体が正準化 |
| `casetest/doesnotexist.txt`（葉が不在） | `...\CaseTest\doesnotexist.txt` ← ディレクトリのみ正準化、葉は入力のまま |

したがって:

- 既存ファイルへの `Edit` — request 時も claim 時も正準形になり一致する。
- 新規ファイルの `Write` — 葉が両時点で不在なので入力綴りが保たれ、**フックが同一の入力文字列を使う限り**一致する。
- **request 後 claim 前に第三者がそのパスにファイルを作った場合** — claim 時だけ葉が正準形に変わり `MISMATCH` になる。**これは望ましい挙動**（承認時に無かったファイルが現れている＝環境が変わっている）なので、緩和しない。

**`identity` は必ず文字列で持つ（数値にしてはならない）**

`identity` は `f"{st_dev}:{st_ino}"` の**文字列**とする。本環境の実測で `st_dev` は `17735206716449772873`（約 2^63.9）を返した。これは JavaScript の `Number` が安全に表せる 2^53 を大きく超えるため、JSON の数値として PWA へ渡すと**丸められて別の値になり、照合が壊れる**か、逆に異なるデバイスが同一と誤判定される。

**`os.fstat(fd)` による照合が有効であることも実測済み**: 同一ファイルに対する `os.stat(path)` と `os.open()` 後の `os.fstat(fd)` の `(st_dev, st_ino)` は一致する。親設計書 §4.3 の「open してから fstat で照合する」方式は Windows でも成立する。

**ハッシュは 2 本取る。**

```
payload_sha256     = sha256(canonical_json(payload)).hexdigest()   # 比較用（安定）
payload_raw_sha256 = sha256(json.dumps(payload, sort_keys=True,
                            ensure_ascii=False, separators=(",",":"))
                            .encode("utf-8")).hexdigest()          # 生バイト用
```

NFC 正規化は**単射ではない** — 異なる 2 つの文字列が同じ NFC 形になりうる。canonical ハッシュだけで照合すると、「承認された文字列 A」と「実行される文字列 B」が `NFC(A) == NFC(B)` の関係にある場合を通してしまう。`claim` では**両方のハッシュを照合する**ことでこれを塞ぐ。

**ハッシュを計算する主体**: **フック側**。サーバは受け取ったハッシュを保存するだけで再計算しない（サーバはファイルシステムを見られない）。`claim` 時の照合も**フックが再計算した値**をサーバへ送り、サーバは `req:` の値と文字列比較する。したがってこの仕組みは「フックが正直であること」を前提とする — **エージェントに乗っ取られたフックからは守れない。** 守るのは「承認待ちの 150 秒間に環境が変わること」であり、それが本設計の脅威モデルである。

---

## 4. `workspace_id` の定義

```python
def workspace_id(cwd: str) -> str:
    root = git_toplevel(cwd) or cwd          # git rev-parse --show-toplevel
    real = os.path.realpath(root).replace("\\", "/")
    return hashlib.sha256(real.encode("utf-8")).hexdigest()
```

| 状況 | 扱い |
|---|---|
| Git 管理下 | `git rev-parse --show-toplevel` の結果を使う |
| Git 管理外 | `cwd` をそのまま使う |
| `git` コマンドが失敗（未インストール等） | `cwd` にフォールバックし、`base_revision` を `null` にする。**エラーにしない** |
| `cwd` が symlink / junction の先 | `realpath` で解決した実体パスを使う |
| ネットワークドライブ / UNC パス | `realpath` の結果をそのまま使う。特別扱いしない |

`base_revision` = `git rev-parse HEAD` の 40 桁 hex、取得できなければ `null`。**`base_revision` が `null` の場合、`claim` 時の照合はこの項目をスキップする**（`null` == `null` を一致とみなす）。Git 管理外での作業をブロックしないため。

---

## 5. `risk_rules.yaml` スキーマとツール名の正規化

### 5.1 スキーマ

```yaml
version: 1

# ツール名の正規化。Claude Code と Hermes で名前が違うものを1つに寄せる。
tool_aliases:
  shell:    [Bash, terminal, run_terminal_cmd, shell]
  write:    [Write, write_file, create_file]
  edit:     [Edit, MultiEdit, str_replace_editor, edit_file]
  notebook: [NotebookEdit]
  read:     [Read, read_file, view, Glob, Grep, LS, list_dir, codebase_search]

# 読み取り専用として素通りさせるカテゴリ。ここに無いものは副作用ありとみなす。
read_only_categories: [read]

high:
  - id: force_push
    applies_to: [shell]
    pattern: '\bgit\s+push\b.*(--force|-f)\b'
    reason: "履歴を破壊する"
  - id: any_push
    applies_to: [shell]
    pattern: '\bgit\s+push\b'
    reason: "外部公開。ユーザールール上 Codex 経由が原則"
  - id: secret_path
    applies_to: [write, edit, shell]
    path_pattern: '(^|/)(\.env(\.[^/]+)?|secrets?/|\.git/|\.ssh/|settings\.json)$'
    reason: "認証情報・設定への到達"

medium:
  - id: pkg_install
    applies_to: [shell]
    pattern: '\b(npm|pnpm|yarn|pip|uv)\s+(install|add)\b'
```

- ルールは `high` → `medium` の順に評価し、**最初にマッチしたものを採用**する。
- `pattern` は正規化済みコマンド文字列に対して、`path_pattern` は各 target の `realpath` に対して適用する。
- 未マッチは `LOW`。

#### 5.1a **上のスキーマ例には誤りがある**（2026-08-11・テストが発見）

**上の `risk_rules.yaml` 例をそのまま実装に写してはならない。** テスト実装が 3 件の偽陰性を検出した。

**(1) `secret_path` の `$` の位置が誤っている（最も影響が大きい）**

```
(^|/)(\.env(\.[^/]+)?|secrets?/|\.git/|\.ssh/|settings\.json)$
```

`$` が選択肢全体に掛かるため、`secrets/` / `.git/` / `.ssh/` は**末尾がスラッシュの文字列にしか一致しない**。結果、次がすべて **LOW** に落ちていた。

| 例 | 本来 |
|---|---|
| `Write` → `proj/secrets/api_key.pem` | HIGH |
| `Edit` → `~/.ssh/id_rsa` | HIGH |
| `.git/config` | HIGH |
| `cat /home/h/.ssh/id_rsa` | HIGH |

**規則**: ディレクトリ形式の選択肢（`secrets/` `.git/` `.ssh/`）は**パス中のどこかのセグメント**に一致させる。ファイル形式（`.env` `.env.local` `settings.json`）は最終要素に一致させる。`secretsomething` のような過剰一致を作らないこと。

**(2)(3) `sudo` と外部への `curl -X POST` にルートが存在しなかった**

親設計書 §4.2 は両方を HIGH の例として挙げているが、`risk_rules.yaml` に**ルールが 1 つも無く**、Hermes の検出器も拾わない。どちらも LOW だった。HIGH ルールを追加する。

- `sudo`: `sudoku` 等に誤爆しないこと
- 外部への POST/PUT: `curl` / `wget` 相当を対象とする。**GET は対象外**（データ持ち出しが論点であり、取得は別）

**教訓**: 設計書の表に書いた正規表現は、**実装が書き写した時点では誰も実行していない**。§5.1b(2) と同じ類型の事故が、同じ 1 本の正規表現で 2 回起きている。ルールは必ず「一致すべき例」と「一致してはならない例」を対にしてテストへ落とす。

#### 5.1b 実装時に確定した 3 点（2026-08-11・実装からの指摘を受けて追記）

**(1) `write` / `edit` / `notebook` の対象パスを `tool_input` のどのキーから取るか**

Claude Code と Hermes でキー名が違う。次の順に探索し、**どれも無ければ `ValueError` を送出する**（推測で空扱いにしない）。

```
file_path        # Claude Code
  → path         # 本リポジトリ tools/file_operations.py
  → notebook_path
```

**(2) `path_pattern` を shell コマンドへ適用するときはトークン化してから照合する**

`path_pattern` はパス末尾に `$` で錨を打っているため、**生のコマンド文字列へ `re.search` してもまず一致しない**（`"cat .env"` が `secret_path` をすり抜ける）。`applies_to` に `shell` を含む `path_pattern` ルールは、コマンドを `shlex` で分割し（引用符が不均衡なら空白分割へフォールバック）、**各トークンに対して照合する**。

**(3) Hermes 検出器のヒットは `rule_id` を 1 個に固定する**

`detect_dangerous_command()` は自由文の説明を返す。これをそのまま `rule_id` にすると §1.2 が前提とする「`rule_id` は閉じた語彙」が壊れる。

- `rule_id` は必ず **`hermes_dangerous_command`** に固定する。
- Hermes の自由文は `reason` にのみ入れる（`reason` はワイヤに乗らない）。

### 5.2 未知のツールの扱い（安全性クリティカル）

```python
category = alias_lookup(tool_name)     # None なら未知

if category is None:
    return Risk("HIGH", "unknown_tool",
                f"未知のツール {tool_name} は副作用ありとみなす")
if category in read_only_categories:
    return Risk("LOW", "read_only", "")
# 以降、ルール評価
```

**MCP ツール・カスタムツールはすべて「未知」に落ちて HIGH になる。** これは意図した挙動である。読み取り専用と確認できたものだけを `tool_aliases.read` へ**人間が明示的に追加**する。自動学習・自動追加を実装しない。

### 5.3 `risk.py` の複製同期

`modal_hub/core/risk.py` と `hh_hooks/risk.py` は同一内容でなければならない。

- 正は `modal_hub/core/risk.py`。
- `scripts/sync_hook_modules.py` が `core/risk.py` と `core/canonical.py` を `hh_hooks/` へコピーし、先頭に `# GENERATED FILE - DO NOT EDIT` を付与する。
- **`modal_hub/tests/test_hook_module_sync.py` が両者の差分を検出したら失敗させる。** 手で編集された複製を CI で落とす。

---

## 6. エージェントトークン

### 6.1 形式

```
hha1.<base64url(payload_json)>.<base64url(hmac_sha256(HH_AGENT_TOKEN_SIGNING_KEY, header+payload))>
```

`payload_json`:

```json
{
  "tid": "uuid4",
  "sub": "claude_code:desktop-haruki",
  "source": "claude_code",
  "session_id": "opaque",
  "workspace_id": "sha256hex",
  "iat": 1786000000,
  "exp": 1786086400
}
```

- **有効期限 24 時間。**
- `sub` / `source` / `session_id` / `workspace_id` は**トークンから読む**。リクエストボディの同名フィールドは**検証にのみ使い、不一致なら 400**。ボディの値を採用しない。
- **有効性は `agent_session:<tid>` の存在と `exp` で判定する（§11）。** 失効は同キーの削除。墓標方式は使わない。

### 6.2 発行・更新・保存

| 項目 | 内容 |
|---|---|
| 発行 | `hh auth login` がペアリングコードを使って Hub から取得 |
| 保存先 | `%USERPROFILE%\.hh-agent\agent_token.json`（**環境変数に置かない**） |
| 更新 | 有効期限の 2 時間前を切っていたらフック起動時に自動更新。更新失敗時は既存トークンで続行し、期限切れなら deny |
| 失効 | `hh auth revoke` が `agent_session:<tid>` を**削除**する |
| ローテーション | 署名鍵は `HH_AGENT_TOKEN_SIGNING_KEY` と `HH_AGENT_TOKEN_SIGNING_KEY_PREV` の 2 本を持ち、検証は両方で試す。発行は新しい方のみ。鍵交換後 24 時間で `_PREV` を削除する |

### 6.3 子プロセスへの継承 — **完全には防げない。その前提で設計する**

環境変数に置かないのは「エージェントが `env` を読んで盗む」経路を塞ぐためだが、**トークンファイルは同一ユーザー権限で読める**。Windows で ACL を分けても、エージェント自身が同じユーザーで動いている以上、原理的に隠しきれない。

したがって**盗まれた場合の被害を限定する設計にする**:

1. **スコープが狭い** — エージェントトークンでは `respond` を呼べない。**自分で承認することは絶対にできない。**
2. **オブジェクト所有権照合** — 盗んだトークンでも、そのトークンの `session_id` / `workspace_id` に紐づく承認しか触れない。
3. **レート制限** — `sub` ごとに HIGH 承認要求 20 件/時。通知疲れを誘発する大量要求を封じる。
4. **監査** — 全要求が `sub` 付きで残る。

**この残存リスクは設計書に明記された既知の受容事項であり、「解決済み」と書いてはならない。**

---

## 7. PWA 認証（ペアリング / Cookie / CSRF / WS チケット）

### 7.1 ペアリング（初回のみ）

**オファーのキーと「使用済み」のキーを分ける。** 同一キーに対して「作成」と「`skip_if_exists=True` による消費」を行う設計は成立しない — オファー作成時点でキーが存在するため、**正規の初回ペアリングが必ず 409 で失敗する**。

| キー | 用途 | 作成者 |
|---|---|---|
| `pairing_offer:<sha256(code)>` | `{"created_at":..., "exp": created_at+300}` | `hh pwa pair` |
| `pairing_used:<sha256(code)>` | `{"used_at":..., "device_name":...}` | ペアリング実行時に `put(skip_if_exists=True)` |

**手順**

1. ユーザーが `hh pwa pair` を実行 → Hub が 8 桁コードを生成し `pairing_offer:<hash>` を作る（**有効 5 分**）。コードは端末の画面にのみ表示し、ログに残さない。
2. スマホで PWA を開きコードを入力 → `POST /api/pwa/pair {"code":"...", "device_name":"iPhone"}`。
3. サーバ:
   a. `pairing_offer:<hash>` が存在し `exp` 内であることを確認（無ければ 401 `PAIRING_INVALID`）。
   b. `pairing_used:<hash>` を `put(skip_if_exists=True)`。**失敗したら 409 `PAIRING_CONSUMED`**（＝単回使用の判定はこちらで行う）。
   c. `pwa_session:<sid>` を作成し、Cookie を発行。
4. `pairing_offer:` は削除する（残っていても `pairing_used:` があるので再利用はできない）。

**レート制限（必須）**: 8 桁コードは総当たり可能な空間である。

| 単位 | 上限 |
|---|---|
| 送信元 IP ごと | 10 回 / 10 分 |
| **全体（コードを問わず）** | 30 回 / 10 分 |

全体上限を置くのは、IP を分散した総当たりを止めるため。上限超過は 429（`Retry-After` 付き）とし、監査に `pairing_rate_limited` を残す。**失敗が全体で 30 回に達したら、その時点で有効な `pairing_offer:` をすべて無効化する**（攻撃を検知したらオファー自体を引っ込める）。

### 7.1b `HH_PAIRING_CODE`（Secret の静的コード）の位置づけ（2026-08-11 確定）

Secret の `HH_PAIRING_CODE` と §7.1 の動的 `pairing_offer:` フローは**役割が違う**。実装からの指摘を受けて次のとおり確定する。

| | `HH_PAIRING_CODE`（静的） | `pairing_offer:`（動的・§7.1） |
|---|---|---|
| 用途 | **初回ブートストラップ 1 回限り** | 2 回目以降のすべてのペアリング |
| 有効条件 | **`pwa_session:` が 1 つも存在したことがない間だけ** | オファー作成から 5 分 |
| 失効 | 初回ペアリング成功と同時に**恒久的に無効** | 使用 or 5 分経過 |

**規則**

1. `POST /api/pwa/pair` は、まず `bootstrap_done` キーの有無を見る。存在すれば静的コードの照合を**一切行わない**（動的フローのみ）。
2. 存在しない場合に限り `HH_PAIRING_CODE` との照合を許す。成功したら `bootstrap_done` を `put(skip_if_exists=True)` で作り、以後の静的コード経路を閉じる。
3. 静的経路も §7.1 の**レート制限（IP 10回/10分・全体 30回/10分）の対象**とする。例外にしない。
4. `HH_AGENT_TOKEN_SIGNING_KEY` のローテーションでは `bootstrap_done` を削除しない。削除すると静的コード経路が復活する。

**理由**: 静的コードは失効も期限もない永続資格情報であり、Secret を再作成しない限りローテーションできない。ブートストラップにだけ必要なので、使ったら閉じる。

### 7.2 Cookie

```
Set-Cookie: hh_pwa=<signed_session_id>; HttpOnly; Secure; SameSite=Strict;
            Path=/; Max-Age=2592000
```

- 中身は `hmac` 署名付きの `pwa_session_id`。
- **有効判定はサーバ側 `pwa_session:<sid>` の存在と `exp`（§11）で行う。** Cookie の `Max-Age` はブラウザへのヒントにすぎず、盗まれた値の再送を止められない。
- `exp` は発行から **30 日**。使用のたびに延長しない（固定期限）。
- `POST /api/pwa/logout` は `pwa_session:<sid>` を**削除する**（墓標を立てるのではない）。

### 7.3 CSRF

- ペアリング成功時と `GET /api/approval/pending` の応答に `csrf_token` を含める（**Cookie とは別経路**）。
- `csrf_token = hmac(HH_PWA_SESSION_KEY, pwa_session_id + issued_hour)`。有効 2 時間。
- `POST /api/approval/respond` は `csrf` フィールド必須。加えて **`Origin` ヘッダが Hub 自身の Origin と完全一致すること**を確認する（`Referer` は使わない）。

### 7.4 WS チケット

ブラウザの `WebSocket` は任意ヘッダを付けられないため、Cookie では認証しない（クロスサイト WS の Cookie 送信を当てにしない）。

1. `POST /api/pwa/ws-ticket`（Cookie ＋ CSRF 必須）→ `{"ticket": "<uuid4>", "expires_in": 30}`
2. `WS /ws/approval?ticket=<uuid4>`
3. サーバは `wsticket:<ticket>` を `put(skip_if_exists=True)` で**消費**。既存なら接続を拒否（単回使用）。
4. 消費後 30 秒以内に接続が確立しなければ無効。

---

## 8. バイパスファイル

| 項目 | 内容 |
|---|---|
| パス | `%USERPROFILE%\.hh-agent\bypass` |
| 作成 | **`hh bypass enable` のみ。** ユーザーが対話的に実行し、理由の入力を求められる |
| 内容 | `{"enabled_at": <epoch>, "reason": "...", "sig": "<hmac>"}` |
| 署名 | `HH_BYPASS_LOCAL_KEY`（`%USERPROFILE%\.hh-agent\local.key`、初回生成・0600 相当の ACL）で HMAC。**署名が無効なファイルは無視する** |
| TTL | **30 分**。`enabled_at` からの経過で判定 |
| 未来時刻 | `enabled_at > now + 60` なら**無効**として扱う（時計変更による無期限バイパスの防止） |
| 削除 | `hh bypass disable`、または TTL 切れで自動的に無効（ファイルは残ってよい） |
| 監査 | 使用のたびに `%USERPROFILE%\.hh-agent\bypass_audit.log` へ追記。**Hub とは独立**（Hub 障害時に使う機能なので Hub に依存させない） |
| 表示 | 有効時は毎回 stderr に `[HH-AGENT] BYPASS ACTIVE - approval gate disabled (expires in N min)` を出す |

**ACL について正直に書く**: エージェントは同一ユーザー権限で動くため、`hh bypass enable` を自分で実行することも、`local.key` を読んで署名を偽造することも原理的に可能である。この仕組みは**誤操作と事故を防ぐもので、悪意あるエージェントからの防御ではない**。§6.3 と同じ受容事項。

---

## 9. PWA のセキュリティ要件

### 9.1 応答ヘッダ（すべて必須）

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self';
    connect-src 'self'; img-src 'self' data:; font-src 'self';
    frame-ancestors 'none'; base-uri 'none'; form-action 'none'
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
Cross-Origin-Opener-Policy: same-origin
Cache-Control: no-store            # 承認内容を含む応答すべて
```

- `'unsafe-inline'` を**使わない**。インラインの `<script>` / `<style>` を書かず、`app.js` / `style.css` へ出す。
- 外部ホストへの参照を一切書かない（CDN・Web フォント・外部画像はすべて禁止）。

### 9.2 XSS 対策（実装規則）

**コマンド全文・ファイルパス・差分・エラーメッセージはすべて「攻撃者が内容を決められる文字列」である。** エージェントが実行しようとしているコマンドは、そのエージェントを騙した誰かが書いたかもしれない。

- **`innerHTML` / `insertAdjacentHTML` / `outerHTML` を使わない。** 例外なし。
- テキストの挿入は `textContent` のみ。
- 差分の色付けは、行ごとに `document.createElement('div')` してクラスを付け、本文は `textContent` で入れる。
- 属性への値の埋め込みは `setAttribute` を使い、`href` / `src` には**ユーザー由来の値を入れない**。
- `eval` / `new Function` / `setTimeout(string)` を使わない。
- **`modal_hub/tests/test_pwa_no_innerhtml.py`** が `mobile_app/pwa_approval/*.js` を走査し、禁止 API の出現でテストを失敗させる。

### 9.3 Service Worker

- キャッシュするのは静的アセット（`index.html` / `app.js` / `style.css` / `manifest`）のみ。
- **`/api/*` の応答を絶対にキャッシュしない。**
- 承認処理を SW 内に実装しない。

---

## 10. 監査ログ

### 10.1 ファイル名

```
audit/<YYYY-MM>/<approval_id>.<event>.<event_id>.json
```

- `<event>` ∈ `requested` / `notified` / `notify_failed` / `approved` / `rejected` / `timed_out` / `claim_granted` / `mismatch` / `consumed` / `failed` / `auth_failed` / `rate_limited` / `pairing_rate_limited` / `bypass_used` / `unexpected_transition`
- **`<event_id>` はランダムではなく決定的に導出する。**

```
event_id = sha256(f"{approval_id}|{event}|{discriminator}").hexdigest()[:16]
```

`discriminator` はイベント種別ごとに定める:

| event | discriminator |
|---|---|
| `requested` | `""`（1 承認につき 1 回） |
| `approved` / `rejected` | `""`（decision は write-once なので 1 回） |
| `timed_out` | `""` |
| `claim_granted` | `claim_attempt_id` |
| `mismatch` | `claim_attempt_id` |
| `consumed` / `failed` | `lease_id` |
| `notified` / `notify_failed` | 試行回数 |

**ランダム名（v1 の `rand8`）を使ってはならない。** commit は成功したが HTTP 応答が失われたケースでリトライすると、ランダム名では**別ファイルが作られて同じ事象が重複記録される**。決定的な名前なら同じファイルへ上書きになり、**冪等**になる。

- 書き込みは temp ファイル → `os.replace()` → `volume.commit()`。
- 順序は `at` フィールドとイベント種別から復元する（seq 番号は採番しない。複数コンテナでの採番は競合する）。

### 10.1b 永続 outbox

状態キーの更新（`modal.Dict`）と監査ファイルの書き込み（Volume）は**別ストアであり、トランザクションにできない**。片方だけ成功する状態を放置すると監査が欠落する。

**outbox 方式**:

1. 監査イベントをまず `modal.Dict` の `outbox:<event_id>` へ書く（状態更新と同じストアなので、こちらは確実に残る）。
2. Volume への書き出しを試みる。成功したら `outbox:<event_id>` を削除。
3. 失敗しても、**定期 GC ジョブ（1 日 1 回、§親設計書 §4.3）が `outbox:*` を走査して Volume へ再書き出しする。**

`event_id` が決定的なので、再書き出しが重複を生むことはない。

**「監査に失敗したら」の判定は「outbox への登録に失敗したら」を意味する。** Volume 書き込みの失敗単体では 500 を返さない（後で回収されるため）。

### 10.2 レコード

```json
{
  "at": 1786000000.123,
  "event": "claim_granted",
  "approval_id": "uuid4",
  "sub": "claude_code:desktop-haruki",
  "source": "claude_code",
  "session_id": "opaque",
  "workspace_id": "sha256hex",
  "tool_name": "Bash",
  "risk": "HIGH",
  "rule_id": "force_push",
  "payload_redacted": {"command": "git push --force origin main"},
  "detail": null
}
```

### 10.3 Redaction

`payload_redacted` は保存前に必ず次のパターンをマスクする（`<REDACTED:kind>` へ置換）:

**パターンは以下のコードブロックをそのまま実装へ写すこと。** Markdown の表に正規表現を書くとパイプ `|` のエスケープ（`\|`）が混入し、**交替ではなくリテラルの縦棒**として実装されてしまう。

```python
REDACTION_PATTERNS = [
    ("anthropic",   r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai",      r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    ("github",      r"gh[pousr]_[A-Za-z0-9]{20,}"),
    ("aws",         r"AKIA[0-9A-Z]{16}"),
    ("slack",       r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("google",      r"AIza[0-9A-Za-z_-]{35}"),
    ("bearer",      r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
    ("jwt",         r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"),
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ("conn_string", r"(?i)\b[a-z][a-z0-9+.-]*://[^:@/\s]+:[^@/\s]+@"),
    ("generic",     r"(?i)(?:api[_-]?key|token|secret|password|passwd|pwd)\s*[=:]\s*\S{8,}"),
]
```

**適用対象は「表示・保存されるすべての自由文フィールド」**: `payload`、`complete.detail`、`unexpected_transition` の詳細、例外メッセージ。`reason` はクライアントから受け取らない（§1.2 で `reason_code` に変更済み）ため対象外。

**新しいフィールドを追加したら redaction 対象に含めること。** `modal_hub/tests/test_redaction_coverage.py` が、PWA へ返る応答モデルと監査レコードの全 `str` フィールドを列挙し、redaction を通していないものがあればテストを失敗させる。

**Redaction は監査だけでなく、PWA へ返す `summary` / `detail` にも同じ関数を適用する。** 実装は `modal_hub/core/redact.py` の 1 か所。

**Redaction を「漏洩防止の最後の砦」にしてはならない。** 正規表現による秘密検出は必ず取りこぼす（未知の形式のトークン、Base64 に包まれた値、分割された文字列）。本設計で秘密が外部へ漏れないことを担保しているのは redaction ではなく、**ntfy 通知にペイロードを一切載せない**という構造（親設計書 §5.2）である。ntfy へ送るのは opaque な `approval_id` とリスクレベルだけであり、コマンド本文が通知経路に乗ることはない。redaction は「認証済みの PWA 画面と監査ログでの露出を減らす」ための二次的な措置と位置づける。

### 10.4 失敗時の順序保証

| 場面 | 規則 |
|---|---|
| `claim` | outbox 登録が成功しなければ 200 を返さない（500 `AUDIT_FAILED`、retryable）。**lease は焼かない**（§1.4 の `claim_attempt_id` により再取得できる） |
| `respond` | `decision:` の put が成功してから outbox に登録する。outbox 登録に失敗したら 500 を返すが、**決定自体は有効**（ユーザーは承認済みで、覆す方が危険）。PWA には「記録に失敗しました。承認自体は成立しています」と表示する |
| `requested` / `notified` | outbox 登録の失敗は警告のみ。要求の受付は妨げない |

**原則**: 「実行を許す方向の遷移」は監査の永続化が確定しない限り許さない。「実行を止める方向・すでに確定した遷移」は監査が失敗しても進める。

**`respond` の失敗表示について**: v1 では「監査失敗で 500 → PWA は失敗と表示するが Agent 側は実行が進む」という食い違いがあった。決定は既に有効なので Agent が進むのは正しい。**PWA 側の文言を「失敗」ではなく「承認は成立／記録のみ失敗」に変える**ことで解消する。

---

## 11. 資格情報の有効性は「肯定リスト」で判定する（失効の墓標を使わない）

**v1 の `revoked:agent:<tid>` / `revoked:pwa:<session_id>` という「失効の墓標（negative tombstone）」方式は破棄する。**

理由: `modal.Dict` の項目は一定期間アクセスが無いと失効しうる。墓標方式では、**墓標が先に消えると失効させたはずの資格情報が再び有効になる** — つまり保存先の TTL がそのままセキュリティホールになる。ストアの正確な TTL 値に設計の安全性を依存させてはならない。

**代わりに肯定リスト（positive allowlist）で判定する。**

| キー | 内容 | 作成 | 削除 |
|---|---|---|---|
| `agent_session:<tid>` | `{"sub":..., "source":..., "session_id":..., "workspace_id":..., "issued_at":..., "exp":...}` | トークン発行時 | `hh auth revoke` で削除、または `exp` 経過で無効 |
| `pwa_session:<sid>` | `{"device_name":..., "issued_at":..., "exp":...}` | ペアリング成功時 | `logout` で削除、または `exp` 経過で無効 |

検証は次のとおり:

1. 署名を検証する（改ざん検出）。
2. **`agent_session:<tid>` / `pwa_session:<sid>` が存在することを確認する。** 無ければ **401**。
3. レコードの `exp` を確認する。過ぎていれば 401。

これで **失効＝キーの削除**、**ストアの TTL による消滅＝資格情報の無効化** となり、どちらもフェイルクローズする。ストアの TTL が 7 日でも 30 日でも安全性は変わらない（利便性が下がるだけ — ユーザーは再ペアリングすればよい）。

**Cookie の `Max-Age` はブラウザ側のヒントにすぎず、盗まれた Cookie 値の再送を止められない。** 有効期限の判定は必ずサーバ側の `pwa_session:<sid>.exp` で行う。

**署名鍵をローテーションしても `agent_session:` / `pwa_session:` を消してはならない。** 旧鍵で発行された資格情報は `_PREV` が生きている間まだ署名検証を通るため、肯定リストも維持する必要がある。

---

## 12. この仕様で**まだ決めていない**こと

Phase 1b / 1c 分（親設計書 §11 の 11〜16）は未確定のまま。Phase 1a の実装には不要。

加えて Phase 1a でも次は実装時に決めてよい（安全性に影響しないため）:

- PWA の配色・フォント・アニメーション
- ntfy の通知タイトル文言
- ログのフォーマットとレベル
- テストのファイル分割の粒度
