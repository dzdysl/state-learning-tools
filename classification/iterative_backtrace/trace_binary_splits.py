from __future__ import annotations

import argparse
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


def shortest_distinction(model: dict[str, Any], left: str, right: str) -> dict[str, Any]:
    queue = deque([((left, right), [])]); visited = {(left, right)}
    while queue:
        (a, b), prefix = queue.popleft()
        for symbol in model["input_order"]:
            edge_a, edge_b = model["outgoing"][a][symbol], model["outgoing"][b][symbol]
            if edge_a["output"] != edge_b["output"]:
                return {"equivalent": False, "sequence": [*prefix, symbol], "left_output": edge_a["output"], "right_output": edge_b["output"]}
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
            for left, right in itertools.combinations(split["children"], 2):
                diffs = []
                for index, (label_l, label_r) in enumerate(zip(left["signature"], right["signature"])):
                    if label_l == label_r:
                        continue
                    symbol = model["input_order"][index]
                    diffs.append({
                        "index": index, "input": symbol, "abbreviation": ABBR.get(symbol, symbol),
                        "left_target_label": label_l, "right_target_label": label_r,
                        "upstream_pair": list(canonical_pair(label_l, label_r)),
                        "left_transitions": member_transition_variants(model, left["states"], symbol),
                        "right_transitions": member_transition_variants(model, right["states"], symbol),
                    })
                upstream = {tuple(item["upstream_pair"]) for item in diffs}
                if len(diffs) == 1:
                    classification = "strict"
                elif len(upstream) == 1:
                    classification = "convergent_unique"
                else:
                    classification = "branching"
                pair_name = "/".join(canonical_pair(left["name"], right["name"]))
                pairs.append({
                    "round": round_index, "parent": split["parent"], "pair": pair_name,
                    "left": left, "right": right, "difference_count": len(diffs), "differences": diffs,
                    "upstream_pairs": [list(item) for item in sorted(upstream)],
                    "classification": classification,
                })
    return pairs


def node_key(item: dict[str, Any]) -> tuple[int, str, str]:
    return item["round"], item["parent"], item["pair"]


def analyze_graph(pairs: list[dict[str, Any]], policy: str, only_round: int | None, only_parent: str | None) -> dict[str, Any]:
    lookup: dict[tuple[int, tuple[str, str]], dict[str, Any]] = {}
    for item in pairs:
        child_pair = canonical_pair(item["left"]["name"], item["right"]["name"])
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
        for left in item["left"]["states"]:
            for right in item["right"]["states"]:
                results.append({"left": left, "right": right, **shortest_distinction(model, left, right)})
        item["behavior_audit"] = {
            "all_equivalent": all(result["equivalent"] for result in results),
            "state_pairs": results,
        }


def serialize_key(key: tuple[int, str, str]) -> str:
    return f"r{key[0]}:{key[1]}:{key[2]}"


def build_payload(dot: Path, model: dict[str, Any], refinement: dict[str, Any], pairs: list[dict[str, Any]], graph: dict[str, Any], policy: str) -> dict[str, Any]:
    strict = sum(item["classification"] == "strict" for item in pairs)
    convergent = sum(item["classification"] == "convergent_unique" for item in pairs)
    branching = sum(item["classification"] == "branching" for item in pairs)
    return {
        "schema_version": 1, "kind": "mealy_binary_backtrace", "source_dot": str(dot.resolve()),
        "source_sha256": model["sha256"], "entry_policy": policy, "input_order": model["input_order"],
        "counts": {
            "all_pairs": len(pairs), "strict": strict, "convergent_unique": convergent,
            "branching": branching, "independent_entries": len(graph["independent"]),
            "visited_nodes": len(graph["visited"]), "terminals": len(graph["terminals"]),
        },
        "pairs": pairs,
        "independent_entry_keys": [serialize_key(node_key(item)) for item in graph["independent"]],
        "edges": [
            {**edge, "from": serialize_key(edge["from"]), **(
                {"to": serialize_key(edge["to"])} if "to" in edge else
                {"to_terminal": f"stage{edge['to_terminal'][0]}:{'/'.join(edge['to_terminal'][1])}"}
            )}
            for edge in graph["edges"]
        ],
        "terminals": graph["terminals"],
        "refinement_summary": {
            "round_class_counts": [item["class_count"] for item in refinement["rounds"] if not item["converged"]],
            "split_parent_counts": [item["split_parent_count"] for item in refinement["rounds"]],
        },
    }


def write_full_report(payload: dict[str, Any], path: Path) -> None:
    c = payload["counts"]
    lines = [
        f"# {Path(payload['source_dot']).stem} 全量二分类回溯报告", "",
        f"- 入口策略：`{payload['entry_policy']}`", f"- 同父类对子：{c['all_pairs']}",
        f"- 单位置严格对子：{c['strict']}", f"- 汇聚型唯一路径：{c['convergent_unique']}",
        f"- 真正分支对子：{c['branching']}", f"- 独立入口：{c['independent_entries']}", "",
        "## 对子角色", "", "| 轮次 | 父类 | 子类对 | 分类 | 角色 | 差异输入 |", "|---:|---|---|---|---|---|",
    ]
    for item in sorted(payload["pairs"], key=lambda x: (-x["round"], label_key(x["parent"]), x["pair"])):
        signals = "/".join(diff["abbreviation"] for diff in item["differences"])
        lines.append(f"| {item['round']} | `{item['parent']}` | `{item['pair']}` | `{item['classification']}` | `{item['role']}` | `{signals}` |")
    lines.extend(["", "## 回溯节点明细", ""])
    for item in sorted((x for x in payload["pairs"] if x["role"] != "unvisited_non_entry"), key=lambda x: (-x["round"], x["pair"])):
        lines.extend([
            f"### 第 {item['round']} 轮 `{item['pair']}`", "",
            f"- 父类：`{item['parent']}`", f"- 分类：`{item['classification']}`", f"- 角色：`{item['role']}`",
            f"- 左侧：`{item['left']['name']}` = {{{', '.join(item['left']['states'])}}}",
            f"- 右侧：`{item['right']['name']}` = {{{', '.join(item['right']['states'])}}}", "",
            "| 输入 | 上游类别对 | 左侧成员转移数 | 右侧成员转移数 |", "|---|---|---:|---:|",
        ])
        for diff in item["differences"]:
            lines.append(f"| `{diff['input']}` | `{'/'.join(diff['upstream_pair'])}` | {len(diff['left_transitions'])} | {len(diff['right_transitions'])} |")
        lines.append("")
    lines.extend(["## 不作为独立入口的对子", ""])
    for item in payload["pairs"]:
        if item["role"] == "unvisited_non_entry":
            lines.append(f"- 第{item['round']}轮 `{item['parent']}` 的 `{item['pair']}`：`{item['classification']}`，包含 {item['difference_count']} 个差异位置。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_independent_report(payload: dict[str, Any], path: Path) -> None:
    independent = set(payload["independent_entry_keys"])
    lines = [f"# {Path(payload['source_dot']).stem} 独立入口行为区分报告", ""]
    index = 0
    for item in sorted(payload["pairs"], key=lambda x: (-x["round"], label_key(x["parent"]), x["pair"])):
        if serialize_key(node_key(item)) not in independent:
            continue
        index += 1
        audit = item["behavior_audit"]
        lines.extend([
            f"## {index}. 第 {item['round']} 轮 `{item['parent']}`：`{item['pair']}`", "",
            f"- 拆分：`{item['left']['name']}={{{', '.join(item['left']['states'])}}}` / `{item['right']['name']}={{{', '.join(item['right']['states'])}}}`",
            f"- 入口分类：`{item['classification']}`", f"- signature差异数：{item['difference_count']}",
            f"- 组级行为全部等价：{audit['all_equivalent']}", "",
        ])
        for result in audit["state_pairs"]:
            if result["equivalent"]:
                lines.append(f"- `{result['left']}/{result['right']}`：模型内等价。")
            else:
                lines.append(f"- `{result['left']}/{result['right']}`：`{' '.join(result['sequence'])}` → `{result['left_output']}/{result['right_output']}`")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def write_flowchart(payload: dict[str, Any], path: Path) -> int:
    active = [item for item in payload["pairs"] if item["role"] != "unvisited_non_entry"]
    ids = {serialize_key(node_key(item)): f"n{index}" for index, item in enumerate(active, 1)}
    terminal_ids = {f"stage{item['stage']}:{'/'.join(item['pair'])}": f"t{index}" for index, item in enumerate(payload["terminals"], 1)}
    lines = [
        "digraph MealyBinaryBacktrace {",
        '  graph [rankdir=BT, bgcolor="white", pad="0.25", nodesep="0.35", ranksep="0.85", splines=polyline, fontname="Microsoft YaHei", labelloc="t", label="Mealy 全量二分类回溯图"];',
        '  node [shape=box, style="rounded,filled", fontname="Microsoft YaHei", fontsize=9];',
        '  edge [fontname="Microsoft YaHei", fontsize=8, arrowsize=0.7];',
    ]
    for terminal_key, terminal_id in terminal_ids.items():
        lines.append(f'  {terminal_id} [fillcolor="#FFF2CC", color="#D99A00", label="{esc(terminal_key)}"];')
    by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in active: by_round[item["round"]].append(item)
    for round_index in sorted(by_round):
        lines.append(f'  subgraph cluster_r{round_index} {{ label="第 {round_index} 轮"; color="#B7C9E2"; rank=same;')
        for item in sorted(by_round[round_index], key=lambda x: (label_key(x["parent"]), x["pair"])):
            key = serialize_key(node_key(item)); node_id = ids[key]
            fill = "#DCEBFF" if item["role"] == "independent_entry" else "#F3F4F6"
            label = f"父类 {item['parent']}\\n{item['pair']}\\n{item['classification']} | {item['role']}"
            lines.append(f'    {node_id} [fillcolor="{fill}", label="{esc(label)}"];')
        lines.append("  }")
    for edge in payload["edges"]:
        source = ids.get(edge["from"])
        target = ids.get(edge.get("to")) or terminal_ids.get(edge.get("to_terminal"))
        if source and target:
            lines.append(f'  {source} -> {target} [label="{esc(edge["signals"])}"];')
    lines.extend([
        '  legend [shape=note, fillcolor="#F8F9FA", color="#999999", label="strict：单位置差异\\lconvergent_unique：多信令汇聚到同一上游对子\\lbranching：多个上游对子\\l蓝底：独立入口；灰底：已覆盖中间节点\\l"];',
        "}",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(active) + len(terminal_ids)


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
    parser.add_argument("--entry-policy", choices=["strict", "unique-path"], default="unique-path")
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
    add_behavior_audit(graph["independent"], model)
    payload = build_payload(args.dot, model, refinement, pairs, graph, args.entry_policy)
    args.output_dir.mkdir(parents=True, exist_ok=True); base = args.basename or args.dot.stem
    json_path = args.output_dir / f"{base}_all_binary_backtrace.json"
    report_path = args.output_dir / f"{base}_all_binary_backtrace_report.md"
    independent_path = args.output_dir / f"{base}_independent_entry_behavior_report.md"
    flow_path = args.output_dir / f"{base}_all_binary_backtrace_flowchart.dot"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_full_report(payload, report_path); write_independent_report(payload, independent_path)
    node_count = write_flowchart(payload, flow_path)
    formats = {item.strip().lower() for item in args.formats.split(",") if item.strip()}
    payload["artifacts"] = [str(json_path), str(report_path), str(independent_path), *render(flow_path, formats, node_count, args.max_render_nodes, args.force_render)]
    print(json.dumps({"counts": payload["counts"], "artifacts": payload["artifacts"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
