from __future__ import annotations

import json
import contextlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml


MODULE_DIR = Path(__file__).resolve().parents[1]
EXPERIMENT_DIR = MODULE_DIR / "experiments"
REAL_CONFIG = Path(
    "D:/state-learning-lab/projects/state-learning-experiments/experiments/open5gs/"
    "ueransim-smc-context-pdu-selection/open5gs266-smc-context-h13-interrupted-20260730/"
    "followups/cycle-cover-repeat10-register-analysis-20260731/analysis/derived/"
    "register_inference/c01-c14-ngksi-signal-inference.yaml"
)
H14_CONFIG = Path(
    "D:/state-learning-lab/projects/state-learning-experiments/experiments/open5gs/"
    "ueransim-smc-context-pdu-selection/h14-base-runtime-20260804/"
    "analysis/register-inference/config.yaml"
)
sys.path.insert(0, str(EXPERIMENT_DIR))

from infer_cycle_ngksi_regions import (
    RegionInferenceError,
    build_v3_workbook_payload,
    build_regions,
    candidate_status,
    cleanup_workbook_intermediates,
    default_signal_tree,
    effective_snapshot,
    ensure_workbook_frozen_headers,
    guarded_candidates,
    infer,
    load_config,
    main,
    numeric_input_observations,
    publish_workbook_delivery,
    render_v3_report,
    set_valued_numeric_candidates,
    signal_gated_candidates,
    signal_observations,
    standalone_event_sample,
    stable_slots,
    tree_text,
    unique_sorted_trees,
    validate_v3_analysis,
    validate_signal_definitions,
    validate_numeric_input_definitions,
    validate_observation_alignment,
    v3_input_register_updates,
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
    @unittest.skipUnless(H14_CONFIG.exists(), "H14 complete-cycle fixture is not available")
    def test_h14_summary_report_and_workbook_contract(self) -> None:
        config = load_config(H14_CONFIG)
        result = infer(config, H14_CONFIG)
        report, integrity = render_v3_report(result, config, H14_CONFIG)
        payload, sheet_rows = build_v3_workbook_payload(result, config, H14_CONFIG)
        self.assertEqual(
            {"edge_group_count": 52, "cycle_count": 23, "cycle_variant_count": 37, "cycle_edge_usage_count": 90},
            integrity,
        )
        self.assertIn('<table id="edge-summary" style="width:100%; table-layout:fixed">', report)
        self.assertEqual(1, report.count('<table id='))
        self.assertEqual(1, report.count("<code>E0073</code>"))
        self.assertIn("联合拟合失败", report)
        self.assertIn("相对稳定推断迁移检验", report)
        self.assertNotIn("分区分歧", report)
        self.assertNotIn("观察冲突", report)
        self.assertIn("相对稳定", report)
        self.assertIn("假设性", report)
        self.assertIn("unknown", report)
        self.assertIn("r_i&#x27; = i", report)
        self.assertNotIn("unknown/unobserved_signal_branch", report)
        self.assertNotIn("partial_observational_candidate", report)
        self.assertNotIn("observationally_exact_candidate", report)
        self.assertNotIn("r_i[ngksi_uplink]", report)
        self.assertNotIn("direct_input_observation", report)
        self.assertNotIn("carried_input_register", report)
        self.assertIn("交集", report)
        self.assertIn("/<br>", report)
        self.assertIn("<colgroup>", report)
        self.assertEqual({"循环边使用": 90}, sheet_rows)
        sheets = {sheet["name"]: sheet for sheet in payload["sheets"]}
        self.assertEqual(["循环边使用"], list(sheets))
        cycle_sheet = sheets["循环边使用"]
        self.assertIn("候选类型", cycle_sheet["headers"])
        self.assertIn("候选生成结果", cycle_sheet["headers"])
        self.assertIn("相对稳定推断来源边", cycle_sheet["headers"])
        self.assertIn("相对稳定推断", cycle_sheet["headers"])
        self.assertIn("迁移检验", cycle_sheet["headers"])
        self.assertNotIn("环内序号", cycle_sheet["headers"])
        self.assertEqual([10, 14, 12, 10, 8, 8, 18, 20, 12], cycle_sheet["widths"][:9])
        workbook_text = "\n".join(
            cell for sheet in sheets.values() for row in sheet["rows"] for cell in row
        )
        self.assertNotIn("unknown/unobserved_signal_branch", workbook_text)
        self.assertNotIn("r_i[ngksi_uplink]", workbook_text)
        self.assertNotIn("direct_input_observation", workbook_text)
        self.assertNotIn("carried_input_register", workbook_text)
        self.assertIn(" ｜ ", workbook_text)
        self.assertIn("反推状态", cycle_sheet["headers"])
        self.assertIn("反推候选与假设", cycle_sheet["headers"])
        s008_e0073 = next(row for row in cycle_sheet["rows"] if row[0] == "S008" and row[3] == "E0073")
        s036_e0073 = next(row for row in cycle_sheet["rows"] if row[0] == "S036" and row[3] == "E0073")
        s008_e0002 = next(row for row in cycle_sheet["rows"] if row[0] == "S008" and row[3] == "E0002")
        self.assertEqual("假设性", s008_e0073[8])
        self.assertEqual("迁移失败，执行前序反推", s008_e0073[14])
        self.assertIn("8/8 个样本不匹配", s008_e0073[15])
        self.assertEqual("迁移成功", s036_e0073[14])
        self.assertIn("16/16 个样本成立", s036_e0073[15])
        self.assertEqual("已生成 6 个候选", s008_e0002[16])
        self.assertIn("允许输出：{6,7}", s008_e0002[17])
        self.assertIn("E0016 r'=r", s008_e0002[17])
        self.assertIn("前序反推分支", report)

        by_edge = {item["edge"]["edge_id"]: item for item in result["results"]}
        stable_summary = result["relatively_stable_inference"]
        self.assertEqual(3, len(stable_summary["groups"]))
        self.assertEqual(9, stable_summary["target_edge_count"])
        self.assertEqual(22, stable_summary["target_partition_count"])
        self.assertEqual(
            {
                "migration_failed": 1,
                "migration_succeeded": 4,
                "no_matching_relatively_stable_inference": 17,
            },
            stable_summary["migration_status_counts"],
        )
        self.assertEqual([7], stable_summary["dynamic_t_preference"]["values"])
        self.assertEqual(
            [
                "preferred_exact_constant_assignment",
                "preferred_derived_guard_value",
                "default_complexity_order",
            ],
            stable_summary["dynamic_t_preference"]["ranking_policy"],
        )
        groups = {
            (tuple((item["signal_id"], item["value"]) for item in group["signal_context"]),
             group["logical_input"], group["logical_output"]): group
            for group in stable_summary["groups"]
        }
        self.assertEqual(0, groups[((), "authenticationResponse", "securityModeCommand")]["target_partition_count"])
        self.assertEqual(
            2,
            groups[(("isInitMsg", 0),), "registrationRequestGUTI", "authenticationRequest"]["target_partition_count"],
        )
        self.assertEqual("hypothetical_candidate", by_edge["E0073"]["candidate_grade"])
        resolution = by_edge["E0073"]["hypothetical_candidate_resolution"]
        self.assertEqual("combined_sample_fit_failed", resolution["selection"]["status"])
        self.assertTrue(resolution["selection"]["error"]["continue_to_migration_and_backward_inference"])
        migration = by_edge["E0073"]["relatively_stable_inference_migration"]
        partitions = {item["cycle_id"]: item for item in migration["cycle_results"]}
        self.assertEqual("migration_failed", partitions["S008"]["status"])
        self.assertEqual("migration_succeeded", partitions["S036"]["status"])
        first = partitions["S008"]["failing_candidates"][0]["counterexamples"][0]
        self.assertEqual(
            {"r_before": 0, "predicted_r_after": 1, "observed_r_after": 0},
            {key: first[key] for key in ("r_before", "predicted_r_after", "observed_r_after")},
        )
        no_matching = {
            (item["edge"]["edge_id"], partition["cycle_id"])
            for item in result["results"]
            for partition in (item.get("relatively_stable_inference_migration") or {}).get("cycle_results", [])
            if partition["status"] == "no_matching_relatively_stable_inference"
        }
        self.assertEqual(17, len(no_matching))
        self.assertIn(("E0001", "S018"), no_matching)
        self.assertIn(("E0085", "S039"), no_matching)
        self.assertIn(("E0181", "S039"), no_matching)
        backward = by_edge["E0002"]["backward_inference"]["attempts"]
        self.assertEqual(1, len(backward))
        attempt = backward[0]
        self.assertEqual("E0073:S008:E0002", attempt["inference_id"])
        self.assertEqual(["E0016"], [held["edge"]["edge_id"] for held in attempt["held_predecessors"]])
        self.assertTrue(all(sample["allowed_r_after_values"] == [6, 7] for sample in attempt["samples"]))
        self.assertEqual(
            ["r' = 7", "r' = 6", "r' = r + 6", "r' = r + 7", "r' = r_i[ngksi_uplink] - 1", "r' = r_i[ngksi_uplink]"],
            [candidate["branch_update_text"] for candidate in attempt["candidates"]],
        )
        self.assertTrue(attempt["candidates"][0]["preference"]["preferred"])
        self.assertEqual("preferred_exact_constant_assignment", attempt["candidates"][0]["preference"]["reason"])

    @unittest.skipUnless(H14_CONFIG.exists(), "H14 complete-cycle fixture is not available")
    def test_cli_requires_report_but_workbook_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()):
            root = Path(temp)
            with self.assertRaises(SystemExit):
                main(["--config", str(H14_CONFIG), "--output", str(root / "result.json")])
            self.assertEqual(0, main([
                "--config", str(H14_CONFIG), "--output", str(root / "result.json"),
                "--report", str(root / "summary.md"),
            ]))
            result = json.loads((root / "result.json").read_text(encoding="utf-8"))
            self.assertNotIn("workbook_artifact", result)
            self.assertIn("本次未请求 Excel 审计工作簿", (root / "summary.md").read_text(encoding="utf-8"))
            self.assertEqual(2, main([
                "--config", str(H14_CONFIG), "--output", str(root / "other.json"),
                "--report", str(root / "other.md"),
                "--workbook-preview-dir", str(root / "preview"),
            ]))

    def test_workbook_delivery_relinks_a_stale_same_byte_hard_link(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.xlsx"
            replacement = root / "replacement.xlsx"
            delivery = root / "delivery.xlsx"
            first.write_bytes(b"same workbook bytes")
            replacement.write_bytes(b"same workbook bytes")
            publish_workbook_delivery(first, delivery)
            self.assertTrue(os.path.samefile(first, delivery))
            publish_workbook_delivery(replacement, delivery)
            self.assertTrue(os.path.samefile(replacement, delivery))
            changed = root / "changed.xlsx"
            changed.write_bytes(b"new workbook bytes")
            publish_workbook_delivery(changed, delivery)
            self.assertTrue(os.path.samefile(changed, delivery))

    def test_workbook_inspection_sidecar_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workbook = Path(temp) / "audit.xlsx"
            sidecar = Path(str(workbook) + ".inspect.ndjson")
            sidecar.write_text('{"intermediate": true}\n', encoding="utf-8")
            cleanup_workbook_intermediates(workbook)
            self.assertFalse(sidecar.exists())

    def test_workbook_repair_adds_freeze_pane_and_table_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workbook = Path(temp) / "audit.xlsx"
            with zipfile.ZipFile(workbook, "w") as archive:
                archive.writestr(
                    "xl/worksheets/sheet1.xml",
                    '<x:worksheet xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<x:sheetViews><x:sheetView showGridLines="0" workbookViewId="0" /></x:sheetViews>'
                    '</x:worksheet>',
                )
                archive.writestr(
                    "xl/tables/table1.xml",
                    '<x:table xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ref="A1:P91">'
                    '<x:tableColumns count="1"><x:tableColumn id="1" name="循环" /></x:tableColumns>'
                    '</x:table>',
                )
            ensure_workbook_frozen_headers(workbook)
            with zipfile.ZipFile(workbook) as archive:
                sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                table_xml = archive.read("xl/tables/table1.xml").decode("utf-8")
            self.assertIn('state="frozen"', sheet_xml)
            self.assertIn('autoFilter ref="A1:P91"', table_xml)

    def test_v3_rejects_static_derived_guard_preference(self) -> None:
        with self.assertRaisesRegex(RegionInferenceError, "not configurable"):
            validate_v3_analysis({"preferred_derived_guard_values": [7]})

    def test_v3_backward_inference_policy_is_closed(self) -> None:
        with self.assertRaisesRegex(RegionInferenceError, "predecessor_policy"):
            validate_v3_analysis({
                "backward_inference": {
                    "enabled": True,
                    "value_domain": "observed_global",
                    "predecessor_policy": "any_predecessor",
                    "earlier_predecessor_policy": "hold",
                    "signal_scope": "observed_reference_branches_only",
                },
            })

    def test_dynamic_t_orders_exact_constant_then_matching_guard_tree(self) -> None:
        tree_five = {
            "kind": "derived_value_guard",
            "guard": {
                "variable": "input_register", "input_register_id": "ksi",
                "operator": "==", "value": 5,
            },
            "true": {"kind": "leaf", "formula": {"kind": "constant", "value": 1}},
            "false": {"kind": "leaf", "formula": {"kind": "constant", "value": 0}},
        }
        tree_seven = {
            **tree_five,
            "guard": {**tree_five["guard"], "value": 7},
        }
        constant_seven = {"kind": "leaf", "formula": {"kind": "constant", "value": 7}}
        ordered = unique_sorted_trees([tree_five, constant_seven, tree_seven], [7])
        self.assertIs(constant_seven, ordered[0])
        self.assertIs(tree_seven, ordered[1])
        self.assertIs(tree_five, ordered[2])

    def test_set_valued_backward_search_reports_no_exact_candidate(self) -> None:
        samples = []
        for repetition, before, allowed in ((2, 0, [0]), (3, 1, [3]), (4, 2, [0])):
            sample = _region(repetition, before, 0)
            sample["allowed_r_after_values"] = allowed
            sample["input_register_values"] = {}
            samples.append(sample)
        self.assertEqual(
            [],
            set_valued_numeric_candidates(samples, [], list(range(8)), 3, 1, 1, [7]),
        )

    @unittest.skipUnless(REAL_CONFIG.exists(), "C01/C02 frozen integration fixture is not available")
    def test_real_c01_c14_trace_end_to_end(self) -> None:
        result = infer(load_config(REAL_CONFIG), REAL_CONFIG)
        by_edge = {item["edge"]["edge_id"]: item for item in result["results"]}
        self.assertEqual(3, result["schema_version"])
        self.assertEqual(42, len(by_edge))
        self.assertEqual("r' = r", by_edge["E0019"]["candidates"][0]["update_tree_text"])
        self.assertEqual("relatively_stable_candidate", by_edge["E0019"]["candidates"][0]["candidate_grade"])
        self.assertEqual("r_i[ngksi_uplink]' = r_i[ngksi_uplink]", by_edge["E0019"]["candidates"][0]["input_register_updates"][0]["update"]["text"])
        self.assertIn("if s0 == 1:\n  unknown/unobserved_signal_branch", by_edge["E0037"]["candidates"][0]["update_tree_text"])
        self.assertIn("if r < 6:\n    r' = r + 1", by_edge["E0037"]["candidates"][0]["update_tree_text"])
        self.assertEqual(3, by_edge["E0037"]["direct_regions"][0]["repetition"])
        c02_normal = by_edge["E0145"]
        self.assertEqual("hypothetical_candidate", c02_normal["candidates"][0]["candidate_grade"])
        self.assertEqual((2, [1, 7], 3), (c02_normal["direct_regions"][0]["previous_output"]["value"], [item["value"] for item in c02_normal["direct_regions"][0]["signals"] + c02_normal["direct_regions"][0]["inputs"]], c02_normal["direct_regions"][0]["terminal_output"]["value"]))
        self.assertIn("if r < 6:\n    r' = r + 1", c02_normal["candidates"][0]["update_tree_text"])
        c02_guti_text = {candidate["update_tree_text"] for candidate in by_edge["E0146"]["candidates"]}
        self.assertEqual(2, len(c02_guti_text))
        self.assertTrue(all("r_i[ngksi_uplink] + 1" in text for text in c02_guti_text))
        self.assertEqual("r' = r", by_edge["E0160"]["candidates"][0]["update_tree_text"])
        self.assertEqual("r_i[ngksi_uplink]' = r_i[ngksi_uplink]", by_edge["E0160"]["candidates"][0]["input_register_updates"][0]["update"]["text"])
        self.assertEqual("r' = r", by_edge["E0163"]["candidates"][0]["update_tree_text"])
        self.assertIn("if r < 6:\n    r' = r + 1", by_edge["E0169"]["candidates"][0]["update_tree_text"])
        self.assertEqual((3, [0, 7], 4), (by_edge["E0169"]["direct_regions"][0]["previous_output"]["value"], [item["value"] for item in by_edge["E0169"]["direct_regions"][0]["signals"] + by_edge["E0169"]["direct_regions"][0]["inputs"]], by_edge["E0169"]["direct_regions"][0]["terminal_output"]["value"]))
        self.assertEqual(4, len(by_edge["E0170"]["candidates"]))
        self.assertEqual(4, len(by_edge["E0050"]["candidates"]))
        c04_registration = by_edge["E0073"]
        self.assertEqual(1, len(c04_registration["signal_slots"]))
        self.assertEqual([1, 7, 0, 7], [item["value"] for item in c04_registration["direct_regions"][0]["raw_region"]])
        self.assertEqual([0, 7], [item["value"] for item in c04_registration["direct_regions"][0]["effective_region_snapshot"]["observation_items"]])
        self.assertEqual([], c04_registration["candidates"])
        self.assertEqual((0, 0), (c04_registration["direct_regions"][0]["previous_output"]["value"], c04_registration["direct_regions"][0]["terminal_output"]["value"]))
        self.assertIn((0, 1), {(sample["previous_output"]["value"], sample["terminal_output"]["value"]) for sample in c04_registration["direct_regions"]})
        resolution = c04_registration["hypothetical_candidate_resolution"]
        self.assertEqual([], resolution["intersection_candidates"])
        partitions = {partition["cycle_id"]: partition for partition in resolution["cycle_candidates"]}
        self.assertEqual({"C04", "C14"}, set(partitions))
        self.assertTrue(any("r' = r" in candidate["update_tree_text"] for candidate in partitions["C04"]["candidates"]))
        self.assertTrue(any("if r < 6:" in candidate["update_tree_text"] for candidate in partitions["C14"]["candidates"]))
        self.assertTrue(resolution["combined_sample_fit"]["triggered"])
        self.assertEqual("combined_sample_fit_failed", resolution["combined_sample_fit"]["status"])
        self.assertEqual([], resolution["combined_sample_fit"]["candidates"])
        self.assertEqual("combined_sample_fit_failed", resolution["selection"]["status"])
        self.assertNotIn("hypothetical_reconciliation", c04_registration)
        self.assertIn("unknown/unanchored_signal_context", by_edge["E0002"]["candidates"][0]["update_tree_text"])
        self.assertEqual("r_i[ngksi_uplink]' = i", by_edge["E0002"]["candidates"][0]["input_register_updates"][0]["update"]["text"])
        self.assertEqual("r' = r", by_edge["E0016"]["candidates"][0]["update_tree_text"])
        self.assertEqual("r_i[ngksi_uplink]' = r_i[ngksi_uplink]", by_edge["E0016"]["candidates"][0]["input_register_updates"][0]["update"]["text"])
        self.assertEqual(2, len(by_edge["E0083"]["candidates"]))
        self.assertTrue(all("r_i[ngksi_uplink] < 6" in candidate["update_tree_text"] for candidate in by_edge["E0083"]["candidates"]))
        e0083_reconciliation = by_edge["E0083"]["hypothetical_candidate_resolution"]
        self.assertEqual(
            "fit_cycle_minimal_candidates_then_combine_samples_if_intersection_empty",
            e0083_reconciliation["strategy"],
        )
        self.assertEqual([], e0083_reconciliation["intersection_candidates"])
        self.assertTrue(e0083_reconciliation["combined_sample_fit"]["triggered"])
        self.assertEqual(2, len(e0083_reconciliation["combined_sample_fit"]["candidates"]))
        c06_registration = by_edge["E0085"]
        self.assertEqual("hypothetical_candidate", c06_registration["candidates"][0]["candidate_grade"])
        self.assertEqual(2, len(c06_registration["candidates"]))
        self.assertTrue(all("if s0 == 1:" in candidate["update_tree_text"] for candidate in c06_registration["candidates"]))
        self.assertTrue(all("r' = r\n" not in candidate["update_tree_text"] for candidate in c06_registration["candidates"]))
        c06_smc_text = {candidate["update_tree_text"] for candidate in by_edge["E0103"]["candidates"]}
        self.assertEqual({"r' = r", "r' = 0", "r' = r_i[ngksi_uplink] - 7"}, c06_smc_text)
        self.assertEqual("relatively_stable_candidate", by_edge["E0103"]["candidates"][0]["candidate_grade"])
        self.assertEqual("r' = r", by_edge["E0114"]["candidates"][0]["update_tree_text"])
        self.assertEqual("r_i[ngksi_uplink]' = r_i[ngksi_uplink]", by_edge["E0114"]["candidates"][0]["input_register_updates"][0]["update"]["text"])
        self.assertEqual("r' = r", by_edge["E0172"]["candidates"][0]["update_tree_text"])
        self.assertEqual("hypothetical_candidate", by_edge["E0172"]["candidates"][0]["candidate_grade"])
        self.assertEqual("r' = r", by_edge["E0042"]["candidates"][0]["update_tree_text"])
        self.assertEqual(3, len(by_edge["E0097"]["candidates"]))
        self.assertIn("r' = r + 1", by_edge["E0097"]["candidates"][1]["update_tree_text"])
        self.assertIn("r_i[ngksi_uplink] < 6", by_edge["E0098"]["candidates"][0]["update_tree_text"])
        self.assertEqual("r' = r", by_edge["E0019"]["candidates"][0]["update_tree_text"])
        c09_registration = by_edge["E0001"]
        self.assertEqual(3, len(c09_registration["candidates"]))
        self.assertTrue(all(candidate["candidate_grade"] == "hypothetical_candidate" for candidate in c09_registration["candidates"]))
        self.assertTrue(any("r_i[ngksi_uplink] - 7" in candidate["update_tree_text"] for candidate in c09_registration["candidates"]))
        self.assertEqual("r' = r", by_edge["E0076"]["candidates"][0]["update_tree_text"])
        self.assertIn("unknown/unanchored_signal_context", by_edge["E0086"]["candidates"][0]["update_tree_text"])
        self.assertEqual("r' = r", by_edge["E0112"]["candidates"][0]["update_tree_text"])
        self.assertEqual("r' = r", by_edge["E0127"]["candidates"][0]["update_tree_text"])
        self.assertEqual("relatively_stable_candidate", by_edge["E0127"]["candidates"][0]["candidate_grade"])
        self.assertIn("if r < 6:\n    r' = r + 1", by_edge["E0133"]["candidates"][0]["update_tree_text"])
        self.assertIn("unknown/unanchored_signal_context", by_edge["E0134"]["candidates"][0]["update_tree_text"])
        self.assertEqual("r' = r", by_edge["E0196"]["candidates"][0]["update_tree_text"])
        self.assertEqual("r' = r", by_edge["E0051"]["candidates"][0]["update_tree_text"])
        self.assertIn("unknown/unanchored_signal_context", by_edge["E0062"]["candidates"][0]["update_tree_text"])
        self.assertEqual("r' = r", by_edge["E0064"]["candidates"][0]["update_tree_text"])
        self.assertEqual(3, len(by_edge["E0109"]["candidates"]))
        self.assertTrue(any("r_i[ngksi_uplink] - 6" in candidate["update_tree_text"] for candidate in by_edge["E0109"]["candidates"]))
        self.assertEqual(3, len(by_edge["E0110"]["candidates"]))
        self.assertTrue(any("r_i[ngksi_uplink] + 1" in candidate["update_tree_text"] for candidate in by_edge["E0110"]["candidates"]))
        self.assertEqual("r' = r", by_edge["E0124"]["candidates"][0]["update_tree_text"])
        self.assertEqual("r' = r", by_edge["E0174"]["candidates"][0]["update_tree_text"])
        self.assertTrue(any("r' = r - 1" in candidate["update_tree_text"] for candidate in by_edge["E0181"]["candidates"]))
        self.assertIn("unknown/unanchored_signal_context", by_edge["E0182"]["candidates"][0]["update_tree_text"])
        self.assertEqual(3, len(by_edge["E0193"]["candidates"]))
        self.assertTrue(any("r_i[ngksi_uplink] + 1" in candidate["update_tree_text"] for candidate in by_edge["E0203"]["candidates"]))

    def test_v3_last_write_projection_preserves_raw_observations(self) -> None:
        first_signal = _signal(1, symbol="registrationRequestGUTI")
        second_signal = _signal(0, symbol="registrationRequest")
        first_input = {**_input(7, symbol="registrationRequestGUTI"), "input_register_id": "ngksi_uplink", "definition_id": "guti"}
        second_input = {**_input(7, symbol="registrationRequest"), "input_register_id": "ngksi_uplink", "definition_id": "regular"}
        snapshot = effective_snapshot([first_signal, first_input, second_signal, second_input])
        self.assertEqual([0], [item["value"] for item in snapshot["signals"]])
        self.assertEqual([7], [item["value"] for item in snapshot["numeric_inputs"]])
        self.assertEqual("registrationRequest", snapshot["numeric_inputs"][0]["input_symbol"])
        self.assertEqual(2, snapshot["overwritten_count"])
        self.assertEqual(1, len(snapshot["numeric_inputs"][0]["overwrites"]))

    def test_v3_input_register_assignment_hold_and_unobservable(self) -> None:
        event = {"numeric_inputs": [{"input_register_id": "ngksi_uplink", "definition_id": "reg", "input_symbol": "registrationRequest", "field_path": "ue_side.fields.ksi", "value": 7}]}
        updates = v3_input_register_updates(event, ["ngksi_uplink", "other"], {"ngksi_uplink"})
        self.assertEqual("input_assignment", updates[0]["update"]["kind"])
        self.assertEqual("unobservable_input_register", updates[1]["observability"])
        held = v3_input_register_updates({"numeric_inputs": []}, ["ngksi_uplink"], {"ngksi_uplink"})
        self.assertEqual("input_hold", held[0]["update"]["kind"])

    def test_v3_numeric_selector_order_and_multiple_registers(self) -> None:
        definitions = [
            {"id": "first", "input_register_id": "first_register", "path": "ue_side.fields.first", "value_type": "integer", "match": {"input_symbols": ["*"]}, "phase": "before_register_updates"},
            {"id": "second", "input_register_id": "second_register", "path": "ue_side.fields.second", "value_type": "integer", "match": {"input_symbols": ["registrationRequest", "serviceRequest"]}, "phase": "before_register_updates"},
        ]
        validate_numeric_input_definitions({"numeric_input_definitions": definitions})
        record = {"_trace_line": 1, "step_id": 4, "ue_side": {"fields": {"first": 7, "second": 3}}}
        observations = numeric_input_observations(record, {"logical_input": "registrationRequest"}, definitions)
        self.assertEqual(["first_register", "second_register"], [item["input_register_id"] for item in observations])
        updates = v3_input_register_updates({"numeric_inputs": observations}, ["first_register", "second_register"], {"first_register", "second_register"})
        self.assertEqual(["input_assignment", "input_assignment"], [item["update"]["kind"] for item in updates])

    def test_v3_unanchored_signal_default_uses_s_equals_one(self) -> None:
        tree = default_signal_tree([{
            "signal_id": "initial", "field_path": "ue_side.fields.initial", "input_symbol": "registrationRequest", "occurrence_index": 0,
        }])
        self.assertEqual(1, tree["guard"]["value"])
        self.assertEqual("unanchored_signal_context", tree["true"]["reason"])
        self.assertEqual("r' = r", tree_text(tree)[tree_text(tree).rfind("\n") + 1:].strip())

    def test_v3_unanchored_event_assigns_local_occurrence_indexes(self) -> None:
        signal = _signal(1)
        numeric = _input(7)
        signal.pop("occurrence_index")
        numeric.pop("occurrence_index")
        event = {
            "sequence_line": 9, "repetition": 2,
            "edge": {"edge_id": "E-unanchored"},
            "signals": [signal], "numeric_inputs": [numeric],
        }

        sample = standalone_event_sample(event)

        self.assertNotIn("occurrence_index", event["signals"][0])
        self.assertEqual(0, sample["signals"][0]["occurrence_index"])
        self.assertEqual(0, sample["inputs"][0]["occurrence_index"])
        self.assertEqual("initial", stable_slots([sample], "signal")[0]["signal_id"])

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
