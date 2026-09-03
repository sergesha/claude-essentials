"""Ownership freeze for the publication module seam."""

from __future__ import annotations

import subprocess
import sys


def test_publication_has_exact_acyclic_owners_and_identity_facade() -> None:
    """Catch copied values, misplaced I/O, circular imports, or a dead crash hook."""

    script = r'''
import ast
import importlib
import inspect
import sys

prefix = "lockstep.runtime"
values_name = f"{prefix}._publication_values"
queries_name = f"{prefix}._publication_queries"
facade_name = f"{prefix}.publication"

value_definitions = {
    "PublicationError",
    "PublicationConflict",
    "PublicationJournalError",
    "PublicationLimits",
    "PublicationEntry",
    "PublicationRequest",
    "PreparedPublication",
    "PublicationReceipt",
    "_canonical",
    "_text",
    "_digest",
    "_counter",
    "_coordinate_data",
    "_entry_data",
    "_image_data",
    "_image_from_data",
    "_same_image",
    "_request_data",
}
query_methods = {
    "project_identity",
    "journal_path",
    "commitment_digest",
    "prepared_for",
    "_verify_complete",
    "_open_root",
    "_open_parent",
    "_current_image_data",
    "_current_image",
    "_read_active_optional",
    "_read_journal_digest",
    "_read_journal",
    "_read_json",
    "_validate_plan",
    "_validate_handle",
    "_receipt",
}
stateful_methods = {
    "__init__",
    "prepare",
    "_build_plan",
    "apply_or_recover",
    "rollback_or_recover",
    "_advance_plan",
    "_replace",
    "_store_journal",
    "_write_atomic",
}


def top_level_definitions(module):
    tree = ast.parse(inspect.getsource(module))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


values = importlib.import_module(values_name)
assert queries_name not in sys.modules
assert facade_name not in sys.modules

queries = importlib.import_module(queries_name)
assert facade_name not in sys.modules

facade = importlib.import_module(facade_name)

assert top_level_definitions(values) == value_definitions
assert isinstance(values._HEX, frozenset)
assert values._HEX == frozenset("0123456789abcdef")
assert facade._HEX is values._HEX
for name in value_definitions:
    definition = getattr(values, name)
    assert definition.__module__ == values_name
    assert getattr(facade, name) is definition

assert top_level_definitions(queries) == {"_ProjectPublicationQueries"}
assert queries._ProjectPublicationQueries in facade.ProjectPublisher.__mro__
assert "__init__" not in queries._ProjectPublicationQueries.__dict__
assert {
    name
    for name, member in queries._ProjectPublicationQueries.__dict__.items()
    if inspect.isfunction(member) or isinstance(member, property)
} == query_methods
assert query_methods.isdisjoint(facade.ProjectPublisher.__dict__)
for name in query_methods:
    member = queries._ProjectPublicationQueries.__dict__[name]
    function = member.fget if isinstance(member, property) else member
    assert function.__module__ == queries_name
for name in {
    "PublicationConflict",
    "PublicationJournalError",
    "PreparedPublication",
    "PublicationReceipt",
    "_canonical",
    "_digest",
    "_image_from_data",
    "_same_image",
}:
    assert getattr(queries, name) is getattr(values, name)

assert top_level_definitions(facade) == {"_after_replacement", "ProjectPublisher"}
assert facade._after_replacement.__module__ == facade_name
assert facade.ProjectPublisher.__module__ == facade_name
assert {
    name
    for name, value in facade.ProjectPublisher.__dict__.items()
    if inspect.isfunction(value)
} == stateful_methods

replacement_hook = lambda *_args: None
facade._after_replacement = replacement_hook
assert "_after_replacement" in facade.ProjectPublisher._advance_plan.__code__.co_names
assert facade.ProjectPublisher._advance_plan.__globals__["_after_replacement"] is (
    replacement_hook
)
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
