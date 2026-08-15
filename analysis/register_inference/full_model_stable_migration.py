"""Full-model migration audit for the two completed registration update trees.

The analysis consumes the frozen Algorithm B result without modifying it.  It
checks every H14 edge of the selected input/output pairs, closes the remaining
length-two regions by an explicitly recorded vertical-component preference,
and returns the evidence used by the standalone Chinese report.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from analysis.register_inference.trajectory_formula_discovery import (
    AXES,
    _evaluate_update_tree,
    discover_projection,
    preimage_values,
    replay_input_register,
)


TARGET_INPUT_OUTPUTS = (
    "registrationRequest/authenticationRequest",
    "registrationRequestGUTI/authenticationRequest",
)


def _io(edge: dict[str, Any]) -> str:
    return f"{edge['logical_input']}/{edge['logical_output']}"


def _trajectory_eid(trajectory_id: str) -> str:
    return trajectory_id.split(":", 1)[0]


def _region_index(candidates: dict[str, Any]) -> dict[str, dict[int, dict[str, Any]]]:
    regions: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for result in candidates.get("results", []):
        eid = str(result.get("edge", {}).get("edge_id", ""))
        for region in result.get("direct_regions", []):
            repetition = int(region.get("repetition", -1))
            if not 3 <= repetition <= 10:
                continue
            trajectory_id = f"{eid}:{region['cycle_id']}:L{int(region['sequence_line'])}"
            regions[trajectory_id][repetition] = region
    return regions


def _stable_tree(formulas: dict[str, Any], io: str) -> dict[str, Any]:
    item = formulas.get("new_stable_inference", {}).get("by_input_output", {}).get(io)
    if not item or item.get("status") != "inferred" or len(item.get("final_candidates", [])) != 1:
        raise ValueError(f"expected one completed new-stable tree for {io}")
    candidate = item["final_candidates"][0]
    verification = candidate.get("verification", {})
    if not verification.get("exact"):
        raise ValueError(f"completed tree is not exact for {io}")
    return candidate


def _existing_stable_eids(formulas: dict[str, Any], io: str) -> set[str]:
    item = formulas["new_stable_inference"]["by_input_output"][io]
    ids = item.get("old_member_ids", []) + item.get("new_member_ids", [])
    return {_trajectory_eid(str(trajectory_id)) for trajectory_id in ids}


def _complete(points: list[dict[str, Any]]) -> bool:
    return [int(point["repetition"]) for point in points] == list(range(3, 11))


def _validate_points(tree: dict[str, Any], points: Iterable[dict[str, Any]]) -> dict[str, Any]:
    failures = []
    matched = 0
    sample_count = 0
    for point in points:
        sample_count += 1
        predicted = _evaluate_update_tree(tree, point)
        actual = int(point["r_after"])
        if predicted == actual:
            matched += 1
        else:
            failures.append(
                {
                    "repetition": int(point["repetition"]),
                    "r_before": int(point["r_before"]),
                    "r_i": int(point["r_i"]),
                    "actual_r_after": actual,
                    "predicted_r_after": predicted,
                }
            )
    return {
        "exact": not failures,
        "matched_sample_count": matched,
        "sample_count": sample_count,
        "failures": failures,
    }


def _vertical_preference(formulas: dict[str, Any], io: str) -> dict[str, Any]:
    projection = formulas["stable_aggregation"]["by_input_output"][io]["projections"]["input_after"]
    components = [item for item in projection.get("vertical_components", []) if item.get("strength") == "core"]
    if len(components) != 1:
        raise ValueError(f"expected one core input-after vertical component for {io}")
    component = components[0]
    return {
        "projection": "input_after",
        "x": int(component["x"]),
        "distinct_y": [int(value) for value in component["distinct_y"]],
        "support_count": int(component["support_count"]),
        "selection_policy": "select_vertical_component_x_from_each_set_valued_preimage",
    }


def _edge_results(candidates: dict[str, Any], selected_ios: set[str]) -> dict[str, dict[str, Any]]:
    output = {}
    for result in candidates.get("results", []):
        edge = result.get("edge", {})
        if edge and _io(edge) in selected_ios:
            output[str(edge["edge_id"])] = result
    return output


def _reconstruct_predecessors(
    candidates: dict[str, Any],
    formulas: dict[str, Any],
    trace: Iterable[dict[str, Any]],
    config: dict[str, Any],
    terminal_trajectories: list[dict[str, Any]],
    tree: dict[str, Any],
    vertical: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    regions = _region_index(candidates)
    carried = replay_input_register(trace, config)
    selected_value = int(vertical["x"])
    reconstructed = []
    by_predecessor: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for trajectory in sorted(terminal_trajectories, key=lambda item: item["id"]):
        if not _complete(trajectory["points"]):
            raise ValueError(f"incomplete R3-R10 trajectory {trajectory['id']}")
        by_rep = regions.get(trajectory["id"], {})
        if set(by_rep) != set(range(3, 11)):
            raise ValueError(f"missing source regions for {trajectory['id']}")
        if any(int(by_rep[rep].get("region_edge_count", 0)) != 2 for rep in range(3, 11)):
            raise ValueError(f"reverse closure only accepts length-two regions: {trajectory['id']}")

        predecessor_events = [by_rep[rep]["region_edges"][-2] for rep in range(3, 11)]
        predecessor_ids = {str(event["edge"]["edge_id"]) for event in predecessor_events}
        if len(predecessor_ids) != 1:
            raise ValueError(f"mixed predecessor EIDs in {trajectory['id']}")
        predecessor_eid = next(iter(predecessor_ids))
        predecessor_edge = predecessor_events[0]["edge"]
        points = []
        preimage_samples = []
        for terminal_point, predecessor_event in zip(trajectory["points"], predecessor_events):
            repetition = int(terminal_point["repetition"])
            region = by_rep[repetition]
            allowed = preimage_values(tree, terminal_point, list(range(8)))
            if selected_value not in allowed:
                raise ValueError(
                    f"vertical preference {selected_value} is not a preimage at {trajectory['id']}:R{repetition}"
                )
            trace_line = int(predecessor_event["trace_line"])
            replayed_input = carried.get(trace_line)
            event_input = (predecessor_event.get("input_register_values") or {}).get("ngksi_uplink", {}).get("value")
            if replayed_input is None or (
                event_input is not None and int(event_input) != int(replayed_input)
            ):
                raise ValueError(f"predecessor input replay mismatch at trace line {trace_line}")
            point = {
                "repetition": repetition,
                "r_before": int(region["previous_output"]["value"]),
                "r_i": int(replayed_input),
                "r_after": selected_value,
                "r_before_source": "direct_region_start",
                "r_i_source": "frozen_trace_replay",
                "r_after_source": "reverse_preimage_vertical_x_preference",
                "predecessor_trace_line": trace_line,
            }
            points.append(point)
            preimage_samples.append(
                {
                    "repetition": repetition,
                    "allowed_r_after_values": allowed,
                    "selected_r_after": selected_value,
                }
            )
        reconstructed_id = f"{predecessor_eid}:{trajectory['cycle_id']}:L{trajectory['sequence_line']}"
        predecessor_trajectory = {
            "id": reconstructed_id,
            "eid": predecessor_eid,
            "edge": predecessor_edge,
            "cycle_id": trajectory["cycle_id"],
            "sequence_line": int(trajectory["sequence_line"]),
            "terminal_trajectory_id": trajectory["id"],
            "terminal_edge": trajectory["edge"],
            "signal_context": trajectory.get("signal_context", {}),
            "points": points,
        }
        by_predecessor[predecessor_eid].append(predecessor_trajectory)
        reconstructed.append(
            {
                "terminal_trajectory_id": trajectory["id"],
                "terminal_edge": trajectory["edge"],
                "predecessor_trajectory_id": reconstructed_id,
                "predecessor_edge": predecessor_edge,
                "signal_context": trajectory.get("signal_context", {}),
                "mathematical_preimages": preimage_samples,
                "preference_source": vertical,
                "selected_value": selected_value,
                "predecessor_points": points,
            }
        )

    fitted = {}
    for predecessor_eid, trajectories in sorted(by_predecessor.items()):
        projections = {
            name: discover_projection(trajectories, axis)
            for name, axis in AXES.items()
        }
        fitted[predecessor_eid] = {
            "eid": predecessor_eid,
            "edge": trajectories[0]["edge"],
            "trajectory_ids": [item["id"] for item in trajectories],
            "terminal_trajectory_ids": [item["terminal_trajectory_id"] for item in trajectories],
            "projections": projections,
        }
    return reconstructed, fitted


def analyze_full_model_stable_migration(
    candidates: dict[str, Any],
    formulas: dict[str, Any],
    trace: Iterable[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Return the deterministic H14 full-model migration audit."""
    selected_ios = set(TARGET_INPUT_OUTPUTS)
    edges = _edge_results(candidates, selected_ios)
    trajectories = [
        item for item in formulas.get("trajectories", [])
        if f"{item['logical_input']}/{item['logical_output']}" in selected_ios
    ]
    by_eid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trajectory in trajectories:
        by_eid[str(trajectory["eid"])].append(trajectory)
    regions = _region_index(candidates)

    output_by_io = {}
    reverse_targets = []
    for io in TARGET_INPUT_OUTPUTS:
        candidate = _stable_tree(formulas, io)
        tree = candidate["update_tree"]
        existing = _existing_stable_eids(formulas, io)
        edge_items = []
        for eid, result in sorted(edges.items()):
            edge = result["edge"]
            if _io(edge) != io:
                continue
            edge_trajectories = sorted(by_eid.get(eid, []), key=lambda item: item["id"])
            validations = [
                {
                    "trajectory_id": item["id"],
                    "cycle_id": item["cycle_id"],
                    "sequence_line": item["sequence_line"],
                    "signal_context": item.get("signal_context", {}),
                    "region_edge_count": len(
                        next(iter(regions.get(item["id"], {}).values()))
                        .get("region_edges", [])
                    ) if regions.get(item["id"]) else None,
                    "points": item["points"],
                    "validation": _validate_points(tree, item["points"]),
                }
                for item in edge_trajectories
            ]
            direct_exact = bool(validations) and all(item["validation"]["exact"] for item in validations)
            all_length_two = bool(validations) and all(item["region_edge_count"] == 2 for item in validations)
            if eid in existing:
                status = "existing_stable_inference"
            elif direct_exact:
                status = "stable_inference_migration"
            elif io == "registrationRequest/authenticationRequest" and all_length_two:
                status = "reverse_closure_pending"
                reverse_targets.extend(edge_trajectories)
            else:
                status = "temporarily_not_migrated"
            edge_items.append(
                {
                    "eid": eid,
                    "edge": edge,
                    "status": status,
                    "trajectory_count": len(edge_trajectories),
                    "sample_count": sum(len(item["points"]) for item in edge_trajectories),
                    "direct_validation": {
                        "exact": direct_exact,
                        "matched_sample_count": sum(v["validation"]["matched_sample_count"] for v in validations),
                        "sample_count": sum(v["validation"]["sample_count"] for v in validations),
                    },
                    "trajectories": validations,
                }
            )
        output_by_io[io] = {
            "formula": candidate["formula"],
            "update_tree": tree,
            "existing_stable_eids": sorted(existing),
            "edges": edge_items,
        }

    ordinary_io = "registrationRequest/authenticationRequest"
    ordinary_tree = output_by_io[ordinary_io]["update_tree"]
    vertical = _vertical_preference(formulas, ordinary_io)
    reconstructed, predecessor_fits = _reconstruct_predecessors(
        candidates, formulas, trace, config, reverse_targets, ordinary_tree, vertical
    )
    closure_by_terminal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in reconstructed:
        closure_by_terminal[str(item["terminal_edge"]["edge_id"])].append(item)

    for edge_item in output_by_io[ordinary_io]["edges"]:
        if edge_item["status"] != "reverse_closure_pending":
            continue
        closure = closure_by_terminal[edge_item["eid"]]
        terminal_validations = []
        for item in closure:
            trajectory = next(t for t in by_eid[edge_item["eid"]] if t["id"] == item["terminal_trajectory_id"])
            adjusted = [
                {**point, "r_before": int(item["selected_value"])}
                for point in trajectory["points"]
            ]
            terminal_validations.append(
                {
                    "trajectory_id": trajectory["id"],
                    "selected_predecessor_r_after": int(item["selected_value"]),
                    "adjusted_points": adjusted,
                    "validation": _validate_points(ordinary_tree, adjusted),
                }
            )
        if not terminal_validations or not all(item["validation"]["exact"] for item in terminal_validations):
            raise ValueError(f"reverse closure did not validate {edge_item['eid']}")
        edge_item["status"] = "stable_inference_migration"
        edge_item["reverse_closure"] = {
            "trajectory_count": len(terminal_validations),
            "sample_count": sum(item["validation"]["sample_count"] for item in terminal_validations),
            "matched_sample_count": sum(item["validation"]["matched_sample_count"] for item in terminal_validations),
            "validations": terminal_validations,
        }

    for io, item in output_by_io.items():
        migrated = [edge for edge in item["edges"] if edge["status"] == "stable_inference_migration"]
        pending = [edge for edge in item["edges"] if edge["status"] == "temporarily_not_migrated"]
        covered = [edge for edge in item["edges"] if edge["status"] != "temporarily_not_migrated"]
        item["counts"] = {
            "edge_count": len(item["edges"]),
            "covered_edge_count": len(covered),
            "migration_edge_count": len(migrated),
            "temporarily_not_migrated_count": len(pending),
        }
        item["migration_eids"] = [edge["eid"] for edge in migrated]
        item["temporarily_not_migrated_eids"] = [edge["eid"] for edge in pending]

    return {
        "method": "trajectory_classification_algorithm_b_full_model_stable_inference_migration",
        "scope": list(TARGET_INPUT_OUTPUTS),
        "reader_status_labels": {
            "stable_inference_migration": "稳定性推断（迁移）",
            "temporarily_not_migrated": "暂不迁移",
        },
        "by_input_output": output_by_io,
        "reverse_closure": {
            "value_domain": list(range(8)),
            "vertical_preference": vertical,
            "trajectory_count": len(reconstructed),
            "trajectories": reconstructed,
            "predecessor_fits": predecessor_fits,
        },
    }
