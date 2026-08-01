"""Evidence preparation: extract explicitly mapped observations without deduplication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from config import resolve_input_path
from contracts import DotTransition, PreparedObservation, RegisterInferenceError
from dot_model import parse_dot


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_field(record: dict[str, Any], path: str, line: int) -> Any:
    current: Any = record
    for segment in path.removeprefix("$").removeprefix(".").split("."):
        if not segment:
            continue
        if not isinstance(current, dict) or segment not in current:
            raise RegisterInferenceError(f"Trace line {line}: missing field {path!r}.")
        current = current[segment]
    return current


def read_integer(record: dict[str, Any], path: str, line: int) -> int:
    value = read_field(record, path, line)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RegisterInferenceError(f"Trace line {line}: {path!r} must be an integer, got {value!r}.")
    return value


def _optional(record: dict[str, Any], path: str | None, line: int) -> str | int | None:
    if not path:
        return None
    value = read_field(record, path, line)
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return value
    raise RegisterInferenceError(f"Trace line {line}: {path!r} must be a string or integer.")


def _edge_index(transitions: tuple[DotTransition, ...]) -> dict[tuple[str, str, str], DotTransition]:
    indexed: dict[tuple[str, str, str], DotTransition] = {}
    for transition in transitions:
        for input_symbol in transition.inputs:
            key = (transition.source_state, transition.target_state, input_symbol)
            if key in indexed:
                raise RegisterInferenceError(
                    f"DOT has ambiguous transition for {key}; use a model with unique state/input mapping."
                )
            indexed[key] = transition
    return indexed


def prepare(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    dot_path = resolve_input_path(config_path, config["inputs"]["dot"])
    trace_path = resolve_input_path(config_path, config["inputs"]["trace"])
    transitions = parse_dot(dot_path)
    edge_index = _edge_index(transitions)
    mapping = config["mapping"]
    observations: list[PreparedObservation] = []
    with trace_path.open("r", encoding="utf-8") as handle:
        for trace_line, text in enumerate(handle, start=1):
            if not text.strip():
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RegisterInferenceError(f"Trace line {trace_line}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise RegisterInferenceError(f"Trace line {trace_line}: record must be an object.")
            source_state = read_field(record, mapping["source_state"], trace_line)
            target_state = read_field(record, mapping["target_state"], trace_line)
            input_symbol = read_field(record, mapping["input_symbol"], trace_line)
            if not all(isinstance(value, str) for value in (source_state, target_state, input_symbol)):
                raise RegisterInferenceError(f"Trace line {trace_line}: state and input mappings must be strings.")
            transition = edge_index.get((source_state, target_state, input_symbol))
            if transition is None:
                raise RegisterInferenceError(
                    f"Trace line {trace_line}: {source_state}->{target_state} / {input_symbol} is absent from DOT."
                )
            before = {item["id"]: read_integer(record, item["before"], trace_line) for item in mapping["registers"]}
            after = {item["id"]: read_integer(record, item["after"], trace_line) for item in mapping["registers"]}
            input_values = {item["id"]: read_integer(record, item["path"], trace_line) for item in mapping.get("input_variables", [])}
            observations.append(PreparedObservation(
                observation_id=f"O{len(observations) + 1:05d}", source_state=source_state,
                target_state=target_state, input_symbol=input_symbol, edge_id=transition.edge_id,
                sequence_id=_optional(record, mapping.get("sequence_id"), trace_line),
                cycle_id=_optional(record, mapping.get("cycle_id"), trace_line),
                iteration=_optional(record, mapping.get("iteration"), trace_line),
                register_before=before, register_after=after, input_values=input_values,
                trace_line=trace_line,
            ))
    if not observations:
        raise RegisterInferenceError("Trace contained no non-empty observations.")
    return {
        "schema_version": 1,
        "input_hashes": {"dot": sha256_file(dot_path), "trace": sha256_file(trace_path)},
        "source_paths": {"dot": str(dot_path), "trace": str(trace_path)},
        "transitions": [transition.__dict__ for transition in transitions],
        "registers": [item["id"] for item in mapping["registers"]],
        "input_variables": [item["id"] for item in mapping.get("input_variables", [])],
        "observations": [item.to_json() for item in observations],
        "anomalies": [],
    }
