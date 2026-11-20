#!/usr/bin/env python3
"""読み上げエンジンの実測（実エンジン・headless Chromium・標準ライブラリのみ）

smoke.py は偽エンジン（window.__potTts）で読み上げの流れだけを見る。こちらは本物の
piper-plus + onnxruntime-web でつくよみちゃんを合成し、①合成にかかる時間 ②その間メイン
スレッドが止まるか（10ms の setInterval の最大の間隔）③CSP 違反 を数える。

    python3 test/tts_proxy_check.py "tts-proxy-check.html?proxy=0" "tts-proxy-check.html?proxy=1"

proxy=1 が本体の設定（v0.16.30・ort.env.wasm.proxy）。gapMax が null＝合成中ずっと止まっていた。
threads=N（v0.16.31）：ORT のスレッド数。crossOriginIsolated でないと ORT が 1 に落とす（結果の coi と threads で確認）。
  --coi を付けるとサーバが COOP/COEP ヘッダを直接付ける（本番では coi-sw.js が付ける。SW 経由の確認は index.html を
  先に 2 回開く必要があるので、ここでは扱わない）。spin=0 で session.intra_op.allow_spinning='0'（本体はこれ）。
  idle_cpu_pct＝合成が終わって 1 秒後からの 3 秒間に Chrome 全体が使った CPU%（スレッドプールの空回りを見る）。
★headless の**最初のタブは裏扱いでタイマーが 1 秒に間引かれる**（gapMax が 1000 になる）。同じ理由で日本語 G2P
  （rust wasm）の初期化に失敗することもある（"language ja is not initialised"・proxy と無関係）。
  同じ URL を 2 回並べて **2 回目の値を見る**。
"""
import sys, os, json, subprocess, tempfile, time, http.server, socketserver, threading, functools
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'test'))
import cdp
PORT, DBG = 8010, 9333
COI = '--coi' in sys.argv            # COOP/COEP を付けて crossOriginIsolated にする（本番は Service Worker が付ける）
ARGS = [a for a in sys.argv[1:] if a != '--coi']
class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def end_headers(self):
        if COI:
            self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
            self.send_header('Cross-Origin-Embedder-Policy', 'require-corp')
        super().end_headers()
Handler = functools.partial(H, directory=ROOT)
class Q(socketserver.ThreadingTCPServer): allow_reuse_address = True
srv = Q(('127.0.0.1', PORT), Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
prof = tempfile.mkdtemp(prefix='pot-ttscheck-')
chrome = subprocess.Popen(['/usr/bin/chromium', '--headless=new', '--remote-debugging-port=%d' % DBG, '--user-data-dir=' + prof,
    '--no-first-run', '--no-default-browser-check', '--mute-audio', '--disable-gpu', 'about:blank'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    v = cdp.wait_for_devtools(DBG); print(v.get('Browser'))
    def tree(pid):
        out = [pid]
        for c in subprocess.run(['pgrep', '-P', str(pid)], capture_output=True, text=True).stdout.split():
            out += tree(int(c))
        return out
    def cpu_ticks():
        t = 0
        for pid in tree(chrome.pid):
            try:
                f = open('/proc/%d/stat' % pid).read().rsplit(')', 1)[1].split()
                t += int(f[11]) + int(f[12])
            except Exception: pass
        return t
    HZ = os.sysconf('SC_CLK_TCK')
    for page in ARGS:
        tab = cdp.new_tab(DBG, timeout=180)      # URL は /json/new に渡さない（& 以降を DevTools が食う）＝navigate で開く
        tab.call('Page.enable')
        tab.call('Page.navigate', url='http://localhost:%d/test/%s' % (PORT, page))
        t0 = time.time()
        tab.wait_for('!!window.__R && window.__R.done', timeout=180)
        r = json.loads(tab.eval('JSON.stringify(window.__R)'))
        print('\n==', page, '(%.0fs)' % (time.time() - t0))
        time.sleep(1); c0 = cpu_ticks(); time.sleep(3); idle = (cpu_ticks() - c0) / HZ / 3 * 100   # 合成後の待機中に Chrome 全体が使う CPU%
        r['idle_cpu_pct'] = round(idle)
        print(json.dumps({k: r.get(k) for k in ('proxy','coi','threadsWanted','threads','spin','csp','init_ms','synth','gapMax','samples','idle_cpu_pct')}, ensure_ascii=False))
        if r['err']: print('ERR:', r['err'][:1200])
        tab.close()
finally:
    chrome.terminate(); chrome.wait(timeout=5); srv.shutdown()
