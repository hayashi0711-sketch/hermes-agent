/* =====================================================================
   HH Agent 承認 PWA - アプリ
   - 06_PWA_Design.md (v1, 信号機型) と 05_Phase1a_Spec.md §1.6 / §7 / §9 / §10.4 に従う。
   - innerHTML / insertAdjacentHTML / outerHTML / eval / new Function 禁止。
   - ユーザー由来文字列は textContent か setAttribute のみで DOM に流す。
   - /api/* は Service Worker で絶対にキャッシュしない。
   ===================================================================== */
(function () {
  "use strict";

  // ==========================================================
  // 定数
  // ==========================================================
  const POLL_INTERVAL_MS = 10000;       // §4: WS 接続中でも 10 秒ポーリングを止めない
  const LONG_PRESS_MS = 800;            // 承認ボタンの長押し時間
  const COUNTDOWN_TICK_MS = 250;        // カウントダウン更新 (4Hz で十分滑らか)
  const CSRF_REFRESH_MS = 2 * 60 * 60 * 1000;  // CSRF は 2h (pending が来るたび更新が正)
  const APPROVE_PROGRESS_TICK_MS = 16;  // 長押しプログレス更新 (≈60fps)

  const RISK_INFO = {
    HIGH:   { bg: "--risk-high-bg", fg: "--risk-high-fg", edge: "--risk-high-edge",
              icon: "⚠", label: "HIGH" },     // ⚠
    MEDIUM: { bg: "--risk-med-bg",  fg: "--risk-med-fg",  edge: "--risk-med-edge",
              icon: "！", label: "MEDIUM" },    // ！
    LOW:    { bg: "--risk-low-bg",  fg: "--risk-low-fg",  edge: "--risk-low-edge",
              icon: "✓", label: "LOW" },       // ✓
  };

  // cwd / workspace_id / base_revision が欠落・空文字のときに出す明示プレース
  // ホルダ。空文字のまま描画すると「対象なし」と誤認されるため、必ず
  // 「不明である」ことが伝わる文言にする (05_Phase1a_Spec.md §4 / 06 §3.1 系)。
  const CONTEXT_UNKNOWN = "(不明)";

  // ==========================================================
  // 状態
  // ==========================================================
  const state = {
    csrf: null,
    csrfAt: 0,
    items: [],                  // [{approval_id, tool_name, risk, rule_id, reason,
                                //  summary, grace_remaining_seconds, grace_deadline}]
    currentIndex: 0,            // items の中で表示中の index
    serverEpochBase: null,      // サーバ基準の現在 epoch (ms)
    serverEpochWall: null,      // 上記を取得した wall clock (ms) — 差分で時計ずれを補正
    responding: false,
    pollTimer: null,
    countdownRaf: null,
    countdownZeroPolled: false, // カウントダウンが 0 を跨いだ瞬間の即時ポーリングを 1 回だけに絞るフラグ
    ws: null,
    wsTicketExpiresAt: 0,
    detailCache: {},            // approval_id -> detail object
    offline: false,
  };

  // ==========================================================
  // DOM ユーティリティ (XSS 安全)
  // ==========================================================
  function $(id) { return document.getElementById(id); }

  // 子要素を全消去 (innerHTML = "" を使わない)
  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // 要素を作って textContent でテキストを入れる
  function makeEl(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.setAttribute("class", className);
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  // ==========================================================
  // 実機デバッグ用: 捕捉できなかった JS エラーを画面に出す
  // (2026-08-11: リモートで devtools を見られない環境でスマホ実機の不具合を
  //  切り分けるための一時的な計測。textContent のみ使用、innerHTML 禁止規約
  //  は守る。恒久的な UI ではない)
  // ==========================================================
  function showJsError(context, err) {
    try {
      const msg = (err && (err.message || err.reason || String(err))) || "unknown";
      let banner = document.getElementById("js-error-banner");
      if (!banner) {
        banner = document.createElement("div");
        banner.id = "js-error-banner";
        banner.style.position = "fixed";
        banner.style.left = "0";
        banner.style.right = "0";
        banner.style.bottom = "0";
        banner.style.zIndex = "99999";
        banner.style.background = "#b00020";
        banner.style.color = "#fff";
        banner.style.padding = "10px";
        banner.style.fontSize = "12px";
        banner.style.whiteSpace = "pre-wrap";
        banner.style.wordBreak = "break-word";
        document.body.appendChild(banner);
      }
      banner.textContent = "[JS ERROR] " + context + ": " + msg;
    } catch (e2) { /* 表示自体が失敗しても無視 */ }
  }
  window.addEventListener("error", function (ev) {
    showJsError("error", ev.error || ev.message);
  });
  window.addEventListener("unhandledrejection", function (ev) {
    showJsError("unhandledrejection", ev.reason);
  });

  // ==========================================================
  // エラー表示の正規化 (§3.4 の文言を勝手に増やさない)
  // ==========================================================
  const PAIRING_ERROR_MAP = {
    PAIRING_INVALID: "コードが正しくないか、有効期限（5分）が切れています",
    PAIRING_CONSUMED: "このコードは既に使用済みです",
  };

  // ==========================================================
  // リスク背景の適用
  // ==========================================================
  function applyRiskToView(viewEl, risk) {
    const info = RISK_INFO[risk] || RISK_INFO.MEDIUM;
    // 既存の risk-* クラスを消す
    viewEl.classList.remove("view-risk-high", "view-risk-med", "view-risk-low");
    viewEl.classList.add("view-risk-" + risk.toLowerCase());
    // CSS 変数で色を上書き
    viewEl.style.setProperty("--risk-bg", "var(" + info.bg + ")");
    viewEl.style.setProperty("background", "var(" + info.bg + ")");
    viewEl.style.setProperty("color", "var(" + info.fg + ")");
    viewEl.style.setProperty("--current-risk-edge", "var(" + info.edge + ")");
  }

  // ==========================================================
  // 時計ずれ補正
  // ==========================================================
  // pending の応答に絶対時刻は含まれない (§1.1)。
  // grace_remaining_seconds のみでカウントダウンする。
  // サーバ側のクロック誤差は、新しい pending を受け取ったときに
  // 「前回観測時の wall クロック」と「サーバが残した remaining の差分」
  // から推定する。本実装ではシンプルに「最後の known remaining」を
  // 起点にして毎秒デクリメントする。サーバ再ポーリングで上書き。
  function tickCountdown() {
    state.countdownRaf = requestAnimationFrame(tickCountdown);
    if (state.items.length === 0) return;
    const item = state.items[state.currentIndex];
    if (!item) return;
    const remainMs = computeRemainingMs(item);
    renderCountdown(item, remainMs);
    if (remainMs <= 0) {
      // 期限切れ。サーバ側もそう観測しているはずなので、
      // ポーリングを待たずにサーバへ再取得を試みる。
      // ただし tickCountdown は requestAnimationFrame で ~60fps 走るため、
      // 0 以下の間ずっと呼ぶと scheduleImmediatePoll() が毎フレーム
      // clearTimeout→setTimeout(50ms) を繰り返し、50ms に達する前に
      // 常に破棄されて永遠に発火しなくなる (カウントダウンが 00:00 で
      // 固まる原因)。「正の値 → 0 以下」に跨いだ瞬間の 1 回だけ呼ぶ。
      if (!state.countdownZeroPolled) {
        state.countdownZeroPolled = true;
        scheduleImmediatePoll();
      }
    } else {
      // 新しい pending が来て残り時間が正に戻ったら、次に 0 を跨いだ
      // ときにまた 1 回だけ即時ポーリングできるようにフラグを戻す。
      state.countdownZeroPolled = false;
    }
  }

  function computeRemainingMs(item) {
    // grace_deadline はサーバ epoch (ms)。state.serverEpochBase / state.serverEpochWall
    // から wall clock の現在値を引く。
    if (state.serverEpochBase == null || item.grace_deadline == null) return 0;
    const now = Date.now();
    const offset = now - state.serverEpochWall;
    const serverNow = state.serverEpochBase + offset;
    return Math.max(0, item.grace_deadline - serverNow);
  }

  function scheduleImmediatePoll() {
    // 10 秒タイマを即時に走らせる (既存の予約タイマは中断)
    if (state.pollTimer != null) {
      clearTimeout(state.pollTimer);
    }
    state.pollTimer = setTimeout(pollPending, 50);
  }

  function renderCountdown(item, remainMs) {
    const cd = $("countdown");
    const bar = $("countdown-bar-fill");
    const totalMs = item.total_ms || 150000;
    const sec = Math.ceil(remainMs / 1000);
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    cd.textContent = String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");

    const pct = Math.max(0, Math.min(100, (remainMs / totalMs) * 100));
    bar.style.width = pct + "%";

    const view = $("view-approval");
    // 60 秒未満で high-edge の色に切替 (§3.1)
    if (remainMs < 60000) {
      bar.style.background = "var(--risk-high-edge)";
    } else {
      bar.style.background = "";
    }

    // 残り少ないとき ARIA も更新
    $("countdown-bar").setAttribute("aria-valuenow", String(Math.round(pct)));
  }

  // ==========================================================
  // HTTP
  // ==========================================================
  function originMatches() {
    // §1.6 / §7.3: Origin 一致確認はサーバ側だが、ここでも
    // デバッグ用にログだけ残す (POST の前に Origin を再設定しても無意味)。
    return true;
  }

  async function fetchJSON(url, opts) {
    opts = opts || {};
    const init = {
      method: opts.method || "GET",
      credentials: "same-origin",
      headers: Object.assign({
        "Accept": "application/json",
        "Content-Type": "application/json",
      }, opts.headers || {}),
    };
    if (opts.body !== undefined) {
      init.body = JSON.stringify(opts.body);
    }
    if (opts.timeoutMs) {
      // AbortController でクライアント側タイムアウト
      const ctrl = new AbortController();
      init.signal = ctrl.signal;
      setTimeout(() => ctrl.abort(), opts.timeoutMs);
    }
    let resp;
    try {
      resp = await fetch(url, init);
    } catch (e) {
      // ネットワーク失敗 = Hub 到達不能
      throw { code: "NETWORK_UNREACHABLE", retryable: true, message: String(e) };
    }
    let body = null;
    try { body = await resp.json(); } catch (e) { /* 空かもしれない */ }
    if (!resp.ok) {
      const errBody = body && body.error ? body.error : {};
      throw {
        http: resp.status,
        code: errBody.code || ("HTTP_" + resp.status),
        message: errBody.message || resp.statusText,
        retryable: errBody.retryable === true,
        retryAfter: parseRetryAfter(resp),
      };
    }
    return body || {};
  }

  function parseRetryAfter(resp) {
    const v = resp.headers && resp.headers.get && resp.headers.get("Retry-After");
    if (!v) return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  // ==========================================================
  // Pending 取得 (10 秒ポーリング)
  // ==========================================================
  async function pollPending() {
    if (state.pollTimer != null) clearTimeout(state.pollTimer);
    state.pollTimer = null;
    let shouldReschedule = true;
    try {
      const data = await fetchJSON("/api/approval/pending", { timeoutMs: 8000 });
      onPending(data);
    } catch (e) {
      // 401 = 未ペアリング。showPairing() は毎回 pair-code/pair-device の
      // 入力欄を空にするため、ここで再スケジュールし続けるとユーザーが
      // 入力中の値が 10 秒ごとに消える (2026-08-11 実機で確認)。
      // ペアリング成功時は onPairSubmit() が自ら pollPending() を呼んで
      // ループを再開するので、401 の間はここで止めてよい。
      if (e && e.http === 401) {
        shouldReschedule = false;
      }
      onPendingError(e);
    } finally {
      if (shouldReschedule) {
        // 次のポーリングをスケジュール (10 秒)
        state.pollTimer = setTimeout(pollPending, POLL_INTERVAL_MS);
      }
    }
  }

  function onPending(data) {
    state.offline = false;
    // CSRF を更新 (pending のたびに新トークンが来る前提)
    if (typeof data.csrf_token === "string" && data.csrf_token.length > 0) {
      state.csrf = data.csrf_token;
      state.csrfAt = Date.now();
    }
    // サーバ時計ベースを更新
    state.serverEpochBase = Date.now();
    state.serverEpochWall = Date.now();
    const items = Array.isArray(data.items) ? data.items : [];
    state.items = items.map(normalizeItem);
    if (state.items.length === 0) {
      showStatusNeutral("承認待ちはありません");
      return;
    }
    // currentIndex が範囲外なら 0 へ
    if (state.currentIndex >= state.items.length) state.currentIndex = 0;
    renderApprovalView(state.items[state.currentIndex]);
    // WS チケットを取って接続 (まだなら)
    maybeOpenWs();
  }

  function onPendingError(e) {
    state.offline = true;
    if (e.code === "NETWORK_UNREACHABLE" || (e.http >= 500)) {
      showStatusNeutral("Hub に接続できません。エージェント側は自動的に停止（deny）されます");
      return;
    }
    // 401 の場合: ペアリング画面へ
    if (e.http === 401) {
      showPairing();
      return;
    }
    // その他のエラーは一旦そのまま
    showStatusNeutral("Hub 応答エラー (" + (e.code || "unknown") + ")");
  }

  // raw.context ({cwd, workspace_id, base_revision}) を安全に取り出す。
  // ・context 自体が無い(旧サーバ) / 各値が空文字 のいずれも「不明」として
  //   統一的に扱う。空文字のまま描画すると「対象なし」と誤読されるため、
  //   ここで明示プレースホルダに変換してから呼び出し側へ渡す。
  // ・pending 一覧のアイテムにも detail レスポンスにも同じ形で使う。
  function normalizeContext(raw) {
    const ctx = (raw && typeof raw.context === "object" && raw.context !== null) ?
      raw.context : null;
    const cwdRaw = (ctx && typeof ctx.cwd === "string") ? ctx.cwd : "";
    const revRaw = (ctx && typeof ctx.base_revision === "string") ? ctx.base_revision : "";
    return {
      cwd: cwdRaw.length > 0 ? cwdRaw : CONTEXT_UNKNOWN,
      baseRevision: revRaw.length > 0 ? revRaw : CONTEXT_UNKNOWN,
    };
  }

  function normalizeItem(raw) {
    // grace_remaining_seconds と total (150s) から deadline を組み立てる
    const remainSec = Number(raw.grace_remaining_seconds) || 0;
    const totalSec = 150; // §1.2: 固定
    const ctx = normalizeContext(raw);
    const item = {
      approval_id: String(raw.approval_id),
      tool_name: String(raw.tool_name || ""),
      risk: String(raw.risk || "MEDIUM"),
      rule_id: String(raw.rule_id || ""),
      reason: String(raw.reason || ""),
      summary: String(raw.summary || ""),
      workspace: ctx.cwd,
      base_revision: ctx.baseRevision,
      grace_remaining_seconds: remainSec,
      total_ms: totalSec * 1000,
      grace_deadline: Date.now() + remainSec * 1000,
    };
    return item;
  }

  // ==========================================================
  // WS (リアルタイム)
  // ==========================================================
  async function maybeOpenWs() {
    // 既存の WS が開いていて生きていたら何もしない
    if (state.ws && state.ws.readyState <= 1) return;
    // 既にチケット取得を試み中なら待つ
    if (state.wsTicketExpiresAt > Date.now()) return;
    state.wsTicketExpiresAt = Date.now() + 30000;
    let ticket;
    try {
      const r = await fetchJSON("/api/pwa/ws-ticket", {
        method: "POST",
        body: {},
        timeoutMs: 5000,
      });
      ticket = r.ticket;
    } catch (e) {
      // チケットが取れなくてもポーリングで同期は取れているので無視
      state.wsTicketExpiresAt = 0;
      return;
    }
    if (!ticket) {
      state.wsTicketExpiresAt = 0;
      return;
    }
    openWs(ticket);
  }

  function openWs(ticket) {
    try {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const url = proto + "//" + location.host + "/ws/approval?ticket=" +
                  encodeURIComponent(ticket);
      const ws = new WebSocket(url);
      state.ws = ws;
      ws.addEventListener("open", () => { /* 接続成功 */ });
      ws.addEventListener("message", (ev) => {
        // メッセージ受信 = 状態変化のヒント。10 秒ポーリングで
        // 必ず突き合わせる (§4.3 親設計書)。
        // 現行サーバ (approval_gate.py) は {"type":"pending", "items":[...]}
        // を送る。旧サーバ互換のため {"kind":"changed"} も引き続き受け付ける。
        // 認識できない形は無視する (throw しない・何にでも反応しない)。
        try {
          const msg = JSON.parse(ev.data);
          if (msg && (msg.type === "pending" || msg.type === "changed" ||
                      msg.kind === "changed")) {
            scheduleImmediatePoll();
          }
        } catch (e) { /* 不正メッセージは無視 */ }
      });
      ws.addEventListener("close", () => {
        state.ws = null;
        // 再接続を 5 秒後
        setTimeout(maybeOpenWs, 5000);
      });
      ws.addEventListener("error", () => {
        // close が後で来るはず。無視。
      });
    } catch (e) {
      state.ws = null;
    }
  }

  // ==========================================================
  // 描画: 承認画面
  // ==========================================================
  function renderApprovalView(item) {
    if (!item) return;
    hideAllViews();
    const view = $("view-approval");
    view.hidden = false;

    applyRiskToView(view, item.risk);

    // ヘッダ
    const info = RISK_INFO[item.risk] || RISK_INFO.MEDIUM;
    $("risk-icon").textContent = info.icon;
    $("risk-label").textContent = info.label;
    $("tool-name").textContent = item.tool_name;

    // summary (textContent のみ。innerHTML 禁止)
    $("summary").textContent = item.summary || "(本文なし)";

    // メタ (workspace は normalizeItem/normalizeContext で必ず非空 —
    // 実値か CONTEXT_UNKNOWN プレースホルダのどちらかが入っている)
    const ws = $("workspace");
    clearChildren(ws);
    ws.appendChild(makeEl("span", null, item.workspace));
    const rl = $("rule-line");
    clearChildren(rl);
    if (item.rule_id) {
      rl.appendChild(makeEl("span", null, item.rule_id));
    }
    if (item.reason) {
      if (item.rule_id) rl.appendChild(makeEl("span", null, "  "));
      rl.appendChild(makeEl("span", null, item.reason));
    }

    // キュー (2 件目以降)
    const queue = $("queue");
    const ql = $("queue-list");
    clearChildren(ql);
    const others = [];
    for (let i = 0; i < state.items.length; i++) {
      if (i === state.currentIndex) continue;
      others.push(state.items[i]);
    }
    if (others.length > 0) {
      queue.hidden = false;
      $("queue-label").textContent = "他に " + others.length + " 件";
      for (const o of others) {
        const row = makeEl("li", "queue-row");
        const info2 = RISK_INFO[o.risk] || RISK_INFO.MEDIUM;
        row.style.borderLeftColor = "var(" + info2.edge + ")";
        row.appendChild(makeEl("span", "tool", (info2.icon + " " + o.tool_name)));
        row.appendChild(makeEl("span", "remaining",
          formatRemain(computeRemainingMs(o))));
        row.addEventListener("click", () => {
          state.currentIndex = state.items.indexOf(o);
          if (state.currentIndex < 0) state.currentIndex = 0;
          renderApprovalView(state.items[state.currentIndex]);
        });
        ql.appendChild(row);
      }
    } else {
      queue.hidden = true;
    }

    // 詳細リンク
    $("link-detail").setAttribute("href", "#detail");
    $("link-detail").onclick = (ev) => {
      ev.preventDefault();
      openDetail(item);
    };

    // ボタン
    const deny = $("btn-deny");
    const approve = $("btn-approve");
    deny.disabled = false;
    approve.disabled = false;
    deny.onclick = () => doRespond(item.approval_id, "rejected");
    bindLongPress(approve, () => doRespond(item.approval_id, "approved"));

    // カウントダウン開始
    if (state.countdownRaf != null) cancelAnimationFrame(state.countdownRaf);
    tickCountdown();
  }

  function formatRemain(ms) {
    const sec = Math.max(0, Math.ceil(ms / 1000));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }

  // ==========================================================
  // 描画: 詳細画面
  // ==========================================================
  async function openDetail(item) {
    hideAllViews();
    const view = $("view-detail");
    view.hidden = false;

    // 上端ボーダーでリスク色を伝える
    const info = RISK_INFO[item.risk] || RISK_INFO.MEDIUM;
    $("detail-edge").style.background = "var(" + info.edge + ")";
    $("detail-title").textContent =
      (info.icon + " " + info.label + "  " + item.tool_name);

    // キャッシュがあれば即描画、なければ取得
    let detail = state.detailCache[item.approval_id];
    if (!detail) {
      try {
        detail = await fetchJSON(
          "/api/approval/detail?id=" + encodeURIComponent(item.approval_id),
          { timeoutMs: 5000 }
        );
        state.detailCache[item.approval_id] = detail;
      } catch (e) {
        showStatusNeutral("詳細を取得できません (" + (e.code || "unknown") + ")");
        return;
      }
    }
    renderDetailContent(item, detail);

    // 戻る
    const back = $("link-back");
    back.setAttribute("href", "#back");
    back.onclick = (ev) => {
      ev.preventDefault();
      renderApprovalView(item);
    };

    // 承認/却下ボタン
    $("btn-detail-deny").disabled = false;
    $("btn-detail-approve").disabled = false;
    $("btn-detail-deny").onclick = () => doRespond(item.approval_id, "rejected");
    bindLongPress($("btn-detail-approve"),
      () => doRespond(item.approval_id, "approved"));
  }

  function renderDetailContent(item, detail) {
    // コマンド全文 (等幅・折り返し)
    $("detail-payload").textContent =
      (detail && detail.payload && detail.payload.command) ||
      item.summary || "(本文なし)";

    // 対象パス
    const paths = $("detail-paths");
    clearChildren(paths);
    if (detail && Array.isArray(detail.targets) && detail.targets.length > 0) {
      for (const t of detail.targets) {
        const real = t.realpath || t.path || "";
        const line = makeEl("div", null,
          (t.path || "") + (real && real !== t.path ? " -> " + real : ""));
        paths.appendChild(line);
      }
    } else {
      paths.textContent = "(対象パスなし)";
    }

    // cwd / HEAD — 一覧 (item) 由来の古い値を使い回さず、必ず detail
    // レスポンス自身の context から取る (detail が別リクエストで取得
    // された時点の最新値であるため)。旧サーバで detail.context が無い
    // 場合は item の値へフォールバックせず、素直に「不明」を出す。
    const dctx = normalizeContext(detail || {});
    const cwd = $("detail-cwd");
    clearChildren(cwd);
    cwd.appendChild(makeEl("div", null, "cwd: " + dctx.cwd));
    cwd.appendChild(makeEl("div", null, "HEAD: " + dctx.baseRevision));

    // 差分
    const diffBlock = $("detail-diff-block");
    const diffEl = $("detail-diff");
    clearChildren(diffEl);
    if (detail && Array.isArray(detail.diff) && detail.diff.length > 0) {
      diffBlock.hidden = false;
      for (const row of detail.diff) {
        const div = document.createElement("div");
        if (row && row.kind === "add") div.setAttribute("class", "diff-add");
        else if (row && row.kind === "del") div.setAttribute("class", "diff-del");
        else div.setAttribute("class", "diff-hunk");
        div.textContent = (row && row.text) ? String(row.text) : "";
        diffEl.appendChild(div);
      }
    } else {
      diffBlock.hidden = true;
    }
  }

  // ==========================================================
  // 描画: 一覧画面
  // ==========================================================
  function showList() {
    hideAllViews();
    const view = $("view-list");
    view.hidden = false;
    const ul = $("big-list");
    clearChildren(ul);
    const empty = $("list-empty");
    if (state.items.length === 0) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    for (let i = 0; i < state.items.length; i++) {
      const it = state.items[i];
      const info = RISK_INFO[it.risk] || RISK_INFO.MEDIUM;
      const li = document.createElement("li");
      li.style.borderLeftColor = "var(" + info.edge + ")";
      li.appendChild(makeEl("span", "tool", (info.icon + " " + it.tool_name)));
      li.appendChild(makeEl("span", "remaining", formatRemain(computeRemainingMs(it))));
      li.addEventListener("click", () => {
        state.currentIndex = i;
        renderApprovalView(it);
      });
      ul.appendChild(li);
    }
  }

  // ==========================================================
  // 描画: ペアリング画面
  // ==========================================================
  function showPairing() {
    hideAllViews();
    $("view-pairing").hidden = false;
    $("pair-error").hidden = true;
    $("pair-error").textContent = "";
    $("pair-code").value = "";
    $("pair-device").value = "";
    const form = $("pair-form");
    form.onsubmit = onPairSubmit;
  }

  // 全角数字 (U+FF10-FF19) を半角へ正規化する。日本語キーボードでは
  // inputmode="numeric" でも全角の 10 キーが出ることがあり、それだと
  // ブラウザのネイティブ検証が通っても JS の \d 判定・サーバ側の
  // 桁照合のどちらにも一致せず、無言で弾かれ続ける (2026-08-11 実機で発覚)。
  function normalizeDigits(s) {
    return s.replace(/[０-９]/g, function (ch) {
      return String.fromCharCode(ch.charCodeAt(0) - 0xFEE0);
    });
  }

  async function onPairSubmit(ev) {
    ev.preventDefault();
    const code = normalizeDigits($("pair-code").value.trim());
    const device = $("pair-device").value.trim();
    if (!/^\d{8}$/.test(code)) {
      showPairError("PAIRING_INVALID");
      return;
    }
    const submit = $("pair-submit");
    submit.disabled = true;
    try {
      await fetchJSON("/api/pwa/pair", {
        method: "POST",
        body: { code: code, device_name: device },
        timeoutMs: 8000,
      });
      // 成功 → CSRF を取りに行くために pending を叩く
      state.currentIndex = 0;
      await pollPending();
      // 成功したら承認画面は pollPending 内で描画される
    } catch (e) {
      if (e.http === 429) {
        const ra = e.retryAfter ? e.retryAfter + " 秒後に再試行してください" :
                   "試行が多すぎます";
        showPairErrorRaw("試行が多すぎます。" + ra);
      } else {
        showPairError(e.code || "PAIRING_INVALID");
      }
    } finally {
      submit.disabled = false;
    }
  }

  function showPairError(code) {
    const msg = PAIRING_ERROR_MAP[code] || "コードが正しくないか、有効期限（5分）が切れています";
    showPairErrorRaw(msg);
  }

  function showPairErrorRaw(msg) {
    const e = $("pair-error");
    e.textContent = msg;
    e.hidden = false;
  }

  // ==========================================================
  // 描画: 状態画面
  // ==========================================================
  function showStatusNeutral(message) {
    hideAllViews();
    const view = $("view-status");
    view.hidden = false;
    $("status-title").textContent = "HH 承認";
    $("status-message").textContent = message || "";
  }

  function showStatusTitled(title, message) {
    hideAllViews();
    const view = $("view-status");
    view.hidden = false;
    $("status-title").textContent = title;
    $("status-message").textContent = message || "";
  }

  function hideAllViews() {
    const ids = ["view-pairing", "view-approval", "view-detail",
                 "view-list", "view-status"];
    for (const id of ids) {
      const v = $(id);
      if (v) v.hidden = true;
    }
    // タイマを止める
    if (state.countdownRaf != null) {
      cancelAnimationFrame(state.countdownRaf);
      state.countdownRaf = null;
    }
  }

  // ==========================================================
  // 応答送信
  // ==========================================================
  async function doRespond(approvalId, decision) {
    if (state.responding) return;
    state.responding = true;

    // 二重送信防止: 即座に無効化 (§3.1)
    $("btn-deny").disabled = true;
    $("btn-approve").disabled = true;
    if (!$("view-detail").hidden) {
      $("btn-detail-deny").disabled = true;
      $("btn-detail-approve").disabled = true;
    }

    if (!state.csrf) {
      // CSRF が無いのはまだ pending を取っていない状態
      showStatusNeutral("承認トークンを取得できていません。少し待って再試行してください");
      // 二重送信防止で無効化したボタンを戻す
      $("btn-deny").disabled = false;
      $("btn-approve").disabled = false;
      if (!$("view-detail").hidden) {
        $("btn-detail-deny").disabled = false;
        $("btn-detail-approve").disabled = false;
      }
      state.responding = false;
      return;
    }

    try {
      const r = await fetchJSON("/api/approval/respond", {
        method: "POST",
        body: { approval_id: approvalId, decision: decision, csrf: state.csrf },
        timeoutMs: 5000,
      });
      // 成功
      onRespondOk(approvalId, decision, r);
    } catch (e) {
      onRespondErr(approvalId, decision, e);
    } finally {
      state.responding = false;
    }
  }

  function onRespondOk(approvalId, decision, r) {
    // items から除く
    state.items = state.items.filter((x) => x.approval_id !== approvalId);
    if (state.items.length === 0) {
      showStatusNeutral("承認待ちはありません");
      return;
    }
    if (state.currentIndex >= state.items.length) state.currentIndex = 0;
    renderApprovalView(state.items[state.currentIndex]);
  }

  function onRespondErr(approvalId, decision, e) {
    if (e.http === 409 && e.code === "ALREADY_DECIDED") {
      // 既に処理済み。items から除く。
      state.items = state.items.filter((x) => x.approval_id !== approvalId);
      if (state.items.length === 0) {
        showStatusTitled("この要求は既に処理されました", "");
      } else {
        renderApprovalView(state.items[state.currentIndex] || state.items[0]);
      }
      return;
    }
    if (e.http === 422 && e.code === "GRACE_EXPIRED") {
      // §3.5: 期限切れ自動却下。items から除く。
      state.items = state.items.filter((x) => x.approval_id !== approvalId);
      if (state.items.length === 0) {
        showStatusTitled("猶予時間を過ぎたため自動的に却下されました", "");
      } else {
        renderApprovalView(state.items[state.currentIndex] || state.items[0]);
      }
      return;
    }
    if (e.http === 403 && e.code === "CSRF_FAILED") {
      showStatusNeutral("セキュリティ検証に失敗しました。ページを再読み込みしてください");
      return;
    }
    if (e.http === 500 || e.code === "AUDIT_FAILED") {
      // §10.4: respond の監査失敗。決定自体は有効。
      // 「失敗」と表示してはならない。
      state.items = state.items.filter((x) => x.approval_id !== approvalId);
      if (state.items.length === 0) {
        showStatusTitled(
          "承認は成立しています",
          "記録のみ失敗しました (" + (e.code || "AUDIT_FAILED") + ")"
        );
      } else {
        renderApprovalView(state.items[state.currentIndex] || state.items[0]);
      }
      return;
    }
    // その他: 再試行可能なら静かに戻す
    $("btn-deny").disabled = false;
    $("btn-approve").disabled = false;
    if (!$("view-detail").hidden) {
      $("btn-detail-deny").disabled = false;
      $("btn-detail-approve").disabled = false;
    }
  }

  // ==========================================================
  // 長押し
  // ==========================================================
  function bindLongPress(btn, onComplete) {
    let pressStart = 0;
    let raf = null;
    let done = false;
    const progress = btn.querySelector(".approve-progress");

    function start(ev) {
      if (btn.disabled || done) return;
      pressStart = performance.now();
      btn.classList.add("pressing");
      ev.preventDefault();
      loop();
    }
    function end(ev) {
      if (pressStart === 0) return;
      pressStart = 0;
      btn.classList.remove("pressing");
      if (progress) progress.style.width = "0%";
      if (raf) cancelAnimationFrame(raf);
      raf = null;
    }
    function loop() {
      if (pressStart === 0) return;
      const elapsed = performance.now() - pressStart;
      const pct = Math.min(100, (elapsed / LONG_PRESS_MS) * 100);
      if (progress) progress.style.width = pct + "%";
      if (elapsed >= LONG_PRESS_MS) {
        done = true;
        btn.classList.remove("pressing");
        btn.classList.add("completed");
        try { onComplete(); } finally {
          setTimeout(() => {
            btn.classList.remove("completed");
            done = false;
          }, 300);
        }
        return;
      }
      raf = requestAnimationFrame(loop);
    }

    btn.addEventListener("pointerdown", start);
    btn.addEventListener("pointerup", end);
    btn.addEventListener("pointerleave", end);
    btn.addEventListener("pointercancel", end);
    // キーボード長押しはアクセシビリティ目的で残す (Enter 等)
    btn.addEventListener("keydown", (ev) => {
      if (ev.key === " " || ev.key === "Enter") {
        if (btn.disabled || done) return;
        pressStart = performance.now();
        btn.classList.add("pressing");
        ev.preventDefault();
        loop();
      }
    });
    btn.addEventListener("keyup", (ev) => {
      if (ev.key === " " || ev.key === "Enter") end();
    });
  }

  // ==========================================================
  // Service Worker 登録
  // ==========================================================
  function registerSW() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("./sw.js").catch(() => { /* 無視 */ });
  }

  // ==========================================================
  // 起動
  // ==========================================================
  function boot() {
    hideAllViews();
    registerSW();
    // まず pending を一回叩いて、401 ならペアリング、それ以外ならそのまま描画
    pollPending();
  }

  // DOMContentLoaded で起動 (CSP により defer は使えないが type=module でもないので
  // body 末尾の <script> で DOM 完成後に実行される)
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  // originMatches を lint から外さない (将来利用する)
  if (false) originMatches();
})();
