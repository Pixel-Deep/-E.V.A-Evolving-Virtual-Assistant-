"""Registry for deterministic automation capabilities."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .models import ToolSpec

ToolHandler = Callable[[Any, dict[str, Any]], Any]
ToolVerifier = Callable[[Any, dict[str, Any], Any], bool]
ToolUndo = Callable[[Any, dict[str, Any], Any], Any]


@dataclass(frozen=True)
class ToolBinding:
    spec: ToolSpec
    handler: ToolHandler
    verifier: ToolVerifier | None = None
    undo: ToolUndo | None = None


class ToolRegistry:
    """A single source of truth for tools available to the automation engine."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolBinding] = {}

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        verifier: ToolVerifier | None = None,
        undo: ToolUndo | None = None,
    ) -> None:
        name = spec.name.strip().lower()
        if not name or any(not part for part in name.split(".")):
            raise ValueError("Tool names must be non-empty dotted identifiers, e.g. files.list.")
        if name in self._tools:
            raise ValueError(f"Tool is already registered: {name}")
        if spec.timeout_seconds <= 0:
            raise ValueError("Tool timeout_seconds must be positive.")
        if not callable(handler):
            raise TypeError("Tool handler must be callable.")
        self._tools[name] = ToolBinding(spec=spec, handler=handler, verifier=verifier, undo=undo)

    def get(self, name: str) -> ToolBinding | None:
        return self._tools.get(name.strip().lower())

    def require(self, name: str) -> ToolBinding:
        binding = self.get(name)
        if binding is None:
            raise KeyError(f"Unknown automation tool: {name}")
        return binding

    def list(self, category: str | None = None) -> list[ToolSpec]:
        specs = [binding.spec for binding in self._tools.values()]
        if category:
            specs = [spec for spec in specs if spec.category.lower() == category.lower()]
        return sorted(specs, key=lambda spec: spec.name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def __len__(self) -> int:
        return len(self._tools)
