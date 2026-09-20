#!/usr/bin/env python3
"""Chrome DevTools Protocol の最小クライアント（標準ライブラリのみ・git 未追跡）

このマシンには node が無く、pip の websockets も入っていない。テストのために
パッケージマネージャを持ち込むのはこのプロジェクトの方針（ビルド無し・依存無し）と
合わないので、必要な範囲の WebSocket(RFC 6455) だけ自前で実装している。

必要な範囲＝「localhost の Chrome に平文で繋いでテキストフレームをやりとりする」だけ。
TLS も圧縮も断片化もサーバ側マスクも扱わない（CDP は使わない）。
"""

import base64
import json
import os
import socket
import struct
import time
import urllib.request


class WSError(Exception):
    pass


class WebSocket:
    """クライアント側だけの最小 WebSocket。ws:// のみ・テキストフレームのみ。"""

    def __init__(self, url, timeout=30):
        if not url.startswith('ws://'):
            raise WSError('ws:// only: ' + url)
        rest = url[len('ws://'):]
        hostport, _, path = rest.partition('/')
        path = '/' + path
        host, _, port = hostport.partition(':')
        port = int(port or 80)

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b''

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            'GET %s HTTP/1.1\r\n'
            'Host: %s\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            'Sec-WebSocket-Key: %s\r\n'
            'Sec-WebSocket-Version: 13\r\n\r\n'
        ) % (path, hostport, key)
        self.sock.sendall(req.encode())

        head = self._read_until(b'\r\n\r\n')
        if b' 101 ' not in head.split(b'\r\n')[0]:
            raise WSError('handshake failed: ' + head.split(b'\r\n')[0].decode('latin1'))

    # ── 低レベル ──────────────────────────────────────────────
    def _read_until(self, marker):
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError('closed during handshake')
            self.buf += chunk
        i = self.buf.index(marker) + len(marker)
        head, self.buf = self.buf[:i], self.buf[i:]
        return head

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise WSError('connection closed')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        # クライアント→サーバは必ずマスクする（RFC 6455 5.3）
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        if n < 126:
            header = struct.pack('!BB', 0x80 | opcode, 0x80 | n)
        elif n < (1 << 16):
            header = struct.pack('!BBH', 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack('!BBQ', 0x80 | opcode, 0x80 | 127, n)
        self.sock.sendall(header + mask + masked)

    def send(self, text):
        self._send_frame(0x1, text.encode())

    def recv(self):
        """テキストを1つ返す。ping には自動で pong を返し、次を待つ。"""
        while True:
            b0, b1 = struct.unpack('!BB', self._read(2))
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack('!H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self._read(8))[0]
            if b1 & 0x80:                      # サーバ側マスクは仕様上来ない
                mask = self._read(4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(n)))
            else:
                data = self._read(n)
            if opcode == 0x1:
                return data.decode()
            if opcode == 0x8:
                raise WSError('server closed')
            if opcode == 0x9:
                self._send_frame(0xA, data)    # ping → pong
            # 0xA(pong) / 0x2(binary) / 0x0(継続) は無視でよい

    def close(self):
        try:
            self._send_frame(0x8, b'')
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class Tab:
    """1タブぶんの CDP セッション。call() は同期・イベントは貯めておく。"""

    def __init__(self, ws_url, timeout=30):
        self.ws = WebSocket(ws_url, timeout=timeout)
        self.next_id = 0
        self.events = []

    def call(self, method, **params):
        self.next_id += 1
        mid = self.next_id
        self.ws.send(json.dumps({'id': mid, 'method': method, 'params': params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get('id') == mid:
                if 'error' in msg:
                    raise WSError('%s: %s' % (method, msg['error'].get('message')))
                return msg.get('result', {})
            if 'method' in msg:
                self.events.append(msg)

    def eval(self, expr, await_promise=True):
        """ページ内で式を評価して値を返す。例外や reject は WSError にする。"""
        r = self.call('Runtime.evaluate', expression=expr,
                      returnByValue=True, awaitPromise=await_promise)
        if r.get('exceptionDetails'):
            d = r['exceptionDetails']
            desc = (d.get('exception') or {}).get('description') or d.get('text')
            raise WSError('eval failed: %s\n  expr: %s' % (desc, expr[:120]))
        return r.get('result', {}).get('value')

    def wait_for(self, expr, timeout=30, interval=0.25, label=None):
        """式が truthy になるまで待つ。なったら True、時間切れなら False。"""
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                last = self.eval(expr)
            except WSError:
                last = None          # 読み込み途中で要素が無い等は待ちの一部
            if last:
                return True
            time.sleep(interval)
        return False

    def close(self):
        self.ws.close()


def http_json(port, path, method='GET'):
    url = 'http://127.0.0.1:%d%s' % (port, path)
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode()
    return json.loads(body) if body.strip() else {}


def wait_for_devtools(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            return http_json(port, '/json/version')
        except Exception:
            time.sleep(0.2)
    raise WSError('DevTools port %d に繋がりません' % port)


def new_tab(port, url='about:blank', timeout=30):
    """新しいタブを開いて Tab を返す。新しめの Chrome は PUT を要求する。"""
    q = '/json/new?' + urllib.request.quote(url, safe=':/?&=#%')
    try:
        info = http_json(port, q, method='PUT')
    except Exception:
        info = http_json(port, q, method='GET')
    return Tab(info['webSocketDebuggerUrl'], timeout=timeout)
