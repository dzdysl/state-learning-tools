"""CLI for configurable preparation, candidate generation and Z3 fitting."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from candidates import generate_simple_scalar_candidates
from config import load_config
from contracts import RegisterInferenceError
from fit import fit_scalar_edge_candidates
from prepare import prepare


def write_json(path: Path, content: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegisterInferenceError(f"Invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RegisterInferenceError(f"JSON root must be an object: {path}")
    return value


def priority_from_config(config: dict[str, Any]) -> list[str] | None:
    generator = config.get("candidate_generator", {})
    if not generator:
        return None
    if not isinstance(generator, dict) or generator.get("implementation", "simple_scalar_v1") != "simple_scalar_v1":
        raise RegisterInferenceError("Only candidate_generator implementation simple_scalar_v1 is available.")
    priority = generator.get("priority")
    if priority is not None and (not isinstance(priority, list) or not all(isinstance(item, str) for item in priority)):
        raise RegisterInferenceError("candidate_generator.priority must be a list of strings.")
    return priority


def command_prepare(args: argparse.Namespace) -> None:
    config_path = Path(args.config).resolve()
    write_json(Path(args.output).resolve(), prepare(load_config(config_path), config_path))


def command_candidates(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config).resolve())
    prepared = read_json(Path(args.prepared).resolve())
    write_json(Path(args.output).resolve(), generate_simple_scalar_candidates(prepared, priority_from_config(config)))


def command_fit(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config).resolve())
    fitter = config.get("fitter", {})
    if fitter and (not isinstance(fitter, dict) or fitter.get("implementation", "z3_scalar_edge_v1") != "z3_scalar_edge_v1"):
        raise RegisterInferenceError("Only fitter implementation z3_scalar_edge_v1 is available.")
    write_json(Path(args.output).resolve(), fit_scalar_edge_candidates(read_json(Path(args.prepared).resolve()), read_json(Path(args.candidates).resolve())))


def command_run(args: argparse.Namespace) -> None:
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    output_dir = Path(args.output_dir).resolve()
    prepared = prepare(config, config_path)
    candidates = generate_simple_scalar_candidates(prepared, priority_from_config(config))
    fitter = config.get("fitter", {})
    if fitter and (not isinstance(fitter, dict) or fitter.get("implementation", "z3_scalar_edge_v1") != "z3_scalar_edge_v1"):
        raise RegisterInferenceError("Only fitter implementation z3_scalar_edge_v1 is available.")
    fit_result = fit_scalar_edge_candidates(prepared, candidates)
    write_json(output_dir / "prepared.json", prepared)
    write_json(output_dir / "candidates.json", candidates)
    write_json(output_dir / "fit.json", fit_result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="clean and map raw observations")
    prepare_parser.add_argument("--config", required=True)
    prepare_parser.add_argument("--output", required=True)
    prepare_parser.set_defaults(handler=command_prepare)
    candidates_parser = commands.add_parser("candidates", help="generate formula candidates")
    candidates_parser.add_argument("--config", required=True)
    candidates_parser.add_argument("--prepared", required=True)
    candidates_parser.add_argument("--output", required=True)
    candidates_parser.set_defaults(handler=command_candidates)
    fit_parser = commands.add_parser("fit", help="fit candidates with Z3")
    fit_parser.add_argument("--config", required=True)
    fit_parser.add_argument("--prepared", required=True)
    fit_parser.add_argument("--candidates", required=True)
    fit_parser.add_argument("--output", required=True)
    fit_parser.set_defaults(handler=command_fit)
    run_parser = commands.add_parser("run", help="run all v1 stages")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--output-dir", required=True)
    run_parser.set_defaults(handler=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.handler(args)
        return 0
    except RegisterInferenceError as exc:
        print(f"register inference error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
