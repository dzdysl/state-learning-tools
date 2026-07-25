from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "analyze_refinement.py"
SPEC = importlib.util.spec_from_file_location("analyze_refinement", SCRIPT)
assert SPEC and SPEC.loader
REFINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REFINE)


def sample_payload() -> dict:
    return {
        "source_dot": "hypothesis_7.dot",
        "states": ["s0", "s1", "s2", "s3"],
        "rounds": [
            {
                "round": 1,
                "groups": {
                    "A1": ["s0", "s1"],
                    "D1": ["s2"],
                    "S1": ["s3"],
                },
                "class_count": 3,
                "converged": False,
            },
            {
                "round": 2,
                "groups": {
                    "A1": ["s0"],
                    "A2": ["s1"],
                    "D2": ["s2"],
                    "S1": ["s3"],
                },
                "class_count": 4,
                "converged": False,
            },
            {
                "round": 3,
                "groups": {
                    "A1": ["s0"],
                    "A2": ["s1"],
                    "D2": ["s2"],
                    "S1": ["s3"],
                },
                "class_count": 4,
                "converged": True,
            },
        ],
    }


class RoundRefinementFlowchartTests(unittest.TestCase):
    def test_inheritance_classifies_split_renumber_and_unchanged(self) -> None:
        effective, relations, spans = REFINE.build_refinement_flow(sample_payload())
        self.assertEqual([item["round"] for item in effective], [1, 2])
        self.assertEqual(len(relations), 4)
        by_edge = {
            (item["parent"], item["child"]): item["kind"] for item in relations
        }
        self.assertEqual(by_edge[("A1", "A1")], "split")
        self.assertEqual(by_edge[("A1", "A2")], "split")
        self.assertEqual(by_edge[("D1", "D2")], "renumber")
        self.assertEqual(by_edge[("S1", "S1")], "unchanged")
        self.assertEqual(spans[(1, "A1")], 2)
        self.assertEqual(spans[(1, "D1")], 1)

    def test_each_child_has_exactly_one_parent(self) -> None:
        before = {
            "round": 1,
            "groups": {"A1": ["s0"], "D1": ["s1"]},
        }
        after = {
            "round": 2,
            "groups": {"A1": ["s0", "s1"]},
        }
        with self.assertRaisesRegex(ValueError, "2 parents"):
            REFINE.build_round_relations(before, after)

    def test_round_partition_rejects_duplicate_or_missing_states(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicates"):
            REFINE.validate_round_groups(
                ["s0", "s1"],
                {"A1": ["s0"], "D1": ["s0"]},
                1,
            )

    def test_flowchart_matches_h33_table_layout_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "flow.dot"
            node_count = REFINE.write_flowchart(sample_payload(), output)
            text = output.read_text(encoding="utf-8")
        self.assertEqual(node_count, 7)
        self.assertIn('label="H7 第1–2轮状态拆分流程图"', text)
        self.assertIn('round_1 [shape=box, style="rounded,filled"', text)
        self.assertIn("r1 [label=<", text)
        self.assertIn('PORT="A1" COLSPAN="2"', text)
        self.assertIn("r1:A1:s -> r2:A1:n", text)
        self.assertIn('color="#2E8B57", penwidth=2.5', text)
        self.assertIn('color="#E67E22", penwidth=2.2', text)
        self.assertIn('color="#9AA0A6", penwidth=1.15', text)
        self.assertIn("横向顺序：A → D → N → NG → S → R → X", text)
        self.assertNotIn("subgraph cluster_round", text)
        self.assertNotIn('label="拆分"', text)
        first_table = text[text.index("r1 [label=<"):text.index("r2 [label=<")]
        self.assertLess(first_table.index('PORT="A1"'), first_table.index('PORT="D1"'))
        self.assertLess(first_table.index('PORT="D1"'), first_table.index('PORT="S1"'))

    def test_flowchart_only_is_explicit_cli_option(self) -> None:
        base = [
            "analyze_refinement.py",
            "--dot",
            "model.dot",
            "--output-dir",
            "out",
        ]
        with mock.patch.object(sys, "argv", base):
            self.assertFalse(REFINE.parse_args().flowchart_only)
        with mock.patch.object(sys, "argv", [*base, "--flowchart-only"]):
            self.assertTrue(REFINE.parse_args().flowchart_only)

    def test_canonical_refinement_json_is_hash_checked(self) -> None:
        payload = {
            "kind": "mealy_refinement",
            "source_sha256": "abc123",
            "states": ["s0"],
            "input_order": ["x"],
            "rounds": [{"round": 1}],
        }
        model = {
            "sha256": "abc123",
            "states": ["s0"],
            "input_order": ["x"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "refinement.json"
            path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            loaded = REFINE.load_refinement_payload(
                path,
                Path("model.dot"),
                model,
            )
            self.assertEqual(loaded["source_sha256"], "abc123")
            model["sha256"] = "different"
            with self.assertRaisesRegex(ValueError, "does not match"):
                REFINE.load_refinement_payload(
                    path,
                    Path("model.dot"),
                    model,
                )


if __name__ == "__main__":
    unittest.main()
