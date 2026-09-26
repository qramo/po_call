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
  window.__potTts = { synth: async text => {
    T.ttsTexts.push(text);
    const n = 13230, a = new Float32Array(n);
    for (let i = 0; i < n; i++) a[i] = Math.sin(i / 22050 * 440 * 2 * Math.PI) * 0.3;
    return { samples: a, sampleRate: 22050 };
  } };
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
            '--use-fake-device-for-media-capture',   # 偽マイク（許可ダイアログも出ない）
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
            tab.call('Network.setBlockedURLs', urls=['*esm.sh*'])
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
    st = t.eval("JSON.stringify(['eq','connLegend','reactions','bcNote','speak','chatLog','tabs','mailNow'].reduce((o,id) => {"
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
    show_tab(a, 'chat')
    chat_ok = a.eval("%s('chatRow') && %s('members') === false" % (VISIBLE, VISIBLE))
    h_chat = a.eval("document.getElementById('panChat').offsetHeight")
    show_tab(a, 'settings')
    set_ok = a.eval("%s('sndPeerRow') && %s('chatRow') === false" % (VISIBLE, VISIBLE))
    h_set = a.eval("document.getElementById('panSettings').offsetHeight")
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
    ok = default_ok and chat_ok and set_ok and back_ok and aria == 'tabSettings' and same_h and narrow_ok
    r.check('T30', 'タブでメンバー／ひとこと／設定が入れ替わる', ok, 'pass',
            '既定=メンバー:%s / ひとこと:%s / 設定:%s / 戻る:%s / aria-selected=%r / 高さが揃っている=%s(%s/%s/%s) / 狭い画面で1行=%s'
            % (default_ok, chat_ok, set_ok, back_ok, aria, same_h, h_members, h_chat, h_set, fits))


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
    share_hidden = t.eval("document.getElementById('shareWrap').hidden")
    ver_shown = t.eval("!!document.querySelector('.ver').offsetParent")   # 版はいつでも見える
    join_in = t.eval("document.getElementById('createBox').contains(document.getElementById('join'))")
    # 閉じている間の見出しは「部屋を作る」
    label_closed = t.eval("document.querySelector('#createBox > summary').innerText").strip()
    # ★枠（カード）にしないこと。開いたときのラベルと同じ**素のテキスト行**にする指定なので、
    #   背景や境界線が付いていたら崩れている（プロフィールのカードと同じ重さに見えてしまう）。
    plain = t.eval("(() => { const s = getComputedStyle(document.querySelector('#createBox > summary'));"
                   " return s.borderTopWidth === '0px' &&"
                   " (s.backgroundColor === 'rgba(0, 0, 0, 0)' || s.backgroundColor === 'transparent') })()")
    # 開けば招待リンクも出る（作る前に配る導線＝配信部屋のため）
    t.eval("document.querySelector('#createBox > summary').click()")
    opened = t.wait_for("!document.getElementById('shareWrap').hidden", timeout=SHORT)
    # ★開いたら見出しが「部屋名」ラベルに変身する（プロフィールと同じ作法・利用者の指定）。
    #   中に同じラベルを二重に置かないので、ここが崩れると部屋名の見出しが消える。
    label_open = t.eval("document.querySelector('#createBox > summary').innerText").replace('\n', ' ')
    morphed = '部屋名' in label_open and '部屋を作る' not in label_open and '部屋を作る' in label_closed
    # 共有リンクから来た人は最初から開いている
    t2 = r.open_tab(hash_='#' + urllib.parse.quote('テスト部屋'))
    from_link = t2.eval("document.getElementById('createBox').open")
    ok = (closed and idle_hidden and share_hidden and ver_shown and join_in and opened
          and morphed and plain and from_link)
    r.check('T40', '最初の画面は「名前→部屋を作る→ロビー」', ok, 'pass',
            '畳んである=%s / 状態カード非表示=%s / 招待リンク非表示=%s / 版は見える=%s / 参加ボタンが中=%s / 開くと招待リンク=%s / 見出しが部屋名に変わる=%s(%r) / 枠なしの素の行=%s / 共有リンクから来たら開く=%s'
            % (closed, idle_hidden, share_hidden, ver_shown, join_in, opened, morphed, label_open, plain, from_link))


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
    assert t0.wait_for("document.getElementById('share').href.includes('~pk~')", timeout=SHORT), '配信部屋の鍵ができない'
    hash_ = '#' + t0.eval("document.getElementById('share').href").split('#', 1)[1]
    click_join(t0)   # オーナーが参加＝部屋が開いている（リスナー側の「入口の確認」が live になる）
    assert t0.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), 'オーナーが参加できない'
    row = "document.getElementById('kindRow').hidden"
    pk = "document.getElementById('share').href.includes('~pk~')"
    lbl = "document.getElementById('joinLbl').textContent"
    # ① オーナー本人
    t1 = r.open_tab(hash_=hash_)
    owner_hidden = t1.eval(row); owner_bc = t1.eval("document.getElementById('kindBc').checked")
    owner_lbl = t1.eval(lbl)
    # ② リスナー（鍵を消す＝別の端末）
    t1.eval("localStorage.removeItem('pot-call-bcast')")
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
    # ボタンの文言：聞き役なら「リスナーとして参加する」、オーナーと改名後は「参加する」
    labels = owner_lbl == '参加する' and listener_lbl == 'リスナーとして参加する' and renamed_lbl == '参加する'
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
    ok = (checking and asleep and join_hidden and '誰もいない' in note and returned and opened and note_gone
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

    ①参加前はタグの行が出ない ②A が付けると A・B の行に「いまの話題」と付けた人 ③ロビー（第三のタブ）で
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
    cleared = b.wait_for("%s.includes('まだありません')" % TXT, timeout=SHORT)
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
    「名前、文面」で読む（URL は「URL省略」・絵文字は落ちる）。リスナー側は何も合成しない。オフで読まない。設定は localStorage に残る。

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
    saved = a.eval("localStorage.getItem('pot-call-snd-tts2') === '1'")
    note = a.eval("!document.getElementById('sndTtsNote').hidden")
    n1 = a.eval("window.__T.ttsTexts.length")
    time.sleep(1.5)
    show_tab(b, 'chat')
    b.eval("(() => { const i = document.getElementById('chatText'); i.value = 'こんにちは🎉 https://example.com/a?b=1 です'; document.getElementById('chatSend').click() })()")
    arrived = a.wait_for("window.__T.ttsTexts.length > %d" % n1, timeout=SHORT + 10)
    text = a.eval("window.__T.ttsTexts[window.__T.ttsTexts.length - 1]")
    shape = bool(text) and text.endswith('、こんにちは URL省略 です') and '🎉' not in text and 'example' not in text
    listener_quiet = b.eval("window.__T.ttsTexts.length") == 0   # 聞き役は合成しない
    # オフにすると読まない
    a.eval("document.getElementById('sndTts').click()")
    n2 = a.eval("window.__T.ttsTexts.length")
    time.sleep(2.5)   # 受信側の CHAT_ACCEPT_MS を空ける
    b.eval("(() => { const i = document.getElementById('chatText'); i.value = 'よまない'; document.getElementById('chatSend').click() })()")
    shown = a.wait_for("document.getElementById('chatLog').textContent.includes('よまない')", timeout=SHORT)
    time.sleep(1.0)
    off_quiet = shown and a.eval("window.__T.ttsTexts.length") == n2
    off_saved = a.eval("localStorage.getItem('pot-call-snd-tts2') === '0'")
    show_tab(a, 'members'); show_tab(b, 'members')
    ok = row_owner and row_listener and sample and saved and note and arrived and shape and listener_quiet and off_quiet and off_saved
    r.check('T55', '読み上げ v2：配信者だけに設定・オンで読む・URL省略・絵文字なし・名前付き・聞き役は合成しない・オフで読まない', ok, 'pass',
            '配信者に行=%s / 聞き役に行なし=%s / 見本=%s / 保存=%s / 注記=%s / 届いた=%s / 文面=%r / 聞き役は合成しない=%s / オフで読まない=%s / オフ保存=%s'
            % (row_owner, row_listener, sample, saved, note, arrived, text, listener_quiet, off_quiet, off_saved))


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
    assert o.wait_for("document.getElementById('share').href.includes('~pk~')", timeout=SHORT), '配信部屋の鍵ができない'
    h = '#' + o.eval("document.getElementById('share').href").split('#', 1)[1]
    click_join(o)
    assert o.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), '配信者が参加できない'
    keys = o.eval("localStorage.getItem('pot-call-bcast')")   # 戻るときに使う（同じブラウザなので一時的に消す）
    o.eval("localStorage.removeItem('pot-call-bcast')")   # 聞き役は鍵を持たない
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
    o.eval("localStorage.setItem('pot-call-bcast', %s)" % js_str(keys))
    o.eval("location.hash = %s" % js_str(h[1:]), await_promise=False)
    o.call('Page.reload'); time.sleep(0.5); o.wait_for(BOOTED, timeout=SHORT)
    click_join(o)
    resumed = l.wait_for("!%s.includes('戻るのを待っています') && %s.includes('配信者の声')" % (BC_NOTE, BC_NOTE), timeout=DISCOVER)
    # ④ 二度目：戻らない → 60 秒で「終了」→ 15 秒で退出
    click_leave(o)
    l.wait_for("%s.includes('戻るのを待っています')" % BC_NOTE, timeout=SHORT)
    ended = l.wait_for("%s.includes('配信が終了しました')" % BC_NOTE, timeout=75)
    left = l.wait_for("document.getElementById('tabs').hidden", timeout=25)
    told = l.eval(STATE)
    ok = listed and waiting and gone and resumed and ended and left and '配信は終了' in told
    r.check('T51', '配信者が切れたら待ち、戻らなければ配信終了でロビーへ', ok, 'pass',
            'ロビーに出る=%s / 抜けたら待つ=%s / ロビーから消える=%s / 戻れば続く=%s / 60秒で終了=%s / 自動退出=%s / 案内=%r'
            % (listed, waiting, gone, resumed, ended, left, told[:30]))


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
    """招待リンクのコピーは「部屋名 - 名前」＋改行＋URL（v0.14.7）。バナーのコピーは URL だけ"""
    x = room + 'CP'
    t = r.open_tab()
    t.eval("localStorage.setItem('pot-call-hide-help','1'); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    r.join(t, x)
    assert t.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), '参加できない'
    who = t.eval("document.getElementById('name').value") or 'ゲスト'
    url = t.eval("document.getElementById('share').href")
    t.eval("document.getElementById('copy').click()", await_promise=False)
    got = t.wait_for("__T.clip.length >= 1", timeout=SHORT)
    txt = t.eval("__T.clip[0]") or ''
    lines = txt.split('\n')
    fmt = got and len(lines) == 2 and lines[0] == '🫖 ' + x + ' - ' + who and lines[1] == url
    labeled = t.eval("document.getElementById('copy').textContent").startswith('コピーしました')
    t.eval("document.getElementById('copyTop').click()", await_promise=False)
    t.wait_for("__T.clip.length >= 2", timeout=SHORT)
    top = t.eval("__T.clip[1]") == url
    # 見えているリンクをタップ → URL だけ（v0.14.8）。同じ部屋へのリンクなので hash は変わらない
    before_hash = t.eval("location.hash")
    t.eval("document.getElementById('share').click()", await_promise=False)
    t.wait_for("__T.clip.length >= 3", timeout=SHORT)
    link_only = t.eval("__T.clip[2]") == url and t.eval("location.hash") == before_hash
    told = 'URL をコピーしました' in (t.eval("document.getElementById('shareLabel').textContent") or '')
    ok = fmt and labeled and top and link_only and told
    r.check('T54', '招待リンクのコピーは「部屋名 - 名前」＋URL（リンクのタップは URL だけ）', ok, 'pass',
            '形=%s(%r) / ボタンの文言=%s / バナーは URL だけ=%s / リンクのタップは URL だけ=%s / 案内=%s' % (fmt, txt[:60], labeled, top, link_only, told))
    click_leave(t)


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
                        " && document.getElementById('shareWrap').hidden", timeout=SHORT)
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
    # ★中断からの復帰。iOS は**音を出す他のアプリ**に切り替えると AudioContext を中断するので、
    #   通話中は定期的に resume を試みる（相手が音を手放したら戻る）。ヘッドレスでは
    #   中断を再現できないので、**見張りが動いていること**だけ固定する。
    watching = t.eval("(() => { const a = document.getElementById('appAudio');"
                      " return !!a })()")
    errs = t.eval('__T.errors')
    ok = joined and playing and tracks == 1 and not direct and watching and not errs
    r.check('T42', '音は <audio> から出す（スピーカー直結にしない）', ok, 'pass',
            '参加=%s / 出口が鳴っている=%s / 音声トラック=%s / スピーカー直結=%s / エラー=%s'
            % (joined, playing, tracks, direct, errs or 'なし'))


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
    got = tab.wait_for("document.getElementById('share').textContent.includes('~pk~')", timeout=SHORT)
    click_join(tab)
    return got


# 配信部屋まわりで繰り返し見る値
SPEAK_SHOWN = "getComputedStyle(document.getElementById('speak')).display !== 'none'"
MUTE_SHOWN = "getComputedStyle(document.getElementById('mute')).display !== 'none'"
BC_NOTE = "document.getElementById('bcNote').textContent"
LIVE_TRACKS = "__T.streams.flatMap(s => s.getTracks()).filter(t => t.readyState === 'live').length"


def T26(r, room):
    """配信部屋：リンクに指紋が乗り、部屋名の欄には名前だけが残る

    部屋IDは `名前~pk~指紋`。**入力欄に指紋を出さない**のが要点で、出すと文字数カウンタの
    意味が壊れるうえ、うっかり消されて「同じ名前の別の部屋」に入ってしまう。
    """
    t = r.open_tab()
    name = room + 'BC'
    made = make_bcast(r, t, name)
    url = t.eval("document.getElementById('share').textContent")
    kept = t.eval("document.getElementById('room').value")
    joined = t.wait_for("location.hash.includes('~pk~')", timeout=DISCOVER)
    note = t.eval(BC_NOTE)
    # 指紋は base64url 22文字ちょうど（SHA-256 の先頭16バイト）
    # v0.13.0：リンクは `#room=ハッシュ~pk~指紋&name=名前`。指紋は room= の末尾（& の前）
    room_id = url.split('#room=')[1].split('&')[0] if '#room=' in url else ''
    fp = room_id.split('~pk~')[-1] if '~pk~' in room_id else ''
    ok = made and joined and kept == name and len(fp) == 22 and 'あなたの配信部屋' in note
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
    a.eval("window.__a_bcast_key = localStorage.getItem('pot-call-bcast')")
    b.eval("localStorage.removeItem('pot-call-bcast')")
    click_join(b)
    met = a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
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
    # 配信部屋は**タブが4つ**＝いちばん混む。狭い画面でも1行に収まること
    narrow = []
    for w in (390, 360, 320):
        a.call('Emulation.setDeviceMetricsOverride', width=w, height=900, deviceScaleFactor=2, mobile=True)
        time.sleep(0.3)
        narrow.append(a.eval(TAB_FITS))
    a.call('Emulation.clearDeviceMetricsOverride')
    ok = met and heard and gum == 0 and quiet and ui and mark and in_lobby and all(narrow)
    r.check('T27', '配信部屋：聞き役はマイクを取らず配信者の声だけが流れる', ok, 'pass',
            '2人=%s / 聞き役に音=%s / 聞き役のgetUserMedia=%s回 / 配信者側に音は無い=%s / ボタン非表示=%s / 📣=%s / ロビーに📣付きで出る=%s / 狭い画面でタブが1行=%s'
            % (met, heard, gum, quiet, ui, mark, in_lobby, narrow))
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
    granted = a.eval("(() => { const g = document.querySelector('#members .member:not(:first-child) .m-mic');"
                     " if (g) g.click(); return !!g })()")
    shown = b.wait_for(SPEAK_SHOWN, timeout=SHORT)
    b.eval("document.getElementById('speak').click()", await_promise=False)
    spoke = b.wait_for('__T.gum === 1', timeout=SHORT) and a.wait_for('%s === 1' % AUDIOS, timeout=DISCOVER)
    mute_now = b.eval(MUTE_SHOWN) and not b.eval(SPEAK_SHOWN)   # マイクを持ったらミュートに入れ替わる
    ok = granted and shown and spoke and mute_now
    r.check('T27b', '登壇：許可されると話せる（押すまでマイクは入らない）', ok, 'pass',
            '許可した=%s / ボタンが出た=%s / 声が届いた=%s / ミュートに入れ替わった=%s'
            % (granted, shown, spoke, mute_now))

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
    a.eval("if (window.__a_bcast_key) localStorage.setItem('pot-call-bcast', window.__a_bcast_key)")
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
    a_key = a.eval("localStorage.getItem('pot-call-bcast')")
    b.eval("localStorage.removeItem('pot-call-bcast')")
    b.eval("document.getElementById('retry').click()")
    b_joined = b.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER)
    # その直後に配信者 a もつなぎ直す（配信者の peerId が変わり ownerChanged が発生）
    if a_key:
        a.eval("localStorage.setItem('pot-call-bcast', %s)" % js_str(a_key))
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
        t.eval("localStorage.removeItem('pot-call-bcast')")   # ★同じブラウザなので鍵を消して聞き役にする
        click_join(t)
    if not a.wait_for('%s === 3' % MEMBERS, timeout=DISCOVER):
        r.check('T31', 'おたよりが枠主に届き、選ばれると部屋に出る', False, 'pass',
                '3人そろわなかった（%s人）' % a.eval(MEMBERS))
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
    # 閉じても行は残る（見失わない）／行を押すと開き直せる
    b.eval("document.getElementById('mailPopClose').click()")
    closed = b.eval("document.getElementById('mailPop').hidden") and not b.eval("document.getElementById('mailNow').hidden")
    b.eval("document.getElementById('mailNow').click()")
    reopened = b.eval("!document.getElementById('mailPop').hidden")
    r.check('T31d', '公開されたお便りが紙で大きく出る（後ろは触れる）', popped and passthru and closed and reopened, 'pass',
            '紙が出た=%s / 後ろを塞いでいない=%s / 閉じても行は残る=%s / 押すと開き直せる=%s'
            % (popped, passthru, closed, reopened))
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
    d.eval("localStorage.removeItem('pot-call-bcast')")
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
    b.eval("localStorage.removeItem('pot-call-bcast')")
    click_join(b)
    met = b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    heard = b.wait_for('%s === 1' % AUDIOS, timeout=DISCOVER)
    # 名簿が届いていれば配信者の行に 📣 が出る（＝誰が配信者かまで分かっている）
    mark = b.eval("[...document.querySelectorAll('#members .m-bc')].some(e => e.textContent === '📣')")
    note = b.eval(BC_NOTE)
    ok = met and heard and mark and '配信者の声だけが流れます' in note
    r.check('T32', '配信部屋に後から入っても配信者の声が届く', ok, 'pass',
            '2人=%s / 声が届いた=%s / 配信者が分かる=%s / 説明=%r' % (met, heard, mark, note[:24]))
    for t in (a, b):
        click_leave(t)


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
    assert o.wait_for("document.getElementById('share').href.includes('~pk~')", timeout=SHORT), '配信部屋の鍵ができない'
    h = '#' + o.eval("document.getElementById('share').href").split('#', 1)[1]
    click_join(o)
    assert o.wait_for("!document.getElementById('tabs').hidden", timeout=DISCOVER), '配信者が参加できない'
    # 聞き役は別の端末＝鍵を持たない（localStorage は同じブラウザの全タブで共有されるので消す。配信者は参加済み）
    o.eval("localStorage.removeItem('pot-call-bcast')")
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
    three = a.wait_for('%s === 3' % MEMBERS, timeout=DISCOVER) and b.wait_for('%s === 3' % MEMBERS, timeout=DISCOVER)
    click_leave(o)   # 配信者が去る
    met = a.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER) and b.wait_for('%s === 2' % MEMBERS, timeout=DISCOVER)
    time.sleep(30)   # 自動復帰の判定（25秒）を越えて観測する
    gum = a.eval('__T.gum') + b.eval('__T.gum')
    audios = a.eval(AUDIOS) + b.eval(AUDIOS)
    stayed = a.eval('window.__stay === 1') and b.eval('window.__stay === 1')
    note = a.eval(BC_NOTE)
    ok = three and met and gum == 0 and audios == 0 and stayed and '配信者' in note and row_gone
    r.check('T28', '配信者が去った配信部屋では誰の音も流れない', ok, 'pass',
            '3人=%s / 配信者が去って2人=%s / getUserMedia=%s回 / 音声要素=%s / リロードされていない=%s / 説明=%r / 部屋の種類の2択を出さない=%s'
            % (three, met, gum, audios, stayed, note[:24], row_gone))
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
    warned = tabs[0].eval("!document.getElementById('crowdNote').hidden")
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
        r.check('T9', 'esm.sh 遮断時の起動', False, 'pass', 'ページが起動しなかった')
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
    WARN_SHOWN = "!document.getElementById('audioWarn').hidden"
    appeared = a.wait_for(WARN_SHOWN, timeout=32) or b.wait_for(WARN_SHOWN, timeout=1)
    r.check('T13', '正常な通話で誤警告を出さない', not appeared, 'pass',
            '' if not appeared else a.eval("document.getElementById('audioWarn').textContent"))


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
    # QR は押されるまで qrcode-generator を読みに行かない＝ここまでの流れでは esm.sh への
    # 読み込みが1本ぶん試されていない。CSP の検査の前に通しておく。
    a.eval("document.getElementById('qr').click()", await_promise=False)
    qr = a.wait_for("!!document.querySelector('#qrImg svg')", timeout=SHORT)
    r.check('T21b', 'QRコードが生成できる（esm.sh への読み込みが通る）', qr, 'pass',
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
            if run('T27b'):
                T27b(r, bc_a, bc_b)
        if run('T31'):
            T31(r, room)
        if run('T32'):
            T32(r, room)
        if run('T28'):
            T28(r, room)
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
