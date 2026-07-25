import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "find_shortest_paths.py"
SPEC = importlib.util.spec_from_file_location("find_shortest_paths", SCRIPT)
assert SPEC and SPEC.loader
FIND_PATHS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIND_PATHS)


class InputSequencePresentationTests(unittest.TestCase):
    def test_reachable_path_includes_space_delimited_input_sequence(self) -> None:
        model = {
            "states": ["s0", "s1", "s2"],
            "outgoing": {
                "s0": [{"src": "s0", "dst": "s1", "input": "registrationRequest", "output": "identityRequest"}],
                "s1": [{"src": "s1", "dst": "s2", "input": "identityResponse", "output": "authenticationRequest"}],
            },
        }

        result = FIND_PATHS.build_results(model, "s0", ["s2"])[0]

        self.assertEqual(
            "registrationRequest identityResponse",
            result["input_sequence_text"],
        )

    def test_start_state_uses_explicit_empty_sequence_marker(self) -> None:
        model = {"states": ["s0"], "outgoing": {}}

        result = FIND_PATHS.build_results(model, "s0", ["s0"])[0]

        self.assertEqual([], result["input_sequence"])
        self.assertEqual("(空序列)", result["input_sequence_text"])


if __name__ == "__main__":
    unittest.main()
