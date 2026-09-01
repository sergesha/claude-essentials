"""Closed package-resource workflow templates with atomic project install."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Mapping

import yaml

from lockstep.authoring import (
    validate_logical_name,
)
from lockstep.authoring_compilation import workflow_call_names
from lockstep.authoring_installation import installation_collision
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.template_installation import (
    TemplateRoleSource,
    plan_template_installation,
)
from lockstep.workflow.schema import load_workflow_bytes


class TemplateCollision(ValueError):
    pass


@dataclass(frozen=True)
class TemplateView:
    template: str
    name: str
    roles: Mapping[str, str]
    sources: Mapping[str, str]
    dependencies: Mapping[str, list[str]]
    compile_order: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "template": self.template,
            "name": self.name,
            "roles": dict(self.roles),
            "sources": dict(self.sources),
            "dependencies": {key: list(value) for key, value in self.dependencies.items()},
            "compile_order": list(self.compile_order),
        }


@dataclass(frozen=True)
class InstalledTemplate:
    sources: tuple[Path, ...]
    recipes: tuple[Path, ...]
    compile_order: tuple[str, ...]


_EXPECTED = {"parallel-review", "reviewed-change"}


def _bundle(name: str):
    if name not in _EXPECTED:
        if Path(name).exists() or "/" in name or "\\" in name:
            raise ValueError("custom template paths are a v2 feature")
        raise ValueError(f"unknown template {name!r}")
    return resources.files(__package__).joinpath(name)


def _manifest(name: str) -> dict[str, object]:
    bundle = _bundle(name)
    value = yaml.safe_load(bundle.joinpath("template.yaml").read_text())
    if not isinstance(value, dict) or set(value) != {"template_version", "outputs", "files"}:
        raise ValueError("template manifest is not closed")
    outputs, files = value["outputs"], value["files"]
    if value["template_version"] != "1" or not isinstance(outputs, dict) or not isinstance(files, dict):
        raise ValueError("template manifest is invalid")
    if set(outputs) != set(files) or set(bundle.joinpath(item).name for item in files.values()) != set(files.values()):
        raise ValueError("template role map is incomplete")
    observed = {item.name for item in bundle.iterdir() if item.is_file()}
    if observed != {"template.yaml", *files.values()}:
        raise ValueError("template bundle contains undeclared files")
    return value


def list_templates() -> tuple[str, ...]:
    root = resources.files(__package__)
    observed = {
        item.name for item in root.iterdir()
        if item.is_dir() and not item.name.startswith("__")
    }
    if observed != _EXPECTED:
        raise ValueError("installed template catalog is not the closed v1 set")
    for name in sorted(observed):
        _manifest(name)
    return tuple(sorted(observed))


def show_template(template: str, name: str) -> TemplateView:
    validate_logical_name(name)
    manifest = _manifest(template)
    roles = {role: output.replace("{name}", name) for role, output in manifest["outputs"].items()}
    sources = dict(manifest["files"])
    output_roles = {output: role for role, output in roles.items()}
    captured = _captured_role_sources(template, name, manifest)
    role_dependencies = {
        output_roles[source.role]: tuple(
            output_roles[child]
            for child in workflow_call_names(
                load_workflow_bytes(
                    Path(f"{source.role}.workflow.yaml"), source.content
                )
            )
            if child in output_roles
        )
        for source in captured
    }
    order: list[str] = []
    active: set[str] = set()

    def visit(role: str) -> None:
        if role in active:
            raise ValueError("template role dependencies are recursive")
        if roles[role] in order:
            return
        active.add(role)
        for child in role_dependencies[role]:
            visit(child)
        active.remove(role)
        order.append(roles[role])

    visit("parent")
    dependencies = {
        roles[role]: [roles[child] for child in role_dependencies[role]]
        for role in roles
    }
    return TemplateView(template, name, roles, sources, dependencies, tuple(order))


def _captured_role_sources(
    template: str, name: str, manifest: dict[str, object]
) -> tuple[TemplateRoleSource, ...]:
    bundle = _bundle(template)
    outputs = manifest["outputs"]
    files = manifest["files"]
    if not isinstance(outputs, dict) or not isinstance(files, dict):
        raise ValueError("template manifest is invalid")
    return tuple(
        TemplateRoleSource(
            output.replace("{name}", name),
            bundle.joinpath(files[role])
            .read_text()
            .replace("{name}", name)
            .encode(),
        )
        for role, output in outputs.items()
    )


def install_template(
    template: str, name: str, project: Path, *, state_dir: Path
) -> InstalledTemplate:
    validate_logical_name(name)
    manifest = _manifest(template)
    root = Path(project).resolve()
    publisher = AuthoringPublisher(state_dir)
    publisher.require_ready(root)
    role_sources = _captured_role_sources(template, name, manifest)
    outputs = manifest["outputs"]
    if not isinstance(outputs, dict) or not isinstance(outputs.get("parent"), str):
        raise ValueError("template manifest has no parent role")
    planned = plan_template_installation(
        root,
        role_sources,
        root_role=outputs["parent"].replace("{name}", name),
    )
    collision = installation_collision(planned.plan)
    if collision is not None:
        relative = collision.path.relative_to(root)
        raise TemplateCollision(f"template destination already exists: {relative}")
    publisher.publish(planned.plan)
    return InstalledTemplate(
        planned.sources,
        planned.recipes,
        planned.compile_order,
    )
