from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "visualize_trajectory_formula_candidates",
    MODULE / "experiments" / "visualize_trajectory_formula_candidates.py",
)
assert SPEC and SPEC.loader
VIS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VIS)


class TrajectoryFormulaVisualizationTests(unittest.TestCase):
    def sample(self) -> dict:
        candidate = {"candidate_id":"B-before-after-a1","ast":{"kind":"affine_unit","variable":"r","offset":1},"formula":"r' = r + 1","formula_kind":"affine_unit","scope":"functional_subset","support_level":"core","evidence_grade":"observationally_exact_with_gaps","covered_x":[0,1],"missing_x":[],"unresolved_points":[[1,4]],"direction":{"forward":1,"reverse":0,"majority":"forward_majority"}}
        projection = {"axis":{"x":"r_before"},"unique_points":[{"x":0,"y":1,"support_count":2,"trajectory_ids":["T1"]},{"x":1,"y":2,"support_count":1,"trajectory_ids":["T1"]},{"x":1,"y":4,"support_count":1,"trajectory_ids":["T2"]}],"directed_segments":[{"tail":[0,1],"head":[1,2],"support_count":2,"trajectory_ids":["T1"]}],"vertical_components":[{"x":1,"distinct_y":[2,4],"strength":"weak","support_count":2,"points":[[1,2],[1,4]]}],"trajectory_evidence":[],"no_formula_reason":None,"candidates":[candidate]}
        edge = {"edge":{"edge_id":"E7","source_state":"s0","target_state":"s1","logical_input":"authenticationResponse","logical_output":"securityModeCommand"},"trajectory_count":2,"sample_count":3,"trajectory_ids":["T1","T2"],"signal_contexts":[{}],"projections":{"before_after":projection,"input_after":{**projection,"axis":{"x":"r_i"},"candidates":[],"no_formula_reason":"degenerate_only"}}}
        aggregate_candidate = {"candidate_id":"SB-a1","selection_tier":"simple_projection","formula_kind":"simple_formula","formula":"r' = r + 1","verification":{"sample_count":3,"matched_sample_count":3,"exact":True,"root_branch_counts":{},"failures":[]}}
        stable = {"by_input_output":{"authenticationResponse/securityModeCommand":{"logical_input":"authenticationResponse","logical_output":"securityModeCommand","source_edge_ids":["E7"],"source_trajectory_ids":["T1","T2"],"trajectory_count":2,"sample_count":3,"signal_condition":{"status":"not_applicable","values":{}},"projections":{"before_after":{**projection,"classification":"simple_exact"},"input_after":{**projection,"classification":"pure_vertical"}},"status":"inferred","selection_tier":"simple_projection","final_candidates":[aggregate_candidate]}}}
        edge_meta = {"edge_id":"E0042","source_state":"s2","target_state":"s3","logical_input":"authenticationResponse","logical_output":"securityModeCommand"}
        dynamic = [{"trajectory_id":f"D{index}","triple_points":[[index % 3, 7, (index + 1) % 8],[((index + 1) % 3), 6, (index + 2) % 8]]} for index in range(6)]
        samples = lambda value: [{"repetition":rep,"r_before":rep % 3,"r_i":7,"r_after":value} for rep in range(3, 11)]
        regions = lambda scenario, value: [
            {"id":f"{scenario}:E0042:P0","terminal_eid":"E0042","cycle_id":"S017","sequence_line":17,"boundary_kind":"pseudo_reverse_preimage","samples":samples(value),"dynamic_triples":True},
            {"id":f"{scenario}:E0046:P0","terminal_eid":"E0046","cycle_id":"S012","sequence_line":14,"boundary_kind":"pseudo_hold","samples":samples(6),"dynamic_triples":True},
            {"id":f"{scenario}:E0050:P1","terminal_eid":"E0050","cycle_id":"S009","sequence_line":12,"boundary_kind":"real_downlink","samples":samples(2),"dynamic_triples":False},
        ]
        predecessor = {"counts":{"dynamic_length_two_trajectory_count":6,"hold_edge_count":4,"assignment_scenario_count":2,"eligible_length_one_count":5},"dynamic_length_two_trajectories":dynamic,"hold_inferences":[{"eid":eid,"support_count":2 if eid=="E0172" else 1,"support_trajectory_ids":["E0145:S012:L14","E0146:S012:L15"] if eid=="E0172" else [f"{eid}:S1:L1"]} for eid in ["E0046","E0124","E0160","E0172"]],"reverse_preimages":[{"trajectory_id":"E0085:S017:L17","predecessor_eid":"E0042","candidate_preimages":[{"samples":[{"repetition":rep,"allowed_r_after_values":[6,7]} for rep in range(3,11)]}]}],"assignment_scenarios":[{"scenario_id":"A6","repartitioned_regions":regions("A6",6)},{"scenario_id":"A7","repartitioned_regions":regions("A7",7)}],"eligible_length_one_regions":[{"id":f"E0{n}:S0{n}:L{n}","terminal_eid":f"E0{n}","cycle_id":f"S0{n}","sequence_line":n,"scenario_ids":["A6","A7"],"formula_fitted":False} for n in (50,133,145,146,160)]}
        new_stable = {"by_input_output":{"authenticationResponse/securityModeCommand":{"formula":"r' = r + 1","formula_kind":"ite","method":"reuse_old_aggregation","old_member_ids":["T1"],"new_member_ids":["T2"],"trajectory_validations":[{"trajectory_id":"T1","eid":"E7","signal":0},{"trajectory_id":"T2","eid":"E7","signal":1}],"signal_evidence":{"0":{"old_sample_count":2,"new_sample_count":0,"matched_sample_count":2,"sample_count":2},"1":{"old_sample_count":0,"new_sample_count":1,"matched_sample_count":1,"sample_count":1}},"validation":{"matched_sample_count":3,"sample_count":3}}}}
        return {"edges":{"E7":edge},"candidate_groups":[{"candidate_id":"B-before-after-a1","logical_input":"authenticationResponse","logical_output":"securityModeCommand","projection":"before_after","formula":"r'=r_before+1","owners":["E7"],"core_owners":["E7"],"compatible_eids":[],"partial_compatible_eids":[]}],"stable_aggregation":stable,"predecessor_repartition":predecessor,"new_stable_inference":new_stable,"trajectories":[{"id":"T1","eid":"E7","cycle_id":"C1","sequence_line":3,"candidate_grade":"relatively_stable_candidate","signal_context":{},"points":[{"repetition":3,"r_before":0,"r_i":7,"r_after":1,"input_source":"direct_observation"},{"repetition":4,"r_before":1,"r_i":7,"r_after":2,"input_source":"direct_observation"}]},{"id":"T2","eid":"E7","cycle_id":"C2","sequence_line":4,"candidate_grade":"relatively_stable_candidate","signal_context":{},"points":[{"repetition":3,"r_before":1,"r_i":6,"r_after":4,"input_source":"propagated"}]}]}

    def test_offline_filters_formula_viewport_and_determinism(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "a.html", Path(temp) / "b.html"
            VIS.render_html(self.sample(), first); VIS.render_html(self.sample(), second)
            content = first.read_text(encoding="utf-8")
            self.assertEqual(first.read_bytes(), second.read_bytes())
        for text in ("plotly.js", "轨迹归类算法 B", "二维公式候选", "稳定性推断聚合", "新稳定推断", "前序最简与重划分", "disabled", "new_stable_inference", "old_member_ids", "new_member_ids", "trajectory_validations", "memberList(item,'old')", "memberList(item,'new')", "['before_after','input_after']", "旧稳定背景", "新稳定轨迹", "复用旧聚合", "同信号联合重聚合", "s=0", "s=1", "function eidLabel", "${id} — ${e.source_state}→${e.target_state}", "window.algorithmBViewer", 'id="candidate-type"', 'id="input-output"', 'id="signal"', 'id="eid"', 'id="projection"', 'id="formula-kind"', 'id="candidate-group"', "r' = r + 1", "GLOBAL_RANGES", "scaleanchor:'y'", "groupclick:'togglegroup'", "colorByDisplay", '"plotly_up_angle":45.0', "stable_aggregation"):
            self.assertIn(text, content)
        filter_ids = ['id="candidate-type"', 'id="input-output"', 'id="signal"', 'id="eid"', 'id="projection"', 'id="formula-kind"', 'id="candidate-group"']
        self.assertEqual([content.index(value) for value in filter_ids], sorted(content.index(value) for value in filter_ids))
        self.assertLess(
            content.index("if(until==='projection')return true;"),
            content.index("if(s.projection&&x.projection!==s.projection)return false;"),
        )
        self.assertEqual(VIS.display_formula_groups(self.sample()), [("before_after", "r' = r + 1")])
        for removed in ("候选与铅垂证据", "完整循环详情", "边摘要", 'id="edge-summary"', 'id="evidence-grade"', 'id="evidence"', 'id="trajectories"'):
            self.assertNotIn(removed, content)
        self.assertNotIn("轨迹聚类算法 B", content)
        application = content.rsplit("<script>", 1)[-1]
        for forbidden in ("soft-DTW", "distance matrix", "silhouette", "merge gap", "cluster_id", "自动簇"):
            self.assertNotIn(forbidden, application)
        for forbidden in ("world", "世界", "W6", "W7"):
            self.assertNotIn(forbidden, application)

    def test_predecessor_repartition_is_a_disabled_audit_only_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "viewer.html"
            VIS.render_html(self.sample(), output)
            content = output.read_text(encoding="utf-8")
        self.assertIn('id="mode-repartition" aria-pressed="false" disabled', content)
        self.assertNotIn('id="assignment-scenario"', content)
        self.assertNotIn("drawRepartition", content)

    def test_generated_application_script_passes_node_syntax_check(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            html = Path(temp) / "viewer.html"
            script = Path(temp) / "viewer.js"
            VIS.render_html(self.sample(), html)
            script.write_text(
                html.read_text(encoding="utf-8").rsplit("<script>", 1)[-1]
                .rsplit("</script>", 1)[0],
                encoding="utf-8",
            )
            checked = subprocess.run(
                ["node", "--check", str(script)], capture_output=True, text=True
            )
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_plotly_up_angles_match_up_referenced_markers(self):
        expected = {
            (0, 1): 0.0, (1, 0): 90.0, (0, -1): 180.0, (-1, 0): -90.0,
            (1, 1): 45.0, (1, -1): 135.0, (-1, -1): -135.0, (-1, 1): -45.0,
        }
        for vector, angle in expected.items():
            self.assertEqual(angle, VIS.plotly_up_angle(*vector))


if __name__ == "__main__":
    unittest.main()
