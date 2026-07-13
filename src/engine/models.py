"""Domain models for Torcida pools."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum


class PickOutcome(str, Enum):
    HOME = "1"
    DRAW = "X"
    AWAY = "2"


class PickStatus(str, Enum):
    OPEN = "open"
    WON = "won"
    LOST = "lost"
    VOID = "void"  # match cancelled / no odds available


class PayoutPreset(str, Enum):
    WINNER_TAKES_ALL = "winner_takes_all"
    TOP3 = "top3"           # 50 / 30 / 20
    POKER = "poker"          # top 20% paid, harmonic decay


@dataclass
class Pool:
    id: str
    name: str
    creator_id: int          # telegram user id
    payout_preset: PayoutPreset = PayoutPreset.TOP3
    language: str = "pt-BR"  # narrator persona
    narrator_delay_s: int = 0
    entry_points: int = 1000  # virtual points per entry (free mode)
    created_at: float = field(default_factory=time.time)
    invite_code: str = field(default_factory=lambda: secrets.token_urlsafe(6))


@dataclass
class Entry:
    pool_id: str
    user_id: int
    display_name: str
    points: int = 0
    joined_at: float = field(default_factory=time.time)


@dataclass
class Pick:
    id: str
    pool_id: str
    user_id: int
    fixture_id: int
    market: str              # "1x2" | "flash_goal_before" | ...
    selection: str           # PickOutcome value or market-specific
    odds_decimal: float      # consensus odds at pick time
    placed_at: float = field(default_factory=time.time)
    status: PickStatus = PickStatus.OPEN
    points_awarded: int = 0
