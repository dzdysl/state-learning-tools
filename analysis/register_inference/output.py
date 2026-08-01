"""Reserved renderer seam for inferred DOT models and diagnostic JSON."""

from __future__ import annotations

from typing import Any


def emit_unsupported_output(_: dict[str, Any]) -> dict[str, Any]:
    return {"status": "not_implemented", "output": "unsupported_v1"}
