"""Small, deterministic parser for labelled Mealy DOT transitions."""

from __future__ import annotations

import re
from pathlib import Path

from contracts import DotTransition, RegisterInferenceError


EDGE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*->\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\[([^\]]*)\]\s*;",
    re.MULTILINE,
)
LABEL_RE = re.compile(r'label\s*=\s*"((?:\\.|[^"\\])*)"')


def split_label(label: str) -> tuple[tuple[str, ...], str]:
    if " / " in label:
        input_text, output = label.split(" / ", 1)
    elif "/" in label:
        input_text, output = label.split("/", 1)
    else:
        input_text, output = label, ""
    return tuple(part.strip() for part in input_text.split("|") if part.strip()), output.strip()


def parse_dot(path: Path) -> tuple[DotTransition, ...]:
    text = path.read_text(encoding="utf-8")
    transitions: list[DotTransition] = []
    for order, match in enumerate(EDGE_RE.finditer(text), start=1):
        source_state, target_state, attributes = match.groups()
        label_match = LABEL_RE.search(attributes)
        if not label_match:
            continue
        label = label_match.group(1).replace(r'\"', '"').replace(r"\\", "\\")
        inputs, output = split_label(label)
        transitions.append(
            DotTransition(
                edge_id=f"E{order:04d}",
                source_state=source_state,
                target_state=target_state,
                inputs=inputs,
                output=output,
                order=order,
            )
        )
    if not transitions:
        raise RegisterInferenceError(f"No labelled transitions found in DOT: {path}")
    return tuple(transitions)
