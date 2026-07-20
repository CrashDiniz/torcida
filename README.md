# Torcida ⚽

Live World Cup pools with odds-priced picks, an AI radio narrator with voice in your
Telegram group, and poker-style payout structures. Built on TxLINE real-time football
data anchored on Solana, for the TxODDS World Cup Hackathon (Superteam Earn, 2026).

**Bot:** [@torcidaapp_bot](https://t.me/torcidaapp_bot) ·
**Web:** [torcida.app](https://torcida.app) (soon) ·
**X:** [@torcidaapp](https://x.com/torcidaapp)

## What it does

- Create a pool, share an invite link, friends join in one tap — no signup.
- Picks are priced by live consensus odds at pick time: calling the underdog pays more.
- In-play flash picks ("goal before 75'?") resolved by the live feed in seconds.
- An AI narrator posts voice notes on goals, cards and odds swings in a Brazilian
  radio-style "resenha" (PT-BR), with a configurable anti-spoiler delay. The
  narration pipeline (event → LLM line → TTS) is language-agnostic; EN/ES personas
  are on the roadmap.
- Pool creator chooses the payout structure: winner-takes-all, top 3, or top 20%
  poker-style table.

## Architecture

```
TxLINE (SSE odds/scores/events + REST fixtures/lineups)
  └─ src/ingest    auth, SSE consumer, local fixture cache, recorder/replayer
  └─ src/engine    pools, odds-priced scoring, leaderboard, payout tables
  └─ src/bot       Telegram bot (aiogram)
  └─ src/narrator  event -> LLM commentary -> TTS voice notes
  └─ src/api       FastAPI backend for the React web app
```

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in tokens
```

## Tests

```bash
.venv/bin/pytest
```

## TxLINE mainnet setup notes (what actually worked)

We run live on mainnet (`https://txline.txodds.com`) after starting on devnet.
Notes for anyone hitting trouble with the mainnet API:

1. **The auth flow is identical to devnet, but tokens are per-network.** A
   devnet `txoracle_api_...` token does NOT work against the mainnet base URL.
   Redo the full flow against mainnet: `POST /auth/guest/start` (guest JWT) →
   on-chain `Subscribe` instruction signed by your wallet on **mainnet-beta**
   (needs a little real SOL for the fee) → `POST /api/token/activate` with the
   signed activation message + the subscribe tx signature.
2. **Every data call needs BOTH headers** — `Authorization: Bearer <guest JWT>`
   *and* `X-Api-Token: <txoracle token>`. Missing either one looks like an
   auth problem with the token when it isn't.
3. **Guest JWTs expire; the api token doesn't.** If calls suddenly 401, refresh
   the JWT via `/auth/guest/start` and keep the same `X-Api-Token`.
4. **Mainnet free tier (SL1) is ~60s delayed**; devnet is real-time. During
   live matches we ran a hybrid: devnet feed for the live experience, mainnet
   for the on-chain subscription + `validateStatV2` settlement proofs. After
   the tournament we flipped the live feed to mainnet with
   [`deploy/flip_mainnet.sh`](deploy/flip_mainnet.sh) (idempotent, with
   sanity-check and rollback).
5. **FixtureIds are identical on devnet and mainnet**, so a flip is just
   swapping `TXLINE_API_BASE` + token — no data migration, no orphaned picks.
6. **Sending `validateStatV2` proofs on mainnet: expect confirm timeouts.**
   Under congestion the 30s confirmation window expires while the tx never
   lands (blockhash expiry). Just retry the send — ours landed on attempt 2.
   See [`onchain/verify_result.js`](onchain/verify_result.js).
7. **Knockout-stage gotcha for settlement logic**: the feed emits
   `fulltime_finalised` at the end of the regular 90 minutes even when the
   match goes to extra time. Only `game_finalised` is the authoritative end of
   a knockout match — we learned this live, during the World Cup final.
