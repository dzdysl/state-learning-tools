"""Z3-backed selection of a single edge-local formula per register."""

from __future__ import annotations

from typing import Any

import z3

from contracts import RegisterInferenceError


def _candidate_expression(candidate: dict[str, Any], observation: dict[str, Any], register: str) -> z3.ArithRef:
    kind = candidate["kind"]
    before = z3.IntVal(observation["register_before"][register])
    if kind == "identity":
        return before
    if kind == "constant":
        return z3.IntVal(candidate["value"])
    if kind == "add_constant":
        return before + z3.IntVal(candidate["value"])
    if kind == "copy_input":
        return z3.IntVal(observation["input_values"][candidate["input"]])
    raise RegisterInferenceError(f"Unsupported formula kind: {kind}")


def _check_candidate(candidate: dict[str, Any], observations: list[dict[str, Any]], register: str) -> tuple[bool, list[str]]:
    solver = z3.Solver()
    failed: list[str] = []
    for observation in observations:
        expected = z3.IntVal(observation["register_after"][register])
        constraint = expected == _candidate_expression(candidate, observation, register)
        solver.push()
        solver.add(constraint)
        if solver.check() != z3.sat:
            failed.append(observation["observation_id"])
        solver.pop()
        solver.add(constraint)
    return solver.check() == z3.sat, failed


def fit_scalar_edge_candidates(prepared: dict[str, Any], candidates: dict[str, Any]) -> dict[str, Any]:
    observations_by_id = {item["observation_id"]: item for item in prepared["observations"]}
    results: list[dict[str, Any]] = []
    for group in candidates["groups"]:
        observations = [observations_by_id[item] for item in group["observation_ids"]]
        checks: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        for candidate in group["candidates"]:
            sat, failed = _check_candidate(candidate, observations, group["register"])
            checks.append({"candidate": candidate, "satisfiable": sat, "failed_observation_ids": failed})
            if sat and selected is None:
                selected = candidate
        results.append({
            "edge_id": group["edge_id"], "register": group["register"],
            "status": "sat" if selected else "unsat",
            "selected_candidate": selected,
            "candidate_checks": checks,
        })
    return {
        "schema_version": 1,
        "fitter": "z3_scalar_edge_v1",
        "status": "sat" if all(item["status"] == "sat" for item in results) else "unsat",
        "results": results,
    }
