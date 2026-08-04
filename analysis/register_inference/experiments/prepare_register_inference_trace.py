"""Materialize and validate the lossless JSONL input for register inference.

The register inference adapter consumes the runner's complete
``statelearner_trace.jsonl`` directly.  This tool does not clean, flatten,
deduplicate, reorder, or decode the trace into another schema.  It validates
the exact grouping contract used by ``infer_cycle_ngksi_regions.py``, then
copies the source bytes into the experiment evidence area and writes a
provenance manifest for that materialization.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Any

import yaml


class TracePreparationError(ValueError):
    """Raised when raw runner data cannot satisfy the inference input contract."""


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve(config_path: Path, text: str) -> Path:
    candidate = Path(text)
    return candidate if candidate.is_absolute() else (config_path.parent / candidate).resolve()


def read_config(path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TracePreparationError(f"Invalid YAML config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise TracePreparationError("Config must be a YAML mapping.")
    inputs = config.get("inputs")
    if not isinstance(inputs, dict):
        raise TracePreparationError("Config must contain an inputs mapping.")
    for key in ("trace", "cycle_cover", "sequence_file"):
        if not isinstance(inputs.get(key), str) or not inputs[key]:
            raise TracePreparationError(f"Config inputs.{key} must be a non-empty path string.")
    return config


def read_sequence_lines(path: Path) -> list[tuple[str, ...]]:
    if not path.is_file():
        raise TracePreparationError(f"Sequence file does not exist: {path}")
    lines = [tuple(line.split()) for line in path.read_text(encoding="utf-8").splitlines()]
    if not lines or any(not line for line in lines):
        raise TracePreparationError(f"Sequence file has an empty line: {path}")
    return lines


def read_trace_groups(path: Path) -> tuple[dict[int, list[dict[str, Any]]], int]:
    if not path.is_file():
        raise TracePreparationError(f"Source trace does not exist: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise TracePreparationError(f"Source trace is not UTF-8: {path}") from exc
    if not lines:
        raise TracePreparationError(f"Source trace is empty: {path}")
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for trace_line, text in enumerate(lines, start=1):
        if not text.strip():
            raise TracePreparationError(f"Trace line {trace_line}: blank lines are not permitted.")
        try:
            record = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TracePreparationError(f"Trace line {trace_line}: invalid JSON.") from exc
        if not isinstance(record, dict):
            raise TracePreparationError(f"Trace line {trace_line}: expected a JSON object.")
        sequence_id = record.get("sequence_id")
        if not isinstance(sequence_id, int) or isinstance(sequence_id, bool):
            raise TracePreparationError(f"Trace line {trace_line}: missing integer sequence_id.")
        sequence_inputs = record.get("sequence_inputs")
        if not isinstance(sequence_inputs, list) or not all(isinstance(item, str) and item for item in sequence_inputs):
            raise TracePreparationError(f"Trace line {trace_line}: sequence_inputs must be a non-empty string list.")
        abstract_io = record.get("abstract_io")
        if not isinstance(abstract_io, dict) or not isinstance(abstract_io.get("input"), str):
            raise TracePreparationError(f"Trace line {trace_line}: abstract_io.input must be a string.")
        groups[sequence_id].append(record)
    return dict(groups), len(lines)


def selected_cycle_variants(config: dict[str, Any], config_path: Path) -> tuple[list[tuple[str, int]], Path]:
    cycle_path = resolve(config_path, config["inputs"]["cycle_cover"])
    try:
        cycle_cover = json.loads(cycle_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TracePreparationError(f"Cycle-cover JSON does not exist: {cycle_path}") from exc
    except json.JSONDecodeError as exc:
        raise TracePreparationError(f"Cycle-cover JSON is invalid: {cycle_path}") from exc
    cycles = cycle_cover.get("sequence_export", {}).get("cycles")
    if not isinstance(cycles, list):
        raise TracePreparationError("cycle_cover.sequence_export.cycles must be a list; routes is not supported.")
    wanted = config.get("analysis", {}).get("cycle_ids")
    if wanted is not None and (
        not isinstance(wanted, list) or not all(isinstance(cycle_id, str) and cycle_id for cycle_id in wanted)
    ):
        raise TracePreparationError("analysis.cycle_ids must be a list of non-empty strings when present.")
    chosen = [cycle for cycle in cycles if wanted is None or cycle.get("cycle_id") in wanted]
    chosen_ids = {cycle.get("cycle_id") for cycle in chosen}
    if wanted is not None and chosen_ids != set(wanted):
        raise TracePreparationError("At least one configured cycle_id is absent from cycle_cover.sequence_export.cycles.")
    if not chosen:
        raise TracePreparationError("No cycles selected for trace preparation.")
    variants: list[tuple[str, int]] = []
    for cycle in chosen:
        cycle_id = cycle.get("cycle_id")
        if not isinstance(cycle_id, str) or not cycle_id:
            raise TracePreparationError("Every selected cycle must have a non-empty cycle_id.")
        entries = cycle.get("variants")
        if not isinstance(entries, list) or not entries:
            raise TracePreparationError(f"{cycle_id}: variants must be a non-empty list.")
        for variant in entries:
            line_number = variant.get("line_number") if isinstance(variant, dict) else None
            if not isinstance(line_number, int):
                raise TracePreparationError(f"{cycle_id}: every variant requires integer line_number.")
            variants.append((cycle_id, line_number))
    return variants, cycle_path


def validate_selected_groups(
    groups: dict[int, list[dict[str, Any]]],
    sequence_lines: list[tuple[str, ...]],
    variants: list[tuple[str, int]],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for cycle_id, line_number in variants:
        if not 1 <= line_number <= len(sequence_lines):
            raise TracePreparationError(f"{cycle_id} line {line_number}: outside the sequence file.")
        expected = sequence_lines[line_number - 1]
        matches = [
            (sequence_id, records)
            for sequence_id, records in groups.items()
            if tuple(records[-1]["sequence_inputs"]) == expected
        ]
        if len(matches) != 1:
            raise TracePreparationError(
                f"{cycle_id} line {line_number}: expected exactly one trace group, found {len(matches)}."
            )
        sequence_id, records = matches[0]
        if len(records) != len(expected):
            raise TracePreparationError(
                f"Trace sequence {sequence_id}: {len(records)} records for {len(expected)} inputs."
            )
        for offset, (record, input_symbol) in enumerate(zip(records, expected), start=1):
            observed = record["abstract_io"]["input"]
            if observed != input_symbol:
                raise TracePreparationError(
                    f"Trace sequence {sequence_id} step {offset}: expected {input_symbol!r}, got {observed!r}."
                )
        checks.append(
            {
                "cycle_id": cycle_id,
                "sequence_line": line_number,
                "trace_sequence_id": sequence_id,
                "input_count": len(expected),
            }
        )
    return checks


def materialize(source: Path, target: Path) -> str:
    source_hash = sha256_file(source)
    if target.exists():
        if sha256_file(target) != source_hash:
            raise TracePreparationError(
                f"Evidence trace already exists with different bytes; do not overwrite evidence: {target}"
            )
        return "verified_existing_byte_identical"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if sha256_file(target) != source_hash:
        raise TracePreparationError(f"Byte-preserving copy check failed: {target}")
    return "byte_preserving_copy"


def write_manifest(path: Path, payload: dict[str, Any], overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise TracePreparationError(f"Manifest already exists; pass --overwrite to replace it: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Register-inference YAML configuration")
    parser.add_argument("--source-trace", required=True, help="Complete raw statelearner_trace.jsonl")
    parser.add_argument("--evidence-trace", required=True, help="Byte-identical evidence trace destination")
    parser.add_argument("--manifest", required=True, help="Derived JSON conversion manifest")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing the derived manifest only")
    args = parser.parse_args(argv)
    try:
        config_path = Path(args.config).resolve()
        config = read_config(config_path)
        source = Path(args.source_trace).resolve()
        target = Path(args.evidence_trace).resolve()
        expected_target = resolve(config_path, config["inputs"]["trace"])
        if target != expected_target:
            raise TracePreparationError(
                "--evidence-trace must equal config inputs.trace so inference consumes the materialized evidence."
            )
        groups, record_count = read_trace_groups(source)
        sequence_path = resolve(config_path, config["inputs"]["sequence_file"])
        sequence_lines = read_sequence_lines(sequence_path)
        variants, cycle_path = selected_cycle_variants(config, config_path)
        selected_checks = validate_selected_groups(groups, sequence_lines, variants)
        operation = materialize(source, target)
        source_hash = sha256_file(source)
        payload = {
            "schema_version": 1,
            "kind": "lossless-register-inference-trace-materialization",
            "operation": operation,
            "source": {"path": str(source), "bytes": source.stat().st_size, "sha256": source_hash},
            "evidence_trace": {"path": str(target), "bytes": target.stat().st_size, "sha256": sha256_file(target)},
            "contract": {
                "encoding": "utf-8 JSON Lines",
                "record_count": record_count,
                "sequence_group_count": len(groups),
                "sequence_ids": sorted(groups),
                "sequence_file": str(sequence_path),
                "sequence_file_sha256": sha256_file(sequence_path),
                "sequence_line_count": len(sequence_lines),
                "cycle_cover": str(cycle_path),
                "cycle_cover_sha256": sha256_file(cycle_path),
                "sequence_export_key": "sequence_export.cycles",
                "selected_variant_count": len(selected_checks),
                "selected_variant_checks": selected_checks,
            },
            "preservation": {
                "payload_transformation": "none",
                "permitted_inference_conversion": [
                    "in-memory integer parsing of configured field values",
                    "in-memory boolean parsing of configured signal values",
                    "in-memory trace_line annotation",
                ],
                "forbidden_inputs": ["statelearner_trace.cleaned.jsonl", "filtered JSONL", "reordered JSONL"],
            },
        }
        write_manifest(Path(args.manifest).resolve(), payload, args.overwrite)
        print(
            f"Materialized {record_count} records in {len(groups)} groups; "
            f"validated {len(selected_checks)} selected cycle variants."
        )
        return 0
    except TracePreparationError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
