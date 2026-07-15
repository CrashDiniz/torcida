# Deploy — Torcida (D4)

One Railway service runs bot + Mini App web (Dockerfile CMD). Landing is
static (`landing/index.html`) — served by the same app at `/landing` or by
Vercel as a separate static site.

## 1. Railway (needs Crash login)

1. https://railway.app → New Project → Deploy from GitHub → `CrashDiniz/torcida`
2. Variables (Settings → Variables) — names MUST match what the code reads
   (`grep -rn os.environ src/`); the old `TXLINE_BASE_URL` was wrong:
   - `TELEGRAM_BOT_TOKEN` — from app/.env (required; app crashes without it)
   - `TXLINE_API_TOKEN` — from app/.env (required)
   - `TXLINE_API_BASE` — from app/.env (World Cup base; defaults wrong if unset)
   - `TXLINE_JWT` — from app/.env (guest JWT; optional but set it)
   - `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` — from app/.env (voice; falls
     back to edge-tts if absent, so app still runs without them)
   - `APP_DIRECT_LINK=https://t.me/torcidaapp_bot/app` (fixed, from app/.env)
   - `WEBAPP_URL=https://app.torcida.app` (after DNS below; optional at boot)
   - `DATABASE_PATH=/data/app.sqlite3`
   - Optional: `DEEPSEEK_API_KEY` (unpredictable narration text; edge/EL work without)
3. Add a Volume mounted at `/data` (sqlite must survive redeploys).
4. Settings → Networking → Generate Domain → note `<svc>.up.railway.app`.

## 2. DNS at Spaceship (torcidafun@gmail.com account)

- `app.torcida.app`  CNAME → `<svc>.up.railway.app`
- `torcida.app` → landing: A/ALIAS to Vercel (or CNAME www) if landing goes
  to Vercel; simplest hackathon path: point `torcida.app` to Railway too and
  serve the landing at `/` for non-Telegram user agents (TODO if chosen).
- Then in Railway: Settings → Networking → Custom Domain → `app.torcida.app`.

## 3. BotFather (Crash's Telegram, 2 min)

- `/newapp` → pick @torcidaapp_bot → title "Torcida", short name `app`,
  URL `https://app.torcida.app` → gives **t.me/torcidaapp_bot/app**
  (Direct Link: opens the Mini App straight from group buttons).
- Optional: `/setdomain` for login widget later.

## 4. After deploy

- Kill the local tunnel bot/web; update `WEBAPP_URL` env in Railway only.
- Bot sets the DM menu button automatically on startup from `WEBAPP_URL`.
- Smoke: open t.me/torcidaapp_bot/app, place a pick, check `/api/state`.

## Local dev (current setup)

- bot: `.venv/bin/python -m src.bot.main`
- web: `.venv/bin/uvicorn src.web.app:app --port 8090`
- tunnel: `cloudflared tunnel --url http://localhost:8090` → set `WEBAPP_URL`
  in `.env` to the printed trycloudflare URL, restart bot.
