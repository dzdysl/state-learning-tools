from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from candidates import generate_simple_scalar_candidates
from config import load_config
from contracts import RegisterInferenceError
from fit import fit_scalar_edge_candidates
from prepare import prepare


EXAMPLE_DIR = MODULE_DIR / "examples"
SCRIPT = MODULE_DIR / "analyze_register_machine.py"


class RegisterInferenceTests(unittest.TestCase):
    def test_prepare_preserves_repeated_observations_and_dot_identity(self) -> None:
        config_path = EXAMPLE_DIR / "simple-register-inference.yaml"
        prepared = prepare(load_config(config_path), config_path)
        self.assertEqual(4, len(prepared["observations"]))
        self.assertEqual(["E0001", "E0001", "E0002", "E0002"], [item["edge_id"] for item in prepared["observations"]])
        self.assertEqual([2, 3, 2, 3], [item["iteration"] for item in prepared["observations"]])
        self.assertEqual([], prepared["anomalies"])

    def test_candidates_and_z3_fit_choose_increment_and_input_copy(self) -> None:
        config_path = EXAMPLE_DIR / "simple-register-inference.yaml"
        config = load_config(config_path)
        prepared = prepare(config, config_path)
        candidates = generate_simple_scalar_candidates(prepared, config["candidate_generator"]["priority"])
        fitted = fit_scalar_edge_candidates(prepared, candidates)
        self.assertEqual("sat", fitted["status"])
        selected = {item["edge_id"]: item["selected_candidate"] for item in fitted["results"]}
        self.assertEqual({"kind": "add_constant", "value": 1}, selected["E0001"])
        self.assertEqual({"kind": "copy_input", "input": "i0"}, selected["E0002"])

    def test_fit_reports_unsat_when_external_candidate_cannot_explain_samples(self) -> None:
        config_path = EXAMPLE_DIR / "simple-register-inference.yaml"
        prepared = prepare(load_config(config_path), config_path)
        candidates = {
            "schema_version": 1,
            "groups": [{
                "edge_id": "E0001", "register": "r0", "observation_ids": ["O00001", "O00002"],
                "candidates": [{"kind": "identity"}],
            }],
        }
        result = fit_scalar_edge_candidates(prepared, candidates)
        self.assertEqual("unsat", result["status"])
        check = result["results"][0]["candidate_checks"][0]
        self.assertEqual(["O00001", "O00002"], check["failed_observation_ids"])

    def test_unknown_dot_transition_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("simple.dot", "simple-register-inference.yaml"):
                (root / name).write_text((EXAMPLE_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")
            (root / "trace.jsonl").write_text(json.dumps({
                "transition": {"source": "s0", "target": "s1", "input": "wrong"},
                "register": {"before": 0, "after": 1}, "inputs": {"i0": 1},
            }) + "\n", encoding="utf-8")
            config_text = (root / "simple-register-inference.yaml").read_text(encoding="utf-8").replace("simple-trace.jsonl", "trace.jsonl")
            (root / "config.yaml").write_text(config_text, encoding="utf-8")
            config_path = root / "config.yaml"
            with self.assertRaisesRegex(RegisterInferenceError, "absent from DOT"):
                prepare(load_config(config_path), config_path)

    def test_run_cli_writes_three_stable_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "output"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "run", "--config", str(EXAMPLE_DIR / "simple-register-inference.yaml"), "--output-dir", str(output_dir)],
                capture_output=True, text=True, encoding="utf-8", check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(["candidates.json", "fit.json", "prepared.json"], sorted(path.name for path in output_dir.iterdir()))
            self.assertEqual("sat", json.loads((output_dir / "fit.json").read_text(encoding="utf-8"))["status"])


if __name__ == "__main__":
    unittest.main()
