"""Title-first, repetition-resistant retrieval over the existing schema-1 Reader.

Only this search binding changes. Release pinning, reads and access boundaries
remain owned by the configured Reader. No model calls or filesystem writes.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping

from langchain.tools import tool

from deerflow.tools.builtins import regional_llm_wiki_tool as legacy

_ALIASES = (("家庭医生", "家医"), ("妇幼保健", "妇幼"), ("公共卫生", "公卫"), ("医疗卫生共同体", "医共体"), ("医疗联合体", "医联体"))
_INTENTS = {"solution": ("解决方案", "建设方案", "方案"), "manual": ("操作手册", "用户手册", "使用手册"), "parameters": ("技术参数", "控标参数", "参数"), "introduction": ("产品介绍", "产品说明")}
_FILLERS = re.compile("介绍一下|帮我找一下|请问|帮我|查一下|找一下|有没有|有哪些|是什么|相关的|相关|资料|内容|信息系统|系统|关于|一下|我们|什么|如何|的|吗|呢|请")
CONTENT_STATE_VERSION = "document-content-v2"
_PLACEHOLDER_GUIDANCE = (
    "本文档内容待填充。其他原件见子目录。",
    "本文档内容待填充。涵盖区域资源预约、双向转诊、多病共管三个小产品，暂无原件，待入库时补充。",
    "本文档内容待填充。请整理各产品线的标杆案例信息。",
    "本文档内容待填充。请整理总客户盘点信息。",
    "本目录内容待填充（需求规格说明/接口方案/部署手册/操作手册等设计类文档，或 Axure/Figma 原型代码）。",
    "本目录内容待填充（需求规格说明/接口方案/部署手册/操作手册等设计类文档，或 Axure/Figma 原型代码）。区域妇幼原型见子目录 `00-区域妇幼/`。",
    "通用测试报告分类，暂无资料。功能测试报告见子目录 `00-功能测试报告/`。",
)
_TEMPLATE = re.compile(
    r"(?:(?:内容)?待填充[。.!！…]*|占位(?:文档|页面|页)?[。.!！…]*|(?:本目录|本文档)内容待填充[。.!！…]*"
    r"|请将原始(?:文件内容|接口文档)提取为 Markdown 填入此处[。.]?"
    r"|包括控标点、演示话术、截图素材等|可使用 Skill [A-Za-z0-9_.-]+ 自动生成"
    r"|通用测试报告分类，暂无资料。|" + "|".join(re.escape(line) for line in _PLACEHOLDER_GUIDANCE) + r")"
)


def document_content_state(text: str) -> str:
    """Ignore only an initial plain H1 title, never factual section headings."""
    body = []
    seen_content = False
    for raw in text.splitlines():
        line = re.sub(r"^>\s*", "", raw.strip()).strip()
        if not line or re.fullmatch(r"[-*_]{3,}", line):
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)(?:\s+#+)?$", line)
        if heading:
            line = heading.group(2)
            # Ambiguous or information-bearing titles remain substantive.
            if not seen_content and heading.group(1) == "#" and not re.search(r"[0-9:：=。！？!?]|支持|必须|禁止|不少于|不超过", line):
                seen_content = True
                continue
        seen_content = True
        body.append(line)
    if not body:
        return "empty"
    if len(text.strip()) <= 1500 and all(_TEMPLATE.fullmatch(line) for line in body):
        return "placeholder"
    return "substantive"


def _normalize(value: str) -> str:
    normalized = value.casefold()
    for long, short in _ALIASES:
        normalized = normalized.replace(long, short)
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", normalized)


def _query(query: str):
    topic = _normalize(query)
    intents = []
    for kind, forms in _INTENTS.items():
        if any(word in topic for word in forms):
            intents.append(kind)
            for word in forms:
                topic = topic.replace(word, "")
    topic = _FILLERS.sub("", topic)
    terms = set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", topic))
    for run in re.findall(r"[\u3400-\u9fff]+", topic):
        terms.update(run[i : i + 2] for i in range(len(run) - 1))
    return topic, sorted(terms), intents


def _page_state(page: Mapping, states: Mapping[str, str]) -> str:
    if page.get("fact_eligible") is False:
        return page.get("content_state", "placeholder")
    declared = page.get("content_state")
    if declared in {"placeholder", "empty", "substantive", "mixed"}:
        return declared
    values = [states.get(item, "unknown") for item in page.get("evidence_ids", [])]
    if values and all(value in {"placeholder", "empty"} for value in values):
        return "placeholder" if "placeholder" in values else "empty"
    if "placeholder" in values or "empty" in values:
        return "mixed"
    return "unknown"


def rank_pages(pages, query: str, *, domain: str | None = None, scope: str = "facts", evidence_states: Mapping[str, str] | None = None):
    """Use field presence and topic coverage, never raw keyword frequency."""
    topic, terms, intents = _query(query)
    candidates = []
    for page in pages:
        if domain is not None and page.get("domain") != domain:
            continue
        state = _page_state(page, evidence_states or {})
        if scope == "facts" and state in {"placeholder", "empty"}:
            continue
        title = _normalize(str(page.get("title", "")))
        summary = _normalize(str(page.get("summary", "")))
        keywords = _normalize(" ".join(str(item) for item in page.get("keywords", [])))
        weights = {term: 3 if len(term) > 2 else 1 for term in terms}
        denominator = sum(weights.values()) or 1

        def coverage(text):
            return sum(weight for term, weight in weights.items() if term in text) / denominator

        title_match, summary_match, keyword_match = coverage(title), coverage(summary), coverage(keywords)
        intent_match = any(any(form in title for form in _INTENTS[kind]) for kind in intents)
        if terms and not (title_match or summary_match or keyword_match):
            continue
        if not terms and (not intents or not intent_match):
            continue
        score = 30 * title_match + 5 * summary_match + 4 * keyword_match
        if topic and topic in title:
            score += 12
        if page.get("type") == "source" and title_match:
            score += 6
            # Prefer whole source navigation over many pieces of one manual.
            score += -8 if str(page["page_id"]).startswith("source-table-group:") else -6 if "/compat-parts/" in str(page.get("path", "")) else 2
            if any(form in title for form in _INTENTS["solution"]):
                score += 3
        if intent_match:
            score += 12 * (title_match if terms else 1)
        candidates.append((score, page))
    candidates.sort(key=lambda item: (-item[0], str(item[1]["page_id"])))
    result, seen_sources = [], set()
    for _, page in candidates:
        # Root source and its deterministic pieces must not monopolize top-k.
        documents = page.get("source_document_ids")
        key = tuple(sorted(documents)) if page.get("type") == "source" and isinstance(documents, list) and documents else None
        if key is not None and key in seen_sources:
            continue
        if key is not None:
            seen_sources.add(key)
        result.append(page)
    return result


def _legacy_evidence_states(release) -> dict[str, str]:
    """Old or stale exports: verify small whole documents, not summaries.

    A short tail shard is never sufficient to classify its original document.
    Large sources remain unknown here and are still eligible for retrieval.
    """
    states = {}
    for item in release.manifest["evidence"]:
        if not isinstance(item, dict) or not isinstance(item.get("evidence_id"), str):
            raise legacy._ReleaseError("integrity_failed")
        declared = item.get("content_state")
        if release.manifest.get("content_state_version") == CONTENT_STATE_VERSION and declared in {"substantive", "placeholder", "empty"}:
            states[item["evidence_id"]] = declared
            continue
        relative = legacy._safe_relative_path(item.get("path"), required_prefix="evidence")
        if relative.parts[:2] != ("evidence", "wiki"):
            continue
        path = release.root
        for part in relative.parts:
            path /= part
            if path.is_symlink():
                raise legacy._ReleaseError("integrity_failed")
        path.resolve(strict=True).relative_to(release.root.resolve(strict=True))
        if path.stat().st_size <= 6000:
            body = legacy._read_limited(path, expected_sha256=item.get("sha256"), max_chars=6000)
            states[item["evidence_id"]] = document_content_state(body)
    return states


def _search(query: str, domain: str | None, limit: int, *, scope: str = "facts") -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(limit) is not int or not isinstance(scope, str) or scope not in {"facts", "catalog"}:
        return legacy._response("invalid_request")
    if domain is not None and (not isinstance(domain, str) or len(domain) > 128):
        return legacy._response("invalid_request")
    try:
        release = legacy._open_release()
        pages = release.manifest["pages"]
        if any(not isinstance(page, dict) or not isinstance(page.get("page_id"), str) for page in pages):
            raise legacy._ReleaseError("integrity_failed")
        states = _legacy_evidence_states(release)
        document_ids = {}
        for entry in release.manifest["evidence"]:
            relative = str(entry.get("path", "")).split("/")
            document_ids[entry["evidence_id"]] = entry.get("source_document_id") or ("sharded:" + relative[2] if len(relative) > 3 and relative[1] in {"compat-shards", "compat-fields"} else entry["evidence_id"])
        pages = [{**page, "source_document_ids": page.get("source_document_ids") or sorted({document_ids[item] for item in page.get("evidence_ids", []) if item in document_ids})} for page in pages]
        if release.manifest.get("content_state_version") != CONTENT_STATE_VERSION:
            # Never trust a stale negative classification or modify a pinned
            # manifest. Unknown large/sharded documents remain retrievable.
            pages = [{key: value for key, value in page.items() if key not in {"content_state", "fact_eligible"}} for page in pages]
        selected = rank_pages(pages, query, domain=domain, scope=scope, evidence_states=states)[: min(max(1, limit), release.max_search_results)]
        return legacy._response(
            "ok",
            release_id=release.release_id,
            result_scope=scope,
            pages=[{**{key: page.get(key) for key in ("page_id", "title", "type", "domain")}, "content_state": _page_state(page, states), "fact_eligible": _page_state(page, states) not in {"placeholder", "empty"}} for page in selected],
            scope_note="文档缺资料状态仅适用于该文档，不能推断整个产品缺资料；搜索结果须读取页面核验。",
        )
    except legacy._ReleaseError as error:
        return legacy._response(error.code)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        return legacy._response("integrity_failed")


@tool("search_llm_wiki", parse_docstring=True)
async def search_llm_wiki(query: str, domain: str | None = None, limit: int = 5, scope: str = "facts") -> str:
    """Search published Wiki pages, prioritizing topic titles and document intent.

    Args:
        query: User question or concise topic keywords.
        domain: Optional exact first-level domain name.
        limit: Requested candidate count, capped by the Reader configuration.
        scope: facts excludes placeholder-only pages; catalog explicitly includes missing-document entries.
    """
    return await asyncio.to_thread(_search, query, domain, limit, scope=scope)
