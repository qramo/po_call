#!/usr/bin/env python3
"""ぽっと通話 ローカルテスト用サーバ（git 未追跡・本番には載せない）

`python3 -m http.server` の代わりにこれを使う。違いは1つだけ：

    index.html を配信するときに appId を書き換えて、本番のロビーから隔離する。

これが無いと、ローカルで開いた2タブが本番と同じ `__lobby__` に入り、
テスト用の部屋名が本番のロビー一覧に出て、公開 Nostr リレーにも publish される。
ファイル自体は書き換えないので、index.html を普通に編集しながら使える。

    python3 test/serve.py              # http://localhost:8000（appId は …-dev）
    python3 test/serve.py --tag t1     # 別の隔離空間（…-t1）で並行して試す
    python3 test/serve.py --prod       # 書き換えなし＝本番と同じ appId（最終確認用）

ポート 8000 は固定に近い：TURN Worker の CORS が `http://localhost:8000` しか
許可していないので、他のポートだと TURN が取れず relay-first が効かない。
（`127.0.0.1:8000` も別オリジン扱いで弾かれる。必ず `localhost` で開くこと）
"""

import argparse
import functools
import http.server
import os
import socketserver
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ID_SRC = "appId: 'kuramo-webrtc-call'"
# appId の書き換えだけでは Nostr 直publish は隔離できない（部屋名のハッシュも
# リレー一覧も appId と無関係なので、テスト部屋の在室が公開リレーへ流れてしまう）。
# テスト時はリレー一覧を空にして publish 先を無くす。
NOSTR_SRC = 'const NOSTR_RELAYS = TRYSTERO_BASE.relayConfig.urls'
NOSTR_DST = 'const NOSTR_RELAYS = []   /* テスト中は公開リレーへ流さない */'


class Handler(http.server.SimpleHTTPRequestHandler):
    tag = 'dev'

    def _patched_index(self):
        path = os.path.join(ROOT, 'index.html')
        with open(path, 'rb') as f:
            body = f.read()
        if self.tag:
            src = APP_ID_SRC.encode()
            if src not in body:
                # appId の書き方が変わった＝隔離が効いていない。黙って本番ロビーに
                # 出るのが一番まずいので、配信せずに止める。
                self.send_error(500, 'appId not found in index.html; update APP_ID_SRC in test/serve.py')
                return None
            dst = APP_ID_SRC.replace('kuramo-webrtc-call', 'kuramo-webrtc-call-' + self.tag).encode()
            body = body.replace(src, dst)
            if NOSTR_SRC.encode() not in body:
                self.send_error(500, 'NOSTR_RELAYS not found; update NOSTR_SRC in test/serve.py')
                return None
            body = body.replace(NOSTR_SRC.encode(), NOSTR_DST.encode())
        return body

    def do_GET(self):
        if self.path.split('?')[0] in ('/', '/index.html'):
            body = self._patched_index()
            if body is None:
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')   # 編集がリロードで必ず反映されるように
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass   # アクセスログは黙らせる（テスト出力を汚さない）


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--tag', default='dev', help="appId の接尾辞（既定 dev）")
    ap.add_argument('--prod', action='store_true', help='appId を書き換えない（本番と同じロビーに出る）')
    a = ap.parse_args()

    Handler.tag = '' if a.prod else a.tag
    handler = functools.partial(Handler, directory=ROOT)
    with Server(('127.0.0.1', a.port), handler) as httpd:
        where = '本番と同じ appId（ロビーに出ます）' if a.prod else "appId: kuramo-webrtc-call-" + a.tag
        print('http://localhost:%d/   %s' % (a.port, where))
        print('（127.0.0.1 ではなく localhost で開くこと。TURN の CORS がそれしか許可していない）')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nbye')


if __name__ == '__main__':
    sys.exit(main())
