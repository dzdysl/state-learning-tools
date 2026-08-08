"""Infer typed, temporally ordered register-update candidates from cycle traces.

Each region is bounded by consecutive configured downlink register observations.
Intervening observations retain their event order and typed identity.  The model
tree keeps transport-context signals, numeric wrap guards, and counterexample-
derived value guards as different node kinds.  Results remain observational
candidates; they are not claims about AMF implementation variables.
"""

from __future__ import annotations

import argparse
import html
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from itertools import product
from pathlib import Path
from typing import Any, Iterable

import yaml

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from dot_model import parse_dot


class RegionInferenceError(RuntimeError):
    """Raised when selected trace material cannot safely be aligned."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def get_path(record: dict[str, Any], path: str) -> Any:
    current: Any = record
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            raise KeyError(path)
        current = current[segment]
    return current


def optional_integer(record: dict[str, Any], path: str) -> int | None:
    try:
        value = get_path(record, path)
    except KeyError:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def optional_boolean(record: dict[str, Any], path: str) -> int | None:
    try:
        value = get_path(record, path)
    except KeyError:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1"):
            return 1
        if normalized in ("false", "0"):
            return 0
    return None


def read_sequence_lines(path: Path) -> list[tuple[str, ...]]:
    lines = [tuple(line.split()) for line in path.read_text(encoding="utf-8").splitlines()]
    if not lines or any(not line for line in lines):
        raise RegionInferenceError(f"Sequence file has an empty line: {path}")
    return lines


def read_trace_groups(path: Path) -> dict[int, list[dict[str, Any]]]:
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as handle:
        for trace_line, text in enumerate(handle, start=1):
            if not text.strip():
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RegionInferenceError(f"Trace line {trace_line}: invalid JSON.") from exc
            if not isinstance(record, dict) or not isinstance(record.get("sequence_id"), int):
                raise RegionInferenceError(f"Trace line {trace_line}: missing integer sequence_id.")
            record["_trace_line"] = trace_line
            groups[record["sequence_id"]].append(record)
    for records in groups.values():
        records.sort(key=lambda record: record.get("step_id", -1))
    return groups


def validate_signal_definitions(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    definitions = mapping.get("signal_definitions", [])
    if not isinstance(definitions, list):
        raise RegionInferenceError("mapping.signal_definitions must be a list.")
    seen_ids: set[str] = set()
    for position, definition in enumerate(definitions):
        if not isinstance(definition, dict):
            raise RegionInferenceError(f"mapping.signal_definitions[{position}] must be a mapping.")
        signal_id = definition.get("id")
        path = definition.get("path")
        symbols = definition.get("match", {}).get("input_symbols") if isinstance(definition.get("match"), dict) else None
        if not isinstance(signal_id, str) or not signal_id or signal_id in seen_ids:
            raise RegionInferenceError(f"Signal definition {position} needs a unique non-empty id.")
        if not isinstance(path, str) or not path:
            raise RegionInferenceError(f"Signal {signal_id}: path must be a non-empty string.")
        if definition.get("value_type") != "boolean":
            raise RegionInferenceError(f"Signal {signal_id}: only value_type boolean is currently supported.")
        if not isinstance(symbols, list) or not symbols or not all(isinstance(item, str) and item for item in symbols):
            raise RegionInferenceError(f"Signal {signal_id}: match.input_symbols must be a non-empty string list.")
        if "*" in symbols and len(symbols) != 1:
            raise RegionInferenceError(f"Signal {signal_id}: wildcard '*' must be the only input symbol selector.")
        if definition.get("phase", "before_numeric_inputs") != "before_numeric_inputs":
            raise RegionInferenceError(f"Signal {signal_id}: only phase before_numeric_inputs is supported.")
        seen_ids.add(signal_id)
    return definitions


def validate_numeric_input_definitions(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate schema-v3 typed numeric inputs and their logical registers.

    ``id`` identifies a concrete trace field definition, whereas
    ``input_register_id`` identifies the intentionally shared logical input
    register.  Several message fields may therefore write one register.
    """
    definitions = mapping.get("numeric_input_definitions")
    if not isinstance(definitions, list) or not definitions:
        raise RegionInferenceError("mapping.numeric_input_definitions must be a non-empty list for schema_version 3.")
    seen_ids: set[str] = set()
    for position, definition in enumerate(definitions):
        if not isinstance(definition, dict):
            raise RegionInferenceError(f"Numeric input definition {position} must be a mapping.")
        definition_id = definition.get("id")
        register_id = definition.get("input_register_id")
        path = definition.get("path")
        selector = definition.get("match")
        symbols = selector.get("input_symbols") if isinstance(selector, dict) else None
        if not isinstance(definition_id, str) or not definition_id or definition_id in seen_ids:
            raise RegionInferenceError(f"Numeric input definition {position} needs a unique non-empty id.")
        if not isinstance(register_id, str) or not register_id:
            raise RegionInferenceError(f"Numeric input {definition_id}: input_register_id must be a non-empty string.")
        if not isinstance(path, str) or not path:
            raise RegionInferenceError(f"Numeric input {definition_id}: path must be a non-empty string.")
        if definition.get("value_type") != "integer":
            raise RegionInferenceError(f"Numeric input {definition_id}: only value_type integer is supported.")
        if not isinstance(symbols, list) or not symbols or not all(isinstance(item, str) and item for item in symbols):
            raise RegionInferenceError(f"Numeric input {definition_id}: match.input_symbols must be a non-empty string list.")
        if "*" in symbols and len(symbols) != 1:
            raise RegionInferenceError(f"Numeric input {definition_id}: wildcard '*' must be the only selector.")
        if definition.get("phase", "before_register_updates") != "before_register_updates":
            raise RegionInferenceError(
                f"Numeric input {definition_id}: only phase before_register_updates is supported."
            )
        seen_ids.add(definition_id)
    return definitions


def validate_v3_analysis(analysis: dict[str, Any]) -> None:
    if "preferred_derived_guard_values" in analysis:
        raise RegionInferenceError(
            "analysis.preferred_derived_guard_values is not configurable; "
            "preferences are learned from relatively stable derived_value_guard candidates."
        )
    backward = analysis.get("backward_inference")
    if backward is None:
        return
    if not isinstance(backward, dict):
        raise RegionInferenceError("analysis.backward_inference must be a mapping.")
    enabled = backward.get("enabled")
    if not isinstance(enabled, bool):
        raise RegionInferenceError("analysis.backward_inference.enabled must be boolean.")
    if not enabled:
        return
    expected = {
        "value_domain": "observed_global",
        "predecessor_policy": "nearest_no_downlink_predecessor",
        "earlier_predecessor_policy": "hold",
        "signal_scope": "matching_effective_signal_context",
    }
    for key, value in expected.items():
        if backward.get(key) != value:
            raise RegionInferenceError(
                f"analysis.backward_inference.{key} must be {value!r} when backward inference is enabled."
            )


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegionInferenceError(f"Invalid YAML config {path}: {exc}") from exc
    if not isinstance(config, dict) or config.get("schema_version") not in (1, 2, 3):
        raise RegionInferenceError("Config must be a mapping with schema_version 1, 2, or 3.")
    inputs = config.get("inputs")
    mapping = config.get("mapping")
    analysis = config.get("analysis")
    if not isinstance(inputs, dict) or not isinstance(mapping, dict) or not isinstance(analysis, dict):
        raise RegionInferenceError("Config requires inputs, mapping, and analysis mappings.")
    for name in ("dot", "trace", "cycle_cover", "sequence_file"):
        if not isinstance(inputs.get(name), str) or not inputs[name]:
            raise RegionInferenceError(f"inputs.{name} must be a non-empty path string.")
    downlink = mapping.get("downlink_ksi_by_output")
    if not isinstance(downlink, dict) or not all(
        isinstance(key, str) and isinstance(item, str) and item for key, item in downlink.items()
    ):
        raise RegionInferenceError("mapping.downlink_ksi_by_output must map symbols to dotted field paths.")
    if config["schema_version"] == 3:
        validate_numeric_input_definitions(mapping)
        validate_v3_analysis(analysis)
    else:
        uplink = mapping.get("uplink_ksi_by_input")
        if not isinstance(uplink, dict):
            raise RegionInferenceError("mapping.uplink_ksi_by_input must be a mapping.")
        for symbol, paths in uplink.items():
            if not isinstance(symbol, str) or not symbol:
                raise RegionInferenceError("mapping.uplink_ksi_by_input keys must be non-empty symbols.")
            if isinstance(paths, str) and paths:
                continue
            if isinstance(paths, list) and paths and all(isinstance(item, str) and item for item in paths):
                continue
            raise RegionInferenceError(
                f"mapping.uplink_ksi_by_input.{symbol} must be one dotted path or an ordered non-empty path list."
            )
    validate_signal_definitions(mapping)
    repetitions = analysis.get("repetitions", [2, 10])
    if not isinstance(repetitions, list) or len(repetitions) != 2 or not all(isinstance(item, int) for item in repetitions):
        raise RegionInferenceError("analysis.repetitions must be a two-item integer list.")
    if repetitions[0] < 2 or repetitions[1] < repetitions[0]:
        raise RegionInferenceError("analysis.repetitions must begin at 2 or later and be ordered.")
    minimum = analysis.get("min_consecutive_support", 3)
    if not isinstance(minimum, int) or minimum < 1:
        raise RegionInferenceError("analysis.min_consecutive_support must be a positive integer.")
    numeric_depth = analysis.get("max_numeric_depth", analysis.get("max_depth", 1))
    derived_depth = analysis.get("max_derived_signal_depth", 1)
    if numeric_depth not in (0, 1):
        raise RegionInferenceError("analysis.max_numeric_depth currently supports only 0 or 1.")
    if derived_depth not in (0, 1):
        raise RegionInferenceError("analysis.max_derived_signal_depth currently supports only 0 or 1.")
    cycles = analysis.get("cycle_ids")
    if cycles is not None and (not isinstance(cycles, list) or not all(isinstance(item, str) for item in cycles)):
        raise RegionInferenceError("analysis.cycle_ids must be a list of strings when supplied.")
    d_states = mapping.get("d_states", [])
    if not isinstance(d_states, list) or not all(isinstance(item, str) for item in d_states):
        raise RegionInferenceError("mapping.d_states must be a list of state IDs when supplied.")
    return config


def resolve(config_path: Path, text: str) -> Path:
    path = Path(text)
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def edge_index(dot_path: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for transition in parse_dot(dot_path):
        for symbol in transition.inputs:
            key = (transition.source_state, transition.target_state, symbol)
            if key in indexed:
                raise RegionInferenceError(f"DOT has more than one transition for {key}.")
            indexed[key] = {
                "edge_id": transition.edge_id,
                "source_state": transition.source_state,
                "target_state": transition.target_state,
                "logical_input": symbol,
                "logical_output": transition.output,
            }
    return indexed


def selected_cycles(cycle_cover: dict[str, Any], wanted: list[str] | None) -> list[dict[str, Any]]:
    cycles = cycle_cover.get("sequence_export", {}).get("cycles", [])
    if not isinstance(cycles, list):
        raise RegionInferenceError("cycle_cover.sequence_export.cycles must be a list.")
    found = [cycle for cycle in cycles if wanted is None or cycle.get("cycle_id") in wanted]
    if wanted is not None and {cycle.get("cycle_id") for cycle in found} != set(wanted):
        raise RegionInferenceError("At least one configured cycle_id is absent from cycle cover.")
    if not found:
        raise RegionInferenceError("No cycles selected for inference.")
    return found


def matched_records(
    trace_groups: dict[int, list[dict[str, Any]]], sequence_lines: list[tuple[str, ...]], variant: dict[str, Any], cycle_id: str,
) -> tuple[int, list[dict[str, Any]]]:
    line_number = variant.get("line_number")
    if not isinstance(line_number, int) or not 1 <= line_number <= len(sequence_lines):
        raise RegionInferenceError(f"{cycle_id}: invalid variant line_number {line_number!r}.")
    expected = sequence_lines[line_number - 1]
    matches = [
        (sequence_id, records) for sequence_id, records in trace_groups.items()
        if records and tuple(records[-1].get("sequence_inputs", [])) == expected
    ]
    if len(matches) != 1:
        raise RegionInferenceError(f"{cycle_id} line {line_number}: expected one trace group, found {len(matches)}.")
    sequence_id, records = matches[0]
    if len(records) != len(expected):
        raise RegionInferenceError(f"Trace sequence {sequence_id}: {len(records)} records for {len(expected)} inputs.")
    return sequence_id, records


def make_edge(
    cycle: dict[str, Any], loop_inputs: list[str], edge_offset: int, record: dict[str, Any], indexed_edges: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    nodes = cycle.get("rotated_nodes")
    if not isinstance(nodes, list) or len(nodes) != len(loop_inputs) + 1:
        raise RegionInferenceError(f"{cycle.get('cycle_id')}: rotated_nodes do not match loop length.")
    input_symbol = record.get("abstract_io", {}).get("input")
    expected_input = loop_inputs[edge_offset]
    if input_symbol != expected_input:
        raise RegionInferenceError(f"Trace line {record['_trace_line']}: expected {expected_input!r}, got {input_symbol!r}.")
    key = (nodes[edge_offset], nodes[edge_offset + 1], input_symbol)
    if key not in indexed_edges:
        raise RegionInferenceError(f"Trace line {record['_trace_line']}: DOT transition {key} is absent.")
    return indexed_edges[key]


def source_events(
    cycle: dict[str, Any], variant: dict[str, Any], records: list[dict[str, Any]], indexed_edges: dict[tuple[str, str, str], dict[str, Any]], end_repeat: int,
) -> Iterable[tuple[int, int, dict[str, Any], dict[str, Any]]]:
    prefix_length = cycle.get("prefix_length")
    loop_length = cycle.get("loop_length")
    loop_inputs = variant.get("loop_inputs")
    if not isinstance(prefix_length, int) or not isinstance(loop_length, int) or not isinstance(loop_inputs, list) or loop_length != len(loop_inputs):
        raise RegionInferenceError(f"{cycle.get('cycle_id')}: malformed loop metadata.")
    if prefix_length + end_repeat * loop_length > len(records):
        raise RegionInferenceError(f"{cycle.get('cycle_id')} line {variant.get('line_number')}: trace is shorter than requested repetitions.")
    for repetition in range(1, end_repeat + 1):
        for edge_offset in range(loop_length):
            index = prefix_length + (repetition - 1) * loop_length + edge_offset
            record = records[index]
            yield repetition, edge_offset + 1, record, make_edge(cycle, loop_inputs, edge_offset, record, indexed_edges)


def output_observation(record: dict[str, Any], edge: dict[str, Any], downlink_paths: dict[str, str]) -> dict[str, Any] | None:
    output = record.get("abstract_io", {}).get("output")
    field_path = downlink_paths.get(output)
    if field_path is None:
        return None
    value = optional_integer(record, field_path)
    if value is None:
        raise RegionInferenceError(f"Trace line {record['_trace_line']}: missing integer output field {field_path} for {output}.")
    return {
        "edge": edge,
        "output_symbol": output,
        "field_path": field_path,
        "value": value,
        "trace_line": record["_trace_line"],
        "event_position": record.get("step_id"),
    }


def input_observations(record: dict[str, Any], edge: dict[str, Any], uplink_paths: dict[str, Any]) -> list[dict[str, Any]]:
    input_symbol = edge["logical_input"]
    configured = uplink_paths.get(input_symbol)
    if configured is None:
        return []
    paths = [configured] if isinstance(configured, str) else configured
    observations: list[dict[str, Any]] = []
    for declaration_index, field_path in enumerate(paths):
        value = optional_integer(record, field_path)
        if value is None:
            raise RegionInferenceError(f"Trace line {record['_trace_line']}: missing integer input field {field_path} for {input_symbol}.")
        observations.append({
            "kind": "numeric_input",
            "input_symbol": input_symbol,
            "field_path": field_path,
            "value": value,
            "trace_line": record["_trace_line"],
            "event_position": record.get("step_id"),
            "declaration_index": declaration_index,
        })
    return observations


def numeric_input_observations(
    record: dict[str, Any], edge: dict[str, Any], definitions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect schema-v3 inputs without giving message names semantic priority."""
    input_symbol = edge["logical_input"]
    observations: list[dict[str, Any]] = []
    for declaration_index, definition in enumerate(definitions):
        symbols = definition["match"]["input_symbols"]
        if "*" not in symbols and input_symbol not in symbols:
            continue
        value = optional_integer(record, definition["path"])
        if value is None:
            raise RegionInferenceError(
                f"Trace line {record['_trace_line']}: missing integer input field {definition['path']} "
                f"for configured input {input_symbol}."
            )
        observations.append({
            "kind": "numeric_input",
            "definition_id": definition["id"],
            "input_register_id": definition["input_register_id"],
            "input_symbol": input_symbol,
            "field_path": definition["path"],
            "value": value,
            "trace_line": record["_trace_line"],
            "event_position": record.get("step_id"),
            "declaration_index": declaration_index,
        })
    return observations


def signal_observations(record: dict[str, Any], edge: dict[str, Any], definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    input_symbol = edge["logical_input"]
    observations: list[dict[str, Any]] = []
    for declaration_index, definition in enumerate(definitions):
        symbols = definition["match"]["input_symbols"]
        if "*" not in symbols and input_symbol not in symbols:
            continue
        value = optional_boolean(record, definition["path"])
        if value is None:
            raise RegionInferenceError(
                f"Trace line {record['_trace_line']}: missing boolean signal field {definition['path']} "
                f"for configured input {input_symbol}."
            )
        observations.append({
            "kind": "signal",
            "signal_id": definition["id"],
            "input_symbol": input_symbol,
            "field_path": definition["path"],
            "value": value,
            "trace_line": record["_trace_line"],
            "event_position": record.get("step_id"),
            "declaration_index": declaration_index,
        })
    return observations


def assign_occurrence_indices(items: list[dict[str, Any]]) -> None:
    counts: dict[tuple[str, ...], int] = defaultdict(int)
    for item in items:
        if item["kind"] == "signal":
            identity = (item["kind"], item["signal_id"], item["field_path"], item["input_symbol"])
        else:
            identity = (item["kind"], item["field_path"], item["input_symbol"])
        item["occurrence_index"] = counts[identity]
        counts[identity] += 1


def build_regions(
    cycle: dict[str, Any], variant: dict[str, Any], sequence_id: int, records: list[dict[str, Any]], indexed_edges: dict[tuple[str, str, str], dict[str, Any]],
    downlink_paths: dict[str, str], uplink_paths: dict[str, Any], signal_definitions: list[dict[str, Any]], start_repeat: int, end_repeat: int,
) -> list[dict[str, Any]]:
    previous: dict[str, Any] | None = None
    pending_items: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []
    for repetition, _, record, edge in source_events(cycle, variant, records, indexed_edges, end_repeat):
        # Event order is inherited from source_events.  Within an event,
        # configured signals precede numeric inputs, each in declaration order.
        pending_items.extend(signal_observations(record, edge, signal_definitions))
        pending_items.extend(input_observations(record, edge, uplink_paths))
        current_output = output_observation(record, edge, downlink_paths)
        if current_output is None:
            continue
        if repetition >= start_repeat and previous is not None:
            assign_occurrence_indices(pending_items)
            regions.append({
                "cycle_id": cycle["cycle_id"],
                "sequence_line": variant["line_number"],
                "trace_sequence_id": sequence_id,
                "repetition": repetition,
                "terminal_edge": edge,
                "previous_output": previous,
                "observation_items": pending_items,
                "signals": [item for item in pending_items if item["kind"] == "signal"],
                "inputs": [item for item in pending_items if item["kind"] == "numeric_input"],
                "terminal_output": current_output,
                "output_delta": current_output["value"] - previous["value"],
            })
        previous = current_output
        pending_items = []
    return regions


def item_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    if item["kind"] == "signal":
        return (item["kind"], item["signal_id"], item["field_path"], item["input_symbol"], item["occurrence_index"])
    return (item["kind"], item["field_path"], item["input_symbol"], item["occurrence_index"])


def validate_observation_alignment(regions: list[dict[str, Any]]) -> None:
    if not regions:
        return
    first = [item_identity(item) for item in regions[0]["observation_items"]]
    for region in regions[1:]:
        current = [item_identity(item) for item in region["observation_items"]]
        if current != first:
            raise RegionInferenceError(
                f"Alignment anomaly on edge {region['terminal_edge']['edge_id']}: ordered observation identities differ "
                f"between sequence line {regions[0]['sequence_line']} repetition {regions[0]['repetition']} "
                f"and sequence line {region['sequence_line']} repetition {region['repetition']}; observations were not shifted."
            )


def stable_slots(regions: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    if not regions:
        return []
    key = "signals" if kind == "signal" else "inputs"
    first = [item_identity(item) for item in regions[0][key]]
    for region in regions[1:]:
        current = [item_identity(item) for item in region[key]]
        if current != first:
            raise RegionInferenceError(
                f"Alignment anomaly on edge {region['terminal_edge']['edge_id']}: {kind} slot identities differ "
                f"between sequence line {regions[0]['sequence_line']} repetition {regions[0]['repetition']} "
                f"and sequence line {region['sequence_line']} repetition {region['repetition']}; observations were not shifted."
            )
    slots: list[dict[str, Any]] = []
    for index, item in enumerate(regions[0][key]):
        slot = {
            "id": f"s{index}" if kind == "signal" else f"i{index}",
            "kind": kind,
            "input_symbol": item["input_symbol"],
            "field_path": item["field_path"],
            "occurrence_index": item["occurrence_index"],
        }
        if kind == "signal":
            slot["signal_id"] = item["signal_id"]
            slot["declaration_index"] = item["declaration_index"]
        else:
            slot["declaration_index"] = item["declaration_index"]
        slots.append(slot)
    return slots


def input_value(region: dict[str, Any], slot: int) -> int:
    return region["inputs"][slot]["value"]


def input_register_value(region: dict[str, Any], input_register_id: str) -> int:
    values = region.get("input_register_values", {})
    if input_register_id not in values:
        raise RegionInferenceError(f"Input register {input_register_id!r} is unobservable in this sample.")
    return values[input_register_id]["value"]


def signal_value(region: dict[str, Any], slot: int) -> int:
    return region["signals"][slot]["value"]


def longest_consecutive_support(regions: list[dict[str, Any]]) -> int:
    by_line: dict[int, list[int]] = defaultdict(list)
    for region in regions:
        by_line[region["sequence_line"]].append(region["repetition"])
    best = 0
    for repetitions in by_line.values():
        run = 0
        previous: int | None = None
        for repetition in sorted(set(repetitions)):
            run = run + 1 if previous is not None and repetition == previous + 1 else 1
            best = max(best, run)
            previous = repetition
    return best


def formula_value(formula: dict[str, Any], region: dict[str, Any]) -> int:
    kind = formula["kind"]
    if kind == "constant":
        return formula["value"]
    if kind == "r_plus":
        return region["previous_output"]["value"] + formula["value"]
    if kind == "input_plus":
        return input_value(region, formula["slot"]) + formula["value"]
    if kind == "input_register_plus":
        return input_register_value(region, formula["input_register_id"]) + formula["value"]
    raise RegionInferenceError(f"Unknown formula kind: {kind}")


def formula_complexity(formula: dict[str, Any]) -> int:
    if formula["kind"] == "r_plus" and formula["value"] == 0:
        return 0
    if formula["kind"] == "constant":
        return 1
    if formula["kind"] == "r_plus":
        return 2
    return 3


def signed_offset(value: int) -> str:
    return f" + {value}" if value >= 0 else f" - {abs(value)}"


def formula_text(formula: dict[str, Any]) -> str:
    if formula["kind"] == "constant":
        return f"r' = {formula['value']}"
    if formula["kind"] == "r_plus":
        return "r' = r" if formula["value"] == 0 else f"r' = r{signed_offset(formula['value'])}"
    if formula["kind"] == "input_plus":
        return (
            f"r' = i{formula['slot']}" if formula["value"] == 0
            else f"r' = i{formula['slot']}{signed_offset(formula['value'])}"
        )
    return (
        f"r' = r_i[{formula['input_register_id']}]" if formula["value"] == 0
        else f"r' = r_i[{formula['input_register_id']}]{signed_offset(formula['value'])}"
    )


def exact_leaf_candidates(regions: list[dict[str, Any]], slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not regions:
        return []
    after = [region["terminal_output"]["value"] for region in regions]
    candidates: list[dict[str, Any]] = []
    if len(set(after)) == 1:
        candidates.append({"kind": "constant", "value": after[0]})
    r_deltas = [region["terminal_output"]["value"] - region["previous_output"]["value"] for region in regions]
    if len(set(r_deltas)) == 1:
        candidates.append({"kind": "r_plus", "value": r_deltas[0]})
    for slot in range(len(slots)):
        deltas = [region["terminal_output"]["value"] - input_value(region, slot) for region in regions]
        if len(set(deltas)) == 1:
            candidates.append({"kind": "input_plus", "slot": slot, "value": deltas[0]})
    unique = {json.dumps(item, sort_keys=True): item for item in candidates}
    return sorted(unique.values(), key=lambda item: (formula_complexity(item), json.dumps(item, sort_keys=True)))


def threshold_value(guard: dict[str, Any], region: dict[str, Any]) -> bool:
    value = region["previous_output"]["value"] if guard["variable"] == "r" else input_value(region, guard["slot"])
    return value < guard["threshold"]


def leaf_nodes(regions: list[dict[str, Any]], slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"kind": "leaf", "formula": formula} for formula in exact_leaf_candidates(regions, slots)]


def threshold_candidates(regions: list[dict[str, Any]], slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    guards: list[dict[str, Any]] = []
    guards.extend(
        {"variable": "r", "operator": "<", "threshold": value}
        for value in sorted({region["previous_output"]["value"] for region in regions})[1:]
    )
    for slot in range(len(slots)):
        guards.extend(
            {"variable": "input", "slot": slot, "operator": "<", "threshold": value}
            for value in sorted({input_value(region, slot) for region in regions})[1:]
        )
    candidates: list[dict[str, Any]] = []
    false_formula = {"kind": "constant", "value": 0}
    for guard in guards:
        true_regions = [region for region in regions if threshold_value(guard, region)]
        false_regions = [region for region in regions if not threshold_value(guard, region)]
        if not true_regions or not false_regions or false_formula not in exact_leaf_candidates(false_regions, slots):
            continue
        for true_formula in exact_leaf_candidates(true_regions, slots):
            candidates.append({
                "kind": "threshold_guard",
                "guard": guard,
                "true": {"kind": "leaf", "formula": true_formula},
                "false": {"kind": "leaf", "formula": false_formula},
            })
    return candidates


def fit_without_derived(
    regions: list[dict[str, Any]], slots: list[dict[str, Any]], min_support: int, max_numeric_depth: int,
) -> list[dict[str, Any]]:
    if longest_consecutive_support(regions) < min_support:
        return []
    leaves = leaf_nodes(regions, slots)
    if leaves:
        return leaves
    if max_numeric_depth:
        return threshold_candidates(regions, slots)
    return []


def derived_value_candidates(
    regions: list[dict[str, Any]], slots: list[dict[str, Any]], min_support: int, max_numeric_depth: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for slot in range(len(slots)):
        for value in sorted({input_value(region, slot) for region in regions}):
            true_regions = [region for region in regions if input_value(region, slot) == value]
            false_regions = [region for region in regions if input_value(region, slot) != value]
            if not true_regions or not false_regions:
                continue
            if min(longest_consecutive_support(true_regions), longest_consecutive_support(false_regions)) < min_support:
                continue
            true_candidates = fit_without_derived(true_regions, slots, min_support, max_numeric_depth)
            false_candidates = fit_without_derived(false_regions, slots, min_support, max_numeric_depth)
            for true_tree, false_tree in product(true_candidates, false_candidates):
                candidates.append({
                    "kind": "derived_value_guard",
                    "guard": {"variable": "input", "slot": slot, "operator": "==", "value": value},
                    "true": true_tree,
                    "false": false_tree,
                })
    return candidates


def numeric_candidates(
    regions: list[dict[str, Any]], slots: list[dict[str, Any]], min_support: int,
    max_numeric_depth: int, max_derived_depth: int,
) -> list[dict[str, Any]]:
    candidates = fit_without_derived(regions, slots, min_support, max_numeric_depth)
    if not candidates and max_derived_depth:
        candidates = derived_value_candidates(regions, slots, min_support, max_numeric_depth)
    return unique_sorted_trees(candidates)


def guarded_candidates(
    regions: list[dict[str, Any]], slots: list[dict[str, Any]], min_support: int,
) -> list[dict[str, Any]]:
    """Compatibility entry point for callers that do not configure signals."""
    return numeric_candidates(regions, slots, min_support, max_numeric_depth=1, max_derived_depth=1)


def unknown_node(reason: str) -> dict[str, Any]:
    return {"kind": "unknown", "reason": reason}


def signal_gated_candidates(
    regions: list[dict[str, Any]], signal_slots: list[dict[str, Any]], input_slots: list[dict[str, Any]],
    min_support: int, max_numeric_depth: int, max_derived_depth: int, signal_offset: int = 0,
) -> list[dict[str, Any]]:
    if signal_offset == len(signal_slots):
        if longest_consecutive_support(regions) < min_support:
            return [unknown_node("insufficient_support")]
        candidates = numeric_candidates(regions, input_slots, min_support, max_numeric_depth, max_derived_depth)
        return candidates or []
    slot = signal_slots[signal_offset]
    branch_candidates: dict[int, list[dict[str, Any]]] = {}
    for value in (1, 0):
        branch_regions = [region for region in regions if signal_value(region, signal_offset) == value]
        if not branch_regions:
            branch_candidates[value] = [unknown_node("unobserved_signal_branch")]
        elif longest_consecutive_support(branch_regions) < min_support:
            branch_candidates[value] = [unknown_node("insufficient_support")]
        else:
            branch_candidates[value] = signal_gated_candidates(
                branch_regions, signal_slots, input_slots, min_support,
                max_numeric_depth, max_derived_depth, signal_offset + 1,
            )
    if not branch_candidates[1] or not branch_candidates[0]:
        return []
    candidates = []
    for true_tree, false_tree in product(branch_candidates[1], branch_candidates[0]):
        candidates.append({
            "kind": "signal_guard",
            "guard": {
                "variable": "signal",
                "slot": signal_offset,
                "operator": "==",
                "value": 1,
                "signal_id": slot["signal_id"],
                "field_path": slot["field_path"],
                "input_symbol": slot["input_symbol"],
                "occurrence_index": slot["occurrence_index"],
            },
            "true": true_tree,
            "false": false_tree,
        })
    return unique_sorted_trees(candidates)


def tree_metrics(tree: dict[str, Any]) -> tuple[int, int, int, int]:
    kind = tree["kind"]
    if kind == "unknown":
        return (1, 0, 0, 0)
    if kind == "leaf":
        return (0, 0, 0, formula_complexity(tree["formula"]))
    true_metrics = tree_metrics(tree["true"])
    false_metrics = tree_metrics(tree["false"])
    return (
        true_metrics[0] + false_metrics[0],
        true_metrics[1] + false_metrics[1] + (1 if kind == "derived_value_guard" else 0),
        true_metrics[2] + false_metrics[2] + (1 if kind == "threshold_guard" else 0),
        true_metrics[3] + false_metrics[3],
    )


def tree_score(tree: dict[str, Any]) -> tuple[int, int, int, int, str]:
    return (*tree_metrics(tree), json.dumps(tree, sort_keys=True))


def tree_derived_guard_values(tree: dict[str, Any]) -> set[int]:
    if tree["kind"] in ("unknown", "leaf"):
        return set()
    values = {tree["guard"]["value"]} if tree["kind"] == "derived_value_guard" else set()
    return values | tree_derived_guard_values(tree["true"]) | tree_derived_guard_values(tree["false"])


def tree_exact_constant_values(tree: dict[str, Any]) -> set[int]:
    """Return exact constant assignments represented by the whole observed tree.

    A bare constant leaf is exact.  A signal-gated tree is also exact for this
    purpose when every non-unknown observed branch resolves to the same exact
    constant.  Numeric threshold and derived-value trees are not collapsed to
    a constant merely because one of their leaves happens to contain it.
    """
    kind = tree["kind"]
    if kind == "leaf":
        formula = tree["formula"]
        return {formula["value"]} if formula["kind"] == "constant" else set()
    if kind != "signal_guard":
        return set()
    observed = [branch for branch in (tree["true"], tree["false"]) if branch["kind"] != "unknown"]
    if not observed:
        return set()
    values = [tree_exact_constant_values(branch) for branch in observed]
    if any(len(branch_values) != 1 for branch_values in values):
        return set()
    common = set.intersection(*values)
    return common if len(common) == 1 else set()


def candidate_preference(tree: dict[str, Any], preferred_values: list[int] | None = None) -> dict[str, Any]:
    preferred_values = [] if preferred_values is None else preferred_values
    constant_values = tree_exact_constant_values(tree)
    constant_matches = [value for value in preferred_values if value in constant_values]
    if constant_matches:
        value = constant_matches[0]
        return {
            "preferred": True,
            "tier": 0,
            "rank": preferred_values.index(value),
            "reason": "preferred_exact_constant_assignment",
            "value": value,
        }
    guard_values = tree_derived_guard_values(tree)
    matches = [value for value in preferred_values if value in guard_values]
    if matches:
        value = matches[0]
        return {
            "preferred": True,
            "tier": 1,
            "rank": preferred_values.index(value),
            "reason": "preferred_derived_guard_value",
            "value": value,
        }
    return {
        "preferred": False,
        "tier": 2,
        "rank": len(preferred_values),
        "reason": "default_complexity_order",
        "value": None,
    }


def preferred_tree_score(
    tree: dict[str, Any], preferred_values: list[int] | None = None,
) -> tuple[int, int, int, int, int, int, str]:
    preference = candidate_preference(tree, preferred_values)
    return (preference["tier"], preference["rank"], *tree_score(tree))


def unique_sorted_trees(
    candidates: list[dict[str, Any]], preferred_values: list[int] | None = None,
) -> list[dict[str, Any]]:
    unique = {json.dumps(candidate, sort_keys=True): candidate for candidate in candidates}
    return sorted(unique.values(), key=lambda tree: preferred_tree_score(tree, preferred_values))


def tree_text(tree: dict[str, Any], indent: int = 0) -> str:
    prefix = "  " * indent
    kind = tree["kind"]
    if kind == "unknown":
        return f"{prefix}unknown/{tree['reason']}"
    if kind == "leaf":
        return f"{prefix}{formula_text(tree['formula'])}"
    guard = tree["guard"]
    if kind == "signal_guard":
        condition = f"s{guard['slot']} == 1"
    elif kind == "threshold_guard":
        if guard["variable"] == "r":
            variable = "r"
        elif guard["variable"] == "input_register":
            variable = f"r_i[{guard['input_register_id']}]"
        else:
            variable = f"i{guard['slot']}"
        condition = f"{variable} < {guard['threshold']}"
    else:
        variable = f"r_i[{guard['input_register_id']}]" if guard["variable"] == "input_register" else f"i{guard['slot']}"
        condition = f"{variable} == {guard['value']}"
    return "\n".join((
        f"{prefix}if {condition}:",
        tree_text(tree["true"], indent + 1),
        f"{prefix}else:",
        tree_text(tree["false"], indent + 1),
    ))


def guard_path(tree: dict[str, Any], branch: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    branch = [] if branch is None else branch
    if tree["kind"] in ("leaf", "unknown"):
        return [{"path": branch, "terminal": tree}]
    result: list[dict[str, Any]] = []
    node_guard = {"node_kind": tree["kind"], **tree["guard"]}
    result.extend(guard_path(tree["true"], [*branch, {**node_guard, "branch": True}]))
    result.extend(guard_path(tree["false"], [*branch, {**node_guard, "branch": False}]))
    return result


def candidate_status(tree: dict[str, Any]) -> str:
    return "partial_observational_candidate" if tree_metrics(tree)[0] else "observationally_exact_candidate"


def candidate_record(
    tree: dict[str, Any], preferred_values: list[int] | None = None,
) -> dict[str, Any]:
    """Return the stable, evidence-facing representation of one update tree."""
    return {
        "guard_path": guard_path(tree),
        "update_tree": tree,
        "update_tree_text": tree_text(tree),
        "observational_status": candidate_status(tree),
        "preference": candidate_preference(tree, preferred_values),
        "complexity": {
            "unknown_branches": tree_metrics(tree)[0],
            "derived_value_nodes": tree_metrics(tree)[1],
            "threshold_nodes": tree_metrics(tree)[2],
            "leaf_formula_complexity": tree_metrics(tree)[3],
        },
    }


def resolve_hypothetical_candidates(
    direct: list[dict[str, Any]], signal_slots: list[dict[str, Any]], register_ids: list[str],
    min_support: int, max_numeric_depth: int, max_derived_depth: int,
    preferred_derived_values: list[int] | None = None,
) -> dict[str, Any]:
    """Build each cycle's minimal logic before any combined-sample fit."""
    partitioned: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in direct:
        partitioned[sample["cycle_id"]].append(sample)

    partitions: list[dict[str, Any]] = []
    trees_by_partition: dict[str, dict[str, dict[str, Any]]] = {}
    for cycle_id in sorted(partitioned):
        samples = partitioned[cycle_id]
        trees = v3_signal_gated_candidates(
            samples, signal_slots, register_ids, min_support, max_numeric_depth, max_derived_depth,
            preferred_derived_values=preferred_derived_values,
        )
        by_key = {json.dumps(tree, ensure_ascii=False, sort_keys=True): tree for tree in trees}
        trees_by_partition[cycle_id] = by_key
        partitions.append({
            "cycle_id": cycle_id,
            "sequence_lines": sorted({sample["sequence_line"] for sample in samples}),
            "support_count": len(samples),
            "longest_consecutive_support": longest_consecutive_support(samples),
            "candidates": [
                candidate_record(tree, preferred_derived_values)
                for tree in sorted(
                    by_key.values(), key=lambda item: preferred_tree_score(item, preferred_derived_values),
                )
            ],
        })

    intersection_keys = set.intersection(*(set(trees) for trees in trees_by_partition.values())) if trees_by_partition else set()
    representative = {
        key: next(trees[key] for trees in trees_by_partition.values() if key in trees)
        for key in intersection_keys
    }
    return {
        "strategy": "fit_cycle_minimal_candidates_then_combine_samples_if_intersection_empty",
        "cycle_axis": "cycle_id",
        "cycle_candidates": partitions,
        "intersection_candidates": [
            candidate_record(representative[key], preferred_derived_values)
            for key in sorted(
                intersection_keys,
                key=lambda item: preferred_tree_score(representative[item], preferred_derived_values),
            )
        ],
    }


def structural_candidates(edges: Iterable[dict[str, Any]], d_states: set[str], downlink_paths: dict[str, str], uplink_paths: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for edge in edges:
        if edge["source_state"] != edge["target_state"] and edge["target_state"] in d_states:
            result.append({"formula": {"kind": "constant", "value": 7}, "origin": "d_state_reset_prior", "priority": 0})
        if edge["logical_output"] not in downlink_paths and edge["logical_input"] not in uplink_paths:
            result.append({"formula": {"kind": "r_plus", "value": 0}, "origin": "no_ksi_default", "priority": 1})
    return result


def structural_candidates_v3(
    edges: Iterable[dict[str, Any]], d_states: set[str], downlink_paths: dict[str, str],
    numeric_definitions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for edge in edges:
        if edge["source_state"] != edge["target_state"] and edge["target_state"] in d_states:
            result.append({"formula": {"kind": "constant", "value": 7}, "origin": "d_state_reset_prior", "priority": 0})
        input_has_numeric = any(
            "*" in definition["match"]["input_symbols"] or edge["logical_input"] in definition["match"]["input_symbols"]
            for definition in numeric_definitions
        )
        if edge["logical_output"] not in downlink_paths and not input_has_numeric:
            result.append({"formula": {"kind": "r_plus", "value": 0}, "origin": "no_ksi_default", "priority": 1})
    return result


def effective_snapshot(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Project raw region observations to last-write values without discarding audit data."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    overwritten: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        key_id = item["signal_id"] if item["kind"] == "signal" else item["input_register_id"]
        key = (item["kind"], key_id)
        if key in latest:
            overwritten[key].append(latest[key])
        latest[key] = item
    projected: list[dict[str, Any]] = []
    for key, item in latest.items():
        clone = dict(item)
        clone["occurrence_index"] = 0
        clone["overwrites"] = [
            {
                "input_symbol": old["input_symbol"], "field_path": old["field_path"],
                "value": old["value"], "trace_line": old["trace_line"],
                "event_position": old["event_position"],
            }
            for old in overwritten[key]
        ]
        projected.append(clone)
    signals = sorted(
        (item for item in projected if item["kind"] == "signal"),
        key=lambda item: (item["declaration_index"], item["signal_id"]),
    )
    numeric = sorted(
        (item for item in projected if item["kind"] == "numeric_input"),
        key=lambda item: (item["declaration_index"], item["input_register_id"]),
    )
    return {
        "signals": signals,
        "numeric_inputs": numeric,
        "observation_items": [*signals, *numeric],
        "overwritten_count": sum(len(items) for items in overwritten.values()),
    }


def v3_input_register_updates(
    event: dict[str, Any], all_register_ids: list[str], cycle_register_ids: set[str],
) -> list[dict[str, Any]]:
    writes: dict[str, dict[str, Any]] = {}
    for item in event["numeric_inputs"]:
        writes[item["input_register_id"]] = item
    result: list[dict[str, Any]] = []
    for register_id in all_register_ids:
        if register_id not in cycle_register_ids:
            result.append({
                "input_register_id": register_id,
                "observability": "unobservable_input_register",
                "update": None,
            })
        elif register_id in writes:
            source = writes[register_id]
            result.append({
                "input_register_id": register_id,
                "observability": "direct_input_observation",
                "update": {
                    "kind": "input_assignment",
                    "text": f"r_i[{register_id}]' = i",
                    "source": {key: source[key] for key in ("definition_id", "input_symbol", "field_path", "value")},
                },
            })
        else:
            result.append({
                "input_register_id": register_id,
                "observability": "carried_input_register",
                "update": {"kind": "input_hold", "text": f"r_i[{register_id}]' = r_i[{register_id}]"},
            })
    return result


def build_v3_regions(
    cycle: dict[str, Any], variant: dict[str, Any], sequence_id: int, records: list[dict[str, Any]],
    indexed_edges: dict[tuple[str, str, str], dict[str, Any]], downlink_paths: dict[str, str],
    numeric_definitions: list[dict[str, Any]], signal_definitions: list[dict[str, Any]],
    initial_repeat: int, end_repeat: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """Build raw regions and per-edge samples for schema-v3 inference.

    Input registers are intentionally reset at the beginning of repetition 2.
    Repetition 2 establishes their values; callers fit only later repetitions.
    """
    events: list[dict[str, Any]] = []
    register_values: dict[str, dict[str, Any]] = {}
    cycle_register_ids: set[str] = set()
    previous: dict[str, Any] | None = None
    pending: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []
    for repetition, loop_edge_index, record, edge in source_events(cycle, variant, records, indexed_edges, end_repeat):
        if repetition == initial_repeat:
            register_values = {}
        signals = signal_observations(record, edge, signal_definitions)
        numeric = numeric_input_observations(record, edge, numeric_definitions)
        for item in numeric:
            register_values[item["input_register_id"]] = dict(item)
            cycle_register_ids.add(item["input_register_id"])
        event = {
            "cycle_id": cycle["cycle_id"], "sequence_line": variant["line_number"], "trace_sequence_id": sequence_id,
            "repetition": repetition, "loop_edge_index": loop_edge_index, "edge": edge, "trace_line": record["_trace_line"],
            "event_position": record.get("step_id"), "signals": signals, "numeric_inputs": numeric,
            "input_register_values": {key: dict(value) for key, value in register_values.items()},
        }
        events.append(event)
        pending.append(event)
        current_output = output_observation(record, edge, downlink_paths)
        event["downlink_output"] = current_output
        if current_output is None:
            continue
        if previous is not None and repetition >= initial_repeat:
            raw_items = [item for pending_event in pending for item in [*pending_event["signals"], *pending_event["numeric_inputs"]]]
            assign_occurrence_indices(raw_items)
            snapshots: list[dict[str, Any]] = []
            prefix_items: list[dict[str, Any]] = []
            for pending_event in pending:
                prefix_items.extend([*pending_event["signals"], *pending_event["numeric_inputs"]])
                snapshots.append(effective_snapshot(prefix_items))
            regions.append({
                "cycle_id": cycle["cycle_id"], "sequence_line": variant["line_number"],
                "trace_sequence_id": sequence_id, "repetition": repetition,
                "previous_output": previous, "terminal_output": current_output,
                "raw_observation_items": raw_items, "effective_region_snapshot": effective_snapshot(raw_items),
                "edge_events": [
                    {
                        **event_item,
                        "effective_edge_snapshot": snapshots[position],
                    }
                    for position, event_item in enumerate(pending)
                ],
            })
        previous = current_output
        pending = []
    return regions, events, cycle_register_ids


def standalone_event_sample(event: dict[str, Any]) -> dict[str, Any]:
    """Create an independently aligned sample for an edge without a terminal region.

    Region observations receive occurrence indexes across all pending events. A
    predecessor edge may have no later downlink anchor and is therefore absent
    from ``direct_by_edge``; its event-local observations still need the same
    explicit slot identity before they can form a signal guard.
    """
    items = [dict(item) for item in [*event["signals"], *event["numeric_inputs"]]]
    assign_occurrence_indices(items)
    return {
        "sequence_line": event["sequence_line"],
        "repetition": event["repetition"],
        "signals": [item for item in items if item["kind"] == "signal"],
        "inputs": [item for item in items if item["kind"] == "numeric_input"],
        "observation_items": items,
        "terminal_edge": event["edge"],
    }


def region_edge_record(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge": event["edge"],
        "loop_edge_index": event["loop_edge_index"],
        "trace_line": event["trace_line"],
        "event_position": event["event_position"],
        "signals": event["signals"],
        "numeric_inputs": event["numeric_inputs"],
        "input_register_values": event["input_register_values"],
        "input_register_updates": event.get("input_register_updates", []),
        "effective_edge_snapshot": event["effective_edge_snapshot"],
    }


def v3_exact_leaf_candidates(regions: list[dict[str, Any]], register_ids: list[str]) -> list[dict[str, Any]]:
    if not regions:
        return []
    after = [region["terminal_output"]["value"] for region in regions]
    candidates: list[dict[str, Any]] = []
    if len(set(after)) == 1:
        candidates.append({"kind": "constant", "value": after[0]})
    deltas = [region["terminal_output"]["value"] - region["previous_output"]["value"] for region in regions]
    if len(set(deltas)) == 1:
        candidates.append({"kind": "r_plus", "value": deltas[0]})
    for register_id in register_ids:
        values = [input_register_value(region, register_id) for region in regions]
        offsets = [after_value - value for after_value, value in zip(after, values)]
        if len(set(offsets)) == 1:
            candidates.append({"kind": "input_register_plus", "input_register_id": register_id, "value": offsets[0]})
    unique = {json.dumps(item, sort_keys=True): item for item in candidates}
    return sorted(unique.values(), key=lambda item: (formula_complexity(item), json.dumps(item, sort_keys=True)))


def v3_guard_value(guard: dict[str, Any], region: dict[str, Any]) -> int:
    return region["previous_output"]["value"] if guard["variable"] == "r" else input_register_value(region, guard["input_register_id"])


def v3_threshold_candidates(regions: list[dict[str, Any]], register_ids: list[str]) -> list[dict[str, Any]]:
    guards = [
        {"variable": "r", "operator": "<", "threshold": value}
        for value in sorted({region["previous_output"]["value"] for region in regions})[1:]
    ]
    for register_id in register_ids:
        guards.extend(
            {"variable": "input_register", "input_register_id": register_id, "operator": "<", "threshold": value}
            for value in sorted({input_register_value(region, register_id) for region in regions})[1:]
        )
    false_formula = {"kind": "constant", "value": 0}
    candidates: list[dict[str, Any]] = []
    for guard in guards:
        true_regions = [region for region in regions if v3_guard_value(guard, region) < guard["threshold"]]
        false_regions = [region for region in regions if v3_guard_value(guard, region) >= guard["threshold"]]
        if not true_regions or not false_regions or false_formula not in v3_exact_leaf_candidates(false_regions, register_ids):
            continue
        for true_formula in v3_exact_leaf_candidates(true_regions, register_ids):
            candidates.append({
                "kind": "threshold_guard", "guard": guard,
                "true": {"kind": "leaf", "formula": true_formula},
                "false": {"kind": "leaf", "formula": false_formula},
            })
    return candidates


def v3_fit_without_derived(regions: list[dict[str, Any]], register_ids: list[str], min_support: int, max_numeric_depth: int) -> list[dict[str, Any]]:
    if longest_consecutive_support(regions) < min_support:
        return []
    leaves = [{"kind": "leaf", "formula": formula} for formula in v3_exact_leaf_candidates(regions, register_ids)]
    return leaves if leaves else (v3_threshold_candidates(regions, register_ids) if max_numeric_depth else [])


def v3_derived_candidates(regions: list[dict[str, Any]], register_ids: list[str], min_support: int, max_numeric_depth: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for register_id in register_ids:
        for value in sorted({input_register_value(region, register_id) for region in regions}):
            true_regions = [region for region in regions if input_register_value(region, register_id) == value]
            false_regions = [region for region in regions if input_register_value(region, register_id) != value]
            if not true_regions or not false_regions or min(longest_consecutive_support(true_regions), longest_consecutive_support(false_regions)) < min_support:
                continue
            for true_tree, false_tree in product(
                v3_fit_without_derived(true_regions, register_ids, min_support, max_numeric_depth),
                v3_fit_without_derived(false_regions, register_ids, min_support, max_numeric_depth),
            ):
                candidates.append({
                    "kind": "derived_value_guard",
                    "guard": {"variable": "input_register", "input_register_id": register_id, "operator": "==", "value": value},
                    "true": true_tree, "false": false_tree,
                })
    return candidates


def v3_numeric_candidates(
    regions: list[dict[str, Any]], register_ids: list[str], min_support: int,
    max_numeric_depth: int, max_derived_depth: int, preferred_derived_values: list[int] | None = None,
) -> list[dict[str, Any]]:
    candidates = v3_fit_without_derived(regions, register_ids, min_support, max_numeric_depth)
    if not candidates and max_derived_depth:
        candidates = v3_derived_candidates(regions, register_ids, min_support, max_numeric_depth)
    return unique_sorted_trees(candidates, preferred_derived_values)


def v3_signal_gated_candidates(
    regions: list[dict[str, Any]], signal_slots: list[dict[str, Any]], register_ids: list[str], min_support: int,
    max_numeric_depth: int, max_derived_depth: int, signal_offset: int = 0,
    preferred_derived_values: list[int] | None = None,
) -> list[dict[str, Any]]:
    if signal_offset == len(signal_slots):
        if longest_consecutive_support(regions) < min_support:
            return [unknown_node("insufficient_support")]
        return v3_numeric_candidates(
            regions, register_ids, min_support, max_numeric_depth, max_derived_depth, preferred_derived_values,
        )
    slot = signal_slots[signal_offset]
    branches: dict[int, list[dict[str, Any]]] = {}
    for value in (1, 0):
        subset = [region for region in regions if signal_value(region, signal_offset) == value]
        if not subset:
            branches[value] = [unknown_node("unobserved_signal_branch")]
        elif longest_consecutive_support(subset) < min_support:
            branches[value] = [unknown_node("insufficient_support")]
        else:
            branches[value] = v3_signal_gated_candidates(
                subset, signal_slots, register_ids, min_support, max_numeric_depth, max_derived_depth,
                signal_offset + 1, preferred_derived_values,
            )
    return unique_sorted_trees([
        {
            "kind": "signal_guard",
            "guard": {
                "variable": "signal", "slot": signal_offset, "operator": "==", "value": 1,
                "signal_id": slot["signal_id"], "field_path": slot["field_path"],
                "input_symbol": slot["input_symbol"], "occurrence_index": slot["occurrence_index"],
            },
            "true": true_tree, "false": false_tree,
        }
        for true_tree, false_tree in product(branches[1], branches[0])
    ], preferred_derived_values)


def observed_numeric_tree(tree: dict[str, Any]) -> dict[str, Any] | None:
    """Remove signal guards whose other branch is explicitly unobserved.

    This normalization is only used when comparing the observed numeric update
    across concrete edges with the same abstract input/output pair. It does not
    fill in or claim anything about the unobserved signal branch.
    """
    current = tree
    while current.get("kind") == "signal_guard":
        true_tree = current["true"]
        false_tree = current["false"]
        true_unknown = true_tree.get("kind") == "unknown"
        false_unknown = false_tree.get("kind") == "unknown"
        if true_unknown == false_unknown:
            return None
        current = false_tree if true_unknown else true_tree
    return current


def v3_tree_value(tree: dict[str, Any], region: dict[str, Any]) -> int | None:
    kind = tree["kind"]
    if kind == "unknown":
        return None
    if kind == "leaf":
        return formula_value(tree["formula"], region)
    guard = tree["guard"]
    if kind == "signal_guard":
        branch = signal_value(region, guard["slot"]) == 1
    else:
        value = v3_guard_value(guard, region)
        branch = value < guard["threshold"] if kind == "threshold_guard" else value == guard["value"]
    return v3_tree_value(tree["true"] if branch else tree["false"], region)


def typed_signal_context(sample: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "signal_id": signal["signal_id"],
            "field_path": signal["field_path"],
            "input_symbol": signal["input_symbol"],
            "occurrence_index": signal["occurrence_index"],
            "value": signal["value"],
        }
        for signal in sample.get("signals", [])
    ]


def signal_context_key(sample: dict[str, Any]) -> str:
    return json.dumps(typed_signal_context(sample), ensure_ascii=False, sort_keys=True)


def typed_terminal_signal_context(sample: dict[str, Any]) -> list[dict[str, Any]]:
    region_edges = sample.get("region_edges", [])
    context = typed_signal_context(region_edges[-1] if region_edges else sample)
    occurrences: dict[tuple[str, str, str], int] = defaultdict(int)
    normalized: list[dict[str, Any]] = []
    for signal in context:
        key = (signal["signal_id"], signal["field_path"], signal["input_symbol"])
        normalized.append({**signal, "occurrence_index": occurrences[key]})
        occurrences[key] += 1
    return normalized


def terminal_signal_context_key(sample: dict[str, Any]) -> str:
    return json.dumps(typed_terminal_signal_context(sample), ensure_ascii=False, sort_keys=True)


def set_valued_leaf_formulas(
    samples: list[dict[str, Any]], register_ids: list[str], value_domain: list[int],
) -> list[dict[str, Any]]:
    if not samples:
        return []
    allowed = [set(sample["allowed_r_after_values"]) for sample in samples]
    formulas: list[dict[str, Any]] = []
    for value in value_domain:
        if all(value in values for values in allowed):
            formulas.append({"kind": "constant", "value": value})

    first_before = samples[0]["previous_output"]["value"]
    for offset in sorted({value - first_before for value in allowed[0]}):
        if all(sample["previous_output"]["value"] + offset in values for sample, values in zip(samples, allowed)):
            formulas.append({"kind": "r_plus", "value": offset})

    for register_id in register_ids:
        first_input = input_register_value(samples[0], register_id)
        for offset in sorted({value - first_input for value in allowed[0]}):
            if all(
                input_register_value(sample, register_id) + offset in values
                for sample, values in zip(samples, allowed)
            ):
                formulas.append({
                    "kind": "input_register_plus", "input_register_id": register_id, "value": offset,
                })
    unique = {json.dumps(formula, sort_keys=True): formula for formula in formulas}
    return sorted(unique.values(), key=lambda formula: (formula_complexity(formula), json.dumps(formula, sort_keys=True)))


def set_valued_threshold_candidates(
    samples: list[dict[str, Any]], register_ids: list[str], value_domain: list[int],
) -> list[dict[str, Any]]:
    guards = [
        {"variable": "r", "operator": "<", "threshold": value}
        for value in sorted({sample["previous_output"]["value"] for sample in samples})[1:]
    ]
    for register_id in register_ids:
        guards.extend(
            {"variable": "input_register", "input_register_id": register_id, "operator": "<", "threshold": value}
            for value in sorted({input_register_value(sample, register_id) for sample in samples})[1:]
        )
    candidates: list[dict[str, Any]] = []
    for guard in guards:
        true_samples = [sample for sample in samples if v3_guard_value(guard, sample) < guard["threshold"]]
        false_samples = [sample for sample in samples if v3_guard_value(guard, sample) >= guard["threshold"]]
        if not true_samples or not false_samples:
            continue
        if not all(0 in sample["allowed_r_after_values"] for sample in false_samples):
            continue
        for formula in set_valued_leaf_formulas(true_samples, register_ids, value_domain):
            candidates.append({
                "kind": "threshold_guard", "guard": guard,
                "true": {"kind": "leaf", "formula": formula},
                "false": {"kind": "leaf", "formula": {"kind": "constant", "value": 0}},
            })
    return candidates


def set_valued_fit_without_derived(
    samples: list[dict[str, Any]], register_ids: list[str], value_domain: list[int],
    min_support: int, max_numeric_depth: int,
) -> list[dict[str, Any]]:
    if longest_consecutive_support(samples) < min_support:
        return []
    leaves = [
        {"kind": "leaf", "formula": formula}
        for formula in set_valued_leaf_formulas(samples, register_ids, value_domain)
    ]
    return leaves if leaves else (
        set_valued_threshold_candidates(samples, register_ids, value_domain) if max_numeric_depth else []
    )


def set_valued_numeric_candidates(
    samples: list[dict[str, Any]], register_ids: list[str], value_domain: list[int], min_support: int,
    max_numeric_depth: int, max_derived_depth: int, preferred_derived_values: list[int],
) -> list[dict[str, Any]]:
    candidates = set_valued_fit_without_derived(
        samples, register_ids, value_domain, min_support, max_numeric_depth,
    )
    if not candidates and max_derived_depth:
        derived: list[dict[str, Any]] = []
        for register_id in register_ids:
            for value in sorted({input_register_value(sample, register_id) for sample in samples}):
                true_samples = [sample for sample in samples if input_register_value(sample, register_id) == value]
                false_samples = [sample for sample in samples if input_register_value(sample, register_id) != value]
                if not true_samples or not false_samples:
                    continue
                if min(longest_consecutive_support(true_samples), longest_consecutive_support(false_samples)) < min_support:
                    continue
                for true_tree, false_tree in product(
                    set_valued_fit_without_derived(
                        true_samples, register_ids, value_domain, min_support, max_numeric_depth,
                    ),
                    set_valued_fit_without_derived(
                        false_samples, register_ids, value_domain, min_support, max_numeric_depth,
                    ),
                ):
                    derived.append({
                        "kind": "derived_value_guard",
                        "guard": {
                            "variable": "input_register", "input_register_id": register_id,
                            "operator": "==", "value": value,
                        },
                        "true": true_tree, "false": false_tree,
                    })
        candidates = derived
    return unique_sorted_trees(candidates, preferred_derived_values)


def fill_observed_signal_branch(
    tree: dict[str, Any], sample: dict[str, Any], replacement: dict[str, Any], signal_offset: int = 0,
) -> dict[str, Any]:
    if tree["kind"] != "signal_guard":
        return replacement
    if signal_offset >= len(sample.get("signals", [])):
        raise RegionInferenceError("Backward inference: predecessor signal context is incomplete.")
    branch = sample["signals"][signal_offset]["value"] == 1
    return {
        **tree,
        "true": fill_observed_signal_branch(tree["true"], sample, replacement, signal_offset + 1)
        if branch else tree["true"],
        "false": tree["false"] if branch else fill_observed_signal_branch(
            tree["false"], sample, replacement, signal_offset + 1,
        ),
    }


def infer_nearest_predecessor(
    *, target_item: dict[str, Any], cycle_id: str, samples: list[dict[str, Any]],
    relatively_stable_candidate: dict[str, Any], results_by_edge: dict[str, dict[str, Any]],
    downlink_paths: dict[str, str], value_domain: list[int], min_support: int,
    max_numeric_depth: int, max_derived_depth: int, preferred_derived_values: list[int],
) -> dict[str, Any]:
    paths = [sample.get("region_edges", []) for sample in samples]
    if any(len(path) < 2 for path in paths):
        return {"status": "no_predecessor_without_downlink_anchor", "attempts": []}
    predecessors = [path[-2] for path in paths]
    predecessor_ids = {event["edge"]["edge_id"] for event in predecessors}
    if len(predecessor_ids) != 1:
        return {
            "status": "unaligned_nearest_predecessor", "attempts": [],
            "predecessor_edge_ids": sorted(predecessor_ids),
        }
    predecessor_id = next(iter(predecessor_ids))
    predecessor_edge = predecessors[0]["edge"]
    if predecessor_edge["logical_output"] in downlink_paths:
        return {
            "status": "nearest_predecessor_has_downlink_anchor", "attempts": [],
            "predecessor_edge_id": predecessor_id,
        }
    predecessor_contexts = {signal_context_key(event) for event in predecessors}
    if len(predecessor_contexts) != 1:
        return {
            "status": "unaligned_predecessor_signal_context", "attempts": [],
            "predecessor_edge_id": predecessor_id,
        }
    predecessor_item = results_by_edge.get(predecessor_id)
    if predecessor_item is None or not predecessor_item.get("candidates"):
        return {
            "status": "predecessor_candidate_unavailable", "attempts": [],
            "predecessor_edge_id": predecessor_id,
        }

    held_predecessors: dict[str, dict[str, Any]] = {}
    for path in paths:
        for event in path[:-2]:
            edge = event["edge"]
            if edge["logical_output"] in downlink_paths:
                return {
                    "status": "earlier_predecessor_has_downlink_anchor", "attempts": [],
                    "predecessor_edge_id": predecessor_id,
                }
            held_predecessors[edge["edge_id"]] = {
                "edge": edge,
                "assumed_update": {"kind": "r_plus", "value": 0},
                "assumed_update_text": "r' = r",
            }

    inferred_samples: list[dict[str, Any]] = []
    for sample, predecessor, path in zip(samples, predecessors, paths):
        terminal_event = path[-1]
        allowed: list[int] = []
        for candidate_value in value_domain:
            proxy = {
                **sample,
                "previous_output": {**sample["previous_output"], "value": candidate_value},
                "input_register_values": terminal_event["input_register_values"],
            }
            if v3_tree_value(relatively_stable_candidate["update_tree"], proxy) == sample["terminal_output"]["value"]:
                allowed.append(candidate_value)
        inferred_samples.append({
            "cycle_id": cycle_id,
            "sequence_line": sample["sequence_line"],
            "repetition": sample["repetition"],
            "previous_output": {"value": sample["previous_output"]["value"]},
            "input_register_values": predecessor["input_register_values"],
            "allowed_r_after_values": allowed,
            "target_r_after": sample["terminal_output"]["value"],
            "target_input_register_values": terminal_event["input_register_values"],
        })
    if any(not sample["allowed_r_after_values"] for sample in inferred_samples):
        return {
            "status": "no_preimage_in_observed_global_domain", "attempts": [],
            "predecessor_edge_id": predecessor_id, "samples": inferred_samples,
        }

    common_registers = sorted(set.intersection(*(
        set(sample["input_register_values"]) for sample in inferred_samples
    ))) if inferred_samples else []
    branch_trees = set_valued_numeric_candidates(
        inferred_samples, common_registers, value_domain, min_support,
        max_numeric_depth, max_derived_depth, preferred_derived_values,
    )
    baseline_trees = [candidate["update_tree"] for candidate in predecessor_item["candidates"]]
    paired_trees: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for baseline, branch in product(baseline_trees, branch_trees):
        full_tree = fill_observed_signal_branch(baseline, predecessors[0], branch)
        key = json.dumps(full_tree, ensure_ascii=False, sort_keys=True)
        paired_trees.setdefault(key, (branch, full_tree))
    inference_id = f"{target_item['edge']['edge_id']}:{cycle_id}:{predecessor_id}"
    assumptions = [
        "relatively_stable_inference_backpropagation", "nearest_no_downlink_predecessor",
        "earlier_predecessors_hold", "observed_global_value_domain", "region_to_edge_decomposition",
    ]
    candidates = []
    for branch, full_tree in sorted(
        paired_trees.values(),
        key=lambda pair: (
            preferred_tree_score(pair[0], preferred_derived_values),
            preferred_tree_score(pair[1], preferred_derived_values),
        ),
    ):
        candidate = {
            **candidate_record(full_tree, preferred_derived_values),
            "candidate_grade": "hypothetical_candidate",
            "origin": "relatively_stable_inference_backpropagation",
            "assumptions": assumptions,
            "support_count": len(inferred_samples),
            "branch_update_tree": branch,
            "branch_update_text": tree_text(branch),
            "preference": candidate_preference(branch, preferred_derived_values),
            "preference_scope": "inferred_observed_signal_branch",
        }
        candidates.append(candidate)
    candidates.sort(key=lambda item: (
        preferred_tree_score(item["branch_update_tree"], preferred_derived_values),
        preferred_tree_score(item["update_tree"], preferred_derived_values),
    ))
    attempt = {
        "inference_id": inference_id,
        "status": "candidate_set_generated" if candidates else "no_exact_candidate",
        "target_edge": target_item["edge"],
        "cycle_id": cycle_id,
        "inferred_edge": predecessor_edge,
        "predecessor_policy": "nearest_no_downlink_predecessor",
        "earlier_predecessor_policy": "hold",
        "held_predecessors": [held_predecessors[key] for key in sorted(held_predecessors)],
        "signal_context": typed_signal_context(predecessors[0]),
        "numeric_inputs": predecessors[0]["numeric_inputs"],
        "global_register_value_domain": value_domain,
        "relatively_stable_candidate": relatively_stable_candidate,
        "samples": inferred_samples,
        "candidates": candidates,
    }
    predecessor_item.setdefault("backward_inference", {"attempts": []})["attempts"].append(attempt)
    return {"status": attempt["status"], "attempts": [attempt]}


def relatively_stable_group_key(
    logical_input: str, logical_output: str, signal_context: list[dict[str, Any]],
) -> tuple[str, str, str]:
    return (
        logical_input,
        logical_output,
        json.dumps(signal_context, ensure_ascii=False, sort_keys=True),
    )


def relatively_stable_inference(
    results: list[dict[str, Any]], min_support: int, max_numeric_depth: int, max_derived_depth: int,
    *, downlink_paths: dict[str, str], value_domain: list[int], backward_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build signal-bound relatively stable inference and migrate it to hypotheses."""
    stable_regions_by_key: dict[tuple[str, str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for item in results:
        if item.get("candidate_grade") != "relatively_stable_candidate":
            continue
        edge = item["edge"]
        logical_input = str(edge["logical_input"])
        logical_output = str(edge["logical_output"])
        for region in item.get("direct_regions", []):
            context = typed_terminal_signal_context(region)
            stable_regions_by_key[relatively_stable_group_key(logical_input, logical_output, context)].append(
                (item, region)
            )

    groups: list[dict[str, Any]] = []
    groups_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    preference_sources: dict[int, list[dict[str, Any]]] = defaultdict(list)
    preferred_values: list[int] = []
    for group_index, key in enumerate(sorted(stable_regions_by_key), start=1):
        logical_input, logical_output, context_json = key
        entries = stable_regions_by_key[key]
        stable_regions = [region for _, region in entries]
        signal_context = json.loads(context_json)
        signal_slots = stable_slots(stable_regions, "signal") if signal_context else []
        common_registers = sorted(set.intersection(*(
            set(region.get("input_register_values", {})) for region in stable_regions
        ))) if stable_regions else []
        trees = v3_signal_gated_candidates(
            stable_regions, signal_slots, common_registers, min_support, max_numeric_depth, max_derived_depth,
            preferred_derived_values=preferred_values,
        )
        source_edge_ids = sorted({str(item["edge"]["edge_id"]) for item, _ in entries})
        candidates = [
            {
                **candidate_record(tree, preferred_values),
                "candidate_grade": "relatively_stable_candidate",
                "source_edge_ids": source_edge_ids,
                "support_count": len(stable_regions),
                "longest_consecutive_support": longest_consecutive_support(stable_regions),
            }
            for tree in trees
        ]
        group = {
            "group_index": group_index,
            "signal_context": signal_context,
            "logical_input": logical_input,
            "logical_output": logical_output,
            "source_edge_ids": source_edge_ids,
            "support_count": len(stable_regions),
            "target_edge_ids": [],
            "target_partition_count": 0,
            "candidates": candidates,
        }
        groups.append(group)
        groups_by_key[key] = group
        discovered_values: list[int] = []
        for candidate in candidates:
            for value in sorted(tree_derived_guard_values(candidate["update_tree"])):
                preference_sources[value].append({
                    "group_index": group_index,
                    "signal_context": signal_context,
                    "logical_input": logical_input,
                    "logical_output": logical_output,
                    "source_edge_ids": source_edge_ids,
                    "candidate_update_tree": candidate["update_tree"],
                    "candidate_update_tree_text": candidate["update_tree_text"],
                })
                if value not in preferred_values and value not in discovered_values:
                    discovered_values.append(value)
        preferred_values.extend(sorted(discovered_values))

    results_by_edge = {str(item["edge"]["edge_id"]): item for item in results}
    inference_io_pairs = {
        (str(group["logical_input"]), str(group["logical_output"]))
        for group in groups
    }
    migration_status_counts: dict[str, int] = defaultdict(int)
    target_edge_ids: set[str] = set()
    target_partition_count = 0
    for item in results:
        if item.get("candidate_grade") != "hypothetical_candidate" or not item.get("direct_regions"):
            continue
        edge = item["edge"]
        logical_input = str(edge["logical_input"])
        logical_output = str(edge["logical_output"])
        if (logical_input, logical_output) not in inference_io_pairs:
            continue
        resolution = item.get("hypothetical_candidate_resolution") or {}
        cycle_results: list[dict[str, Any]] = []
        for cycle_entry in resolution.get("cycle_candidates", []):
            cycle_id = str(cycle_entry["cycle_id"])
            samples = [region for region in item["direct_regions"] if str(region["cycle_id"]) == cycle_id]
            contexts = {
                terminal_signal_context_key(sample): typed_terminal_signal_context(sample)
                for sample in samples
            }
            if len(contexts) != 1:
                raise RegionInferenceError(
                    f"Relatively stable inference migration: {edge['edge_id']}/{cycle_id} has multiple effective signal contexts."
                )
            context = next(iter(contexts.values()))
            key = relatively_stable_group_key(logical_input, logical_output, context)
            group = groups_by_key.get(key)
            target_edge_ids.add(str(edge["edge_id"]))
            target_partition_count += 1
            if group is None:
                status = "no_matching_relatively_stable_inference"
                migration_status_counts[status] += 1
                cycle_results.append({
                    "cycle_id": cycle_id,
                    "sequence_lines": sorted({sample["sequence_line"] for sample in samples}),
                    "sample_count": len(samples),
                    "signal_context": context,
                    "status": status,
                    "source_edge_ids": [],
                    "relatively_stable_candidates": [],
                    "exact_candidates": [],
                    "failing_candidates": [],
                    "backward_inference": {"status": "not_run_no_matching_inference", "attempts": []},
                })
                continue

            group["target_partition_count"] += 1
            group["target_edge_ids"] = sorted(set(group["target_edge_ids"]) | {str(edge["edge_id"])})
            local_keys = {
                json.dumps(candidate["update_tree"], ensure_ascii=False, sort_keys=True)
                for candidate in cycle_entry.get("candidates", [])
            }
            exact_candidates: list[dict[str, Any]] = []
            failing_candidates: list[dict[str, Any]] = []
            for candidate in group["candidates"]:
                candidate_key = json.dumps(candidate["update_tree"], ensure_ascii=False, sort_keys=True)
                mismatches: list[dict[str, Any]] = []
                for sample in samples:
                    predicted = v3_tree_value(candidate["update_tree"], sample)
                    observed = sample["terminal_output"]["value"]
                    if predicted == observed:
                        continue
                    mismatches.append({
                        "sequence_line": sample["sequence_line"],
                        "repetition": sample["repetition"],
                        "r_before": sample["previous_output"]["value"],
                        "signal_context": typed_terminal_signal_context(sample),
                        "numeric_inputs": [
                            {
                                "input_register_id": numeric["input_register_id"],
                                "field_path": numeric["field_path"],
                                "input_symbol": numeric["input_symbol"],
                                "value": numeric["value"],
                            }
                            for numeric in sample.get("inputs", [])
                        ],
                        "input_register_values": {
                            register_id: value["value"]
                            for register_id, value in sorted(sample.get("input_register_values", {}).items())
                        },
                        "predicted_r_after": predicted,
                        "observed_r_after": observed,
                    })
                comparison = {
                    "candidate": candidate,
                    "present_in_cycle_minimal_candidates": candidate_key in local_keys,
                }
                if mismatches:
                    failing_candidates.append({
                        **comparison,
                        "mismatch_count": len(mismatches),
                        "counterexamples": mismatches,
                    })
                else:
                    exact_candidates.append(comparison)

            status = "migration_succeeded" if exact_candidates else "migration_failed"
            migration_status_counts[status] += 1
            backward_inference = {
                "status": "not_needed_migration_succeeded",
                "attempts": [],
            }
            if status == "migration_failed" and backward_config:
                attempts: list[dict[str, Any]] = []
                statuses: list[str] = []
                for failure in failing_candidates:
                    inferred = infer_nearest_predecessor(
                        target_item=item, cycle_id=cycle_id, samples=samples,
                        relatively_stable_candidate=failure["candidate"], results_by_edge=results_by_edge,
                        downlink_paths=downlink_paths, value_domain=value_domain,
                        min_support=min_support, max_numeric_depth=max_numeric_depth,
                        max_derived_depth=max_derived_depth,
                        preferred_derived_values=preferred_values,
                    )
                    statuses.append(inferred["status"])
                    attempts.extend(inferred.get("attempts", []))
                backward_inference = {
                    "status": "candidate_set_generated"
                    if "candidate_set_generated" in statuses else (statuses[0] if statuses else "not_run"),
                    "attempts": attempts,
                }
            cycle_results.append({
                "cycle_id": cycle_id,
                "sequence_lines": sorted({sample["sequence_line"] for sample in samples}),
                "sample_count": len(samples),
                "signal_context": context,
                "status": status,
                "source_edge_ids": group["source_edge_ids"],
                "source_support_count": group["support_count"],
                "relatively_stable_candidates": group["candidates"],
                "exact_candidates": exact_candidates,
                "failing_candidates": failing_candidates,
                "backward_inference": backward_inference,
            })
        if cycle_results:
            item["relatively_stable_inference_migration"] = {
                "matching_scope": "same_effective_signal_context_logical_input_output",
                "cycle_results": cycle_results,
            }

    return {
        "method": "signal_context_bound_relatively_stable_inference",
        "dynamic_t_preference": {
            "method": "feedback_from_relatively_stable_derived_value_guards",
            "applies_after": "each_relatively_stable_group_generation",
            "values": preferred_values,
            "sources": [
                {"value": value, "inferences": preference_sources[value]}
                for value in preferred_values
            ],
            "scope": "all_later_candidate_ordering",
            "ranking_policy": [
                "preferred_exact_constant_assignment",
                "preferred_derived_guard_value",
                "default_complexity_order",
            ],
            "previous_candidates_are_not_reordered": True,
        },
        "groups": groups,
        "target_edge_count": len(target_edge_ids),
        "target_partition_count": target_partition_count,
        "migration_status_counts": dict(sorted(migration_status_counts.items())),
    }


def default_signal_tree(signal_slots: list[dict[str, Any]], signal_offset: int = 0) -> dict[str, Any]:
    """A cautious no-anchor default: only all non-initial contexts retain r."""
    if signal_offset == len(signal_slots):
        return {"kind": "leaf", "formula": {"kind": "r_plus", "value": 0}}
    slot = signal_slots[signal_offset]
    return {
        "kind": "signal_guard",
        "guard": {
            "variable": "signal", "slot": signal_offset, "operator": "==", "value": 1,
            "signal_id": slot["signal_id"], "field_path": slot["field_path"],
            "input_symbol": slot["input_symbol"], "occurrence_index": slot["occurrence_index"],
        },
        "true": unknown_node("unanchored_signal_context"),
        "false": default_signal_tree(signal_slots, signal_offset + 1),
    }


def v3_input_update_summary(events: list[dict[str, Any]], all_register_ids: list[str]) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for event in events:
        for item in event["input_register_updates"]:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            entry = summaries.setdefault(key, {**item, "support_count": 0})
            entry["support_count"] += 1
    return sorted(summaries.values(), key=lambda item: (item["input_register_id"], json.dumps(item, ensure_ascii=False, sort_keys=True)))


def infer_v3(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    inputs, mapping, analysis = config["inputs"], config["mapping"], config["analysis"]
    paths = {name: resolve(config_path, inputs[name]) for name in ("dot", "trace", "cycle_cover", "sequence_file")}
    cycle_cover = json.loads(paths["cycle_cover"].read_text(encoding="utf-8"))
    indexed_edges = edge_index(paths["dot"])
    trace_groups = read_trace_groups(paths["trace"])
    sequence_lines = read_sequence_lines(paths["sequence_file"])
    start_repeat, end_repeat = analysis.get("repetitions", [2, 10])
    numeric_definitions = validate_numeric_input_definitions(mapping)
    signal_definitions = validate_signal_definitions(mapping)
    register_ids = list(dict.fromkeys(item["input_register_id"] for item in numeric_definitions))
    downlink_paths = mapping["downlink_ksi_by_output"]
    all_regions: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    global_register_value_domain: set[int] = set()
    cycle_registers: dict[tuple[str, int], set[str]] = {}
    for cycle in selected_cycles(cycle_cover, analysis.get("cycle_ids")):
        variants = cycle.get("variants")
        if not isinstance(variants, list):
            raise RegionInferenceError(f"{cycle.get('cycle_id')}: variants must be a list.")
        for variant in variants:
            sequence_id, records = matched_records(trace_groups, sequence_lines, variant, cycle["cycle_id"])
            regions, events, seen_registers = build_v3_regions(
                cycle, variant, sequence_id, records, indexed_edges, downlink_paths,
                numeric_definitions, signal_definitions, start_repeat, end_repeat,
            )
            cycle_registers[(cycle["cycle_id"], variant["line_number"])] = seen_registers
            for event in events:
                event["input_register_updates"] = v3_input_register_updates(event, register_ids, seen_registers)
                if start_repeat <= event["repetition"] <= end_repeat:
                    global_register_value_domain.update(item["value"] for item in event["numeric_inputs"])
                    if event["downlink_output"] is not None:
                        global_register_value_domain.add(event["downlink_output"]["value"])
            event_lookup = {
                (event["trace_line"], event["event_position"], event["edge"]["edge_id"]): event
                for event in events
            }
            for region in regions:
                for region_event in region["edge_events"]:
                    source = event_lookup[(
                        region_event["trace_line"], region_event["event_position"], region_event["edge"]["edge_id"],
                    )]
                    region_event["input_register_updates"] = source["input_register_updates"]
                    region_event["downlink_output"] = source["downlink_output"]
            # Repetition 2 establishes r_i values.  All later regions are fitted.
            fit_start = start_repeat + 1 if seen_registers else start_repeat
            all_regions.extend(region for region in regions if region["repetition"] >= fit_start)
            all_events.extend(event for event in events if event["repetition"] >= fit_start)

    direct_by_edge: dict[str, list[dict[str, Any]]] = defaultdict(list)
    event_by_edge: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edges: dict[str, dict[str, Any]] = {}
    for event in all_events:
        edges[event["edge"]["edge_id"]] = event["edge"]
        event_by_edge[event["edge"]["edge_id"]].append(event)
    for region in all_regions:
        terminal_event = region["edge_events"][-1]
        edge_id = terminal_event["edge"]["edge_id"]
        snapshot = terminal_event["effective_edge_snapshot"]
        direct_by_edge[edge_id].append({
            "cycle_id": region["cycle_id"], "sequence_line": region["sequence_line"], "repetition": region["repetition"],
            "previous_output": region["previous_output"], "terminal_output": region["terminal_output"],
            "signals": snapshot["signals"], "inputs": snapshot["numeric_inputs"],
            "input_register_values": terminal_event["input_register_values"],
            "region_edge_count": len(region["edge_events"]), "raw_region": region["raw_observation_items"],
            "effective_region_snapshot": region["effective_region_snapshot"],
            "region_edges": [region_edge_record(event) for event in region["edge_events"]],
        })

    minimum = analysis.get("min_consecutive_support", 3)
    max_numeric_depth = analysis.get("max_numeric_depth", analysis.get("max_depth", 1))
    max_derived_depth = analysis.get("max_derived_signal_depth", 1)
    results: list[dict[str, Any]] = []
    index: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for edge_id in sorted(edges):
        edge_events = event_by_edge[edge_id]
        direct = direct_by_edge.get(edge_id, [])
        signal_samples = direct if direct else [standalone_event_sample(event) for event in edge_events]
        if direct:
            for sample in direct:
                sample["observation_items"] = [*sample["signals"], *sample["inputs"]]
                sample["terminal_edge"] = edges[edge_id]
            validate_observation_alignment(direct)
        signal_slots = stable_slots(signal_samples, "signal") if signal_samples else []
        all_known_registers = [
            register_id for register_id in register_ids
            if direct and all(register_id in sample["input_register_values"] for sample in direct)
        ]
        if direct:
            grade = "relatively_stable_candidate" if all(sample["region_edge_count"] == 1 for sample in direct) else "hypothetical_candidate"
            assumptions = [] if grade == "relatively_stable_candidate" else ["region_to_edge_decomposition", "last_write_projection"]
            if grade == "relatively_stable_candidate":
                trees = v3_signal_gated_candidates(
                    direct, signal_slots, all_known_registers, minimum, max_numeric_depth, max_derived_depth,
                )
                resolution = None
            else:
                resolution = resolve_hypothetical_candidates(
                    direct, signal_slots, all_known_registers, minimum, max_numeric_depth, max_derived_depth,
                )
                intersection = resolution["intersection_candidates"]
                combine_samples = not intersection
                if combine_samples:
                    trees = v3_signal_gated_candidates(
                        direct, signal_slots, all_known_registers, minimum, max_numeric_depth, max_derived_depth,
                    )
                    combined_status = "combined_sample_fit_selected" if trees else "combined_sample_fit_failed"
                    selected_source = "combined_sample_fit" if trees else "edge_level_error"
                else:
                    trees = [candidate["update_tree"] for candidate in intersection]
                    combined_status = "not_run_intersection_selected"
                    selected_source = "intersection"
                resolution["combined_sample_fit"] = {
                    "triggered": combine_samples,
                    "status": combined_status,
                    "sample_count": len(direct) if combine_samples else 0,
                    "candidates": [
                        candidate_record(tree) for tree in trees
                    ] if combine_samples else [],
                }
                resolution["selection"] = {
                    "source": selected_source,
                    "status": "candidates_selected" if trees else "combined_sample_fit_failed",
                    "candidate_count": len(trees),
                    "error": None if trees else {
                        "code": "combined_sample_fit_failed",
                        "message": "The cycle-local intersection was empty and the combined samples produced no exact candidate.",
                        "scope": "concrete_edge",
                        "continue_to_migration_and_backward_inference": True,
                    },
                }
        else:
            trees = [default_signal_tree(signal_slots)]
            grade = "hypothetical_candidate"
            assumptions = ["minimal_predecessor_default"]
            if signal_slots:
                assumptions.append("unanchored_signal_guard")
            resolution = {
                "strategy": "not_applicable_no_downlink_anchor",
                "cycle_axis": "cycle_id",
                "cycle_candidates": [], "intersection_candidates": [],
                "combined_sample_fit": {
                    "triggered": False, "status": "not_applicable_no_downlink_anchor",
                    "sample_count": 0, "candidates": [],
                },
                "selection": {
                    "source": "minimal_predecessor_default",
                    "status": "not_applicable_no_downlink_anchor",
                    "candidate_count": len(trees),
                    "error": None,
                },
            }
        updates = v3_input_update_summary(edge_events, register_ids)
        output_candidates: list[dict[str, Any]] = []
        for tree in sorted(trees, key=tree_score):
            status = candidate_status(tree)
            paths_for_candidate = guard_path(tree)
            tree_serialized = json.dumps(tree, ensure_ascii=False, sort_keys=True)
            guard_serialized = json.dumps(paths_for_candidate, ensure_ascii=False, sort_keys=True)
            updates_serialized = json.dumps(updates, ensure_ascii=False, sort_keys=True)
            index[(guard_serialized, tree_serialized, updates_serialized, f"{status}:{grade}")].add(edge_id)
            output_candidates.append({
                "guard_path": paths_for_candidate, "update_tree": tree, "update_tree_text": tree_text(tree),
                "observational_status": status, "candidate_grade": grade,
                "preference": candidate_preference(tree),
                "assumptions": assumptions, "support_count": len(direct) if direct else len(edge_events),
                "longest_consecutive_support": longest_consecutive_support(direct) if direct else longest_consecutive_support(signal_samples),
                "complexity": {
                    "configured_signal_depth": len(signal_slots), "unknown_branches": tree_metrics(tree)[0],
                    "derived_value_nodes": tree_metrics(tree)[1], "threshold_nodes": tree_metrics(tree)[2],
                    "leaf_formula_complexity": tree_metrics(tree)[3],
                },
                "input_register_updates": updates,
            })
        results.append({
            "edge": edges[edge_id], "signal_slots": signal_slots,
            "input_register_ids": all_known_registers,
            "direct_regions": direct, "edge_samples": edge_events,
            "candidate_grade": grade, "candidates": output_candidates,
            "hypothetical_candidate_resolution": resolution,
            "structural_candidates": structural_candidates_v3(
                [edges[edge_id]], set(mapping.get("d_states", [])), downlink_paths, numeric_definitions,
            ),
        })
    relatively_stable_summary = relatively_stable_inference(
        results, minimum, max_numeric_depth, max_derived_depth,
        downlink_paths=downlink_paths,
        value_domain=sorted(global_register_value_domain),
        backward_config=(analysis.get("backward_inference") or {}).copy()
        if (analysis.get("backward_inference") or {}).get("enabled") else None,
    )
    return {
        "schema_version": 3, "kind": "experimental-edge-level-register-candidates",
        "limitations": [
            "有限 Mealy 观察等价类不证明 AMF 实现的寄存器数量有限；本结果只建模配置的可观察寄存器基。",
            "下行 KSI 在当前实验中是区域更新后的 r 观测约定，不是源码级因果证明。",
            "多边区域的前序最简更新和最后写入投影均为可被未来后缀反驳的假设。",
            "输入寄存器默认先赋值或保持；信号条件常数更新仅在后续传播反例出现时才可加入。",
        ],
        "signal_definitions": signal_definitions, "numeric_input_definitions": numeric_definitions,
        "inputs": {name: {"path": str(path), "sha256": sha256_file(path)} for name, path in paths.items()},
        "parameters": {
            "repetitions": [start_repeat, end_repeat], "input_register_initialization_repetition": start_repeat,
            "input_register_fitting_start": start_repeat + 1, "min_consecutive_support": minimum,
            "max_numeric_depth": max_numeric_depth, "max_derived_signal_depth": max_derived_depth,
            "global_register_value_domain": sorted(global_register_value_domain),
            "backward_inference": analysis.get("backward_inference"),
        },
        "relatively_stable_inference": relatively_stable_summary,
        "results": results,
        "candidate_index": [
            {"guard_path": json.loads(guard), "update_tree": json.loads(tree), "input_register_updates": json.loads(updates),
             "observational_status": status_grade.split(":", 1)[0], "candidate_grade": status_grade.split(":", 1)[1],
             "edge_ids": sorted(edge_ids)}
            for (guard, tree, updates, status_grade), edge_ids in sorted(index.items())
        ],
    }


def infer(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    if config["schema_version"] == 3:
        return infer_v3(config, config_path)
    inputs = config["inputs"]
    mapping = config["mapping"]
    analysis = config["analysis"]
    paths = {name: resolve(config_path, inputs[name]) for name in ("dot", "trace", "cycle_cover", "sequence_file")}
    cycle_cover = json.loads(paths["cycle_cover"].read_text(encoding="utf-8"))
    indexed_edges = edge_index(paths["dot"])
    trace_groups = read_trace_groups(paths["trace"])
    sequence_lines = read_sequence_lines(paths["sequence_file"])
    start_repeat, end_repeat = analysis.get("repetitions", [2, 10])
    all_regions: list[dict[str, Any]] = []
    all_edges: dict[str, dict[str, Any]] = {}
    downlink_paths = mapping["downlink_ksi_by_output"]
    uplink_paths = mapping["uplink_ksi_by_input"]
    signal_definitions = validate_signal_definitions(mapping)
    for cycle in selected_cycles(cycle_cover, analysis.get("cycle_ids")):
        variants = cycle.get("variants")
        if not isinstance(variants, list):
            raise RegionInferenceError(f"{cycle.get('cycle_id')}: variants must be a list.")
        for variant in variants:
            sequence_id, records = matched_records(trace_groups, sequence_lines, variant, cycle["cycle_id"])
            regions = build_regions(
                cycle, variant, sequence_id, records, indexed_edges, downlink_paths, uplink_paths,
                signal_definitions, start_repeat, end_repeat,
            )
            all_regions.extend(regions)
            for _, _, _, edge in source_events(cycle, variant, records, indexed_edges, end_repeat):
                all_edges[edge["edge_id"]] = edge
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for region in all_regions:
        grouped[region["terminal_edge"]["edge_id"]].append(region)
    results: list[dict[str, Any]] = []
    index: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    minimum = analysis.get("min_consecutive_support", 3)
    max_numeric_depth = analysis.get("max_numeric_depth", analysis.get("max_depth", 1))
    max_derived_depth = analysis.get("max_derived_signal_depth", 1)
    for edge_id in sorted(all_edges):
        regions = grouped.get(edge_id, [])
        validate_observation_alignment(regions)
        signal_slots = stable_slots(regions, "signal")
        input_slots = stable_slots(regions, "input")
        candidates = signal_gated_candidates(
            regions, signal_slots, input_slots, minimum, max_numeric_depth, max_derived_depth,
        ) if regions else []
        output_candidates = []
        for tree in candidates:
            paths_for_candidate = guard_path(tree)
            status = candidate_status(tree)
            tree_serialized = json.dumps(tree, ensure_ascii=False, sort_keys=True)
            guard_serialized = json.dumps(paths_for_candidate, ensure_ascii=False, sort_keys=True)
            index[(guard_serialized, tree_serialized, status)].add(edge_id)
            output_candidates.append({
                "guard_path": paths_for_candidate,
                "update_tree": tree,
                "update_tree_text": tree_text(tree),
                "status": status,
                "support_count": len(regions),
                "longest_consecutive_support": longest_consecutive_support(regions),
                "complexity": {
                    "configured_signal_depth": len(signal_slots),
                    "unknown_branches": tree_metrics(tree)[0],
                    "derived_value_nodes": tree_metrics(tree)[1],
                    "threshold_nodes": tree_metrics(tree)[2],
                    "leaf_formula_complexity": tree_metrics(tree)[3],
                },
            })
        results.append({
            "edge": all_edges[edge_id],
            "signal_slots": signal_slots,
            "input_slots": input_slots,
            "regions": regions,
            "candidates": output_candidates,
            "structural_candidates": structural_candidates(
                [all_edges[edge_id]], set(mapping.get("d_states", [])), downlink_paths, uplink_paths,
            ),
        })
    return {
        "schema_version": 2,
        "kind": "experimental-typed-temporal-register-candidates",
        "limitations": [
            "下行 KSI 字段在当前实验中被当作所属推断区域的更新后寄存器观测。",
            "候选仅对所选训练区域精确，不构成 AMF 内部变量或实现逻辑的源码证明。",
            "阈值节点只是回绕结构的直接模型化，不预设一般模运算语义。",
            "派生值节点只来自反例输入值枚举，不自动赋予整数 7 任何协议语义。",
            "未观察或支持不足的信号分支保持 unknown，不能据已观察分支外推。",
        ],
        "signal_definitions": signal_definitions,
        "inputs": {name: {"path": str(path), "sha256": sha256_file(path)} for name, path in paths.items()},
        "parameters": {
            "repetitions": [start_repeat, end_repeat],
            "min_consecutive_support": minimum,
            "configured_signal_depth": len(signal_definitions),
            "max_numeric_depth": max_numeric_depth,
            "max_derived_signal_depth": max_derived_depth,
        },
        "results": results,
        "candidate_index": [
            {
                "guard_path": json.loads(guard),
                "update_tree": json.loads(tree),
                "status": status,
                "edge_ids": sorted(edge_ids),
            }
            for (guard, tree, status), edge_ids in sorted(index.items())
        ],
    }


def report_state_key(value: str) -> tuple[int, str]:
    digits = "".join(character for character in value if character.isdigit())
    return (int(digits) if digits else -1, value)


def report_code(text: str) -> str:
    return "<code>" + html.escape(text).replace("\n", "<br>") + "</code>"


def compact_report_text(text: str) -> str:
    """Use reader-facing aliases without changing the machine-readable JSON semantics."""
    return (
        text.replace("unknown/unobserved_signal_branch", "unknown")
        .replace("unknown/unanchored_signal_context", "unknown")
        .replace("unknown/insufficient_support", "unknown")
        .replace("r_i[ngksi_uplink]", "r_i")
    )


def compact_report_code(text: str) -> str:
    return report_code(compact_report_text(text))


def report_candidate_grade_label(grade: str) -> str:
    return {
        "relatively_stable_candidate": "相对稳定",
        "hypothetical_candidate": "假设性",
    }.get(grade, grade)


def report_resolution_label(status: str) -> str:
    return {
        "intersection_selected": "采用最简交集",
        "combined_sample_fit_selected": "联合拟合成功",
        "combined_sample_fit_failed": "联合拟合失败",
        "not_applicable_no_downlink_anchor": "无下行锚点",
        "not_applicable_relatively_stable_candidate": "相对稳定推断",
    }.get(status, status)


def report_observation_label(status: str) -> str:
    return {
        "observationally_exact_candidate": "完整观察",
        "partial_observational_candidate": "部分观察",
    }.get(status, status)


def report_scope_label(scope: str) -> str:
    return {
        "global_candidate": "全局候选",
        "intersection_candidate": "最简交集",
        "combined_sample_fit_candidate": "联合拟合候选",
        "partition_candidate": "循环局部候选",
    }.get(scope, scope)


def report_route_kind_label(kind: str) -> str:
    return {
        "base_simple_cover": "简单覆盖",
        "base_fallback": "补充闭合游走",
        "base_standalone_self_loop": "独立自环",
    }.get(kind, kind)

def report_message_pair(edge: dict[str, Any]) -> str:
    return report_code(str(edge["logical_input"])) + "/<br>" + report_code(str(edge["logical_output"]))


def report_input_sequence(inputs: Iterable[Any]) -> str:
    return " →<br>".join(report_code(str(item)) for item in inputs)


def report_edge_identity(edge: dict[str, Any]) -> str:
    return "<br>".join((
        report_code(str(edge["edge_id"])),
        report_code(f"{edge['source_state']} → {edge['target_state']}"),
        report_message_pair(edge),
    ))


def report_candidate_list(candidates: list[dict[str, Any]], *, include_grade: bool = True) -> str:
    if not candidates:
        return "无"
    rendered: list[str] = []
    for candidate in candidates:
        prefix: list[str] = []
        if include_grade and candidate.get("candidate_grade"):
            prefix.append(report_candidate_grade_label(str(candidate["candidate_grade"])))
        text = compact_report_code(str(candidate.get("update_tree_text", "<missing update_tree_text>")))
        rendered.append(("；".join(prefix) + "：" if prefix else "") + text)
    return "<br><br>".join(rendered)


def report_input_updates(updates: list[dict[str, Any]]) -> str:
    if not updates:
        return "无"
    rendered: list[str] = []
    seen: set[str] = set()
    for item in updates:
        text = compact_report_text(str(item.get("update", {}).get("text", "<missing update>")))
        if text not in seen:
            seen.add(text)
            rendered.append(compact_report_code(text))
    return "<br>".join(rendered)


def report_all_input_updates(samples: list[dict[str, Any]]) -> str:
    return report_input_updates([
        update
        for sample in samples
        for update in sample.get("input_register_updates", [])
    ])


def report_resolution(result: dict[str, Any]) -> tuple[str, str, str]:
    resolution = result.get("hypothetical_candidate_resolution")
    if resolution is None:
        return "not_applicable_relatively_stable_candidate", "不适用", "不适用"
    intersection = resolution.get("intersection_candidates", [])
    if intersection:
        intersection_text = report_candidate_list(intersection, include_grade=False)
    else:
        intersection_text = "空"
    combined = resolution.get("combined_sample_fit", {})
    combined_candidates = combined.get("candidates", [])
    combined_text = report_candidate_list(combined_candidates, include_grade=False) if combined.get("triggered") else "未执行"
    selection = resolution.get("selection", {})
    if selection.get("status") == "combined_sample_fit_failed":
        status = "combined_sample_fit_failed"
    elif selection.get("source") == "intersection":
        status = "intersection_selected"
    elif selection.get("source") == "combined_sample_fit":
        status = "combined_sample_fit_selected"
    else:
        status = str(selection.get("status", "not_applicable_no_downlink_anchor"))
    return status, intersection_text, combined_text


def report_table(headers: list[str], widths: list[str], rows: list[list[str]], *, table_id: str) -> str:
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    def table_cell(cell: str) -> str:
        return cell.replace("<br>", "<br>\n        ")

    body = "\n".join(
        "    <tr>\n" + "\n".join(f"      <td>{table_cell(cell)}</td>" for cell in row) + "\n    </tr>"
        for row in rows
    )
    columns = "".join(f"    <col style=\"width:{width}\">\n" for width in widths)
    return "\n".join((
        f"<table id=\"{html.escape(table_id)}\" style=\"width:100%; table-layout:fixed\">",
        "  <colgroup>", columns.rstrip(), "  </colgroup>",
        f"  <thead><tr>{header_html}</tr></thead>",
        "  <tbody>", body, "  </tbody>", "</table>",
    ))


def report_cycle_catalog(
    cycles: list[dict[str, Any]],
    usage_by_cycle: dict[str, dict[str, dict[str, set[Any]]]],
) -> tuple[str, dict[tuple[str, int], str]]:
    variant_ids: dict[tuple[str, int], str] = {}
    rows: list[list[str]] = []
    for cycle in cycles:
        cycle_id = str(cycle["cycle_id"])
        variants = cycle["variants"]
        rendered_variants: list[str] = []
        for ordinal, variant in enumerate(variants, start=1):
            variant_id = f"V{ordinal:02d}"
            line_number = variant["line_number"]
            variant_ids[(cycle_id, line_number)] = variant_id
            edges = sorted(
                {
                    edge_id for edge_id, item in usage_by_cycle.get(cycle_id, {}).items()
                    if line_number in item["sequence_lines"]
                },
                key=report_state_key,
            )
            rendered_variants.append(
                report_code(variant_id) + "：<br>" + report_input_sequence(variant["loop_inputs"])
                + "<br>具体边：" + html.escape("、".join(edges) or "无")
            )
        rows.append([
            report_code(cycle_id), html.escape(str(cycle.get("cycle_kind", cycle.get("route_kind", "unknown")))),
            "完整闭环 × " + html.escape(str(cycle.get("repeat_count", "?"))),
            "<br><br>".join(rendered_variants),
        ])
    return report_table(
        ["循环", "类型", "重复结构", "expand 变体（不含 .seq 行号）"],
        ["12%", "18%", "16%", "54%"], rows, table_id="cycle-catalog",
    ), variant_ids


def report_cycle_usage_index(
    results: list[dict[str, Any]],
    variant_ids: dict[tuple[str, int], str],
) -> dict[str, dict[str, dict[str, set[Any]]]]:
    usage: dict[str, dict[str, dict[str, set[Any]]]] = defaultdict(
        lambda: defaultdict(lambda: {"variants": set(), "sequence_lines": set(), "loop_edge_indexes": set()})
    )
    for result in results:
        edge_id = str(result["edge"]["edge_id"])
        for sample in result["edge_samples"]:
            cycle_id = str(sample["cycle_id"])
            line_number = sample["sequence_line"]
            key = (cycle_id, line_number)
            if key not in variant_ids:
                raise RegionInferenceError(f"Report coverage: {cycle_id} has an unknown selected variant.")
            item = usage[cycle_id][edge_id]
            item["variants"].add(variant_ids[key])
            item["sequence_lines"].add(line_number)
            item["loop_edge_indexes"].add(sample.get("loop_edge_index"))
    return usage


def report_partition_for_cycle(result: dict[str, Any], cycle_id: str) -> list[dict[str, Any]]:
    resolution = result.get("hypothetical_candidate_resolution") or {}
    for partition in resolution.get("cycle_candidates", []):
        if partition.get("cycle_id") == cycle_id:
            return partition.get("candidates", [])
    return []


def report_file_link(path: Path, report_path: Path | None) -> str:
    del report_path
    return report_code(path.name)


def report_candidate_grades(candidates: list[dict[str, Any]]) -> str:
    grades = sorted({str(candidate["candidate_grade"]) for candidate in candidates if candidate.get("candidate_grade")})
    return "<br>".join(report_candidate_grade_label(grade) for grade in grades) if grades else "—"


def text_candidate_list(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "无"

    def expression(tree: dict[str, Any]) -> str:
        kind = tree["kind"]
        if kind == "unknown":
            return "unknown"
        if kind == "leaf":
            return compact_report_text(formula_text(tree["formula"])).removeprefix("r' = ")
        guard = tree["guard"]
        if kind == "signal_guard":
            condition = f"s{guard['slot']}=1"
        elif guard["variable"] == "r":
            operator = "<" if kind == "threshold_guard" else "="
            limit = guard.get("threshold", guard.get("value"))
            condition = f"r{operator}{limit}"
        else:
            operator = "<" if kind == "threshold_guard" else "="
            limit = guard.get("threshold", guard.get("value"))
            condition = f"r_i{operator}{limit}"
        return f"ite({condition}, {expression(tree['true'])}, {expression(tree['false'])})"

    rendered = ["r' = " + expression(candidate["update_tree"]) for candidate in candidates]
    if len(rendered) == 1:
        return rendered[0]
    numerals = "①②③④⑤⑥⑦⑧⑨⑩"
    return " ｜ ".join(
        f"{numerals[index] if index < len(numerals) else str(index + 1) + '.'} {text}"
        for index, text in enumerate(rendered)
    )


def text_input_updates(updates: list[dict[str, Any]]) -> str:
    if not updates:
        return "无"
    rendered: list[str] = []
    seen: set[str] = set()
    for item in updates:
        text = compact_report_text(str(item.get("update", {}).get("text", "<missing update>")))
        if text not in seen:
            seen.add(text)
            rendered.append(text)
    return "\n".join(rendered)


def text_all_input_updates(samples: list[dict[str, Any]]) -> str:
    return text_input_updates([
        update
        for sample in samples
        for update in sample.get("input_register_updates", [])
    ])


def backward_attempts_for_cycle(item: dict[str, Any], cycle_id: str) -> list[dict[str, Any]]:
    return [
        attempt for attempt in (item.get("backward_inference") or {}).get("attempts", [])
        if str(attempt.get("cycle_id")) == cycle_id
    ]


def backward_branch_candidate_text(attempts: list[dict[str, Any]]) -> str:
    candidates = [
        {"update_tree": candidate["branch_update_tree"]}
        for attempt in attempts for candidate in attempt.get("candidates", [])
    ]
    return text_candidate_list(candidates) if candidates else "无"


def signal_context_text(contexts: list[list[dict[str, Any]]]) -> str:
    rendered = []
    for context in contexts:
        content = ", ".join(
            f"{item['signal_id']}={item['value']}"
            for item in context
        )
        rendered.append("{" + content + "}")
    return " ｜ ".join(dict.fromkeys(rendered)) or "{}"


def numeric_input_context_text(items: list[dict[str, Any]]) -> str:
    content = ", ".join(
        f"{item.get('input_register_id', item.get('field_path'))}={item.get('value')}"
        for item in items
    )
    return "[" + content + "]"


def candidate_grade(candidate: dict[str, Any], item: dict[str, Any], scope: str) -> str:
    grade = candidate.get("candidate_grade")
    if grade:
        return str(grade)
    if scope in {"intersection_candidate", "combined_sample_fit_candidate", "partition_candidate"}:
        return "hypothetical_candidate"
    matching = {
        str(existing.get("candidate_grade"))
        for existing in item.get("candidates", [])
        if existing.get("update_tree_text") == candidate.get("update_tree_text") and existing.get("candidate_grade")
    }
    return "\n".join(sorted(matching)) if matching else "无候选类型"


def candidate_complexity_text(candidate: dict[str, Any]) -> str:
    complexity = candidate.get("complexity", {})
    if not complexity:
        return "无"
    labels = {
        "configured_signal_depth": "信号层",
        "derived_value_nodes": "派生值节点",
        "leaf_formula_complexity": "叶公式复杂度",
        "threshold_nodes": "阈值节点",
        "unknown_branches": "未知分支",
    }
    return "；".join(f"{labels.get(key, key)}={value}" for key, value in sorted(complexity.items()))


def report_audit_context(
    result: dict[str, Any], config: dict[str, Any], config_path: Path,
) -> dict[str, Any]:
    cycle_cover_path = resolve(config_path, config["inputs"]["cycle_cover"])
    cycle_cover = json.loads(cycle_cover_path.read_text(encoding="utf-8"))
    cycles = selected_cycles(cycle_cover, config["analysis"].get("cycle_ids"))
    variant_ids = {
        (str(cycle["cycle_id"]), variant["line_number"]): f"V{ordinal:02d}"
        for cycle in cycles
        for ordinal, variant in enumerate(cycle["variants"], start=1)
    }
    usage_by_cycle = report_cycle_usage_index(result["results"], variant_ids)
    by_edge = {str(item["edge"]["edge_id"]): item for item in result["results"]}
    integrity = {
        "edge_group_count": len(by_edge),
        "cycle_count": len(cycles),
        "cycle_variant_count": len(variant_ids),
        "cycle_edge_usage_count": sum(len(edges) for edges in usage_by_cycle.values()),
    }
    return {
        "cycles": cycles,
        "variant_ids": variant_ids,
        "usage_by_cycle": usage_by_cycle,
        "by_edge": by_edge,
        "integrity": integrity,
    }


def render_v3_report(
    result: dict[str, Any], config: dict[str, Any], config_path: Path,
    report_path: Path | None = None, output_path: Path | None = None,
    workbook_path: Path | None = None,
) -> tuple[str, dict[str, int]]:
    context = report_audit_context(result, config, config_path)
    by_edge = context["by_edge"]
    summary_rows: list[list[str]] = []
    for edge_id in sorted(by_edge, key=report_state_key):
        item = by_edge[edge_id]
        resolution_status, intersection, combined = report_resolution(item)
        cycle_ids = sorted({sample["cycle_id"] for sample in item["edge_samples"]}, key=report_state_key)
        resolution_summary = report_resolution_label(resolution_status)
        if resolution_status == "combined_sample_fit_failed":
            resolution_summary += "；本边没有前向候选，但仍保留迁移检验与前序反推资格"
        candidate_cell = report_candidate_list(item["candidates"], include_grade=False)
        backward_attempts = (item.get("backward_inference") or {}).get("attempts", [])
        if backward_attempts:
            backward_lines = []
            for attempt in backward_attempts:
                branch_candidates = [
                    {
                        "update_tree": candidate["branch_update_tree"],
                        "update_tree_text": candidate["branch_update_text"],
                    }
                    for candidate in attempt.get("candidates", [])
                ]
                backward_lines.append(
                    report_code(str(attempt["cycle_id"])) + " 反推 "
                    + report_code(signal_context_text([attempt.get("signal_context", [])]))
                    + "：<br>" + report_candidate_list(branch_candidates, include_grade=False)
                )
            candidate_cell += "<br><br>前序反推分支：<br>" + "<br>".join(backward_lines)
        summary_rows.append([
            "<br>".join(report_code(cycle_id) for cycle_id in cycle_ids) + " " + report_edge_identity(item["edge"]),
            candidate_cell,
            report_all_input_updates(item["edge_samples"]),
            report_candidate_grade_label(str(item.get("candidate_grade", "无候选类型"))) + "<br>" + resolution_summary,
        ])

    input_lines = [
        "- " + report_file_link(Path(item["path"]), report_path) + "："
        + report_code(item["sha256"])
        for _, item in sorted(result["inputs"].items())
    ]
    input_lines.append(
        "- " + report_file_link(config_path, report_path) + "：" + report_code(sha256_file(config_path))
    )
    if output_path is not None:
        input_lines.append("- " + report_file_link(output_path, report_path) + "：同次命令生成的机器可读候选 JSON")
    relatively_stable = result.get("relatively_stable_inference", {})
    migration_counts = relatively_stable.get("migration_status_counts", {})
    lines = [
        "# ngKSI 边级寄存器候选推断摘要", "",
        "## 范围与读取规则", "",
        "本报告由 schema v3 推断器直接生成。循环以 `cycle_id` 为主键；变体 `Vxx` 只描述",
        "`expand` 产生的逻辑输入差异，不把物理 `.seq` 行号作为报告主键。每个具体 DOT 边均按",
        "`src → dst` 与 `input / output` 列出；同一边在不同循环中的使用不会合并删除。", "",
        "候选是可被后续行为反驳的观察候选，不是 AMF 源码变量或源码级更新时点。观察区域统一写为",
        "`(r_before, ordered_observation_items, r_after)`；信号量使用 `{signal=value}`，数值输入使用",
        "`[input=value]`。假设性候选先取循环局部最简树交集；交集为空时执行联合拟合。联合拟合无候选",
        "只记录该具体边的生成失败，仍保留相对稳定推断迁移检验和前序反推。", "",
        "## 输入与参数", "",
        "- 拟合轮次：`R%d–R%d`；输入寄存器初始化：`R%d`；拟合起点：`R%d`。" % (
            result["parameters"]["repetitions"][0], result["parameters"]["repetitions"][1],
            result["parameters"].get("input_register_initialization_repetition", result["parameters"]["repetitions"][0]),
            result["parameters"].get("input_register_fitting_start", result["parameters"]["repetitions"][0]),
        ),
        *input_lines,
        "- JSON、YAML、完整原始 trace 与环导出均由同次命令记录；不使用 cleaned trace。", "",
        "## 重点结果", "",
        "下表按 H13 固定四列格式整理全部具体 DOT 边组。全局候选保留并列公式；完整树、循环局部候选、",
        "交集或联合拟合、相对稳定推断迁移和前序反推均保留在 JSON。" if workbook_path is None else
        "循环—边使用、循环局部候选、交集或联合拟合、相对稳定推断迁移和前序反推请阅读同次生成的 Excel；完整树与全部证据保留在 JSON。", "",
        report_table(
            ["循环、边与节点", "边级候选", "输入寄存器", "候选等级"],
            ["27%", "32%", "16%", "25%"], summary_rows, table_id="edge-summary",
        ), "",
        "## 相对稳定推断迁移检验", "",
        "工具按相同有效信号上下文、`input` 和 `output` 合并单边区域，形成带信号门控的相对稳定推断，",
        "再把目标映射观察区域的 `r_before`、有效 `r_i` 和信号量直接代入模型树。", "",
        "- 相对稳定推断组数：%d；假设性目标边数：%d；目标循环分区数：%d。" % (
            len(relatively_stable.get("groups", [])), relatively_stable.get("target_edge_count", 0),
            relatively_stable.get("target_partition_count", 0),
        ),
        "- 迁移成功：%d；无匹配相对稳定推断：%d；迁移失败并尝试前序反推：%d。" % (
            migration_counts.get("migration_succeeded", 0),
            migration_counts.get("no_matching_relatively_stable_inference", 0),
            migration_counts.get("migration_failed", 0),
        ),
        "- 从相对稳定推断的 `derived_value_guard: r_i==T` 动态提取后续排序偏好：`%s`；"
        "后续精确 `r'=T` 最高，含同一派生值分裂的候选其次，其余按原复杂度排序。" % (
            ", ".join(str(value) for value in relatively_stable.get("dynamic_t_preference", {}).get("values", [])) or "无",
        ),
        "- 迁移失败后只反推终止观察边之前最近的无 KSI 下行边；更早无下行边逐条保留 `r'=r` 假设。", "",
        "## 详细审计工作簿", "",
        "Excel 只保留“循环边使用”工作表；每行同时展示本循环候选、最简交集或联合拟合、候选生成结果、",
        "相对稳定推断、迁移结果、首个反例、反推状态与反推候选假设。并列候选采用横向编号排列。",
        ("- 工作簿：" + report_file_link(workbook_path, report_path)) if workbook_path is not None
        else "- 本次未请求 Excel 审计工作簿；如需生成，显式提供 `--workbook <path>`。",
        "",
    ]
    integrity = context["integrity"]
    lines.extend([
        "## 完整性与可读性复核", "",
        "- 边组数：%d；循环数：%d；变体数：%d；循环—边使用数：%d。" % (
            integrity["edge_group_count"], integrity["cycle_count"], integrity["cycle_variant_count"], integrity["cycle_edge_usage_count"],
        ),
        "- 已断言：全部边组进入本摘要表；完整候选树与证据保留在 JSON。"
        + ("全部循环—边使用进入 Excel。" if workbook_path is not None else "本次没有请求 Excel。"),
        "- 可读性：摘要表使用固定布局 HTML、`colgroup` 固定列宽；消息对在 `/` 后换行，状态边与",
        "  公式独立换行。" + ("工作簿仅一张表，全部单元格居中，A-I 使用紧凑列宽；并列公式横向排列。" if workbook_path is not None else ""),
    ])
    report = "\n".join(lines) + "\n"
    validate_v3_report(report, result, integrity)
    return report, integrity


def validate_v3_report(
    report: str,
    result: dict[str, Any],
    integrity: dict[str, int],
) -> None:
    searchable = report.replace("<br>\n        ", "<br>")
    if report.count('style="width:100%; table-layout:fixed"') != 1 or "<colgroup>" not in report:
        raise RegionInferenceError("Report validation: H13-style fixed-layout summary table is incomplete.")
    if "/<br>" not in report:
        raise RegionInferenceError("Report validation: logical message pairs are missing explicit line breaks.")
    if integrity["edge_group_count"] != len(result["results"]):
        raise RegionInferenceError("Report validation: not every edge group is represented.")
    for item in result["results"]:
        edge_id = str(item["edge"]["edge_id"])
        if searchable.count(report_code(edge_id)) < 1:
            raise RegionInferenceError(f"Report validation: missing summary edge {edge_id}.")
        for candidate in item["candidates"]:
            if compact_report_code(str(candidate["update_tree_text"])) not in searchable:
                raise RegionInferenceError(f"Report validation: global candidate lost for {edge_id}.")


WORKBOOK_SHEET_NAMES = ("循环边使用",)


def workbook_sheet(name: str, headers: list[str], rows: list[list[str]], widths: list[int], candidate_type_column: int | None = None) -> dict[str, Any]:
    if len(headers) != len(widths):
        raise RegionInferenceError(f"Workbook payload: {name} header/width mismatch.")
    if any(len(row) != len(headers) for row in rows):
        raise RegionInferenceError(f"Workbook payload: {name} row width mismatch.")
    return {
        "name": name,
        "headers": headers,
        "rows": rows,
        "widths": widths,
        "candidateTypeColumn": candidate_type_column,
    }


def build_v3_workbook_payload(
    result: dict[str, Any], config: dict[str, Any], config_path: Path,
) -> tuple[dict[str, Any], dict[str, int]]:
    context = report_audit_context(result, config, config_path)
    by_edge: dict[str, dict[str, Any]] = context["by_edge"]
    cycles: list[dict[str, Any]] = context["cycles"]
    usage_by_cycle: dict[str, dict[str, dict[str, set[Any]]]] = context["usage_by_cycle"]
    variant_ids: dict[tuple[str, int], str] = context["variant_ids"]
    cycle_usage_rows: list[list[str]] = []

    for cycle in sorted(cycles, key=lambda item: report_state_key(str(item["cycle_id"]))):
        cycle_id = str(cycle["cycle_id"])
        for edge_id in sorted(usage_by_cycle.get(cycle_id, {}), key=report_state_key):
            item = by_edge[edge_id]
            edge = item["edge"]
            usage = usage_by_cycle[cycle_id][edge_id]
            local = report_partition_for_cycle(item, cycle_id)
            resolution = item.get("hypothetical_candidate_resolution") or {}
            resolution_status, intersection_text, combined_text = report_resolution(item)
            local_candidates = local or item["candidates"]
            if resolution_status == "intersection_selected":
                selected_resolution = text_candidate_list(resolution.get("intersection_candidates", []))
            elif resolution_status == "combined_sample_fit_selected":
                selected_resolution = text_candidate_list(resolution.get("combined_sample_fit", {}).get("candidates", []))
            elif resolution_status == "combined_sample_fit_failed":
                selected_resolution = "联合拟合失败（0 个候选）"
            else:
                selected_resolution = "不适用"
            migration = item.get("relatively_stable_inference_migration") or {}
            migration_result = next(
                (entry for entry in migration.get("cycle_results", []) if str(entry["cycle_id"]) == cycle_id),
                None,
            )
            if migration_result is None:
                migration_status = "不适用"
                migration_evidence = "本边没有同一 input/output 的相对稳定推断迁移任务。"
                inference_sources = "不适用"
                inference_candidates = "不适用"
            elif migration_result["status"] == "migration_succeeded":
                migration_status = "迁移成功"
                migration_evidence = (
                    f"{migration_result['sample_count']}/{migration_result['sample_count']} 个样本成立；"
                    f"信号上下文 {signal_context_text([migration_result['signal_context']])}。"
                )
                inference_sources = "、".join(migration_result.get("source_edge_ids", [])) or "不适用"
                inference_candidates = text_candidate_list(migration_result.get("relatively_stable_candidates", []))
            elif migration_result["status"] == "migration_failed":
                migration_status = "迁移失败，执行前序反推"
                failures = migration_result.get("failing_candidates", [])
                first_failure = failures[0] if failures else None
                first = first_failure["counterexamples"][0] if first_failure and first_failure.get("counterexamples") else None
                if first_failure and first:
                    variant = variant_ids.get((cycle_id, first["sequence_line"]), f"line {first['sequence_line']}")
                    register_values = "、".join(
                        f"r_i[{register_id}]={value}"
                        for register_id, value in first["input_register_values"].items()
                    ) or "无 r_i"
                    migration_evidence = (
                        f"{first_failure['mismatch_count']}/{migration_result['sample_count']} 个样本不匹配；"
                        f"首例 {variant} R{first['repetition']}：r_before={first['r_before']}，{register_values}，"
                        f"{signal_context_text([first['signal_context']])}，"
                        f"预测 r_after={first['predicted_r_after']}，观察 r_after={first['observed_r_after']}。"
                        "该差值进入最近前序无下行边反推。"
                    )
                else:
                    migration_evidence = "相对稳定推断未覆盖本循环样本，已进入最近前序无下行边反推。"
                inference_sources = "、".join(migration_result.get("source_edge_ids", [])) or "不适用"
                inference_candidates = text_candidate_list(migration_result.get("relatively_stable_candidates", []))
            else:
                migration_status = "无匹配相对稳定推断"
                migration_evidence = (
                    f"目标信号上下文 {signal_context_text([migration_result.get('signal_context', [])])}；"
                    "没有相同 signal/input/output 的相对稳定推断，不执行前序反推。"
                )
                inference_sources = "不适用"
                inference_candidates = "不适用"
            backward_attempts = backward_attempts_for_cycle(item, cycle_id)
            if backward_attempts:
                backward_candidates = [
                    candidate
                    for attempt in backward_attempts for candidate in attempt.get("candidates", [])
                ]
                backward_status = f"已生成 {len(backward_candidates)} 个候选" if backward_candidates else "无精确候选"
                targets = "、".join(sorted({str(attempt["target_edge"]["edge_id"]) for attempt in backward_attempts}))
                allowed_sets = sorted({
                    "{" + ",".join(str(value) for value in sample["allowed_r_after_values"]) + "}"
                    for attempt in backward_attempts for sample in attempt.get("samples", [])
                })
                held = sorted({
                    str(held_item["edge"]["edge_id"])
                    for attempt in backward_attempts for held_item in attempt.get("held_predecessors", [])
                })
                backward_evidence = (
                    f"由 {targets} 的相对稳定推断迁移失败反推；"
                    f"本边信号上下文：{signal_context_text([backward_attempts[0].get('signal_context', [])])}；"
                    f"数值输入：{numeric_input_context_text(backward_attempts[0].get('numeric_inputs', []))}；"
                    f"允许输出：{'、'.join(allowed_sets) or '无'}；"
                    f"{backward_branch_candidate_text(backward_attempts)}；"
                    f"更早边保持假设：{('、'.join(held) + " r'=r") if held else '无'}。"
                )
            elif migration_result and migration_result.get("backward_inference", {}).get("attempts"):
                target_attempts = migration_result["backward_inference"]["attempts"]
                inferred_edges = "、".join(sorted({str(attempt["inferred_edge"]["edge_id"]) for attempt in target_attempts}))
                backward_status = "已触发前序反推"
                backward_evidence = f"本边迁移失败；完整反推候选记录在前序边 {inferred_edges} 的对应循环行。"
            else:
                backward_status = "不适用"
                backward_evidence = "本循环—边没有由相对稳定推断迁移失败触发的前序反推。"
            cycle_usage_rows.append([
                cycle_id,
                report_route_kind_label(str(cycle.get("cycle_kind", cycle.get("route_kind", "unknown")))),
                "\n".join(sorted(usage["variants"])),
                edge_id,
                str(edge["source_state"]),
                str(edge["target_state"]),
                str(edge["logical_input"]),
                str(edge["logical_output"]),
                report_candidate_grade_label(str(item.get("candidate_grade", "无候选类型"))),
                text_candidate_list(local_candidates),
                selected_resolution,
                report_resolution_label(resolution_status),
                inference_sources,
                inference_candidates,
                migration_status,
                compact_report_text(migration_evidence),
                backward_status,
                compact_report_text(backward_evidence),
            ])

    sheets = [
        workbook_sheet(
            "循环边使用",
            [
                "循环", "路线类型", "适用变体", "EID", "src", "dst", "input", "output", "候选类型",
                "本循环候选", "最简交集或联合拟合", "候选生成结果", "相对稳定推断来源边", "相对稳定推断", "迁移检验", "迁移证据与解释",
                "反推状态", "反推候选与假设",
            ],
            cycle_usage_rows,
            [10, 14, 12, 10, 8, 8, 18, 20, 12, 68, 54, 18, 18, 68, 28, 72, 18, 78],
            8,
        ),
    ]
    payload = {"schema": "register-inference-workbook-v1", "sheets": sheets}
    sheet_rows = {sheet["name"]: len(sheet["rows"]) for sheet in sheets}
    validate_v3_workbook_payload(payload, context, result)
    return payload, sheet_rows


def validate_v3_workbook_payload(payload: dict[str, Any], context: dict[str, Any], result: dict[str, Any]) -> None:
    sheets = {sheet["name"]: sheet for sheet in payload.get("sheets", [])}
    if tuple(sheets) != WORKBOOK_SHEET_NAMES:
        raise RegionInferenceError("Workbook validation: the cycle-edge-use sheet is missing or another sheet was added.")
    sheet = sheets["循环边使用"]
    if len(sheet["rows"]) != context["integrity"]["cycle_edge_usage_count"]:
        raise RegionInferenceError("Workbook validation: cycle-edge coverage drifted.")
    if "环内序号" in sheet["headers"]:
        raise RegionInferenceError("Workbook validation: the removed loop-edge ordinal column returned.")
    if "候选类型" not in sheet["headers"]:
        raise RegionInferenceError("Workbook validation: candidate grade needs a dedicated column.")
    expected_uses = {
        (cycle_id, edge_id)
        for cycle_id, edges in context["usage_by_cycle"].items()
        for edge_id in edges
    }
    actual_uses = {(row[0], row[3]) for row in sheet["rows"]}
    if actual_uses != expected_uses:
        raise RegionInferenceError("Workbook validation: one or more cycle-edge uses are missing.")
    by_edge = {str(item["edge"]["edge_id"]): item for item in result["results"]}
    if "E0073" in by_edge:
        e0073_rows = {row[0]: row for row in sheet["rows"] if row[3] == "E0073"}
        if e0073_rows.get("S008", [None] * 15)[14] != "迁移失败，执行前序反推":
            raise RegionInferenceError("Workbook validation: E0073/S008 migration failure is missing.")
        if e0073_rows.get("S036", [None] * 15)[14] != "迁移成功":
            raise RegionInferenceError("Workbook validation: E0073/S036 relatively stable migration is missing.")
        e0002_s008 = next(
            (row for row in sheet["rows"] if row[0] == "S008" and row[3] == "E0002"), None,
        )
        if e0002_s008 is None or e0002_s008[16] != "已生成 6 个候选":
            raise RegionInferenceError("Workbook validation: E0002/S008 backward candidates are missing.")


def render_v3_workbook(
    payload: dict[str, Any], workbook: Path, *, node: str, node_modules: str, preview_dir: Path | None = None,
) -> dict[str, Any]:
    renderer = Path(__file__).with_name("render_register_inference_workbook.mjs")
    if not renderer.exists():
        raise RegionInferenceError(f"Workbook renderer is missing: {renderer}")
    if not node_modules:
        raise RegionInferenceError(
            "Workbook rendering requires artifact-tool node_modules. Set --workbook-node-modules or REGISTER_INFERENCE_NODE_MODULES."
        )
    with tempfile.TemporaryDirectory(prefix="register-inference-workbook-") as temporary:
        payload_path = Path(temporary) / "workbook-payload.json"
        payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        command = [node, str(renderer), "--payload", str(payload_path), "--output", str(workbook), "--node-modules", node_modules]
        if preview_dir is not None:
            command.extend(["--preview-dir", str(preview_dir)])
        completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown node renderer failure"
        raise RegionInferenceError(f"Workbook renderer failed: {message}")
    try:
        metadata = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RegionInferenceError(f"Workbook renderer emitted no JSON metadata: {completed.stdout!r}") from exc
    if not workbook.exists():
        raise RegionInferenceError("Workbook renderer reported success without creating the XLSX file.")
    ensure_workbook_frozen_headers(workbook)
    cleanup_workbook_intermediates(workbook)
    metadata["sha256"] = sha256_file(workbook)
    return metadata


def ensure_workbook_frozen_headers(workbook: Path) -> None:
    """Repair freeze panes and table filters without altering cell content."""
    with zipfile.ZipFile(workbook, "r") as source:
        members = {member.filename: source.read(member.filename) for member in source.infolist()}
    sheet_names = sorted(name for name in members if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
    if not sheet_names:
        raise RegionInferenceError("Workbook freeze repair: no worksheet XML members found.")
    changed = False
    for sheet_name in sheet_names:
        text = members[sheet_name].decode("utf-8")
        if '<x:pane ' in text:
            continue
        marker = '<x:sheetView showGridLines="0" workbookViewId="0" />'
        replacement = (
            '<x:sheetView showGridLines="0" workbookViewId="0">'
            '<x:pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen" />'
            '</x:sheetView>'
        )
        if marker not in text:
            raise RegionInferenceError(f"Workbook freeze repair: unsupported sheet view in {sheet_name}.")
        members[sheet_name] = text.replace(marker, replacement, 1).encode("utf-8")
        changed = True
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ET.register_namespace("x", namespace)
    table_names = sorted(name for name in members if name.startswith("xl/tables/table") and name.endswith(".xml"))
    for table_name in table_names:
        root = ET.fromstring(members[table_name])
        if root.find(f"{{{namespace}}}autoFilter") is not None:
            continue
        table_range = root.get("ref")
        columns = root.find(f"{{{namespace}}}tableColumns")
        if not table_range or columns is None:
            raise RegionInferenceError(f"Workbook filter repair: unsupported table XML in {table_name}.")
        auto_filter = ET.Element(f"{{{namespace}}}autoFilter", {"ref": table_range})
        root.insert(list(root).index(columns), auto_filter)
        members[table_name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        changed = True
    if not changed:
        return
    with tempfile.NamedTemporaryFile(prefix="register-inference-freeze-", suffix=".xlsx", delete=False, dir=workbook.parent) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for name, data in members.items():
                target.writestr(name, data)
        os.replace(temporary_path, workbook)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def cleanup_workbook_intermediates(workbook: Path) -> None:
    """Remove the artifact-tool inspection sidecar; it is never a record deliverable."""
    workbook.with_name(workbook.name + ".inspect.ndjson").unlink(missing_ok=True)


def publish_workbook_delivery(workbook: Path, delivery: Path) -> str:
    """Expose the generated workbook through a short same-volume hard-link for Excel."""
    if workbook.resolve() == delivery.resolve():
        return sha256_file(workbook)
    delivery.parent.mkdir(parents=True, exist_ok=True)
    if delivery.exists():
        if os.path.samefile(workbook, delivery):
            return sha256_file(delivery)
        # ``--workbook-delivery`` is an explicit generated-output destination.
        # A renderer may atomically replace the record copy, leaving the older
        # delivery hard link behind; refresh that named delivery in either case.
        delivery.unlink()
    try:
        os.link(workbook, delivery)
    except OSError as exc:
        raise RegionInferenceError(
            f"Workbook delivery requires a same-volume hard link and could not be created: {delivery} ({exc})"
        ) from exc
    delivery_hash = sha256_file(delivery)
    if delivery_hash != sha256_file(workbook):
        raise RegionInferenceError("Workbook delivery SHA-256 mismatch.")
    return delivery_hash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="YAML region-inference configuration")
    parser.add_argument("--output", required=True, help="JSON output path")
    parser.add_argument("--report", required=True, help="Required H13-style Markdown summary path for schema v3")
    parser.add_argument("--workbook", default=None, help="Optional Excel audit workbook path; generate only when explicitly requested")
    parser.add_argument(
        "--workbook-node", default=os.environ.get("REGISTER_INFERENCE_NODE", "node"),
        help="Node executable for the artifact-tool workbook renderer (default: REGISTER_INFERENCE_NODE or node)",
    )
    parser.add_argument(
        "--workbook-node-modules", default=os.environ.get("REGISTER_INFERENCE_NODE_MODULES"),
        help="Node module directory containing @oai/artifact-tool (or REGISTER_INFERENCE_NODE_MODULES)",
    )
    parser.add_argument(
        "--workbook-preview-dir", default=None,
        help="Optional directory for artifact-tool PNG previews of every workbook sheet",
    )
    parser.add_argument(
        "--workbook-delivery", default=None,
        help="Optional short same-volume hard-link path for opening the workbook in Excel",
    )
    args = parser.parse_args(argv)
    try:
        config_path = Path(args.config).resolve()
        config = load_config(config_path)
        if config["schema_version"] != 3:
            raise RegionInferenceError("The required complete Markdown report is currently defined for schema_version 3 only.")
        result = infer(config, config_path)
        output = Path(args.output).resolve()
        report = Path(args.report).resolve()
        if not args.workbook and (args.workbook_preview_dir or args.workbook_delivery):
            raise RegionInferenceError("--workbook-preview-dir and --workbook-delivery require --workbook.")
        workbook = Path(args.workbook).resolve() if args.workbook else None
        report_text, integrity = render_v3_report(result, config, config_path, report, output, workbook)
        workbook_payload, expected_sheet_rows = (
            build_v3_workbook_payload(result, config, config_path) if workbook is not None else (None, None)
        )
    except (RegionInferenceError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"region-inference error: {exc}", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(report_text, encoding="utf-8")
    workbook_metadata = None
    delivery_path = None
    delivery_sha256 = None
    if workbook is not None:
        try:
            workbook_metadata = render_v3_workbook(
                workbook_payload, workbook,
                node=args.workbook_node,
                node_modules=args.workbook_node_modules,
                preview_dir=Path(args.workbook_preview_dir).resolve() if args.workbook_preview_dir else None,
            )
        except (RegionInferenceError, OSError, subprocess.SubprocessError) as exc:
            print(f"region-inference error: {exc}", file=sys.stderr)
            return 2
        if workbook_metadata.get("sheet_rows") != expected_sheet_rows:
            print("region-inference error: workbook sheet-row metadata mismatch.", file=sys.stderr)
            return 2
        try:
            delivery_path = Path(args.workbook_delivery).resolve() if args.workbook_delivery else None
            delivery_sha256 = publish_workbook_delivery(workbook, delivery_path) if delivery_path else None
        except (RegionInferenceError, OSError) as exc:
            print(f"region-inference error: {exc}", file=sys.stderr)
            return 2
    result["report_artifact"] = {
        "path": str(report), "sha256": sha256_file(report),
        "integrity": integrity, "report_contract": "h13-style-summary-v2",
    }
    if workbook is not None and workbook_metadata is not None:
        result["workbook_artifact"] = {
            "path": str(workbook), "sha256": workbook_metadata["sha256"],
            "sheet_rows": workbook_metadata["sheet_rows"], "integrity": integrity,
            "workbook_contract": "single-sheet-cycle-edge-audit-v2",
        }
        if delivery_path is not None:
            result["workbook_artifact"]["delivery_path"] = str(delivery_path)
            result["workbook_artifact"]["delivery_sha256"] = delivery_sha256
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    destinations = f"{output} / {report}" + (f" / {workbook}" if workbook is not None else "")
    artifact_label = "H13-style Markdown summary" + (", and single-sheet Excel cycle-edge audit" if workbook is not None else "")
    print(f"Wrote {len(result['results'])} edge groups, {len(result['candidate_index'])} candidate-index entries, {artifact_label} to {destinations}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
