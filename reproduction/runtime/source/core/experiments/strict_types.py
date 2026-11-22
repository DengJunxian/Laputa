"""Strict, immutable record foundations shared by research schemas.

Extracted without behavior changes in Phase 2 to break schema import cycles.
The legacy v1 configuration loader remains independent. Research input is a
finite JSON value tree; YAML is restricted to that same scalar vocabulary.
Numbers are never repaired or converted from strings. A declared float needs
a binary64 Python float, and a declared integer needs an actual int (not bool).
"""
from __future__ import annotations

import json
import math
import re
from bisect import bisect_right
from itertools import accumulate, chain
from operator import index
from pathlib import Path
from typing import Any, Literal, TypeVar
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResearchInputError(ValueError):
    """The input cannot represent a strict research record."""


def _immutable(*args: Any, **kwargs: Any) -> None:
    raise TypeError("Research record containers are immutable")


class _FrozenList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = _immutable
    pop = remove = reverse = sort = __iadd__ = __imul__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


class _FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = _immutable
    update = __ior__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


class SealedJSONObjectV1(_FrozenDict):
    """A validated JSON object shareable across read-only barriers."""

    __slots__ = ('_sealed',)
    __setattr__ = __delattr__ = _immutable

    def __new__(cls, wire):
        if not SealedJSONObjectV1.is_sealed(wire):
            if type(wire) is not dict:
                raise ResearchInputError("Sealed objects require JSON mappings")
            _check_tree(wire)
            wire = _freeze(wire)
        value = super().__new__(cls)
        dict.update(value, wire)
        object.__setattr__(value, '_sealed', True)
        return value

    def __init__(self, *args, **kwargs):
        pass

    @staticmethod
    def is_sealed(value):
        return isinstance(value, SealedJSONObjectV1) and getattr(value, '_sealed', False) is True


@dataclass(frozen=True, slots=True)
class _SealedJSONBlock:
    rows: tuple
    canonical: bytes | None = None


class SealedJSONRowsV1(_FrozenList):
    """A validated immutable prefix and newly checked JSON rows."""

    __slots__ = ('_blocks',)
    __setattr__ = __delattr__ = _immutable

    def __new__(cls, prefix, tail):
        if prefix is not None and type(prefix) is not SealedJSONRowsV1:
            raise ResearchInputError("Unsupported sealed row format")
        if type(tail) is not list or any(type(row) is not dict for row in tail):
            raise ResearchInputError("Sealed rows require JSON objects")
        _check_tree(tail)
        value = super().__new__(cls)
        if prefix is not None:
            list.extend(value, prefix)
        rows = tuple(_freeze(tail))
        list.extend(value, rows)
        blocks = prefix._blocks if prefix is not None else ()
        object.__setattr__(value, '_blocks', (*blocks, _SealedJSONBlock(rows)) if rows else blocks)
        return value

    def __init__(self, prefix, tail):
        pass

    def retains(self, prefix):
        return len(self._blocks) >= len(prefix._blocks) and all(
            new is old for new, old in zip(self._blocks, prefix._blocks)
        )


@dataclass(frozen=True, slots=True)
class _SealedRecordBlock:
    rows: tuple
    keys: frozenset
    canonical: bytes | None = None


class SealedRecordRows(Sequence):
    """Immutable typed records in wire order, indexed by shared blocks."""

    __slots__ = ('record_type', 'key', '_blocks', '_ends')
    __setattr__ = __delattr__ = append = extend = _immutable

    def __new__(cls, record_type, key, rows, *, prefix=None):
        rows = tuple(rows)
        if not issubclass(record_type, StrictResearchModel) or any(type(row) is not record_type for row in rows):
            raise ResearchInputError("Sealed history requires the declared immutable record type")
        keys = frozenset(getattr(row, key) for row in rows)
        if prefix is not None:
            if type(prefix) is not SealedRecordRows or prefix.record_type is not record_type or prefix.key != key:
                raise ResearchInputError("Sealed history field differs from its prefix")
            old_blocks = prefix._blocks
        else:
            old_blocks = ()
        if len(keys) != len(rows) or any(not keys.isdisjoint(block.keys) for block in old_blocks):
            raise ValueError(f"Duplicate {key}")
        blocks = []
        # Bound the transient allocation when a cold archive first encodes its blocks.
        for offset in range(0, len(rows), 2048):
            chunk = rows[offset:offset+2048]
            blocks.append(_SealedRecordBlock(chunk, frozenset(getattr(row, key) for row in chunk)))
        retained = (*old_blocks, *blocks)
        value = super().__new__(cls)
        object.__setattr__(value, 'record_type', record_type)
        object.__setattr__(value, 'key', key)
        object.__setattr__(value, '_blocks', retained)
        object.__setattr__(value, '_ends', tuple(accumulate(len(block.rows) for block in retained)))
        return value

    def appended(self, rows):
        return SealedRecordRows(self.record_type, self.key, rows, prefix=self) if rows else self

    def retains(self, prefix):
        return (self.record_type is prefix.record_type and self.key == prefix.key
                and len(self._blocks) >= len(prefix._blocks)
                and all(old is new for old, new in zip(prefix._blocks, self._blocks)))

    def __len__(self):
        return self._ends[-1] if self._ends else 0

    def __iter__(self):
        return chain.from_iterable(block.rows for block in self._blocks)

    def __getitem__(self, position):
        if isinstance(position, slice):
            return [self[i] for i in range(*position.indices(len(self)))]
        position = index(position)
        if position < 0:
            position += len(self)
        if not 0 <= position < len(self):
            raise IndexError('history index out of range')
        block = bisect_right(self._ends, position)
        start = self._ends[block-1] if block else 0
        return self._blocks[block].rows[position-start]

    def __eq__(self, other):
        if self is other:
            return True
        if isinstance(other, (SealedRecordRows, list)):
            return len(self) == len(other) and all(left == right for left, right in zip(self, other))
        return NotImplemented

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        if isinstance(value, SealedJSONObjectV1) and getattr(value, '_sealed', False) is True:
            return value
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        if type(value) is SealedJSONRowsV1:
            return value
        return _FrozenList(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _check_tree(value: Any, path: str = "$", active: set[int] | None = None) -> None:
    """Reject nonfinite values, foreign objects, non-string keys and cycles."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ResearchInputError(f"Nonfinite number at {path}")
        return
    if isinstance(value, StrictResearchModel):
        # Instances are already validated and recursively frozen; Pydantic
        # also revalidates them when they occur in a declared model field.
        return
    if type(value) in (SealedJSONRowsV1, SealedRecordRows) or (
            isinstance(value, SealedJSONObjectV1) and getattr(value, '_sealed', False) is True):
        return
    if not isinstance(value, (dict, list, tuple)):
        raise ResearchInputError(f"Non-JSON value at {path}: {type(value).__name__}")
    active = set() if active is None else active
    if id(value) in active:
        raise ResearchInputError(f"Cyclic input at {path}")
    active.add(id(value))
    try:
        is_object = isinstance(value, dict)
        children = value.items() if is_object else enumerate(value)
        for key, item in children:
            if is_object and type(key) is not str:
                raise ResearchInputError(f"Object keys must be strings at {path}")
            item_type = type(item)
            if item is None or item_type in (str, bool, int) or (item_type is float and math.isfinite(item)):
                continue
            item_path = f"{path}.{key}" if is_object else f"{path}[{key}]"
            _check_tree(item, item_path, active)
    finally:
        active.remove(id(value))


def _check_scalar_preservation(raw: Any, validated: Any) -> None:
    """Reject scalar coercion after Pydantic resolves unions and containers.

Pydantic strict floats accept ints, and numeric Literals compare equal to bool
or 1.0. Comparing the actual result closes those routes for list, Sequence,
Mapping, tuple, TypedDict and union fields without guessing the selected type.
Nested StrictResearchModel objects validate their own fields in the same way.
"""
    if raw is None or type(raw) in (str, bool, int, float):
        if type(raw) is not type(validated):
            raise ValueError(
                f"Scalar coercion is forbidden: {type(raw).__name__} to {type(validated).__name__}"
            )
    elif isinstance(raw, Mapping) and isinstance(validated, Mapping):
        if raw is validated and SealedJSONObjectV1.is_sealed(raw):
            return
        for key, value in raw.items():
            if key in validated:
                _check_scalar_preservation(value, validated[key])
    elif isinstance(raw, (list, tuple)) and isinstance(validated, Sequence):
        if raw is validated and type(raw) is SealedJSONRowsV1:
            return
        for value, result in zip(raw, validated):
            _check_scalar_preservation(value, result)


class StrictResearchModel(BaseModel):
    """Finite records with exact scalar types and recursively frozen containers.

    Validated list/dict containers retain their JSON shapes and normal read APIs.
    Nested model fields must also derive from this class; ordinary/legacy
    BaseModel instances are rejected, not trusted as prevalidated input.
Normal mutation methods and model_copy(update=...) cannot bypass validation.
This is an application invariant, not a sandbox against object.__setattr__.
"""

    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, allow_inf_nan=False,
        validate_default=True, revalidate_instances="always",
    )

    @model_validator(mode="before")
    @classmethod
    def _finite_input(cls, value: Any) -> Any:
        _check_tree(value)
        return value

    @field_validator("*", mode="wrap")
    @classmethod
    def _exact_scalars(cls, value: Any, handler: Any) -> Any:
        result = handler(value)
        _check_scalar_preservation(value, result)
        return result

    @model_validator(mode="after")
    def _freeze_containers(self):
        for name in type(self).model_fields:
            value = getattr(self, name)
            _check_tree(value, f"$.{name}")
            object.__setattr__(self, name, _freeze(value))
        return self

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any):
        if kwargs.get("strict") is False or kwargs.get("extra") not in (None, "forbid"):
            raise ResearchInputError("Research validation cannot relax strictness or extra fields")
        return super().model_validate(obj, **kwargs)

    @classmethod
    def model_validate_json(cls, json_data: str | bytes | bytearray, **kwargs: Any):
        return cls.model_validate(strict_json_loads(json_data), **kwargs)

    @classmethod
    def model_validate_strings(cls, obj: Any, **kwargs: Any):
        raise ResearchInputError("String-to-scalar coercion is forbidden for research records")

    @classmethod
    def model_construct(cls, _fields_set=None, **values: Any):
        raise ResearchInputError("Research records require validation; model_construct is unavailable")

    def model_copy(self, *, update=None, deep=False):
        payload = self.model_dump(round_trip=True)
        if update:
            payload.update(update)
        return type(self).model_validate(payload)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ResearchInputError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ResearchInputError(f"Nonfinite numeric constant: {value}")


def _finite_float(text: str) -> float:
    result = float(text)
    if not math.isfinite(result):
        raise ResearchInputError("Numeric literal exceeds finite binary64 range")
    return result


def strict_json_loads(text: str | bytes | bytearray) -> Any:
    """Decode one JSON document, preserving numbers and null without repair."""
    try:
        value = json.loads(text, object_pairs_hook=_object_without_duplicates,
                           parse_constant=_reject_constant, parse_float=_finite_float)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ResearchInputError(f"Invalid JSON: {exc}") from exc
    _check_tree(value)
    return value


class _StrictYamlLoader(yaml.SafeLoader):
    # Do not mutate SafeLoader's shared resolver table or the legacy loader.
    yaml_implicit_resolvers = {}

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent) or self.peek_event().anchor is not None:
            raise ResearchInputError("YAML anchors/aliases are not research values; expand explicitly")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            raise ResearchInputError("Expected a YAML mapping")
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if type(key) is not str:
                raise ResearchInputError("YAML mapping keys must be strings")
            if key == "<<":
                raise ResearchInputError("YAML merge keys are not supported in research input")
            if key in result:
                raise ResearchInputError(f"Duplicate YAML key: {key!r}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


_StrictYamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:null", re.compile(r"^(?:null|~|)$"), ["n", "~", ""])
_StrictYamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), ["t", "f"])
_StrictYamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int", re.compile(r"^-?(?:0|[1-9][0-9]*)$"), list("-0123456789"))
_StrictYamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+)$"),
    list("-0123456789"))
_StrictYamlLoader.add_implicit_resolver(
    "!nonfinite", re.compile(r"^[+-]?\.?(?:nan|inf(?:inity)?)$", re.IGNORECASE),
    list("+-.nNiI"))
_StrictYamlLoader.add_constructor("!nonfinite", lambda loader, node: _reject_constant(node.value))
_StrictYamlLoader.add_constructor(
    "tag:yaml.org,2002:float", lambda loader, node: _finite_float(loader.construct_scalar(node)))


def strict_yaml_loads(text: str | bytes) -> Any:
    """Decode YAML with JSON-style scalars, no aliases, merges or custom tags.

Unquoted yes/on/dates remain strings, not PyYAML's implicit bool/datetime.
Explicit tags are rejected so !!int cannot repair a quoted integer string.
"""
    try:
        for token in yaml.scan(text):
            if isinstance(token, yaml.tokens.TagToken):
                raise ResearchInputError("Explicit YAML tags are forbidden")
        value = yaml.load(text, Loader=_StrictYamlLoader)
    except yaml.YAMLError as exc:
        raise ResearchInputError(f"Invalid YAML: {exc}") from exc
    _check_tree(value)
    return value


ResearchModelT = TypeVar("ResearchModelT", bound=StrictResearchModel)


def load_research_model(path: str | Path, model_type: type[ResearchModelT]) -> ResearchModelT:
    """Read an explicitly typed research model; never guess or repair formats."""
    path = Path(path)
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = strict_json_loads(content)
    elif path.suffix.lower() in (".yaml", ".yml"):
        payload = strict_yaml_loads(content)
    else:
        raise ResearchInputError("Research inputs must use .json, .yaml or .yml")
    if not isinstance(model_type, type) or not issubclass(model_type, StrictResearchModel):
        raise TypeError("model_type must be a StrictResearchModel subclass")
    return model_type.model_validate(payload)

