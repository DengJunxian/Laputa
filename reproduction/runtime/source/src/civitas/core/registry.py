from collections import defaultdict
from collections.abc import Callable

from civitas.contracts.base import require_unique
from civitas.contracts.config import MechanismSpec
from civitas.contracts.ports import Mechanism
from civitas.contracts.state import TickContext


class MechanismRegistry:
    def __init__(self, mechanisms: list[Mechanism], compositors: dict[str, Callable] | None = None,
                 reserved_fields: frozenset[str] = frozenset()):
        self.mechanisms = tuple(sorted(mechanisms, key=lambda m: m.spec.mechanism_id))
        self.compositors = dict(compositors or {})
        require_unique([m.spec.mechanism_id for m in self.mechanisms], "mechanism ID")
        writers = defaultdict(list)
        for mechanism in self.mechanisms:
            spec = MechanismSpec.model_validate(mechanism.spec)
            for field in spec.writes:
                if field in reserved_fields:
                    raise ValueError(f"Mechanism cannot write scenario-owned field: {field}")
                writers[field].append(spec)
        for field, specs in writers.items():
            if len(specs) > 1 and (field not in self.compositors or len({s.phase for s in specs}) != 1):
                raise ValueError(f"Multiple writers require one phase and explicit compositor: {field}")
        if self.compositors.keys() - writers.keys():
            raise ValueError("Compositor references an undeclared field")

    def apply(self, phase: str, context: TickContext) -> dict:
        proposed = defaultdict(list)
        for mechanism in self.mechanisms:
            spec = mechanism.spec
            if spec.phase != phase:
                continue
            restricted = context.model_copy(update={"data": {k: v for k, v in context.data.items() if k in spec.reads}})
            patch = restricted.model_copy(update={"data": mechanism.propose(restricted)}).data
            if patch.keys() - set(spec.writes):
                raise ValueError("Mechanism wrote an undeclared field")
            for field, value in patch.items():
                proposed[field].append(value)
        data = context.model_dump()["data"]
        for field, values in proposed.items():
            data[field] = self.compositors[field](tuple(values)) if field in self.compositors else values[0]
        return data
