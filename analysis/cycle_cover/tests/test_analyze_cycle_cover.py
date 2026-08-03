from __future__ import annotations

import itertools
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from unittest import mock


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import analyze_cycle_cover as cycle_cover


EXPERIMENTS_REPOSITORY = (
    Path(__file__).resolve().parents[4] / "state-learning-experiments"
)
H13_RECORD = (
    EXPERIMENTS_REPOSITORY
    / "experiments"
    / "open5gs"
    / "ueransim-smc-context-pdu-selection"
    / "open5gs266-smc-context-h13-interrupted-20260730"
)
H13_TARGET = H13_RECORD / "analysis" / "derived" / "hypothesis_13_smp.dot"
H13_CLOSURE = H13_RECORD / "evidence" / "hypotheses" / "hypothesis_13.dot"
H13_AVAILABLE = H13_TARGET.is_file() and H13_CLOSURE.is_file()
H14_RECORD = (
    H13_RECORD
    / "followups"
    / "h14-learning-complete-finalizer-failed-20260801"
)
H14_TARGET = H14_RECORD / "analysis" / "derived" / "hypothesis_14_smp.dot"
H14_CLOSURE = H14_RECORD / "evidence" / "hypotheses" / "hypothesis_14.dot"
H14_AVAILABLE = H14_TARGET.is_file() and H14_CLOSURE.is_file()


def write_dot(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("digraph G {\n" + body + "\n}\n", encoding="utf-8")
    return path


def analyze_h13() -> cycle_cover.AnalysisResult:
    return cycle_cover.analyze_cycle_cover(
        H13_TARGET,
        H13_CLOSURE,
        excluded_states=["s2"],
        required_inputs=[],
        required_outputs=["authenticationRequest", "securityModeCommand"],
        signal_mode="output-only",
    )


class DotParsingAndCandidateTests(unittest.TestCase):
    def test_merged_inputs_parallel_closure_priority_and_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_dot(
                root,
                "target.dot",
                """
  graph [rankdir=LR];
  node [shape=circle];
  __start0 [label=""];
  __start0 -> s0;
  s0 -> s1 [label="registrationRequest | registrationRequestGUTI / authenticationRequest"];
  s1 -> s2 [label="registrationRequest / authenticationRequest"];
""",
            )
            closure = write_dot(
                root,
                "closure.dot",
                """
  __start0 [label=""];
  __start0 -> s0;
  s0 -> s1 [label="registrationRequest / authenticationRequest"];
  s1 -> s0 [label="deregistrationRequest / null_action"];
  s1 -> s0 [label="securityModeComplete / securityModeCommand"];
  s1 -> s2 [label="registrationRequest / authenticationRequest"];
  s2 -> s0 [label="registrationRequest / authenticationRequest"];
""",
            )

            result = cycle_cover.analyze_cycle_cover(
                target,
                closure,
                excluded_states=["s2"],
                required_inputs=["registrationRequestGUTI"],
                required_outputs=["securityModeCommand"],
                signal_mode="any",
            )

            self.assertEqual(("s0", "s1"), result.target_model.states)
            self.assertNotIn("graph", result.target_model.states)
            self.assertNotIn("node", result.target_model.states)
            self.assertEqual(
                ("registrationRequest", "registrationRequestGUTI"),
                result.target_edges[0].inputs,
            )
            self.assertEqual(1, len(result.candidates))
            self.assertEqual(2, result.candidates[0].length)
            self.assertEqual(
                "securityModeComplete / securityModeCommand",
                result.candidates[0].edges[1].label,
            )
            self.assertEqual((0, 1), result.candidates[0].signal_edge_indexes)

    def test_s0_targets_can_use_distinct_original_return_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_dot(
                root,
                "target.dot",
                """
  s0 -> s1 [label="registrationRequest / authenticationRequest"];
  s0 -> s6 [label="registrationRequestGUTI / identityRequest"];
""",
            )
            closure = write_dot(
                root,
                "closure.dot",
                """
  s0 -> s1 [label="registrationRequest / authenticationRequest"];
  s0 -> s6 [label="registrationRequestGUTI / identityRequest"];
  s1 -> s0 [label="deregistrationRequest / null_action"];
  s6 -> s1 [label="identityResponse / authenticationRequest"];
""",
            )
            result = cycle_cover.analyze_cycle_cover(
                target,
                closure,
                excluded_states=[],
                required_inputs=["registrationRequest", "registrationRequestGUTI"],
                required_outputs=[],
                signal_mode="any",
            )
            routes = {
                tuple(candidate.nodes): candidate
                for candidate in result.candidates
            }
            self.assertIn(("s0", "s1"), routes)
            self.assertIn(("s0", "s6", "s1"), routes)
            self.assertEqual(2, len(result.selected))
            self.assertEqual({"E001", "E002"}, set().union(
                *(candidate.target_ids for candidate in result.selected)
            ))

    def test_signal_matching_is_exact_and_non_signal_cycle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_dot(
                root,
                "target.dot",
                '  s0 -> s1 [label="registrationRequestExtra / null_action"];',
            )
            closure = write_dot(
                root,
                "closure.dot",
                """
  s0 -> s1 [label="registrationRequestExtra / null_action"];
  s1 -> s0 [label="return / null_action"];
""",
            )
            with self.assertRaisesRegex(
                cycle_cover.CycleCoverError,
                "No signal-valid cycle or closed walk",
            ):
                cycle_cover.analyze_cycle_cover(
                    target,
                    closure,
                    excluded_states=[],
                    required_inputs=["registrationRequest"],
                    required_outputs=[],
                    signal_mode="any",
                )

    def test_duplicate_target_pair_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_dot(
                root,
                "target.dot",
                """
  s0 -> s1 [label="a / authenticationRequest"];
  s0 -> s1 [label="b / securityModeCommand"];
""",
            )
            closure = write_dot(
                root,
                "closure.dot",
                """
  s0 -> s1 [label="a / authenticationRequest"];
  s1 -> s0 [label="return / null_action"];
""",
            )
            with self.assertRaisesRegex(
                cycle_cover.CycleCoverError,
                "duplicate directed state pair",
            ):
                cycle_cover.analyze_cycle_cover(
                    target,
                    closure,
                    excluded_states=[],
                    required_inputs=[],
                    required_outputs=[],
                )

    def test_missing_target_pair_and_uncoverable_target_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_dot(
                root,
                "target.dot",
                '  s0 -> s1 [label="a / authenticationRequest"];',
            )
            missing = write_dot(
                root,
                "missing.dot",
                '  s1 -> s0 [label="return / null_action"];',
            )
            with self.assertRaisesRegex(
                cycle_cover.CycleCoverError,
                "missing from the closure graph",
            ):
                cycle_cover.analyze_cycle_cover(
                    target,
                    missing,
                    excluded_states=[],
                    required_inputs=[],
                    required_outputs=["authenticationRequest"],
                )

            no_return = write_dot(
                root,
                "no_return.dot",
                '  s0 -> s1 [label="a / authenticationRequest"];',
            )
            with self.assertRaisesRegex(
                cycle_cover.CycleCoverError,
                "No signal-valid cycle or closed walk",
            ):
                cycle_cover.analyze_cycle_cover(
                    target,
                    no_return,
                    excluded_states=[],
                    required_inputs=[],
                    required_outputs=["authenticationRequest"],
                )

    def test_cycle_enumeration_safety_limit_fails_without_partial_result(self) -> None:
        adjacency = {
            "s0": ("s1", "s2"),
            "s1": ("s0",),
            "s2": ("s0",),
        }
        with self.assertRaisesRegex(
            cycle_cover.CycleCoverError,
            "refusing to return a partial optimum",
        ):
            cycle_cover.enumerate_simple_cycles(
                ("s0", "s1", "s2"),
                adjacency,
                max_candidates=1,
            )

    def test_output_format_is_svg_only(self) -> None:
        self.assertEqual({"svg"}, cycle_cover.parse_formats("svg"))
        for value in ("dot", "pdf", "dot,svg,pdf", "png"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    cycle_cover.CycleCoverError,
                    "SVG-only",
                ):
                    cycle_cover.parse_formats(value)


class SequenceExportTests(unittest.TestCase):
    def test_shortest_prefix_merged_expansion_and_repeat_format(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_dot(
                root,
                "target.dot",
                '  s3 -> s4 [label="loopA | loopB / authenticationRequest"];',
            )
            closure = write_dot(
                root,
                "closure.dot",
                """
  s0 -> s2 [label="first / null_action"];
  s0 -> s1 [label="second / null_action"];
  s2 -> s3 [label="viaFirst / null_action"];
  s1 -> s3 [label="viaSecond / null_action"];
  s3 -> s4 [label="loopA / authenticationRequest"];
  s3 -> s4 [label="loopB / authenticationRequest"];
  s4 -> s3 [label="return / null_action"];
""",
            )
            result = cycle_cover.analyze_cycle_cover(
                target,
                closure,
                excluded_states=[],
                required_inputs=[],
                required_outputs=["authenticationRequest"],
                signal_mode="output-only",
            )
            lines, metadata = cycle_cover.build_sequence_export(
                result,
                start_state="s0",
                repeat_count=2,
                merged_input_policy="expand",
            )

            self.assertEqual(
                [
                    "first viaFirst loopA return loopA return",
                    "first viaFirst loopB return loopB return",
                ],
                lines,
            )
            self.assertEqual(2, metadata["line_count"])
            self.assertEqual(
                ["first", "viaFirst"],
                metadata["cycles"][0]["prefix_inputs"],
            )
            self.assertEqual("s3", metadata["cycles"][0]["cycle_start_state"])
            self.assertEqual(2, metadata["cycles"][0]["variant_count"])
            self.assertTrue(
                metadata["validation"]["all_lines_close_after_repetition"]
            )
            self.assertTrue(
                metadata["validation"][
                    "single_space_delimited_nonempty_lines"
                ]
            )
            sequence_path = root / "inputs" / "cycles.seq"
            written = cycle_cover.write_sequence_export(
                result,
                output_path=sequence_path,
                start_state="s0",
                repeat_count=2,
                merged_input_policy="expand",
                overwrite=False,
            )
            self.assertEqual(
                ("\n".join(lines) + "\n").encode("utf-8"),
                sequence_path.read_bytes(),
            )
            self.assertEqual(
                cycle_cover.sha256_file(sequence_path),
                written["sha256"],
            )

    def test_sequence_export_rejects_unreachable_and_nondeterministic_graphs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_dot(
                root,
                "target.dot",
                '  s3 -> s4 [label="loop / authenticationRequest"];',
            )
            unreachable_closure = write_dot(
                root,
                "unreachable.dot",
                """
  s0 -> s0 [label="idle / null_action"];
  s3 -> s4 [label="loop / authenticationRequest"];
  s4 -> s3 [label="return / null_action"];
""",
            )
            unreachable_result = cycle_cover.analyze_cycle_cover(
                target,
                unreachable_closure,
                excluded_states=[],
                required_inputs=[],
                required_outputs=["authenticationRequest"],
                signal_mode="output-only",
            )
            with self.assertRaisesRegex(
                cycle_cover.CycleCoverError,
                "unreachable from s0",
            ):
                cycle_cover.build_sequence_export(
                    unreachable_result,
                    start_state="s0",
                    repeat_count=1,
                    merged_input_policy="first",
                )

            nondeterministic_closure = write_dot(
                root,
                "nondeterministic.dot",
                """
  s0 -> s3 [label="access / null_action"];
  s0 -> s4 [label="access / null_action"];
  s3 -> s4 [label="loop / authenticationRequest"];
  s4 -> s3 [label="return / null_action"];
""",
            )
            nondeterministic_result = cycle_cover.analyze_cycle_cover(
                target,
                nondeterministic_closure,
                excluded_states=[],
                required_inputs=[],
                required_outputs=["authenticationRequest"],
                signal_mode="output-only",
            )
            with self.assertRaisesRegex(
                cycle_cover.CycleCoverError,
                "Non-deterministic closure DOT transition",
            ):
                cycle_cover.build_sequence_export(
                    nondeterministic_result,
                    start_state="s0",
                    repeat_count=1,
                    merged_input_policy="first",
                )

    def test_minimum_state_rotation_uses_first_occurrence(self) -> None:
        edges = (
            cycle_cover.Transition("s4", "s5", "a / x", ("a",), "x", 0),
            cycle_cover.Transition("s5", "s4", "b / x", ("b",), "x", 1),
            cycle_cover.Transition("s4", "s6", "c / x", ("c",), "x", 2),
            cycle_cover.Transition("s6", "s4", "d / x", ("d",), "x", 3),
        )
        candidate = cycle_cover.CandidateCycle(
            candidate_id="K001",
            nodes=("s4", "s5", "s4", "s6"),
            edges=edges,
            target_ids=frozenset({"E001"}),
            signal_edge_indexes=(0,),
        )

        start, nodes, rotated_edges = (
            cycle_cover.rotate_candidate_to_minimum_state(candidate)
        )

        self.assertEqual("s4", start)
        self.assertEqual(("s4", "s5", "s4", "s6"), nodes)
        self.assertEqual(edges, rotated_edges)


class SvgRenderValidationTests(unittest.TestCase):
    def test_malformed_svg_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "cycle.svg"

            def write_malformed_svg(arguments: list[str], **_: object) -> None:
                Path(arguments[3]).write_text(
                    '<?xml version="1.0"?>\\n<svg xmlns="http://www.w3.org/2000/svg"/>',
                    encoding="utf-8",
                )

            with (
                mock.patch.object(
                    cycle_cover,
                    "resolve_graphviz_engine",
                    return_value="dot",
                ),
                mock.patch.object(
                    cycle_cover.subprocess,
                    "run",
                    side_effect=write_malformed_svg,
                ),
                self.assertRaisesRegex(
                    cycle_cover.CycleCoverError,
                    "malformed SVG",
                ),
            ):
                cycle_cover.render_svg("digraph G {}", output, engine="dot")

            self.assertFalse(output.exists())
            self.assertFalse((Path(str(output) + ".tmp")).exists())


class ExactOptimizerTests(unittest.TestCase):
    @staticmethod
    def transition(name: str) -> cycle_cover.Transition:
        return cycle_cover.Transition(
            src=f"{name}_src",
            dst=f"{name}_dst",
            label=name,
            inputs=(name,),
            output="out",
            order=0,
            kind="target",
        )

    @classmethod
    def candidate(
        cls,
        candidate_id: str,
        targets: tuple[str, ...],
        edge_names: tuple[str, ...],
    ) -> cycle_cover.CandidateCycle:
        edges = tuple(cls.transition(name) for name in edge_names)
        nodes = tuple(f"{candidate_id}_s{index}" for index in range(len(edges)))
        return cycle_cover.CandidateCycle(
            candidate_id=candidate_id,
            nodes=nodes,
            edges=edges,
            target_ids=frozenset(targets),
            signal_edge_indexes=(0,),
        )

    def test_branch_and_bound_matches_brute_force_lexicographic_optimum(self) -> None:
        candidates = (
            self.candidate("K001", ("E001", "E002"), ("a", "b")),
            self.candidate("K002", ("E003", "E004"), ("c", "d")),
            self.candidate("K003", ("E001", "E003"), ("shared", "x")),
            self.candidate("K004", ("E002", "E004"), ("shared", "z")),
            self.candidate("K005", ("E001", "E002", "E003", "E004"), ("u", "v", "w")),
            self.candidate("K006", ("E001", "E002"), ("e", "f")),
        )
        target_set = {"E001", "E002", "E003", "E004"}

        brute_key = None
        brute_ids = None
        for count in range(1, len(candidates) + 1):
            for subset in itertools.combinations(candidates, count):
                if set().union(*(candidate.target_ids for candidate in subset)) < target_set:
                    continue
                usage = Counter(
                    identity
                    for candidate in subset
                    for identity in candidate.edge_identities
                )
                key = (
                    max(candidate.length for candidate in subset),
                    len(subset),
                    cycle_cover.repeat_count(usage),
                    sum(candidate.length for candidate in subset),
                    tuple(candidate.candidate_id for candidate in subset),
                )
                if brute_key is None or key < brute_key:
                    brute_key = key
                    brute_ids = key[-1]

        selected, maximum_length, usage = cycle_cover.select_optimal_cycles(
            candidates,
            target_count=4,
        )
        actual_key = (
            maximum_length,
            len(selected),
            cycle_cover.repeat_count(usage),
            sum(candidate.length for candidate in selected),
            tuple(candidate.candidate_id for candidate in selected),
        )
        self.assertEqual(brute_key, actual_key)
        self.assertEqual(brute_ids, ("K001", "K002"))


@unittest.skipUnless(H13_AVAILABLE, "H13 experiment evidence is not available")
class H13IntegrationTests(unittest.TestCase):
    def test_h13_exact_baseline_and_access_closures(self) -> None:
        result = analyze_h13()

        self.assertEqual(33, len(result.target_edges))
        self.assertTrue(result.used_closed_walk_fallback)
        self.assertEqual(63, len(result.candidates))
        self.assertEqual(
            {2: 6, 3: 6, 4: 6, 5: 7, 6: 2, 7: 6, 8: 5, 9: 7, 10: 9, 11: 5, 12: 4},
            dict(sorted(Counter(candidate.length for candidate in result.candidates).items())),
        )
        self.assertEqual(14, len(result.selected))
        self.assertEqual(9, result.minimum_max_length)
        self.assertEqual(64, result.total_length)
        self.assertEqual(29, result.repeated_edge_uses)
        self.assertEqual(
            {2: 3, 3: 4, 4: 1, 5: 2, 7: 1, 8: 2, 9: 1},
            dict(sorted(Counter(candidate.length for candidate in result.selected).items())),
        )
        self.assertEqual(
            {"simple_directed_cycle": 12, "composite_closed_walk": 2},
            dict(Counter(candidate.walk_type for candidate in result.selected)),
        )
        self.assertEqual(
            {f"E{index:03d}" for index in range(1, 34)},
            set().union(*(candidate.target_ids for candidate in result.selected)),
        )
        self.assertTrue(all(candidate.signal_edge_indexes for candidate in result.selected))
        with self.assertRaisesRegex(
            cycle_cover.CycleCoverError,
            "E009, E033",
        ):
            cycle_cover.analyze_cycle_cover(
                H13_TARGET,
                H13_CLOSURE,
                excluded_states=["s2"],
                required_inputs=[],
                required_outputs=["authenticationRequest", "securityModeCommand"],
                signal_mode="output-only",
                allow_closed_walk_fallback=False,
            )

        target_ids = {
            edge.pair: f"E{index:03d}"
            for index, edge in enumerate(result.target_edges, start=1)
        }
        for pair in (("s0", "s1"), ("s0", "s6")):
            target_id = target_ids[pair]
            covering = [
                candidate
                for candidate in result.candidates
                if target_id in candidate.target_ids
            ]
            self.assertTrue(covering, pair)
            self.assertTrue(
                any(any(edge.kind == "closure" for edge in candidate.edges) for candidate in covering),
                pair,
            )

    def test_h13_output_is_byte_deterministic(self) -> None:
        try:
            cycle_cover.resolve_graphviz_engine("dot")
        except cycle_cover.CycleCoverError as error:
            self.skipTest(str(error))

        result = analyze_h13()
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_root = Path(first)
            second_root = Path(second)
            cycle_cover.generate_outputs(
                result,
                first_root,
                basename="hypothesis_13",
                formats={"svg"},
                engine="dot",
                overwrite=False,
            )
            cycle_cover.generate_outputs(
                result,
                second_root,
                basename="hypothesis_13",
                formats={"svg"},
                engine="dot",
                overwrite=False,
            )
            first_files = {
                path.relative_to(first_root): path.read_bytes()
                for path in first_root.rglob("*")
                if path.is_file()
            }
            second_files = {
                path.relative_to(second_root): path.read_bytes()
                for path in second_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(first_files, second_files)
            payload = json.loads(
                (
                    first_root / "hypothesis_13_cycle_cover.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(payload["validation"]["all_target_edges_covered"])
            self.assertEqual(33, len(payload["target_edges"]))
            self.assertEqual("output-only", payload["parameters"]["signal_match_mode"])
            self.assertTrue(payload["parameters"]["used_closed_walk_fallback"])
            self.assertEqual(14, len(payload["selected_cycles"]))
            self.assertEqual(16, len(first_files))
            self.assertFalse(list(first_root.rglob("*.dot")))
            self.assertFalse(list(first_root.rglob("*.pdf")))
            self.assertTrue(
                payload["validation"]["all_selected_artifacts_are_svg"]
            )

    def test_h13_full_smp_cycle_svgs_are_well_formed(self) -> None:
        try:
            cycle_cover.resolve_graphviz_engine("dot")
        except cycle_cover.CycleCoverError as error:
            self.skipTest(str(error))

        result = analyze_h13()
        selected_ids, colors, _ = cycle_cover.selected_cycle_metadata(result)
        for candidate in result.selected:
            cycle_id = selected_ids[candidate.candidate_id]
            dot_text = cycle_cover.build_cycle_smp_dot(
                result,
                basename="hypothesis_13",
                candidate=candidate,
                cycle_id=cycle_id,
                color=colors[cycle_id],
            )
            closure_count = sum(
                edge.kind == "closure" for edge in candidate.edges
            )
            target_count = sum(
                edge.kind == "target" for edge in candidate.edges
            )
            distinct_target_count = len(
                {
                    edge.identity
                    for edge in candidate.edges
                    if edge.kind == "target"
                }
            )
            self.assertIn("__start0 -> s0;", dot_text)
            self.assertIn('s2 [shape="circle" label="s2"]', dot_text)
            self.assertEqual(34 + closure_count, dot_text.count(" -> "))
            self.assertEqual(
                distinct_target_count,
                dot_text.count('style="solid", penwidth=4.0'),
            )
            self.assertEqual(
                33 - distinct_target_count,
                dot_text.count(
                    'color="black", fontcolor="black", '
                    'style="solid", penwidth=1.0'
                ),
            )
            self.assertEqual(
                closure_count,
                dot_text.count('style="dashed"'),
            )
            self.assertIn(colors[cycle_id], dot_text)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cycle_cover.generate_outputs(
                result,
                root,
                basename="hypothesis_13",
                formats={"svg"},
                engine="dot",
                overwrite=False,
            )
            svgs = sorted((root / "cycles").glob("*.svg"))
            self.assertEqual(14, len(svgs))
            self.assertFalse(list(root.rglob("*.dot")))
            self.assertFalse(list(root.rglob("*.pdf")))
            for svg in svgs:
                self.assertGreater(svg.stat().st_size, 0)
                svg_root = ET.parse(svg).getroot()
                self.assertTrue(svg_root.tag.endswith("svg"))
                view_box = svg_root.attrib.get("viewBox", "").split()
                self.assertEqual(4, len(view_box))
                self.assertGreater(float(view_box[2]), 0)
                self.assertGreater(float(view_box[3]), 0)
                titles = {
                    element.text
                    for element in svg_root.iter()
                    if element.tag.endswith("title")
                }
                self.assertTrue(
                    {f"s{index}" for index in range(17)}.issubset(titles)
                )
                self.assertIn("__start0", titles)

    def test_h13_sequence_export_baseline(self) -> None:
        result = analyze_h13()
        lines, metadata = cycle_cover.build_sequence_export(
            result,
            start_state="s0",
            repeat_count=10,
            merged_input_policy="expand",
        )

        self.assertEqual(28, len(lines))
        self.assertEqual(14, metadata["cycle_count"])
        self.assertEqual(
            [2, 2, 2, 2, 1, 1, 2, 2, 1, 1, 2, 2, 4, 4],
            [cycle["variant_count"] for cycle in metadata["cycles"]],
        )
        self.assertEqual(
            ["s1", "s12", "s13", "s0", "s1", "s7", "s12",
             "s1", "s0", "s9", "s4", "s1", "s1", "s1"],
            [cycle["cycle_start_state"] for cycle in metadata["cycles"]],
        )
        self.assertEqual(
            [21, 25, 26, 30, 31, 33, 35, 41, 50, 55, 73, 81, 81, 91],
            [
                cycle["variants"][0]["input_count"]
                for cycle in metadata["cycles"]
            ],
        )
        self.assertTrue(
            metadata["validation"]["all_cycle_starts_reachable"]
        )
        self.assertTrue(
            metadata["validation"]["all_concrete_transitions_defined"]
        )
        self.assertTrue(
            metadata["validation"]["all_lines_close_after_repetition"]
        )
        self.assertTrue(
            metadata["validation"]["excluded_states_absent_from_access_graph"]
        )
        self.assertTrue(all(line and "  " not in line for line in lines))


class LayeredRouteTests(unittest.TestCase):
    def test_parallel_targets_and_concrete_self_loops_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_dot(
                root,
                "target.dot",
                """
  s0 -> s1 [label="a / authenticationRequest"];
  s0 -> s1 [label="b / authenticationRequest"];
""",
            )
            closure = write_dot(
                root,
                "closure.dot",
                """
  s0 -> s1 [label="a / authenticationRequest"];
  s0 -> s1 [label="b / authenticationRequest"];
  s1 -> s0 [label="return / null_action"];
  s1 -> s1 [label="registrationRequest / authenticationRequest"];
  s1 -> s1 [label="registrationRequestGUTI / authenticationRequest"];
""",
            )
            analysis = cycle_cover.build_layered_analysis(
                target,
                closure,
                excluded_states=[],
                required_inputs=[],
                required_outputs=["authenticationRequest"],
            )
            self.assertEqual(2, len(analysis.targets))
            self.assertEqual("parallel_target_state_pair", analysis.input_warnings[0]["code"])
            covered = set().union(
                *(route.target_ids for route in analysis.base_simple_routes)
            )
            self.assertEqual({"E001", "E002"}, covered)
            self.assertEqual(2, len(analysis.standalone_self_loops))
            self.assertEqual(
                ["registrationRequest / authenticationRequest", "registrationRequestGUTI / authenticationRequest"],
                [route.edges[0].label for route in analysis.standalone_self_loops],
            )

    def test_embedded_self_loop_runs_three_times_in_every_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_dot(
                root,
                "target.dot",
                """
  s0 -> s1 [label="a / authenticationRequest"];
  s1 -> s2 [label="b / null_action"];
  s2 -> s0 [label="c / null_action"];
""",
            )
            closure = write_dot(
                root,
                "closure.dot",
                """
  s0 -> s1 [label="a / authenticationRequest"];
  s1 -> s2 [label="b / null_action"];
  s2 -> s0 [label="c / null_action"];
  s1 -> s1 [label="loop / authenticationRequest"];
""",
            )
            analysis = cycle_cover.build_layered_analysis(
                target, closure, [], [], ["authenticationRequest"]
            )
            self.assertEqual(1, len(analysis.extra_short_routes))
            self.assertEqual(1, len(analysis.extra_embedded_routes))
            lines, metadata = cycle_cover.build_route_sequence_export(
                analysis,
                analysis.extra_embedded_routes,
                start_state="s0",
                repeat_count=2,
                merged_input_policy="expand",
            )
            self.assertEqual(["a loop loop loop b c a loop loop loop b c"], lines)
            self.assertEqual(1, metadata["line_count"])

    def test_base_and_extra_outputs_are_isolated_and_valid_svg(self) -> None:
        try:
            cycle_cover.resolve_graphviz_engine("dot")
        except cycle_cover.CycleCoverError as error:
            self.skipTest(str(error))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = write_dot(
                root,
                "target.dot",
                """
  s0 -> s1 [label="a / authenticationRequest"];
  s1 -> s2 [label="b / null_action"];
  s2 -> s0 [label="c / null_action"];
""",
            )
            closure = write_dot(
                root,
                "closure.dot",
                """
  s0 -> s1 [label="a / authenticationRequest"];
  s1 -> s2 [label="b / null_action"];
  s2 -> s0 [label="c / null_action"];
  s1 -> s1 [label="loop / authenticationRequest"];
""",
            )
            analysis = cycle_cover.build_layered_analysis(
                target, closure, [], [], ["authenticationRequest"]
            )
            summary = cycle_cover.generate_layered_outputs(
                analysis,
                root / "base",
                "base",
                root / "base.seq",
                "s0",
                2,
                "expand",
                "dot",
                False,
                root / "extra",
                "extra",
                root / "extra.seq",
            )
            self.assertIn("base", summary)
            self.assertIn("extra", summary)
            self.assertTrue((root / "base.seq").is_file())
            self.assertTrue((root / "extra.seq").is_file())
            self.assertTrue((root / "base" / "base_cycle_cover.json").is_file())
            self.assertTrue((root / "extra" / "extra_cycle_cover.json").is_file())
            for svg in list((root / "base" / "cycles").glob("*.svg")) + list((root / "extra" / "cycles").glob("*.svg")):
                self.assertTrue(ET.parse(svg).getroot().tag.endswith("svg"))


@unittest.skipUnless(H14_AVAILABLE, "H14 experiment evidence is not available")
class H14LayeredIntegrationTests(unittest.TestCase):
    def test_h14_layered_baseline_and_sequences(self) -> None:
        analysis = cycle_cover.build_layered_analysis(
            H14_TARGET,
            H14_CLOSURE,
            excluded_states=["s2"],
            required_inputs=[],
            required_outputs=["authenticationRequest", "securityModeCommand"],
            signal_mode="output-only",
        )
        self.assertEqual(36, len(analysis.targets))
        self.assertEqual(6, len(analysis.standalone_self_loops))
        self.assertEqual(16, len(analysis.extra_short_routes))
        self.assertEqual(30, len(analysis.extra_embedded_routes))
        self.assertTrue(analysis.base_fallback_routes)
        base = (
            analysis.base_simple_routes
            + analysis.base_fallback_routes
            + analysis.standalone_self_loops
        )
        extra = analysis.extra_short_routes + analysis.extra_embedded_routes
        base_lines, base_metadata = cycle_cover.build_route_sequence_export(
            analysis, base, "s0", 10, "expand"
        )
        extra_lines, extra_metadata = cycle_cover.build_route_sequence_export(
            analysis, extra, "s0", 10, "expand"
        )
        self.assertEqual(37, len(base_lines))
        self.assertEqual(70, len(extra_lines))
        self.assertTrue(base_metadata["validation"]["all_lines_simulated_against_closure_dot"])
        self.assertTrue(extra_metadata["validation"]["all_lines_close_after_each_iteration"])


if __name__ == "__main__":
    unittest.main()
