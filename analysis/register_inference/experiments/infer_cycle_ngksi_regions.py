"""Infer typed, temporally ordered register-update candidates from cycle traces.

Each region is bounded by consecutive configured downlink register observations.
Intervening observations retain their event order and typed identity.  The model
tree keeps transport-context signals, numeric wrap guards, and counterexample-
derived value guards as different node kinds.  Results remain observational
candidates; they are not claims about AMF implementation variables.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
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


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegionInferenceError(f"Invalid YAML config {path}: {exc}") from exc
    if not isinstance(config, dict) or config.get("schema_version") not in (1, 2):
        raise RegionInferenceError("Config must be a mapping with schema_version 1 or 2.")
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
) -> Iterable[tuple[int, dict[str, Any], dict[str, Any]]]:
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
            yield repetition, record, make_edge(cycle, loop_inputs, edge_offset, record, indexed_edges)


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
    for repetition, record, edge in source_events(cycle, variant, records, indexed_edges, end_repeat):
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
    return f"r' = i{formula['slot']}{signed_offset(formula['value'])}"


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


def unique_sorted_trees(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {json.dumps(candidate, sort_keys=True): candidate for candidate in candidates}
    return sorted(unique.values(), key=tree_score)


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
        variable = "r" if guard["variable"] == "r" else f"i{guard['slot']}"
        condition = f"{variable} < {guard['threshold']}"
    else:
        condition = f"i{guard['slot']} == {guard['value']}"
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


def structural_candidates(edges: Iterable[dict[str, Any]], d_states: set[str], downlink_paths: dict[str, str], uplink_paths: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for edge in edges:
        if edge["source_state"] != edge["target_state"] and edge["target_state"] in d_states:
            result.append({"formula": {"kind": "constant", "value": 7}, "origin": "d_state_reset_prior", "priority": 0})
        if edge["logical_output"] not in downlink_paths and edge["logical_input"] not in uplink_paths:
            result.append({"formula": {"kind": "r_plus", "value": 0}, "origin": "no_ksi_default", "priority": 1})
    return result


def infer(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
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
            for _, _, edge in source_events(cycle, variant, records, indexed_edges, end_repeat):
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="YAML region-inference configuration")
    parser.add_argument("--output", required=True, help="JSON output path")
    args = parser.parse_args(argv)
    try:
        config_path = Path(args.config).resolve()
        result = infer(load_config(config_path), config_path)
    except (RegionInferenceError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"region-inference error: {exc}", file=sys.stderr)
        return 2
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(result['results'])} edge groups and {len(result['candidate_index'])} candidate-index entries to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
