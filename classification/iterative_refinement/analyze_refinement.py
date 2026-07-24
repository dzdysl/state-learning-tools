from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any


STATE_RE = re.compile(r"\b(s\d+)\s*\[")
EDGE_RE = re.compile(r'\b(s\d+)\s*->\s*(s\d+)\s*\[\s*label="([^"]+)"[^\]]*\]')
PREFIX_ORDER = {"A": 0, "D": 1, "N": 2, "NG": 3, "S": 4, "R": 5, "X": 6}
PREFIX_COLORS = {
    "A": "#DCEBFF", "D": "#FFE1E1", "N": "#DFF3E4", "NG": "#EBDDFA",
    "S": "#FFF0BD", "R": "#DDF5F7", "X": "#ECEFF1",
}


def state_key(state: str) -> int:
    return int(state[1:])


def label_parts(label: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Za-z]+)(\d*)", label)
    if not match:
        return label, 0
    return match.group(1), int(match.group(2) or 0)


def label_key(label: str) -> tuple[int, int, str]:
    prefix, number = label_parts(label)
    return PREFIX_ORDER.get(prefix, 99), number, label


def split_edge_label(label: str) -> tuple[list[str], str]:
    if " / " in label:
        left, output = label.split(" / ", 1)
    elif "/" in label:
        left, output = label.split("/", 1)
    else:
        raise ValueError(f"edge label has no input/output separator: {label!r}")
    inputs = [item.strip() for item in left.split(" | ") if item.strip()]
    if not inputs:
        raise ValueError(f"edge label has no input: {label!r}")
    return inputs, output.strip()


def parse_dot(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="strict")
    states = set(STATE_RE.findall(text))
    outgoing: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    source_order: list[str] = []
    input_order_by_state: dict[str, list[str]] = defaultdict(list)
    edges: list[dict[str, str]] = []
    for src, dst, label in EDGE_RE.findall(text):
        inputs, output = split_edge_label(label)
        states.update((src, dst))
        if src not in source_order:
            source_order.append(src)
        for input_symbol in inputs:
            if input_symbol in outgoing[src]:
                previous = outgoing[src][input_symbol]
                raise ValueError(
                    f"non-deterministic transition for ({src}, {input_symbol}): "
                    f"{previous['dst']} and {dst}"
                )
            item = {"src": src, "dst": dst, "input": input_symbol, "output": output}
            outgoing[src][input_symbol] = item
            input_order_by_state[src].append(input_symbol)
            edges.append(item)
    if not states or not edges:
        raise ValueError(f"no Mealy states or transitions found in {path}")
    anchor = next((state for state in source_order if outgoing[state]), None)
    if anchor is None:
        raise ValueError("no state has outgoing transitions")
    input_order = input_order_by_state[anchor]
    expected = set(input_order)
    for state in sorted(states, key=state_key):
        actual = set(outgoing.get(state, {}))
        missing = [symbol for symbol in input_order if symbol not in actual]
        extra = [symbol for symbol in input_order_by_state.get(state, []) if symbol not in expected]
        if missing or extra or len(actual) != len(expected):
            raise ValueError(f"incomplete input alphabet at {state}: missing={missing}, extra={extra}")
    return {
        "states": sorted(states, key=state_key),
        "edges": edges,
        "outgoing": dict(outgoing),
        "input_order": input_order,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def open5gs_groups(model: dict[str, Any]) -> dict[str, list[str]]:
    states = model["states"]
    outgoing = model["outgoing"]
    groups: dict[str, set[str]] = {name: set() for name in ("D", "N", "NG", "A", "S", "R", "X")}
    for state in states:
        transitions = outgoing[state].values()
        if transitions and all(item["output"] == "null_action" for item in transitions):
            groups["X"].add(state)
    if "s0" in states and "s0" not in groups["X"]:
        groups["D"].add("s0")
    for edge in model["edges"]:
        src, dst, input_symbol, output = edge["src"], edge["dst"], edge["input"], edge["output"]
        if dst in groups["X"]:
            continue
        if input_symbol == "deregistrationRequest" or "reject" in output.lower() or (
            input_symbol == "securityModeReject" and src != dst
        ):
            groups["D"].add(dst)
        if output == "authenticationRequest":
            groups["N"].add(dst)
        if input_symbol == "registrationRequestGUTI" and output == "identityRequest":
            groups["NG"].add(dst)
        if input_symbol == "authenticationResponse" and output == "securityModeCommand":
            groups["A"].add(dst)
        if output == "registrationAccept":
            groups["S"].add(dst)
        if output in {"serviceAccept", "configurationUpdateCommand"}:
            groups["R"].add(dst)
    return {name: sorted(values, key=state_key) for name, values in groups.items()}


def load_custom_groups(path: Path) -> dict[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "groups" in data:
        data = data["groups"]
    if not isinstance(data, dict):
        raise ValueError("initial group JSON must be an object mapping labels to state arrays")
    return {str(label): sorted(map(str, states), key=state_key) for label, states in data.items()}


def validate_groups(states: list[str], groups: dict[str, list[str]]) -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    for label, members in groups.items():
        for state in members:
            owners[state].append(label)
    conflicts = {state: labels for state, labels in owners.items() if len(labels) != 1}
    missing = sorted(set(states) - set(owners), key=state_key)
    extra = sorted(set(owners) - set(states), key=state_key)
    if conflicts or missing or extra:
        raise ValueError(f"invalid initial grouping: conflicts={conflicts}, missing={missing}, extra={extra}")


def state_map(groups: dict[str, list[str]]) -> dict[str, str]:
    return {state: label for label, members in groups.items() for state in members}


def partition(groups: dict[str, list[str]]) -> set[tuple[str, ...]]:
    return {tuple(sorted(members, key=state_key)) for members in groups.values() if members}


def refine(model: dict[str, Any], initial: dict[str, list[str]], max_rounds: int) -> dict[str, Any]:
    input_order = model["input_order"]
    outgoing = model["outgoing"]
    previous = {label: list(members) for label, members in initial.items() if members}
    previous_map = state_map(previous)
    rounds: list[dict[str, Any]] = []
    frozen = {label for label in previous if label == "X"}
    for round_index in range(1, max_rounds + 1):
        counters: dict[str, int] = defaultdict(lambda: 1)
        next_groups: dict[str, list[str]] = {}
        split_details: list[dict[str, Any]] = []
        parent_records: list[dict[str, Any]] = []
        for parent, members in sorted(previous.items(), key=lambda item: label_key(item[0])):
            prefix, _ = label_parts(parent)
            if parent in frozen:
                next_groups[parent] = list(members)
                parent_records.append({"parent": parent, "members": members, "children": [parent]})
                continue
            buckets: dict[tuple[str, ...], list[str]] = defaultdict(list)
            for state in members:
                signature = tuple(previous_map[outgoing[state][symbol]["dst"]] for symbol in input_order)
                buckets[signature].append(state)
            ordered_buckets = sorted(buckets.items(), key=lambda item: min(state_key(s) for s in item[1]))
            children: list[str] = []
            child_records: list[dict[str, Any]] = []
            for signature, child_members in ordered_buckets:
                child_members = sorted(child_members, key=state_key)
                child = f"{prefix}{counters[prefix]}"
                counters[prefix] += 1
                next_groups[child] = child_members
                children.append(child)
                child_records.append({"name": child, "states": child_members, "signature": list(signature)})
            parent_records.append({"parent": parent, "members": members, "children": children})
            if len(child_records) > 1:
                baseline = child_records[0]["signature"]
                for child in child_records:
                    child["differences_from_baseline"] = [
                        {"index": index, "input": input_order[index], "baseline": baseline[index], "current": value}
                        for index, value in enumerate(child["signature"])
                        if value != baseline[index]
                    ]
                split_details.append({"parent": parent, "parent_states": members, "children": child_records})
        converged = partition(previous) == partition(next_groups)
        rounds.append({
            "round": round_index, "groups": next_groups, "parents": parent_records,
            "splits": split_details, "split_parent_count": len(split_details),
            "class_count": len(next_groups), "converged": converged,
        })
        if converged:
            return {
                "rounds": rounds, "final_groups": next_groups,
                "final_effective_round": max(0, round_index - 1), "convergence_round": round_index,
            }
        previous = next_groups
        previous_map = state_map(previous)
    raise ValueError(f"refinement did not converge within {max_rounds} rounds")


def build_payload(dot: Path, model: dict[str, Any], initial: dict[str, list[str]], result: dict[str, Any], profile: str) -> dict[str, Any]:
    return {
        "schema_version": 1, "kind": "mealy_refinement", "source_dot": str(dot.resolve()),
        "source_sha256": model["sha256"], "profile": profile, "states": model["states"],
        "input_order": model["input_order"], "initial_groups": initial, **result,
    }


def format_signature(values: list[str]) -> str:
    return "(" + ", ".join(values) + ")"


def write_report(payload: dict[str, Any], path: Path) -> None:
    lines = [
        f"# {Path(payload['source_dot']).stem} 迭代细化报告", "", "## 1. 数据与 signature", "",
        f"- DOT：`{payload['source_dot']}`", f"- SHA-256：`{payload['source_sha256']}`",
        f"- 状态数：{len(payload['states'])}", f"- 输入顺序：`{'`, `'.join(payload['input_order'])}`",
        "- signature：依次把每条输入转移的目标状态替换为上一轮类别标签；输出动作不进入 signature。",
        "", "## 2. 初始分类", "",
    ]
    for label, members in sorted(payload["initial_groups"].items(), key=lambda item: label_key(item[0])):
        lines.append(f"- `{label}` = {{{', '.join(members)}}}")
    lines.extend(["", "## 3. 逐轮细化", ""])
    for item in payload["rounds"]:
        lines.extend([f"### 第 {item['round']} 轮", ""])
        if item["converged"]:
            lines.extend(["本轮没有产生新的状态集合划分，判定收敛。", ""])
            continue
        lines.extend([f"- 类别数：{item['class_count']}", f"- 拆分父类数：{item['split_parent_count']}", ""])
        for split in item["splits"]:
            lines.extend([f"#### 父类 `{split['parent']}`：{{{', '.join(split['parent_states'])}}}", ""])
            for child in split["children"]:
                lines.append(f"- `{child['name']}` = {{{', '.join(child['states'])}}}")
                lines.append(f"  - signature：`{format_signature(child['signature'])}`")
                diffs = child.get("differences_from_baseline", [])
                if diffs:
                    text = "；".join(f"{d['input']}：{d['baseline']}→{d['current']}" for d in diffs)
                    lines.append(f"  - 与基准差异：{text}")
                else:
                    lines.append("  - 与基准 signature 相同。")
            lines.append("")
    lines.extend(["## 4. 最终分类", ""])
    for label, members in sorted(payload["final_groups"].items(), key=lambda item: label_key(item[0])):
        lines.append(f"- `{label}` = {{{', '.join(members)}}}")
    lines.extend([
        "", "## 5. 汇总", "", f"- 最后有效细化轮：第 {payload['final_effective_round']} 轮。",
        f"- 收敛确认轮：第 {payload['convergence_round']} 轮。",
        f"- 最终类别数：{len(payload['final_groups'])}。",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def write_flowchart(payload: dict[str, Any], path: Path) -> int:
    effective = [item for item in payload["rounds"] if not item["converged"]]
    lines = [
        "digraph MealyRoundRefinement {",
        '  graph [rankdir=TB, bgcolor="white", pad="0.25", nodesep="0.22", ranksep="0.85", splines=polyline, fontname="Microsoft YaHei", labelloc="t", label="Mealy 状态逐轮细化图"];',
        '  node [shape=box, style="rounded,filled", fontname="Microsoft YaHei", fontsize=9];',
        '  edge [fontname="Microsoft YaHei", fontsize=8, arrowsize=0.65];',
    ]
    node_count = 0
    for item in effective:
        idx = item["round"]
        lines.append(f'  subgraph cluster_round_{idx} {{ label="第 {idx} 轮 - {item["class_count"]} 类"; color="#B7C9E2"; rank=same;')
        for label, members in sorted(item["groups"].items(), key=lambda pair: label_key(pair[0])):
            prefix, _ = label_parts(label)
            fill = PREFIX_COLORS.get(prefix, "#FFFFFF")
            member_text = ", ".join(members)
            lines.append(f'    r{idx}_{label} [fillcolor="{fill}", label="{esc(label)}\\n{{{esc(member_text)}}}"];')
            node_count += 1
        lines.append("  }")
    for before, after in zip(effective, effective[1:]):
        owner = state_map(before["groups"])
        children_by_parent: dict[str, list[str]] = defaultdict(list)
        for child, members in after["groups"].items():
            parents = {owner[state] for state in members}
            if len(parents) != 1:
                raise ValueError(f"child {child} has multiple parents: {sorted(parents)}")
            children_by_parent[next(iter(parents))].append(child)
        for parent, children in children_by_parent.items():
            for child in sorted(children, key=label_key):
                same_members = set(before["groups"][parent]) == set(after["groups"][child])
                if len(children) > 1:
                    color, meaning = "#2E8B57", "拆分"
                elif same_members and parent != child:
                    color, meaning = "#E67E22", "重编号"
                else:
                    color, meaning = "#9CA3AF", "延续"
                lines.append(
                    f'  r{before["round"]}_{parent} -> r{after["round"]}_{child} '
                    f'[color="{color}", fontcolor="{color}", label="{meaning}"];'
                )
    lines.extend([
        '  legend [shape=note, fillcolor="#F8F9FA", color="#999999", label="绿色：父类真正拆分\\l橙色：成员不变但类别重编号\\l灰色：类别与成员均延续\\l"];',
        "}",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return node_count


def render(dot_path: Path, formats: set[str], node_count: int, limit: int, force: bool) -> list[str]:
    written = [str(dot_path)]
    requested = formats & {"svg", "pdf"}
    if not requested:
        return written
    if node_count > limit and not force:
        print(f"skip SVG/PDF rendering: {node_count} class nodes exceed --max-render-nodes={limit}")
        return written
    dot = shutil.which("dot")
    if not dot:
        raise RuntimeError("Graphviz 'dot' is required for SVG/PDF rendering")
    for fmt in sorted(requested):
        target = dot_path.with_suffix(f".{fmt}")
        subprocess.run([dot, f"-T{fmt}", str(dot_path), "-o", str(target)], check=True)
        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError(f"Graphviz produced an empty file: {target}")
        written.append(str(target))
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze coarse grouping and iterative Mealy refinement")
    parser.add_argument("--dot", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--basename")
    parser.add_argument("--profile", default="open5gs-nas", choices=["open5gs-nas"])
    parser.add_argument("--initial-groups", type=Path)
    parser.add_argument("--formats", default="dot,svg,pdf")
    parser.add_argument("--max-rounds", type=int, default=100)
    parser.add_argument("--max-render-nodes", type=int, default=250)
    parser.add_argument("--force-render", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    model = parse_dot(args.dot)
    initial = load_custom_groups(args.initial_groups) if args.initial_groups else open5gs_groups(model)
    validate_groups(model["states"], initial)
    result = refine(model, initial, args.max_rounds)
    profile = f"custom:{args.initial_groups}" if args.initial_groups else args.profile
    payload = build_payload(args.dot, model, initial, result, profile)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base = args.basename or args.dot.stem
    json_path = args.output_dir / f"{base}_refinement.json"
    report_path = args.output_dir / f"{base}_refinement_report.md"
    flow_path = args.output_dir / f"{base}_round_refinement_flowchart.dot"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_report(payload, report_path)
    node_count = write_flowchart(payload, flow_path)
    formats = {item.strip().lower() for item in args.formats.split(",") if item.strip()}
    artifacts = [str(json_path), str(report_path), *render(flow_path, formats, node_count, args.max_render_nodes, args.force_render)]
    print(json.dumps({
        "states": len(payload["states"]),
        "round_class_counts": [item["class_count"] for item in payload["rounds"] if not item["converged"]],
        "split_parent_counts": [item["split_parent_count"] for item in payload["rounds"]],
        "final_classes": len(payload["final_groups"]),
        "artifacts": artifacts,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
