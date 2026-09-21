// 固定部屋の在室バナー（案内ページ用）。本体が公開 Nostr リレーへ流す在室（部屋「名」の SHA-256 を d タグに持つ
// kind 20001・30 秒ごと・内容は絵文字だけ）を購読して、「ポットが温かい（N 人）／冷めている」を書く。
// サーバは介さない。読めないときは静的なリンクのまま（見た目は壊さない）。
// ★本体 index.html の NOSTR_PUBLISH_MS（30 秒）と STALE_MS は対。片方だけ変えると「居るのに居ない」になる。
// 使い方：<a class="pot" data-room="部屋名" href="…"> … <span class="state"></span> … </a> を置いて、このモジュールを読む。
const RELAYS = ['wss://relay.primal.net', 'wss://purplerelay.com', 'wss://bucket.coracle.social', 'wss://yabu.me/v2', 'wss://x.kojira.io', 'wss://relay.notoshi.win', 'wss://relay.mostr.pub']
const KIND = 20001
const STALE_MS = 90000   // これだけ新着が無ければ「その人は席を立った」
const GRACE_MS = 8000    // 接続直後、これだけ待って在室が無ければ「冷めている」で確定（それまでは「確認中」）

const pots = [...document.querySelectorAll('.pot[data-room]')]
// ★リンクは data-room から機械的に組む（手で %-エンコードした href が、全角の「５」と半角の「5」を取り違えて
//   別の部屋を作っていた。2026-09-21・やまびこ部屋）。固定の部屋は ID＝名前なので room と name に同じ値を入れる
for (const el of pots) { const q = encodeURIComponent(el.dataset.room); el.href = '../#room=' + q + '&name=' + q }
if (pots.length) {
  const hash = async name => {
    const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode('potto:' + name))
    return [...new Uint8Array(buf)].slice(0, 8).map(b => b.toString(16).padStart(2, '0')).join('')
  }
  const rooms = new Map()   // d → { el, seen: Map(pubkey → {ts, emoji}), checked }
  const render = () => {
    const now = Date.now()
    for (const r of rooms.values()) {
      for (const [k, v] of r.seen) if (now - v.ts > STALE_MS) r.seen.delete(k)
      const n = r.seen.size, st = r.el.querySelector('.state'), cta = r.el.querySelector('.cta:not([data-fixed])')   // data-fixed の説明文（やまびこ）は上書きしない
      r.el.classList.toggle('live', n > 0)
      if (n > 0) {
        const faces = [...r.seen.values()].slice(0, 5).map(v => v.emoji).join('')
        st.innerHTML = '<b>ポットが温かい</b>　' + faces + '　' + n + ' 人'
        if (cta) cta.textContent = 'いま入ると、その人と話せます →'
      } else if (!r.checked) {
        st.textContent = '確認中…'
      } else {
        st.textContent = '☁️ いまは冷めています'
        if (cta) cta.textContent = '入って待つと、次に来た人と話せます →'
      }
    }
  }
  try {
    const ntP = import('https://esm.sh/nostr-tools@2.10.4')
    for (const el of pots) rooms.set(await hash(el.dataset.room), { el, seen: new Map(), checked: false })
    render(); window.__presenceRooms = [...rooms.keys()]
    const nt = await ntP
    const pool = new nt.SimplePool()
    pool.subscribeMany(RELAYS, [{ kinds: [KIND], '#d': [...rooms.keys()], since: Math.floor(Date.now() / 1000) - 120 }], {
      onevent(e) {
        window.__presenceEvents = (window.__presenceEvents || 0) + 1
        const d = (e.tags.find(t => t[0] === 'd') || [])[1]
        const r = rooms.get(d); if (!r) return
        let emoji = '🙂'; try { emoji = JSON.parse(e.content).e || '🙂' } catch {}
        r.seen.set(e.pubkey, { ts: Date.now(), emoji }); r.checked = true
        render()
      }
    })
    setTimeout(() => { for (const r of rooms.values()) r.checked = true; render() }, GRACE_MS)
    setInterval(render, 5000)
  } catch (err) {
    window.__presenceError = String(err && err.message || err)
    for (const r of rooms.values()) r.checked = true
    render()
  }
}
