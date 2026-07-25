from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


STATE_RE = re.compile(r"\b(s\d+)\s*\[")
EDGE_RE = re.compile(r'\b(s\d+)\s*->\s*(s\d+)\s*\[\s*label="([^"]+)"[^\]]*\]')


def state_key(state: str) -> int:
    return int(state[1:])


def split_label(label: str) -> tuple[list[str], str]:
    if " / " in label:
        left, output = label.split(" / ", 1)
    elif "/" in label:
        left, output = label.split("/", 1)
    else:
        raise ValueError(f"edge label has no input/output separator: {label!r}")
    return [item.strip() for item in left.split(" | ") if item.strip()], output.strip()


def parse_dot(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    states = set(STATE_RE.findall(text))
    outgoing: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for src, dst, label in EDGE_RE.findall(text):
        inputs, output = split_label(label)
        states.update((src, dst))
        for input_symbol in inputs:
            key = (src, input_symbol)
            if key in seen:
                raise ValueError(f"non-deterministic transition: {key}")
            seen.add(key)
            outgoing[src].append({"src": src, "dst": dst, "input": input_symbol, "output": output})
    if not states:
        raise ValueError(f"no states found in {path}")
    return {
        "states": sorted(states, key=state_key),
        "outgoing": dict(outgoing),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def bfs(model: dict[str, Any], start: str) -> tuple[dict[str, tuple[str, dict[str, str]]], set[str]]:
    if start not in model["states"]:
        raise ValueError(f"start state does not exist: {start}")
    predecessor: dict[str, tuple[str, dict[str, str]]] = {}
    visited = {start}
    queue = deque([start])
    while queue:
        state = queue.popleft()
        for edge in model["outgoing"].get(state, []):
            target = edge["dst"]
            if target in visited:
                continue
            visited.add(target)
            predecessor[target] = (state, edge)
            queue.append(target)
    return predecessor, visited


def reconstruct(predecessor: dict[str, tuple[str, dict[str, str]]], start: str, target: str) -> list[dict[str, str]]:
    trace: list[dict[str, str]] = []
    current = target
    while current != start:
        previous, edge = predecessor[current]
        trace.append(edge)
        current = previous
    trace.reverse()
    return trace


def format_input_sequence(input_sequence: list[str]) -> str:
    """Return the directly usable, space-delimited input sequence."""
    return " ".join(input_sequence) if input_sequence else "(空序列)"


def build_results(model: dict[str, Any], start: str, targets: list[str]) -> list[dict[str, Any]]:
    predecessor, visited = bfs(model, start)
    results = []
    for target in targets:
        if target not in model["states"]:
            results.append({"target": target, "reachable": False, "reason": "state_not_declared"})
        elif target not in visited:
            results.append({"target": target, "reachable": False, "reason": "unreachable"})
        else:
            trace = reconstruct(predecessor, start, target)
            input_sequence = [edge["input"] for edge in trace]
            results.append({
                "target": target,
                "reachable": True,
                "length": len(trace),
                "input_sequence": input_sequence,
                "input_sequence_text": format_input_sequence(input_sequence),
                "output_sequence": [edge["output"] for edge in trace],
                "state_sequence": [start, *[edge["dst"] for edge in trace]],
                "trace": trace,
            })
    return results


def write_report(payload: dict[str, Any], path: Path) -> None:
    lines = [
        f"# {Path(payload['source_dot']).stem} 最短可达序列", "",
        f"- 起点：`{payload['start_state']}`", f"- DOT SHA-256：`{payload['source_sha256']}`", "",
    ]
    for item in payload["results"]:
        lines.extend([f"## `{item['target']}`", ""])
        if not item["reachable"]:
            lines.extend([f"不可达：`{item['reason']}`。", ""])
            continue
        lines.extend([f"- 长度：{item['length']}", f"- 输入序列：`{item['input_sequence_text']}`", "", "| 步骤 | 源状态 | 输入 | 输出 | 目标状态 |", "|---:|---|---|---|---|"])
        for index, edge in enumerate(item["trace"], 1):
            lines.append(f"| {index} | `{edge['src']}` | `{edge['input']}` | `{edge['output']}` | `{edge['dst']}` |")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find shortest access sequences in a Mealy DOT")
    parser.add_argument("--dot", type=Path, required=True)
    parser.add_argument("--start", default="s0")
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--basename")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    model = parse_dot(args.dot)
    targets = model["states"] if args.all else args.target
    if not targets:
        raise ValueError("provide at least one --target or use --all")
    results = build_results(model, args.start, targets)
    payload = {
        "schema_version": 1,
        "kind": "mealy_shortest_paths",
        "source_dot": str(args.dot.resolve()),
        "source_sha256": model["sha256"],
        "start_state": args.start,
        "results": results,
    }
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        base = args.basename or args.dot.stem
        json_path = args.output_dir / f"{base}_shortest_paths.json"
        report_path = args.output_dir / f"{base}_shortest_paths.md"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_report(payload, report_path)
        payload["artifacts"] = [str(json_path), str(report_path)]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
