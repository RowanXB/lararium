from dataclasses import dataclass
from typing import Literal

type ActorType = Literal["user", "rule", "connector", "agent", "system"]


@dataclass(frozen=True, slots=True)
class Actor:
    type: ActorType
    id: str
