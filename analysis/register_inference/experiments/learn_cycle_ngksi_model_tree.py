"""Learn a small guarded AMF-visible KSI relation from one repeated-cycle trace.

This experimental adapter maps a cycle-cover route to its concrete .seq line
and learns each concrete line independently. It does not pool lines or claim
that an inferred field is an AMF implementation variable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


class ModelTreeError(RuntimeError):
    pass


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
            raise ModelTreeError(f"Missing field {path!r}.")
        current = current[segment]
    return current


def integer_field(record: dict[str, Any], path: str, trace_line: int) -> int:
    value = get_path(record, path)
    if isinstance(value, bool):
        raise ModelTreeError(f"Trace line {trace_line}: {path} is boolean, not an integer.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ModelTreeError(f"Trace line {trace_line}: {path} is not an integer: {value!r}.") from exc


def leaf_formula(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    before = [sample["before"] for sample in samples]
    after = [sample["after"] for sample in samples]
    formulas: list[dict[str, Any]] = []
    if all(left == right for left, right in zip(before, after)):
        formulas.append({"kind": "identity", "complexity": 0})
    if len(set(after)) == 1:
        formulas.append({"kind": "constant", "value": after[0], "complexity": 1})
    deltas = [right - left for left, right in zip(before, after)]
    if len(set(deltas)) == 1 and deltas[0] != 0:
        formulas.append({"kind": "add_constant", "value": deltas[0], "complexity": 2})
    return min(formulas, key=lambda item: (item["complexity"], item["kind"], item.get("value", 0))) if formulas else None


def tree_score(tree: dict[str, Any]) -> tuple[int, int, str]:
    if tree["kind"] == "leaf":
        formula = tree["formula"]
        return (0, formula["complexity"], json.dumps(formula, sort_keys=True))
    left = tree_score(tree["true"])
    right = tree_score(tree["false"])
    return (1 + left[0] + right[0], left[1] + right[1], json.dumps(tree, sort_keys=True))


def fit_model_tree(samples: list[dict[str, Any]], max_depth: int = 1) -> dict[str, Any] | None:
    """Find the smallest exact tree with constant/identity/additive leaves."""
    formula = leaf_formula(samples)
    if formula is not None:
        return {"kind": "leaf", "formula": formula, "sample_count": len(samples)}
    if max_depth <= 0:
        return None
    thresholds = sorted(set(sample["before"] for sample in samples))[1:]
    candidates: list[dict[str, Any]] = []
    for threshold in thresholds:
        left = [sample for sample in samples if sample["before"] < threshold]
        right = [sample for sample in samples if sample["before"] >= threshold]
        if not left or not right:
            continue
        left_tree = fit_model_tree(left, max_depth - 1)
        right_tree = fit_model_tree(right, max_depth - 1)
        if left_tree is None or right_tree is None:
            continue
        candidates.append({
            "kind": "split", "feature": "ngksi_before", "operator": "<", "threshold": threshold,
            "true": left_tree, "false": right_tree, "sample_count": len(samples),
        })
    return min(candidates, key=tree_score) if candidates else None


def formula_text(formula: dict[str, Any]) -> str:
    if formula["kind"] == "identity":
        return "ngksi' = ngksi"
    if formula["kind"] == "constant":
        return f"ngksi' = {formula['value']}"
    return f"ngksi' = ngksi + {formula['value']}"


def tree_text(tree: dict[str, Any], indent: str = "") -> str:
    if tree["kind"] == "leaf":
        return indent + formula_text(tree["formula"])
    return "\n".join((
        indent + f"if ngksi_before < {tree['threshold']}:",
        tree_text(tree["true"], indent + "  "),
        indent + "else:",
        tree_text(tree["false"], indent + "  "),
    ))


def read_sequence_lines(path: Path) -> list[tuple[str, ...]]:
    lines = [tuple(line.split()) for line in path.read_text(encoding="utf-8").splitlines()]
    if not lines or any(not line for line in lines):
        raise ModelTreeError(f"Sequence file has an empty line: {path}")
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
                raise ModelTreeError(f"Trace line {trace_line}: invalid JSON.") from exc
            if not isinstance(record, dict) or not isinstance(record.get("sequence_id"), int):
                raise ModelTreeError(f"Trace line {trace_line}: missing integer sequence_id.")
            record["_trace_line"] = trace_line
            groups[record["sequence_id"]].append(record)
    for records in groups.values():
        records.sort(key=lambda record: record.get("step_id", -1))
    return groups


def cycle_variants(cycle_cover: dict[str, Any], cycle_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    export = cycle_cover.get("sequence_export", {})
    cycles = export.get("cycles", [])
    cycle = next((item for item in cycles if item.get("cycle_id") == cycle_id), None)
    if cycle is None:
        raise ModelTreeError(f"Cycle {cycle_id!r} is absent from sequence_export.")
    if not isinstance(cycle.get("variants"), list):
        raise ModelTreeError(f"Cycle {cycle_id!r} has no sequence variants.")
    return cycle, cycle["variants"]


def extract_samples(
    trace_groups: dict[int, list[dict[str, Any]]], sequence_lines: list[tuple[str, ...]],
    cycle: dict[str, Any], variants: list[dict[str, Any]], start_repeat: int, end_repeat: int,
    edge_offset: int, after_offset: int,
) -> list[dict[str, Any]]:
    prefix_length = cycle["prefix_length"]
    loop_length = cycle["loop_length"]
    if not 0 <= edge_offset < loop_length:
        raise ModelTreeError(f"edge_offset must be in [0, {loop_length - 1}].")
    selected: list[dict[str, Any]] = []
    for variant in variants:
        line_number = variant["line_number"]
        expected_inputs = sequence_lines[line_number - 1]
        matching = [
            (sequence_id, records) for sequence_id, records in trace_groups.items()
            if records and tuple(records[-1].get("sequence_inputs", [])) == expected_inputs
        ]
        if len(matching) != 1:
            raise ModelTreeError(f"C{cycle['cycle_id'][1:]} line {line_number}: expected one trace group, found {len(matching)}.")
        sequence_id, records = matching[0]
        if len(records) != len(expected_inputs):
            raise ModelTreeError(f"Trace sequence {sequence_id}: {len(records)} records for {len(expected_inputs)} inputs.")
        loop_inputs = variant["loop_inputs"]
        for repetition in range(start_repeat, end_repeat + 1):
            index = prefix_length + (repetition - 1) * loop_length + edge_offset
            after_index = index + after_offset
            if after_index >= len(records):
                raise ModelTreeError(f"C{cycle['cycle_id'][1:]} line {line_number}, repetition {repetition}: post-state is unavailable.")
            record = records[index]
            following = records[after_index]
            actual_input = get_path(record, "abstract_io.input")
            if actual_input != loop_inputs[edge_offset]:
                raise ModelTreeError(f"Trace line {record['_trace_line']}: expected {loop_inputs[edge_offset]!r}, got {actual_input!r}.")
            selected.append({
                "cycle_id": cycle["cycle_id"], "sequence_line": line_number,
                "trace_sequence_id": sequence_id, "repetition": repetition,
                "edge_offset": edge_offset, "trace_line_before": record["_trace_line"],
                "trace_line_after": following["_trace_line"],
                "before": integer_field(following, "ue_side.fields.registration_ksi_value", following["_trace_line"]),
                "after": integer_field(following, "downlink_side.fields.auth_request_ksi_value", following["_trace_line"]),
                "input": actual_input, "output": get_path(record, "abstract_io.output"),
                "following_input": get_path(following, "abstract_io.input"),
            })
    return selected


def learn(args: argparse.Namespace) -> dict[str, Any]:
    trace_path = Path(args.trace).resolve()
    cycle_path = Path(args.cycle_cover).resolve()
    sequence_path = Path(args.sequence_file).resolve()
    cycle_cover = json.loads(cycle_path.read_text(encoding="utf-8"))
    cycle, variants = cycle_variants(cycle_cover, args.cycle_id)
    samples = extract_samples(
        read_trace_groups(trace_path), read_sequence_lines(sequence_path), cycle, variants,
        args.start_repeat, args.end_repeat, args.edge_offset, args.after_offset,
    )
    by_line: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_line[sample["sequence_line"]].append(sample)
    learned: list[dict[str, Any]] = []
    for line_number, line_samples in sorted(by_line.items()):
        registration_tree = fit_model_tree(line_samples, args.max_depth)
        learned.append({
            "cycle_id": args.cycle_id, "sequence_line": line_number,
            "sample_count": len(line_samples), "samples": line_samples,
            "models": [
                {
                    "model_id": "registration_to_authentication_request_ksi",
                    "input_field": "ue_side.fields.registration_ksi_value",
                    "output_field": "downlink_side.fields.auth_request_ksi_value",
                    "status": "exact" if registration_tree else "no_exact_tree",
                    "tree": registration_tree,
                    "tree_text": tree_text(registration_tree) if registration_tree else None,
                    "scope": "AMF-visible Registration Request KSI to AMF Authentication Request KSI relation.",
                },
            ],
        })
    return {
        "schema_version": 1,
        "kind": "experimental-cycle-ngksi-model-tree",
        "limitations": [
            "Each .seq line is learned independently; no samples are pooled.",
            "The output observation is taken from the next explicitly adjacent trace record, not an inferred missing event.",
            "Leaves support only constant, identity and integer-additive formulas; no modulo formula is enabled.",
            "A fitted tree is a behavioral candidate, not a confirmed AMF register implementation.",
        ],
        "inputs": {
            "trace": {"path": str(trace_path), "sha256": sha256_file(trace_path)},
            "cycle_cover": {"path": str(cycle_path), "sha256": sha256_file(cycle_path)},
            "sequence_file": {"path": str(sequence_path), "sha256": sha256_file(sequence_path)},
        },
        "parameters": {
            "cycle_id": args.cycle_id, "repetitions": [args.start_repeat, args.end_repeat],
            "edge_offset": args.edge_offset, "after_offset": args.after_offset,
            "max_depth": args.max_depth,
        },
        "results": learned,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--cycle-cover", required=True)
    parser.add_argument("--sequence-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cycle-id", default="C01")
    parser.add_argument("--start-repeat", type=int, default=2)
    parser.add_argument("--end-repeat", type=int, default=10)
    parser.add_argument("--edge-offset", type=int, default=0)
    parser.add_argument("--after-offset", type=int, default=1)
    parser.add_argument("--max-depth", type=int, default=1)
    args = parser.parse_args(argv)
    if args.start_repeat < 1 or args.end_repeat < args.start_repeat or args.max_depth < 0:
        parser.error("Repeat range and max depth must be non-negative and ordered.")
    try:
        result = learn(args)
    except (ModelTreeError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"model-tree error: {exc}", file=sys.stderr)
        return 2
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for item in result["results"]:
        for model in item["models"]:
            print(f"{item['cycle_id']} line {item['sequence_line']} / {model['model_id']}: {model['status']}")
            if model["tree_text"]:
                print(model["tree_text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
