"""Simple, replaceable formula candidate generation for integer registers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from contracts import RegisterInferenceError


DEFAULT_PRIORITY = ("identity", "constant", "add_constant", "copy_input")


def _group_observations(prepared: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for observation in prepared["observations"]:
        for register in prepared["registers"]:
            groups[(observation["edge_id"], register)].append(observation)
    return groups


def generate_simple_scalar_candidates(prepared: dict[str, Any], priority: list[str] | None = None) -> dict[str, Any]:
    if prepared.get("schema_version") != 1:
        raise RegisterInferenceError("Prepared input must have schema_version: 1.")
    priority = priority or list(DEFAULT_PRIORITY)
    unknown = set(priority) - set(DEFAULT_PRIORITY)
    if unknown:
        raise RegisterInferenceError(f"Unknown candidate kinds: {sorted(unknown)}")
    groups: list[dict[str, Any]] = []
    for (edge_id, register), observations in sorted(_group_observations(prepared).items()):
        before = [item["register_before"][register] for item in observations]
        after = [item["register_after"][register] for item in observations]
        candidates: list[dict[str, Any]] = []
        if all(left == right for left, right in zip(before, after)):
            candidates.append({"kind": "identity"})
        if len(set(after)) == 1:
            candidates.append({"kind": "constant", "value": after[0]})
        deltas = [right - left for left, right in zip(before, after)]
        if len(set(deltas)) == 1:
            candidates.append({"kind": "add_constant", "value": deltas[0]})
        for input_id in prepared.get("input_variables", []):
            if all(item["input_values"][input_id] == result for item, result in zip(observations, after)):
                candidates.append({"kind": "copy_input", "input": input_id})
        order = {kind: index for index, kind in enumerate(priority)}
        candidates.sort(key=lambda item: (order[item["kind"]], str(sorted(item.items()))))
        groups.append({
            "edge_id": edge_id,
            "register": register,
            "observation_ids": [item["observation_id"] for item in observations],
            "candidates": candidates,
        })
    return {
        "schema_version": 1,
        "prepared_input_hash": prepared["input_hashes"],
        "generator": "simple_scalar_v1",
        "guard_generator": "disabled",
        "groups": groups,
    }
