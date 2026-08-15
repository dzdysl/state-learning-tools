import json
from pathlib import Path
import unittest

import yaml

from analysis.register_inference.full_model_stable_migration import (
    analyze_full_model_stable_migration,
)
from analysis.register_inference.experiments.report_full_model_stable_migration import (
    render_report,
)
from analysis.register_inference.trajectory_formula_discovery import load_jsonl


H14 = Path(
    r"D:\state-learning-lab\projects\state-learning-experiments\experiments\open5gs"
    r"\ueransim-smc-context-pdu-selection\h14-base-runtime-20260804"
)


class FullModelStableMigrationIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candidates = json.loads(
            (H14 / "analysis/register-inference/candidates.json").read_text(encoding="utf-8")
        )
        cls.formulas = json.loads(
            (H14 / "analysis/register-inference/trajectory-formula-candidates.json").read_text(
                encoding="utf-8"
            )
        )
        cls.trace = load_jsonl(H14 / "evidence/statelearner_trace.jsonl")
        cls.config = yaml.safe_load(
            (H14 / "analysis/register-inference/config.yaml").read_text(encoding="utf-8")
        )
        cls.result = analyze_full_model_stable_migration(
            cls.candidates, cls.formulas, cls.trace, cls.config
        )

    def test_full_model_edge_coverage_and_statuses(self):
        by_io = self.result["by_input_output"]
        ordinary = by_io["registrationRequest/authenticationRequest"]
        guti = by_io["registrationRequestGUTI/authenticationRequest"]
        self.assertEqual(ordinary["counts"]["covered_edge_count"], 13)
        self.assertEqual(ordinary["counts"]["edge_count"], 15)
        self.assertEqual(ordinary["migration_eids"], ["E0085", "E0181", "E0193"])
        self.assertEqual(ordinary["temporarily_not_migrated_eids"], ["E0001", "E0073"])
        self.assertEqual(guti["counts"]["covered_edge_count"], 10)
        self.assertEqual(guti["counts"]["edge_count"], 10)
        self.assertEqual(guti["temporarily_not_migrated_eids"], [])

    def test_vertical_x_seven_selects_one_candidate_from_two_preimages(self):
        reverse = self.result["reverse_closure"]
        self.assertEqual(reverse["trajectory_count"], 9)
        self.assertEqual(reverse["vertical_preference"]["x"], 7)
        for trajectory in reverse["trajectories"]:
            self.assertEqual(trajectory["selected_value"], 7)
            self.assertEqual(len(trajectory["mathematical_preimages"]), 8)
            for sample in trajectory["mathematical_preimages"]:
                self.assertEqual(sample["allowed_r_after_values"], [6, 7])
                self.assertEqual(sample["selected_r_after"], 7)
            self.assertTrue(
                all(point["r_after"] == 7 for point in trajectory["predecessor_points"])
            )

    def test_predecessor_inputs_and_formula_ownership(self):
        reverse = self.result["reverse_closure"]
        self.assertEqual(
            sorted(reverse["predecessor_fits"]), ["E0042", "E0114", "E0210"]
        )
        e0042 = reverse["predecessor_fits"]["E0042"]
        for projection in ("before_after", "input_after"):
            self.assertEqual(
                [item["formula"] for item in e0042["projections"][projection]["candidates"]],
                ["r' = 7"],
            )
        for eid in ("E0114", "E0210"):
            for projection in ("before_after", "input_after"):
                self.assertEqual(
                    reverse["predecessor_fits"][eid]["projections"][projection]["candidates"],
                    [],
                )
        for trajectory in reverse["trajectories"]:
            for point in trajectory["predecessor_points"]:
                self.assertEqual(point["r_before_source"], "direct_region_start")
                self.assertEqual(point["r_i_source"], "frozen_trace_replay")

    def test_terminal_reverse_closure_and_e0193_are_exact(self):
        ordinary = self.result["by_input_output"]["registrationRequest/authenticationRequest"]
        by_eid = {item["eid"]: item for item in ordinary["edges"]}
        self.assertEqual(by_eid["E0085"]["reverse_closure"]["matched_sample_count"], 64)
        self.assertEqual(by_eid["E0085"]["reverse_closure"]["sample_count"], 64)
        self.assertEqual(by_eid["E0181"]["reverse_closure"]["matched_sample_count"], 8)
        self.assertEqual(by_eid["E0181"]["reverse_closure"]["sample_count"], 8)
        self.assertEqual(by_eid["E0193"]["direct_validation"]["matched_sample_count"], 16)
        self.assertEqual(by_eid["E0193"]["direct_validation"]["sample_count"], 16)
        self.assertEqual(
            {
                tuple(
                    (point["r_before"], point["r_i"], point["r_after"])
                    for point in trajectory["points"]
                )
                for trajectory in by_eid["E0193"]["trajectories"]
            },
            {((1, 7, 2),) * 8},
        )

    def test_report_is_standalone_and_uses_only_requested_reader_statuses(self):
        fake_provenance = {
            name: {"path": name, "sha256": "0" * 64}
            for name in ("candidates", "trajectory-formulas", "trace", "cycle-cover", "config")
        }
        report = render_report(self.result, fake_provenance)
        for text in (
            "项目结构与证据链",
            "算法 B 到本阶段的工作脉络",
            "9条长度2区域的前序反推",
            "E0042：s3→s7，securityModeReject/null_action",
            "E0114：s9→s7，securityModeReject/null_action",
            "E0210：s17→s15，securityModeReject/null_action",
            "E0001：s0→s1，registrationRequest/authenticationRequest",
            "E0073：s6→s1，registrationRequest/authenticationRequest",
            "覆盖13/15条H14边",
            "覆盖10/10条H14边",
        ):
            self.assertIn(text, report)
        for forbidden in ("兼容迁移", "条件性迁移", "弱证据迁移"):
            self.assertNotIn(forbidden, report)


if __name__ == "__main__":
    unittest.main()
