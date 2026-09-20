#!/usr/bin/env python3
"""公開 Nostr リレーが「いま実際に受け付けるか」を測る（ぽっと通話のシグナリング用・git未追跡）

    python3 test/relaycheck.py test/relays.json     # 候補一覧（JSONの配列）を渡す

**リレー一覧は放っておくと必ず腐る。**2026-08-14 に測ったときは、当時使っていた5本のうち
4本が死んでいた（rate-limit / PoW要求 / 接続不可）。半年ごとに回すこと。
候補は Trystero の既定47本を種にするとよい（バンドルの `defaultRelayUrls` を取り出す）。


Trystero 0.25.2 が出すのと同じ形のイベントを投げて、OK が返るかを見る。
  kind    = ハッシュ由来の 20000〜29999（NIP-16 ephemeral＝リレーは保存しない）
  tags    = [["x", <topic>]]
  pubkey  = 実行ごとの使い捨て鍵（Trystero もページ毎に keygen している）
NIP-11 の自己申告は当てにならない（PoW不要と書いてあるリレーが 28bit 要求してくる）
ので、**実際に投げた結果だけ**を判定に使う。
"""
import concurrent.futures as cf
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import time
from base64 import b64encode

# ───────── secp256k1 / BIP-340 schnorr（純Python・依存なし） ─────────
P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and p1[1] != p2[1]:
        return None
    if p1 == p2:
        lam = 3 * p1[0] * p1[0] * pow(2 * p1[1], P - 2, P) % P
    else:
        lam = (p2[1] - p1[1]) * pow(p2[0] - p1[0], P - 2, P) % P
    x3 = (lam * lam - p1[0] - p2[0]) % P
    return (x3, (lam * (p1[0] - x3) - p1[1]) % P)


def _mul(p, n):
    r = None
    for i in range(256):
        if (n >> i) & 1:
            r = _add(r, p)
        p = _add(p, p)
    return r


def _tagged(tag, msg):
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def schnorr_sign(msg, sec):
    d0 = int.from_bytes(sec, 'big')
    Pt = _mul(G, d0)
    d = d0 if Pt[1] % 2 == 0 else N - d0
    aux = os.urandom(32)
    t = d ^ int.from_bytes(_tagged('BIP0340/aux', aux), 'big')
    px = Pt[0].to_bytes(32, 'big')
    k0 = int.from_bytes(_tagged('BIP0340/nonce', t.to_bytes(32, 'big') + px + msg), 'big') % N
    R = _mul(G, k0)
    k = k0 if R[1] % 2 == 0 else N - k0
    rx = R[0].to_bytes(32, 'big')
    e = int.from_bytes(_tagged('BIP0340/challenge', rx + px + msg), 'big') % N
    return rx + ((k + e * d) % N).to_bytes(32, 'big')


def keygen():
    while True:
        sec = os.urandom(32)
        if 0 < int.from_bytes(sec, 'big') < N:
            Pt = _mul(G, int.from_bytes(sec, 'big'))
            return sec, Pt[0].to_bytes(32, 'big').hex()


# ───────── 最小 WebSocket クライアント ─────────
class WS:
    def __init__(self, host, path, timeout=12):
        self.sock = socket.create_connection((host, 443), timeout)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(self.sock, server_hostname=host)
        self.sock.settimeout(timeout)
        key = b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\nUser-Agent: potalk-relay-probe\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            c = self.sock.recv(4096)
            if not c:
                raise IOError('closed during handshake')
            buf += c
        head, _, rest = buf.partition(b'\r\n\r\n')
        if b'101' not in head.split(b'\r\n')[0]:
            raise IOError('upgrade refused: ' + head.split(b'\r\n')[0].decode('latin1')[:60])
        self.buf = rest

    def send(self, text):
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = struct.pack('!BB', 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack('!BBH', 0x81, 0x80 | 126, n)
        else:
            hdr = struct.pack('!BBQ', 0x81, 0x80 | 127, n)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _need(self, n):
        while len(self.buf) < n:
            c = self.sock.recv(65536)
            if not c:
                raise IOError('closed')
            self.buf += c

    def recv(self):
        self._need(2)
        b0, b1 = self.buf[0], self.buf[1]
        ln = b1 & 0x7F
        off = 2
        if ln == 126:
            self._need(4); ln = struct.unpack('!H', self.buf[2:4])[0]; off = 4
        elif ln == 127:
            self._need(10); ln = struct.unpack('!Q', self.buf[2:10])[0]; off = 10
        self._need(off + ln)
        payload = self.buf[off:off + ln]
        self.buf = self.buf[off + ln:]
        op = b0 & 0x0F
        if op == 8:
            raise IOError('server closed frame')
        if op in (9, 10):
            return None
        return payload.decode('utf-8', 'replace')

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ───────── 1本ぶんの試験 ─────────
def topic_hash(s, mod):
    return sum(ord(c) for c in s) % mod


def probe(host, timeout=12):
    t0 = time.time()
    path = '/'
    if '/' in host:
        host, _, tail = host.partition('/')
        path = '/' + tail
    try:
        ws = WS(host, path, timeout)
    except Exception as e:
        return {'ok': False, 'stage': 'connect', 'msg': f'{type(e).__name__}: {str(e)[:60]}',
                'ms': int((time.time() - t0) * 1000)}
    try:
        sec, pub = keygen()
        topic = 'potalk-probe-' + os.urandom(4).hex()
        kind = topic_hash(topic, 10000) + 20000
        ev = {'kind': kind, 'tags': [['x', topic]], 'created_at': int(time.time()),
              'content': 'probe', 'pubkey': pub}
        ser = json.dumps([0, ev['pubkey'], ev['created_at'], ev['kind'], ev['tags'], ev['content']],
                         separators=(',', ':'), ensure_ascii=False)
        eid = hashlib.sha256(ser.encode()).digest()
        ev['id'] = eid.hex()
        ev['sig'] = schnorr_sign(eid, sec).hex()
        ws.send(json.dumps(['EVENT', ev], separators=(',', ':')))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception as e:
                return {'ok': False, 'stage': 'read', 'msg': f'{type(e).__name__}: {str(e)[:60]}',
                        'ms': int((time.time() - t0) * 1000)}
            if raw is None:
                continue
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if m and m[0] == 'OK' and m[1] == ev['id']:
                return {'ok': bool(m[2]), 'stage': 'publish',
                        'msg': (m[3] if len(m) > 3 else '')[:70],
                        'ms': int((time.time() - t0) * 1000)}
            if m and m[0] == 'NOTICE':
                return {'ok': False, 'stage': 'notice', 'msg': str(m[1])[:70],
                        'ms': int((time.time() - t0) * 1000)}
        return {'ok': False, 'stage': 'timeout', 'msg': 'OK が返らない',
                'ms': int((time.time() - t0) * 1000)}
    finally:
        ws.close()


if __name__ == '__main__':
    hosts = json.load(open(sys.argv[1]))
    out = {}
    with cf.ThreadPoolExecutor(12) as ex:
        futs = {ex.submit(probe, h): h for h in hosts}
        for f in cf.as_completed(futs):
            h = futs[f]
            try:
                out[h] = f.result()
            except Exception as e:
                out[h] = {'ok': False, 'stage': 'crash', 'msg': str(e)[:60], 'ms': 0}
    json.dump(out, open('probe_result.json', 'w'), ensure_ascii=False)
    good = sorted([h for h, v in out.items() if v['ok']], key=lambda h: out[h]['ms'])
    print(f"受理された: {len(good)} / {len(hosts)}\n")
    print("■ OK（速い順）")
    for h in good:
        print(f"  {out[h]['ms']:>6}ms  {h}")
    print("\n■ NG")
    for h, v in sorted(out.items(), key=lambda kv: kv[0]):
        if not v['ok']:
            print(f"  [{v['stage']}] {h} — {v['msg']}")
