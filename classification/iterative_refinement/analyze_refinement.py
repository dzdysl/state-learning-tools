from __future__ import annotations

import argparse
import html
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


def load_refinement_payload(
    path: Path,
    dot: Path,
    model: dict[str, Any],
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("kind") != "mealy_refinement":
        raise ValueError(f"{path} is not a mealy_refinement JSON")
    if payload.get("source_sha256") != model["sha256"]:
        raise ValueError(
            f"refinement JSON source hash {payload.get('source_sha256')} does not "
            f"match {dot} ({model['sha256']})"
        )
    if payload.get("states") != model["states"]:
        raise ValueError("refinement JSON state list does not match the DOT model")
    if payload.get("input_order") != model["input_order"]:
        raise ValueError("refinement JSON input order does not match the DOT model")
    if not payload.get("rounds"):
        raise ValueError("refinement JSON contains no rounds")
    return payload


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


def validate_round_groups(
    states: list[str],
    groups: dict[str, list[str]],
    round_index: int,
) -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    for label, members in groups.items():
        if not members:
            raise ValueError(f"round {round_index} contains empty class {label}")
        for state in members:
            owners[state].append(label)
    duplicates = {
        state: labels for state, labels in owners.items() if len(labels) != 1
    }
    missing = sorted(set(states) - set(owners), key=state_key)
    extra = sorted(set(owners) - set(states), key=state_key)
    if duplicates or missing or extra:
        raise ValueError(
            f"invalid round {round_index} grouping: "
            f"duplicates={duplicates}, missing={missing}, extra={extra}"
        )


def build_round_relations(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[dict[str, Any]]:
    owner = state_map(before["groups"])
    children_by_parent: dict[str, list[str]] = defaultdict(list)
    for child, members in after["groups"].items():
        parents = {owner[state] for state in members}
        if len(parents) != 1:
            raise ValueError(
                f"round {after['round']} child {child} has "
                f"{len(parents)} parents: {sorted(parents)}"
            )
        parent = next(iter(parents))
        children_by_parent[parent].append(child)
    missing_parents = sorted(
        set(before["groups"]) - set(children_by_parent),
        key=label_key,
    )
    if missing_parents:
        raise ValueError(
            f"round {before['round']} parents have no descendants in "
            f"round {after['round']}: {missing_parents}"
        )
    relations = []
    for parent, children in sorted(
        children_by_parent.items(),
        key=lambda item: label_key(item[0]),
    ):
        parent_members = set(before["groups"][parent])
        child_union = {
            state
            for child in children
            for state in after["groups"][child]
        }
        if child_union != parent_members:
            raise ValueError(
                f"round {before['round']} parent {parent} descendants do not "
                f"preserve members: parent={sorted(parent_members, key=state_key)}, "
                f"children={sorted(child_union, key=state_key)}"
            )
        is_split = len(children) >= 2
        for child in sorted(children, key=label_key):
            child_members = set(after["groups"][child])
            if is_split:
                kind = "split"
            elif child_members == parent_members and child != parent:
                kind = "renumber"
            elif child_members == parent_members and child == parent:
                kind = "unchanged"
            else:
                raise ValueError(
                    f"round {before['round']} {parent} -> round {after['round']} "
                    f"{child} is neither a split, renumber, nor unchanged relation"
                )
            relations.append({
                "from_round": before["round"],
                "to_round": after["round"],
                "parent": parent,
                "child": child,
                "kind": kind,
                "parent_states": sorted(parent_members, key=state_key),
                "child_states": sorted(child_members, key=state_key),
            })
    if len(relations) != len(after["groups"]):
        raise ValueError(
            f"round {before['round']} -> {after['round']} relation count "
            f"{len(relations)} does not equal child count {len(after['groups'])}"
        )
    return relations


def build_refinement_flow(
    payload: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[tuple[int, str], int],
]:
    effective = [item for item in payload["rounds"] if not item["converged"]]
    if not effective:
        raise ValueError("flowchart requires at least one effective refinement round")
    for item in effective:
        validate_round_groups(payload["states"], item["groups"], item["round"])
    relations = [
        relation
        for before, after in zip(effective, effective[1:])
        for relation in build_round_relations(before, after)
    ]
    final_owner = state_map(effective[-1]["groups"])
    spans: dict[tuple[int, str], int] = {}
    for item in effective:
        for label, members in item["groups"].items():
            descendants = {final_owner[state] for state in members}
            if not descendants:
                raise ValueError(
                    f"round {item['round']} class {label} has no final descendants"
                )
            spans[(item["round"], label)] = len(descendants)
    return effective, relations, spans


def flowchart_title(payload: dict[str, Any], effective: list[dict[str, Any]]) -> str:
    stem = Path(payload["source_dot"]).stem
    match = re.fullmatch(r"hypothesis_(\d+)", stem, flags=re.IGNORECASE)
    model = f"H{match.group(1)}" if match else stem
    first, last = effective[0]["round"], effective[-1]["round"]
    return f"{model} 第{first}–{last}轮状态拆分流程图"


def member_text_html(members: list[str], span: int) -> str:
    values = [html.escape(state) for state in members]
    if not values:
        return "{}"
    max_chars = max(14, span * 18)
    lines: list[str] = []
    current = ""
    for value in values:
        candidate = value if not current else f"{current}, {value}"
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = value
        else:
            current = candidate
    lines.append(current)
    return "{" + "<BR/>".join(lines) + "}"


def write_flowchart(payload: dict[str, Any], path: Path) -> int:
    effective, relations, spans = build_refinement_flow(payload)
    base_width = 92
    lines = [
        "digraph MealyRoundRefinement {",
        '  graph [rankdir=TB, bgcolor="white", pad="0.30", nodesep="0.18", '
        'ranksep="1.05", splines=polyline, outputorder=edgesfirst, '
        'fontname="Microsoft YaHei", labelloc=t, labeljust=c, '
        f'label="{esc(flowchart_title(payload, effective))}", fontsize=24];',
        '  node [shape=plain, fontname="Microsoft YaHei"];',
        '  edge [fontname="Microsoft YaHei", arrowsize=0.70];',
        "",
    ]
    for item in effective:
        idx = item["round"]
        lines.extend([
            f'  round_{idx} [shape=box, style="rounded,filled", '
            'fillcolor="#17365D", fontcolor="white", color="#17365D", '
            f'margin="0.14,0.09", label="第 {idx} 轮\\n{item["class_count"]} 类"];',
            f"  r{idx} [label=<",
            '    <TABLE BORDER="0" CELLBORDER="1" CELLSPACING="2" '
            'CELLPADDING="4" COLOR="#6B7280">',
            "      <TR>",
        ])
        for label, members in sorted(
            item["groups"].items(),
            key=lambda pair: label_key(pair[0]),
        ):
            prefix, _ = label_parts(label)
            fill = PREFIX_COLORS.get(prefix, "#FFFFFF")
            span = spans[(idx, label)]
            width = base_width * span
            members_html = member_text_html(members, span)
            lines.append(
                f'        <TD PORT="{html.escape(label)}" COLSPAN="{span}" '
                f'FIXEDSIZE="TRUE" WIDTH="{width}" HEIGHT="58" '
                f'BGCOLOR="{fill}"><B>{html.escape(label)}</B><BR/>'
                f'<FONT POINT-SIZE="9">{members_html}</FONT></TD>'
            )
        lines.extend([
            "      </TR>",
            "    </TABLE>",
            "  >];",
            f"  {{ rank=same; round_{idx}; r{idx}; }}",
            f"  round_{idx} -> r{idx} [style=invis, weight=300];",
            "",
        ])
    for before, after in zip(effective, effective[1:]):
        lines.append(
            f"  round_{before['round']} -> round_{after['round']} "
            "[style=invis, weight=300, minlen=2];"
        )
    lines.append("")
    edge_styles = {
        "split": ('#2E8B57', "2.5"),
        "renumber": ('#E67E22', "2.2"),
        "unchanged": ('#9AA0A6', "1.15"),
    }
    for relation in relations:
        color, width = edge_styles[relation["kind"]]
        lines.append(
            f"  r{relation['from_round']}:{relation['parent']}:s -> "
            f"r{relation['to_round']}:{relation['child']}:n "
            f'[color="{color}", penwidth={width}];'
        )
    lines.extend([
        "",
        "  legend [shape=plain, label=<",
        '    <TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" '
        'CELLPADDING="6" COLOR="#6B7280">',
        '      <TR><TD COLSPAN="9" BGCOLOR="#F3F4F6"><B>图例</B></TD></TR>',
        '      <TR><TD><FONT COLOR="#2E8B57">━━▶</FONT></TD>'
        '<TD ALIGN="LEFT">绿色：真正拆分</TD>'
        '<TD><FONT COLOR="#E67E22">━━▶</FONT></TD>'
        '<TD ALIGN="LEFT">橙色：成员不变，仅编号变化</TD>'
        '<TD><FONT COLOR="#9AA0A6">──▶</FONT></TD>'
        '<TD ALIGN="LEFT">灰色：名称和成员均不变</TD>'
        "<TD></TD><TD></TD><TD></TD></TR>",
        '      <TR><TD BGCOLOR="#DCEBFF"><B>A 类</B></TD>'
        '<TD BGCOLOR="#FFE1E1"><B>D 类</B></TD>'
        '<TD BGCOLOR="#DFF3E4"><B>N 类</B></TD>'
        '<TD BGCOLOR="#EBDDFA"><B>NG 类</B></TD>'
        '<TD BGCOLOR="#FFF0BD"><B>S 类</B></TD>'
        '<TD BGCOLOR="#DDF5F7"><B>R 类</B></TD>'
        '<TD BGCOLOR="#ECEFF1"><B>X 类</B></TD>'
        '<TD COLSPAN="2" ALIGN="LEFT">横向顺序：A → D → N → NG → S → R → X</TD></TR>',
        "    </TABLE>",
        "  >];",
        "  { rank=sink; legend; }",
        f"  round_{effective[-1]['round']} -> legend [style=invis, weight=200];",
        "}",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sum(item["class_count"] for item in effective)


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
    parser.add_argument(
        "--refinement-json",
        type=Path,
        help="reuse a verified canonical refinement JSON with --flowchart-only",
    )
    parser.add_argument("--profile", default="open5gs-nas", choices=["open5gs-nas"])
    parser.add_argument("--initial-groups", type=Path)
    parser.add_argument("--formats", default="dot,svg,pdf")
    parser.add_argument("--max-rounds", type=int, default=100)
    parser.add_argument("--max-render-nodes", type=int, default=250)
    parser.add_argument("--force-render", action="store_true")
    parser.add_argument(
        "--flowchart-only",
        action="store_true",
        help="write only round-refinement DOT/SVG/PDF; leave JSON and report untouched",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    model = parse_dot(args.dot)
    if args.refinement_json:
        if not args.flowchart_only:
            raise ValueError("--refinement-json requires --flowchart-only")
        if args.initial_groups:
            raise ValueError(
                "--initial-groups cannot be combined with --refinement-json"
            )
        payload = load_refinement_payload(args.refinement_json, args.dot, model)
    else:
        initial = (
            load_custom_groups(args.initial_groups)
            if args.initial_groups else open5gs_groups(model)
        )
        validate_groups(model["states"], initial)
        result = refine(model, initial, args.max_rounds)
        profile = (
            f"custom:{args.initial_groups}" if args.initial_groups else args.profile
        )
        payload = build_payload(args.dot, model, initial, result, profile)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base = args.basename or args.dot.stem
    json_path = args.output_dir / f"{base}_refinement.json"
    report_path = args.output_dir / f"{base}_refinement_report.md"
    flow_path = args.output_dir / f"{base}_round_refinement_flowchart.dot"
    artifacts = []
    if not args.flowchart_only:
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_report(payload, report_path)
        artifacts.extend([str(json_path), str(report_path)])
    node_count = write_flowchart(payload, flow_path)
    formats = {item.strip().lower() for item in args.formats.split(",") if item.strip()}
    artifacts.extend(render(flow_path, formats, node_count, args.max_render_nodes, args.force_render))
    print(json.dumps({
        "states": len(payload["states"]),
        "round_class_counts": [item["class_count"] for item in payload["rounds"] if not item["converged"]],
        "split_parent_counts": [item["split_parent_count"] for item in payload["rounds"]],
        "final_classes": len(payload["final_groups"]),
        "artifacts": artifacts,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
