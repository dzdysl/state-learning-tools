import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "render_graphviz.py"
SPEC = importlib.util.spec_from_file_location("render_graphviz", SCRIPT)
assert SPEC and SPEC.loader
render_graphviz = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render_graphviz)


class TraceRenderingTests(unittest.TestCase):
    def test_trace_edges_survive_smp_simplification_and_are_annotated(self) -> None:
        trace = [
            {"step": 1, "src": "s0", "dst": "s1", "input": "x", "output": "one"},
            {"step": 2, "src": "s1", "dst": "s1", "input": "y", "output": "null_action"},
            {"step": 3, "src": "s1", "dst": "s0", "input": "z", "output": "null_action"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.dot"
            destination = root / "derived.dot"
            source.write_text(
                "digraph g {\n"
                "  s0 [shape=\"circle\" label=\"s0\"];\n"
                "  s1 [shape=\"circle\" label=\"s1\"];\n"
                "  s2 [shape=\"circle\" label=\"s2\"];\n"
                "  s0 -> s1 [label=\"x / one\"];\n"
                "  s1 -> s1 [label=\"y / null_action\"];\n"
                "  s1 -> s0 [label=\"z / null_action\"];\n"
                "  s2 -> s2 [label=\"ordinary / null_action\"];\n"
                "}\n",
                encoding="utf-8",
            )
            render_graphviz.simplify_dot_file(
                source,
                destination,
                delete_self_loops=True,
                delete_to_s0=True,
                delete_null_sink_incoming=True,
                merge_transitions=True,
                trace=trace,
                annotations={2: "Δ observed: run-1=other"},
            )
            render_graphviz.apply_trace_node_styles(
                destination, trace, {2: "Δ observed: run-1=other"}, render_graphviz.TRACE_COLOR
            )
            derived = destination.read_text(encoding="utf-8")
            self.assertIn('trace_step="1"', derived)
            self.assertIn('trace_step="2"', derived)
            self.assertIn('trace_step="3"', derived)
            self.assertIn('[2] y / null_action\\nΔ observed mismatch', derived)
            self.assertIn('step 2: Δ observed: run-1=other', derived)
            self.assertNotIn("ordinary / null_action", derived)
            self.assertIn("trace_legend", derived)

    def test_observed_output_lengths_and_differences_are_validated(self) -> None:
        trace = [{"step": 1, "src": "s0", "dst": "s1", "input": "x", "output": "one"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.json"
            path.write_text(
                json.dumps(
                    {
                        "kind": "mealy_trace_observations",
                        "observed_runs": [{"name": "run-1", "output_sequence": ["other"]}],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(render_graphviz.load_observed_outputs(path, trace), {1: "Δ observed: run-1=other"})
            path.write_text(
                json.dumps(
                    {
                        "kind": "mealy_trace_observations",
                        "observed_runs": [{"name": "run-1", "output_sequence": []}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                render_graphviz.load_observed_outputs(path, trace)

    def test_trace_hash_must_match_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.dot"
            trace_path = root / "trace.json"
            source.write_text("digraph g {}\n", encoding="utf-8")
            trace_path.write_text(
                json.dumps(
                    {
                        "kind": "mealy_simulation",
                        "source_sha256": hashlib.sha256(b"different").hexdigest(),
                        "trace": [{"step": 1, "src": "s0", "dst": "s1", "input": "x", "output": "one"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                render_graphviz.load_trace(trace_path, source)


if __name__ == "__main__":
    unittest.main()
