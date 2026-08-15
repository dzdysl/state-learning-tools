"""Render H14 inference categories and an aggressive hold-repartition overlay.

The original DOT supplies edge semantics and the existing SMP SVG supplies layout.
This tool only derives figure annotations; it never modifies inference candidates.
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
COLORS = {
    "relatively_stable": "#1B9E77",
    "hypothetical": "#D95F02",
    "global_hold": "#7570B3",
    "terminal_attribution": "#E7298A",
    "new_length_one": "#1F78B4",
}
DOT_EDGE_RE = re.compile(
    r'^\s*(?P<src>s\d+)\s*->\s*(?P<dst>s\d+)\s*\[label="(?P<label>[^"]+)"\];\s*$'
)


def qname(name: str) -> str:
    return f"{{{SVG_NS}}}{name}"


def input_output(edge: dict[str, Any]) -> str:
    return f"{edge['logical_input']}/{edge['logical_output']}"


def edge_pair(edge: dict[str, Any]) -> tuple[str, str]:
    return str(edge["source_state"]), str(edge["target_state"])


def category_for_result(item: dict[str, Any]) -> str:
    grade = item.get("candidate_grade")
    if grade == "relatively_stable_candidate":
        return "relatively_stable"
    if grade == "hypothetical_candidate":
        return "hypothetical"
    raise ValueError(f"No edge category for {item['edge']['edge_id']}.")


def base_classifications(
    candidates: dict[str, Any],
) -> dict[tuple[str, str], dict[str, set[str]]]:
    pairs: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for item in candidates["results"]:
        edge = item["edge"]
        pairs[edge_pair(edge)][category_for_result(item)].add(str(edge["edge_id"]))
    return pairs


def _point_from_region(region: dict[str, Any]) -> tuple[int, int, int]:
    register_values = region.get("input_register_values") or {}
    numeric = register_values.get("ngksi_uplink")
    if numeric is None and register_values:
        numeric = register_values[sorted(register_values)[0]]
    if numeric is None:
        inputs = region.get("inputs") or []
        numeric = inputs[-1] if inputs else None
    if numeric is None:
        raise ValueError(
            f"Missing input register in {region['cycle_id']}:L{region['sequence_line']} "
            f"R{region['repetition']}."
        )
    return (
        int(region["previous_output"]["value"]),
        int(numeric["value"]),
        int(region["terminal_output"]["value"]),
    )


def _complete_groups(
    candidates: dict[str, Any], *, region_length: int | None = None
) -> Iterable[tuple[dict[str, Any], list[dict[str, Any]]]]:
    for item in candidates["results"]:
        if item.get("candidate_grade") != "hypothetical_candidate":
            continue
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for region in item.get("direct_regions", []):
            if region_length is not None and int(region["region_edge_count"]) != region_length:
                continue
            grouped[(str(region["cycle_id"]), int(region["sequence_line"]))].append(region)
        for key in sorted(grouped):
            regions = sorted(grouped[key], key=lambda value: int(value["repetition"]))
            if [int(value["repetition"]) for value in regions] == list(range(3, 11)):
                yield item, regions


def classify_complete_length_two(
    candidates: dict[str, Any], formulas: dict[str, Any]
) -> list[dict[str, Any]]:
    geometry = formulas["predecessor_repartition"]["stable_geometry"]
    stable_points = {
        io: {tuple(map(int, point)) for point in detail["triple_points"]}
        for io, detail in geometry.items()
    }
    classified: list[dict[str, Any]] = []
    for _, regions in _complete_groups(candidates, region_length=2):
        region = regions[0]
        predecessor = region["region_edges"][0]["edge"]
        terminal = region["region_edges"][-1]["edge"]
        terminal_io = input_output(terminal)
        points = [_point_from_region(value) for value in regions]
        inside = bool(stable_points.get(terminal_io)) and all(
            point in stable_points[terminal_io] for point in points
        )
        classified.append(
            {
                "trajectory_id": f"{terminal['edge_id']}:{region['cycle_id']}:L{region['sequence_line']}",
                "cycle_id": str(region["cycle_id"]),
                "sequence_line": int(region["sequence_line"]),
                "predecessor_edge": predecessor,
                "predecessor_input_output": input_output(predecessor),
                "terminal_edge": terminal,
                "terminal_input_output": terminal_io,
                "triple_points": [list(point) for point in points],
                "dynamic": len(set(points)) > 1,
                "inside_stable_triples": inside,
            }
        )
    return sorted(classified, key=lambda value: value["trajectory_id"])


def propagated_input_outputs(
    length_two: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    inside = sorted(
        {item["predecessor_input_output"] for item in length_two if item["inside_stable_triples"]}
    )
    outside = sorted(
        {
            item["predecessor_input_output"]
            for item in length_two
            if not item["inside_stable_triples"]
        }
    )
    return inside, outside, sorted(set(inside) - set(outside))


def parse_dot_edges(dot_path: Path) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    for line in dot_path.read_text(encoding="utf-8").splitlines():
        match = DOT_EDGE_RE.match(line)
        if not match:
            continue
        label = match.group("label")
        if " / " not in label:
            raise ValueError(f"DOT edge label has no input/output separator: {label}")
        logical_input, logical_output = label.split(" / ", 1)
        edges.append(
            {
                "source_state": match.group("src"),
                "target_state": match.group("dst"),
                "logical_input": logical_input,
                "logical_output": logical_output,
                "input_output": f"{logical_input}/{logical_output}",
            }
        )
    if not edges:
        raise ValueError("Original DOT contains no parseable edges.")
    return edges


def propagated_dot_edges(
    dot_edges: list[dict[str, str]], propagated_ios: list[str]
) -> list[dict[str, str]]:
    selected = [edge for edge in dot_edges if edge["input_output"] in set(propagated_ios)]
    return sorted(
        selected,
        key=lambda edge: (
            int(edge["source_state"][1:]),
            int(edge["target_state"][1:]),
            edge["input_output"],
        ),
    )


def matched_dynamic_length_two_regions(
    length_two: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        item
        for item in length_two
        if item["inside_stable_triples"]
        and item["dynamic"]
        and item["terminal_input_output"] == "registrationRequest/authenticationRequest"
    ]


def _continuous_extension_boundary(
    edges: list[dict[str, Any]], propagated_ios: set[str]
) -> dict[str, Any]:
    """Find the last hold edge that continuously extends the preceding real downlink.

    A propagated hold is not a new observation anchor.  Continuity starts at the
    real downlink preceding the original region, survives only propagated I/O
    edges, and is permanently interrupted by any other hypothetical edge.
    """
    continuity_valid = True
    valid_boundaries: list[int] = []
    interruptions: list[dict[str, Any]] = []
    ignored_extensions: list[dict[str, Any]] = []
    for index, edge in enumerate(edges[:-1]):
        io = input_output(edge)
        if io in propagated_ios:
            if continuity_valid:
                valid_boundaries.append(index)
            else:
                ignored_extensions.append(
                    {"index": index, "edge_id": edge["edge_id"], "input_output": io}
                )
            continue
        continuity_valid = False
        interruptions.append(
            {"index": index, "edge_id": edge["edge_id"], "input_output": io}
        )
    boundary = valid_boundaries[-1] if valid_boundaries else None
    return {
        "boundary_index": boundary,
        "valid_boundary_indices": valid_boundaries,
        "interruptions": interruptions,
        "ignored_extension_edges": ignored_extensions,
        "terminal_suffix_length": len(edges) if boundary is None else len(edges[boundary + 1 :]),
    }


def repartition_length_one_regions(
    candidates: dict[str, Any], propagated_ios: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible = set(propagated_ios)
    results: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for _, regions in _complete_groups(candidates):
        repartitioned: list[dict[str, Any]] = []
        rejection_samples: list[dict[str, Any]] = []
        for region in regions:
            edges = [entry["edge"] for entry in region["region_edges"]]
            continuity = _continuous_extension_boundary(edges, eligible)
            boundary = continuity["boundary_index"]
            if boundary is None:
                rejection_samples.append(
                    {"repetition": int(region["repetition"]), **continuity}
                )
                repartitioned = []
                break
            suffix = edges[boundary + 1 :]
            if len(suffix) != 1:
                rejection_samples.append(
                    {"repetition": int(region["repetition"]), **continuity}
                )
                repartitioned = []
                break
            repartitioned.append(
                {
                    "repetition": int(region["repetition"]),
                    "pseudo_boundary_edge": edges[boundary],
                    "terminal_edge": suffix[0],
                    "extension_continuity": continuity,
                }
            )
        if len(repartitioned) != 8:
            first = regions[0]
            terminal = first["region_edges"][-1]["edge"]
            rejected.append(
                {
                    "id": f"{terminal['edge_id']}:{first['cycle_id']}:L{first['sequence_line']}",
                    "cycle_id": str(first["cycle_id"]),
                    "sequence_line": int(first["sequence_line"]),
                    "terminal_edge": terminal,
                    "status": "rejected_non_continuous_observable_downlink_extension",
                    "samples": rejection_samples,
                }
            )
            continue
        terminal = repartitioned[0]["terminal_edge"]
        if any(value["terminal_edge"]["edge_id"] != terminal["edge_id"] for value in repartitioned):
            raise ValueError("Repartition terminal edge changed across R3-R10.")
        first = regions[0]
        results.append(
            {
                "id": f"{terminal['edge_id']}:{first['cycle_id']}:L{first['sequence_line']}",
                "cycle_id": str(first["cycle_id"]),
                "sequence_line": int(first["sequence_line"]),
                "terminal_edge": terminal,
                "pseudo_boundary_edge": repartitioned[0]["pseudo_boundary_edge"],
                "repetitions": list(range(3, 11)),
                "formula_fitted": False,
                "status": "structural_length_one_after_global_hold_repartition",
                "extension_policy": "continuous_from_preceding_real_downlink",
            }
        )
    return (
        sorted(results, key=lambda value: value["id"]),
        sorted(rejected, key=lambda value: value["id"]),
    )


def derive_overlay(
    candidates: dict[str, Any], formulas: dict[str, Any], dot_edges: list[dict[str, str]]
) -> dict[str, Any]:
    length_two = classify_complete_length_two(candidates, formulas)
    inside_ios, outside_ios, propagated_ios = propagated_input_outputs(length_two)
    overlap = sorted(set(inside_ios) & set(outside_ios))
    if overlap:
        raise ValueError(f"Unsafe global hold propagation; overlapping I/O: {overlap}")
    new_regions, rejected_regions = repartition_length_one_regions(candidates, propagated_ios)
    return {
        "scope": "figure_only_no_formula_inference",
        "complete_length_two_regions": length_two,
        "inside_predecessor_input_outputs": inside_ios,
        "outside_predecessor_input_outputs": outside_ios,
        "overlap_predecessor_input_outputs": overlap,
        "propagated_input_outputs": propagated_ios,
        "propagated_dot_edges": propagated_dot_edges(dot_edges, propagated_ios),
        "matched_dynamic_length_two_regions": matched_dynamic_length_two_regions(length_two),
        "new_structural_length_one_regions": new_regions,
        "rejected_non_continuous_extension_regions": rejected_regions,
        "extension_policy": {
            "kind": "continuous_observable_downlink_extension",
            "anchor": "preceding_real_ksi_downlink",
            "non_extension_hypothetical_edge_interrupts": True,
            "extension_edge_does_not_create_anchor_after_interruption": True,
        },
    }


def set_edge_style(
    edge_group: ET.Element,
    base_roles: dict[str, set[str]],
    special_roles: dict[str, set[str]],
) -> None:
    path = next((item for item in edge_group if item.tag == qname("path")), None)
    if path is None:
        raise ValueError("SMP edge group has no path.")
    roles = sorted(set(base_roles) | set(special_roles))
    edge_ids = sorted(set().union(*base_roles.values(), *special_roles.values()))
    edge_group.set("data-register-inference-categories", ",".join(roles))
    edge_group.set("data-register-inference-edge-ids", ",".join(edge_ids))

    if special_roles.get("new_length_one"):
        visible_role, color, dash = "new_length_one", COLORS["new_length_one"], "13,6"
    elif special_roles.get("terminal_attribution"):
        visible_role, color, dash = (
            "terminal_attribution", COLORS["terminal_attribution"], None
        )
    elif special_roles.get("global_hold"):
        visible_role, color = "global_hold", COLORS["global_hold"]
        dash = None if special_roles.get("directly_supported_predecessor") else "10,5"
    elif base_roles.get("relatively_stable"):
        visible_role, color, dash = "relatively_stable", COLORS["relatively_stable"], None
    else:
        visible_role, color, dash = "hypothetical", COLORS["hypothetical"], None
    edge_group.set("data-visible-role", visible_role)
    path.set("class", f"register-visible-edge register-visible-edge-{visible_role.replace('_', '-')}")
    path.set("stroke", color)
    path.set("stroke-width", "4.4" if special_roles else "3.2")
    if dash:
        path.set("stroke-dasharray", dash)
    else:
        path.attrib.pop("stroke-dasharray", None)
    for element in edge_group:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "polygon":
            element.set("fill", color)
            element.set("stroke", color)
        elif tag == "text":
            element.set("fill", "#111111")


def _legend_line(
    legend: ET.Element,
    *, x: int, y: int, color: str, label: str, dash: str | None = None,
) -> None:
    attributes = {
        "x1": str(x), "y1": str(y), "x2": str(x + 34), "y2": str(y),
        "stroke": color, "stroke-width": "5", "stroke-linecap": "round",
    }
    if dash:
        attributes["stroke-dasharray"] = dash
    legend.append(ET.Element(qname("line"), attributes))
    node = ET.Element(qname("text"), {
        "x": str(x + 44), "y": str(y + 5), "font-family": "Arial, sans-serif",
        "font-size": "15", "fill": "#111111",
    })
    node.text = label
    legend.append(node)


def add_legend(root: ET.Element, summary: dict[str, Any], base_counts: dict[str, int]) -> None:
    legend = ET.Element(qname("g"), {"id": "register-inference-legend"})
    legend.append(ET.Element(qname("rect"), {
        "x": "20", "y": "-104", "width": "2940", "height": "92", "rx": "7",
        "fill": "#FFFFFF", "fill-opacity": "0.96", "stroke": "#555555", "stroke-width": "1",
    }))
    _legend_line(legend, x=38, y=-78, color=COLORS["relatively_stable"],
                 label=f"相对稳定推断（{base_counts.get('relatively_stable', 0)} 个 EID）")
    _legend_line(legend, x=430, y=-78, color=COLORS["hypothetical"],
                 label=f"假设性推断（{base_counts.get('hypothetical', 0)} 个 EID）")
    _legend_line(
        legend, x=800, y=-78, color=COLORS["global_hold"], dash="10,5",
        label="同 I/O 全 H14 可观察下行延伸 r'=r（紫色虚线，须连续有效）",
    )
    _legend_line(
        legend, x=1510, y=-78, color=COLORS["global_hold"],
        label="匹配动态长度2区域直接支持的前序最简边（紫色实线）",
    )
    _legend_line(
        legend, x=2090, y=-78, color=COLORS["new_length_one"], dash="13,6",
        label=("连续可观察下行延伸假设下新长度1区域（派生标记，不拟合公式；"
               f"{len(summary['new_structural_length_one_regions'])} 个区域）"),
    )
    note = ET.Element(qname("text"), {
        "x": "38", "y": "-33", "font-family": "Arial, sans-serif",
        "font-size": "14", "fill": "#333333",
    })
    note.text = (
        "派生颜色直接覆盖原边颜色；蓝色边可能同时具有前序归因角色。"
        "延伸边不能自建观察锚点，遇到非延伸假设性边即中断。"
        "20条延伸语义中，7条回归s0边显示为约1cm左上短箭头，4条缺失自环仅记录不绘制；标签保持黑色。"
    )
    legend.append(note)
    root.append(legend)


def _node_geometry(graph: ET.Element) -> dict[str, tuple[float, float, float, float]]:
    nodes: dict[str, tuple[float, float, float, float]] = {}
    for group in graph.findall(qname("g")):
        if group.attrib.get("class") != "node":
            continue
        title = group.find(qname("title"))
        ellipse = group.find(qname("ellipse"))
        if title is None or not title.text or ellipse is None:
            continue
        nodes[title.text] = (
            float(ellipse.attrib["cx"]),
            float(ellipse.attrib["cy"]),
            float(ellipse.attrib["rx"]),
            float(ellipse.attrib["ry"]),
        )
    return nodes


def _ensure_global_hold_marker(root: ET.Element) -> None:
    defs = root.find(qname("defs"))
    if defs is None:
        defs = ET.Element(qname("defs"))
        root.insert(0, defs)
    marker = ET.Element(qname("marker"), {
        "id": "global-hold-arrow", "viewBox": "0 0 10 10", "refX": "9", "refY": "5",
        "markerUnits": "userSpaceOnUse", "markerWidth": "8", "markerHeight": "8",
        "orient": "auto-start-reverse",
    })
    marker.append(ET.Element(qname("path"), {
        "d": "M 0 0 L 10 5 L 0 10 z", "fill": COLORS["global_hold"],
    }))
    defs.append(marker)


def _add_s0_return_stubs(
    root: ET.Element,
    graph: ET.Element,
    edges: list[dict[str, str]],
) -> set[tuple[str, str]]:
    """Represent omitted non-self returns to s0 with one-centimetre up-left stubs."""
    if not edges:
        return set()
    _ensure_global_hold_marker(root)
    nodes = _node_geometry(graph)
    inserted: set[tuple[str, str]] = set()
    first_group = next(
        (index for index, child in enumerate(graph) if child.tag == qname("g")), len(graph)
    )
    unit = -(2.0 ** -0.5)
    stub_length = 28.35
    for edge in edges:
        source, target = edge["source_state"], edge["target_state"]
        if target != "s0" or source == target:
            raise ValueError(f"Not a non-self return to s0: {source}->{target}")
        if source not in nodes:
            raise ValueError(f"Return stub references a node absent from SMP: {source}")
        sx, sy, srx, sry = nodes[source]
        start_x = sx + unit * (max(srx, sry) + 3.0)
        start_y = sy + unit * (max(srx, sry) + 3.0)
        end_x = start_x + unit * stub_length
        end_y = start_y + unit * stub_length
        group = ET.Element(qname("g"), {
            "class": "edge register-overlay-s0-return-stub",
            "data-overlay-role": "global-hold",
            "data-semantic-source": "original-dot",
            "data-register-inference-categories": "global_hold",
            "data-source-state": source,
            "data-target-state": target,
            "data-input-output": edge["input_output"],
        })
        title = ET.Element(qname("title"))
        title.text = f"{source}->{target}: {edge['input_output']}"
        group.append(title)
        group.append(ET.Element(qname("path"), {
            "class": "register-s0-return-stub",
            "fill": "none", "stroke": COLORS["global_hold"], "stroke-width": "4.4",
            "stroke-dasharray": "10,5", "stroke-linecap": "round", "stroke-linejoin": "round",
            "marker-end": "url(#global-hold-arrow)",
            "data-stub-length-pt": f"{stub_length:.2f}",
            "d": f"M{start_x:.2f},{start_y:.2f} L{end_x:.2f},{end_y:.2f}",
        }))
        graph.insert(first_group, group)
        first_group += 1
        inserted.add((source, target))
    return inserted


def render(
    base_svg: Path,
    dot_path: Path,
    candidates_json: Path,
    formula_json: Path,
    output_svg: Path,
) -> dict[str, Any]:
    candidates = json.loads(candidates_json.read_text(encoding="utf-8"))
    formulas = json.loads(formula_json.read_text(encoding="utf-8"))
    summary = derive_overlay(candidates, formulas, parse_dot_edges(dot_path))
    base_pairs = base_classifications(candidates)
    special_pairs: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for edge in summary["propagated_dot_edges"]:
        special_pairs[(edge["source_state"], edge["target_state"])]["global_hold"].add(
            edge["input_output"]
        )
    for item in summary["matched_dynamic_length_two_regions"]:
        predecessor, terminal = item["predecessor_edge"], item["terminal_edge"]
        special_pairs[edge_pair(predecessor)]["directly_supported_predecessor"].add(
            str(predecessor["edge_id"])
        )
        special_pairs[edge_pair(terminal)]["terminal_attribution"].add(
            str(terminal["edge_id"])
        )
    for item in summary["new_structural_length_one_regions"]:
        terminal = item["terminal_edge"]
        special_pairs[edge_pair(terminal)]["new_length_one"].add(str(terminal["edge_id"]))

    root = ET.parse(base_svg).getroot()
    view_box = [float(value) for value in root.attrib["viewBox"].split()]
    view_box[1] -= 120.0
    view_box[3] += 120.0
    root.set("viewBox", " ".join(f"{value:.2f}" for value in view_box))
    if root.attrib.get("height", "").endswith("pt"):
        height = float(root.attrib["height"][:-2]) + 120.0
        root.set("height", f"{height:g}pt")
    graph = root.find(qname("g"))
    if graph is None:
        raise ValueError("SMP SVG has no graph group.")
    matched_base: set[tuple[str, str]] = set()
    matched_special: set[tuple[str, str]] = set()
    for group in graph.findall(qname("g")):
        if group.attrib.get("class") != "edge":
            continue
        title = group.find(qname("title"))
        if title is None or not title.text or "->" not in title.text:
            continue
        pair = tuple(title.text.split("->", 1))
        base_roles, special_roles = base_pairs.get(pair, {}), special_pairs.get(pair, {})
        if not base_roles and not special_roles:
            continue
        if not base_roles:
            raise ValueError(f"Special H14 edge pair has no base inference category: {pair}")
        set_edge_style(group, base_roles, special_roles)
        matched_base.add(pair)
        if special_roles:
            matched_special.add(pair)
    missing = sorted(set(special_pairs) - matched_special)
    missing_global_only = [
        pair for pair in missing if set(special_pairs[pair]) == {"global_hold"}
    ]
    unsupported_missing = sorted(set(missing) - set(missing_global_only))
    if unsupported_missing:
        raise ValueError(f"Non-global special edge pairs missing from SMP SVG: {unsupported_missing}")
    dot_by_pair = {
        (edge["source_state"], edge["target_state"]): edge
        for edge in summary["propagated_dot_edges"]
    }
    missing_edges = [dot_by_pair[pair] for pair in missing_global_only]
    s0_return_edges = [
        edge for edge in missing_edges
        if edge["target_state"] == "s0" and edge["source_state"] != edge["target_state"]
    ]
    omitted_self_loops = [
        edge for edge in missing_edges if edge["source_state"] == edge["target_state"]
    ]
    unsupported_edges = [
        edge for edge in missing_edges
        if edge not in s0_return_edges and edge not in omitted_self_loops
    ]
    if unsupported_edges:
        raise ValueError(f"Missing global-hold edges have no display rule: {unsupported_edges}")
    inserted_stub_pairs = _add_s0_return_stubs(root, graph, s0_return_edges)
    expected_stub_pairs = {
        (edge["source_state"], edge["target_state"]) for edge in s0_return_edges
    }
    if inserted_stub_pairs != expected_stub_pairs:
        raise ValueError("Not all omitted non-self returns to s0 received a short stub.")
    matched_special.update(inserted_stub_pairs)
    if not matched_base:
        raise ValueError("No inferred H14 edges matched the SMP SVG.")

    base_counts = {
        role: len({eid for roles in base_pairs.values() for eid in roles.get(role, set())})
        for role in ("relatively_stable", "hypothetical")
    }
    add_legend(root, summary, base_counts)
    metadata = ET.Element(qname("metadata"), {"id": "register-overlay-derivation"})
    metadata.text = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    root.append(metadata)
    output_svg.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output_svg, encoding="utf-8", xml_declaration=True)
    return {
        "complete_length_two_count": len(summary["complete_length_two_regions"]),
        "inside_predecessor_input_outputs": summary["inside_predecessor_input_outputs"],
        "outside_predecessor_input_outputs": summary["outside_predecessor_input_outputs"],
        "propagated_input_outputs": summary["propagated_input_outputs"],
        "propagated_dot_edge_count": len(summary["propagated_dot_edges"]),
        "matched_dynamic_length_two_region_count": len(
            summary["matched_dynamic_length_two_regions"]
        ),
        "new_structural_length_one_count": len(summary["new_structural_length_one_regions"]),
        "rejected_non_continuous_extension_count": len(
            summary["rejected_non_continuous_extension_regions"]
        ),
        "styled_pair_count": len(matched_base),
        "special_pair_count": len(matched_special),
        "visible_propagated_dot_edge_count": (
            len(summary["propagated_dot_edges"]) - len(omitted_self_loops)
        ),
        "s0_return_stub_count": len(inserted_stub_pairs),
        "omitted_self_loop_count": len(omitted_self_loops),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-svg", required=True)
    parser.add_argument("--dot", required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--trajectory-formulas", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    summary = render(
        Path(args.base_svg), Path(args.dot), Path(args.candidates),
        Path(args.trajectory_formulas), Path(args.output),
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
