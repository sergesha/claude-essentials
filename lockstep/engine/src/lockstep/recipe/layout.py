"""Shared compiler and discovery recipe-directory vocabulary."""

from enum import StrEnum


class RecipeDirectory(StrEnum):
    GENERATED_CHILDREN = "generated/children"
