/* =====================================================================
   HH Agent 承認 PWA - Service Worker
   - 06_PWA_Design.md §5 と 05_Phase1a_Spec.md §9.3 に従う。
   - キャッシュ対象は index.html (= "/") / app.js / style.css /
     manifest.webmanifest のみ。
   - /api/* と /ws/* は絶対にキャッシュしない (常にネットワークへ)。
   - 承認処理を SW に実装しない。
   - オフライン時は index.html の静的シェルへフォールバックする。
   -
   - DEFECT 1 修正 (2026-08-11): main.py は "/" (index.html) /
     "/sw.js" / "/manifest.webmanifest" のみ明示ルートを持ち、
     app.js / style.css は "/static/" マウント配下でのみ配信される。
     "/index.html" という別ルートは存在しない (main.py も "/" しか
     登録していない) ため、ここでも別扱いしない。旧版は "./app.js" /
     "./style.css" / "./index.html" をキャッシュ・照合しており、実在
     しない URL を常に 404 でキャッシュ・照合していた。
   ===================================================================== */

const CACHE_VERSION = "v5";
const STATIC_CACHE = "hh-pwa-static-" + CACHE_VERSION;

// キャッシュしてよい静的アセットの固定リスト。main.py が実際に登録して
// いるルート (index の "/" ・ "/manifest.webmanifest" ・
// "/static/app.js" ・ "/static/style.css") と 1:1 で対応させる。
// URL にクエリが付いたらキャッシュミス扱い (CSP と整合)。
const PRECACHE_URLS = [
  "/",
  "/manifest.webmanifest",
  "/static/app.js",
  "/static/style.css",
];

// インストール時: 静的ファイルをプリキャッシュ。
// ネットワーク取得を試み、失敗してもインストールを失敗させない
// (オフライン初回起動でも SW が有効になるように)。
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => {
      return Promise.all(
        PRECACHE_URLS.map((url) =>
          cache.add(url).catch(() => { /* 個別失敗は許容 */ })
        )
      );
    }).then(() => self.skipWaiting())
  );
});

// アクティベート時: 古いキャッシュを破棄。
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((k) => k !== STATIC_CACHE).map((k) => caches.delete(k))
      );
    }).then(() => self.clients.claim())
  );
});

// ==========================================================
// 取得 (フェッチ) ハンドラ
// ==========================================================
self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;          // POST 等は介入しない
  const url = new URL(req.url);

  // 1) API / WebSocket はネットワークへ直通。キャッシュもしない。
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) {
    event.respondWith(fetch(req));
    return;
  }

  // 2) クロスオリジンは触らない
  if (url.origin !== self.location.origin) {
    return;                                  // 既定のネットワーク挙動
  }

  // 3) 静的アセット: キャッシュ優先 (cache-first)
  if (isStaticAsset(url.pathname)) {
    event.respondWith(cacheFirst(req));
    return;
  }

  // 4) その他 (想定なし): ネットワークへ
  event.respondWith(fetch(req));
});

function isStaticAsset(pathname) {
  if (pathname === "/") return true;
  if (pathname === "/static/app.js") return true;
  if (pathname === "/static/style.css") return true;
  if (pathname === "/manifest.webmanifest") return true;
  return false;
}

async function cacheFirst(req) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(req, { ignoreSearch: true });
  if (cached) return cached;
  try {
    const resp = await fetch(req);
    if (resp && resp.ok) {
      // 成功したものだけキャッシュに保存
      cache.put(req, resp.clone()).catch(() => { /* 無視 */ });
    }
    return resp;
  } catch (e) {
    // ネットワークもキャッシュも無い → オフラインのフォールバック
    return new Response(
      "<!DOCTYPE html><html lang=\"ja\"><head><meta charset=\"utf-8\">" +
      "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
      "<title>オフライン</title></head><body style=\"font-family:-apple-system," +
      "BlinkMacSystemFont,sans-serif;padding:24px;color:#ecedee;" +
      "background:#101114;\">" +
      "<h1>オフラインです</h1>" +
      "<p>Hub に接続できません。接続を確認して再読み込みしてください。</p>" +
      "</body></html>",
      { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
    );
  }
}
