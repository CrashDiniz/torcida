"""Torcida Telegram bot.

Flows:
  /start                – welcome + how it works
  /start join_<code>    – deep-link join (invite links: t.me/<bot>?start=join_<code>)
  /novo <nome>          – create a pool in this chat (creator picks payout preset)
  /jogos                – list upcoming fixtures with pick buttons
  /placar               – live leaderboard of this chat's pool
  /meus                 – my picks in this chat's pool
  /boloes               – pools I participate in
Inline buttons: pick 1/X/2 priced at current consensus odds.
A background task consumes the TxLINE scores stream and announces goals and
final results (with settled leaderboard) in every chat holding picks.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
import uuid

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from ..engine.models import PayoutPreset, Pick, PickStatus, Pool
from ..engine.odds import parse_snapshot
from ..engine.scoring import points_for
from ..engine.settlement import FixtureState, SettlementService
from ..engine.store import Store
from ..ingest.txline import TxLineClient

log = logging.getLogger("bot")
router = Router()

store = Store()
txline: TxLineClient | None = None
settlement: SettlementService | None = None


def _fixture_started(fixture_id: int) -> bool:
    """True once the scores stream has produced any state for the fixture."""
    return settlement is not None and fixture_id in settlement._states

PAYOUT_LABELS = {
    PayoutPreset.WINNER_TAKES_ALL: "🥇 Vencedor leva tudo",
    PayoutPreset.TOP3: "🏆 Top 3 (50/30/20)",
    PayoutPreset.POKER: "🃏 Top 20% (estilo poker)",
}

# chat_id -> pool_id (one active pool per group chat, MVP simplification)
_chat_pools: dict[int, str] = {}
# chat_id -> (requester_id, pool name) awaiting "create another pool?" confirmation
_pending_new: dict[int, tuple[int, str]] = {}

SELECTION_LABELS = {"1": "casa", "X": "empate", "2": "visitante"}
STATUS_EMOJI = {PickStatus.OPEN: "⏳", PickStatus.WON: "✅",
                PickStatus.LOST: "❌", PickStatus.VOID: "⚪"}


def _pool_for_chat(chat_id: int) -> Pool | None:
    pool_id = _chat_pools.get(chat_id)
    if pool_id:
        return store.pool_by_id(pool_id)
    pool = store.pool_by_chat(chat_id)  # restore binding after restart
    if pool is not None:
        _chat_pools[chat_id] = pool.id
    return pool


def _register_chat_pool(chat_id: int, pool: Pool) -> None:
    _chat_pools[chat_id] = pool.id
    store.bind_chat(pool.id, chat_id)


async def _fixture_label(fixture_id: int) -> str:
    label = store.fixture_label(fixture_id)
    if label:
        return label
    try:
        assert txline is not None
        fx = await txline.fixtures(start_epoch_day=int(time.time() // 86400))
        for f in fx:
            if f.get("FixtureId") and f.get("Participant1"):
                store.set_fixture_label(
                    f["FixtureId"], f"{f['Participant1']} x {f['Participant2']}")
    except Exception:
        log.warning("fixture label lookup failed for %s", fixture_id, exc_info=True)
    return store.fixture_label(fixture_id) or f"Jogo #{fixture_id}"


@router.message(CommandStart(deep_link=True))
async def start_deeplink(message: Message, command: CommandObject) -> None:
    payload = command.args or ""
    if payload.startswith("join_"):
        pool = store.pool_by_invite(payload.removeprefix("join_"))
        if pool is None:
            await message.answer("😕 Esse bolão não existe mais.")
            return
        user = message.from_user
        store.join(pool.id, user.id, user.first_name or user.username or str(user.id))
        # memory-only: joins happen in the joiner's DM, which must not steal the
        # pool's persistent group binding (used for goal/final announcements)
        _chat_pools[message.chat.id] = pool.id
        await message.answer(
            f"🎉 <b>{html.escape(user.first_name or 'Você')}</b> entrou em "
            f"<b>{html.escape(pool.name)}</b>!\n"
            f"Premiação: {PAYOUT_LABELS[pool.payout_preset]}\n\n"
            f"Use /jogos para palpitar e /placar para ver a classificação.",
            parse_mode="HTML",
        )
        return
    await start(message)


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "⚽ <b>Torcida</b> — bolão ao vivo com odds de verdade.\n\n"
        "• /novo <i>nome</i> — criar um bolão neste grupo\n"
        "• /jogos — palpitar nos próximos jogos\n"
        "• /placar — classificação ao vivo\n"
        "• /meus — seus palpites\n"
        "• /boloes — seus bolões\n\n"
        "Palpites valem mais quando você acerta a zebra: os pontos são "
        "calculados pela odd de consenso no momento do palpite. 🦓",
        parse_mode="HTML",
    )


async def _create_pool(message: Message, name: str,
                       creator_id: int, creator_name: str) -> None:
    pool = Pool(id=uuid.uuid4().hex, name=name, creator_id=creator_id)
    store.create_pool(pool, telegram_chat_id=message.chat.id)
    store.join(pool.id, creator_id, creator_name)
    _register_chat_pool(message.chat.id, pool)

    me = await message.bot.get_me()
    invite = f"https://t.me/{me.username}?start=join_{pool.invite_code}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"payout:{pool.id}:{preset.value}")
    ] for preset, label in PAYOUT_LABELS.items()])
    await message.answer(
        f"🏟 <b>{html.escape(pool.name)}</b> criado!\n\n"
        f"Convite (manda pros amigos):\n{invite}\n\n"
        f"Escolhe a premiação:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.message(Command("novo"))
async def new_pool(message: Message, command: CommandObject) -> None:
    name = (command.args or "").strip() or f"Bolão de {message.from_user.first_name}"
    existing = _pool_for_chat(message.chat.id)
    if existing is not None:
        _pending_new[message.chat.id] = (message.from_user.id, name)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Criar novo", callback_data="newpool:yes"),
            InlineKeyboardButton(text="❌ Manter atual", callback_data="newpool:no"),
        ]])
        await message.answer(
            f"⚠️ Este grupo já tem um bolão ativo: "
            f"<b>{html.escape(existing.name)}</b>.\n"
            f"Criar <b>{html.escape(name)}</b> mesmo assim?\n"
            f"<i>O grupo passa a usar o novo; o antigo continua valendo "
            f"pra quem já palpitou.</i>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return
    await _create_pool(message, name, message.from_user.id,
                       message.from_user.first_name or str(message.from_user.id))


@router.callback_query(F.data.startswith("newpool:"))
async def confirm_new_pool(callback: CallbackQuery) -> None:
    pending = _pending_new.get(callback.message.chat.id)
    if pending is None:
        await callback.answer("Esse pedido expirou — manda /novo de novo.")
        return
    requester_id, name = pending
    if callback.from_user.id != requester_id:
        await callback.answer("Só quem pediu o /novo decide 😉")
        return
    _pending_new.pop(callback.message.chat.id, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    if callback.data.endswith(":no"):
        await callback.answer("Beleza, bolão atual mantido.")
        return
    await _create_pool(callback.message, name, requester_id,
                       callback.from_user.first_name or str(requester_id))
    await callback.answer()


@router.callback_query(F.data.startswith("payout:"))
async def choose_payout(callback: CallbackQuery) -> None:
    _, pool_id, preset_value = callback.data.split(":")
    pool = store.pool_by_id(pool_id)
    if pool is None or callback.from_user.id != pool.creator_id:
        await callback.answer("Só quem criou o bolão escolhe a premiação 😉")
        return
    preset = PayoutPreset(preset_value)
    with store._conn() as c:  # MVP: direct update
        c.execute("UPDATE pools SET payout_preset=? WHERE id=?", (preset.value, pool_id))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Premiação definida: {PAYOUT_LABELS[preset]} ✅")
    await callback.answer()


@router.message(Command("jogos"))
async def fixtures(message: Message) -> None:
    pool = _pool_for_chat(message.chat.id)
    if pool is None:
        await message.answer("Crie um bolão primeiro: /novo nome-do-bolão")
        return
    assert txline is not None
    import time as _time
    epoch_day = int(_time.time() // 86400)
    fx = await txline.fixtures(start_epoch_day=epoch_day)
    upcoming = sorted(fx, key=lambda f: f.get("StartTime", 0))[:5]
    if not upcoming:
        await message.answer("Nenhum jogo encontrado agora — tenta de novo mais tarde.")
        return
    for f in upcoming:
        odds = parse_snapshot(await txline.odds_snapshot(f["FixtureId"]))
        home, away = f.get("Participant1", "?"), f.get("Participant2", "?")
        store.set_fixture_label(f["FixtureId"], f"{home} x {away}")
        if not odds.live:
            live_flag = " (odds de referência)"
        elif odds.period:
            live_flag = f" (odds do {odds.period.replace('half=1', '1º tempo')})"
        else:
            live_flag = ""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"{home} · {odds.home:.2f}",
                callback_data=f"pick:{pool.id}:{f['FixtureId']}:1:{odds.home:.2f}"),
            InlineKeyboardButton(
                text=f"Empate · {odds.draw:.2f}",
                callback_data=f"pick:{pool.id}:{f['FixtureId']}:X:{odds.draw:.2f}"),
            InlineKeyboardButton(
                text=f"{away} · {odds.away:.2f}",
                callback_data=f"pick:{pool.id}:{f['FixtureId']}:2:{odds.away:.2f}"),
        ]])
        mine = store.pick_for(pool.id, message.from_user.id, f["FixtureId"])
        current = (f"\nSeu palpite: <b>{SELECTION_LABELS[mine.selection]} "
                   f"@ {mine.odds_decimal:.2f}</b> (toque pra trocar)"
                   if mine else "")
        await message.answer(
            f"⚽ <b>{html.escape(home)} x {html.escape(away)}</b>{live_flag}\n"
            f"Acertou, leva <i>100 × odd</i> em pontos:{current}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )


@router.callback_query(F.data.startswith("pick:"))
async def place_pick(callback: CallbackQuery) -> None:
    _, pool_id, fixture_id, selection, odds_str = callback.data.split(":")
    user = callback.from_user
    odds = float(odds_str)
    label = SELECTION_LABELS[selection]
    if _fixture_started(int(fixture_id)):
        await callback.answer("⛔ Bola rolando — palpites travados pra esse jogo!",
                              show_alert=True)
        return
    store.join(pool_id, user.id, user.first_name or str(user.id))
    existing = store.pick_for(pool_id, user.id, int(fixture_id))
    if existing is not None:
        if existing.selection == selection:
            await callback.answer(
                f"Você já palpitou {label} @ {existing.odds_decimal:.2f} 😉")
            return
        store.replace_pick(existing.id, selection, odds, time.time())
        await callback.answer(
            f"Palpite trocado: {SELECTION_LABELS[existing.selection]} ➜ {label} "
            f"@ {odds:.2f} (vale {points_for(odds)} pts) 🔁")
        return
    pick = Pick(id=uuid.uuid4().hex, pool_id=pool_id, user_id=user.id,
                fixture_id=int(fixture_id), market="1x2",
                selection=selection, odds_decimal=odds)
    store.place_pick(pick)
    await callback.answer(
        f"Palpite registrado: {label} @ {odds:.2f} "
        f"(vale {points_for(odds)} pts) 🎯", show_alert=False)


async def _pick_lines(picks: list[Pick]) -> list[str]:
    lines = []
    for p in picks:
        label = await _fixture_label(p.fixture_id)
        pts = (f"+{p.points_awarded} pts" if p.status == PickStatus.WON
               else f"vale {points_for(p.odds_decimal)} pts"
               if p.status == PickStatus.OPEN else "0 pts")
        lines.append(
            f"{STATUS_EMOJI[p.status]} <b>{html.escape(label)}</b> — "
            f"{SELECTION_LABELS.get(p.selection, p.selection)} "
            f"@ {p.odds_decimal:.2f} · {pts}")
    return lines


@router.message(Command("meus"))
async def my_picks(message: Message) -> None:
    if message.chat.type == "private":
        # DM: every pool the user is in, grouped
        sections = []
        for pool in store.pools_for_user(message.from_user.id):
            picks = store.picks_for_user(pool.id, message.from_user.id)
            if picks:
                lines = await _pick_lines(picks)
                sections.append(f"📌 <b>{html.escape(pool.name)}</b>\n"
                                + "\n".join(lines))
        if not sections:
            await message.answer("Você ainda não palpitou em nenhum bolão. ⚽")
            return
        await message.answer("🎯 Seus palpites:\n\n" + "\n\n".join(sections),
                             parse_mode="HTML")
        return
    pool = _pool_for_chat(message.chat.id)
    if pool is None:
        await message.answer("Crie um bolão primeiro: /novo nome-do-bolão")
        return
    picks = store.picks_for_user(pool.id, message.from_user.id)
    if not picks:
        await message.answer("Você ainda não palpitou. Manda um /jogos! ⚽")
        return
    lines = await _pick_lines(picks)
    await message.answer(
        f"🎯 Seus palpites em <b>{html.escape(pool.name)}</b>:\n\n"
        + "\n".join(lines),
        parse_mode="HTML",
    )


@router.message(Command("boloes"))
async def my_pools(message: Message) -> None:
    pools = store.pools_for_user(message.from_user.id)
    if not pools:
        await message.answer("Você não está em nenhum bolão. Crie um: /novo nome")
        return
    lines = []
    for pool in pools:
        rows = store.standings(pool.id)
        pos = next((i + 1 for i, (uid, _, _) in enumerate(rows)
                    if uid == message.from_user.id), None)
        points = next((pts for uid, _, pts in rows
                       if uid == message.from_user.id), 0)
        rank = f"#{pos} de {len(rows)}" if pos else "—"
        lines.append(f"• <b>{html.escape(pool.name)}</b> — "
                     f"{points} pts ({rank})")
    await message.answer("🏟 Seus bolões:\n\n" + "\n".join(lines),
                         parse_mode="HTML")


@router.message(Command("placar"))
async def leaderboard(message: Message) -> None:
    pool = _pool_for_chat(message.chat.id)
    if pool is None:
        await message.answer("Crie um bolão primeiro: /novo nome-do-bolão")
        return
    rows = store.standings(pool.id)
    if not rows:
        await message.answer("Ninguém no bolão ainda. Manda o link de convite!")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'{i + 1}.'} "
        f"<b>{html.escape(name)}</b> — {points} pts"
        for i, (_, name, points) in enumerate(rows)
    ]
    await message.answer(
        f"📊 <b>{html.escape(pool.name)}</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


async def _safe_send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        log.warning("announce to chat %s failed", chat_id, exc_info=True)


def _make_announcers(bot: Bot):
    async def on_goal(state: FixtureState) -> None:
        chats = store.chats_for_fixture(state.fixture_id)
        if not chats:
            return
        label = await _fixture_label(state.fixture_id)
        text = (f"⚽ <b>GOOOL!</b>\n{html.escape(label)}: "
                f"<b>{state.home_goals} x {state.away_goals}</b>")
        for _, chat_id in chats:
            await _safe_send(bot, chat_id, text)

    async def on_final(state: FixtureState, settled: int) -> None:
        label = await _fixture_label(state.fixture_id)
        medals = ["🥇", "🥈", "🥉"]
        for pool_id, chat_id in store.chats_for_fixture(state.fixture_id):
            pool = store.pool_by_id(pool_id)
            rows = store.standings(pool_id)
            board = "\n".join(
                f"{medals[i] if i < 3 else f'{i + 1}.'} "
                f"<b>{html.escape(name)}</b> — {points} pts"
                for i, (_, name, points) in enumerate(rows))
            await _safe_send(
                bot, chat_id,
                f"🏁 <b>Fim de jogo!</b>\n{html.escape(label)}: "
                f"<b>{state.home_goals} x {state.away_goals}</b>\n\n"
                f"Palpites liquidados. 📊 <b>{html.escape(pool.name)}</b>:\n"
                f"{board}")

    return on_goal, on_final


async def _consume_scores(service: SettlementService) -> None:
    assert txline is not None
    async for event in txline.stream("scores"):
        try:
            await service.handle_event(event)
        except Exception:
            log.exception("settlement failed for event")


async def _log_update(handler, event, data):
    if getattr(event, "message", None) and event.message.text:
        m = event.message
        log.info("msg %s(%s) chat=%s: %s", m.from_user.first_name,
                 m.from_user.id, m.chat.id, m.text)
    elif getattr(event, "callback_query", None):
        cq = event.callback_query
        log.info("callback %s(%s): %s", cq.from_user.first_name,
                 cq.from_user.id, cq.data)
    return await handler(event, data)


BOT_COMMANDS = [
    ("novo", "criar um bolão neste grupo"),
    ("jogos", "palpitar nos próximos jogos"),
    ("placar", "classificação ao vivo do bolão"),
    ("meus", "seus palpites"),
    ("boloes", "seus bolões"),
]


async def main() -> None:
    global txline, settlement
    logging.basicConfig(level=logging.INFO)
    from dotenv import load_dotenv
    load_dotenv(".env")
    txline = TxLineClient.from_env()
    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
    dp = Dispatcher()
    dp.update.outer_middleware(_log_update)
    dp.include_router(router)
    on_goal, on_final = _make_announcers(bot)
    settlement = SettlementService(store, on_goal=on_goal, on_final=on_final)
    scores_task = asyncio.create_task(_consume_scores(settlement))
    from aiogram.types import BotCommand
    await bot.set_my_commands(
        [BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
    log.info("Torcida bot starting (polling + live settlement)")
    try:
        await dp.start_polling(bot)
    finally:
        scores_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
