#!/usr/bin/env python3
"""ぽっと通話 回帰テスト（ヘッドレス Chrome・git 未追跡）

    python3 test/smoke.py                 # 全部
    python3 test/smoke.py --only T3 T5     # 一部だけ
    python3 test/smoke.py --headful        # 画面を出して眺める
    python3 test/smoke.py --keep           # 終わっても Chrome を残す（devtools で覗く）

このアプリは状態がモジュールスコープに閉じていて外から読めないので、判定は
「ブラウザから観測できるもの」だけで行う：DOM・`location.hash`・そして
**getUserMedia を差し替えて掴んだ MediaStream を覚えておく**（マイクの解放漏れは
これでしか見えない）。アプリ側のコードは1行も変えない。

⚠️ 既知バグを「落ちるはずのテスト」として先に書いてある（KNOWN-FAIL）。
   リファクタでそれが ✅ に変わることが受け入れ条件になる。期待値は EXPECT を見ること。
"""

import argparse
import json
import urllib.parse
import os
import random
import re
import shutil
import string
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

# 相手の発見（Nostr 経由の合流＋WebRTC 確立）は数秒〜十数秒かかる。短くすると
# 「壊れた」ではなく「まだ」で落ちるので、長めに取る。
DISCOVER = 60
SHORT = 15

# ページのスクリプトより先に仕込む計測。getUserMedia を包んで、掴んだストリームを
# 全部覚えておく（退出後に readyState を見れば解放漏れが分かる）。
INSTRUMENT = r"""
(() => {
  const T = window.__T = { gum: 0, gumArgs: [], streams: [], errors: [], playErrors: [], csp: [] };
  // CSP で塞がれた読み込み・送信を控える。**塞がれても多くは静かに失敗する**（TURN 資格情報が
  // 取れなければ黙って直結に落ちるだけ、など）ので、行き先を1つ書き忘れただけの事故は
  // 画面を見ても気づけない。ここで拾って T21 が0件を確認する。
  addEventListener('securitypolicyviolation', e => {
    T.csp.push(e.effectiveDirective + ' → ' + (e.blockedURI || '(inline)'));
  });
  const md = navigator.mediaDevices;
  const orig = md.getUserMedia.bind(md);
  md.getUserMedia = async c => {
    T.gum++; T.gumArgs.push(JSON.parse(JSON.stringify(c)));   // 渡した条件も控える
    const s = await orig(c); T.streams.push(s); return s;
  };
  // 「音声を有効化」ボタンは play() の reject で出る。何で落ちたのかは握りつぶされて
  // 残らないので、理由だけ控えておく（アプリの挙動は変えないよう必ず再throw する）。
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    return play.call(this).catch(e => { T.playErrors.push(e.name + ': ' + e.message); throw e; });
  };
  // wakeLock：join で取り leave で返す。取りっぱなしだと退出後も画面が消えなくなる。
  // release イベントは環境によって飛ばないので、**アプリが release() を呼んだか**を直接数える。
  // なお取得は前面のタブでしか通らない（隠れていると NotAllowedError）＝理由も控えておく。
  T.wake = { req: 0, released: 0 };
  try {
    const proto = Object.getPrototypeOf(navigator.wakeLock);
    const origReq = proto.request;
    proto.request = async function (type) {
      T.wake.req++;
      let s;
      try { s = await origReq.call(this, type); }
      catch (e) { T.wake.err = e.name + ' (visible=' + document.visibilityState + ')'; throw e; }
      T.wake.got = (T.wake.got || 0) + 1;
      const origRel = s.release.bind(s);
      s.release = () => { T.wake.released++; return origRel(); };
      return s;
    };
  } catch {}
  // 成長タイマーの書き込み。localStorage は同一オリジンの全タブで共有されるので、値を読むと
  // 他タブの加算まで拾ってしまう。**このタブが書いたか**だけを数える。
  T.statsWrites = 0;
  const origSet = localStorage.setItem.bind(localStorage);
  localStorage.setItem = (k, v) => { if (k === 'pot-call-stats') T.statsWrites++; return origSet(k, v); };
  // ひとことの読み上げ v2（v0.14.42）。実エンジン（piper-plus・約 100MB）は落とさず、アプリの差し替え口 window.__potTts に
  // 偽エンジン（440Hz・0.6 秒の PCM）を入れる。読んだ文面は T.ttsTexts に控える（音は聞けないので）。
  T.ttsTexts = [];
  T.ttsGets = 0;   // loadTts が差し替え口を読んだ回数（遅延読み込みの見張り・v0.15.2。リロードで 0 に戻る）
  const __fakeTts = { synth: async text => {
    T.ttsTexts.push(text);
    const n = 33075, a = new Float32Array(n);   // 1.5 秒（相手の行が光るのを見る余裕）
    for (let i = 0; i < n; i++) a[i] = Math.sin(i / 22050 * 440 * 2 * Math.PI) * 0.3;
    return { samples: a, sampleRate: 22050 };
  } };
  Object.defineProperty(window, '__potTts', { configurable: true, get() { T.ttsGets++; return __fakeTts; } });
  // 通知音は WebAudio の発振器で作る。鳴ったかどうかはこれで数えられる（音は聞けないので）。
  T.osc = 0;
  const origOsc = AudioContext.prototype.createOscillator;
  AudioContext.prototype.createOscillator = function () { T.osc++; return origOsc.call(this); };
  // 音量：**音源 → GainNode → 出口**が張られたか。画面には出ないので接続を控える。
  T.srcGain = 0; T.masterGain = null; T.gainOut = new Set(); T.edges = [];
  const connect = AudioNode.prototype.connect;
  AudioNode.prototype.connect = function (dest, ...rest) {
    if (this instanceof MediaStreamAudioSourceNode && dest instanceof GainNode) {
      T.srcGain++; T.masterGain = dest;
    }
    // 出口まで**たどり着けるか**を見たいので、接続は辺として全部控える。
    // 直接つながっているかだけを見ると、間にノードを1つ挟んだだけで嘘の赤になる
    // （2026-09-12：appOut を挟んだときに実際そうなった）。
    if (dest instanceof AudioNode) T.edges.push([this, dest]);
    if (this instanceof GainNode && dest instanceof AudioDestinationNode) T.gainOut.add(this);
    return connect.call(this, dest, ...rest);
  };
  // `.volume` に小数を代入していないか。**iOS では代入が反映されるのに音は変わらない**ので、
  // ここに頼った実装は「値は入っているのに効かない」＝画面からもログからも気づけない。
  T.volSet = [];
  {
    const d = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'volume');
    Object.defineProperty(HTMLMediaElement.prototype, 'volume', {
      configurable: true, get: d.get,
      set(v) { T.volSet.push(v); return d.set.call(this, v); }
    });
  }
  // confirm はヘッドレスでは自動で「キャンセル」になる（＝退出できない）。承諾に差し替え、文面を控える
  // （v0.13.5 から退出ボタンが確認を出す）。承諾しない挙動を見たいテストは __T.confirmAnswer = false にする。
  T.confirms = []; T.confirmAnswer = true;
  window.confirm = msg => { T.confirms.push(String(msg)); return T.confirmAnswer; };
  // 小窓の絵（canvas）に描いた文字を控える（v0.14.5：部屋名が出ているか、ハッシュになっていないかを見る）
  T.canvasText = [];
  const origFill = CanvasRenderingContext2D.prototype.fillText;
  CanvasRenderingContext2D.prototype.fillText = function (txt, ...rest) { T.canvasText.push(String(txt)); return origFill.call(this, txt, ...rest); };
  // クリップボードに書いた文字を控える（v0.14.7：招待リンクのコピーは「部屋名 - 名前」＋URL）。
  // ヘッドレスでは writeText が権限で落ちることがあるので、成功したことにして中身だけ取る
  T.clip = [];
  try { navigator.clipboard.writeText = async txt => { T.clip.push(String(txt)); }; } catch {}
  addEventListener('error', e => T.errors.push(String(e.message)));
  addEventListener('unhandledrejection', e => T.errors.push('rejection: ' + e.reason));
})()
"""

# ─ ページ内で使う小さな式 ────────────────────────────────────────
BOOTED = "typeof document.getElementById('join').onclick === 'function'"   # モジュールが最後まで走ったか
LIVE = "__T.streams.flatMap(s => s.getTracks()).filter(t => t.readyState === 'live').length"
MEMBERS = "document.querySelectorAll('#members .member').length"
# 無視中の相手は .member.ignored として薄く残る。接続の有無を見たいときはこちら。
# 相手の音が実際に鳴っているか。無視は「鳴らさない」なので、ここが観測点になる。
AUDIOS = "document.querySelectorAll('body > audio').length"
MUTED = "document.querySelectorAll('#members .member.muted').length"
STATE = "document.getElementById('state').textContent"
LOBBY = "[...document.querySelectorAll('#lobbyList .room-name')].map(e => e.textContent).join('|')"
JOIN_SHOWN = "getComputedStyle(document.getElementById('join')).display !== 'none'"
# 相手の音声が実際に届いているか。onPeerStream が発火して初めて <audio> が作られるので、
# 「参加者リストには出ているのに音が来ない」＝ここが 0 で捕まる。
# ★#appAudio は**このアプリの音の出口**（BGM・効果音・音量調整を通した声を鳴らす <audio>）で、
# 相手ごとの音声要素ではない。数に混ぜると「無視したのに1つ残る」等の嘘の赤が出る（実際そうなった）。
AUDIOS = "document.querySelectorAll('audio:not(#appAudio)').length"
UNLOCK_SHOWN = "getComputedStyle(document.getElementById('unlock')).display !== 'none'"


# 参加ボタンを押す。**リンクで来た（hash あり）タブは「入口の確認」で最大十数秒ボタンが止まる**（v0.12.0）。
# 確認が終わって押せるようになるまで待ち、「お休み中」と判定されたら「この名前で新しく開く」を押す
# （＝以前と同じく、その名前で部屋を立てる）。hash 無しのタブは即座に押せる。
JOIN_READY = ("(() => { const j = document.getElementById('join'), o = document.getElementById('entryOpen');"
              " return (!j.disabled && !j.hidden) || (o && !!o.offsetParent) })()")
JOIN_CLICK = ("(() => { const j = document.getElementById('join'), o = document.getElementById('entryOpen');"
              " if (o && o.offsetParent) o.click(); j.click() })()")   # 「名前をつけて参加」は参加ボタンを戻すだけ（押すのは本人）


def click_join(tab, gesture=False):
    tab.wait_for(JOIN_READY, timeout=DISCOVER)
    if gesture:
        tab.call('Runtime.evaluate', expression=JOIN_CLICK, userGesture=True)
    else:
        tab.eval(JOIN_CLICK, await_promise=False)


# 退出ボタンを押す。v0.14.2 から退出は**ページ内の2択**（confirm() ではない）で確認するので、
# ボタンのあとに「退出する」も押す。人が押すのと同じ経路。
LEAVE_CLICK = ("(() => { document.getElementById('leave').click(); const y = document.getElementById('leaveYes');"
               " if (y && y.offsetParent) y.click() })()")


def click_leave(tab):
    tab.eval(LEAVE_CLICK, await_promise=False)


def js_str(s):
    return "'" + s.replace('\\', '\\\\').replace("'", "\\'") + "'"


class Runner:
    def __init__(self, args):
        self.args = args
        self.tabs = []
        self.results = []

    # ── 起動まわり ──────────────────────────────────────────
    def start_server(self):
        if self.args.no_serve:
            return None
        p = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, 'test', 'serve.py'),
             '--port', str(self.args.web_port), '--tag', self.args.tag],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        time.sleep(0.6)
        if p.poll() is not None:
            sys.exit('テスト用サーバを起動できませんでした（ポート %d は使用中）。\n'
                     '手動テスト用の serve.py が動いているなら、それを止めるか --no-serve で相乗りしてください。'
                     % self.args.web_port)
        return p

    def start_chrome(self):
        self.profile = tempfile.mkdtemp(prefix='potcall-test-')
        flags = [
            CHROME,
            '--headless=new' if not self.args.headful else '',
            '--remote-debugging-port=%d' % self.args.port,
            '--user-data-dir=' + self.profile,
            '--no-first-run', '--no-default-browser-check',
            '--use-fake-device-for-media-stream',   # 偽マイク（許可ダイアログも出ない）
            '--use-fake-ui-for-media-stream',
            # 既定では自動再生を許してテストを安定させる。--real-autoplay を付けると本物の
            # ブラウザと同じ制限になり、「音声を有効化」ボタンが出る条件を再現できる。
            '' if self.args.real_autoplay else '--autoplay-policy=no-user-gesture-required',
            '--mute-audio',
            '--disable-gpu',
            'about:blank',
        ]
        self.chrome = subprocess.Popen([f for f in flags if f],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        v = cdp.wait_for_devtools(self.args.port)
        print('  ' + v.get('Browser', '?'))

    def open_tab(self, hash_='', block_esm=False):
        tab = cdp.new_tab(self.args.port)
        self.tabs.append(tab)
        tab.call('Page.enable')
        tab.call('Runtime.enable')
        if block_esm:
            tab.call('Network.enable')
            # v0.16.2 から Trystero は同梱（vendor/）。ライブラリの読み込み失敗は、その 1 本を塞いで作る
            tab.call('Network.setBlockedURLs', urls=['*vendor/trystero-*'])
        tab.call('Page.addScriptToEvaluateOnNewDocument', source=INSTRUMENT)
        tab.call('Page.navigate', url=self.args.url + hash_)
        if not tab.wait_for(BOOTED, timeout=SHORT):
            return tab   # 起動しなかったこと自体を判定したいテストがあるのでそのまま返す
        return tab

    def join(self, tab, room):
        """部屋名を打って参加する。**v0.13.0 から部屋の実体はハッシュ**なので、同じ名前を打っても
        別の部屋になる。既に同じ名前で参加しているタブがあれば、**そのタブの URL（#room=…&name=…）で
        開き直してから**参加する（＝実機で共有リンクを踏むのと同じ）。"""
        link = None
        name_of = lambda h: urllib.parse.unquote(h.split('&name=', 1)[1]) if h.startswith('#room=') and '&name=' in h else ''
        for other in self.tabs:
            if other is tab:
                continue
            try:
                h = other.eval("location.hash")
                typed = other.eval("document.getElementById('room').value")
            except Exception:
                continue
            if name_of(h) == room:
                link = h
                break
            if typed == room:
                # 同じ名前を打って参加の途中のタブ＝そのタブの URL が決まるのを待ってから、それで開き直す
                if other.wait_for("location.hash.startsWith('#room=')", timeout=DISCOVER):
                    link = other.eval("location.hash")
                    break
        if link:
            # ★hash だけ変えても同じ文書のまま（モジュールが読み直さない）。hash を書いてから**リロード**する
            tab.eval("location.hash = %s" % js_str(link[1:]), await_promise=False)
            tab.call('Page.reload')
            time.sleep(0.5)
            tab.wait_for(BOOTED, timeout=SHORT)
            click_join(tab)
            return
        tab.eval("(() => { const r = document.getElementById('room');"
                 " r.value = %s; r.dispatchEvent(new Event('input')); })()" % js_str(room))
        click_join(tab)

    def cleanup(self):
        for t in self.tabs:
            try:
                t.close()
            except Exception:
                pass
        if self.args.keep:
            print('\n(--keep) Chrome は残しています: http://127.0.0.1:%d' % self.args.port)
            return
        try:
            self.chrome.terminate()
            self.chrome.wait(timeout=5)
        except Exception:
            pass
        shutil.rmtree(self.profile, ignore_errors=True)

    # ── 判定の記録 ──────────────────────────────────────────
    def check(self, tid, name, ok, expect, note=''):
        if expect == 'known-fail':
            mark = '🎉 FIXED  ' if ok else '⚠️  KNOWN-FAIL'
        else:
            mark = '✅ PASS  ' if ok else '❌ FAIL  '
        print('  %s %s %s%s' % (mark, tid, name, ('  — ' + note) if note else ''))
        self.results.append((tid, name, ok, expect))


# ─ テスト本体 ────────────────────────────────────────────────
# expect='known-fail' ＝ 現時点で落ちるのが正しい（＝レビュー指摘の未修正バグ）

def T1(r, room):
    """起動：モジュールが最後まで走り、エラーが出ていない"""
    t = r.open_tab()
    booted = t.eval(BOOTED)
    errs = t.eval('__T.errors')
    r.check('T1', '起動する', bool(booted) and not errs, 'pass', str(errs or ''))
    return t


def T1b(r):
    """参加前：通話中だけのものが出ていない（EQ・凡例・リアクション）

    未参加の画面に通話中のUIが混じると「押せそうなのに何も起きない」ものが増える。
    新しく通話中のUIを足したら、ここにも足すこと。
    """
    t = r.open_tab()
    # ★hidden 属性ではなく**実際に描かれているか**を見る。CSS で display を指定していると
    #   [hidden] を打ち消してしまい、属性は付いているのに画面には出る（2026-08-15 に踏んだ）。
    st = t.eval("JSON.stringify(['eq','connLegend','reactions','speak','chatLog','tabs','mailNow'].reduce((o,id) => {"
                " const el = document.getElementById(id);"
                " o[id] = !el.offsetParent && getComputedStyle(el).display === 'none'; return o }, {}))")
    d = json.loads(st)
    ok = all(d.values())
    r.check('T1b', '参加前は通話中のUIを出さない', ok, 'pass', st)


def T2(r, room):
    """B-1：壊れたパーセントエンコーディングの hash でも起動する"""
    t = r.open_tab(hash_='#%E3%81')
    ok = bool(t.wait_for(BOOTED, timeout=SHORT))
    r.check('T2', '壊れた #hash でも起動する', ok, 'pass',
            '' if ok else 'decodeURIComponent の URIError で初期化が全部止まっている')
    return t


def T2b(r, room):
    """正しい #hash からは今までどおり部屋名が入る（T2 の修正で壊していないか）"""
    name = 'テスト部屋'
    t = r.open_tab(hash_='#' + '%E3%83%86%E3%82%B9%E3%83%88%E9%83%A8%E5%B1%8B')
    got = t.eval("document.getElementById('room').value") if t.eval(BOOTED) else None
    r.check('T2b', '正しい #hash から部屋名が入る', got == name, 'pass', 'value=%r' % got)
    return t


def T14(r, room):
    """部屋名の制御文字・双方向制御が落ちる（confirm の文面偽装・表示順の反転を防ぐ）

    部屋名は「〇〇に参加しますか？」の確認ダイアログとロビー一覧にそのまま出るので、
    改行や U+202E(RTL上書き) を混ぜられると文面を偽装できる。参加時に落とす。
    絵文字は割らずに残ること（コードポイント単位で切る）も同時に見る。
    """
    t = r.open_tab()
    # 'a' + RTL上書き + 改行 + 'b' + タブ + 家族絵文字（ZWJ結合・分解されてはいけない）
    t.eval("(() => { const r = document.getElementById('room');"
           " r.value = 'a\\u202E\\nb\\t\\u{1F468}\\u200D\\u{1F469}\\u200D\\u{1F467}';"
           " r.dispatchEvent(new Event('input')) })()", await_promise=False)
    click_join(t)
    ok = t.wait_for("document.getElementById('room').value === 'ab\\u{1F468}\\u200D\\u{1F469}\\u200D\\u{1F467}'",
                    timeout=SHORT)
    r.check('T14', '部屋名から制御文字が落ちる（絵文字は壊さない）', ok, 'pass',
            '' if ok else 'value=%r' % t.eval("document.getElementById('room').value"))
    click_leave(t)


def T3(r, room):
    """2タブが同じ部屋で互いを認識する"""
    a, b = r.open_tab(), r.open_tab()
    r.join(a, room)
    r.join(b, room)
    ok = a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and \
        b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    r.check('T3', '2タブが互いを認識する', ok, 'pass',
            '' if ok else 'A=%s B=%s' % (a.eval(STATE), b.eval(STATE)))
    return a, b


def T22(r, a, b):
    """無視：その人の音を受け取らなくなる（**接続は切らない／相手には伝わらない**）

    無視は受信側だけで完結する設計。期待値は3つ:
      ・押した側で、その人の <audio> が消える（＝鳴らさない）
      ・行が「無視中」の見た目になる
      ・**相手側は何も変わらない**（人数も状態もそのまま＝伝わっていない）
    接続を切る案は 2026-08-14 に実測で捨てた（Trystero が即座に張り直すため）。
    """
    before = a.eval(AUDIOS)
    # 初回だけ confirm が出る。ヘッドレスでは自動で承諾されないので、先に「見た」印を入れておく。
    a.eval("localStorage.setItem('pot-call-ignore-seen','1')")
    clicked = a.eval("(() => { const b = document.querySelector('#members .m-ig:not([disabled]):not(.off)'); if (b) b.click(); return !!b })()")
    quiet = a.wait_for("%s === 0" % AUDIOS, timeout=15)
    marked = a.eval("document.querySelectorAll('#members .member.ignored').length") == 1
    still = a.eval(MEMBERS) == 2          # 接続は切らないので行は残る
    time.sleep(8)                          # 相手が繋ぎ直しに来ないこと＝churn が無いこと
    calm = a.eval(AUDIOS) == 0 and b.eval(MEMBERS) == 2 and 'エラー' not in b.eval(STATE)
    ok = clicked and before == 1 and quiet and marked and still and calm
    r.check('T22', '無視でその人の音だけ受け取らなくなる（相手には伝わらない）', ok, 'pass',
            '無視前の音声要素=%s / 無視後=%s / 印=%s / 行は残る=%s / 8秒後も静かで相手は平常=%s'
            % (before, a.eval(AUDIOS), marked, still, calm))


def T22b(r, a, b):
    """「戻す」でその人の音がまた届く（相手に送り直しを頼んで回復する）"""
    clicked = a.eval("(() => { const b = document.querySelector('#members .m-ig.off'); if (b) b.click(); return !!b })()")
    ok = clicked and a.wait_for("%s === 1" % AUDIOS, timeout=DISCOVER) and \
        a.eval("document.querySelectorAll('#members .member.ignored').length") == 0
    r.check('T22b', '「戻す」で音が戻る', ok, 'pass',
            '' if ok else '音声要素=%s' % a.eval(AUDIOS))


def T23(r, a, b):
    """リアクション：相手に届き、**押した人のアバターから**浮かぶ

    ワイヤに流すのは絵文字ではなく一覧の添字。受け取る側は添字として検査して
    自分の一覧からしか描かない（改造クライアントが任意の絵を出せる穴を塞ぐ）。
    ここでは「送れば相手に出る」「自分の画面にも出る」を見る。

    ⚠️ **許可リストそのものは自動テストで守れていない。**受信処理はモジュールスコープに
    閉じていて外から叩けないので、「範囲外の添字や文字列を送りつけても描かない」は
    人が読んで担保している（index.html の reactAction.onMessage）。
    ここを触るときは、その1行を必ず目で確認すること。
    """
    fly = "document.querySelectorAll('.react-fly').length"
    # リアクションはアバターが見えていないと飛ばない。前のテスト（T25 など）が上のタブを
    # ひとことに切り替えたままでも測れるように、両方ともメンバーへ戻してから押す。
    show_tab(a, 'members'); show_tab(b, 'members')
    a.eval("document.querySelectorAll('#reactions button')[0].click()")
    mine = a.wait_for("%s >= 1" % fly, timeout=10)
    theirs = b.wait_for("%s >= 1" % fly, timeout=20)
    shown = b.eval("(document.querySelector('.react-fly')||{}).textContent")
    palette = a.eval("document.querySelectorAll('#reactions button').length")
    # スマホでダブルタップ→ズームに化けるのを止める指定。実機でしか症状が出ないので
    # ここで指定の存在だけ守る（2026-08-15 に実機で踏んだ）。
    ta = a.eval("getComputedStyle(document.querySelector('#reactions button')).touchAction")
    ok = mine and theirs and shown == '\U0001f44f' and palette == 8 and ta == 'manipulation'
    r.check('T23', 'リアクションが相手にも浮かぶ', ok, 'pass',
            '自分=%s 相手=%s 出た絵文字=%r パレット=%s個 touch-action=%s'
            % (mine, theirs, shown, palette, ta))


def T24(r, a, b):
    """自動復帰：**正常な通話では発動しない**（誤爆でリロードされない）

    自動復帰の最悪の壊れ方は「壊れていないのにページを再読み込みし続ける」こと。
    そうなると手動で操作する隙すら無くなる。判定が働き始める 25秒 を十分に越えて
    観測し、リロードが起きていないことを見る（window に置いた印が消えないこと＝
    ページが読み込み直されていないこと）。
    """
    a.eval("window.__stay = 1")
    time.sleep(35)
    stayed = a.eval("window.__stay === 1")
    joined = a.eval("%s === 2" % MEMBERS)
    audio = a.eval(AUDIOS) == 1
    ok = stayed and joined and audio
    r.check('T24', '正常な通話では自動復帰が誤爆しない', ok, 'pass',
            'リロードされていない=%s / 参加者2人=%s / 音が来ている=%s' % (stayed, joined, audio))


def flip_back(tab):
    """見出しの「シェア」でカードを裏返し、QR が描かれるまで待つ（招待のコピーは裏の「招待リンクをコピー」）。
    裏のタブでは回る動きが止まるので、手前に出してから押す（人が押すときも必ず手前）"""
    tab.call('Page.bringToFront')
    tab.eval("document.getElementById('shareFlip').click()", await_promise=False)
    return tab.wait_for("!document.getElementById('flipBack').hidden && !!document.querySelector('#fbQr svg')", timeout=SHORT)


def show_tab(tab, which):
    """通話中のタブを切り替える（人が押すのと同じ経路）。which は members / chat / settings"""
    tab.eval("document.getElementById('tab%s').click()" % which.capitalize())


# タブが1行に収まり、はみ出していないか（狭い画面での折り返し・見切れの検出）
TAB_FITS = """(() => { const t = [...document.querySelectorAll('#tabs .tab')].filter(e => !e.hidden);
  const box = document.getElementById('tabs').getBoundingClientRect();
  const over = t[t.length-1].getBoundingClientRect().right - box.right;
  const rows = new Set(t.map(e => Math.round(e.getBoundingClientRect().bottom))).size;
  return over <= 1 && rows === 1 })()"""


# そのタブが**実際に描かれているか**（属性ではなく算出後の表示で見る）
VISIBLE = "(id => { const e = document.getElementById(id);" \
          " return !!e.offsetParent && getComputedStyle(e).display !== 'none' })"


def T33(r, a, b):
    """参加者リストの並び：**左は見るだけ・右は押すもの**

    2026-08-18 の整理。見る印（ミュート・経路・配信部屋）と押す印（登壇・無視）が
    右端で交互に並んでいて、どれが押せるのか分からなかった。
    役割で場所を分けたので、**その順番が崩れていないこと**を見る。
    """
    order = a.eval("[...document.querySelector('#members .member').children].map(e => e.className.split(' ')[0]).join(',')")
    # 状態のまとまりの中身（ミュート印・経路。普通の部屋では配信の枠を作らない）
    state = a.eval("[...document.querySelector('#members .m-state').children].map(e => e.className.split(' ')[0]).join(',')")
    no_bc = a.eval("!document.querySelector('#members .m-bc')")     # 普通の部屋には配信の枠を出さない
    # 押せるものは右端だけ。状態のまとまりの中にボタンが混じっていないこと
    clean = a.eval("!document.querySelector('#members .m-state button')")
    # 名前の左端が全員そろっている（枠を常に空けているので、ミュートの有無でずれない）
    aligned = a.eval("(() => { const xs = [...document.querySelectorAll('#members .m-id')]"
                     ".map(e => Math.round(e.getBoundingClientRect().left)); return new Set(xs).size === 1 })()")
    # ★普通の部屋に「おたより」タブを出さないこと。配信部屋だけの機能なので、出ていると
    #   押しても切り替わらない「効かないタブ」になる（2026-08-18 に実際に出ていた：
    #   .tab に display を足したときに [hidden] を書き忘れ、hidden が効かなくなっていた）。
    no_mail = a.eval("%s('tabMail') === false" % VISIBLE)
    ok = (order == 'm-emoji,m-state,m-id,m-ig' and state == 'm-mute,m-conn'
          and no_bc and clean and aligned and no_mail)
    r.check('T33', '参加者リストは左が状態・右が操作', ok, 'pass',
            '並び=%r / 状態=%r / 配信の枠なし=%s / 状態に押せるものが無い=%s / 名前の左端がそろう=%s / 普通の部屋におたよりタブなし=%s'
            % (order, state, no_bc, clean, aligned, no_mail))


def T30(r, a, b):
    """タブ：既定はメンバー、押すと入れ替わる、退出で消える

    タブは**要素をDOMから消さずに表示だけを握る**。壊れ方は2種類あって、どちらも
    「押しても何も起きない」ではなく「**見えていないのに動いているつもり**」になる:
      ・パネルを隠したのに中身が見えている（＝隠せていない）
      ・参加していないのにタブが出ている（未参加の画面に通話中のUIが混じる）
    """
    default_ok = a.eval("%s('members') && %s('chatRow') === false && %s('sndPeerRow') === false"
                        % (VISIBLE, VISIBLE, VISIBLE))
    # 高さは「5人ぶん」で決め打ち＝**タブを切り替えてもカードの高さが動かない**
    # （動くと下の共有リンクやロビーまで一緒に動いて落ち着かない）
    h_members = a.eval("document.getElementById('panMembers').offsetHeight")
    # ★v0.16.25 試作から入力欄（#chatRow）は操作盤（#dock）の「ひとこと」の段にあり、主タブとは独立に出る
    #   （メンバーを見ながら打てる）。主タブでひとことを開くと段もひとことになる（入力欄が見える）が、
    #   設定に切り替えても段はそのまま＝入力欄は消えない。ここで見るのは**パネル（#panChat）**の入れ替え。
    show_tab(a, 'chat')
    chat_ok = a.eval("%s('chatRow') && %s('panChat') && %s('members') === false" % (VISIBLE, VISIBLE, VISIBLE))
    h_chat = a.eval("document.getElementById('panChat').offsetHeight")
    show_tab(a, 'settings')
    set_ok = a.eval("%s('sndPeerRow') && %s('panChat') === false" % (VISIBLE, VISIBLE))
    h_set = a.eval("document.getElementById('panSettings').offsetHeight")
    # 設定タブをもう一度押すと直前のタブ（ひとこと）に戻る（トグルで閉じる）
    show_tab(a, 'settings')
    toggle_ok = a.eval("%s('panChat') && %s('sndPeerRow') === false" % (VISIBLE, VISIBLE))
    show_tab(a, 'settings')   # 設定に戻しておく
    # ★ぴったり一致は求めない。**経路の凡例が参加直後だけ開いている**（8秒で畳む）ので、
    #   メンバーパネルの高さはそのぶん十数px動く。見ているのは「タブを切り替えたときに
    #   カードがガクッと動かないこと」なので、その範囲に収まっているかで判定する。
    same_h = max(h_members, h_chat, h_set) - min(h_members, h_chat, h_set) <= 20 and h_members > 200
    # 選択中のタブだけ aria-selected="true"（読み上げと見た目の両方がこれを見ている）
    aria = a.eval("[...document.querySelectorAll('#tabs .tab')]"
                  ".filter(e => e.getAttribute('aria-selected') === 'true').map(e => e.id).join()")
    show_tab(a, 'members')
    back_ok = a.eval("%s('members')" % VISIBLE)
    # ★スマホ幅でタブが**折り返さず・はみ出さない**こと。実機（CSS幅390px）で
    #   「メンバ／ー」と割れた（2026-08-17）。はみ出すと設定タブが画面外に出て押せなくなる。
    #   ここは配信部屋（タブ4つ＝最も混む状態）でも確かめたいが、T30 は普通の部屋なので3つ。
    #   4つの場合は T31 側で見る。
    fits = []
    for w in (430, 390, 360, 320):
        a.call('Emulation.setDeviceMetricsOverride', width=w, height=900, deviceScaleFactor=2, mobile=True)
        time.sleep(0.3)
        fits.append(a.eval(TAB_FITS))
    a.call('Emulation.clearDeviceMetricsOverride')
    narrow_ok = all(fits)
    ok = default_ok and chat_ok and set_ok and toggle_ok and back_ok and aria == 'tabSettings' and same_h and narrow_ok
    r.check('T30', 'タブでメンバー／ひとこと／設定が入れ替わる', ok, 'pass',
            '既定=メンバー:%s / ひとこと:%s / 設定:%s / トグル閉じ=%s / 戻る:%s / aria-selected=%r / 高さが揃っている=%s(%s/%s/%s) / 狭い画面で1行=%s'
            % (default_ok, chat_ok, set_ok, toggle_ok, back_ok, aria, same_h, h_members, h_chat, h_set, fits))


def T35(r, a, b):
    """音量：**全端末 GainNode 経路**で効き、保存され、100% に戻せる

    ★2026-08-19 の実機で確定したこと：iOS 18.7 / Safari 26.6 の `audio.volume` は
    **代入がプロパティには反映されるのに音は変わらない**。だから「代入して読み返す」判定は
    成立せず、`.volume` に頼った実装は**画面からもログからも気づけないまま効かない**
    （最初にそれで出して iPhone で効かなかった）。いまは端末で分岐せず、
    音量は **`音源 → マスターGain → 出力`** だけで作る。ここで見るのは4つ：

      ・**100% では経路を張らない**（既定の人の鳴らし方は今までと同じ＝戻り道でもある）
      ・50% で経路が張られ、**`<audio>` は muted で残る**（srcObject は付いたまま。
        Safari はストリームを繋いだ要素が生きていないと Web Audio 側で鳴らない）
      ・**出口まで繋がっている**（忘れると画面は正常なのに完全な無音）
      ・**`.volume` に小数を代入していない**（＝iPhone で効かなかった原因そのものの再発防止）
    """
    show_tab(a, 'settings')
    seen = a.eval("%s('vol')" % VISIBLE)
    n = a.eval("document.querySelectorAll('audio:not(#appAudio)').length")
    # 既定（100%）は素の再生のまま＝経路を張らない・muted にもしない
    idle = a.eval('__T.srcGain') == 0 and a.eval("[...document.querySelectorAll('audio:not(#appAudio)')].every(e => !e.muted)")
    a.eval("(() => { const s = document.getElementById('vol'); s.value = '50';"
           " s.dispatchEvent(new Event('input')) })()")
    routed = a.wait_for('__T.srcGain >= 1', timeout=SHORT)
    gain = a.eval('__T.masterGain ? __T.masterGain.gain.value : None'.replace('None', 'null'))
    # マスターGain から**出口までたどり着けるか**（途中に appOut などが挟まってもよい）
    out = a.eval('''(() => {
      if (!__T.masterGain) return false;
      const seen = new Set(); const stack = [__T.masterGain];
      while (stack.length) {
        const n = stack.pop();
        if (!n || seen.has(n)) continue;
        seen.add(n);
        // 出口はスピーカー（AudioDestinationNode）とは限らない。いまは <audio> へ渡す
        // MediaStreamAudioDestinationNode が正規の出口（2026-09-12 の設計変更）。
        if (n instanceof AudioDestinationNode || n instanceof MediaStreamAudioDestinationNode) return true;
        __T.edges.forEach(([x, y]) => { if (x === n) stack.push(y) });
      }
      return false })()''')
    kept = a.eval("[...document.querySelectorAll('audio:not(#appAudio)')].every(e => e.muted && !!e.srcObject)")
    saved = a.eval("localStorage.getItem('pot-call-vol')")
    label = a.eval("document.getElementById('volVal').textContent")
    # 100% に戻す（**戻せないと iPhone が永久に無音になる**／テストの後始末としても必須）
    a.eval("(() => { const s = document.getElementById('vol'); s.value = '100';"
           " s.dispatchEvent(new Event('input')) })()")
    back = a.wait_for("[...document.querySelectorAll('audio:not(#appAudio)')].every(e => !e.muted)", timeout=SHORT)
    # ★`.volume` に小数を代入していないこと。iOS では値が入るのに音が変わらないので、
    #   ここに頼った実装は**テストでも実機でも「入っている」ように見えてしまう**。
    vol_set = a.eval('JSON.stringify(__T.volSet)')
    no_volume = a.eval('__T.volSet.every(v => v === 1)')
    errs = a.eval('__T.errors')
    show_tab(a, 'members')
    ok = (seen and n >= 1 and idle and routed and gain is not None and abs(gain - 0.5) < 0.01
          and out and kept and back and saved == '0.5' and label == '50%' and no_volume and not errs)
    r.check('T35', '音量が GainNode 経路で効き、100% に戻せる', ok, 'pass',
            'スライダーが見える=%s / 音声要素=%s / 100%%では張らない=%s / 50%%で繋いだ=%s / gain=%s / 出口までたどれる=%s / muted で残る=%s / 100%%で戻る=%s / 保存=%r / 表示=%r / .volume に頼っていない=%s(%s) / エラー=%s'
            % (seen, n, idle, routed, gain, out, kept, back, saved, label, no_volume, vol_set, errs or 'なし'))


def T37(r, room):
    """BGM：選ぶと鳴り、**退出で止まる**（相手は要らないので1タブで見る）

    音そのものはヘッドレスでは聞けないので、**発振器が増え続けているか**で「鳴っている」を見る
    （通知音と同じ見方＝`__T.osc`）。ここで固定したいのは3つ:

      ・**既定は「なし」**（通話アプリで勝手に音が鳴るのは事故）
      ・選ぶと鳴り、「なし」に戻すと**止まる**
      ・**退出で止まる**。BGM は join が確保する資源なので、leave が畳まないと
        退出後も鳴り続ける（`stopVad` が AudioContext を閉じる前に畳む必要もある）
    """
    t = r.open_tab()
    t.eval("document.getElementById('room').value = %s" % js_str(room + 'BGM'))
    click_join(t)
    joined = t.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    show_tab(t, 'settings')
    off_default = t.eval("document.getElementById('bgmOff').checked") and \
        t.eval("document.getElementById('bgmVolRow').hidden")
    base = t.eval('__T.osc')
    time.sleep(2)
    silent = t.eval('__T.osc') == base                      # 既定では何も鳴らさない
    # 「軽快な音楽」を人と同じ経路で選ぶ
    t.eval("(() => { const b = document.getElementById('bgmTune'); b.checked = true; b.onchange() })()",
           await_promise=False)
    playing = t.wait_for('__T.osc > %d' % base, timeout=SHORT)
    shown = t.eval("!document.getElementById('bgmVolRow').hidden")   # BGM の音量が出る
    saved = t.eval("localStorage.getItem('pot-call-bgm')")
    # 鳴らしたまま退出 → **止まること**
    click_leave(t)
    t.wait_for("document.getElementById('tabs').hidden", timeout=SHORT)
    time.sleep(1.5)
    after = t.eval('__T.osc')
    time.sleep(3)
    stopped = t.eval('__T.osc') == after
    errs = t.eval('__T.errors')
    ok = joined and off_default and silent and playing and shown and saved == 'tune' and stopped and not errs
    r.check('T37', 'BGM は選ぶと鳴り、退出で止まる', ok, 'pass',
            '参加=%s / 既定はなし=%s / 既定は無音=%s / 鳴った=%s / BGM音量が出る=%s / 保存=%r / 退出で止まる=%s / エラー=%s'
            % (joined, off_default, silent, playing, shown, saved, stopped, errs or 'なし'))


def T38(r, room):
    """小窓（PiP）：出せて、**退出で必ず畳まれる**（相手は要らないので1タブで見る）

    2026-09-10・iPhone 実機で分かったこと：**小窓を出している間は他アプリを見ていても
    ページが前面扱いのまま生き続け、マイクが止まらない**。スマホで「アプリを切り替えると
    通話が切れる」に効く唯一の手なので、壊れたら気づけるようにしておく。

    ★PiP の要求は**押した瞬間に同期で**呼ばないと NotAllowedError で弾かれる。だから
      テスト側も `userGesture=True` で「本物のタップ」として押す（普通の eval では入れない）。
    ★見たいのは対称性：join が作った映像トラックを leave が畳むこと。
      畳み忘れると、退出後も小窓が浮いたまま・カメラ映像用の資源が残る。
    """
    t = r.open_tab()
    # ★小窓は保留中の機能（未コミット）。**入っていないビルドでは黙って飛ばす**
    #   （テストだけ残って赤くなると、本当の回帰と見分けがつかなくなる）
    if not t.eval("!!document.getElementById('pip')"):
        print('  ⏭  SKIP  T38 小窓（この版には入っていない）')
        return
    t.eval("document.getElementById('room').value = %s" % js_str(room + 'PIP'))
    click_join(t, gesture=True)
    joined = t.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    shown = t.wait_for("!document.getElementById('pip').hidden", timeout=SHORT)
    # ★参加後に**3秒ぶん録って Blob に差し替える**。iPhone の小窓は**ストリームだと真っ黒**で、
    #   **録った動画（ファイルに近いもの）でないと映らない**（2026-09-13・実機で確定）。
    #   ここが崩れると「小窓は出るが中身が黒い」に逆戻りする。
    swapped = t.wait_for("document.getElementById('pipVideo').src.startsWith('blob:')", timeout=SHORT)
    # 押す前に再生済みであること（**再生していないと PiP に入れない**。src 差し替え直後は
    # まだ中身が無いので、loadeddata を待ってから再生している）
    ready = t.wait_for("(() => { const v = document.getElementById('pipVideo');"
                       " return v.readyState >= 2 && !v.paused })()", timeout=SHORT)
    t.call('Runtime.evaluate', expression="document.getElementById('pip').click()", userGesture=True)
    entered = t.wait_for("!!document.pictureInPictureElement", timeout=SHORT)
    label = t.eval("document.getElementById('pip').textContent")
    # ★小窓は**映像だけ**。音は常に appAudio が受け持つ（小窓は音の盾にはならないと
    #   2026-09-13 に実測で確定したので、音を持たせる意味が無い）。
    #   小窓のストリームに音声トラックが混ざっていないことを見る（混ざると二重に鳴る）。
    # 録画に差し替わると srcObject は null になる（src = blob:）ので、**どちらの形でも
    # 音を持っていないこと**を見る。音は常に appAudio 側（小窓は音に関与しない）。
    audio_on = t.eval("(() => { const v = document.getElementById('pipVideo');"
                      " return v.muted && (!v.srcObject || v.srcObject.getAudioTracks().length === 0) })()")
    # ★小窓の**中身が描かれているか**。入れたかどうかだけ見ていると「真っ黒の小窓」に気づけない
    #   （2026-09-12・実機で実際に真っ黒だった）。映像の1フレームを取り出して色を数える。
    painted = t.eval('''(async () => {
      const v = document.getElementById('pipVideo');
      await new Promise(r => setTimeout(r, 700));
      const c = document.createElement('canvas');
      c.width = v.videoWidth || 0; c.height = v.videoHeight || 0;
      if (!c.width) return 'videoWidth=0';
      c.getContext('2d').drawImage(v, 0, 0);
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      const seen = new Set();
      for (let i = 0; i < d.length; i += 4 * 97) seen.add(d[i] + ',' + d[i+1] + ',' + d[i+2]);
      return seen.size + '色';
    })()''')
    # 退出 → 小窓が閉じ、映像トラックも捨てられていること
    t.call('Runtime.evaluate', expression=LEAVE_CLICK, userGesture=True)
    t.wait_for("document.getElementById('tabs').hidden", timeout=SHORT)
    time.sleep(1)
    closed = t.eval("!document.pictureInPictureElement")
    audio_off = t.eval("!document.getElementById('appAudio').muted")   # 出口はずっと鳴らす側のまま
    torn = t.eval("document.getElementById('pipVideo').srcObject === null")
    gone = t.eval("document.getElementById('pip').hidden")
    errs = t.eval('__T.errors')
    # 小窓の絵に**部屋名**が描かれていて、部屋 ID（ハッシュ）が描かれていないこと（v0.14.5）
    texts = t.eval("__T.canvasText")
    rid = t.eval("(location.hash.match(/room=([^&]+)/) || [])[1] || ''")
    name_drawn = any((room + 'PIP') in x for x in texts)
    hash_drawn = bool(rid) and any(rid[:8] in x for x in texts)
    ok = (joined and shown and swapped and ready and entered and 'とじる' in label and closed and torn and gone
          and audio_on and audio_off and not errs
          and isinstance(painted, str) and painted.endswith('色') and int(painted[:-1]) >= 3
          and name_drawn and not hash_drawn)
    r.check('T38', '小窓（PiP）が出せて、退出で畳まれる（絵は部屋名・ハッシュではない）', ok, 'pass',
            '参加=%s / ボタン=%s / 録画に差し替わった=%s / 映像が再生済み=%s / 小窓に入った=%s / 小窓は映像だけ=%s / 中身が描かれている=%s / ラベル=%r / 退出で閉じる=%s / 出口が戻る=%s / 映像を捨てた=%s / ボタンが消える=%s / 絵に部屋名=%s / 絵にハッシュ=%s / エラー=%s'
            % (joined, shown, swapped, ready, entered, audio_on, painted, label, closed, audio_off, torn, gone, name_drawn, hash_drawn, errs or 'なし'))


def T39(r, a):
    """マイクの取得条件を**明示している**（`{audio:true}` に戻っていないこと）

    2026-09-12：Android の一部機種で音が回り込む（ハウリング）という報告。既定任せだと
    端末やブラウザで挙動が変わるので、**echoCancellation / noiseSuppression を明示**した。
    書いても既定と同じ挙動なので**画面では絶対に気づけない**＝ここで固定しておく。
    `{audio:true}` に戻す変更が入ったら落ちる。
    """
    args = a.eval('JSON.stringify(__T.gumArgs[0] || null)')
    try:
        c = (json.loads(args) or {}).get('audio')
    except Exception:
        c = None
    ok = isinstance(c, dict) and c.get('echoCancellation') is True and c.get('noiseSuppression') is True
    r.check('T39', 'マイクの取得条件を明示している', ok, 'pass', '渡した条件=%s' % args)


def T40(r):
    """最初の画面：**名前 → 部屋を作る（畳んである）→ ロビー**の順で、余計なものが出ていない

    2026-09-12・利用者の指定。「名前を決めて部屋を選ぶ」を主導線にしたいので、
    部屋を新しく作る側は畳む。**共有リンクから来た人だけ開いた状態**にする
    （畳んだままだと「参加する」が隠れて、リンクを踏んだのに入れない画面になる）。
    ★版表示は**畳む範囲の外**に出すこと。中に入れると β から来た人の「安定版へ」が消える。
    """
    t = r.open_tab()
    closed = t.eval("!document.getElementById('createBox').open")
    idle_hidden = t.eval("document.getElementById('statusCard').hidden")
    share_hidden = t.eval("document.getElementById('shareFlip').hidden")
    ver_shown = t.eval("!!document.querySelector('.ver').offsetParent")   # 版はいつでも見える
    join_in = t.eval("document.getElementById('createBox').contains(document.getElementById('join'))")
    # 閉じている間の見出しは「部屋を作る」
    label_closed = t.eval("document.querySelector('#createBox > summary').innerText").strip()
    # ★枠の扱いは**畳んでいるときと開いたときで違う**（CSS の .create のコメントに経緯がある）。
    #   2026-09-12 は「枠にしない」が利用者の指定だったが、2026-09-27 に利用者自身が
    #   「畳んだ状態を少し目立たせたい」に変えた（v0.14.56）。折衷として**畳んでいる間だけ**角丸の枠を付け、
    #   開いたら 09-12 のまま素の「部屋名」ラベルに戻す。両方を見る（片方だけだと元に戻っても気づけない）。
    framed_closed = t.eval("(() => { const s = getComputedStyle(document.querySelector('#createBox > summary'));"
                           " return s.borderTopWidth !== '0px' &&"
                           " s.backgroundColor !== 'rgba(0, 0, 0, 0)' && s.backgroundColor !== 'transparent' })()")
    # 開けば招待リンクも出る（作る前に配る導線＝配信部屋のため）
    t.eval("document.querySelector('#createBox > summary').click()")
    # 招待は見出しの「シェア」（押すとカードが裏返る・2026-11-09 に通話中とそろえた）
    opened = t.wait_for("!document.getElementById('shareFlip').hidden && !!document.getElementById('shareFlip').offsetParent", timeout=SHORT)
    # ★開いたら見出しが「部屋名」ラベルに変身する（プロフィールと同じ作法・利用者の指定）。
    #   中に同じラベルを二重に置かないので、ここが崩れると部屋名の見出しが消える。
    label_open = t.eval("document.querySelector('#createBox > summary').innerText").replace('\n', ' ')
    morphed = '部屋名' in label_open and '部屋を作る' not in label_open and '部屋を作る' in label_closed
    # 開いたら枠を外して入力欄に繋げる（2026-09-12 の指定はここが生きている）
    plain_open = t.eval("(() => { const s = getComputedStyle(document.querySelector('#createBox > summary'));"
                        " return s.borderTopWidth === '0px' &&"
                        " (s.backgroundColor === 'rgba(0, 0, 0, 0)' || s.backgroundColor === 'transparent') })()")
    # 共有リンクから来た人は最初から開いている
    t2 = r.open_tab(hash_='#' + urllib.parse.quote('テスト部屋'))
    from_link = t2.eval("document.getElementById('createBox').open")
    ok = (closed and idle_hidden and share_hidden and ver_shown and join_in and opened
          and morphed and framed_closed and plain_open and from_link)
    r.check('T40', '最初の画面は「名前→部屋を作る→ロビー」', ok, 'pass',
            '畳んである=%s / 状態カード非表示=%s / 招待リンク非表示=%s / 版は見える=%s / 参加ボタンが中=%s / 開くと招待リンク=%s / 見出しが部屋名に変わる=%s(%r) / 畳んだら角丸の枠=%s / 開いたら枠なし=%s / 共有リンクから来たら開く=%s'
            % (closed, idle_hidden, share_hidden, ver_shown, join_in, opened, morphed, label_open, framed_closed, plain_open, from_link))


def T43(r):
    """見た目が時刻で変わる（v0.11.0）：正午は白い面・深夜は紺／固定を選べば時刻に関係なく同じ／文字は読める

    面（--panel）は時刻で補間、墨（--text）は面の明るさで入れ替わる。この2つが同じ時刻で
    食い違う（白い面に白い文字）と読めなくなるので、正午と深夜で本文/面の比を実測する。
    エンジンは <style> 直後の classic script。apply(mode, 分) で設定と時計を差し替えられる。
    """
    t = r.open_tab()
    has = t.eval("typeof window.__potTheme === 'object' && typeof window.__potTheme.apply === 'function'")
    var = lambda name: t.eval("getComputedStyle(document.documentElement).getPropertyValue('%s').trim()" % name)

    def lum(h):
        h = h.lstrip('#'); c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4 for v in c]
        return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]

    def ratio(a, b):
        x, y = sorted([lum(a), lum(b)], reverse=True); return (x + .05) / (y + .05)

    t.eval("window.__potTheme.apply('time', 12 * 60)")
    noon_panel, noon_text = var('--panel'), var('--text')
    noon_scheme = t.eval("document.documentElement.style.colorScheme")
    noon_meta = t.eval("document.querySelector('meta[name=theme-color]').content")
    t.eval("window.__potTheme.apply('time', 0)")
    mid_panel, mid_text = var('--panel'), var('--text')
    mid_scheme = t.eval("document.documentElement.style.colorScheme")
    mid_meta = t.eval("document.querySelector('meta[name=theme-color]').content")
    # 朝夕の入れ替わりの途中でも読めること（5分刻みで一日をなぞって最小の比を取る）
    worst = 99
    for m in range(0, 1440, 5):
        t.eval("window.__potTheme.apply('time', %d)" % m)
        worst = min(worst, ratio(var('--text'), var('--panel')))
    # 固定を選べば時刻に関係なく同じ／設定は覚える
    t.eval("window.__potTheme.set('night'); window.__potTheme.apply('night', 12 * 60)")
    fixed = var('--panel') == '#0d1526'
    remembered = t.eval("localStorage.getItem('pot-call-theme')") == 'night'
    t.eval("localStorage.removeItem('pot-call-theme'); window.__potTheme.apply()")
    ok = (has and noon_panel == '#ffffff' and mid_panel == '#0d1526' and noon_scheme == 'light' and mid_scheme == 'dark'
          and noon_meta != mid_meta and ratio(noon_text, noon_panel) >= 4.5 and ratio(mid_text, mid_panel) >= 4.5
          and worst >= 4.5 and fixed and remembered)
    r.check('T43', '見た目が時刻で変わり、固定も選べる', ok, 'pass',
            'エンジン=%s / 正午の面=%s(%s, 比%.1f) / 深夜の面=%s(%s, 比%.1f) / 一日で最も低い比=%.1f / theme-color追従=%s / 夜固定=%s / 覚える=%s'
            % (has, noon_panel, noon_scheme, ratio(noon_text, noon_panel), mid_panel, mid_scheme, ratio(mid_text, mid_panel),
               worst, noon_meta != mid_meta, fixed, remembered))


def T44(r):
    """指紋付きのリンクで来たら、誰であれ「配信部屋／雑談部屋」の2択を出さない（v0.11.5）

    2026-09-17・利用者の指摘。①オーナー本人が自分のリンクを開いても2択は出ず、📣 が選択状態
    ②リスナーには出ない ③リスナーが**名前を変えたら**出る（別の部屋を作る）＋リンクから指紋が落ちる
    ④**元の名前に戻せば**指紋が戻って2択も消える（iPhone の IME は見た目が変わらなくても input を
    飛ばすので、「触っただけで普通の部屋に化ける」を防ぐ）
    """
    t0 = r.open_tab()
    t0.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    t0.eval("(() => { document.getElementById('createBox').open = true; const i = document.getElementById('room');"
            " i.value = '指紋テスト'; i.dispatchEvent(new Event('input')); document.getElementById('kindBc').click() })()")
    assert t0.wait_for("document.getElementById('fbUrl').href.includes('~pk~')", timeout=SHORT), '配信部屋の鍵ができない'
    hash_ = '#' + t0.eval("document.getElementById('fbUrl').href").split('#', 1)[1]
    click_join(t0)   # オーナーが参加＝部屋が開いている（リスナー側の「入口の確認」が live になる）
    assert t0.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), 'オーナーが参加できない'
    row = "document.getElementById('kindRow').hidden"
    pk = "document.getElementById('fbUrl').href.includes('~pk~')"
    lbl = "document.getElementById('joinLbl').textContent"
    # ① オーナー本人
    t1 = r.open_tab(hash_=hash_)
    owner_hidden = t1.eval(row); owner_bc = t1.eval("document.getElementById('kindBc').checked")
    owner_lbl = t1.eval(lbl)
    # ② リスナー（鍵を消す＝別の端末）
    t1.eval("window.__potBcErase(true)")
    t2 = r.open_tab(hash_=hash_)
    # リンクで来たので入口の確認が入る。開いている部屋なので、確認が終わればボタンが押せる状態に戻る
    entry_live = t2.wait_for("!document.getElementById('join').disabled && !document.getElementById('join').hidden", timeout=DISCOVER)
    listener_hidden = t2.eval(row); listener_pk = t2.eval(pk); listener_lbl = t2.eval(lbl)
    # 触っただけ（同じ値で input）→ 変わらない
    t2.eval("(() => { const i = document.getElementById('room'); i.dispatchEvent(new Event('input')) })()")
    touched_hidden = t2.eval(row); touched_pk = t2.eval(pk)
    # ③ 名前を変えた → 2択が出て指紋が落ちる
    t2.eval("(() => { const i = document.getElementById('room'); i.value = '指紋テスト2'; i.dispatchEvent(new Event('input')) })()")
    renamed_shown = not t2.eval(row); renamed_pk = t2.eval(pk); renamed_lbl = t2.eval(lbl)
    # ④ 元に戻した → 指紋が戻って2択が消える
    t2.eval("(() => { const i = document.getElementById('room'); i.value = '指紋テスト'; i.dispatchEvent(new Event('input')) })()")
    back_hidden = t2.eval(row); back_pk = t2.eval(pk)
    # ボタンの文言：オーナー本人は「配信を始める」（v0.14.56）、聞き役なら「リスナーとして参加する」（用語集は v0.16.19 で 聞き役→リスナー に変更）、改名後（🏠 で新しく作る）は「部屋を作る」
    labels = owner_lbl == '配信を始める' and listener_lbl == 'リスナーとして参加する' and renamed_lbl == '部屋を作る'
    ok = (owner_hidden and owner_bc and entry_live and listener_hidden and listener_pk and touched_hidden and touched_pk
          and renamed_shown and not renamed_pk and back_hidden and back_pk and labels)
    r.check('T44', '指紋付きのリンクでは部屋の種類を選ばせない（聞き役はボタンで分かる）', ok, 'pass',
            'オーナー:隠れる=%s/📣=%s / リスナー:開いていると分かった=%s/隠れる=%s/指紋=%s / 触っただけ:隠れたまま=%s/指紋=%s / 改名:出る=%s/指紋なし=%s / 戻す:隠れる=%s/指紋=%s / 文言=%s(%r/%r/%r)'
            % (owner_hidden, owner_bc, entry_live, listener_hidden, listener_pk, touched_hidden, touched_pk,
               renamed_shown, not renamed_pk, back_hidden, back_pk, labels, owner_lbl, listener_lbl, renamed_lbl))


def T45(r, room):
    """BGM：中断が明けたときに**作り直しは1回だけ**（「ダダダダダ」の再現・v0.11.9）

    iOS は他アプリへ切り替えると AudioContext を `interrupted` にし、`resume()` の約束は
    中断が明けるまで解決しない。その間、3秒ごとの起こし直し・再生イベント・visibilitychange が
    resume() を何本も積み、**明けた瞬間に全部が解決して startBgm() が連続で走る**＝最初の一拍が
    連射される（2026-09-18・利用者の「切れたときに BGM がダダダダダ」）。
    ヘッドレスに中断は無いので、resume() を「呼ばれたら保留・合図で一斉に解決」に差し替えて再現する。
    判定：中断明け 0.8 秒の発振器の増分が、1回の作り直しぶん（別に測る）の 1.6 倍以内。
    """
    t = r.open_tab()
    t.eval("document.getElementById('room').value = %s" % js_str(room + 'BGM2'))
    click_join(t)
    joined = t.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    show_tab(t, 'settings')
    t.eval("(() => { const b = document.getElementById('bgmTune'); b.checked = true; b.onchange() })()", await_promise=False)
    t.wait_for('__T.osc > 0', timeout=SHORT)
    # 中断を仕込む：resume() を保留にし、context も本当に止める
    t.eval("""(() => {
      const P = AudioContext.prototype, origResume = P.resume, origSuspend = P.suspend
      window.__pend = []; window.__ctx = null
      const co = P.createOscillator; P.createOscillator = function () { window.__ctx = this; return co.call(this) }
      P.resume = function () { return new Promise(res => window.__pend.push(() => origResume.call(this).then(res))) }
      window.__release = () => { const ps = window.__pend.splice(0); ps.forEach(f => f()) }
      window.__interrupt = () => origSuspend.call(window.__ctx)
    })()""")
    t.wait_for('!!window.__ctx', timeout=SHORT)

    def episode(triggers):
        t.eval("window.__interrupt()", await_promise=False)
        time.sleep(0.3)
        for _ in range(triggers):
            t.eval("document.getElementById('appAudio').dispatchEvent(new Event('play'))")
            time.sleep(0.05)
        time.sleep(0.3)
        pending = t.eval('window.__pend.length')
        before = t.eval('__T.osc')
        t.eval("window.__release()")
        time.sleep(0.8)
        return pending, t.eval('__T.osc') - before

    p1, one = episode(1)      # 1本だけ積んだとき＝作り直し1回ぶんの発振器の数
    time.sleep(1)
    p6, six = episode(6)      # 6本積んだとき（実機では 3 秒ごとに増える）
    ok = joined and one > 0 and six <= one * 1.6
    r.check('T45', 'BGM は中断が明けても作り直しは1回だけ', ok, 'pass',
            '参加=%s / 1本のとき: 保留=%d・発振器+%d / 6本のとき: 保留=%d・発振器+%d（上限 %.0f）'
            % (joined, p1, one, p6, six, one * 1.6))


def T46(r, room):
    """入口の確認（v0.12.0）：リンクで来た人は、部屋が開いているかロビーで見てから入る

    ①誰もいない部屋のリンク → 探している間はボタンが止まり、しばらくして「お休み中」の案内と
      「この名前で新しく開く」が出る（参加ボタンは隠れる）。開くを押せば従来どおり入れる
    ②開いている部屋のリンク → 「いま開いています」になり、ボタンがそのまま押せる。文言は「参加する」
    ③自分で部屋名を打った人（hash 無し）は確認そのものが無い（即座に押せる）
    """
    dead = room + 'DEAD'
    t = r.open_tab(hash_='#' + urllib.parse.quote(dead))
    # 初回訪問はヘルプ（モーダル）が開く。開いたままだと後ろの入力欄にフォーカスできない（実機でも先に閉じる）
    t.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    checking = t.eval("document.getElementById('join').disabled") and \
        t.eval("document.getElementById('joinLbl').textContent") == 'この部屋を探しています…'
    asleep = t.wait_for("!!document.getElementById('entryOpen').offsetParent", timeout=DISCOVER)
    # ★属性ではなく**実際に描かれていない**ことを見る。#join は display:flex なので [hidden] を
    #   CSS に書き忘れると属性だけ付いて見えたまま（v0.12.0 の実装でまさにそうなった）
    join_hidden = t.eval("document.getElementById('join').hidden && !document.getElementById('join').offsetParent")
    note = t.eval("document.getElementById('entryText').textContent")
    # 「部屋の名前をつけて参加」→ 参加ボタンが戻り、部屋名の欄にフォーカス（すぐには入らない）
    t.eval("document.getElementById('entryOpen').click()", await_promise=False)
    returned = t.eval("(() => { const j = document.getElementById('join'); return !j.hidden && !j.disabled && document.activeElement === document.getElementById('room') })()")
    note_gone = t.eval("document.getElementById('entryNote').hidden")
    t.eval("document.getElementById('join').click()", await_promise=False)
    opened = t.wait_for("document.getElementById('state').textContent.includes('参加中')", timeout=DISCOVER)
    # ② その部屋はいま開いている → 別のタブでリンクを開くと「開いています」
    t2 = r.open_tab(hash_='#' + urllib.parse.quote(dead))
    live = t2.wait_for("!document.getElementById('join').disabled && !document.getElementById('join').hidden", timeout=DISCOVER)
    live_note = t2.eval("document.getElementById('entryText').textContent")
    live_lbl = t2.eval("document.getElementById('joinLbl').textContent")
    # ③ hash 無し
    t3 = r.open_tab()
    plain = not t3.eval("document.getElementById('join').disabled") and t3.eval("document.getElementById('entryNote').hidden")
    ok = (checking and asleep and join_hidden and '誰もいません' in note and returned and opened and note_gone
          and live and '開いています' in live_note and live_lbl == '参加する' and plain)
    r.check('T46', 'リンクで来た人は、部屋が開いているか見てから入る', ok, 'pass',
            '探している=%s / お休み中の案内=%s(%r) / 参加ボタンは隠れる=%s / 名前をつけて参加→ボタンが戻り欄にフォーカス=%s / 案内が消えた=%s / 参加できた=%s / 開いている部屋=%s(%r, %r) / 自分で打った人は即座=%s'
            % (checking, asleep, note[:22], join_hidden, returned, note_gone, opened, live, live_note, live_lbl, plain))


def T47(r, room):
    """部屋の実体はハッシュ（v0.13.0）：同名の部屋が共存し、リンクで来た人は同じ部屋に入り、名前は受け取る

    ①同じ名前で2つ作ると**別の部屋**（ハッシュが違う・互いに会わない・ロビーに2つ並ぶ）
    ②リンク（#room=ハッシュ&name=名前）で来た人は**その部屋**に入る（URL の room= が一致）
    ③名前を持たないリンク（#room=ハッシュ だけ）で来ても、開いていれば**名前を受け取って**入れる
    ④BGM のシードはハッシュ＝同名でも別の曲になり得る（ここでは ID が違うことだけ見る）
    """
    name = room + 'DUP'
    a = r.open_tab()
    a.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    a.eval("(() => { const i = document.getElementById('room'); i.value = %s; i.dispatchEvent(new Event('input')) })()" % js_str(name))
    click_join(a)
    assert a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), 'A が参加できない'
    link_a = a.eval("location.hash")
    # ① 同じ名前でもう1つ作る（リンクを使わず名前を打つ）
    b = r.open_tab()
    b.eval("(() => { const i = document.getElementById('room'); i.value = %s; i.dispatchEvent(new Event('input')) })()" % js_str(name))
    click_join(b)
    assert b.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), 'B が参加できない'
    link_b = b.eval("location.hash")
    room_a = link_a.split('#room=')[1].split('&')[0]; room_b = link_b.split('#room=')[1].split('&')[0]
    distinct = room_a != room_b and len(room_a) == 16 and len(room_b) == 16
    time.sleep(8)
    alone = a.eval(MEMBERS) == 1 and b.eval(MEMBERS) == 1           # 互いに会わない
    # ロビー（第三のタブ）に同名の部屋が2つ並ぶ
    c = r.open_tab()
    two = c.wait_for("[...document.querySelectorAll('#lobbyList .room-name')].filter(e => e.textContent.includes(%s)).length === 2" % js_str(name), timeout=DISCOVER)
    # ② A のリンクで来た人は A の部屋へ
    d = r.open_tab(hash_=link_a)
    click_join(d)
    joined_a = a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    same_id = d.eval("location.hash").split('&')[0] == link_a.split('&')[0]
    # ③ 名前を持たないリンク（room= だけ）で来ても、開いていれば名前を受け取って入れる
    e = r.open_tab(hash_=link_b.split('&')[0])
    got_name = e.wait_for("document.getElementById('room').value === %s" % js_str(name), timeout=DISCOVER)
    click_join(e)
    joined_b = b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    label_e = e.eval("document.getElementById('state').textContent")
    ok = distinct and alone and two and joined_a and same_id and got_name and joined_b and name in label_e
    r.check('T47', '部屋の実体はハッシュ（同名共存・リンクで合流・名前を受け取る）', ok, 'pass',
            '別々のハッシュ=%s / 互いに会わない=%s / ロビーに2つ=%s / Aのリンクで合流=%s(同じID=%s) / 名前なしリンクで名前を受け取る=%s / 合流=%s / 表示=%r'
            % (distinct, alone, two, joined_a, same_id, got_name, joined_b, label_e[:30]))
    for t in (a, b, d, e):
        click_leave(t)


def T48(r, room):
    """話題タグ（v0.13.1）：付けると相手にも届き、ロビーはタグを大きく・部屋名を小さく・付けた人を出す

    ①参加前はタグの行が出ない ②A が付けると A・B の札に話題と付けた人の名前 ③ロビー（第三のタブ）で
    タグが見出しになり、部屋名と「◯◯が付けた」が小さく添えられる ④B が上書きすると A も新しいほうになる
    ⑤空にすると消える
    """
    name = room + 'TAG'
    a = r.open_tab()
    a.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    before = a.eval("document.getElementById('tagRow').hidden")
    who = a.eval("document.getElementById('name').value") or 'ゲスト'   # A の名前（アプリが自動で付けたもの）
    r.join(a, name)
    assert a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), 'A が参加できない'
    b = r.open_tab()
    r.join(b, name)
    assert a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER), 'B が合流できない'
    shown = not a.eval("document.getElementById('tagRow').hidden")
    # ② A が付ける
    a.eval("(() => { document.getElementById('tagEdit').click(); const i = document.getElementById('tagInput'); i.value = 'スカイラインGTR'; document.getElementById('tagSend').click() })()")
    TXT = "document.getElementById('tagText').textContent"
    a_has = a.wait_for("%s.includes('スカイラインGTR')" % TXT, timeout=SHORT)
    b_has = b.wait_for("%s.includes('スカイラインGTR') && %s.includes(%s)" % (TXT, TXT, js_str(who)), timeout=SHORT)
    # ③ ロビー
    c = r.open_tab()
    lobby = c.wait_for("[...document.querySelectorAll('#lobbyList .room-name')].some(e => e.textContent === '🏷 スカイラインGTR')", timeout=DISCOVER)
    sub = c.eval("(() => { const e = document.querySelector('#lobbyList .room-sub'); return e ? e.textContent : '' })()")
    # ③b 変換中の Enter（IME の確定）では送らない：isComposing 付きの keydown を投げても値はそのまま
    a.eval("(() => { document.getElementById('tagEdit').click(); const i = document.getElementById('tagInput'); i.value = 'へんかんちゅう';"
           " i.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', isComposing:true, bubbles:true})) })()")
    time.sleep(0.5)
    ime_safe = not a.eval("document.getElementById('tagForm').hidden") and a.eval("%s.includes('スカイラインGTR')" % TXT)
    a.eval("document.getElementById('tagForm').hidden = true")
    # ④ B が上書き
    b.eval("(() => { document.getElementById('tagEdit').click(); const i = document.getElementById('tagInput'); i.value = '初恋トーク'; document.getElementById('tagSend').click() })()")
    a_new = a.wait_for("%s.includes('初恋トーク')" % TXT, timeout=SHORT)
    # ⑤ 空にすると消える
    a.eval("(() => { document.getElementById('tagEdit').click(); const i = document.getElementById('tagInput'); i.value = ''; document.getElementById('tagSend').click() })()")
    cleared = b.wait_for("%s.includes('話題をつける')" % TXT, timeout=SHORT)   # v0.16.25 試作：無いときは札に「話題をつける」
    ok = before and shown and a_has and b_has and lobby and name in sub and who in sub and ime_safe and a_new and cleared
    r.check('T48', '話題タグが相手とロビーに届く', ok, 'pass',
            '参加前は出ない=%s / 参加中は出る=%s / A=%s / B（付けた人つき）=%s / ロビーでタグが見出し=%s（添え=%r） / 変換中のEnterで送らない=%s / 上書き=%s / 空で消える=%s'
            % (before, shown, a_has, b_has, lobby, sub[:40], ime_safe, a_new, cleared))
    for t in (a, b):
        click_leave(t)


def T49(r, a, b):
    """ひとことの未読の点（v0.13.4）：別のタブを見ている間に届くと 💬 タブに点が出て、開くと消える

    ★T25 のあとに走る（A・B は通話中）。B をメンバータブに戻してから A が送る。
    """
    show_tab(b, 'chat')      # 直前のテストで届いたぶんを既読にしてから
    show_tab(b, 'members')
    DOT = "document.getElementById('tabChat').classList.contains('unread') && !!document.querySelector('#tabChat .t-dot').offsetParent"
    before = not b.eval("document.getElementById('tabChat').classList.contains('unread')")
    time.sleep(2.5)   # 受信側は同じ相手からの連投を CHAT_ACCEPT_MS で間引く。直前の T25 の送信から間を空ける
    a.eval("(() => { const i = document.getElementById('chatText'); i.value = 'みてる？'; document.getElementById('chatSend').click() })()")
    lit = b.wait_for(DOT, timeout=SHORT)
    # 自分が送ったぶんでは点かない
    self_quiet = not a.eval("document.getElementById('tabChat').classList.contains('unread')")
    show_tab(b, 'chat')
    cleared = b.wait_for("!document.getElementById('tabChat').classList.contains('unread')", timeout=SHORT)
    # 見ている間に届いたものは点かない
    time.sleep(2.5)
    a.eval("(() => { const i = document.getElementById('chatText'); i.value = 'もういっこ'; document.getElementById('chatSend').click() })()")
    arrived = b.wait_for("document.getElementById('chatLog').textContent.includes('もういっこ')", timeout=SHORT)
    still = arrived and not b.eval("document.getElementById('tabChat').classList.contains('unread')")
    show_tab(b, 'members')
    ok = before and lit and self_quiet and cleared and still
    r.check('T49', 'ひとことの未読の点が出て、開くと消える', ok, 'pass',
            '最初は無い=%s / 届いたら点く=%s / 自分の送信では点かない=%s / 開くと消える=%s / 見ている間は点かない=%s'
            % (before, lit, self_quiet, cleared, still))


def T55(r, a, b):
    """ひとことの読み上げ v2（v0.14.42）：配信部屋のオーナーだけに設定が出て、オンで見本を読み、リスナーのひとことを
    「名前、文面」で読む（URL は「URL省略」・絵文字は落ちる）。リスナー側は何も合成しない。オフで読まない。設定は保存しない（v0.14.50）。
    ロビーのカードにも つくよみ のチップが出る（v0.15.0・人数には数えない・オフで消える）。

    ★T27 の直後・T27b（つなぎ直しを繰り返す）の前に走る（a＝配信者・b＝聞き役・どちらも通話中のまま）。
    実エンジン（100MB）は落とさず、INSTRUMENT の差し替え口 window.__potTts（偽エンジン）で判定する（__T.ttsTexts）。
    """
    if not a or not b:
        return
    show_tab(a, 'settings'); show_tab(b, 'settings')
    row_owner = a.wait_for("!document.getElementById('sndTtsRow').hidden && !!document.getElementById('sndTtsRow').offsetParent", timeout=SHORT)
    row_listener = b.eval("document.getElementById('sndTtsRow').hidden")
    n0 = a.eval("window.__T.ttsTexts.length")
    a.eval("document.getElementById('sndTts').click()")
    sample = a.wait_for("window.__T.ttsTexts.length > %d" % n0, timeout=SHORT)   # オンにした操作で見本を1回読む
    saved = a.eval("localStorage.getItem('pot-call-snd-tts2') === null && localStorage.getItem('pot-call-snd-tts') === null")   # v0.14.50：保存しない
    note = a.eval("!document.getElementById('sndTtsNote').hidden")
    # 段階②：聞き役に案内が届き（{ok} を返し）2 本目のトラックが載って仮想行 🗣 が出る。配信者の行も残っている
    vrow_b = b.wait_for("!!document.querySelector('#members .member.virtual')", timeout=SHORT)
    two = b.wait_for("%s >= 2" % AUDIOS, timeout=SHORT + 10)
    vrow_a = a.eval("!!document.querySelector('#members .member.virtual')")
    face = b.eval("(() => { const i = document.querySelector('#members .member.virtual img.m-face'); return !!i && i.getAttribute('src') === 'img/tsukuyomi-128.png' })()")   # 顔グラフィック（v0.14.46）
    # ロビーのカードにも つくよみ のチップが出る（v0.15.0）。配信者の在室の tt を見て描くので、聞き役の画面でも出る。
    # 人数には数えない（つくよみを足して「3人」になっていないこと）
    lobby_chip = b.wait_for("(() => { const c = document.querySelector('#lobbyList .room.here'); if (!c) return false;"
                            " const i = c.querySelector('img.rp-face');"
                            " return !!i && i.getAttribute('src') === 'img/tsukuyomi-128.png' && /つくよみ/.test(i.closest('.rp').textContent) })()", timeout=SHORT)
    lobby_count = b.eval("(() => { const c = document.querySelector('#lobbyList .room.here'); return c ? c.querySelector('.room-count').textContent : '' })()").startswith('2人')
    # 配信者自身の画面でも自分の部屋のカードに出る（自分の在室は renderLobby が合成して足す経路＝聞き役とは別）
    lobby_chip_a = a.wait_for("(() => { const c = document.querySelector('#lobbyList .room.here'); if (!c) return false;"
                              " const i = c.querySelector('img.rp-face'); return !!i && /つくよみ/.test(i.closest('.rp').textContent) })()", timeout=SHORT)
    n1 = a.eval("window.__T.ttsTexts.length")
    time.sleep(1.5)
    show_tab(b, 'chat')
    b.eval("(() => { const i = document.getElementById('chatText'); i.value = 'こんにちは🎉 https://example.com/a?b=1 です'; document.getElementById('chatSend').click() })()")
    arrived = a.wait_for("window.__T.ttsTexts.length > %d" % n1, timeout=SHORT + 10)
    text = a.eval("window.__T.ttsTexts[window.__T.ttsTexts.length - 1]")
    shape = bool(text) and text.endswith('、こんにちは URL省略 です') and '🎉' not in text and 'example' not in text
    lit = b.wait_for("document.querySelector('#members .member:first-child').classList.contains('speaking')", timeout=SHORT)   # {now}＝自分の行が光る
    # 同じ人が続けて送ったら名前を省く（v0.14.53）
    time.sleep(2.5)
    n1b = a.eval("window.__T.ttsTexts.length")
    b.eval("(() => { const i = document.getElementById('chatText'); i.value = 'つづけて'; document.getElementById('chatSend').click() })()")
    a.wait_for("window.__T.ttsTexts.length > %d" % n1b, timeout=SHORT + 10)
    noname = a.eval("window.__T.ttsTexts[window.__T.ttsTexts.length - 1]") == 'つづけて'
    # 「名前は読まない」スイッチ（v0.14.55）：配信者自身のひとこと（別の投稿者＝本来は名前が付く）が名前なしで読まれる
    show_tab(a, 'settings')
    sw = a.eval("(() => { const r = document.getElementById('sndTtsNameRow'); const b = document.getElementById('sndTtsNoName'); b.click(); return !r.hidden && b.checked })()")
    n1c = a.eval("window.__T.ttsTexts.length")
    show_tab(a, 'chat')
    a.eval("(() => { const i = document.getElementById('chatText'); i.value = 'じぶん'; document.getElementById('chatSend').click() })()")
    a.wait_for("window.__T.ttsTexts.length > %d" % n1c, timeout=SHORT)
    noname2 = sw and a.eval("window.__T.ttsTexts[window.__T.ttsTexts.length - 1]") == 'じぶん' and a.eval("localStorage.getItem('pot-call-tts-noname') === '1'")
    show_tab(a, 'settings'); a.eval("document.getElementById('sndTtsNoName').click()")   # 戻す
    # 英単語のカタカナ辞書（v0.14.60）：複数語（hello world）・CamelCase（WebRTC）・名前（qramo）・単語（test）
    # ＋日本語の読み辞書（v0.15.5・TTS_YOMI）：成る程 → なるほど（英字より先に置換され、英字辞書とも両立）
    n1d = a.eval("window.__T.ttsTexts.length")
    show_tab(a, 'chat')
    a.eval("(() => { const i = document.getElementById('chatText'); i.value = '成る程 hello worldとWebRTCの接続 qramo test'; document.getElementById('chatSend').click() })()")
    a.wait_for("window.__T.ttsTexts.length > %d" % n1d, timeout=SHORT)
    kana_text = a.eval("window.__T.ttsTexts[window.__T.ttsTexts.length - 1]")
    kana = kana_text.endswith('なるほど ハローワールドとウェブアールティーシーの接続 クラモ テスト')
    listener_quiet = b.eval("window.__T.ttsTexts.length") == 0   # 聞き役は合成しない
    # オフにすると読まない
    a.eval("document.getElementById('sndTts').click()")
    n2 = a.eval("window.__T.ttsTexts.length")
    time.sleep(2.5)   # 受信側の CHAT_ACCEPT_MS を空ける
    b.eval("(() => { const i = document.getElementById('chatText'); i.value = 'よまない'; document.getElementById('chatSend').click() })()")
    shown = a.wait_for("document.getElementById('chatLog').textContent.includes('よまない')", timeout=SHORT)
    time.sleep(1.0)
    off_quiet = shown and a.eval("window.__T.ttsTexts.length") == n2
    off_saved = a.eval("localStorage.getItem('pot-call-snd-tts2') === null")   # v0.14.50：保存しない
    vrow_gone = b.wait_for("!document.querySelector('#members .member.virtual')", timeout=SHORT)   # オフの案内で仮想行が消える
    chip_gone = b.wait_for("(() => { const c = document.querySelector('#lobbyList .room.here'); return !!c && !c.querySelector('img.rp-face') })()", timeout=SHORT)   # ロビーの印も消える（v0.15.0）
    show_tab(a, 'members'); show_tab(b, 'members')
    ok = (row_owner and row_listener and sample and saved and note and vrow_b and two and vrow_a and face and arrived and shape and lit
          and noname and noname2 and kana and listener_quiet and off_quiet and off_saved and vrow_gone
          and lobby_chip and lobby_chip_a and lobby_count and chip_gone)
    r.check('T55', '読み上げ v2：配信者だけに設定・オンで読む・2本目のトラックと仮想行・ロビーの印・URL省略・名前付き・聞き役は合成しない・オフで消える', ok, 'pass',
            '配信者に行=%s / 聞き役に行なし=%s / 見本=%s / 保存しない=%s / 注記=%s / 聞き役に仮想行=%s / 音声2本=%s / 配信者に仮想行=%s / 顔=%s / 届いた=%s / 文面=%r / 投稿者の行が光る=%s / 連続なら名前なし=%s / 名前を読まないスイッチ=%s / カタカナ辞書=%s(%r) / 聞き役は合成しない=%s / オフで読まない=%s / オフでも保存しない=%s / オフで仮想行が消える=%s / ロビーの印(聞き役)=%s / ロビーの印(配信者)=%s / 人数に数えない=%s / オフでロビーの印も消える=%s'
            % (row_owner, row_listener, sample, saved, note, vrow_b, two, vrow_a, face, arrived, text, lit, noname, noname2, kana, kana_text, listener_quiet, off_quiet, off_saved, vrow_gone, lobby_chip, lobby_chip_a, lobby_count, chip_gone))


def T55b(r, a, b):
    """読み上げ v2：**配信者がオンにした後から入った人**にも案内→{ok}→2本目のトラック→仮想行が届く（T55 の後・a は配信者）"""
    if not a or not b:
        return
    show_tab(a, 'settings')
    a.eval("document.getElementById('sndTts').click()")   # T55 の最後でオフにしてあるので、もう一度オン
    on = a.wait_for("document.getElementById('sndTts').checked", timeout=SHORT)
    c = r.open_tab(hash_=a.eval('location.hash'))
    c.eval("window.__potBcErase(true)")   # 聞き役にする（T27 と同じ理由）
    click_join(c)
    met = c.wait_for('%s >= 2' % MEMBERS, timeout=DISCOVER)
    vrow = c.wait_for("!!document.querySelector('#members .member.virtual')", timeout=DISCOVER)
    two = c.wait_for("%s >= 2" % AUDIOS, timeout=DISCOVER)
    # 配信者が「つなぎ直す」（ページ再読み込み）→ 読み上げのオンが引き継がれ、聞き役 c には新しい配信者から届き直す（v0.14.54）
    # ★テストの都合：c を聞き役にするため鍵置き場を空にしたので、a の再読み込みの前に a の鍵を書き戻す（T27b と同じ）。
    #   戻ったら再び空にする（後の T27b で b が再読み込みしたときにオーナーにならないように）
    a.eval("if (window.__a_bcast_key) window.__potBcRestore()")
    a.eval("document.getElementById('retry').click()", await_promise=False)
    time.sleep(3)
    back = a.wait_for('%s >= 2' % MEMBERS, timeout=DISCOVER)
    a.eval("window.__a_bcast_key = window.__potBcList().length ? '1' : null; window.__potBcErase(true)")
    kept = a.wait_for("document.getElementById('sndTts').checked && !document.getElementById('sndTtsRow').hidden", timeout=SHORT)
    c.eval("document.querySelectorAll('#members .member.virtual').length")   # 古い行は配信者が抜けた時点で消えている
    vrow2 = c.wait_for("!!document.querySelector('#members .member.virtual')", timeout=DISCOVER)
    # ★遅延読み込み（v0.15.2）：つなぎ直しの引き継ぎではエンジン（差し替え口）に触らない。
    #   触った回数は INSTRUMENT の getter が数えている（リロードで 0 に戻る）。仮想行や案内は
    #   トラックだけで成立するので、この時点で 0 のまま＝700MB を掴んでいない、が確かめられる。
    lazy0 = a.eval("__T.ttsGets") == 0
    # ★失敗の日本語化（v0.15.3）は**ここでしか**測れない：T55 の途中は ttsEngine が既に載っていて
    #   loadTts が素通りする。つなぎ直し直後（遅延読み込み後）はエンジンが空なので、差し替え口の
    #   getter を一時的に例外（Out of memory）にすれば失敗経路が通る。画面には日本語＋直し方が出て、
    #   oomN（pot-call-tts-oom）が記録されること。終わったら必ず戻す（壊れたまま残ると以降が連鎖で死ぬ）。
    err_ja = err_raw_hidden = oom_counted = False
    try:
        a.eval("(() => { window.__ttsBak = Object.getOwnPropertyDescriptor(window, '__potTts');"
               " Object.defineProperty(window, '__potTts', { configurable: true,"
               " get() { throw new Error('RangeError: Out of memory (テスト用)') } }) })()")
        a.eval("localStorage.removeItem('pot-call-tts-oom')")
        show_tab(c, 'chat')
        c.eval("(() => { const i = document.getElementById('chatText'); i.value = '読み込みを失敗させる'; document.getElementById('chatSend').click() })()")
        err_ja = a.wait_for("(() => { const t = document.getElementById('sndTtsProg').textContent;"
                            " return t.includes('メモリが足りませんでした') && t.includes('つなぎ直す') })()", timeout=SHORT + 10)
        err_raw_hidden = a.eval("!document.getElementById('sndTtsProg').textContent.includes('Out of memory')")   # 生英語は画面に出さない
        oom_counted = a.eval("localStorage.getItem('pot-call-tts-oom') === '1'")
    finally:
        a.eval("(() => { if (window.__ttsBak) Object.defineProperty(window, '__potTts', window.__ttsBak) })()")
    # getter は戻す（後始末）。**失敗後の自己修復（次のひとことで loadTts を再試行）は自動判定しない**：
    #   2 通目のひとことがリレー越しに a へ届くのを待つ形になり、配送タイミングで揺れるため。
    #   製品ロジックは健全（失敗後は ttsEngine も ttsLoading も null＝次の speakChat が必ず loadTts を再呼び出し）で、
    #   実機でも「失敗しても次のひとことで読める」を利用者が確認済み（HANDOFF）。ここでは B の 3 点だけ見る。
    a.eval("document.getElementById('sndTts').click()")   # 後のテストに持ち越さない
    show_tab(a, 'members')
    click_leave(c)
    ok = (on and met and vrow and two and back and kept and vrow2 and lazy0
          and err_ja and err_raw_hidden and oom_counted)
    r.check('T55b', '読み上げ v2：後から入った聞き役にも届く・配信者がつなぎ直しても読み上げは続く', ok, 'pass',
            'オン=%s / 合流=%s / 仮想行=%s / 音声2本=%s / つなぎ直し後に戻った=%s / チェック維持=%s / 聞き役に仮想行が戻る=%s / つなぎ直しでは読み込まない=%s / 失敗は日本語＋直し方=%s / 生英語を出さない=%s / oom記録=%s'
            % (on, met, vrow, two, back, kept, vrow2, lazy0, err_ja, err_raw_hidden, oom_counted))


def T57(r, a, b):
    """読み上げ v2：雑談部屋では設定行が出ない（配信部屋のオーナー限定）。★T49 のあと（A・B は雑談部屋で通話中）"""
    show_tab(a, 'settings'); show_tab(b, 'settings')
    hidden = a.eval("document.getElementById('sndTtsRow').hidden") and b.eval("document.getElementById('sndTtsRow').hidden")
    show_tab(a, 'members'); show_tab(b, 'members')
    r.check('T57', '読み上げ v2：雑談部屋では設定が出ない', hidden, 'pass', '両方とも非表示=%s' % hidden)


def T50(r, room):
    """退出は確認してから（v0.13.5・v0.14.2 でページ内の2択に）：「やめる」なら残り、「退出する」なら退出。文面に部屋名。
    ★confirm() を使わないこと（ダイアログでページの JS が止まり、閉じた瞬間に BGM が連なる＝2026-09-20 に再発）"""
    t = r.open_tab()
    r.join(t, room + 'BYE')
    assert t.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), '参加できない'
    t.eval("document.getElementById('leave').click()", await_promise=False)
    shown = t.eval("!!document.getElementById('leaveConfirm').offsetParent")
    text = t.eval("document.getElementById('leaveConfirmText').textContent")
    no_confirm = t.eval("__T.confirms.length") == 0
    t.eval("document.getElementById('leaveNo').click()", await_promise=False)
    time.sleep(0.5)
    stayed = not t.eval("document.getElementById('tabs').hidden") and t.eval("document.getElementById('leaveConfirm').hidden")
    t.eval("document.getElementById('leave').click()", await_promise=False)
    t.eval("document.getElementById('leaveYes').click()", await_promise=False)
    left = t.wait_for("document.getElementById('tabs').hidden", timeout=SHORT)
    gone = t.eval("document.getElementById('leaveConfirm').hidden")
    ok = shown and (room + 'BYE') in text and '退出' in text and no_confirm and stayed and left and gone
    r.check('T50', '退出は確認してから（ページ内の2択・confirm() を使わない）', ok, 'pass',
            '2択が出る=%s / 文面=%r / confirm()を呼んでいない=%s / やめるで残る=%s / 退出するで退出=%s / 退出後は畳まれる=%s'
            % (shown, text, no_confirm, stayed, left, gone))


def T51(r, room):
    """配信の終わり（v0.14.0）：配信者が切れたら 60 秒待ち、戻れば続き、戻らなければ「配信終了」→15 秒でロビーへ

    ①配信者がいる間はロビーに出る ②配信者が抜けると聞き役に「戻るのを待っています」、ロビーからは消える
    ③配信者が戻れば何事もなく続く ④二度目に抜けて 60 秒経つと「配信が終了しました」→さらに 15 秒で自動退出、
      状態に案内が出る。★実時間で 2 分ほどかかる
    """
    o = r.open_tab()
    o.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    o.eval("(() => { document.getElementById('createBox').open = true; const i = document.getElementById('room');"
           " i.value = %s; i.dispatchEvent(new Event('input')); document.getElementById('kindBc').click() })()" % js_str(room + 'END'))
    assert o.wait_for("document.getElementById('fbUrl').href.includes('~pk~')", timeout=SHORT), '配信部屋の鍵ができない'
    h = '#' + o.eval("document.getElementById('fbUrl').href").split('#', 1)[1]
    click_join(o)
    assert o.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), '配信者が参加できない'
    # ★v0.16.23：鍵は IndexedDB（pot-call-keys）に引っ越したので、退避は下の __potBcErase(true) が担う
    # （同じブラウザで「抜けたら戻る」を再現するため、いったん stash へ逃がして元に戻す）
    o.eval("window.__potBcErase(true)")   # 聞き役は鍵を持たない
    l = r.open_tab(hash_=h)
    click_join(l)
    assert l.wait_for("%s === 2" % MEMBERS, timeout=DISCOVER), '聞き役が合流できない'
    c = r.open_tab()
    listed = c.wait_for("%s.includes(%s)" % (LOBBY, js_str(room + 'END')), timeout=DISCOVER)
    # ② 配信者が抜ける
    click_leave(o)
    waiting = l.wait_for("%s.includes('戻るのを待っています')" % BC_NOTE, timeout=SHORT)
    gone = c.wait_for("!%s.includes(%s)" % (LOBBY, js_str(room + 'END')), timeout=DISCOVER)
    # ③ 戻る（同じ鍵で、同じリンクから開き直す＝実機で配信者が入り直すのと同じ）
    o.eval("window.__potBcRestore()")
    o.eval("location.hash = %s" % js_str(h[1:]), await_promise=False)
    o.call('Page.reload'); time.sleep(0.5); o.wait_for(BOOTED, timeout=SHORT)
    click_join(o)
    # ★v0.16.23：同じタブの同じ鍵で戻ったことを先に待つ（聞いている側の見立てを先に数えない）
    back = o.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    resumed = l.wait_for("!%s.includes('戻るのを待っています') && %s.includes('配信者の声')" % (BC_NOTE, BC_NOTE), timeout=DISCOVER)
    # ④ 二度目：戻らない → 60 秒で「終了」→ 15 秒で退出
    click_leave(o)
    l.wait_for("%s.includes('戻るのを待っています')" % BC_NOTE, timeout=SHORT)
    ended = l.wait_for("%s.includes('配信が終了しました')" % BC_NOTE, timeout=75)
    left = l.wait_for("document.getElementById('tabs').hidden", timeout=25)
    told = l.eval(STATE)
    ok = listed and waiting and gone and back and resumed and ended and left and '配信は終了' in told
    r.check('T51', '配信者が切れたら待ち、戻らなければ配信終了でロビーへ', ok, 'pass',
            'ロビーに出る=%s / 抜けたら待つ=%s / ロビーから消える=%s / 戻れば続く=%s / 60秒で終了=%s / 自動退出=%s / 案内=%r'
            % (listed, waiting, gone, back and resumed, ended, left, told[:30]))


def T52(r, room):
    """ロビーのタップの確認はページ内の2択（v0.14.3・confirm() を使わない）

    ①未参加のタップ → その部屋の直下に「参加しますか？」＋参加する／やめる。やめるで畳まれ、参加するで入る
    ②通話中に別の部屋をタップ → 「◯◯を退出して△△に移りますか？」＋移る／やめる。移るで部屋が変わる
    ③在室の描き直しで2択が消えない（状態から毎回描く）
    """
    x = room + 'X'
    a = r.open_tab(); r.join(a, x)
    assert a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), 'A が参加できない'
    c = r.open_tab()
    c.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    ROOM_BTN = "[...document.querySelectorAll('#lobbyList .room')].find(b => b.textContent.includes(%s))" % js_str(x)
    assert c.wait_for("!!" + ROOM_BTN, timeout=DISCOVER), 'ロビーに出ない'
    c.eval(ROOM_BTN + ".click()", await_promise=False)
    shown = c.wait_for("!!document.getElementById('roomAsk') && !!document.getElementById('roomAsk').offsetParent", timeout=SHORT)
    text = c.eval("(document.getElementById('roomAskText') || {}).textContent || ''")
    under = c.eval("(() => { const a = document.getElementById('roomAsk'); return !!a && a.previousElementSibling === " + ROOM_BTN + " })()")
    time.sleep(6)   # 在室の描き直し（5秒ごと）をまたいでも消えない
    persists = c.eval("!!document.getElementById('roomAsk')")
    c.eval("document.getElementById('roomAskNo').click()", await_promise=False)
    time.sleep(0.5)
    dismissed = not c.eval("!!document.getElementById('roomAsk')")
    no_confirm = c.eval("__T.confirms.length") == 0
    c.eval(ROOM_BTN + ".click()", await_promise=False)
    c.wait_for("!!document.getElementById('roomAskYes')", timeout=SHORT)
    c.eval("document.getElementById('roomAskYes').click()", await_promise=False)
    joined = c.wait_for("document.getElementById('state').textContent.includes('通話中')", timeout=DISCOVER)
    # ② 通話中に別の部屋へ
    y = room + 'Y'
    b = r.open_tab(); r.join(b, y)
    assert b.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), 'B が参加できない'
    assert b.wait_for("!!" + ROOM_BTN, timeout=DISCOVER), 'B のロビーに X が出ない'
    b.eval(ROOM_BTN + ".click()", await_promise=False)
    b.wait_for("!!document.getElementById('roomAskYes')", timeout=SHORT)
    text2 = b.eval("(document.getElementById('roomAskText') || {}).textContent || ''")
    yes2 = b.eval("(document.getElementById('roomAskYes') || {}).textContent || ''")
    b.eval("document.getElementById('roomAskYes').click()", await_promise=False)
    moved = a.wait_for('%s === 3' % MEMBERS, timeout=DISCOVER)
    ok = (shown and '参加しますか' in text and under and persists and dismissed and no_confirm and joined
          and '退出して' in text2 and y in text2 and yes2 == '移る' and moved)
    r.check('T52', 'ロビーのタップの確認はページ内の2択', ok, 'pass',
            '2択が出る=%s(%r) / 部屋の直下=%s / 描き直しで消えない=%s / やめるで畳む=%s / confirm()を呼んでいない=%s / 参加するで入る=%s / 通話中の文言=%s(%r・%r) / 移るで移動=%s'
            % (shown, text[:30], under, persists, dismissed, no_confirm, joined, '退出して' in text2, text2[:40], yes2, moved))
    for t in (a, b, c):
        click_leave(t)


def T53(r, room):
    """無視の初回説明は <dialog>（v0.14.6・confirm() を使わない）：やめるで無視されず、無視するで無視され、2回目は出ない"""
    x = room + 'IGN'
    a = r.open_tab(); r.join(a, x)
    assert a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), 'A が参加できない'
    b = r.open_tab(); r.join(b, x)
    assert a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER), 'B が合流できない'
    a.eval("localStorage.removeItem('pot-call-ignore-seen')")
    BTN = "document.querySelector('#members .m-ig:not([disabled]):not(.off)')"
    assert a.wait_for("!!" + BTN, timeout=SHORT), '無視ボタンが出ない（相手のプロフィール待ち）'
    a.eval(BTN + ".click()", await_promise=False)
    opened = a.wait_for("document.getElementById('ignoreDlg').open", timeout=SHORT)
    title = a.eval("document.getElementById('ignoreTitle').textContent")
    no_confirm = a.eval("__T.confirms.length") == 0
    a.eval("document.getElementById('ignoreNo').click()", await_promise=False)
    time.sleep(0.4)
    kept = not a.eval("document.getElementById('ignoreDlg').open") and a.eval("document.querySelectorAll('#members .member.ignored').length") == 0
    a.eval(BTN + ".click()", await_promise=False)
    a.wait_for("document.getElementById('ignoreDlg').open", timeout=SHORT)
    a.eval("document.getElementById('ignoreYes').click()", await_promise=False)
    ignored = a.wait_for("document.querySelectorAll('#members .member.ignored').length === 1", timeout=SHORT)
    seen = a.eval("localStorage.getItem('pot-call-ignore-seen')") == '1'
    # 戻して、もう一度 → 2回目はダイアログを出さずにすぐ無視
    a.eval("(() => { const b = document.querySelector('#members .m-ig.off'); if (b) b.click() })()", await_promise=False)   # 自分の行の（見えない）ボタンではなく「戻す」を押す
    a.wait_for("document.querySelectorAll('#members .member.ignored').length === 0", timeout=SHORT)
    a.wait_for("!!" + BTN, timeout=SHORT)
    a.eval(BTN + ".click()", await_promise=False)
    time.sleep(0.4)
    direct = a.eval("document.querySelectorAll('#members .member.ignored').length") == 1 and not a.eval("document.getElementById('ignoreDlg').open")
    ok = opened and '受け取らない' in title and no_confirm and kept and ignored and seen and direct
    r.check('T53', '無視の初回説明は <dialog>（confirm() を使わない）', ok, 'pass',
            '出た=%s(%r) / confirm()を呼んでいない=%s / やめるで無視されない=%s / 無視するで無視=%s / 覚えた=%s / 2回目は出ない=%s'
            % (opened, title[:24], no_confirm, kept, ignored, seen, direct))
    for t in (a, b):
        click_leave(t)


def T54(r, room):
    """招待リンクのコピーは「部屋名 - 名前」＋改行＋URL（v0.14.7）。バナーのコピーは URL だけ。コピーのボタンはカードの裏（2026-11-09）"""
    x = room + 'CP'
    t = r.open_tab()
    t.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(t, x)
    assert t.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), '参加できない'
    who = t.eval("document.getElementById('name').value") or 'ゲスト'
    url = t.eval("document.getElementById('fbUrl').href")
    # 招待のコピーはカードの裏（見出しの「シェア」→「招待リンクをコピー」・2026-11-09）
    assert flip_back(t), '裏返らない'
    t.eval("document.getElementById('fbCopy').click()", await_promise=False)
    got = t.wait_for("__T.clip.length >= 1", timeout=SHORT)
    txt = t.eval("__T.clip[0]") or ''
    lines = txt.split('\n')
    fmt = got and len(lines) == 2 and lines[0] == '🫖 ' + x + ' - ' + who and lines[1] == url
    labeled = t.eval("document.getElementById('fbCopy').textContent").startswith('コピーしました')
    t.eval("document.getElementById('copyTop').click()", await_promise=False)
    t.wait_for("__T.clip.length >= 2", timeout=SHORT)
    top = t.eval("__T.clip[1]") == url
    # ★ロビーの部屋カードの共有ボタンも同じ形（v0.15.29・利用者の要望）：別タブ c のロビーに t の部屋が出たら押す。
    #   名前はコピーする本人（c）の名前。URL は t の共有リンクと同じ
    c = r.open_tab()
    c.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    CARD = "[...document.querySelectorAll('#lobbyList .room')].find(b => b.textContent.includes(%s))" % js_str(x)
    card = c.wait_for("!!" + CARD, timeout=DISCOVER)
    card_txt = ''
    if card:
        c.eval(CARD + ".querySelector('.room-share').click()", await_promise=False)
        c.wait_for("__T.clip.length >= 1", timeout=SHORT)
        card_txt = c.eval("__T.clip[0]") or ''
    # 名前の欄は在室者（v0.15.30）：いま部屋にいるのは t だけなので t の名前（コピーした c の名前ではない）
    cl = card_txt.split('\n')
    card_fmt = bool(card) and len(cl) == 2 and cl[0] == '🫖 ' + x + ' - ' + who and cl[1] == url
    # ★雑談部屋は在室者の羅列（v0.15.30）：c も入ると t の招待リンクは「t、c」
    # ★部屋名ではなく t のリンクで入る（部屋の実体はハッシュ。同名でも別の部屋になる）。同じタブのハッシュ変更では
    #   読み込み直しにならないので、新しいタブで開く（T47 と同じやり方）
    c2 = r.open_tab(hash_=t.eval('location.hash'))
    # 同じ Chrome プロファイル＝同じ localStorage の名前になるので、別の名前にしてから入る（羅列は重複を畳む）
    who_c = who + '2'
    c2.eval("(() => { const n = document.getElementById('name'); n.value = %s; n.dispatchEvent(new Event('input')) })()" % js_str(who_c))
    click_join(c2)
    # ★「つながり中」の仮の行（在室情報の名前）でも members に名前が出るので、接続が済んで仮の行から外れるまで待つ
    seen = t.wait_for("document.getElementById('members').textContent.includes(%s) && !((window.__potPending && window.__potPending()) || []).includes(%s)"
                      % (js_str(who_c), js_str(who_c)), timeout=DISCOVER)
    t.eval("document.getElementById('fbCopy').click()", await_promise=False)   # 裏は出したまま
    t.wait_for("__T.clip.length >= 3", timeout=SHORT)
    two = (t.eval("__T.clip[2]") or '').split('\n')[0]
    names = seen and two == '🫖 ' + x + ' - ' + who + '、' + who_c
    # ★配信部屋は配信者の名前（v0.15.30）：配信者 o の招待リンクも、別タブ d のロビーのカードも「📣 部屋名 - o の名前」
    xb = x + 'BC'
    o = r.open_tab()
    bc_ok = make_bcast(r, o, xb) and o.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER)
    bc_inv = bc_card = False
    if bc_ok:
        who_o = o.eval("document.getElementById('name').value") or 'ゲスト'
        url_o = o.eval("document.getElementById('fbUrl').href")
        flip_back(o); o.eval("document.getElementById('fbCopy').click()", await_promise=False)
        o.wait_for("__T.clip.length >= 1", timeout=SHORT)
        bc_inv = (o.eval("__T.clip[0]") or '') == '📣 ' + xb + ' - ' + who_o + '\n' + url_o
        d = r.open_tab()
        d.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(dd => dd.close())")
        CARD_B = "[...document.querySelectorAll('#lobbyList .room')].find(b => b.textContent.includes(%s))" % js_str(xb)
        if d.wait_for("!!" + CARD_B, timeout=DISCOVER):
            d.eval(CARD_B + ".querySelector('.room-share').click()", await_promise=False)
            d.wait_for("__T.clip.length >= 1", timeout=SHORT)
            bc_card = (d.eval("__T.clip[0]") or '') == '📣 ' + xb + ' - ' + who_o + '\n' + url_o
        click_leave(o)
    ok = fmt and labeled and top and card_fmt and names and bc_inv and bc_card
    r.check('T54', '招待リンクのコピーは「部屋名 - 名前」＋URL（バナーは URL だけ・ロビーのカードも同じ形・雑談は在室者の羅列・配信は配信者）', ok, 'pass',
            '形=%s(%r) / ボタンの文言=%s / バナーは URL だけ=%s / ロビーのカード=%s(%r) / 在室者の羅列=%s(%r) / 配信：招待=%s・カード=%s'
            % (fmt, txt[:60], labeled, top, card_fmt, card_txt[:60], names, two[:60], bc_inv, bc_card))
    click_leave(c2); click_leave(t)


def T41(r, room):
    """退出したら URL から部屋を落とす（**次に開いたとき最初の画面が開いてしまう**のを防ぐ）

    2026-09-12：参加時に焼き付けた hash を退出後も残していたため、次に開くと
    「共有リンクから来た人」と区別がつかず、**「部屋を作る」が開いた状態で始まっていた**。
    URL としても、居ない部屋を指したままなのは嘘。
    ★ただし**入力欄の部屋名は消さない**（退出直後にそのまま入り直せる・招待リンクも作れる）。
    """
    t = r.open_tab()
    t.eval("document.querySelector('#createBox > summary').click()")
    t.eval("(() => { const r = document.getElementById('room'); r.value = %s;"
           " r.dispatchEvent(new Event('input')) })()" % js_str(room + 'OUT'))
    click_join(t, gesture=True)
    joined = t.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    in_hash = t.eval("location.hash") != ''
    t.call('Runtime.evaluate', expression=LEAVE_CLICK, userGesture=True)
    t.wait_for("document.getElementById('tabs').hidden", timeout=SHORT)
    time.sleep(0.5)
    cleared = t.eval("location.hash") == ''
    kept = t.eval("document.getElementById('room').value") != ''    # 部屋名は残す
    # ★畳んだら状態カードごと消えること。**「退出しました／0人」だけ残る**のを防ぐ
    #   （2026-09-12・利用者の指摘）。状態カードと招待リンクは「部屋を作る」と一蓮托生。
    t.eval("document.querySelector('#createBox > summary').click()")
    folded = t.wait_for("document.getElementById('statusCard').hidden"
                        " && document.getElementById('shareFlip').hidden", timeout=SHORT)
    ok = joined and in_hash and cleared and kept and folded
    r.check('T41', '退出で URL から部屋が落ちる（部屋名は残る）', ok, 'pass',
            '参加=%s / 参加中に hash がある=%s / 退出で消える=%s / 部屋名は残る=%s / 畳めば状態カードも消える=%s'
            % (joined, in_hash, cleared, kept, folded))


def T42(r, room):
    """このアプリの音は**スピーカーへ直接ではなく `<audio>` から出す**

    2026-09-12・iPhone 実機で分かったこと：`ctx.destination` から鳴らすと **iPhone では
    ほとんど聞こえない**（耳を近づけて「チリチリ」程度）。**メディア要素に載せると普通に鳴り**、
    さらに**他アプリへ切り替えても AudioContext が中断されない**（背面でも鳴り続ける）。
    小窓（PiP）でも同じ効果が出ていたが、**効いていたのは小窓ではなくメディア要素**だった。

    ここが外れると、**画面では何も起きていないのに iPhone だけ音が消える**（誰も気づけない）。
    """
    t = r.open_tab()
    t.eval("document.getElementById('room').value = %s" % js_str(room + 'OUT2'))
    click_join(t, gesture=True)
    joined = t.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    # 出口の <audio> が、ストリームを繋いだ状態で鳴っていること
    playing = t.wait_for("(() => { const a = document.getElementById('appAudio');"
                         " return !!a && !!a.srcObject && !a.paused && !a.muted })()", timeout=SHORT)
    tracks = t.eval("(() => { const a = document.getElementById('appAudio');"
                    " return a && a.srcObject ? a.srcObject.getAudioTracks().length : 0 })()")
    # ★スピーカーへ直接繋いでいないこと（繋ぐと二重に鳴る／iPhone で音が細る）
    direct = t.eval('__T.edges.some(([x, y]) => y instanceof AudioDestinationNode)')
    # ★中断からの復帰（v0.15.1）：iOS は割り込みで **ctx だけでなく <audio> 要素も pause する**ので、
    #   復帰時に要素を play し直す（resume だけでは戻らない＝2026-09-29 の実測・internal/lab/tools/audio_wake_test.html）。
    #   ヘッドレスでは中断そのものを再現できないので、「要素を止めてから復帰イベントを起こすと鳴り直る」を固定する。
    t.eval("(() => { const a = document.getElementById('appAudio'); a.pause();"
           " document.dispatchEvent(new Event('visibilitychange')) })()")
    watching = t.wait_for("!document.getElementById('appAudio').paused", timeout=SHORT)
    errs = t.eval('__T.errors')
    ok = joined and playing and tracks == 1 and not direct and watching and not errs
    r.check('T42', '音は <audio> から出す（スピーカー直結にしない）', ok, 'pass',
            '参加=%s / 出口が鳴っている=%s / 音声トラック=%s / スピーカー直結=%s / 止めても復帰で鳴り直る=%s / エラー=%s'
            % (joined, playing, tracks, direct, watching, errs or 'なし'))


def T25(r, a, b):
    """ひとこと：相手に届き、**受け取った文字はそのまま描かない**

    他人が決めた中身が自分の画面に出る最初の経路。守るべきは3つ:
      ・相手に届いて、その人のアバターから吹き出しが出る
      ・**制御文字や双方向制御は落とされる**（capText を通っている）
      ・長さで切られる（60文字）

    ★入力欄は「ひとこと」タブの中にあるので、**人と同じようにタブを切り替えてから**打つ。
    """
    show_tab(a, 'chat')
    a.eval("(() => { const t = document.getElementById('chatText');"
           " t.value = 'こんにちは'; document.getElementById('chatSend').click() })()")
    mine = a.wait_for("document.querySelectorAll('.chat-bubble').length >= 1", timeout=10)
    theirs = b.wait_for("document.querySelectorAll('.chat-bubble').length >= 1", timeout=20)
    shown = b.eval("(document.querySelector('.chat-bubble')||{}).textContent")
    # 中身は textContent で入っているか（要素が生えていない＝生のHTMLとして解釈されていない）
    safe = b.eval("!document.querySelector('.chat-bubble *')")
    # ★吹き出しは**その人の行の中**にあること。body 直下に座標計算で置くと、あとで
    # レイアウトが動いたときに行から離れた場所に取り残される（2026-08-15 に踏んだ）。
    inrow = b.eval("!!document.querySelector('#members .member > .chat-bubble')")
    # iOS は 16px 未満の入力欄にフォーカスすると勝手に拡大する。実機でしか症状が
    # 出ないので、ここで指定だけ守る（2026-08-15 に踏んだ）。
    fs = a.eval("parseFloat(getComputedStyle(document.getElementById('chatText')).fontSize)")
    ok = mine and theirs and shown == 'こんにちは' and safe and inrow and fs >= 16
    r.check('T25', 'ひとことが相手の吹き出しに出る', ok, 'pass',
            '自分=%s 相手=%s 出た文字=%r 中に要素が生えていない=%s 行の中にある=%s 入力欄=%spx'
            % (mine, theirs, shown, safe, inrow, fs))


def T25b(r, a, b):
    """ひとこと：制御文字・双方向制御が落ち、長さで切られる（capText を通っている）"""
    b.eval("document.querySelectorAll('.chat-bubble').forEach(e => e.remove())")
    time.sleep(1.4)   # 送信側の下限（1200ms）を越えてから
    # U+202E（右横書き上書き）と改行を混ぜ、60文字を超える長さで送る
    # エスケープの二重解釈（Python→JS）を避けるため、危ない文字は JS 側で
    # 文字コードから作る。ソースに生の制御文字を置かない意味もある。
    a.eval("(() => { const t = document.getElementById('chatText');"
           " t.value = 'あ' + String.fromCharCode(0x202E) + 'い'"
           "   + String.fromCharCode(10) + 'う' + 'ん'.repeat(80);"
           " document.getElementById('chatSend').click() })()")
    got = b.wait_for("document.querySelectorAll('.chat-bubble').length >= 1", timeout=20)
    shown = b.eval("(document.querySelector('.chat-bubble')||{}).textContent") or ''
    clean = '\u202e' not in shown and '\n' not in shown
    capped = len(shown) <= 60
    ok = got and clean and capped
    r.check('T25b', 'ひとことの危ない文字が落ちて長さで切られる', ok, 'pass',
            '双方向制御と改行が無い=%s / 60文字以内=%s（実際%s文字）' % (clean, capped, len(shown)))


def make_bcast(r, tab, room):
    """配信部屋を作って参加する（📣 を選ぶ → 鍵ができるのを待つ → 参加）

    鍵づくり（crypto.subtle.generateKey）は非同期なので、**共有リンクに指紋が乗るまで待つ**。
    待たずに参加すると普通の部屋に入ってしまい、症状が「なぜか相手に会えない」になる。
    """
    tab.eval("(() => { const r = document.getElementById('room');"
             " r.value = %s; r.dispatchEvent(new Event('input'));"
             " const c = document.getElementById('kindBc');"
             " c.checked = true; c.dispatchEvent(new Event('change')) })()" % js_str(room))
    got = tab.wait_for("document.getElementById('fbUrl').href.includes('~pk~')", timeout=SHORT)
    click_join(tab)
    return got


# 配信部屋まわりで繰り返し見る値
SPEAK_SHOWN = "getComputedStyle(document.getElementById('speak')).display !== 'none'"
MUTE_SHOWN = "getComputedStyle(document.getElementById('mute')).display !== 'none'"
# 配信部屋の説明は v0.16.29 から知らせのカード（key 'bc'）。閉じて ⏱️ に入っていても文は読める
BC_NOTE = "((window.__potToasts().find(t => t.key === 'bc') || {}).text || '')"
LIVE_TRACKS = "__T.streams.flatMap(s => s.getTracks()).filter(t => t.readyState === 'live').length"


def T26(r, room):
    """配信部屋：リンクに指紋が乗り、部屋名の欄には名前だけが残る

    部屋IDは `名前~pk~指紋`。**入力欄に指紋を出さない**のが要点で、出すと文字数カウンタの
    意味が壊れるうえ、うっかり消されて「同じ名前の別の部屋」に入ってしまう。
    """
    t = r.open_tab()
    name = room + 'BC'
    made = make_bcast(r, t, name)
    url = t.eval("document.getElementById('fbUrl').href")
    kept = t.eval("document.getElementById('room').value")
    joined = t.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER)
    note = t.eval(BC_NOTE)
    # 指紋は base64url 22文字ちょうど（SHA-256 の先頭16バイト）
    # v0.13.0：リンクは `#room=ハッシュ~pk~指紋&name=名前`。指紋は room= の末尾（& の前）
    room_id = url.split('#room=')[1].split('&')[0] if '#room=' in url else ''
    fp = room_id.split('~pk~')[-1] if '~pk~' in room_id else ''
    ok = made and joined and kept == name and len(fp) == 22 and 'あなたの配信' in note
    r.check('T26', '配信部屋のリンクに指紋が乗る（部屋名の欄は名前だけ）', ok, 'pass',
            'リンク=%r / 入力欄=%r / 指紋=%s文字 / 説明=%r' % (url[-40:], kept, len(fp), note[:24]))
    click_leave(t)
    return t


def T27(r, room):
    """配信部屋：リスナーはマイクを取らず、配信者の声だけが流れる

    このアプリで**他人の音を鳴らすかどうかを受信側が決める**最初の場所。守るべきは3つ:
      ・聞き役は getUserMedia を**一度も呼ばない**（許可ダイアログが出ない＝入口が軽い）
      ・配信者の声は聞き役に届く
      ・**聞き役の音は誰にも流れない**（そもそも送っていない）
    """
    name = room + 'HAI'
    a = r.open_tab()
    make_bcast(r, a, name)
    if not a.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER):
        r.check('T27', '配信部屋：聞き役はマイクを取らない', False, 'pass', '配信部屋を作れなかった')
        return None, None
    b = r.open_tab(hash_=a.eval('location.hash'))
    # ★テストの都合：**localStorage は同じブラウザの全タブで共有される**ので、そのままだと
    #   2枚目のタブも配信者の鍵を見つけて「自分もオーナー」になってしまう（実機では別端末なので
    #   起きない）。聞き役を作るために鍵置き場だけ空にする。A は参加時に鍵を読み込み済みなので
    #   影響を受けない（A がつなぎ直すテストの前には A の鍵を書き戻す）。
    a.eval("window.__a_bcast_key = window.__potBcList().length ? '1' : null")
    b.eval("window.__potBcErase(true)")
    click_join(b)
    # ★③（v0.15.10）：配信者 a は普通の聞き役を見ない＝自分だけ（1）。聞き役 b は配信者が見える＝2。
    met = a.wait_for('%s === 1' % MEMBERS, timeout=DISCOVER) and b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    heard = b.wait_for('%s === 1' % AUDIOS, timeout=DISCOVER)
    time.sleep(3)   # 逆向き（聞き役→配信者）に音が来ないことを見るため、少し置いてから
    gum = b.eval('__T.gum')
    quiet = a.eval(AUDIOS) == 0
    ui = (not b.eval(SPEAK_SHOWN)) and (not b.eval(MUTE_SHOWN))   # 許可されるまでどちらも出ない
    # 配信者の行に 📣。**枠は全行にあり、中身の有無で見る**（2026-08-18 に左へ移して枠を常設した）
    mark = a.eval("[...document.querySelectorAll('#members .m-bc')].some(e => e.textContent === '📣')")
    # ロビー：**参加していない第三のタブ**から見て 📣 付きで出ること。ここは指紋の扱いの
    # 見張りでもある（受信側で部屋IDを丸ごと30文字に切ると指紋が欠け、タップしても誰にも会えない）
    c = r.open_tab()
    in_lobby = c.wait_for('%s.includes(%s)' % (LOBBY, js_str('📣 ' + name)), timeout=DISCOVER)
    # ★配信部屋の聞き役はロビーに身元を出さない（v0.15.0）。**人数には入るがチップは出ない**。
    #   ここは「表示していない」ではなく「送っていない」ことの見張り：名前を送っていれば誰でも読めてしまう。
    #   a と b は同じブラウザなので profile（名前）が共有される＝名前では区別できない。**チップの本数**で見る。
    card = ("[...document.querySelectorAll('#lobbyList .room')]"
            ".find(e => e.textContent.includes(%s))" % js_str('📣 ' + name))
    # ★順番が大事：**先に「2人」を待つ**。チップ1本は「オーナーぶんしか届いていない」段階でも真になるので、
    #   先にチップを見ると聞き役の在室が届く前に通ってしまう（最初こう書いて取り違えた）。
    anon_count = c.wait_for("(() => { const e = %s;"
                            " return !!e && /^2人/.test(e.querySelector('.room-count').textContent) })()" % card,
                            timeout=DISCOVER)
    anon_chip = anon_count and c.eval(
        "(() => { const e = %s; return !!e && e.querySelectorAll('.rp').length === 1 })()" % card)
    # 配信部屋は**タブが4つ**＝いちばん混む。狭い画面でも1行に収まること
    narrow = []
    for w in (390, 360, 320):
        a.call('Emulation.setDeviceMetricsOverride', width=w, height=900, deviceScaleFactor=2, mobile=True)
        time.sleep(0.3)
        narrow.append(a.eval(TAB_FITS))
    a.call('Emulation.clearDeviceMetricsOverride')
    ok = (met and heard and gum == 0 and quiet and ui and mark and in_lobby
          and anon_chip and anon_count and all(narrow))
    r.check('T27', '配信部屋：聞き役はマイクを取らず配信者の声だけが流れる', ok, 'pass',
            '2人=%s / 聞き役に音=%s / 聞き役のgetUserMedia=%s回 / 配信者側に音は無い=%s / ボタン非表示=%s / 📣=%s / ロビーに📣付きで出る=%s / 聞き役はロビーに出ない=%s / 人数には入る=%s / 狭い画面でタブが1行=%s'
            % (met, heard, gum, quiet, ui, mark, in_lobby, anon_chip, anon_count, narrow))
    return a, b


def T27b(r, a, b):
    """配信部屋：登壇（🎙 で許可 → 押して初めてマイク → 取り消しでマイクも止まる）

    ★許可の判定は**署名した名簿**で決まる。ボタンの有無ではない（改造クライアントが
    自分でボタンを出しても、他人のブラウザは名簿に無い音を鳴らさない）。この一点は
    受信処理がモジュールスコープにあるため自動テストで守れていないので、
    index.html の acceptRoster を触るときは必ず目で確認すること。
    """
    if not a or not b:
        return
    # ★③（v0.15.10）：配信者は普通の聞き役を見なくなったので、**許可には聞き役の「通話希望」が要る**。
    #   配信者が募集オン → b が通話希望 → a の一覧に b が 🙋 で現れる → その行の 🎙 で許可（従来どおり）。
    #   これは T55b の残り c の巻き添え（2026-09-29）も自然に消す：a に出るのは希望者だけ＝b しかいない。
    show_tab(a, 'settings')
    a.wait_for("!document.getElementById('reqOpenRow').hidden", timeout=DISCOVER)
    a.eval("(() => { const c = document.getElementById('reqOpen'); if (!c.checked) c.click() })()")
    raise_shown = b.wait_for("!document.getElementById('raise').hidden", timeout=DISCOVER)   # 募集が届いてボタンが出る
    b.eval("document.getElementById('raise').click()")
    show_tab(a, 'members')
    settled = (a.wait_for("[...document.querySelectorAll('#members .m-bc')].some(e => e.textContent === '🙋')", timeout=DISCOVER)
               and a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER))   # self + 希望者 b（③で普通の聞き役は出ない）
    granted = a.eval("(() => { const g = document.querySelector('#members .member:not(:first-child) .m-mic');"
                     " if (g) g.click(); return !!g })()")
    shown = b.wait_for(SPEAK_SHOWN, timeout=SHORT)
    b.eval("document.getElementById('speak').click()", await_promise=False)
    spoke = b.wait_for('__T.gum === 1', timeout=SHORT) and a.wait_for('%s === 1' % AUDIOS, timeout=DISCOVER)
    mute_now = b.eval(MUTE_SHOWN) and not b.eval(SPEAK_SHOWN)   # マイクを持ったらミュートに入れ替わる
    ok = settled and granted and shown and spoke and mute_now
    r.check('T27b', '登壇：許可されると話せる（押すまでマイクは入らない）', ok, 'pass',
            '2人に落ち着いた=%s / 許可した=%s / ボタンが出た=%s / 声が届いた=%s / ミュートに入れ替わった=%s'
            % (settled, granted, shown, spoke, mute_now))

    # 登壇権限の引き継ぎ（v0.14.26）：登壇中に「つなぎ直す」でリロードしても、登壇権限が自動復元される
    # リロード前のトークン確認
    pre_tok = b.eval("!!sessionStorage.getItem('pot-call-grant:' + location.hash.slice(1).split('&')[0].replace('#room=','').replace('room=',''))")
    b.eval("document.getElementById('retry').click()")
    # リロード完了と自動再参加を待つ
    rejoined = b.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    # 復元によりマイクを使うボタン（SPEAK_SHOWN）が自動で表示されること
    resumed_speak = b.wait_for(SPEAK_SHOWN, timeout=DISCOVER)
    # マイクを使うをクリックして再度声が届くこと
    b.eval("document.getElementById('speak').click()", await_promise=False)
    spoke_again = a.wait_for('%s === 1' % AUDIOS, timeout=DISCOVER)
    ok_resume = pre_tok and rejoined and resumed_speak and spoke_again
    r.check('T27d', '登壇引き継ぎ：つなぎ直しても登壇権限が自動復元される', ok_resume, 'pass',
            '事前トークンあり=%s / 再参加した=%s / マイクを使うが出た=%s / 再登壇で声が届いた=%s'
            % (pre_tok, rejoined, resumed_speak, spoke_again))

    # 配信者がつなぎ直した場合（v0.14.26）：リスナー b のマイク権限が維持され、配信者 a が復帰後も声が届く
    a.eval("if (window.__a_bcast_key) window.__potBcRestore()")
    a.eval("document.getElementById('retry').click()")
    # 配信者のリロード完了と自動再参加を待つ
    a_rejoined = a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    # リスナー b のマイクは止まらず、ミュートボタン（MUTE_SHOWN）が出たままであること
    b_mic_kept = b.eval(MUTE_SHOWN) and not b.eval(SPEAK_SHOWN)
    # 配信者 a にリスナー b の声が再び届くこと
    spoke_to_rejoined_a = a.wait_for('%s === 1' % AUDIOS, timeout=DISCOVER)
    ok_owner_resume = a_rejoined and b_mic_kept and spoke_to_rejoined_a
    r.check('T27e', '配信者のつなぎ直し：リスナーのマイク権限が維持され通話が続く', ok_owner_resume, 'pass',
            '配信者が再参加した=%s / リスナーのマイク維持=%s / 復帰した配信者に声が届いた=%s'
            % (a_rejoined, b_mic_kept, spoke_to_rejoined_a))

    # 登壇耐性強化（v0.14.27）：リスナーが再接続（retry）した後に、配信者もつなぎ直した場合でも登壇権限が復元される
    # リスナー b が「つなぎ直す」でリロード（同一ブラウザなので b のリロード直前に a の鍵を退避して消す）
    a_key = a.eval("window.__potBcList().length ? '1' : null")
    b.eval("window.__potBcErase(true)")
    b.eval("document.getElementById('retry').click()")
    b_joined = b.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    # その直後に配信者 a もつなぎ直す（配信者の peerId が変わり ownerChanged が発生）
    if a_key:
        a.eval("window.__potBcRestore()")
    a.eval("document.getElementById('retry').click()")
    a_rejoined_again = a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    # 新しい配信者に対してもリスナー b がトークンを提示し、登壇権限が維持されること
    b_kept_with_new_owner = b.wait_for(SPEAK_SHOWN, timeout=DISCOVER)
    b.eval("document.getElementById('speak').click()", await_promise=False)
    b_spoke_again = a.wait_for('%s === 1' % AUDIOS, timeout=DISCOVER)
    ok_robust = b_joined and a_rejoined_again and b_kept_with_new_owner and b_spoke_again
    r.check('T27f', '登壇耐性：リスナー再接続後に配信者が再接続しても登壇権限が確実に引き継がれる', ok_robust, 'pass',
            'リスナー再参加=%s / 配信者再参加=%s / 新配信者への登壇維持=%s / 声が届いた=%s'
            % (b_joined, a_rejoined_again, b_kept_with_new_owner, b_spoke_again))

    # 取り消し：配信者側で音が止まり、**登壇していた人のマイクも実際に止まる**
    btn_ready = a.wait_for("!!document.querySelector('#members .member:not(:first-child) .m-mic.on')", timeout=SHORT)
    revoked = a.eval("(() => { const g = document.querySelector('#members .member:not(:first-child) .m-mic.on');"
                     " if (g) { g.click(); return true; } return false; })()")
    silent = a.wait_for('%s === 0' % AUDIOS, timeout=SHORT)
    released = b.wait_for('%s === 0' % LIVE_TRACKS, timeout=SHORT)
    # 取り消された人は**聞き役に戻る**ので、「マイクを使う」も「ミュート」もどちらも出ない
    # （出したままだと、押しても誰にも届かないボタンになる）
    back = (not b.eval(SPEAK_SHOWN)) and (not b.eval(MUTE_SHOWN))
    ok2 = btn_ready and revoked and silent and released and back
    r.check('T27c', '登壇の取り消しで音が止まりマイクも返る', ok2, 'pass',
            'ボタンあり=%s / クリックした=%s / 音が止まった=%s / マイクを返した=%s / 聞き役に戻った=%s'
            % (btn_ready, revoked, silent, released, back))
    click_leave(a)
    click_leave(b)


def T31(r, room):
    """おたより：枠主が募集 → リスナーが投稿（枠主にだけ届く）→ 枠主が選んで公開

    ラジオのハガキ。**安全性は文面の検査ではなく経路の形**で担保している:
      ・投稿は**枠主にしか届かない**（他のリスナーの画面には出ない）
      ・部屋に出せるのは**枠主だけ**（受け手は署名済み名簿の配信者と peerId が一致するかを見る）
    ここでは「往復が成立すること」と「**投稿が第三者に漏れないこと**」を見る。
    ★なりすまし（枠主でない人が公開を送る）は受信処理が外から叩けないので**人が読んで担保**。
      index.html の mailAction.onMessage にある `peerId !== call.ownerPeer` の1行を必ず目で確認すること。
    """
    name = room + 'MAIL'
    a = r.open_tab()
    make_bcast(r, a, name)
    if not a.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER):
        r.check('T31', 'おたよりが枠主に届き、選ばれると部屋に出る', False, 'pass', '配信部屋を作れなかった')
        return
    h = a.eval('location.hash')
    b, c = r.open_tab(hash_=h), r.open_tab(hash_=h)     # 聞き役2人（片方は「漏れていないか」の見張り）
    for t in (b, c):
        t.eval("window.__potBcErase(true)")   # ★同じブラウザなので鍵を消して聞き役にする
        click_join(t)
    # ★③（v0.15.10）：配信者 a は普通の聞き役を見ない（自分だけ＝1）。3 人そろった合図は聞き役側（自分＋配信者＝2）と、
    #   配信者側の「🙂 ほか 2 人が聞いています」で見る。
    three = (b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and c.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
             and a.wait_for("(document.querySelector('#members .m-more') || {}).textContent === '🙂 ほか 2 人が聞いています'", timeout=DISCOVER))
    if not three:
        r.check('T31', 'おたよりが枠主に届き、選ばれると部屋に出る', False, 'pass',
                '3人そろわなかった（配信者の一覧=%s / 聞き役b=%s / 聞き役c=%s）' % (a.eval(MEMBERS), b.eval(MEMBERS), c.eval(MEMBERS)))
        return
    # 枠主：お題を出して募集開始
    show_tab(a, 'mail')
    a.eval("(() => { const t = document.getElementById('mailText');"
           " t.value = '最近食べて美味しかったもの'; document.getElementById('mailSend').click() })()")
    opened = b.wait_for("document.getElementById('mailHead').textContent.includes('最近食べて美味しかったもの')",
                        timeout=SHORT) and b.eval("%s('tabMail')" % VISIBLE)
    # リスナー：投稿する（枠主にだけ届くはず）
    show_tab(b, 'mail')
    b.eval("(() => { const t = document.getElementById('mailText');"
           " t.value = 'つけ麺'; document.getElementById('mailSend').click() })()")
    got = a.wait_for("document.getElementById('mailBody').textContent.includes('つけ麺')", timeout=SHORT)
    # ★もう一人の聞き役には届いていないこと（投稿は枠主にだけ届く）
    leaked = c.eval("document.body.textContent.includes('つけ麺')")
    # 枠主：選んで公開 → 3人全員のタブの外に出る
    a.eval("(() => { const b = [...document.querySelectorAll('#mailBody .mail-pick')].pop(); if (b) b.click() })()")
    shown = all(t.wait_for("!document.getElementById('mailNow').hidden"
                           " && document.getElementById('mailNow').textContent.includes('つけ麺')", timeout=SHORT)
                for t in (a, b, c))
    # 大きく出す紙。**モーダルにしない**ので、後ろの退出ボタンは押せたままであること
    popped = all(t.eval("!document.getElementById('mailPop').hidden"
                        " && document.getElementById('mailPopText').textContent === 'つけ麺'") for t in (a, b, c))
    passthru = b.eval("getComputedStyle(document.getElementById('mailPop')).pointerEvents") == 'none'
    blurred = 'blur' in (b.eval("(() => { const st = getComputedStyle(document.getElementById('mailPop')); return st.backdropFilter || st.webkitBackdropFilter || '' })()") or '')
    # 閉じても行は残る（見失わない）／行を押すと開き直せる
    b.eval("document.getElementById('mailPopClose').click()")
    closed = b.eval("document.getElementById('mailPop').hidden") and not b.eval("document.getElementById('mailNow').hidden")
    b.eval("document.getElementById('mailNow').click()")
    reopened = b.eval("!document.getElementById('mailPop').hidden")
    # v0.16.6：紙の外をタップしても閉じる（中のタップでは閉じない）。本物のマウス操作で
    b.call('Page.bringToFront')
    b.eval("document.querySelectorAll('dialog[open]').forEach(d => d.close())")   # ヘルプの窓が上にあるとタップがそちらに当たる
    def tap(x, y):
        b.call('Input.dispatchMouseEvent', type='mousePressed', x=x, y=y, button='left', clickCount=1)
        b.call('Input.dispatchMouseEvent', type='mouseReleased', x=x, y=y, button='left', clickCount=1)
        time.sleep(0.4)
    cr = json.loads(b.eval("JSON.stringify(document.querySelector('#mailPop .mp-card').getBoundingClientRect())"))
    tap(cr['left'] + cr['width'] / 2, cr['top'] + cr['height'] / 2)
    inside_kept = b.eval("!document.getElementById('mailPop').hidden")
    tap(cr['left'] + cr['width'] / 2, max(2, cr['top'] - 12))
    outside_closed = b.eval("document.getElementById('mailPop').hidden") and not b.eval("document.getElementById('mailNow').hidden")
    r.check('T31d', '公開されたお便りが紙で大きく出る（後ろは触れる）・紙の外のタップで閉じる', popped and passthru and blurred and closed and reopened and inside_kept and outside_closed, 'pass',
            '紙が出た=%s / 後ろを塞いでいない=%s / 背景ぼかし=%s / 閉じても行は残る=%s / 押すと開き直せる=%s / 中のタップで閉じない=%s / 外のタップで閉じる=%s'
            % (popped, passthru, blurred, closed, reopened, inside_kept, outside_closed))
    b.eval("document.getElementById('mailPopClose').click()")
    # 公開中のお便りは**タブの外**なので、どのタブにいても見える
    show_tab(c, 'members')
    outside = c.eval("!document.getElementById('mailNow').hidden")
    ok = opened and got and not leaked and shown and outside
    r.check('T31', 'おたよりが枠主に届き、選ばれると部屋に出る', ok, 'pass',
            '募集が届く=%s / 投稿が枠主に届く=%s / 第三者には漏れない=%s / 公開が全員に出る=%s / タブの外に出る=%s'
            % (opened, got, not leaked, shown, outside))

    # 書き直し（1人1通＝差し替え）。**送る側の下限2秒／受け取る側1.5秒**を越えてから
    # （人が書き直すときにこの間隔を割ることはないが、テストは一瞬で操作してしまう）
    time.sleep(2.5)
    b.eval("(() => { const t = document.getElementById('mailText');"
           " t.value = 'ざるそば'; document.getElementById('mailSend').click() })()")
    swapped = a.wait_for("document.getElementById('mailBody').textContent.includes('ざるそば')", timeout=SHORT)
    once = a.eval("document.querySelectorAll('#mailBody .mail-item').length") == 1
    r.check('T31b', 'お便りの書き直しで差し替わる（1人1通）', swapped and once, 'pass',
            '差し替わった=%s / 件数=1=%s' % (swapped, once))

    # ★募集が始まった**後**に入ってきた人にも、お題と公開中のお便りが出ること。
    #   枠主からの push は**名簿より先に着くことがある**（名簿は署名の計算を挟むので非同期）ので、
    #   先に着いた分は「枠主が出したものか」を判定できず捨てられる。順番に依存しない形
    #   （枠主が分かった瞬間に自分から取りに行く）になっているかを見る。2026-08-17 に実際に落ちた経路。
    d = r.open_tab(hash_=h)
    d.eval("window.__potBcErase(true)")
    click_join(d)
    late_title = d.wait_for("document.getElementById('mailHead').textContent.includes('最近食べて美味しかったもの')",
                            timeout=DISCOVER)
    late_now = d.wait_for("!document.getElementById('mailNow').hidden"
                          " && document.getElementById('mailNow').textContent.includes('つけ麺')", timeout=SHORT)
    r.check('T31c', '後から入った人にも募集と公開中のお便りが出る', late_title and late_now, 'pass',
            'お題が出た=%s / 公開中のお便りが出た=%s' % (late_title, late_now))
    for t in (a, b, c, d):
        click_leave(t)


def T32(r, room):
    """配信部屋に**後から入った人**にも、配信者の声が届く

    2026-08-17 におたよりで踏んだのと**同じ型**の見落としを潰すためのテスト。
    それまでの配信部屋のテスト（T27 系）は**全員が先に揃った状態**しか歩いていなかったので、
    「途中から入る」道には誰も足を踏み入れていなかった。

    ここで確かめているのは、名簿（誰が話してよいか）が**後から来た人にもちゃんと届く**こと。
    届かなければ、その人は誰の音も鳴らさない＝**入れたのに無音**になる（配信部屋の最悪の壊れ方）。
    """
    name = room + 'LATE'
    a = r.open_tab()
    make_bcast(r, a, name)
    if not a.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER):
        r.check('T32', '配信部屋に後から入っても配信者の声が届く', False, 'pass', '配信部屋を作れなかった')
        return
    h = a.eval('location.hash')
    time.sleep(8)   # ★枠主が一人でしばらく居る状態を作る（名簿は既に配り終えている）
    b = r.open_tab(hash_=h)
    b.eval("window.__potBcErase(true)")
    click_join(b)
    met = b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    heard = b.wait_for('%s === 1' % AUDIOS, timeout=DISCOVER)
    # 名簿が届いていれば配信者の行に 📣 が出る（＝誰が配信者かまで分かっている）
    mark = b.eval("[...document.querySelectorAll('#members .m-bc')].some(e => e.textContent === '📣')")
    # ★後から入った人は、名簿が届いて ownerPresent になるまでの約 1 秒だけ「接続が切れました」（grace）が出る仕様。
    #   音が届いた直後に 1 回だけ読むとその一瞬を拾って落ちることがある（2026-09-30）。落ち着くのを待って読む。
    settled = b.wait_for("%s.includes('配信者の声だけが流れます')" % BC_NOTE, timeout=SHORT)
    note = b.eval(BC_NOTE)
    ok = met and heard and mark and settled
    r.check('T32', '配信部屋に後から入っても配信者の声が届く', ok, 'pass',
            '2人=%s / 声が届いた=%s / 配信者が分かる=%s / 説明=%r' % (met, heard, mark, note[:24]))
    for t in (a, b):
        click_leave(t)


def T27h(r, room):
    """通話希望者募集（v0.15.9）：配信者がスイッチをオン → 聞き役に「通話希望」ボタン → 押すと配信者に 🙋 →
    従来どおり 🎙 で許可すると聞き役は「マイクを使う」に変わる。

    ★挙手は grantAction の逆向き（聞き役→配信者）。募集オフの間は聞き役に通話希望ボタンを出さない。
    """
    o = r.open_tab()
    if not make_bcast(r, o, room + 'REQ') or not o.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER):
        r.check('T27h', '通話希望者募集：希望→許可', False, 'pass', '配信部屋を作れなかった')
        return
    l = r.open_tab(hash_=o.eval('location.hash'))
    l.eval("window.__potBcErase(true)")   # 聞き役（鍵を持たない）
    click_join(l)
    # ★③（v0.15.10）：募集オフ・挙手前は、配信者は普通の聞き役を見ない＝自分だけ（1）。聞き役は配信者（声を出せる人）が見える＝2。
    met = o.wait_for('%s === 1' % MEMBERS, timeout=DISCOVER) and l.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    # ① 募集オフの間は聞き役に「通話希望」ボタンが出ない
    off_hidden = l.eval("document.getElementById('raise').hidden")
    # ② 配信者が募集オン → 聞き役にボタンが出る
    show_tab(o, 'settings')
    row_shown = o.wait_for("!document.getElementById('reqOpenRow').hidden", timeout=SHORT)
    o.eval("document.getElementById('reqOpen').click()")
    raise_shown = l.wait_for("!document.getElementById('raise').hidden", timeout=SHORT)
    # ③ 聞き役が「通話希望」→ 配信者のメンバーに 🙋
    l.eval("document.getElementById('raise').click()")
    mark = o.wait_for("[...document.querySelectorAll('#members .m-bc')].some(e => e.textContent === '🙋')", timeout=SHORT)
    label_on = l.eval("document.querySelector('#raise span').textContent") == '取り消す'
    # ④ 配信者が 🎙 で許可 → 🙋 が消えて 🎙、聞き役に「マイクを使う」
    granted = o.eval("(() => { const g = document.querySelector('#members .member:not(:first-child) .m-mic');"
                     " if (g) g.click(); return !!g })()")
    speak_shown = l.wait_for(SPEAK_SHOWN, timeout=SHORT)
    mark_gone = o.wait_for("[...document.querySelectorAll('#members .m-bc')].every(e => e.textContent !== '🙋')"
                           " && [...document.querySelectorAll('#members .m-bc')].some(e => e.textContent === '🎙')", timeout=SHORT)
    raise_gone = l.wait_for("document.getElementById('raise').hidden", timeout=SHORT)   # 許可されたら通話希望は要らない
    ok = met and off_hidden and row_shown and raise_shown and mark and label_on and granted and speak_shown and mark_gone and raise_gone
    r.check('T27h', '通話希望者募集：オフでボタン無し・オンで通話希望→🙋→🎙 許可→マイクを使う', ok, 'pass',
            '2人=%s / 募集オフでボタン無し=%s / 設定に募集行=%s / オンでボタン=%s / 希望で🙋=%s / ボタン文言=%s / 許可した=%s / マイクを使う=%s / 🙋消えて🎙=%s / 通話希望が消える=%s'
            % (met, off_hidden, row_shown, raise_shown, mark, label_on, granted, speak_shown, mark_gone, raise_gone))
    click_leave(l); click_leave(o)


def T63(r, room):
    """通話希望者募集（v0.15.19）：**募集をオンにしてから**入ってきた聞き役にも「通話希望」ボタンが出る

    配信者は聞き役の onPeerJoin の瞬間に「募集中」を送るが、聞き役はまだ名簿の検証中で配信者が分からず、
    v0.15.18 まではそれを捨てていた（オンオフし直すと出る、という利用者の報告）。入れ直さずに出ることを見る。
    """
    o = r.open_tab()
    if not make_bcast(r, o, room + 'REQL') or not o.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER):
        r.check('T63', '後から入った聞き役にも募集が届く', False, 'pass', '配信部屋を作れなかった')
        return
    show_tab(o, 'settings')
    o.wait_for("!document.getElementById('reqOpenRow').hidden", timeout=SHORT)
    o.eval("document.getElementById('reqOpen').click()")
    l = r.open_tab(hash_=o.eval('location.hash'))
    l.eval("window.__potBcErase(true)")   # 聞き役（鍵を持たない）
    click_join(l)
    met = l.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    raise_shown = l.wait_for("!document.getElementById('raise').hidden", timeout=15)   # 配り直し（30 秒）より前に出ること
    r.check('T63', '募集オンのあとに入った聞き役にも、入れ直しなしで「通話希望」が出る', bool(met and raise_shown), 'pass',
            '入室=%s / 15 秒以内に通話希望=%s' % (met, raise_shown))
    click_leave(l); click_leave(o)


def T66(r, room):
    """ロビー：通話中の人がタブを閉じたら（退出の合図なし）、その部屋は数秒で一覧から消える（v0.15.24）

    タブを閉じた・スマホが落ちた人は room: null を送ってこないので、ロビーの onPeerLeave が消す唯一の合図。
    回線の揺れによる点滅を抑えるため 3 秒待ってから消す。PR #4 の「30 秒の掃除に任せる」案では 34.5 秒残った。
    """
    o = r.open_tab()
    r.join(o, room + 'CL')
    c = r.open_tab()
    if not c.wait_for('%s.includes(%s)' % (LOBBY, js_str(room + 'CL')), timeout=DISCOVER):
        r.check('T66', 'タブを閉じた人の部屋が数秒で一覧から消える', False, 'pass', '一覧に出なかった'); return
    o.call('Page.close')   # ブラウザのタブを閉じる（退出ボタンは押さない）
    t0 = time.time()
    gone = c.wait_for('!%s.includes(%s)' % (LOBBY, js_str(room + 'CL')), timeout=20)
    dt = time.time() - t0
    r.check('T66', 'タブを閉じた人の部屋が数秒で一覧から消える', bool(gone) and dt < 10, 'pass', '消えるまで %.1f 秒' % dt)


def ntf_viewer(r):
    """新しい部屋の知らせを受ける閲覧者のタブ（T71・T71b）。Notification をモックしてトグルをオンにし、
    「ほかの画面を見ている」状態（hasFocus が偽）にして、探索期間（DISCOVER_MS 8 秒）が過ぎるまで待つ。"""
    c = r.open_tab()
    c.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    shown = c.eval("!document.getElementById('ntfRoomOpt').hidden")
    c.eval("""(() => {
      window.__ntf = []
      window.Notification = class { constructor(t, o) { window.__ntf.push([t, (o || {}).body || '']) } close() {}
        static get permission() { return 'granted' } static requestPermission() { return Promise.resolve('granted') } }
      const b = document.getElementById('ntfRoom'); b.checked = true; b.dispatchEvent(new Event('change'))
      document.hasFocus = () => false
    })()""")
    time.sleep(10)
    return c, shown


def T71b(r, room):
    """ロビー：常設の部屋（ボットが 24 時間いる）は、ボットだけなら「通話中」と数えず知らせもしない。
    人が入った瞬間に「通話が始まりました」を 1 回知らせ、題名にも数える（v0.15.31）

    数えると題名の印が常時点灯し、ボットの再起動のたびに通知が鳴る。テストはボット役のタブを常設の ID で入れる
    （テストの appId は本番から隔離されているので、本物の案内役とは混ざらない）。
    """
    name = room + 'ST'
    c, _ = ntf_viewer(r)
    base0 = c.eval("document.title")
    b = r.open_tab(hash_='#room=X7fZgwbqs8CqUF8T&name=%s' % urllib.parse.quote(name))
    b.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    b.wait_for("(() => { const e = document.getElementById('entryTry'); return !!e && !e.hidden && !!e.offsetParent })()", timeout=DISCOVER)
    b.eval("document.getElementById('entryTry').click()", await_promise=False)
    listed = c.wait_for('%s.includes(%s)' % (LOBBY, js_str(name)), timeout=DISCOVER)
    time.sleep(3)
    base = c.eval("document.title")   # 常設の部屋（ボットだけ）が一覧に出たあとでも、題名は数えていないはず
    bot_quiet = c.eval("window.__ntf.length") == 0 and base == base0
    h = r.open_tab()
    h.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(h, name)
    got = c.wait_for("window.__ntf.length === 1", timeout=DISCOVER)
    ntf = c.eval("JSON.stringify(window.__ntf)")
    c.eval("Object.defineProperty(document, 'hidden', { get: () => false, configurable: true }); document.hasFocus = () => true; dispatchEvent(new Event('focus'))")
    n_rooms = lambda t: int(re.match(r'● (\d+)部屋で通話中｜', t).group(1)) if re.match(r'● (\d+)部屋で通話中｜', t) else 0
    counted = n_rooms(c.eval("document.title")) == n_rooms(base) + 1   # ほかのテストの部屋が残っていても増分で見る
    # 入室中の h の題名：数が出るなら「ほか」付き（自分の部屋を数えていないため・Issue #9）
    ht = h.eval("document.title")
    joined_title = (not ht.startswith('● ')) or ht.startswith('● ほか')
    ok = bool(listed and bot_quiet and got and name in ntf and counted and joined_title)
    r.check('T71b', '新しい部屋の知らせ：常設の部屋はボットだけなら数えず知らせない・人が入ったら 1 回知らせて数える', ok, 'pass',
            '一覧に出た=%s ボットだけは静か=%s 人が入って通知=%s(%s) 数えた=%s' % (bool(listed), bot_quiet, bool(got), ntf, counted))
    click_leave(h); click_leave(b)


def T71(r, room):
    """ロビー：新しい部屋の知らせ（v0.15.31）。ほかの画面を見ている閲覧者に、OS の通知とタブの題名・アイコンで伝える

    ①通知をオンにした閲覧者は、新しい部屋が始まると OS の通知を 1 回受ける（題名「通話が始まりました」・本文に部屋名）
    ②見ていない間はタブの題名が「🔔 新しい部屋｜…」、戻ると「● 1部屋で通話中｜…」、アイコンが印つき（data:）に
    ③その部屋が消えて 2 分以内に戻っても、もう一度は知らせない（裏タブの在室の途切れで誤報を出さない）
    ④部屋が無くなると題名・アイコンは元に戻る
    Notification はモック（headless は許可の問い合わせを出せない）、「ほかの画面」は document.hasFocus の差し替えで作る。
    """
    name = room + 'NT'
    c, shown = ntf_viewer(r)
    base = c.eval("document.title")
    o = r.open_tab()
    r.join(o, name)
    got = c.wait_for("window.__ntf.length >= 1", timeout=DISCOVER)
    t_first = time.time()
    ntf = c.eval("JSON.stringify(window.__ntf)")
    ok1 = bool(got) and '通話が始まりました' in ntf and name in ntf
    fresh_title = c.eval("document.title").startswith('🔔 新しい部屋｜')
    fav = c.wait_for("[...document.querySelectorAll('link[rel=icon]')].every(l => l.getAttribute('href').startsWith('data:image/png'))", timeout=10)
    # 見に戻る＝前に出ていて焦点もある（テストのタブは裏扱いなので document.hidden も差し替える）
    c.eval("Object.defineProperty(document, 'hidden', { get: () => false, configurable: true }); document.hasFocus = () => true; dispatchEvent(new Event('focus'))")
    count_title = c.eval("document.title").startswith('● 1部屋で通話中｜')
    # ③ 退出→一覧から消える→同じ部屋に入り直す（音の間隔 10 秒の外で）
    room_before = o.eval("new URLSearchParams(location.hash.slice(1)).get('room')")
    click_leave(o)
    gone = c.wait_for('!%s.includes(%s)' % (LOBBY, js_str(name)), timeout=20)
    restored = c.eval("document.title") == base and c.eval("[...document.querySelectorAll('link[rel=icon]')].every(l => !l.getAttribute('href').startsWith('data:'))")
    c.eval("Object.defineProperty(document, 'hidden', { get: () => true, configurable: true }); document.hasFocus = () => false")
    time.sleep(max(0, 12 - (time.time() - t_first)))
    click_join(o)   # 退出しても参加先（pendingHash）は残る＝同じ部屋 ID に入り直す
    back = c.wait_for('%s.includes(%s)' % (LOBBY, js_str(name)), timeout=DISCOVER)
    time.sleep(3)
    same_room = o.eval("new URLSearchParams(location.hash.slice(1)).get('room')") == room_before
    quiet = c.eval("window.__ntf.length") == 1
    ok = bool(shown and ok1 and fresh_title and fav and count_title and gone and restored and back and same_room and quiet)
    r.check('T71', '新しい部屋の知らせ：通知 1 回・題名とアイコンの印・消えてすぐ戻った部屋は知らせない・無くなれば元に戻る', ok, 'pass',
            'トグル表示=%s 通知=%s 🔔題名=%s 印アイコン=%s ●題名=%s 消えた=%s 元に戻った=%s 戻った=%s 同じ部屋=%s 再通知なし=%s'
            % (shown, ntf, fresh_title, bool(fav), count_title, bool(gone), restored, bool(back), same_room, quiet))
    click_leave(o)


def T72(r, room):
    """プロフィールカード（名刺交換機能・v0.16.0）：
    通話相手のアバター/名前タップでモーダル展開、相手へP2P要求（action 'card'）、
    相手の名刺（自己紹介・認証済みSNS・チップ）が表示されること。
    自衛ボタン（非表示＆消音）で即座に相手が無視され、ダイアログが閉じること。
    """
    name = room + 'CARD'
    a, b = r.open_tab(), r.open_tab()
    # タブBに名刺データ（アバター画像付き）を設定
    b.eval("""(() => {
        const card = {
            avatar: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
            bio: 'テスト自己紹介',
            tip: { p: 'ofuse', id: 'sampleuser' },
            socials: { x: 'test_x_user', note: 'test_note', nostr: 'npub1exu6zn04ffgzk2g4mcwwl7kzeczdxg2l3ymj8q6e4l3n8f0u0c3s4w9abc' },
            verified: { x: true }
        };
        localStorage.setItem('pot-call-card', JSON.stringify(card));
    })()""")
    r.join(a, name)
    r.join(b, name)
    joined = (a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and
              b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER))

    # タブAでメンバー一覧内の相手（.you を持たない行）のアバターまたは名前をクリック
    opened = a.eval("""(() => {
        const row = [...document.querySelectorAll('#members .member')].find(el => !el.querySelector('.you'));
        if (!row) return false;
        const tap = row.querySelector('.m-tap');
        if (!tap) return false;
        tap.click();
        return true;
    })()""")
    dlg_open = a.wait_for("document.getElementById('cardDlg').open", timeout=SHORT)

    # 相手の名刺データが届いて反映されるのを待つ
    bio_ok = a.wait_for("document.getElementById('cdBio').textContent === 'テスト自己紹介'", timeout=SHORT)
    tip_ok = a.eval("document.getElementById('cdTipSub').textContent.includes('sampleuser')")
    avatar_ok = a.wait_for("!document.getElementById('cdAvatar').hidden && document.getElementById('cdEmoji').hidden && !document.getElementById('cdSubEmoji').hidden", timeout=SHORT)
    # v0.16.1：「認証済み」の自己申告（localStorage の verified）は送らない・✅ にしない
    x_badge = a.eval("""(() => !document.querySelector('#cdSocials .cd-ok'))()""")
    un_badge = a.eval("""(() => {
        const un = document.querySelector('#cdSocials .cd-un');
        return un && un.textContent.includes('未認証');
    })()""")
    nostr_shortened = a.eval("""(() => {
        const ids = [...document.querySelectorAll('#cdSocials .cd-id')].map(el => el.textContent);
        return ids.some(t => t.startsWith('npub1exu6') && t.includes('…') && t.length < 20);
    })()""")

    btn_has_heart = a.eval("""(() => {
        const btn = document.getElementById('cdIgnore');
        return !!(btn && btn.querySelector('svg'));
    })()""")

    # 閉じるボタン（#cdClose）で閉じられることを確認
    close_btn = a.eval("""(() => {
        const btn = document.getElementById('cdClose');
        if (!btn) return false;
        btn.click();
        return true;
    })()""")
    closed_by_x = a.wait_for("!document.getElementById('cardDlg').open", timeout=SHORT)

    # チャットログからの名刺展開テスト：タブBからひとこと送信
    b.eval("""(() => {
        const txt = document.getElementById('chatText');
        if (txt) txt.value = 'テストチャット';
        const snd = document.getElementById('chatSend');
        if (snd) snd.click();
    })()""")
    chat_arrived = a.wait_for("document.querySelectorAll('#chatLog .cl-row:not(.cl-sys)').length >= 1", timeout=SHORT)
    # 1回目のタップで行を選択（.pick になり❤️が出る）
    row_picked = a.eval("""(() => {
        const row = document.querySelector('#chatLog .cl-row.can');
        if (!row) return false;
        row.click();
        return row.classList.contains('pick');
    })()""")
    # 2回目のタップ（選択中に名前をタップ）で名刺が開く
    name_tapped = a.eval("""(() => {
        const who = document.querySelector('#chatLog .cl-row.pick .cl-who');
        if (!who) return false;
        who.click();
        return true;
    })()""")
    dlg_open_from_chat = a.wait_for("document.getElementById('cardDlg').open", timeout=SHORT)

    # 自衛ボタン（#cdIgnore）をクリック
    ignored = a.eval("""(() => {
        const btn = document.getElementById('cdIgnore');
        if (!btn || btn.hidden) return false;
        btn.click();
        return true;
    })()""")
    dlg_closed = a.wait_for("!document.getElementById('cardDlg').open", timeout=SHORT)

    # タブAでタブBが無視されたか確認（メンバー行に .ignored が付く）
    peer_ignored = a.wait_for("document.querySelectorAll('#members .member.ignored').length === 1", timeout=SHORT)

    ok = bool(joined and opened and dlg_open and bio_ok and tip_ok and avatar_ok and x_badge and un_badge and nostr_shortened and btn_has_heart and close_btn and closed_by_x and chat_arrived and row_picked and name_tapped and dlg_open_from_chat and ignored and dlg_closed and peer_ignored)
    r.check('T72', '名刺交換：アバタータップで展開・相手カード受信(bio/SNS/自己申告は✅にしない/Tip/画像＋添え絵文字/Nostr短縮/自衛ハートSVG)・✕で閉じる・チャット名前タップで開く・自衛ボタンで無視連動', ok, 'pass',
            '2人参加=%s / 開いた=%s / モーダル開=%s / bio=%s / tip=%s / アバター画像=%s / 自己申告は✅なし=%s / 未認証バッジ=%s / Nostr短縮=%s / 自衛ハート=%s / ✕閉=%s / チャット着=%s / 行選択=%s / 名前タップ=%s / チャットから開=%s / 自衛押下=%s / 閉じた=%s / 無視反映=%s'
            % (joined, opened, dlg_open, bio_ok, tip_ok, avatar_ok, x_badge, un_badge, nostr_shortened, btn_has_heart, closed_by_x, chat_arrived, row_picked, name_tapped, dlg_open_from_chat, ignored, dlg_closed, peer_ignored))
    click_leave(a); click_leave(b)


def T74(r, room):
    """名刺の本人確認を受け手が確かめる（v0.16.1）

    ①持ち主：Nostr の拡張機能（モック）で名刺の鍵に署名して認証。お礼は Zap（npub）
    ②受け手：開いた時点は「要確認」・お礼は控えめ（weak）→「✅ を確かめる」で問い返し（鍵の持ち主か）と
      証拠（Nostr の署名）を確かめて ✅・お礼はふつう＋「Zap の行き先も確認」
    ③偽の証拠：別の指紋の行に署名したイベントと、他人（@jack）の X 投稿を証拠として送る → どちらも ✅ にならず、
      お礼は控えめのまま、押すと「本人確認されていない窓口」の確認が挟まる
    ※smoke のタブは同じ Chrome プロファイル＝localStorage と IndexedDB（名刺の鍵）を共有する。持ち主と受け手が
      同じ鍵になるが、問い返しと証拠の照合の経路は本物と同じ
    """
    name = room + 'VER'
    a = r.open_tab()
    a.eval("localStorage.removeItem('pot-call-card'); localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    npub = a.eval("""(async () => {
        const nt = await import('./vendor/nostr-tools-2.10.4.mjs')
        const sk = nt.generateSecretKey(), pk = nt.getPublicKey(sk)
        window.__nt = nt; window.__sk = sk
        window.nostr = { getPublicKey: async () => pk, signEvent: async ev => nt.finalizeEvent(ev, sk) }
        return nt.nip19.npubEncode(pk)
    })()""")
    a.eval("document.getElementById('btnOpenCardEdit').click()")
    a.wait_for("document.getElementById('cardEditDlg').open", timeout=SHORT)
    a.eval("""(() => {
        const n = document.getElementById('ceSns_nostr'); n.value = %s; n.dispatchEvent(new Event('input'))
        const z = document.getElementById('ceTip_zap'); z.value = %s; z.dispatchEvent(new Event('input'))
        document.getElementById('ceSave').click()
    })()""" % (js_str(npub), js_str(npub)))
    a.wait_for("!document.getElementById('cardEditDlg').open", timeout=SHORT)
    a.eval("document.getElementById('btnOpenCardEdit').click()")
    a.wait_for("document.getElementById('cardEditDlg').open", timeout=SHORT)
    a.eval("document.getElementById('ceBtnVer_nostr').click()")
    a.wait_for("document.getElementById('cardAuthDlg').open", timeout=SHORT)
    a.eval("document.getElementById('caVerifyBtn').click()")
    owner_ok = a.wait_for("document.getElementById('caMsg').classList.contains('ok')", timeout=30)
    owner_msg = a.eval("document.getElementById('caMsg').textContent")
    a.eval("document.querySelectorAll('dialog[open]').forEach(d => d.close())")

    b = r.open_tab()
    b.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(a, name)
    r.join(b, name)
    joined = b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    OPEN = """(() => { const row = [...document.querySelectorAll('#members .member')].find(el => !el.querySelector('.you'));
        const t = row && row.querySelector('.m-tap'); if (!t) return false; t.click(); return true })()"""
    CHIP = "[...document.querySelectorAll('#cdSocials .cd-sns')].map(e => e.textContent).join('|')"
    b.eval(OPEN)
    t_req = time.time()
    claim = b.wait_for("%s.includes('要確認')" % CHIP, timeout=SHORT)
    weak = b.eval("document.getElementById('cdTip').classList.contains('weak')")
    b.eval("document.getElementById('cdVerifyBtn').click()")
    verified = b.wait_for("[...document.querySelectorAll('#cdSocials .cd-ok')].some(e => e.textContent.includes('✅'))", timeout=30)
    strong = b.wait_for("!document.getElementById('cdTip').classList.contains('weak') && document.getElementById('cdTipTrust').textContent.includes('Zap の行き先')", timeout=SHORT)
    chips1 = b.eval(CHIP)

    # ③偽の証拠：別の指紋の行に署名したイベント・他人（@jack）の投稿。鍵は同じ（問い返しは通る）が証拠が合わない
    a.eval("""(async () => {
        const c = JSON.parse(localStorage.getItem('pot-call-card') || '{}')
        const fp = c.proofs.nostr.fp
        c.proofs.nostr.ev = window.__nt.finalizeEvent({ kind: 30078, created_at: Math.floor(Date.now() / 1000), tags: [['d', 'potalk-card']], content: 'potalk1-AAAAAAAAAAAAAAAAAAAAAA' }, window.__sk)
        c.socials.x = 'jack'; c.proofs.x = { id: 'jack', sid: '20', fp }
        localStorage.setItem('pot-call-card', JSON.stringify(c))
    })()""")
    b.eval("document.getElementById('cdClose').click()")
    time.sleep(max(0, 11 - (time.time() - t_req)))   # 同じ相手への名刺の要求は 10 秒に 1 回まで
    b.eval(OPEN)
    reclaim = b.wait_for("%s.includes('要確認') && %s.includes('jack')" % (CHIP, CHIP), timeout=SHORT)
    b.eval("document.getElementById('cdVerifyBtn').click()")
    done2 = b.wait_for("!document.getElementById('cdVerifyBtn').disabled && document.getElementById('cdVerifyMsg').textContent.length > 0 && !%s.includes('確認中')" % CHIP, timeout=40)
    chips2 = b.eval(CHIP)
    fake_rejected = '✅' not in chips2 and chips2.count('確認できません') == 2
    weak2 = b.eval("document.getElementById('cdTip').classList.contains('weak')")
    b.eval("document.getElementById('cdTipLink').click()")
    warned = b.wait_for("document.getElementById('cardWarnDlg').open && document.getElementById('cdWarnTitle').textContent.includes('本人確認されていない')", timeout=SHORT)
    b.eval("document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    ok = bool(owner_ok and joined and claim and weak and verified and strong and reclaim and done2 and fake_rejected and weak2 and warned)
    r.check('T74', '名刺の本人確認：受け手が問い返し＋証拠で ✅（Zap の行き先も）・偽の証拠（別の指紋・他人の X 投稿）は ✅ にならず、お礼は控えめ＋確認が挟まる', ok, 'pass',
            '持ち主の認証=%s(%s) / 参加=%s / 要確認=%s / お礼控えめ=%s / 確かめて✅=%s / お礼ふつう=%s [%s] / 偽を受信=%s / 確かめ終わり=%s / 偽は✅なし=%s [%s] / お礼控えめ=%s / 確認が挟まる=%s'
            % (bool(owner_ok), owner_msg[:30], bool(joined), bool(claim), weak, bool(verified), bool(strong), chips1, bool(reclaim), bool(done2), fake_rejected, chips2, weak2, bool(warned)))
    a.eval("localStorage.removeItem('pot-call-card')")
    click_leave(a); click_leave(b)


def T75(r, room):
    """ひとことの行をタップすると、名前の隣に無視のハートが見える（14px）。もう一度名前を押すと名刺が開く

    v0.16.0 でハートの大きさを決める CSS が消えて 18px になり、v0.16.1 で戻した。表示そのものの回帰も見る。
    """
    name = room + 'HRT'
    a, b = r.open_tab(), r.open_tab()
    for t in (a, b):   # ヘルプの窓が開いていると、タップがそちらに当たる
        t.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(a, name); r.join(b, name)
    joined = a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    b.eval("(() => { document.getElementById('tabChat')?.click(); const t = document.getElementById('chatText'); t.value = 'ハートの確認'; document.getElementById('chatSend').click() })()")
    a.eval("document.getElementById('tabChat')?.click()")
    arrived = a.wait_for("!!document.querySelector('#chatLog .cl-row.can')", timeout=SHORT)
    # スマホの幅で、指のタップ（タッチイベント）として行の本文を押す（element.click() では拾えない違いを見るため）
    a.call('Page.bringToFront')   # 裏のタブには入力イベントが届かない（描画待ちで止まる）
    a.call('Emulation.setDeviceMetricsOverride', width=390, height=844, deviceScaleFactor=2, mobile=True)
    a.call('Emulation.setTouchEmulationEnabled', enabled=True, maxTouchPoints=5)
    a.eval("document.querySelector('#chatLog .cl-row.can')?.scrollIntoView({block: 'center'})")
    time.sleep(0.5)
    pt = a.eval("(() => { const e = document.querySelector('#chatLog .cl-row.can .cl-text'); if (!e) return null; const r = e.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2] })()")
    for t in ('touchStart', 'touchEnd'):
        if pt is not None:
            a.call('Input.dispatchTouchEvent', type=t, touchPoints=([{'x': pt[0], 'y': pt[1]}] if t == 'touchStart' else []))
    time.sleep(0.5)
    size = a.eval("""(() => { const b = document.querySelector('#chatLog .cl-row.pick .cl-ig'); if (!b) return 'no-button';
        const st = getComputedStyle(b), sv = b.querySelector('svg'); if (!sv) return 'no-svg';
        const r = sv.getBoundingClientRect(); return st.display + ' ' + Math.round(r.width) + 'x' + Math.round(r.height) })()""")
    shown = isinstance(size, str) and not size.startswith('none') and size.endswith('14x14')
    picked = a.eval("!!document.querySelector('#chatLog .cl-row.pick')")
    a.eval("document.querySelector('#chatLog .cl-row.can .cl-who')?.click()")
    card = a.wait_for("document.getElementById('cardDlg') && document.getElementById('cardDlg').open", timeout=SHORT) if picked else False
    a.eval("document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    ok = bool(joined and arrived and picked and shown and card)
    r.check('T75', 'ひとことの行を選ぶと無視のハートが見える（14px）・名前をもう一度押すと名刺', ok, 'pass',
            '参加=%s / 着信=%s / タップで選択=%s / ハート=%s / 名刺=%s' % (bool(joined), bool(arrived), picked, size, bool(card)))
    click_leave(a); click_leave(b)


def T76(r, room):
    """同梱ライブラリ（v0.16.2）：vendor/ のファイルが SHA256SUMS と一致し、ページは esm.sh へ一切取りに行かない

    中身を 1 バイトでも変えたら落ちる（版を上げるときは vendor/README.md の手順で SHA256SUMS も揃える）。
    例外は trystero-0.25.4-pool0.mjs（v0.16.27）：公開版の数字 1 つ（lo=20→0）だけを変えた写しで、それ以外が同じことも見る。
    参加・QR・在室の公開（nostr-tools）まで通して、読み込んだスクリプトの出どころを見る。
    ＋読み上げ（v0.16.14）：import map と CSP に jsDelivr／unpkg が残っていない（実エンジンは 100MB なので落とさず、宛先だけ見る）。
    """
    import hashlib
    here = os.path.dirname(os.path.abspath(__file__))
    vd = os.path.join(here, '..', 'vendor')
    bad = []
    for line in open(os.path.join(vd, 'SHA256SUMS')):
        h, f = line.split()
        if hashlib.sha256(open(os.path.join(vd, f), 'rb').read()).hexdigest() != h:
            bad.append(f)
    stock = open(os.path.join(vd, 'trystero-0.25.4.mjs')).read()
    if stock.count('lo=20,Ft=class') != 1 or open(os.path.join(vd, 'trystero-0.25.4-pool0.mjs')).read() != stock.replace('lo=20,Ft=class', 'lo=0,Ft=class'):
        bad.append('trystero-0.25.4-pool0.mjs（公開版との違いが lo だけではない）')
    a = r.open_tab()
    a.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(a, room + 'VND')
    joined = a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    qr = flip_back(a)   # QR はカードの裏
    time.sleep(3)   # 在室の公開（nostr-tools の読み込み）を待つ
    urls = a.eval("JSON.stringify(performance.getEntriesByType('resource').map(e => e.name))")
    names = json.loads(urls)
    esm = [u for u in names if 'esm.sh' in u or 'jsdelivr' in u or 'unpkg' in u]
    vend = sorted({u.split('/vendor/')[1] for u in names if '/vendor/' in u})
    cdn = a.eval("(() => { const t = document.querySelector('script[type=importmap]').textContent + document.querySelector('meta[http-equiv=Content-Security-Policy]').content;"
                 " return /jsdelivr|unpkg|esm\\.sh/.test(t) })()")
    tts = a.eval("Object.values(JSON.parse(document.querySelector('script[type=importmap]').textContent).imports).every(u => u.startsWith('./vendor/'))")
    ok = bool(not bad and joined and qr and not esm and not cdn and tts and [v for v in vend if v.startswith('trystero-')] in (['trystero-0.25.4-pool0.mjs'], ['trystero-0.25.4.mjs']) and 'qrcode-generator-1.4.4.mjs' in vend)
    r.check('T76', '同梱ライブラリ：SHA256SUMS と一致・外部 CDN へ取りに行かない・vendor/ から読む（読み上げの import map も）', ok, 'pass',
            '不一致=%s / 参加=%s / QR=%s / CDN=%s / map・CSP に CDN=%s / 読み上げ→vendor=%s / vendor=%s' % (bad, bool(joined), bool(qr), esm[:3], cdn, tts, vend))
    click_leave(a)


def T77(r, room):
    """見えない文字・向きを変える文字の掃除（v0.16.3）：名前・ひとこと・名刺の bio に混ぜても、相手の画面では消えている

    「jack」と「ja＋見えない文字＋ck」は画面で同じに見えるので、名乗りの偽装に使える（監査の改善メモ・名刺のなりすまし対策）。
    絵文字の結合（ZWJ）は残る（👨‍👩‍👧 が壊れない）。JS 側では \\u エスケープで書く（実体を貼らない）。
    """
    name = room + 'INV'
    a, b = r.open_tab(), r.open_tab()
    for t in (a, b):
        t.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    # b：名前と名刺の bio に見えない文字を混ぜる（同じプロファイルなので名刺は localStorage を直接書く）
    # ★smoke のタブは localStorage を共有し、通話中はプロフィールの編集がロックされる。A が入った**後**、B が入る**直前**に
    #   保存済みのプロフィールの名前を書き換える（B は入るときにリンクで読み込み直すので、その名前で入る）
    r.join(a, name)
    a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    b.eval("""(() => {
        const p = JSON.parse(localStorage.getItem('pot-call-profile') || '{}'); p.name = 'ja\\u200Bck\\u2060\\u3164\\u202E'
        localStorage.setItem('pot-call-profile', JSON.stringify(p))
        localStorage.setItem('pot-call-card', JSON.stringify({ bio: 'he\\u200Bllo\\u2028\\uFEFF\\u00AD👨\\u200D👩\\u200D👧' }))
    })()""")
    r.join(b, name)
    joined = a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    # 行は相手のプロフィールが届く前から「ゲスト」で出るので、名前が届くのを待ってから読む
    a.wait_for("[...document.querySelectorAll('#members .member')].some(el => !el.querySelector('.you') && el.querySelector('.m-name').textContent !== 'ゲスト')", timeout=SHORT)
    shown_name = a.eval("[...document.querySelectorAll('#members .member')].filter(el => !el.querySelector('.you')).map(el => el.querySelector('.m-name').textContent).join('|')")
    b.eval("(() => { document.getElementById('tabChat')?.click(); const t = document.getElementById('chatText'); t.value = 'sa\\u200Cfe\\u2066'; document.getElementById('chatSend').click() })()")
    a.eval("document.getElementById('tabChat')?.click()")
    a.wait_for("!!document.querySelector('#chatLog .cl-row.can .cl-text')", timeout=SHORT)
    chat = a.eval("document.querySelector('#chatLog .cl-row.can .cl-text').textContent")
    a.eval("document.querySelector('#chatLog .cl-row.can .cl-who').click()"); a.eval("document.querySelector('#chatLog .cl-row.pick .cl-who')?.click()")
    a.wait_for("document.getElementById('cardDlg').open && document.getElementById('cdBio').textContent.length > 0", timeout=SHORT)
    bio = a.eval("document.getElementById('cdBio').textContent")
    a.eval("document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    fam = '\U0001F468\u200D\U0001F469\u200D\U0001F467'
    ok = bool(joined and shown_name == 'jack' and chat == 'safe' and bio == 'hello' + fam)
    r.check('T77', '見えない文字・向きを変える文字は、名前・ひとこと・名刺の bio で相手の画面から消える（絵文字の結合は残る）', ok, 'pass',
            '参加=%s / 名前=%r / ひとこと=%r / bio=%r' % (bool(joined), shown_name, chat, bio))
    b.eval("(() => { const i = document.getElementById('name'); i.value = ''; i.dispatchEvent(new Event('input')); localStorage.removeItem('pot-call-card') })()")
    click_leave(a); click_leave(b)


def T78(r, room):
    """日本語入力の「変換を確定する Enter」では、ひとことを送らない（v0.16.3・10-11 利用者の報告）

    Mac の Safari は変換の確定の後に keydown を送り、そのとき isComposing は false・keyCode は 229。
    isComposing だけを見ていると、確定の Enter がそのまま送信になる。合成イベントで 3 通りを確かめる：
    ①変換中（isComposing=true）②確定の Enter（isComposing=false・keyCode=229）→ どちらも送らない ③ふつうの Enter → 送る
    """
    a = r.open_tab()
    a.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(a, room + 'IME')
    joined = a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    a.eval("document.getElementById('tabChat')?.click()")
    ROWS = "document.querySelectorAll('#chatLog .cl-row:not(.cl-sys)').length"
    KEY = """(() => { const t = document.getElementById('chatText'); t.value = %s; t.focus()
        const e = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true, isComposing: %s })
        Object.defineProperty(e, 'keyCode', { get: () => %d }); t.dispatchEvent(e) })()"""
    n0 = a.eval(ROWS)
    a.eval(KEY % ("'へんかんちゅう'", 'true', 229)); time.sleep(0.5)
    n1 = a.eval(ROWS)
    a.eval(KEY % ("'変換確定'", 'false', 229)); time.sleep(0.5)
    n2 = a.eval(ROWS)
    kept = a.eval("document.getElementById('chatText').value")
    a.eval(KEY % ("'送る'", 'false', 13))
    sent = a.wait_for("%s === %d" % (ROWS, n0 + 1), timeout=SHORT)
    ok = bool(joined and n1 == n0 and n2 == n0 and kept == '変換確定' and sent)
    r.check('T78', '変換中・変換確定（keyCode 229）の Enter では送らず、ふつうの Enter で送る', ok, 'pass',
            '参加=%s / 変換中で送らない=%s / 確定で送らない=%s（欄に残る=%r）/ ふつうの Enter で送る=%s' % (bool(joined), n1 == n0, n2 == n0, kept, bool(sent)))
    click_leave(a)


def T79(r, room):
    """お礼の Zap（Lightning アドレス）：同じタブで開き（ウォレットアプリに渡す）、アドレスをコピーできる（v0.16.5・10-11 テスターの報告）

    lightning: を新しいタブで開くと、iOS Safari は「アドレスが無効です」になる（アプリが無いときも同じ）。
    https の窓口（OFUSE など）は従来どおり新しいタブ・コピーの欄は出ない。
    """
    addr = 'potalk_test@walletofsatoshi.com'
    a = r.open_tab()
    a.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    a.eval("localStorage.setItem('pot-call-card', JSON.stringify({ tip: { p: 'zap', id: %s } }))" % js_str(addr))
    try:
        a.call('Browser.grantPermissions', permissions=['clipboardReadWrite', 'clipboardSanitizedWrite'], origin=r.args.url.rstrip('/'))
    except Exception:
        pass
    r.join(a, room + 'ZAP')
    joined = a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    OPEN_SELF = "(() => { const row = [...document.querySelectorAll('#members .member')].find(el => el.querySelector('.you')); row.querySelector('.m-tap').click() })()"
    a.eval(OPEN_SELF)
    a.wait_for("document.getElementById('cardDlg').open && !document.getElementById('cdTip').hidden", timeout=SHORT)
    ln = a.eval("(() => { const l = document.getElementById('cdTipLink'); return l.getAttribute('href') + ' ' + l.target })()")
    copy_shown = a.eval("!document.getElementById('cdTipLn').hidden")
    a.call('Page.bringToFront')
    a.eval("document.getElementById('cdTipCopy').click()")
    copied = a.wait_for("document.getElementById('cdTipCopy').textContent.includes('コピーしました')", timeout=SHORT)
    clip = a.eval("navigator.clipboard.readText().catch(() => '')")
    a.eval("document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    # https の窓口は新しいタブ・コピーの欄なし
    a.eval("localStorage.setItem('pot-call-card', JSON.stringify({ tip: { p: 'ofuse', id: 'sampleuser' } }))")
    a.eval(OPEN_SELF)
    a.wait_for("document.getElementById('cardDlg').open && document.getElementById('cdTipSub').textContent.includes('sampleuser')", timeout=SHORT)
    https = a.eval("(() => { const l = document.getElementById('cdTipLink'); return l.getAttribute('href') + ' ' + l.target })()")
    no_copy = a.eval("document.getElementById('cdTipLn').hidden")
    a.eval("document.querySelectorAll('dialog[open]').forEach(d => d.close()); localStorage.removeItem('pot-call-card')")
    ok = bool(joined and ln == 'lightning:%s _self' % addr and copy_shown and copied and clip in (addr, '') and https == 'https://ofuse.me/sampleuser _blank' and no_copy)
    r.check('T79', 'お礼の Zap は同じタブで開き・アドレスをコピーできる／https の窓口は新しいタブ・コピー欄なし', ok, 'pass',
            '参加=%s / Zap=%r / コピー欄=%s / コピー=%s(%r) / https=%r / コピー欄なし=%s' % (bool(joined), ln, copy_shown, bool(copied), clip, https, no_copy))
    click_leave(a)


def T80(r, room):
    """名刺の窓は、背景（窓の外）をタップしても閉じる（v0.16.6・利用者の要望）

    ①窓の中を押しても閉じない ②窓の中で押して外で離しても閉じない（文字を選ぶ途中）③窓の外を押すと閉じる
    本物のマウス操作（Input.dispatchMouseEvent）で確かめる。
    """
    a = r.open_tab()
    a.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    a.eval("localStorage.setItem('pot-call-card', JSON.stringify({ bio: '背景で閉じる確認' }))")
    r.join(a, room + 'BKD')
    joined = a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    a.call('Page.bringToFront')
    OPEN_SELF = "(() => { const row = [...document.querySelectorAll('#members .member')].find(el => el.querySelector('.you')); row.querySelector('.m-tap').click() })()"
    OPENED = "document.getElementById('cardDlg').open"
    def press(x0, y0, x1=None, y1=None):
        x1 = x0 if x1 is None else x1; y1 = y0 if y1 is None else y1
        a.call('Input.dispatchMouseEvent', type='mousePressed', x=x0, y=y0, button='left', clickCount=1)
        if (x1, y1) != (x0, y0):
            a.call('Input.dispatchMouseEvent', type='mouseMoved', x=x1, y=y1, button='left')
        a.call('Input.dispatchMouseEvent', type='mouseReleased', x=x1, y=y1, button='left', clickCount=1)
        time.sleep(0.4)
    a.eval(OPEN_SELF); a.wait_for(OPENED, timeout=SHORT)
    rc = a.eval("JSON.stringify(document.getElementById('cardDlg').getBoundingClientRect())")
    rc = json.loads(rc)
    cx, cy = rc['left'] + rc['width'] / 2, rc['top'] + 40
    press(cx, cy)
    inside_kept = a.eval(OPENED)
    press(cx, cy, 5, 5)
    drag_kept = a.eval(OPENED)
    press(5, 5)
    closed = a.wait_for("!" + OPENED, timeout=SHORT)
    a.eval(OPEN_SELF)
    reopen = a.wait_for(OPENED, timeout=SHORT)
    a.eval("document.querySelectorAll('dialog[open]').forEach(d => d.close()); localStorage.removeItem('pot-call-card')")
    ok = bool(joined and inside_kept and drag_kept and closed and reopen)
    r.check('T80', '名刺の窓：外をタップで閉じる・中のタップや中から外へのドラッグでは閉じない', ok, 'pass',
            '参加=%s / 中で閉じない=%s / ドラッグで閉じない=%s / 外で閉じる=%s / 開き直せる=%s' % (bool(joined), inside_kept, drag_kept, bool(closed), bool(reopen)))
    click_leave(a)


def T81(r, room):
    """名刺の SNS に YouTube（v0.16.7・まずは自己申告）：URL を @ハンドルか UC… に切り詰め、旧形式は案内、名刺では「未認証」

    ハンドルは日本語も使える（URL では %E3… に符号化されている）。/c/ と /user/ は名前からチャンネルを特定できないので受けない。
    """
    a = r.open_tab()
    a.eval("localStorage.setItem('pot-call-hide-help','1'); localStorage.removeItem('pot-call-card'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    a.eval("document.getElementById('btnOpenCardEdit').click()")
    a.wait_for("document.getElementById('cardEditDlg').open", timeout=SHORT)
    TRIM = """(v => { const i = document.getElementById('ceSns_youtube'); i.value = v; i.dispatchEvent(new Event('input'))
        return [i.value, i.classList.contains('is-bad'), document.getElementById('ceWarn').hidden ? '' : document.getElementById('ceWarn').textContent] })(%s)"""
    cases = {
        'https://www.youtube.com/@potalk_app/videos': ('@potalk_app', False),
        'https://m.youtube.com/@%E3%81%BD%E3%81%A3%E3%81%A8%E9%80%9A%E8%A9%B1': ('@ぽっと通話', False),
        'https://youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw': ('UCuAXFkgsw1L7xaCfnd5JJOw', False),
        'ぽっと通話': ('@ぽっと通話', False),
        'https://www.youtube.com/c/OldName': (None, True),
        'ja\u200Bck': (None, True),
    }
    got = {}
    for v, (want, bad) in cases.items():
        val, is_bad, warn = a.eval(TRIM % json.dumps(v))
        got[v] = (val, is_bad, warn[:60])
    ok_trim = all((got[v][0] == want if want else True) and got[v][1] == bad for v, (want, bad) in cases.items())
    old_hint = '@ハンドル' in got['https://www.youtube.com/c/OldName'][2]
    a.eval(TRIM % json.dumps('https://www.youtube.com/@%E3%81%BD%E3%81%A3%E3%81%A8%E9%80%9A%E8%A9%B1'))
    a.eval("document.getElementById('ceSave').click()")
    saved = a.wait_for("!document.getElementById('cardEditDlg').open", timeout=SHORT)
    r.join(a, room + 'YT')
    a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    a.eval("(() => { const row = [...document.querySelectorAll('#members .member')].find(el => el.querySelector('.you')); row.querySelector('.m-tap').click() })()")
    a.wait_for("document.getElementById('cardDlg').open", timeout=SHORT)
    chip = a.eval("(() => { const c = [...document.querySelectorAll('#cdSocials .cd-sns')].find(e => e.textContent.includes('YouTube')); return c ? c.textContent + ' ' + c.getAttribute('href') : '' })()")
    ok_chip = '@ぽっと通話' in chip and '未認証' in chip and 'https://www.youtube.com/@%E3%81%BD%E3%81%A3%E3%81%A8%E9%80%9A%E8%A9%B1' in chip
    a.eval("document.querySelectorAll('dialog[open]').forEach(d => d.close()); localStorage.removeItem('pot-call-card')")
    ok = bool(ok_trim and old_hint and saved and ok_chip)
    r.check('T81', '名刺の YouTube：URL を @ハンドル／UC… に切り詰め（日本語ハンドル可）・旧形式は案内・名刺では未認証', ok, 'pass',
            '切り詰め=%s %r / 旧形式の案内=%s / 保存=%s / 名刺=%r' % (ok_trim, got, old_hint, bool(saved), chip))
    click_leave(a)


def T82(r, room):
    """名刺の画像（v0.16.8）：切り抜きの窓でドラッグした位置が反映される・透明は白・JPEG で 4 万文字以内・やめるで変わらない・30MB 超は断る

    テスト用の画像はページの中で作る（左半分が赤・右半分が青の 4000×3000）。右へドラッグして左端を見せ、拡大して決定
    → 仕上がりの真ん中は赤。全面透明の PNG は白になる（JPEG は透明を持てず、塗らないと黒）。
    """
    a = r.open_tab()
    a.eval("localStorage.setItem('pot-call-hide-help','1'); localStorage.removeItem('pot-call-card'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    a.call('Page.bringToFront')
    a.eval("document.getElementById('btnOpenCardEdit').click()")
    a.wait_for("document.getElementById('cardEditDlg').open", timeout=SHORT)
    PICK = """(async kind => {
        let blob
        if (kind === 'big') blob = new Blob([new Uint8Array(31 * 1024 * 1024)], { type: 'image/png' })
        else {
          const c = document.createElement('canvas')
          if (kind === 'clear') { c.width = c.height = 1000 }
          else { c.width = 4000; c.height = 3000; const g = c.getContext('2d'); g.fillStyle = '#ff0000'; g.fillRect(0, 0, 2000, 3000); g.fillStyle = '#0000ff'; g.fillRect(2000, 0, 2000, 3000) }
          blob = await new Promise(ok => c.toBlob(ok, 'image/png'))
        }
        const dt = new DataTransfer(); dt.items.add(new File([blob], kind + '.png', { type: 'image/png' }))
        const i = document.getElementById('ceAvatarFile'); i.files = dt.files; i.dispatchEvent(new Event('change'))
        return true
    })(%s)"""
    CENTER = """(async () => { const src = document.getElementById('ceAvatarImg').getAttribute('src') || ''
        if (!src.startsWith('data:image/jpeg')) return 'none'
        const im = new Image(); await new Promise(ok => { im.onload = ok; im.src = src })
        const c = document.createElement('canvas'); c.width = c.height = 128; const g = c.getContext('2d'); g.drawImage(im, 0, 0)
        const d = g.getImageData(64, 64, 1, 1).data; return [d[0], d[1], d[2], src.length].join(',') })()"""
    # ①ドラッグで位置を決める
    a.eval(PICK % json.dumps('rb'))
    opened = a.wait_for("document.getElementById('cropDlg').open", timeout=SHORT)
    # v0.16.10：開いた瞬間のフォーカスは「決定」（canvas に当たると枠が角丸からはみ出して見えた）・canvas に内側の枠を描かない
    focus_ok = a.eval("document.activeElement && document.activeElement.id") == 'cropOk'
    no_ring = a.eval("(() => { const c = document.getElementById('cropCanvas'); c.focus(); const o = getComputedStyle(c).outlineStyle; document.getElementById('cropOk').focus(); return o })()") == 'none'
    rc = json.loads(a.eval("JSON.stringify(document.getElementById('cropCanvas').getBoundingClientRect())"))
    cx, cy = rc['left'] + rc['width'] / 2, rc['top'] + rc['height'] / 2
    a.call('Input.dispatchMouseEvent', type='mousePressed', x=cx, y=cy, button='left', clickCount=1)
    for k in range(1, 6):
        a.call('Input.dispatchMouseEvent', type='mouseMoved', x=cx + 80 * k, y=cy, button='left')
    a.call('Input.dispatchMouseEvent', type='mouseReleased', x=cx + 400, y=cy, button='left', clickCount=1)
    a.eval("(() => { const z = document.getElementById('cropZoom'); z.value = '3'; z.dispatchEvent(new Event('input')) })()")
    a.eval("document.getElementById('cropOk').click()")
    a.wait_for("!document.getElementById('cropDlg').open && !document.getElementById('ceAvatarImg').hidden", timeout=SHORT)
    px = a.eval(CENTER)
    r_, g_, b_, n = [int(x) for x in px.split(',')] if px != 'none' else [0, 0, 0, 0]
    red = r_ > 200 and b_ < 60 and 0 < n <= 40000
    # ②やめる＝変わらない
    before = a.eval("document.getElementById('ceAvatarImg').getAttribute('src')")
    a.eval(PICK % json.dumps('rb'))
    a.wait_for("document.getElementById('cropDlg').open", timeout=SHORT)
    a.eval("document.getElementById('cropCancel').click()")
    a.wait_for("!document.getElementById('cropDlg').open", timeout=SHORT)
    kept = a.eval("document.getElementById('ceAvatarImg').getAttribute('src')") == before
    # ③透明は白
    a.eval(PICK % json.dumps('clear'))
    a.wait_for("document.getElementById('cropDlg').open", timeout=SHORT)
    a.eval("document.getElementById('cropOk').click()")
    a.wait_for("!document.getElementById('cropDlg').open", timeout=SHORT)
    time.sleep(0.3)
    px2 = a.eval(CENTER)
    white = px2 != 'none' and all(int(x) > 230 for x in px2.split(',')[:3])
    # ④30MB 超は断る（切り抜きの窓を開かない）
    a.eval(PICK % json.dumps('big'))
    refused = a.wait_for("!document.getElementById('ceAvatarMsg').hidden && document.getElementById('ceAvatarMsg').textContent.includes('大きすぎます')", timeout=SHORT)
    no_crop = not a.eval("document.getElementById('cropDlg').open")
    a.eval("document.querySelectorAll('dialog[open]').forEach(d => d.close()); localStorage.removeItem('pot-call-card')")
    ok = bool(opened and focus_ok and no_ring and red and kept and white and refused and no_crop)
    r.check('T82', '名刺の画像：切り抜きの位置が反映・透明は白・JPEG で 4 万文字以内・やめるで変わらない・30MB 超は断る・枠のはみ出しなし', ok, 'pass',
            'フォーカスは決定=%s / canvas に内側の枠なし=%s / 窓=%s / ドラッグ位置（真ん中が赤）=%s(%s) / やめる=%s / 透明は白=%s(%s) / 大きすぎを断る=%s / 窓を開かない=%s'
            % (focus_ok, no_ring, bool(opened), red, px, kept, white, px2, bool(refused), no_crop))


def T83(r, room):
    """通話中に花が育ったら、相手のメンバー一覧の花も追従する（v0.16.9・利用者の指摘「メンバー一覧と部屋カードで育ち具合がずれる」）

    累積をあと 30 秒で 🌱 → 🌿 に育つ値にして入る（4 分 30 秒・参加 2 回 → 入室で 3 回）。プロフィールは入室・マイク・ミュートの
    ときしか送っていなかったので、修正前は相手の一覧だけ入室時の 🌱 のままだった（ロビーの在室は 5 秒ごとなので追従していた）。
    """
    name = room + 'GRW'
    a = r.open_tab()
    a.eval("localStorage.setItem('pot-call-hide-help','1'); localStorage.setItem('pot-call-stats', JSON.stringify({ seconds: 270, joins: 2, days: 1, lastDay: '' }))")
    a.call('Page.reload')
    a.wait_for(BOOTED, timeout=SHORT)
    a.eval("document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    b = r.open_tab()
    b.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(a, name); r.join(b, name)
    joined = a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    PEER_STAGE = "(() => { const row = [...document.querySelectorAll('#members .member')].find(el => !el.querySelector('.you')); const s = row && row.querySelector('.m-stage'); return s ? s.textContent : '' })()"
    first = b.eval(PEER_STAGE)
    grown = b.wait_for(PEER_STAGE + " === '🌿'", timeout=50)
    a_self = a.eval("(() => { const s = document.querySelector('#members .member .you')?.closest('.member')?.querySelector('.m-stage'); return s ? s.textContent : '' })()")
    ok = bool(joined and first == '🌱' and grown)
    r.check('T83', '通話中に花が育ったら、相手のメンバー一覧の花も追従する', ok, 'pass',
            '参加=%s / はじめ=%r / 30 秒後に相手の一覧で 🌿=%s / 本人の行=%r' % (bool(joined), first, bool(grown), a_self))
    a.eval("localStorage.removeItem('pot-call-stats')")
    click_leave(a); click_leave(b)


def T84(r, room):
    """大音量の相手だけを自動で抑える（v0.16.11・脅威モデル B8）

    B のマイクを最大振幅の矩形波に差し替えて入る（改造クライアントの爆音の代わり）。C はテスト用の標準のマイク音。
    見ている A では：B だけがリミッターの経路（via='limit'）に移り「小さくしました」の知らせが出る（文は v0.16.17 で短く）／C は素の再生のまま（via=''）。
    ★B は最初に入る（あとから入るタブはリンクで読み込み直すので、差し替えが消える）。
    """
    name = room + 'LOUD'
    a, b, c = r.open_tab(), r.open_tab(), r.open_tab()
    for t in (a, b, c):
        t.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    b.eval("""(() => { navigator.mediaDevices.getUserMedia = async () => {
        const ctx = new AudioContext(); await ctx.resume().catch(() => {})
        const o = ctx.createOscillator(); o.type = 'square'; o.frequency.value = 440
        const d = ctx.createMediaStreamDestination(); o.connect(d); o.start(); window.__loudCtx = ctx
        return d.stream } })()""")
    r.join(b, name)
    b.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    r.join(a, name); r.join(c, name)
    joined = a.wait_for('%s === 3' % MEMBERS, timeout=DISCOVER)
    # B と C の声が A に届いて解析に載るまで待つ
    a.wait_for("window.__potLoud().length === 2", timeout=DISCOVER)
    b_id = b.eval("window.__potSelfId ? window.__potSelfId() : ''")
    caught = a.wait_for("window.__potLoud().some(x => x.loud && x.via === 'limit')", timeout=20)
    time.sleep(6)   # ふつうの相手が誤って抑えられないかを、しばらく見る
    st = a.eval("JSON.stringify(window.__potLoud())")
    rows = json.loads(st)
    loud_n = sum(1 for x in rows if x['loud']); normal = [x for x in rows if not x['loud']]
    notice = a.eval("window.__potToasts().map(t => t.text).join(' / ')")
    # 知らせが画面で見えていること（文字が入っただけでなく）：上のカードが出ているか、6 秒で閉じたあとは
    # ⏱️（履歴）が黄色の数字で残っている（v0.16.28 で知らせを上からのカードに）・抑えている相手の行に 🔉
    visible = a.eval("""(() => { const c = document.querySelector('#toasts .toast:not(.off)'); if (c && c.getBoundingClientRect().height > 0) return true
      const h = document.getElementById('toastHist'); return !h.hidden && h.classList.contains('active') && h.getBoundingClientRect().height > 0 })()""")
    badge = a.eval("[...document.querySelectorAll('#members .member')].filter(el => el.querySelector('.m-loud')).length")
    # 素の再生（<audio>）は、抑えた相手のぶんだけ止まり、ふつうの相手は鳴ったまま
    els = json.loads(a.eval("JSON.stringify([...document.querySelectorAll('audio:not(#appAudio)')].filter(e => e.srcObject).map(e => e.muted))"))
    ok = bool(joined and caught and loud_n == 1 and len(normal) == 1 and normal[0]['via'] == '' and '小さくしました' in notice and sorted(els) == [False, True] and visible and badge == 1)
    r.check('T84', '大音量の相手だけをリミッターに通して知らせる・ふつうの相手は素の再生のまま', ok, 'pass',
            '3人=%s / 検知=%s / 状態=%s / 素の再生の muted=%s / 知らせ=%r 見える=%s / 🔉の行=%s' % (bool(joined), bool(caught), st, els, notice[:40], visible, badge))
    for t in (a, b, c):
        click_leave(t)


def T87(r):
    """配信鍵の引っ越し（v0.16.23・監査 S1）：v0.16.22 までの localStorage の鍵（JWK）が、起動時に
    取り出せない鍵として IndexedDB へ移り、localStorage から消える。**指紋は変わらない**＝前に配った
    配信リンクで、今まで通り配信者として入れる（同じ部屋名で作り直しても同じリンクになる）。
    """
    a = r.open_tab()
    a.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    a.eval("window.__potBcErase()")
    # 旧版と同じ作り方で鍵を置く（取り出せる鍵・JWK・raw の公開鍵・指紋＝SHA-256 の先頭 16 バイト）
    fp = a.eval(r"""(async () => {
      const b64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
      const kp = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, true, ['sign', 'verify'])
      const jwk = await crypto.subtle.exportKey('jwk', kp.privateKey)
      const raw = await crypto.subtle.exportKey('raw', kp.publicKey)
      const pub = b64u(raw), fp = b64u((await crypto.subtle.digest('SHA-256', raw)).slice(0, 16))
      localStorage.setItem('pot-call-bcast', JSON.stringify({ [fp]: { name: '引っ越しテスト', jwk, pub, at: Date.now() } }))
      return fp
    })()""")
    a.call('Page.reload'); time.sleep(0.5)
    booted = a.wait_for(BOOTED, timeout=SHORT)
    listed = a.wait_for("window.__potBcList().some(k => k.fp === %s && k.name === '引っ越しテスト')" % js_str(fp), timeout=SHORT)
    ls_gone = a.eval("localStorage.getItem('pot-call-bcast') === null")
    # 同じ名前で配信部屋を選ぶと、同じ指紋のリンクになる（新しい鍵を作らない）
    a.eval("(() => { document.getElementById('createBox').open = true; const i = document.getElementById('room');"
           " i.value = '引っ越しテスト'; i.dispatchEvent(new Event('input')); document.getElementById('kindBc').click() })()")
    same = a.wait_for("document.getElementById('fbUrl').href.includes('~pk~' + %s)" % js_str(fp), timeout=SHORT)
    a.eval("window.__potBcErase()")
    ok = booted and listed and ls_gone and same
    r.check('T87', '配信鍵の引っ越し：localStorage の旧い鍵が IndexedDB へ移り、指紋（配信リンク）は変わらない', ok, 'pass',
            '起動=%s / 移った=%s / 旧い置き場から消えた=%s / 同じリンク=%s' % (booted, listed, ls_gone, same))


def T90(r, room):
    """入室後の招待リンク・QR は部屋名を載せない（短くする）／部屋名を後から変えられる（v0.16.26 試作・利用者の提案）。
    ・雑談部屋：A が入ると招待リンクから &name= が消え、アドレスバーには残る。B は名前なしのリンクで来て、名前はロビーから引く。
      A が名前を変える → B の見出し・アドレスバー・ひとこと（「部屋名を…に変えました」）に届く。
      C は**旧名つきの古いリンク**で来る → 入ると新しい名前になる（nameAt が新しいほうが勝つ）
    ・配信部屋：聞き役には変えるボタンが無い。配信者が変えると聞き役に届く
    """
    hide_help = "localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())"
    head_has = lambda n: "document.getElementById('state').textContent.includes(%s)" % js_str(n)
    rename = lambda t, n: t.eval("(() => { const b = document.querySelector('#state .rn-edit'); if (!b) return false; b.click();"
                                 " document.getElementById('nameInput').value = %s; document.getElementById('nameSend').click(); return true })()" % js_str(n))
    name0, name1 = room + 'RN', room + '改'
    a = r.open_tab(); a.eval(hide_help)
    r.join(a, name0)
    joined = bool(a.wait_for("!!document.querySelector('#state .rn-edit')", timeout=DISCOVER))
    share = a.eval("document.getElementById('fbUrl').href") or ''
    old = a.eval('location.hash') or ''
    short = '#room=' in share and '&name=' not in share and '&name=' in old
    b = r.open_tab(hash_=share[share.find('#'):] if '#' in share else '')
    b.eval(hide_help)
    from_lobby = bool(b.wait_for("document.getElementById('room').value === %s" % js_str(name0), timeout=DISCOVER))
    click_join(b)
    both = bool(a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER))
    did = bool(rename(a, name1))
    a_ok = bool(a.wait_for(head_has(name1) + " && decodeURIComponent(location.hash).includes(%s)" % js_str('&name=' + name1), timeout=SHORT))
    b_ok = bool(b.wait_for(head_has(name1) + " && decodeURIComponent(location.hash).includes(%s)" % js_str('&name=' + name1)
                           + " && document.getElementById('chatLog').textContent.includes(%s)" % js_str('部屋名を「' + name1 + '」に変えました'), timeout=SHORT))
    c = r.open_tab(hash_=old); c.eval(hide_help)
    c.wait_for("document.getElementById('room').value === %s" % js_str(name0), timeout=SHORT)
    click_join(c)
    c_ok = bool(c.wait_for('%s === 3' % MEMBERS, timeout=DISCOVER) and c.wait_for(head_has(name1), timeout=SHORT))
    for t in (a, b, c):
        click_leave(t)
    # 配信部屋
    o = r.open_tab(); o.eval(hide_help); o.eval("document.getElementById('createBox').open = true")
    made = make_bcast(r, o, room + 'RB')
    o_in = bool(o.wait_for("location.hash.includes('~pk~') && !!document.querySelector('#state .rn-edit')", timeout=DISCOVER))
    l = r.open_tab(hash_=o.eval('location.hash')); l.eval(hide_help); l.eval("window.__potBcErase(true)")
    click_join(l)
    l_in = bool(l.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER))
    l_cant = l_in and not l.eval("!!document.querySelector('#state .rn-edit')")
    o_did = bool(rename(o, room + '配信改'))
    l_ok = bool(l.wait_for(head_has(room + '配信改'), timeout=SHORT))
    ok = joined and short and from_lobby and both and did and a_ok and b_ok and c_ok and made and o_in and l_cant and o_did and l_ok
    r.check('T90', '入室後の招待リンクは部屋名なし／部屋名を後から変えられる（雑談は誰でも・配信は配信者だけ）', ok, 'pass',
            '参加=%s / リンクに名前なし=%s / 名前はロビーから=%s / 2人=%s / 変えた=%s / A=%s / B=%s / 古いリンクの C=%s / '
            '配信部屋=%s・%s / 聞き役は変えられない=%s / 配信者が変えた=%s / 聞き役に届く=%s'
            % (joined, short, from_lobby, both, did, a_ok, b_ok, c_ok, made, o_in, l_cant, o_did, l_ok))


def T89(r, room):
    """配信部屋のおたよりの入力欄が、キーボードが出ても盤やタブの裏に隠れない（2026-11-07 β 実機）。
    話題タグの欄と同じ位置合わせ（keepFieldVisible）。おたよりの欄はタブの下にあるので、上の境目は貼り付いたタブの下端。
    キーボードは画面の高さを縮めて代用する（700 → 400）。ページの上端から・下端からの両方。
    """
    phone = lambda t, h: t.call('Emulation.setDeviceMetricsOverride', width=390, height=h, deviceScaleFactor=2, mobile=True)
    hide_help = "localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())"

    def field_seen(t):
        """おたよりタブを開き、ページの上端から・下端からの 2 通りで欄にフォーカス→キーボード相当に縮める"""
        res = []
        for start in ('window.scrollTo(0, 0)', 'window.scrollTo(0, document.body.scrollHeight)'):
            phone(t, 700); time.sleep(0.4)
            t.eval("document.activeElement && document.activeElement.blur()"); time.sleep(0.3)
            t.eval("document.getElementById('tabMail').click()"); time.sleep(0.5)
            t.eval(start); time.sleep(0.4)
            t.call('Page.bringToFront')
            t.eval("document.getElementById('mailText').focus()")
            phone(t, 400)
            res.append(bool(t.wait_for("""(() => {
              const f = document.getElementById('mailForm').getBoundingClientRect()
              // 入力中は盤ごと畳む（typing-field）ので、下の境目は見えている画面の下端
              const vv = window.visualViewport, dock = document.getElementById('dock')
              return !document.getElementById('mailForm').hidden && f.top >= document.getElementById('tabs').getBoundingClientRect().bottom
                && document.body.classList.contains('typing-field') && !dock.getClientRects().length
                && f.bottom <= vv.offsetTop + vv.height
            })()""", timeout=4)))
        t.eval("document.activeElement && document.activeElement.blur()")
        # 入力欄から出たら盤が戻る
        res[-1] = res[-1] and bool(t.wait_for("!document.body.classList.contains('typing-field') && document.getElementById('dock').getClientRects().length > 0", timeout=3))
        phone(t, 700)
        return res

    # 配信者：お題を書く欄
    a = r.open_tab(); phone(a, 700)
    a.eval(hide_help); a.eval("document.getElementById('createBox').open = true")
    made = make_bcast(r, a, room + 'MKB')
    joined = a.wait_for("location.hash.includes('~pk~') && !document.getElementById('tabMail').hidden", timeout=DISCOVER)
    owner = field_seen(a) if joined else [False, False]
    # リスナー：配信者が募集を始めたあと、投稿を書く欄
    a.eval("document.getElementById('tabMail').click()")
    a.eval("(() => { const t = document.getElementById('mailText'); t.value = '最近の楽しみ'; document.getElementById('mailSend').click() })()")
    b = r.open_tab(hash_=a.eval('location.hash')); phone(b, 700)
    b.eval(hide_help); b.eval("window.__potBcErase(true)")   # ★同じブラウザなので鍵を消して聞き役にする
    click_join(b)
    heard = (b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
             and b.wait_for("document.getElementById('mailHead').textContent.includes('最近の楽しみ')", timeout=SHORT))
    listener = field_seen(b) if heard else [False, False]
    for t in (a, b):
        t.call('Emulation.clearDeviceMetricsOverride')
    ok = made and joined and heard and all(owner) and all(listener)
    r.check('T89', '配信部屋のおたよりの入力欄が、キーボードが出ても盤やタブの裏に隠れない・入力中は盤を畳む（配信者・リスナー）', ok, 'pass',
            '配信部屋=%s / 参加=%s / 配信者:上端から=%s・下端から=%s / 募集が届いた=%s / リスナー:上端から=%s・下端から=%s'
            % (made, joined, owner[0], owner[1], heard, listener[0], listener[1]))


def dock_drag(t, dy):
    """盤の引き手を本物のマウス操作で払う（dy>0 で下へ）。払ったあとの click で開閉が戻らないことも一緒に見る"""
    x, y = t.eval("(() => { const b = document.getElementById('dockGrip').getBoundingClientRect(); return [b.x + b.width / 2, b.y + b.height / 2] })()")
    t.call('Input.dispatchMouseEvent', type='mousePressed', x=x, y=y, button='left', clickCount=1)
    for k in (1, 2, 3):
        t.call('Input.dispatchMouseEvent', type='mouseMoved', x=x, y=y + dy * k / 3, button='left')
    t.call('Input.dispatchMouseEvent', type='mouseReleased', x=x, y=y + dy, button='left', clickCount=1)
    time.sleep(0.4)
    return t.eval("document.getElementById('dock').classList.contains('open')")


def T91(r, room):
    """盤の開閉（2026-11-10 レビュー）。入室したら一度見せてから引っ込める・キーボードで触ったら引っ込めない。
    払う向きは引き手の矢印と同じ：PC で盤がカードの底板のまま下に余地があれば下へ払うと開く。画面の低いノートで盤が画面の下端に
    貼り付いているときと、スマホ（下に固定）は上へ伸びるので上へ。
    PC で矢印だけ反転して払う向きが逆のままだった。払ったあとの click で開閉が戻らないことも見る"""
    a = r.open_tab()
    a.call('Emulation.setDeviceMetricsOverride', width=1000, height=900, deviceScaleFactor=1, mobile=False)
    a.eval("localStorage.setItem('pot-call-hide-help','1'); localStorage.removeItem('pot-dock-open'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(a, room + 'DOCK')
    joined = a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    OPEN = "document.getElementById('dock').classList.contains('open')"
    peek = bool(a.wait_for(OPEN, timeout=3))
    a.call('Page.bringToFront')   # 手前のタブでないと focus() で focusin が来ない
    focused = a.eval("(() => { const b = document.querySelector('#dock .reactions button'); if (!b) return 'no button'; b.focus(); return document.activeElement === b })()")   # キーボードで触った（focusin）
    time.sleep(3.5)
    kept = a.eval(OPEN) is True
    a.eval("document.activeElement.blur(); document.getElementById('dockGrip').click()")   # 押して閉じる
    time.sleep(0.4)
    tap = a.eval(OPEN) is False
    DOWN = "document.getElementById('dock').classList.contains('down')"
    a.eval("window.scrollTo(0, 0)"); time.sleep(0.3)
    tall_down = a.eval(DOWN) is True                 # 下に余地がある＝下へ伸びる
    pc = [dock_drag(a, 60), dock_drag(a, -60)]       # 下で開く・上で閉じる
    # 画面の低いノート：盤が画面の下端に貼り付く＝上へ伸びる（2026-11-10 利用者）
    a.call('Emulation.setDeviceMetricsOverride', width=1000, height=420, deviceScaleFactor=1, mobile=False)
    time.sleep(0.6)
    short_up = a.eval(DOWN) is False
    short = [dock_drag(a, -60), dock_drag(a, 60)]    # 上で開く・下で閉じる
    a.call('Emulation.setDeviceMetricsOverride', width=390, height=844, deviceScaleFactor=2, mobile=True)
    time.sleep(0.5)
    phone = [dock_drag(a, -60), dock_drag(a, 60)]    # 上で開く・下で閉じる
    a.call('Emulation.clearDeviceMetricsOverride')
    a.call('Page.navigate', url='about:blank')       # 部屋とロビーから抜ける（残すと後ろの 2 タブのテストの相手探しが重くなる）
    ok = joined and peek and kept and tap and tall_down and pc == [True, False] and short_up and short == [True, False] and phone == [True, False]
    r.check('T91', '盤の開閉（入室で見せる・キーボードで止まる・払う向きが矢印と同じ）', ok, 'pass',
            '参加=%s / 入室で見せる=%s / フォーカス=%s / キーボードで触ると残る=%s / 押して閉じる=%s / PC 高い窓は下向き=%s・下・上=%s / 低い窓は上向き=%s・上・下=%s / スマホ 上・下=%s'
            % (joined, peek, focused, kept, tap, tall_down, pc, short_up, short, phone))


def T92(r, room):
    """スマホの「通話中の部屋」ドロワーと戻るボタン（2026-11-10 レビュー）。開くと履歴を 1 つ積み、
    × で閉じると戻して消す。開いたまま退出（配信の終了・定員超過の押し出しと同じ後片付け）しても積んだぶんを戻す。
    以前は URL の書き換えで目印ごと消え、戻るを押すと退出した部屋の URL に戻っていた"""
    a = r.open_tab()
    a.call('Emulation.setDeviceMetricsOverride', width=390, height=844, deviceScaleFactor=2, mobile=True)
    a.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(a, room + 'DRW')
    joined = a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    ST = "!!(history.state && history.state.potLobby)"
    a.eval("document.getElementById('lobbyTab').click()")
    pushed = a.wait_for("document.body.classList.contains('lobby-open') && " + ST, timeout=SHORT)
    a.eval("document.getElementById('lobbyClose').click()")
    closed = a.wait_for("!document.body.classList.contains('lobby-open') && !" + ST, timeout=SHORT)
    stay = a.eval("location.hash.startsWith('#room=') && document.body.classList.contains('in-call')")
    a.eval("document.getElementById('lobbyTab').click()")
    again = a.wait_for(ST, timeout=SHORT)
    a.eval("document.getElementById('leave').click(); document.getElementById('leaveYes') && document.getElementById('leaveYes').click()")
    left = a.wait_for("!document.body.classList.contains('in-call') && !document.body.classList.contains('lobby-open')", timeout=SHORT)
    time.sleep(0.6)
    clean = a.eval("location.hash === '' && !" + ST)
    a.eval("history.back()"); time.sleep(0.6)
    back = a.eval("location.hash")
    a.call('Emulation.clearDeviceMetricsOverride')
    a.call('Page.navigate', url='about:blank')       # ロビーから抜ける（T91 と同じ）
    ok = joined and pushed and closed and stay and again and left and clean and not back.startswith('#room=')
    r.check('T92', 'ドロワーの履歴（× で戻す・開いたまま退出しても戻るが部屋の URL に戻らない）', ok, 'pass',
            '参加=%s / 開くと積む=%s / × で戻す=%s / 通話のまま=%s / 再び開く=%s / 退出=%s / 退出後 hash と目印なし=%s / 戻った先=%r'
            % (joined, pushed, closed, stay, again, left, clean, back))


def T93(r, room):
    """ロビーのカードは、見た目が変わっていなければ作り直さない（v0.16.27・利用者 2026-11-12）。
    在室の合図のたびに一覧を全部入れ替えていたので、カード右上の共有ボタンが毎秒 1〜2 回別の要素になり、
    色がちらつく・✓ がすぐ消える・押しても反応しないことがあった。
    ①何も変わらない 6 秒のあいだ、共有ボタンが同じ要素のまま ②押すと ✓ が 1 秒後も残り、そのあと戻る
    ③人が増えたら（見た目が変わったら）カードの中身は新しくなる
    """
    x = room + 'SHR'
    a = r.open_tab(); r.join(a, x)
    assert a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), 'A が参加できない'
    c = r.open_tab()
    c.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    CARD = "[...document.querySelectorAll('#lobbyList .room')].find(b => b.textContent.includes(%s))" % js_str(x)
    assert c.wait_for("!!" + CARD, timeout=DISCOVER), 'ロビーに出ない'
    time.sleep(2)
    c.eval("window.__sh = %s.querySelector('.room-share')" % CARD)
    time.sleep(6)   # 在室の合図（描き直し）を何度かまたぐ
    same = c.eval("window.__sh.isConnected && window.__sh === %s.querySelector('.room-share')" % CARD)
    n = c.eval("__T.clip.length")
    c.eval("window.__sh.click()", await_promise=False)
    time.sleep(1.0)
    copied = c.eval("__T.clip.length") == n + 1
    check = c.eval("%s.querySelector('.room-share').classList.contains('copied')" % CARD)
    back = c.wait_for("!%s.querySelector('.room-share').classList.contains('copied')" % CARD, timeout=SHORT)
    b = r.open_tab(); r.join(b, x)   # 同じ名前の部屋を探して入る
    grew = c.wait_for("(%s.querySelector('.room-count') || {}).textContent === '2人'" % CARD, timeout=DISCOVER)
    ok = same and copied and check and back and grew
    r.check('T93', 'ロビーのカードは変わらなければ作り直さない（共有ボタンが同じ要素のまま・✓ が残る・人が増えれば中身は変わる）', ok, 'pass',
            '6 秒後も同じ要素=%s / コピーした=%s / 1 秒後も ✓=%s / そのあと戻る=%s / 2人に変わる=%s' % (same, copied, check, back, grew))
    for t in (a, b):
        click_leave(t)
    c.call('Page.navigate', url='about:blank')


def T94(r, room):
    """メンバー一覧の先頭の行の吹き出しが、上のタブ（貼り付く #tabs）に隠れない（v0.16.27・利用者 2026-11-12）。
    吹き出しは行の上に出るが、タブの行は z-index が上なので先頭の行の吹き出しはタブの裏に潜っていた。
    ①先頭の行（自分）の吹き出しはタブの下端より下・自分の行に被さる（上端が自分の行の中） ②2 行目（相手）の吹き出しは今までどおり行の上
    """
    x = room + 'BUB'
    a = r.open_tab(); r.join(a, x)
    assert a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), 'A が参加できない'
    b = r.open_tab(); r.join(b, x)
    two = a.wait_for("document.querySelectorAll('#members .member').length >= 2", timeout=DISCOVER)
    a.eval("document.getElementById('tabMembers').click(); window.scrollTo(0, 0)")
    SAY = "(() => { const t = document.getElementById('chatText'); t.value = %s; document.getElementById('chatSend').click() })()"
    a.eval(SAY % js_str('先頭'))
    BUB = "[...document.querySelectorAll('#members .chat-bubble')].find(e => e.textContent === %s)"
    TOP = "(() => { const e = %s; if (!e) return false; const b = e.getBoundingClientRect(), row = e.parentElement.getBoundingClientRect(); return b.top >= document.getElementById('tabs').getBoundingClientRect().bottom && b.top < row.bottom })()"
    first = a.wait_for(TOP % (BUB % js_str('先頭')), timeout=SHORT)
    b.eval(SAY % js_str('二番目'))
    shown = a.wait_for("!!" + BUB % js_str('二番目'), timeout=DISCOVER)
    above = a.eval("(() => { const e = %s; return !!e && !e.classList.contains('over') && e.getBoundingClientRect().bottom <= e.parentElement.getBoundingClientRect().top + 1 })()" % (BUB % js_str('二番目')))
    ok = two and first and shown and above
    r.check('T94', '先頭の行の吹き出しはタブに隠れない（2 行目は今までどおり行の上）', ok, 'pass',
            '2 人=%s / 先頭の吹き出しがタブより下で自分の行に被さる=%s / 2 行目に届いた=%s / 2 行目は行の上=%s' % (two, first, shown, above))
    for t in (a, b):
        click_leave(t)


def T95(r, room):
    """入室の行（v0.16.27・利用者 2026-11-13）：雑談部屋でも、後から入った人は「〇〇さんが入室しました」とひとことに出る。
    自分の入室も、参加した時点（一人目でも）に自分の画面に出る（配信部屋も同じ。配信の聞き役は T27i）。
    ①一人目の A に自分の行が出る ②A に B の入室が出る（B の名前・1 行だけ・ミュートの切り替えで届き直しても増えない）
    ③B には自分の行だけ（A は先にいた人なので出ない）
    """
    x = room + 'ENT2'
    SYS = "[...document.querySelectorAll('#chatLog .cl-sys .cl-text')].filter(e => e.textContent.endsWith('さんが入室しました')).map(e => e.textContent)"
    NAME = "(JSON.parse(localStorage.getItem('pot-call-profile')||'{}').name)||'ゲスト'"
    a = r.open_tab(); r.join(a, x)
    assert a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), 'A が参加できない'
    aname = a.eval(NAME)
    solo = a.eval(SYS) == [aname + ' さんが入室しました']
    time.sleep(5)
    b = r.open_tab(); r.join(b, x)
    bname = b.eval(NAME)
    got = a.wait_for(SYS + ".includes(%s + ' さんが入室しました')" % js_str(bname), timeout=DISCOVER + 5)
    b.eval("document.getElementById('mute').click()"); time.sleep(1.5)
    b.eval("document.getElementById('mute').click()"); time.sleep(2)
    alog, blog = a.eval(SYS), b.eval(SYS)
    once = alog == [aname + ' さんが入室しました', bname + ' さんが入室しました']
    bonly = blog == [bname + ' さんが入室しました']
    ok = solo and got and once and bonly
    r.check('T95', '入室の行：自分の入室は一人目でも出る・雑談部屋でも後から来た人の入室が出る（先にいた人の分は出ない・増えない）', ok, 'pass',
            'A に自分の行=%s / A に B の入室=%s / A のひとこと=%s / B のひとこと=%s' % (solo, got, alog, blog))
    for t in (a, b):
        click_leave(t)


def T96(r, room):
    """知らせは画面の上から重ねて出すカード（v0.16.28・利用者 2026-11-13）。
    ①重ねて出る（上書きで消えない）・同時に開くのは 2 枚まで ②閉じた知らせは ⏱️（履歴）に残り、押すと開き直す
    ③片づいていない知らせ（keep）があると ⏱️ が黄色 ④× は履歴からも消す ⑤上へ払うと閉じる ⑥退出で全部消える
    ⑦カードはページの高さを変えない（重ねて出す）
    """
    a = r.open_tab(); r.join(a, room + 'NTC')
    assert a.wait_for("document.body.classList.contains('in-call')", timeout=DISCOVER), '参加できない'
    time.sleep(1)
    T = "window.__potToasts()"
    h0 = a.eval("document.documentElement.scrollHeight")
    a.eval("window.__potNotify('🔉 大きい音', { key: 'loud:x', keep: true, warn: true }); window.__potNotify('🔌 切れた'); window.__potNotify('🎙 許可')")
    time.sleep(0.6)
    st = json.loads(a.eval("JSON.stringify(%s)" % T))
    three = len(st) == 3
    open2 = sorted(x['text'] for x in st if x['open']) == ['🎙 許可', '🔌 切れた']
    hist = a.eval("(() => { const h = document.getElementById('toastHist'); return !h.hidden && h.classList.contains('active') && document.getElementById('toastHistN').textContent === '1' })()")
    flat = a.eval("document.documentElement.scrollHeight") == h0
    a.eval("document.getElementById('toastHist').click()"); time.sleep(0.6)
    reopened = a.eval("%s.every(x => x.open)" % T)
    a.eval("[...document.querySelectorAll('#toasts .toast')].find(c => c.textContent.includes('切れた')).querySelector('.toast-x').click()"); time.sleep(0.5)
    xed = a.eval("!%s.some(x => x.text === '🔌 切れた')" % T)
    # 上へ払う（ポインタの合成イベント）
    a.eval("""(() => { const c = [...document.querySelectorAll('#toasts .toast')].find(c => c.textContent.includes('許可'))
      const r = c.getBoundingClientRect(), x = r.left + 30, y = r.top + 20, o = { bubbles: true, pointerId: 7, clientX: x }
      c.dispatchEvent(new PointerEvent('pointerdown', { ...o, clientY: y })); c.dispatchEvent(new PointerEvent('pointermove', { ...o, clientY: y - 50 }))
      c.dispatchEvent(new PointerEvent('pointerup', { ...o, clientY: y - 50 })) })()"""); time.sleep(0.5)
    swiped = a.eval("%s.some(x => x.text === '🎙 許可' && !x.open)" % T)
    click_leave(a); time.sleep(0.5)
    gone = a.eval("%s.length === 0 && document.getElementById('toastHist').hidden && !document.querySelector('#toasts .toast')" % T)
    ok = three and open2 and hist and flat and reopened and xed and swiped and gone
    r.check('T96', '知らせは上からのカード（重ねる・2 枚まで・⏱️ に残る・× で消える・払うと閉じる・退出で消える・ページを伸ばさない）', ok, 'pass',
            '3 件=%s / 開くのは新しい 2 枚=%s / ⏱️ 黄色 1=%s / 高さ不変=%s / ⏱️ で開き直す=%s / × で消える=%s / 払うと閉じる=%s / 退出で消える=%s'
            % (three, open2, hist, flat, reopened, xed, swiped, gone))


def T97(r, room):
    """状態から出す知らせ（人数・配信部屋の説明・音が届かない・v0.16.29）は**中身が変わったときだけ**出し直す。
    ①同じ中身で文だけ変わる（配信終了の秒読み）は同じカードのまま書き換わる ②× で消したら同じ中身の間は出ない
    ③中身が変われば出る ④当てはまらなくなったら消える ⑤配信部屋の説明・音が届かないの「つなぎ直す」がカードに出る
    """
    a = r.open_tab(); r.join(a, room + 'NST')
    assert a.wait_for("document.body.classList.contains('in-call')", timeout=DISCOVER), '参加できない'
    time.sleep(1)
    T = "window.__potToasts()"
    one = "(%s.find(x => x.key === 't') || {})" % T
    a.eval("window.__potNotifyState('t', '秒読み 9', { sig: 'cd', keep: true, warn: true })"); time.sleep(0.5)
    a.eval("window.__c0 = document.querySelector('#toasts .toast')")
    a.eval("window.__potNotifyState('t', '秒読み 8', { sig: 'cd', keep: true, warn: true })"); time.sleep(0.2)
    inplace = a.eval("document.querySelector('#toasts .toast') === window.__c0 && window.__c0.textContent.includes('秒読み 8') && %s.text === '秒読み 8'" % one)
    a.eval("document.querySelector('#toasts .toast .toast-x').click()"); time.sleep(0.4)
    a.eval("window.__potNotifyState('t', '秒読み 7', { sig: 'cd' })"); time.sleep(0.3)
    stays = a.eval("!%s.some(x => x.key === 't')" % T)
    a.eval("window.__potNotifyState('t', '終わりました', { sig: 'end' })"); time.sleep(0.4)
    back = a.eval("%s.text === '終わりました' && %s.open" % (one, one))
    a.eval("window.__potNotifyState('t', '')"); time.sleep(0.4)
    gone = a.eval("!%s.some(x => x.key === 't')" % T)
    click_leave(a)
    # 配信部屋：配信者には「あなたの配信」のカード
    o = r.open_tab()
    made = make_bcast(r, o, room + 'NSB')
    bc = o.wait_for("%s.includes('あなたの配信')" % BC_NOTE, timeout=DISCOVER)
    click_leave(o)
    ok = inplace and stays and back and gone and made and bc
    r.check('T97', '状態の知らせは中身が変わったときだけ出す（秒読みは同じカードで書き換え・× の後は同じ中身なら出ない・配信の説明もカード）', ok, 'pass',
            '同じカードで書き換え=%s / × の後は出ない=%s / 中身が変われば出る=%s / 当てはまらなければ消える=%s / 配信の説明=%s'
            % (inplace, stays, back, gone, bool(made and bc)))


def T88(r, room):
    """主タブで「ひとこと」を開くと、最新の行が見える位置へ送る（2026-11-07 β 実機）。
    ひとことの欄は下詰めでスクロールしないので、最新の行は欄の下端にある。スマホで縦が狭いと
    （上段の話題が 2 行・下にアドレスバー）欄の下端が盤の裏に隠れ、開いても空に見えていた。
    判定：最後の行（無ければ欄）の下端が「貼り付いたタブの下〜盤の上」に入ること。ページの上端から・下端からの両方。
    """
    a = r.open_tab()
    a.call('Emulation.setDeviceMetricsOverride', width=390, height=640, deviceScaleFactor=2, mobile=True)
    # 初回に自動で開く助言（モーダル）が開いているとページが動かない＝閉じておく
    a.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(a, room + 'LOG')
    joined = a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    a.eval("(() => { const t = document.getElementById('chatText'); t.value = '最新の行'; document.getElementById('chatSend').click() })()")
    posted = a.wait_for("document.getElementById('chatLog').textContent.includes('最新の行')", timeout=SHORT)   # 入室の行が先にある（v0.16.27）
    a.eval("document.activeElement && document.activeElement.blur()"); time.sleep(0.5)
    seen = []
    for start in ('window.scrollTo(0, 0)', 'window.scrollTo(0, document.body.scrollHeight)'):
        a.eval("document.getElementById('tabMembers').click()"); time.sleep(0.3)
        a.eval(start); time.sleep(0.4)
        a.eval("document.getElementById('tabChat').click()")
        seen.append(bool(a.wait_for("""(() => {
          const log = document.getElementById('chatLog'), tabs = document.getElementById('tabs')
          const last = (!log.hidden && log.lastElementChild) || document.getElementById('panChat')
          const b = last.getBoundingClientRect().bottom
          return b <= document.getElementById('dock').getBoundingClientRect().top && b >= tabs.getBoundingClientRect().bottom + 20
        })()""", timeout=3)))
    a.call('Emulation.clearDeviceMetricsOverride')
    ok = joined and posted and all(seen)
    r.check('T88', '主タブで「ひとこと」を開くと最新の行が見える（スマホの狭い縦・盤の裏に隠れない）', ok, 'pass',
            '参加=%s / 送れた=%s / 上端から=%s / 下端から=%s' % (joined, posted, seen[0], seen[1]))


def T86(r, room):
    """通話中の部屋の並び順（v0.16.15）：評価の関数と、自分の部屋が先頭に固定されること

    関数（window.__potLobbyRank）：人数・話している人・伸び・話題・🌱・満員・黙っている時間・開いてからの時間で点を付け、
    揺れ止め（差が 0.75 以下は入れ替えない・並べ替えの時刻でなければ出し入れだけ）と例外（自分の部屋が先頭・🌱 なら
    ようこそ・やまびこを一番の部屋のすぐ下へ）を当てる。画面：ほかに 2 人の部屋があっても、ひとりでいる自分の部屋が一覧の先頭。
    """
    a = r.open_tab()
    a.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    js = r"""(() => {
      const R = window.__potLobbyRank, F = o => Object.assign({ n: 1, talkers: 0, mean: 0, silentMs: 0, grow: 0, tagged: false, sprout: false, full: false, ageSec: 600 }, o)
      const lively = R.score(F({ n: 3, talkers: 2, mean: 1.5 })), lone = R.score(F({ n: 1 }))
      const full = R.score(F({ n: 8, talkers: 2, mean: 1, full: true }))
      const quiet = R.score(F({ n: 3, silentMs: 15 * 60000 })), loud3 = R.score(F({ n: 3, talkers: 1, mean: 1 }))
      const old = R.score(F({ n: 3, ageSec: 6 * 3600 })), young = R.score(F({ n: 3 }))
      const extras = R.score(F({ n: 2, grow: 2, tagged: true, sprout: true })) - R.score(F({ n: 2 }))
      const keep = R.base(['a', 'b'], { a: 3, b: 3.5 }, true).join()
      const swap = R.base(['a', 'b'], { a: 3, b: 4 }, true).join()
      const hold = R.base(['a', 'b'], { a: 1, b: 9, c: 5 }, false).join()
      const pin = R.apply(['x', 'y', 'w', 'e'], 'y', ['w', 'e']).join()
      const pin2 = R.apply(['x', 'y'], '', ['w', 'e']).join()
      return JSON.stringify({ lively: lively > lone, fullDown: full < loud3, quietDown: quiet < loud3 - 7, oldDown: Math.abs((young - old) - 2) < 1e-9,
        extras: Math.abs(extras - 3.5) < 1e-9, keep, swap, hold, pin, pin2 })
    })()"""
    res = json.loads(a.eval(js))
    fn_ok = (res['lively'] and res['fullDown'] and res['quietDown'] and res['oldDown'] and res['extras']
             and res['keep'] == 'a,b' and res['swap'] == 'b,a' and res['hold'] == 'c,a,b'
             and res['pin'] == 'y,x,w,e' and res['pin2'] == 'x,y')
    # 画面：B・C が 2 人の部屋、A はひとりで別の部屋。A の一覧の先頭は自分の部屋（.here）
    b = r.open_tab(); c = r.open_tab()
    for t in (b, c):
        t.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(b, room + 'RK2')
    link = b.wait_for("location.hash.startsWith('#room=')", timeout=DISCOVER) and b.eval('location.hash')
    c2 = r.open_tab(hash_=link) if link else c
    c2.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    click_join(c2)
    r.join(a, room + 'RK1')
    rooms2 = a.wait_for("document.querySelectorAll('#lobbyList button.room').length >= 2", timeout=DISCOVER + 20)
    first_here = a.wait_for("(document.querySelector('#lobbyList button.room') || {}).classList?.contains('here')", timeout=SHORT)
    ok = bool(fn_ok and rooms2 and first_here)
    r.check('T86', '通話中の部屋の並び順：評価の関数（人数・賑やかさ・満員・黙っている・時間・揺れ止め・例外）と、自分の部屋が先頭', ok, 'pass',
            '関数=%s %s / 2部屋見えた=%s / 自分の部屋が先頭=%s' % (fn_ok, res, bool(rooms2), bool(first_here)))
    for t in (a, b, c2):
        try: click_leave(t)
        except Exception: pass


def T85(r, room):
    """β 限定の大音量テスト（?loudtest=1・v0.16.12）と、見張りの数値

    B を ?loudtest=1 で開くと、マイクの代わりに最大振幅の矩形波（2 秒鳴らして 3 秒休む）を送る。版の表示に 🧪。
    A では B が抑えられ（via='limit'）、届いた音の最大（rms）が記録され、診断の窓（版の長押し）に「届いた音の最大」が出る。
    """
    name = room + 'LT'
    b = r.open_tab(hash_='?loudtest=1')
    a = r.open_tab()
    for t in (a, b):
        t.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    badge = '🧪' in b.eval("document.getElementById('ver').textContent")
    r.join(b, name)
    b.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    r.join(a, name)
    joined = a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    caught = a.wait_for("window.__potLoud().some(x => x.loud && x.via === 'limit')", timeout=20)
    lv = json.loads(a.eval("JSON.stringify(window.__potLoud())"))
    rms = max([x['rms'] for x in lv] or [0])
    a.eval("document.getElementById('ver').dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX: 1, clientY: 1 }))")
    shown = a.wait_for("!document.getElementById('diag').hidden && document.getElementById('diag').textContent.includes('届いた音の最大')", timeout=8)
    diag = a.eval("document.getElementById('diag').textContent")
    ok = bool(badge and joined and caught and rms >= 0.5 and shown)
    r.check('T85', 'β の ?loudtest=1 で大きな音を送ると受け手が抑える・届いた音の最大が診断の窓に出る', ok, 'pass',
            '🧪=%s / 2人=%s / 抑えた=%s / 最大rms=%.2f / 診断=%r' % (badge, bool(joined), bool(caught), rms, diag[-90:] if shown else ''))
    click_leave(a); click_leave(b)


def T73(r):
    """プロフィールカード編集＆認証チャレンジUI（v0.16.0）：
    編集モーダル（#cardEditDlg）展開、SNS入力で認証ボタン出現、
    認証モーダル（#cardAuthDlg）展開でワンタイムコード提示、
    認証済みバッジのタップによる解除機能が正しく動作すること。
    """
    a = r.open_tab()
    # プロフィール編集を開く
    a.eval("document.getElementById('btnOpenCardEdit').click()")
    dlg_open = a.wait_for("document.getElementById('cardEditDlg').open", timeout=SHORT)

    # GitHub アカウントを入力
    a.eval("""(() => {
        const inp = document.getElementById('ceSns_github');
        inp.value = 'octocat';
        inp.dispatchEvent(new Event('input'));
    })()""")

    # 認証ボタンが表示されたか確認
    btn_shown = a.wait_for("""(() => {
        const btn = document.getElementById('ceBtnVer_github');
        return btn && !btn.hidden;
    })()""", timeout=SHORT)

    # 認証ボタンをクリックして認証モーダルを開く
    a.eval("document.getElementById('ceBtnVer_github').click()")
    auth_open = a.wait_for("document.getElementById('cardAuthDlg').open", timeout=SHORT)

    # ワンタイムコードが表示されているか
    # v0.16.1：確認コードは名刺の鍵の指紋（potalk1-＋22 文字）。鍵は IndexedDB の取り出せない鍵
    a.wait_for("/^potalk1-[A-Za-z0-9_-]{22}$/.test(document.getElementById('caCode').textContent)", timeout=SHORT)
    code_text = a.eval("document.getElementById('caCode').textContent")
    has_code = bool(code_text and code_text.startswith('potalk1-'))
    refreshed_code = a.eval("""(async () => {   // 鍵は取り出せない（extractable=false）
        const db = await new Promise((ok, ng) => { const o = indexedDB.open('pot-call-keys', 1); o.onsuccess = () => ok(o.result); o.onerror = ng })
        const v = await new Promise(ok => { const q = db.transaction('keys').objectStore('keys').get('card'); q.onsuccess = () => ok(q.result) })
        db.close()
        return !!v && v.priv && v.priv.extractable === false
    })()""")

    # キャンセルで閉じる
    a.eval("document.getElementById('caCancelBtn').click()")
    auth_closed = a.wait_for("!document.getElementById('cardAuthDlg').open", timeout=SHORT)

    # X の認証ボタンで開いた時、ポスト用UIとインテントリンクがセットされているか
    a.eval("""(() => {
        const inp = document.getElementById('ceSns_x');
        inp.value = 'potalk_app';
        inp.dispatchEvent(new Event('input'));
        document.getElementById('ceBtnVer_x').click();
    })()""")
    a.wait_for("document.getElementById('cardAuthDlg').open", timeout=SHORT)
    tweet_btn_href = a.eval("document.getElementById('caTweetBtn').href")
    has_tweet_intent = bool(tweet_btn_href and 'x.com/intent/post?text=' in tweet_btn_href)
    a.eval("document.getElementById('caCancelBtn').click()")
    a.wait_for("!document.getElementById('cardAuthDlg').open", timeout=SHORT)

    # 認証済み状態をシミュレートしてUI同期
    a.eval("""(() => {
        const c = JSON.parse(localStorage.getItem('pot-call-card') || '{}');
        c.socials = { github: 'octocat' };
        c.proofs = { github: { id: 'octocat', fp: %s } };
        localStorage.setItem('pot-call-card', JSON.stringify(c));
        // 再度編集モーダルを開き直して同期
        document.getElementById('ceCancel').click();
        document.getElementById('btnOpenCardEdit').click();
    })()""" % js_str(code_text[len('potalk1-'):]))

    badge_shown = a.wait_for("""(() => {
        const b = document.getElementById('ceVerBadge_github');
        return b && !b.hidden && b.textContent.includes('✅');
    })()""", timeout=SHORT)

    # 認証バッジをクリックして解除
    a.eval("document.getElementById('ceVerBadge_github').click()")
    unverified = a.wait_for("""(() => {
        const b = document.getElementById('ceVerBadge_github');
        const btn = document.getElementById('ceBtnVer_github');
        return b && b.hidden && btn && !btn.hidden;
    })()""", timeout=SHORT)

    # URL貼り付け時の自動切り詰め（OFUSE, Amazon, X）とラジオボタン自動選択の検証
    a.eval("""(() => {
        const inpOfuse = document.getElementById('ceTip_ofuse');
        inpOfuse.value = 'https://ofuse.me/aa2c9a98';
        inpOfuse.dispatchEvent(new Event('input'));

        const inpAmz = document.getElementById('ceTip_amazon');
        inpAmz.value = 'https://www.amazon.co.jp/hz/wishlist/ls/3869YUP07M5ZS?ref_=list_d_wl_lfu_nav_9';
        inpAmz.dispatchEvent(new Event('input'));

        const inpX = document.getElementById('ceSns_x');
        inpX.value = 'https://x.com/potalk_app?s=20';
        inpX.dispatchEvent(new Event('input'));
    })()""")

    trimmed_ofuse = a.wait_for("document.getElementById('ceTip_ofuse').value === 'aa2c9a98'", timeout=SHORT)
    trimmed_amz = a.wait_for("document.getElementById('ceTip_amazon').value === '3869YUP07M5ZS'", timeout=SHORT)
    trimmed_x = a.wait_for("document.getElementById('ceSns_x').value === 'potalk_app'", timeout=SHORT)
    # ほしい物リストの短縮 URL は展開できない＝案内を出す（ID だけを記憶する）
    a.eval("""(() => { const i = document.getElementById('ceTip_amazon'); i.value = 'https://amzn.asia/d/abc123'; i.dispatchEvent(new Event('input')) })()""")
    short_hint = a.wait_for("!document.getElementById('ceWarn').hidden && document.getElementById('ceWarn').textContent.includes('短縮 URL')", timeout=SHORT)
    a.eval("""(() => { const i = document.getElementById('ceTip_amazon'); i.value = '3869YUP07M5ZS'; i.dispatchEvent(new Event('input')) })()""")

    # 不正なURL入力時に is-bad が付与されるか検証
    a.eval("""(() => {
        const inpNote = document.getElementById('ceSns_note');
        inpNote.value = 'https://example.com/bad_link';
        inpNote.dispatchEvent(new Event('input'));
    })()""")
    is_bad_flag = a.wait_for("document.getElementById('ceSns_note').classList.contains('is-bad')", timeout=SHORT)

    # 保存ボタンで不正入力を弾くか検証
    a.eval("document.getElementById('ceSave').click()")
    save_blocked = a.wait_for("!document.getElementById('ceWarn').hidden && document.getElementById('cardEditDlg').open", timeout=SHORT)

    # 不正入力をクリアして正常保存できるか検証
    a.eval("""(() => {
        document.getElementById('ceSns_note').value = '';
        document.getElementById('ceSns_note').dispatchEvent(new Event('input'));
        document.getElementById('ceSave').click();
    })()""")
    saved_ok = a.wait_for("!document.getElementById('cardEditDlg').open", timeout=SHORT)

    # 再度開いて解除テスト
    a.eval("document.getElementById('btnOpenCardEdit').click()")
    a.wait_for("document.getElementById('cardEditDlg').open", timeout=SHORT)

    a.eval("document.getElementById('ceCancel').click()")

    ok = bool(dlg_open and btn_shown and auth_open and has_code and refreshed_code and has_tweet_intent and auth_closed and badge_shown and unverified and
              trimmed_ofuse and trimmed_amz and trimmed_x and short_hint and is_bad_flag and save_blocked and saved_ok)
    r.check('T73', '名刺編集・認証UI：URL自動切り詰め・短縮URLの案内・形式検証・鍵の行（取り出せない鍵）・Xポスト連携・モーダル開閉・バッジ解除', ok, 'pass',
            '編集開=%s / 認証釦=%s / 認証開=%s / 鍵の行=%s / 取り出せない=%s / X連携=%s / バッジ=%s / 解除=%s / OFUSE切詰=%s / Amazon切詰=%s / X切詰=%s / 短縮案内=%s / 不正検知=%s / 保存防御=%s / 保存OK=%s'
            % (dlg_open, btn_shown, auth_open, code_text, refreshed_code, has_tweet_intent, badge_shown, unverified,
               trimmed_ofuse, trimmed_amz, trimmed_x, short_hint, is_bad_flag, save_blocked, saved_ok))



def T70(r):
    """配信者の本人確認（oath・v0.15.28）：配信者が抜けた後に同じ peerId を名乗る改造クライアントが来ても、
    新入りはそれを配信者として扱わない（📣 なし・配信者専用のおたより非表示・音 0）。正規の聞き役は従来どおり 📣。

    peerId を名乗るには Trystero の selfId を差し替えた bundle が要るので、完全ローカル隔離の e2e
    （internal/security/2026-10-10-peerid/browser-repro/fullrun.py：ローカルリレー＋本物の index.html）を呼ぶ。
    internal（非公開 repo）が無い環境では飛ばす。将来のリファクタで門番（ownerOk/canHear）が外れたときの保険。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, '..', 'internal', 'security', '2026-10-10-peerid', 'browser-repro', 'fullrun.py')
    if not os.path.exists(script):
        print('  ⏭️  SKIP   T70 配信者の本人確認（internal/ が無いので飛ばす）'); return
    try:
        p = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=240,
                           env={**os.environ, 'POT_ROOT': os.path.abspath(os.path.join(here, '..'))})
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b'').decode(errors='replace') if isinstance(e.stdout, bytes) else (e.stdout or '')
        p = None
    res = [l for l in out.splitlines() if l.startswith('RESULT ')]
    r.check('T70', '配信者の本人確認：偽の配信者（同じ peerId）は 📣 にならず・おたよりも音も通らない・正規の聞き役は 📣',
            bool(res) and res[-1].startswith('RESULT ok=1'), 'pass',
            (res[-1] if res else '結果行なし: ' + out[-300:].replace('\n', ' / ')))


def T68(r, room):
    """古い入口リンク（#部屋名）は、同じ名前の部屋が複数開いていたら自動で選ばない（v0.15.25）

    v0.15.24 までは「在室の多いほう」を自動で選んでいた。在室は自己申告なので、偽の在室を本物より多く出すと
    別の部屋へ誘導できた（10-09 のセキュリティ監査）。いまは：
    ①同じ名前で 2 部屋開いている → 「選んでください」の案内（参加ボタンは出ない＝黙って新しい部屋を作らない）
    ②片方が閉じて 1 部屋になる → 「開いています」になり、参加するとその部屋に**ミュートで**入る
    """
    name = room + 'TWIN'
    def make(t):
        t.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
        t.eval("(() => { const i = document.getElementById('room'); i.value = %s; i.dispatchEvent(new Event('input')) })()" % js_str(name))
        click_join(t)
        return t.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    a, b = r.open_tab(), r.open_tab()
    made = make(a) and make(b)
    room_a = a.eval("new URLSearchParams(location.hash.slice(1)).get('room')")
    c = r.open_tab(hash_='#' + urllib.parse.quote(name))
    c.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    choose = c.wait_for("document.getElementById('entryText').textContent.includes('同じ名前の部屋が 2 つ')", timeout=DISCOVER)
    join_hidden = c.eval("document.getElementById('join').hidden && !document.getElementById('join').offsetParent")
    click_leave(b)
    live = c.wait_for("document.getElementById('entryText').textContent.includes('開いています') && !document.getElementById('join').hidden && !document.getElementById('join').disabled", timeout=DISCOVER)
    click_join(c)
    joined = c.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    same = c.eval("new URLSearchParams(location.hash.slice(1)).get('room')") == room_a
    muted = c.eval("document.getElementById('mute').classList.contains('muted')")
    ok = bool(made and choose and join_hidden and live and joined and same and muted)
    r.check('T68', '古い入口リンクは、同名の部屋が複数なら自動で選ばない／1 部屋ならミュートで入る', ok, 'pass',
            '2 部屋を作れた=%s / 選んでくださいの案内=%s / 参加ボタンは隠れる=%s / 1 部屋になると開いています=%s / 参加=%s / 残った部屋=%s / ミュート=%s'
            % (made, choose, join_hidden, live, joined, same, muted))
    click_leave(a); click_leave(c)


def T69(r, room):
    """リンクの入口の「このまま入ってみる」（v0.15.26）

    ロビーでの発見が遅れると 12 秒後に「誰もいないみたい」になり、参加ボタンが消える＝実は開いている部屋に入れない
    （10-06 の計測）。新しい形式のリンク（#room=ID）のお休み中の案内に「このまま入ってみる」を出し、押すと**リンクと
    同じ部屋 ID**に入る。古い形式（#部屋名）には入る先の ID が無いので出さない。
    """
    rid = 'T69' + ''.join(random.choice(string.ascii_letters) for _ in range(13))   # 16 文字のハッシュ（誰もいない部屋）
    t = r.open_tab(hash_='#room=%s&name=%s' % (rid, urllib.parse.quote(room + 'TRY')))
    t.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    shown = t.wait_for("(() => { const b = document.getElementById('entryTry'); return !!b && !b.hidden && !!b.offsetParent })()", timeout=DISCOVER)
    t.eval("document.getElementById('entryTry').click()", await_promise=False)
    joined = t.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    same = t.eval("new URLSearchParams(location.hash.slice(1)).get('room')") == rid
    t2 = r.open_tab(hash_='#' + urllib.parse.quote(room + 'TRYOLD'))
    t2.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    asleep_old = t2.wait_for("!!document.getElementById('entryOpen').offsetParent", timeout=DISCOVER)
    no_try_old = t2.eval("document.getElementById('entryTry').hidden")
    ok = bool(shown and joined and same and asleep_old and no_try_old)
    r.check('T69', 'お休み中のリンクから「このまま入ってみる」でリンクの部屋に入れる（古い形式には出さない）', ok, 'pass',
            'ボタンが出る=%s / 参加=%s / 同じ部屋 ID=%s / 古い形式でお休み中=%s / 古い形式には出ない=%s' % (shown, joined, same, asleep_old, no_try_old))
    click_leave(t)


def T27g(r, room):
    """配信者は、聞き役が突然消えても勝手にリロードしない（v0.15.4）

    2026-09-29・実機で確定した誤爆：唯一の聞き役が bye 無しで切れる（別アプリの通知を開く等）と、
    配信者が cut 枝（lostPeer かつ 0 人）で「壊れた」と判定し、25 秒後に自動リロードしていた。
    配信者にとって 0 人は正常なので、判定そのものを偽にした。**25 秒待たず**に、判定フック
    window.__potBroken() が偽のままであることで確かめる（監視ループは 2 秒周期）。
    対照：ふつうの雑談部屋では、相手が bye 無しで消えると（これは本当に壊れているので）真になる。
    """
    o = r.open_tab()
    if not make_bcast(r, o, room + 'NRL'):
        r.check('T27g', '配信者は聞き役が消えても自動リロードしない', False, 'pass', '配信部屋を作れなかった')
        return
    if not o.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER):
        r.check('T27g', '配信者は聞き役が消えても自動リロードしない', False, 'pass', '配信部屋に入れなかった')
        return
    h = o.eval('location.hash')
    l = r.open_tab(hash_=h)
    l.eval("window.__potBcErase(true)")   # 聞き役（鍵を持たない）
    click_join(l)
    # ★③（v0.15.10）：配信者は普通の聞き役を見ない。聞き役が入った合図は「🙂 ほか 1 人が聞いています」の出現で見る
    #   （l 側は配信者が見えて 2 人）。配信者の MEMBERS は自分だけ＝1 のままなので、それを合図には使えない。
    met = (l.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
           and o.wait_for("!!document.querySelector('#members .m-more')", timeout=DISCOVER))
    # ★聞き役を**突然**落とす：about:blank へ飛ばす（アプリの bye は出ない＝実機の「通知を開いて切れた」相当）
    l.call('Page.navigate', url='about:blank')
    alone = o.wait_for("!document.querySelector('#members .m-more')", timeout=DISCOVER)   # 聞き役が消えて「ほか N 人」も消える
    # 監視ループ（2 秒周期）を数回分待って、判定が偽のままであること＝壊れ扱いしない
    time.sleep(5)
    not_broken = o.eval('typeof window.__potBroken === "function" && window.__potBroken() === false')
    still = o.eval("!document.getElementById('tabs').hidden")   # 通話画面のまま（リロードもされていない）
    ok = met and alone and not_broken and still
    r.check('T27g', '配信者は聞き役が突然消えても自動リロードしない（cut 枝を発火させない）', ok, 'pass',
            '聞き役が入った（ほか1人）=%s / 聞き役が消えた=%s / 壊れ扱いしない=%s / 通話画面のまま=%s'
            % (met, alone, not_broken, still))
    click_leave(o)


def T27j(r, room):
    """BGM の引き継ぎ・配信部屋（v0.15.16）：配信者の BGM がリスナーの初期値になる（自分の好みも配信者に合わせる）。
    自分で BGM を変えたら、それが優先（以後は配信者に合わせない）。
    """
    o = r.open_tab()
    if not make_bcast(r, o, room + 'BGM') or not o.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER):
        r.check('T27j', 'BGM 引き継ぎ（配信部屋）', False, 'pass', '配信部屋を作れなかった')
        return
    # 配信者は焚き火にする
    show_tab(o, 'settings')
    o.eval("(() => { const b = document.getElementById('bgmFire'); b.checked = true; b.onchange() })()")
    o_fire = o.eval("document.getElementById('bgmFire').checked")
    h = o.eval('location.hash')
    # リスナー l は最初から「軽快な音楽」を選んでおく → 配信部屋では配信者（焚き火）に切り替わる
    l = r.open_tab(hash_=h)
    l.eval("localStorage.setItem('pot-call-bgm','tune'); window.__potBcErase(true)")
    click_join(l)
    l.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    adopted = l.wait_for("document.getElementById('bgmFire').checked", timeout=DISCOVER + 5)   # 配信者の焚き火をもらう
    # l が自分で「なし」に変えたら、以後は配信者に合わせない（配信者が別のに変えても追わない）
    show_tab(l, 'settings')
    l.eval("(() => { const b = document.getElementById('bgmOff'); b.checked = true; b.onchange() })()")
    o.eval("(() => { const b = document.getElementById('bgmTune'); b.checked = true; b.onchange() })()")   # 配信者が軽快な音楽へ
    time.sleep(2.5)
    kept_off = l.eval("document.getElementById('bgmOff').checked")   # 自分の選択（なし）が優先されたまま
    ok = o_fire and adopted and kept_off
    r.check('T27j', 'BGM 引き継ぎ（配信部屋）：配信者の BGM が初期値・自分で変えたら優先', ok, 'pass',
            '配信者=焚き火=%s / リスナーが焚き火をもらう=%s / 自分で変えたら追わない=%s' % (o_fire, adopted, kept_off))
    click_leave(l); click_leave(o)


def T27i(r, room):
    """入室通知（v0.15.11）：配信部屋に聞き役が入ると、ひとことに「〇〇さんが入室しました」が出る。
    ロビーのタップ確認に「🤫 静かに入る」チェックが出る（配信部屋の聞き役のときだけ）。
    """
    o = r.open_tab()
    if not make_bcast(r, o, room + 'ENT') or not o.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER):
        r.check('T27i', '入室通知：聞き役の入室がひとことに出る', False, 'pass', '配信部屋を作れなかった')
        return
    h = o.eval('location.hash')
    # 聞き役 l が入る → o のひとことに「…さんが入室しました」（システム行）
    l = r.open_tab(hash_=h)
    l.eval("window.__potBcErase(true)")
    lname = l.eval("(JSON.parse(localStorage.getItem('pot-call-profile')||'{}').name)||''")
    click_join(l)
    notice = o.wait_for("[...document.querySelectorAll('#chatLog .cl-sys .cl-text')]"
                        ".some(e => e.textContent.endsWith('さんが入室しました'))", timeout=DISCOVER + 5)
    named = o.eval("[...document.querySelectorAll('#chatLog .cl-sys .cl-text')]"
                   ".some(e => e.textContent === %s + ' さんが入室しました')" % js_str(lname)) if lname else notice
    # ★本人の画面にも同じ行が出る（v0.15.29）。Trystero は自分の送信を自分に配らないので、送り手側で出す必要がある
    selfline = l.wait_for("[...document.querySelectorAll('#chatLog .cl-sys .cl-text')]"
                          ".some(e => e.textContent === %s + ' さんが入室しました')" % js_str(lname or 'ゲスト'), timeout=SHORT)
    # ★🤫 チェックの存在：第三のタブ c がロビーで配信部屋をタップ → #roomAskQuiet が出る（聞き役のときだけ）
    c = r.open_tab()
    c.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    RB = "[...document.querySelectorAll('#lobbyList .room')].find(b => b.textContent.includes('📣'))"
    tapped = c.wait_for("!!" + RB, timeout=DISCOVER)
    if tapped:
        c.eval(RB + ".click()", await_promise=False)
    quiet_box = c.wait_for("!!document.getElementById('roomAskQuiet')", timeout=SHORT)
    ok = notice and named and selfline and quiet_box
    r.check('T27i', '入室通知：聞き役の入室が「〇〇さんが入室しました」で出る・本人にも出る・🤫 静かにチェックがある', ok, 'pass',
            'ひとことに入室通知=%s / 名前つき=%s / 本人の画面にも=%s / 🤫チェックあり=%s' % (notice, named, selfline, quiet_box))
    click_leave(l); click_leave(o)


def T60(r, room):
    """BGM の引き継ぎ・雑談部屋（v0.15.16）：**なしの人だけ**、一番長くいる人の BGM をもらう。
    BGM を選んでいる人はそのまま（音楽を奪わない）。
    """
    name = room + 'CBGM'
    a = r.open_tab()      # 先客（最古）
    a.eval("localStorage.setItem('pot-call-bgm','tune')")
    r.join(a, name)
    if not a.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER):
        r.check('T60', 'BGM 引き継ぎ（雑談部屋）', False, 'pass', '先客が入れなかった')
        return
    a_tune = a.eval("document.getElementById('bgmTune').checked")
    # 新入り b は BGM「なし」→ 先客の「軽快な音楽」をもらう
    b = r.open_tab()
    b.eval("localStorage.removeItem('pot-call-bgm')")   # なし（既定）
    r.join(b, name)
    b_adopt = b.wait_for("document.getElementById('bgmTune').checked", timeout=DISCOVER + 5)
    # 新入り c は「焚き火」を選んでいる → もらわず自分のまま（音楽を奪わない）
    c = r.open_tab()
    c.eval("localStorage.setItem('pot-call-bgm','fire')")
    r.join(c, name)
    c.wait_for('%s >= 3' % MEMBERS, timeout=DISCOVER)
    time.sleep(2.5)
    c_kept = c.eval("document.getElementById('bgmFire').checked")
    ok = a_tune and b_adopt and c_kept
    r.check('T60', 'BGM 引き継ぎ（雑談部屋）：なしの人だけ最古の人のをもらう・選んでいる人はそのまま', ok, 'pass',
            '先客=軽快=%s / なしの人がもらう=%s / 選んでいる人はそのまま=%s' % (a_tune, b_adopt, c_kept))
    click_leave(a); click_leave(b); click_leave(c)


def T61(r, room):
    """ひとことの行から無視（v0.15.17）：行をタップ → 名前の隣にハート → 押すと無視（初回は説明ダイアログ）。
    無視した人の過去の行は「〇〇 を無視しました（取り消す）」に畳まれ、以後のひとことは届かない。取り消すと戻り、また届く。
    自分の行にはハートが出ない。一覧に出ない聞き役でも「コメントした瞬間に無視できる」の土台。
    """
    name = room + 'IGN'
    a, b = r.open_tab(), r.open_tab()
    a.eval("localStorage.removeItem('pot-call-ignore-seen')")   # 初回の説明ダイアログを通る
    r.join(a, name); r.join(b, name)
    if not (a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)):
        r.check('T61', 'ひとことから無視', False, 'pass', '2人そろわなかった'); return
    # ★同じタブから続けて送るときは送信側の連投抑止（CHAT_EVERY_MS）を越えてから。待たないと 2 通目が
    #   **送信側で丸ごと落ちて**「無視中は届かない」が空振りする（発覚 2026-09-30）。
    say = lambda t, m: (time.sleep(1.7), t.eval("(() => { const i = document.getElementById('chatText'); i.value = %s; document.getElementById('chatSend').click() })()" % js_str(m)))[1]
    seen = lambda t, m, to=SHORT + 5: t.wait_for("[...document.querySelectorAll('#chatLog .cl-row:not(.cl-hid) .cl-text')].some(e => e.textContent === %s)" % js_str(m), timeout=to)
    show_tab(a, 'chat'); show_tab(b, 'chat')
    # b がコメント → a に届く。行は .can（タップ可）で、ハートはまだ出ていない
    say(b, 'いやなこと')
    got = seen(a, 'いやなこと')
    ROW = "[...document.querySelectorAll('#chatLog .cl-row.can')].find(r => r.querySelector('.cl-text').textContent === 'いやなこと')"
    heart_hidden = a.eval("(() => { const r = %s; return !!r && getComputedStyle(r.querySelector('.cl-ig')).display === 'none' })()" % ROW)
    # 行をタップ → ハートが現れる
    a.eval(ROW + ".click()")
    heart_shown = a.wait_for("(() => { const r = %s; return !!r && r.classList.contains('pick') && getComputedStyle(r.querySelector('.cl-ig')).display !== 'none' })()" % ROW, timeout=SHORT)
    # ハートを押す → 初回説明 → 「無視する」
    a.eval(ROW + ".querySelector('.cl-ig').click()")
    dlg = a.wait_for("document.getElementById('ignoreDlg').open", timeout=SHORT)
    a.eval("document.getElementById('ignoreYes').click()")
    folded = a.wait_for("!!document.querySelector('#chatLog .cl-ign') && document.querySelector('#chatLog .cl-ign .cl-text').textContent.endsWith(' を無視しました')", timeout=SHORT)
    row_hidden = a.eval("(() => { const r = [...document.querySelectorAll('#chatLog .cl-row[data-uid]:not(.cl-ign)')].find(r => r.querySelector('.cl-text').textContent === 'いやなこと'); return !!r && r.classList.contains('cl-hid') })()")
    # 無視中は b のひとことが a に届かない（b 自身の画面には出る）
    say(b, 'まだ言う')
    b_self = seen(b, 'まだ言う')
    time.sleep(3)
    blocked = not a.eval("[...document.querySelectorAll('#chatLog .cl-text')].some(e => e.textContent === 'まだ言う')")
    # 取り消す → 畳みが消えて元の行が戻る → また届く
    a.eval("document.querySelector('#chatLog .cl-ign .cl-undo').click()")
    restored = a.wait_for("!document.querySelector('#chatLog .cl-ign') && [...document.querySelectorAll('#chatLog .cl-row:not(.cl-hid) .cl-text')].some(e => e.textContent === 'いやなこと')", timeout=SHORT)
    say(b, 'もどった')
    again = seen(a, 'もどった')
    # 自分の行にはハートが無い（.can でない）
    say(a, 'じぶん')
    seen(a, 'じぶん')
    self_plain = a.eval("(() => { const r = [...document.querySelectorAll('#chatLog .cl-row')].find(r => r.querySelector('.cl-text').textContent === 'じぶん'); return !!r && !r.classList.contains('can') && !r.querySelector('.cl-ig') })()")
    ok = got and heart_hidden and heart_shown and dlg and folded and row_hidden and b_self and blocked and restored and again and self_plain
    r.check('T61', 'ひとことから無視：タップでハート→無視→畳む→届かない→取り消すで戻る・自分の行にはハート無し', ok, 'pass',
            '届く=%s / 最初はハート非表示=%s / タップで出る=%s / 説明ダイアログ=%s / 畳んだ=%s / 元の行を隠した=%s / 相手側には出る=%s / 無視中は届かない=%s / 取り消すで戻る=%s / また届く=%s / 自分の行は素=%s'
            % (got, heart_hidden, heart_shown, dlg, folded, row_hidden, b_self, blocked, restored, again, self_plain))
    click_leave(a); click_leave(b)


def T62(r, room):
    """無視した人のひとことは、つくよみの読み上げでも聞こえない（v0.15.18）。
    読み上げは配信者の端末で合成された 1 本のトラックなので、聞き役は配信者の {now: 投稿者} を見て、
    無視した人の番の間だけ自分の端末でトラックを enabled=false にする（配信者にも相手にも伝わらない）。
    配信者 o・聞き役 l（無視する側）・聞き役 x（無視される側）。
    """
    o = r.open_tab()
    if not make_bcast(r, o, room + 'TIG') or not o.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER):
        r.check('T62', '無視した人の読み上げ', False, 'pass', '配信部屋を作れなかった'); return
    h = o.eval('location.hash')
    l, x = r.open_tab(hash_=h), r.open_tab(hash_=h)
    for t in (l, x):
        t.eval("window.__potBcErase(true); localStorage.setItem('pot-call-ignore-seen','1')")
        click_join(t)
    met = l.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and x.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    # ★つくよみのエンジン読み込み（同梱 JS＋固定 URL の wasm＋モデルで 100MB 超）は回線により 1 分近くかかる。
    #   v0.16.11・vendored 化で 30 秒窓では CDN の揺れで取り逃す（解錠の順番は入口確認があるためここが安全）
    show_tab(o, 'settings'); o.eval("document.getElementById('sndTts').click()")
    tts_l = l.wait_for("%s >= 2" % AUDIOS, timeout=DISCOVER + 60)   # 配信者の声＋つくよみ
    # l の端末で「つくよみの（どれかの）トラックが切られた瞬間があったか」を 50ms ごとに記録する
    l.eval("window.__gateOff = 0; setInterval(() => { if ([...document.querySelectorAll('audio:not(#appAudio)')].some(a => a.srcObject && a.srcObject.getAudioTracks().some(t => !t.enabled))) window.__gateOff++ }, 50)")
    say = lambda t, m: (time.sleep(1.7), t.eval("(() => { const i = document.getElementById('chatText'); i.value = %s; document.getElementById('chatSend').click() })()" % js_str(m)))[1]
    for t in (o, l, x): show_tab(t, 'chat')
    # x がまず 1 通 → l はその行から無視
    say(x, 'はじめまして')
    ROW = "[...document.querySelectorAll('#chatLog .cl-row.can')].find(r => r.querySelector('.cl-text').textContent === 'はじめまして')"
    row = l.wait_for("!!" + ROW, timeout=SHORT + 5)
    time.sleep(3)   # 1 通目の読み上げ（無視前なので聞こえてよい）を済ませる
    l.eval(ROW + ".click()"); l.eval(ROW + ".querySelector('.cl-ig').click()")
    ignored = l.wait_for("!!document.querySelector('#chatLog .cl-ign')", timeout=SHORT)
    time.sleep(1); l.eval("window.__gateOff = 0")
    # 無視後の x のひとこと → 配信者が読む → l ではその間つくよみが切れる
    n = o.eval("window.__T.ttsTexts.length")
    say(x, 'きこえないはず')
    read_x = o.wait_for("window.__T.ttsTexts.length > %d" % n, timeout=SHORT + 10)
    gated = l.wait_for("window.__gateOff > 0", timeout=SHORT)
    reopened = l.wait_for("![...document.querySelectorAll('audio:not(#appAudio)')].some(a => a.srcObject && a.srcObject.getAudioTracks().some(t => !t.enabled))", timeout=SHORT)
    # 無視していない人（配信者自身）のひとことでは切れない
    time.sleep(2); l.eval("window.__gateOff = 0")
    n = o.eval("window.__T.ttsTexts.length")
    say(o, 'みなさんこんばんは')
    read_o = o.wait_for("window.__T.ttsTexts.length > %d" % n, timeout=SHORT + 10)
    time.sleep(2)
    open_o = l.eval("window.__gateOff") == 0
    # x 本人の端末では何も切れていない（無視は l の端末だけ）
    x_open = not x.eval("[...document.querySelectorAll('audio:not(#appAudio)')].some(a => a.srcObject && a.srcObject.getAudioTracks().some(t => !t.enabled))")
    ok = met and tts_l and row and ignored and read_x and gated and reopened and read_o and open_o and x_open
    r.check('T62', '無視した人のひとことは、つくよみの読み上げでも聞こえない（その番だけ自分の端末で消す）', ok, 'pass',
            '3人=%s / つくよみ到着=%s / 行=%s / 無視=%s / 配信者が読んだ=%s / その間切れた=%s / 読み終わりで戻る=%s / 他の人は切れない=%s(%s) / 相手の端末は無関係=%s'
            % (met, tts_l, row, ignored, read_x, gated, reopened, open_o, read_o, x_open))
    for t in (l, x, o): click_leave(t)


def T28(r, room):
    """配信部屋：**配信者がいない部屋では、誰の音も流れない**（受信側の既定）

    v0.12.0 から、リンクで来た聞き役は**配信者のいない配信部屋には入れない**（入口の確認で
    「この配信はいま開いていないみたいです」）。なので**配信者がいったん開いて去った部屋**で見る：
    配信者が開く → 聞き役2人が入る → 配信者が退出 → 残った2人の間で誰の音も流れず、マイクも取らず、
    **正常な通話ではないのに自動復帰が誤爆しない**（音が来ないのが正常な状態）。
    ここが壊れると「配信部屋なのに全員の声が流れる」＝機能の存在理由が消える。
    """
    o = r.open_tab()
    o.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    o.eval("(() => { document.getElementById('createBox').open = true; const i = document.getElementById('room');"
           " i.value = %s; i.dispatchEvent(new Event('input')); document.getElementById('kindBc').click() })()" % js_str(room + 'NOBC'))
    assert o.wait_for("document.getElementById('fbUrl').href.includes('~pk~')", timeout=SHORT), '配信部屋の鍵ができない'
    h = '#' + o.eval("document.getElementById('fbUrl').href").split('#', 1)[1]
    click_join(o)
    assert o.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), '配信者が参加できない'
    # 聞き役は別の端末＝鍵を持たない（localStorage は同じブラウザの全タブで共有されるので消す。配信者は参加済み）
    o.eval("window.__potBcErase(true)")
    a, b = r.open_tab(hash_=h), r.open_tab(hash_=h)
    # 他人の配信部屋のリンクを開いている間は「📣 配信部屋／🏠 雑談部屋」の2択を出さない
    # （部屋の正体はURLで決まっているので、ここで切り替えられると誤解のもとになる）。
    # ★属性ではなく**実際に描かれているか**を見る（display を書いた要素は hidden を付けても
    #   消えないことがある。2026-08-15 に踏んだ罠）
    row_gone = a.eval("(() => { const e = document.getElementById('kindRow');"
                      " return !e.offsetParent && getComputedStyle(e).display === 'none' })()")
    for t in (a, b):
        t.eval("window.__stay = 1")
        click_join(t)
    # ★配信部屋のプライバシー（v0.15.6 ②／v0.15.10 ③）：一覧に出るのは「声を出せる人」だけ。
    #   聞き役 a/b の画面は 自分＋配信者＝2（もう一方の聞き役は出ない・「ほか 1 人」で数だけ）。
    #   配信者 o も、募集オフ・挙手なしなら普通の聞き役を見ない＝自分だけ＝1（③）。人数は別途正確。
    three = (o.wait_for('%s === 1' % MEMBERS, timeout=DISCOVER)
             and a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER))
    more_line = a.wait_for("(document.querySelector('#members .m-more') || {}).textContent === '🙂 ほか 1 人が聞いています'", timeout=SHORT)
    # ★隠れ聞き役のコメントは認知される（v0.15.7）：一覧に出ない b がコメントしても、
    #   a の「ひとこと」（タイムライン）には出る（リストには載せない・コメントはひとことに出る＝利用者の原則）。
    #   吹き出しは「ほか N 人」の行に一瞬出る（誰が、は名乗らない）。ここではタイムライン到達を固定する。
    show_tab(b, 'chat'); show_tab(a, 'chat')
    b.eval("(() => { const i = document.getElementById('chatText'); i.value = 'きこえてる？'; document.getElementById('chatSend').click() })()")
    hidden_chat = a.wait_for("[...document.querySelectorAll('#chatLog .cl-text')].some(e => e.textContent === 'きこえてる？')", timeout=SHORT + 5)
    show_tab(a, 'members'); show_tab(b, 'members')
    click_leave(o)   # 配信者が去る
    # 配信者が去ると声を出せる人がいなくなる＝聞き役の画面は自分だけ（1）＋「ほか 1 人」。o は退出済み
    met = a.wait_for('%s === 1' % MEMBERS, timeout=DISCOVER) and b.wait_for('%s === 1' % MEMBERS, timeout=DISCOVER)
    time.sleep(30)   # 自動復帰の判定（25秒）を越えて観測する
    gum = a.eval('__T.gum') + b.eval('__T.gum')
    audios = a.eval(AUDIOS) + b.eval(AUDIOS)
    stayed = a.eval('window.__stay === 1') and b.eval('window.__stay === 1')
    note = a.eval(BC_NOTE)
    ok = three and more_line and hidden_chat and met and gum == 0 and audios == 0 and stayed and '配信者' in note and row_gone
    r.check('T28', '配信者が去った配信部屋では誰の音も流れない', ok, 'pass',
            '配信者1/聞き役2=%s / ほかN人=%s / 隠れ聞き役のコメントがひとことに出る=%s / 配信者が去って1人=%s / getUserMedia=%s回 / 音声要素=%s / リロードされていない=%s / 説明=%r / 2択を出さない=%s'
            % (three, more_line, hidden_chat, met, gum, audios, stayed, note[:24], row_gone))
    for t in (a, b):
        click_leave(t)


def T25c(r, a, b):
    """ひとことのタイムライン：**吹き出しが消えても残る**／誰の発言かが分かる／下から積まれる

    吹き出しは 4.6秒で消えるので、見逃した人には何も残らなかった。タイムラインはその受け皿。
    守るべきは3つ:
      ・自分の発言も相手の発言も、**誰の言葉か（絵文字＋名前）ごと**残る
      ・**吹き出しが消えた後も残っている**（＝別物として生きている）
      ・**下詰めで積まれる**（新しい行が下、溢れた古い行は上へ抜けて見えなくなる＝流しっぱなし）。
        高さはパネル側で決まっているので、ページが伸びて下の共有リンクを押し出すことはない
    """
    a.eval("document.querySelectorAll('.chat-bubble').forEach(e => e.remove())")
    # ★カードの高さを控えておく。**ここが増えたら「流しっぱなし」が壊れている**
    #   （2026-08-16 の実バグ：パネルが min-height だったので、行が増えるとログが下へ伸び続けた。
    #    ログとパネルが一緒に伸びるので「ログはパネルに収まっている」という判定では捕まらない）
    card_before = a.eval("document.querySelector('.status').offsetHeight")
    # 送信側の下限（1200ms）を越えながら、画面に入る行数を超える数を送る
    for i in range(10):
        a.eval("(() => { const t = document.getElementById('chatText');"
               " t.value = 'ろぐ%d'; document.getElementById('chatSend').click() })()" % i)
        time.sleep(1.4)
    rows = a.eval("document.querySelectorAll('#chatLog .cl-row').length")
    got = b.wait_for("document.querySelectorAll('#chatLog .cl-row').length >= 10", timeout=20)
    # 吹き出しは消えているのに、タイムラインには残っている
    time.sleep(5)
    bubbles = b.eval("document.querySelectorAll('.chat-bubble').length")
    stayed = b.eval("document.querySelectorAll('#chatLog .cl-row').length")
    last = b.eval("(document.querySelector('#chatLog .cl-row:last-child .cl-text')||{}).textContent")
    who = b.eval("!!document.querySelector('#chatLog .cl-row .cl-em') && "
                 "!!document.querySelector('#chatLog .cl-row .cl-who')")
    safe = b.eval("!document.querySelector('#chatLog .cl-text *')")   # 生のHTMLとして解釈されていない
    # **下詰め**（新しい行が下に積まれ、溢れた分は上で切れる）。ここが flex-start に戻ると、
    # 古い行だけが見えて**新着が画面の外に出る**＝タイムラインの意味が消える。
    bottom = b.eval("getComputedStyle(document.getElementById('chatLog')).justifyContent") == 'flex-end'
    # 高さはパネルが決めているので、行が増えてもページ側は伸びない
    card_after = a.eval("document.querySelector('.status').offsetHeight")
    fits = card_after == card_before
    ok = rows >= 10 and got and bubbles == 0 and stayed >= 10 and last == 'ろぐ9' and who and safe and bottom and fits
    r.check('T25c', 'ひとことがタイムラインに残る（吹き出しが消えても）', ok, 'pass',
            '自分=%s行 / 相手=%s行 / 吹き出し=%s / 最後の行=%r / 誰の発言か=%s / 中に要素なし=%s / 下詰め=%s / カードの高さ=%s→%s'
            % (rows, stayed, bubbles, last, who, safe, bottom, card_before, card_after))
    # ★メンバータブへ戻す。**リアクションはアバターが見えていないと飛ばない**ので、
    #   ここを忘れると後続の T23 が「届いていない」ように見える（実際は見えていないだけ）。
    show_tab(a, 'members')


def T25d(r, a):
    """ひとことのタイムライン：**退出すると消える**（次の参加に持ち越さない）

    ⚠️ **T5（退出）の後に呼ぶこと。**ここで自分から退出すると、後続のテストの前提
    （2人が繋がっている）を壊すので、既に退出済みの状態を見るだけにしてある。
    """
    gone = a.wait_for("document.querySelectorAll('#chatLog .cl-row').length === 0", timeout=SHORT)
    hidden = a.eval("(() => { const e = document.getElementById('chatLog');"
                    " return !e.offsetParent && getComputedStyle(e).display === 'none' })()")
    r.check('T25d', '退出でひとことのタイムラインが消える', gone and hidden, 'pass',
            '空になった=%s / 描かれていない=%s' % (gone, hidden))


def T29(r, room):
    """人数上限：**9人目が自分から抜けて「満員です」になる**（先にいた8人は残る）

    ⚠️ **既定では走らない**（`--only T29` を付けたときだけ）。9タブ＝フルメッシュ36本を
    張るので重く、発見にも時間がかかる。上限を触ったときに1回だけ確かめる用。

    ここで守っているのは「全員が同じ判定を回して**全員が抜けてしまう**」ことが起きないこと。
    抜けるのは**参加直後の窓（CAP_PROBE_MS）にいる側だけ**にしてある。
    """
    name = room + 'CAP'
    tabs = []
    for i in range(8):
        t = r.open_tab()
        r.join(t, name)
        tabs.append(t)
        time.sleep(1.5)   # 一斉に入れると発見が団子になるので少しずらす
    full = tabs[0].wait_for('%s === 8' % MEMBERS, timeout=DISCOVER * 2)
    if not full:
        r.check('T29', '満員の部屋に9人目が入れない', False, 'pass',
                '前提が作れなかった（8人が揃わない）: %s人' % tabs[0].eval(MEMBERS))
        return
    warned = tabs[0].eval("window.__potToasts().some(t => t.key === 'crowd')")
    # 9人目
    late = r.open_tab()
    r.join(late, name)
    kicked = late.wait_for("%s.includes('満員')" % STATE, timeout=DISCOVER)
    left = late.eval(JOIN_SHOWN)                     # 未参加の見た目に戻っている
    time.sleep(6)
    stayed = tabs[0].eval(MEMBERS) >= 8              # ★先にいた側は誰も抜けていない
    ok = kicked and left and stayed and warned
    r.check('T29', '満員の部屋に9人目が入れない（先にいた人は抜けない）', ok, 'pass',
            '「満員」と出た=%s / 未参加に戻った=%s / 先にいた側=%s人 / 注意が出ている=%s'
            % (kicked, left, tabs[0].eval(MEMBERS), warned))
    for t in tabs:
        click_leave(t)


def T4(r, a, b):
    """ミュートが相手のリストに伝わる"""
    a.eval("document.getElementById('mute').click()", await_promise=False)
    ok = b.wait_for('%s >= 1' % MUTED, timeout=SHORT)
    r.check('T4', 'ミュートが相手に伝わる', ok, 'pass')
    a.eval("document.getElementById('mute').click()", await_promise=False)   # 戻す


def T11(r, a, b):
    """つなぎ直す：通話が戻り、ミュートが持ち越され、見た目は参加中のまま

    retry は leaveCall() → joinCall() → toggleMute() を続けて呼ぶ唯一の場所で、
    「つなぎ直し中は未参加の見た目に戻さない」という例外的な分岐も持つ。
    ハンドラ抽出（Stage 2）で最も壊れやすいのでここだけ厚めに見る。
    """
    MUTE_ON = "document.getElementById('mute').classList.contains('muted')"
    a.eval("document.getElementById('mute').click()", await_promise=False)
    if not a.wait_for(MUTE_ON, timeout=SHORT):
        r.check('T11', 'つなぎ直しで通話が戻る', False, 'pass', 'ミュートにできず前提が崩れた')
        return
    a.eval("document.getElementById('retry').click()", await_promise=False)
    a.wait_for('%s < 2' % MEMBERS, timeout=SHORT)          # 先に退出が走ったことを確かめてから
    ok = a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    # 落ちたときは両側の見え方を残す（片側だけ見えている＝非対称なのか、双方見えていないのかで原因が違う）
    note = '' if ok else 'A: %r（%s人） / B: %r（%s人）' % (
        a.eval(STATE), a.eval(MEMBERS), b.eval(STATE), b.eval(MEMBERS))
    r.check('T11', 'つなぎ直しで通話が戻る', ok, 'pass', note)
    r.check('T11b', 'つなぎ直しでミュートが持ち越される', bool(a.eval(MUTE_ON)), 'pass')
    got_audio = a.wait_for('%s >= 1' % AUDIOS, timeout=25)
    r.check('T11d', 'つなぎ直しで相手の音声が届く', got_audio, 'pass',
            '' if got_audio else '参加者リストには出ているのに <audio> が無い＝音が来ていない')
    joined_ui = a.eval("document.getElementById('room').disabled === true"
                       " && document.getElementById('setupFields').classList.contains('collapsed')")
    r.check('T11c', 'つなぎ直し後も参加中の見た目のまま', bool(joined_ui), 'pass')
    # つなぎ直しはリロードを挟むようになったので、ユーザー操作の実績（自動再生の許可）が
    # リセットされる。相手の音声が自動再生を止められると「音声を有効化」が出る＝本物の警告。
    # --real-autoplay を付けて走らせると本番と同じ条件で確かめられる。
    r.check('T11e', 'つなぎ直し後に「音声を有効化」が出ない', not a.eval(UNLOCK_SHOWN), 'pass',
            'play() の失敗: %s' % (a.eval('__T.playErrors') or 'なし'))
    # リロードを挟むので、初回訪問あつかいでヘルプが自動で開いてしまう事故が起きる。
    # 復旧の最中に説明を被せられても邪魔なだけ（しかも本人は再読込に気づいていない）。
    r.check('T11f', 'つなぎ直しでヘルプが開かない',
            not a.eval("document.getElementById('helpDialog').open"), 'pass')
    a.eval("document.getElementById('mute').click()", await_promise=False)   # 戻す


def T5(r, a, b):
    """退出：マイク解放・リスト空・未参加の見た目に戻る（join/leave の対称性）"""
    click_leave(a)
    time.sleep(1.0)
    live = a.eval(LIVE)
    members = a.eval(MEMBERS)
    shown = a.eval(JOIN_SHOWN)
    state = a.eval(STATE)
    # ★音の出口（appAudio）も畳まれていること（v0.14.4）。srcObject が残っていると iPhone で退出のたびに
    #   「プププ」（死んだ stream の最後のバッファが繰り返される）。要素は止まり、stream は外れ、track は終わる
    sink = a.eval("(() => { const e = document.getElementById('appAudio'); return e.paused && e.srcObject === null })()")
    ok = live == 0 and members == 0 and shown and state == '退出しました' and sink
    r.check('T5', '退出でマイクとUIが元に戻る', ok, 'pass',
            'live=%s members=%s joinボタン=%s state=%r 音の出口が畳まれた=%s' % (live, members, shown, state, sink))
    ok2 = b.wait_for('%s === 1' % MEMBERS, timeout=SHORT)
    r.check('T5b', '相手側から居なくなる', ok2, 'pass')


def T6(r, a, b, room):
    """再参加：ゾンビ room を掴まない"""
    r.join(a, room)
    ok = a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    r.check('T6', '入り直せる', ok, 'pass', '' if ok else a.eval(STATE))
    # 音が来なければ 10秒後に「送り直して」を送る仕掛けが入ったので、その復帰ぶんまで待つ
    got_audio = a.wait_for('%s >= 1' % AUDIOS, timeout=25)
    r.check('T6b', '入り直しで相手の音声が届く', got_audio, 'pass',
            '' if got_audio else '参加者リストには出ているのに <audio> が無い＝音が来ていない')
    r.check('T6c', '入り直しで「音声を有効化」が残らない', not a.eval(UNLOCK_SHOWN), 'pass',
            '' if not a.eval(UNLOCK_SHOWN) else 'joinCall が unlock を隠していない')


def T7(r, room):
    """ロビー：通話中の部屋が、参加していない第三のタブから見える"""
    c = r.open_tab()
    ok = c.wait_for('%s.includes(%s)' % (LOBBY, js_str(room)), timeout=DISCOVER)
    r.check('T7', 'ロビーに部屋が出る', ok, 'pass',
            '' if ok else '見えたもの: %r' % c.eval(LOBBY))
    return c


def T8(r, room):
    """B-3：join 連打で getUserMedia が二重に走らない"""
    t = r.open_tab()
    t.eval("(() => { const r = document.getElementById('room');"
           " r.value = %s; r.dispatchEvent(new Event('input'));"
           " const j = document.getElementById('join'); j.click(); j.click(); })()" % js_str(room + '-x'),
           await_promise=False)
    time.sleep(3)
    gum = t.eval('__T.gum')
    r.check('T8', 'join 連打で二重にマイクを掴まない', gum == 1, 'pass',
            'getUserMedia 呼び出し回数 = %s' % gum)
    click_leave(t)


def T9(r, room):
    """B-2：join がライブラリ読込で失敗したとき、マイクを離し hash も焼き付けない"""
    t = r.open_tab(block_esm=True)
    if not t.wait_for(BOOTED, timeout=SHORT):
        r.check('T9', 'Trystero の読み込みを塞いでも起動する', False, 'pass', 'ページが起動しなかった')
        return t
    r.join(t, room + '-fail')
    ok_state = t.wait_for("%s.includes('つなぐ準備に失敗')" % STATE, timeout=SHORT)
    live = t.eval(LIVE)
    hash_ = t.eval('location.hash')
    # ★状態カードは初期状態では畳んであるので、**失敗したときは必ず出る**ことを確かめる。
    #   ここが隠れたままだと**エラーの出る場所ごと消えて**、「押しても何も起きない」に見える。
    shown = t.eval("!document.getElementById('statusCard').hidden")
    r.check('T9', 'join 失敗でマイクを解放する', ok_state and live == 0 and shown, 'pass',
            'live=%s state=%r / 失敗の知らせが見えている=%s' % (live, t.eval(STATE), shown))
    r.check('T9b', 'join 失敗で hash を焼き付けない', hash_ == '', 'pass',
            'hash=%r' % hash_)
    return t


def T13(r, a, b):
    """正常な通話中に「音が届いていません」を誤って出さないこと

    音声到着の監視は 6秒待って催促し、3回失敗して初めて表示する（v0.14.21 で 10秒→6秒）。正常時に出てしまうと
    「壊れていないのに壊れたと言う」ことになり、無音を知らせる価値そのものを損なう。
    催促が一巡する時間（6 + 6×3 = 24秒）より長く待って、それでも出ないことを見る。
    """
    WARN_SHOWN = "window.__potToasts().some(t => t.key === 'audio')"
    appeared = a.wait_for(WARN_SHOWN, timeout=32) or b.wait_for(WARN_SHOWN, timeout=1)
    r.check('T13', '正常な通話で誤警告を出さない', not appeared, 'pass',
            '' if not appeared else a.eval("JSON.stringify(window.__potToasts())"))


def T12(r, a):
    """「音声を有効化」が出ていないか（出入りのたびに出る、というユーザー報告の再現）

    unlock ボタンは相手音声の `play()` が reject したときだけ出す作りだが、reject の理由は
    握りつぶされている。--real-autoplay を付けて本物の自動再生制限で走らせ、理由まで見る。
    """
    shown = a.eval("getComputedStyle(document.getElementById('unlock')).display !== 'none'")
    errs = a.eval('__T.playErrors')
    r.check('T12', '入り直しで「音声を有効化」が出ない', not shown, 'pass',
            'play() の失敗: %s' % (errs if errs else 'なし'))


def T16(r, a):
    """接続経路アイコンが出ている（usingRelay の可視化）

    Stage 5 で usingRelay を call オブジェクトへ移す。移し損ねても画面はほぼ同じに見えて
    しまう（アイコンが消えるだけ）ので、ここで固定しておく。
    """
    n = a.eval("document.querySelectorAll('#members .m-conn').length")
    r.check('T16', '参加者リストに接続経路アイコンが出る', n >= 1, 'pass', 'アイコン数=%s' % n)

    # ★TURN が取れているか。取れないと relay-only が付かず**黙って直結にフォールバック**するので、
    # アイコンの個数だけでは気づけない（直結でも同じ数だけ出る）。CSP の connect-src や Worker の
    # CORS で Worker への fetch が塞がれた場合、ここだけが赤くなる＝それを捕まえるための検査。
    # ネットワーク側の都合で TURN に届かない環境では落ちるが、その時は本当に経路が変わっている。
    relay = a.eval("document.querySelectorAll('#members .m-conn.relay').length")
    r.check('T16b', 'TURN が取れて中継経路になっている', relay == n and n >= 1, 'pass',
            '中継=%s / 全体=%s（0なら Worker へ届いていない＝CSP か CORS を疑う）' % (relay, n))


def T21(r, a):
    """CSP が正規の行き先を1つも塞いでいない

    `<meta>` の CSP に書き忘れた行き先があっても、**多くは静かに劣化するだけ**で画面には
    出ない（TURN が取れなければ黙って直結、Nostr が塞がれれば「相手を探しています…」のまま）。
    ブラウザは違反を securitypolicyviolation で必ず教えてくれるので、それを数える。
    通話・ロビー・TURN・Analytics を一通り通した**後**に見ること（読み込みは遅れて起きる）。
    """
    # QR はカードを裏返すまで qrcode-generator を読みに行かない＝ここまでの流れでは vendor/ への
    # 読み込みが1本ぶん試されていない。CSP の検査の前に通しておく。
    qr = flip_back(a)
    r.check('T21b', 'QRコードが生成できる（vendor/ からの読み込みが通る）', qr, 'pass',
            '' if qr else 'SVG が出ない＝CSP の script-src を疑う')

    v = a.eval('JSON.stringify(__T.csp)')
    try:
        items = json.loads(v) if v else []
    except Exception:
        items = []
    r.check('T21', 'CSP が正規の行き先を塞いでいない', not items, 'pass',
            '違反 %d 件%s' % (len(items), ('：' + ' / '.join(items[:4])) if items else ''))


def T20(r, room):
    """経路アイコンの凡例：参加で開く → 時間で畳む → 退出で消える

    **専用タブ・専用の部屋で単独に回す**。本編の流れに混ぜると、相手の発見を待つ数秒で
    LEGEND_OPEN_MS(8秒) を過ぎてしまい「開いた状態で始まる」を観測できない（一度そう書いて
    T20 が誤って落ちた）。相手は要らない検査なので、一人で入れば足りる。

    畳む部分が本題：join で `open = true` した直後に張るタイマーを、自分で消していないか。
    details の `toggle` は**非同期に飛ぶ**ので、そちらを購読しているとタイマーが即座に消えて
    永久に畳まれない（実際にそう書いて踏んだ）。だから summary の click を見ている。
    """
    # summary は状態で姿を変える（閉=チップ .cl-mini／開=凡例の1行目 .cl-full）。要素は
    # どちらの状態でも DOM にあるので、**実際に描かれているか**を display で見ないと判定できない。
    shown = "(s => getComputedStyle(document.querySelector(s)).display !== 'none')"
    t = r.open_tab()
    r.join(t, room + '-lgnd')
    ok = t.wait_for("!document.getElementById('connLegend').hidden", timeout=SHORT)
    op = t.eval("document.getElementById('connLegend').open")
    mini_on = t.eval("%s('#connLegendSum .cl-mini')" % shown)
    lines = t.eval("[...document.querySelectorAll('#connLegend .cl-line')]"
                   ".filter(e => getComputedStyle(e).display !== 'none').length")
    r.check('T20', '参加すると凡例が開いて出る（チップは消える）',
            ok and op and lines == 2 and not mini_on, 'pass',
            '表示=%s open=%s 行数=%s チップ=%s' % (ok, op, lines, mini_on))

    time.sleep(9)   # LEGEND_OPEN_MS = 8000 より少し長く待つ
    op2 = t.eval("document.getElementById('connLegend').open")
    mini_on2 = t.eval("%s('#connLegendSum .cl-mini')" % shown)
    icons = t.eval("document.querySelectorAll('#connLegendSum .cl-mini .m-conn').length")
    r.check('T20c', '凡例が時間で畳まれてチップに戻る', (not op2) and mini_on2 and icons == 2, 'pass',
            'open=%s チップ=%s アイコン数=%s' % (op2, mini_on2, icons))

    click_leave(t)
    hidden = t.wait_for("document.getElementById('connLegend').hidden", timeout=SHORT)
    r.check('T20b', '退出で凡例が消える', hidden, 'pass', 'hidden=%s' % hidden)


def T17(r, room):
    """wakeLock を join で取り、leave で返す（wantWake / wakeLock の対称性）

    取りっぱなしだと退出後も画面が消えない。目視できない漏れなので計測で見る。
    **wakeLock は前面のタブでしか取得できない**（隠れていると NotAllowedError）ので、
    このテストだけは専用タブを前面に出して単独で回す。相手は要らない。
    """
    t = r.open_tab()
    t.call('Page.bringToFront')
    r.join(t, room + '-wake')
    if not t.wait_for("document.getElementById('state').textContent.includes('参加中')", timeout=DISCOVER):
        r.check('T17', 'wakeLock を取って返している', False, 'pass', '参加できなかった')
        return
    time.sleep(1)
    w = t.eval('__T.wake') or {}
    if not w.get('got'):
        r.check('T17', 'wakeLock を取って返している', False, 'pass',
                '取得できなかった 要求=%s 失敗=%s' % (w.get('req'), w.get('err')))
        return
    click_leave(t)
    time.sleep(1)
    w = t.eval('__T.wake') or {}
    r.check('T17', 'wakeLock を取って返している', w.get('released', 0) >= 1, 'pass',
            '取得=%s 解放=%s' % (w.get('got'), w.get('released')))


def T18(r, a):
    """退出後に成長タイマーが止まっている（growTimer の後始末）

    30秒ごとに滞在時間を加算するタイマーが leave で止まっていないと、退出後も時間が
    増え続ける。**このタブが記録を書いた回数**を32秒空けて2回見る（localStorage の値を
    読むと、まだ通話中の別タブの加算まで拾ってしまう）。
    """
    before = a.eval('__T.statsWrites')
    time.sleep(32)
    after = a.eval('__T.statsWrites')
    r.check('T18', '退出後に成長タイマーが止まっている', before == after, 'pass',
            'このタブの書き込み 退出直後=%s / 32秒後=%s' % (before, after))


def T19(r, room):
    """入室音が鳴る（soundsArmed の解禁）

    参加直後1.5秒は「既にいる人ぶん」の接続が連発するので鳴らさない決まり。その解禁が
    効かなくなると無音になるが、音は聞けないので発振器の生成数で見る。
    """
    a = r.open_tab()
    r.join(a, room + '-snd')
    if not a.wait_for("document.getElementById('state').textContent.includes('参加中')", timeout=DISCOVER):
        r.check('T19', '入室音が鳴る', False, 'pass', '参加できなかった')
        return
    time.sleep(3)                      # 解禁（1.5秒）を跨いでから相手を入れる
    base = a.eval('__T.osc')
    b = r.open_tab()
    r.join(b, room + '-snd')
    got = a.wait_for('__T.osc > %d' % base, timeout=DISCOVER)
    r.check('T19', '入室音が鳴る（解禁後に相手が来たとき）', got, 'pass',
            '発振器 %s → %s' % (base, a.eval('__T.osc')))
    click_leave(a)
    click_leave(b)


def T15(r):
    """版の表示：画面に出ているか／ファイル冒頭のコメントと食い違っていないか

    版は APP_VERSION（唯一の定義）と冒頭コメントの2箇所に書いてある。人が片方だけ直す
    のは時間の問題なので、ここで突き合わせる。
    """
    t = r.open_tab()
    shown = t.eval("document.getElementById('ver').textContent")
    src = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
    head = src[:src.index('<html')]
    ok = bool(shown) and shown in head
    r.check('T15', '版の表示が冒頭コメントと一致する', ok, 'pass',
            '画面=%r / 冒頭コメントに同じ番号があるか=%s' % (shown, shown in head if shown else '—'))

    # β（本番以外）にだけ出る「安定版へ」。**本番の hostname では出してはいけない**
    # （出ると本番から本番へのリンクになり、意味が無いどころか紛らわしい）。
    # ここは localhost ＝ 本番以外なので、出ているのが正しい。
    beta_mark = t.eval("document.getElementById('chan').textContent").strip()
    link = t.eval("(() => { const a = document.querySelector('.stable-link');"
                  " return a ? a.getAttribute('href') : null })()")
    # **部屋名（ハッシュ）を引き継ぐ**こと。参加でハッシュが書き換わったら追従する
    t.eval("location.hash = '#%E9%83%A8%E5%B1%8B'")
    time.sleep(0.4)
    link2 = t.eval("(() => { const a = document.querySelector('.stable-link');"
                   " return a ? a.getAttribute('href') : null })()")
    ok2 = (beta_mark == 'β' and link == 'https://potalk.app/'
           and link2 == 'https://potalk.app/#%E9%83%A8%E5%B1%8B')
    r.check('T15b', 'β には安定版への導線が出て、部屋名を引き継ぐ', ok2, 'pass',
            'β表示=%r / 初期=%r / ハッシュ変更後=%r' % (beta_mark, link, link2))


def T34(r):
    """ファビコンが**取得できるURL**で宣言され、実際に読めること

    2026-08-18：ブラウザのタブには出ているのに、**Google の検索結果では空**だった。
    原因は SVG を `data:` URI で埋めていたこと＝**クローラは URL を辿って取りに来る**ので、
    data: では取りようがない。`/favicon.ico` も 404 だった（Google はここも見に来る）。
    「宣言はあるのに取得できない」は画面を見ても気づけないので、ここで実際に読み込んで確かめる。
    """
    t = r.open_tab()
    res = t.eval("""(async () => {
      const links = [...document.querySelectorAll('link[rel~=\"icon\"]')].map(l => l.getAttribute('href'));
      const data = links.filter(h => h.startsWith('data:')).length;
      const urls = links.filter(h => !h.startsWith('data:'));
      const loaded = await Promise.all(urls.map(h => new Promise(ok => {
        const i = new Image(); i.onload = () => ok(i.naturalWidth > 0); i.onerror = () => ok(false); i.src = h;
      })));
      // /favicon.ico も置いてあること（宣言と別に Google が直接見に来る）
      const ico = await new Promise(ok => {
        const i = new Image(); i.onload = () => ok(true); i.onerror = () => ok(false); i.src = '/favicon.ico';
      });
      return JSON.stringify({urls, loaded, data, ico});
    })()""")
    d = json.loads(res)
    ok = len(d['urls']) >= 1 and all(d['loaded']) and d['ico']
    r.check('T34', 'ファビコンが取得できるURLで宣言され、実際に読める', ok, 'pass',
            '宣言=%s / 読めた=%s / data:のみの宣言=%s件 / favicon.ico=%s'
            % (d['urls'], d['loaded'], d['data'], d['ico']))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default=None, help='既定 http://localhost:<web-port>/')
    ap.add_argument('--port', type=int, default=9222, help='DevTools ポート')
    ap.add_argument('--web-port', type=int, default=8000)
    ap.add_argument('--tag', default=None, help='appId の接尾辞（既定は毎回ランダム＝完全隔離）')
    ap.add_argument('--no-serve', action='store_true', help='サーバは自分で起動済み')
    ap.add_argument('--headful', action='store_true')
    ap.add_argument('--real-autoplay', action='store_true',
                    help='自動再生制限を本物と同じにする（「音声を有効化」の再現用）')
    ap.add_argument('--keep', action='store_true')
    ap.add_argument('--only', nargs='*', default=None)
    a = ap.parse_args()

    if not os.path.exists(CHROME):
        sys.exit('Chrome が見つかりません: ' + CHROME)
    a.tag = a.tag or 'ci' + ''.join(random.choice(string.ascii_lowercase) for _ in range(5))
    a.url = a.url or 'http://localhost:%d/' % a.web_port

    print('ぽっと通話 回帰テスト')
    print('  URL   %s' % a.url)
    if a.no_serve:
        # 相乗り時は appId を決めているのは向こうのサーバ。ここで自分のタグを出すと嘘になる
        # （＝隔離できているつもりで、実は手動テストのタブと同じ空間にいる）。
        print('  appId 起動済みサーバ任せ（--no-serve）。手動テスト用タブと同じ空間で走ります')
    else:
        print('  appId kuramo-webrtc-call-%s （本番ロビーからは隔離）' % a.tag)

    r = Runner(a)
    server = r.start_server()
    room = 'テスト' + ''.join(random.choice(string.digits) for _ in range(4))
    want = set(a.only) if a.only else None

    def run(tid):
        return want is None or tid in want

    try:
        r.start_chrome()
        print('  部屋名 %s\n' % room)
        if run('T1'):
            T1(r, room)
        if run('T1b'):
            T1b(r)
        if run('T2'):
            T2(r, room)
        if run('T2b'):
            T2b(r, room)
        if run('T14'):
            T14(r, room)
        if run('T15'):
            T15(r)
        if run('T34'):
            T34(r)
        if run('T40'):
            T40(r)
        if run('T41'):
            T41(r, room)
        if run('T50'):
            T50(r, room)
        if run('T51'):
            T51(r, room)
        if run('T52'):
            T52(r, room)
        if run('T53'):
            T53(r, room)
        if run('T54'):
            T54(r, room)
        if run('T43'):
            T43(r)
        if run('T44'):
            T44(r)
        if run('T46'):
            T46(r, room)
        if run('T47'):
            T47(r, room)
        if run('T48'):
            T48(r, room)
        if run('T17'):
            T17(r, room)
        if run('T19'):
            T19(r, room)
        if run('T20'):
            T20(r, room)
        if run('T37'):
            T37(r, room)
        if run('T45'):
            T45(r, room)
        if run('T42'):
            T42(r, room)
        if run('T38'):
            T38(r, room)
        a_tab = b_tab = None
        if run('T3'):
            a_tab, b_tab = T3(r, room)
            if run('T4'):
                T4(r, a_tab, b_tab)
            if run('T33'):
                T33(r, a_tab, b_tab)
            if run('T39'):
                T39(r, a_tab)
            if run('T30'):
                T30(r, a_tab, b_tab)
            if run('T35'):
                T35(r, a_tab, b_tab)
            if run('T13'):
                T13(r, a_tab, b_tab)
            if run('T16'):
                T16(r, a_tab)
            if run('T11'):
                T11(r, a_tab, b_tab)
            if run('T25'):
                T25(r, a_tab, b_tab)
                if run('T25b'):
                    T25b(r, a_tab, b_tab)
                if run('T25c'):
                    T25c(r, a_tab, b_tab)
                if run('T49'):
                    T49(r, a_tab, b_tab)
                if run('T57'):
                    T57(r, a_tab, b_tab)
            if run('T24'):
                T24(r, a_tab, b_tab)
            if run('T23'):
                T23(r, a_tab, b_tab)
            if run('T22'):
                T22(r, a_tab, b_tab)
                if run('T22b'):
                    T22b(r, a_tab, b_tab)
            if run('T7'):
                T7(r, room)
            if run('T5'):
                T5(r, a_tab, b_tab)
                if run('T25d'):
                    T25d(r, a_tab)   # ★T5 が退出した直後を見る（自分では退出しない）
                if run('T18'):
                    T18(r, a_tab)
                if run('T6'):
                    T6(r, a_tab, b_tab, room)
                    if run('T12'):
                        T12(r, a_tab)
        # 配信部屋。**通話中UIを増やしたので T1b にも足してある**（参加前に出ていないこと）
        if run('T26'):
            T26(r, room)
        if run('T27'):
            bc_a, bc_b = T27(r, room)
            if run('T55'):
                T55(r, bc_a, bc_b)   # ★T27b（つなぎ直しを繰り返す）より前＝a が配信者のまま通話中の状態で見る
                if run('T55b'):
                    T55b(r, bc_a, bc_b)
            if run('T27b'):
                T27b(r, bc_a, bc_b)
        if run('T31'):
            T31(r, room)
        if run('T32'):
            T32(r, room)
        if run('T28'):
            T28(r, room)
        if run('T27g'):
            T27g(r, room)
        if run('T27h'):
            T27h(r, room)
        if run('T63'):
            T63(r, room)
        if run('T66'):
            T66(r, room)
        if run('T68'):
            T68(r, room)
        if run('T69'):
            T69(r, room)
        if run('T70'):
            T70(r)
        if run('T71'):
            T71(r, room)
        if run('T71b'):
            T71b(r, room)
        if run('T72'):
            T72(r, room)
        if run('T73'):
            T73(r)
        if run('T74'):
            T74(r, room)
        if run('T75'):
            T75(r, room)
        if run('T76'):
            T76(r, room)
        if run('T77'):
            T77(r, room)
        if run('T78'):
            T78(r, room)
        if run('T79'):
            T79(r, room)
        if run('T80'):
            T80(r, room)
        if run('T81'):
            T81(r, room)
        if run('T82'):
            T82(r, room)
        if run('T83'):
            T83(r, room)
        if run('T84'):
            T84(r, room)
        if run('T85'):
            T85(r, room)
        if run('T87'):
            T87(r)
        if run('T86'):
            T86(r, room)
        if run('T88'):
            T88(r, room)
        if run('T91'):
            T91(r, room)
        if run('T92'):
            T92(r, room)
        if run('T93'):
            T93(r, room)
        if run('T94'):
            T94(r, room)
        if run('T95'):
            T95(r, room)
        if run('T96'):
            T96(r, room)
        if run('T97'):
            T97(r, room)
        if run('T89'):
            T89(r, room)
        if run('T90'):
            T90(r, room)
        if run('T27i'):
            T27i(r, room)
        if run('T27j'):
            T27j(r, room)
        if run('T60'):
            T60(r, room)
        if run('T61'):
            T61(r, room)
        if run('T62'):
            T62(r, room)
        # 人数上限は9タブ張るので**明示したときだけ**走らせる（既定の回帰には重すぎる）
        if a.only and 'T29' in a.only:
            T29(r, room)
        if run('T8'):
            T8(r, room)
        if run('T9'):
            T9(r, room)
        # CSP は最後に見る。通話・ロビー・TURN・Analytics を一通り通した後でないと、
        # 遅れて起きる読み込みの違反を取りこぼす。
        if run('T21') and a_tab:
            T21(r, a_tab)
    finally:
        r.cleanup()
        if server:
            server.terminate()

    print()
    hard = [x for x in r.results if x[3] == 'pass' and not x[2]]
    fixed = [x for x in r.results if x[3] == 'known-fail' and x[2]]
    known = [x for x in r.results if x[3] == 'known-fail' and not x[2]]
    print('結果: %d 件中 回帰 %d 件 / 既知バグ %d 件 / 直った %d 件'
          % (len(r.results), len(hard), len(known), len(fixed)))
    if fixed:
        print('  🎉 直ったもの: %s → smoke.py の expect を pass に変えてください'
              % ', '.join(x[0] for x in fixed))
    return 1 if hard else 0


if __name__ == '__main__':
    sys.exit(main())
