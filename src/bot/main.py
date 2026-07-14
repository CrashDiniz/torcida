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
                           InlineKeyboardMarkup, Message, WebAppInfo)

from ..engine.models import PayoutPreset, Pick, PickStatus, Pool
from ..engine.odds import parse_snapshot
from ..engine.scoring import points_for
from ..engine.settlement import FixtureState, SettlementService
from ..engine.store import Store
from ..ingest.txline import TxLineClient
from ..narrator.narrator import narrate

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


def _selection_name(selection: str, fixture_label: str | None) -> str:
    """Team name for 1/2 when the fixture label is known (neutral-venue World
    Cup games have no real home side); falls back to casa/visitante."""
    if fixture_label and " x " in fixture_label:
        home, away = fixture_label.split(" x ", 1)
        return {"1": home, "X": "empate", "2": away}.get(selection, selection)
    return SELECTION_LABELS.get(selection, selection)


async def _invite_link(bot: Bot, pool: Pool) -> str:
    me = await bot.me()
    return f"https://t.me/{me.username}?start=join_{pool.invite_code}"


def _share_button(invite: str, pool: Pool,
                  text: str = "📤 Convidar amigos") -> InlineKeyboardButton:
    from urllib.parse import quote
    share = (f"https://t.me/share/url?url={quote(invite)}"
             f"&text={quote(f'Bora pro bolão {pool.name} no Torcida! ⚽')}")
    return InlineKeyboardButton(text=text, url=share)


async def _answer(callback: CallbackQuery, text: str, **kwargs) -> None:
    """callback.answer that tolerates expired queries (queued during downtime)."""
    try:
        await callback.answer(text, **kwargs)
    except Exception:
        log.info("stale callback answer dropped: %s", text)
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
    if payload == "app":
        await open_app(message)
        return
    await start(message)


@router.message(Command("app"))
async def open_app(message: Message) -> None:
    """Mini App entry point (web_app buttons only work in private chats)."""
    url = os.environ.get("WEBAPP_URL")
    if message.chat.type != "private":
        me = await message.bot.get_me()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🎫 Abrir no privado",
            url=f"https://t.me/{me.username}?start=app")]])
        await message.answer(
            "O app abre no privado — seus palpites são segredo 🤫",
            reply_markup=keyboard)
        return
    if not url:
        await message.answer("🚧 App em preparação — volta já!")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🎫 Abrir o Torcida", web_app=WebAppInfo(url=url))]])
    await message.answer(
        "Seu carnê de palpites e o placar ao vivo, só seu 👇",
        reply_markup=keyboard)


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "⚽ <b>Torcida</b> — bolão ao vivo com odds de verdade.\n\n"
        "• /novo <i>nome</i> — criar um bolão neste grupo\n"
        "• /jogos — palpitar nos próximos jogos\n"
        "• /placar — classificação ao vivo\n"
        "• /meus — seus palpites\n"
        "• /boloes — seus bolões\n"
        "• /sair — sair do bolão\n\n"
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
        f"Convite (toca pra copiar e manda pros amigos):\n"
        f"<code>{invite}</code>\n\n"
        f"Escolhe a premiação:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.message(Command("novo"))
async def new_pool(message: Message, command: CommandObject) -> None:
    if message.chat.type == "private":
        await message.answer(
            "Bolão nasce no grupo dos amigos 😉 Me adiciona lá e manda /novo.")
        return
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
        await _answer(callback, "Esse pedido expirou — manda /novo de novo.")
        return
    requester_id, name = pending
    if callback.from_user.id != requester_id:
        await _answer(callback, "Só quem pediu o /novo decide 😉")
        return
    _pending_new.pop(callback.message.chat.id, None)
    await callback.message.edit_reply_markup(reply_markup=None)
    if callback.data.endswith(":no"):
        await _answer(callback, "Beleza, bolão atual mantido.")
        return
    await _create_pool(callback.message, name, requester_id,
                       callback.from_user.first_name or str(requester_id))
    await _answer(callback, "")


@router.callback_query(F.data.startswith("payout:"))
async def choose_payout(callback: CallbackQuery) -> None:
    _, pool_id, preset_value = callback.data.split(":")
    pool = store.pool_by_id(pool_id)
    if pool is None or callback.from_user.id != pool.creator_id:
        await _answer(callback, "Só quem criou o bolão escolhe a premiação 😉")
        return
    preset = PayoutPreset(preset_value)
    with store._conn() as c:  # MVP: direct update
        c.execute("UPDATE pools SET payout_preset=? WHERE id=?", (preset.value, pool_id))
    await callback.message.edit_reply_markup(reply_markup=None)
    theme_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=e, callback_data=f"theme:{pool_id}:{e}")
         for e in THEME_EMOJIS[:4]],
        [InlineKeyboardButton(text=e, callback_data=f"theme:{pool_id}:{e}")
         for e in THEME_EMOJIS[4:]],
    ])
    await callback.message.answer(
        f"Premiação definida: {PAYOUT_LABELS[preset]} ✅\n"
        f"Agora escolhe o tema — ele identifica o bolão em tudo:",
        reply_markup=theme_kb)
    await _answer(callback, "")


THEME_EMOJIS = ["⚽", "🔥", "🏆", "🍻", "🦓", "👑", "🎯", "💰"]


@router.callback_query(F.data.startswith("theme:"))
async def choose_theme(callback: CallbackQuery) -> None:
    _, pool_id, emoji = callback.data.split(":")
    pool = store.pool_by_id(pool_id)
    if pool is None or callback.from_user.id != pool.creator_id:
        await _answer(callback, "Só quem criou o bolão escolhe o tema 😉")
        return
    name = pool.name
    for e in THEME_EMOJIS:  # re-theming replaces the old prefix
        name = name.removeprefix(f"{e} ")
    name = f"{emoji} {name}"
    with store._conn() as c:  # MVP: direct update
        c.execute("UPDATE pools SET name=? WHERE id=?", (name, pool_id))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"Bolão pronto: <b>{html.escape(name)}</b> ✅ Bora palpitar: /jogos",
        parse_mode="HTML")
    await _answer(callback, "")


@router.message(Command("jogos"))
async def fixtures(message: Message) -> None:
    pool = _pool_for_chat(message.chat.id)
    if pool is None:
        await message.answer("Crie um bolão primeiro: /novo nome-do-bolão")
        return
    await _send_fixture_cards(message, pool, message.from_user.id,
                              message.from_user.first_name or "Você")


async def _send_fixture_cards(message: Message, pool: Pool, user_id: int,
                              user_name: str) -> None:
    # cards live in the 🏆 topic when the group has one (keeps resenha clean)
    thread_id = (store.chat_topic(message.chat.id, "bolao")
                 if message.chat.type != "private" else None)
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
        fixture_label = f"{home} x {away}"
        store.set_fixture_label(f["FixtureId"], fixture_label)
        if not odds.live:
            live_flag = " (odds de referência)"
        elif odds.period:
            live_flag = f" (odds do {odds.period.replace('half=1', '1º tempo')})"
        else:
            live_flag = ""
        rows = [[
            InlineKeyboardButton(
                text=f"{home} · {odds.home:.2f}",
                callback_data=f"pick:{pool.id}:{f['FixtureId']}:1:{odds.home:.2f}"),
            InlineKeyboardButton(
                text=f"Empate · {odds.draw:.2f}",
                callback_data=f"pick:{pool.id}:{f['FixtureId']}:X:{odds.draw:.2f}"),
            InlineKeyboardButton(
                text=f"{away} · {odds.away:.2f}",
                callback_data=f"pick:{pool.id}:{f['FixtureId']}:2:{odds.away:.2f}"),
        ]]
        if message.chat.type != "private":
            # per-user private check: callback toasts are the only in-group
            # channel only the tapper sees
            rows.append([InlineKeyboardButton(
                text="👁 Meu palpite (só você vê)",
                callback_data=f"mypick:{pool.id}:{f['FixtureId']}")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        mine = store.pick_for(pool.id, user_id, f["FixtureId"])
        if mine is None:
            current = ""
        elif message.chat.type == "private":
            current = (f"\n✅ Seu palpite: "
                       f"<b>{html.escape(_selection_name(mine.selection, fixture_label))} "
                       f"@ {mine.odds_decimal:.2f}</b> (toque pra trocar)")
        else:  # group card: confirm without leaking the secret pick
            current = (f"\n✅ <b>{html.escape(user_name)}</b> já palpitou aqui 🤫 "
                       f"(👁 mostra o seu · toca numa opção pra trocar)")
        await message.bot.send_message(
            message.chat.id,
            f"⚽ <b>{html.escape(home)} x {html.escape(away)}</b>{live_flag}\n"
            f"Acertou, leva <i>100 × odd</i> em pontos:{current}",
            parse_mode="HTML",
            reply_markup=keyboard,
            message_thread_id=thread_id,
        )
    if thread_id is not None and message.message_thread_id != thread_id:
        await message.answer("👉 Cards de palpite postados no tópico 🏆 Bolão!")


@router.callback_query(F.data.startswith("pick:"))
async def place_pick(callback: CallbackQuery) -> None:
    _, pool_id, fixture_id, selection, odds_str = callback.data.split(":")
    user = callback.from_user
    odds = float(odds_str)
    label = _selection_name(selection, store.fixture_label(int(fixture_id)))
    active = _pool_for_chat(callback.message.chat.id)
    if active is not None and active.id != pool_id:
        await _answer(callback,
                      "♻️ Esse card é de um bolão antigo — manda /jogos "
                      "pra palpitar no atual!", show_alert=True)
        return
    if _fixture_started(int(fixture_id)):
        await _answer(callback, "⛔ Bola rolando — palpites travados pra esse jogo!",
                      show_alert=True)
        return
    store.join(pool_id, user.id, user.first_name or str(user.id))
    existing = store.pick_for(pool_id, user.id, int(fixture_id))
    if existing is not None:
        if existing.selection == selection:
            await _answer(callback,
                          f"Você já palpitou {label} @ {existing.odds_decimal:.2f} 😉")
            return
        store.replace_pick(existing.id, selection, odds, time.time())
        old_label = _selection_name(existing.selection,
                                    store.fixture_label(int(fixture_id)))
        await _answer(callback,
                      f"Palpite trocado: {old_label} ➜ {label} "
                      f"@ {odds:.2f} (vale {points_for(odds)} pts) 🔁")
        return
    pick = Pick(id=uuid.uuid4().hex, pool_id=pool_id, user_id=user.id,
                fixture_id=int(fixture_id), market="1x2",
                selection=selection, odds_decimal=odds)
    store.place_pick(pick)
    await _answer(callback,
                  f"Palpite registrado: {label} @ {odds:.2f} "
                  f"(vale {points_for(odds)} pts) 🎯", show_alert=False)
    # social nudge on the user's FIRST pick in the pool (never reveals the pick)
    if (callback.message.chat.type != "private"
            and len(store.picks_for_user(pool_id, user.id)) == 1):
        pool = active or store.pool_by_id(pool_id)
        if pool is None:
            return
        who = html.escape(user.first_name or "Alguém")
        name = html.escape(pool.name)
        text = (f"🔙 <b>{who}</b> se arrependeu e tá de volta em <b>{name}</b> — "
                f"esse ninguém segura! 😂"
                if store.has_left(pool_id, user.id) else
                f"🔥 <b>{who}</b> acabou de fazer a fezinha em <b>{name}</b> — "
                f"não perca tempo!")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎯 Fazer minha fezinha",
                                 callback_data=f"fezinha:{pool_id}")]])
        await _safe_send(callback.bot, callback.message.chat.id, text,
                         reply_markup=keyboard)


@router.callback_query(F.data.startswith("mypick:"))
async def show_my_pick(callback: CallbackQuery) -> None:
    """Private check of the tapper's pick — alert popup only they can see."""
    _, pool_id, fixture_id = callback.data.split(":")
    pick = store.pick_for(pool_id, callback.from_user.id, int(fixture_id))
    if pick is None:
        await _answer(callback,
                      "Você ainda não palpitou nesse jogo — toca numa opção! 🎯",
                      show_alert=True)
        return
    label = store.fixture_label(int(fixture_id))
    await _answer(callback,
                  f"🤫 Só você vê:\n{label}\n"
                  f"➜ {_selection_name(pick.selection, label)} "
                  f"@ {pick.odds_decimal:.2f} (vale {points_for(pick.odds_decimal)} pts)",
                  show_alert=True)


@router.callback_query(F.data.startswith("fezinha:"))
async def fezinha_button(callback: CallbackQuery) -> None:
    pool_id = callback.data.split(":")[1]
    pool = _pool_for_chat(callback.message.chat.id)
    if pool is None or pool.id != pool_id:
        await _answer(callback, "♻️ Esse aviso é de um bolão antigo — manda /jogos!",
                      show_alert=True)
        return
    await _answer(callback, "")
    await _send_fixture_cards(callback.message, pool, callback.from_user.id,
                              callback.from_user.first_name or "Você")


async def _pick_lines(picks: list[Pick]) -> list[str]:
    lines = []
    for p in picks:
        label = await _fixture_label(p.fixture_id)
        pts = (f"+{p.points_awarded} pts" if p.status == PickStatus.WON
               else f"vale {points_for(p.odds_decimal)} pts"
               if p.status == PickStatus.OPEN else "0 pts")
        lines.append(
            f"{STATUS_EMOJI[p.status]} <b>{html.escape(label)}</b> — "
            f"{html.escape(_selection_name(p.selection, label))} "
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
    text = (f"🎯 Seus palpites em <b>{html.escape(pool.name)}</b>:\n\n"
            + "\n".join(lines))
    # picks are strategic info: answer in DM, keep only a hint in the group
    try:
        await message.bot.send_message(message.from_user.id, text, parse_mode="HTML")
        await message.reply("📬 Te mandei no privado!")
    except Exception:
        # Telegram only lets bots DM users who already started them
        me = await message.bot.get_me()
        await message.reply(
            f"🔒 Palpite é segredo 😉 Abre https://t.me/{me.username} "
            f"e manda /meus lá.")


TOPIC_DEFS = [("anuncios", "📢 Anúncios"), ("bolao", "🏆 Bolão"),
              ("regras", "❓ Como funciona")]


@router.message(Command("salas"))
async def setup_topics(message: Message) -> None:
    chat = message.chat
    if chat.type == "private":
        await message.answer("Esse comando é pra grupos 😉")
        return
    if not getattr(chat, "is_forum", False):
        await message.answer(
            "⚙️ Primeiro ativa os Tópicos: perfil do grupo → Editar → "
            "Tópicos → ligar. Depois manda /salas de novo.")
        return
    member = await message.bot.get_chat_member(chat.id, message.from_user.id)
    if member.status not in ("administrator", "creator"):
        await message.answer("Só admin do grupo pode montar as salas 😉")
        return
    try:
        for key, name in TOPIC_DEFS:
            if store.chat_topic(chat.id, key) is None:
                topic = await message.bot.create_forum_topic(chat.id, name=name)
                store.set_chat_topic(chat.id, key, topic.message_thread_id)
        # announcements are bot-only: a closed topic blocks members from
        # typing, while the bot (topics admin) still posts into it
        try:
            await message.bot.close_forum_topic(
                chat.id, store.chat_topic(chat.id, "anuncios"))
        except Exception as e:  # re-running /salas: already closed is fine
            if "TOPIC_NOT_MODIFIED" not in str(e):
                raise
        try:
            await message.bot.edit_general_forum_topic(chat.id, name="💬 Resenha")
        except Exception as e:  # re-running /salas: name unchanged is fine
            if "TOPIC_NOT_MODIFIED" not in str(e):
                raise
    except Exception:
        log.warning("topic setup failed in chat %s", chat.id, exc_info=True)
        await message.answer(
            "😕 Não consegui criar os tópicos — me promove a admin com "
            "permissão de <b>Gerenciar tópicos</b> e manda /salas de novo.",
            parse_mode="HTML")
        return
    await message.bot.send_message(
        chat.id,
        "📖 <b>Como funciona o Torcida</b>\n\n"
        "• /jogos — palpita nos próximos jogos (1 palpite por jogo; "
        "re-palpite substitui pela odd atual; trava quando a bola rola)\n"
        "• Acertou, leva <i>100 × odd</i> em pontos — zebra vale mais 🦓\n"
        "• /placar — classificação ao vivo\n"
        "• /meus — seus palpites (chegam no privado 🤫)\n"
        "• /sair — sair do bolão\n\n"
        "⚽ Gols e resultados saem em 📢 Anúncios. "
        "Cards de palpite ficam em 🏆 Bolão. Zoeira liberada no chat geral!",
        parse_mode="HTML",
        message_thread_id=store.chat_topic(chat.id, "regras"),
    )
    await message.answer(
        "🏟 Salas prontas: 📢 Anúncios · 🏆 Bolão · ❓ Como funciona ✅")


def _leave_warn(pool_id: str, user_id: int) -> str:
    n = len(store.picks_for_user(pool_id, user_id))
    return (f"\n⚠️ Seus {n} palpite{'s vão' if n != 1 else ' vai'} junto — sem volta."
            if n else "")


def _picks_summary(pool_id: str, user_id: int) -> str:
    """Plain-text pick list for private alert popups (200-char budget)."""
    parts = []
    for p in store.picks_for_user(pool_id, user_id):
        label = store.fixture_label(p.fixture_id)
        parts.append(f"{_selection_name(p.selection, label)} @ {p.odds_decimal:.2f}")
    return " · ".join(parts)


def _pool_leave_rows(user_id: int) -> list[list[InlineKeyboardButton]]:
    rows = []
    for pool in store.pools_for_user(user_id):
        n = len(store.picks_for_user(pool.id, user_id))
        rows.append([InlineKeyboardButton(
            text=f"🚪 {pool.name} · {n} palpite{'s' if n != 1 else ''}",
            callback_data=f"leaveq:{pool.id}:{user_id}")])
    return rows


@router.message(Command("sair"))
async def leave_pool(message: Message) -> None:
    if message.chat.type == "private":
        # DM: pick which pool to leave (covers pools without a bound group)
        rows = _pool_leave_rows(message.from_user.id)
        if not rows:
            await message.answer("Você não está em nenhum bolão. ⚽")
            return
        await message.answer(
            "De qual bolão você quer sair?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return
    pool = _pool_for_chat(message.chat.id)
    if pool is None:
        await message.answer("Não tem bolão ativo neste chat.")
        return
    if all(p.id != pool.id for p in store.pools_for_user(message.from_user.id)):
        await message.answer(
            "Você não está nesse bolão — pra entrar é só palpitar: /jogos 😉")
        return
    rows = [[
        InlineKeyboardButton(
            text="✅ Sair", callback_data=f"leave:{pool.id}:{message.from_user.id}"),
        InlineKeyboardButton(
            text="❌ Ficar", callback_data=f"leave:no:{message.from_user.id}"),
    ]]
    if store.picks_for_user(pool.id, message.from_user.id):
        rows.append([InlineKeyboardButton(
            text="👁 Quais palpites? (só você vê)",
            callback_data=f"leavepicks:{pool.id}:{message.from_user.id}")])
    await message.answer(
        f"Sair de <b>{html.escape(pool.name)}</b>?"
        f"{_leave_warn(pool.id, message.from_user.id)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("leaveq:"))
async def leave_pick_pool(callback: CallbackQuery) -> None:
    """DM list flow: morph the list card into a confirmation, in place."""
    _, pool_id, uid = callback.data.split(":")
    if callback.from_user.id != int(uid):
        await _answer(callback, "Esse botão não é seu 😉")
        return
    pool = store.pool_by_id(pool_id)
    if pool is None:
        await _answer(callback, "Esse bolão não existe mais.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Sair",
                             callback_data=f"leave:{pool.id}:{uid}"),
        InlineKeyboardButton(text="↩️ Voltar",
                             callback_data=f"leavelist:{uid}"),
    ]])
    await callback.message.edit_text(
        f"Sair de <b>{html.escape(pool.name)}</b>?"
        f"{_leave_warn(pool.id, callback.from_user.id)}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await _answer(callback, "")


@router.callback_query(F.data.startswith("leavepicks:"))
async def leave_show_picks(callback: CallbackQuery) -> None:
    _, pool_id, uid = callback.data.split(":")
    if callback.from_user.id != int(uid):
        await _answer(callback, "Esse botão não é seu 😉")
        return
    summary = _picks_summary(pool_id, callback.from_user.id)
    text = (f"🤫 Só você vê — saindo, cancela:\n{summary}" if summary
            else "Você não tem palpite aberto nesse bolão.")
    await _answer(callback, text[:200], show_alert=True)


@router.callback_query(F.data.startswith("leavelist:"))
async def leave_back_to_list(callback: CallbackQuery) -> None:
    if callback.from_user.id != int(callback.data.split(":")[1]):
        await _answer(callback, "Esse botão não é seu 😉")
        return
    rows = _pool_leave_rows(callback.from_user.id)
    if rows:
        await callback.message.edit_text(
            "De qual bolão você quer sair?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await callback.message.edit_text("Você não está em nenhum bolão. ⚽")
    await _answer(callback, "")


async def _next_fixture_hint() -> str | None:
    """'France x Spain começa em ~22h' — grounded in API kickoff times."""
    try:
        assert txline is not None
        fx = await txline.fixtures(start_epoch_day=int(time.time() // 86400))
        now = time.time()
        upcoming = [((f.get("StartTime") or 0) / 1000, f) for f in fx
                    if (f.get("StartTime") or 0) / 1000 > now and f.get("Participant1")]
        if not upcoming:
            return None
        start, f = min(upcoming, key=lambda t: t[0])
        hours = (start - now) / 3600
        when = (f"em ~{hours / 24:.0f} dias" if hours >= 48
                else f"em ~{max(1, round(hours))}h")
        return f"{f['Participant1']} x {f['Participant2']} começa {when}"
    except Exception:
        return None


@router.callback_query(F.data.startswith("leave:"))
async def confirm_leave(callback: CallbackQuery) -> None:
    _, pool_id, uid = callback.data.split(":")
    if callback.from_user.id != int(uid):
        await _answer(callback, "Esse botão não é seu 😉")
        return
    if pool_id == "no":
        await callback.message.edit_reply_markup(reply_markup=None)
        await _answer(callback, "Beleza, você fica! 💪")
        return
    removed = _picks_summary(pool_id, callback.from_user.id)
    if not store.leave(pool_id, callback.from_user.id):
        await callback.message.edit_reply_markup(reply_markup=None)
        await _answer(callback, "Você já não estava nesse bolão.")
        return
    pool = store.pool_by_id(pool_id)
    name = html.escape(pool.name if pool else "bolão")
    who = html.escape(callback.from_user.first_name or "Alguém")
    if removed:
        await _answer(callback, f"👋 Cancelados: {removed}"[:200], show_alert=True)
    else:
        await _answer(callback, "")

    # tell the pool's group there is a free seat (even when leaving via DM)
    bound_chat = store.chat_for_pool(pool_id)
    if bound_chat:
        await _safe_send(
            callback.bot, bound_chat,
            f"👋 <b>{who}</b> saiu de <b>{name}</b> — vaga liberada! "
            f"Bora aproveitar: /jogos")

    hint = await _next_fixture_hint()
    comeback = (f"😢 Se bater o arrependimento: {html.escape(hint)} — "
                f"volta com /jogos 😉" if hint
                else "😢 Se bater o arrependimento, volta com /jogos 😉")
    if callback.message.chat.type == "private":
        # DM flow: morph the card into farewell + refreshed pool list
        rows = _pool_leave_rows(callback.from_user.id)
        text = f"👋 Você saiu de <b>{name}</b>.\n{comeback}"
        if rows:
            await callback.message.edit_text(
                text + "\n\nQuer sair de mais algum?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        else:
            await callback.message.edit_text(text, parse_mode="HTML")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    try:  # farewell in DM; fails silently if the user never started the bot
        await callback.bot.send_message(
            callback.from_user.id,
            f"Você saiu de <b>{name}</b>.\n{comeback}", parse_mode="HTML")
    except Exception:
        log.info("farewell DM to %s skipped", callback.from_user.id)


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
    buttons = []
    for pool in pools:
        invite = await _invite_link(message.bot, pool)
        buttons.append([
            _share_button(invite, pool, text=f"📤 {pool.name[:24]}"),
            InlineKeyboardButton(
                text="🚪 Sair",
                callback_data=f"leaveq:{pool.id}:{message.from_user.id}"),
        ])
    await message.answer("🏟 Seus bolões:\n\n" + "\n".join(lines),
                         parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(Command("placar"))
async def leaderboard(message: Message) -> None:
    pool = _pool_for_chat(message.chat.id)
    if pool is None:
        await message.answer("Crie um bolão primeiro: /novo nome-do-bolão")
        return
    rows = store.standings(pool.id)
    if not rows:
        invite = await _invite_link(message.bot, pool)
        await message.answer(
            f"Ninguém no bolão ainda. Convida a galera — toca pra copiar:\n"
            f"<code>{invite}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[_share_button(invite, pool)]]),
        )
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


async def _safe_send(bot: Bot, chat_id: int, text: str, reply_markup=None) -> None:
    """Announce in the chat's 📢 topic when set up; fall back to the main chat."""
    thread_id = store.chat_topic(chat_id, "anuncios")
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML",
                               message_thread_id=thread_id,
                               reply_markup=reply_markup)
    except Exception:
        if thread_id is None:
            log.warning("announce to chat %s failed", chat_id, exc_info=True)
            return
        try:  # topic may have been deleted by an admin
            await bot.send_message(chat_id, text, parse_mode="HTML",
                                   reply_markup=reply_markup)
        except Exception:
            log.warning("announce to chat %s failed", chat_id, exc_info=True)


async def _send_voice_note(bot: Bot, chat_ids: list[int], ogg) -> None:
    """Voice note to each chat's announcements topic; never raises."""
    from aiogram.types import FSInputFile
    for chat_id in chat_ids:
        try:
            await bot.send_voice(
                chat_id, FSInputFile(ogg),
                message_thread_id=store.chat_topic(chat_id, "anuncios"))
        except Exception:
            log.warning("voice note to chat %s failed", chat_id, exc_info=True)


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
        ogg = await narrate("goal", label, state.home_goals or 0,
                            state.away_goals or 0)
        if ogg:
            await _send_voice_note(bot, [c for _, c in chats], ogg)

    async def on_final(state: FixtureState, settled: int) -> None:
        label = await _fixture_label(state.fixture_id)
        home, away = state.home_goals or 0, state.away_goals or 0
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
                f"<b>{home} x {away}</b>\n\n"
                f"Palpites liquidados. 📊 <b>{html.escape(pool.name)}</b>:\n"
                f"{board}")
            ogg = await narrate("final", label, home, away,
                                leader=rows[0][1] if rows else "")
            if ogg:
                await _send_voice_note(bot, [chat_id], ogg)

    return on_goal, on_final


KICKOFF_ALERT_S = 5 * 60


async def _kickoff_alerts(bot: Bot) -> None:
    """Warn groups holding picks ~5 min before kickoff (API time is source of truth)."""
    alerted: set[int] = set()
    assert txline is not None
    while True:
        try:
            fx = await txline.fixtures(start_epoch_day=int(time.time() // 86400))
            now = time.time()
            for f in fx:
                fid = f.get("FixtureId")
                start_s = (f.get("StartTime") or 0) / 1000  # API sends epoch ms
                delta = start_s - now
                if not fid or fid in alerted or not 0 < delta <= KICKOFF_ALERT_S:
                    continue
                chats = store.chats_for_fixture(fid)
                if not chats:
                    continue
                alerted.add(fid)
                label = await _fixture_label(fid)
                mins = max(1, round(delta / 60))
                text = (f"🔔 <b>Faltam ~{mins} min pro apito!</b>\n"
                        f"⚽ <b>{html.escape(label)}</b> vai começar.\n\n"
                        f"📳 Liga o som e as notificações — os gols saem aqui.\n"
                        f"⛔ Palpites travam na bola rolando — última chance: /jogos")
                for _, chat_id in chats:
                    await _safe_send(bot, chat_id, text)
                log.info("kickoff alert for %s sent to %d chat(s)", fid, len(chats))
        except Exception:
            log.warning("kickoff alert sweep failed", exc_info=True)
        await asyncio.sleep(60)


async def _consume_scores(service: SettlementService) -> None:
    assert txline is not None
    async for event in txline.stream("scores"):
        try:
            await service.handle_event(event)
        except Exception:
            log.exception("settlement failed for event")


async def _log_update(handler, event, data):
    if (getattr(event, "message", None) and event.message.text
            and event.message.from_user):
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
    ("app", "abrir o app (palpites + placar)"),
    ("placar", "classificação ao vivo do bolão"),
    ("meus", "seus palpites"),
    ("boloes", "seus bolões"),
    ("sair", "sair do bolão deste chat"),
    ("salas", "montar tópicos do grupo (admin)"),
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
    kickoff_task = asyncio.create_task(_kickoff_alerts(bot))
    from aiogram.types import BotCommand
    await bot.set_my_commands(
        [BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
    webapp_url = os.environ.get("WEBAPP_URL")
    if webapp_url:
        # fixed "app tab" next to the input field in everyone's DM with the bot
        from aiogram.types import MenuButtonWebApp
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="🎫 Torcida", web_app=WebAppInfo(url=webapp_url)))
    log.info("Torcida bot starting (polling + live settlement)")
    try:
        await dp.start_polling(bot)
    finally:
        scores_task.cancel()
        kickoff_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
