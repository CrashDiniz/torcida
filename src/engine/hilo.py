"""Hi-Lo ("Pulso da Torcida"): in-play higher/lower micro-questions.

The sponsor's third showcase idea made playable: will a match stat end
above or below a line? A question freezes the current whole-match total of
one stat (goals / corners / cards) plus a half-step line and a wall-clock
horizon; the TxLINE scores snapshot at the deadline settles it — no human
referee. HI wins if the total ends above the line, LO if below (the .5
line means there is never a push). Hits build a streak that multiplies the
payout (100 × streak) on a separate Hi-Lo board, so it never distorts the
main 1X2 pool scoring.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

HORIZON_S = 15 * 60          # question window, wall clock (the game clock
                             # pauses at half-time; wall time keeps it honest)
TEST_HORIZON_S = 90          # 🧪 test questions resolve fast
BASE_POINTS = 100            # payout = BASE_POINTS × streak after the hit
CHOICES = ("hi", "lo")

# stat -> line delta over the current total + display names
TEMPLATES: dict[str, dict] = {
    "goals":   {"delta": 0.5, "pt": "gols",       "en": "goals"},
    "corners": {"delta": 1.5, "pt": "escanteios", "en": "corners"},
    "cards":   {"delta": 0.5, "pt": "cartões",    "en": "cards"},
}


@dataclass
class HiloQuestion:
    pool_id: str
    fixture_id: int
    stat: str
    line: float
    base_value: int
    resolve_at: float
    chat_id: int | None = None
    thread_id: int | None = None
    test: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    status: str = "open"
    result: str | None = None
    final_value: int | None = None


def snapshot_totals(items: list) -> dict[str, int] | None:
    """Whole-match stat totals (both teams) from a /scores/snapshot list.
    Uses the highest-Seq event carrying a Score block — the same authority
    rule the settlement engine applies to goals."""
    best_seq, best = -1, None
    for it in items or []:
        if not isinstance(it, dict) or not it.get("Score"):
            continue
        seq = it.get("Seq") or 0
        if seq >= best_seq:
            best_seq, best = seq, it["Score"]
    if best is None:
        return None
    out = {"goals": 0, "corners": 0, "cards": 0}
    for side in ("Participant1", "Participant2"):
        tot = (best.get(side) or {}).get("Total") or {}
        out["goals"] += tot.get("Goals", 0)
        out["corners"] += tot.get("Corners", 0)
        out["cards"] += (tot.get("YellowCards", 0) + tot.get("RedCards", 0))
    return out


def make_question(pool_id: str, fixture_id: int, stat: str, current: int,
                  chat_id: int | None = None, thread_id: int | None = None,
                  test: bool = False) -> HiloQuestion:
    if stat not in TEMPLATES:
        raise ValueError(f"unknown stat {stat!r}")
    horizon = TEST_HORIZON_S if test else HORIZON_S
    return HiloQuestion(
        pool_id=pool_id, fixture_id=fixture_id, stat=stat,
        line=current + TEMPLATES[stat]["delta"], base_value=current,
        resolve_at=time.time() + horizon,
        chat_id=chat_id, thread_id=thread_id, test=test)


def settle(line: float, value: int) -> str:
    """The half-step line guarantees a winner side."""
    return "hi" if value > line else "lo"


def payout(streak: int) -> int:
    """Points paid for a hit that RAISED the streak to this value."""
    return BASE_POINTS * streak
