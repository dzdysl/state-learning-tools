"""Reserved CEGIS seam; no counterexample loop is claimed by the template."""

from __future__ import annotations

from typing import Any


def run_unsupported_cegis(_: dict[str, Any]) -> dict[str, Any]:
    return {"status": "not_implemented", "engine": "unsupported_v1"}
