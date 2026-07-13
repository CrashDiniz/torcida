"""Torcida Telegram bot.

Flows:
  /start                – welcome + how it works
  /start join_<code>    – deep-link join (invite links: t.me/<bot>?start=join_<code>)
  /novo <nome>          – create a pool in this chat (creator picks payout preset)
  /jogos                – list upcoming fixtures with pick buttons
  /placar               – live leaderboard of this chat's pool
Inline buttons: pick 1/X/2 priced at current consensus odds.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import uuid

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from ..engine.models import PayoutPreset, Pick, Pool
from ..engine.odds import parse_snapshot
from ..engine.scoring import points_for
from ..engine.store import Store
from ..ingest.txline import TxLineClient

log = logging.getLogger("bot")
router = Router()

store = Store()
txline: TxLineClient | None = None

PAYOUT_LABELS = {
    PayoutPreset.WINNER_TAKES_ALL: "🥇 Vencedor leva tudo",
    PayoutPreset.TOP3: "🏆 Top 3 (50/30/20)",
    PayoutPreset.POKER: "🃏 Top 20% (estilo poker)",
}

# chat_id -> pool_id (one active pool per group chat, MVP simplification)
_chat_pools: dict[int, str] = {}


def _pool_for_chat(chat_id: int) -> Pool | None:
    pool_id = _chat_pools.get(chat_id)
    return store.pool_by_id(pool_id) if pool_id else None


def _register_chat_pool(chat_id: int, pool: Pool) -> None:
    _chat_pools[chat_id] = pool.id


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
        _register_chat_pool(message.chat.id, pool)
        await message.answer(
            f"🎉 <b>{html.escape(user.first_name or 'Você')}</b> entrou no bolão "
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
        "• /placar — classificação ao vivo\n\n"
        "Palpites valem mais quando você acerta a zebra: os pontos são "
        "calculados pela odd de consenso no momento do palpite. 🦓",
        parse_mode="HTML",
    )


@router.message(Command("novo"))
async def new_pool(message: Message, command: CommandObject) -> None:
    name = (command.args or "").strip() or f"Bolão de {message.from_user.first_name}"
    pool = Pool(id=uuid.uuid4().hex, name=name, creator_id=message.from_user.id)
    store.create_pool(pool, telegram_chat_id=message.chat.id)
    store.join(pool.id, message.from_user.id,
               message.from_user.first_name or str(message.from_user.id))
    _register_chat_pool(message.chat.id, pool)

    me = await message.bot.get_me()
    invite = f"https://t.me/{me.username}?start=join_{pool.invite_code}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"payout:{pool.id}:{preset.value}")
    ] for preset, label in PAYOUT_LABELS.items()])
    await message.answer(
        f"🏟 Bolão <b>{html.escape(pool.name)}</b> criado!\n\n"
        f"Convite (manda pros amigos):\n{invite}\n\n"
        f"Escolhe a premiação:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


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
        live_flag = "" if odds.live else " (odds de referência)"
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
        await message.answer(
            f"⚽ <b>{html.escape(home)} x {html.escape(away)}</b>{live_flag}\n"
            f"Acertou, leva <i>100 × odd</i> em pontos:",
            parse_mode="HTML",
            reply_markup=keyboard,
        )


@router.callback_query(F.data.startswith("pick:"))
async def place_pick(callback: CallbackQuery) -> None:
    _, pool_id, fixture_id, selection, odds_str = callback.data.split(":")
    user = callback.from_user
    store.join(pool_id, user.id, user.first_name or str(user.id))
    pick = Pick(id=uuid.uuid4().hex, pool_id=pool_id, user_id=user.id,
                fixture_id=int(fixture_id), market="1x2",
                selection=selection, odds_decimal=float(odds_str))
    store.place_pick(pick)
    label = {"1": "casa", "X": "empate", "2": "visitante"}[selection]
    await callback.answer(
        f"Palpite registrado: {label} @ {float(odds_str):.2f} "
        f"(vale {points_for(float(odds_str))} pts) 🎯", show_alert=False)


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


async def main() -> None:
    global txline
    logging.basicConfig(level=logging.INFO)
    from dotenv import load_dotenv
    load_dotenv(".env")
    txline = TxLineClient.from_env()
    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Torcida bot starting (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
