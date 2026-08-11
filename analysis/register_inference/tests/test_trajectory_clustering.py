from __future__ import annotations

import json
import importlib.util
import sys
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))

from trajectory_clustering import Point, Trajectory, _completed_points, _signal_slice, analyze, cyclic_soft_dtw_distance, distance_matrix, ksi_distance, silhouette, validate_settings

CLI_PATH = MODULE / "experiments" / "cluster_cycle_trajectories.py"
CLI_SPEC = importlib.util.spec_from_file_location("cluster_cycle_trajectories", CLI_PATH)
assert CLI_SPEC and CLI_SPEC.loader
CLI_MODULE = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(CLI_MODULE)


class TrajectoryClusteringTests(unittest.TestCase):
    def test_cyclic_distance_preserves_order_but_accepts_rotation(self) -> None:
        increment = [Point(0, 1, 7, ()), Point(1, 2, 7, ()), Point(2, 3, 7, ())]
        rotated = increment[1:] + increment[:1]
        scrambled = [increment[0], increment[2], increment[1]]
        self.assertAlmostEqual(0.0, cyclic_soft_dtw_distance(increment, rotated), places=8)
        self.assertGreater(cyclic_soft_dtw_distance(increment, scrambled), 0.0)

    def test_cyclic_orbit_distance_is_symmetric_and_input_order_independent(self) -> None:
        first = [Point(0, 2, 1, (("s", 0),)), Point(2, 5, 3, (("s", 1),)), Point(5, 1, 4, (("s", 0),)), Point(1, 0, 6, (("s", 1),))]
        second = [Point(0, 0, 1, (("s", 0),)), Point(1, 1, 3, (("s", 1),)), Point(2, 2, 4, (("s", 0),)), Point(3, 3, 6, (("s", 1),))]
        for offset in range(4):
            self.assertAlmostEqual(0.0, cyclic_soft_dtw_distance(first, first[offset:] + first[:offset]), places=8)
        self.assertAlmostEqual(cyclic_soft_dtw_distance(first, second), cyclic_soft_dtw_distance(second, first), places=8)
        def trajectory(name, points): return Trajectory(name, "stable", {"logical_input": "a", "logical_output": "b"}, "S", 1, None, 1, [], points)
        ordered = [trajectory("a", first), trajectory("b", second), trajectory("c", first[1:] + first[:1])]
        matrix = distance_matrix(ordered, {})
        swapped = distance_matrix([ordered[2], ordered[0], ordered[1]], {})
        self.assertEqual(matrix[0][1], swapped[1][2])
        self.assertEqual(matrix[0][2], swapped[1][0])

    def test_distinguishes_increment_from_reset_and_ksi_domain(self) -> None:
        increment = [Point(0, 1, 7, ()), Point(1, 2, 7, ()), Point(2, 3, 7, ())]
        reset = [Point(0, 0, 7, ()), Point(1, 0, 7, ()), Point(2, 0, 7, ())]
        self.assertGreater(cyclic_soft_dtw_distance(increment, reset), 0.0)
        self.assertAlmostEqual(1 / 3, ksi_distance(6, 0))
        self.assertEqual(1.0, ksi_distance(7, 0))

    def test_signals_never_enter_distance(self) -> None:
        left = [Point(0, 1, 7, (("s", 0),)), Point(1, 2, 7, (("s", 0),))]
        right = [Point(0, 1, 7, (("s", 1),)), Point(1, 2, 7, (("s", 1),))]
        self.assertAlmostEqual(0.0, cyclic_soft_dtw_distance(left, right), places=8)

    def test_singleton_silhouette_is_zero_and_settings_are_validated(self) -> None:
        self.assertEqual(0.0, silhouette([[0.0, 1.0], [1.0, 0.0]], [[0], [1]]))
        for bad in ({"gamma": 0}, {"position_weight": -1}, {"silhouette_threshold": 2}, {"max_clusters": 1}):
            with self.assertRaises(ValueError): validate_settings(bad)

    def test_empty_observation_items_are_rendered_as_empty_set(self) -> None:
        self.assertEqual("∅", CLI_MODULE.ordered_observation_text({"observation_items": []}))
        self.assertEqual("{s=1}[i=7]", CLI_MODULE.ordered_observation_text({"observation_items": [
            {"kind": "signal", "signal_id": "s", "value": 1},
            {"kind": "numeric_input", "input_register_id": "i", "value": 7},
        ]}))

    def test_selection_excludes_minimal_backward_failed_and_unmatched(self) -> None:
        def region(cycle: str, line: int, rep: int = 3) -> dict:
            return {"cycle_id": cycle, "sequence_line": line, "repetition": rep,
                    "previous_output": {"value": 0}, "terminal_output": {"value": 1},
                    "signals": [], "inputs": [{"value": 7}], "input_register_values": {"i": {"value": 7}}, "region_edge_count": 2}
        stable_edge = {"edge": {"edge_id": "E1", "logical_input": "a", "logical_output": "b"}, "candidate_grade": "relatively_stable_candidate", "direct_regions": [region("S1", 1)]}
        good = {"edge": {"edge_id": "E2", "logical_input": "a", "logical_output": "b"}, "candidate_grade": "hypothetical_candidate", "direct_regions": [region("S2", 2)], "candidates": [{"assumptions": ["region_to_edge_decomposition"]}], "relatively_stable_inference_migration": {"cycle_results": [{"cycle_id": "S2", "status": "migration_succeeded"}]}}
        bad_minimal = {**good, "edge": {"edge_id": "E3", "logical_input": "a", "logical_output": "b"}, "direct_regions": [region("S3", 3)], "candidates": [{"assumptions": ["minimal_predecessor_default"]}]}
        bad_failed = {**good, "edge": {"edge_id": "E4", "logical_input": "a", "logical_output": "b"}, "direct_regions": [region("S4", 4)], "relatively_stable_inference_migration": {"cycle_results": [{"cycle_id": "S4", "status": "migration_failed"}]}}
        bad_io = {**good, "edge": {"edge_id": "E5", "logical_input": "x", "logical_output": "y"}, "direct_regions": [region("S5", 5)]}
        bad_backward = {**good, "edge": {"edge_id": "E6", "logical_input": "a", "logical_output": "b"}, "direct_regions": [region("S6", 6)], "backward_inference": {"attempts": []}}
        payload = {"results": [stable_edge, good, bad_minimal, bad_failed, bad_io, bad_backward], "relatively_stable_inference": {"groups": [{"group_index": 1, "logical_input": "a", "logical_output": "b", "source_edge_ids": ["E1"]}]}}
        result = analyze(payload)
        self.assertEqual(2, result["counts"]["extracted_total"])
        self.assertEqual(2, result["counts"]["period_completion_failed"])
        self.assertEqual(1, result["counts"]["migration_succeeded_not_clustered"])
        self.assertEqual({"minimal_predecessor_default", "migration_failed", "unmatched_io", "backward_inference"}, {item["reason"] for item in result["excluded"]})

    def test_h14_counts_when_fixture_is_available(self) -> None:
        candidate = Path(r"D:\state-learning-lab\projects\state-learning-experiments\experiments\open5gs\ueransim-smc-context-pdu-selection\h14-base-runtime-20260804\analysis\register-inference\candidates.json")
        if not candidate.exists(): self.skipTest("H14 candidates fixture is unavailable")
        result = analyze(json.loads(candidate.read_text(encoding="utf-8")))
        self.assertEqual(86, result["counts"]["extracted_total"])
        self.assertEqual(38, result["counts"]["low_discriminability"])
        self.assertEqual(48, result["counts"]["actually_clustered"])
        self.assertEqual(32, result["counts"]["stable_clustered"])
        self.assertEqual(16, result["counts"]["hypothetical_clustered"])
        self.assertEqual(6, result["counts"]["migration_succeeded_total"])
        self.assertEqual(4, result["counts"]["migration_succeeded_clustered"])
        self.assertEqual(2, result["counts"]["migration_succeeded_not_clustered"])
        self.assertEqual(86, len(result["trajectories"]))
        imputed = sum(any(point["same_phase_imputed"] for point in item["analysis_points"])
                      for item in result["trajectories"])
        self.assertEqual(4, imputed)
        self.assertEqual(82, len(result["trajectories"]) - imputed)
        stable_registration = next(analysis for analysis in result["tiers"]["stable_internal"]
                                   if analysis["name"] == "registrationRequest/authenticationRequest/0")
        self.assertEqual(1, stable_registration["selected_k"])
        self.assertTrue(all("views" not in analysis for analyses in result["tiers"].values() for analysis in analyses))
        self.assertEqual("register-trajectory-clustering-v2", result["schema"])

    def test_strict_same_phase_completion_and_source_markers(self) -> None:
        def sample(rep, before, after, value):
            return {"repetition": rep, "previous_output": {"value": before}, "terminal_output": {"value": after}, "inputs": [] if value is None else [{"value": value}], "signals": []}
        samples = [sample(rep, rep % 7, (rep + 1) % 7, rep) for rep in range(3, 10)] + [sample(10, 3 % 7, 4 % 7, 3)]
        samples[0]["inputs"] = []
        points, markers, status = _completed_points(samples)
        self.assertEqual("eligible", status); self.assertEqual(14, len(points)); self.assertEqual(14, len(markers))
        self.assertEqual("same_phase_imputed", markers[0]["source"]); self.assertEqual("observed", markers[7]["source"])
        self.assertTrue(all(marker["pattern_completed"] for marker in markers[8:]))
        samples[-1]["terminal_output"] = {"value": 6}
        self.assertEqual("period_completion_failed", _completed_points(samples)[2])

    def test_signal_slice_is_independent_and_mixed_is_excluded(self) -> None:
        samples = [{"signals": [{"signal_id": "isInitMsg", "value": 0}]}, {"signals": [{"signal_id": "isInitMsg", "value": 1}]}]
        self.assertEqual("mixed", _signal_slice(samples, "isInitMsg"))
        self.assertEqual("not_applicable", _signal_slice([{"signals": []}], "isInitMsg"))


if __name__ == "__main__":
    unittest.main()
