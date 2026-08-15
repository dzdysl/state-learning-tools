import json
from collections import Counter
import unittest
from pathlib import Path

import yaml

from analysis.register_inference.trajectory_formula_discovery import (
    AXES,
    _evaluate_update_tree,
    _repartition_assignment_scenario,
    aggregate_new_stable_inference,
    aggregate_stable_inference,
    analyze,
    combine_signal_branch_trees,
    discover_projection,
    preimage_values,
    update_tree_text,
)
from analysis.register_inference.experiments.visualize_trajectory_formula_candidates import (
    display_formula_groups,
)


H14 = Path(
    r"D:\state-learning-lab\projects\state-learning-experiments\experiments\open5gs"
    r"\ueransim-smc-context-pdu-selection\h14-base-runtime-20260804"
)


def trajectory(identifier, coordinates, projection="before_after"):
    points = []
    for repetition, (x, y) in enumerate(coordinates, 3):
        point = {"repetition": repetition, "r_after": y, "r_i": 7, "r_before": 0}
        point[AXES[projection].x_field] = x
        points.append(point)
    return {"id": identifier, "points": points}


def stable_trajectory(identifier, triples, *, eid="E1", signal=None):
    return {
        "id": identifier,
        "eid": eid,
        "logical_input": "input",
        "logical_output": "output",
        "signal_context": {} if signal is None else signal,
        "points": [
            {
                "repetition": index + 3,
                "r_before": before,
                "r_i": input_value,
                "r_after": after,
            }
            for index, (before, input_value, after) in enumerate(triples)
        ],
    }


def stable_source(*edge_ids):
    return {
        "relatively_stable_inference": {
            "groups": [
                {
                    "group_index": 0,
                    "logical_input": "input",
                    "logical_output": "output",
                    "signal_context": [],
                    "source_edge_ids": list(edge_ids),
                }
            ]
        }
    }


def repartition_event(eid, logical_input, logical_output, position):
    return {
        "edge": {
            "edge_id": eid,
            "source_state": f"s{position}",
            "target_state": f"s{position + 1}",
            "logical_input": logical_input,
            "logical_output": logical_output,
        },
        "event_position": position,
        "input_register_values": {},
    }


class ExactFormulaDiscoveryUnitTest(unittest.TestCase):
    def test_single_point_is_degenerate_and_has_no_constant(self):
        item = trajectory("one", [(7, 0)] * 8, "input_after")
        result = discover_projection([item], AXES["input_after"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["no_formula_reason"], "degenerate_only")
        self.assertEqual(result["trajectory_evidence"][0]["kind"], "static_point")

    def test_separate_static_points_do_not_form_a_horizontal_constant(self):
        result = discover_projection(
            [
                trajectory("left", [(0, 4)] * 8),
                trajectory("right", [(3, 4)] * 8),
            ],
            AXES["before_after"],
        )
        self.assertEqual(result["candidates"], [])

    def test_real_horizontal_trajectory_forms_constant(self):
        horizontal = trajectory(
            "horizontal",
            [(0, 4), (1, 4), (2, 4), (2, 4), (1, 4), (0, 4), (1, 4), (2, 4)],
        )
        candidate = discover_projection([horizontal], AXES["before_after"])["candidates"][0]
        self.assertEqual(candidate["formula"], "r' = 4")
        self.assertEqual(candidate["support_level"], "core")
        self.assertEqual(candidate["fitting_trajectory_ids"], ["horizontal"])

    def test_distinct_horizontal_trajectories_form_distinct_constants(self):
        first = trajectory("zero", [(0, 0), (1, 0), (2, 0), (1, 0)] * 2)
        second = trajectory("five", [(0, 5), (1, 5), (2, 5), (1, 5)] * 2)
        formulas = [
            item["formula"]
            for item in discover_projection([first, second], AXES["before_after"])["candidates"]
        ]
        self.assertEqual(formulas, ["r' = 0", "r' = 5"])

    def test_pure_vertical_trajectory_is_structural_not_a_formula(self):
        vertical = trajectory(
            "vertical",
            [(7, 0), (7, 1), (7, 2), (7, 3), (7, 2), (7, 1), (7, 0), (7, 1)],
            "input_after",
        )
        result = discover_projection([vertical], AXES["input_after"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["trajectory_evidence"][0]["kind"], "pure_vertical")
        self.assertEqual(result["vertical_components"][0]["strength"], "core")

    def test_degenerate_points_are_compatible_or_unresolved_not_fit_support(self):
        dynamic = trajectory("dynamic", [(0, 0), (1, 1), (2, 2), (1, 1)] * 2)
        compatible = trajectory("compatible", [(1, 1)] * 8)
        unresolved = trajectory("unresolved", [(1, 6)] * 8)
        candidate = discover_projection(
            [dynamic, compatible, unresolved], AXES["before_after"]
        )["candidates"][0]
        self.assertEqual(candidate["formula"], "r' = r")
        self.assertEqual(candidate["fitting_trajectory_ids"], ["dynamic"])
        self.assertEqual(
            [item["trajectory_id"] for item in candidate["compatible_degenerate_trajectories"]],
            ["compatible"],
        )
        self.assertEqual(
            [item["trajectory_id"] for item in candidate["unresolved_degenerate_points"]],
            ["unresolved"],
        )

    def test_affine_requires_three_distinct_x_and_records_gap(self):
        too_short = trajectory("two", [(0, 1), (1, 2)] * 4, "input_after")
        formulas = [item["formula"] for item in discover_projection([too_short], AXES["input_after"])["candidates"]]
        self.assertNotIn("r' = r_i + 1", formulas)
        with_gap = trajectory("gap", [(0, 1), (1, 2), (3, 4), (3, 4), (1, 2), (0, 1), (1, 2), (3, 4)], "input_after")
        candidate = discover_projection([with_gap], AXES["input_after"])["candidates"][0]
        self.assertEqual(candidate["formula"], "r' = r_i + 1")
        self.assertEqual(candidate["missing_x"], [2])
        self.assertEqual(candidate["evidence_grade"], "observationally_exact_with_gaps")

    def test_split_uses_canonical_threshold_and_records_equivalent_interval(self):
        coordinates = [(0, 1), (1, 2), (2, 3), (5, 0), (6, 0), (0, 1), (5, 0), (6, 0)]
        result = discover_projection(
            [trajectory("split-gap", coordinates, "input_after")],
            AXES["input_after"],
        )
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["formula"], "r' = ite(r_i < 5, r_i + 1, 0)")
        self.assertEqual(candidate["equivalent_threshold_interval"], [3, 5])
        self.assertEqual(candidate["missing_x"], [3, 4])
        self.assertEqual(candidate["evidence_grade"], "observationally_exact_with_gaps")

    def test_directed_segment_votes_are_deduplicated_per_eid(self):
        coordinates = [(0, 0), (1, 1), (2, 2), (1, 1), (0, 0), (1, 1), (2, 2), (1, 1)]
        result = discover_projection(
            [trajectory("a", coordinates), trajectory("b", coordinates)],
            AXES["before_after"],
        )
        self.assertEqual(len(result["directed_segments"]), 4)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["direction"]["forward"], 2)
        self.assertEqual(candidate["direction"]["reverse"], 2)
        self.assertEqual(candidate["direction"]["majority"], "mixed")
        self.assertTrue(all(segment["support_count"] > 1 for segment in result["directed_segments"]))

    def test_self_loops_are_excluded_from_vertical_and_direction_votes(self):
        coordinates = [(0, 0), (0, 0), (1, 1), (2, 2), (2, 2), (1, 1), (0, 0), (0, 0)]
        result = discover_projection([trajectory("loops", coordinates)], AXES["before_after"])
        direction = result["candidates"][0]["direction"]
        self.assertEqual(direction["self_loops_excluded"], 2)
        self.assertEqual(direction["vertical"], 0)
        self.assertEqual(
            [item["category"] for item in direction["segments"]].count("self_loop_excluded"),
            2,
        )


class StableAggregationUnitTest(unittest.TestCase):
    def aggregate(self, trajectories, *edge_ids):
        return aggregate_stable_inference(
            stable_source(*(edge_ids or ("E1",))),
            trajectories,
            [("input", "output")],
        )["by_input_output"]["input/output"]

    def test_single_vertical_projection_is_excluded(self):
        triples = [(value, 7, value) for value in range(7)]
        result = self.aggregate([stable_trajectory("T", triples)])
        self.assertEqual(result["projections"]["input_after"]["classification"], "pure_vertical")
        self.assertEqual(result["final_candidates"][0]["formula"], "r' = r")

    def test_two_full_simple_projections_are_equal_candidates(self):
        triples = [(value, value, value) for value in range(4)]
        result = self.aggregate([stable_trajectory("T", triples)])
        self.assertEqual(result["selection_tier"], "simple_projection")
        self.assertEqual(
            [candidate["formula"] for candidate in result["final_candidates"]],
            ["r' = r_i", "r' = r"],
        )

    def test_unique_input_vertical_value_builds_cross_projection_tree(self):
        triples = (
            [(0, value, value + 1) for value in range(6)]
            + [(0, 6, 0)]
            + [(value, 7, value + 1) for value in range(6)]
            + [(6, 7, 0)]
        )
        result = self.aggregate([stable_trajectory("T", triples)])
        self.assertEqual(result["selection_tier"], "cross_projection_guard")
        candidate = result["final_candidates"][0]
        self.assertEqual(
            candidate["formula"],
            "r' = ite(r_i = 7, ite(r < 6, r + 1, 0), ite(r_i < 6, r_i + 1, 0))",
        )
        self.assertTrue(candidate["verification"]["exact"])
        self.assertEqual(candidate["verification"]["root_branch_counts"], {"false": 7, "true": 7})

    def test_multiple_unresolved_input_values_do_not_build_cross_tree(self):
        triples = (
            [(0, value, value + 1) for value in range(6)]
            + [(value, 6, value + 1) for value in range(1, 4)]
            + [(value, 7, value + 1) for value in range(4, 6)]
            + [(6, 7, 0)]
        )
        result = self.aggregate([stable_trajectory("T", triples)])
        self.assertEqual(result["status"], "no_final_candidate")
        self.assertEqual(result["final_candidates"], [])

    def test_backward_preimage_uses_the_full_value_domain(self):
        tree = {
            "kind": "threshold_guard",
            "guard": {"variable": "r", "operator": "<", "threshold": 6},
            "true": {"kind": "leaf", "formula": {"kind": "r_plus", "value": 1}},
            "false": {"kind": "leaf", "formula": {"kind": "constant", "value": 0}},
        }
        point = {"r_before": 0, "r_i": 7, "r_after": 0}
        self.assertEqual(preimage_values(tree, point, range(8)), [6, 7])

    def test_inconsistent_signal_context_is_structured(self):
        triples = [(value, 7, value) for value in range(4)]
        result = self.aggregate(
            [
                stable_trajectory("A", triples, eid="E1", signal={"isInitMsg": 0}),
                stable_trajectory("B", triples, eid="E2", signal={"isInitMsg": 1}),
            ],
            "E1",
            "E2",
        )
        self.assertEqual(result["status"], "inconsistent_signal_context")
        self.assertEqual(result["final_candidates"], [])

    def test_identical_signal_branches_simplify_without_losing_signal_evidence(self):
        tree = {"kind": "leaf", "formula": {"kind": "r_plus", "value": 0}}
        combined, simplified = combine_signal_branch_trees({0: tree, 1: tree})
        self.assertIs(combined, tree)
        self.assertTrue(simplified)
        self.assertNotEqual(combined["kind"], "signal_guard")

    def test_different_signal_branches_create_a_real_evaluable_guard(self):
        zero = {"kind": "leaf", "formula": {"kind": "constant", "value": 0}}
        one = {"kind": "leaf", "formula": {"kind": "constant", "value": 1}}
        combined, simplified = combine_signal_branch_trees({0: zero, 1: one})
        self.assertFalse(simplified)
        self.assertEqual(combined["kind"], "signal_guard")
        self.assertEqual(_evaluate_update_tree(combined, {"signal_context": {"isInitMsg": 0}}), 0)
        self.assertEqual(_evaluate_update_tree(combined, {"signal_context": {"isInitMsg": 1}}), 1)
        self.assertEqual(update_tree_text(combined), "r' = ite(s = 1, 1, 0)")


class NewStableInferenceUnitTest(unittest.TestCase):
    @staticmethod
    def item(identifier, values, *, signal, eid):
        item = stable_trajectory(
            identifier,
            [(value, 7, value) for value in values],
            eid=eid,
            signal={"isInitMsg": signal},
        )
        item["edge"] = {
            "edge_id": eid,
            "source_state": "s1",
            "target_state": "s2",
            "logical_input": "input",
            "logical_output": "output",
        }
        return item

    def stable_result(self, old):
        return aggregate_stable_inference(
            {
                "relatively_stable_inference": {
                    "groups": [
                        {
                            "group_index": 0,
                            "logical_input": "input",
                            "logical_output": "output",
                            "signal_context": [{"signal_id": "isInitMsg", "value": 0}],
                            "source_edge_ids": [old["eid"]],
                        }
                    ]
                }
            },
            [old],
            [("input", "output")],
        )

    @staticmethod
    def predecessor(identifier):
        return {"eligible_length_one_regions": [{"id": identifier}]}

    def test_direction_mismatch_falls_back_to_same_signal_joint_reaggregation(self):
        old = self.item("E1:C:L1", [3, 2, 1, 0, 3, 2, 1, 0], signal=0, eid="E1")
        new = self.item("E2:C:L2", [0, 1, 2, 3, 0, 1, 2, 3], signal=1, eid="E2")
        result = aggregate_new_stable_inference(
            [old, new], self.stable_result(old), self.predecessor(new["id"])
        )["by_input_output"]["input/output"]
        self.assertFalse(result["trajectory_validations"][0]["direction_consistent"])
        self.assertEqual(result["method"], "same_signal_joint_reaggregation")
        self.assertEqual(result["status"], "inferred")
        self.assertEqual(result["final_candidates"][0]["formula"], "r' = r")
        self.assertTrue(result["final_candidates"][0]["identical_signal_branches_simplified"])

    def test_unfit_degenerate_signal_branch_returns_structured_failure(self):
        old = self.item("E1:C:L1", [0, 1, 2, 3, 0, 1, 2, 3], signal=0, eid="E1")
        new = self.item("E2:C:L2", [7] * 8, signal=1, eid="E2")
        result = aggregate_new_stable_inference(
            [old, new], self.stable_result(old), self.predecessor(new["id"])
        )["by_input_output"]["input/output"]
        self.assertEqual(result["method"], "same_signal_joint_reaggregation")
        self.assertEqual(result["status"], "no_exact_signal_branch_candidate")
        self.assertEqual(result["final_candidates"], [])


@unittest.skipUnless(H14.exists(), "requires frozen H14 record")
class H14FormulaDiscoveryIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.candidates = json.loads((H14 / "analysis/register-inference/candidates.json").read_text(encoding="utf-8"))
        cls.trace = [json.loads(line) for line in (H14 / "evidence/statelearner_trace.jsonl").read_text(encoding="utf-8").splitlines()]
        cls.cycle_cover = json.loads((H14.parent / "h14-complete-teardown-20260801/analysis/cycle-cover/base-result.json").read_text(encoding="utf-8"))
        cls.config = yaml.safe_load((H14 / "analysis/register-inference/config.yaml").read_text(encoding="utf-8"))
        cls.result = analyze(cls.candidates, cls.trace, cls.cycle_cover, cls.config)

    def test_exact_h14_scope_and_real_samples(self):
        self.assertEqual(self.result["schema"], "register-trajectory-formula-discovery-v5")
        self.assertEqual(self.result["counts"]["eid_count"], 29)
        self.assertEqual(self.result["counts"]["trajectory_count"], 87)
        self.assertEqual(self.result["counts"]["sample_count"], 696)
        self.assertEqual(self.result["counts"]["candidate_group_count"], 6)
        self.assertEqual(
            {
                key: (value["eid_count"], value["trajectory_count"], value["sample_count"], value["candidate_group_count"])
                for key, value in self.result["counts"]["by_input_output"].items()
            },
            {
                "authenticationResponse/securityModeCommand": (4, 39, 312, 2),
                "registrationRequest/authenticationRequest": (15, 32, 256, 2),
                "registrationRequestGUTI/authenticationRequest": (10, 16, 128, 2),
            },
        )
        self.assertTrue(all([point["repetition"] for point in item["points"]] == list(range(3, 11)) for item in self.result["trajectories"]))
        self.assertTrue(all("signal_context" in item for item in self.result["trajectories"]))

    def test_selected_input_output_subset_is_supported(self):
        result = analyze(
            self.candidates,
            self.trace,
            self.cycle_cover,
            self.config,
            [("authenticationResponse", "securityModeCommand")],
        )
        self.assertEqual(result["counts"]["input_output_count"], 1)
        self.assertEqual(result["counts"]["eid_count"], 4)
        self.assertEqual(result["counts"]["trajectory_count"], 39)

    def test_h14_degenerate_projection_inventory(self):
        kinds = Counter(
            item["kind"]
            for edge in self.result["edges"].values()
            for projection in edge["projections"].values()
            for item in projection["trajectory_evidence"]
        )
        self.assertEqual(
            kinds,
            Counter({"static_point": 79, "dynamic": 68, "pure_vertical": 26, "horizontal": 1}),
        )
        self.assertEqual(
            sum(
                not projection["candidates"]
                for edge in self.result["edges"].values()
                for projection in edge["projections"].values()
            ),
            30,
        )

    def test_expected_projection_candidates_and_vertical_overlap(self):
        for eid in ("E0019", "E0127", "E0163"):
            edge = self.result["edges"][eid]
            self.assertEqual([x["formula"] for x in edge["projections"]["before_after"]["candidates"]], ["r' = r"])
            ia = edge["projections"]["input_after"]
            self.assertEqual([x["formula"] for x in ia["candidates"]], ["r' = ite(r_i < 6, r_i + 1, 0)"])
            self.assertEqual(ia["candidates"][0]["scope"], "functional_subset")
            self.assertNotIn([7, 0], ia["candidates"][0]["support_points"])
            self.assertTrue(ia["candidates"][0]["compatible_degenerate_trajectories"])
            self.assertEqual(ia["vertical_components"][0]["x"], 7)
            self.assertEqual(ia["vertical_components"][0]["strength"], "core")
        for projection in ("before_after", "input_after"):
            value = self.result["edges"]["E0103"]["projections"][projection]
            self.assertEqual(value["candidates"], [])
            self.assertEqual(value["no_formula_reason"], "degenerate_only")

    def test_candidate_groups_are_overlapping_reverse_indexes(self):
        groups = {
            (g["logical_input"], g["logical_output"], g["projection"], g["formula"]): g
            for g in self.result["candidate_groups"]
        }
        ba = groups[("authenticationResponse", "securityModeCommand", "before_after", "r' = r")]
        self.assertEqual(ba["core_owners"], ["E0019", "E0127", "E0163"])
        self.assertEqual(ba["compatible_eids"], ["E0103"])
        ia = groups[("authenticationResponse", "securityModeCommand", "input_after", "r' = ite(r_i < 6, r_i + 1, 0)")]
        self.assertEqual(ia["core_owners"], ["E0019", "E0127", "E0163"])
        self.assertEqual(ia["partial_compatible_eids"], ["E0103"])

        reg_constant = groups[("registrationRequest", "authenticationRequest", "before_after", "r' = 0")]
        self.assertEqual(reg_constant["owners"], ["E0085"])
        self.assertEqual(reg_constant["compatible_eids"], ["E0001", "E0181"])
        reg_split = groups[("registrationRequest", "authenticationRequest", "before_after", "r' = ite(r < 6, r + 1, 0)")]
        self.assertEqual(reg_split["owners"], ["E0013", "E0037", "E0073", "E0121", "E0133", "E0145", "E0157", "E0169", "E0205"])

        guti_before = groups[("registrationRequestGUTI", "authenticationRequest", "before_after", "r' = ite(r < 6, r + 1, 0)")]
        self.assertEqual(guti_before["owners"], ["E0014", "E0038", "E0050", "E0146", "E0170", "E0206"])
        guti_input = groups[("registrationRequestGUTI", "authenticationRequest", "input_after", "r' = ite(r_i < 6, r_i + 1, 0)")]
        self.assertEqual(guti_input["owners"], ["E0038", "E0050", "E0098", "E0146", "E0170", "E0206"])

    def test_input_output_scoped_candidate_ids_do_not_collide(self):
        groups = [
            group for group in self.result["candidate_groups"]
            if group["formula"] == "r' = ite(r < 6, r + 1, 0)"
        ]
        self.assertEqual(len(groups), 2)
        self.assertEqual(len({group["candidate_id"] for group in groups}), 2)
        self.assertEqual(
            display_formula_groups(self.result),
            [
                ("before_after", "r' = 0"),
                ("before_after", "r' = ite(r < 6, r + 1, 0)"),
                ("before_after", "r' = r"),
                ("input_after", "r' = ite(r_i < 6, r_i + 1, 0)"),
            ],
        )

    def test_four_r3_carries_are_reconstructed_without_completion(self):
        trajectories = {item["id"]: item for item in self.result["trajectories"]}
        expected = {"E0019:S036:L22": 3, "E0019:S036:L24": 3, "E0019:S037:L26": 1, "E0019:S037:L28": 1}
        for identifier, value in expected.items():
            self.assertEqual(trajectories[identifier]["points"][0]["r_i"], value)
            self.assertEqual(trajectories[identifier]["points"][0]["input_source"], "carried_from_R2")
        serialized = json.dumps(self.result).lower()
        for forbidden in ("soft-dtw", "distance_matrix", "silhouette", "merge gap", "cluster_id", "pattern_completed"):
            self.assertNotIn(forbidden, serialized)

    def test_h14_relatively_stable_aggregation_is_exact(self):
        aggregation = self.result["stable_aggregation"]
        self.assertEqual(
            aggregation["counts"],
            {
                "input_output_count": 3,
                "eid_count": 20,
                "trajectory_count": 57,
                "sample_count": 456,
                "final_candidate_count": 3,
            },
        )
        by_io = aggregation["by_input_output"]
        expected = {
            "authenticationResponse/securityModeCommand": (
                39,
                312,
                "not_applicable",
                "simple_projection",
                "r' = r",
            ),
            "registrationRequest/authenticationRequest": (
                9,
                72,
                "observed",
                "simple_projection",
                "r' = ite(r < 6, r + 1, 0)",
            ),
            "registrationRequestGUTI/authenticationRequest": (
                9,
                72,
                "observed",
                "cross_projection_guard",
                "r' = ite(r_i = 7, ite(r < 6, r + 1, 0), ite(r_i < 6, r_i + 1, 0))",
            ),
        }
        for io, (trajectory_count, sample_count, signal_status, tier, formula) in expected.items():
            item = by_io[io]
            self.assertEqual(item["status"], "inferred")
            self.assertEqual((item["trajectory_count"], item["sample_count"]), (trajectory_count, sample_count))
            self.assertEqual(item["signal_condition"]["status"], signal_status)
            self.assertEqual(item["selection_tier"], tier)
            self.assertEqual([candidate["formula"] for candidate in item["final_candidates"]], [formula])
            verification = item["final_candidates"][0]["verification"]
            self.assertEqual(verification["matched_sample_count"], sample_count)
            self.assertTrue(verification["exact"])
            self.assertEqual(verification["failures"], [])
        self.assertEqual(
            by_io["registrationRequest/authenticationRequest"]["signal_condition"]["values"],
            {"isInitMsg": 0},
        )
        guti = by_io["registrationRequestGUTI/authenticationRequest"]
        self.assertEqual(guti["signal_condition"]["values"], {"isInitMsg": 0})
        self.assertEqual(
            guti["final_candidates"][0]["verification"]["root_branch_counts"],
            {"false": 64, "true": 8},
        )
        self.assertNotEqual(guti["final_candidates"][0]["update_tree"]["kind"], "signal_guard")

    def test_h14_predecessor_repartition_matches_the_frozen_expectation(self):
        result = self.result["predecessor_repartition"]
        self.assertEqual(
            result["counts"],
            {
                "dynamic_length_two_trajectory_count": 6,
                "hypothetical_trajectory_count": 30,
                "stable_match_count": 5,
                "reverse_preimage_count": 1,
                "hold_edge_count": 4,
                "assignment_scenario_count": 2,
                "eligible_length_one_count": 5,
                "input_old_migration_statuses": {
                    "migration_failed": 1,
                    "migration_succeeded": 6,
                    "no_matching_relatively_stable_inference": 23,
                },
                "selected_old_migration_statuses": {
                    "migration_succeeded": 1,
                    "no_matching_relatively_stable_inference": 5,
                },
            },
        )
        self.assertEqual(
            [item["eid"] for item in result["hold_inferences"]],
            ["E0046", "E0124", "E0160", "E0172"],
        )
        e0172 = next(item for item in result["hold_inferences"] if item["eid"] == "E0172")
        self.assertEqual(
            e0172["support_trajectory_ids"],
            ["E0145:S012:L14", "E0146:S012:L15"],
        )
        reverse = result["reverse_preimages"][0]
        self.assertEqual(reverse["trajectory_id"], "E0085:S017:L17")
        self.assertTrue(reverse["does_not_infer_edge_formula"])
        candidate = reverse["candidate_preimages"][0]
        self.assertEqual(candidate["consistent_cycle_values"], [6, 7])
        self.assertTrue(
            all(sample["allowed_r_after_values"] == [6, 7] for sample in candidate["samples"])
        )
        self.assertEqual(
            [
                (scenario["scenario_id"], scenario["selections"][0]["value"])
                for scenario in result["assignment_scenarios"]
            ],
            [("A6", 6), ("A7", 7)],
        )
        self.assertEqual(
            [item["id"] for item in result["eligible_length_one_regions"]],
            [
                "E0050:S009:L12",
                "E0133:S003:L3",
                "E0145:S005:L6",
                "E0145:S012:L14",
                "E0146:S012:L15",
            ],
        )
        self.assertTrue(all(not item["formula_fitted"] for item in result["eligible_length_one_regions"]))
        self.assertEqual(
            {
                item["old_migration_status"]
                for item in result["hypothetical_trajectory_inventory"]
            },
            {
                "migration_succeeded",
                "migration_failed",
                "no_matching_relatively_stable_inference",
            },
        )
        failed = next(
            item for item in result["hypothetical_trajectory_inventory"]
            if item["old_migration_status"] == "migration_failed"
        )
        self.assertEqual(failed["trajectory_id"], "E0073:S008:L10")
        self.assertEqual(failed["exclusion_reason"], "region_length_not_two")
        for scenario in result["assignment_scenarios"]:
            e0085 = next(
                item for item in scenario["repartitioned_regions"]
                if item["original_trajectory_id"] == "E0085:S017:L17"
                and item["boundary_kind"] == "real_downlink"
            )
            self.assertTrue(e0085["newly_length_one"])
            self.assertFalse(e0085["dynamic_triples"])
            self.assertFalse(e0085["next_stage_stable_inference_eligible"])
            self.assertEqual(e0085["exclusion_reason"], "static_triples")

    def test_h14_new_stable_inference_reuses_and_simplifies_old_formulas(self):
        result = self.result["new_stable_inference"]
        self.assertEqual(
            result["counts"],
            {
                "input_output_count": 2,
                "old_trajectory_count": 18,
                "new_trajectory_count": 5,
                "old_sample_count": 144,
                "new_sample_count": 40,
                "final_candidate_count": 2,
            },
        )
        expected = {
            "registrationRequest/authenticationRequest": (
                96,
                "r' = ite(r < 6, r + 1, 0)",
                ["E0133:S003:L3", "E0145:S005:L6", "E0145:S012:L14"],
            ),
            "registrationRequestGUTI/authenticationRequest": (
                88,
                "r' = ite(r_i = 7, ite(r < 6, r + 1, 0), ite(r_i < 6, r_i + 1, 0))",
                ["E0050:S009:L12", "E0146:S012:L15"],
            ),
        }
        for io, (sample_count, formula, new_ids) in expected.items():
            item = result["by_input_output"][io]
            self.assertEqual(item["status"], "inferred")
            self.assertEqual(item["method"], "reused_old_aggregation")
            self.assertEqual(item["new_member_ids"], new_ids)
            self.assertTrue(all(x["reuse_eligible"] for x in item["trajectory_validations"]))
            candidate = item["final_candidates"][0]
            self.assertEqual(candidate["formula"], formula)
            self.assertTrue(candidate["identical_signal_branches_simplified"])
            self.assertNotEqual(candidate["update_tree"]["kind"], "signal_guard")
            self.assertEqual(candidate["verification"]["matched_sample_count"], sample_count)
            self.assertEqual(candidate["verification"]["sample_count"], sample_count)
            self.assertEqual(set(item["signal_evidence"]), {"0", "1"})

    def test_hold_only_extends_a_continuous_preceding_real_downlink(self):
        terminal = repartition_event(
            "T", "registrationRequest", "authenticationRequest", 3
        )
        hold = repartition_event("H", "deregistrationRequest", "null_action", 2)
        interruption = repartition_event(
            "X", "securityModeReject", "null_action", 1
        )
        trajectory = {
            "id": "T:C:L1",
            "eid": "T",
            "candidate_grade": "hypothetical_candidate",
            "cycle_id": "C",
            "sequence_line": 1,
            "points": [{"repetition": 3, "r_before": 0, "r_i": 7, "r_after": 1}],
        }

        def run(events):
            regions = {
                trajectory["id"]: {
                    3: {
                        "previous_output": {"value": 0},
                        "terminal_output": {"value": 1},
                        "region_edges": events,
                    }
                }
            }
            return _repartition_assignment_scenario(
                [trajectory], regions, {"H"}, {}, "A"
            )["repartitioned_regions"]

        continuous = run([hold, terminal])
        self.assertEqual(
            ["pseudo_hold", "real_downlink"],
            [item["boundary_kind"] for item in continuous],
        )
        self.assertEqual(["T"], continuous[-1]["region_edge_ids"])

        interrupted = run([interruption, hold, terminal])
        self.assertEqual(["real_downlink"], [item["boundary_kind"] for item in interrupted])
        self.assertEqual(["X", "H", "T"], interrupted[0]["region_edge_ids"])
        self.assertFalse(interrupted[0]["newly_length_one"])


if __name__ == "__main__":
    unittest.main()
