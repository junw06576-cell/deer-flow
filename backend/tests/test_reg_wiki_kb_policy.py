"""Contract tests for reg-wiki-kb memory and evidence isolation rules."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOUL_PATH = REPO_ROOT / "agents" / "reg-wiki-kb" / "SOUL.md"
SKILL_PATH = REPO_ROOT / "skills" / "public" / "product-kb-qa" / "SKILL.md"
MEMORY_PROMPT_PATH = REPO_ROOT / "backend" / "packages" / "harness" / "deerflow" / "agents" / "memory" / "backends" / "deermem" / "deermem" / "core" / "prompts" / "reg-wiki-kb" / "memory_update.chat.yaml"

UNAVAILABLE_RESPONSE = "产品知识库当前不可用，请联系管理员检查知识库挂载或文件权限。"
NOT_FOUND_RESPONSE = "产品知识库（Wiki）中未找到相关内容。"
EVIDENCE_STATES = {
    "UNVERIFIED",
    "INDEX_READY",
    "EVIDENCE_READY",
    "NOT_FOUND",
    "KNOWLEDGE_BASE_UNAVAILABLE",
}


def test_soul_and_skill_share_evidence_state_contract() -> None:
    soul = SOUL_PATH.read_text(encoding="utf-8")
    skill = SKILL_PATH.read_text(encoding="utf-8")

    for content in (soul, skill):
        for state in EVIDENCE_STATES:
            assert state in content
        assert UNAVAILABLE_RESPONSE in content
        assert NOT_FOUND_RESPONSE in content
        assert "持久化 Memory" in content
        assert "先前工具结果" in content
        assert "本轮" in content


def test_reg_wiki_memory_policy_does_not_replace_wiki_evidence() -> None:
    prompt = MEMORY_PROMPT_PATH.read_text(encoding="utf-8")

    assert "Policy ID: reg-wiki-kb-memory-v1" in prompt
    assert "Memory 只用于个性化和连续协作" in prompt
    assert "禁止沉淀" in prompt
    assert "Wiki 目录、文件名、标题、数量、路径" in prompt
    assert "助手先前给出的结论" in prompt
    assert "不要借本次更新主动清理历史 Memory" in prompt
