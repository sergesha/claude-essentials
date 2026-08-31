"""Ownership freeze for the owner-policy module seam."""

from __future__ import annotations

import subprocess
import sys


def test_owner_policy_has_exact_acyclic_owners_and_identity_facade() -> None:
    """Catch copied definitions, misplaced policy, or circular imports."""

    script = r'''
import ast
import importlib
import inspect
import sys

prefix = "lockstep.runtime.effects"
values_name = f"{prefix}._owner_policy_values"
requirements_name = f"{prefix}._owner_policy_requirements"
admission_name = f"{prefix}._owner_policy_admission"
facade_name = f"{prefix}.owner_policy"

values_definitions = {
    "_lower_hex",
    "_exact_generation",
    "_RuntimeBindingFacts",
    "OwnerRuntimeGrant",
    "OwnerRuntimeSnapshot",
}
requirements_definitions = {
    "_canonical_digest",
    "_canonical_bounded_tuple",
    "_canonical_uses",
    "grant_selection_key",
    "requirement_digest",
    "RuntimeRequirement",
    "_stable_requirement_facts",
    "_merge_requirement",
    "_runtime_descriptors",
    "RuntimeRequirementIndex",
    "RuntimeProvisioningInventory",
    "_BoundRuntimeRequirementIndex",
}
admission_definitions = {
    "_RuntimeAdmissionChanged",
    "RuntimeAdmissionDecision",
    "OwnerRuntimeAuthority",
}


def top_level_definitions(module):
    tree = ast.parse(inspect.getsource(module))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


values = importlib.import_module(values_name)
assert requirements_name not in sys.modules
assert admission_name not in sys.modules
assert facade_name not in sys.modules

requirements = importlib.import_module(requirements_name)
assert admission_name not in sys.modules
assert facade_name not in sys.modules

admission = importlib.import_module(admission_name)
assert facade_name not in sys.modules

facade = importlib.import_module(facade_name)

owners = (
    (values, values_name, values_definitions),
    (requirements, requirements_name, requirements_definitions),
    (admission, admission_name, admission_definitions),
)
for owner, owner_name, definitions in owners:
    assert top_level_definitions(owner) == definitions
    for name in definitions:
        definition = getattr(owner, name)
        assert definition.__module__ == owner_name
        assert getattr(facade, name) is definition

assert top_level_definitions(facade) == set()

assert requirements.OwnerRuntimeSnapshot is values.OwnerRuntimeSnapshot
assert requirements._RuntimeBindingFacts is values._RuntimeBindingFacts
assert admission.OwnerRuntimeSnapshot is values.OwnerRuntimeSnapshot
assert admission.OwnerRuntimeGrant is values.OwnerRuntimeGrant
assert admission._RuntimeBindingFacts is values._RuntimeBindingFacts
assert admission.RuntimeRequirement is requirements.RuntimeRequirement
assert admission.RuntimeRequirementIndex is requirements.RuntimeRequirementIndex
assert admission._BoundRuntimeRequirementIndex is requirements._BoundRuntimeRequirementIndex
assert facade.RuntimeProvisioningInventory is requirements.RuntimeProvisioningInventory
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
