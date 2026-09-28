"""Safety tests for the manifest-gated Regional LLM Wiki tools."""

from __future__ import annotations

import hashlib
import json
import shutil
from types import SimpleNamespace

from deerflow.tools.builtins import regional_llm_wiki_tool as tool_module


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _agent_release_id(manifest: dict) -> str:
    stable_manifest = {key: value for key, value in manifest.items() if key not in {"published_at", "agent_release_id"}}
    canonical = json.dumps(stable_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{manifest['release_id']}-{_sha(canonical)}"


def test_schema2_agent_release_id_matches_compiler_contract_vector():
    manifest = {
        "schema_version": 2,
        "release_id": "wiki-release-1",
        "evidence_release_id": "wiki-release-1",
        "source_commit": "abc123",
        "published_at": "2026-09-28T00:00:00Z",
        "fallback_source_count": 1,
        "fallback_table_group_count": 2,
        "acceptance_quarantine_count": 0,
        "pages": [
            {
                "page_id": "concept:cloud-his",
                "path": "llm-wiki/concepts/cloud-his.md",
                "sha256": "a" * 64,
                "title": "云 HIS",
                "type": "concept",
                "domain": "global",
                "summary": "区域医疗平台概念",
                "keywords": ["云 HIS"],
                "evidence_ids": ["evidence:12345678901234567890"],
            }
        ],
        "evidence": [
            {
                "evidence_id": "evidence:12345678901234567890",
                "path": "evidence/wiki/00-product.md",
                "sha256": "b" * 64,
                "allowed_locations": ["L001-L002"],
                "tfs_url": None,
            }
        ],
    }

    assert tool_module._schema2_agent_release_id(manifest) == ("wiki-release-1-e0d2a176fe988169045a0d0d0dfc16e63f7f0975a04a7d6ae1c78c3be4d71804")


def _install_release(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    release_root = runtime_root / "releases" / "release-1"
    page_path = release_root / "llm-wiki" / "01-产品策划中心" / "concepts" / "cloud-his.md"
    evidence_path = release_root / "evidence" / "wiki" / "01-产品策划中心" / "cloud-his.md"
    page_path.parent.mkdir(parents=True)
    evidence_path.parent.mkdir(parents=True)
    page_content = "# 云 HIS\n\n适用范围：区域医疗。\n"
    evidence_content = "# 原始资料\n\n云 HIS 适用于区域医疗。\n"
    page_path.write_text(page_content, encoding="utf-8")
    evidence_path.write_text(evidence_content, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "release_id": "release-1",
        "source_commit": "abc123",
        "published_at": "2026-09-21T00:00:00Z",
        "pages": [
            {
                "page_id": "concept:cloud-his",
                "path": "llm-wiki/01-产品策划中心/concepts/cloud-his.md",
                "sha256": _sha(page_content),
                "title": "云 HIS",
                "type": "concept",
                "domain": "01-产品策划中心",
                "summary": "区域医疗云 HIS",
                "evidence_ids": ["evidence:cloud-his"],
            }
        ],
        "evidence": [
            {
                "evidence_id": "evidence:cloud-his",
                "path": "evidence/wiki/01-产品策划中心/cloud-his.md",
                "sha256": _sha(evidence_content),
                "allowed_locations": ["L003-L003"],
                "tfs_url": "https://tfs.example.invalid/original",
            }
        ],
    }
    (release_root / "agent-access-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (runtime_root / "current").symlink_to(release_root)
    config = SimpleNamespace(
        regional_llm_wiki=SimpleNamespace(
            enabled=True,
            runtime_root=str(runtime_root),
            max_search_results=5,
            max_page_chars=24_000,
            max_evidence_chars=16_000,
        )
    )
    monkeypatch.setattr(tool_module, "get_app_config", lambda: config)
    return manifest, page_path, evidence_path


def _upgrade_fixture_to_schema2(tmp_path, manifest, page_path, evidence_path):
    runtime_root = tmp_path / "runtime"
    old_release_root = runtime_root / "releases" / manifest["release_id"]
    new_manifest = dict(manifest)
    new_manifest.update(
        {
            "schema_version": 2,
            "evidence_release_id": manifest["release_id"],
            "fallback_source_count": 0,
            "fallback_table_group_count": 0,
            "acceptance_quarantine_count": 0,
        }
    )
    new_manifest["agent_release_id"] = _agent_release_id(new_manifest)
    new_release_root = runtime_root / "releases" / new_manifest["agent_release_id"]
    old_release_root.rename(new_release_root)
    (new_release_root / "agent-access-manifest.json").write_text(json.dumps(new_manifest), encoding="utf-8")
    (runtime_root / "current").unlink()
    (runtime_root / "current").symlink_to(new_release_root)
    return new_manifest, new_release_root / page_path.relative_to(old_release_root), new_release_root / evidence_path.relative_to(old_release_root)


def _publish_schema2_manifest(tmp_path, manifest):
    runtime_root = tmp_path / "runtime"
    current = runtime_root / "current"
    old_root = current.resolve()
    manifest["agent_release_id"] = _agent_release_id(manifest)
    new_root = runtime_root / "releases" / manifest["agent_release_id"]
    old_root.rename(new_root)
    (new_root / "agent-access-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    current.unlink()
    current.symlink_to(new_root)
    return new_root


def test_search_and_read_are_manifest_gated(tmp_path, monkeypatch):
    _install_release(tmp_path, monkeypatch)

    search = json.loads(tool_module._search("云 HIS", None, 5))
    assert search["status"] == "ok"
    assert search["release_id"] == "release-1"
    assert search["pages"] == [{"domain": "01-产品策划中心", "page_id": "concept:cloud-his", "title": "云 HIS", "type": "concept"}]

    page = json.loads(tool_module._read_page("release-1", "concept:cloud-his"))
    assert page["status"] == "ok"
    assert page["evidence_ids"] == ["evidence:cloud-his"]
    assert "区域医疗" in page["content"]
    assert "path" not in page


def test_search_matches_unsegmented_chinese_question(tmp_path, monkeypatch):
    _install_release(tmp_path, monkeypatch)

    search = json.loads(tool_module._search("云HIS如何规划", None, 5))

    assert search["status"] == "ok"
    assert search["pages"][0]["page_id"] == "concept:cloud-his"


def test_schema2_pins_agent_export_and_reads_wiki_and_evidence(tmp_path, monkeypatch):
    manifest, page_path, evidence_path = _install_release(tmp_path, monkeypatch)
    manifest, page_path, evidence_path = _upgrade_fixture_to_schema2(tmp_path, manifest, page_path, evidence_path)

    search = json.loads(tool_module._search("云 HIS", None, 5))
    agent_release_id = manifest["agent_release_id"]
    assert search["status"] == "ok"
    assert search["agent_release_id"] == agent_release_id
    assert search["release_id"] == agent_release_id
    assert search["wiki_release_id"] == manifest["release_id"]

    page = json.loads(tool_module._read_page(agent_release_id, "concept:cloud-his"))
    assert page["status"] == "ok"
    assert page["agent_release_id"] == agent_release_id
    assert page["wiki_release_id"] == manifest["release_id"]
    evidence = json.loads(tool_module._read_evidence(agent_release_id, "concept:cloud-his", "evidence:cloud-his", "L003-L003"))
    assert evidence["status"] == "ok"
    assert evidence["agent_release_id"] == agent_release_id
    assert "云 HIS 适用于区域医疗" in evidence["content"]
    assert page_path.is_file() and evidence_path.is_file()


def test_schema2_keeps_an_inflight_answer_on_its_pinned_export(tmp_path, monkeypatch):
    manifest, page_path, evidence_path = _install_release(tmp_path, monkeypatch)
    manifest, page_path, evidence_path = _upgrade_fixture_to_schema2(tmp_path, manifest, page_path, evidence_path)
    old_agent_release_id = manifest["agent_release_id"]
    search = json.loads(tool_module._search("云 HIS", None, 5))

    # Publish a second immutable export with changed content, then switch current.
    runtime_root = tmp_path / "runtime"
    old_release_root = runtime_root / "releases" / old_agent_release_id
    next_manifest = json.loads(json.dumps(manifest))
    next_manifest["release_id"] = "release-2"
    next_manifest["evidence_release_id"] = "release-2"
    next_manifest["published_at"] = "2026-09-22T00:00:00Z"
    new_root = runtime_root / "releases" / "staging-release-2"
    shutil.copytree(old_release_root, new_root)
    next_page = new_root / next_manifest["pages"][0]["path"]
    next_evidence = new_root / next_manifest["evidence"][0]["path"]
    next_page_content = "# 云 HIS\n\n适用范围：新区医疗。\n"
    next_evidence_content = "# 新原始资料\n\n云 HIS 适用于新区医疗。\n"
    next_page.write_text(next_page_content, encoding="utf-8")
    next_evidence.write_text(next_evidence_content, encoding="utf-8")
    next_manifest["pages"][0]["sha256"] = _sha(next_page_content)
    next_manifest["evidence"][0]["sha256"] = _sha(next_evidence_content)
    next_manifest["agent_release_id"] = _agent_release_id(next_manifest)
    final_root = runtime_root / "releases" / next_manifest["agent_release_id"]
    new_root.rename(final_root)
    (final_root / "agent-access-manifest.json").write_text(json.dumps(next_manifest), encoding="utf-8")
    (runtime_root / "current").unlink()
    (runtime_root / "current").symlink_to(final_root)

    old_page = json.loads(tool_module._read_page(old_agent_release_id, "concept:cloud-his"))
    new_page = json.loads(tool_module._read_page(next_manifest["agent_release_id"], "concept:cloud-his"))
    assert search["agent_release_id"] == old_agent_release_id
    assert "区域医疗" in old_page["content"]
    assert "新区医疗" in new_page["content"]
    assert page_path.is_file() and evidence_path.is_file()


def test_schema2_rejects_manifest_revision_and_release_identity_mismatch(tmp_path, monkeypatch):
    manifest, page_path, evidence_path = _install_release(tmp_path, monkeypatch)
    manifest, _, _ = _upgrade_fixture_to_schema2(tmp_path, manifest, page_path, evidence_path)

    release_root = tmp_path / "runtime" / "current"
    current_root = release_root.resolve()
    bad_manifest = dict(manifest)
    bad_manifest["evidence_release_id"] = "different-wiki-release"
    (current_root / "agent-access-manifest.json").write_text(json.dumps(bad_manifest), encoding="utf-8")

    assert json.loads(tool_module._release_state()) == {"status": "integrity_failed"}


def test_schema2_rejects_unknown_or_boolean_schema_version(tmp_path, monkeypatch):
    manifest, page_path, evidence_path = _install_release(tmp_path, monkeypatch)
    manifest, _, _ = _upgrade_fixture_to_schema2(tmp_path, manifest, page_path, evidence_path)
    manifest_path = (tmp_path / "runtime" / "current").resolve() / "agent-access-manifest.json"

    for unsupported_version in (3, True, "2"):
        bad_manifest = dict(manifest)
        bad_manifest["schema_version"] = unsupported_version
        manifest_path.write_text(json.dumps(bad_manifest), encoding="utf-8")
        assert json.loads(tool_module._release_state()) == {"status": "integrity_failed"}


def test_schema2_rejects_orphan_evidence_and_content_hash_mismatch(tmp_path, monkeypatch):
    manifest, page_path, evidence_path = _install_release(tmp_path, monkeypatch)
    manifest, _, _ = _upgrade_fixture_to_schema2(tmp_path, manifest, page_path, evidence_path)
    corrupted_manifest = dict(manifest)
    corrupted_manifest["pages"] = [dict(manifest["pages"][0], evidence_ids=["evidence:missing"])]
    _publish_schema2_manifest(tmp_path, corrupted_manifest)
    assert json.loads(tool_module._release_state()) == {"status": "integrity_failed"}

    valid_root = _publish_schema2_manifest(tmp_path, manifest)
    tampered_evidence_path = valid_root / manifest["evidence"][0]["path"]
    tampered_evidence_path.write_text("tampered evidence", encoding="utf-8")
    result = json.loads(tool_module._read_evidence(manifest["agent_release_id"], "concept:cloud-his", "evidence:cloud-his", None))
    assert result == {"status": "integrity_failed"}


def test_schema2_rejects_wiki_release_id_as_export_directory_id(tmp_path, monkeypatch):
    manifest, page_path, evidence_path = _install_release(tmp_path, monkeypatch)
    manifest, _, _ = _upgrade_fixture_to_schema2(tmp_path, manifest, page_path, evidence_path)

    result = json.loads(tool_module._read_page(manifest["release_id"], "concept:cloud-his"))

    assert result == {"status": "not_found"}


def test_evidence_must_be_declared_by_the_selected_page(tmp_path, monkeypatch):
    _install_release(tmp_path, monkeypatch)

    allowed = json.loads(tool_module._read_evidence("release-1", "concept:cloud-his", "evidence:cloud-his", "L003-L003"))
    assert allowed["status"] == "ok"
    assert allowed["tfs_url"] == "https://tfs.example.invalid/original"

    denied = json.loads(tool_module._read_evidence("release-1", "concept:cloud-his", "evidence:other", None))
    assert denied == {"status": "not_found"}

    invalid_locator = json.loads(tool_module._read_evidence("release-1", "concept:cloud-his", "evidence:cloud-his", "../../secret"))
    assert invalid_locator == {"status": "invalid_request"}


def test_rejects_path_escape_and_does_not_return_content(tmp_path, monkeypatch):
    manifest, _, _ = _install_release(tmp_path, monkeypatch)
    manifest["pages"][0]["path"] = "llm-wiki/../../outside.md"
    release_root = tmp_path / "runtime" / "releases" / "release-1"
    (release_root / "agent-access-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = json.loads(tool_module._read_page("release-1", "concept:cloud-his"))
    assert result == {"status": "integrity_failed"}


def test_rejects_symlinked_page_outside_release(tmp_path, monkeypatch):
    manifest, page_path, _ = _install_release(tmp_path, monkeypatch)
    outside_page = tmp_path / "outside.md"
    outside_page.write_text("outside release", encoding="utf-8")
    page_path.unlink()
    page_path.symlink_to(outside_page)
    manifest["pages"][0]["sha256"] = _sha(outside_page.read_text(encoding="utf-8"))
    release_root = tmp_path / "runtime" / "releases" / "release-1"
    (release_root / "agent-access-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = json.loads(tool_module._read_page("release-1", "concept:cloud-his"))
    assert result == {"status": "integrity_failed"}


def test_rejects_stale_release_and_disabled_reader(tmp_path, monkeypatch):
    _install_release(tmp_path, monkeypatch)
    assert json.loads(tool_module._read_page("old-release", "concept:cloud-his")) == {"status": "not_found"}

    monkeypatch.setattr(
        tool_module,
        "get_app_config",
        lambda: SimpleNamespace(regional_llm_wiki=SimpleNamespace(enabled=False)),
    )
    assert json.loads(tool_module._release_state()) == {"status": "release_unavailable"}


def test_agent_allowlist_filters_configured_builtin_and_mcp_shaped_tools():
    from deerflow.agents.lead_agent.agent import _filter_tools_by_agent_allowlist

    tools = [
        SimpleNamespace(name="search_llm_wiki"),
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="web_search"),
        SimpleNamespace(name="mcp_sensitive_tool"),
    ]
    result = _filter_tools_by_agent_allowlist(tools, ["search_llm_wiki"])
    assert [tool.name for tool in result] == ["search_llm_wiki"]
