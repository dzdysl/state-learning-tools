#!/usr/bin/env python3
"""Render one entry-scoped Mealy backtrace as a path-only SVG overlay."""

from __future__ import annotations

import argparse
import hashlib
import html
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


STATE_RE = re.compile(r"\b(s\d+)\s*\[")
EDGE_RE = re.compile(
    r'\b(?P<src>s\d+)\s*->\s*(?P<dst>s\d+)\s*\[\s*label="(?P<label>[^"]+)"[^\]]*\]'
)
PAIR_TITLE_RE = re.compile(r"^(s\d+)->(s\d+)$")
TRANSLATE_RE = re.compile(r"translate\(([-+0-9.eE]+)[,\s]+([-+0-9.eE]+)\)")
DISPLAY_ID_RE = re.compile(r"^B(\d+)$")
OVERLAY_BEGIN = "<!-- BEGIN backtrace-overlay -->"
OVERLAY_END = "<!-- END backtrace-overlay -->"
PALETTE = (
    "#D81B60", "#1E88E5", "#FFC107", "#004D40",
    "#8E24AA", "#43A047", "#FB8C00", "#3949AB",
    "#00ACC1", "#6D4C41", "#7CB342", "#C0CA33",
    "#F06292", "#5E35B1", "#00897B", "#E53935",
)
WINDOWS_INKSCAPE = Path(r"D:\Inkscape\bin\inkscape.com")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_record(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
        "sha256": sha256(resolved),
    }


def split_edge_label(label: str) -> tuple[list[str], str]:
    separator = " / " if " / " in label else "/"
    if separator not in label:
        raise ValueError(f"edge label has no input/output separator: {label!r}")
    inputs, output = label.split(separator, 1)
    return [item.strip() for item in inputs.split(" | ") if item.strip()], output.strip()


def parse_dot(path: Path, *, require_complete: bool) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    states = set(STATE_RE.findall(text))
    outgoing: dict[str, dict[str, dict[str, str]]] = {}
    input_order: list[str] = []
    render_edges = []
    for match in EDGE_RE.finditer(text):
        src, dst, label = match.group("src"), match.group("dst"), match.group("label")
        inputs, output = split_edge_label(label)
        states.update((src, dst))
        render_edge = {
            "src": src,
            "dst": dst,
            "inputs": inputs,
            "output": output,
            "label": label,
        }
        render_edges.append(render_edge)
        for input_symbol in inputs:
            if input_symbol in outgoing.setdefault(src, {}):
                raise ValueError(f"non-deterministic transition for ({src}, {input_symbol})")
            transition = {
                "src": src,
                "dst": dst,
                "input": input_symbol,
                "output": output,
            }
            outgoing[src][input_symbol] = transition
            if input_symbol not in input_order:
                input_order.append(input_symbol)
    if require_complete:
        for state in states:
            missing = [
                symbol for symbol in input_order
                if symbol not in outgoing.get(state, {})
            ]
            if missing:
                raise ValueError(f"incomplete input alphabet at {state}: {missing}")
    return {
        "states": sorted(states, key=lambda item: int(item[1:])),
        "outgoing": outgoing,
        "input_order": input_order,
        "render_edges": render_edges,
    }


def null_sink_states(model: dict[str, Any]) -> set[str]:
    sinks = set()
    for state, outgoing in model["outgoing"].items():
        transitions = list(outgoing.values())
        if (
            transitions
            and all(edge["output"] == "null_action" for edge in transitions)
            and all(edge["dst"] == state for edge in transitions)
        ):
            sinks.add(state)
    return sinks


def load_backtrace(path: Path, source_dot: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("kind") != "mealy_binary_backtrace":
        raise ValueError(f"not a mealy_binary_backtrace JSON: {path}")
    if payload.get("schema_version") != 2:
        raise ValueError(
            "backtrace JSON schema version 2 is required; regenerate it with "
            "the current trace_binary_splits.py"
        )
    if payload.get("source_sha256") != sha256(source_dot):
        raise ValueError("backtrace JSON source SHA-256 does not match original DOT")
    return payload


def select_entry(payload: dict[str, Any], selector: str) -> dict[str, Any]:
    matches = [
        item for item in payload["pairs"]
        if item.get("display_id") == selector or item.get("key") == selector
    ]
    if len(matches) != 1:
        raise ValueError(f"entry selector must resolve exactly once: {selector!r}")
    entry = matches[0]
    if entry.get("role") != "independent_entry":
        raise ValueError(
            f"{selector!r} resolves to role {entry.get('role')!r}, "
            "not independent_entry"
        )
    return entry


def child_for_state(item: dict[str, Any], state: str) -> dict[str, Any]:
    matches = [child for child in item["children"] if state in child["states"]]
    if len(matches) != 1:
        raise ValueError(
            f"state {state} belongs to {len(matches)} children at {item['key']}"
        )
    return matches[0]


def child_view_for_input(
    item: dict[str, Any],
    child_name: str,
    input_symbol: str,
) -> dict[str, Any]:
    matches = [
        view
        for difference in item["differences"]
        if difference["input"] == input_symbol
        for view in difference["child_views"]
        if view["child"] == child_name
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one child view for {item['key']} {child_name} {input_symbol}"
        )
    return matches[0]


def recompute_entry_paths(
    payload: dict[str, Any],
    entry: dict[str, Any],
    model: dict[str, Any],
) -> list[dict[str, Any]]:
    pair_lookup = {item["key"]: item for item in payload["pairs"]}
    records = [
        item for item in payload["entry_paths"]
        if item["entry_key"] == entry["key"]
    ]
    if len(records) != 1:
        raise ValueError(f"expected one entry_paths record for {entry['key']}")
    result_paths = []
    for path_index, path_record in enumerate(records[0]["paths"], 1):
        relations = path_record["relations"]
        variants = []
        expected_sequences = {
            tuple(sequence)
            for sequence in itertools.product(
                *(relation["inputs"] for relation in relations)
            )
        }
        recorded_sequences = {
            tuple(variant["input_sequence"])
            for variant in path_record["trace_variants"]
        }
        if expected_sequences != recorded_sequences:
            raise ValueError(
                f"trace variant input choices do not match relations for path {path_index}"
            )
        for variant_index, recorded_variant in enumerate(
            path_record["trace_variants"], 1
        ):
            input_sequence = recorded_variant["input_sequence"]
            branches: dict[str, Any] = {}
            for branch_name, entry_child in zip(("A", "B"), entry["children"]):
                recorded_branch = recorded_variant["branches"].get(branch_name)
                if not recorded_branch:
                    raise ValueError(f"missing branch {branch_name}")
                if recorded_branch["entry_child"] != entry_child["name"]:
                    raise ValueError(f"branch {branch_name} entry child mismatch")
                starts = entry_child["states"]
                if recorded_branch["start_states"] != starts:
                    raise ValueError(f"branch {branch_name} start states mismatch")
                recorded_by_start = {
                    item["start_state"]: item
                    for item in recorded_branch["trajectories"]
                }
                if set(recorded_by_start) != set(starts):
                    raise ValueError(f"branch {branch_name} trajectory coverage mismatch")
                trajectories = []
                for start_state in starts:
                    current_state = start_state
                    states = [start_state]
                    steps = []
                    for relation, input_symbol in zip(relations, input_sequence):
                        source = pair_lookup[relation["from"]]
                        source_child = child_for_state(source, current_state)
                        transition = model["outgoing"][current_state][input_symbol]
                        view = child_view_for_input(
                            source, source_child["name"], input_symbol
                        )
                        step = {
                            "relation_from": relation["from"],
                            "src": transition["src"],
                            "dst": transition["dst"],
                            "input": transition["input"],
                            "output": transition["output"],
                            "source_child": source_child["name"],
                        }
                        if "to" in relation:
                            target = pair_lookup[relation["to"]]
                            target_child = child_for_state(target, transition["dst"])
                            if target_child["name"] != view["target_label"]:
                                raise ValueError(
                                    f"target child mismatch for {transition}"
                                )
                            step["relation_to"] = relation["to"]
                            step["target_child"] = target_child["name"]
                        else:
                            step["to_terminal"] = relation["to_terminal"]
                            step["terminal_class"] = view["target_label"]
                        steps.append(step)
                        current_state = transition["dst"]
                        states.append(current_state)
                    recomputed = {
                        "start_state": start_state,
                        "states": states,
                        "steps": steps,
                    }
                    if recorded_by_start[start_state] != recomputed:
                        raise ValueError(
                            f"recorded trajectory does not match original DOT: "
                            f"{entry['display_id']} branch {branch_name} {start_state}"
                        )
                    trajectories.append(recomputed)
                branches[branch_name] = {
                    "entry_child": entry_child["name"],
                    "start_states": starts,
                    "trajectories": trajectories,
                }
            variants.append({
                "variant_index": variant_index,
                "input_sequence": input_sequence,
                "branches": branches,
            })
        result_paths.append({
            "path_index": path_index,
            "node_keys": path_record["node_keys"],
            "relations": relations,
            "terminal": path_record["terminal"],
            "trace_variants": variants,
        })
    return result_paths


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_element(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element if local_name(item.tag) == name), None)


def svg_model(path: Path) -> dict[str, Any]:
    root = ET.fromstring(path.read_bytes())
    view_box = tuple(float(item) for item in root.attrib["viewBox"].split())
    if len(view_box) != 4:
        raise ValueError("SVG viewBox must contain four numbers")
    graph = next(
        (
            item for item in root.iter()
            if local_name(item.tag) == "g"
            and item.attrib.get("id") == "graph0"
            and item.attrib.get("class") == "graph"
        ),
        None,
    )
    if graph is None:
        raise ValueError("SVG has no graph0 group")
    transform = graph.attrib.get("transform", "")
    translate_match = TRANSLATE_RE.search(transform)
    if not translate_match or "scale(1 1)" not in transform or "rotate(0)" not in transform:
        raise ValueError(f"unsupported graph0 transform: {transform!r}")
    translate = (float(translate_match.group(1)), float(translate_match.group(2)))
    nodes: dict[str, dict[str, float]] = {}
    edges: dict[tuple[str, str], list[dict[str, str]]] = {}
    for group in graph:
        if local_name(group.tag) != "g":
            continue
        title = child_element(group, "title")
        title_text = title.text if title is not None else ""
        if group.attrib.get("class") == "node" and re.fullmatch(r"s\d+", title_text or ""):
            shape = next(
                (
                    item for item in group
                    if local_name(item.tag) in {"ellipse", "circle"}
                ),
                None,
            )
            if shape is None:
                raise ValueError(f"state node {title_text} has no circle/ellipse")
            rx = float(shape.attrib.get("rx", shape.attrib.get("r", "0")))
            ry = float(shape.attrib.get("ry", shape.attrib.get("r", "0")))
            nodes[title_text] = {
                "cx": float(shape.attrib["cx"]),
                "cy": float(shape.attrib["cy"]),
                "rx": rx,
                "ry": ry,
            }
        if group.attrib.get("class") == "edge":
            match = PAIR_TITLE_RE.fullmatch(title_text or "")
            if not match:
                continue
            paths = [item for item in group if local_name(item.tag) == "path"]
            if len(paths) != 1:
                raise ValueError(f"edge {title_text} must contain exactly one path")
            texts = [
                "".join(item.itertext()).strip()
                for item in group
                if local_name(item.tag) == "text"
            ]
            edges.setdefault((match.group(1), match.group(2)), []).append({
                "d": paths[0].attrib["d"],
                "label": " ".join(item for item in texts if item),
            })
    return {
        "root": root,
        "nodes": nodes,
        "edges": edges,
        "view_box": view_box,
        "translate": translate,
    }


def map_base_geometries(
    base_model: dict[str, Any],
    svg: dict[str, Any],
) -> dict[tuple[str, str, str, str], str]:
    svg_edge_count = sum(len(items) for items in svg["edges"].values())
    if svg_edge_count != len(base_model["render_edges"]):
        raise ValueError(
            f"base DOT/SVG edge count mismatch: "
            f"{len(base_model['render_edges'])} != {svg_edge_count}"
        )
    geometries: dict[tuple[str, str, str, str], str] = {}
    for edge in base_model["render_edges"]:
        candidates = [
            item for item in svg["edges"].get((edge["src"], edge["dst"]), [])
            if item["label"] == edge["label"]
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"base DOT edge does not map uniquely to SVG: "
                f"{edge['src']}->{edge['dst']} {edge['label']!r}"
            )
        for input_symbol in edge["inputs"]:
            geometries[
                (edge["src"], edge["dst"], input_symbol, edge["output"])
            ] = candidates[0]["d"]
    return geometries


def color_for_entry(display_id: str) -> str:
    match = DISPLAY_ID_RE.fullmatch(display_id)
    if not match:
        raise ValueError(f"entry has no canonical Bnn display ID: {display_id!r}")
    index = int(match.group(1))
    if index < 1:
        raise ValueError(f"invalid display ID: {display_id!r}")
    return PALETTE[(index - 1) % len(PALETTE)]


def graph_to_page(
    point: tuple[float, float],
    translate: tuple[float, float],
) -> tuple[float, float]:
    return point[0] + translate[0], point[1] + translate[1]


def self_loop_path(
    node: dict[str, float],
    view_box: tuple[float, float, float, float],
    translate: tuple[float, float],
) -> tuple[str, str]:
    cx, cy, rx, ry = node["cx"], node["cy"], node["rx"], node["ry"]
    page_x, page_y = graph_to_page((cx, cy), translate)
    x0, y0, width, height = view_box
    clearances = {
        "top": page_y - y0 - ry,
        "right": x0 + width - page_x - rx,
        "bottom": y0 + height - page_y - ry,
        "left": page_x - x0 - rx,
    }
    direction = max(("top", "right", "bottom", "left"), key=lambda item: clearances[item])
    reach = max(30.0, 1.6 * max(rx, ry))
    if clearances[direction] < reach + 3:
        raise ValueError("no self-loop direction fits inside the existing viewBox")
    if direction == "top":
        start, end = (cx - .7 * rx, cy - .7 * ry), (cx + .7 * rx, cy - .7 * ry)
        c1, c2 = (cx - 1.6 * rx, cy - ry - reach), (cx + 1.6 * rx, cy - ry - reach)
    elif direction == "right":
        start, end = (cx + .7 * rx, cy - .7 * ry), (cx + .7 * rx, cy + .7 * ry)
        c1, c2 = (cx + rx + reach, cy - 1.6 * ry), (cx + rx + reach, cy + 1.6 * ry)
    elif direction == "bottom":
        start, end = (cx + .7 * rx, cy + .7 * ry), (cx - .7 * rx, cy + .7 * ry)
        c1, c2 = (cx + 1.6 * rx, cy + ry + reach), (cx - 1.6 * rx, cy + ry + reach)
    else:
        start, end = (cx - .7 * rx, cy + .7 * ry), (cx - .7 * rx, cy - .7 * ry)
        c1, c2 = (cx - rx - reach, cy + 1.6 * ry), (cx - rx - reach, cy - 1.6 * ry)
    return (
        f"M{start[0]:.2f},{start[1]:.2f}"
        f"C{c1[0]:.2f},{c1[1]:.2f} {c2[0]:.2f},{c2[1]:.2f} "
        f"{end[0]:.2f},{end[1]:.2f}",
        direction,
    )


def ellipse_boundary(
    node: dict[str, float],
    unit: tuple[float, float],
) -> float:
    ux, uy = unit
    return 1.0 / math.sqrt(
        (ux / node["rx"]) ** 2 + (uy / node["ry"]) ** 2
    )


def generated_curve(
    source: dict[str, float],
    target: dict[str, float],
    view_box: tuple[float, float, float, float],
    translate: tuple[float, float],
) -> tuple[str, str]:
    dx, dy = target["cx"] - source["cx"], target["cy"] - source["cy"]
    distance = math.hypot(dx, dy)
    if distance == 0:
        raise ValueError("cannot generate a non-loop curve for coincident nodes")
    unit = (dx / distance, dy / distance)
    source_radius = ellipse_boundary(source, unit)
    target_radius = ellipse_boundary(target, unit)
    start = (
        source["cx"] + unit[0] * source_radius,
        source["cy"] + unit[1] * source_radius,
    )
    end = (
        target["cx"] - unit[0] * target_radius,
        target["cy"] - unit[1] * target_radius,
    )
    normal = (-unit[1], unit[0])
    offset = min(120.0, max(30.0, distance * .15))
    candidates = []
    x0, y0, width, height = view_box
    for sign in (1.0, -1.0):
        c1 = (
            start[0] + dx / 3 + normal[0] * offset * sign,
            start[1] + dy / 3 + normal[1] * offset * sign,
        )
        c2 = (
            end[0] - dx / 3 + normal[0] * offset * sign,
            end[1] - dy / 3 + normal[1] * offset * sign,
        )
        page_points = [
            graph_to_page(point, translate) for point in (start, c1, c2, end)
        ]
        margins = [
            point[0] - x0 for point in page_points
        ] + [
            x0 + width - point[0] for point in page_points
        ] + [
            point[1] - y0 for point in page_points
        ] + [
            y0 + height - point[1] for point in page_points
        ]
        candidates.append((min(margins), sign, c1, c2))
    clearance, sign, c1, c2 = max(candidates, key=lambda item: item[0])
    if clearance < 3:
        raise ValueError("generated curve would leave the existing viewBox")
    return (
        f"M{start[0]:.2f},{start[1]:.2f}"
        f"C{c1[0]:.2f},{c1[1]:.2f} {c2[0]:.2f},{c2[1]:.2f} "
        f"{end[0]:.2f},{end[1]:.2f}",
        "positive_normal" if sign > 0 else "negative_normal",
    )


def enrich_paths_with_geometry(
    paths: list[dict[str, Any]],
    model: dict[str, Any],
    base_geometries: dict[tuple[str, str, str, str], str],
    svg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    sinks = null_sink_states(model)
    geometry_ids: dict[str, str] = {}
    geometries: list[dict[str, str]] = []

    def register(d: str, method: str) -> str:
        if d not in geometry_ids:
            identifier = f"G{len(geometry_ids) + 1:03d}"
            geometry_ids[d] = identifier
            geometries.append({"geometry_id": identifier, "d": d, "method": method})
        return geometry_ids[d]

    for path_record in paths:
        for variant in path_record["trace_variants"]:
            for branch_name in ("A", "B"):
                for trajectory in variant["branches"][branch_name]["trajectories"]:
                    for step in trajectory["steps"]:
                        src, dst = step["src"], step["dst"]
                        key = (src, dst, step["input"], step["output"])
                        if dst in sinks:
                            step.update({
                                "rendered": False,
                                "render_source": "skipped_sink",
                                "geometry_id": None,
                                "skip_reason": (
                                    f"{dst} is a null-action self-loop sink under SMP rules"
                                ),
                            })
                            continue
                        if key in base_geometries:
                            d = base_geometries[key]
                            method = "base_svg_edge"
                            source = "base_edge"
                        elif src == dst:
                            d, direction = self_loop_path(
                                svg["nodes"][src], svg["view_box"], svg["translate"]
                            )
                            method = f"generated_self_loop_{direction}"
                            source = "restored_self_loop"
                        elif dst == "s0":
                            reverse_paths = {
                                item["d"] for item in svg["edges"].get((dst, src), [])
                            }
                            if len(reverse_paths) == 1:
                                d = next(iter(reverse_paths))
                                method = "reverse_base_edge"
                            else:
                                d, direction = generated_curve(
                                    svg["nodes"][src],
                                    svg["nodes"][dst],
                                    svg["view_box"],
                                    svg["translate"],
                                )
                                method = f"generated_to_s0_{direction}"
                            source = "restored_to_s0"
                        else:
                            raise ValueError(
                                f"trace edge is absent from SMP for an unsupported reason: "
                                f"{src}->{dst} {step['input']}/{step['output']}"
                            )
                        step.update({
                            "rendered": True,
                            "render_source": source,
                            "geometry_id": register(d, method),
                            "geometry_method": method,
                        })
    return paths, geometries


def overlay_fragment(geometries: list[dict[str, str]], color: str) -> str:
    lines = [
        OVERLAY_BEGIN,
        (
            f'<g id="backtrace-overlay" class="backtrace-overlay" '
            f'fill="none" stroke="{color}" stroke-width="6" '
            f'stroke-opacity="0.50" stroke-linecap="round" '
            f'stroke-linejoin="round" marker-start="none" marker-end="none" '
            f'pointer-events="none">'
        ),
    ]
    lines.extend(
        f'  <path id="backtrace-geometry-{item["geometry_id"]}" '
        f'd="{html.escape(item["d"], quote=True)}"/>'
        for item in geometries
    )
    lines.extend(["</g>", OVERLAY_END])
    return "\n".join(lines) + "\n"


def inject_overlay(base_svg: Path, fragment: str) -> bytes:
    original_bytes = base_svg.read_bytes()
    original = original_bytes.decode("utf-8")
    if OVERLAY_BEGIN in original or 'id="backtrace-overlay"' in original:
        raise ValueError("base SVG already contains a backtrace overlay")
    svg_close = original.rfind("</svg>")
    graph_close = original.rfind("</g>", 0, svg_close)
    if graph_close < 0:
        raise ValueError("could not locate graph0 closing tag")
    injected = original[:graph_close] + fragment + original[graph_close:]
    restored = injected.replace(fragment, "", 1)
    if restored.encode("utf-8") != original_bytes:
        raise AssertionError("removing overlay fragment does not restore base SVG bytes")
    return injected.encode("utf-8")


def locate_inkscape() -> str:
    found = shutil.which("inkscape.com") or shutil.which("inkscape")
    if found:
        return found
    if WINDOWS_INKSCAPE.is_file():
        return str(WINDOWS_INKSCAPE)
    raise RuntimeError("Inkscape is required for PDF export")


def parse_formats(value: str) -> list[str]:
    formats = []
    for item in value.split(","):
        normalized = item.strip().lower()
        if not normalized:
            continue
        if normalized not in {"svg", "pdf"}:
            raise argparse.ArgumentTypeError(f"unsupported format: {normalized}")
        if normalized not in formats:
            formats.append(normalized)
    if not formats:
        raise argparse.ArgumentTypeError("at least one of svg,pdf is required")
    return formats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render one independent Mealy backtrace over an existing SMP SVG"
    )
    parser.add_argument("--dot", type=Path, required=True)
    parser.add_argument("--base-dot", type=Path, required=True)
    parser.add_argument("--base-svg", type=Path, required=True)
    parser.add_argument("--backtrace-json", type=Path, required=True)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--formats", type=parse_formats, default=parse_formats("svg,pdf"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dot = args.dot.resolve()
    base_dot = args.base_dot.resolve()
    base_svg = args.base_svg.resolve()
    backtrace_json = args.backtrace_json.resolve()
    for path in (dot, base_dot, base_svg, backtrace_json):
        if not path.is_file():
            raise FileNotFoundError(path)
    model = parse_dot(dot, require_complete=True)
    base_model = parse_dot(base_dot, require_complete=False)
    payload = load_backtrace(backtrace_json, dot)
    entry = select_entry(payload, args.entry)
    svg = svg_model(base_svg)
    state_set = set(model["states"])
    if set(base_model["states"]) != state_set:
        raise ValueError("SMP DOT state set does not match original DOT")
    if set(svg["nodes"]) != state_set:
        raise ValueError("SMP SVG state set does not match original DOT")
    base_geometries = map_base_geometries(base_model, svg)
    paths = recompute_entry_paths(payload, entry, model)
    paths, geometries = enrich_paths_with_geometry(
        paths, model, base_geometries, svg
    )
    color = color_for_entry(entry["display_id"])
    fragment = overlay_fragment(geometries, color)
    svg_bytes = inject_overlay(base_svg, fragment)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{dot.stem}_{entry['display_id']}_backtrace_path"
    targets = {"json": output_dir / f"{stem}.json"}
    for output_format in args.formats:
        targets[output_format] = output_dir / f"{stem}.{output_format}"
    protected = [path for path in targets.values() if path.exists()]
    if protected:
        raise FileExistsError(
            "refusing to replace existing output(s): "
            + ", ".join(str(path) for path in protected)
        )

    with tempfile.TemporaryDirectory(dir=output_dir, prefix=f".{stem}-") as temp:
        temp_dir = Path(temp)
        temp_svg = temp_dir / f"{stem}.svg"
        temp_svg.write_bytes(svg_bytes)
        temp_outputs: dict[str, Path] = {}
        if "svg" in args.formats:
            temp_outputs["svg"] = temp_svg
        if "pdf" in args.formats:
            temp_pdf = temp_dir / f"{stem}.pdf"
            completed = subprocess.run(
                [
                    locate_inkscape(),
                    "--export-area-page",
                    f"--export-filename={temp_pdf}",
                    str(temp_svg),
                ],
                text=True,
                capture_output=True,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise RuntimeError(f"Inkscape PDF export failed: {detail}")
            if not temp_pdf.is_file() or temp_pdf.stat().st_size == 0:
                raise RuntimeError("Inkscape did not create a non-empty PDF")
            temp_outputs["pdf"] = temp_pdf

        audit = {
            "schema_version": 1,
            "kind": "mealy_backtrace_overlay",
            "sources": {
                "original_dot": source_record(dot),
                "base_dot": source_record(base_dot),
                "base_svg": source_record(base_svg),
                "backtrace_json": source_record(backtrace_json),
            },
            "overlays": [{
                "entry": {
                    "display_id": entry["display_id"],
                    "key": entry["key"],
                    "role": entry["role"],
                    "children": entry["children"],
                },
                "terminals": sorted({
                    path_record["terminal"] for path_record in paths
                }),
                "color": color,
                "style": {
                    "fill": "none",
                    "stroke_width_px": 6,
                    "stroke_opacity": 0.50,
                    "stroke_linecap": "round",
                    "stroke_linejoin": "round",
                    "marker_start": "none",
                    "marker_end": "none",
                },
                "paths": paths,
                "geometries": geometries,
                "concrete_segment_count": sum(
                    len(trajectory["steps"])
                    for path_record in paths
                    for variant in path_record["trace_variants"]
                    for branch in variant["branches"].values()
                    for trajectory in branch["trajectories"]
                ),
                "unique_rendered_geometry_count": len(geometries),
            }],
            "outputs": {
                output_format: {
                    "path": str(targets[output_format]),
                    "bytes": temp_path.stat().st_size,
                    "sha256": sha256(temp_path),
                }
                for output_format, temp_path in temp_outputs.items()
            },
        }
        temp_json = temp_dir / f"{stem}.json"
        temp_json.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_outputs["json"] = temp_json
        published = []
        try:
            for output_format in [*args.formats, "json"]:
                source = temp_outputs[output_format]
                destination = targets[output_format]
                os.replace(source, destination)
                published.append(destination)
        except Exception:
            for path in published:
                path.unlink(missing_ok=True)
            raise
    print(json.dumps({
        "entry": entry["display_id"],
        "color": color,
        "concrete_segments": audit["overlays"][0]["concrete_segment_count"],
        "unique_geometries": len(geometries),
        "outputs": [str(path) for path in targets.values()],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
