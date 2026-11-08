// ptt/sync.mjs — ぽちっと通話（riryo3/pochittalk）の worker.js から、/ptt/ に置く HTML とアイコンを作る。
//   node ptt/sync.mjs <worker.js のパス>
// 元の HTML はそのまま使い、ここに並べた置き換えだけを当てる（どれか 1 つでも当たらなければ止まる＝上流が変わった合図）。
// 置き換えの理由は ptt/README.md。worker.js は Cloudflare Worker 用の 1 ファイルで、HTML とアイコンが中に入っている。
import { readFileSync, writeFileSync, mkdtempSync, copyFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const src = process.argv[2]
if (!src) { console.error('使い方: node ptt/sync.mjs <worker.js>'); process.exit(1) }
const here = dirname(fileURLToPath(import.meta.url))
// worker.js の中の定数を取り出す（テンプレートリテラルの解釈を Node に任せる＝手で剥がすと \ の扱いを間違える）
const tmp = join(mkdtempSync(join(tmpdir(), 'ptt-')), 'w.mjs')
copyFileSync(src, tmp)
writeFileSync(tmp, readFileSync(tmp, 'utf8') + '\nexport { HTML, FAVICON_SVG, PNG_FAVICON, PNG_APPLE, PNG_OG, APP_VERSION }\n')
const w = await import(pathToFileURL(tmp).href)

const TURN = 'https://pot-turn.itsuma-kuramoto.workers.dev/'
const RELAYS = 'wss://relay.primal.net wss://purplerelay.com wss://bucket.coracle.social wss://yabu.me/v2 wss://x.kojira.io wss://relay.notoshi.win wss://relay.ditto.pub'
// 本体（index.html）と同じ考え方の CSP：読み込むコードは自分のドメインだけ。つなぐ先は TURN の Worker と Nostr リレーだけ
const CSP = `<meta http-equiv="Content-Security-Policy" content="
  default-src 'none';
  script-src 'self' 'unsafe-inline';
  style-src 'self' 'unsafe-inline';
  img-src 'self' data: blob:;
  media-src 'self' blob:;
  connect-src 'self' ${TURN.slice(0, -1)} ${RELAYS};
  base-uri 'self';
  form-action 'none';
  object-src 'none'">`
// ぽっと通話の名前を、ぽちっと通話の名前がまだ無いときだけ初期値に使う（保存は元のコードが押したときにする）
const NAME_SEED = `try { savedName = localStorage.getItem('pochitalk_ptt_name'); } catch (_) {}
    // [potalk] ぽっと通話（同じドメインの本体）で決めた名前を、初めて開いたときの初期値に使う
    if (!savedName) { try { savedName = (JSON.parse(localStorage.getItem('pot-call-profile') || '{}').name || '').slice(0, 12) || null; } catch (_) {} }`

const R = [
  // 読み込み：外部の配信元（esm.sh）をやめ、本体の vendor/ を使う（同じ版・同じ通信の形）
  ["from '/vendor/trystero-0.25.4.mjs'", "from '../vendor/trystero-0.25.4.mjs'", 1],
  ["import('https://esm.sh/@noble/curves@1.4.0/secp256k1')", "import('../vendor/noble-curves-1.4.0-secp256k1.mjs')", 1],
  ["import('https://esm.sh/qrcode-generator@1.4.4')", "import('../vendor/qrcode-generator-1.4.4.mjs')", 1],
  // TURN の資格情報は本体と同じ Worker から（ぽちっと通話の /api/turn はこのドメインに無い）
  ["fetch('/api/turn', { cache: 'no-store' })", `fetch('${TURN}', { cache: 'no-store' })`, 1],
  // アイコンは同じフォルダに置く
  ['href="/favicon.svg"', 'href="favicon.svg"', 1],
  ['href="/favicon.png"', 'href="favicon.png"', 1],
  ['href="/apple-touch-icon.png"', 'href="apple-touch-icon.png"', 1],
  ['__ORIGIN__/og.png', 'https://potalk.app/ptt/og.png', 2],
  ['__ORIGIN__/', 'https://potalk.app/ptt/', 1],
  ['__VERSION__', w.APP_VERSION + '・potalk.app/ptt', 2],
  ['<meta name="viewport"', CSP + '\n  <meta name="viewport"', 1],
  ["try { savedName = localStorage.getItem('pochitalk_ptt_name'); } catch (_) {}", NAME_SEED, 1],
  // ぽっと通話へ戻る道：ホーム（部屋を出たあとの画面）の先頭に置く。通話中の画面には置かない（押し間違いで通話が切れないように）
  ['<div class="home" id="homeView">', '<div class="home" id="homeView">\n    <a class="potalk-back" id="potalkBack" href="../">← ぽっと通話に戻る</a>', 1],
  ['  </style>\n</head>', '    .potalk-back { align-self: flex-start; color: #93c5fd; font-size: 0.85rem; text-decoration: none; padding: 4px 2px; }\n  </style>\n</head>', 1],
  // プライバシーの説明を、この置き場所に合わせる（配信元が違う・TURN の資格情報の出どころが違う）
  ['ただし、ページを配信している Cloudflare には、一般的なアクセス情報（IPアドレスなど）が、同社の方針に沿って記録されることがあります。',
   'ただし、ページを配信している GitHub Pages には、一般的なアクセス情報（IPアドレスなど）が、同社の方針に沿って記録されることがあります。', 1],
  ['<li><b>esm.sh</b>: 通話用のライブラリ（Trystero）の配信元です。ページを開くと、ここにもアクセスが発生します。QRコードを作るライブラリも、ここから配信されます（QRのボタンを押したときだけ読み込みます。QRは、端末の中で作られ、招待URLは外に送られません）。</li>',
   '<li><b>ライブラリ</b>: 通話用のライブラリ（Trystero）・QRコードを作るライブラリ・在室の知らせに署名するライブラリは、このページと同じ potalk.app から配信します（外部の配信元は使いません）。QRは端末の中で作られ、招待URLは外に送られません。</li>\n        <li><b>TURN の資格情報</b>: ぽっと通話（potalk.app）と同じ Cloudflare Worker から、短い期限の資格情報を受け取ります。この Worker は資格情報を渡すだけで、音声・接続情報・在室の知らせは通りません。</li>', 1],
]
let html = w.HTML
for (const [from, to, n] of R) {
  const got = html.split(from).length - 1
  if (got !== n) { console.error(`置き換えが合わない（${got} 件・期待 ${n} 件）: ${from.slice(0, 80)}`); process.exit(2) }
  html = html.split(from).join(to)
}
// 残っていたら止める：外部から読むコード・ルートからの絶対パス
for (const bad of ['esm.sh/', "'/vendor/", '"/vendor/', '__ORIGIN__', '__VERSION__', "'/api/"]) {
  if (html.includes(bad)) { console.error('残っている: ' + bad); process.exit(3) }
}
writeFileSync(join(here, 'index.html'), html)
writeFileSync(join(here, 'favicon.svg'), w.FAVICON_SVG)
for (const [k, f] of [['PNG_FAVICON', 'favicon.png'], ['PNG_APPLE', 'apple-touch-icon.png'], ['PNG_OG', 'og.png']]) writeFileSync(join(here, f), Buffer.from(w[k], 'base64'))
console.log('ptt/ を作りました（元の版 ' + w.APP_VERSION + '）')
