"""Authority facade preserves public identities across focused modules."""

from lockstep.recipe import authority
from lockstep.recipe._authority_models import (
    AdmittedRecipe,
    AuthorityDenied,
    AuthorityRequirement,
    AuthorizedMaterialization,
    AuthorizedRecipe,
    CanonicalRecipeFile,
    OwnerReviewedGrant,
    OwnerReviewedPythonTarget,
    RecipeAuthorityError,
    RecipeAuthorityPolicy,
    RecipeCandidate,
    RecipeLimits,
)
from lockstep.recipe._recipe_document import (
    canonical_execution_bytes,
    recipe_definition_sha256,
)
from lockstep.recipe._recipe_ingress import StrictRecipeIngress


def test_authority_facade_reexports_focused_module_identities() -> None:
    expected = {
        "AdmittedRecipe": AdmittedRecipe,
        "AuthorityDenied": AuthorityDenied,
        "AuthorityRequirement": AuthorityRequirement,
        "AuthorizedMaterialization": AuthorizedMaterialization,
        "AuthorizedRecipe": AuthorizedRecipe,
        "CanonicalRecipeFile": CanonicalRecipeFile,
        "OwnerReviewedGrant": OwnerReviewedGrant,
        "OwnerReviewedPythonTarget": OwnerReviewedPythonTarget,
        "RecipeAuthorityError": RecipeAuthorityError,
        "RecipeAuthorityPolicy": RecipeAuthorityPolicy,
        "RecipeCandidate": RecipeCandidate,
        "RecipeLimits": RecipeLimits,
        "StrictRecipeIngress": StrictRecipeIngress,
        "canonical_execution_bytes": canonical_execution_bytes,
        "recipe_definition_sha256": recipe_definition_sha256,
    }

    for name, value in expected.items():
        assert getattr(authority, name) is value

    assert set(authority.__all__) == set(expected)
