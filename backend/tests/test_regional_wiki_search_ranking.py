"""Retrieval regressions use synthetic titles, never production business text."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations import regional_wiki_search as search


@pytest.mark.parametrize("case", json.loads((Path(__file__).parent / "fixtures/document-content-cases.json").read_text()), ids=lambda case: case["name"])
def test_conservative_classification_contract(case):
    assert search.document_content_state(case["text"]) == case["state"]


def page(page_id, title, kind="source", summary="", **fields):
    return {"page_id": page_id, "title": title, "type": kind, "domain": "01-产品策划中心", "summary": summary, "keywords": [title], "evidence_ids": [page_id], **fields}


def corpus():
    return [
        page("solution", "家医签约 解决方案", summary="面向家庭医生签约服务的系统方案。"),
        page("mobile", "移动公卫（家医）解决方案"),
        page("placeholder", "家医签约", kind="entity", content_state="placeholder", fact_eligible=False),
        page("intro", "家医签约 产品介绍", content_state="placeholder", fact_eligible=False),
        page("other", "区域平台解决方案", summary="家医 " * 400),
        *[page(f"news-{i}", f"家庭医生宣传材料 {i}", kind="entity", summary="家庭医生 家医签约 " * 100) for i in range(8)],
    ]


@pytest.mark.parametrize("query", ["家医", "家庭医生", "家医签约", "家医 方案", "家医相关的方案", "家庭医生签约信息系统解决方案"])
def test_solution_recalled_despite_repetition_alias_and_placeholders(query):
    results = search.rank_pages(corpus(), query)
    ids = [item["page_id"] for item in results[:5]]
    assert "solution" in ids[:3]
    assert "placeholder" not in ids
    assert "intro" not in ids


def test_placeholder_intro_does_not_mean_entire_product_is_missing():
    pages = [page("intro", "妇幼保健 产品介绍", content_state="placeholder", fact_eligible=False), page("solution", "妇幼保健 解决方案")]
    assert [item["page_id"] for item in search.rank_pages(pages, "妇幼 方案")] == ["solution"]
    catalog = search.rank_pages(pages, "妇幼", scope="catalog")
    assert {item["page_id"] for item in catalog} == {"intro", "solution"}
    assert search.rank_pages(pages, "妇幼 产品介绍", scope="catalog")[0]["page_id"] == "intro"


def test_topic_is_required_even_when_unrelated_solution_matches_intent():
    assert search.rank_pages([page("unrelated", "妇幼保健解决方案")], "家医方案") == []


def test_factual_sentence_mentioning_placeholder_is_not_excluded():
    assert search.document_content_state("# 家医\n\n签约模块已上线，培训部分待填充。\n") == "substantive"
    assert search.document_content_state("# 家医 产品介绍\n\n> 待填充\n> 请将原始文件内容提取为 Markdown 填入此处\n---\n") == "placeholder"
    assert search.document_content_state("# 家医 产品介绍\n\n---\n") == "empty"
    assert search.document_content_state("# 产品资料\n\n> 占位\n> 本文档内容待填充。其他原件见子目录。\n") == "placeholder"
    assert search.document_content_state("# 硬件资源\n\n> 待填充\n> 可使用 Skill example 自动生成\n") == "placeholder"


def test_mixed_page_is_retained_and_substantive_source_is_diversified():
    pages = [page("mixed", "家医签约", kind="concept", content_state="mixed", fact_eligible=True), *[page(f"group-{i}", "家医签约解决方案", source_document_ids=["same-source"]) for i in range(8)], page("other", "移动家医解决方案")]
    selected = search.rank_pages(pages, "家医")[:5]
    assert sum(item["page_id"].startswith("group-") for item in selected) == 1
    assert "mixed" in {item["page_id"] for item in selected}
    assert "other" in {item["page_id"] for item in selected}


def test_domain_and_limit_preserve_existing_api(monkeypatch, tmp_path):
    manifest = {"pages": corpus(), "evidence": []}
    release = SimpleNamespace(root=tmp_path, release_id="release-1", manifest=manifest, max_search_results=5)
    monkeypatch.setattr(search.legacy, "_open_release", lambda: release)
    result = json.loads(search._search("家医方案", None, 5))
    assert result["release_id"] == "release-1"
    assert result["pages"][0]["page_id"] == "solution"
    assert all("summary" not in item and "path" not in item for item in result["pages"])
    assert json.loads(search._search("家医", "02-资质与合规", 5))["pages"] == []
    assert json.loads(search._search("家医", None, True))["status"] == "invalid_request"
    assert json.loads(search._search("家医", None, 5, scope="unknown"))["status"] == "invalid_request"


def test_async_tool_offloads_and_keeps_name(monkeypatch):
    assert search.search_llm_wiki.name == "search_llm_wiki"
    assert "scope" in search.search_llm_wiki.args
    calls = []

    async def offload(function, *args, **kwargs):
        calls.append((function, args, kwargs))
        return '{"status":"ok"}'

    monkeypatch.setattr(search.asyncio, "to_thread", offload)
    assert asyncio.run(search.search_llm_wiki.ainvoke({"query": "家医"})) == '{"status":"ok"}'
    assert calls[0][0] is search._search


def test_legacy_placeholder_is_verified_from_evidence_not_title(monkeypatch, tmp_path):
    relative = "evidence/wiki/产品介绍.md"
    text = "# 家医 产品介绍\n\n> 待填充\n"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(text)
    pages = [page("intro", "家医签约", summary="看起来包含完整产品介绍")]
    manifest = {"pages": pages, "evidence": [{"evidence_id": "intro", "path": relative, "sha256": hashlib.sha256(text.encode()).hexdigest()}]}
    release = SimpleNamespace(root=tmp_path, release_id="r", manifest=manifest, max_search_results=5)
    monkeypatch.setattr(search.legacy, "_open_release", lambda: release)
    assert json.loads(search._search("家医", None, 5))["pages"] == []
    catalog = json.loads(search._search("家医", None, 5, scope="catalog"))
    assert catalog["pages"][0]["fact_eligible"] is False
    path.write_text(text + "改动")
    assert json.loads(search._search("家医", None, 5)) == {"status": "integrity_failed"}


def test_stale_content_policy_is_rechecked_without_mutating_pinned_manifest(monkeypatch, tmp_path):
    relative = "evidence/wiki/parameters.md"
    text = "# 参数\n\n## 最大并发：2000\n"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(text)
    manifest = {
        "content_state_version": "document-content-v1",
        "pages": [page("params", "家医技术参数", content_state="empty", fact_eligible=False)],
        "evidence": [{"evidence_id": "params", "content_state": "empty", "path": relative, "sha256": hashlib.sha256(text.encode()).hexdigest()}],
    }
    release = SimpleNamespace(root=tmp_path, release_id="old-policy", manifest=manifest, max_search_results=5)
    monkeypatch.setattr(search.legacy, "_open_release", lambda: release)
    result = json.loads(search._search("家医参数", None, 5))
    assert result["pages"][0]["page_id"] == "params"
    assert result["pages"][0]["fact_eligible"] is True
    assert manifest["pages"][0]["fact_eligible"] is False
    assert manifest["evidence"][0]["content_state"] == "empty"


def test_document_intent_does_not_override_topic_and_is_not_always_solution():
    pages = [page("solution", "家医签约解决方案"), page("parameters", "家医签约技术参数"), page("unrelated", "妇幼技术参数")]
    assert search.rank_pages(pages, "家医技术参数")[0]["page_id"] == "parameters"
    assert search.rank_pages(pages, "家医方案")[0]["page_id"] == "solution"


def test_tool_is_loadable_through_existing_resolver():
    from deerflow.reflection.resolvers import resolve_variable

    assert resolve_variable("integrations.regional_wiki_search:search_llm_wiki") is search.search_llm_wiki
