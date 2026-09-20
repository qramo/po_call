#!/usr/bin/env python3
"""Stage 5 の移行漏れ検出（git 未追跡）

`call` に移した名前が、素のまま残っていないかを調べる。**行単位で「call. を含むから
OK」と判断してはいけない**：同じ行に移行済みと未移行が混在していると見逃す
（実際にそれで `onPeerJoin` の `warn = ''` を取りこぼし、ReferenceError で
入室音と状態表示が死んだ）。ここでは名前の直前が `call.` かどうかを1件ずつ見る。

    python3 test/check_migration.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOVED = ['myStream', 'usingRelay', 'wantWake', 'wakeLock', 'profileAction', 'nudgeAction',
         'audioWatch', 'peerSince', 'nudges', 'warn', 'lastRoom', 'growTimer',
         'soundsArmed', 'soundsTimer', 'leaving', 'joinedId', 'joinedAt', 'muted', 'room',
         'peers', 'audios', 'profiles']
# 受信データ側に同じ名前のプロパティがある名前（`muted: v.muted` の左側＝キーは別物なので数えない）。
# キーの位置だけを除くので、`muted ? a : b` のような素の参照は今までどおり捕まる。
DATA_KEYS = ('muted', 'room')
# `room` だけは**同名の仮引数・ローカルが正当に存在する**（selectRoom / push / roomSeconds /
# roomHash / renderLobby の forEach / publishPresence）。移してはいけないので行の文面ごと許す。
# **これらの行を編集したらここも直すこと**。ずれたら再検査させる作り（黙って通さないため）。
SHADOWED_ROOM = {
    'const roomHash = async room => {',
    "const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode('potto:' + room))",
    'const room = call.joinedId',
    'const d = await roomHash(room)',
    'if (room !== call.joinedId) return',
    'const roomSeconds = (room, people) => {',
    'const kept = Math.max(live, roomFloor.get(room) || 0)',
    'roomFloor.set(room, kept)',
    'const push = (room, p) => {',
    'let a = byRoom.get(room)',
    'if (!a) byRoom.set(room, a = [])',
    'names.slice(0, LOBBY_MAX_ROOMS).forEach(room => {',
    'const people = byRoom.get(room)',
    "btn.className = 'room' + (room === call.joinedId ? ' here' : '') + (act ? ' act' + act : '')",
    "const nm = document.createElement('span'); nm.className = 'room-name'; nm.textContent = '🏠 ' + room",
    "durText.textContent = '⏱ ' + fmtDuration(roomSeconds(room, people))",
    'btn.onclick = () => selectRoom(room)',
    'const selectRoom = room => {',
    'if (room === call.joinedId) return',
    r"if (!confirm('参加中の部屋「' + call.joinedId + '」を退出して「' + room + '」に移りますか？\nミュート状態で入室します。')) return",
    r"if (!confirm('「' + room + '」に参加しますか？\nミュート状態で入室します。')) return",
    'roomInput.value = room; syncRoomCount(); syncShare()',
}

all_lines = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read().splitlines()
# CSS のクラス名（.audio-warn）や HTML の属性まで拾わないよう、スクリプト部分だけを見る
start = next(i for i, l in enumerate(all_lines) if '<script type="module">' in l)
src = all_lines[start:]
# call オブジェクトのプロパティ定義（`  name: ...`）と、ブラウザAPI・別名は除く
PROP = re.compile(r'^\s{2}\w+:')
SKIP = ('navigator.wakeLock', 'lastRoomSound', 'leaveCall')

bad = []
for i, line in enumerate(src, start + 1):
    code = line.split('//')[0]          # 行末コメントは対象外（実害がないため）
    # スプレッド `[...audios.values()]` は素の参照だが、直前が `.` なので下の後読みに
    # 弾かれて**見逃していた**。長さを変えずに潰しておく（`.foo` の除外は保ったまま）。
    code = code.replace('...', '   ')
    if PROP.match(line):
        continue
    for name in MOVED:
        if name == 'room' and code.split('//')[0].strip() in SHADOWED_ROOM:
            continue
        tail = r'\b(?![\'"])' + (r'(?!\s*:)' if name in DATA_KEYS else '')
        for m in re.finditer(r'(?<![-.\w$"\'])' + name + tail, code):
            ctx = code[max(0, m.start() - 12):m.end() + 12]
            if any(sk in ctx for sk in SKIP):
                continue
            bad.append((i, name, line.strip()[:96]))

if bad:
    print('❌ 素のまま残っている参照 %d 件' % len(bad))
    for i, name, line in bad:
        print('  %4d  %-14s %s' % (i, name, line))
    sys.exit(1)
print('✅ 移行漏れなし（%d 個の名前を検査）' % len(MOVED))
