from __future__ import annotations

import importlib.util
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "trace_binary_splits.py"
SPEC = importlib.util.spec_from_file_location("trace_binary_splits", SCRIPT)
assert SPEC and SPEC.loader
TRACE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRACE)


def difference(symbol: str, upstream: tuple[str, str]) -> dict:
    canonical = TRACE.canonical_pair(*upstream)
    return {
        "index": 0,
        "input": symbol,
        "abbreviation": symbol,
        "upstream_pair": list(canonical),
        "child_views": [
            {"child": None, "target_label": upstream[0], "transitions": []},
            {"child": None, "target_label": upstream[1], "transitions": []},
        ],
    }


def pair(
    round_index: int,
    parent: str,
    first: str,
    second: str,
    differences: list[dict],
    classification: str,
) -> dict:
    children = [
        {"name": name, "states": [f"s_{name}"], "signature": []}
        for name in TRACE.canonical_pair(first, second)
    ]
    differences = copy.deepcopy(differences)
    for item in differences:
        for child, view in zip(children, item["child_views"]):
            view["child"] = child["name"]
    return {
        "round": round_index,
        "parent": parent,
        "pair": "/".join(child["name"] for child in children),
        "children": children,
        "difference_count": len(differences),
        "differences": differences,
        "upstream_pairs": sorted({tuple(item["upstream_pair"]) for item in differences}),
        "classification": classification,
    }


class BinaryBacktraceTests(unittest.TestCase):
    def test_default_cli_policy_is_strict(self) -> None:
        argv = [
            "trace_binary_splits.py",
            "--dot",
            "model.dot",
            "--output-dir",
            "out",
        ]
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(TRACE.parse_args().entry_policy, "strict")

    def test_later_entry_covers_earlier_strict_pair(self) -> None:
        earlier = pair(
            1, "P0", "A", "B",
            [difference("x", ("I", "J"))],
            "strict",
        )
        later = pair(
            2, "P1", "L", "R",
            [difference("y", ("A", "B"))],
            "strict",
        )
        graph = TRACE.analyze_graph([earlier, later], "strict", None, None)
        self.assertEqual(
            [item["pair"] for item in graph["independent"]],
            ["/".join(TRACE.canonical_pair("L", "R"))],
        )
        self.assertEqual(later["role"], "independent_entry")
        self.assertEqual(earlier["role"], "covered")

    def test_multi_signal_intermediate_merges_same_upstream_pair(self) -> None:
        target = pair(
            1, "P0", "A", "B",
            [difference("z", ("I", "J"))],
            "strict",
        )
        intermediate = pair(
            2, "P1", "L", "R",
            [difference("x", ("A", "B")), difference("y", ("A", "B"))],
            "convergent_unique",
        )
        root = pair(
            3, "P2", "U", "V",
            [difference("q", ("L", "R"))],
            "strict",
        )
        graph = TRACE.analyze_graph([target, intermediate, root], "strict", None, None)
        merged = [
            edge for edge in graph["edges"]
            if edge["from"] == TRACE.node_key(intermediate)
        ]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["signals"], "x/y")
        self.assertEqual(intermediate["role"], "covered")

    def test_multi_upstream_intermediate_branches(self) -> None:
        intermediate = pair(
            2, "P1", "L", "R",
            [difference("x", ("A", "B")), difference("y", ("C", "D"))],
            "branching",
        )
        root = pair(
            3, "P2", "U", "V",
            [difference("q", ("L", "R"))],
            "strict",
        )
        graph = TRACE.analyze_graph([intermediate, root], "strict", None, None)
        branches = [
            edge for edge in graph["edges"]
            if edge["from"] == TRACE.node_key(intermediate)
        ]
        self.assertEqual(len(branches), 2)
        self.assertEqual(
            {tuple(edge["to_terminal"][1]) for edge in branches},
            {
                TRACE.canonical_pair("A", "B"),
                TRACE.canonical_pair("C", "D"),
            },
        )
        self.assertEqual(intermediate["role"], "covered")

    def test_flowchart_uses_parent_tables_and_exclusion_rows(self) -> None:
        strict = pair(
            1, "P", "A1", "A2",
            [difference("x", ("A", "D"))],
            "strict",
        )
        excluded = pair(
            1, "P", "A1", "A3",
            [difference("x", ("A", "D")), difference("y", ("A", "S"))],
            "branching",
        )
        strict.update({"key": "r1:P:A1/A2", "role": "independent_entry"})
        excluded.update({"key": "r1:P:A1/A3", "role": "unvisited_non_entry"})
        payload = {
            "source_dot": "hypothesis_test.dot",
            "pairs": [strict, excluded],
            "rounds": [{
                "round": 1,
                "splits": [{
                    "parent": "P",
                    "children": [
                        {"name": "A1", "states": ["s1"]},
                        {"name": "A2", "states": ["s2"]},
                        {"name": "A3", "states": ["s3"]},
                    ],
                }],
            }],
            "terminal_ids": {"stage0:A/D": "K1"},
            "edges": [{
                "from": strict["key"],
                "to_terminal": "stage0:A/D",
                "signals": "x",
                "inputs": ["x"],
            }],
            "highlighted_node_keys": [strict["key"]],
            "highlighted_edges": [{"from": strict["key"], "to": "stage0:A/D"}],
            "input_order": ["x", "y"],
        }
        old_abbr = dict(TRACE.ABBR)
        TRACE.ABBR.update({"x": "X", "y": "Y"})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "flow.dot"
                TRACE.write_flowchart(payload, output)
                text = output.read_text(encoding="utf-8")
        finally:
            TRACE.ABBR.clear()
            TRACE.ABBR.update(old_abbr)
        self.assertIn("rankdir=BT", text)
        self.assertIn("shape=plain", text)
        self.assertIn("父类 P", text)
        self.assertIn("独立入口对子", text)
        self.assertIn("非入口对子", text)
        self.assertNotIn("A1/A2 [shape=", text)

    def test_terminal_audit_continues_after_equal_immediate_output(self) -> None:
        def edge(src: str, symbol: str, output: str, dst: str) -> dict:
            return {"src": src, "input": symbol, "output": output, "dst": dst}

        outgoing = {
            "sL": {
                "trigger": edge("sL", "trigger", "same", "uL"),
                "registrationRequest": edge("sL", "registrationRequest", "same", "sL"),
            },
            "sR": {
                "trigger": edge("sR", "trigger", "same", "uR"),
                "registrationRequest": edge("sR", "registrationRequest", "same", "sR"),
            },
            "uL": {
                "trigger": edge("uL", "trigger", "same", "uL"),
                "registrationRequest": edge(
                    "uL", "registrationRequest", "authenticationRequest", "uL"
                ),
            },
            "uR": {
                "trigger": edge("uR", "trigger", "same", "uR"),
                "registrationRequest": edge(
                    "uR", "registrationRequest", "null_action", "uR"
                ),
            },
        }
        model = {
            "input_order": ["trigger", "registrationRequest"],
            "outgoing": outgoing,
        }
        item = pair(
            1, "P", "L", "R",
            [difference("trigger", ("A", "X"))],
            "strict",
        )
        item["children"][0]["states"] = ["sL"]
        item["children"][1]["states"] = ["sR"]
        graph = {
            "edges": [{
                "from": TRACE.node_key(item),
                "to_terminal": (0, ("A", "X")),
                "signals": "T",
                "inputs": ["trigger"],
            }]
        }
        audit = TRACE.build_terminal_audits(graph, [item], model)[0]
        state_pair = audit["inputs"][0]["state_pairs"][0]
        self.assertTrue(state_pair["immediate_output_equal"])
        suffix = state_pair["shortest_observable_suffix"]
        self.assertEqual(suffix["sequence"], ["registrationRequest"])
        self.assertEqual(
            suffix["outputs"],
            [
                {"state": "uL", "output": "authenticationRequest"},
                {"state": "uR", "output": "null_action"},
            ],
        )

    def test_entry_scoped_branches_survive_child_order_swap(self) -> None:
        def edge(src: str, symbol: str, output: str, dst: str) -> dict:
            return {"src": src, "input": symbol, "output": output, "dst": dst}

        target = pair(
            1, "P0", "A", "B",
            [difference("y", ("I", "J"))],
            "strict",
        )
        target["children"][0]["states"] = ["uA"]
        target["children"][1]["states"] = ["uB"]
        target["key"] = TRACE.serialize_key(TRACE.node_key(target))
        target["differences"][0]["child_views"][0]["transitions"] = [
            edge("uA", "y", "oa", "vA")
        ]
        target["differences"][0]["child_views"][1]["transitions"] = [
            edge("uB", "y", "ob", "vB")
        ]

        root = pair(
            2, "P1", "L", "R",
            [difference("x", ("B", "A"))],
            "strict",
        )
        root["children"][0]["states"] = ["sA"]
        root["children"][1]["states"] = ["sB"]
        root["key"] = TRACE.serialize_key(TRACE.node_key(root))
        root["differences"][0]["child_views"][0]["transitions"] = [
            edge("sA", "x", "same", "uB")
        ]
        root["differences"][0]["child_views"][1]["transitions"] = [
            edge("sB", "x", "same", "uA")
        ]
        graph = {
            "edges": [
                {
                    "from": TRACE.node_key(root),
                    "to": TRACE.node_key(target),
                    "signals": "x",
                    "inputs": ["x"],
                },
                {
                    "from": TRACE.node_key(target),
                    "to_terminal": (0, ("I", "J")),
                    "signals": "y",
                    "inputs": ["y"],
                },
            ]
        }
        model = {
            "outgoing": {
                "sA": {"x": edge("sA", "x", "same", "uB")},
                "sB": {"x": edge("sB", "x", "same", "uA")},
                "uA": {"y": edge("uA", "y", "oa", "vA")},
                "uB": {"y": edge("uB", "y", "ob", "vB")},
            }
        }
        paths = TRACE.build_entry_paths(graph, [root], [root, target], model)
        variant = paths[0]["paths"][0]["trace_variants"][0]
        self.assertEqual(
            variant["branches"]["A"]["trajectories"][0]["states"],
            ["sA", "uB", "vB"],
        )
        self.assertEqual(
            variant["branches"]["B"]["trajectories"][0]["states"],
            ["sB", "uA", "vA"],
        )

    def test_multi_member_and_multi_input_variants_expand_without_cross_pairs(self) -> None:
        def edge(src: str, symbol: str, dst: str) -> dict:
            return {"src": src, "input": symbol, "output": "same", "dst": dst}

        entry = pair(
            1,
            "P",
            "A",
            "B",
            [difference("x", ("I", "J")), difference("y", ("I", "J"))],
            "convergent_unique",
        )
        entry["children"][0]["states"] = ["s1", "s2"]
        entry["children"][1]["states"] = ["s3"]
        entry["key"] = TRACE.serialize_key(TRACE.node_key(entry))
        for diff in entry["differences"]:
            symbol = diff["input"]
            diff["child_views"][0]["transitions"] = [
                edge("s1", symbol, f"u1{symbol}"),
                edge("s2", symbol, f"u2{symbol}"),
            ]
            diff["child_views"][1]["transitions"] = [
                edge("s3", symbol, f"u3{symbol}")
            ]
        model = {
            "outgoing": {
                state: {
                    symbol: edge(state, symbol, f"u{index}{symbol}")
                    for symbol in ("x", "y")
                }
                for index, state in enumerate(("s1", "s2", "s3"), 1)
            }
        }
        graph = {
            "edges": [{
                "from": TRACE.node_key(entry),
                "to_terminal": (0, ("I", "J")),
                "signals": "x/y",
                "inputs": ["x", "y"],
            }]
        }
        paths = TRACE.build_entry_paths(graph, [entry], [entry], model)
        variants = paths[0]["paths"][0]["trace_variants"]
        self.assertEqual(
            [item["input_sequence"] for item in variants],
            [["x"], ["y"]],
        )
        for variant in variants:
            self.assertEqual(
                sum(
                    len(branch["trajectories"])
                    for branch in variant["branches"].values()
                ),
                3,
            )

    def test_schema_payload_contains_no_directional_fields(self) -> None:
        forbidden = {"left", "right", "left_state", "right_state", "left_output", "right_output"}

        def assert_clean(value: object) -> None:
            if isinstance(value, dict):
                self.assertTrue(forbidden.isdisjoint(value))
                for child in value.values():
                    assert_clean(child)
            elif isinstance(value, list):
                for child in value:
                    assert_clean(child)

        sample = pair(
            1, "P", "A", "B",
            [difference("x", ("I", "J"))],
            "strict",
        )
        assert_clean(json.loads(json.dumps(sample)))


if __name__ == "__main__":
    unittest.main()
