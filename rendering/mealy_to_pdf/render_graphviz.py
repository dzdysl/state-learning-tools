#!/usr/bin/env python3
"""Safely render DOT/GV files, optionally using the legacy Open5GS simplification rules."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


WINDOWS_GRAPHVIZ = Path(r"C:\Program Files\Graphviz\bin\dot.exe")
TRACE_MARKER = 'trace_step="'
TRACE_COLOR = "#d32f2f"

EDGE_RE = re.compile(
    r'^\s*(?P<src>s\d+)\s*->\s*(?P<dst>s\d+)\s*'
    r'\[label="(?P<label>[^"]*)"(?:\s+(?P<attrs>[^\]]*))?\]\s*;\s*$'
)


def locate_engine(engine: str) -> str:
    found = shutil.which(engine)
    if found:
        return found
    if engine == "dot" and WINDOWS_GRAPHVIZ.is_file():
        return str(WINDOWS_GRAPHVIZ)
    raise FileNotFoundError(
        f"Graphviz engine '{engine}' was not found. Install Graphviz or add it to PATH."
    )


def parse_formats(value: str) -> list[str]:
    formats: list[str] = []
    for item in value.split(","):
        output_format = item.strip().lower()
        if output_format and output_format not in formats:
            formats.append(output_format)
    if not formats:
        raise argparse.ArgumentTypeError("--formats must contain at least one format")
    return formats


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def split_edge_label(label: str) -> tuple[str, str]:
    """Return the single input and output symbols from a Mealy edge label."""
    plain_label = label.split(r"\n", 1)[0]
    if " / " not in plain_label:
        raise ValueError(f"not a Mealy edge label: {label!r}")
    input_symbol, output_symbol = (part.strip() for part in plain_label.split(" / ", 1))
    return input_symbol, output_symbol


def load_trace(path: Path, source: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("kind") != "mealy_simulation":
        raise ValueError(f"trace JSON is not a mealy_simulation payload: {path}")
    if payload.get("source_sha256") != sha256(source):
        raise ValueError(f"trace JSON source hash does not match DOT input: {path}")
    trace = payload.get("trace")
    if not isinstance(trace, list) or not trace:
        raise ValueError(f"trace JSON contains no simulation steps: {path}")
    required = {"step", "src", "dst", "input", "output"}
    for expected_step, item in enumerate(trace, 1):
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(f"invalid trace step {expected_step} in {path}")
        if item["step"] != expected_step or not all(isinstance(item[key], str) for key in required - {"step"}):
            raise ValueError(f"invalid trace step {expected_step} in {path}")
    return trace


def load_observed_outputs(path: Path, trace: list[dict[str, Any]]) -> dict[int, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("kind") != "mealy_trace_observations":
        raise ValueError(f"comparison JSON is not a mealy_trace_observations payload: {path}")
    runs = payload.get("observed_runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError(f"comparison JSON contains no observed runs: {path}")
    differences: dict[int, list[str]] = {}
    for index, run in enumerate(runs, 1):
        if not isinstance(run, dict) or not isinstance(run.get("name"), str) or not isinstance(run.get("output_sequence"), list):
            raise ValueError(f"invalid observed run {index} in {path}")
        outputs = run["output_sequence"]
        if len(outputs) != len(trace) or not all(isinstance(output, str) for output in outputs):
            raise ValueError(f"observed run {run['name']!r} does not contain {len(trace)} output symbols")
        for step, (expected, observed) in enumerate(zip(trace, outputs), 1):
            if expected["output"] != observed:
                differences.setdefault(step, []).append(f"{run['name']}={observed}")
    return {step: "Δ observed: " + ", ".join(outputs) for step, outputs in differences.items()}


def decorate_trace_lines(
    lines: list[str], trace: list[dict[str, Any]], annotations: dict[int, str], color: str,
) -> list[str]:
    """Style trace transitions before simplification so they are retained and unmerged."""
    by_edge: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for item in trace:
        key = (item["src"], item["dst"], item["input"], item["output"])
        by_edge.setdefault(key, []).append(item)
    seen: set[tuple[str, str, str, str]] = set()
    decorated: list[str] = []
    for line in lines:
        match = EDGE_RE.match(line.rstrip("\r\n"))
        if not match:
            decorated.append(line)
            continue
        try:
            input_symbol, output_symbol = split_edge_label(match.group("label"))
        except ValueError:
            decorated.append(line)
            continue
        key = (match.group("src"), match.group("dst"), input_symbol, output_symbol)
        steps = by_edge.get(key)
        if not steps:
            decorated.append(line)
            continue
        seen.add(key)
        step_numbers = ",".join(str(item["step"]) for item in steps)
        notes = [annotations[item["step"]] for item in steps if item["step"] in annotations]
        label = f"[{step_numbers}] {match.group('label')}"
        if notes:
            label += r"\nΔ observed mismatch"
        indent = line[: len(line) - len(line.lstrip())]
        eol = "\n" if line.endswith("\n") else ""
        decorated.append(
            f'{indent}{match.group("src")} -> {match.group("dst")} '
            f'[label="{label}" color="{color}" fontcolor="{color}" penwidth="3" '
            f'trace_step="{step_numbers}"];{eol}'
        )
    missing = set(by_edge) - seen
    if missing:
        rendered = ", ".join(f"{src}->{dst} {input_symbol}/{output}" for src, dst, input_symbol, output in sorted(missing))
        raise ValueError(f"trace transitions were not found in DOT input: {rendered}")
    return decorated


def apply_trace_node_styles(dot_path: Path, trace: list[dict[str, Any]], annotations: dict[int, str], color: str) -> None:
    """Highlight trace states and add a compact comparison legend to a derived DOT."""
    states = {trace[0]["src"], *(item["dst"] for item in trace)}
    node_re = re.compile(r'^(?P<indent>\s*)(?P<state>s\d+)\s*\[[^\]]*\];\s*$')
    lines = dot_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    styled: list[str] = []
    for line in lines:
        match = node_re.match(line.rstrip("\r\n"))
        if match and match.group("state") in states:
            eol = "\n" if line.endswith("\n") else ""
            styled.append(
                f'{match.group("indent")}{match.group("state")} '
                f'[shape="circle" label="{match.group("state")}" color="{color}" '
                f'fontcolor="{color}" penwidth="3"];{eol}'
            )
        else:
            styled.append(line)
    mismatch_summary = r"\n".join(f"step {step}: {note}" for step, note in sorted(annotations.items()))
    legend = f'Red = model trace ({len(trace)} steps)' + (r"\n" + mismatch_summary if mismatch_summary else r"\nno observed differences")
    escaped_legend = legend.replace('"', r'\"')
    insertion = f'\ttrace_legend [shape="note" color="{color}" fontcolor="{color}" label="{escaped_legend}"];\n'
    for index in range(len(styled) - 1, -1, -1):
        if styled[index].strip() == "}":
            styled.insert(index, insertion)
            break
    else:
        raise ValueError(f"DOT graph has no closing brace: {dot_path}")
    dot_path.write_text("".join(styled), encoding="utf-8")


def merge_dot_transitions(lines: list[str]) -> list[str]:
    """Merge edges having the same source, destination and output.

    For example, `a / out` and `b / out` become one edge labelled
    `a | b / out`. Non-matching Graphviz lines are retained unchanged.
    """
    edge_re = re.compile(
        r'^(?P<indent>\s*)'
        r'(?P<src>s\d+)\s*->\s*(?P<dst>s\d+)\s*'
        r'\[label="(?P<inp>[^"]*?)\s*/\s*(?P<out>[^"]*?)"\]\s*;'
        r'(?P<eol>\s*)$'
    )
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    line_keys: list[tuple[str, str, str] | None] = []
    for index, line in enumerate(lines):
        match = edge_re.match(line.rstrip("\r\n"))
        if not match:
            line_keys.append(None)
            continue
        key = (match.group("src"), match.group("dst"), match.group("out").strip())
        line_keys.append(key)
        group = groups.setdefault(
            key,
            {
                "indent": match.group("indent"),
                "inputs": [],
                "seen": set(),
                "first_index": index,
            },
        )
        input_symbol = match.group("inp").strip()
        if input_symbol not in group["seen"]:
            group["inputs"].append(input_symbol)
            group["seen"].add(input_symbol)

    output: list[str] = []
    emitted: set[tuple[str, str, str]] = set()
    for index, line in enumerate(lines):
        key = line_keys[index]
        if key is None:
            output.append(line)
            continue
        if key in emitted or groups[key]["first_index"] != index:
            continue
        source, destination, output_symbol = key
        input_symbols = " | ".join(groups[key]["inputs"])
        output.append(
            f'{groups[key]["indent"]}{source} -> {destination} '
            f'[label="{input_symbols} / {output_symbol}"];\n'
        )
        emitted.add(key)
    return output


def simplify_dot_file(
    source: Path,
    destination: Path,
    *,
    delete_self_loops: bool,
    delete_to_s0: bool,
    delete_null_sink_incoming: bool,
    merge_transitions: bool,
    trace: list[dict[str, Any]] | None = None,
    annotations: dict[int, str] | None = None,
    trace_color: str = TRACE_COLOR,
) -> None:
    """Apply the original `generate_PDF.py` simplification rules to a copy."""
    lines = source.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    if trace:
        lines = decorate_trace_lines(lines, trace, annotations or {}, trace_color)
    outgoing_outputs: dict[str, list[str]] = {}
    outgoing_destinations: dict[str, list[str]] = {}
    for line in lines:
        match = EDGE_RE.match(line.rstrip("\r\n"))
        if not match:
            continue
        label = match.group("label")
        if " / " in label:
            _, output_symbol = label.split(" / ", 1)
        else:
            parts = label.split("/", 1)
            output_symbol = parts[1] if len(parts) == 2 else ""
        source_state = match.group("src")
        outgoing_outputs.setdefault(source_state, []).append(output_symbol.strip())
        outgoing_destinations.setdefault(source_state, []).append(match.group("dst"))
    null_sink_states = {
        state
        for state, outputs in outgoing_outputs.items()
        if outputs
        and all(output == "null_action" for output in outputs)
        and all(destination_state == state for destination_state in outgoing_destinations[state])
    }

    self_loop_re = re.compile(r"^.*s([0-9]+)\s*->\s*s\1\s*\[.*?\];\s*$")
    to_s0_re = re.compile(r"^.*s([0-9]+)\s*->\s*s0\s*\[.*?\];\s*$")

    def should_delete(line: str) -> bool:
        stripped = line.rstrip("\r\n")
        # Trace edges remain visible even when they would normally be simplified away.
        if TRACE_MARKER in stripped:
            return False
        edge_match = EDGE_RE.match(stripped)
        if delete_null_sink_incoming and edge_match and edge_match.group("dst") in null_sink_states:
            return True
        # Preserve manually styled critical transitions, matching the legacy script.
        if 'color="blue"' in stripped:
            return False
        return (delete_self_loops and bool(self_loop_re.match(stripped))) or (
            delete_to_s0 and bool(to_s0_re.match(stripped))
        )

    filtered = [line for line in lines if not should_delete(line)]
    if merge_transitions:
        filtered = merge_dot_transitions(filtered)
    destination.write_text("".join(filtered), encoding="utf-8")


def parse_state_map(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    mapping: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.match(r"^\s*(s\d+)\s*->\s*(s\d+)\s*$", line)
        if match:
            mapping[match.group(1)] = match.group(2)
    return mapping


def parse_merge_report(path: Path) -> tuple[set[str], dict[str, str]]:
    if not path.is_file():
        return set(), {}
    representatives: set[str] = set()
    original_to_representative: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.match(r"^\s*(s\d+)\s*\|\s*(.*?)\s*$", line)
        if not match:
            continue
        representative = match.group(1)
        representatives.add(representative)
        for state in [representative, *re.findall(r"\bs\d+\b", match.group(2))]:
            original_to_representative[state] = representative
    return representatives, original_to_representative


def load_plot_metadata(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def map_state(state: str, original_to_representative: dict[str, str], state_map: dict[str, str]) -> str:
    return state_map.get(original_to_representative.get(state, state), original_to_representative.get(state, state))


def apply_ordered_state_styles(dot_path: Path, report_path: Path, map_path: Path, metadata_path: Path) -> None:
    representatives, original_to_representative = parse_merge_report(report_path)
    state_map = parse_state_map(map_path)
    metadata = load_plot_metadata(metadata_path)
    orange_states = {state_map.get(representative, representative) for representative in representatives}
    ab_states = set(metadata.get("A_clusters", [])) | set(metadata.get("B_clusters", [])) | set(metadata.get("indirect_ab_states", []))
    excluded = set(metadata.get("excluded_ab_states", []))
    green_states = {
        map_state(state, original_to_representative, state_map)
        for state in ab_states - excluded
    }
    green_states.difference_update(
        map_state(state, original_to_representative, state_map) for state in excluded
    )
    if not orange_states and not green_states:
        return
    node_re = re.compile(r'^(?P<indent>\s*)(?P<state>s\d+)\s*\[[^\]]*\];\s*$', re.MULTILINE)

    def replace_node(match: re.Match[str]) -> str:
        line, state, indent = match.group(0), match.group("state"), match.group("indent")
        if 'color="red"' in line:
            return line
        if state in green_states:
            return f'{indent}{state} [shape="circle" label="{state}" color="#2e7d32" penwidth="4"];'
        if state in orange_states:
            return f'{indent}{state} [shape="circle" label="{state}" color="#f28c28" penwidth="4"];'
        return line

    dot_path.write_text(node_re.sub(replace_node, dot_path.read_text(encoding="utf-8", errors="ignore")), encoding="utf-8")


def apply_critical_edge_styles(dot_path: Path, report_path: Path, metadata_path: Path, *, map_path: Path | None = None) -> None:
    metadata = load_plot_metadata(metadata_path)
    specifications = metadata.get("critical_transition_specs", [])
    if not specifications:
        return
    _, original_to_representative = parse_merge_report(report_path)
    state_map = parse_state_map(map_path) if map_path else {}
    critical_edges = {
        (
            map_state(spec["src"], original_to_representative, state_map),
            map_state(spec["dst"], original_to_representative, state_map),
            spec["input"],
            spec["output"],
        )
        for spec in specifications
    }
    edge_re = re.compile(
        r'^(?P<indent>\s*)(?P<src>s\d+)\s*->\s*(?P<dst>s\d+)\s*'
        r'\[label="(?P<label>.*?)"(?:\s+(?P<attrs>[^\]]*?))?\];\s*$'
    )

    def replace_edge(line: str) -> str:
        stripped, eol = line.rstrip("\r\n"), line[len(line.rstrip("\r\n")):]
        match = edge_re.match(stripped)
        if not match:
            return line
        label = match.group("label").split("\\n", 1)[0]
        if " / " not in label:
            return line
        input_symbol, output_text = (part.strip() for part in label.split(" / ", 1))
        candidates = {(match.group("src"), match.group("dst"), input_symbol, output) for output in output_text.split("|")}
        if not candidates & critical_edges:
            return line
        return (
            f'{match.group("indent")}{match.group("src")} -> {match.group("dst")} '
            f'[label="{match.group("label")}" color="blue" fontcolor="blue" penwidth="3"];{eol}'
        )

    lines = dot_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    dot_path.write_text("".join(replace_edge(line) for line in lines), encoding="utf-8")


def resolve_inputs(inputs: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    for source in inputs:
        path = source.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"DOT input does not exist or is not a file: {source}")
        resolved.append(path)
    return resolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+", help="one or more DOT/GV source files")
    parser.add_argument("--formats", type=parse_formats, default=parse_formats("svg,pdf"), help="comma-separated Graphviz formats (default: svg,pdf)")
    parser.add_argument("--output-dir", type=Path, help="directory for rendered files (default: each input's directory)")
    parser.add_argument("--basename", help="output basename; valid only with one input")
    parser.add_argument("--engine", default="dot", help="Graphviz layout engine (default: dot)")
    parser.add_argument("--overwrite", action="store_true", help="replace existing rendered/simplified outputs")
    parser.add_argument("--simplify", action="store_true", help="write a simplified copy before rendering, using the legacy Open5GS rules")
    parser.add_argument("--delete-self-loops", action=argparse.BooleanOptionalAction, default=True, help="remove sN -> sN edges in --simplify mode")
    parser.add_argument("--delete-to-s0", action=argparse.BooleanOptionalAction, default=True, help="remove sN -> s0 edges in --simplify mode")
    parser.add_argument("--delete-null-sink-incoming", action=argparse.BooleanOptionalAction, default=True, help="remove edges entering null-action self-loop sinks in --simplify mode")
    parser.add_argument("--merge-transitions", action=argparse.BooleanOptionalAction, default=True, help="merge matching transitions in --simplify mode")
    parser.add_argument("--ordered-styles", action="store_true", help="apply legacy merged-state and critical-edge styles in --simplify mode")
    parser.add_argument("--report-path", type=Path, help="legacy merged_result.txt path for --ordered-styles")
    parser.add_argument("--state-map-path", type=Path, help="legacy merged_result_state_map.txt path for --ordered-styles")
    parser.add_argument("--metadata-path", type=Path, help="legacy merged_result_plot_metadata.json path for styles")
    parser.add_argument("--trace-json", type=Path, help="mealy_simulation JSON whose transitions are highlighted in the derived graph")
    parser.add_argument("--comparison-json", type=Path, help="mealy_trace_observations JSON used to annotate trace/output differences")
    parser.add_argument("--trace-color", default=TRACE_COLOR, help=f"Graphviz color for trace edges and states (default: {TRACE_COLOR})")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.basename and len(args.inputs) != 1:
        raise ValueError("--basename can only be used with exactly one input")
    if args.ordered_styles and not args.simplify:
        raise ValueError("--ordered-styles requires --simplify")
    if args.comparison_json and not args.trace_json:
        raise ValueError("--comparison-json requires --trace-json")
    if args.trace_json and not args.simplify:
        raise ValueError("--trace-json requires --simplify")
    if args.trace_json and len(args.inputs) != 1:
        raise ValueError("--trace-json can only be used with exactly one DOT input")
    sources = resolve_inputs(args.inputs)
    trace: list[dict[str, Any]] | None = None
    annotations: dict[int, str] = {}
    if args.trace_json:
        trace = load_trace(args.trace_json.expanduser().resolve(), sources[0])
        if args.comparison_json:
            annotations = load_observed_outputs(args.comparison_json.expanduser().resolve(), trace)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else None
    planned: list[tuple[Path, Path, list[Path]]] = []
    for source in sources:
        destination_dir = output_dir or source.parent
        base = args.basename or source.stem
        rendered_base = f"{base}_smp" if args.simplify else base
        simplified = destination_dir / f"{rendered_base}.dot" if args.simplify else source
        targets = [destination_dir / f"{rendered_base}.{output_format}" for output_format in args.formats]
        planned.append((source, simplified, targets))
    protected = [path for _, simplified, targets in planned for path in ([simplified] if args.simplify else []) + targets if path.exists()]
    if protected and not args.overwrite:
        raise FileExistsError("Refusing to overwrite existing file(s). Use --overwrite if intended:\n" + "\n".join(f"  - {path}" for path in protected))

    engine = locate_engine(args.engine)
    result: list[dict[str, object]] = []
    for source, simplified, targets in planned:
        simplified.parent.mkdir(parents=True, exist_ok=True)
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
        render_source = source
        if args.simplify:
            simplify_dot_file(
                source,
                simplified,
                delete_self_loops=args.delete_self_loops,
                delete_to_s0=args.delete_to_s0,
                delete_null_sink_incoming=args.delete_null_sink_incoming,
                merge_transitions=args.merge_transitions,
                trace=trace,
                annotations=annotations,
                trace_color=args.trace_color,
            )
            if args.ordered_styles:
                base_dir = simplified.parent
                report = (args.report_path or base_dir / "merged_result.txt").expanduser().resolve()
                state_map = (args.state_map_path or base_dir / "merged_result_state_map.txt").expanduser().resolve()
                metadata = (args.metadata_path or base_dir / "merged_result_plot_metadata.json").expanduser().resolve()
                apply_ordered_state_styles(simplified, report, state_map, metadata)
                apply_critical_edge_styles(simplified, report, metadata, map_path=state_map)
            if trace:
                apply_trace_node_styles(simplified, trace, annotations, args.trace_color)
            render_source = simplified
        file_result: dict[str, object] = {
            "source": str(source), "source_sha256": sha256(source), "render_source": str(render_source),
            "simplified": args.simplify, "trace_json": str(args.trace_json) if trace else None,
            "comparison_json": str(args.comparison_json) if args.comparison_json else None,
            "outputs": [],
        }
        for target, output_format in zip(targets, args.formats):
            completed = subprocess.run([engine, f"-T{output_format}", str(render_source), "-o", str(target)], text=True, capture_output=True)
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or "no Graphviz diagnostic"
                raise RuntimeError(f"Graphviz failed for {render_source} as {output_format}: {detail}")
            if not target.is_file() or target.stat().st_size == 0:
                raise RuntimeError(f"Graphviz did not create a non-empty file: {target}")
            file_result["outputs"].append({"format": output_format, "path": str(target), "sha256": sha256(target), "bytes": target.stat().st_size})
        result.append(file_result)
    print(json.dumps({"renderer": engine, "engine": args.engine, "files": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
