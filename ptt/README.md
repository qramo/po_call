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
| Trystero・@noble/curves・qrcode-generator を `../vendor/` から読む | 本体と同じく、第三者の CDN（esm.sh）のコードを動かさない。Trystero は同じ 0.25.4（通信の形は同じ） |
| TURN の資格情報を本体の Worker（pot-turn）から取る | 向こうの `/api/turn` はこのドメインに無い。Worker は資格情報を渡すだけ（CLAUDE.md の法的な前提のまま） |
| CSP を足す | つなぐ先を TURN の Worker と Nostr リレーに絞る |
| 名前の初期値に本体の名前（`pot-call-profile`）を使う | ぽっと通話の人が、同じ名前で続けられるように。ぽちっと通話の名前がまだ無いときだけ |
| アイコン・OGP・版表記・プライバシーの説明 | 置き場所（GitHub Pages・potalk.app/ptt/）に合わせる |

使っていないもの：おうむ返しのボット（`/bot`）はリョータさんの側で動いているので、ここには置かない（同じ網なので一覧には出る）。

## 本体との関係

- 本体（`../index.html`）のロビーは、kind 29334 の在室を読んで、ぽちっと通話の部屋を 🔘 で出す（タップで `ptt/?r=<部屋>`）。本体からは何も送らない。
- 本体の TURN は IP を見せない強制中継だが、ぽちっと通話は「つながらないときだけ中継」（`?relay` で強制）。同じ部屋の人に IP が見えることがある（向こうのプライバシーの説明のとおり）。
