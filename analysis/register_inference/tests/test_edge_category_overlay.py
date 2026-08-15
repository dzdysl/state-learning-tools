import hashlib
import json
import math
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from analysis.register_inference.experiments.render_edge_category_overlay import (
    COLORS,
    SVG_NS,
    _continuous_extension_boundary,
    derive_overlay,
    parse_dot_edges,
    render,
)


SERIES = Path(
    r"D:\state-learning-lab\projects\state-learning-experiments\experiments\open5gs"
    r"\ueransim-smc-context-pdu-selection"
)
H14 = SERIES / "h14-base-runtime-20260804"
MODEL = SERIES / "h14-complete-teardown-20260801"
CANDIDATES = H14 / "analysis/register-inference/candidates.json"
FORMULAS = H14 / "analysis/register-inference/trajectory-formula-candidates.json"
DOT = MODEL / "evidence/hypotheses/hypothesis_14.dot"
BASE_SVG = MODEL / "analysis/model/smp.svg"
NS = {"svg": SVG_NS}


class EdgeCategoryOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candidates = json.loads(CANDIDATES.read_text(encoding="utf-8"))
        cls.formulas = json.loads(FORMULAS.read_text(encoding="utf-8"))
        cls.derived = derive_overlay(cls.candidates, cls.formulas, parse_dot_edges(DOT))

    def test_global_hold_input_output_difference_and_dot_edges(self):
        self.assertEqual(16, len(self.derived["complete_length_two_regions"]))
        self.assertEqual(
            [
                "deregistrationRequest/null_action",
                "securityModeComplete/registrationAccept",
            ],
            self.derived["inside_predecessor_input_outputs"],
        )
        self.assertEqual(
            ["securityModeReject/null_action"],
            self.derived["outside_predecessor_input_outputs"],
        )
        self.assertEqual([], self.derived["overlap_predecessor_input_outputs"])
        self.assertEqual(
            self.derived["inside_predecessor_input_outputs"],
            self.derived["propagated_input_outputs"],
        )
        counts = Counter(edge["input_output"] for edge in self.derived["propagated_dot_edges"])
        self.assertEqual(
            {
                "deregistrationRequest/null_action": 18,
                "securityModeComplete/registrationAccept": 2,
            },
            dict(counts),
        )

    def test_regular_registration_matched_dynamic_length_two_regions(self):
        matched = self.derived["matched_dynamic_length_two_regions"]
        self.assertEqual(
            ["E0133:S003:L3", "E0145:S005:L6", "E0145:S012:L14"],
            [item["trajectory_id"] for item in matched],
        )
        self.assertEqual(
            [("E0124", "E0133"), ("E0160", "E0145"), ("E0172", "E0145")],
            [
                (item["predecessor_edge"]["edge_id"], item["terminal_edge"]["edge_id"])
                for item in matched
            ],
        )

    def test_only_continuous_extension_length_one_regions_are_retained(self):
        self.assertEqual(
            [
                "E0050:S009:L12",
                "E0133:S003:L3",
                "E0133:S039:L29",
                "E0145:S005:L6",
                "E0145:S012:L14",
                "E0146:S005:L7",
                "E0146:S012:L15",
            ],
            [item["id"] for item in self.derived["new_structural_length_one_regions"]],
        )
        self.assertTrue(
            all(not item["formula_fitted"] for item in self.derived["new_structural_length_one_regions"])
        )
        rejected = {
            item["id"]: item
            for item in self.derived["rejected_non_continuous_extension_regions"]
        }
        self.assertIn("E0001:S018:L18", rejected)
        sample = rejected["E0001:S018:L18"]["samples"][0]
        self.assertEqual(
            ["E0042", "E0086"],
            [item["edge_id"] for item in sample["interruptions"]],
        )
        self.assertEqual(
            ["E0076"],
            [item["edge_id"] for item in sample["ignored_extension_edges"]],
        )

    def test_extension_continuity_never_restarts_after_an_interruption(self):
        def edge(eid, logical_input, logical_output="null_action"):
            return {
                "edge_id": eid,
                "logical_input": logical_input,
                "logical_output": logical_output,
            }

        extension = {"deregistrationRequest/null_action"}
        continuous = _continuous_extension_boundary(
            [
                edge("H1", "deregistrationRequest"),
                edge("H2", "deregistrationRequest"),
                edge("T", "registrationRequest", "authenticationRequest"),
            ],
            extension,
        )
        self.assertEqual(1, continuous["boundary_index"])
        self.assertEqual(1, continuous["terminal_suffix_length"])

        interrupted = _continuous_extension_boundary(
            [
                edge("X", "securityModeReject"),
                edge("H", "deregistrationRequest"),
                edge("T", "registrationRequest", "authenticationRequest"),
            ],
            extension,
        )
        self.assertIsNone(interrupted["boundary_index"])
        self.assertEqual(["X"], [item["edge_id"] for item in interrupted["interruptions"]])
        self.assertEqual(
            ["H"], [item["edge_id"] for item in interrupted["ignored_extension_edges"]]
        )

    def test_svg_roles_labels_and_missing_smp_edges(self):
        before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (CANDIDATES, FORMULAS)
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "overlay.svg"
            summary = render(BASE_SVG, DOT, CANDIDATES, FORMULAS, output)
            root = ET.parse(output).getroot()
        self.assertEqual(20, summary["propagated_dot_edge_count"])
        self.assertEqual(16, summary["visible_propagated_dot_edge_count"])
        self.assertEqual(7, summary["s0_return_stub_count"])
        self.assertEqual(4, summary["omitted_self_loop_count"])
        self.assertEqual(7, summary["new_structural_length_one_count"])
        self.assertGreater(summary["rejected_non_continuous_extension_count"], 0)
        xml = ET.tostring(root, encoding="unicode")
        for role in ("relatively_stable", "hypothetical", "global_hold", "new_length_one"):
            color = COLORS[role]
            self.assertIn(color, xml)
        self.assertNotIn(COLORS["terminal_attribution"], xml)
        self.assertIn("匹配动态长度2区域直接支持的前序最简边", xml)
        self.assertIn("须连续有效", xml)
        self.assertIn("遇到非延伸假设性边即中断", xml)
        self.assertIn("派生颜色直接覆盖原边颜色", xml)
        self.assertIn("派生标记，不拟合公式", xml)
        stubs = root.findall(".//svg:g[@class='edge register-overlay-s0-return-stub']", NS)
        self.assertEqual(7, len(stubs))
        marker = root.find(".//svg:marker[@id='global-hold-arrow']", NS)
        self.assertEqual("userSpaceOnUse", marker.attrib["markerUnits"])
        self.assertEqual("8", marker.attrib["markerWidth"])
        self.assertEqual("8", marker.attrib["markerHeight"])
        self.assertNotIn("register-overlay-dot-only", xml)
        self.assertNotIn("dot-only-edge", xml)
        self.assertNotIn("DOT补绘", xml)
        for group in stubs:
            self.assertEqual("s0", group.attrib["data-target-state"])
            self.assertNotEqual("s0", group.attrib["data-source-state"])
            self.assertEqual([], group.findall("svg:text", NS))
            path = group.find("svg:path", NS)
            self.assertIsNotNone(path)
            self.assertEqual(COLORS["global_hold"], path.attrib["stroke"])
            self.assertEqual("28.35", path.attrib["data-stub-length-pt"])
            match = re.fullmatch(
                r"M(-?\d+\.\d+),(-?\d+\.\d+) L(-?\d+\.\d+),(-?\d+\.\d+)",
                path.attrib["d"],
            )
            self.assertIsNotNone(match)
            x1, y1, x2, y2 = map(float, match.groups())
            self.assertLess(x2, x1)
            self.assertLess(y2, y1)
            self.assertAlmostEqual(28.35, math.hypot(x2 - x1, y2 - y1), places=1)

        graph = root.find("svg:g", NS)
        edge_groups = [group for group in graph.findall("svg:g", NS) if group.attrib.get("class", "").startswith("edge")]
        self.assertTrue(all(len(group.findall("svg:path", NS)) == 1 for group in edge_groups))

        by_title = {
            group.find("svg:title", NS).text: group
            for group in edge_groups
            if group.find("svg:title", NS) is not None
        }
        for pair in ("s11->s10", "s12->s13"):
            group = by_title[pair]
            self.assertEqual("new_length_one", group.attrib["data-visible-role"])
            path = group.find("svg:path", NS)
            self.assertEqual(COLORS["new_length_one"], path.attrib["stroke"])
            self.assertEqual("13,6", path.attrib["stroke-dasharray"])
            polygon = group.find("svg:polygon", NS)
            self.assertEqual(COLORS["new_length_one"], polygon.attrib["fill"])
            self.assertIn("terminal_attribution", group.attrib["data-register-inference-categories"])

        for pair in ("s10->s11", "s13->s12", "s14->s12"):
            path = by_title[pair].find("svg:path", NS)
            self.assertEqual(COLORS["global_hold"], path.attrib["stroke"])
            self.assertNotIn("stroke-dasharray", path.attrib)
        ordinary = by_title["s5->s12"].find("svg:path", NS)
        self.assertEqual(COLORS["global_hold"], ordinary.attrib["stroke"])
        self.assertEqual("10,5", ordinary.attrib["stroke-dasharray"])
        self.assertIn("#111111", xml)
        after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (CANDIDATES, FORMULAS)
        }
        self.assertEqual(before, after)

    def test_render_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.svg"
            second = Path(directory) / "second.svg"
            render(BASE_SVG, DOT, CANDIDATES, FORMULAS, first)
            render(BASE_SVG, DOT, CANDIDATES, FORMULAS, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
