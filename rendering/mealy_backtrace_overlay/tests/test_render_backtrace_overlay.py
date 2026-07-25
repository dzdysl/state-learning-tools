from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "render_backtrace_overlay.py"
SPEC = importlib.util.spec_from_file_location("render_backtrace_overlay", SCRIPT)
assert SPEC and SPEC.loader
OVERLAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OVERLAY)


class BacktraceOverlayTests(unittest.TestCase):
    def test_palette_is_stable_for_first_two_entries(self) -> None:
        self.assertEqual(OVERLAY.color_for_entry("B01"), "#D81B60")
        self.assertEqual(OVERLAY.color_for_entry("B02"), "#1E88E5")
        self.assertEqual(
            OVERLAY.color_for_entry("B17"),
            OVERLAY.color_for_entry("B01"),
        )

    def test_overlay_injection_is_byte_reversible_and_path_only(self) -> None:
        original = (
            '<?xml version="1.0" encoding="UTF-8"?>\r\n'
            '<svg viewBox="0 0 10 10">\r\n'
            '<g id="graph0" class="graph" transform="scale(1 1) rotate(0) translate(0 0)">\r\n'
            '</g>\r\n</svg>\r\n'
        ).encode("utf-8")
        fragment = OVERLAY.overlay_fragment(
            [{"geometry_id": "G001", "d": "M0,0C1,1 2,2 3,3", "method": "test"}],
            "#D81B60",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "base.svg"
            path.write_bytes(original)
            output = OVERLAY.inject_overlay(path, fragment)
        self.assertEqual(output.replace(fragment.encode("utf-8"), b"", 1), original)
        text = output.decode("utf-8")
        self.assertIn('stroke-width="6"', text)
        self.assertIn('stroke-opacity="0.50"', text)
        self.assertIn('marker-end="none"', text)
        self.assertNotIn("<text", fragment)
        self.assertNotIn("<polygon", fragment)

    def test_edge_sources_cover_base_loop_to_s0_and_sink(self) -> None:
        def step(src: str, dst: str, symbol: str, output: str) -> dict:
            return {"src": src, "dst": dst, "input": symbol, "output": output}

        paths = [{
            "path_index": 1,
            "trace_variants": [{
                "branches": {
                    "A": {"trajectories": [{
                        "steps": [
                            step("s0", "s1", "base", "ok"),
                            step("s1", "s1", "loop", "null_action"),
                            step("s1", "s0", "back", "null_action"),
                            step("s1", "s9", "sink", "null_action"),
                        ]
                    }]},
                    "B": {"trajectories": []},
                }
            }],
        }]
        model = {
            "outgoing": {
                "s0": {"base": step("s0", "s1", "base", "ok")},
                "s1": {
                    "loop": step("s1", "s1", "loop", "null_action"),
                    "back": step("s1", "s0", "back", "null_action"),
                    "sink": step("s1", "s9", "sink", "null_action"),
                },
                "s9": {"x": step("s9", "s9", "x", "null_action")},
            }
        }
        svg = {
            "nodes": {
                "s0": {"cx": 10.0, "cy": -10.0, "rx": 2.0, "ry": 2.0},
                "s1": {"cx": 50.0, "cy": -50.0, "rx": 2.0, "ry": 2.0},
                "s9": {"cx": 90.0, "cy": -90.0, "rx": 2.0, "ry": 2.0},
            },
            "edges": {("s0", "s1"): [{"d": "M10,-10C20,-20 30,-30 50,-50"}]},
            "view_box": (0.0, 0.0, 120.0, 120.0),
            "translate": (0.0, 120.0),
        }
        enriched, geometries = OVERLAY.enrich_paths_with_geometry(
            paths,
            model,
            {("s0", "s1", "base", "ok"): "M10,-10C20,-20 30,-30 50,-50"},
            svg,
        )
        steps = enriched[0]["trace_variants"][0]["branches"]["A"]["trajectories"][0]["steps"]
        self.assertEqual(
            [item["render_source"] for item in steps],
            ["base_edge", "restored_self_loop", "restored_to_s0", "skipped_sink"],
        )
        self.assertEqual(steps[2]["geometry_method"], "reverse_base_edge")
        self.assertIsNone(steps[3]["geometry_id"])
        self.assertEqual(len(geometries), 2)

    def test_unknown_missing_edge_is_rejected(self) -> None:
        paths = [{
            "trace_variants": [{
                "branches": {
                    "A": {"trajectories": [{"steps": [{
                        "src": "s1", "dst": "s2", "input": "x", "output": "ok"
                    }]}]},
                    "B": {"trajectories": []},
                }
            }]
        }]
        model = {
            "outgoing": {
                "s1": {"x": {"src": "s1", "dst": "s2", "input": "x", "output": "ok"}},
                "s2": {"x": {"src": "s2", "dst": "s1", "input": "x", "output": "ok"}},
            }
        }
        svg = {
            "nodes": {
                "s0": {"cx": 0.0, "cy": 0.0, "rx": 2.0, "ry": 2.0},
                "s1": {"cx": 10.0, "cy": 10.0, "rx": 2.0, "ry": 2.0},
                "s2": {"cx": 20.0, "cy": 20.0, "rx": 2.0, "ry": 2.0},
            },
            "edges": {},
            "view_box": (0.0, 0.0, 100.0, 100.0),
            "translate": (0.0, 0.0),
        }
        with self.assertRaisesRegex(ValueError, "unsupported reason"):
            OVERLAY.enrich_paths_with_geometry(paths, model, {}, svg)


if __name__ == "__main__":
    unittest.main()
