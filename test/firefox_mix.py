#!/usr/bin/env python3
"""Linux の Firefox（Docker の Selenium）と Mac の Chrome を同じ部屋に入れて、声が届くまでと黄色の警告を測る

    docker run -d --name potff --shm-size=2g -p 4444:4444 --cap-add=NET_ADMIN selenium/standalone-firefox:latest
    python3 test/firefox_mix.py [--url https://beta.potalk.app/] [--trials 3] [--watch 60] [--netem "delay 120ms 40ms loss 2%"]

★Docker の中からは Mac の localhost を「localhost」として開けない（マイクと TURN の条件を満たせない）ので、
  **公開中の β** を使う＝本番と同じ appId で、部屋は本番のロビーに一時的に出る。名前に「テスト（開発中）」を入れ、終わったら全員退出する。
--netem を付けると Firefox のコンテナの通信を遅く・パケットロスありにする（回線が強くない人の再現。tc／NET_ADMIN が要る）。

測るもの：
  A. Chrome 3 人の部屋に Firefox が入る → Firefox が全員の声を受け取るまで／全員が Firefox の声を受け取るまで
  B. Firefox が先にいる部屋に Chrome が入る（trials 回）→ 同上
  C. そのまま --watch 秒見守り、Firefox の画面に黄色の「音がまだ届いていません」が出た回数と、各 Chrome の画面での回数
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
ap = argparse.ArgumentParser()
ap.add_argument('--url', default='https://beta.potalk.app/')
ap.add_argument('--trials', type=int, default=3)
ap.add_argument('--watch', type=int, default=60)
ap.add_argument('--netem', default='')
ap.add_argument('--wd', default='http://localhost:4444')
a = ap.parse_args()

FLOW = "[...document.querySelectorAll('audio:not(#appAudio)')].filter(x => x.srcObject && x.srcObject.getAudioTracks().some(t => t.readyState === 'live' && !t.muted)).length"
MEM = "document.querySelectorAll('#members .member').length"
PEND = "document.querySelectorAll('#members .m-pending').length"
WARN = "(() => { const w = document.getElementById('audioWarn'); return w && !w.hidden ? w.textContent : '' })()"
READY = "(() => { const j = document.getElementById('join'); return !!j && typeof j.onclick === 'function' && !j.disabled && !j.hidden })()"
LEAVE = "(() => { const l = document.getElementById('leave'); if (l) l.click(); const y = document.getElementById('leaveYes'); if (y) y.click() })()"
PREP = "try { localStorage.setItem('pot-call-hide-help', '1'); localStorage.setItem('pot-call-profile', %s) } catch {}"

# ── WebDriver（W3C）の最小クライアント ──────────────────────────────
def wd(method, path, body=None):
    req = urllib.request.Request(a.wd + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b'{}').get('value')
class FF:
    def __init__(self):
        caps = {'capabilities': {'alwaysMatch': {'browserName': 'firefox', 'moz:firefoxOptions': {'args': ['-headless'], 'prefs': {
            'media.navigator.streams.fake': True, 'media.navigator.permission.disabled': True,
            'media.autoplay.default': 0, 'media.autoplay.blocking_policy': 0}}}}}
        self.sid = wd('POST', '/session', caps)['sessionId']
    def go(self, url): wd('POST', '/session/%s/url' % self.sid, {'url': url})
    def eval(self, expr): return wd('POST', '/session/%s/execute/sync' % self.sid, {'script': 'return (' + expr + ')', 'args': []})
    def wait(self, expr, timeout):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.eval(expr): return True
            except Exception: pass
            time.sleep(0.5)
        return False
    def quit(self):
        try: wd('DELETE', '/session/' + self.sid)
        except Exception: pass

# ── Chrome（Mac）────────────────────────────────────────────────
procs, profs = [], []
class CH:
    def __init__(self, port):
        p = tempfile.mkdtemp(prefix='potff-'); profs.append(p)
        self.proc = subprocess.Popen([CHROME, '--headless=new', '--remote-debugging-port=%d' % port, '--user-data-dir=' + p, '--no-first-run',
            '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required', '--mute-audio', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); procs.append(self.proc)
        cdp.wait_for_devtools(port); self.port = port
    def open(self, url, name, emoji):
        self.t = cdp.new_tab(self.port); self.t.call('Page.enable'); self.t.call('Runtime.enable')
        self.t.call('Page.addScriptToEvaluateOnNewDocument', source=PREP % json.dumps(json.dumps({'name': name, 'emoji': emoji}, ensure_ascii=False)))
        self.t.call('Page.navigate', url=url)
    def eval(self, e): return self.t.eval(e)
    def wait(self, e, timeout): return bool(self.t.wait_for(e, timeout=timeout))
    def quit(self):
        try: self.eval(LEAVE)
        except Exception: pass
        time.sleep(1); self.proc.terminate()

def ff_open(ff, url, name, emoji):
    ff.go(a.url)   # 同じオリジンで保存を書いてから、目的の URL を開き直す
    ff.eval('(() => { ' + PREP % json.dumps(json.dumps({'name': name, 'emoji': emoji}, ensure_ascii=False)) + '; return 1 })()')
    # ★#hash だけ違う URL への移動は読み直しにならない（同じ文書のまま＝リンクの部屋を読まない）。? を付けて別の文書として開く
    base, _, h = url.partition('#')
    ff.go(base + ('&' if '?' in base else '?') + 'ff=' + str(int(time.time())) + ('#' + h if h else ''))

def join_and_wait(x):
    x.wait(READY + " || (document.getElementById('entryOpen') && !!document.getElementById('entryOpen').offsetParent)", 60)
    x.eval("(() => { document.getElementById('join').click(); return 1 })()")

def measure(newcomer, others, n_total, timeout=90):
    """newcomer が入ってから：newcomer が全員の声を受け取るまで／全員が newcomer の声を受け取るまで"""
    t0 = time.time(); got_in = got_out = None; pend = {}
    while time.time() - t0 < timeout and (got_in is None or got_out is None):
        el = round(time.time() - t0, 1)
        for k, x in enumerate([newcomer] + others):   # 「🔄 つながり中…」が出ていた時間（どの画面で何秒目に）
            try:
                if x.eval(PEND): pend.setdefault(k, [el, el])[1] = el
            except Exception: pass
        try:
            if got_in is None and newcomer.eval(FLOW) >= n_total - 1: got_in = el
            if got_out is None and all(o.eval(FLOW) >= n_total - 1 for o in others): got_out = el
        except Exception: pass
        time.sleep(0.5)
    if pend: print('   🔄 つながり中（0=新しい人の画面）:', {k: '%.1f〜%.1fs' % tuple(v) for k, v in pend.items()})
    return got_in, got_out

if a.netem:
    print('Firefox のコンテナに netem: ' + a.netem)
    subprocess.run(['docker', 'exec', '-u', 'root', 'potff', 'sh', '-c', 'tc qdisc del dev eth0 root 2>/dev/null; tc qdisc add dev eth0 root netem ' + a.netem], check=False)

ff = None; chs = []
f = lambda v: ('%5.1fs' % v) if v is not None else '  ×  '
try:
    ff = FF()
    print('Firefox:', ff.eval('navigator.userAgent'))
    # ── A. Chrome 3 人の部屋に Firefox が入る ──
    c0 = CH(9800); chs.append(c0); c0.open(a.url, 'テスト係C1', '🐱')
    c0.wait(READY, 60)
    c0.eval("(() => { const r = document.getElementById('room'); r.value = 'テスト（開発中）Firefox'; r.dispatchEvent(new Event('input')); document.getElementById('join').click() })()")
    assert c0.wait("location.hash.startsWith('#room=')", 60)
    link = a.url + c0.eval('location.hash')
    for i, (nm, em) in enumerate([('テスト係C2', '🐶'), ('テスト係C3', '🐼')], 1):
        c = CH(9800 + i); chs.append(c); c.open(link, nm, em); join_and_wait(c)
    assert all(c.wait('%s === 3 && %s >= 2' % (MEM, FLOW), 90) for c in chs), 'Chrome 3 人がつながらない'
    ff_open(ff, link, 'テスト係FF（Linux Firefox）', '🦊'); join_and_wait(ff)
    i_, o_ = measure(ff, chs, 4)
    st = lambda x: '%s人/音%s' % (x.eval(MEM), x.eval(FLOW))
    print('   状態  FF:', st(ff), ' C:', [st(c) for c in chs], ' FF state:', ff.eval("document.getElementById('state').textContent"))
    print('   FF の相手名:', ff.eval("[...document.querySelectorAll('#members .member .m-name')].map(e => e.textContent).join(',')"), ' FF pc:', ff.eval("JSON.stringify(window.__potPc())"), ' conn:', ff.eval("window.__potPcs().map(p => p.connectionState + '/' + p.iceConnectionState).join(' ')"))
    print('A. Firefox が入る   Firefox が全員の声: %s   全員が Firefox の声: %s' % (f(i_), f(o_)))
    # ── B. Firefox が先にいる部屋に Chrome が入る ──
    for k in range(a.trials):
        chs[-1].quit(); chs.pop(); time.sleep(4)
        c = CH(9810 + k); c.open(link, 'テスト係C%d' % (4 + k), '🐸'); join_and_wait(c); chs.append(c)
        i_, o_ = measure(c, [ff] + chs[:-1], 4)
        print('B%d. Chrome が入る   新しい人が全員の声: %s   全員（Firefox 含む）が新しい人の声: %s' % (k + 1, f(i_), f(o_)))
    # ── C. 見守り ──
    warns_ff, warns_ch = 0, 0; was_ff = was_ch = False; samples = []
    end = time.time() + a.watch
    while time.time() < end:
        w = ff.eval(WARN) or ''
        if w and not was_ff: warns_ff += 1; samples.append(w[:60])
        was_ff = bool(w)
        wc = any(c.eval(WARN) for c in chs)
        if wc and not was_ch: warns_ch += 1
        was_ch = wc
        time.sleep(1)
    print('C. %d 秒見守り   Firefox の画面の黄色: %d 回   Chrome の画面の黄色: %d 回  %s' % (a.watch, warns_ff, warns_ch, samples[:2]))
    print('   Firefox の受信トラック:', ff.eval("[...document.querySelectorAll('audio:not(#appAudio)')].map(x => x.srcObject ? x.srcObject.getAudioTracks().map(t => t.readyState + (t.muted ? '/muted' : '/ok')).join('+') : 'none').join(' | ')"))
finally:
    for c in chs:
        try: c.quit()
        except Exception: pass
    if ff:
        try: ff.eval(LEAVE)
        except Exception: pass
        time.sleep(1); ff.quit()
    if a.netem: subprocess.run(['docker', 'exec', '-u', 'root', 'potff', 'sh', '-c', 'tc qdisc del dev eth0 root 2>/dev/null'], check=False)
    for p in procs:
        try: p.terminate()
        except Exception: pass
    for p in profs: shutil.rmtree(p, ignore_errors=True)
    print('片づけました')
