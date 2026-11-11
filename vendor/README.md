# vendor/ — 同梱している外部ライブラリ（v0.16.2〜）

esm.sh から実行時に読み込んでいたライブラリを、**同じ中身のまま**ここに置いて自分のドメインから配る。
理由：第三者 CDN が侵害・再ビルドされても、全利用者のページで知らないコードが動かないようにする（脅威モデル S1）。
配信鍵・名刺の鍵を扱うページなので、読み込むコードは固定する。

| ファイル | 元 | 使う所 |
| :--- | :--- | :--- |
| `trystero-0.25.4.mjs` | `https://esm.sh/trystero@0.25.4?bundle` が指す `/trystero@0.25.4/es2022/trystero.bundle.mjs` | 参加（②ライブラリの読み込み）・ロビー |
| `trystero-0.25.4-pool0.mjs` | **〈β だけの試験・2026-11-11〉** 上の `trystero-0.25.4.mjs` の数字 1 つだけを変えた写し：`sed 's/lo=20,Ft=class/lo=0,Ft=class/'`（先に作っておく接続の数 20→0＝必要になってから作る。リョータさんの提案）。「中身は変えない」の決まりの例外なので、別名で置き、**本番（main）には入れない**。やめるときはこのファイル・この行・SHA256SUMS の行・`index.html` の 2 か所を戻す | β の参加・ロビー |
| `nostr-tools-2.10.4.mjs` | `https://esm.sh/nostr-tools@2.10.4?bundle` が指す `/nostr-tools@2.10.4/es2022/nostr-tools.bundle.mjs` | 在室の公開・名刺の Nostr の確認 |
| `qrcode-generator-1.4.4.mjs` | `https://esm.sh/qrcode-generator@1.4.4?bundle` が指す `/qrcode-generator@1.4.4/es2022/qrcode-generator.bundle.mjs` | 招待の QR |
| `noble-curves-1.4.0-secp256k1.mjs` | `https://esm.sh/@noble/curves@1.4.0/secp256k1?bundle` が指す `/@noble/curves@1.4.0/es2022/secp256k1.bundle.mjs` | `ptt/`（ぽちっと通話）の在室の知らせの署名 |
| `piper-plus-0.7.0/` | npm `piper-plus@0.7.0` の tarball の `src/`（全部）と `dist/rust-wasm/piper_plus_wasm.js`・`piper_plus_wasm_bg.wasm`（60MB） | 読み上げ（v0.16.14〜） |
| `piper-plus-g2p-0.4.2/` | npm `@piper-plus/g2p@0.4.2` の tarball の `src/`（全部）と `data/sv_function_words.json` | 読み上げ（piper-plus が import） |
| `onnxruntime-web-1.30.0/dist/` | npm `onnxruntime-web@1.30.0` の tarball の `dist/ort.wasm.min.mjs`・`ort-wasm-simd-threaded.mjs`・`ort-wasm-simd-threaded.wasm`（14MB） | 読み上げ（推論） |

- **中身は 1 バイトも変えない**。`SHA256SUMS` と一致することを `test/smoke.py` の T76 が確かめる（`shasum -a 256 -c SHA256SUMS`）。
- **版を上げるとき**：同じ URL から取り直し、ファイル名の版・`index.html` の import と modulepreload・`SHA256SUMS`・`LICENSES.md`・この表を揃える。
  Trystero は版で通信の形が変わりうる（`index.html` の「Trystero 0.25.x API notes」と RUNBOOK §4 を先に読む）。版が同じなら通信の形は変わらず、本番・CLI・常設ページ・泡版とそのままつながる。
- ライセンス本文は `LICENSES.md`（中に含まれる依存も含む）。
- **読み上げの 3 つ（v0.16.14〜）は npm の tarball から取る**（`npm pack piper-plus@0.7.0 @piper-plus/g2p@0.4.2 onnxruntime-web@1.30.0`。npm が integrity を確かめる）。
  それまで読んでいた jsDelivr／unpkg の同じパスと 1 バイトも違わないことを確かめてある（10-14）。**ディレクトリの並びは配布物のまま**にする
  （モジュール同士が `./timing.js`・`../data/…` のような相対 import で互いを読むため）。置くのは実際に読むファイルだけ
  （ヘッドレス Chrome で実エンジンを読ませて、取りに行った URL を全部控えたもの＋各パッケージの `src/` の残り）。
  ORT の wasm は `numThreads = 1` でも `ort-wasm-simd-threaded.*` を読む（wasm 専用バンドルが指すのはこれだけ）。
  piper-plus が探す中国語の辞書 `assets/pinyin_*.json` は npm にも無く、CDN でも 404 だった＝置かない（日本語には使わない）。
  版を上げるときは import map・`TTS_WASM_URL`・`TTS_ORT_DIST`（`index.html`）とディレクトリ名を揃え、実エンジンで 1 回読ませて取りに行く URL を見直す。
- **大きいファイル**：`piper_plus_wasm_bg.wasm` は 60MB（GitHub は 50MB 超で push 時に警告・100MB で拒否。GitHub Pages は gzip で配るので転送は約 19MB）。
  版を上げるたびに履歴に 60MB が積もるので、上げるのは必要なときだけにする。
