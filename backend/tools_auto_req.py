"""DeerFlow 工具：reg-auto-req-analysis 脚本封装

将 5 个 Python 脚本的 13 个子命令注册为独立工具，
替代 bash 权限，防止 Agent 手写脚本操作 TFS/Redis。
"""

import logging

from langchain.tools import tool

from deerflow.sandbox.tools import ensure_sandbox_initialized
from deerflow.tools.types import Runtime

logger = logging.getLogger(__name__)

SKILL_ROOT = "/mnt/skills/public/reg-auto-req-analysis"


def _run(runtime: Runtime, cmd: str, timeout: int = 300) -> str:
    """在沙箱中执行命令并返回输出。"""
    sandbox = ensure_sandbox_initialized(runtime)
    try:
        output = sandbox.execute_command(cmd, timeout=timeout)
        return str(output) if output is not None else ""
    except Exception as e:
        logger.error("Command failed: %s", e)
        return f"Error: {e}"


# ── tfs_client.py ──────────────────────────────────────────────

@tool("tfs_precheck")
def tfs_precheck(runtime: Runtime, config_path: str = "/mnt/user-data/workspace/tfs-config.json") -> str:
    """TFS connectivity and authentication self-check."""
    cmd = f"python3 {SKILL_ROOT}/_lib/tfs/tfs_client.py precheck --config {config_path} 2>&1"
    return _run(runtime, cmd)


@tool("tfs_fetch")
def tfs_fetch(runtime: Runtime, work_item_id: int,
              config_path: str = "/mnt/user-data/workspace/tfs-config.json") -> str:
    """Fetch a TFS work item. Auto-creates workspace directories."""
    _run(runtime, f"mkdir -p /mnt/user-data/workspace/过程文件/{work_item_id} 2>/dev/null")
    cmd = f"python3 {SKILL_ROOT}/_lib/tfs/tfs_client.py fetch {work_item_id} --config {config_path} 2>&1"
    return _run(runtime, cmd)


@tool("tfs_download_attachments")
def tfs_download_attachments(runtime: Runtime, work_item_id: int,
                             output_dir: str = "",
                             config_path: str = "/mnt/user-data/workspace/tfs-config.json") -> str:
    """下载 TFS 工作项的来源附件。
    Args:
        work_item_id: TFS 工作项 ID
        output_dir: 附件输出目录，缺省为 过程文件/{work_item_id}/附件
        config_path: 配置文件路径
    """
    if not output_dir:
        output_dir = f"/mnt/user-data/workspace/过程文件/{work_item_id}/附件"
    _run(runtime, f"mkdir -p {output_dir} 2>/dev/null")
    cmd = (f"python3 {SKILL_ROOT}/_lib/tfs/tfs_client.py download-attachments {work_item_id} "
           f"--output-dir {output_dir} --include-external --config {config_path} 2>&1")
    return _run(runtime, cmd)


@tool("tfs_list_iterations")
def tfs_list_iterations(runtime: Runtime, project: str,
                        expected_date: str = "",
                        config_path: str = "/mnt/user-data/workspace/tfs-config.json") -> str:
    """查询 TFS 迭代日历，用于时效/排期判定。
    Args:
        project: TFS 项目名称（用工作项的 teamProject）
        expected_date: 期望日期 YYYY-MM-DD 格式（可选）
        config_path: 配置文件路径
    """
    cmd = f"python3 {SKILL_ROOT}/_lib/tfs/tfs_client.py list-iterations --project '{project}' --config {config_path}"
    if expected_date:
        cmd += f" --expected-date {expected_date}"
    cmd += " 2>&1"
    return _run(runtime, cmd, timeout=120)


@tool("tfs_add_tag")
def tfs_add_tag(runtime: Runtime, work_item_id: int, tag: str,
                config_path: str = "/mnt/user-data/workspace/tfs-config.json",
                dry_run: bool = False) -> str:
    """给 TFS 工作项打 QC/分析标签。
    Args:
        work_item_id: 工作项 ID
        tag: 标签名（如 PM-AI-QC-NEED-REVIEW）
        config_path: 配置文件路径
        dry_run: 是否仅预检不执行
    """
    cmd = f"python3 {SKILL_ROOT}/_lib/tfs/tfs_client.py add-tag {work_item_id} --tag {tag} --config {config_path}"
    if dry_run:
        cmd += " --dry-run"
    cmd += " 2>&1"
    return _run(runtime, cmd)


# ── pipeline.py ────────────────────────────────────────────────

@tool("pipeline_validate")
def pipeline_validate(runtime: Runtime, plan_path: str) -> str:
    """校验执行计划 JSON 的完整性和合规性。不访问 TFS。
    Args:
        plan_path: 执行计划 JSON 的完整路径
    """
    cmd = f"python3 {SKILL_ROOT}/_lib/tfs/pipeline.py validate --plan {plan_path} 2>&1"
    return _run(runtime, cmd)


@tool("pipeline_apply")
def pipeline_apply(runtime: Runtime, plan_path: str,
                   config_path: str = "/mnt/user-data/workspace/tfs-config.json",
                   execute: bool = False) -> str:
    """执行计划：校验 + 写 TFS。
    Args:
        plan_path: 执行计划 JSON 路径
        config_path: 配置文件路径
        execute: 是否真正写入 TFS（缺省 dry-run）
    """
    cmd = f"python3 {SKILL_ROOT}/_lib/tfs/pipeline.py apply --plan {plan_path} --config {config_path}"
    if execute:
        cmd += " --execute"
    cmd += " 2>&1"
    return _run(runtime, cmd)


# ── attachment_converter.py ────────────────────────────────────

@tool("attachment_convert")
def attachment_convert(runtime: Runtime, input_dir: str, output_dir: str) -> str:
    """转换并解析 TFS 附件内容。
    Args:
        input_dir: 附件所在目录
        output_dir: 解析结果输出目录
    """
    _run(runtime, f"mkdir -p {output_dir} 2>/dev/null")
    cmd = (f"python3 {SKILL_ROOT}/_lib/tfs/attachment_converter.py "
           f"--input-dir {input_dir} --output-dir {output_dir} 2>&1")
    return _run(runtime, cmd)


# ── redis_client.py ────────────────────────────────────────────

@tool("redis_hgetall")
def redis_hgetall(runtime: Runtime, collection: str, work_item_id: int,
                  config_path: str = "/mnt/user-data/workspace/tfs-config.json") -> str:
    """查询工作项最新的 Redis 执行结果摘要。
    Args:
        collection: TFS 集合名称
        work_item_id: 工作项 ID
        config_path: 配置文件路径
    """
    cmd = (f"python3 {SKILL_ROOT}/_lib/tfs/redis_client.py "
           f"hgetall {collection} {work_item_id} --config {config_path} 2>&1")
    return _run(runtime, cmd)


# ── build_menu_business_index.py ──────────────────────────────

@tool("menu_lookup")
def menu_lookup(runtime: Runtime, area: str,
                index_path: str = "") -> str:
    """按 TFS 区域路径查询候选产品模块。
    Args:
        area: TFS System.AreaPath 的首级路径
        index_path: 菜单业务索引 JSON 路径（可选）
    """
    cmd = f"python3 {SKILL_ROOT}/_lib/build_menu_business_index.py lookup --area '{area}'"
    if index_path:
        cmd += f" --index {index_path}"
    cmd += " 2>&1"
    return _run(runtime, cmd)
