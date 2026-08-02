from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence


EDGE_RE = re.compile(
    r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*->\s*'
    r'([A-Za-z_][A-Za-z0-9_]*)\s*\[([^\]]*)\]\s*;',
    re.MULTILINE,
)
LABEL_RE = re.compile(r'label\s*=\s*"((?:\\.|[^"\\])*)"')
STATE_RE = re.compile(
    r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\[(?![^\]]*->)[^\]]*\]\s*;',
    re.MULTILINE,
)
STATE_NUMBER_RE = re.compile(r"^s(\d+)$")
DOT_RESERVED_IDS = frozenset({"strict", "graph", "digraph", "node", "edge", "subgraph"})
EDGE_STATEMENT_RE = re.compile(
    r"(?m)^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)\s*->\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\[([^\]]*)\]\s*;"
)
CYCLE_PALETTE = (
    "#D81B60",
    "#1E88E5",
    "#D19A00",
    "#004D40",
    "#F4511E",
    "#7E57C2",
    "#43A047",
    "#6D4C41",
    "#00ACC1",
    "#7A8B00",
    "#3949AB",
    "#FB8C00",
    "#8E24AA",
    "#00897B",
    "#5E35B1",
    "#546E7A",
)
SignalMode = Literal["any", "input-and-output", "output-only"]
SIGNAL_MODES = frozenset(("any", "input-and-output", "output-only"))
MergedInputPolicy = Literal["first", "expand"]
MERGED_INPUT_POLICIES = frozenset(("first", "expand"))


class CycleCoverError(RuntimeError):
    pass


@dataclass(frozen=True)
class Transition:
    src: str
    dst: str
    label: str
    inputs: tuple[str, ...]
    output: str
    order: int
    kind: str = "closure"

    @property
    def pair(self) -> tuple[str, str]:
        return (self.src, self.dst)

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.kind, self.src, self.dst, self.label)


@dataclass(frozen=True)
class DotModel:
    path: Path
    states: tuple[str, ...]
    edges: tuple[Transition, ...]


@dataclass(frozen=True)
class CandidateCycle:
    candidate_id: str
    nodes: tuple[str, ...]
    edges: tuple[Transition, ...]
    target_ids: frozenset[str]
    signal_edge_indexes: tuple[int, ...]

    @property
    def length(self) -> int:
        return len(self.edges)

    @property
    def edge_identities(self) -> tuple[tuple[str, str, str, str], ...]:
        return tuple(edge.identity for edge in self.edges)

    @property
    def walk_type(self) -> str:
        if len(set(self.nodes)) == len(self.nodes):
            return "simple_directed_cycle"
        return "composite_closed_walk"


@dataclass(frozen=True)
class AnalysisResult:
    target_model: DotModel
    closure_model: DotModel
    target_edges: tuple[Transition, ...]
    candidates: tuple[CandidateCycle, ...]
    selected: tuple[CandidateCycle, ...]
    minimum_max_length: int
    repeated_edge_uses: int
    total_length: int
    edge_usage: Counter[tuple[str, str, str, str]]
    required_inputs: frozenset[str]
    required_outputs: frozenset[str]
    signal_mode: SignalMode
    used_closed_walk_fallback: bool
    excluded_states: frozenset[str]


def state_key(state: str) -> tuple[int, int | str, str]:
    match = STATE_NUMBER_RE.fullmatch(state)
    if match:
        return (0, int(match.group(1)), state)
    return (1, state, state)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_label(label: str) -> tuple[tuple[str, ...], str]:
    if " / " in label:
        input_text, output = label.split(" / ", 1)
    elif "/" in label:
        input_text, output = label.split("/", 1)
    else:
        input_text, output = label, ""
    inputs = tuple(part.strip() for part in input_text.split("|") if part.strip())
    return inputs, output.strip()


def unescape_dot_label(value: str) -> str:
    return value.replace(r"\"", '"').replace(r"\\", "\\")


def parse_dot(path: Path) -> DotModel:
    resolved = path.resolve()
    text = resolved.read_text(encoding="utf-8")
    states: set[str] = {
        state
        for state in STATE_RE.findall(text)
        if state.lower() not in DOT_RESERVED_IDS
    }
    edges: list[Transition] = []
    for order, match in enumerate(EDGE_RE.finditer(text)):
        src, dst, attributes = match.groups()
        label_match = LABEL_RE.search(attributes)
        if not label_match:
            continue
        label = unescape_dot_label(label_match.group(1))
        inputs, output = split_label(label)
        edges.append(
            Transition(
                src=src,
                dst=dst,
                label=label,
                inputs=inputs,
                output=output,
                order=order,
            )
        )
        states.update((src, dst))
    if not edges:
        raise CycleCoverError(f"No labelled transitions found in DOT: {resolved}")
    return DotModel(
        path=resolved,
        states=tuple(sorted(states, key=state_key)),
        edges=tuple(edges),
    )


def is_synthetic_state(state: str) -> bool:
    return state.startswith("__start")


def filter_model(model: DotModel, excluded_states: frozenset[str]) -> DotModel:
    excluded = set(excluded_states)
    states = tuple(
        state
        for state in model.states
        if state not in excluded and not is_synthetic_state(state)
    )
    allowed = set(states)
    edges = tuple(
        edge
        for edge in model.edges
        if edge.src in allowed
        and edge.dst in allowed
        and not is_synthetic_state(edge.src)
        and not is_synthetic_state(edge.dst)
    )
    return DotModel(path=model.path, states=states, edges=edges)


def state_number(state: str) -> int:
    match = STATE_NUMBER_RE.fullmatch(state)
    if not match:
        raise CycleCoverError(
            "Cycle sequence export requires numeric state names such as s0; "
            f"got {state!r}."
        )
    return int(match.group(1))


def rotate_candidate_to_minimum_state(
    candidate: CandidateCycle,
) -> tuple[str, tuple[str, ...], tuple[Transition, ...]]:
    if not candidate.nodes or not candidate.edges:
        raise CycleCoverError(
            f"Candidate {candidate.candidate_id} has no closed route."
        )
    if len(candidate.nodes) != len(candidate.edges):
        raise CycleCoverError(
            f"Candidate {candidate.candidate_id} has mismatched nodes and edges."
        )
    minimum_index = min(
        range(len(candidate.nodes)),
        key=lambda index: state_number(candidate.nodes[index]),
    )
    rotated_nodes = (
        candidate.nodes[minimum_index:] + candidate.nodes[:minimum_index]
    )
    rotated_edges = (
        candidate.edges[minimum_index:] + candidate.edges[:minimum_index]
    )
    for index, edge in enumerate(rotated_edges):
        expected_src = rotated_nodes[index]
        expected_dst = rotated_nodes[(index + 1) % len(rotated_nodes)]
        if edge.src != expected_src or edge.dst != expected_dst:
            raise CycleCoverError(
                f"Candidate {candidate.candidate_id} is not a contiguous "
                f"closed route at edge {index}: {edge.src}->{edge.dst}, "
                f"expected {expected_src}->{expected_dst}."
            )
    return rotated_nodes[0], rotated_nodes, rotated_edges


def build_deterministic_input_graph(
    model: DotModel,
) -> tuple[
    dict[str, tuple[tuple[str, Transition], ...]],
    dict[tuple[str, str], Transition],
]:
    outgoing: dict[str, list[tuple[str, Transition]]] = defaultdict(list)
    transition_by_input: dict[tuple[str, str], Transition] = {}
    for edge in sorted(model.edges, key=lambda item: item.order):
        if not edge.inputs:
            raise CycleCoverError(
                f"Transition {edge.src}->{edge.dst} has no input symbol: "
                f"{edge.label!r}."
            )
        for input_symbol in edge.inputs:
            key = (edge.src, input_symbol)
            if key in transition_by_input:
                raise CycleCoverError(
                    "Non-deterministic closure DOT transition for "
                    f"({edge.src}, {input_symbol})."
                )
            transition_by_input[key] = edge
            outgoing[edge.src].append((input_symbol, edge))
    return (
        {state: tuple(edges) for state, edges in outgoing.items()},
        transition_by_input,
    )


def shortest_access_traces(
    model: DotModel,
    start_state: str,
    target_states: Iterable[str],
) -> dict[str, tuple[tuple[str, Transition], ...]]:
    if start_state not in model.states:
        raise CycleCoverError(
            f"Sequence start state does not exist: {start_state}."
        )
    outgoing, _ = build_deterministic_input_graph(model)
    predecessor: dict[str, tuple[str, str, Transition]] = {}
    visited = {start_state}
    queue = deque([start_state])
    while queue:
        state = queue.popleft()
        for input_symbol, edge in outgoing.get(state, ()):
            if edge.dst in visited:
                continue
            visited.add(edge.dst)
            predecessor[edge.dst] = (state, input_symbol, edge)
            queue.append(edge.dst)

    traces: dict[str, tuple[tuple[str, Transition], ...]] = {}
    for target_state in target_states:
        if target_state not in model.states:
            raise CycleCoverError(
                f"Cycle start state does not exist in closure DOT: "
                f"{target_state}."
            )
        if target_state not in visited:
            raise CycleCoverError(
                f"Cycle start state {target_state} is unreachable from "
                f"{start_state}."
            )
        reversed_trace: list[tuple[str, Transition]] = []
        current = target_state
        while current != start_state:
            previous, input_symbol, edge = predecessor[current]
            reversed_trace.append((input_symbol, edge))
            current = previous
        traces[target_state] = tuple(reversed(reversed_trace))
    return traces


def build_sequence_export(
    result: AnalysisResult,
    start_state: str,
    repeat_count: int,
    merged_input_policy: MergedInputPolicy,
) -> tuple[list[str], dict[str, Any]]:
    if repeat_count < 1:
        raise CycleCoverError("--sequence-repeat-count must be positive.")
    if merged_input_policy not in MERGED_INPUT_POLICIES:
        raise CycleCoverError(
            "Unknown merged-input policy: "
            f"{merged_input_policy!r}; expected first or expand."
        )

    selected_ids, _, _ = selected_cycle_metadata(result)
    rotated: list[
        tuple[CandidateCycle, str, tuple[str, ...], tuple[Transition, ...]]
    ] = []
    for candidate in result.selected:
        cycle_start, nodes, edges = rotate_candidate_to_minimum_state(candidate)
        rotated.append((candidate, cycle_start, nodes, edges))

    access_traces = shortest_access_traces(
        result.closure_model,
        start_state,
        dict.fromkeys(item[1] for item in rotated),
    )
    _, transition_by_input = build_deterministic_input_graph(
        result.closure_model
    )

    lines: list[str] = []
    cycle_entries: list[dict[str, Any]] = []
    all_lines_closed = True
    for candidate, cycle_start, nodes, edges in rotated:
        cycle_id = selected_ids[candidate.candidate_id]
        prefix_trace = access_traces[cycle_start]
        prefix_inputs = [input_symbol for input_symbol, _ in prefix_trace]
        prefix_states = [
            start_state,
            *[edge.dst for _, edge in prefix_trace],
        ]
        input_options: list[tuple[str, ...]] = []
        for edge in edges:
            if not edge.inputs:
                raise CycleCoverError(
                    f"Candidate {candidate.candidate_id} contains an edge "
                    f"without inputs: {edge.label!r}."
                )
            input_options.append(
                edge.inputs
                if merged_input_policy == "expand"
                else (edge.inputs[0],)
            )

        first_line = len(lines) + 1
        variants: list[dict[str, Any]] = []
        for loop_inputs_tuple in itertools.product(*input_options):
            loop_inputs = list(loop_inputs_tuple)
            current = cycle_start
            for _ in range(repeat_count):
                for expected_edge, input_symbol in zip(edges, loop_inputs):
                    actual_edge = transition_by_input.get(
                        (current, input_symbol)
                    )
                    if actual_edge is None:
                        raise CycleCoverError(
                            "Cycle sequence uses an undefined transition: "
                            f"({current}, {input_symbol})."
                        )
                    if (
                        current != expected_edge.src
                        or actual_edge.dst != expected_edge.dst
                    ):
                        raise CycleCoverError(
                            "Concrete cycle input does not follow the selected "
                            f"route: ({current}, {input_symbol}) reaches "
                            f"{actual_edge.dst}, expected {expected_edge.dst}."
                        )
                    current = actual_edge.dst
            if current != cycle_start:
                all_lines_closed = False
                raise CycleCoverError(
                    f"Generated line for {cycle_id} does not return to "
                    f"{cycle_start}."
                )
            tokens = prefix_inputs + loop_inputs * repeat_count
            line_text = " ".join(tokens)
            if (
                not line_text
                or line_text != line_text.strip()
                or "  " in line_text
            ):
                raise CycleCoverError(
                    f"Generated invalid sequence formatting for {cycle_id}."
                )
            lines.append(line_text)
            variants.append(
                {
                    "line_number": len(lines),
                    "loop_inputs": loop_inputs,
                    "input_count": len(tokens),
                }
            )
        cycle_entries.append(
            {
                "cycle_id": cycle_id,
                "candidate_id": candidate.candidate_id,
                "cycle_start_state": cycle_start,
                "rotated_nodes": list(nodes) + [nodes[0]],
                "prefix_state_sequence": prefix_states,
                "prefix_inputs": prefix_inputs,
                "prefix_length": len(prefix_inputs),
                "loop_length": len(edges),
                "variant_count": len(variants),
                "first_line": first_line,
                "last_line": len(lines),
                "variants": variants,
            }
        )

    return lines, {
        "start_state": start_state,
        "repeat_count": repeat_count,
        "merged_input_policy": merged_input_policy,
        "line_count": len(lines),
        "cycle_count": len(result.selected),
        "cycles": cycle_entries,
        "validation": {
            "all_cycle_starts_reachable": True,
            "all_concrete_transitions_defined": True,
            "all_lines_close_after_repetition": all_lines_closed,
            "excluded_states_absent_from_access_graph": all(
                state not in result.closure_model.states
                for state in result.excluded_states
            ),
            "single_space_delimited_nonempty_lines": all(
                line and line == line.strip() and "  " not in line
                for line in lines
            ),
        },
    }


def write_sequence_export(
    result: AnalysisResult,
    output_path: Path,
    start_state: str,
    repeat_count: int,
    merged_input_policy: MergedInputPolicy,
    overwrite: bool,
) -> dict[str, Any]:
    resolved = output_path.resolve()
    if resolved.exists() and not overwrite:
        raise CycleCoverError(
            "Sequence output already exists; pass --overwrite to replace it: "
            f"{resolved}"
        )
    lines, metadata = build_sequence_export(
        result,
        start_state=start_state,
        repeat_count=repeat_count,
        merged_input_policy=merged_input_policy,
    )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(resolved.name + ".tmp")
    try:
        temporary.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, resolved)
    finally:
        if temporary.exists():
            temporary.unlink()
    metadata.update(
        {
            "path": str(resolved),
            "bytes": resolved.stat().st_size,
            "sha256": sha256_file(resolved),
        }
    )
    return metadata


def transition_hits_required_input(
    edge: Transition,
    required_inputs: frozenset[str],
) -> bool:
    return bool(required_inputs.intersection(edge.inputs))


def transition_hits_required_output(
    edge: Transition,
    required_outputs: frozenset[str],
) -> bool:
    return bool(edge.output and edge.output in required_outputs)


def transition_hits_required_signal(
    edge: Transition,
    required_inputs: frozenset[str],
    required_outputs: frozenset[str],
    signal_mode: SignalMode,
) -> bool:
    input_hit = transition_hits_required_input(edge, required_inputs)
    output_hit = transition_hits_required_output(edge, required_outputs)
    if signal_mode == "output-only":
        return output_hit
    if signal_mode == "input-and-output":
        return input_hit or output_hit
    if signal_mode == "any":
        return input_hit or output_hit
    raise CycleCoverError(f"Unknown signal mode: {signal_mode}")


def signal_edge_indexes_for_candidate(
    edges: Sequence[Transition],
    required_inputs: frozenset[str],
    required_outputs: frozenset[str],
    signal_mode: SignalMode,
) -> tuple[int, ...]:
    return tuple(
        index
        for index, edge in enumerate(edges)
        if transition_hits_required_signal(
            edge, required_inputs, required_outputs, signal_mode
        )
    )


def candidate_satisfies_signal(
    edges: Sequence[Transition],
    required_inputs: frozenset[str],
    required_outputs: frozenset[str],
    signal_mode: SignalMode,
) -> bool:
    if not required_inputs and not required_outputs:
        return True
    if signal_mode == "output-only":
        return any(
            transition_hits_required_output(edge, required_outputs)
            for edge in edges
        )
    if signal_mode == "input-and-output":
        return any(
            transition_hits_required_input(edge, required_inputs)
            for edge in edges
        ) and any(
            transition_hits_required_output(edge, required_outputs)
            for edge in edges
        )
    if signal_mode == "any":
        return any(
            transition_hits_required_signal(
                edge, required_inputs, required_outputs, signal_mode
            )
            for edge in edges
        )
    raise CycleCoverError(f"Unknown signal mode: {signal_mode}")


def enumerate_simple_cycles(
    states: Sequence[str],
    adjacency: dict[str, tuple[str, ...]],
    max_candidates: int,
) -> list[tuple[str, ...]]:
    ordered_states = tuple(sorted(states, key=state_key))
    rank = {state: index for index, state in enumerate(ordered_states)}
    cycles: list[tuple[str, ...]] = []

    def record(path: list[str]) -> None:
        cycles.append(tuple(path))
        if len(cycles) > max_candidates:
            raise CycleCoverError(
                "Simple-cycle enumeration exceeded "
                f"--max-candidates={max_candidates}; refusing to return a partial optimum."
            )

    for start in ordered_states:
        start_rank = rank[start]
        path = [start]
        visited = {start}

        def dfs(current: str) -> None:
            for destination in adjacency.get(current, ()):
                if rank[destination] < start_rank:
                    continue
                if destination == start:
                    record(path)
                    continue
                if destination in visited:
                    continue
                visited.add(destination)
                path.append(destination)
                dfs(destination)
                path.pop()
                visited.remove(destination)

        dfs(start)

    cycles.sort(
        key=lambda nodes: (
            len(nodes),
            tuple(state_key(state) for state in nodes),
        )
    )
    return cycles


def build_candidates(
    target_model: DotModel,
    closure_model: DotModel,
    required_inputs: frozenset[str],
    required_outputs: frozenset[str],
    signal_mode: SignalMode,
    max_candidates: int,
    allow_closed_walk_fallback: bool,
) -> tuple[tuple[Transition, ...], tuple[CandidateCycle, ...], bool]:
    target_by_pair: dict[tuple[str, str], Transition] = {}
    target_ids: dict[tuple[str, str], str] = {}
    target_edges: list[Transition] = []
    for edge in target_model.edges:
        if edge.pair in target_by_pair:
            previous = target_by_pair[edge.pair]
            raise CycleCoverError(
                "Target DOT contains duplicate directed state pair "
                f"{edge.src}->{edge.dst}: {previous.label!r} and {edge.label!r}. "
                "Use an SMP/merged target DOT with one edge per state pair."
            )
        target_edge = replace(edge, kind="target")
        target_by_pair[edge.pair] = target_edge
        target_id = f"E{len(target_edges) + 1:03d}"
        target_ids[edge.pair] = target_id
        target_edges.append(target_edge)

    closure_by_pair: dict[tuple[str, str], list[Transition]] = defaultdict(list)
    for edge in closure_model.edges:
        closure_by_pair[edge.pair].append(replace(edge, kind="closure"))

    missing_pairs = sorted(
        (pair for pair in target_by_pair if pair not in closure_by_pair),
        key=lambda pair: (state_key(pair[0]), state_key(pair[1])),
    )
    if missing_pairs:
        formatted = ", ".join(f"{src}->{dst}" for src, dst in missing_pairs)
        raise CycleCoverError(
            "Target edges are missing from the closure graph by state pair: "
            f"{formatted}"
        )

    chosen_closure: dict[tuple[str, str], Transition] = {}
    for pair, variants in closure_by_pair.items():
        chosen_closure[pair] = min(
            variants,
            key=lambda edge: (
                not transition_hits_required_signal(
                    edge, required_inputs, required_outputs, signal_mode
                ),
                edge.order,
            ),
        )

    adjacency_sets: dict[str, set[str]] = defaultdict(set)
    for edge in closure_model.edges:
        adjacency_sets[edge.src].add(edge.dst)
    adjacency = {
        state: tuple(sorted(destinations, key=state_key))
        for state, destinations in adjacency_sets.items()
    }
    node_cycles = enumerate_simple_cycles(
        closure_model.states,
        adjacency,
        max_candidates=max_candidates,
    )

    raw_cycles: list[
        tuple[
            tuple[str, ...],
            tuple[Transition, ...],
            frozenset[str],
            tuple[int, ...],
        ]
    ] = []
    for nodes in node_cycles:
        pairs = tuple(
            (nodes[index], nodes[(index + 1) % len(nodes)])
            for index in range(len(nodes))
        )
        edges = tuple(
            target_by_pair.get(pair, chosen_closure[pair]) for pair in pairs
        )
        covered = frozenset(
            target_ids[pair] for pair in pairs if pair in target_ids
        )
        if not covered:
            continue
        signal_indexes = signal_edge_indexes_for_candidate(
            edges, required_inputs, required_outputs, signal_mode
        )
        raw_cycles.append((nodes, edges, covered, signal_indexes))

    candidates_without_ids = [
        item
        for item in raw_cycles
        if candidate_satisfies_signal(
            item[1], required_inputs, required_outputs, signal_mode
        )
    ]

    used_closed_walk_fallback = False
    all_target_ids = {
        f"E{index:03d}" for index in range(1, len(target_edges) + 1)
    }
    covered_target_ids = set().union(
        *(item[2] for item in candidates_without_ids)
    ) if candidates_without_ids else set()
    uncovered = sorted(all_target_ids - covered_target_ids)

    if uncovered and allow_closed_walk_fallback:
        used_closed_walk_fallback = True

        def rotate_to_anchor(nodes: tuple[str, ...], anchor: str) -> tuple[str, ...]:
            index = nodes.index(anchor)
            return nodes[index:] + nodes[:index]

        def edges_for_nodes(nodes: tuple[str, ...]) -> tuple[Transition, ...]:
            return tuple(
                target_by_pair.get(pair, chosen_closure[pair])
                for pair in (
                    (nodes[index], nodes[(index + 1) % len(nodes)])
                    for index in range(len(nodes))
                )
            )

        seen_walks = {
            tuple(edge.identity for edge in item[1])
            for item in candidates_without_ids
        }
        signal_cycles = [
            item
            for item in raw_cycles
            if candidate_satisfies_signal(
                item[1], required_inputs, required_outputs, signal_mode
            )
        ]
        target_uncovered = frozenset(uncovered)
        for base_nodes, _, base_covered, _ in raw_cycles:
            if not base_covered.intersection(target_uncovered):
                continue
            for signal_nodes, _, _, _ in signal_cycles:
                shared = sorted(set(base_nodes).intersection(signal_nodes), key=state_key)
                for anchor in shared:
                    nodes = (
                        rotate_to_anchor(base_nodes, anchor)
                        + rotate_to_anchor(signal_nodes, anchor)
                    )
                    edges = edges_for_nodes(nodes)
                    key = tuple(edge.identity for edge in edges)
                    if key in seen_walks:
                        continue
                    seen_walks.add(key)
                    covered = frozenset(
                        target_ids[edge.pair]
                        for edge in edges
                        if edge.pair in target_ids
                    )
                    if not covered:
                        continue
                    signal_indexes = signal_edge_indexes_for_candidate(
                        edges,
                        required_inputs,
                        required_outputs,
                        signal_mode,
                    )
                    if not candidate_satisfies_signal(
                        edges, required_inputs, required_outputs, signal_mode
                    ):
                        continue
                    candidates_without_ids.append(
                        (nodes, edges, covered, signal_indexes)
                    )
                    if len(candidates_without_ids) > max_candidates:
                        raise CycleCoverError(
                            "Closed-walk fallback candidate generation exceeded "
                            f"--max-candidates={max_candidates}; refusing to "
                            "return a partial optimum."
                        )

    candidates_without_ids.sort(
        key=lambda item: (
            len(item[0]),
            tuple(state_key(state) for state in item[0]),
            tuple(edge.label for edge in item[1]),
        )
    )
    candidates = tuple(
        CandidateCycle(
            candidate_id=f"K{index:03d}",
            nodes=nodes,
            edges=edges,
            target_ids=covered,
            signal_edge_indexes=signal_indexes,
        )
        for index, (nodes, edges, covered, signal_indexes) in enumerate(
            candidates_without_ids, start=1
        )
    )
    if not candidates:
        raise CycleCoverError(
            "No signal-valid cycle or closed walk covers any target edge."
        )

    covered_target_ids = set().union(
        *(candidate.target_ids for candidate in candidates)
    )
    uncovered = sorted(all_target_ids - covered_target_ids)
    if uncovered:
        raise CycleCoverError(
            "Some target edges cannot be covered by any signal-valid cycle or "
            "closed walk: "
            + ", ".join(uncovered)
        )
    return tuple(target_edges), candidates, used_closed_walk_fallback


def repeat_count(
    edge_usage: Counter[tuple[str, str, str, str]],
) -> int:
    return sum(max(count - 1, 0) for count in edge_usage.values())


def select_optimal_cycles(
    candidates: Sequence[CandidateCycle],
    target_count: int,
) -> tuple[
    tuple[CandidateCycle, ...],
    int,
    Counter[tuple[str, str, str, str]],
]:
    target_ids = tuple(f"E{index:03d}" for index in range(1, target_count + 1))
    target_set = frozenset(target_ids)
    lengths = sorted({candidate.length for candidate in candidates})
    minimum_max_length: int | None = None
    for length in lengths:
        union = set().union(
            *(
                candidate.target_ids
                for candidate in candidates
                if candidate.length <= length
            )
        )
        if union >= target_set:
            minimum_max_length = length
            break
    if minimum_max_length is None:
        raise CycleCoverError("No complete target-edge cover exists.")

    eligible = tuple(
        index
        for index, candidate in enumerate(candidates)
        if candidate.length <= minimum_max_length
    )
    options_by_target: dict[str, tuple[int, ...]] = {}
    for target_id in target_ids:
        options = tuple(
            index
            for index in eligible
            if target_id in candidates[index].target_ids
        )
        if not options:
            raise CycleCoverError(
                f"Target {target_id} became uncovered at maximum length "
                f"{minimum_max_length}."
            )
        options_by_target[target_id] = options

    forced = frozenset(
        options[0]
        for options in options_by_target.values()
        if len(options) == 1
    )

    def selection_state(
        selected_indexes: frozenset[int],
    ) -> tuple[
        frozenset[str],
        Counter[tuple[str, str, str, str]],
        int,
    ]:
        covered: set[str] = set()
        usage: Counter[tuple[str, str, str, str]] = Counter()
        total = 0
        for index in selected_indexes:
            candidate = candidates[index]
            covered.update(candidate.target_ids)
            usage.update(candidate.edge_identities)
            total += candidate.length
        return frozenset(covered), usage, total

    initial_covered, initial_usage, initial_total = selection_state(forced)
    best_indexes: frozenset[int] | None = None
    best_key: tuple[int, int, int, tuple[str, ...]] | None = None
    seen: set[frozenset[int]] = set()

    def search(
        selected_indexes: frozenset[int],
        covered: frozenset[str],
        usage: Counter[tuple[str, str, str, str]],
        total_length: int,
    ) -> None:
        nonlocal best_indexes, best_key
        if selected_indexes in seen:
            return
        seen.add(selected_indexes)

        if covered >= target_set:
            key = (
                len(selected_indexes),
                repeat_count(usage),
                total_length,
                tuple(
                    candidates[index].candidate_id
                    for index in sorted(selected_indexes)
                ),
            )
            if best_key is None or key < best_key:
                best_key = key
                best_indexes = selected_indexes
            return

        uncovered = target_set - covered
        available = tuple(
            index for index in eligible if index not in selected_indexes
        )
        gains = {
            index: len(candidates[index].target_ids.intersection(uncovered))
            for index in available
        }
        max_gain = max(gains.values(), default=0)
        if max_gain == 0:
            return
        lower_bound = math.ceil(len(uncovered) / max_gain)
        if best_key is not None:
            minimum_count = len(selected_indexes) + lower_bound
            if minimum_count > best_key[0]:
                return
            current_repeat = repeat_count(usage)
            if minimum_count == best_key[0] and current_repeat > best_key[1]:
                return
            if (
                minimum_count == best_key[0]
                and current_repeat == best_key[1]
            ):
                available_lengths = sorted(
                    candidates[index].length
                    for index in available
                    if gains[index] > 0
                )
                optimistic_total = total_length + sum(
                    available_lengths[:lower_bound]
                )
                if optimistic_total > best_key[2]:
                    return

        pivot = min(
            uncovered,
            key=lambda target_id: (
                len(
                    [
                        index
                        for index in options_by_target[target_id]
                        if index not in selected_indexes
                    ]
                ),
                target_id,
            ),
        )
        branch_indexes = [
            index
            for index in options_by_target[pivot]
            if index not in selected_indexes
        ]
        branch_indexes.sort(
            key=lambda index: (
                -gains[index],
                sum(
                    1
                    for identity in candidates[index].edge_identities
                    if usage[identity] > 0
                ),
                candidates[index].length,
                candidates[index].candidate_id,
            )
        )

        for index in branch_indexes:
            candidate = candidates[index]
            new_usage = usage.copy()
            new_usage.update(candidate.edge_identities)
            search(
                selected_indexes | {index},
                covered | candidate.target_ids,
                new_usage,
                total_length + candidate.length,
            )

    search(forced, initial_covered, initial_usage, initial_total)
    if best_indexes is None or best_key is None:
        raise CycleCoverError("Exact set-cover search found no solution.")
    selected = tuple(candidates[index] for index in sorted(best_indexes))
    _, usage, _ = selection_state(best_indexes)
    return selected, minimum_max_length, usage


def analyze_cycle_cover(
    dot_path: Path,
    closure_dot_path: Path,
    excluded_states: Iterable[str],
    required_inputs: Iterable[str],
    required_outputs: Iterable[str],
    signal_mode: SignalMode = "output-only",
    max_candidates: int = 100_000,
    allow_closed_walk_fallback: bool = True,
) -> AnalysisResult:
    if signal_mode not in SIGNAL_MODES:
        raise CycleCoverError(
            "signal_mode must be one of: " + ", ".join(sorted(SIGNAL_MODES))
        )
    excluded = frozenset(excluded_states)
    input_set = frozenset(required_inputs)
    output_set = frozenset(required_outputs)
    target_model = filter_model(parse_dot(dot_path), excluded)
    closure_model = filter_model(parse_dot(closure_dot_path), excluded)
    target_edges, candidates, used_closed_walk_fallback = build_candidates(
        target_model,
        closure_model,
        input_set,
        output_set,
        signal_mode,
        max_candidates=max_candidates,
        allow_closed_walk_fallback=allow_closed_walk_fallback,
    )
    selected, minimum_max_length, edge_usage = select_optimal_cycles(
        candidates,
        len(target_edges),
    )
    total_length = sum(candidate.length for candidate in selected)
    return AnalysisResult(
        target_model=target_model,
        closure_model=closure_model,
        target_edges=target_edges,
        candidates=candidates,
        selected=selected,
        minimum_max_length=minimum_max_length,
        repeated_edge_uses=repeat_count(edge_usage),
        total_length=total_length,
        edge_usage=edge_usage,
        required_inputs=input_set,
        required_outputs=output_set,
        signal_mode=signal_mode,
        used_closed_walk_fallback=used_closed_walk_fallback,
        excluded_states=excluded,
    )


def dot_escape(value: str) -> str:
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def resolve_graphviz_engine(engine: str) -> str:
    resolved = shutil.which(engine)
    if resolved:
        return resolved
    windows_candidate = Path(r"C:\Program Files\Graphviz\bin") / f"{engine}.exe"
    if windows_candidate.is_file():
        return str(windows_candidate)
    raise CycleCoverError(f"Graphviz engine not found: {engine}")


def render_svg(dot_text: str, output: Path, engine: str) -> None:
    executable = resolve_graphviz_engine(engine)
    reproducible_environment = os.environ.copy()
    reproducible_environment["SOURCE_DATE_EPOCH"] = "0"
    temporary = output.with_name(output.name + ".tmp")
    try:
        subprocess.run(
            [executable, "-Tsvg", "-o", str(temporary)],
            input=dot_text,
            text=True,
            encoding="utf-8",
            check=True,
            env=reproducible_environment,
        )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise CycleCoverError(f"Graphviz produced an empty SVG: {output}")
        try:
            root = ET.parse(temporary).getroot()
        except (ET.ParseError, OSError) as error:
            raise CycleCoverError(
                "Graphviz produced a malformed SVG; the artifact was not "
                f"published: {output} ({error})"
            ) from error
        if not root.tag.endswith("svg"):
            raise CycleCoverError(
                "Graphviz output has no SVG root element; the artifact was "
                f"not published: {output}"
            )
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()


def selected_cycle_metadata(
    result: AnalysisResult,
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[tuple[str, str, str, str], tuple[str, ...]],
]:
    selected_ids = {
        candidate.candidate_id: f"C{index:02d}"
        for index, candidate in enumerate(result.selected, start=1)
    }
    colors = {
        selected_ids[candidate.candidate_id]: CYCLE_PALETTE[
            (index - 1) % len(CYCLE_PALETTE)
        ]
        for index, candidate in enumerate(result.selected, start=1)
    }
    memberships: dict[
        tuple[str, str, str, str], list[str]
    ] = defaultdict(list)
    for candidate in result.selected:
        cycle_id = selected_ids[candidate.candidate_id]
        for edge in candidate.edges:
            memberships[edge.identity].append(cycle_id)
    return (
        selected_ids,
        colors,
        {
            identity: tuple(sorted(cycle_ids))
            for identity, cycle_ids in memberships.items()
        },
    )


def build_cycle_smp_dot(
    result: AnalysisResult,
    basename: str,
    candidate: CandidateCycle,
    cycle_id: str,
    color: str,
) -> str:
    source_text = result.target_model.path.read_text(encoding="utf-8")
    target_by_pair = {edge.pair: edge for edge in result.target_edges}
    candidate_identities = set(candidate.edge_identities)
    route = " → ".join((*candidate.nodes, candidate.nodes[0]))

    graph_attributes = (
        '\n  graph [overlap=false, splines=true, bgcolor="white", '
        'fontname="Microsoft YaHei", fontsize=20, labelloc="t", '
        'pad=0.25, nodesep=0.65, ranksep=0.85, '
        f'label="{dot_escape(basename)} {cycle_id}｜长度 {candidate.length}\\n'
        f'{dot_escape(route)}\\n'
        '彩色实线=本环SMP目标边；彩色虚线=原始H13补充闭环边；'
        '黑色=其余SMP边"];\n'
        '  node [fontname="Microsoft YaHei", fontsize=12, '
        'color="#475569", penwidth=1.4];\n'
        '  edge [fontname="Microsoft YaHei", fontsize=9, arrowsize=0.8];\n'
    )
    opening = source_text.find("{")
    if opening < 0:
        raise CycleCoverError(
            f"Target DOT has no graph opening brace: {result.target_model.path}"
        )
    source_text = (
        source_text[: opening + 1]
        + graph_attributes
        + source_text[opening + 1 :]
    )

    def recolor_target(match: re.Match[str]) -> str:
        indent, src, dst, attributes = match.groups()
        target = target_by_pair.get((src, dst))
        if target is None:
            return match.group(0)
        if target.identity not in candidate_identities:
            return (
                f"{indent}{src} -> {dst} [{attributes}, "
                'color="black", fontcolor="black", '
                'style="solid", penwidth=1.0];'
            )
        return (
            f"{indent}{src} -> {dst} [{attributes}, "
            f'color="{color}", fontcolor="{color}", '
            f'style="solid", penwidth=4.0, '
            f'tooltip="{cycle_id}"];'
        )

    source_text = EDGE_STATEMENT_RE.sub(recolor_target, source_text)
    closing = source_text.rfind("}")
    if closing < 0:
        raise CycleCoverError(
            f"Target DOT has no graph closing brace: {result.target_model.path}"
        )

    additions = [
        "",
        f"  // Original-H13 transitions added only to close {cycle_id}.",
    ]
    for node in candidate.nodes:
        additions.append(
            f'  {node} [color="{color}", penwidth=2.8, '
            'style="filled", fillcolor="#ffffff"];'
        )
    closure_edges = sorted(
        (edge for edge in candidate.edges if edge.kind == "closure"),
        key=lambda edge: (
            state_key(edge.src),
            state_key(edge.dst),
            edge.order,
            edge.label,
        ),
    )
    for edge in closure_edges:
        additions.append(
            f'  {edge.src} -> {edge.dst} '
            f'[label="{dot_escape(edge.label)}", '
            f'tooltip="{cycle_id} (closure)", color="{color}", '
            f'fontcolor="{color}", style="dashed", '
            "penwidth=4.0, constraint=false];"
        )
    additions.append("")
    return (
        source_text[:closing]
        + "\n".join(additions)
        + source_text[closing:]
    )


def artifact_record(path: Path, relative_to: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(relative_to).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def edge_payload(
    edge: Transition,
    required_inputs: frozenset[str],
    required_outputs: frozenset[str],
    signal_mode: SignalMode,
    use_count: int,
) -> dict[str, Any]:
    return {
        "src": edge.src,
        "dst": edge.dst,
        "label": edge.label,
        "inputs": list(edge.inputs),
        "output": edge.output,
        "kind": edge.kind,
        "required_signal": transition_hits_required_signal(
            edge, required_inputs, required_outputs, signal_mode
        ),
        "global_use_count": use_count,
    }


def build_payload(
    result: AnalysisResult,
    basename: str,
    selected_artifacts: dict[str, dict[str, Any]],
    sequence_export: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_distribution = Counter(
        candidate.length for candidate in result.candidates
    )
    selected_distribution = Counter(
        candidate.length for candidate in result.selected
    )
    selected_ids, colors, memberships = selected_cycle_metadata(result)
    coverage_cycles: dict[str, list[str]] = defaultdict(list)
    selected_payload: list[dict[str, Any]] = []
    for candidate in result.selected:
        cycle_id = selected_ids[candidate.candidate_id]
        for target_id in candidate.target_ids:
            coverage_cycles[target_id].append(cycle_id)
        selected_payload.append(
            {
                "cycle_id": cycle_id,
                "candidate_id": candidate.candidate_id,
                "color": colors[cycle_id],
                "walk_type": candidate.walk_type,
                "length": candidate.length,
                "nodes": list(candidate.nodes) + [candidate.nodes[0]],
                "target_edge_ids": sorted(candidate.target_ids),
                "signal_edge_indexes": list(candidate.signal_edge_indexes),
                "edges": [
                    edge_payload(
                        edge,
                        result.required_inputs,
                        result.required_outputs,
                        result.signal_mode,
                        result.edge_usage[edge.identity],
                    )
                    for edge in candidate.edges
                ],
                "artifact": selected_artifacts[cycle_id],
            }
        )

    candidate_payload = [
        {
            "candidate_id": candidate.candidate_id,
            "walk_type": candidate.walk_type,
            "length": candidate.length,
            "nodes": list(candidate.nodes) + [candidate.nodes[0]],
            "target_edge_ids": sorted(candidate.target_ids),
            "signal_edge_indexes": list(candidate.signal_edge_indexes),
        }
        for candidate in result.candidates
    ]
    target_payload = []
    for index, edge in enumerate(result.target_edges, start=1):
        target_id = f"E{index:03d}"
        target_payload.append(
            {
                "target_id": target_id,
                **edge_payload(
                    edge,
                    result.required_inputs,
                    result.required_outputs,
                    result.signal_mode,
                    sum(
                        1
                        for candidate in result.selected
                        if target_id in candidate.target_ids
                    ),
                ),
                "cover_count": len(coverage_cycles[target_id]),
                "selected_cycle_ids": sorted(coverage_cycles[target_id]),
            }
        )

    identity_to_edge: dict[tuple[str, str, str, str], Transition] = {}
    for candidate in result.selected:
        for edge in candidate.edges:
            identity_to_edge[edge.identity] = edge
    usage_payload = []
    for identity, count in sorted(
        result.edge_usage.items(),
        key=lambda item: (
            item[0][0],
            state_key(item[0][1]),
            state_key(item[0][2]),
            item[0][3],
        ),
    ):
        edge = identity_to_edge[identity]
        usage_payload.append(
            {
                **edge_payload(
                    edge,
                    result.required_inputs,
                    result.required_outputs,
                    result.signal_mode,
                    count,
                ),
                "repeated_uses": max(count - 1, 0),
                "selected_cycle_ids": list(memberships[identity]),
            }
        )

    payload = {
        "schema_version": 3,
        "kind": "mealy_minimum_cycle_cover",
        "source_dot": str(result.target_model.path),
        "source_sha256": sha256_file(result.target_model.path),
        "closure_dot": str(result.closure_model.path),
        "closure_sha256": sha256_file(result.closure_model.path),
        "basename": basename,
        "parameters": {
            "excluded_states": sorted(result.excluded_states, key=state_key),
            "required_inputs": sorted(result.required_inputs),
            "required_outputs": sorted(result.required_outputs),
            "signal_match_mode": result.signal_mode,
            "coverage_semantics": "target_edge_union",
            "cycle_type": (
                "simple_directed_or_composite_closed_walk"
                if result.used_closed_walk_fallback
                else "simple_directed"
            ),
            "used_closed_walk_fallback": result.used_closed_walk_fallback,
            "visualization": "one_full_smp_svg_per_selected_cycle",
            "objective_order": [
                "maximum_cycle_length",
                "cycle_count",
                "repeated_transition_uses",
                "total_cycle_length",
                "canonical_candidate_ids",
            ],
        },
        "counts": {
            "target_edges": len(result.target_edges),
            "candidate_cycles": len(result.candidates),
            "selected_cycles": len(result.selected),
            "minimum_max_cycle_length": result.minimum_max_length,
            "total_selected_cycle_length": result.total_length,
            "distinct_selected_transitions": len(result.edge_usage),
            "repeated_transition_uses": result.repeated_edge_uses,
            "candidate_length_distribution": {
                str(length): candidate_distribution[length]
                for length in sorted(candidate_distribution)
            },
            "selected_length_distribution": {
                str(length): selected_distribution[length]
                for length in sorted(selected_distribution)
            },
        },
        "candidate_cycles": candidate_payload,
        "selected_cycles": selected_payload,
        "target_edges": target_payload,
        "selected_transition_usage": usage_payload,
        "validation": {
            "all_target_edges_covered": all(
                entry["cover_count"] >= 1 for entry in target_payload
            ),
            "all_selected_cycles_signal_valid": all(
                candidate_satisfies_signal(
                    candidate.edges,
                    result.required_inputs,
                    result.required_outputs,
                    result.signal_mode,
                )
                for candidate in result.selected
            )
            if (result.required_inputs or result.required_outputs)
            else True,
            "all_selected_cycles_within_minimum_max_length": all(
                candidate.length <= result.minimum_max_length
                for candidate in result.selected
            ),
            "selected_cycle_ids_unique": len(selected_ids)
            == len(set(selected_ids.values())),
            "all_selected_artifacts_are_svg": all(
                artifact["path"].endswith(".svg")
                for artifact in selected_artifacts.values()
            ),
        },
    }
    if sequence_export is not None:
        payload["sequence_export"] = sequence_export
    return payload


def write_report(payload: dict[str, Any], path: Path) -> None:
    counts = payload["counts"]
    lines = [
        f"# {payload['basename']} 最小环覆盖报告",
        "",
        "## 1. 输入与约束",
        "",
        f"- SMP 覆盖目标：`{payload['source_dot']}`",
        f"- SMP SHA-256：`{payload['source_sha256']}`",
        f"- 闭环来源：`{payload['closure_dot']}`",
        f"- 闭环来源 SHA-256：`{payload['closure_sha256']}`",
        "- 排除状态："
        + ", ".join(f"`{item}`" for item in payload["parameters"]["excluded_states"]),
        "- 必需输入信令："
        + ", ".join(f"`{item}`" for item in payload["parameters"]["required_inputs"]),
        "- 必需输出信令："
        + ", ".join(f"`{item}`" for item in payload["parameters"]["required_outputs"]),
        f"- 信令约束模式：`{payload['parameters']['signal_match_mode']}`",
        "- 闭合游走 fallback："
        + ("启用并已使用" if payload["parameters"]["used_closed_walk_fallback"] else "未使用"),
        "- 覆盖口径：每条 SMP 目标边至少属于一个选中环；原始 DOT 的额外边只用于闭环。",
        "",
        "## 2. 从小到大的候选环统计",
        "",
        "| 环长度 | 候选环数 | 最优解选中数 |",
        "|---:|---:|---:|",
    ]
    candidate_distribution = counts["candidate_length_distribution"]
    selected_distribution = counts["selected_length_distribution"]
    for length in sorted(
        (int(value) for value in candidate_distribution),
    ):
        lines.append(
            f"| {length} | {candidate_distribution[str(length)]} | "
            f"{selected_distribution.get(str(length), 0)} |"
        )
    lines.extend(
        [
            "",
            "## 3. 精确最优结果",
            "",
            f"- 目标边覆盖：{counts['target_edges']} / {counts['target_edges']}",
            f"- 最小最大环长度：{counts['minimum_max_cycle_length']}",
            f"- 环数量：{counts['selected_cycles']}",
            f"- 总环长：{counts['total_selected_cycle_length']}",
            f"- 不同转移数：{counts['distinct_selected_transitions']}",
            f"- 重复转移使用次数：{counts['repeated_transition_uses']}",
            "",
            "词典序目标：最大环长度 → 环数量 → 重复转移使用次数 → 总环长 → 规范候选 ID。",
            "",
            "## 4. 选中的环",
            "",
            "每张图都保留完整 SMP；彩色边和节点属于当前环，黑色边是其余 "
            "SMP 转移，彩色虚线是从原始 H13 补入的闭环转移。",
            "",
            "| 环 | 颜色 | 长度 | 路径 | 覆盖目标边 | SMP 图 |",
            "|---|---|---:|---|---:|---|",
        ]
    )
    for cycle in payload["selected_cycles"]:
        route = " → ".join(cycle["nodes"])
        lines.append(
            f"| {cycle['cycle_id']} | `{cycle['color']}` | "
            f"{cycle['length']} | `{route}` | "
            f"{len(cycle['target_edge_ids'])} | "
            f"[SVG]({cycle['artifact']['path']}) |"
        )
    for cycle in payload["selected_cycles"]:
        lines.extend(
            [
                "",
                f"### {cycle['cycle_id']}（长度 {cycle['length']}，"
                f"颜色 `{cycle['color']}`）",
                "",
                f"![{cycle['cycle_id']}]({cycle['artifact']['path']})",
                "",
                f"- 路径：`{' → '.join(cycle['nodes'])}`",
                f"- 覆盖目标边：{', '.join(cycle['target_edge_ids'])}",
                "- 转移：",
            ]
        )
        for edge in cycle["edges"]:
            flags = [edge["kind"]]
            if edge["required_signal"]:
                flags.append("必需信令")
            if edge["global_use_count"] > 1:
                flags.append(f"全局复用×{edge['global_use_count']}")
            lines.append(
                f"  - `{edge['src']} → {edge['dst']}`："
                f"`{edge['label']}`（{', '.join(flags)}）"
            )
    sequence_export = payload.get("sequence_export")
    if sequence_export is not None:
        lines.extend(
            [
                "",
                "## 5. 环循环输入序列",
                "",
                f"- 文件：`{sequence_export['path']}`",
                f"- SHA-256：`{sequence_export['sha256']}`",
                f"- 访问起点：`{sequence_export['start_state']}`",
                f"- 每条具体环重复次数：{sequence_export['repeat_count']}",
                "- 合并输入策略："
                f"`{sequence_export['merged_input_policy']}`",
                f"- 总行数：{sequence_export['line_count']}",
                "",
                "| 环 | 循环起点 | 最短前缀 | 组合数 | 行号 | 每行输入数 |",
                "|---|---|---|---:|---:|---:|",
            ]
        )
        for cycle in sequence_export["cycles"]:
            prefix_text = " ".join(cycle["prefix_inputs"]) or "（空序列）"
            input_counts = sorted(
                {variant["input_count"] for variant in cycle["variants"]}
            )
            lines.append(
                f"| {cycle['cycle_id']} | `{cycle['cycle_start_state']}` | "
                f"`{prefix_text}` | {cycle['variant_count']} | "
                f"{cycle['first_line']}–{cycle['last_line']} | "
                f"{'/'.join(str(value) for value in input_counts)} |"
            )
        target_section_number = 6
        repeated_section_number = 7
    else:
        target_section_number = 5
        repeated_section_number = 6
    lines.extend(
        [
            "",
            f"## {target_section_number}. 目标边覆盖",
            "",
            "| 目标边 | 转移 | 覆盖次数 | 环 |",
            "|---|---|---:|---|",
        ]
    )
    for edge in payload["target_edges"]:
        lines.append(
            f"| {edge['target_id']} | `{edge['src']} → {edge['dst']}` "
            f"`{edge['label']}` | {edge['cover_count']} | "
            f"{', '.join(edge['selected_cycle_ids'])} |"
        )
    repeated = [
        edge
        for edge in payload["selected_transition_usage"]
        if edge["repeated_uses"] > 0
    ]
    lines.extend(
        [
            "",
            f"## {repeated_section_number}. 重复转移",
            "",
            "| 转移 | 类型 | 使用次数 | 重复次数 |",
            "|---|---|---:|---:|",
        ]
    )
    for edge in repeated:
        lines.append(
            f"| `{edge['src']} → {edge['dst']}` `{edge['label']}` | "
            f"{edge['kind']} | {edge['global_use_count']} | "
            f"{edge['repeated_uses']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_formats(value: str) -> set[str]:
    formats = {part.strip().lower() for part in value.split(",") if part.strip()}
    if formats != {"svg"}:
        raise CycleCoverError(
            "Cycle-cover figures are intentionally SVG-only; "
            "use --formats svg."
        )
    return formats


def prepare_output_dir(
    output_dir: Path,
    basename: str,
    overwrite: bool,
) -> Path:
    resolved = output_dir.resolve()
    main_paths = [
        resolved / f"{basename}_cycle_cover.json",
        resolved / f"{basename}_cycle_cover_report.md",
    ]
    cycles_dir = resolved / "cycles"
    existing = [path for path in main_paths if path.exists()]
    legacy_artifacts = (
        tuple(cycles_dir.glob(f"{basename}_cycle_*"))
        + tuple(cycles_dir.glob(f"{basename}_smp_cycle_*"))
        if cycles_dir.is_dir()
        else ()
    )
    if legacy_artifacts:
        existing.append(cycles_dir)
    if existing and not overwrite:
        raise CycleCoverError(
            "Output already exists; pass --overwrite to replace only this "
            f"basename's cycle-cover artifacts: {existing[0]}"
        )
    resolved.mkdir(parents=True, exist_ok=True)
    cycles_dir.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for path in main_paths:
            if path.is_file():
                path.unlink()
        for path in legacy_artifacts:
            if path.is_file():
                path.unlink()
    return resolved


def generate_outputs(
    result: AnalysisResult,
    output_dir: Path,
    basename: str,
    formats: set[str],
    engine: str,
    overwrite: bool,
    sequence_output: Path | None = None,
    sequence_start_state: str = "s0",
    sequence_repeat_count: int = 10,
    sequence_merged_input_policy: MergedInputPolicy = "first",
) -> dict[str, Any]:
    if (
        sequence_output is not None
        and sequence_output.resolve().exists()
        and not overwrite
    ):
        raise CycleCoverError(
            "Sequence output already exists; pass --overwrite to replace it: "
            f"{sequence_output.resolve()}"
        )
    output = prepare_output_dir(output_dir, basename, overwrite=overwrite)
    if formats != {"svg"}:
        raise CycleCoverError("Only SVG cycle-cover figures are supported.")
    cycles_dir = output / "cycles"
    selected_ids, colors, _ = selected_cycle_metadata(result)
    selected_artifacts: dict[str, dict[str, Any]] = {}
    for selected_index, candidate in enumerate(result.selected, start=1):
        cycle_id = selected_ids[candidate.candidate_id]
        stem = (
            f"{basename}_smp_cycle_{selected_index:02d}_"
            f"len{candidate.length:02d}"
        )
        svg_path = cycles_dir / f"{stem}.svg"
        dot_text = build_cycle_smp_dot(
            result,
            basename=basename,
            candidate=candidate,
            cycle_id=cycle_id,
            color=colors[cycle_id],
        )
        render_svg(dot_text, svg_path, engine=engine)
        selected_artifacts[cycle_id] = artifact_record(svg_path, output)

    sequence_export = None
    if sequence_output is not None:
        sequence_export = write_sequence_export(
            result,
            output_path=sequence_output,
            start_state=sequence_start_state,
            repeat_count=sequence_repeat_count,
            merged_input_policy=sequence_merged_input_policy,
            overwrite=overwrite,
        )
    payload = build_payload(
        result,
        basename=basename,
        selected_artifacts=selected_artifacts,
        sequence_export=sequence_export,
    )
    report_path = output / f"{basename}_cycle_cover_report.md"
    write_report(payload, report_path)
    payload["report_artifact"] = artifact_record(report_path, output)
    json_path = output / f"{basename}_cycle_cover.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "target_edges": len(result.target_edges),
        "candidate_cycles": len(result.candidates),
        "selected_cycles": len(result.selected),
        "minimum_max_cycle_length": result.minimum_max_length,
        "total_selected_cycle_length": result.total_length,
        "repeated_transition_uses": result.repeated_edge_uses,
        "artifacts": [
            str(json_path),
            str(report_path),
            *[
                str(output / artifact["path"])
                for artifact in selected_artifacts.values()
            ],
        ],
    }
    if sequence_export is not None:
        summary["sequence_export"] = sequence_export
        summary["artifacts"].append(sequence_export["path"])
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find an exact lexicographic minimum simple-cycle cover for a "
            "target Mealy DOT, using a second DOT for closure edges."
        )
    )
    parser.add_argument("--dot", type=Path, required=True)
    parser.add_argument("--closure-dot", type=Path, required=True)
    parser.add_argument("--exclude-state", action="append", default=[])
    parser.add_argument("--required-input", action="append", default=[])
    parser.add_argument("--required-output", action="append", default=[])
    parser.add_argument(
        "--signal-mode",
        choices=sorted(SIGNAL_MODES),
        default="output-only",
        help=(
            "How required inputs/outputs make a candidate signal-valid. "
            "Default: output-only."
        ),
    )
    parser.add_argument(
        "--no-closed-walk-fallback",
        action="store_true",
        help=(
            "Fail if signal-valid simple cycles alone cannot cover every "
            "target edge."
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--basename")
    parser.add_argument("--formats", default="svg")
    parser.add_argument("--engine", default="dot")
    parser.add_argument("--max-candidates", type=int, default=100_000)
    parser.add_argument(
        "--sequence-output",
        type=Path,
        help=(
            "Optional .seq output containing a shortest access prefix "
            "followed by repeated selected closed routes."
        ),
    )
    parser.add_argument("--sequence-start-state", default="s0")
    parser.add_argument("--sequence-repeat-count", type=int, default=10)
    parser.add_argument(
        "--sequence-merged-input-policy",
        choices=sorted(MERGED_INPUT_POLICIES),
        default="first",
        help=(
            "Use the first input on each merged SMP edge or expand every "
            "concrete input combination. Default: first."
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.max_candidates < 1:
        raise CycleCoverError("--max-candidates must be positive.")
    if args.sequence_output is not None and args.sequence_repeat_count < 1:
        raise CycleCoverError("--sequence-repeat-count must be positive.")
    result = analyze_cycle_cover(
        dot_path=args.dot,
        closure_dot_path=args.closure_dot,
        excluded_states=args.exclude_state,
        required_inputs=args.required_input,
        required_outputs=args.required_output,
        signal_mode=args.signal_mode,
        max_candidates=args.max_candidates,
        allow_closed_walk_fallback=not args.no_closed_walk_fallback,
    )
    summary = generate_outputs(
        result,
        output_dir=args.output_dir,
        basename=args.basename or args.dot.stem,
        formats=parse_formats(args.formats),
        engine=args.engine,
        overwrite=args.overwrite,
        sequence_output=args.sequence_output,
        sequence_start_state=args.sequence_start_state,
        sequence_repeat_count=args.sequence_repeat_count,
        sequence_merged_input_policy=args.sequence_merged_input_policy,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
