// coi-sw.js — ぽっと通話：ページを cross-origin isolated にするための Service Worker（v0.16.31）
// 登録するのは本体が読み上げを読み込むときだけ（index.html の coiRegister）＝読み上げを使う人のブラウザにだけ入る
// 役目は 1 つだけ：置き場所の「/」「/index.html」（と test/ の計測ページ）の**文書の応答**に
//   Cross-Origin-Opener-Policy: same-origin ＋ Cross-Origin-Embedder-Policy: require-corp
// を足す。読み上げ（onnxruntime の wasm）を複数スレッドで回すのに要る SharedArrayBuffer は、この 2 つが
// 揃ったページでしか使えず、GitHub Pages はヘッダを付けられないため。
// キャッシュしない・オフライン対応もしない・サブリソースと他のページ（manual/ など）は触らない。
// 理由と影響の整理は index.html の読み上げの節「★crossOriginIsolated」。
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()))
// パスは SW の置き場所（scope）から数える：potalk.app では「/」、旧 β（qramo.github.io/po_call/）では「/po_call/」
const BASE = new URL('./', self.registration.scope).pathname
const isolate = p => p === BASE || p === BASE + 'index.html' || p.startsWith(BASE + 'test/')
self.addEventListener('fetch', e => {
  const req = e.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  if (url.origin !== self.location.origin) return                        // 別ドメインは触らない（CORS/CORP の判断はブラウザに）
  if (req.mode === 'navigate' ? !isolate(url.pathname) : !req.referrer) return
  // 文書は上の 3 つだけ。サブリソースは**isolated な文書から読まれるもの全部**に COEP を足す＝Worker のスクリプト
  // （ort.wasm.min.mjs・ort-wasm-simd-threaded.mjs）の応答に COEP が無いと、その Worker は isolated にならず
  // SharedArrayBuffer が使えない（2026-11-21・ヘッダを文書だけに足した版は「no available backend」で失敗した）
  // ★サブリソースは「referrer があるか」だけで見ている。index.html に referrer の指定（no-referrer など）を入れると
  // Worker のスクリプトにヘッダが付かなくなり、上と同じ失敗に戻る（#18）
  e.respondWith(fetch(req).then(r => {
    if (r.status === 0 || r.type === 'opaque' || r.redirected) return r   // 作り直せない応答はそのまま（次に開いたときに効く）
    if (r.status === 204 || r.status === 205 || r.status === 304) return r   // 本文を持てない応答に本文つきで作り直すと TypeError（#18）
    const h = new Headers(r.headers)
    h.set('Cross-Origin-Opener-Policy', 'same-origin')
    h.set('Cross-Origin-Embedder-Policy', 'require-corp')
    return new Response(r.body, { status: r.status, statusText: r.statusText, headers: h })
  }))
})
