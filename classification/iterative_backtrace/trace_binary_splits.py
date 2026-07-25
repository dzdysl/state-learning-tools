from __future__ import annotations

import argparse
import html
import hashlib
import itertools
import json
import re
import shutil
import subprocess
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


STATE_RE = re.compile(r"\b(s\d+)\s*\[")
EDGE_RE = re.compile(r'\b(s\d+)\s*->\s*(s\d+)\s*\[\s*label="([^"]+)"[^\]]*\]')
PREFIX_ORDER = {"A": 0, "D": 1, "N": 2, "NG": 3, "S": 4, "R": 5, "X": 6}
ABBR = {
    "registrationRequest": "RR", "registrationRequestGUTI": "RRG", "registrationComplete": "RC",
    "deregistrationRequest": "DR", "serviceRequest": "SR", "securityModeReject": "SMR",
    "authenticationResponse": "AR", "authenticationFailure": "AF", "deregistrationAccept": "DA",
    "securityModeComplete": "SMC", "identityResponse": "IR", "configurationUpdateComplete": "CUC",
}


def state_key(state: str) -> int:
    return int(state[1:])


def label_parts(label: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Za-z]+)(\d*)", label)
    return (match.group(1), int(match.group(2) or 0)) if match else (label, 0)


def label_key(label: str) -> tuple[int, int, str]:
    prefix, number = label_parts(label)
    return PREFIX_ORDER.get(prefix, 99), number, label


def canonical_pair(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((left, right), key=label_key))


def split_label(label: str) -> tuple[list[str], str]:
    separator = " / " if " / " in label else "/"
    if separator not in label:
        raise ValueError(f"edge label has no input/output separator: {label!r}")
    left, output = label.split(separator, 1)
    return [item.strip() for item in left.split(" | ") if item.strip()], output.strip()


def parse_dot(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    states = set(STATE_RE.findall(text))
    outgoing: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    input_order: list[str] = []
    edges = []
    for src, dst, label in EDGE_RE.findall(text):
        inputs, output = split_label(label)
        states.update((src, dst))
        for input_symbol in inputs:
            if input_symbol in outgoing[src]:
                raise ValueError(f"non-deterministic transition for ({src}, {input_symbol})")
            edge = {"src": src, "dst": dst, "input": input_symbol, "output": output}
            outgoing[src][input_symbol] = edge
            edges.append(edge)
            if input_symbol not in input_order:
                input_order.append(input_symbol)
    for state in states:
        missing = [symbol for symbol in input_order if symbol not in outgoing.get(state, {})]
        if missing:
            raise ValueError(f"incomplete input alphabet at {state}: {missing}")
    return {
        "states": sorted(states, key=state_key), "outgoing": dict(outgoing), "edges": edges,
        "input_order": input_order, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def open5gs_groups(model: dict[str, Any]) -> dict[str, list[str]]:
    groups: dict[str, set[str]] = {name: set() for name in ("D", "N", "NG", "A", "S", "R", "X")}
    for state in model["states"]:
        transitions = model["outgoing"][state].values()
        if transitions and all(edge["output"] == "null_action" for edge in transitions):
            groups["X"].add(state)
    if "s0" in model["states"] and "s0" not in groups["X"]:
        groups["D"].add("s0")
    for edge in model["edges"]:
        src, dst, inp, out = edge["src"], edge["dst"], edge["input"], edge["output"]
        if dst in groups["X"]:
            continue
        if inp == "deregistrationRequest" or "reject" in out.lower() or (inp == "securityModeReject" and src != dst):
            groups["D"].add(dst)
        if out == "authenticationRequest": groups["N"].add(dst)
        if inp == "registrationRequestGUTI" and out == "identityRequest": groups["NG"].add(dst)
        if inp == "authenticationResponse" and out == "securityModeCommand": groups["A"].add(dst)
        if out == "registrationAccept": groups["S"].add(dst)
        if out in {"serviceAccept", "configurationUpdateCommand"}: groups["R"].add(dst)
    result = {label: sorted(states, key=state_key) for label, states in groups.items()}
    owners: dict[str, list[str]] = defaultdict(list)
    for label, states in result.items():
        for state in states: owners[state].append(label)
    bad = {state: labels for state, labels in owners.items() if len(labels) != 1}
    missing = set(model["states"]) - set(owners)
    if bad or missing:
        raise ValueError(f"invalid Open5GS grouping: conflicts={bad}, missing={sorted(missing, key=state_key)}")
    return result


def state_map(groups: dict[str, list[str]]) -> dict[str, str]:
    return {state: label for label, states in groups.items() for state in states}


def partition(groups: dict[str, list[str]]) -> set[tuple[str, ...]]:
    return {tuple(states) for states in groups.values() if states}


def recompute_refinement(dot: Path, model: dict[str, Any]) -> dict[str, Any]:
    initial = open5gs_groups(model)
    previous = {label: states for label, states in initial.items() if states}
    rounds = []
    for round_index in range(1, 101):
        previous_map = state_map(previous)
        counters: dict[str, int] = defaultdict(lambda: 1)
        next_groups: dict[str, list[str]] = {}
        splits = []
        parents = []
        for parent, members in sorted(previous.items(), key=lambda item: label_key(item[0])):
            prefix, _ = label_parts(parent)
            if parent == "X":
                next_groups[parent] = members
                parents.append({"parent": parent, "members": members, "children": [parent]})
                continue
            buckets: dict[tuple[str, ...], list[str]] = defaultdict(list)
            for state in members:
                signature = tuple(previous_map[model["outgoing"][state][symbol]["dst"]] for symbol in model["input_order"])
                buckets[signature].append(state)
            child_records = []
            for signature, states in sorted(buckets.items(), key=lambda item: min(state_key(s) for s in item[1])):
                name = f"{prefix}{counters[prefix]}"; counters[prefix] += 1
                states = sorted(states, key=state_key)
                next_groups[name] = states
                child_records.append({"name": name, "states": states, "signature": list(signature)})
            parents.append({"parent": parent, "members": members, "children": [c["name"] for c in child_records]})
            if len(child_records) > 1:
                splits.append({"parent": parent, "parent_states": members, "children": child_records})
        converged = partition(previous) == partition(next_groups)
        rounds.append({
            "round": round_index, "groups": next_groups, "parents": parents, "splits": splits,
            "split_parent_count": len(splits), "class_count": len(next_groups), "converged": converged,
        })
        if converged:
            return {
                "schema_version": 1, "kind": "mealy_refinement", "source_dot": str(dot.resolve()),
                "source_sha256": model["sha256"], "profile": "open5gs-nas", "states": model["states"],
                "input_order": model["input_order"], "initial_groups": initial, "rounds": rounds,
                "final_groups": next_groups, "final_effective_round": round_index - 1, "convergence_round": round_index,
            }
        previous = next_groups
    raise ValueError("refinement did not converge within 100 rounds")


def load_refinement(path: Path | None, dot: Path, model: dict[str, Any]) -> dict[str, Any]:
    if path is None:
        return recompute_refinement(dot, model)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("kind") != "mealy_refinement":
        raise ValueError(f"not a mealy_refinement JSON: {path}")
    if data.get("source_sha256") != model["sha256"]:
        raise ValueError("refinement JSON source SHA-256 does not match DOT")
    return data


def member_transition_variants(model: dict[str, Any], states: list[str], input_symbol: str) -> list[dict[str, str]]:
    variants = []
    seen = set()
    for state in states:
        edge = model["outgoing"][state][input_symbol]
        key = (edge["src"], edge["dst"], edge["output"])
        if key not in seen:
            seen.add(key); variants.append(edge)
    return variants


def shortest_distinction(model: dict[str, Any], state_a: str, state_b: str) -> dict[str, Any]:
    queue = deque([((state_a, state_b), [])]); visited = {(state_a, state_b)}
    while queue:
        (a, b), prefix = queue.popleft()
        for symbol in model["input_order"]:
            edge_a, edge_b = model["outgoing"][a][symbol], model["outgoing"][b][symbol]
            if edge_a["output"] != edge_b["output"]:
                return {
                    "equivalent": False,
                    "sequence": [*prefix, symbol],
                    "outputs": [
                        {"state": a, "output": edge_a["output"]},
                        {"state": b, "output": edge_b["output"]},
                    ],
                }
            pair = (edge_a["dst"], edge_b["dst"])
            if pair not in visited:
                visited.add(pair); queue.append((pair, [*prefix, symbol]))
    return {"equivalent": True, "visited_product_pairs": len(visited)}


def enumerate_pairs(refinement: dict[str, Any], model: dict[str, Any]) -> list[dict[str, Any]]:
    pairs = []
    for round_data in refinement["rounds"]:
        if round_data["converged"]:
            continue
        round_index = round_data["round"]
        for split in round_data["splits"]:
            for first, second in itertools.combinations(split["children"], 2):
                children = sorted((first, second), key=lambda item: label_key(item["name"]))
                child_a, child_b = children
                diffs = []
                for index, (target_a, target_b) in enumerate(
                    zip(child_a["signature"], child_b["signature"])
                ):
                    if target_a == target_b:
                        continue
                    symbol = model["input_order"][index]
                    diffs.append({
                        "index": index, "input": symbol, "abbreviation": ABBR.get(symbol, symbol),
                        "upstream_pair": list(canonical_pair(target_a, target_b)),
                        "child_views": [
                            {
                                "child": child_a["name"],
                                "target_label": target_a,
                                "transitions": member_transition_variants(
                                    model, child_a["states"], symbol
                                ),
                            },
                            {
                                "child": child_b["name"],
                                "target_label": target_b,
                                "transitions": member_transition_variants(
                                    model, child_b["states"], symbol
                                ),
                            },
                        ],
                    })
                upstream = {tuple(item["upstream_pair"]) for item in diffs}
                if len(diffs) == 1:
                    classification = "strict"
                elif len(upstream) == 1:
                    classification = "convergent_unique"
                else:
                    classification = "branching"
                pair_name = "/".join(child["name"] for child in children)
                pairs.append({
                    "round": round_index, "parent": split["parent"], "pair": pair_name,
                    "children": children,
                    "difference_count": len(diffs), "differences": diffs,
                    "upstream_pairs": [list(item) for item in sorted(upstream)],
                    "classification": classification,
                })
    return pairs


def node_key(item: dict[str, Any]) -> tuple[int, str, str]:
    return item["round"], item["parent"], item["pair"]


def analyze_graph(pairs: list[dict[str, Any]], policy: str, only_round: int | None, only_parent: str | None) -> dict[str, Any]:
    lookup: dict[tuple[int, tuple[str, str]], dict[str, Any]] = {}
    for item in pairs:
        child_pair = canonical_pair(*(child["name"] for child in item["children"]))
        lookup[(item["round"], child_pair)] = item
    visited: set[tuple[int, str, str]] = set()
    edges: list[dict[str, Any]] = []
    terminals: dict[tuple[int, tuple[str, str]], dict[str, Any]] = {}

    def expand(item: dict[str, Any]) -> None:
        key = node_key(item)
        if key in visited:
            return
        visited.add(key)
        by_upstream: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for diff in item["differences"]:
            by_upstream[tuple(diff["upstream_pair"])].append(diff)
        for upstream_pair, differences in by_upstream.items():
            target = lookup.get((item["round"] - 1, upstream_pair)) if item["round"] > 1 else None
            signal = "/".join(diff["abbreviation"] for diff in differences)
            if target is not None:
                edges.append({"from": key, "to": node_key(target), "signals": signal, "inputs": [d["input"] for d in differences]})
                expand(target)
            else:
                terminal_key = (item["round"] - 1, upstream_pair)
                terminals.setdefault(terminal_key, {
                    "stage": item["round"] - 1, "pair": list(upstream_pair),
                    "kind": "initial_difference" if item["round"] == 1 else "partition_boundary",
                })
                edges.append({"from": key, "to_terminal": terminal_key, "signals": signal, "inputs": [d["input"] for d in differences]})

    eligible = []
    for item in sorted(pairs, key=lambda x: (-x["round"], label_key(x["parent"]), x["pair"])):
        if only_round is not None and item["round"] != only_round:
            continue
        if only_parent is not None and item["parent"] != only_parent:
            continue
        is_eligible = item["classification"] == "strict" or (policy == "unique-path" and item["classification"] == "convergent_unique")
        if is_eligible:
            eligible.append(item)
    independent = []
    for item in eligible:
        if node_key(item) in visited:
            continue
        independent.append(item)
        expand(item)
    independent_keys = {node_key(item) for item in independent}
    for item in pairs:
        key = node_key(item)
        if key in independent_keys:
            item["role"] = "independent_entry"
        elif key in visited:
            item["role"] = "covered"
        else:
            item["role"] = "unvisited_non_entry"
    return {
        "independent": independent, "visited": visited, "edges": edges,
        "terminals": list(terminals.values()),
    }


def add_behavior_audit(entries: list[dict[str, Any]], model: dict[str, Any]) -> None:
    for item in entries:
        results = []
        child_a, child_b = item["children"]
        for state_a in child_a["states"]:
            for state_b in child_b["states"]:
                results.append({
                    "states": [state_a, state_b],
                    **shortest_distinction(model, state_a, state_b),
                })
        item["behavior_audit"] = {
            "all_equivalent": all(result["equivalent"] for result in results),
            "state_pairs": results,
        }


def serialize_key(key: tuple[int, str, str]) -> str:
    return f"r{key[0]}:{key[1]}:{key[2]}"


def terminal_name(stage: int, pair: tuple[str, str]) -> str:
    return f"stage{stage}:{'/'.join(pair)}"


def build_entry_paths(
    graph: dict[str, Any],
    independent: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    model: dict[str, Any],
) -> list[dict[str, Any]]:
    by_source: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for edge in graph["edges"]:
        by_source[edge["from"]].append(edge)
    pair_lookup = {serialize_key(node_key(item)): item for item in pairs}

    def child_for_state(item: dict[str, Any], state: str) -> dict[str, Any]:
        matches = [child for child in item["children"] if state in child["states"]]
        if len(matches) != 1:
            raise ValueError(
                f"state {state} belongs to {len(matches)} children at {serialize_key(node_key(item))}"
            )
        return matches[0]

    def child_view_for_input(
        item: dict[str, Any],
        child_name: str,
        input_symbol: str,
    ) -> dict[str, Any]:
        matches = [
            view
            for diff in item["differences"]
            if diff["input"] == input_symbol
            for view in diff["child_views"]
            if view["child"] == child_name
        ]
        if len(matches) != 1:
            raise ValueError(
                f"expected one child view for {item['key']} {child_name} {input_symbol}"
            )
        return matches[0]

    def trace_variant(
        entry: dict[str, Any],
        relations: list[dict[str, Any]],
        input_sequence: tuple[str, ...],
    ) -> dict[str, Any]:
        branches: dict[str, Any] = {}
        for branch_name, entry_child in zip(("A", "B"), entry["children"]):
            trajectories = []
            for start_state in entry_child["states"]:
                current_state = start_state
                states = [start_state]
                steps = []
                for relation, input_symbol in zip(relations, input_sequence):
                    source_item = pair_lookup[relation["from"]]
                    source_child = child_for_state(source_item, current_state)
                    transition = model["outgoing"][current_state][input_symbol]
                    view = child_view_for_input(
                        source_item, source_child["name"], input_symbol
                    )
                    if transition["dst"] != next(
                        edge["dst"]
                        for edge in view["transitions"]
                        if edge["src"] == current_state
                    ):
                        raise ValueError(
                            f"transition mismatch while tracing {entry['key']} from {current_state}"
                        )
                    step = {
                        "relation_from": relation["from"],
                        "src": transition["src"],
                        "dst": transition["dst"],
                        "input": transition["input"],
                        "output": transition["output"],
                        "source_child": source_child["name"],
                    }
                    if "to" in relation:
                        target_item = pair_lookup[relation["to"]]
                        target_child = child_for_state(target_item, transition["dst"])
                        if target_child["name"] != view["target_label"]:
                            raise ValueError(
                                f"{transition['dst']} resolves to {target_child['name']}, "
                                f"expected {view['target_label']}"
                            )
                        step["relation_to"] = relation["to"]
                        step["target_child"] = target_child["name"]
                    else:
                        step["to_terminal"] = relation["to_terminal"]
                        step["terminal_class"] = view["target_label"]
                    steps.append(step)
                    current_state = transition["dst"]
                    states.append(current_state)
                trajectories.append({
                    "start_state": start_state,
                    "states": states,
                    "steps": steps,
                })
            branches[branch_name] = {
                "entry_child": entry_child["name"],
                "start_states": entry_child["states"],
                "trajectories": trajectories,
            }
        return {
            "input_sequence": list(input_sequence),
            "branches": branches,
        }

    results = []
    for entry in independent:
        paths: list[dict[str, Any]] = []

        def walk(
            current: tuple[int, str, str],
            node_keys: list[str],
            relations: list[dict[str, Any]],
        ) -> None:
            for edge in by_source.get(current, []):
                relation = {
                    "from": serialize_key(edge["from"]),
                    "signals": edge["signals"],
                    "inputs": edge["inputs"],
                }
                if "to" in edge:
                    relation["to"] = serialize_key(edge["to"])
                    walk(edge["to"], [*node_keys, relation["to"]], [*relations, relation])
                else:
                    stage, pair = edge["to_terminal"]
                    relation["to_terminal"] = terminal_name(stage, pair)
                    complete_relations = [*relations, relation]
                    variants = [
                        trace_variant(entry, complete_relations, input_sequence)
                        for input_sequence in itertools.product(
                            *(item["inputs"] for item in complete_relations)
                        )
                    ]
                    paths.append({
                        "node_keys": node_keys,
                        "relations": complete_relations,
                        "terminal": relation["to_terminal"],
                        "trace_variants": variants,
                    })

        key = node_key(entry)
        walk(key, [serialize_key(key)], [])
        results.append({"entry_key": serialize_key(key), "paths": paths})
    return results


def build_terminal_audits(
    graph: dict[str, Any],
    pairs: list[dict[str, Any]],
    model: dict[str, Any],
) -> list[dict[str, Any]]:
    lookup = {node_key(item): item for item in pairs}
    audits = []
    for edge in graph["edges"]:
        if "to_terminal" not in edge:
            continue
        source = lookup[edge["from"]]
        stage, terminal_pair = edge["to_terminal"]
        matching = [
            diff for diff in source["differences"]
            if tuple(diff["upstream_pair"]) == terminal_pair
        ]
        input_audits = []
        for diff in matching:
            state_pairs = []
            child_a, child_b = source["children"]
            for state_a in child_a["states"]:
                for state_b in child_b["states"]:
                    transition_a = model["outgoing"][state_a][diff["input"]]
                    transition_b = model["outgoing"][state_b][diff["input"]]
                    record: dict[str, Any] = {
                        "states": [state_a, state_b],
                        "transitions": [transition_a, transition_b],
                        "immediate_output_equal": (
                            transition_a["output"] == transition_b["output"]
                        ),
                    }
                    if record["immediate_output_equal"]:
                        record["shortest_observable_suffix"] = shortest_distinction(
                            model, transition_a["dst"], transition_b["dst"]
                        )
                    state_pairs.append(record)
            input_audits.append({
                "input": diff["input"],
                "abbreviation": diff["abbreviation"],
                "state_pairs": state_pairs,
            })
        audits.append({
            "from": serialize_key(edge["from"]),
            "terminal": terminal_name(stage, terminal_pair),
            "initial_pair": list(terminal_pair),
            "inputs": input_audits,
        })
    return audits


def highlighted_subgraph(graph: dict[str, Any]) -> tuple[set[str], set[tuple[str, str]]]:
    independent = graph["independent"]
    if not independent:
        return set(), set()
    latest_round = max(item["round"] for item in independent)
    roots = {node_key(item) for item in independent if item["round"] == latest_round}
    by_source: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for edge in graph["edges"]:
        by_source[edge["from"]].append(edge)
    nodes: set[str] = set()
    edges: set[tuple[str, str]] = set()

    def visit(key: tuple[int, str, str]) -> None:
        source = serialize_key(key)
        if source in nodes:
            return
        nodes.add(source)
        for edge in by_source.get(key, []):
            if "to" in edge:
                target = serialize_key(edge["to"])
                edges.add((source, target))
                visit(edge["to"])
            else:
                stage, pair = edge["to_terminal"]
                edges.add((source, terminal_name(stage, pair)))

    for root in roots:
        visit(root)
    return nodes, edges


def build_payload(dot: Path, model: dict[str, Any], refinement: dict[str, Any], pairs: list[dict[str, Any]], graph: dict[str, Any], policy: str) -> dict[str, Any]:
    strict = sum(item["classification"] == "strict" for item in pairs)
    convergent = sum(item["classification"] == "convergent_unique" for item in pairs)
    branching = sum(item["classification"] == "branching" for item in pairs)
    visited_items = sorted(
        (item for item in pairs if item["role"] != "unvisited_non_entry"),
        key=lambda item: (-item["round"], label_key(item["parent"]), item["pair"]),
    )
    for index, item in enumerate(visited_items, 1):
        item["display_id"] = f"B{index:02d}"
    for item in pairs:
        item["key"] = serialize_key(node_key(item))
        item["entry_eligible"] = (
            item["classification"] == "strict"
            or (policy == "unique-path" and item["classification"] == "convergent_unique")
        )
        if item["entry_eligible"]:
            item["entry_exclusion_reason"] = None
        elif item["classification"] == "convergent_unique":
            item["entry_exclusion_reason"] = (
                f"{item['difference_count']} 个 signature 位置不同；"
                "虽汇聚到同一上游类别对，但不满足 strict 的单位置条件"
            )
        else:
            item["entry_exclusion_reason"] = (
                f"{item['difference_count']} 个 signature 位置不同，且指向 "
                f"{len(item['upstream_pairs'])} 个不同上游类别对"
            )
    terminal_pairs = sorted(
        {tuple(item["pair"]) for item in graph["terminals"]},
        key=lambda pair: (label_key(pair[0]), label_key(pair[1])),
    )
    terminal_ids = {
        terminal_name(0, pair): f"K{index}" for index, pair in enumerate(terminal_pairs, 1)
    }
    highlighted_nodes, highlighted_edges = highlighted_subgraph(graph)
    serialized_edges = []
    for edge in graph["edges"]:
        record = {
            "from": serialize_key(edge["from"]),
            "signals": edge["signals"],
            "inputs": edge["inputs"],
        }
        if "to" in edge:
            record["to"] = serialize_key(edge["to"])
        else:
            stage, pair = edge["to_terminal"]
            record["to_terminal"] = terminal_name(stage, pair)
        serialized_edges.append(record)
    round_stats = []
    for round_data in refinement["rounds"]:
        if round_data["converged"]:
            continue
        round_pairs = [item for item in pairs if item["round"] == round_data["round"]]
        round_stats.append({
            "round": round_data["round"],
            "split_parent_count": round_data["split_parent_count"],
            "candidate_pairs": len(round_pairs),
            "strict_pairs": sum(item["classification"] == "strict" for item in round_pairs),
            "non_strict_pairs": sum(item["classification"] != "strict" for item in round_pairs),
        })
    return {
        "schema_version": 2, "kind": "mealy_binary_backtrace", "source_dot": str(dot.resolve()),
        "source_sha256": model["sha256"], "entry_policy": policy, "input_order": model["input_order"],
        "counts": {
            "all_pairs": len(pairs), "strict": strict, "convergent_unique": convergent,
            "branching": branching, "independent_entries": len(graph["independent"]),
            "non_strict": convergent + branching,
            "covered_nodes": sum(item["role"] == "covered" for item in pairs),
            "visited_nodes": len(graph["visited"]), "terminals": len(graph["terminals"]),
            "round_relations": sum("to" in edge for edge in serialized_edges),
            "terminal_relations": sum("to_terminal" in edge for edge in serialized_edges),
            "initial_key_differences": len(terminal_pairs),
        },
        "pairs": pairs,
        "independent_entry_keys": [serialize_key(node_key(item)) for item in graph["independent"]],
        "entry_paths": build_entry_paths(graph, graph["independent"], pairs, model),
        "edges": serialized_edges,
        "terminals": graph["terminals"],
        "terminal_ids": terminal_ids,
        "terminal_audits": build_terminal_audits(graph, pairs, model),
        "rounds": [
            {
                "round": round_data["round"],
                "class_count": round_data["class_count"],
                "split_parent_count": round_data["split_parent_count"],
                "splits": round_data["splits"],
            }
            for round_data in refinement["rounds"] if not round_data["converged"]
        ],
        "round_pair_statistics": round_stats,
        "highlighted_node_keys": sorted(highlighted_nodes),
        "highlighted_edges": [
            {"from": source, "to": target}
            for source, target in sorted(highlighted_edges)
        ],
        "refinement_summary": {
            "round_class_counts": [item["class_count"] for item in refinement["rounds"] if not item["converged"]],
            "split_parent_counts": [item["split_parent_count"] for item in refinement["rounds"]],
            "final_effective_round": refinement["final_effective_round"],
            "convergence_round": refinement["convergence_round"],
            "state_count": len(refinement["states"]),
        },
    }


def transition_text(edge: dict[str, str]) -> str:
    return f"{edge['src']} --{edge['input']} / {edge['output']}→ {edge['dst']}"


def distinction_outputs_text(result: dict[str, Any]) -> str:
    return "/".join(
        f"{item['state']}:{item['output']}" for item in result["outputs"]
    )


def role_text(role: str) -> str:
    return {
        "independent_entry": "独立入口",
        "covered": "已由较晚路径覆盖",
        "unvisited_non_entry": "非入口且未参与回溯",
    }[role]


def write_full_report(payload: dict[str, Any], path: Path) -> None:
    c = payload["counts"]
    pair_lookup = {item["key"]: item for item in payload["pairs"]}
    terminal_ids = payload["terminal_ids"]
    lines = [
        f"# {Path(payload['source_dot']).stem} 全量二分类回溯检验报告", "",
        "## 1. 数据来源与判定规则", "",
        f"- 原始 DOT：`{payload['source_dot']}`",
        f"- SHA-256：`{payload['source_sha256']}`",
        f"- 入口策略：`{payload['entry_policy']}`；默认入口必须恰有一个 signature 差异位置。",
        "- 节点按 `(轮次, 父类, 无序子类对)` 去重；从最后有效轮向第1轮扫描。",
        "- 多位置对子不独立启动；仅在回溯需要时作为中间节点，同一上游对子合并信令，不同上游对子分别分支。", "",
        "| 位置 | 输入 | 简写 |", "|---:|---|---|",
    ]
    for index, symbol in enumerate(payload["input_order"], 1):
        lines.append(f"| {index} | `{symbol}` | `{ABBR.get(symbol, symbol)}` |")
    lines.extend([
        "", "## 2. 重算统计", "",
        "| 轮次 | 拆分父类 | 候选对子 | 严格入口对子 | 非严格对子 |",
        "|---:|---:|---:|---:|---:|",
    ])
    for item in sorted(payload["round_pair_statistics"], key=lambda x: -x["round"]):
        lines.append(
            f"| {item['round']} | {item['split_parent_count']} | {item['candidate_pairs']} | "
            f"{item['strict_pairs']} | {item['non_strict_pairs']} |"
        )
    lines.extend([
        f"| 合计 | — | {c['all_pairs']} | {c['strict']} | {c['non_strict']} |", "",
        f"- 状态数：{payload['refinement_summary']['state_count']}；拆分父类数："
        f"`{payload['refinement_summary']['split_parent_counts']}`。",
        f"- 独立入口：{c['independent_entries']}；回溯节点：{c['visited_nodes']}；"
        f"已覆盖节点：{c['covered_nodes']}。",
        f"- 轮间关系：{c['round_relations']}；终点关系：{c['terminal_relations']}；"
        f"初始关键差异：{c['initial_key_differences']}。", "",
        "## 3. 独立入口与去重路径", "",
        "| 序号 | 入口 | 轮次与父类 | 子类对 | 去重路径 |",
        "|---:|---|---|---|---|",
    ])
    for index, entry in enumerate(payload["entry_paths"], 1):
        item = pair_lookup[entry["entry_key"]]
        path_texts = []
        for path_record in entry["paths"]:
            parts = []
            for relation in path_record["relations"]:
                source = pair_lookup[relation["from"]]["display_id"]
                target = (
                    pair_lookup[relation["to"]]["display_id"]
                    if "to" in relation else terminal_ids[relation["to_terminal"]]
                )
                parts.append(f"{source} --{relation['signals']}→ {target}")
            path_texts.append("；".join(parts))
        lines.append(
            f"| {index} | `{item['display_id']}` | 第{item['round']}轮 `{item['parent']}` | "
            f"`{item['pair']}` | {'<br>'.join(path_texts)} |"
        )
    lines.extend(["", "### 固定 A/B 具体状态轨迹", ""])
    for entry in payload["entry_paths"]:
        item = pair_lookup[entry["entry_key"]]
        lines.extend([f"#### `{item['display_id']}`", ""])
        for path_index, path_record in enumerate(entry["paths"], 1):
            for variant_index, variant in enumerate(path_record["trace_variants"], 1):
                lines.append(
                    f"- 路径 {path_index} / 输入变体 {variant_index}："
                    f"`{' '.join(variant['input_sequence'])}` → `{path_record['terminal']}`"
                )
                for branch_name in ("A", "B"):
                    branch = variant["branches"][branch_name]
                    for trajectory in branch["trajectories"]:
                        lines.append(
                            f"  - branch {branch_name}（入口子类 `{branch['entry_child']}`，"
                            f"起点 `{trajectory['start_state']}`）："
                            f"`{' → '.join(trajectory['states'])}`"
                        )
        lines.append("")
    lines.extend([
        "",
        "## 4. 去重后的回溯节点：完整 signature、代表转移与全部成员转移变体",
        "",
    ])
    current_round = None
    edge_lookup: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in payload["edges"]:
        edge_lookup[edge["from"]].append(edge)
    for item in sorted(
        (x for x in payload["pairs"] if x["role"] != "unvisited_non_entry"),
        key=lambda x: (-x["round"], label_key(x["parent"]), x["pair"]),
    ):
        if current_round != item["round"]:
            current_round = item["round"]
            lines.extend([f"### 第 {current_round} 轮", ""])
        lines.extend([
            f"#### `{item['display_id']}`：父类 `{item['parent']}` 的 `{item['pair']}`（{role_text(item['role'])}）",
            "",
        ])
        for child in item["children"]:
            lines.extend([
                f"- 子类 `{child['name']}` = {{{', '.join(child['states'])}}}",
                f"  - signature：`({', '.join(child['signature'])})`",
            ])
        child_a, child_b = item["children"]
        lines.extend([
            "",
            f"| 位置 | 输入 | 简写 | 上游类别对 | `{child_a['name']}` 代表转移 | "
            f"`{child_b['name']}` 代表转移 |",
            "|---:|---|---|---|---|---|",
        ])
        for diff in item["differences"]:
            view_a, view_b = diff["child_views"]
            transition_a = view_a["transitions"][0]
            transition_b = view_b["transitions"][0]
            lines.append(
                f"| {diff['index'] + 1} | `{diff['input']}` | `{diff['abbreviation']}` | "
                f"`{'/'.join(diff['upstream_pair'])}` | `{transition_text(transition_a)}` | "
                f"`{transition_text(transition_b)}` |"
            )
            lines.append(
                f"|  | 全部成员转移变体 |  |  | "
                f"`{'；'.join(transition_text(edge) for edge in view_a['transitions'])}` | "
                f"`{'；'.join(transition_text(edge) for edge in view_b['transitions'])}` |"
            )
        for edge in edge_lookup[item["key"]]:
            target = (
                pair_lookup[edge["to"]]["display_id"]
                if "to" in edge else terminal_ids[edge["to_terminal"]]
            )
            lines.append(f"\n- 回溯关系：`{item['display_id']} --{edge['signals']}→ {target}`。")
        audit = item.get("behavior_audit")
        if audit:
            lines.append(f"- 全部成员状态交叉对在模型内行为等价：`{audit['all_equivalent']}`。")
            for result in audit["state_pairs"]:
                states = "/".join(result["states"])
                if result["equivalent"]:
                    lines.append(f"  - `{states}`：模型内等价。")
                else:
                    lines.append(
                        f"  - `{states}`：最短区分序列 "
                        f"`{' '.join(result['sequence'])}` → "
                        f"`{distinction_outputs_text(result)}`。"
                    )
        lines.append("")
    lines.extend(["## 5. 初始关键分裂与可观察输出", ""])
    audits_by_terminal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for audit in payload["terminal_audits"]:
        audits_by_terminal[audit["terminal"]].append(audit)
    for terminal, terminal_id in sorted(terminal_ids.items(), key=lambda item: int(item[1][1:])):
        pair = terminal.split(":", 1)[1]
        lines.extend([f"### `{terminal_id}`：初始类别 `{pair}`", ""])
        for audit in audits_by_terminal[terminal]:
            source = pair_lookup[audit["from"]]
            for input_audit in audit["inputs"]:
                lines.append(
                    f"- 来源 `{source['display_id']}` 的 `{source['pair']}`，输入 "
                    f"`{input_audit['input']}`（`{input_audit['abbreviation']}`）："
                )
                for state_pair in input_audit["state_pairs"]:
                    transition_a, transition_b = state_pair["transitions"]
                    lines.append(f"  - 状态 `{transition_a['src']}`：`{transition_text(transition_a)}`")
                    lines.append(f"  - 状态 `{transition_b['src']}`：`{transition_text(transition_b)}`")
                    if not state_pair["immediate_output_equal"]:
                        lines.append(
                            f"  - 直接可观察：`{transition_a['src']}:{transition_a['output']}/"
                            f"{transition_b['src']}:{transition_b['output']}`。"
                        )
                    else:
                        suffix = state_pair["shortest_observable_suffix"]
                        if suffix["equivalent"]:
                            lines.append("  - 即时输出相同，目标状态在模型内等价。")
                        else:
                            lines.append(
                                f"  - 即时输出相同；最短可观察后缀："
                                f"`{' '.join(suffix['sequence'])}`，最终输出 "
                                f"`{distinction_outputs_text(suffix)}`。"
                            )
        lines.append("")
    lines.extend([
        f"## 6. 不符合严格入口条件的 {c['non_strict']} 个对子", "",
        "| 轮次 | 父类 | 子类对 | 分类 | 差异数 | 排除原因 | 差异输入与目标类别 | 是否作为中间节点 |",
        "|---:|---|---|---|---:|---|---|---|",
    ])
    for item in sorted(
        (item for item in payload["pairs"] if item["classification"] != "strict"),
        key=lambda x: (-x["round"], label_key(x["parent"]), x["pair"]),
    ):
        differences = "；".join(
            f"{diff['abbreviation']}({'→'.join(diff['upstream_pair'])})"
            for diff in item["differences"]
        )
        lines.append(
            f"| {item['round']} | `{item['parent']}` | `{item['pair']}` | "
            f"`{item['classification']}` | {item['difference_count']} | "
            f"{item['entry_exclusion_reason']} | `{differences}` | "
            f"{'是' if item['role'] == 'covered' else '否'} |"
        )
    stem = Path(payload["source_dot"]).stem
    lines.extend([
        "", "## 7. 一致性结论", "",
        f"- 最后有效细化轮：第 {payload['refinement_summary']['final_effective_round']} 轮；"
        f"第 {payload['refinement_summary']['convergence_round']} 轮确认收敛。",
        "- 所有轮间关系严格指向上一轮，无环；公共尾链仅展开一次。",
        "- 所有代表转移和成员变体均来自原始 DOT；初始终点均落实到直接或最短后缀可观察输出差异。", "",
        "## 8. 全量回溯流程图", "",
        f"![{stem} 全量二分类回溯流程图]({stem}_all_binary_backtrace_flowchart.svg)", "",
        f"可编辑 DOT：`{stem}_all_binary_backtrace_flowchart.dot`；"
        f"PDF：`{stem}_all_binary_backtrace_flowchart.pdf`；"
        f"审计 JSON：`{stem}_all_binary_backtrace.json`。", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def table_pair_list(items: list[dict[str, Any]]) -> str:
    return "; ".join(item["pair"] for item in items)


def write_flowchart(payload: dict[str, Any], path: Path) -> int:
    pair_lookup = {item["key"]: item for item in payload["pairs"]}
    highlighted_nodes = set(payload["highlighted_node_keys"])
    highlighted_edges = {
        (item["from"], item["to"]) for item in payload["highlighted_edges"]
    }
    parent_ids: dict[tuple[int, str], str] = {}
    for round_data in payload["rounds"]:
        for index, split in enumerate(round_data["splits"], 1):
            parent_ids[(round_data["round"], split["parent"])] = f"P_R{round_data['round']}_{index}"
    lines = [
        "digraph MealyBinaryBacktrace {",
        '  graph [rankdir=BT, compound=true, newrank=true, splines=polyline, overlap=false, '
        'nodesep=0.45, ranksep=1.05, pad=0.25, bgcolor="white", fontname="Microsoft YaHei", '
        f'labelloc="t", label="{esc(Path(payload["source_dot"]).stem)} 全量二分类回溯流程图"];',
        '  node [fontname="Microsoft YaHei", fontsize=10];',
        '  edge [fontname="Microsoft YaHei", fontsize=9, color="#555555", arrowsize=0.8];',
        "",
        "  subgraph cluster_initial {",
        '    label="初始类别关键差异"; color="#e6a23c"; style="rounded"; rank=same;',
    ]
    terminal_node_ids = {}
    for terminal, identifier in sorted(payload["terminal_ids"].items(), key=lambda item: int(item[1][1:])):
        node_id = f"K_{identifier[1:]}"
        terminal_node_ids[terminal] = node_id
        label = terminal.split(":", 1)[1]
        lines.append(
            f'    {node_id} [shape=box, style="rounded,filled", fillcolor="#fff2cc", '
            f'color="#d99a00", penwidth=2, label="{esc(label)}"];'
        )
    lines.append("  }")
    for round_data in payload["rounds"]:
        round_index = round_data["round"]
        lines.append(
            f'  subgraph cluster_round_{round_index} {{ label="第{round_index}轮"; '
            'color="#b7c9e2"; style="rounded"; rank=same;'
        )
        for split in round_data["splits"]:
            parent = split["parent"]
            node_id = parent_ids[(round_index, parent)]
            parent_pairs = [
                item for item in payload["pairs"]
                if item["round"] == round_index and item["parent"] == parent
            ]
            independent = [item for item in parent_pairs if item["role"] == "independent_entry"]
            covered = [item for item in parent_pairs if item["role"] == "covered"]
            non_entry = [item for item in parent_pairs if item["classification"] != "strict"]
            active = bool(independent or covered)
            highlighted = any(item["key"] in highlighted_nodes for item in parent_pairs)
            border = "#1565c0" if highlighted else ("#2f75b5" if active else "#9e9e9e")
            background = "#dcecff" if highlighted else ("#ffffff" if active else "#f2f2f2")
            border_width = "3" if highlighted else "2"
            rows = [
                f'<TR><TD BGCOLOR="{background}"><B>父类 {html.escape(parent)}</B></TD></TR>'
            ]
            for child in split["children"]:
                rows.append(
                    f'<TR><TD ALIGN="LEFT"><B>{html.escape(child["name"])}</B> = '
                    f'{{{html.escape(", ".join(child["states"]))}}}</TD></TR>'
                )
            if independent:
                rows.append(
                    '<TR><TD ALIGN="LEFT"><FONT COLOR="#1f4e79">独立入口对子：'
                    f'{html.escape(table_pair_list(independent))}</FONT></TD></TR>'
                )
            if covered:
                rows.append(
                    '<TR><TD ALIGN="LEFT"><FONT COLOR="#1f4e79">覆盖对子：'
                    f'{html.escape(table_pair_list(covered))}</FONT></TD></TR>'
                )
            if non_entry:
                labels = "; ".join(
                    f"{item['pair']}({item['difference_count']}处"
                    f"{'，中间节点' if item['role'] == 'covered' else ''})"
                    for item in non_entry
                )
                rows.append(
                    '<TR><TD ALIGN="LEFT"><FONT COLOR="#777777">非入口对子：'
                    f'{html.escape(labels)}</FONT></TD></TR>'
                )
            table_border = border_width if active else "0"
            table = (
                f'<<TABLE BORDER="{table_border}" CELLBORDER="1" CELLSPACING="0" '
                f'CELLPADDING="5" COLOR="{border}" BGCOLOR="{background}">'
                f'{"".join(rows)}</TABLE>>'
            )
            if active:
                lines.append(f"    {node_id} [shape=plain label={table}];")
            else:
                lines.append(
                    f'    {node_id} [shape=box, style="dashed", color="{border}", '
                    f'penwidth=2, margin=0, label={table}];'
                )
        lines.append("  }")
    grouped_edges: dict[tuple[str, str, bool], list[str]] = defaultdict(list)
    for edge in payload["edges"]:
        source_item = pair_lookup[edge["from"]]
        source = parent_ids[(source_item["round"], source_item["parent"])]
        if "to" in edge:
            target_item = pair_lookup[edge["to"]]
            target = parent_ids[(target_item["round"], target_item["parent"])]
            target_pair = target_item["pair"]
            edge_target_key = edge["to"]
        else:
            target = terminal_node_ids[edge["to_terminal"]]
            target_pair = edge["to_terminal"].split(":", 1)[1]
            edge_target_key = edge["to_terminal"]
        highlighted = (edge["from"], edge_target_key) in highlighted_edges
        grouped_edges[(source, target, highlighted)].append(
            f"{source_item['pair']}: {edge['signals']} → {target_pair}"
        )
    for (source, target, highlighted), labels in grouped_edges.items():
        attributes = [f'label="{esc(chr(10).join(labels))}"']
        if highlighted:
            attributes.extend(['color="#1565c0"', 'fontcolor="#1565c0"', "penwidth=3"])
        lines.append(f"  {source} -> {target} [{', '.join(attributes)}];")
    legend_rows = ['<TR><TD ALIGN="LEFT"><B>信令简写</B></TD></TR>']
    legend_rows.extend(
        f'<TR><TD ALIGN="LEFT">{html.escape(ABBR[symbol])} = '
        f'{html.escape(symbol)}</TD></TR>'
        for symbol in payload["input_order"]
    )
    lines.extend([
        '  legend [shape=plain, label=<<TABLE BORDER="1" CELLBORDER="0" '
        'CELLSPACING="0" CELLPADDING="3">'
        + "".join(legend_rows)
        + "</TABLE>>];",
        "}",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(parent_ids) + len(terminal_node_ids) + 1


def render(dot_path: Path, formats: set[str], node_count: int, limit: int, force: bool) -> list[str]:
    artifacts = [str(dot_path)]
    requested = formats & {"svg", "pdf"}
    if not requested: return artifacts
    if node_count > limit and not force:
        print(f"skip SVG/PDF rendering: {node_count} nodes exceed --max-render-nodes={limit}")
        return artifacts
    dot = shutil.which("dot")
    if not dot: raise RuntimeError("Graphviz 'dot' is required for SVG/PDF rendering")
    for fmt in sorted(requested):
        target = dot_path.with_suffix(f".{fmt}")
        subprocess.run([dot, f"-T{fmt}", str(dot_path), "-o", str(target)], check=True)
        if target.stat().st_size == 0: raise RuntimeError(f"empty Graphviz artifact: {target}")
        artifacts.append(str(target))
    return artifacts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trace binary class splits through Mealy refinement")
    parser.add_argument("--dot", type=Path, required=True)
    parser.add_argument("--refinement-json", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--basename")
    parser.add_argument("--entry-policy", choices=["strict", "unique-path"], default="strict")
    parser.add_argument("--only-round", type=int)
    parser.add_argument("--only-parent")
    parser.add_argument("--max-pairs", type=int, default=100000)
    parser.add_argument("--formats", default="dot,svg,pdf")
    parser.add_argument("--max-render-nodes", type=int, default=250)
    parser.add_argument("--force-render", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args(); model = parse_dot(args.dot); refinement = load_refinement(args.refinement_json, args.dot, model)
    pairs = enumerate_pairs(refinement, model)
    if len(pairs) > args.max_pairs:
        raise ValueError(f"candidate pair count {len(pairs)} exceeds --max-pairs={args.max_pairs}; filter by round/parent")
    graph = analyze_graph(pairs, args.entry_policy, args.only_round, args.only_parent)
    add_behavior_audit([item for item in pairs if item["role"] != "unvisited_non_entry"], model)
    payload = build_payload(args.dot, model, refinement, pairs, graph, args.entry_policy)
    args.output_dir.mkdir(parents=True, exist_ok=True); base = args.basename or args.dot.stem
    json_path = args.output_dir / f"{base}_all_binary_backtrace.json"
    report_path = args.output_dir / f"{base}_all_binary_backtrace_report.md"
    flow_path = args.output_dir / f"{base}_all_binary_backtrace_flowchart.dot"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_full_report(payload, report_path)
    node_count = write_flowchart(payload, flow_path)
    formats = {item.strip().lower() for item in args.formats.split(",") if item.strip()}
    payload["artifacts"] = [
        str(json_path), str(report_path),
        *render(flow_path, formats, node_count, args.max_render_nodes, args.force_render),
    ]
    print(json.dumps({"counts": payload["counts"], "artifacts": payload["artifacts"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
