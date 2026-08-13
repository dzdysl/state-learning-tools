import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(r"D:\state-learning-lab\projects\state-learning-experiments\experiments\open5gs\ueransim-smc-context-pdu-selection\h14-base-runtime-20260804\analysis\register-inference")
SCRIPT = Path(__file__).parents[1] / "experiments" / "visualize_directed_polyline_families.py"
SPEC = importlib.util.spec_from_file_location("directed_polyline", SCRIPT)
VIS = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(VIS)


@unittest.skipUnless(ROOT.exists(), "requires frozen H14 inputs")
class DirectedPolylineVisualizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candidates = json.loads((ROOT / "candidates.json").read_text(encoding="utf-8"))
        cls.scope = json.loads((ROOT / "trajectory-clusters.json").read_text(encoding="utf-8"))
        trace = [json.loads(line) for line in (ROOT.parents[1] / "evidence" / "statelearner_trace.jsonl").read_text(encoding="utf-8").splitlines()]
        import yaml
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        cycle = json.loads((ROOT.parents[2] / "h14-complete-teardown-20260801" / "analysis" / "cycle-cover" / "base-result.json").read_text(encoding="utf-8"))
        cls.payload = VIS.build_payload(cls.candidates, cls.scope, trace, cycle, config)

    def test_h14_member_and_point_counts(self):
        self.assertEqual(self.payload["count"], 86)
        self.assertEqual(len({m["edge"]["edge_id"] for m in self.payload["members"]}), 29)
        self.assertTrue(all(len(m["points"]) == 8 for m in self.payload["members"]))
        self.assertTrue(all([p["R"] for p in m["points"]] == list(range(3, 11)) for m in self.payload["members"]))

    def test_carried_r2_corrections_and_no_completion(self):
        wanted = {"E0019:S036:L22": 3, "E0019:S036:L24": 3,
                  "E0019:S037:L26": 1, "E0019:S037:L28": 1}
        members = {m["id"]: m for m in self.payload["members"]}
        for key, value in wanted.items():
            self.assertEqual(members[key]["points"][0]["r_i"], value)
            self.assertEqual(members[key]["points"][0]["source"], "carried_from_R2")
        self.assertNotIn("pattern_completed", json.dumps(self.payload, ensure_ascii=False))

    def test_exact_templates_and_formula_states(self):
        for member in self.payload["members"]:
            same = [m for m in self.payload["members"] if m["template"] == member["template"]]
            self.assertTrue(all(m["template_key"] == member["template_key"] for m in same))
        states = {m["kind"] for m in self.payload["members"]}
        self.assertEqual(states, {"相对稳定推断", "假设性候选（观察区域归因）"})
        failed = [m for m in self.payload["members"] if m["edge"]["edge_id"] == "E0073"]
        self.assertTrue(failed)
        self.assertTrue(all(m["formula_status"] == "combined_sample_fit_failed" for m in failed))
        self.assertEqual({m["signal_slice"] for m in self.payload["members"]}, {"0", "1", "not_applicable"})

    def test_direction_and_wrap_geometry_is_real_and_static_has_no_arrows(self):
        dynamic = next(m for m in self.payload["members"] if m["template_type"] == "动态模板")
        geometry = VIS.visual_segments(dynamic["points"], "ba")
        self.assertTrue(geometry["arrows"])
        first = geometry["arrows"][0]
        self.assertEqual(first["vector"], [dynamic["points"][1]["r_before"] - dynamic["points"][0]["r_before"],
                                           dynamic["points"][1]["r_after"] - dynamic["points"][0]["r_after"]])
        self.assertTrue(any(w["index"] >= 0 for w in geometry["wraps"]))
        static = next(m for m in self.payload["members"] if m["template_type"] == "静态模板")
        self.assertEqual([], VIS.visual_segments(static["points"], "ba")["arrows"])

    def test_plotly_up_angles_cover_cardinal_and_diagonal_data_directions(self):
        expected = {(0, 1): 0, (1, 0): 90, (0, -1): 180, (-1, 0): -90,
                    (1, 1): 45, (1, -1): 135, (-1, 1): -45, (-1, -1): -135}
        for vector, angle in expected.items():
            self.assertAlmostEqual(VIS.plotly_up_angle(*vector), angle)

    def test_deterministic_offline_html_without_clustering_terms(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "one.html", Path(temp) / "two.html"
            VIS.render_html(self.payload, first); VIS.render_html(self.payload, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            content = first.read_text(encoding="utf-8")
            page, custom = content.split("</script><script>", 1)
            text = (page.split("<script>", 1)[0] + custom).lower()
            for token in ("3d r_before/r_i/r_after", "2d r_before-r_after", "2d r_i-r_after", "层级筛选", "template_type", "x.signal_slice", "方向箭头", "6→0", "i=7"):
                self.assertIn(token.lower(), text)
            for forbidden in ("soft-dtw", "distance_matrix", "silhouette", "merge gap"):
                self.assertNotIn(forbidden, text)

    def test_template_details_consume_each_member_points_and_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "details.html"
            VIS.render_html(self.payload, output)
            text = output.read_text(encoding="utf-8")
            self.assertIn("function detailPath(points)", text)
            self.assertIn("R${q.R}:(${q.r_before},${q.r_i},${q.r_after})", text)
            self.assertIn("[${esc(q.source)}]", text)
            self.assertIn("s=${esc(x.signal_slice)}", text)
            self.assertIn("template-card", text)
            self.assertIn("angleref:'up'", text)
            self.assertIn("a.plotly_up_angle", text)
            self.assertNotIn("a.angle*180/Math.PI-90", text)

    def test_member_legend_group_links_main_direction_and_wrap_traces(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "legend-groups.html"
            VIS.render_html(self.payload, output)
            text = output.read_text(encoding="utf-8")
            self.assertIn("legendgroup:`member:${x.id}`", text)
            self.assertIn("directionTrace(x.visuals[mode],mode,color,`member:${x.id}`)", text)
            self.assertIn("wrapTrace(x.visuals[mode],mode,`member:${x.id}`)", text)
            self.assertIn("layout.legend={groupclick:'togglegroup'}", text)
            self.assertIn("legendgroup,showlegend:false", text)
            self.assertNotIn("legendgroup:'wrap'", text)

    def test_html_uses_fixed_global_ranges_and_equal_units(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "global-ranges.html"
            VIS.render_html(self.payload, output)
            text = output.read_text(encoding="utf-8")
            self.assertIn("DATA.members.flatMap(x=>x.points)", text)
            self.assertIn("GLOBAL_RANGES.r_before", text)
            self.assertIn("GLOBAL_RANGES.r_i", text)
            self.assertIn("GLOBAL_RANGES.r_after", text)
            self.assertIn("fixedrange:true", text)
            self.assertIn("scaleanchor:'y',scaleratio:1", text)
            self.assertIn("layout.scene.aspectmode='data'", text)


if __name__ == "__main__":
    unittest.main()
