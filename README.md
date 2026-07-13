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
- An AI narrator (radio-style persona, PT/EN/ES) posts voice notes on goals, cards
  and odds swings — with a configurable anti-spoiler delay.
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
