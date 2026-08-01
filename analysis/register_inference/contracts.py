"""Stable JSON contracts shared by register-inference modules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


class RegisterInferenceError(RuntimeError):
    """Raised when input evidence cannot safely be used for inference."""


@dataclass(frozen=True)
class DotTransition:
    edge_id: str
    source_state: str
    target_state: str
    inputs: tuple[str, ...]
    output: str
    order: int


@dataclass(frozen=True)
class PreparedObservation:
    observation_id: str
    source_state: str
    target_state: str
    input_symbol: str
    edge_id: str
    sequence_id: str | int | None
    cycle_id: str | int | None
    iteration: int | None
    register_before: dict[str, int]
    register_after: dict[str, int]
    input_values: dict[str, int]
    trace_line: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
