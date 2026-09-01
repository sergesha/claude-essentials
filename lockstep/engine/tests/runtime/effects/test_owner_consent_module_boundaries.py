"""Ownership freeze for the owner-consent module seam."""

from __future__ import annotations

import subprocess
import sys


def test_owner_consent_has_exact_acyclic_value_owner_and_identity_facade() -> None:
    """Catch copied values, misplaced SQL authority, or circular imports."""

    script = r'''
import ast
import importlib
import inspect
import sys
import typing

prefix = "lockstep.runtime.effects"
values_name = f"{prefix}._owner_consent_values"
facade_name = f"{prefix}.owner_consent"

values_definitions = {
    "_text",
    "_digest",
    "_coordinate_data",
    "_canonical",
    "_utc",
    "PublicationConsentCommitment",
    "IssuedPublicationConsent",
    "StoredPublicationConsent",
}
value_methods = {
    "PublicationConsentCommitment": {"build", "to_dict"},
    "IssuedPublicationConsent": set(),
    "StoredPublicationConsent": set(),
}
authority_methods = {
    "__init__",
    "_now",
    "_token_hash",
    "_row_epoch",
    "current_epoch",
    "issue",
    "_commitment_from_row",
    "_stored_from_row",
    "inspect_token",
    "_receipt_data",
    "_acceptance_from_row",
    "redeem",
    "revoke",
    "_publish_item",
    "_publication_grant",
    "resolve",
    "commitment",
}


def module_tree(module):
    return ast.parse(inspect.getsource(module))


def top_level_definitions(tree):
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def class_methods(tree, class_name):
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


values = importlib.import_module(values_name)
assert facade_name not in sys.modules

values_tree = module_tree(values)
assert top_level_definitions(values_tree) == values_definitions
assert not any(
    (
        isinstance(node, ast.Import)
        and any(alias.name == facade_name for alias in node.names)
    )
    or (
        isinstance(node, ast.ImportFrom)
        and node.module in {facade_name, "owner_consent"}
    )
    for node in ast.walk(values_tree)
)
for class_name, methods in value_methods.items():
    assert class_methods(values_tree, class_name) == methods
for name in values_definitions:
    assert getattr(values, name).__module__ == values_name

facade = importlib.import_module(facade_name)
facade_tree = module_tree(facade)
assert top_level_definitions(facade_tree) == {"OwnerConsentAuthority"}
assert class_methods(facade_tree, "OwnerConsentAuthority") == authority_methods
assert facade.OwnerConsentAuthority.__module__ == facade_name

for name in values_definitions:
    assert getattr(facade, name) is getattr(values, name)

commitment_hints = typing.get_type_hints(values.PublicationConsentCommitment.build)
stored_hints = typing.get_type_hints(values.StoredPublicationConsent)
issue_hints = typing.get_type_hints(facade.OwnerConsentAuthority.issue)
inspect_hints = typing.get_type_hints(facade.OwnerConsentAuthority.inspect_token)
redeem_hints = typing.get_type_hints(facade.OwnerConsentAuthority.redeem)

assert commitment_hints["return"] is values.PublicationConsentCommitment
assert stored_hints["commitment"] is values.PublicationConsentCommitment
assert issue_hints["commitment"] is values.PublicationConsentCommitment
assert issue_hints["return"] is values.IssuedPublicationConsent
assert inspect_hints["return"] is values.StoredPublicationConsent
assert redeem_hints["commitment"] is values.PublicationConsentCommitment
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
