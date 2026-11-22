"""Addressed SHA-256 draws; no mutable stream and no run/treatment identity."""
from typing import Literal

from civitas.contracts.base import Id, NonNegative, Record, content_hash

ALGORITHM = "civitas-addressed-sha256-v1"


class DrawAddress(Record):
    algorithm: Literal["civitas-addressed-sha256-v1"] = ALGORITHM
    root_seed: NonNegative
    replica: NonNegative
    process: Id
    entity: Id
    tick: NonNegative
    semantic_slot: Id


class AddressedRNG:
    def __init__(self, root_seed: int, replica: int):
        self._root = DrawAddress(root_seed=root_seed, replica=replica, process="init", entity="init",
                                 tick=0, semantic_slot="init")

    def u64(self, process: str, entity: str, tick: int, semantic_slot: str) -> int:
        address = self._root.model_copy(update=dict(process=process, entity=entity, tick=tick,
                                                    semantic_slot=semantic_slot))
        return int(content_hash(address)[:16], 16)

    def uniform(self, process: str, entity: str, tick: int, semantic_slot: str) -> float:
        # Same open-interval 52-bit mapping as the frozen research RNG, new address version.
        return ((self.u64(process, entity, tick, semantic_slot) >> 12) + 0.5) / (1 << 52)
