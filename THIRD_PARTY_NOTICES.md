# Third-Party Notices

ぽっと通話 (po-talk) は MIT ライセンスで配布されます（[LICENSE](LICENSE)）。
本ファイルは、本プロジェクトが利用・同梱する第三者ソフトウェアの著作権表示と
ライセンスをまとめたものです。

---

## 実行時に読み込む依存（リポジトリには同梱していません）

以下は `index.html` から実行時に CDN（esm.sh）経由で読み込むもので、本リポジトリは
これらのソースコードを再配布していません。参考として明記します。

- **Trystero** — MIT License — © Dan Motzenbecker
  https://github.com/dmotz/trystero
- **qrcode-generator** — MIT License — © Kazuhiko Arase
  https://github.com/kazuhikoarase/qrcode-generator
- **nostr-tools** — The Unlicense（パブリックドメイン相当・義務なし）
  https://github.com/nbd-wtf/nostr-tools

---

## 同梱している素材

### Lucide（アイコン）— ISC License

`index.html` 内のインライン SVG アイコン（雲・双方向矢印・マイク・入退室・QR 等）は
[Lucide](https://lucide.dev/) のアイコンに基づいています。ISC ライセンスに従い、
以下の著作権表示と許諾表示を保持します。

```
ISC License

Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part of
Feather (MIT). All other copyright (c) for Lucide are held by Lucide Contributors 2022.

Permission to use, copy, modify, and/or distribute this software for any purpose
with or without fee is hereby granted, provided that the above copyright notice
and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT,
OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA
OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,
ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

---

### 挿絵「ティーポットとカップ」（`start/index.html` の冒頭）— Pixabay Content License

- 元素材：Pixabay #32041「Tea Service, Teapot, Coffee Pot」— 作者 Clker-Free-Vector-Images（2012-04-13 公開）
  https://pixabay.com/images/id-32041/ （svgsilh.com の再配布 SVG から取り込み）
- ライセンス：Pixabay Content License（帰属表示不要・改変可・商用可。**無加工での単体再配布は不可**）。
  2019-01-09 より前の公開なので、公開当時の CC0 1.0 でもある。
- 本プロジェクトでの扱い：塗りを CSS の色に差し替え、湯気の線を描き足した**改変版**を、案内ページの一部として
  埋め込んでいる。この挿絵だけを切り出しての再配布は、Pixabay の条件に従うこと（本プロジェクトの MIT の対象外）。

## 本ライセンスの対象外

- `images/popopo-follow.png` … POPOPO の素材（第三者に帰属）。本プロジェクトの
  MIT ライセンスの対象ではありません。
- `start/index.html` 内の挿絵 SVG … 上記 Pixabay #32041 の改変版。同じく MIT の対象外。
