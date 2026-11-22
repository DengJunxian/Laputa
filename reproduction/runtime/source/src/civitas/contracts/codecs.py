"""Closed record-family dispatch; stored data never names an importable class.

Only v4 adds a family. The v1-v3 classes, defaults and canonical payloads are
unchanged. An absent wire version keeps the original v1 default; explicitly
supplied versions must be exact integers from this table.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .base import Record
from .decisions import Command, DecisionBundle
from .economic import EconomicCommand, EconomicDecisionBundle, EconomicExecutionReceipt
from .financial import FinancialCommand, FinancialDecisionBundle, FinancialExecutionReceipt
from .portfolio import PortfolioCommand, PortfolioDecisionBundle, PortfolioExecutionReceipt
from .receipts import ExecutionReceipt
from .state import (
    EconomicPreparedTick, EconomicWorldSnapshot,
    FinancialPreparedTick, FinancialWorldSnapshot,
    PortfolioPreparedTick, PortfolioWorldSnapshot,
    PreparedTick, WorldSnapshot,
)


@dataclass(frozen=True)
class WorldCodec:
    snapshot: type[WorldSnapshot]
    prepared: type[PreparedTick]
    decision: type[DecisionBundle]
    command: type[Command]
    receipt: type[ExecutionReceipt]


WORLD_CODECS = MappingProxyType({
    1: WorldCodec(WorldSnapshot, PreparedTick, DecisionBundle, Command, ExecutionReceipt),
    2: WorldCodec(FinancialWorldSnapshot, FinancialPreparedTick, FinancialDecisionBundle,
                  FinancialCommand, FinancialExecutionReceipt),
    3: WorldCodec(PortfolioWorldSnapshot, PortfolioPreparedTick, PortfolioDecisionBundle,
                  PortfolioCommand, PortfolioExecutionReceipt),
    4: WorldCodec(EconomicWorldSnapshot, EconomicPreparedTick, EconomicDecisionBundle,
                  EconomicCommand, EconomicExecutionReceipt),
})


def codec_for_version(version: int) -> WorldCodec:
    if type(version) is not int or version not in WORLD_CODECS:
        raise ValueError(f"Unknown or unsupported world schema version: {version!r}")
    return WORLD_CODECS[version]


def _codec_for_record(value) -> WorldCodec:
    if isinstance(value, Record):
        version = getattr(value, "schema_version", None)
    elif isinstance(value, Mapping):
        version = value.get("schema_version", 1)
    else:
        raise ValueError("Versioned records require a validated record or a JSON object")
    return codec_for_version(version)


def decode_snapshot(value) -> WorldSnapshot:
    return _codec_for_record(value).snapshot.model_validate(value)


def decode_decision(value) -> DecisionBundle:
    return _codec_for_record(value).decision.model_validate(value)


def decode_command(value) -> Command:
    return _codec_for_record(value).command.model_validate(value)


def decode_receipt(value) -> ExecutionReceipt:
    return _codec_for_record(value).receipt.model_validate(value)
