from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import quote


STATE_RE = re.compile(r"\b(s\d+)\s*\[")
EDGE_RE = re.compile(r'\b(s\d+)\s*->\s*(s\d+)\s*\[\s*label="([^"]+)"[^\]]*\]')


def state_key(state: str) -> int:
    return int(state[1:])


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
    for src, dst, label in EDGE_RE.findall(text):
        inputs, output = split_label(label)
        states.update((src, dst))
        for input_symbol in inputs:
            if input_symbol in outgoing[src]:
                raise ValueError(f"non-deterministic transition for ({src}, {input_symbol})")
            outgoing[src][input_symbol] = {"src": src, "dst": dst, "input": input_symbol, "output": output}
            if input_symbol not in input_order:
                input_order.append(input_symbol)
    if not states:
        raise ValueError(f"no states found in {path}")
    for state in states:
        missing = [symbol for symbol in input_order if symbol not in outgoing.get(state, {})]
        if missing:
            raise ValueError(f"incomplete input alphabet at {state}: {missing}")
    return {
        "states": sorted(states, key=state_key), "outgoing": dict(outgoing),
        "input_order": input_order, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def simulate(model: dict[str, Any], start: str, sequence: list[str]) -> dict[str, Any]:
    if start not in model["states"]:
        raise ValueError(f"unknown start state: {start}")
    current = start
    trace = []
    for index, input_symbol in enumerate(sequence, 1):
        edge = model["outgoing"][current].get(input_symbol)
        if edge is None:
            raise ValueError(f"no {input_symbol} transition from {current}")
        trace.append({"step": index, **edge})
        current = edge["dst"]
    return {
        "start_state": start, "input_sequence": sequence,
        "output_sequence": [edge["output"] for edge in trace],
        "final_state": current, "trace": trace,
    }


def shortest_access(model: dict[str, Any], start: str, target: str) -> list[str] | None:
    queue = deque([(start, [])])
    visited = {start}
    while queue:
        state, sequence = queue.popleft()
        if state == target:
            return sequence
        for input_symbol in model["input_order"]:
            target_state = model["outgoing"][state][input_symbol]["dst"]
            if target_state not in visited:
                visited.add(target_state)
                queue.append((target_state, [*sequence, input_symbol]))
    return None


def distinguish(model: dict[str, Any], left: str, right: str) -> dict[str, Any]:
    if left not in model["states"] or right not in model["states"]:
        raise ValueError(f"unknown state pair: {left}, {right}")
    queue = deque([((left, right), [])])
    visited = {(left, right)}
    while queue:
        (state_l, state_r), prefix = queue.popleft()
        for input_symbol in model["input_order"]:
            edge_l = model["outgoing"][state_l][input_symbol]
            edge_r = model["outgoing"][state_r][input_symbol]
            step = {
                "input": input_symbol,
                "left": {"src": state_l, "dst": edge_l["dst"], "output": edge_l["output"]},
                "right": {"src": state_r, "dst": edge_r["dst"], "output": edge_r["output"]},
            }
            if edge_l["output"] != edge_r["output"]:
                sequence = [*prefix, input_symbol]
                left_trace = simulate(model, left, sequence)
                right_trace = simulate(model, right, sequence)
                return {
                    "equivalent": False, "length": len(sequence), "input_sequence": sequence,
                    "first_output_difference_index": len(sequence),
                    "left_output": edge_l["output"], "right_output": edge_r["output"],
                    "left_trace": left_trace["trace"], "right_trace": right_trace["trace"],
                }
            pair = (edge_l["dst"], edge_r["dst"])
            if pair not in visited:
                visited.add(pair)
                queue.append((pair, [*prefix, input_symbol]))
    return {"equivalent": True, "visited_product_pairs": len(visited)}


def load_refinement(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("kind") != "mealy_refinement":
        raise ValueError(f"not a mealy_refinement JSON: {path}")
    return data


def groups_for_round(refinement: dict[str, Any], round_index: int | None) -> dict[str, list[str]]:
    if round_index is None:
        return refinement["final_groups"]
    if round_index == 0:
        return refinement["initial_groups"]
    for item in refinement["rounds"]:
        if item["round"] == round_index:
            return item["groups"]
    raise ValueError(f"round not found in refinement JSON: {round_index}")


def resolve_side(spec: str, model: dict[str, Any], refinement: dict[str, Any] | None, round_index: int | None) -> list[str]:
    if spec in model["states"]:
        return [spec]
    if refinement is None:
        raise ValueError(f"{spec!r} is not a state; provide --refinement-json to resolve class labels")
    groups = groups_for_round(refinement, round_index)
    if spec not in groups:
        raise ValueError(f"class {spec!r} not found in selected refinement round")
    return groups[spec]


def earliest_split(refinement: dict[str, Any] | None, left: str, right: str) -> dict[str, Any] | None:
    if refinement is None:
        return None
    stages = [(0, refinement["initial_groups"])] + [
        (item["round"], item["groups"]) for item in refinement["rounds"]
    ]
    previous_labels = None
    for round_index, groups in stages:
        mapping = {state: label for label, states in groups.items() for state in states}
        labels = (mapping[left], mapping[right])
        if labels[0] != labels[1]:
            detail = {"round": round_index, "left_class": labels[0], "right_class": labels[1]}
            if previous_labels is not None:
                detail["previous_class"] = previous_labels[0]
            if round_index > 0:
                round_data = next(item for item in refinement["rounds"] if item["round"] == round_index)
                for split in round_data["splits"]:
                    child_names = {child["name"] for child in split["children"]}
                    if labels[0] in child_names and labels[1] in child_names:
                        detail["split"] = split
                        break
            return detail
        previous_labels = labels
    return None


def choose_table(connection: sqlite3.Connection, explicit: str | None) -> str:
    if explicit:
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{explicit}")')}
        if not {"command", "result"}.issubset(columns):
            raise ValueError(f"table {explicit!r} does not contain command/result columns")
        return explicit
    candidates = []
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}
        if {"command", "result"}.issubset(columns):
            count = connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            candidates.append((count, name))
    if not candidates:
        raise ValueError("no SQLite table with command/result columns found")
    return max(candidates)[1]


def readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()).replace('\\', '/'), safe='/:')}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def result_tokens(row: tuple[str] | None) -> list[str] | None:
    return None if row is None else row[0].split()


def sqlite_evidence(database: Path, table_name: str | None, model: dict[str, Any], left: str, right: str, suffix: list[str]) -> dict[str, Any]:
    access_l = shortest_access(model, "s0", left)
    access_r = shortest_access(model, "s0", right)
    if access_l is None or access_r is None:
        return {"available": False, "reason": "one_or_both_states_unreachable_from_s0"}
    with readonly_connection(database) as connection:
        table = choose_table(connection, table_name)
        command_l = " ".join([*access_l, *suffix])
        command_r = " ".join([*access_r, *suffix])
        row_l = connection.execute(f'SELECT result FROM "{table}" WHERE command=?', (command_l,)).fetchone()
        row_r = connection.execute(f'SELECT result FROM "{table}" WHERE command=?', (command_r,)).fetchone()
        outputs_l, outputs_r = result_tokens(row_l), result_tokens(row_r)
        evidence: dict[str, Any] = {
            "available": row_l is not None and row_r is not None,
            "table": table, "left_access": access_l, "right_access": access_r,
            "suffix": suffix, "left_command": command_l, "right_command": command_r,
            "left_result": outputs_l, "right_result": outputs_r,
        }
        if evidence["available"]:
            tail_l = outputs_l[len(access_l):]
            tail_r = outputs_r[len(access_r):]
            evidence["left_suffix_outputs"] = tail_l
            evidence["right_suffix_outputs"] = tail_r
            evidence["first_suffix_difference_index"] = next(
                (index + 1 for index, (a, b) in enumerate(zip(tail_l, tail_r)) if a != b), None
            )
            return evidence
        rows = connection.execute(f'SELECT command, result FROM "{table}"').fetchall()
        def extensions(prefix: list[str]) -> dict[tuple[str, ...], list[str]]:
            found = {}
            for command, result in rows:
                commands = command.split()
                results = result.split()
                if commands[:len(prefix)] == prefix and len(commands) > len(prefix):
                    found[tuple(commands[len(prefix):])] = results[len(prefix):]
            return found
        ext_l, ext_r = extensions(access_l), extensions(access_r)
        order = {symbol: index for index, symbol in enumerate(model["input_order"])}
        common = sorted(set(ext_l) & set(ext_r), key=lambda seq: (len(seq), tuple(order.get(x, 10**6) for x in seq), seq))
        observed = next((seq for seq in common if ext_l[seq] != ext_r[seq]), None)
        if observed:
            evidence["recorded_common_suffix"] = list(observed)
            evidence["recorded_left_outputs"] = ext_l[observed]
            evidence["recorded_right_outputs"] = ext_r[observed]
        return evidence


def write_distinction_report(payload: dict[str, Any], path: Path) -> None:
    lines = [
        f"# {Path(payload['source_dot']).stem} 状态区分报告", "",
        f"- 左侧：`{payload['left_spec']}` = {{{', '.join(payload['left_states'])}}}",
        f"- 右侧：`{payload['right_spec']}` = {{{', '.join(payload['right_states'])}}}",
        f"- 可合并：{'是' if payload['all_equivalent'] else '否'}", "",
        "## 状态对检查", "",
    ]
    for item in payload["pair_results"]:
        result = item["distinction"]
        lines.append(f"### `{item['left']}` / `{item['right']}`")
        lines.append("")
        if result["equivalent"]:
            lines.append("在该确定性模型内未找到可观察区分序列。")
        else:
            lines.append(f"- 最短区分输入：`{' '.join(result['input_sequence'])}`")
            lines.append(f"- 首个不同输出：`{result['left_output']}` / `{result['right_output']}`")
        if item.get("earliest_split"):
            split = item["earliest_split"]
            lines.append(f"- 首次分类分开：第 {split['round']} 轮，`{split['left_class']}` / `{split['right_class']}`")
        lines.append("")
    evidence = payload.get("sqlite_evidence")
    if evidence:
        lines.extend(["## SQLite证据", "", f"- 表：`{evidence.get('table')}`", f"- 精确查询均存在：{evidence.get('available')}"])
        if evidence.get("available"):
            lines.append(f"- 左侧后缀输出：`{' '.join(evidence['left_suffix_outputs'])}`")
            lines.append(f"- 右侧后缀输出：`{' '.join(evidence['right_suffix_outputs'])}`")
        elif evidence.get("recorded_common_suffix"):
            lines.append(f"- 数据库内最短公共区分后缀：`{' '.join(evidence['recorded_common_suffix'])}`")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--basename")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate and distinguish Mealy states")
    sub = parser.add_subparsers(dest="command", required=True)
    sim = sub.add_parser("simulate")
    sim.add_argument("--dot", type=Path, required=True)
    sim.add_argument("--start", default="s0")
    sim.add_argument("--sequence", nargs="+", required=True)
    add_output_args(sim)
    dist = sub.add_parser("distinguish")
    dist.add_argument("--dot", type=Path, required=True)
    dist.add_argument("--left", required=True)
    dist.add_argument("--right", required=True)
    dist.add_argument("--refinement-json", type=Path)
    dist.add_argument("--round", type=int)
    dist.add_argument("--database", type=Path)
    dist.add_argument("--table")
    add_output_args(dist)
    return parser.parse_args()


def write_payload(payload: dict[str, Any], output_dir: Path | None, basename: str, suffix: str, report_writer=None) -> None:
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / f"{basename}_{suffix}.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        artifacts = [str(json_path)]
        if report_writer:
            report_path = output_dir / f"{basename}_{suffix}.md"
            report_writer(payload, report_path)
            artifacts.append(str(report_path))
        payload["artifacts"] = artifacts
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    args = parse_args()
    model = parse_dot(args.dot)
    base = args.basename or args.dot.stem
    if args.command == "simulate":
        payload = {
            "schema_version": 1, "kind": "mealy_simulation", "source_dot": str(args.dot.resolve()),
            "source_sha256": model["sha256"], **simulate(model, args.start, args.sequence),
        }
        write_payload(payload, args.output_dir, base, "simulation")
        return
    refinement = load_refinement(args.refinement_json)
    left_states = resolve_side(args.left, model, refinement, args.round)
    right_states = resolve_side(args.right, model, refinement, args.round)
    pair_results = []
    for left in left_states:
        for right in right_states:
            pair_results.append({
                "left": left, "right": right, "distinction": distinguish(model, left, right),
                "earliest_split": earliest_split(refinement, left, right),
            })
    all_equivalent = all(item["distinction"]["equivalent"] for item in pair_results)
    payload = {
        "schema_version": 1, "kind": "mealy_state_distinction", "source_dot": str(args.dot.resolve()),
        "source_sha256": model["sha256"], "left_spec": args.left, "right_spec": args.right,
        "left_states": left_states, "right_states": right_states, "all_equivalent": all_equivalent,
        "pair_results": pair_results,
    }
    non_equivalent = [item for item in pair_results if not item["distinction"]["equivalent"]]
    if args.database and len(left_states) == len(right_states) == 1 and non_equivalent:
        shortest = min(non_equivalent, key=lambda item: item["distinction"]["length"])
        payload["sqlite_evidence"] = sqlite_evidence(
            args.database, args.table, model, shortest["left"], shortest["right"],
            shortest["distinction"]["input_sequence"],
        )
    write_payload(payload, args.output_dir, base, "state_distinction", write_distinction_report)


if __name__ == "__main__":
    main()
