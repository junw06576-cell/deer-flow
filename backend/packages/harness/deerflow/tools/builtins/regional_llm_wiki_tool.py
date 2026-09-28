"""Read-only, manifest-gated access to a published Regional LLM Wiki release.

These tools intentionally run in Gateway, rather than in the general sandbox.
The only mounted input is the maintainer's published release parent.  Callers
never provide filesystem paths: all reads are resolved through validated IDs in
``agent-access-manifest.json``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.tools import tool

from deerflow.config import get_app_config

_MANIFEST_NAME = "agent-access-manifest.json"
_CURRENT_NAME = "current"
_RELEASES_NAME = "releases"
_MAX_QUERY_CHARS = 500


class _ReleaseError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Release:
    root: Path
    agent_release_id: str
    wiki_release_id: str
    schema_version: int
    manifest: dict[str, Any]
    max_search_results: int
    max_page_chars: int
    max_evidence_chars: int


def _response(status: str, **payload: object) -> str:
    return json.dumps({"status": status, **payload}, ensure_ascii=False, sort_keys=True)


def _safe_relative_path(value: object, *, required_prefix: str) -> Path:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise _ReleaseError("integrity_failed")
    path = Path(value)
    if any(part in {"", ".", ".."} for part in path.parts) or not path.parts or path.parts[0] != required_prefix:
        raise _ReleaseError("integrity_failed")
    return path


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value) is not None


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _schema2_agent_release_id(manifest: dict[str, Any]) -> str:
    """Reproduce the compiler's immutable Agent export revision identifier."""
    stable_manifest = {key: value for key, value in manifest.items() if key not in {"published_at", "agent_release_id"}}
    canonical = json.dumps(stable_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    wiki_release_id = stable_manifest.get("release_id")
    if not _valid_identifier(wiki_release_id):
        raise _ReleaseError("integrity_failed")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{wiki_release_id}-{digest}"


def _validate_manifest(manifest: object, release_root: Path) -> tuple[int, str, str]:
    if not isinstance(manifest, dict):
        raise _ReleaseError("integrity_failed")
    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise _ReleaseError("integrity_failed")

    wiki_release_id = manifest.get("release_id")
    if not _valid_identifier(wiki_release_id):
        raise _ReleaseError("integrity_failed")
    if not isinstance(manifest.get("source_commit"), str) or not manifest["source_commit"]:
        raise _ReleaseError("integrity_failed")

    if schema_version == 1:
        agent_release_id = wiki_release_id
        evidence_release_id = manifest.get("evidence_release_id", wiki_release_id)
    else:
        agent_release_id = manifest.get("agent_release_id")
        evidence_release_id = manifest.get("evidence_release_id")
        if not _valid_identifier(agent_release_id):
            raise _ReleaseError("integrity_failed")
        for count_name in ("fallback_source_count", "fallback_table_group_count", "acceptance_quarantine_count"):
            count = manifest.get(count_name)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise _ReleaseError("integrity_failed")
        if agent_release_id != _schema2_agent_release_id(manifest):
            raise _ReleaseError("integrity_failed")

    if evidence_release_id != wiki_release_id or release_root.name != agent_release_id:
        raise _ReleaseError("integrity_failed")
    if not isinstance(manifest.get("pages"), list) or not isinstance(manifest.get("evidence"), list):
        raise _ReleaseError("integrity_failed")

    page_ids: set[str] = set()
    page_paths: set[str] = set()
    page_evidence_ids: list[str] = []
    for page in manifest["pages"]:
        if not isinstance(page, dict) or not _valid_identifier(page.get("page_id")):
            raise _ReleaseError("integrity_failed")
        page_id = page["page_id"]
        path = page.get("path")
        _safe_relative_path(path, required_prefix="llm-wiki")
        evidence_ids = page.get("evidence_ids")
        keywords = page.get("keywords", [])
        if (
            page_id in page_ids
            or path in page_paths
            or not _valid_sha256(page.get("sha256"))
            or not isinstance(page.get("title"), str)
            or not isinstance(page.get("type"), str)
            or not isinstance(page.get("domain"), str)
            or not isinstance(page.get("summary", ""), str)
            or not isinstance(keywords, list)
            or not all(isinstance(keyword, str) for keyword in keywords)
            or not isinstance(evidence_ids, list)
            or not all(_valid_identifier(evidence_id) for evidence_id in evidence_ids)
            or len(evidence_ids) != len(set(evidence_ids))
        ):
            raise _ReleaseError("integrity_failed")
        page_ids.add(page_id)
        page_paths.add(path)
        page_evidence_ids.extend(evidence_ids)

    evidence_ids: set[str] = set()
    evidence_paths: set[str] = set()
    for evidence in manifest["evidence"]:
        if not isinstance(evidence, dict) or not _valid_identifier(evidence.get("evidence_id")):
            raise _ReleaseError("integrity_failed")
        evidence_id = evidence["evidence_id"]
        path = evidence.get("path")
        _safe_relative_path(path, required_prefix="evidence")
        locations = evidence.get("allowed_locations")
        tfs_url = evidence.get("tfs_url")
        if (
            evidence_id in evidence_ids
            or path in evidence_paths
            or not _valid_sha256(evidence.get("sha256"))
            or not isinstance(locations, list)
            or not all(isinstance(location, str) and location for location in locations)
            or (tfs_url is not None and (not isinstance(tfs_url, str) or not tfs_url.startswith(("http://", "https://"))))
        ):
            raise _ReleaseError("integrity_failed")
        evidence_ids.add(evidence_id)
        evidence_paths.add(path)

    if not evidence_ids or not set(page_evidence_ids).issubset(evidence_ids) or evidence_ids - set(page_evidence_ids):
        raise _ReleaseError("integrity_failed")
    return schema_version, agent_release_id, wiki_release_id


def _read_limited(path: Path, *, expected_sha256: object, max_chars: int) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise _ReleaseError("integrity_failed")
        content = path.read_text(encoding="utf-8")
    except _ReleaseError:
        raise
    except (OSError, UnicodeError):
        raise _ReleaseError("integrity_failed") from None
    if not isinstance(expected_sha256, str) or hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_sha256:
        raise _ReleaseError("integrity_failed")
    return content[:max_chars]


def _open_release(agent_release_id: str | None = None) -> _Release:
    config = get_app_config().regional_llm_wiki
    if not config.enabled:
        raise _ReleaseError("release_unavailable")
    try:
        runtime_root = Path(config.runtime_root).resolve(strict=True)
        releases_root = (runtime_root / _RELEASES_NAME).resolve(strict=True)
        releases_root.relative_to(runtime_root)
        if agent_release_id is None:
            current = runtime_root / _CURRENT_NAME
            if not current.is_symlink():
                raise _ReleaseError("integrity_failed")
            release_root = current.resolve(strict=True)
        else:
            if not _valid_identifier(agent_release_id):
                raise _ReleaseError("not_found")
            release_root = (releases_root / agent_release_id).resolve(strict=True)
        release_root.relative_to(releases_root)
        manifest_path = release_root / _MANIFEST_NAME
        if manifest_path.is_symlink():
            raise _ReleaseError("integrity_failed")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except _ReleaseError:
        raise
    except FileNotFoundError:
        if agent_release_id is not None:
            raise _ReleaseError("not_found") from None
        raise _ReleaseError("release_unavailable") from None
    except ValueError:
        raise _ReleaseError("integrity_failed") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _ReleaseError("release_unavailable") from None

    schema_version, resolved_agent_release_id, wiki_release_id = _validate_manifest(manifest, release_root)
    if agent_release_id is not None and agent_release_id != resolved_agent_release_id:
        raise _ReleaseError("integrity_failed")
    return _Release(
        root=release_root,
        agent_release_id=resolved_agent_release_id,
        wiki_release_id=wiki_release_id,
        schema_version=schema_version,
        manifest=manifest,
        max_search_results=config.max_search_results,
        max_page_chars=config.max_page_chars,
        max_evidence_chars=config.max_evidence_chars,
    )


def _release_for_id(agent_release_id: str) -> _Release:
    # A search response pins a version for one answer.  Maintenance retains at
    # least the preceding release, so a publish flip cannot mix old index data
    # with new page content during that answer.
    return _open_release(agent_release_id)


def _normalize_agent_release_id(agent_release_id: str | None, legacy_release_id: str | None) -> str:
    if agent_release_id and legacy_release_id and agent_release_id != legacy_release_id:
        raise _ReleaseError("invalid_request")
    value = agent_release_id or legacy_release_id
    if not _valid_identifier(value):
        raise _ReleaseError("invalid_request")
    return value


def _page(release: _Release, page_id: str) -> dict[str, Any]:
    for item in release.manifest["pages"]:
        if isinstance(item, dict) and item.get("page_id") == page_id:
            return item
    raise _ReleaseError("not_found")


def _evidence(release: _Release, evidence_id: str) -> dict[str, Any]:
    for item in release.manifest["evidence"]:
        if isinstance(item, dict) and item.get("evidence_id") == evidence_id:
            return item
    raise _ReleaseError("not_found")


def _search_terms(query: str) -> list[str]:
    """Produce deterministic CJK-friendly terms without an external tokenizer."""
    normalized = query.casefold()
    compact = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", normalized)
    terms = [term for term in re.findall(r"[0-9a-z]+|[\u3400-\u9fff]+", normalized) if term]
    if compact:
        terms.append(compact)
        terms.extend(compact[index : index + 2] for index in range(max(0, len(compact) - 1)))
    return list(dict.fromkeys(term for term in terms if len(term) >= 2 or term.isascii()))


def _search(query: str, domain: str | None, limit: int) -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_CHARS or not isinstance(limit, int) or isinstance(limit, bool):
        return _response("invalid_request")
    try:
        release = _open_release()
        if domain is not None and (not isinstance(domain, str) or len(domain) > 128):
            return _response("invalid_request")
        requested_limit = min(max(1, limit), release.max_search_results)
        terms = _search_terms(query)
        candidates: list[tuple[int, dict[str, Any]]] = []
        for page in release.manifest["pages"]:
            if not isinstance(page, dict) or not isinstance(page.get("page_id"), str):
                raise _ReleaseError("integrity_failed")
            if domain is not None and page.get("domain") != domain:
                continue
            searchable = " ".join(str(page.get(key, "")) for key in ("page_id", "title", "type", "domain", "summary", "keywords")).casefold()
            searchable_compact = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", searchable)
            score = sum(searchable.count(term) + searchable_compact.count(term) for term in terms)
            if score:
                candidates.append((score, page))
        candidates.sort(key=lambda entry: (-entry[0], str(entry[1]["page_id"])))
        return _response(
            "ok",
            agent_release_id=release.agent_release_id,
            release_id=release.agent_release_id,
            wiki_release_id=release.wiki_release_id,
            pages=[
                {
                    "page_id": page["page_id"],
                    "title": page.get("title", page["page_id"]),
                    "type": page.get("type"),
                    "domain": page.get("domain"),
                }
                for _, page in candidates[:requested_limit]
            ],
        )
    except _ReleaseError as error:
        return _response(error.code)
    except (OSError, UnicodeError, ValueError):
        return _response("integrity_failed")


@tool("search_llm_wiki", parse_docstring=True)
async def search_llm_wiki(query: str, domain: str | None = None, limit: int = 5) -> str:
    """Search the current published Regional LLM Wiki index.

    Args:
        query: The user's knowledge question or concise search terms.
        domain: Optional published first-level domain name.
        limit: Maximum candidate pages to return, from 1 to 5.
    """
    return await asyncio.to_thread(_search, query, domain, limit)


def _read_page(agent_release_id: str, page_id: str) -> str:
    try:
        release = _release_for_id(agent_release_id)
        page = _page(release, page_id)
        relative_path = _safe_relative_path(page.get("path"), required_prefix="llm-wiki")
        file_path = (release.root / relative_path).resolve(strict=True)
        file_path.relative_to(release.root)
        content = _read_limited(file_path, expected_sha256=page.get("sha256"), max_chars=release.max_page_chars)
        evidence_ids = page.get("evidence_ids", [])
        if not isinstance(evidence_ids, list) or not all(isinstance(item, str) for item in evidence_ids):
            raise _ReleaseError("integrity_failed")
        return _response(
            "ok",
            agent_release_id=release.agent_release_id,
            release_id=release.agent_release_id,
            wiki_release_id=release.wiki_release_id,
            page_id=page_id,
            title=page.get("title", page_id),
            type=page.get("type"),
            domain=page.get("domain"),
            evidence_ids=evidence_ids,
            content=content,
        )
    except _ReleaseError as error:
        return _response(error.code)
    except (OSError, UnicodeError, ValueError):
        return _response("integrity_failed")


@tool("read_llm_wiki_page", parse_docstring=True)
async def read_llm_wiki_page(
    page_id: str,
    agent_release_id: str | None = None,
    release_id: str | None = None,
) -> str:
    """Read one manifest-approved LLM Wiki page from a pinned export.

    Args:
        page_id: A candidate page ID returned by search_llm_wiki.
        agent_release_id: The immutable Agent export ID returned by search_llm_wiki.
        release_id: Deprecated alias for agent_release_id for in-flight conversations.
    """
    try:
        selected_id = _normalize_agent_release_id(agent_release_id, release_id)
    except _ReleaseError as error:
        return _response(error.code)
    return await asyncio.to_thread(_read_page, selected_id, page_id)


def _read_evidence(agent_release_id: str, page_id: str, evidence_id: str, locator: str | None) -> str:
    try:
        release = _release_for_id(agent_release_id)
        page = _page(release, page_id)
        evidence_ids = page.get("evidence_ids", [])
        if not isinstance(evidence_ids, list) or evidence_id not in evidence_ids:
            raise _ReleaseError("not_found")
        evidence = _evidence(release, evidence_id)
        allowed_locations = evidence.get("allowed_locations", [])
        if not isinstance(allowed_locations, list) or not all(isinstance(item, str) for item in allowed_locations):
            raise _ReleaseError("integrity_failed")
        if locator is not None and locator not in allowed_locations:
            raise _ReleaseError("invalid_request")
        relative_path = _safe_relative_path(evidence.get("path"), required_prefix="evidence")
        file_path = (release.root / relative_path).resolve(strict=True)
        file_path.relative_to(release.root)
        content = _read_limited(file_path, expected_sha256=evidence.get("sha256"), max_chars=release.max_evidence_chars)
        return _response(
            "ok",
            agent_release_id=release.agent_release_id,
            release_id=release.agent_release_id,
            wiki_release_id=release.wiki_release_id,
            page_id=page_id,
            evidence_id=evidence_id,
            locator=locator,
            allowed_locations=allowed_locations,
            tfs_url=evidence.get("tfs_url"),
            content=content,
        )
    except _ReleaseError as error:
        return _response(error.code)
    except (OSError, UnicodeError, ValueError):
        return _response("integrity_failed")


@tool("read_llm_wiki_evidence", parse_docstring=True)
async def read_llm_wiki_evidence(
    page_id: str,
    evidence_id: str,
    agent_release_id: str | None = None,
    release_id: str | None = None,
    locator: str | None = None,
) -> str:
    """Read one evidence snapshot declared by an already-read LLM Wiki page.

    Args:
        page_id: The page ID returned by search_llm_wiki and read_llm_wiki_page.
        evidence_id: An evidence ID returned by read_llm_wiki_page for that page.
        agent_release_id: The immutable Agent export ID returned by search_llm_wiki.
        release_id: Deprecated alias for agent_release_id for in-flight conversations.
        locator: Optional declared line, section, or table-range locator.
    """
    try:
        selected_id = _normalize_agent_release_id(agent_release_id, release_id)
    except _ReleaseError as error:
        return _response(error.code)
    return await asyncio.to_thread(_read_evidence, selected_id, page_id, evidence_id, locator)


def _release_state() -> str:
    try:
        release = _open_release()
        return _response(
            "ok",
            agent_release_id=release.agent_release_id,
            release_id=release.agent_release_id,
            wiki_release_id=release.wiki_release_id,
            schema_version=release.schema_version,
            source_commit=release.manifest.get("source_commit"),
            published_at=release.manifest.get("published_at"),
            fallback_source_count=release.manifest.get("fallback_source_count", 0),
            fallback_table_group_count=release.manifest.get("fallback_table_group_count", 0),
            acceptance_quarantine_count=release.manifest.get("acceptance_quarantine_count", 0),
        )
    except _ReleaseError as error:
        return _response(error.code)


@tool("get_llm_wiki_release_state", parse_docstring=True)
async def get_llm_wiki_release_state() -> str:
    """Return safe metadata about the currently published Regional LLM Wiki release."""
    return await asyncio.to_thread(_release_state)
