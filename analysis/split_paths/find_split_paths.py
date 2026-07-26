from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


STATE_RE = re.compile(r"\b(s\d+)\s*\[")
EDGE_RE = re.compile(
    r'\b(s\d+)\s*->\s*(s\d+)\s*\[\s*label="([^"]+)"[^\]]*\]'
)


def state_key(state: str) -> int:
    return int(state[1:])


def split_label(label: str) -> tuple[list[str], str]:
    if " / " in label:
        inputs, output = label.split(" / ", 1)
    elif "/" in label:
        inputs, output = label.split("/", 1)
    else:
        raise ValueError(f"edge label has no input/output separator: {label!r}")
    return [item.strip() for item in inputs.split(" | ") if item.strip()], output.strip()


def parse_dot(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    states = set(STATE_RE.findall(text))
    outgoing: dict[str, list[dict[str, str]]] = defaultdict(list)
    incoming: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()

    for src, dst, label in EDGE_RE.findall(text):
        inputs, output = split_label(label)
        states.update((src, dst))
        for input_symbol in inputs:
            key = (src, input_symbol)
            if key in seen:
                raise ValueError(f"non-deterministic transition: {key}")
            seen.add(key)
            edge = {
                "src": src,
                "dst": dst,
                "input": input_symbol,
                "output": output,
            }
            outgoing[src].append(edge)
            incoming[dst].append(edge)

    if not states:
        raise ValueError(f"no states found in {path}")

    return {
        "states": sorted(states, key=state_key),
        "outgoing": dict(outgoing),
        "incoming": dict(incoming),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def bfs_shortest_trace(
    model: dict[str, Any], start: str, target: str
) -> list[dict[str, str]] | None:
    if start not in model["states"]:
        raise ValueError(f"start state does not exist: {start}")
    if target not in model["states"]:
        raise ValueError(f"target state does not exist: {target}")

    predecessor: dict[str, tuple[str, dict[str, str]]] = {}
    visited = {start}
    queue = deque([start])

    while queue:
        state = queue.popleft()
        if state == target:
            break
        for edge in model["outgoing"].get(state, []):
            dst = edge["dst"]
            if dst in visited:
                continue
            visited.add(dst)
            predecessor[dst] = (state, edge)
            queue.append(dst)

    if target not in visited:
        return None

    trace: list[dict[str, str]] = []
    current = target
    while current != start:
        previous, edge = predecessor[current]
        trace.append(edge)
        current = previous
    trace.reverse()
    return trace


def state_sequence(start: str, trace: list[dict[str, str]]) -> list[str]:
    return [start, *[edge["dst"] for edge in trace]]


def input_sequence_text(trace: list[dict[str, str]]) -> str:
    inputs = [edge["input"] for edge in trace]
    return " ".join(inputs) if inputs else "(空序列)"


def trace_payload(start: str, trace: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "length": len(trace),
        "input_sequence": [edge["input"] for edge in trace],
        "input_sequence_text": input_sequence_text(trace),
        "output_sequence": [edge["output"] for edge in trace],
        "state_sequence": state_sequence(start, trace),
        "trace": trace,
    }


def reverse_bfs_intersection(
    model: dict[str, Any],
    shorter_target: str,
    longer_states: list[str],
    excluded: set[str],
) -> dict[str, Any]:
    longer_position = {state: index for index, state in enumerate(longer_states)}
    visited = {shorter_target}
    queue = deque([shorter_target])
    distance = {shorter_target: 0}
    next_toward_shorter: dict[str, tuple[str, dict[str, str]]] = {}

    while queue:
        layer_distance = distance[queue[0]]
        layer: list[str] = []
        while queue and distance[queue[0]] == layer_distance:
            layer.append(queue.popleft())

        candidates = [
            state
            for state in layer
            if state in longer_position and state not in excluded
        ]
        if candidates:
            intersection = max(candidates, key=longer_position.__getitem__)
            trace: list[dict[str, str]] = []
            current = intersection
            while current != shorter_target:
                next_state, edge = next_toward_shorter[current]
                trace.append(edge)
                current = next_state
            return {
                "found": True,
                "distance": layer_distance,
                "layer_candidates": candidates,
                "intersection": intersection,
                "intersection_index_on_r": longer_position[intersection],
                "trace_to_shorter": trace,
            }

        for state in layer:
            for edge in model["incoming"].get(state, []):
                predecessor = edge["src"]
                if predecessor in visited:
                    continue
                visited.add(predecessor)
                distance[predecessor] = layer_distance + 1
                next_toward_shorter[predecessor] = (state, edge)
                queue.append(predecessor)

    return {"found": False, "reason": "no_non_excluded_intersection"}


def backward_common_transition_suffix(
    first_trace: list[dict[str, str]],
    second_trace: list[dict[str, str]],
) -> dict[str, Any]:
    matched_reversed: list[dict[str, str]] = []
    for first_edge, second_edge in zip(
        reversed(first_trace), reversed(second_trace)
    ):
        first_label = (first_edge["input"], first_edge["output"])
        second_label = (second_edge["input"], second_edge["output"])
        if first_label != second_label:
            break
        matched_reversed.append(
            {
                "input": first_edge["input"],
                "output": first_edge["output"],
            }
        )
    matched = list(reversed(matched_reversed))
    input_sequence = [item["input"] for item in matched]
    return {
        "comparison": "contiguous_transition_labels_from_end",
        "label_fields": ["input", "output"],
        "length": len(matched),
        "input_sequence": input_sequence,
        "input_sequence_text": (
            " ".join(input_sequence) if input_sequence else "(空序列)"
        ),
        "output_sequence": [item["output"] for item in matched],
        "labels": matched,
    }


def transition_labels(trace: list[dict[str, str]]) -> list[tuple[str, str]]:
    return [(edge["input"], edge["output"]) for edge in trace]


def reframe_zero_common_tail(
    candidate: dict[str, Any],
    start: str,
) -> dict[str, Any]:
    common_prefix_trace = candidate["common_prefix"]["trace"]
    shorter_suffix = candidate["distinguishing_suffixes"]["to_shorter"][
        "trace"
    ]
    longer_suffix = candidate["distinguishing_suffixes"]["to_longer"]["trace"]
    shorter_access = [*common_prefix_trace, *shorter_suffix]
    longer_access = [*common_prefix_trace, *longer_suffix]
    tail_length = len(longer_suffix)

    base = {
        "basis_candidate_id": candidate["candidate_id"],
        "decomposition": {
            "shorter": "common_prefix + common_tail",
            "longer": "common_prefix + longer_only_middle + common_tail",
        },
        "comparison": "contiguous_transition_labels",
        "label_fields": ["input", "output"],
    }
    if tail_length == 0:
        return {
            **base,
            "status": "not_reframed",
            "reason": "longer_suffix_is_empty",
        }
    if tail_length > len(shorter_access):
        return {
            **base,
            "status": "not_reframed",
            "reason": "longer_suffix_longer_than_shorter_access",
        }

    shorter_prefix = shorter_access[:-tail_length]
    shorter_tail = shorter_access[-tail_length:]
    if transition_labels(shorter_tail) != transition_labels(longer_suffix):
        return {
            **base,
            "status": "not_reframed",
            "reason": "longer_suffix_not_a_tail_of_shorter_access",
        }
    if len(longer_access) < len(shorter_prefix) + tail_length:
        return {
            **base,
            "status": "not_reframed",
            "reason": "longer_access_too_short_for_prefix_tail_decomposition",
        }

    longer_prefix = longer_access[: len(shorter_prefix)]
    if transition_labels(shorter_prefix) != transition_labels(longer_prefix):
        return {
            **base,
            "status": "not_reframed",
            "reason": "shorter_prefix_not_a_prefix_of_longer_access",
        }

    longer_middle = longer_access[len(shorter_prefix) : -tail_length]
    if not longer_middle:
        return {
            **base,
            "status": "not_reframed",
            "reason": "longer_only_middle_is_empty",
        }

    common_prefix_end = state_sequence(start, shorter_prefix)[-1]
    longer_middle_end = state_sequence(
        common_prefix_end, longer_middle
    )[-1]
    return {
        **base,
        "status": "reframed",
        "common_prefix": {
            "end_state": common_prefix_end,
            **trace_payload(start, shorter_prefix),
        },
        "longer_only_middle": trace_payload(
            common_prefix_end, longer_middle
        ),
        "common_tail": {
            "length": tail_length,
            "input_sequence": [edge["input"] for edge in longer_suffix],
            "input_sequence_text": input_sequence_text(longer_suffix),
            "output_sequence": [edge["output"] for edge in longer_suffix],
            "to_shorter": trace_payload(
                common_prefix_end, shorter_tail
            ),
            "to_longer": trace_payload(
                longer_middle_end, longer_suffix
            ),
        },
    }


def decompose_candidate_middle(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    tail_length = candidate["backward_common_suffix"]["length"]
    if tail_length == 0:
        return {
            "status": "not_decomposed",
            "reason": "backward_common_suffix_is_empty",
        }

    common_prefix = candidate["common_prefix"]
    prefix_end = common_prefix["end_state"]
    shorter_suffix = candidate["distinguishing_suffixes"]["to_shorter"][
        "trace"
    ]
    longer_suffix = candidate["distinguishing_suffixes"]["to_longer"]["trace"]
    shorter_middle = shorter_suffix[:-tail_length]
    longer_middle = longer_suffix[:-tail_length]
    shorter_tail = shorter_suffix[-tail_length:]
    longer_tail = longer_suffix[-tail_length:]
    shorter_middle_end = state_sequence(prefix_end, shorter_middle)[-1]
    longer_middle_end = state_sequence(prefix_end, longer_middle)[-1]

    return {
        "status": "decomposed",
        "decomposition": {
            "shorter": (
                "common_prefix + shorter_middle + common_tail"
            ),
            "longer": "common_prefix + longer_middle + common_tail",
        },
        "common_prefix": common_prefix,
        "shorter_middle": trace_payload(prefix_end, shorter_middle),
        "longer_middle": trace_payload(prefix_end, longer_middle),
        "common_tail": {
            **candidate["backward_common_suffix"],
            "to_shorter": trace_payload(
                shorter_middle_end, shorter_tail
            ),
            "to_longer": trace_payload(longer_middle_end, longer_tail),
        },
    }


def pmt_payload(
    common_prefix: dict[str, Any],
    shorter_target: str,
    longer_target: str,
    shorter_middle: list[dict[str, str]],
    shorter_tail: list[dict[str, str]],
    longer_middle: list[dict[str, str]],
    longer_tail: list[dict[str, str]],
    common_tail: dict[str, Any],
) -> dict[str, Any]:
    """Build the reportable A/B P/M/T view (A=shorter, B=longer)."""
    prefix_end = common_prefix["end_state"]
    shorter_middle_end = state_sequence(prefix_end, shorter_middle)[-1]
    longer_middle_end = state_sequence(prefix_end, longer_middle)[-1]
    return {
        "branch_roles": {
            "A": {"role": "shorter", "target": shorter_target},
            "B": {"role": "longer", "target": longer_target},
        },
        "formulas": {
            "A": "P + M_A + T_A",
            "B": "P + M_B + T_B",
        },
        "P": common_prefix,
        "M_A": trace_payload(prefix_end, shorter_middle),
        "T_A": trace_payload(shorter_middle_end, shorter_tail),
        "M_B": trace_payload(prefix_end, longer_middle),
        "T_B": trace_payload(longer_middle_end, longer_tail),
        "common_tail": common_tail,
    }


def pmt_from_candidate(
    candidate: dict[str, Any],
    shorter_target: str,
    longer_target: str,
) -> dict[str, Any]:
    tail_length = candidate["backward_common_suffix"]["length"]
    prefix = candidate["common_prefix"]
    shorter_suffix = candidate["distinguishing_suffixes"]["to_shorter"][
        "trace"
    ]
    longer_suffix = candidate["distinguishing_suffixes"]["to_longer"][
        "trace"
    ]
    shorter_middle = (
        shorter_suffix[:-tail_length] if tail_length else shorter_suffix
    )
    longer_middle = (
        longer_suffix[:-tail_length] if tail_length else longer_suffix
    )
    shorter_tail = shorter_suffix[-tail_length:] if tail_length else []
    longer_tail = longer_suffix[-tail_length:] if tail_length else []
    return pmt_payload(
        prefix,
        shorter_target,
        longer_target,
        shorter_middle,
        shorter_tail,
        longer_middle,
        longer_tail,
        candidate["backward_common_suffix"],
    )


def pmt_from_reframe(
    reframe: dict[str, Any],
    shorter_target: str,
    longer_target: str,
) -> dict[str, Any]:
    common_prefix = reframe["common_prefix"]
    common_tail = reframe["common_tail"]
    prefix_end = common_prefix["end_state"]
    return {
        "branch_roles": {
            "A": {"role": "shorter", "target": shorter_target},
            "B": {"role": "longer", "target": longer_target},
        },
        "formulas": {
            "A": "P + M_A + T_A",
            "B": "P + M_B + T_B",
        },
        "P": common_prefix,
        "M_A": trace_payload(prefix_end, []),
        "T_A": common_tail["to_shorter"],
        "M_B": reframe["longer_only_middle"],
        "T_B": common_tail["to_longer"],
        "common_tail": {
            "length": common_tail["length"],
            "input_sequence": common_tail["input_sequence"],
            "input_sequence_text": common_tail["input_sequence_text"],
            "output_sequence": common_tail["output_sequence"],
        },
    }


def candidate_payload(
    candidate_id: str,
    start: str,
    shorter_target: str,
    longer_target: str,
    prefix_index: int,
    prefix_trace: list[dict[str, str]],
    shorter_suffix: list[dict[str, str]],
    longer_suffix: list[dict[str, str]],
) -> dict[str, Any]:
    shorter_length = len(shorter_suffix)
    longer_length = len(longer_suffix)
    payload = {
        "candidate_id": candidate_id,
        "prefix_index_on_r": prefix_index,
        "common_prefix": {
            "end_state": state_sequence(start, prefix_trace)[-1],
            **trace_payload(start, prefix_trace),
        },
        "distinguishing_suffixes": {
            "to_shorter": {
                "target": shorter_target,
                **trace_payload(
                    state_sequence(start, prefix_trace)[-1], shorter_suffix
                ),
            },
            "to_longer": {
                "target": longer_target,
                **trace_payload(
                    state_sequence(start, prefix_trace)[-1], longer_suffix
                ),
            },
        },
        "suffix_lengths": {
            "to_shorter": shorter_length,
            "to_longer": longer_length,
        },
        "backward_common_suffix": backward_common_transition_suffix(
            shorter_suffix, longer_suffix
        ),
    }
    payload["middle_decomposition"] = decompose_candidate_middle(payload)
    return payload


def extend_prefix_candidates(
    model: dict[str, Any],
    start: str,
    shorter_target: str,
    longer_target: str,
    longer_trace: list[dict[str, str]],
    initial_index: int,
    initial_shorter_suffix: list[dict[str, str]],
) -> dict[str, Any]:
    longer_states = state_sequence(start, longer_trace)
    candidates: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []

    for prefix_index in range(initial_index, len(longer_trace) + 1):
        prefix_trace = longer_trace[:prefix_index]
        prefix_end = longer_states[prefix_index]
        longer_suffix = longer_trace[prefix_index:]
        if prefix_index == initial_index:
            shorter_suffix = initial_shorter_suffix
        else:
            shorter_suffix = bfs_shortest_trace(
                model, prefix_end, shorter_target
            )

        evaluation: dict[str, Any] = {
            "prefix_index_on_r": prefix_index,
            "prefix_end_state": prefix_end,
            "prefix_length": prefix_index,
            "to_longer_length": len(longer_suffix),
        }
        if shorter_suffix is None:
            evaluation.update(
                {
                    "to_shorter_reachable": False,
                    "eligible": False,
                    "reason": "shorter_target_unreachable_from_prefix_end",
                }
            )
            evaluations.append(evaluation)
            continue

        shorter_length = len(shorter_suffix)
        is_initial = prefix_index == initial_index
        eligible = is_initial or shorter_length <= len(longer_suffix)
        evaluation.update(
            {
                "to_shorter_reachable": True,
                "to_shorter_length": shorter_length,
                "eligible": eligible,
                "reason": (
                    "initial_reverse_bfs_candidate"
                    if is_initial
                    else (
                        "shorter_suffix_not_longer"
                        if eligible
                        else "shorter_suffix_longer"
                    )
                ),
            }
        )
        evaluations.append(evaluation)
        if not eligible:
            continue

        candidate = candidate_payload(
            f"P{len(candidates) + 1:02d}",
            start,
            shorter_target,
            longer_target,
            prefix_index,
            prefix_trace,
            shorter_suffix,
            longer_suffix,
        )
        candidates.append(candidate)

    selection: dict[str, Any]
    if not candidates:
        selection = {
            "status": "no_eligible_candidate",
            "selected_candidate_id": None,
        }
    else:
        maximum_common_suffix_length = max(
            item["backward_common_suffix"]["length"] for item in candidates
        )
        maxima = [
            item
            for item in candidates
            if item["backward_common_suffix"]["length"]
            == maximum_common_suffix_length
        ]
        selected_maximum = max(
            maxima, key=lambda item: item["prefix_index_on_r"]
        )
        selection = {
            "status": (
                "selected_maximum_backward_common_suffix"
                if len(maxima) == 1
                else (
                    "selected_maximum_backward_common_suffix_"
                    "longest_prefix_tiebreak"
                )
            ),
            "criteria": {
                "maximum_backward_common_suffix_length": (
                    maximum_common_suffix_length
                ),
                "comparison": "contiguous_transition_labels_from_end",
                "label_fields": ["input", "output"],
                "secondary_tiebreak": {
                    "name": "maximum_common_prefix_length",
                    "equivalent": (
                        "minimum_longer_middle_length_when_common_tail_is_fixed"
                    ),
                    "selected_prefix_length": selected_maximum[
                        "common_prefix"
                    ]["length"],
                },
            },
            "selected_candidate_id": selected_maximum["candidate_id"],
            "primary_tie_candidate_ids": [
                item["candidate_id"] for item in maxima
            ],
        }

    selected = next(
        (
            item
            for item in candidates
            if item["candidate_id"] == selection["selected_candidate_id"]
        ),
        None,
    )
    maximum_common_suffix_length = (
        selection.get("criteria", {}).get(
            "maximum_backward_common_suffix_length"
        )
    )
    zero_common_tail_reframes: list[dict[str, Any]] = []
    if maximum_common_suffix_length == 0:
        primary_tie_ids = set(selection["primary_tie_candidate_ids"])
        zero_common_tail_reframes = [
            reframe_zero_common_tail(item, start)
            for item in candidates
            if item["candidate_id"] in primary_tie_ids
        ]
    return {
        "evaluations": evaluations,
        "eligible_candidates": candidates,
        "selection": selection,
        "selected_candidate": selected,
        "zero_common_tail_reframes": zero_common_tail_reframes,
    }


def analyze_split_paths(
    model: dict[str, Any],
    start: str,
    first_target: str,
    second_target: str,
    exclude_start_intersection: bool = True,
) -> dict[str, Any]:
    first_trace = bfs_shortest_trace(model, start, first_target)
    second_trace = bfs_shortest_trace(model, start, second_target)
    if first_trace is None:
        raise ValueError(f"target is unreachable from {start}: {first_target}")
    if second_trace is None:
        raise ValueError(f"target is unreachable from {start}: {second_target}")

    if len(first_trace) <= len(second_trace):
        shorter_target, shorter_trace = first_target, first_trace
        longer_target, longer_trace = second_target, second_trace
        swapped = False
    else:
        shorter_target, shorter_trace = second_target, second_trace
        longer_target, longer_trace = first_target, first_trace
        swapped = True

    longer_states = state_sequence(start, longer_trace)
    excluded = {start} if exclude_start_intersection else set()
    reverse = reverse_bfs_intersection(
        model, shorter_target, longer_states, excluded
    )

    result: dict[str, Any] = {
        "requested_targets": [first_target, second_target],
        "roles_swapped": swapped,
        "shorter": {
            "target": shorter_target,
            "shortest_access": trace_payload(start, shorter_trace),
        },
        "longer": {
            "target": longer_target,
            "shortest_access": trace_payload(start, longer_trace),
        },
        "reverse_bfs": {
            "start": shorter_target,
            "direction": "incoming_edges_reversed",
            "excluded_intersections": sorted(excluded, key=state_key),
            **reverse,
        },
    }

    if not reverse["found"]:
        result["candidate_search"] = None
        result["common_prefix"] = None
        result["distinguishing_suffixes"] = None
        return result

    intersection_index = reverse["intersection_index_on_r"]
    candidate_search = extend_prefix_candidates(
        model,
        start,
        shorter_target,
        longer_target,
        longer_trace,
        intersection_index,
        reverse["trace_to_shorter"],
    )
    result["candidate_search"] = candidate_search
    selected = candidate_search["selected_candidate"]
    result["common_prefix"] = (
        selected["common_prefix"] if selected is not None else None
    )
    result["distinguishing_suffixes"] = (
        selected["distinguishing_suffixes"]
        if selected is not None
        else None
    )
    selected_reframe = next(
        (
            item
            for item in candidate_search["zero_common_tail_reframes"]
            if item["status"] == "reframed"
            and item["basis_candidate_id"]
            == candidate_search["selection"]["selected_candidate_id"]
        ),
        None,
    )
    if selected_reframe is not None:
        result["final_result"] = {
            "mode": "pmt_reframed_common_tail",
            "basis_candidate_id": selected["candidate_id"],
            "pmt": pmt_from_reframe(
                selected_reframe, shorter_target, longer_target
            ),
            "zero_common_tail_reframe": selected_reframe,
        }
    elif selected is not None:
        tail_length = selected["backward_common_suffix"]["length"]
        selected_failed_reframe = next(
            (
                item
                for item in candidate_search["zero_common_tail_reframes"]
                if item["basis_candidate_id"] == selected["candidate_id"]
            ),
            None,
        )
        result["final_result"] = {
            "mode": (
                "pmt_common_tail" if tail_length else "pmt_empty_tail"
            ),
            "basis_candidate_id": selected["candidate_id"],
            "pmt": pmt_from_candidate(
                selected, shorter_target, longer_target
            ),
            "zero_common_tail_reframe": selected_failed_reframe,
        }
    else:
        result["final_result"] = {
            "mode": "no_selected_candidate",
            "zero_common_tail_reframes": candidate_search[
                "zero_common_tail_reframes"
            ],
        }
    return result


def render_report(payload: dict[str, Any]) -> str:
    analysis = payload["analysis"]
    shorter = analysis["shorter"]
    longer = analysis["longer"]
    lines = [
        "# Mealy 状态访问分裂路径",
        "",
        f"- 起点：`{payload['start_state']}`",
        f"- 较短目标 `l`：`{shorter['target']}`，"
        f"最短长度 {shorter['shortest_access']['length']}",
        f"- 较长目标 `r`：`{longer['target']}`，"
        f"最短长度 {longer['shortest_access']['length']}",
        f"- DOT SHA-256：`{payload['source_sha256']}`",
        "",
    ]

    reverse = analysis["reverse_bfs"]
    if not reverse["found"]:
        lines.extend(
            [
                "排除起点后，反向 BFS 未找到与 `r` 最短路径相交的节点。",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            f"首次相交层：反向距离 {reverse['distance']}；"
            f"选定交点：`{reverse['intersection']}`。",
            "",
        ]
    )
    final_result = analysis["final_result"]
    if final_result["mode"] == "no_selected_candidate":
        lines.extend(["没有可用的访问分裂候选。", ""])
        return "\n".join(lines)

    pmt = final_result["pmt"]
    selection = analysis["candidate_search"]["selection"]
    lines.extend(
        [
            "## 最终 P/M/T 分解",
            "",
            f"- 候选：`{final_result['basis_candidate_id']}`",
            f"- 公共前缀 P：`{pmt['P']['input_sequence_text']}`",
            f"- A（较短目标 `{pmt['branch_roles']['A']['target']}`）中间段 M_A：`{pmt['M_A']['input_sequence_text']}`",
            f"- A 尾段 T_A：`{pmt['T_A']['input_sequence_text']}`",
            f"- B（较长目标 `{pmt['branch_roles']['B']['target']}`）中间段 M_B：`{pmt['M_B']['input_sequence_text']}`",
            f"- B 尾段 T_B：`{pmt['T_B']['input_sequence_text']}`",
            "",
            "`A = P + M_A + T_A`",
            "",
            "`B = P + M_B + T_B`",
            "",
        ]
    )
    if len(selection["primary_tie_candidate_ids"]) > 1:
        lines.extend(
            [
                "反向最长公共尾并列；已选择公共前缀最长、"
                "即 B 中间段最短的候选。",
                "",
            ]
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find a heuristic common access prefix and two branch suffixes "
            "for a pair of deterministic Mealy states"
        )
    )
    parser.add_argument("--dot", type=Path, required=True)
    parser.add_argument(
        "--target",
        action="append",
        required=True,
        help="Target state; provide exactly twice",
    )
    parser.add_argument("--start", default="s0")
    parser.add_argument(
        "--allow-start-intersection",
        action="store_true",
        help="Allow the start state to be selected as the reverse-BFS intersection",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--basename")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if len(args.target) != 2:
        raise ValueError("provide exactly two --target values")

    model = parse_dot(args.dot)
    analysis = analyze_split_paths(
        model,
        args.start,
        args.target[0],
        args.target[1],
        exclude_start_intersection=not args.allow_start_intersection,
    )
    payload = {
        "schema_version": 5,
        "kind": "mealy_access_split_path_heuristic",
        "source_dot": str(args.dot.resolve()),
        "source_sha256": model["sha256"],
        "start_state": args.start,
        "analysis": analysis,
    }

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        basename = args.basename or (
            f"{args.dot.stem}_{args.target[0]}_{args.target[1]}_access_split"
        )
        json_path = args.output_dir / f"{basename}.json"
        report_path = args.output_dir / f"{basename}.md"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report_path.write_text(render_report(payload), encoding="utf-8")
        payload["artifacts"] = [str(json_path), str(report_path)]

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
