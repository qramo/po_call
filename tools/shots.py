#!/usr/bin/env python3
"""使い方ページ（/manual/）のスクリーンショットを撮り直す。

    python3 tools/shots.py            # images/manual/*.png を作り直す（約 1 分）

回帰テストと同じヘッドレス Chrome（test/smoke.py の Runner）で、**隔離した appId** の部屋に 2 タブで入り、
iPhone 幅（390px・2 倍）・昼の配色（ミルクティー）・架空の名前で各画面を撮る。画面を変えたらこれを回して
コミットするだけで /manual/ の絵が追従する（手で撮ると名前や配色がばらつく）。
撮る対象は下の SHOTS。要素の id が変わったらここも直す（撮れないと例外で止まる）。
"""
import sys, os, base64, argparse, time, random, string, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'test'))
import smoke

OUT = os.path.join(ROOT, 'images', 'manual')
os.makedirs(OUT, exist_ok=True)
W, H = 390, 844
SHOTS = []   # (name, tab, clip-expr or None, prep-js)

def shot(tab, name, clip_expr=None):
    """clip_expr は「要素」を返す JS。ビューポートを要素の下端まで広げて先頭にスクロールし、**画面全体を撮ってから
    PIL で要素の矩形に切る**（CDP の clip はモバイル emulation と組み合わせると縮んだ絵になることがある）。"""
    from PIL import Image
    import io
    time.sleep(0.4)
    if clip_expr:
        bottom = tab.eval("(() => { const r = (%s).getBoundingClientRect(); return Math.ceil(r.bottom + window.scrollY) + 40 })()" % clip_expr)
        tab.call('Emulation.setDeviceMetricsOverride', width=W, height=max(H, bottom), deviceScaleFactor=2, mobile=True)
        # 撮るタブを前面に（裏のタブは描画が止まり、古いフレームが混ざる＝04 で上 50px が切れた）
        tab.call('Page.bringToFront'); time.sleep(0.3)
        tab.eval('window.scrollTo(0, 0)'); time.sleep(0.8)
        # 矩形が動かなくなるまで待つ（参加直後は上の欄が畳まれる途中で、測った位置と撮った位置がずれる）
        RECT = "(() => { const r = (%s).getBoundingClientRect(); return {x:r.x, y:r.y, w:r.width, h:r.height} })()" % clip_expr
        b = tab.eval(RECT)
        for _ in range(20):
            time.sleep(0.5); b2 = tab.eval(RECT)
            if b2 == b: break
            b = b2
        # 直前にもう一度先頭へ（アプリ側の smooth scroll が走っていると撮る位置がずれる）
        b = tab.eval("(() => { document.documentElement.style.scrollBehavior = 'auto'; window.scrollTo(0, 0); const r = (%s).getBoundingClientRect(); return {x:r.x, y:r.y, w:r.width, h:r.height, sy: window.scrollY} })()" % clip_expr)
        d = tab.call('Page.captureScreenshot', format='png')
        sy2 = tab.eval('window.scrollY')
        if os.environ.get('SHOTS_DEBUG') or sy2 != b['sy']: print('   ', name, 'scrollY before/after', b['sy'], sy2, 'rect.y', b['y'])
        im = Image.open(io.BytesIO(base64.b64decode(d['data'])))
        k = im.width / W   # 実際の倍率（deviceScaleFactor）
        pad = 6
        box = (max(0, int((b['x'] - pad) * k)), max(0, int((b['y'] - pad) * k)), min(im.width, int((b['x'] + b['w'] + pad) * k)), min(im.height, int((b['y'] + b['h'] + pad) * k)))
        im = im.crop(box)
        tab.call('Emulation.setDeviceMetricsOverride', width=W, height=H, deviceScaleFactor=2, mobile=True)
    else:
        d = tab.call('Page.captureScreenshot', format='png')
        im = Image.open(io.BytesIO(base64.b64decode(d['data'])))
    path = os.path.join(OUT, name + '.png')
    im.save(path, optimize=True)
    print('  %-22s %4dx%-4d %5d KB' % (name, im.width, im.height, os.path.getsize(path) // 1024))

def persona(tab, name, emoji):
    """名前と絵文字を **画面から** 入れる（localStorage はタブ間で共有なので、書いても他のタブに上書きされる。
    再読込のたびに呼ぶこと）。"""
    tab.wait_for(smoke.BOOTED, timeout=30)
    tab.eval("localStorage.setItem('pot-call-hide-help','1'); localStorage.setItem('pot-call-ignore-seen','1'); localStorage.setItem('pot-call-theme','day'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    tab.eval("window.__potTheme.apply('day', 12*60)")
    # β の印（🦞・β・安定版へ）は hostname で付くので、本番の見た目に戻す
    tab.eval("(() => { const m = document.getElementById('brandMark'); if (m) m.textContent = '🫖'; const c = document.getElementById('chan'); if (c) c.textContent = ''; document.querySelectorAll('.stable-link').forEach(e => e.remove()) })()")
    tab.eval("(() => { const n = document.getElementById('name'); n.value = %s; n.dispatchEvent(new Event('input')); const b = [...document.querySelectorAll('#avatars .avatar')].find(b => b.textContent === %s); if (b) b.click() })()" % (json.dumps(name), json.dumps(emoji)))

def setup(tab, name, emoji):
    tab.call('Emulation.setDeviceMetricsOverride', width=W, height=H, deviceScaleFactor=2, mobile=True)
    persona(tab, name, emoji)

def main():
    a = argparse.Namespace(url='http://localhost:8010/', port=9333, web_port=8010,
        tag='shots' + ''.join(random.choice(string.ascii_lowercase) for _ in range(4)),
        no_serve=False, headful=False, real_autoplay=False, keep=False, only=None)
    r = smoke.Runner(a); srv = r.start_server(); r.start_chrome()
    try:
        A = r.open_tab(); setup(A, 'みどり', '🐱')
        B = r.open_tab(); setup(B, 'さくら', '🌸')
        room = 'お昼のお茶会'
        # ── B が先に部屋を作る（A の最初の画面に「通話中の部屋」が出るように） ──
        r.join(B, room)
        assert B.wait_for("!document.getElementById('tabs').hidden", timeout=40), 'B join'
        A.eval("document.getElementById('room').value = ''")
        assert A.wait_for("document.querySelectorAll('#lobbyList .room').length >= 1", timeout=40), 'lobby'
        A.eval("window.scrollTo(0, 0)")
        shot(A, '01-first', "document.querySelector('.card')")                       # 最初の画面（名前・絵文字・部屋を作る）
        shot(A, '02-lobby', "document.querySelector('.lobby')")                      # 通話中の部屋の一覧
        A.eval("(() => { const d = [...document.querySelectorAll('#setupFields details')].find(d => d.textContent.includes('部屋を作る')); if (d) d.open = true; document.getElementById('room').value = %s })()" % json.dumps(room)); time.sleep(0.3)
        shot(A, '03-kind', "document.getElementById('setupFields')")                # 名前・絵文字・部屋を作る（📣／🏠）
        # ── A も同じ部屋へ（B のリンクで合流。開き直すと名前が戻るので入れ直してから参加） ──
        A.call('Page.navigate', url=B.eval('location.href')); time.sleep(0.5)
        A.call('Page.reload'); time.sleep(0.8)          # ハッシュだけの移動は再読込にならない＝入口の確認が走らないので、明示的に読み直す
        persona(A, 'みどり', '🐱'); time.sleep(0.5)
        # 入口の確認が「🟢 いま開いています」になるまで待つ（ロビーの発見が 12 秒を超えると「誰もいない」に落ち、
        # そのまま押すと**別の新しい部屋**を作ってしまう）。発見は 1 秒ごとに再判定されるので待てば戻る
        live = A.wait_for("document.getElementById('entryText').textContent.includes('🟢')", timeout=90)
        if not live: print('entry:', repr(A.eval("document.getElementById('entryText').textContent")), '| hash', A.eval('location.hash')[:60], '| lobby rooms', A.eval("document.querySelectorAll('#lobbyList .room').length"), '| B hash', B.eval('location.hash')[:60], '| B lobby', B.eval("document.querySelectorAll('#lobbyList .room').length"))
        assert live, 'entry live'
        smoke.click_join(A)
        ok = A.wait_for('%s === 2' % smoke.MEMBERS, timeout=60)
        if not ok: print('A state:', A.eval(smoke.STATE), '| join hidden', A.eval("document.getElementById('join').hidden"), '| entry', A.eval("document.getElementById('entryText').textContent"), '| B members', B.eval(smoke.MEMBERS))
        assert ok, 'A join'
        B.wait_for('%s === 2' % smoke.MEMBERS, timeout=40)
        time.sleep(3)   # 参加直後の畳む動き（setupFields のスライド）が終わってから撮る＝矩形がずれない
        A.eval("window.scrollTo(0, 0)")
        shot(A, '04-call', "document.getElementById('statusCard')")                 # 通話中の画面（メンバー）
        # 話題タグ
        A.eval("document.getElementById('tagEdit').click()"); time.sleep(0.3)
        A.eval("document.getElementById('tagInput').value = '今日のお昼ご飯'; document.getElementById('tagSend').click()")
        B.wait_for("document.body.textContent.includes('今日のお昼ご飯')", timeout=15)
        # ひとこと
        B.eval("document.getElementById('tabChat').click(); document.getElementById('chatText').value = 'こんにちは！'; document.getElementById('chatSend').click()")
        A.wait_for("document.body.textContent.includes('こんにちは！')", timeout=15)
        A.eval("document.getElementById('tabChat').click()"); time.sleep(0.5)
        shot(A, '05-chat', "document.getElementById('statusCard')")                 # ひとことタブ（未読→開いた状態）
        A.eval("document.getElementById('tabSettings').click()"); time.sleep(0.4)
        shot(A, '06-settings', "document.getElementById('statusCard')")             # ⚙️ 設定（音量・BGM・通知の音）
        A.eval("document.getElementById('tabMembers').click()"); time.sleep(0.3)
        # メンバー行（相手をミュート中に見せる：B がミュート）
        B.eval("document.getElementById('mute').click()")
        A.wait_for("document.querySelectorAll('#members .member.muted').length >= 1", timeout=15)
        shot(A, '07-members', "document.getElementById('members')")                # メンバーの見方（ミュート・経路・ハート）
        # 無視（割れたハート）
        A.eval("document.querySelector('#members .m-ig:not([disabled]):not(.off)').click()")
        A.wait_for("document.querySelectorAll('#members .member.ignored').length === 1", timeout=10)
        shot(A, '08-ignore', "document.getElementById('members')")
        A.eval("document.querySelector('#members .m-ig.off').click()")              # 戻す
        # 共有リンクと QR
        A.eval("document.getElementById('qr').click()"); time.sleep(0.8)
        shot(A, '09-share', "document.querySelector('.share-box') || document.getElementById('share').parentElement")
        # 退出の確認・音の解錠
        A.eval("document.getElementById('leave').click()"); time.sleep(0.3)
        shot(A, '10-leave', "document.getElementById('leaveConfirm')")
        A.eval("document.getElementById('leaveNo').click()")
        A.eval("document.getElementById('unlock').style.display = 'block'"); time.sleep(0.2)
        shot(A, '11-unlock', "document.getElementById('unlock')")
        A.eval("document.getElementById('unlock').style.display = 'none'")
        print('done →', OUT)
    finally:
        r.cleanup()
        if srv: srv.terminate()

if __name__ == '__main__':
    main()
