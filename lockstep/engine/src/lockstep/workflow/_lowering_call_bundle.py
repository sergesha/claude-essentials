"""Internal workflow-lowering responsibility owner."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any, Mapping  # noqa: UP035 - preserves existing hints

import yaml

from lockstep.recipe.layout import RecipeDirectory

from ._lowering_child_specialization import (
    _specialize_child_edges,
    _specialize_child_loops,
    _specialize_child_node,
    _specialized_child_state,
    _specialized_fragment_digest,
)
from ._lowering_contracts import (
    LoweredDependency,
    LoweredGeneratedFile,
    _Exit,
    _Fragment,
)
from ._lowering_identity import _specialized_state_key
from .canonical import canonical_yaml
from .ir import CallIR


class _LoweringCallBundle:
    @staticmethod
    def _load_child_document(
        resolved: Any, source_file: Any | None
    ) -> tuple[Any, dict[str, Any], dict[str, Any]]:
        selected_file = source_file or next(
            item
            for item in resolved.standalone.files
            if item.relative_path == resolved.standalone.root_relative_path
        )
        document = yaml.safe_load(selected_file.content)
        if not isinstance(document, dict):
            raise ValueError(  # noqa: TRY004
                "resolved child root must be a YAML mapping"
            )
        state = document.setdefault("state", {})
        if not isinstance(state, dict):
            raise ValueError("resolved child state must be a mapping")  # noqa: TRY004
        return selected_file, document, state

    @staticmethod
    def _validate_root_child_state(
        resolved: Any, selected_file: Any, state: dict[str, Any]
    ) -> None:
        if selected_file.relative_path != resolved.standalone.root_relative_path:
            return
        child_contract = resolved.contract
        for key, state_type in {
            **dict(child_contract.state_inputs),
            **dict(child_contract.state_exports),
        }.items():
            if key not in state:
                raise ValueError(
                    f"child state contract key {key!r} is missing from "
                    "standalone schema"
                )
            if state[key] != state_type:
                raise ValueError(
                    f"child state contract type mismatch for {key!r}: "
                    f"expected {state_type!r}, got {state[key]!r}"
                )

    def _register_specialized_child_channels(
        self,
        specialized: dict[str, Any],
    ) -> None:
        specialized_state = specialized.get("state", {})
        for specialized_node in specialized.get("nodes", {}).values():
            if not isinstance(specialized_node, dict):
                continue
            for field in ("state_key", "resume_key"):
                state_key = specialized_node.get(field)
                if isinstance(state_key, str) and state_key not in self.state:
                    self.declare_generated_state(
                        state_key, specialized_state.get(state_key, "dict")
                    )
            message = specialized_node.get("message")
            effect = (
                message.get("lockstep_effect") if isinstance(message, dict) else None
            )
            inputs = effect.get("inputs") if isinstance(effect, dict) else None
            if isinstance(inputs, dict):
                for selector in inputs.values():
                    state_key = (
                        selector.get("state_key")
                        if isinstance(selector, dict)
                        else None
                    )
                    if isinstance(state_key, str) and state_key not in self.state:
                        self.declare_generated_state(
                            state_key, specialized_state.get(state_key, "any")
                        )
            if isinstance(effect, dict):
                self._register_specialized_effect_channels(effect, specialized_state)

    def _register_specialized_effect_channels(
        self,
        effect: dict[str, Any],
        specialized_state: dict[str, Any],
    ) -> None:
        shared_keys = []
        for field in ("scope_state_keys", "ancestor_deadline_state_keys"):
            values = effect.get(field)
            if isinstance(values, list):
                shared_keys.extend(key for key in values if isinstance(key, str))
        result_state_key = effect.get("result_state_key")
        if isinstance(result_state_key, str):
            shared_keys.append(result_state_key)
        for state_key in shared_keys:
            if state_key not in self.state:
                self.declare_generated_state(
                    state_key,
                    specialized_state.get(state_key, "dict"),
                )

    def _specialize_call_members(
        self,
        *,
        block: CallIR,
        pointer: str,
        resolved: Any,
        call_digest: str,
        namespace: str,
        scope_key: str,
        child_outcome: str,
        reserved_child_channels: frozenset[str],
        producer_bindings: Mapping[
            str, tuple[tuple[str, str, str, str, str, str], ...]
        ],
    ) -> tuple[str, list[tuple[str, bytes]]]:
        generated_base = f"{RecipeDirectory.GENERATED_CHILDREN}/{call_digest}"
        generated_path = f"{generated_base}/{resolved.standalone.root_relative_path}"
        specialized_members: list[tuple[str, bytes]] = []
        for source_file in resolved.standalone.files:
            specialized = self._specialize_child(
                resolved,
                namespace,
                scope_key,
                child_outcome,
                block.runner,
                reserved_channels=reserved_child_channels,
                source_file=source_file,
                artifact_bindings=producer_bindings.get(source_file.relative_path, ()),
            )
            target_path = f"{generated_base}/{source_file.relative_path}"
            specialized_bytes = canonical_yaml(specialized)
            specialized_members.append((target_path, specialized_bytes))
            self._register_specialized_child_channels(specialized)
            self.generated_files.append(
                LoweredGeneratedFile(
                    target_path,
                    specialized_bytes,
                    hashlib.sha256(specialized_bytes).hexdigest(),
                    block.workflow,
                    pointer,
                    resolved.source_definition_sha256,
                )
            )
        return generated_path, specialized_members

    @staticmethod
    def _compiled_bundle_digest(
        root_path: str,
        members: Mapping[str, bytes],
    ) -> str:
        digest = hashlib.sha256(b"lockstep.compiled-bundle/v1\0")
        digest.update(root_path.encode("utf-8"))
        digest.update(b"\0")
        for member_path, member_bytes in sorted(members.items()):
            digest.update(member_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(member_bytes).hexdigest().encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _record_call_workflow_dependency(
        self,
        *,
        block: CallIR,
        pointer: str,
        resolved: Any,
        generated_path: str,
        specialized_members: list[tuple[str, bytes]],
    ) -> None:
        self.dependencies.append(
            LoweredDependency(
                "workflow",
                block.workflow,
                pointer,
                resolved.source_definition_sha256,
                self._compiled_bundle_digest(generated_path, dict(specialized_members)),
                generated_path,
            )
        )

    @staticmethod
    def _specialized_members_by_source(
        resolved: Any,
        specialized_members: list[tuple[str, bytes]],
    ) -> dict[str, tuple[str, bytes]]:
        return {
            source_file.relative_path: member
            for source_file, member in zip(
                resolved.standalone.files, specialized_members, strict=True
            )
        }

    @staticmethod
    def _reachable_dependency_members(
        generated_root: str,
        specialized_by_source: Mapping[str, tuple[str, bytes]],
    ) -> set[str]:
        reachable = {generated_root}
        pending = [generated_root]
        while pending:
            current = pending.pop()
            current_document = yaml.safe_load(specialized_by_source[current][1])
            nodes = (
                current_document.get("nodes", {})
                if isinstance(current_document, dict)
                else {}
            )
            for node in nodes.values() if isinstance(nodes, dict) else ():
                graph = node.get("graph") if isinstance(node, dict) else None
                if not isinstance(graph, str):
                    continue
                child_source = (PurePosixPath(current).parent / graph).as_posix()
                if child_source not in specialized_by_source:
                    raise ValueError(
                        "compiled child dependency graph references an unknown member"
                    )
                if child_source not in reachable:
                    reachable.add(child_source)
                    pending.append(child_source)
        return reachable

    def _rebased_dependency_digest(
        self,
        generated_root: str,
        specialized_by_source: Mapping[str, tuple[str, bytes]],
    ) -> tuple[str, str]:
        rebased_root = specialized_by_source[generated_root][0]
        reachable = self._reachable_dependency_members(
            generated_root, specialized_by_source
        )
        members = {
            specialized_by_source[source_path][0]: specialized_by_source[source_path][1]
            for source_path in reachable
        }
        return rebased_root, self._compiled_bundle_digest(rebased_root, members)

    @staticmethod
    def _call_fragment_dependency_digest(
        *,
        resolved: Any,
        specialized_by_source: Mapping[str, tuple[str, bytes]],
        dependency: Any,
        namespace: str,
    ) -> str:
        for source_file in resolved.standalone.files:
            _target_path, specialized_bytes = specialized_by_source[
                source_file.relative_path
            ]
            original_document = yaml.safe_load(source_file.content)
            specialized_document = yaml.safe_load(specialized_bytes)
            if not isinstance(original_document, dict) or not isinstance(
                specialized_document, dict
            ):
                continue
            transformed = _specialized_fragment_digest(
                original_document,
                specialized_document,
                dependency.compiled_sha256,
                namespace,
            )
            if transformed is not None:
                return transformed
        raise ValueError("compiled child fragment dependency projection is unavailable")

    def _record_call_transitive_dependencies(
        self,
        *,
        resolved: Any,
        specialized_members: list[tuple[str, bytes]],
        namespace: str,
        pointer: str,
    ) -> None:
        specialized_by_source = self._specialized_members_by_source(
            resolved, specialized_members
        )
        for dependency in resolved.standalone.dependencies:
            rebased_root = None
            compiled_sha256 = dependency.compiled_sha256
            if dependency.generated_root is not None:
                rebased_root, compiled_sha256 = self._rebased_dependency_digest(
                    dependency.generated_root, specialized_by_source
                )
            elif dependency.kind == "fragment":
                compiled_sha256 = self._call_fragment_dependency_digest(
                    resolved=resolved,
                    specialized_by_source=specialized_by_source,
                    dependency=dependency,
                    namespace=namespace,
                )
            self.dependencies.append(
                LoweredDependency(
                    dependency.kind,
                    dependency.logical_name,
                    f"{pointer}{dependency.use_pointer}",
                    dependency.definition_sha256,
                    compiled_sha256,
                    rebased_root,
                )
            )

    def _call_post_output(
        self,
        *,
        namespace: str,
        child_contract: Any,
        artifact_specs: Mapping[str, tuple[str, str, str, str, str]],
        producer_bindings: Mapping[
            str, tuple[tuple[str, str, str, str, str, str], ...]
        ],
    ) -> dict[str, str]:
        output = {
            key: f"{{state.{namespace}_{key}}}" for key in child_contract.state_exports
        }
        producers = tuple(
            item for items in producer_bindings.values() for item in items
        )
        for qualified in artifact_specs:
            channel, _name = self.artifact_state_keys[qualified]
            producer = next(item for item in producers if item[0] == qualified)
            output[channel] = (
                f"{{state.{_specialized_state_key(namespace, producer[4])}}}"
            )
        return output

    def _finish_call_graph(
        self,
        *,
        pointer: str,
        generated_path: str,
        namespace: str,
        child_contract: Any,
        artifact_specs: Mapping[str, tuple[str, str, str, str, str]],
        producer_bindings: Mapping[
            str, tuple[tuple[str, str, str, str, str, str], ...]
        ],
        saved_context: dict[str, str],
        child_outcome: str,
        context: str,
        scope: _Fragment,
        pre: str,
    ) -> _Fragment:
        child = self.node(
            pointer,
            "call",
            "direct",
            {"type": "subgraph", "graph": generated_path, "mode": "direct"},
        )
        post = self.node(
            pointer,
            "call",
            "post",
            {
                "type": "passthrough",
                "output": self._call_post_output(
                    namespace=namespace,
                    child_contract=child_contract,
                    artifact_specs=artifact_specs,
                    producer_bindings=producer_bindings,
                ),
            },
        )
        restoration_output = {
            "current_step": f"{{state.{saved_context['current_step']}}}",
            "_loop_counts": f"{{state.{saved_context['_loop_counts']}}}",
            "_loop_limit_reached": (
                f"{{state.{saved_context['_loop_limit_reached']}}}"
            ),
        }
        restorations = {
            outcome: self.node(
                pointer,
                "call",
                f"restore-{outcome.lower()}",
                {"type": "passthrough", "output": restoration_output},
            )
            for outcome in ("PASS", "FAIL", "ERROR", "ABORTED")
        }
        self.edge(context, scope.entry)
        self.connect(scope.exits, pre)
        self.edge(pre, child)
        self.edge(child, post)
        for outcome, restore in restorations.items():
            self.edge(post, restore, f"{child_outcome} == '{outcome}'")
            if outcome != "PASS":
                self.edge(restore, self.outcome_target(outcome))
        return _Fragment(context, [_Exit(restorations["PASS"])])

    def _specialize_child(
        self,
        resolved: Any,
        namespace: str,
        scope_key: str,
        child_outcome: str,
        runner: str,
        *,
        reserved_channels: frozenset[str],
        source_file: Any | None = None,
        artifact_bindings: tuple[tuple[str, str, str, str, str, str], ...] = (),
    ) -> dict[str, Any]:
        selected_file, document, state = self._load_child_document(
            resolved, source_file
        )
        child_contract = resolved.contract
        self._validate_root_child_state(resolved, selected_file, state)
        key_map, new_state = _specialized_child_state(
            state,
            child_contract=child_contract,
            namespace=namespace,
            scope_key=scope_key,
            child_outcome=child_outcome,
            reserved_channels=reserved_channels,
        )
        document["state"] = new_state
        nodes = document.get("nodes", {})
        if not isinstance(nodes, dict):
            raise ValueError("resolved child nodes must be a mapping")  # noqa: TRY004
        node_map = {name: f"{namespace}.{name}" for name in nodes}
        specialized_nodes: dict[str, Any] = {}
        managed_briefs: dict[str, str] = {}
        for name, raw_node in nodes.items():
            specialized_name = node_map[name]
            specialized_node, brief = _specialize_child_node(
                raw_node,
                original_name=name,
                namespace=namespace,
                runner=runner,
                scope_key=scope_key,
                key_map=key_map,
                new_state=new_state,
                artifact_bindings=artifact_bindings,
                inside_parallel_branch=self.inside_parallel_branch,
            )
            specialized_nodes[specialized_name] = specialized_node
            if brief is not None:
                brief_name, state_key, content = brief
                if brief_name in specialized_nodes or brief_name in managed_briefs.values():
                    raise ValueError("managed brief node identity collides")
                managed_briefs[specialized_name] = brief_name
                specialized_nodes[brief_name] = {
                    "type": "passthrough",
                    "output": {state_key: content},
                }
        document["nodes"] = specialized_nodes
        document["edges"] = _specialize_child_edges(
            document.get("edges", []),
            node_map=node_map,
            key_map=key_map,
            managed_briefs=managed_briefs,
        )
        _specialize_child_loops(document, node_map)
        document["name"] = f"{document.get('name', resolved.logical_name)}-{namespace}"
        return document
