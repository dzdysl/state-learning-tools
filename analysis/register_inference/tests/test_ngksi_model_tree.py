from __future__ import annotations

import sys
import unittest
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
EXPERIMENT_DIR = MODULE_DIR / "experiments"
sys.path.insert(0, str(EXPERIMENT_DIR))

from learn_cycle_ngksi_model_tree import fit_model_tree, tree_text


class ModelTreeTests(unittest.TestCase):
    def test_one_split_finds_increment_then_reset(self) -> None:
        samples = [{"before": value, "after": (value + 1) if value < 6 else 0} for value in range(7)]
        tree = fit_model_tree(samples, max_depth=1)
        self.assertIsNotNone(tree)
        assert tree is not None
        self.assertEqual("split", tree["kind"])
        self.assertEqual(6, tree["threshold"])
        self.assertEqual("add_constant", tree["true"]["formula"]["kind"])
        self.assertEqual(1, tree["true"]["formula"]["value"])
        self.assertEqual("constant", tree["false"]["formula"]["kind"])
        self.assertIn("ngksi_before < 6", tree_text(tree))

    def test_depth_zero_rejects_a_non_leaf_rule(self) -> None:
        samples = [{"before": value, "after": (value + 1) if value < 2 else 0} for value in range(3)]
        self.assertIsNone(fit_model_tree(samples, max_depth=0))


if __name__ == "__main__":
    unittest.main()
