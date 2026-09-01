"""Internal workflow-lowering responsibility owner."""

from __future__ import annotations

import shlex

from ._lowering_contracts import _Exit, _Fragment
from ._lowering_descriptors import lower_accept_descriptor, lower_publish_descriptor
from .canonical import plain
from .ir import AcceptIR, ChooseIR, DecideIR, StepIR, VerifyIR
from .semantics import BlockContract


class _LoweringBlocks:
    def _lower_step(
        self,
        block: StepIR,
        pointer: str,
        retry_limit: int | None,
        failure_target: str | None,
    ) -> _Fragment:
        logical = block.id or block.step
        result_key = f"{logical.replace('-', '_')}_result"
        artifact = block.artifact
        artifact_contract = (
            {
                "handle": artifact.handle,
                "path": artifact.path,
                "markdown": {"sections": list(artifact.markdown.sections)},
            }
            if artifact is not None
            else {}
        )
        descriptor = {
            "schema": "lockstep.effect/v1",
            "kind": "manual",
            "logical_id": logical,
            "runner": None,
            "inputs": {},
            "writes": list(block.writes),
            "artifacts": [],
            "deadline_seconds": None,
            "scope_state_keys": [],
            "result_schema": "lockstep.effect-result/v1",
        }
        message = {
            "step": block.step,
            "task": block.task,
            "exit_criterion": block.exit,
            "evidence_schema": (
                plain(block.evidence) if block.evidence is not None else {}
            ),
            "artifact_contract": artifact_contract,
            "lockstep_effect": descriptor,
        }
        return self.descriptor_interrupt(
            pointer,
            "step",
            logical,
            descriptor,
            message,
            result_key,
            retry_limit,
            failure_target=failure_target,
        )

    def _lower_verify(
        self,
        block: VerifyIR,
        pointer: str,
        retry_limit: int | None,
        failure_target: str | None,
    ) -> _Fragment:
        logical = block.id or f"verify-{pointer.rsplit('/', 1)[-1]}"
        result_key = f"{logical.replace('-', '_')}_result"
        command_key = f"{logical.replace('-', '_')}_command"
        self.declare_generated_state(command_key, "dict")
        prepare = self.node(
            pointer,
            "verify",
            "command",
            {
                "type": "passthrough",
                "output": {
                    command_key: {
                        "schema": "lockstep.pinned-command/v1",
                        "logical_argv": shlex.split(block.command),
                        "logical_cwd": block.cwd or ".",
                        "result_source": "exit",
                    }
                },
            },
        )
        descriptor = {
            "schema": "lockstep.effect/v1",
            "kind": "verify",
            "logical_id": logical,
            "runner": {
                "selector": "pinned",
                "required_capabilities": ["workspace", "bounded_result", "sandbox"],
            },
            "inputs": {
                "command": {"state_key": command_key},
                "snapshot": {"runtime_key": "current_project_snapshot"},
            },
            "writes": [],
            "artifacts": [],
            "deadline_seconds": block.timeout,
            "scope_state_keys": list(self.active_scope_state_keys),
            "result_schema": "lockstep.effect-result/v1",
        }
        effect = self.descriptor_interrupt(
            pointer,
            "verify",
            logical,
            descriptor,
            {"step": logical, "lockstep_effect": descriptor},
            result_key,
            retry_limit,
            failure_target=failure_target,
        )
        self.edge(prepare, effect.entry)
        return _Fragment(prepare, effect.exits)

    def _lower_decide(self, block: DecideIR, pointer: str) -> _Fragment:
        logical = block.id or "decision"
        result_key = f"{logical.replace('-', '_')}_result"
        using = plain(block.using)
        descriptor = {
            "schema": "lockstep.effect/v1",
            "kind": "decide",
            "logical_id": logical,
            "decision": {
                "type": "changed-paths",
                "since": "start",
                "cases": [
                    {"label": label, "paths": list(paths)}
                    for label, paths in using["cases"].items()
                ],
                "default": using["default"],
            },
            "inputs": {
                "start_snapshot": {"runtime_key": "run_start_project_snapshot"},
                "current_snapshot": {"runtime_key": "current_project_snapshot"},
            },
            "result_schema": "lockstep.decision-result/v1",
        }
        self.outcome_keys[logical] = result_key
        return self.descriptor_interrupt(
            pointer,
            "decide",
            logical,
            descriptor,
            {"step": logical, "lockstep_effect": descriptor},
            result_key,
            None,
        )

    def _lower_accept(self, block: AcceptIR, pointer: str) -> _Fragment:
        logical = block.id or f"accept-{pointer.rsplit('/', 1)[-1]}"
        result_key = f"{logical.replace('-', '_')}_result"
        try:
            producer_key, declared_name = self.artifact_state_keys[block.artifact_from]
        except KeyError as exc:
            raise ValueError(
                "accept artifact lacks a compiler-owned producer result channel"
            ) from exc
        artifact = self.validated.artifacts[block.artifact_from]
        descriptor = lower_accept_descriptor(
            logical,
            block.artifact_from,
            producer_key,
            declared_name,
            artifact.destination,
        )
        acceptance = self.descriptor_interrupt(
            pointer,
            "accept",
            logical,
            descriptor,
            {"step": logical, "lockstep_effect": descriptor},
            result_key,
            None,
        )
        publication_logical = f"publish-{logical}"
        publication_result = f"{publication_logical.replace('-', '_')}_result"
        publish_descriptor = lower_publish_descriptor(
            publication_logical,
            artifact_handle=block.artifact_from,
            producer_result_state_key=producer_key,
            declared_name=declared_name,
            acceptance_result_state_key=result_key,
            destination=artifact.destination,
        )
        publication = self.descriptor_interrupt(
            pointer,
            "publish",
            publication_logical,
            publish_descriptor,
            {"step": publication_logical, "lockstep_effect": publish_descriptor},
            publication_result,
            None,
        )
        self.connect(acceptance.exits, publication.entry)
        return _Fragment(acceptance.entry, publication.exits)

    def _lower_choose(
        self,
        block: ChooseIR,
        contract: BlockContract,
        pointer: str,
    ) -> _Fragment:
        result_key = self.outcome_keys.get(
            block.value, block.value.replace("-", "_") + "_result"
        )
        router = self.node(pointer, "choose", "route", {"type": "passthrough"})
        join = self.node(pointer, "choose", "join", {"type": "passthrough"})
        for label in block.cases:
            fragment = self.flow_contract(
                contract.branches[label], f"{pointer}/choose/cases/{label}"
            )
            self.edge(router, fragment.entry, f"{result_key}.value == '{label}'")
            self.connect(fragment.exits, join)
        if block.default is not None and contract.default is not None:
            fragment = self.flow_contract(contract.default, f"{pointer}/choose/default")
            condition = " and ".join(
                f"{result_key}.value != '{label}'" for label in block.cases
            )
            self.edge(router, fragment.entry, condition)
            self.connect(fragment.exits, join)
        return _Fragment(router, [_Exit(join)])
