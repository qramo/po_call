# ptt/ — ぽちっと通話（押している間だけ話せる部屋）

[riryo3/pochittalk](https://github.com/riryo3/pochittalk)（リョータさん・MIT・`LICENSE`）の Web 版を、potalk.app/ptt/ から配るための置き場。
**中身はリョータさんの worker.js の HTML そのまま**で、下の置き換えだけを `sync.mjs` で当てる（手で直さない）。
同じ appId（`pochitalk-v2`）・同じ部屋 ID（`h-<8桁>`）・同じ在室の知らせ（Nostr kind 29334）なので、
**ここで入った部屋と、リョータさんのページで入った部屋はつながる**（同じ網）。

## 作り直し方

```bash
gh api repos/riryo3/pochittalk/contents/worker.js --jq .download_url | xargs curl -sL -o /tmp/worker.js
node ptt/sync.mjs /tmp/worker.js     # index.html・favicon.svg・favicon.png・apple-touch-icon.png・og.png を書き出す
```

置き換えが 1 つでも当たらなければ止まる（上流が変わった合図）。そのときは `sync.mjs` の置き換えを直す。

| 置き換え | 理由 |
| :--- | :--- |
| Trystero・@noble/curves・qrcode-generator を `../vendor/` から読む | 本体と同じく、第三者の CDN（esm.sh）のコードを動かさない。Trystero は同じ 0.25.4（通信の形は同じ）。向こうは数字を 3 つ変えた版（`trystero-0.25.4-p9.mjs`：先に作っておく接続 20→0・待ち時間 2 つ）を使うが、ここは公開版のまま（`vendor/` は手で直さない決まり）。版表記に「Trystero 0.25.4 公開版」と出す |
| TURN の資格情報を本体の Worker（pot-turn）から取る | 向こうの `/api/turn` はこのドメインに無い。Worker は資格情報を渡すだけ（CLAUDE.md の法的な前提のまま） |
| CSP を足す | つなぐ先を TURN の Worker と Nostr リレーに絞る |
| 名前の初期値に本体の名前（`pot-call-profile`）を使う | ぽっと通話の人が、同じ名前で続けられるように。ぽちっと通話の名前がまだ無いときだけ |
| アイコン・OGP・版表記・プライバシーの説明 | 置き場所（GitHub Pages・potalk.app/ptt/）に合わせる |
| 「保存データを消す」は `pochitalk_` で始まるものだけ消す | 向こうはサイト全体（localStorage・IndexedDB・キャッシュ）を消す。このドメインでそれをやると、ぽっと通話の名前と、**作り直せない放送の鍵（IndexedDB `pot-call-keys`）まで消える** |

使っていないもの：おうむ返しのボット（`/bot`）はリョータさんの側で動いているので、ここには置かない（同じ網なので一覧には出る）。

## 本体との関係

- 本体（`../index.html`）のロビーは、kind 29334 の在室を読んで、ぽちっと通話の部屋を 🔘 で出す（タップで `ptt/?r=<部屋>`）。本体からは何も送らない。
- 部屋の番号は英数字 8 桁（`[0-9a-z]{8}`。z12q39 より前は 16 進の 8 桁）。本体のロビーも同じ形で受ける。
- TURN は本体と同じく、既定で強制中継（設定でオフにした端末だけ直接を優先。z12q41 時点）。
- 手元で試すときは `localhost:8000` で開く。pot-turn は `localhost:8010`（smoke の番地）を通さないので、TURN の資格情報が取れず、入室と同時に取ったマイクが捨てられて、送信ボタンを押すまで音が出ない（向こうの作り。本番では読み込み時に資格情報が取れているので起きにくい）。
