from __future__ import annotations

import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "experiments" / "prepare_register_inference_trace.py"
SPEC = importlib.util.spec_from_file_location("prepare_register_inference_trace", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


def record(sequence_id: int, inputs: list[str], current_input: str) -> dict:
    return {
        "sequence_id": sequence_id,
        "sequence_inputs": inputs,
        "abstract_io": {"input": current_input, "output": "out"},
    }


class PrepareRegisterInferenceTraceTests(unittest.TestCase):
    def write_fixture(self, root: Path, *, use_cycles: bool = True, bad_input: bool = False) -> tuple[Path, Path, Path, Path, Path]:
        source = root / "raw.jsonl"
        rows = [
            record(11, ["a"], "a"),
            record(11, ["a", "b"], "wrong" if bad_input else "b"),
            record(19, ["c"], "c"),
        ]
        source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        sequence = root / "input.seq"
        sequence.write_text("a b\nc\n", encoding="utf-8")
        cover = root / "cover.json"
        key = "cycles" if use_cycles else "routes"
        cover.write_text(json.dumps({"sequence_export": {key: [
            {"cycle_id": "C1", "variants": [{"line_number": 1}]},
            {"cycle_id": "C2", "variants": [{"line_number": 2}]},
        ]}}), encoding="utf-8")
        evidence = root / "evidence" / "statelearner_trace.jsonl"
        config = root / "config.yaml"
        config.write_text(
            "schema_version: 3\ninputs:\n"
            "  trace: evidence/statelearner_trace.jsonl\n"
            "  cycle_cover: cover.json\n"
            "  sequence_file: input.seq\n"
            "analysis:\n  cycle_ids: [C1, C2]\n",
            encoding="utf-8",
        )
        return source, sequence, cover, evidence, config

    def test_materializes_byte_identical_trace_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _, _, evidence, config = self.write_fixture(root)
            manifest = root / "derived" / "trace-materialization.json"
            self.assertEqual(prepare.main([
                "--config", str(config), "--source-trace", str(source),
                "--evidence-trace", str(evidence), "--manifest", str(manifest),
            ]), 0)
            self.assertEqual(source.read_bytes(), evidence.read_bytes())
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["contract"]["sequence_export_key"], "sequence_export.cycles")
            self.assertEqual(payload["contract"]["record_count"], 3)
            self.assertEqual(payload["contract"]["selected_variant_count"], 2)

    def test_rejects_legacy_routes_interface(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _, _, evidence, config = self.write_fixture(root, use_cycles=False)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                prepare.main([
                    "--config", str(config), "--source-trace", str(source),
                    "--evidence-trace", str(evidence), "--manifest", str(root / "manifest.json"),
                ])

    def test_rejects_step_input_that_inference_cannot_align(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _, _, evidence, config = self.write_fixture(root, bad_input=True)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                prepare.main([
                    "--config", str(config), "--source-trace", str(source),
                    "--evidence-trace", str(evidence), "--manifest", str(root / "manifest.json"),
                ])


if __name__ == "__main__":
    unittest.main()
