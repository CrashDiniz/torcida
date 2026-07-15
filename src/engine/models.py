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


class Visibility(str, Enum):
    PUBLIC = "public"    # listed on the showcase, anyone joins in one tap
    REQUEST = "request"  # listed, but entry needs the creator's approval
    HIDDEN = "hidden"    # off the showcase, join only via invite link


@dataclass
class Pool:
    id: str
    name: str
    creator_id: int          # telegram user id
    payout_preset: PayoutPreset = PayoutPreset.TOP3
    language: str = "pt-BR"  # narrator persona
    narrator_delay_s: int = 0
    buy_in: int = 0          # fictional chips each entry pays into the pot (0 = free)
    visibility: Visibility = Visibility.HIDDEN
    created_at: float = field(default_factory=time.time)
    invite_code: str = field(default_factory=lambda: secrets.token_urlsafe(6))


@dataclass
class Entry:
    pool_id: str
    user_id: int
    display_name: str
    points: int = 0
    joined_at: float = field(default_factory=time.time)


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


@dataclass
class JoinRequest:
    pool_id: str
    user_id: int
    display_name: str
    status: RequestStatus = RequestStatus.PENDING
    requested_at: float = field(default_factory=time.time)


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
