#!/usr/bin/env python3
"""4 人が話している部屋に 5 人目が入ったとき、全員とつながるまで・全員の声が届くまでの時間を測る（隔離 appId）

    python3 test/joinlag.py --trials 8
本物の公開 Nostr リレーと Cloudflare TURN を使う（appId だけ隔離＝本番のロビーには出ない）。
人ごとに別の Chrome（偽マイク）。5 人目は毎回まっさらな Chrome で入り、測ったら退出する。
"""
import argparse, json, os, random, shutil, string, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser(); ap.add_argument('--trials', type=int, default=8); ap.add_argument('--members', type=int, default=4); ap.add_argument('--timeout', type=int, default=150)
a = ap.parse_args()
URL = 'http://localhost:8000/'
tag = 'lag' + ''.join(random.choices(string.ascii_lowercase, k=4))
procs, profs = [], []
FLOW = "[...document.querySelectorAll('audio:not(#appAudio)')].filter(x => x.srcObject && x.srcObject.getAudioTracks().some(t => t.readyState === 'live' && !t.muted)).length"
MEM = "document.querySelectorAll('#members .member').length"
def chrome(port):
    p = tempfile.mkdtemp(prefix='potlag-'); profs.append(p)
    procs.append(subprocess.Popen([CHROME, '--headless=new', '--remote-debugging-port=%d' % port, '--user-data-dir=' + p, '--no-first-run',
        '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required', '--mute-audio', 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    cdp.wait_for_devtools(port); return procs[-1]
def tab(port, url):
    t = cdp.new_tab(port); t.call('Page.enable'); t.call('Runtime.enable')
    t.call('Page.addScriptToEvaluateOnNewDocument', source="try{localStorage.setItem('pot-call-hide-help','1')}catch{}")
    t.call('Page.navigate', url=url)
    t.wait_for("typeof document.getElementById('join').onclick === 'function'", timeout=40); return t
READY = "(() => { const j = document.getElementById('join'); return !j.disabled && !j.hidden })()"
srv = subprocess.Popen([sys.executable, os.path.join(ROOT, 'test', 'serve.py'), '--port', '8000', '--tag', tag], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
time.sleep(0.8); assert srv.poll() is None, 'port 8000 busy'
try:
    print('隔離 appId: kuramo-webrtc-call-' + tag)
    chrome(9700); m0 = tab(9700, URL)
    m0.eval("(() => { const r = document.getElementById('room'); r.value = '接続遅れテスト'; r.dispatchEvent(new Event('input')); document.getElementById('join').click() })()", await_promise=False)
    assert m0.wait_for("location.hash.startsWith('#room=')", timeout=60)
    link = m0.eval('location.hash'); members = [m0]
    for i in range(1, a.members):
        chrome(9700 + i); t = tab(9700 + i, URL + link); t.wait_for(READY, timeout=60); t.eval("document.getElementById('join').click()", await_promise=False); members.append(t)
    n = a.members
    ok = all(t.wait_for('%s === %d && %s >= %d' % (MEM, n, FLOW, n - 1), timeout=120) for t in members)
    print('最初の %d 人が全員つながった: %s' % (n, ok))
    rows = []
    for k in range(a.trials):
        p = chrome(9790); t = tab(9790, URL + link); t.wait_for(READY, timeout=60)
        t0 = time.time(); t.eval("document.getElementById('join').click()", await_promise=False)
        seen, heard, other = {}, {}, None
        while time.time() - t0 < a.timeout:
            m, f = t.eval(MEM) - 1, t.eval(FLOW)
            el = round(time.time() - t0, 1)
            seen.setdefault(m, el); heard.setdefault(f, el)
            if other is None and all(x.eval(FLOW) >= n for x in members): other = el
            if m >= n and f >= n and other is not None: break
            time.sleep(0.5)
        first = lambda d, v: d.get(v) or next((d[x] for x in sorted(d) if x >= v), None)
        r = { 'k': k + 1, 'see1': first(seen, 1), 'seeAll': first(seen, n), 'hear1': first(heard, 1), 'hearAll': first(heard, n), 'othersHear': other }
        rows.append(r); f = lambda v: ('%6.1fs' % v) if v is not None else '   ×  '
        print('%2d回目  見つけた: 最初 %s / 全員 %s   声が届いた: 最初 %s / 全員 %s   みんなに自分の声: %s' % (r['k'], f(r['see1']), f(r['seeAll']), f(r['hear1']), f(r['hearAll']), f(r['othersHear'])), flush=True)
        t.eval("(() => { document.getElementById('leave').click(); const y = document.getElementById('leaveYes'); if (y) y.click() })()", await_promise=False)
        time.sleep(3); p.terminate(); procs.remove(p)
        for x in members: x.wait_for('%s === %d' % (MEM, n), timeout=40)
        time.sleep(3)
    allv = [r['hearAll'] for r in rows]
    done = sorted(v for v in allv if v is not None)
    print('\\n全員の声が届くまで：%d / %d 回で完了。中央値 %s 秒・最悪 %s 秒・10 秒超 %d 回・未完了 %d 回' % (
        len(done), len(rows), done[len(done)//2] if done else '-', done[-1] if done else '-', sum(1 for v in done if v > 10), allv.count(None)))
finally:
    for p in procs:
        try: p.terminate()
        except Exception: pass
    srv.terminate()
    for p in profs: shutil.rmtree(p, ignore_errors=True)
