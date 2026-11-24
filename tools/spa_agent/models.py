from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SceneType(str, Enum):
    UNKNOWN = "UNKNOWN"
    LOBBY_SOLO = "LOBBY_SOLO"
    LOBBY_COOP = "LOBBY_COOP"
    PREP_SHOP = "PREP_SHOP"
    CONFIRM_READY = "CONFIRM_READY"
    BATTLE = "BATTLE"
    SETTLEMENT = "SETTLEMENT"


class ActionKind(str, Enum):
    BUY = "BUY"
    PICK_REWARD = "PICK_REWARD"
    UPGRADE_SHOP = "UPGRADE_SHOP"
    REFRESH = "REFRESH"
    DEPLOY = "DEPLOY"
    SELL = "SELL"
    READY = "READY"
    CONFIRM_DIALOG = "CONFIRM_DIALOG"
    WAIT = "WAIT"


@dataclass
class ShopCard:
    slot_idx: int
    x: int
    name: str = ""
    pact: str = ""
    cost: int = 0
    tier: int = 0
    is_free_reward: bool = False


@dataclass
class BenchUnit:
    slot_idx: int
    x: int
    y: int
    name: str = ""
    tier: int = 0
    elite: bool = False
    occupied: bool = True


@dataclass
class ShopState:
    scene: SceneType = SceneType.UNKNOWN
    round_num: int = 0
    gold: int = 0
    hp: int = 100
    deploy_slots_free: int = 0
    shop_level: int = 1
    shop_maxed: bool = False
    upgrade_cost: int | None = None
    upgrade_x: int | None = None
    is_reward_shop: bool = False
    cards: list[ShopCard] = field(default_factory=list)
    bench: list[BenchUnit] = field(default_factory=list)
    synergies: dict[str, int] = field(default_factory=dict)
    raw_texts: list[str] = field(default_factory=list)


@dataclass
class Action:
    kind: ActionKind
    reason: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "reason": self.reason, "params": self.params}
