"""Dataset-scoped RAGFlow tools for the regional-platform DeerFlow agents.

The public tool signatures intentionally do not accept ``dataset_ids``.  This
prevents the model from widening retrieval scope, even if it ignores a Skill.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool
from ragflow_tools import ragflow_search as _ragflow_search


GENERAL_DATASET_ID = "2fc6554e857011f18194cb9696436b2e"
MARKDOWN_DATASET_ID = "66a8675c8ca811f18194cb9696436b2e"


def _validate_parameters(
    query: str,
    similarity_threshold: float,
    vector_similarity_weight: float,
) -> None:
    if not query or not query.strip():
        raise ValueError("query must not be empty")
    if not 0 <= similarity_threshold <= 1:
        raise ValueError("similarity_threshold must be between 0 and 1")
    if not 0 <= vector_similarity_weight <= 1:
        raise ValueError("vector_similarity_weight must be between 0 and 1")


def _invoke_scoped_search(
    *,
    query: str,
    dataset_id: str,
    similarity_threshold: float,
    vector_similarity_weight: float,
) -> Any:
    _validate_parameters(query, similarity_threshold, vector_similarity_weight)
    arguments = {
        "query": query.strip(),
        "dataset_ids": [dataset_id],
        "similarity_threshold": similarity_threshold,
        "vector_similarity_weight": vector_similarity_weight,
    }

    # ragflow_search is normally a LangChain StructuredTool in DeerFlow.
    if hasattr(_ragflow_search, "invoke"):
        return _ragflow_search.invoke(arguments)
    if callable(_ragflow_search):
        return _ragflow_search(**arguments)
    raise TypeError("ragflow_tools.ragflow_search is not callable")


@tool
def regional_platform_general_search(
    query: str,
    similarity_threshold: float = 0.2,
    vector_similarity_weight: float = 0.5,
) -> Any:
    """Search only the 区卫平台6.0标准方案 general-document dataset."""

    return _invoke_scoped_search(
        query=query,
        dataset_id=GENERAL_DATASET_ID,
        similarity_threshold=similarity_threshold,
        vector_similarity_weight=vector_similarity_weight,
    )


@tool
def regional_platform_markdown_search(
    query: str,
    similarity_threshold: float = 0.2,
    vector_similarity_weight: float = 0.5,
) -> Any:
    """Search only the 区卫平台6.0标准方案（MD版） dataset."""

    return _invoke_scoped_search(
        query=query,
        dataset_id=MARKDOWN_DATASET_ID,
        similarity_threshold=similarity_threshold,
        vector_similarity_weight=vector_similarity_weight,
    )


__all__ = [
    "regional_platform_general_search",
    "regional_platform_markdown_search",
]
