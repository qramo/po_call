# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A serverless P2P voice-chat room ("ぽっと通話〈実験版〉"), shipped as a **single static `index.html`**. No build step, no package manager, no tests, no backend. Everything — markup, CSS, and the ES module — lives in that one file. UI text and code comments are in Japanese; keep new user-facing strings in Japanese to match.

Deployed via GitHub Pages (`main` / root), which supplies the HTTPS that `getUserMedia` requires. Pushing to `main` publishes.

## Where the notes live

The handoff log (`HANDOFF.md`), code reviews and plans are **not in this repo**. They sit in a nested private repo at `internal/` (GitHub: `po-talk/potalk-internal`), which this repo's `.gitignore` excludes. Start a session by reading `internal/HANDOFF.md` from its top section. The two always-on clients (greeter / echo) are another private repo, `po-talk/potalk-greeter`, checked out at `~/Downloads/potalk-greeter`; they must be redeployed by hand whenever the data-channel protocol changes.

## Running locally

```bash
python3 -m http.server 8000   # then open http://localhost:8000
```

`file://` will not work: `getUserMedia` only runs on `https://` or `localhost`. There is nothing to build, lint, or test — verify changes by opening two browser tabs/devices on the same room name and confirming they connect.

## Architecture

The one dependency is [Trystero](https://github.com/dmotz/trystero), pinned and loaded at runtime via **dynamic `import('https://esm.sh/trystero@0.25.4?bundle')` inside the join handler** — not a top-level import. This is deliberate: the join flow is staged (① mic → ② library load → ③ room join), and each stage reports its own failure into `#state`, so a slow or blocked CDN shows a specific message instead of a dead page. Keep new failure modes inside that pattern. There is no `node_modules` and no bundler, so upgrading means editing that URL.

Trystero handles peer discovery and signaling over public **Nostr relays**, which is why no backend exists. Peers that open the same `roomId` under the same `appId` (`kuramo-webrtc-call`) find each other and negotiate WebRTC directly. Room identity is a random 16-char base64url id carried in the URL hash (`...#room=<id>&name=<部屋名>`); the name is metadata (sent over a `meta` data channel and in lobby presence), so two rooms may share a name. The share link *is* the room. Legacy `#部屋名` links act as an *entrance*: the app looks the name up in the lobby and joins if such a room is live, otherwise offers to create a new one.

**Broadcast rooms** (`...#room=<id>~pk~<22-char fingerprint>&name=…`) extend that identity rather than sitting beside it: the fingerprint — base64url of the first 16 bytes of SHA-256 over the owner's ECDSA P-256 public key — is **part of the Trystero `roomId`**, so everyone in the room provably shares one owner key and nobody can claim ownership by self-report. The owner's private key lives in `localStorage` (`pot-call-bcast`, keyed by fingerprint); the owner signs a roster of peerIds allowed to speak and sends it over a data channel, and **every client refuses to play audio from anyone not on a verified roster** (`canHear()`). Listeners never call `getUserMedia` at all. Anything that touches "who may be heard" belongs in `canHear` — it is the same filter layer the per-peer 無視 feature uses. This adds no server: key generation, signing, and verification are all `crypto.subtle` in the browser, and rosters ride the existing WebRTC data channels, so the legal posture below is unchanged.

Connections are a **full mesh** — every peer connects to every other peer. This caps comfortable use at roughly 4–6 participants; more requires an SFU. Audio is browser-to-browser end-to-end; only signaling touches a relay.

Call state lives in one module-scope object `call` (built from the `CALL_INIT` template; `resetCall()` restores it on leave) plus a handful of module-scope `let`s for audio/lobby/UI. Adding a feature usually means adding a handler and keeping `leaveCall` symmetric with `runJoin` — `leave` must tear down every resource `join` acquired (stop tracks, null `srcObject`, remove audio elements, release the wake lock), or a rejoin inherits stale state.

## Why "no backend" is load-bearing, not just a convenience

The zero-server design is also what keeps the operator from being treated as a 電気通信事業者 that 他人の通信を媒介する under Japan's 電気通信事業法. The legal position rests on one technical fact: **no communication path — audio, signaling, or presence — ever passes through equipment the operator runs.** Concretely:

- **Media** is browser-to-browser P2P (WebRTC), end-to-end. When direct connection fails it relays via **Cloudflare TURN** (`turn.cloudflare.com`) — a third party, not the operator's. Short-lived TURN credentials are minted by the operator's own tiny **Cloudflare Worker** (`pot-turn`, which holds the secret TURN key as an env var; the browser fetches ICE servers from it at load and passes them to Trystero via `rtcConfig.iceServers`). That Worker only hands out credentials — **no audio, signaling, or presence ever passes through it** — so the media path still never touches operator-run equipment.
- **Signaling / peer discovery** rides **third-party public Nostr relays** — not the operator's.
- **The lobby** (active-room list / liveness monitoring) is the same mechanism: a fixed hidden room (`__lobby__`) whose presence heartbeats travel over those same Nostr relays and WebRTC data channels. It adds no server.

So the operator only distributes a static page (a *tool* that lets peers talk directly). This posture is unchanged by features added on top as long as they stay on these rails.

**The boundary that would change the analysis** — do not cross without deliberate legal review: standing up your **own** signaling server, relay, TURN, or **SFU**; or **storing/relaying** call content or presence on your own server. The scaling ceilings below (full mesh; a Trystero-based lobby that also meshes) will tempt exactly these. If you need to scale, prefer approaches that stay serverless (e.g. publishing presence directly to public Nostr relays) over introducing infrastructure you operate. This is not legal advice; confirm with 総務省 guidance or a lawyer before any public/commercial launch.

The `pot-turn` Worker is the one piece of operator-run code, and it stays on the safe side of that boundary **because it is a credential minter, not a communication path**: it issues short-lived TURN keys but carries no audio, signaling, or presence (the TURN server itself is Cloudflare's). Keep any operator-run code to this shape — auth/coordination only, never carrying the actual communication — or the 媒介 analysis changes.

## Trystero 0.25.x API notes

The library's API changed and most guidance found online (including this repo's README) predates it. Verify against the installed version before trusting an example:

- **Callbacks are assigned, not called**: `room.onPeerJoin = id => {}`, `room.onPeerStream = (stream, id) => {}`. The older `room.onPeerJoin(fn)` call form is gone; using it would overwrite the handler.
- **Subpath imports throw.** `trystero/torrent`, `trystero/mqtt`, `trystero/firebase`, `trystero/ipfs`, `trystero/supabase` all raise "Importing from ... is deprecated" — each strategy is now its own package (`@trystero-p2p/torrent`, etc.). Bare `trystero` re-exports the **nostr** strategy, which is what this app uses.
- **Relay options live under `relayConfig`**: `relayConfig: {urls: [...], redundancy: N}`. The flat `relayUrls` / `relayRedundancy` names are silently ignored — unknown keys don't error, they just fall back to the shuffled default relays (`defaultRedundancy` is 5).
- `turnConfig: [{urls, username, credential}, ...]` is valid and *adds* to Trystero's default STUN servers. Use `rtcConfig.iceServers` instead only if you want to replace the defaults entirely.
- **Re-sent streams are invisible through `onPeerStream`.** Trystero remembers remote streams by the *sender's* stream id. If a peer removes and re-adds the same `MediaStream`, the browser delivers the new track in a *new* `MediaStream` object, but Trystero looks the id up, finds the old object, and never calls `onPeerStream` again (it fires once, early, with the stale object when the stream *metadata* arrives over the data channel). Two consequences baked into this app (v0.14.20): (1) the receiver takes tracks straight from the `RTCPeerConnection` `track` event via the `window.__potTrack` hook in the PeerConnection wrapper, mapping the connection back to a peerId with `room.getPeers()`; (2) a sender answering a 「送り直して」(nudge) request calls `removeStream` and then `addStream(new MediaStream(tracks))` so the fresh id also reaches old clients. Re-calling `addStream` with the same stream does nothing: `addTrack` throws because a sender already exists.
- A received track can be `live` yet `muted` forever (SDP negotiated, no RTP). "Audio arrived" therefore means `track.readyState === 'live' && !track.muted` (`audioFlowing`), not "an `<audio>` element exists". Renegotiation after the data channel is open travels over that channel (fast), not over Nostr.

## Mobile constraints the code works around

These are load-bearing; removing them silently breaks real devices:

- **iOS autoplay**: peer audio `.play()` can reject. `play()` catches this and reveals the `#unlock` button so a user gesture can start playback.
- **In-app browsers**: Facebook/Instagram/LINE/X/WeChat webviews often cannot access the mic. The UA sniff at the top shows a banner telling users to open in Safari/Chrome, with a copy-link button.
- **Wake lock**: `navigator.wakeLock` is requested on join and re-requested on `visibilitychange`, since the lock is dropped when the page is backgrounded. All wake-lock calls are best-effort and swallow errors — unsupported browsers must still work.
