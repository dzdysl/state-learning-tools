"""Exact two-projection formula discovery for directed register trajectories.

This module implements trajectory classification algorithm B.  It is deliberately independent from the
soft-DTW trajectory-clustering pipeline: an EID owns zero or more exact
projection candidates, while normalized candidate groups are only reverse
indexes and never a partition of the EIDs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from itertools import product
from typing import Any, Iterable


SCHEMA = "register-trajectory-formula-discovery-v4"
DEFAULT_INPUT_OUTPUTS = (
    ("authenticationResponse", "securityModeCommand"),
    ("registrationRequest", "authenticationRequest"),
    ("registrationRequestGUTI", "authenticationRequest"),
)
INPUT_REGISTER = "r_i[ngksi_uplink]"


@dataclass(frozen=True)
class Axis:
    projection: str
    x_field: str
    x_register: str
    x_label: str


AXES = {
    "before_after": Axis("before_after", "r_before", "r", "r_before"),
    "input_after": Axis("input_after", "r_i", INPUT_REGISTER, "r_i"),
}


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _at_path(record: dict[str, Any], dotted: str) -> Any:
    value: Any = record
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def replay_input_register(
    trace: Iterable[dict[str, Any]], config: dict[str, Any]
) -> dict[int, int | None]:
    """Replay configured numeric-input writes independently per sequence."""
    definitions = config.get("mapping", {}).get("numeric_input_definitions", [])
    sequences: dict[Any, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for line, record in enumerate(trace, 1):
        sequences[record.get("sequence_id", line)].append((line, record))
    carried: dict[int, int | None] = {}
    for rows in sequences.values():
        current: int | None = None
        for line, record in rows:
            if record.get("symbol_index") == 1:
                current = None
            symbol = (record.get("abstract_io") or {}).get("input")
            for definition in definitions:
                if definition.get("input_register_id") != "ngksi_uplink":
                    continue
                if symbol not in definition.get("match", {}).get("input_symbols", []):
                    continue
                observed = _at_path(record, str(definition.get("path", "")))
                if observed is not None:
                    current = int(observed)
            carried[line] = current
    return carried


def _cycle_variants(cycle_cover: dict[str, Any]) -> set[tuple[str, int]]:
    cycles = cycle_cover.get("sequence_export", {}).get("cycles")
    if not isinstance(cycles, list):
        raise ValueError("cycle_cover.sequence_export.cycles is required")
    return {
        (str(cycle["cycle_id"]), int(variant["line_number"]))
        for cycle in cycles
        for variant in cycle.get("variants", [])
    }


def _signal_context(region: dict[str, Any]) -> dict[str, int]:
    context: dict[str, int] = {}
    for signal in region.get("signals", []):
        identifier = signal.get("signal_id") or signal.get("id")
        if identifier is not None and signal.get("value") is not None:
            context[str(identifier)] = int(signal["value"])
    return context


def reconstruct_trajectories(
    candidates: dict[str, Any],
    trace: Iterable[dict[str, Any]],
    cycle_cover: dict[str, Any],
    config: dict[str, Any],
    input_outputs: Iterable[tuple[str, str]] = DEFAULT_INPUT_OUTPUTS,
) -> list[dict[str, Any]]:
    """Materialize real R3--R10 trajectories for the selected I/O pairs."""
    valid_variants = _cycle_variants(cycle_cover)
    carried = replay_input_register(trace, config)
    selected_pairs = set(input_outputs)
    trajectories: list[dict[str, Any]] = []
    for result in candidates.get("results", []):
        edge = result.get("edge", {})
        logical_pair = (edge.get("logical_input"), edge.get("logical_output"))
        if logical_pair not in selected_pairs:
            continue
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for region in result.get("direct_regions", []):
            repetition = int(region.get("repetition", -1))
            if not 3 <= repetition <= 10:
                continue
            cycle_id = str(region.get("cycle_id"))
            sequence_line = int(region.get("sequence_line", -1))
            if (cycle_id, sequence_line) not in valid_variants:
                raise ValueError(
                    f"unknown cycle variant {cycle_id}:L{sequence_line} for {edge.get('edge_id')}"
                )
            before = region.get("previous_output") or {}
            after = region.get("terminal_output") or {}
            if before.get("value") is None or after.get("value") is None:
                raise ValueError("projection point is missing r_before or r_after")
            observed = (region.get("input_register_values") or {}).get("ngksi_uplink")
            source = "direct_observation"
            if observed and observed.get("value") is not None:
                input_value = int(observed["value"])
                input_trace_line = int(observed.get("trace_line", 0)) or None
            else:
                terminal_line = int(after.get("trace_line", 0))
                input_value = carried.get(terminal_line)
                input_trace_line = terminal_line or None
                source = "carried_from_R2" if repetition == 3 else "carried_from_previous_input"
            if input_value is None:
                raise ValueError(
                    f"unobservable {INPUT_REGISTER} at {edge.get('edge_id')}:{cycle_id}:L{sequence_line}:R{repetition}"
                )
            grouped[(cycle_id, sequence_line)].append(
                {
                    "repetition": repetition,
                    "r_before": int(before["value"]),
                    "r_after": int(after["value"]),
                    "r_i": int(input_value),
                    "input_source": source,
                    "input_trace_line": input_trace_line,
                    "previous_output_trace_line": before.get("trace_line"),
                    "terminal_output_trace_line": after.get("trace_line"),
                    "signal_context": _signal_context(region),
                }
            )
        for (cycle_id, sequence_line), points in sorted(grouped.items()):
            points.sort(key=lambda item: item["repetition"])
            repetitions = [point["repetition"] for point in points]
            if repetitions != list(range(3, 11)):
                raise ValueError(
                    f"expected real R3-R10 at {edge.get('edge_id')}:{cycle_id}:L{sequence_line}, got {repetitions}"
                )
            signal_contexts = {
                json.dumps(point["signal_context"], sort_keys=True, separators=(",", ":"))
                for point in points
            }
            if len(signal_contexts) != 1:
                raise ValueError(
                    f"mixed signal context at {edge.get('edge_id')}:{cycle_id}:L{sequence_line}"
                )
            trajectories.append(
                {
                    "id": f"{edge['edge_id']}:{cycle_id}:L{sequence_line}",
                    "eid": str(edge["edge_id"]),
                    "cycle_id": cycle_id,
                    "sequence_line": sequence_line,
                    "logical_input": str(edge["logical_input"]),
                    "logical_output": str(edge["logical_output"]),
                    "candidate_grade": result.get("candidate_grade"),
                    "signal_context": points[0]["signal_context"],
                    "points": points,
                }
            )
    return sorted(trajectories, key=lambda item: item["id"])


def _formula_key(ast: dict[str, Any]) -> str:
    return json.dumps(ast, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _constant(value: int) -> dict[str, Any]:
    return {"kind": "constant", "value": value}


def _affine(variable: str, offset: int) -> dict[str, Any]:
    return {"kind": "affine_unit", "variable": variable, "offset": offset}


def _ite(variable: str, threshold: int, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "ite",
        "guard": {"variable": variable, "operator": "<", "value": threshold},
        "true_branch": left,
        "false_branch": right,
    }


def formula_text(ast: dict[str, Any], abbreviated: bool = True) -> str:
    def expression(node: dict[str, Any]) -> str:
        kind = node["kind"]
        if kind == "constant":
            return str(node["value"])
        if kind == "affine_unit":
            variable = node["variable"]
            if abbreviated and variable == INPUT_REGISTER:
                variable = "r_i"
            offset = int(node["offset"])
            if offset == 0:
                return variable
            return f"{variable} {'+' if offset > 0 else '-'} {abs(offset)}"
        if kind == "ite":
            variable = node["guard"]["variable"]
            if abbreviated and variable == INPUT_REGISTER:
                variable = "r_i"
            return (
                f"ite({variable} < {node['guard']['value']}, "
                f"{expression(node['true_branch'])}, {expression(node['false_branch'])})"
            )
        raise ValueError(f"unknown formula kind {kind}")

    return f"r' = {expression(ast)}"


def evaluate_formula(ast: dict[str, Any], x: int) -> int:
    kind = ast["kind"]
    if kind == "constant":
        return int(ast["value"])
    if kind == "affine_unit":
        return x + int(ast["offset"])
    if kind == "ite":
        branch = ast["true_branch"] if x < int(ast["guard"]["value"]) else ast["false_branch"]
        return evaluate_formula(branch, x)
    raise ValueError(f"unknown formula kind {kind}")


def _point_records(
    trajectories: list[dict[str, Any]], axis: Axis
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    counter: Counter[tuple[int, int]] = Counter()
    sources: dict[tuple[int, int], set[str]] = defaultdict(set)
    segment_counter: Counter[tuple[int, int, int, int]] = Counter()
    segment_sources: dict[tuple[int, int, int, int], set[str]] = defaultdict(set)
    for trajectory in trajectories:
        projected = []
        for point in trajectory["points"]:
            coordinate = (int(point[axis.x_field]), int(point["r_after"]))
            projected.append(coordinate)
            counter[coordinate] += 1
            sources[coordinate].add(trajectory["id"])
        for left, right in zip(projected, projected[1:]):
            key = (*left, *right)
            segment_counter[key] += 1
            segment_sources[key].add(trajectory["id"])
    points = [
        {
            "x": x,
            "y": y,
            "support_count": counter[(x, y)],
            "trajectory_ids": sorted(sources[(x, y)]),
        }
        for x, y in sorted(counter)
    ]
    segments = [
        {
            "tail": [x1, y1],
            "head": [x2, y2],
            "support_count": segment_counter[(x1, y1, x2, y2)],
            "trajectory_ids": sorted(segment_sources[(x1, y1, x2, y2)]),
        }
        for x1, y1, x2, y2 in sorted(segment_counter)
    ]
    return points, segments


def _projection_trajectory_evidence(
    trajectories: list[dict[str, Any]], axis: Axis
) -> list[dict[str, Any]]:
    evidence = []
    for trajectory in trajectories:
        projected = [
            [int(point[axis.x_field]), int(point["r_after"])]
            for point in trajectory["points"]
        ]
        unique_points = sorted({tuple(point) for point in projected})
        distinct_x = {point[0] for point in unique_points}
        distinct_y = {point[1] for point in unique_points}
        non_self = [
            [left, right]
            for left, right in zip(projected, projected[1:])
            if left != right
        ]
        if len(unique_points) == 1:
            kind = "static_point"
        elif len(distinct_x) == 1:
            kind = "pure_vertical"
        elif len(distinct_y) == 1 and any(
            right[0] != left[0] and right[1] == left[1]
            for left, right in zip(projected, projected[1:])
        ):
            kind = "horizontal"
        else:
            kind = "dynamic"
        evidence.append(
            {
                "trajectory_id": trajectory["id"],
                "kind": kind,
                "unique_points": [list(point) for point in unique_points],
                "non_self_segments": non_self,
                "signal_context": trajectory.get("signal_context", {}),
            }
        )
    return evidence


def _select_trajectories(
    trajectories: list[dict[str, Any]], identifiers: set[str]
) -> list[dict[str, Any]]:
    return [item for item in trajectories if item["id"] in identifiers]


def _vertical_components(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_x: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        by_x[point["x"]].append(point)
    output = []
    for x, items in sorted(by_x.items()):
        ys = sorted({item["y"] for item in items})
        if len(ys) < 2:
            continue
        output.append(
            {
                "x": x,
                "distinct_y": ys,
                "strength": "core" if len(ys) >= 3 else "weak",
                "support_count": sum(item["support_count"] for item in items),
                "points": [[x, y] for y in ys],
            }
        )
    return output


def _functional_obligations(points: list[dict[str, Any]]) -> tuple[dict[int, int], set[int]]:
    by_x: dict[int, set[int]] = defaultdict(set)
    for point in points:
        by_x[point["x"]].add(point["y"])
    mandatory = {x: next(iter(ys)) for x, ys in by_x.items() if len(ys) == 1}
    vertical = {x for x, ys in by_x.items() if len(ys) > 1}
    return mandatory, vertical


def _primitive_options(
    points: list[dict[str, Any]],
    variable: str,
    *,
    min_distinct_x: int,
    allow_small_constant: bool,
    allow_constants: bool = True,
) -> list[dict[str, Any]]:
    if not points:
        return []
    mandatory, _ = _functional_obligations(points)
    candidates: dict[str, dict[str, Any]] = {}
    if allow_constants:
        values = sorted({point["y"] for point in points})
        for value in values:
            ast = _constant(value)
            matched = [point for point in points if point["y"] == value]
            matched_x = sorted({point["x"] for point in matched})
            if any(y != value for y in mandatory.values()):
                continue
            if len(matched_x) < min_distinct_x and not allow_small_constant:
                continue
            candidates[_formula_key(ast)] = {"ast": ast, "matched": matched}
    offsets = sorted({point["y"] - point["x"] for point in points})
    for offset in offsets:
        ast = _affine(variable, offset)
        matched = [point for point in points if point["y"] == point["x"] + offset]
        matched_x = sorted({point["x"] for point in matched})
        if len(matched_x) < max(3, min_distinct_x):
            continue
        if any(y != x + offset for x, y in mandatory.items()):
            continue
        candidates[_formula_key(ast)] = {"ast": ast, "matched": matched}
    return [candidates[key] for key in sorted(candidates)]


def _support_level(ast: dict[str, Any], distinct_x: int) -> str:
    if ast["kind"] == "constant":
        return "weak" if distinct_x == 1 else ("limited" if distinct_x == 2 else "core")
    return "core"


def _branch_for(ast: dict[str, Any], x: int) -> str:
    if ast["kind"] != "ite":
        return "simple"
    return "true" if x < int(ast["guard"]["value"]) else "false"


def _direction_evidence(ast: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter()
    evidence = []
    for segment in segments:
        x1, y1 = segment["tail"]
        x2, y2 = segment["head"]
        matches = evaluate_formula(ast, x1) == y1 and evaluate_formula(ast, x2) == y2
        if x1 == x2 and y1 == y2:
            category = "self_loop_excluded"
        elif x1 == x2:
            category = "vertical"
        elif matches and _branch_for(ast, x1) != _branch_for(ast, x2):
            category = "cross_component"
        elif matches:
            category = "forward" if x2 > x1 else "reverse"
        else:
            category = "outside_candidate"
        counts[category] += 1
        evidence.append({"tail": segment["tail"], "head": segment["head"], "category": category})
    forward, reverse = counts["forward"], counts["reverse"]
    majority = "mixed"
    if forward > reverse:
        majority = "forward_majority"
    elif reverse > forward:
        majority = "reverse_majority"
    return {
        "policy": "deduplicated_directed_segments_per_eid",
        "forward": forward,
        "reverse": reverse,
        "self_loops_excluded": sum(
            1 for segment in segments if segment["tail"] == segment["head"]
        ),
        "vertical": counts["vertical"],
        "cross_component": counts["cross_component"],
        "outside_candidate": counts["outside_candidate"],
        "majority": majority,
        "segments": evidence,
    }


def _finalize_candidate(
    ast: dict[str, Any],
    points: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    matched: list[dict[str, Any]],
    *,
    support_level: str | None = None,
    threshold_interval: list[int] | None = None,
    fitting_trajectory_ids: Iterable[str] = (),
) -> dict[str, Any]:
    covered_x = sorted({point["x"] for point in matched})
    missing_x = (
        [x for x in range(min(covered_x), max(covered_x) + 1) if x not in covered_x]
        if covered_x
        else []
    )
    matched_pairs = {(point["x"], point["y"]) for point in matched}
    all_pairs = {(point["x"], point["y"]) for point in points}
    unresolved = sorted([list(pair) for pair in all_pairs - matched_pairs])
    candidate = {
        "ast": ast,
        "formula": formula_text(ast),
        "formula_expanded": formula_text(ast, abbreviated=False),
        "formula_kind": ast["kind"],
        "scope": "full_projection" if not unresolved else "functional_subset",
        "support_level": support_level or "core",
        "evidence_grade": "observationally_exact_with_gaps" if missing_x else "observationally_exact",
        "covered_x": covered_x,
        "missing_x": missing_x,
        "support_points": sorted([[point["x"], point["y"]] for point in matched]),
        "unresolved_points": unresolved,
        "raw_support_count": sum(point["support_count"] for point in matched),
        "direction": _direction_evidence(ast, segments),
        "fitting_trajectory_ids": sorted(set(fitting_trajectory_ids)),
        "compatible_degenerate_trajectories": [],
        "unresolved_degenerate_points": [],
    }
    if threshold_interval is not None:
        candidate["equivalent_threshold_interval"] = threshold_interval
    return candidate


def _attach_degenerate_evidence(
    candidate: dict[str, Any], trajectory_evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    compatible = []
    unresolved = []
    for evidence in trajectory_evidence:
        if evidence["kind"] not in {"static_point", "pure_vertical"}:
            continue
        matched = []
        unmatched = []
        for point in evidence["unique_points"]:
            target = matched if evaluate_formula(candidate["ast"], point[0]) == point[1] else unmatched
            target.append(point)
        if matched:
            compatible.append(
                {
                    "trajectory_id": evidence["trajectory_id"],
                    "kind": evidence["kind"],
                    "points": matched,
                }
            )
        if unmatched:
            unresolved.append(
                {
                    "trajectory_id": evidence["trajectory_id"],
                    "kind": evidence["kind"],
                    "points": unmatched,
                }
            )
    candidate["compatible_degenerate_trajectories"] = compatible
    candidate["unresolved_degenerate_points"] = unresolved
    if unresolved:
        candidate["scope"] = "functional_subset"
    return candidate


def discover_projection(
    trajectories: list[dict[str, Any]], axis: Axis
) -> dict[str, Any]:
    all_points, all_segments = _point_records(trajectories, axis)
    trajectory_evidence = _projection_trajectory_evidence(trajectories, axis)
    verticals = _vertical_components(all_points)
    candidates: list[dict[str, Any]] = []

    horizontal_by_value: dict[int, set[str]] = defaultdict(set)
    for evidence in trajectory_evidence:
        if evidence["kind"] == "horizontal":
            horizontal_by_value[evidence["unique_points"][0][1]].add(
                evidence["trajectory_id"]
            )
    for value, identifiers in sorted(horizontal_by_value.items()):
        selected = _select_trajectories(trajectories, identifiers)
        points, segments = _point_records(selected, axis)
        distinct_x = len({point["x"] for point in points})
        if distinct_x < 2:
            continue
        candidates.append(
            _attach_degenerate_evidence(
                _finalize_candidate(
                    _constant(value),
                    points,
                    segments,
                    points,
                    support_level="limited" if distinct_x == 2 else "core",
                    fitting_trajectory_ids=identifiers,
                ),
                trajectory_evidence,
            )
        )

    dynamic_ids = {
        evidence["trajectory_id"]
        for evidence in trajectory_evidence
        if evidence["kind"] == "dynamic"
    }
    dynamic_trajectories = _select_trajectories(trajectories, dynamic_ids)
    points, segments = _point_records(dynamic_trajectories, axis)
    simple = _primitive_options(
        points,
        axis.x_register,
        min_distinct_x=1,
        allow_small_constant=True,
        allow_constants=False,
    )
    mandatory, _ = _functional_obligations(points)
    full_simple = []
    for option in simple:
        ast = option["ast"]
        distinct_x = len({point["x"] for point in option["matched"]})
        if ast["kind"] == "affine_unit" and distinct_x < 3:
            continue
        if all(evaluate_formula(ast, x) == y for x, y in mandatory.items()):
            full_simple.append(
                _finalize_candidate(
                    ast,
                    points,
                    segments,
                    option["matched"],
                    support_level=_support_level(ast, distinct_x),
                    fitting_trajectory_ids=dynamic_ids,
                )
            )
    # Easy-to-hard: a simple formula that covers every mandatory x suppresses
    # needless split enumeration.  Vertical alternatives remain unresolved.
    dynamic_candidates = full_simple
    if not dynamic_candidates and mandatory:
        x_values = sorted({point["x"] for point in points})
        split_candidates: list[dict[str, Any]] = []
        for threshold in range(min(x_values) + 1, max(x_values) + 1):
            left_points = [point for point in points if point["x"] < threshold]
            right_points = [point for point in points if point["x"] >= threshold]
            left = _primitive_options(
                left_points,
                axis.x_register,
                min_distinct_x=3,
                allow_small_constant=False,
            )
            right = _primitive_options(
                right_points,
                axis.x_register,
                min_distinct_x=1,
                allow_small_constant=True,
            )
            for left_option in left:
                for right_option in right:
                    ast = _ite(axis.x_register, threshold, left_option["ast"], right_option["ast"])
                    matched = [point for point in points if evaluate_formula(ast, point["x"]) == point["y"]]
                    if any(evaluate_formula(ast, x) != y for x, y in mandatory.items()):
                        continue
                    left_x = sorted({point["x"] for point in left_option["matched"]})
                    right_x = sorted({point["x"] for point in right_option["matched"]})
                    if not left_x or not right_x:
                        continue
                    interval = [max(left_x) + 1, min(right_x)]
                    canonical = interval[1]
                    ast = _ite(axis.x_register, canonical, left_option["ast"], right_option["ast"])
                    matched = [point for point in points if evaluate_formula(ast, point["x"]) == point["y"]]
                    split_candidates.append(
                        _finalize_candidate(
                            ast,
                            points,
                            segments,
                            matched,
                            threshold_interval=interval,
                            fitting_trajectory_ids=dynamic_ids,
                        )
                    )
        if split_candidates:
            max_main = max(
                sum(1 for x in candidate["covered_x"] if x < candidate["ast"]["guard"]["value"])
                for candidate in split_candidates
            )
            dynamic_candidates = [
                candidate
                for candidate in split_candidates
                if sum(1 for x in candidate["covered_x"] if x < candidate["ast"]["guard"]["value"])
                == max_main
            ]
    candidates.extend(
        _attach_degenerate_evidence(candidate, trajectory_evidence)
        for candidate in dynamic_candidates
    )
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = _formula_key(candidate["ast"])
        unique.setdefault(key, candidate)
    return {
        "axis": {"x": axis.x_label, "y": "r_after", "x_register": axis.x_register, "y_register": "r'"},
        "unique_points": all_points,
        "directed_segments": all_segments,
        "vertical_components": verticals,
        "trajectory_evidence": trajectory_evidence,
        "no_formula_reason": (
            None
            if unique
            else (
                "degenerate_only"
                if trajectory_evidence
                and all(
                    item["kind"] in {"static_point", "pure_vertical"}
                    for item in trajectory_evidence
                )
                else "no_simple_formula"
            )
        ),
        "candidates": [unique[key] for key in sorted(unique)],
    }


def _formula_ast_to_update_tree(ast: dict[str, Any]) -> dict[str, Any]:
    kind = ast["kind"]
    if kind == "constant":
        return {"kind": "leaf", "formula": {"kind": "constant", "value": int(ast["value"])}}
    if kind == "affine_unit":
        variable = ast["variable"]
        formula_kind = "r_plus" if variable == "r" else "input_register_plus"
        formula: dict[str, Any] = {"kind": formula_kind, "value": int(ast["offset"])}
        if formula_kind == "input_register_plus":
            formula["input_register_id"] = "ngksi_uplink"
        return {"kind": "leaf", "formula": formula}
    if kind == "ite":
        variable = ast["guard"]["variable"]
        guard: dict[str, Any] = {
            "variable": "r" if variable == "r" else "input_register",
            "operator": "<",
            "threshold": int(ast["guard"]["value"]),
        }
        if guard["variable"] == "input_register":
            guard["input_register_id"] = "ngksi_uplink"
        return {
            "kind": "threshold_guard",
            "guard": guard,
            "true": _formula_ast_to_update_tree(ast["true_branch"]),
            "false": _formula_ast_to_update_tree(ast["false_branch"]),
        }
    raise ValueError(f"unknown formula kind {kind}")


def _evaluate_update_tree(tree: dict[str, Any], point: dict[str, Any]) -> int:
    kind = tree["kind"]
    if kind == "leaf":
        formula = tree["formula"]
        formula_kind = formula["kind"]
        if formula_kind == "constant":
            return int(formula["value"])
        if formula_kind == "r_plus":
            return int(point["r_before"]) + int(formula["value"])
        if formula_kind == "input_register_plus":
            return int(point["r_i"]) + int(formula["value"])
        raise ValueError(f"unknown update formula kind {formula_kind}")
    guard = tree["guard"]
    value = int(point["r_before"] if guard["variable"] == "r" else point["r_i"])
    if kind == "threshold_guard":
        branch = tree["true"] if value < int(guard["threshold"]) else tree["false"]
        return _evaluate_update_tree(branch, point)
    if kind == "derived_value_guard":
        branch = tree["true"] if value == int(guard["value"]) else tree["false"]
        return _evaluate_update_tree(branch, point)
    raise ValueError(f"unknown update tree kind {kind}")


def update_tree_text(tree: dict[str, Any], abbreviated: bool = True) -> str:
    def expression(node: dict[str, Any]) -> str:
        if node["kind"] == "leaf":
            formula = node["formula"]
            kind = formula["kind"]
            if kind == "constant":
                return str(formula["value"])
            variable = "r" if kind == "r_plus" else (
                "r_i" if abbreviated else INPUT_REGISTER
            )
            offset = int(formula["value"])
            if offset == 0:
                return variable
            return f"{variable} {'+' if offset > 0 else '-'} {abs(offset)}"
        guard = node["guard"]
        variable = "r" if guard["variable"] == "r" else (
            "r_i" if abbreviated else INPUT_REGISTER
        )
        if node["kind"] == "threshold_guard":
            condition = f"{variable} < {guard['threshold']}"
        elif node["kind"] == "derived_value_guard":
            condition = f"{variable} = {guard['value']}"
        else:
            raise ValueError(f"unknown update tree kind {node['kind']}")
        return f"ite({condition}, {expression(node['true'])}, {expression(node['false'])})"

    return f"r' = {expression(tree)}"


def _tree_key(tree: dict[str, Any]) -> str:
    return json.dumps(tree, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_candidate_id(
    logical_input: str, logical_output: str, tree: dict[str, Any]
) -> str:
    identity = json.dumps(
        [logical_input, logical_output, tree],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"SB-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:10]}"


def _verify_update_tree(
    tree: dict[str, Any], trajectories: list[dict[str, Any]]
) -> dict[str, Any]:
    failures = []
    branch_counts = Counter()
    total = 0
    matched = 0
    for trajectory in trajectories:
        for point in trajectory["points"]:
            total += 1
            if tree["kind"] == "derived_value_guard":
                value = point["r_before"] if tree["guard"]["variable"] == "r" else point["r_i"]
                branch_counts["true" if value == tree["guard"]["value"] else "false"] += 1
            predicted = _evaluate_update_tree(tree, point)
            if predicted == int(point["r_after"]):
                matched += 1
            else:
                failures.append(
                    {
                        "trajectory_id": trajectory["id"],
                        "repetition": point["repetition"],
                        "r_before": point["r_before"],
                        "r_i": point["r_i"],
                        "r_after": point["r_after"],
                        "predicted": predicted,
                    }
                )
    return {
        "sample_count": total,
        "matched_sample_count": matched,
        "exact": matched == total,
        "root_branch_counts": dict(sorted(branch_counts.items())),
        "failures": failures,
    }


def _stable_projection_classification(projection: dict[str, Any]) -> str:
    evidence = projection["trajectory_evidence"]
    effective = [item for item in evidence if item["kind"] in {"horizontal", "dynamic"}]
    x_values = {point["x"] for point in projection["unique_points"]}
    if not effective and len(x_values) == 1 and projection["vertical_components"]:
        return "pure_vertical"
    if any(candidate["scope"] == "full_projection" for candidate in projection["candidates"]):
        return "simple_exact"
    if projection["candidates"]:
        return "candidate_only"
    if evidence and all(item["kind"] in {"static_point", "pure_vertical"} for item in evidence):
        return "degenerate_only"
    return "no_candidate"


def _stable_signal_condition(
    trajectories: list[dict[str, Any]], source_groups: list[dict[str, Any]]
) -> dict[str, Any]:
    contexts = {
        json.dumps(item.get("signal_context", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for item in trajectories
    }
    if len(contexts) > 1:
        return {
            "status": "inconsistent_signal_context",
            "values": [json.loads(value) for value in sorted(contexts)],
        }
    values = json.loads(next(iter(contexts))) if contexts else {}
    return {
        "status": "observed" if values else "not_applicable",
        "values": values,
        "source_group_contexts": [group.get("signal_context", []) for group in source_groups],
    }


def _finalize_stable_aggregate_candidate(
    logical_input: str,
    logical_output: str,
    tree: dict[str, Any],
    trajectories: list[dict[str, Any]],
    *,
    selection_tier: str,
    source_projections: dict[str, Any],
) -> dict[str, Any] | None:
    verification = _verify_update_tree(tree, trajectories)
    if not verification["exact"]:
        return None
    return {
        "candidate_id": _stable_candidate_id(logical_input, logical_output, tree),
        "selection_tier": selection_tier,
        "formula_kind": "cross_projection_tree" if tree["kind"] == "derived_value_guard" else "simple_formula",
        "formula": update_tree_text(tree),
        "formula_expanded": update_tree_text(tree, abbreviated=False),
        "update_tree": tree,
        "source_projections": source_projections,
        "verification": verification,
    }


def aggregate_stable_inference(
    candidates: dict[str, Any],
    trajectories: list[dict[str, Any]],
    input_outputs: Iterable[tuple[str, str]],
) -> dict[str, Any]:
    """Aggregate relatively-stable source trajectories from simple to complex."""
    selected_pairs = tuple(input_outputs)
    groups_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for group in candidates.get("relatively_stable_inference", {}).get("groups", []):
        pair = (str(group.get("logical_input")), str(group.get("logical_output")))
        if pair in selected_pairs:
            groups_by_pair[pair].append(group)
    output: dict[str, Any] = {}
    all_source_eids: set[str] = set()
    all_source_trajectory_ids: set[str] = set()
    final_candidate_count = 0
    for logical_input, logical_output in selected_pairs:
        pair = (logical_input, logical_output)
        source_groups = groups_by_pair.get(pair, [])
        source_eids = sorted(
            {
                str(eid)
                for group in source_groups
                for eid in group.get("source_edge_ids", [])
            }
        )
        selected = [
            trajectory
            for trajectory in trajectories
            if (trajectory["logical_input"], trajectory["logical_output"]) == pair
            and trajectory["eid"] in set(source_eids)
        ]
        signal_condition = _stable_signal_condition(selected, source_groups)
        projection_results = {
            name: discover_projection(selected, axis) for name, axis in AXES.items()
        }
        for projection in projection_results.values():
            projection["classification"] = _stable_projection_classification(projection)
        entry: dict[str, Any] = {
            "logical_input": logical_input,
            "logical_output": logical_output,
            "source_group_indices": sorted(
                int(group["group_index"]) for group in source_groups if group.get("group_index") is not None
            ),
            "source_edge_ids": source_eids,
            "source_trajectory_ids": [item["id"] for item in selected],
            "trajectory_count": len(selected),
            "sample_count": sum(len(item["points"]) for item in selected),
            "signal_condition": signal_condition,
            "projections": projection_results,
            "final_candidates": [],
        }
        if not source_groups:
            entry["status"] = "no_relatively_stable_source"
            output[f"{logical_input}/{logical_output}"] = entry
            continue
        if signal_condition["status"] == "inconsistent_signal_context":
            entry["status"] = "inconsistent_signal_context"
            output[f"{logical_input}/{logical_output}"] = entry
            continue

        simple_candidates: dict[str, dict[str, Any]] = {}
        for projection_name, projection in projection_results.items():
            for candidate in projection["candidates"]:
                if candidate["scope"] != "full_projection":
                    continue
                tree = _formula_ast_to_update_tree(candidate["ast"])
                finalized = _finalize_stable_aggregate_candidate(
                    logical_input,
                    logical_output,
                    tree,
                    selected,
                    selection_tier="simple_projection",
                    source_projections={projection_name: candidate["formula"]},
                )
                if finalized is not None:
                    simple_candidates.setdefault(_tree_key(tree), finalized)
        if simple_candidates:
            entry["status"] = "inferred"
            entry["selection_tier"] = "simple_projection"
            entry["final_candidates"] = [simple_candidates[key] for key in sorted(simple_candidates)]
        else:
            split_candidates: dict[str, dict[str, Any]] = {}
            current_name = "input_after"
            other_name = "before_after"
            current_axis = AXES[current_name]
            other_axis = AXES[other_name]
            current_projection = projection_results[current_name]
            other_projection = projection_results[other_name]
            vertical_x = {component["x"] for component in current_projection["vertical_components"]}
            samples = [
                (trajectory, point)
                for trajectory in selected
                for point in trajectory["points"]
            ]
            for current in current_projection["candidates"]:
                mismatch_x = {
                    int(point[current_axis.x_field])
                    for _, point in samples
                    if evaluate_formula(current["ast"], int(point[current_axis.x_field]))
                    != int(point["r_after"])
                }
                if len(mismatch_x) != 1:
                    continue
                split_value = next(iter(mismatch_x))
                if split_value not in vertical_x:
                    continue
                for other in other_projection["candidates"]:
                    branch_samples = [
                        point
                        for _, point in samples
                        if int(point[current_axis.x_field]) == split_value
                    ]
                    if not branch_samples or any(
                        evaluate_formula(other["ast"], int(point[other_axis.x_field]))
                        != int(point["r_after"])
                        for point in branch_samples
                    ):
                        continue
                    tree = {
                        "kind": "derived_value_guard",
                        "guard": {
                            "variable": "input_register",
                            "input_register_id": "ngksi_uplink",
                            "operator": "==",
                            "value": split_value,
                        },
                        "true": _formula_ast_to_update_tree(other["ast"]),
                        "false": _formula_ast_to_update_tree(current["ast"]),
                    }
                    finalized = _finalize_stable_aggregate_candidate(
                        logical_input,
                        logical_output,
                        tree,
                        selected,
                        selection_tier="cross_projection_guard",
                        source_projections={
                            "guard_projection": current_name,
                            "guard_value": split_value,
                            "true_branch": {"projection": other_name, "formula": other["formula"]},
                            "false_branch": {"projection": current_name, "formula": current["formula"]},
                        },
                    )
                    if finalized is not None:
                        split_candidates.setdefault(_tree_key(tree), finalized)
            if split_candidates:
                entry["status"] = "inferred"
                entry["selection_tier"] = "cross_projection_guard"
                entry["final_candidates"] = [split_candidates[key] for key in sorted(split_candidates)]
            else:
                entry["status"] = "no_final_candidate"
                entry["selection_tier"] = None
        final_candidate_count += len(entry["final_candidates"])
        all_source_eids.update(source_eids)
        all_source_trajectory_ids.update(entry["source_trajectory_ids"])
        output[f"{logical_input}/{logical_output}"] = entry
    return {
        "method": "relatively_stable_source_projection_aggregation",
        "precedence": ["simple_projection", "cross_projection_guard"],
        "counts": {
            "input_output_count": len(output),
            "eid_count": len(all_source_eids),
            "trajectory_count": len(all_source_trajectory_ids),
            "sample_count": sum(item["sample_count"] for item in output.values()),
            "final_candidate_count": final_candidate_count,
        },
        "by_input_output": output,
    }


def _trajectory_id(edge_id: str, cycle_id: str, sequence_line: int) -> str:
    return f"{edge_id}:{cycle_id}:L{sequence_line}"


def _region_index(candidates: dict[str, Any]) -> dict[str, dict[int, dict[str, Any]]]:
    indexed: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for result in candidates.get("results", []):
        edge_id = str(result.get("edge", {}).get("edge_id", ""))
        for region in result.get("direct_regions", []):
            repetition = int(region.get("repetition", -1))
            if not 3 <= repetition <= 10:
                continue
            identifier = _trajectory_id(
                edge_id, str(region.get("cycle_id")), int(region.get("sequence_line", -1))
            )
            indexed[identifier][repetition] = region
    return indexed


def _migration_status_index(candidates: dict[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for result in candidates.get("results", []):
        edge_id = str(result.get("edge", {}).get("edge_id", ""))
        migration = result.get("relatively_stable_inference_migration") or {}
        for cycle_result in migration.get("cycle_results", []):
            cycle_id = str(cycle_result.get("cycle_id"))
            for sequence_line in cycle_result.get("sequence_lines", []):
                statuses[_trajectory_id(edge_id, cycle_id, int(sequence_line))] = str(
                    cycle_result.get("status", "not_recorded")
                )
    return statuses


def _axis_direction(
    trajectories: Iterable[dict[str, Any]], field: str
) -> dict[str, Any]:
    """Vote once per EID and ordered triple segment, matching algorithm-B direction policy."""
    by_eid: dict[str, set[tuple[tuple[int, int, int], tuple[int, int, int]]]] = defaultdict(set)
    for trajectory in trajectories:
        triples = [
            (int(point["r_before"]), int(point["r_i"]), int(point["r_after"]))
            for point in trajectory["points"]
        ]
        for left, right in zip(triples, triples[1:]):
            if left != right:
                by_eid[str(trajectory.get("eid", trajectory["id"]))].add((left, right))
    offset = 0 if field == "r_before" else 1
    counts = Counter()
    for segments in by_eid.values():
        for left, right in segments:
            if right[offset] > left[offset]:
                counts["forward"] += 1
            elif right[offset] < left[offset]:
                counts["reverse"] += 1
            else:
                counts["vertical"] += 1
    majority = "mixed"
    if counts["forward"] > counts["reverse"]:
        majority = "forward_majority"
    elif counts["reverse"] > counts["forward"]:
        majority = "reverse_majority"
    return {
        "policy": "deduplicated_directed_segments_per_eid",
        "forward": counts["forward"],
        "reverse": counts["reverse"],
        "vertical": counts["vertical"],
        "majority": majority,
    }


def _trajectory_direction(trajectory: dict[str, Any], field: str) -> dict[str, Any]:
    return _axis_direction([trajectory], field)


def preimage_values(
    tree: dict[str, Any], point: dict[str, Any], value_domain: Iterable[int]
) -> list[int]:
    """Return every predecessor r_after value allowed by a terminal update tree."""
    return [
        int(value)
        for value in value_domain
        if _evaluate_update_tree(tree, {**point, "r_before": int(value)})
        == int(point["r_after"])
    ]


def _region_register_value(event: dict[str, Any]) -> int | None:
    observed = (event.get("input_register_values") or {}).get("ngksi_uplink") or {}
    return int(observed["value"]) if observed.get("value") is not None else None


def _repartition_assignment_scenario(
    trajectories: list[dict[str, Any]],
    regions: dict[str, dict[int, dict[str, Any]]],
    hold_edge_ids: set[str],
    reverse_choices: dict[str, int],
    scenario_id: str,
) -> dict[str, Any]:
    grouped: dict[tuple[str, str, int, int, str], list[dict[str, Any]]] = defaultdict(list)
    for trajectory in trajectories:
        if trajectory.get("candidate_grade") != "hypothetical_candidate":
            continue
        identifier = trajectory["id"]
        by_repetition = regions.get(identifier, {})
        for point in trajectory["points"]:
            repetition = int(point["repetition"])
            region = by_repetition.get(repetition)
            if region is None:
                continue
            current_before = int(region["previous_output"]["value"])
            pending: list[dict[str, Any]] = []
            segment_index = 0
            for event in region.get("region_edges", []):
                pending.append(event)
                edge = event["edge"]
                edge_id = str(edge["edge_id"])
                reverse_key = f"{identifier}:R{repetition}:{edge_id}"
                boundary_kind: str | None = None
                pseudo_after: int | None = None
                assumption: str | None = None
                if reverse_key in reverse_choices:
                    boundary_kind = "pseudo_reverse_preimage"
                    pseudo_after = int(reverse_choices[reverse_key])
                    assumption = "scenario_consistent_preimage_value"
                elif edge_id in hold_edge_ids:
                    boundary_kind = "pseudo_hold"
                    pseudo_after = current_before
                    assumption = "edge_level_conditional_hold"
                if boundary_kind is None:
                    continue
                grouped[(identifier, edge_id, segment_index, int(trajectory["sequence_line"]), boundary_kind)].append(
                    {
                        "repetition": repetition,
                        "r_before": current_before,
                        "r_i": _region_register_value(event),
                        "r_after": pseudo_after,
                        "region_edge_ids": [str(item["edge"]["edge_id"]) for item in pending],
                        "boundary_edge": edge,
                        "boundary_kind": boundary_kind,
                        "assumption": assumption,
                    }
                )
                current_before = int(pseudo_after)
                pending = []
                segment_index += 1
            if pending:
                event = pending[-1]
                edge = event["edge"]
                grouped[(identifier, str(edge["edge_id"]), segment_index, int(trajectory["sequence_line"]), "real_downlink")].append(
                    {
                        "repetition": repetition,
                        "r_before": current_before,
                        "r_i": _region_register_value(event),
                        "r_after": int(region["terminal_output"]["value"]),
                        "region_edge_ids": [str(item["edge"]["edge_id"]) for item in pending],
                        "boundary_edge": edge,
                        "boundary_kind": "real_downlink",
                        "assumption": None,
                    }
                )

    repartitioned = []
    for key in sorted(grouped):
        original_id, edge_id, segment_index, sequence_line, boundary_kind = key
        samples = sorted(grouped[key], key=lambda item: item["repetition"])
        repetitions = [sample["repetition"] for sample in samples]
        triples = {
            (sample["r_before"], sample["r_i"], sample["r_after"])
            for sample in samples
        }
        region_length = len(samples[0]["region_edge_ids"]) if samples else 0
        newly_length_one = region_length == 1 and boundary_kind == "real_downlink"
        eligible = (
            newly_length_one
            and repetitions == list(range(3, 11))
            and len(triples) > 1
        )
        trajectory_meta = next(item for item in trajectories if item["id"] == original_id)
        repartitioned.append(
            {
                "id": f"{scenario_id}:{original_id}:P{segment_index}",
                "scenario_id": scenario_id,
                "original_trajectory_id": original_id,
                "cycle_id": trajectory_meta["cycle_id"],
                "sequence_line": sequence_line,
                "segment_index": segment_index,
                "terminal_eid": edge_id,
                "terminal_edge": samples[0]["boundary_edge"] if samples else {},
                "region_edge_ids": samples[0]["region_edge_ids"] if samples else [],
                "region_length": region_length,
                "boundary_kind": boundary_kind,
                "samples": samples,
                "complete_r3_r10": repetitions == list(range(3, 11)),
                "dynamic_triples": len(triples) > 1,
                "newly_length_one": newly_length_one,
                "next_stage_stable_inference_eligible": eligible,
                "exclusion_reason": None if eligible else (
                    "pseudo_boundary_not_independent_evidence"
                    if boundary_kind != "real_downlink"
                    else "not_new_length_one"
                    if not newly_length_one
                    else "incomplete_r3_r10"
                    if repetitions != list(range(3, 11))
                    else "static_triples"
                ),
            }
        )
    return {
        "scenario_id": scenario_id,
        "reverse_choices": reverse_choices,
        "repartitioned_regions": repartitioned,
    }


def infer_predecessor_repartition(
    candidates: dict[str, Any],
    trajectories: list[dict[str, Any]],
    stable_aggregation: dict[str, Any],
) -> dict[str, Any]:
    """Infer conditional predecessor holds and repartition hypothetical regions once."""
    regions = _region_index(candidates)
    migration_statuses = _migration_status_index(candidates)
    stable_by_io = stable_aggregation.get("by_input_output", {})
    stable_trajectories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trajectory in trajectories:
        io = f"{trajectory['logical_input']}/{trajectory['logical_output']}"
        source_ids = set(stable_by_io.get(io, {}).get("source_trajectory_ids", []))
        if trajectory["id"] in source_ids:
            stable_trajectories[io].append(trajectory)

    stable_geometry = {}
    for io, items in sorted(stable_trajectories.items()):
        stable_geometry[io] = {
            "triple_points": sorted(
                {
                    (int(point["r_before"]), int(point["r_i"]), int(point["r_after"]))
                    for trajectory in items
                    for point in trajectory["points"]
                }
            ),
            "direction": {
                field: _axis_direction(items, field) for field in ("r_before", "r_i")
            },
        }

    length_two = []
    hypothetical_inventory = []
    hold_support: dict[str, dict[str, Any]] = {}
    reverse_evidence = []
    value_domain = sorted(
        int(value)
        for value in candidates.get("parameters", {}).get("global_register_value_domain", range(8))
    )
    for trajectory in trajectories:
        if trajectory.get("candidate_grade") != "hypothetical_candidate":
            continue
        by_repetition = regions.get(trajectory["id"], {})
        triples = [
            (int(point["r_before"]), int(point["r_i"]), int(point["r_after"]))
            for point in trajectory["points"]
        ]
        required_repetitions = list(range(3, 11))
        point_repetitions = [int(point["repetition"]) for point in trajectory["points"]]
        complete = (
            point_repetitions == required_repetitions
            and set(by_repetition) == set(required_repetitions)
        )
        region_lengths = [
            int(by_repetition[repetition].get("region_edge_count", 0))
            for repetition in required_repetitions
            if repetition in by_repetition
        ]
        all_regions_length_two = (
            len(region_lengths) == len(required_repetitions)
            and all(length == 2 for length in region_lengths)
        )
        region_length = region_lengths[0] if len(set(region_lengths)) == 1 else None
        dynamic = len(set(triples)) > 1
        selected = complete and all_regions_length_two and dynamic
        hypothetical_inventory.append(
            {
                "trajectory_id": trajectory["id"],
                "terminal_eid": trajectory["eid"],
                "terminal_input_output": f"{trajectory['logical_input']}/{trajectory['logical_output']}",
                "cycle_id": trajectory["cycle_id"],
                "sequence_line": trajectory["sequence_line"],
                "signal_context": trajectory.get("signal_context", {}),
                "old_migration_status": migration_statuses.get(trajectory["id"], "not_recorded"),
                "complete_r3_r10": complete,
                "region_length": region_length,
                "region_lengths_r3_r10": region_lengths,
                "dynamic_triples": dynamic,
                "selection_status": "selected_dynamic_length_two" if selected else "excluded",
                "exclusion_reason": None if selected else (
                    "incomplete_r3_r10" if not complete else
                    "region_length_not_two" if not all_regions_length_two else
                    "static_triples"
                ),
            }
        )
        if not selected:
            continue
        predecessor_events = [by_repetition[rep]["region_edges"][-2] for rep in range(3, 11)]
        predecessor_ids = {str(event["edge"]["edge_id"]) for event in predecessor_events}
        if len(predecessor_ids) != 1:
            continue
        predecessor_id = next(iter(predecessor_ids))
        predecessor_edge = predecessor_events[0]["edge"]
        io = f"{trajectory['logical_input']}/{trajectory['logical_output']}"
        geometry = stable_geometry.get(io, {"triple_points": [], "direction": {}})
        contained = set(triples).issubset({tuple(point) for point in geometry["triple_points"]})
        direction_checks = {}
        direction_consistent = True
        for field in ("r_before", "r_i"):
            observed = _trajectory_direction(trajectory, field)
            reference = geometry.get("direction", {}).get(field, {"majority": "mixed"})
            active = observed["forward"] + observed["reverse"] > 0
            matches = not active or (
                reference["majority"] != "mixed"
                and observed["majority"] == reference["majority"]
            )
            direction_checks[field] = {
                "active": active,
                "observed": observed,
                "stable_reference": reference,
                "matches": matches,
            }
            direction_consistent = direction_consistent and matches
        matched = contained and direction_consistent
        evidence = {
            "trajectory_id": trajectory["id"],
            "terminal_eid": trajectory["eid"],
            "terminal_input_output": io,
            "cycle_id": trajectory["cycle_id"],
            "sequence_line": trajectory["sequence_line"],
            "signal_context": trajectory.get("signal_context", {}),
            "old_migration_status": migration_statuses.get(trajectory["id"], "not_recorded"),
            "predecessor_edge": predecessor_edge,
            "triple_points": [list(point) for point in triples],
            "unique_triple_points": [list(point) for point in sorted(set(triples))],
            "contained_in_stable_triples": contained,
            "direction": direction_checks,
            "stable_trajectory_match": matched,
            "classification": "hold_supported" if matched else "reverse_preimage_required",
        }
        length_two.append(evidence)
        if matched:
            hold = hold_support.setdefault(
                predecessor_id,
                {
                    "eid": predecessor_id,
                    "edge": predecessor_edge,
                    "formula": "r' = r",
                    "update_tree": {"kind": "leaf", "formula": {"kind": "r_plus", "value": 0}},
                    "status": "conditional_hold_inference",
                    "support_trajectory_ids": [],
                    "terminal_input_outputs": [],
                    "signal_contexts": [],
                },
            )
            hold["support_trajectory_ids"].append(trajectory["id"])
            hold["terminal_input_outputs"].append(io)
            hold["signal_contexts"].append(trajectory.get("signal_context", {}))
            continue

        aggregation = stable_by_io.get(io, {})
        candidate_evidence = []
        for stable_candidate in aggregation.get("final_candidates", []):
            sample_preimages = []
            for point in trajectory["points"]:
                allowed = preimage_values(
                    stable_candidate["update_tree"], point, value_domain
                )
                sample_preimages.append(
                    {"repetition": int(point["repetition"]), "allowed_r_after_values": allowed}
                )
            common = sorted(
                set.intersection(
                    *(set(sample["allowed_r_after_values"]) for sample in sample_preimages)
                )
            ) if sample_preimages else []
            candidate_evidence.append(
                {
                    "source_candidate_id": stable_candidate["candidate_id"],
                    "source_formula": stable_candidate["formula"],
                    "source_update_tree": stable_candidate["update_tree"],
                    "samples": sample_preimages,
                    "consistent_cycle_values": common,
                }
            )
        reverse_evidence.append(
            {
                "trajectory_id": trajectory["id"],
                "predecessor_eid": predecessor_id,
                "predecessor_edge": predecessor_edge,
                "status": "set_valued_event_preimage",
                "value_domain": value_domain,
                "candidate_preimages": candidate_evidence,
                "does_not_infer_edge_formula": True,
            }
        )

    for hold in hold_support.values():
        for field in ("support_trajectory_ids", "terminal_input_outputs"):
            hold[field] = sorted(set(hold[field]))
        hold["signal_contexts"] = [
            json.loads(value)
            for value in sorted({json.dumps(item, sort_keys=True) for item in hold["signal_contexts"]})
        ]
        hold["support_count"] = len(hold["support_trajectory_ids"])

    reverse_options = []
    for evidence in reverse_evidence:
        choices = []
        for candidate in evidence["candidate_preimages"]:
            for value in candidate["consistent_cycle_values"]:
                choices.append(
                    {
                        "trajectory_id": evidence["trajectory_id"],
                        "predecessor_eid": evidence["predecessor_eid"],
                        "source_candidate_id": candidate["source_candidate_id"],
                        "value": int(value),
                    }
                )
        reverse_options.append(choices)

    scenario_specs = []
    if reverse_options and all(reverse_options):
        combinations = list(product(*reverse_options))
        for index, combination in enumerate(combinations, 1):
            values = [item["value"] for item in combination]
            scenario_id = f"A{values[0]}" if len(values) == 1 else f"A{index:03d}"
            scenario_specs.append(
                {
                    "scenario_id": scenario_id,
                    "assumption": "same_preimage_value_across_R3_R10_per_evidence_trajectory",
                    "selections": list(combination),
                }
            )
    else:
        scenario_specs.append({"scenario_id": "A0", "assumption": "hold_only", "selections": []})

    hypothetical = [
        item for item in trajectories if item.get("candidate_grade") == "hypothetical_candidate"
    ]
    scenarios = []
    for spec in scenario_specs:
        reverse_choices = {}
        for selection in spec["selections"]:
            evidence = next(
                item for item in reverse_evidence
                if item["trajectory_id"] == selection["trajectory_id"]
                and item["predecessor_eid"] == selection["predecessor_eid"]
            )
            for repetition in range(3, 11):
                reverse_choices[
                    f"{evidence['trajectory_id']}:R{repetition}:{evidence['predecessor_eid']}"
                ] = int(selection["value"])
        scenario = _repartition_assignment_scenario(
            hypothetical, regions, set(hold_support), reverse_choices, spec["scenario_id"]
        )
        scenario["assumption"] = spec["assumption"]
        scenario["selections"] = spec["selections"]
        scenarios.append(scenario)

    eligible_index: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        for region in scenario["repartitioned_regions"]:
            if not region["next_stage_stable_inference_eligible"]:
                continue
            key = f"{region['terminal_eid']}:{region['cycle_id']}:L{region['sequence_line']}"
            entry = eligible_index.setdefault(
                key,
                {
                    "id": key,
                    "terminal_eid": region["terminal_eid"],
                    "terminal_edge": region["terminal_edge"],
                    "cycle_id": region["cycle_id"],
                    "sequence_line": region["sequence_line"],
                    "scenario_ids": [],
                    "status": "next_stage_stable_inference_eligible",
                    "formula_fitted": False,
                },
            )
            entry["scenario_ids"].append(scenario["scenario_id"])
    for entry in eligible_index.values():
        entry["scenario_ids"] = sorted(set(entry["scenario_ids"]))

    input_migration_counts = Counter(
        item["old_migration_status"] for item in hypothetical_inventory
    )
    selected_migration_counts = Counter(item["old_migration_status"] for item in length_two)
    return {
        "method": "dynamic_length_two_predecessor_attribution_and_single_pass_repartition",
        "scope": "selected_input_outputs_hypothetical_direct_regions",
        "old_migration_status_policy": "audit_only_not_a_filter",
        "direction_policy": "stable_triple_containment_and_deduplicated_segment_majority",
        "value_domain": value_domain,
        "counts": {
            "dynamic_length_two_trajectory_count": len(length_two),
            "hypothetical_trajectory_count": len(hypothetical_inventory),
            "stable_match_count": sum(item["stable_trajectory_match"] for item in length_two),
            "reverse_preimage_count": len(reverse_evidence),
            "hold_edge_count": len(hold_support),
            "assignment_scenario_count": len(scenarios),
            "eligible_length_one_count": len(eligible_index),
            "input_old_migration_statuses": dict(sorted(input_migration_counts.items())),
            "selected_old_migration_statuses": dict(sorted(selected_migration_counts.items())),
        },
        "stable_geometry": stable_geometry,
        "hypothetical_trajectory_inventory": sorted(
            hypothetical_inventory, key=lambda item: item["trajectory_id"]
        ),
        "dynamic_length_two_trajectories": sorted(length_two, key=lambda item: item["trajectory_id"]),
        "hold_inferences": [hold_support[key] for key in sorted(hold_support)],
        "reverse_preimages": sorted(reverse_evidence, key=lambda item: item["trajectory_id"]),
        "assignment_scenarios": scenarios,
        "eligible_length_one_regions": [eligible_index[key] for key in sorted(eligible_index)],
        "boundaries": {
            "hold_is_conditional_and_refutable": True,
            "reverse_preimage_does_not_infer_edge_formula": True,
            "pseudo_boundaries_are_not_independent_stable_evidence": True,
            "repartition_is_single_pass_without_formula_fitting": True,
        },
    }


def _candidate_id(
    logical_input: str, logical_output: str, projection: str, ast: dict[str, Any]
) -> str:
    identity = json.dumps(
        [logical_input, logical_output, projection, ast],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    return f"B-{projection.replace('_', '-')}-{digest}"


def _compatible(candidate: dict[str, Any], projection: dict[str, Any]) -> tuple[bool, bool]:
    ast = candidate["ast"]
    evidence = projection.get("trajectory_evidence", [])
    effective = [
        item for item in evidence if item["kind"] in {"horizontal", "dynamic"}
    ]
    points = [point for item in effective for point in item["unique_points"]]
    if effective:
        obligations: dict[int, set[int]] = defaultdict(set)
        for x, y in points:
            obligations[x].add(y)
        mandatory = {
            x: next(iter(values)) for x, values in obligations.items() if len(values) == 1
        }
        if mandatory and any(evaluate_formula(ast, x) != y for x, y in mandatory.items()):
            return False, False
        matched = [point for point in points if evaluate_formula(ast, point[0]) == point[1]]
        if not matched:
            return False, False
        unresolved = len(matched) != len(points)
    else:
        points = [point for item in evidence for point in item["unique_points"]]
        matched = [point for point in points if evaluate_formula(ast, point[0]) == point[1]]
        if not matched:
            return False, False
        unresolved = len(matched) != len(points)
    branches = {_branch_for(ast, point[0]) for point in matched}
    partial = unresolved or (ast["kind"] == "ite" and branches != {"true", "false"})
    return True, partial


def analyze(
    candidates: dict[str, Any],
    trace: Iterable[dict[str, Any]],
    cycle_cover: dict[str, Any],
    config: dict[str, Any],
    input_outputs: Iterable[tuple[str, str]] = DEFAULT_INPUT_OUTPUTS,
) -> dict[str, Any]:
    selected_pairs = tuple(dict.fromkeys(tuple(pair) for pair in input_outputs))
    if not selected_pairs:
        raise ValueError("at least one input/output pair is required")
    trajectories = reconstruct_trajectories(
        candidates, trace, cycle_cover, config, selected_pairs
    )
    by_eid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edge_metadata = {
        str(result["edge"]["edge_id"]): result["edge"]
        for result in candidates.get("results", [])
        if (
            result.get("edge", {}).get("logical_input"),
            result.get("edge", {}).get("logical_output"),
        )
        in set(selected_pairs)
    }
    for trajectory in trajectories:
        by_eid[trajectory["eid"]].append(trajectory)
    edges: dict[str, Any] = {}
    for eid in sorted(by_eid):
        projections = {
            name: discover_projection(by_eid[eid], axis)
            for name, axis in AXES.items()
        }
        refs = {}
        for name, projection in projections.items():
            refs[name] = []
            for candidate in projection["candidates"]:
                candidate["candidate_id"] = _candidate_id(
                    edge_metadata[eid]["logical_input"],
                    edge_metadata[eid]["logical_output"],
                    name,
                    candidate["ast"],
                )
                refs[name].append(candidate["candidate_id"])
        edges[eid] = {
            "edge": edge_metadata[eid],
            "trajectory_count": len(by_eid[eid]),
            "sample_count": sum(len(item["points"]) for item in by_eid[eid]),
            "trajectory_ids": [item["id"] for item in by_eid[eid]],
            "signal_contexts": [
                json.loads(value)
                for value in sorted(
                    {
                        json.dumps(item.get("signal_context", {}), sort_keys=True)
                        for item in by_eid[eid]
                    }
                )
            ],
            "projections": projections,
            "combined_signature": refs,
        }
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for eid, edge in edges.items():
        for projection_name, projection in edge["projections"].items():
            for candidate in projection["candidates"]:
                logical_input = edge["edge"]["logical_input"]
                logical_output = edge["edge"]["logical_output"]
                key = (
                    logical_input,
                    logical_output,
                    projection_name,
                    _formula_key(candidate["ast"]),
                )
                group = groups.setdefault(
                    key,
                    {
                        "candidate_id": candidate["candidate_id"],
                        "logical_input": logical_input,
                        "logical_output": logical_output,
                        "projection": projection_name,
                        "ast": candidate["ast"],
                        "formula": candidate["formula"],
                        "owners": [],
                        "core_owners": [],
                        "compatible_eids": [],
                        "partial_compatible_eids": [],
                    },
                )
                group["owners"].append(eid)
                if candidate["support_level"] == "core":
                    group["core_owners"].append(eid)
    for (logical_input, logical_output, projection_name, _), group in groups.items():
        owners = set(group["owners"])
        for eid, edge in edges.items():
            if eid in owners:
                continue
            metadata = edge["edge"]
            if (
                metadata["logical_input"],
                metadata["logical_output"],
            ) != (logical_input, logical_output):
                continue
            compatible, partial = _compatible(group, edge["projections"][projection_name])
            if compatible:
                group["partial_compatible_eids" if partial else "compatible_eids"].append(eid)
        for field in ("owners", "core_owners", "compatible_eids", "partial_compatible_eids"):
            group[field] = sorted(group[field])
    output_groups = [groups[key] for key in sorted(groups)]
    stable_aggregation = aggregate_stable_inference(candidates, trajectories, selected_pairs)
    predecessor_repartition = infer_predecessor_repartition(
        candidates, trajectories, stable_aggregation
    )
    return {
        "schema": SCHEMA,
        "settings": {
            "input_outputs": [
                {"logical_input": logical_input, "logical_output": logical_output}
                for logical_input, logical_output in selected_pairs
            ],
            "repetitions": list(range(3, 11)),
            "candidate_type_filter": None,
            "direction_policy": "deduplicated_directed_segments_per_eid",
            "minimum_affine_distinct_x": 3,
            "vertical_core_distinct_y": 3,
            "predecessor_repartition_passes": 1,
        },
        "counts": {
            "input_output_count": len(selected_pairs),
            "eid_count": len(edges),
            "trajectory_count": len(trajectories),
            "sample_count": sum(len(item["points"]) for item in trajectories),
            "candidate_group_count": len(output_groups),
            "by_input_output": {
                f"{logical_input}/{logical_output}": {
                    "eid_count": sum(
                        1
                        for edge in edges.values()
                        if (
                            edge["edge"]["logical_input"],
                            edge["edge"]["logical_output"],
                        )
                        == (logical_input, logical_output)
                    ),
                    "trajectory_count": sum(
                        1
                        for item in trajectories
                        if (item["logical_input"], item["logical_output"])
                        == (logical_input, logical_output)
                    ),
                    "sample_count": sum(
                        len(item["points"])
                        for item in trajectories
                        if (item["logical_input"], item["logical_output"])
                        == (logical_input, logical_output)
                    ),
                    "candidate_group_count": sum(
                        1
                        for group in output_groups
                        if (group["logical_input"], group["logical_output"])
                        == (logical_input, logical_output)
                    ),
                }
                for logical_input, logical_output in selected_pairs
            },
        },
        "edges": edges,
        "candidate_groups": output_groups,
        "stable_aggregation": stable_aggregation,
        "predecessor_repartition": predecessor_repartition,
        "trajectories": trajectories,
    }
