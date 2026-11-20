#!/usr/bin/env python3
"""読み上げエンジンの実測（実エンジン・headless Chromium・標準ライブラリのみ）

smoke.py は偽エンジン（window.__potTts）で読み上げの流れだけを見る。こちらは本物の
piper-plus + onnxruntime-web でつくよみちゃんを合成し、①合成にかかる時間 ②その間メイン
スレッドが止まるか（10ms の setInterval の最大の間隔）③CSP 違反 を数える。

    python3 test/tts_proxy_check.py "tts-proxy-check.html?proxy=0" "tts-proxy-check.html?proxy=1"

proxy=1 が本体の設定（v0.16.30・ort.env.wasm.proxy）。gapMax が null＝合成中ずっと止まっていた。
★headless の**最初のタブは裏扱いでタイマーが 1 秒に間引かれる**（gapMax が 1000 になる）。同じ理由で日本語 G2P
  （rust wasm）の初期化に失敗することもある（"language ja is not initialised"・proxy と無関係）。
  同じ URL を 2 回並べて **2 回目の値を見る**。
"""
import sys, os, json, subprocess, tempfile, time, http.server, socketserver, threading, functools
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'test'))
import cdp
PORT, DBG = 8010, 9333
class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
Handler = functools.partial(H, directory=ROOT)
class Q(socketserver.ThreadingTCPServer): allow_reuse_address = True
srv = Q(('127.0.0.1', PORT), Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
prof = tempfile.mkdtemp(prefix='pot-ttscheck-')
chrome = subprocess.Popen(['/usr/bin/chromium', '--headless=new', '--remote-debugging-port=%d' % DBG, '--user-data-dir=' + prof,
    '--no-first-run', '--no-default-browser-check', '--mute-audio', '--disable-gpu', 'about:blank'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    v = cdp.wait_for_devtools(DBG); print(v.get('Browser'))
    for page in sys.argv[1:]:
        tab = cdp.new_tab(DBG, 'http://localhost:%d/test/%s' % (PORT, page), timeout=180)
        tab.call('Page.enable')
        t0 = time.time()
        tab.wait_for('!!window.__R && window.__R.done', timeout=180)
        r = json.loads(tab.eval('JSON.stringify(window.__R)'))
        print('\n==', page, '(%.0fs)' % (time.time() - t0))
        print(json.dumps({k: r.get(k) for k in ('proxy','csp','init_ms','synth','gapMax','samples','audio_s')}, ensure_ascii=False))
        if r['err']: print('ERR:', r['err'][:1200])
        tab.close()
finally:
    chrome.terminate(); chrome.wait(timeout=5); srv.shutdown()
