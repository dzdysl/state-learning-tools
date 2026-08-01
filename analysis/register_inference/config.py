"""Loading and validating the deliberately small v1 YAML configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from contracts import RegisterInferenceError


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegisterInferenceError(f"Invalid YAML configuration {path}: {exc}") from exc
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise RegisterInferenceError("Configuration must be a mapping with schema_version: 1.")
    inputs = config.get("inputs")
    mapping = config.get("mapping")
    if not isinstance(inputs, dict) or not isinstance(mapping, dict):
        raise RegisterInferenceError("Configuration requires inputs and mapping mappings.")
    for name in ("dot", "trace"):
        if not isinstance(inputs.get(name), str) or not inputs[name]:
            raise RegisterInferenceError(f"inputs.{name} must be a non-empty path string.")
    for name in ("source_state", "target_state", "input_symbol"):
        if not isinstance(mapping.get(name), str) or not mapping[name]:
            raise RegisterInferenceError(f"mapping.{name} must be a non-empty field path.")
    registers = mapping.get("registers")
    if not isinstance(registers, list) or not registers:
        raise RegisterInferenceError("mapping.registers must declare at least one register.")
    register_ids: set[str] = set()
    for item in registers:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("id", "before", "after")):
            raise RegisterInferenceError("Each register requires string id, before and after paths.")
        if item["id"] in register_ids:
            raise RegisterInferenceError(f"Duplicate register id: {item['id']}")
        register_ids.add(item["id"])
    input_variables = mapping.get("input_variables", [])
    if not isinstance(input_variables, list):
        raise RegisterInferenceError("mapping.input_variables must be a list when supplied.")
    for item in input_variables:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("id", "path")):
            raise RegisterInferenceError("Each input variable requires string id and path.")
    return config


def resolve_input_path(config_path: Path, configured_path: str) -> Path:
    candidate = Path(configured_path)
    return candidate.resolve() if candidate.is_absolute() else (config_path.parent / candidate).resolve()
