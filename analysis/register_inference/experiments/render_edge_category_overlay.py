"""Overlay H14 register-inference edge categories on an existing SMP SVG layout.

The SVG is used as an immutable presentation base: node positions, paths and labels
are copied byte-for-byte in meaning, then only edge styles and a legend are added.
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

CATEGORIES = {
    "relatively_stable": {
        "color": "#1B9E77",
        "label": "相对稳定推断",
    },
    "predecessor_minimal": {
        "color": "#D95F02",
        "label": "前序最简假设性推断",
    },
    "terminal_observable": {
        "color": "#7570B3",
        "label": "前序归因（末端有可观察下行）的假设性推断",
    },
    "backward_pair": {
        "color": "#E7298A",
        "label": "前序反推组合（E0002 + E0073）",
    },
}


def qname(name: str) -> str:
    return f"{{{SVG_NS}}}{name}"


def category_for_result(item: dict[str, Any]) -> str:
    resolution = item.get("hypothetical_candidate_resolution") or {}
    if item.get("candidate_grade") == "relatively_stable_candidate":
        return "relatively_stable"
    if resolution.get("strategy") == "not_applicable_no_downlink_anchor":
        return "predecessor_minimal"
    if resolution.get("strategy") == "fit_cycle_minimal_candidates_then_combine_samples_if_intersection_empty":
        return "terminal_observable"
    raise ValueError(f"No edge category for {item['edge']['edge_id']}.")


def classifications(result: dict[str, Any]) -> dict[tuple[str, str], list[tuple[str, str]]]:
    pairs: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for item in result["results"]:
        edge = item["edge"]
        edge_id = str(edge["edge_id"])
        category = "backward_pair" if edge_id in {"E0002", "E0073"} else category_for_result(item)
        pairs[(str(edge["source_state"]), str(edge["target_state"]))].append((category, edge_id))
    return pairs


def set_edge_style(edge_group: ET.Element, categories: list[str], edge_ids: list[str]) -> None:
    primary = categories[0]
    color = CATEGORIES[primary]["color"]
    edge_group.set("data-register-inference-categories", ",".join(categories))
    edge_group.set("data-register-inference-edge-ids", ",".join(edge_ids))
    for element in edge_group:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "path":
            element.set("stroke", color)
            element.set("stroke-width", "3.4")
        elif tag == "polygon":
            element.set("fill", color)
            element.set("stroke", color)
        elif tag == "text":
            element.set("fill", color)

    if len(categories) > 1:
        secondary = categories[1]
        secondary_color = CATEGORIES[secondary]["color"]
        path = next((item for item in edge_group if item.tag == qname("path")), None)
        if path is not None:
            outer = ET.Element(qname("path"), {
                "fill": "none",
                "stroke": secondary_color,
                "stroke-width": "7.2",
                "stroke-dasharray": "10,5",
                "stroke-linecap": "round",
                "d": path.attrib["d"],
            })
            edge_group.insert(list(edge_group).index(path), outer)


def add_legend(root: ET.Element, category_counts: dict[str, int]) -> None:
    legend = ET.Element(qname("g"), {"id": "register-inference-legend"})
    legend.append(ET.Element(qname("rect"), {
        "x": "24", "y": "20", "width": "1540", "height": "50", "rx": "6",
        "fill": "#FFFFFF", "fill-opacity": "0.94", "stroke": "#666666", "stroke-width": "1",
    }))
    x = 42
    for category in ("relatively_stable", "predecessor_minimal", "terminal_observable", "backward_pair"):
        detail = CATEGORIES[category]
        legend.append(ET.Element(qname("line"), {
            "x1": str(x), "y1": "45", "x2": str(x + 30), "y2": "45",
            "stroke": detail["color"], "stroke-width": "5", "stroke-linecap": "round",
        }))
        text = ET.Element(qname("text"), {
            "x": str(x + 40), "y": "50", "font-family": "Arial, sans-serif", "font-size": "15",
            "fill": "#222222",
        })
        text.text = f"{detail['label']}（{category_counts.get(category, 0)} 条）"
        legend.append(text)
        x += 365
    root.append(legend)


def render(base_svg: Path, candidates_json: Path, output_svg: Path) -> dict[str, int]:
    result = json.loads(candidates_json.read_text(encoding="utf-8"))
    pairs = classifications(result)
    root = ET.parse(base_svg).getroot()
    graph = root.find(qname("g"))
    if graph is None:
        raise ValueError("SMP SVG has no graph group.")

    category_counts: dict[str, int] = defaultdict(int)
    styled_pairs = 0
    for group in graph.findall(qname("g")):
        if group.attrib.get("class") != "edge":
            continue
        title = group.find(qname("title"))
        if title is None or not title.text or "->" not in title.text:
            continue
        source, target = title.text.split("->", 1)
        entries = pairs.get((source, target), [])
        if not entries:
            continue
        categories = list(dict.fromkeys(category for category, _ in entries))
        edge_ids = [edge_id for _, edge_id in entries]
        set_edge_style(group, categories, edge_ids)
        styled_pairs += 1
        for category, _ in entries:
            category_counts[category] += 1

    if styled_pairs == 0:
        raise ValueError("No inferred H14 edges matched the SMP SVG.")
    add_legend(root, category_counts)
    output_svg.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output_svg, encoding="utf-8", xml_declaration=True)
    return {"styled_pair_count": styled_pairs, **dict(sorted(category_counts.items()))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-svg", required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    summary = render(Path(args.base_svg), Path(args.candidates), Path(args.output))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
