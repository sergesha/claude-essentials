"""Stable, source-addressable diagnostics for Workflow DSL input."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: Path
    line: int | None = None
    column: int | None = None
    pointer: str = ""
    hint: str | None = None
    generated_node: str | None = None

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["path"] = str(self.path)
        return result

    def render_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True)

    def render_text(self) -> str:
        location = str(self.path)
        if self.line is not None and self.column is not None:
            location = f"{location}:{self.line}:{self.column}"
        details = [f"{self.code} {self.message}", "", location, "", f"DSL pointer: {self.pointer}"]
        if self.hint:
            details.append(self.hint)
        if self.generated_node:
            details.append(f"Generated node: {self.generated_node}")
        return "\n".join(details)


class DiagnosticError(ValueError):
    def __init__(self, diagnostics: tuple[Diagnostic, ...] | list[Diagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__(self.render_text())

    def render_text(self) -> str:
        return "\n\n".join(diagnostic.render_text() for diagnostic in self.diagnostics)

    def render_json(self) -> str:
        return json.dumps([diagnostic.as_dict() for diagnostic in self.diagnostics], sort_keys=True)
