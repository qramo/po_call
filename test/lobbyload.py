#!/usr/bin/env python3
"""ロビーの混み具合で、ページを開く重さがどう変わるかを測る（隔離した appId・本番には出ない）

    python3 test/lobbyload.py              # 他の閲覧者 0/10/20/30 人で測る
    python3 test/lobbyload.py --levels 0 15

しくみ：ページを開いた人は全員ロビー（__lobby__）に入り、閲覧者どうしが全員つながる。
「閲覧者」役のタブを増やしながら、毎回まっさらな Chrome（スマホ相当：390px・CPU 4 倍遅く）で
部屋のリンクを開き、次を測る：
  起動        モジュールが最後まで走るまで
  ロビー接続  ロビーの接続（RTCPeerConnection）が閲覧者数の 9 割に達するまで、と最大数
  入口        リンクの部屋が「いま開いています」になるまで
  参加        「参加する」を押してから相手が見えるまで
  重い処理    最初の 30 秒の long task（50ms 超の処理）の合計と最長
  メモリ      JS ヒープ
test/serve.py を 8000 番・隔離 tag で自前で起動する（TURN の Origin 許可が localhost:8000 なので）。
"""
import argparse, json, os, random, shutil, string, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp  # noqa
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser(); ap.add_argument('--levels', type=int, nargs='+', default=[0, 10, 20, 30]); ap.add_argument('--per-chrome', type=int, default=6)
a = ap.parse_args()
URL = 'http://localhost:8000/'
tag = 'load' + ''.join(random.choices(string.ascii_lowercase, k=4))
procs, profs = [], []
INSTR = r"""(() => { const L = window.__L = { lt: 0, ltMax: 0, n: 0 };
  try { new PerformanceObserver(l => l.getEntries().forEach(e => { L.lt += e.duration; L.n++; if (e.duration > L.ltMax) L.ltMax = e.duration })).observe({ entryTypes: ['longtask'] }) } catch {}
  L.warn = []; const w = console.warn.bind(console); console.warn = (...x) => { L.warn.push(String(x[0]).slice(0, 120)); return w(...x) }
  try { localStorage.setItem('pot-call-hide-help', '1') } catch {} })()"""
def chrome(port):
    p = tempfile.mkdtemp(prefix='potload-'); profs.append(p)
    procs.append(subprocess.Popen([CHROME, '--headless=new', '--remote-debugging-port=%d' % port, '--user-data-dir=' + p, '--no-first-run',
        '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required', '--mute-audio', 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    cdp.wait_for_devtools(port); return port
def tab(port, url, instr=False):
    t = cdp.new_tab(port); t.call('Page.enable'); t.call('Runtime.enable')
    t.call('Page.addScriptToEvaluateOnNewDocument', source=INSTR if instr else "try{localStorage.setItem('pot-call-hide-help','1')}catch{}")
    t.call('Page.navigate', url=url); return t
srv = subprocess.Popen([sys.executable, os.path.join(ROOT, 'test', 'serve.py'), '--port', '8000', '--tag', tag], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
time.sleep(0.8)
assert srv.poll() is None, 'port 8000 busy'
viewers = []; vports = []; nextport = [9600]
def add_viewers(n):
    while len(viewers) < n:
        if not vports or len(viewers) % a.per_chrome == 0:
            vports.append(chrome(nextport[0])); nextport[0] += 1
        viewers.append(tab(vports[-1], URL))
try:
    print('隔離 appId: kuramo-webrtc-call-' + tag)
    # 部屋の主（1 人・閲覧者にも数える）
    hp = chrome(9590); host = tab(hp, URL)
    host.wait_for("typeof document.getElementById('join').onclick === 'function'", timeout=40)
    host.eval("(() => { const r = document.getElementById('room'); r.value = '負荷テスト'; r.dispatchEvent(new Event('input')); document.getElementById('join').click() })()", await_promise=False)
    assert host.wait_for("location.hash.startsWith('#room=')", timeout=60)
    link = host.eval('location.hash')
    print('%6s | %6s | %13s | %6s | %6s | %15s | %6s' % ('閲覧者', '起動', 'ロビー接続', '入口', '参加', '重い処理(合計/最長)', 'メモリ'))
    for lv in a.levels:
        add_viewers(max(0, lv - 1))   # 主も 1 人と数える
        settle = time.time() + 25 + lv
        while time.time() < settle:   # 閲覧者どうしのつながりが落ち着くまで
            time.sleep(2)
        mp = chrome(9580); m = cdp.new_tab(mp); m.call('Page.enable'); m.call('Runtime.enable')
        m.call('Emulation.setDeviceMetricsOverride', width=390, height=844, deviceScaleFactor=3, mobile=True)
        m.call('Emulation.setCPUThrottlingRate', rate=4)
        m.call('Page.addScriptToEvaluateOnNewDocument', source=INSTR)
        t0 = time.time(); m.call('Page.navigate', url=URL + link)
        boot = m.wait_for("typeof document.getElementById('join').onclick === 'function'", timeout=60) and time.time() - t0
        # ★__potPc().open は Trystero が先に作っておく受け口（約 20 本）も数える。実際につながった数で見る
        CONN = "window.__potPcs().filter(p => p.connectionState === 'connected').length"
        want = max(1, int(lv * 0.9))
        lob = m.wait_for("%s >= %d" % (CONN, want), timeout=90) and time.time() - t0
        live = m.wait_for("document.getElementById('entryText').textContent.includes('🟢') || (!document.getElementById('join').disabled && !document.getElementById('join').hidden)", timeout=90) and time.time() - t0
        t1 = time.time(); m.eval("(() => { const o = document.getElementById('entryOpen'); if (o && o.offsetParent) return; document.getElementById('join').click() })()", await_promise=False)
        joined = m.wait_for("document.querySelectorAll('#members .member').length >= 2", timeout=90) and time.time() - t1
        rest = 30 - (time.time() - t0)
        if rest > 0: time.sleep(rest)
        L = json.loads(m.eval("JSON.stringify({ lt: Math.round(__L.lt), mx: Math.round(__L.ltMax), pc: window.__potPcs().filter(p => p.connectionState === 'connected').length, vw: document.getElementById('lobbyViewers').textContent, rl: __L.warn.filter(w => w.includes('rate-limited')).length, rf: __L.warn.filter(w => w.includes('relay failure')).length, ws: [...new Set(__L.warn.map(w => w.replace(/wss:\/\/[^ ]+/, 'RELAY')))].slice(0, 4), mem: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576) : -1 })"))
        f = lambda v: ('%5.1fs' % v) if v else '  ×  '
        print('%6d | %6s | %6s (%3d本) | %6s | %6s | %7dms / %4dms | %4dMB | %s' % (lv, f(boot), f(lob), L['pc'], f(live), f(joined), L['lt'], L['mx'], L['mem'], L['vw']))
        print('        リレーの拒否：rate-limited %d 件・relay failure %d 件  %s' % (L['rl'], L['rf'], L['ws']))
        m.eval("(() => { document.getElementById('leave').click(); const y = document.getElementById('leaveYes'); if (y) y.click() })()", await_promise=False); time.sleep(1)
        procs[-1].terminate(); procs.pop()
finally:
    for p in procs:
        try: p.terminate()
        except Exception: pass
    srv.terminate()
    for p in profs: shutil.rmtree(p, ignore_errors=True)
