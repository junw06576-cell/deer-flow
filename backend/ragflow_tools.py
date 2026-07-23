"""
RAGFlow knowledge base retrieval tools for DeerFlow integration.

Supports dynamic dataset selection:
  - By default, uses RAGFLOW_DATASET_IDS from environment
  - Override per-query by passing dataset_ids parameter
  - List all available datasets with ragflow_list_datasets tool

Requires environment variables:
  RAGFLOW_API_KEY  - RAGFlow API key (from RAGFlow UI -> Avatar -> API)
  RAGFLOW_DATASET_IDS - Comma-separated default dataset IDs to search
  RAGFLOW_BASE_URL - RAGFlow API base URL (default: http://host.docker.internal:9380)
"""

import os
import httpx
from langchain_core.tools import tool

_RAGFLOW_BASE_URL = os.getenv("RAGFLOW_BASE_URL", "http://host.docker.internal:9380")
_RAGFLOW_API_KEY = os.getenv("RAGFLOW_API_KEY", "")
_DATASET_IDS_STR = os.getenv("RAGFLOW_DATASET_IDS", "")
_CHAT_ID = os.getenv("RAGFLOW_CHAT_ID", "")


def _get_default_dataset_ids() -> list[str]:
    """Parse comma-separated dataset IDs from environment."""
    if not _DATASET_IDS_STR:
        return []
    return [did.strip() for did in _DATASET_IDS_STR.split(",") if did.strip()]


def _parse_dataset_ids(dataset_ids: str | None) -> list[str] | None:
    """Parse a comma-separated dataset_ids string into a list.
    Returns None if input is empty/None (meaning: use defaults).
    """
    if not dataset_ids or not dataset_ids.strip():
        return None
    ids = [did.strip() for did in dataset_ids.split(",") if did.strip()]
    return ids if ids else None


@tool
def ragflow_search(query: str, dataset_ids: str | None = None) -> str:
    """Search the RAGFlow knowledge base for relevant document chunks.

    IMPORTANT: You SHOULD use this tool as the FIRST step for ANY user question,
    especially when the question involves internal documents, product information,
    company details, project lists, case studies, healthcare informatization,
    or any domain-specific knowledge. Do NOT rely solely on your own knowledge —
    always check the knowledge base first.

    Args:
        query: The search query or question to look up in the knowledge base.
        dataset_ids: Optional comma-separated dataset IDs to search. If not provided,
            uses the default RAGFLOW_DATASET_IDS from environment. Use ragflow_list_datasets
            to discover available datasets. Example: "id1,id2,id3"
    """
    resolved_ids = _parse_dataset_ids(dataset_ids) or _get_default_dataset_ids()
    if not resolved_ids:
        return (
            "[RAGFlow] Error: No dataset IDs configured. Either pass dataset_ids "
            "parameter or set RAGFLOW_DATASET_IDS in .env. "
            "Use ragflow_list_datasets to discover available datasets."
        )
    if not _RAGFLOW_API_KEY:
        return "[RAGFlow] Error: RAGFLOW_API_KEY not configured. Set it in .env."

    url = f"{_RAGFLOW_BASE_URL}/api/v1/retrieval"
    headers = {
        "Authorization": f"Bearer {_RAGFLOW_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "question": query,
        "dataset_ids": resolved_ids,
        "page": 1,
        "page_size": 6,
        "similarity_threshold": 0.2,
        "vector_similarity_weight": 0.3,
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            return f"[RAGFlow] API error: {data.get('message', 'Unknown error')}"

        # RAGFlow returns {"code": 0, "data": {"chunks": [...], "doc_aggs": [...], "total": N}}
        data_body = data.get("data", {})
        if isinstance(data_body, dict):
            chunks = data_body.get("chunks", [])
        elif isinstance(data_body, list):
            chunks = data_body
        else:
            chunks = []

        if not chunks:
            return "No relevant content found in the knowledge base for this query."

        results = []
        for i, chunk in enumerate(chunks[:6], 1):
            content = chunk.get("content", "").strip()
            doc_name = chunk.get("document_keyword", "Unknown")
            similarity = chunk.get("similarity", 0)
            dataset_id = chunk.get("dataset_id", "")
            kb_label = f" [dataset: {dataset_id}]" if dataset_id else ""
            results.append(
                f"[{i}] Source: {doc_name} (similarity: {similarity:.2f}){kb_label}\n{content}\n"
            )

        return "\n---\n".join(results)

    except httpx.ConnectError:
        return (
            f"[RAGFlow] Connection error: Cannot reach {_RAGFLOW_BASE_URL}. "
            "Ensure RAGFlow is running and the URL is correct."
        )
    except httpx.TimeoutException:
        return "[RAGFlow] Request timed out. The knowledge base may be busy."
    except Exception as e:
        return f"[RAGFlow] Error: {str(e)}"


@tool
def ragflow_list_datasets(page: int = 1, page_size: int = 100) -> str:
    """List all available datasets (knowledge bases) in RAGFlow.

    Use this tool to discover which datasets exist and get their IDs.
    Then use the dataset IDs with ragflow_search's dataset_ids parameter
    to search specific knowledge bases.

    Args:
        page: Page number (default: 1)
        page_size: Number of results per page (default: 100)
    """
    if not _RAGFLOW_API_KEY:
        return "[RAGFlow] Error: RAGFLOW_API_KEY not configured. Set it in .env."

    url = f"{_RAGFLOW_BASE_URL}/api/v1/datasets"
    headers = {
        "Authorization": f"Bearer {_RAGFLOW_API_KEY}",
    }
    params = {
        "page": page,
        "page_size": page_size,
    }

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            return f"[RAGFlow] API error: {data.get('message', 'Unknown error')}"

        datasets = data.get("data", [])
        if not datasets:
            return "No datasets found in RAGFlow."

        results = ["Available RAGFlow Datasets:\n"]
        for ds in datasets:
            ds_id = ds.get("id", "N/A")
            name = ds.get("name", "Unnamed")
            doc_count = ds.get("document_count", ds.get("doc_num", 0))
            chunk_count = ds.get("chunk_num", 0)
            created = ds.get("create_date", ds.get("create_time", "N/A"))
            results.append(
                f"  • {name}\n"
                f"    ID: {ds_id}\n"
                f"    Documents: {doc_count} | Chunks: {chunk_count}\n"
                f"    Created: {created}\n"
            )

        total = data.get("total", len(datasets))
        if total > page * page_size:
            results.append(f"\n... showing page {page}, total {total} datasets. Use page={page+1} for more.")

        # Show current default config
        defaults = _get_default_dataset_ids()
        if defaults:
            results.append(f"\nCurrent default dataset IDs (from .env): {', '.join(defaults)}")
        else:
            results.append("\nNo default dataset IDs configured in .env.")

        return "\n".join(results)

    except httpx.ConnectError:
        return (
            f"[RAGFlow] Connection error: Cannot reach {_RAGFLOW_BASE_URL}. "
            "Ensure RAGFlow is running and the URL is correct."
        )
    except httpx.TimeoutException:
        return "[RAGFlow] Request timed out."
    except Exception as e:
        return f"[RAGFlow] Error: {str(e)}"


@tool
def ragflow_chat(query: str, dataset_ids: str | None = None) -> str:
    """Ask a question through RAGFlow's chat API with knowledge base grounding.
    The answer is generated by RAGFlow's configured LLM using retrieved context.
    Use this when you need a grounded, well-referenced answer from the knowledge base
    rather than just raw document chunks.

    Args:
        query: The question to ask the RAGFlow knowledge base assistant.
        dataset_ids: Optional comma-separated dataset IDs to restrict the chat scope.
            If not provided, the RAGFlow assistant uses its own configured datasets.
    """
    if not _RAGFLOW_API_KEY:
        return "[RAGFlow] Error: RAGFLOW_API_KEY not configured. Set it in .env."
    if not _CHAT_ID:
        return (
            "[RAGFlow] Error: RAGFLOW_CHAT_ID not configured. "
            "Create an assistant in RAGFlow, get its ID, and set RAGFLOW_CHAT_ID in .env."
        )

    url = f"{_RAGFLOW_BASE_URL}/api/v1/openai/{_CHAT_ID}/chat/completions"
    headers = {
        "Authorization": f"Bearer {_RAGFLOW_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "model",
        "messages": [{"role": "user", "content": query}],
        "stream": False,
    }

    # If specific dataset IDs are provided, pass them to the chat API
    resolved_ids = _parse_dataset_ids(dataset_ids)
    if resolved_ids:
        payload["dataset_ids"] = resolved_ids

    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        data = resp.json()

        choices = data.get("choices", [])
        if not choices:
            return "[RAGFlow] No response generated."

        message = choices[0].get("message", {})
        content = message.get("content", "No answer generated.")

        # Append references if available
        refs = []
        if "reference" in choices[0]:
            for ref in choices[0]["reference"]:
                doc_name = ref.get("document_keyword", "Unknown")
                refs.append(doc_name)
        if refs:
            content += f"\n\nReferences: {', '.join(refs)}"

        return content

    except httpx.ConnectError:
        return (
            f"[RAGFlow] Connection error: Cannot reach {_RAGFLOW_BASE_URL}. "
            "Ensure RAGFlow is running and the URL is correct."
        )
    except httpx.TimeoutException:
        return "[RAGFlow] Request timed out."
    except Exception as e:
        return f"[RAGFlow] Error: {str(e)}"
