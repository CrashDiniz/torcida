"""Torcida Mini App: manage picks + live leaderboard inside Telegram.

Run: .venv/bin/uvicorn src.web.app:app --port 8090
Auth: Telegram WebApp initData (HMAC) — no passwords, no sessions.
Shares the bot's sqlite store. Picks placed here fetch the CURRENT odds
server-side (no stale card odds) and lock at kickoff (API StartTime).
"""
from __future__ import annotations

import html
import os
import time
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src.engine.models import (PayoutPreset, Pick, PickStatus, Pool,
                               RequestStatus, Visibility)
from src.engine.odds import parse_snapshot
from src.engine.scoring import points_for
from src.engine.settlement import snapshot_score
from src.engine.teams import pt
from src.engine.store import Store
from src.ingest.txline import TxLineClient

from .auth import validate_init_data

load_dotenv()
app = FastAPI(title="Torcida Mini App")
store = Store()
txline = TxLineClient.from_env()

STATUS_LABELS = {PickStatus.OPEN: ("⏳", "aberto"), PickStatus.WON: ("✅", "ganhou"),
                 PickStatus.LOST: ("❌", "perdeu")}

PAYOUT_LABELS = {
    PayoutPreset.WINNER_TAKES_ALL: "🥇 Vencedor leva tudo",
    PayoutPreset.TOP3: "🏆 Top 3 (50/30/20)",
    PayoutPreset.POKER: "🃏 Top 20% (estilo poker)",
}
PAYOUT_OPTIONS = [(p.value, PAYOUT_LABELS[p]) for p in PayoutPreset]
# app-created pools are always discoverable — the app has no surface to share
# an invite link, so a hidden pool would be a dead end. Private/link-only pools
# are born in the group via /novo instead.
VISIBILITY_OPTIONS = [
    (Visibility.PUBLIC.value, "🔓 Livre — qualquer um entra"),
    (Visibility.REQUEST.value, "🙋 Sob pedido — você aprova quem entra"),
]

_fixtures_cache: tuple[float, list] = (0.0, [])
FIXTURES_TTL_S = 60


def _selection_name(selection: str, fixture_label: str | None) -> str:
    if fixture_label and " x " in fixture_label:
        home, away = fixture_label.split(" x ", 1)
        return {"1": home, "X": "empate", "2": away}.get(selection, selection)
    return {"1": "casa", "X": "empate", "2": "visitante"}.get(selection, selection)


async def _fixtures() -> list:
    global _fixtures_cache
    ts, cached = _fixtures_cache
    if time.time() - ts < FIXTURES_TTL_S:
        return cached
    fx = await txline.fixtures(start_epoch_day=int(time.time() // 86400))
    fx = sorted(fx, key=lambda f: f.get("StartTime", 0))[:5]
    _fixtures_cache = (time.time(), fx)
    return fx


# TEST MODE: picks stay open for a grace window AFTER kickoff (set via env so
# it can be flipped without a code change). 0 = lock exactly at kickoff.
PICK_GRACE_S = int(os.environ.get("PICK_GRACE_S", "0"))


def fixture_locked(fixture: dict, now: float | None = None) -> bool:
    """Kickoff lock by API start time (ms epoch) — source of truth for the app.
    Picks stay open until kickoff + PICK_GRACE_S (test-mode grace)."""
    start_s = (fixture.get("StartTime") or 0) / 1000
    return start_s + PICK_GRACE_S <= (now or time.time())


def _auth(init_data: str) -> dict | None:
    return validate_init_data(init_data, os.environ["TELEGRAM_BOT_TOKEN"])


class StateRequest(BaseModel):
    initData: str


class PickRequest(BaseModel):
    initData: str
    pool_id: str
    fixture_id: int
    selection: str


class LeaveRequest(BaseModel):
    initData: str
    pool_id: str


async def _state_payload(uid: int, first_name: str) -> dict:
    upcoming = await _fixtures()
    pools = []
    for pool in store.pools_for_user(uid):
        my_picks = store.picks_for_user(pool.id, uid)
        mine_by_fixture = {p.fixture_id: p.selection for p in my_picks}
        picks = []
        for p in my_picks:
            label = store.fixture_label(p.fixture_id) or f"jogo {p.fixture_id}"
            emoji, status = STATUS_LABELS.get(p.status, ("⏳", "aberto"))
            pts = (p.points_awarded if p.status == PickStatus.WON
                   else points_for(p.odds_decimal) if p.status == PickStatus.OPEN
                   else 0)
            verification = (store.verification(p.fixture_id)
                            if p.status != PickStatus.OPEN else None)
            verify_url = (
                f"https://explorer.solana.com/tx/{verification['tx_sig']}?cluster=devnet"
                if verification and verification["valid"] and verification["tx_sig"]
                else None)
            picks.append({"fixture": label,
                          "selection": _selection_name(p.selection, label),
                          "odds": f"{p.odds_decimal:.2f}", "points": pts,
                          "emoji": emoji, "status": status,
                          "verify_url": verify_url})
        fixtures = []
        for f in upcoming:
            home, away = pt(f.get("Participant1", "?")), pt(f.get("Participant2", "?"))
            fixtures.append({
                "fixture_id": f["FixtureId"], "home": home, "away": away,
                "locked": fixture_locked(f),
                "mine": mine_by_fixture.get(f["FixtureId"]),
            })
        standings = [{"name": name, "points": points,
                      "chips": chips, "me": row_uid == uid}
                     for row_uid, name, points, chips in store.pot_split(pool.id)]
        pools.append({"id": pool.id, "name": pool.name, "picks": picks,
                      "fixtures": fixtures, "standings": standings,
                      "buy_in": pool.buy_in, "pot": store.pot_for(pool.id),
                      "payout_label": PAYOUT_LABELS[pool.payout_preset],
                      "is_creator": pool.creator_id == uid})
    requests = [{"pool_id": r.pool_id, "pool_name": (p.name if
                 (p := store.pool_by_id(r.pool_id)) else "?"),
                 "user_id": r.user_id, "name": r.display_name}
                for r in store.pending_requests_for_creator(uid)]
    return {"first_name": first_name, "pools": pools, "requests": requests}


def _discover_payload(uid: int) -> dict:
    """Public/request pools the user can browse — with their join state so the
    card knows whether to show Join, Request, Pending or Open."""
    items = []
    for pool in store.public_pools():
        if store.is_member(pool.id, uid):
            my_status = "member"
        elif (rs := store.request_status(pool.id, uid)) == RequestStatus.PENDING:
            my_status = "pending"
        else:
            my_status = "none"
        items.append({
            "id": pool.id, "name": pool.name,
            "creator": store.creator_name(pool),
            "visibility": pool.visibility.value, "buy_in": pool.buy_in,
            "pot": store.pot_for(pool.id), "entries": store.entry_count(pool.id),
            "payout_label": PAYOUT_LABELS[pool.payout_preset],
            "my_status": my_status,
        })
    return {"pools": items, "payout_options": PAYOUT_OPTIONS,
            "visibility_options": VISIBILITY_OPTIONS}


@app.post("/api/state")
async def state(req: StateRequest):
    user = _auth(req.initData)
    if user is None:
        return JSONResponse({"error": "auth"}, status_code=401)
    return await _state_payload(int(user["id"]), user.get("first_name", "Torcedor"))


@app.post("/api/pick")
async def place_pick(req: PickRequest):
    user = _auth(req.initData)
    if user is None:
        return JSONResponse({"error": "auth"}, status_code=401)
    uid = int(user["id"])
    if req.selection not in ("1", "X", "2"):
        return JSONResponse({"error": "selection"}, status_code=400)
    if all(p.id != req.pool_id for p in store.pools_for_user(uid)):
        return JSONResponse({"error": "pool"}, status_code=403)
    fixture = next((f for f in await _fixtures()
                    if f["FixtureId"] == req.fixture_id), None)
    if fixture is None:
        return JSONResponse({"error": "fixture"}, status_code=404)
    if fixture_locked(fixture):
        return JSONResponse({"error": "locked"}, status_code=409)

    # odds are fetched NOW, server-side — immune to stale cards
    odds = parse_snapshot(await txline.odds_snapshot(req.fixture_id))
    value = {"1": odds.home, "X": odds.draw, "2": odds.away}[req.selection]
    label = f"{pt(fixture.get('Participant1', '?'))} x {pt(fixture.get('Participant2', '?'))}"
    store.set_fixture_label(req.fixture_id, label)

    import uuid as _uuid
    existing = store.pick_for(req.pool_id, uid, req.fixture_id)
    if existing is not None:
        if existing.selection == req.selection:
            return {"ok": True, "unchanged": True}
        store.replace_pick(existing.id, req.selection, value, time.time())
    else:
        store.place_pick(Pick(id=_uuid.uuid4().hex, pool_id=req.pool_id,
                              user_id=uid, fixture_id=req.fixture_id,
                              market="1x2", selection=req.selection,
                              odds_decimal=value))
    return {"ok": True, "selection": _selection_name(req.selection, label),
            "odds": f"{value:.2f}", "points": points_for(value)}


@app.post("/api/leave")
async def leave(req: LeaveRequest):
    user = _auth(req.initData)
    if user is None:
        return JSONResponse({"error": "auth"}, status_code=401)
    store.leave(req.pool_id, int(user["id"]))
    return {"ok": True}


class DiscoverRequest(BaseModel):
    initData: str


class CreateRequest(BaseModel):
    initData: str
    name: str
    visibility: str = "public"
    buy_in: int = 0
    payout_preset: str = "top3"


class JoinRequest_(BaseModel):
    initData: str
    pool_id: str


class ApproveRequest(BaseModel):
    initData: str
    pool_id: str
    user_id: int
    decision: str  # "approve" | "deny"


@app.post("/api/discover")
async def discover(req: DiscoverRequest):
    user = _auth(req.initData)
    if user is None:
        return JSONResponse({"error": "auth"}, status_code=401)
    return _discover_payload(int(user["id"]))


@app.post("/api/create")
async def create_pool(req: CreateRequest):
    user = _auth(req.initData)
    if user is None:
        return JSONResponse({"error": "auth"}, status_code=401)
    name = req.name.strip()[:60]
    if not name:
        return JSONResponse({"error": "name"}, status_code=400)
    try:
        visibility = Visibility(req.visibility)
        preset = PayoutPreset(req.payout_preset)
    except ValueError:
        return JSONResponse({"error": "option"}, status_code=400)
    if visibility == Visibility.HIDDEN:  # app can't share an invite link
        return JSONResponse({"error": "hidden_unsupported"}, status_code=400)
    buy_in = max(0, min(int(req.buy_in), 1_000_000))
    uid = int(user["id"])
    pool = Pool(id=uuid.uuid4().hex, name=name, creator_id=uid,
                payout_preset=preset, buy_in=buy_in, visibility=visibility)
    store.create_pool(pool)  # app-born pool: not bound to a group chat
    store.join(pool.id, uid, user.get("first_name", "Anfitrião"))
    return {"ok": True, "pool_id": pool.id}


@app.post("/api/join")
async def join_pool(req: JoinRequest_):
    user = _auth(req.initData)
    if user is None:
        return JSONResponse({"error": "auth"}, status_code=401)
    pool = store.pool_by_id(req.pool_id)
    if pool is None:
        return JSONResponse({"error": "pool"}, status_code=404)
    uid = int(user["id"])
    if store.is_member(pool.id, uid):
        return {"ok": True, "status": "member"}
    if pool.visibility != Visibility.PUBLIC:
        return JSONResponse({"error": "not_open"}, status_code=403)
    store.join(pool.id, uid, user.get("first_name", "Torcedor"))
    return {"ok": True, "status": "member"}


async def _notify_creator(creator_id: int, requester: str, pool_name: str) -> None:
    """Best-effort DM to the pool creator when someone asks to join. Only lands
    if the creator has already started the bot; never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    import httpx
    text = (f"🙋 <b>{html.escape(requester)}</b> quer entrar no seu bolão "
            f"<b>{html.escape(pool_name)}</b>.\n"
            f"Abra o Torcida e aprove na aba 🎫 Meus bolões.")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": creator_id, "text": text, "parse_mode": "HTML"})
    except Exception:
        pass  # creator hasn't DM'd the bot, or network hiccup — banner still shows


@app.post("/api/request")
async def request_join(req: JoinRequest_):
    user = _auth(req.initData)
    if user is None:
        return JSONResponse({"error": "auth"}, status_code=401)
    pool = store.pool_by_id(req.pool_id)
    if pool is None:
        return JSONResponse({"error": "pool"}, status_code=404)
    uid = int(user["id"])
    if store.is_member(pool.id, uid):
        return {"ok": True, "status": "member"}
    if pool.visibility != Visibility.REQUEST:
        return JSONResponse({"error": "not_requestable"}, status_code=403)
    name = user.get("first_name", "Torcedor")
    store.create_join_request(pool.id, uid, name)
    await _notify_creator(pool.creator_id, name, pool.name)
    return {"ok": True, "status": "pending"}


@app.post("/api/approve")
async def approve_request(req: ApproveRequest):
    user = _auth(req.initData)
    if user is None:
        return JSONResponse({"error": "auth"}, status_code=401)
    pool = store.pool_by_id(req.pool_id)
    if pool is None or pool.creator_id != int(user["id"]):
        return JSONResponse({"error": "not_creator"}, status_code=403)
    if req.decision == "approve":
        rs = store.request_status(pool.id, req.user_id)
        name = next((r.display_name for r in
                     store.pending_requests_for_creator(int(user["id"]))
                     if r.user_id == req.user_id), "Torcedor")
        if rs is None:
            return JSONResponse({"error": "no_request"}, status_code=404)
        store.join(pool.id, req.user_id, name)
        store.set_request_status(pool.id, req.user_id, RequestStatus.APPROVED)
        return {"ok": True, "status": "approved"}
    store.set_request_status(pool.id, req.user_id, RequestStatus.DENIED)
    return {"ok": True, "status": "denied"}


# --- live scoreboard for the landing page ------------------------------------

_live_cache: tuple[float, dict] = (0.0, {})
LIVE_TTL_S = 15
LIVE_PHASES = {"h1", "ht", "h2", "et1", "htet", "et2", "pe", "i"}
SPORTS_TABS = [
    {"id": "soccer", "label": "⚽ Futebol", "active": True},
    {"id": "basket", "label": "🏀 Basquete", "active": False},
    {"id": "nfl", "label": "🏈 NFL", "active": False},
]
# board phase caption (no "ao vivo" here — the board already has a LIVE badge)
BOARD_PHASE = {"h1": "1º tempo", "ht": "intervalo", "h2": "2º tempo",
               "et1": "prorrogação", "et2": "prorrogação", "pe": "pênaltis"}
# ESP x ARG 19/07 — the landing board dresses this one up as the grand final
FINAL_FIXTURE_ID = 18_257_739


async def _live_payload() -> dict:
    """Live World Cup scores for the landing board — cached so many visitors
    share one upstream fetch. Other sports are declared but inactive: the
    TxODDS schema already carries them, we just don't ingest them yet."""
    global _live_cache
    ts, cached = _live_cache
    if cached and time.time() - ts < LIVE_TTL_S:
        return cached
    now = time.time()
    live: list[dict] = []
    for f in await _fixtures():
        if (f.get("StartTime") or 0) / 1000 > now:
            continue  # not kicked off yet
        try:
            sc = snapshot_score(await txline.scores_snapshot(f["FixtureId"]))
        except Exception:
            sc = None
        if sc is not None and sc[3]:
            continue  # already ended
        # kicked off and not finished -> live. The score feed is sparse (only
        # emits on incidents), so it may not carry an in-play phase yet at
        # kickoff; time-since-kickoff is the signal, same as the bot.
        hs, aw, phase_code = (sc[0], sc[1], sc[2]) if sc else (0, 0, "")
        odds = None
        try:
            od = parse_snapshot(await txline.odds_snapshot(f["FixtureId"]))
            odds = {"home": round(od.home, 2), "draw": round(od.draw, 2),
                    "away": round(od.away, 2)}
        except Exception:
            pass
        live.append({
            "home": pt(f.get("Participant1", "?")), "away": pt(f.get("Participant2", "?")),
            "hs": hs, "as": aw,
            "phase": BOARD_PHASE.get(phase_code, "em jogo"), "odds": odds,
        })
    upcoming = []
    if not live:
        future = sorted((f for f in await _fixtures()
                         if (f.get("StartTime") or 0) / 1000 > now),
                        key=lambda f: f.get("StartTime") or 0)
        upcoming = [
            {"label": f"{pt(f.get('Participant1', '?'))} x {pt(f.get('Participant2', '?'))}",
             "at": f.get("StartTime"),
             "final": f.get("FixtureId") == FINAL_FIXTURE_ID}
            for f in future[:2]
        ]
    payload = {"sports": SPORTS_TABS,
               "soccer": {"live": live, "upcoming": upcoming,
                          # kept for anything still reading the old shape
                          "next": upcoming[0]["label"] if upcoming else None,
                          "next_at": upcoming[0]["at"] if upcoming else None}}
    _live_cache = (time.time(), payload)
    return payload


# --- retrospect: finished-game team stats + odds for the games ahead ----------

_recap_cache: tuple[float, dict] = (0.0, {})
RECAP_TTL_S = 120
RECAP_LOOKBACK_DAYS = 4


def _side_stats(block: dict) -> dict:
    tot, ht = block.get("Total") or {}, block.get("HT") or {}
    return {"goals": tot.get("Goals", 0), "ht_goals": ht.get("Goals", 0),
            "corners": tot.get("Corners", 0), "yellows": tot.get("YellowCards", 0),
            "reds": tot.get("RedCards", 0)}


async def _recap_payload() -> dict:
    """Team stats of recently finished fixtures (the feed's per-half Score
    block: goals, corners, cards) plus current 1X2 odds for the games ahead.
    Fuels the /retro research page — the feed is team-level, no lineups."""
    global _recap_cache
    ts, cached = _recap_cache
    if cached and time.time() - ts < RECAP_TTL_S:
        return cached
    now = time.time()
    fx = sorted(await txline.fixtures(
                    start_epoch_day=int(now // 86400) - RECAP_LOOKBACK_DAYS),
                key=lambda f: f.get("StartTime") or 0)
    played, upcoming = [], []
    for f in fx:
        base = {"home": pt(f.get("Participant1", "?")),
                "away": pt(f.get("Participant2", "?")),
                "start": f.get("StartTime")}
        if (f.get("StartTime") or 0) / 1000 <= now:
            try:
                snap = await txline.scores_snapshot(f["FixtureId"])
            except Exception:
                continue
            sc = snapshot_score(snap)
            ev = max((e for e in snap if isinstance(e, dict) and e.get("Score")),
                     key=lambda e: e.get("Seq") or 0, default=None)
            if not (sc and sc[3] and ev):
                continue  # live or unfinished — the landing board covers it
            played.append(base | {"h": _side_stats(ev["Score"].get("Participant1") or {}),
                                  "a": _side_stats(ev["Score"].get("Participant2") or {})})
        else:
            odds = None
            try:
                od = parse_snapshot(await txline.odds_snapshot(f["FixtureId"]))
                odds = {"home": round(od.home, 2), "draw": round(od.draw, 2),
                        "away": round(od.away, 2)}
            except Exception:
                pass
            upcoming.append(base | {"odds": odds})
    payload = {"played": played, "next": upcoming}
    _recap_cache = (time.time(), payload)
    return payload


@app.get("/api/live")
async def live() -> dict:
    return await _live_payload()


PAGE = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Torcida</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --paper: #F1E9D8; --ink: #26221A; --green: #1B4D3E;
    --stamp: #C8102E; --gold: #D9A441; --board: #14120E;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: var(--paper); color: var(--ink); min-height: 100vh;
    font-family: Georgia, 'Times New Roman', serif; padding-bottom: 40px;
    background-image: radial-gradient(rgba(38,34,26,.05) 1px, transparent 1px);
    background-size: 5px 5px;
  }
  header {
    background: var(--green); color: var(--paper); text-align: center;
    padding: 18px 12px 14px; border-bottom: 4px double var(--gold);
  }
  header .badge {
    display: inline-block; border: 2px solid var(--paper); border-radius: 50%;
    width: 44px; height: 44px; line-height: 40px; font-size: 22px;
    margin-bottom: 6px; background: repeating-linear-gradient(
      90deg, var(--green) 0 6px, #163f33 6px 12px);
  }
  header h1 { font-size: 26px; letter-spacing: 4px; text-transform: uppercase; }
  header small { color: var(--gold); letter-spacing: 2px; font-size: 10px;
    text-transform: uppercase; }
  .board {
    background: var(--board); color: #EDE6D3; margin: 14px auto 4px;
    max-width: 520px; border-radius: 8px; padding: 10px 12px;
    font-family: 'Courier New', monospace; box-shadow: 0 3px 0 rgba(0,0,0,.25);
  }
  .board .row { display: flex; justify-content: space-between; padding: 3px 0;
    border-bottom: 1px dashed rgba(237,230,211,.15); font-size: 14px; }
  .board .row:last-child { border-bottom: 0; }
  .board .live { color: #FF4D4D; animation: blink 1.2s infinite; }
  @keyframes blink { 50% { opacity: .35; } }
  section { max-width: 520px; margin: 18px auto 0; padding: 0 14px; }
  h2 {
    font-size: 13px; text-transform: uppercase; letter-spacing: 3px;
    color: var(--green); border-bottom: 2px solid var(--green);
    padding-bottom: 4px; margin-bottom: 10px;
  }
  .fixture { margin-bottom: 12px; }
  .fixture .fx-name { font-size: 15px; font-weight: bold; margin-bottom: 6px; }
  .opts { display: flex; gap: 6px; }
  .opt {
    flex: 1; padding: 9px 4px; font-family: Georgia, serif; font-size: 13px;
    background: #FBF6EA; border: 1.5px solid var(--green); border-radius: 5px;
    color: var(--ink); cursor: pointer; text-align: center;
  }
  .opt b { display: block; font-family: 'Courier New', monospace; }
  .opt.on { background: var(--green); color: var(--paper);
    box-shadow: 1px 2px 0 rgba(38,34,26,.3); }
  .opt.on::after { content: " ✓"; color: var(--gold); }
  .opt:disabled { opacity: .45; cursor: not-allowed; }
  .locked { color: var(--stamp); font-size: 12px; margin-top: 4px; }
  .ticket {
    background: #FBF6EA; border: 1px solid #D8CDB4; border-radius: 6px;
    padding: 10px 14px; margin-bottom: 8px; position: relative;
    box-shadow: 1px 2px 0 rgba(38,34,26,.12);
  }
  .ticket::before, .ticket::after {
    content: ""; position: absolute; top: 50%; width: 14px; height: 14px;
    background: var(--paper); border: 1px solid #D8CDB4; border-radius: 50%;
    transform: translateY(-50%);
  }
  .ticket::before { left: -8px; } .ticket::after { right: -8px; }
  .ticket .fx { font-size: 15px; font-weight: bold; }
  .ticket .sel { color: var(--stamp); font-weight: bold; }
  .ticket .meta { font-size: 12px; color: #6E6552; margin-top: 2px; }
  table { width: 100%; border-collapse: collapse; }
  td, th { padding: 7px 8px; font-size: 14px; text-align: left;
    border-bottom: 1px solid #DDD3BC; }
  tr.me { background: rgba(217,164,65,.18); }
  tr.me td:first-child::after { content: " ← você"; color: var(--stamp);
    font-size: 11px; }
  td.pts { text-align: right; font-family: 'Courier New', monospace;
    font-weight: bold; }
  .medal { margin-right: 4px; }
  .stamp {
    display: inline-block; border: 2px solid var(--stamp); color: var(--stamp);
    padding: 1px 8px; font-size: 10px; text-transform: uppercase;
    letter-spacing: 2px; transform: rotate(-4deg); border-radius: 3px;
    margin-top: 8px; opacity: .85;
  }
  .leave {
    display: block; margin: 22px auto 0; background: none; color: #8A7F68;
    border: 1px dashed #B7AB90; border-radius: 5px; padding: 7px 16px;
    font-family: Georgia, serif; font-size: 12px; cursor: pointer;
  }
  .empty { text-align: center; color: #6E6552; padding: 30px 10px;
    font-style: italic; }
  #err { text-align: center; padding: 40px 20px; color: var(--stamp); }
  #toast {
    position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
    background: var(--board); color: var(--paper); border-radius: 6px;
    padding: 9px 18px; font-size: 13px; opacity: 0; transition: opacity .3s;
    pointer-events: none; max-width: 90vw; text-align: center;
  }
  #toast.show { opacity: 1; }
  .tabs { display: flex; max-width: 520px; margin: 0 auto; position: sticky;
    top: 0; z-index: 5; background: var(--paper); border-bottom: 2px solid var(--green); }
  .tab { flex: 1; padding: 12px 6px; text-align: center; cursor: pointer;
    font-family: Georgia, serif; font-size: 13px; letter-spacing: 1px;
    text-transform: uppercase; color: #8A7F68; background: none; border: 0;
    border-bottom: 3px solid transparent; }
  .tab.on { color: var(--green); font-weight: bold; border-bottom-color: var(--gold); }
  .card { background: #FBF6EA; border: 1px solid #D8CDB4; border-radius: 6px;
    padding: 12px 14px; margin-bottom: 12px; box-shadow: 1px 2px 0 rgba(38,34,26,.12); }
  .card .cname { font-size: 16px; font-weight: bold; }
  .card .cby { font-size: 12px; color: #6E6552; margin-bottom: 8px; }
  .pills { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .pill { font-size: 11px; padding: 2px 8px; border-radius: 10px;
    background: rgba(27,77,62,.1); color: var(--green); border: 1px solid rgba(27,77,62,.25); }
  .pill.pot { background: rgba(217,164,65,.18); color: #8A6A1E; border-color: rgba(217,164,65,.5); }
  .btn { display: block; width: 100%; padding: 10px; font-family: Georgia, serif;
    font-size: 14px; background: var(--green); color: var(--paper); border: 0;
    border-radius: 5px; cursor: pointer; box-shadow: 1px 2px 0 rgba(38,34,26,.3); }
  .btn:disabled { opacity: .5; cursor: not-allowed; box-shadow: none; }
  .btn.ghost { background: none; color: var(--green); border: 1.5px solid var(--green);
    box-shadow: none; }
  .btn.stamp-btn { background: var(--stamp); }
  .potline { font-size: 13px; color: #8A6A1E; text-align: center; margin: 6px auto 0;
    max-width: 520px; }
  .form { max-width: 520px; margin: 0 auto 8px; padding: 0 14px; }
  .form input, .form select { width: 100%; padding: 9px 10px; margin-bottom: 8px;
    font-family: Georgia, serif; font-size: 14px; border: 1.5px solid #C9BC9D;
    border-radius: 5px; background: #FBF6EA; color: var(--ink); }
  .form label { font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
    color: var(--green); display: block; margin-bottom: 3px; }
  .reqbar { background: rgba(217,164,65,.15); border: 1px solid rgba(217,164,65,.5);
    border-radius: 6px; padding: 10px 12px; margin-bottom: 8px; }
  .reqbar .who { font-size: 14px; margin-bottom: 6px; }
  .reqbar .acts { display: flex; gap: 8px; }
  .reqbar .acts .btn { padding: 6px; font-size: 13px; }
  td.chips { text-align: right; font-family: 'Courier New', monospace; color: #8A6A1E; }
</style>
</head>
<body>
<header>
  <div class="badge">⚽</div>
  <h1>Torcida</h1>
  <small>Associação de Palpiteiros ★ Fundada na Copa</small>
</header>
<nav class="tabs">
  <button class="tab" id="tab-discover" onclick="switchTab('discover')">🔎 Descobrir</button>
  <button class="tab" id="tab-mine" onclick="switchTab('mine')">🎫 Meus bolões</button>
</nav>
<div id="root"><p class="empty">Carregando…</p></div>
<div id="toast"></div>
<script>
const tg = window.Telegram?.WebApp;
let state = null;       // /api/state — my pools
let discover = null;    // /api/discover — public pools + create options
let tab = 'mine';
const medals = ['🥇','🥈','🥉'];
const VIS_LABEL = {public: '🔓 Livre', request: '🙋 Sob pedido', hidden: '🔒 Convite'};

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2600);
}
function esc(t) { const d = document.createElement('div');
  d.textContent = t ?? ''; return d.innerHTML; }

async function api(path, body) {
  const res = await fetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({initData: tg.initData, ...body})
  });
  return {ok: res.ok, status: res.status, data: await res.json().catch(() => ({}))};
}

async function load() {
  const root = document.getElementById('root');
  if (!tg || !tg.initData) {
    root.innerHTML = '<div id="err">Abra pelo botão do bot no Telegram 😉</div>';
    return;
  }
  tg.ready(); tg.expand();
  const res = await api('/api/state', {});
  if (!res.ok) {
    root.innerHTML = '<div id="err">Não consegui te reconhecer — reabre pelo bot.</div>';
    return;
  }
  state = res.data;
  tab = state.pools.length ? 'mine' : 'discover';
  render();
  if (tab === 'discover') loadDiscover();
}

async function refreshState() {
  const st = await api('/api/state', {});
  if (st.ok) state = st.data;
}

async function loadDiscover() {
  const res = await api('/api/discover', {});
  if (res.ok) { discover = res.data; if (tab === 'discover') render(); }
}

function switchTab(t) {
  tab = t;
  render();
  if (t === 'discover' && !discover) loadDiscover();
}

function render() {
  document.getElementById('tab-discover').classList.toggle('on', tab === 'discover');
  document.getElementById('tab-mine').classList.toggle('on', tab === 'mine');
  if (tab === 'discover') renderDiscover(); else renderMine();
}

function openMine() { tab = 'mine'; render(); }

function renderMine() {
  const root = document.getElementById('root');
  const reqs = (state.requests || []).map(r => `
    <div class="reqbar">
      <div class="who">🙋 <b>${esc(r.name)}</b> quer entrar em <b>${esc(r.pool_name)}</b></div>
      <div class="acts">
        <button class="btn" onclick="approve('${r.pool_id}',${r.user_id},'approve')">✅ Aprovar</button>
        <button class="btn ghost" onclick="approve('${r.pool_id}',${r.user_id},'deny')">❌ Negar</button>
      </div>
    </div>`).join('');
  const reqBlock = reqs ? '<section>' + reqs + '</section>' : '';
  if (!state.pools.length) {
    root.innerHTML = reqBlock +
      '<p class="empty">Você ainda não está em nenhum bolão.<br>' +
      'Vai na aba <b>🔎 Descobrir</b> e faz sua fezinha! ⚽</p>';
    return;
  }
  root.innerHTML = reqBlock + state.pools.map((pool, pi) => `
    <div class="board">
      <div class="row"><span>${esc(pool.name).toUpperCase()}</span>
        <span class="live">● AO VIVO</span></div>
      ${pool.standings.slice(0,3).map((r,i) => `
        <div class="row"><span>${medals[i]} ${esc(r.name)}</span>
          <span>${pool.pot ? r.chips + ' 🪙' : r.points + ' PTS'}</span></div>`).join('')}
    </div>
    ${pool.pot
      ? `<div class="potline">💰 Prêmio: <b>${pool.pot} fichas</b> · ${esc(pool.payout_label)}
           <div style="font-size:11px;opacity:.75">fichas fictícias, sem dinheiro real —
           pote valendo com escrow Solana tá no roadmap</div></div>`
      : `<div class="potline">Bolão por pontos — valendo de verdade, sem dinheiro 🦓</div>`}
    <section>
      <h2>⚽ Palpitar · trocar</h2>
      ${pool.fixtures.map(f => `
        <div class="fixture">
          <div class="fx-name">${esc(f.home)} x ${esc(f.away)}</div>
          <div class="opts">
            ${['1','X','2'].map(sel => `
              <button class="opt ${f.mine === sel ? 'on' : ''}"
                ${f.locked ? 'disabled' : ''}
                onclick="pick(${pi},'${pool.id}',${f.fixture_id},'${sel}')">
                ${sel === '1' ? esc(f.home) : sel === '2' ? esc(f.away) : 'Empate'}
              </button>`).join('')}
          </div>
          ${f.locked ? '<div class="locked">🔒 Bola rolando — travado</div>' : ''}
        </div>`).join('')}
      <div class="meta" style="font-size:11px;color:#8A7F68">
        A odd é a do momento do toque — quanto mais zebra, mais pontos. 🦓</div>
    </section>
    <section>
      <h2>🎫 Seus palpites</h2>
      ${pool.picks.length ? pool.picks.map(p => `
        <div class="ticket">
          <div class="fx">${p.emoji} ${esc(p.fixture)}</div>
          <div>➜ <span class="sel">${esc(p.selection)}</span> @ ${p.odds}</div>
          <div class="meta">${p.status} · ${p.points} pts</div>
          ${p.verify_url ? `<div class="meta">🔐 <a href="${p.verify_url}"
            target="_blank" rel="noopener">resultado verificado on-chain
            (Merkle proof TxLINE)</a></div>` : ''}
        </div>`).join('')
        : '<p class="empty">Nenhum palpite ainda — toca num time aí em cima! 👆</p>'}
      <div class="stamp">Válido até o apito final</div>
    </section>
    <section>
      <h2>🏆 Classificação</h2>
      <table>${pool.standings.map((r,i) => `
        <tr class="${r.me ? 'me' : ''}">
          <td><span class="medal">${medals[i] ?? (i+1)+'.'}</span>${esc(r.name)}</td>
          ${pool.pot ? `<td class="chips">${r.chips} 🪙</td>` : ''}
          <td class="pts">${r.points}</td></tr>`).join('')}
      </table>
      <button class="leave" onclick="leavePool('${pool.id}')">
        🚪 Sair de ${esc(pool.name)}</button>
    </section>`).join('');
}

function renderDiscover() {
  const root = document.getElementById('root');
  if (!discover) { root.innerHTML = '<p class="empty">Carregando bolões…</p>'; return; }
  const cards = discover.pools.map(p => {
    let btn;
    if (p.my_status === 'member')
      btn = `<button class="btn ghost" onclick="openMine()">✅ Você está nesse — abrir</button>`;
    else if (p.my_status === 'pending')
      btn = `<button class="btn" disabled>⏳ Solicitação enviada</button>`;
    else if (p.visibility === 'public')
      btn = `<button class="btn" onclick="join('${p.id}')">🎯 Fazer minha fezinha</button>`;
    else
      btn = `<button class="btn" onclick="requestJoin('${p.id}')">🙋 Solicitar entrada</button>`;
    return `<div class="card">
      <div class="cname">${esc(p.name)}</div>
      <div class="cby">por ${esc(p.creator)} · ${p.entries} palpiteiro${p.entries === 1 ? '' : 's'}</div>
      <div class="pills">
        <span class="pill">${VIS_LABEL[p.visibility] || ''}</span>
        <span class="pill">${p.buy_in ? 'Entrada: ' + p.buy_in + ' 🪙' : 'Grátis'}</span>
        ${p.buy_in ? `<span class="pill pot">Prêmio: ${p.pot} 🪙</span>` : ''}
        <span class="pill">${esc(p.payout_label)}</span>
      </div>
      ${btn}
    </div>`;
  }).join('');
  root.innerHTML = `
    <section>
      <button class="btn stamp-btn" onclick="toggleForm()">➕ Criar um bolão</button>
      <div id="createForm" style="display:none"></div>
    </section>
    <section>
      <h2>🏟 Bolões abertos</h2>
      ${discover.pools.length ? cards
        : '<p class="empty">Nenhum bolão aberto ainda — cria o primeiro! ⚽</p>'}
      <p class="empty" style="font-size:11px;line-height:1.5">
        <b>Modo pontos</b> funciona valendo agora. No <b>modo pote</b>, as 🪙 são
        fichas fictícias (sem dinheiro real) — escrow on-chain na Solana tá no roadmap.</p>
    </section>`;
}

function renderForm() {
  const f = document.getElementById('createForm');
  if (!f || !discover) return;
  const vOpts = discover.visibility_options
    .map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('');
  const pOpts = discover.payout_options
    .map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('');
  f.innerHTML = `<div class="form">
    <label>Nome do bolão</label>
    <input id="f-name" maxlength="60" placeholder="Bolão da firma">
    <label>Quem pode entrar</label>
    <select id="f-vis">${vOpts}</select>
    <label>Entrada (fichas fictícias — 0 = grátis)</label>
    <input id="f-buyin" type="number" min="0" value="0" inputmode="numeric">
    <label>Premiação</label>
    <select id="f-payout">${pOpts}</select>
    <button class="btn" onclick="createPool()">🏟 Criar bolão</button>
  </div>`;
}

function toggleForm() {
  const f = document.getElementById('createForm');
  if (!f) return;
  if (f.style.display === 'none') { renderForm(); f.style.display = 'block'; }
  else { f.style.display = 'none'; }
}

async function createPool() {
  const name = (document.getElementById('f-name').value || '').trim();
  if (!name) { toast('Dá um nome pro bolão 😉'); return; }
  const body = {
    name,
    visibility: document.getElementById('f-vis').value,
    buy_in: parseInt(document.getElementById('f-buyin').value || '0', 10),
    payout_preset: document.getElementById('f-payout').value,
  };
  const res = await api('/api/create', body);
  if (!res.ok) { toast('😕 Não rolou — tenta de novo'); return; }
  toast('🏟 Bolão criado! Bora chamar a galera.');
  if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  discover = null; await refreshState();
  tab = 'mine'; render(); loadDiscover();
}

async function join(poolId) {
  const res = await api('/api/join', {pool_id: poolId});
  if (!res.ok) { toast('😕 Não consegui te colocar — tenta de novo'); return; }
  toast('🎉 Você entrou! Faz tua fezinha.');
  if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  discover = null; await refreshState();
  tab = 'mine'; render(); loadDiscover();
}

async function requestJoin(poolId) {
  const res = await api('/api/request', {pool_id: poolId});
  if (!res.ok) { toast('😕 Não rolou — tenta de novo'); return; }
  toast('🙋 Pedido enviado! O dono do bolão decide.');
  discover = null; loadDiscover();
}

async function approve(poolId, userId, decision) {
  const res = await api('/api/approve',
    {pool_id: poolId, user_id: userId, decision});
  if (!res.ok) { toast('😕 Não rolou — tenta de novo'); return; }
  toast(decision === 'approve' ? '✅ Entrou no bolão!' : '❌ Recusado.');
  await refreshState(); discover = null; render();
}

async function pick(pi, poolId, fixtureId, sel) {
  const res = await api('/api/pick',
    {pool_id: poolId, fixture_id: fixtureId, selection: sel});
  if (res.status === 409) { toast('⛔ Bola rolando — palpites travados!'); }
  else if (!res.ok) { toast('😕 Não rolou — tenta de novo'); }
  else if (res.data.unchanged) { toast('Você já tá nesse palpite 😉'); }
  else {
    toast(`🎯 ${res.data.selection} @ ${res.data.odds} — vale ${res.data.points} pts`);
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  }
  const st = await api('/api/state', {});
  if (st.ok) { state = st.data; render(); }
}

function leavePool(poolId) {
  const pool = state.pools.find(p => p.id === poolId);
  const name = pool ? pool.name : 'este bolão';
  const doLeave = async (okPressed) => {
    if (!okPressed) return;
    const res = await api('/api/leave', {pool_id: poolId});
    if (!res.ok) { toast('😕 Não rolou — tenta de novo'); return; }
    toast('👋 Você saiu — os palpites foram junto.');
    const st = await api('/api/state', {});
    if (st.ok) { state = st.data; render(); }
  };
  const msg = `Sair de ${name}? Seus palpites vão junto — sem volta.`;
  if (tg.showConfirm) tg.showConfirm(msg, doLeave);
  else doLeave(confirm(msg));
}

load();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


@app.get("/landing", response_class=HTMLResponse)
async def landing() -> str:
    """torcida.app landing preview (deployed as static site on D4)."""
    from pathlib import Path
    page = Path(__file__).resolve().parents[2] / "landing" / "index.html"
    return page.read_text(encoding="utf-8")


@app.get("/api/recap")
async def api_recap() -> JSONResponse:
    return JSONResponse(await _recap_payload())


@app.get("/og.png")
async def og_image():
    """Link-preview card (og:image) for torcida.app."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    return FileResponse(Path(__file__).resolve().parents[2] / "landing" / "og.png",
                        media_type="image/png")


@app.get("/retro", response_class=HTMLResponse)
async def retro() -> str:
    """Retrospecto: team stats of the played games + odds for the next ones."""
    from pathlib import Path
    page = Path(__file__).resolve().parents[2] / "landing" / "retro.html"
    return page.read_text(encoding="utf-8")


# --- judge-facing replay demo (no live match needed) --------------------------

@app.get("/demo", response_class=HTMLResponse)
async def demo_page() -> str:
    """REPLAY TxLINE: the recorded semifinal through the real pipeline."""
    from pathlib import Path
    page = Path(__file__).resolve().parents[2] / "landing" / "demo.html"
    return page.read_text(encoding="utf-8")


@app.get("/demo/demo.json")
async def demo_data():
    from pathlib import Path
    from fastapi.responses import FileResponse
    return FileResponse(Path("data/demo/demo.json"), media_type="application/json")


@app.get("/demo/audio/{name}")
async def demo_audio(name: str):
    from pathlib import Path
    from fastapi.responses import FileResponse
    if not name.replace(".", "").replace("_", "").isalnum() or ".." in name:
        return {"error": "nope"}
    path = Path("data/demo/audio") / name
    if not path.exists():
        return {"error": "not found"}
    return FileResponse(path, media_type="audio/mpeg")
