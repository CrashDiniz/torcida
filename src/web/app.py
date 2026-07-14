"""Torcida Mini App: manage picks + live leaderboard inside Telegram.

Run: .venv/bin/uvicorn src.web.app:app --port 8090
Auth: Telegram WebApp initData (HMAC) — no passwords, no sessions.
Shares the bot's sqlite store. Picks placed here fetch the CURRENT odds
server-side (no stale card odds) and lock at kickoff (API StartTime).
"""
from __future__ import annotations

import os
import time

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src.engine.models import Pick, PickStatus
from src.engine.odds import parse_snapshot
from src.engine.scoring import points_for
from src.engine.store import Store
from src.ingest.txline import TxLineClient

from .auth import validate_init_data

load_dotenv()
app = FastAPI(title="Torcida Mini App")
store = Store()
txline = TxLineClient.from_env()

STATUS_LABELS = {PickStatus.OPEN: ("⏳", "aberto"), PickStatus.WON: ("✅", "ganhou"),
                 PickStatus.LOST: ("❌", "perdeu")}

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


def fixture_locked(fixture: dict, now: float | None = None) -> bool:
    """Kickoff lock by API start time (ms epoch) — source of truth for the app."""
    start_s = (fixture.get("StartTime") or 0) / 1000
    return start_s <= (now or time.time())


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
            picks.append({"fixture": label,
                          "selection": _selection_name(p.selection, label),
                          "odds": f"{p.odds_decimal:.2f}", "points": pts,
                          "emoji": emoji, "status": status})
        fixtures = []
        for f in upcoming:
            home, away = f.get("Participant1", "?"), f.get("Participant2", "?")
            fixtures.append({
                "fixture_id": f["FixtureId"], "home": home, "away": away,
                "locked": fixture_locked(f),
                "mine": mine_by_fixture.get(f["FixtureId"]),
            })
        standings = [{"name": name, "points": points, "me": row_uid == uid}
                     for row_uid, name, points in store.standings(pool.id)]
        pools.append({"id": pool.id, "name": pool.name, "picks": picks,
                      "fixtures": fixtures, "standings": standings})
    return {"first_name": first_name, "pools": pools}


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
    label = f"{fixture.get('Participant1', '?')} x {fixture.get('Participant2', '?')}"
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
</style>
</head>
<body>
<header>
  <div class="badge">⚽</div>
  <h1>Torcida</h1>
  <small>Associação de Palpiteiros ★ Fundada na Copa</small>
</header>
<div id="root"><p class="empty">Carregando o placar…</p></div>
<div id="toast"></div>
<script>
const tg = window.Telegram?.WebApp;
let state = null;

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
  render();
}

function render() {
  const root = document.getElementById('root');
  if (!state.pools.length) {
    root.innerHTML = '<p class="empty">Você ainda não está em nenhum bolão.<br>' +
      'Manda /jogos no grupo e faz sua fezinha! ⚽</p>';
    return;
  }
  const medals = ['🥇','🥈','🥉'];
  root.innerHTML = state.pools.map((pool, pi) => `
    <div class="board">
      <div class="row"><span>${esc(pool.name).toUpperCase()}</span>
        <span class="live">● AO VIVO</span></div>
      ${pool.standings.slice(0,3).map((r,i) => `
        <div class="row"><span>${medals[i]} ${esc(r.name)}</span>
          <span>${r.points} PTS</span></div>`).join('')}
    </div>
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
        </div>`).join('')
        : '<p class="empty">Nenhum palpite ainda — toca num time aí em cima! 👆</p>'}
      <div class="stamp">Válido até o apito final</div>
    </section>
    <section>
      <h2>🏆 Classificação</h2>
      <table>${pool.standings.map((r,i) => `
        <tr class="${r.me ? 'me' : ''}">
          <td><span class="medal">${medals[i] ?? (i+1)+'.'}</span>${esc(r.name)}</td>
          <td class="pts">${r.points}</td></tr>`).join('')}
      </table>
      <button class="leave" onclick="leavePool('${pool.id}', '${esc(pool.name)}')">
        🚪 Sair de ${esc(pool.name)}</button>
    </section>`).join('');
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

function leavePool(poolId, name) {
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
