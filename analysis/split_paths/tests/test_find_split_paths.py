import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "find_split_paths.py"
SPEC = importlib.util.spec_from_file_location("find_split_paths", SCRIPT)
assert SPEC and SPEC.loader
SPLIT_PATHS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SPLIT_PATHS)


def edge(src: str, dst: str, input_symbol: str) -> dict[str, str]:
    return {
        "src": src,
        "dst": dst,
        "input": input_symbol,
        "output": f"out_{input_symbol}",
    }


def model_from_edges(edges: list[dict[str, str]]) -> dict:
    outgoing: dict[str, list[dict[str, str]]] = {}
    incoming: dict[str, list[dict[str, str]]] = {}
    states = set()
    for item in edges:
        states.update((item["src"], item["dst"]))
        outgoing.setdefault(item["src"], []).append(item)
        incoming.setdefault(item["dst"], []).append(item)
    return {
        "states": sorted(states, key=SPLIT_PATHS.state_key),
        "outgoing": outgoing,
        "incoming": incoming,
    }


class SplitPathHeuristicTests(unittest.TestCase):
    def test_excludes_s0_and_finds_deeper_common_prefix(self) -> None:
        model = model_from_edges(
            [
                edge("s0", "s8", "direct_short"),
                edge("s0", "s1", "a"),
                edge("s1", "s3", "b"),
                edge("s3", "s10", "c"),
                edge("s10", "s8", "short_suffix"),
                edge("s10", "s11", "d"),
                edge("s11", "s12", "e"),
                edge("s12", "s15", "f"),
                edge("s15", "s18", "long_suffix"),
            ]
        )

        result = SPLIT_PATHS.analyze_split_paths(model, "s0", "s8", "s18")

        self.assertEqual("s8", result["shorter"]["target"])
        self.assertEqual("s18", result["longer"]["target"])
        self.assertEqual("s10", result["reverse_bfs"]["intersection"])
        self.assertEqual(["a", "b", "c"], result["common_prefix"]["input_sequence"])
        self.assertEqual(
            ["short_suffix"],
            result["distinguishing_suffixes"]["to_shorter"]["input_sequence"],
        )
        self.assertEqual(
            ["d", "e", "f", "long_suffix"],
            result["distinguishing_suffixes"]["to_longer"]["input_sequence"],
        )
        self.assertEqual(
            "selected_maximum_backward_common_suffix",
            result["candidate_search"]["selection"]["status"],
        )

    def test_swaps_roles_when_first_target_is_longer(self) -> None:
        model = model_from_edges(
            [
                edge("s0", "s1", "a"),
                edge("s1", "s2", "b"),
                edge("s0", "s3", "short"),
                edge("s1", "s3", "branch"),
            ]
        )

        result = SPLIT_PATHS.analyze_split_paths(model, "s0", "s2", "s3")

        self.assertTrue(result["roles_swapped"])
        self.assertEqual("s3", result["shorter"]["target"])
        self.assertEqual("s2", result["longer"]["target"])

    def test_same_reverse_layer_chooses_deepest_state_on_r(self) -> None:
        model = model_from_edges(
            [
                edge("s0", "s1", "a"),
                edge("s1", "s2", "b"),
                edge("s2", "s3", "c"),
                edge("s3", "s9", "r_end"),
                edge("s1", "s8", "early_branch"),
                edge("s3", "s8", "deep_branch"),
                edge("s0", "s8", "short"),
            ]
        )

        result = SPLIT_PATHS.analyze_split_paths(model, "s0", "s8", "s9")

        self.assertEqual(["s1", "s3"], result["reverse_bfs"]["layer_candidates"])
        self.assertEqual("s3", result["reverse_bfs"]["intersection"])
        self.assertEqual(["a", "b", "c"], result["common_prefix"]["input_sequence"])

    def test_selects_longest_backward_common_transition_suffix(self) -> None:
        model = model_from_edges(
            [
                edge("s0", "s1", "a"),
                edge("s1", "s2", "x"),
                edge("s2", "s3", "y"),
                edge("s3", "s9", "z"),
                edge("s2", "s7", "q"),
                edge("s7", "s1", "z"),
            ]
        )

        result = SPLIT_PATHS.analyze_split_paths(model, "s0", "s1", "s9")
        search = result["candidate_search"]

        self.assertEqual(2, len(search["eligible_candidates"]))
        self.assertEqual(
            "selected_maximum_backward_common_suffix",
            search["selection"]["status"],
        )
        self.assertEqual("P02", search["selection"]["selected_candidate_id"])
        self.assertEqual(2, result["common_prefix"]["length"])
        self.assertEqual(
            [0, 1],
            [
                search["eligible_candidates"][0]["backward_common_suffix"][
                    "length"
                ],
                search["eligible_candidates"][1]["backward_common_suffix"][
                    "length"
                ],
            ],
        )
        self.assertEqual(
            ["z"],
            search["selected_candidate"]["backward_common_suffix"][
                "input_sequence"
            ],
        )
        self.assertEqual(
            "pmt_common_tail",
            result["final_result"]["mode"],
        )
        pmt = result["final_result"]["pmt"]
        self.assertEqual(
            ["q"], pmt["M_A"]["input_sequence"]
        )
        self.assertEqual(
            ["y"], pmt["M_B"]["input_sequence"]
        )
        self.assertEqual(
            ["z"], pmt["T_A"]["input_sequence"]
        )
        self.assertEqual(["z"], pmt["T_B"]["input_sequence"])
        self.assertEqual("P + M_A + T_A", pmt["formulas"]["A"])
        self.assertEqual("P + M_B + T_B", pmt["formulas"]["B"])

    def test_equal_suffix_lengths_remain_eligible(self) -> None:
        model = model_from_edges(
            [
                edge("s0", "s1", "a"),
                edge("s1", "s2", "b"),
                edge("s2", "s3", "c"),
                edge("s2", "s1", "back"),
            ]
        )

        result = SPLIT_PATHS.analyze_split_paths(model, "s0", "s1", "s3")
        candidates = result["candidate_search"]["eligible_candidates"]

        self.assertEqual(2, len(candidates))
        self.assertEqual(
            {"to_shorter": 1, "to_longer": 1},
            candidates[1]["suffix_lengths"],
        )
        self.assertEqual(
            "shorter_suffix_not_longer",
            result["candidate_search"]["evaluations"][1]["reason"],
        )

    def test_breaks_common_suffix_ties_with_longest_prefix(self) -> None:
        model = model_from_edges(
            [
                edge("s0", "s1", "a"),
                edge("s1", "s2", "b"),
                edge("s2", "s3", "c"),
                edge("s3", "s4", "d"),
                edge("s4", "s5", "e"),
                edge("s5", "s6", "f"),
                edge("s6", "s9", "g"),
                edge("s2", "s1", "back_1"),
                edge("s3", "s7", "back_3a"),
                edge("s7", "s8", "back_3b"),
                edge("s8", "s1", "back_3c"),
            ]
        )

        result = SPLIT_PATHS.analyze_split_paths(model, "s0", "s1", "s9")
        search = result["candidate_search"]

        self.assertEqual(3, len(search["eligible_candidates"]))
        self.assertEqual(
            "selected_maximum_backward_common_suffix_longest_prefix_tiebreak",
            search["selection"]["status"],
        )
        self.assertEqual("P03", search["selection"]["selected_candidate_id"])
        self.assertEqual(3, result["common_prefix"]["length"])
        self.assertEqual(
            [1, 2, 3],
            [
                item["common_prefix"]["length"]
                for item in search["eligible_candidates"]
            ],
        )
        self.assertEqual(
            [0, 0, 0],
            [
                item["backward_common_suffix"]["length"]
                for item in search["eligible_candidates"]
            ],
        )
        self.assertEqual(
            ["P01", "P02", "P03"],
            search["selection"]["primary_tie_candidate_ids"],
        )
        self.assertEqual(
            "maximum_common_prefix_length",
            search["selection"]["criteria"]["secondary_tiebreak"]["name"],
        )
        self.assertEqual("pmt_empty_tail", result["final_result"]["mode"])
        self.assertEqual(
            [], result["final_result"]["pmt"]["T_A"]["input_sequence"]
        )
        self.assertEqual(
            [], result["final_result"]["pmt"]["T_B"]["input_sequence"]
        )

        report = SPLIT_PATHS.render_report(
            {"start_state": "s0", "source_sha256": "test", "analysis": result}
        )
        self.assertIn("`A = P + M_A + T_A`", report)
        self.assertIn("`B = P + M_B + T_B`", report)
        self.assertIn("候选：`P03`", report)

    def test_backward_match_requires_equal_input_and_output(self) -> None:
        first = [
            {"input": "x", "output": "same"},
            {"input": "tail", "output": "left"},
        ]
        second = [
            {"input": "y", "output": "same"},
            {"input": "tail", "output": "right"},
        ]

        common = SPLIT_PATHS.backward_common_transition_suffix(first, second)

        self.assertEqual(0, common["length"])

    def test_reframes_zero_common_tail_as_prefix_middle_tail(self) -> None:
        model = model_from_edges(
            [
                edge("s0", "s1", "a"),
                edge("s1", "s2", "b"),
                edge("s2", "s3", "c"),
                edge("s3", "s4", "d"),
                edge("s4", "s5", "e"),
                edge("s5", "s6", "d"),
                edge("s6", "s7", "e"),
            ]
        )

        result = SPLIT_PATHS.analyze_split_paths(model, "s0", "s5", "s7")
        final_result = result["final_result"]

        self.assertEqual(
            "pmt_reframed_common_tail",
            final_result["mode"],
        )
        pmt = final_result["pmt"]
        self.assertEqual(
            ["a", "b", "c"], pmt["P"]["input_sequence"]
        )
        self.assertEqual(
            [], pmt["M_A"]["input_sequence"]
        )
        self.assertEqual(
            ["d", "e"], pmt["M_B"]["input_sequence"]
        )
        self.assertEqual(
            ["d", "e"], pmt["T_A"]["input_sequence"]
        )
        self.assertEqual(
            ["d", "e"], pmt["T_B"]["input_sequence"]
        )
        self.assertEqual(
            ["s3", "s4", "s5"], pmt["T_A"]["state_sequence"]
        )
        self.assertEqual(
            ["s5", "s6", "s7"], pmt["T_B"]["state_sequence"]
        )

    def test_reports_failed_zero_common_tail_reframe(self) -> None:
        model = model_from_edges(
            [
                edge("s0", "s1", "a"),
                edge("s1", "s2", "b"),
                edge("s2", "s3", "c"),
                edge("s3", "s4", "f"),
                edge("s4", "s5", "g"),
                edge("s5", "s6", "d"),
                edge("s6", "s7", "e"),
            ]
        )

        result = SPLIT_PATHS.analyze_split_paths(model, "s0", "s5", "s7")
        reframe = result["candidate_search"]["zero_common_tail_reframes"][0]

        self.assertEqual("not_reframed", reframe["status"])
        self.assertEqual(
            "longer_suffix_not_a_tail_of_shorter_access",
            reframe["reason"],
        )
        pmt = result["final_result"]["pmt"]
        self.assertEqual("pmt_empty_tail", result["final_result"]["mode"])
        self.assertEqual([], pmt["T_A"]["input_sequence"])
        self.assertEqual([], pmt["T_B"]["input_sequence"])
        self.assertEqual(
            result["distinguishing_suffixes"]["to_shorter"]["input_sequence"],
            pmt["M_A"]["input_sequence"],
        )
        self.assertEqual(
            result["distinguishing_suffixes"]["to_longer"]["input_sequence"],
            pmt["M_B"]["input_sequence"],
        )

    def test_reports_no_non_start_intersection(self) -> None:
        model = model_from_edges(
            [
                edge("s0", "s1", "left"),
                edge("s0", "s2", "right"),
            ]
        )

        result = SPLIT_PATHS.analyze_split_paths(model, "s0", "s1", "s2")

        self.assertFalse(result["reverse_bfs"]["found"])
        self.assertIsNone(result["common_prefix"])
        self.assertIsNone(result["distinguishing_suffixes"])

    def test_equal_length_keeps_requested_order(self) -> None:
        model = model_from_edges(
            [
                edge("s0", "s1", "left"),
                edge("s0", "s2", "right"),
            ]
        )

        result = SPLIT_PATHS.analyze_split_paths(model, "s0", "s1", "s2")

        self.assertFalse(result["roles_swapped"])
        self.assertEqual("s1", result["shorter"]["target"])


if __name__ == "__main__":
    unittest.main()
