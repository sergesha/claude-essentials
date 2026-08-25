"""Closed package-resource workflow templates with atomic project install."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Mapping

import yaml

from lockstep.authoring import compile_source, link_recipe_dependencies


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


def _source_text(template: str, role: str, name: str) -> str:
    manifest = _manifest(template)
    source = _bundle(template).joinpath(manifest["files"][role]).read_text()
    return source.replace("{name}", name)


def _role_dependencies(template: str, role: str) -> tuple[str, ...]:
    document = yaml.safe_load(
        _bundle(template).joinpath(_manifest(template)["files"][role]).read_text()
    )
    dependencies: list[str] = []

    def walk(value):
        if isinstance(value, dict):
            call = value.get("call")
            if isinstance(call, dict) and isinstance(call.get("workflow"), str):
                target = call["workflow"]
                for candidate, output in _manifest(template)["outputs"].items():
                    if output == target:
                        dependencies.append(candidate)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document)
    return tuple(dict.fromkeys(dependencies))


def show_template(template: str, name: str) -> TemplateView:
    manifest = _manifest(template)
    roles = {role: output.replace("{name}", name) for role, output in manifest["outputs"].items()}
    sources = dict(manifest["files"])
    role_dependencies = {role: _role_dependencies(template, role) for role in roles}
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


def _compile_role(source: Path, children):
    return compile_source(source, children=children)


def _replace_destination(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def _journal_path(project: Path) -> Path:
    return project / ".lockstep" / ".template-install.json"


def _remove_empty(path: Path, stop: Path) -> None:
    current = path
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _recover_install(project: Path) -> None:
    journal = _journal_path(project)
    if not journal.is_file():
        return
    try:
        value = json.loads(journal.read_text())
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "entries"}
            or value.get("schema") != "lockstep.template-install/v1"
            or not isinstance(value.get("entries"), list)
            or len(value["entries"]) > 256
        ):
            raise ValueError("template recovery journal is invalid")
        entries = value["entries"]
        for item in entries:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "sha256"}
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str)
                or len(item["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in item["sha256"])
            ):
                raise ValueError("template recovery journal entry is invalid")
            logical = PurePosixPath(item["path"])
            if (
                logical.is_absolute()
                or logical.as_posix() != item["path"]
                or any(part in {"", ".", ".."} for part in logical.parts)
            ):
                raise ValueError("template recovery journal path is unsafe")
            destination = project.joinpath(*logical.parts)
            if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == item["sha256"]:
                destination.unlink()
                _remove_empty(destination.parent, project)
    finally:
        if journal.exists():
            journal.unlink()
            _remove_empty(journal.parent, project)


def install_template(template: str, name: str, project: Path) -> InstalledTemplate:
    if template not in _EXPECTED:
        if Path(template).exists() or "/" in template or "\\" in template:
            raise ValueError("custom template paths are a v2 feature")
        raise ValueError(f"unknown template {template!r}")
    root = Path(project).resolve()
    _recover_install(root)
    shown = show_template(template, name)
    source_destinations = {
        role: root / ".lockstep" / "workflows" / f"{output}.workflow.yaml"
        for role, output in shown.roles.items()
    }
    basic_recipe_destinations = {
        role: root / ".lockstep" / "recipes" / f"{output}.recipe.yaml"
        for role, output in shown.roles.items()
    }
    for destination in (*source_destinations.values(), *basic_recipe_destinations.values()):
        if destination.exists() or destination.is_symlink():
            raise TemplateCollision(str(destination.relative_to(root)))

    with tempfile.TemporaryDirectory(prefix="lockstep-template-stage-") as raw:
        stage = Path(raw)
        staged_sources = {}
        for role, output in shown.roles.items():
            target = stage / "workflows" / f"{output}.workflow.yaml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_source_text(template, role, name))
            staged_sources[role] = target
        compiled_by_name = {}
        compiled_by_role = {}
        for output in shown.compile_order:
            role = next(key for key, value in shown.roles.items() if value == output)
            children = {
                shown.roles[child]: compiled_by_name[shown.roles[child]]
                for child in _role_dependencies(template, role)
            }
            validated, _catalog, compiled = _compile_role(staged_sources[role], children)
            compiled_by_name[output] = (validated, compiled)
            compiled_by_role[role] = compiled

        staged_files: dict[Path, bytes] = {}
        recipe_roots = []
        for role, compiled in compiled_by_role.items():
            output = shown.roles[role]
            recipe_root = root / ".lockstep" / "recipes"
            root_destination = recipe_root / f"{output}.recipe.yaml"
            recipe_roots.append(root_destination)
            children = tuple(shown.dependencies[output])
            staged_files[root_destination] = link_recipe_dependencies(
                compiled.recipe_bytes, children
            )
            staged_files[recipe_root / f"{output}.dependencies.json"] = compiled.dependency_manifest_bytes
            staged_files[recipe_root / f"{output}.source-map.json"] = compiled.source_map_bytes
            for item in compiled.generated_files:
                staged_files[recipe_root / item.relative_path] = item.content
        for role, destination in source_destinations.items():
            staged_files[destination] = staged_sources[role].read_bytes()
        for destination in staged_files:
            if destination.exists() or destination.is_symlink():
                raise TemplateCollision(str(destination.relative_to(root)))

        journal = _journal_path(root)
        entries = [
            {
                "path": str(path.relative_to(root)),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, content in sorted(staged_files.items(), key=lambda item: str(item[0]))
        ]
        journal.parent.mkdir(parents=True, exist_ok=True)
        staged_journal = stage / "template-install.json"
        staged_journal.write_text(
            json.dumps(
                {"schema": "lockstep.template-install/v1", "entries": entries},
                sort_keys=True,
            )
        )
        os.replace(staged_journal, journal)
        try:
            for index, (destination, content) in enumerate(
                sorted(staged_files.items(), key=lambda item: str(item[0]))
            ):
                staged = stage / "publish" / str(index)
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(content)
                _replace_destination(staged, destination)
        except BaseException:
            _recover_install(root)
            raise
        journal.unlink()

    return InstalledTemplate(
        tuple(source_destinations.values()),
        tuple(recipe_roots),
        shown.compile_order,
    )
