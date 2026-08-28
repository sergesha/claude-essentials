"""Template vocabulary over the neutral captured-workflow planner."""

from __future__ import annotations

from pathlib import Path

from lockstep.authoring_installation import (
    CapturedWorkflowSource,
    PlannedWorkflowInstallation,
    plan_captured_workflow_installation,
)

TemplateRoleSource = CapturedWorkflowSource
PlannedTemplateInstallation = PlannedWorkflowInstallation


def plan_template_installation(
    project: Path,
    role_sources: tuple[TemplateRoleSource, ...],
    *,
    root_role: str,
) -> PlannedTemplateInstallation:
    return plan_captured_workflow_installation(
        project, role_sources, root_role=root_role
    )
