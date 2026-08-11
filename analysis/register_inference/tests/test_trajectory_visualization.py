from __future__ import annotations
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("visualize_cycle_trajectories", MODULE / "experiments" / "visualize_cycle_trajectories.py")
assert SPEC and SPEC.loader
VIS = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(VIS)

class TrajectoryVisualizationTests(unittest.TestCase):
    def sample(self) -> dict:
        point = lambda pos, value: {"cycle_position":pos,"cycle":1,"source_repetition":pos+2,"source":"observed","same_phase_imputed":False,"pattern_completed":False,"r_before":value,"r_after":(value+1)%7,"i":7 if pos == 1 else value,"signals":[]}
        item = lambda ident, origin, status: {"id":ident,"origin":origin,"edge":{"edge_id":ident,"logical_input":"registrationRequest","logical_output":"authenticationRequest","source_state":"s0","target_state":"s1"},"cycle_id":"C1","sequence_line":1,"signal_slice":0,"clustering_status":status,"migration_status":None,"samples":[{"inputs":[{"input_register_id":"ngksi_uplink","value":7}],"effective_region_snapshot":{"numeric_inputs":[]}}],"analysis_points":[point(1,6),point(2,0)]}
        return {"schema":"register-trajectory-clustering-v2","trajectories":[item("stable","stable","eligible"),item("low","stable","low_discriminability"),item("hyp","hypothetical","eligible")],"tiers":{"stable_internal":[{"clusters":[["stable"]]}],"hypothetical_internal":[{"clusters":[["hyp"]]}],"joint":[{"clusters":[["stable","hyp"]]}]}}

    def test_panels_keep_tier_labels_and_low_background_scoped(self):
        data = self.sample()
        self.assertEqual({"stable":1}, VIS.panel_members(data,"stable_internal")[("registrationRequest","authenticationRequest","0")])
        self.assertEqual(["stable","low"], [x["id"] for x in VIS.panel_trajectories(data,"stable_internal")[("registrationRequest","authenticationRequest","0")]])
        self.assertEqual(["hyp"], [x["id"] for x in VIS.panel_trajectories(data,"hypothetical_internal")[("registrationRequest","authenticationRequest","0")]])

    def test_offline_html_and_three_svg_outputs(self):
        data = self.sample()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); html = root / "trajectory-visualization.html"; VIS.html(data, html)
            content = html.read_text(encoding="utf-8")
            self.assertIn("plotly.js", content); self.assertIn("tier-buttons", content); self.assertIn("trajectory-canvas", content)
            self.assertIn("Plotly.react", content); self.assertIn("6→0", content); self.assertEqual(1, content.count("const PANELS="))
            for tier, name in (("stable_internal","stable.svg"),("hypothetical_internal","hypothetical.svg"),("joint","joint.svg")):
                path = root / name; VIS.svg(data, tier, path); self.assertIn("<svg", path.read_text(encoding="utf-8"))

    def test_each_html_panel_is_dimension_pure_and_deterministic(self):
        data = self.sample(); panels = VIS._html_panels(data)
        for panel in panels.values():
            types = {trace["type"] for trace in panel["figure"]["data"]}
            if panel["is3d"]:
                self.assertTrue(types <= {"scatter3d", "cone", "mesh3d"})
            else:
                self.assertEqual({"scatter"}, types)
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "first.html", Path(temp) / "second.html"
            VIS.html(data, first); VIS.html(data, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_transparent_hover_targets_cover_each_trajectory(self):
        panels = VIS._html_panels(self.sample())
        for panel in panels.values():
            traces = panel["figure"]["data"]
            trajectories = [trace for trace in traces if trace.get("meta", {}).get("role") == "trajectory"]
            targets = [trace for trace in traces if trace.get("meta", {}).get("role") == "hover-target"]
            self.assertEqual(len(trajectories), len(targets))
            for visible, target in zip(trajectories, targets):
                self.assertEqual(visible["text"], target["text"])
                self.assertGreater(target["marker"]["size"], visible["marker"]["size"])
                self.assertEqual(visible["meta"]["tier"], target["meta"]["tier"])
                self.assertEqual(visible["meta"]["cluster"], target["meta"]["cluster"])
                self.assertEqual(visible["meta"]["cycle"], target["meta"]["cycle"])
                self.assertEqual("rgba(0,0,0,0)", target["marker"]["color"])
                self.assertEqual(0, target["marker"]["opacity"])
                self.assertEqual(0, target["marker"]["line"]["width"])
                self.assertFalse(target["showlegend"])

    def test_html_localizes_internal_values_and_switches_canvas_geometry(self):
        data = self.sample()
        data["trajectories"][0]["signal_slice"] = "not_applicable"
        data["trajectories"][1]["signal_slice"] = "not_applicable"
        data["tiers"]["stable_internal"][0]["clusters"] = [["stable"]]
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "view.html"; VIS.html(data, target)
            content = target.read_text(encoding="utf-8")
        self.assertIn("not_applicable:'不适用'", content)
        self.assertIn("all:'全部'", content)
        self.assertIn("canvas.two-d #trajectory-canvas", content)
        self.assertIn("aspect-ratio:1/1", content)
        self.assertIn("function setCanvasMode", content)
        self.assertIn("Plotly.Plots.resize", content)

    def test_only_the_plotted_io_numeric_input_creates_z_axis(self):
        item = self.sample()["trajectories"][0]
        self.assertTrue(VIS._has_numeric_input(item))
        self.assertTrue(VIS._panel_uses_3d([item]))
        item["samples"] = [{"inputs": [], "effective_region_snapshot": {"numeric_inputs": []}}]
        self.assertFalse(VIS._has_numeric_input(item))
        other = self.sample()["trajectories"][0]
        self.assertFalse(VIS._panel_uses_3d([item, other]))

if __name__ == "__main__": unittest.main()
