"""Extension points intentionally left independent of the v1 algorithms."""

from __future__ import annotations

from typing import Any, Protocol


class CandidateGenerator(Protocol):
    def __call__(self, prepared: dict[str, Any], priority: list[str] | None = None) -> dict[str, Any]: ...


class RegisterFitter(Protocol):
    def __call__(self, prepared: dict[str, Any], candidates: dict[str, Any]) -> dict[str, Any]: ...


class CegisEngine(Protocol):
    def run(self, configuration: dict[str, Any]) -> dict[str, Any]: ...


class ModelOutput(Protocol):
    def emit(self, result: dict[str, Any]) -> None: ...
