from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


MODULE_DIR = Path(__file__).resolve().parents[1]
EXPERIMENT_DIR = MODULE_DIR / "experiments"
REAL_CONFIG = Path(
    "D:/state-learning-lab/projects/state-learning-experiments/experiments/open5gs/"
    "ueransim-smc-context-pdu-selection/open5gs266-smc-context-h13-interrupted-20260730/"
    "followups/cycle-cover-repeat10-register-analysis-20260731/analysis/derived/"
    "register_inference/c01-c02-ngksi-signal-inference.yaml"
)
sys.path.insert(0, str(EXPERIMENT_DIR))

from infer_cycle_ngksi_regions import (
    RegionInferenceError,
    build_regions,
    candidate_status,
    guarded_candidates,
    infer,
    load_config,
    signal_gated_candidates,
    signal_observations,
    stable_slots,
    validate_signal_definitions,
    validate_observation_alignment,
)


def _record(
    sequence_id: int,
    step_id: int,
    command: str,
    output: str,
    output_value: int,
    input_value: int | None = None,
    is_init: bool = False,
) -> dict:
    fields = {"isInitMsg": str(is_init).lower()}
    if input_value is not None:
        fields["registration_ksi_value"] = input_value
    downlink = {"auth_request_ksi_value": output_value} if output == "authenticationRequest" else {"smc_ksi_value": output_value}
    return {
        "sequence_id": sequence_id,
        "step_id": step_id,
        "sequence_inputs": [
            "registrationRequest", "authenticationResponse", "registrationRequest",
            "authenticationResponse", "registrationRequest", "authenticationResponse",
            "registrationRequest", "authenticationResponse", "registrationRequest",
        ],
        "abstract_io": {"input": command, "output": output},
        "ue_side": {"fields": fields},
        "downlink_side": {"fields": downlink},
    }


def _signal(value: int, *, signal_id: str = "initial", symbol: str = "registrationRequest", occurrence: int = 0) -> dict:
    return {
        "kind": "signal",
        "signal_id": signal_id,
        "input_symbol": symbol,
        "field_path": f"ue_side.fields.{signal_id}",
        "value": value,
        "trace_line": 1,
        "event_position": 1,
        "declaration_index": 0,
        "occurrence_index": occurrence,
    }


def _input(value: int, *, symbol: str = "registrationRequest", path: str = "ue_side.fields.ksi", occurrence: int = 0) -> dict:
    return {
        "kind": "numeric_input",
        "input_symbol": symbol,
        "field_path": path,
        "value": value,
        "trace_line": 1,
        "event_position": 1,
        "occurrence_index": occurrence,
        "declaration_index": 0,
    }


def _region(repetition: int, before: int, after: int, *, signals: list[dict] | None = None, inputs: list[dict] | None = None) -> dict:
    signals = [] if signals is None else signals
    inputs = [] if inputs is None else inputs
    return {
        "sequence_line": 1,
        "repetition": repetition,
        "previous_output": {"value": before},
        "terminal_output": {"value": after},
        "signals": signals,
        "inputs": inputs,
        "observation_items": [*signals, *inputs],
        "terminal_edge": {"edge_id": "E-test"},
    }


class CycleNgksiRegionTests(unittest.TestCase):
    @unittest.skipUnless(REAL_CONFIG.exists(), "C01/C02 frozen integration fixture is not available")
    def test_real_c01_c05_trace_end_to_end(self) -> None:
        result = infer(load_config(REAL_CONFIG), REAL_CONFIG)
        by_edge = {item["edge"]["edge_id"]: item for item in result["results"]}
        self.assertEqual(15, len(by_edge))
        self.assertEqual("r' = r", by_edge["E0019"]["candidates"][0]["update_tree_text"])
        self.assertIn("if s0 == 1:\n  unknown/unobserved_signal_branch", by_edge["E0037"]["candidates"][0]["update_tree_text"])
        self.assertIn("if r < 6:\n    r' = r + 1", by_edge["E0037"]["candidates"][0]["update_tree_text"])
        c02_normal = by_edge["E0145"]
        self.assertEqual(
            (1, [1, 7], 2),
            (
                c02_normal["regions"][0]["previous_output"]["value"],
                [item["value"] for item in c02_normal["regions"][0]["observation_items"]],
                c02_normal["regions"][0]["terminal_output"]["value"],
            ),
        )
        self.assertIn("if r < 6:\n    r' = r + 1", c02_normal["candidates"][0]["update_tree_text"])
        c02_guti_text = {candidate["update_tree_text"] for candidate in by_edge["E0146"]["candidates"]}
        self.assertEqual(3, len(c02_guti_text))
        self.assertTrue(any("r' = r" in text for text in c02_guti_text))
        self.assertTrue(any("r' = 1" in text for text in c02_guti_text))
        self.assertTrue(any("r' = i0 + 1" in text for text in c02_guti_text))
        self.assertEqual("r' = r", by_edge["E0163"]["candidates"][0]["update_tree_text"])
        self.assertIn("if r < 6:\n    r' = r + 1", by_edge["E0169"]["candidates"][0]["update_tree_text"])
        self.assertEqual(
            (2, [0, 7], 3),
            (
                by_edge["E0169"]["regions"][0]["previous_output"]["value"],
                [item["value"] for item in by_edge["E0169"]["regions"][0]["observation_items"]],
                by_edge["E0169"]["regions"][0]["terminal_output"]["value"],
            ),
        )
        self.assertEqual(4, len(by_edge["E0170"]["candidates"]))
        self.assertEqual(4, len(by_edge["E0050"]["candidates"]))
        c04_registration = by_edge["E0073"]
        self.assertEqual(2, len(c04_registration["signal_slots"]))
        self.assertEqual(2, len(c04_registration["input_slots"]))
        self.assertEqual([1, 7, 0, 7], [item["value"] for item in c04_registration["regions"][0]["observation_items"]])
        self.assertEqual(4, len(c04_registration["candidates"]))
        self.assertEqual(3, len(by_edge["E0083"]["candidates"]))
        self.assertEqual("r' = r", by_edge["E0019"]["candidates"][0]["update_tree_text"])

    def test_cross_cycle_regions_keep_two_edges_separate_and_force_signal_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "model.dot").write_text(
                'digraph g {\n'
                '  s0 -> s1 [label="registrationRequest / authenticationRequest"];\n'
                '  s1 -> s3 [label="authenticationResponse / securityModeCommand"];\n'
                '  s3 -> s1 [label="registrationRequest / authenticationRequest"];\n'
                '}\n',
                encoding="utf-8",
            )
            cycle = {
                "sequence_export": {"cycles": [{
                    "cycle_id": "C01", "prefix_length": 1, "loop_length": 2,
                    "rotated_nodes": ["s1", "s3", "s1"],
                    "variants": [{"line_number": 1, "loop_inputs": ["authenticationResponse", "registrationRequest"]}],
                }]},
            }
            (root / "cycle.json").write_text(json.dumps(cycle), encoding="utf-8")
            (root / "input.seq").write_text(
                "registrationRequest authenticationResponse registrationRequest authenticationResponse "
                "registrationRequest authenticationResponse registrationRequest authenticationResponse registrationRequest\n",
                encoding="utf-8",
            )
            records = [
                _record(1, 1, "registrationRequest", "authenticationRequest", 0, 7),
                _record(1, 2, "authenticationResponse", "securityModeCommand", 0),
                _record(1, 3, "registrationRequest", "authenticationRequest", 1, 7),
                _record(1, 4, "authenticationResponse", "securityModeCommand", 1),
                _record(1, 5, "registrationRequest", "authenticationRequest", 2, 7),
                _record(1, 6, "authenticationResponse", "securityModeCommand", 2),
                _record(1, 7, "registrationRequest", "authenticationRequest", 3, 7),
                _record(1, 8, "authenticationResponse", "securityModeCommand", 3),
                _record(1, 9, "registrationRequest", "authenticationRequest", 4, 7),
            ]
            (root / "trace.jsonl").write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            config = {
                "schema_version": 2,
                "inputs": {"dot": "model.dot", "trace": "trace.jsonl", "cycle_cover": "cycle.json", "sequence_file": "input.seq"},
                "mapping": {
                    "downlink_ksi_by_output": {
                        "authenticationRequest": "downlink_side.fields.auth_request_ksi_value",
                        "securityModeCommand": "downlink_side.fields.smc_ksi_value",
                    },
                    "uplink_ksi_by_input": {"registrationRequest": "ue_side.fields.registration_ksi_value"},
                    "signal_definitions": [{
                        "id": "initial_uplink_context",
                        "path": "ue_side.fields.isInitMsg",
                        "value_type": "boolean",
                        "match": {"input_symbols": ["registrationRequest"]},
                        "phase": "before_numeric_inputs",
                    }],
                    "d_states": ["s3"],
                },
                "analysis": {
                    "repetitions": [2, 4], "min_consecutive_support": 3,
                    "max_numeric_depth": 1, "max_derived_signal_depth": 1,
                },
            }
            config_path = root / "config.yaml"
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            result = infer(load_config(config_path), config_path)

        by_input = {item["edge"]["logical_input"]: item for item in result["results"]}
        smc = by_input["authenticationResponse"]
        auth = by_input["registrationRequest"]
        self.assertEqual([1, 2, 3], [item["previous_output"]["value"] for item in smc["regions"]])
        self.assertEqual("r' = r", smc["candidates"][0]["update_tree_text"])
        self.assertEqual([], smc["signal_slots"])
        self.assertEqual([["signal", "numeric_input"]] * 3, [[item["kind"] for item in region["observation_items"]] for region in auth["regions"]])
        self.assertEqual("signal_guard", auth["candidates"][0]["update_tree"]["kind"])
        self.assertEqual("unobserved_signal_branch", auth["candidates"][0]["update_tree"]["true"]["reason"])
        self.assertIn("r' = r + 1", auth["candidates"][0]["update_tree_text"])
        self.assertEqual("partial_observational_candidate", auth["candidates"][0]["status"])
        self.assertEqual("d_state_reset_prior", smc["structural_candidates"][0]["origin"])

    def test_threshold_is_typed_and_else_is_constant_zero(self) -> None:
        regions = [
            _region(2, 0, 1),
            _region(3, 1, 2),
            _region(4, 6, 0),
        ]
        candidates = guarded_candidates(regions, [], min_support=3)
        matching = [
            item for item in candidates
            if item["kind"] == "threshold_guard"
            and item["guard"] == {"variable": "r", "operator": "<", "threshold": 6}
            and item["true"] == {"kind": "leaf", "formula": {"kind": "r_plus", "value": 1}}
            and item["false"] == {"kind": "leaf", "formula": {"kind": "constant", "value": 0}}
        ]
        self.assertEqual(1, len(matching))
        self.assertTrue(all(
            item["false"] == {"kind": "leaf", "formula": {"kind": "constant", "value": 0}}
            for item in candidates
            if item["kind"] == "threshold_guard"
        ))

    def test_input_plus_tie_is_preserved(self) -> None:
        regions = [
            _region(2, 1, 2, inputs=[_input(1)]),
            _region(3, 2, 3, inputs=[_input(2)]),
            _region(4, 3, 4, inputs=[_input(3)]),
        ]
        slots = stable_slots(regions, "input")
        candidates = guarded_candidates(regions, slots, min_support=3)
        self.assertEqual(
            [{"kind": "r_plus", "value": 1}, {"kind": "input_plus", "slot": 0, "value": 1}],
            [item["formula"] for item in candidates],
        )

    def test_list_and_wildcard_selectors_and_multiple_signals(self) -> None:
        definitions = [
            {"id": "selected", "path": "ue_side.fields.a", "value_type": "boolean", "match": {"input_symbols": ["registrationRequest", "serviceRequest"]}, "phase": "before_numeric_inputs"},
            {"id": "all_inputs", "path": "ue_side.fields.b", "value_type": "boolean", "match": {"input_symbols": ["*"]}, "phase": "before_numeric_inputs"},
            {"id": "other", "path": "ue_side.fields.c", "value_type": "boolean", "match": {"input_symbols": ["authenticationResponse"]}, "phase": "before_numeric_inputs"},
        ]
        validate_signal_definitions({"signal_definitions": definitions})
        record = {"_trace_line": 1, "step_id": 4, "ue_side": {"fields": {"a": "true", "b": "false"}}}
        observations = signal_observations(record, {"logical_input": "registrationRequest"}, definitions)
        self.assertEqual(["selected", "all_inputs"], [item["signal_id"] for item in observations])
        self.assertEqual([1, 0], [item["value"] for item in observations])

    def test_cross_event_order_and_same_event_signal_before_numeric_input(self) -> None:
        cycle = {
            "cycle_id": "C-test", "prefix_length": 0, "loop_length": 3,
            "rotated_nodes": ["s0", "s1", "s2", "s0"],
        }
        variant = {"line_number": 1, "loop_inputs": ["start", "middle", "finish"]}
        records = [
            {
                "_trace_line": 1, "step_id": 1, "abstract_io": {"input": "start", "output": "anchor"},
                "ue_side": {"fields": {"a": "false", "b": "false"}},
                "downlink_side": {"fields": {"ksi": 1}},
            },
            {
                "_trace_line": 2, "step_id": 2, "abstract_io": {"input": "middle", "output": "plain"},
                "ue_side": {"fields": {"a": "true", "b": "false", "ksi": 7, "other": 70}},
                "downlink_side": {"fields": {}},
            },
            {
                "_trace_line": 3, "step_id": 3, "abstract_io": {"input": "finish", "output": "anchor"},
                "ue_side": {"fields": {"a": "false", "b": "true", "ksi": 6, "other": 60}},
                "downlink_side": {"fields": {"ksi": 2}},
            },
        ]
        edges = {
            ("s0", "s1", "start"): {"edge_id": "E0", "source_state": "s0", "target_state": "s1", "logical_input": "start", "logical_output": "anchor"},
            ("s1", "s2", "middle"): {"edge_id": "E1", "source_state": "s1", "target_state": "s2", "logical_input": "middle", "logical_output": "plain"},
            ("s2", "s0", "finish"): {"edge_id": "E2", "source_state": "s2", "target_state": "s0", "logical_input": "finish", "logical_output": "anchor"},
        }
        definitions = [
            {"id": "a", "path": "ue_side.fields.a", "value_type": "boolean", "match": {"input_symbols": ["middle", "finish"]}, "phase": "before_numeric_inputs"},
            {"id": "b", "path": "ue_side.fields.b", "value_type": "boolean", "match": {"input_symbols": ["*"]}, "phase": "before_numeric_inputs"},
        ]
        regions = build_regions(
            cycle, variant, 1, records, edges, {"anchor": "downlink_side.fields.ksi"},
            {
                "middle": ["ue_side.fields.ksi", "ue_side.fields.other"],
                "finish": ["ue_side.fields.ksi", "ue_side.fields.other"],
            },
            definitions, 1, 1,
        )
        self.assertEqual(1, len(regions))
        items = regions[0]["observation_items"]
        self.assertEqual(
            [(2, "signal", "a"), (2, "signal", "b"), (2, "numeric_input", None), (2, "numeric_input", None),
             (3, "signal", "a"), (3, "signal", "b"), (3, "numeric_input", None), (3, "numeric_input", None)],
            [(item["event_position"], item["kind"], item.get("signal_id")) for item in items],
        )
        self.assertEqual(
            ["ue_side.fields.ksi", "ue_side.fields.other", "ue_side.fields.ksi", "ue_side.fields.other"],
            [item["field_path"] for item in items if item["kind"] == "numeric_input"],
        )

    def test_same_field_on_different_messages_has_distinct_slots(self) -> None:
        first_inputs = [_input(7, symbol="registrationRequest"), _input(7, symbol="registrationRequestGUTI", occurrence=0)]
        second_inputs = [_input(6, symbol="registrationRequest"), _input(6, symbol="registrationRequestGUTI", occurrence=0)]
        regions = [_region(2, 1, 2, inputs=first_inputs), _region(3, 2, 3, inputs=second_inputs)]
        slots = stable_slots(regions, "input")
        self.assertEqual(["registrationRequest", "registrationRequestGUTI"], [slot["input_symbol"] for slot in slots])
        self.assertEqual(["i0", "i1"], [slot["id"] for slot in slots])

    def test_reordered_slots_are_reported_instead_of_shifted(self) -> None:
        regions = [
            _region(2, 1, 2, inputs=[_input(7, symbol="registrationRequest"), _input(6, symbol="registrationRequestGUTI")]),
            _region(3, 2, 3, inputs=[_input(6, symbol="registrationRequestGUTI"), _input(7, symbol="registrationRequest")]),
        ]
        with self.assertRaisesRegex(RegionInferenceError, "Alignment anomaly"):
            stable_slots(regions, "input")

    def test_cross_type_temporal_reordering_is_reported(self) -> None:
        signal = _signal(1)
        numeric = _input(7)
        first = _region(2, 1, 2, signals=[signal], inputs=[numeric])
        second_signal = {**signal, "value": 0}
        second_numeric = {**numeric, "value": 6}
        second = _region(3, 2, 3, signals=[second_signal], inputs=[second_numeric])
        second["observation_items"] = [second_numeric, second_signal]
        with self.assertRaisesRegex(RegionInferenceError, "ordered observation identities differ"):
            validate_observation_alignment([first, second])

    def test_constant_true_signal_creates_unknown_false_branch(self) -> None:
        regions = [
            _region(rep, rep - 1, rep, signals=[_signal(1)], inputs=[_input(7)])
            for rep in range(2, 5)
        ]
        trees = signal_gated_candidates(regions, stable_slots(regions, "signal"), stable_slots(regions, "input"), 3, 1, 1)
        self.assertTrue(trees)
        self.assertTrue(all(tree["kind"] == "signal_guard" for tree in trees))
        self.assertTrue(all(tree["false"] == {"kind": "unknown", "reason": "unobserved_signal_branch"} for tree in trees))
        self.assertTrue(all(candidate_status(tree) == "partial_observational_candidate" for tree in trees))

    def test_dual_value_signal_can_be_observationally_exact(self) -> None:
        regions = [
            *[_region(rep, rep, rep + 1, signals=[_signal(1)]) for rep in range(2, 5)],
            *[_region(rep, rep, 0, signals=[_signal(0)]) for rep in range(5, 8)],
        ]
        trees = signal_gated_candidates(regions, stable_slots(regions, "signal"), [], 3, 1, 1)
        self.assertTrue(trees)
        self.assertTrue(all(candidate_status(tree) == "observationally_exact_candidate" for tree in trees))

    def test_insufficient_signal_branch_is_explicit_unknown(self) -> None:
        regions = [_region(2, 1, 2, signals=[_signal(1)]), _region(3, 2, 3, signals=[_signal(1)])]
        trees = signal_gated_candidates(regions, stable_slots(regions, "signal"), [], 3, 1, 1)
        self.assertEqual("insufficient_support", trees[0]["true"]["reason"])
        self.assertEqual("unobserved_signal_branch", trees[0]["false"]["reason"])

    def test_derived_input_value_split_runs_only_after_base_and_mod_fail(self) -> None:
        regions = [
            *[_region(rep, rep - 1, rep, inputs=[_input(7)]) for rep in range(2, 5)],
            *[_region(rep, rep - 4, 9, inputs=[_input(5)]) for rep in range(5, 8)],
        ]
        candidates = guarded_candidates(regions, stable_slots(regions, "input"), min_support=3)
        self.assertTrue(candidates)
        self.assertTrue(all(item["kind"] == "derived_value_guard" for item in candidates))
        self.assertEqual({5, 7}, {item["guard"]["value"] for item in candidates})

    def test_derived_split_rejects_nonconsecutive_overfit(self) -> None:
        regions = [
            _region(rep, rep, rep + (1 if rep % 2 else 4), inputs=[_input(7 if rep % 2 else 5)])
            for rep in range(2, 8)
        ]
        candidates = guarded_candidates(regions, stable_slots(regions, "input"), min_support=3)
        self.assertEqual([], candidates)


if __name__ == "__main__":
    unittest.main()
