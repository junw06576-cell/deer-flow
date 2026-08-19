#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证并执行 auto-req-qc / auto-req-analysis 的受约束 TFS 计划。

默认只 dry-run；只有显式传入 --execute 才会写 TFS。计划中的自然语言判断
业务结论由 skill 写入结构化快照；本脚本生成确定性报告，并负责校验、并发保护与可恢复执行。
"""
import argparse
import copy
import hashlib
import html
import json
import os
import re
import secrets
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

import tfs_client as tfs
import redis_client


PLAN_VERSION = 2
SUPPORTED_PLAN_VERSIONS = {1, PLAN_VERSION}
ANALYSIS_REF_PROFILE = 'analysis-ref-v1'
RUN_BOUND_PROFILE = 'run-bound-v1'
RUN_RECEIPT_SCHEMA = 'run-receipt-v1'
ANALYSIS_RESULT_SCHEMA = 'analysis-result-v1'
RUN_STATUS_SCHEMA = 'run-status-v1'
RUN_ID_RE = re.compile(r'^[A-Za-z0-9_-]{8,80}$')
ANALYSIS_GAP_ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
EVIDENCE_REF_RE = re.compile(r'^(?:work-item|kb:[0-9]+|wiki:[0-9]+|req:[0-9]+)$')
QC_TAGS = {'PM-AI-QC-NEED-INFO', 'PM-AI-QC-NEED-REVIEW'}
ANALYSIS_TAGS = {'PM-AI-AUTO-ANA', 'PM-AI-MANUAL-REVIEW', 'PM-AI-STOP-AUTO'}
# 下游人工确认通过标签。不再作硬挡（允许覆盖重跑）；非 SKIP 重跑时在清旧标签阶段作废。
DOWNSTREAM_PASSED_TAGS = {'PM-AI-MANUAL-PASSED'}
AUTO_SCOPES = {'field-ui-copy', 'unit-test-completion', 'config-enum-adjustment'}
# 优化类类别（既有功能优化为主）：AUTO-ANA 时须在 kb.findings 含至少一条 state=已证实 的现有实现锚点。
OPTIMIZATION_CATEGORIES = {
    'existing-ui-simple', 'existing-query-simple', 'existing-query-adjustment', 'existing-complex',
    'bug-fix', 'print-adjustment', 'data-management',
    'permission-config', 'performance', 'mobile-adaptation',
}
QC_VERDICTS = {'NEED-INFO', 'NEED-REVIEW'}
ROUTING_VERDICTS = {'SKIP-ANALYSIS'}
ANALYSIS_VERDICTS = {'AUTO-ANA', 'MANUAL-REVIEW', 'MANUAL-REVIEW-STOP'}
ANALYSIS_DESCRIPTION_REQUIREMENTS = {
    'report': ('合规性', '查询条件', '列定义', '统计口径', '特殊统计逻辑'),
    'third-party-debug': ('已开发接口', '协作事项'),
    'third-party-adjustment': ('现有接口变更', '影响范围', '参数控制', '取值逻辑'),
    'third-party-new': ('业务背景', '对接流程', '触发时机', '数据支撑与字段映射', '返回数据处理', '影响与参数控制'),
    'existing-ui-simple': ('需求背景', '涉及功能', '修改方案', '参数控制', '显示与交互位置', '影响范围', '现有行为/数据影响'),
    'existing-query-simple': ('查询入口与条件', '查询规则与流程', '不涉及范围'),
    'existing-query-adjustment': (
        '需求背景', '查询入口与条件', '匹配规则与组合逻辑', '结果字段与展示', '空值与异常处理',
        '关联视图与权限', '验收场景', '影响范围', '现有行为/数据影响'),
    'existing-complex': ('涉及条线与模块', '改造内容', '改造流程', '改造范围', '风险与项目注意事项'),
    'new-feature': ('业务场景', '功能方案', '参数控制', '权限与数据权限', '脱敏与历史数据', '数据与第三方影响', '文字交互说明'),
    'print-adjustment': ('打印场景', '单据类型', '修改内容', '模板处理', '数据源', '现有行为/数据影响'),
    'bug-fix': ('问题现象', '复现步骤或已知条件', '涉及功能', '修复方案', '现有逻辑影响'),
    'data-management': ('操作场景', '数据对象', '操作规则', '批量或导入导出', '数据校验', '现有行为/数据影响'),
    'permission-config': ('权限场景', '参数编号与名称', '参数值域', '控制效果', '验证点', '现有行为/数据影响'),
    'performance': ('性能问题', '影响范围', '优化方案', '预期效果', '现有行为/数据影响'),
    'mobile-adaptation': ('业务场景', '涉及功能', '数据同步逻辑', '与 Web 差异', '现有行为/数据影响'),
}
CONCISE_ANALYSIS_DESCRIPTION_REQUIREMENTS = {
    'existing-ui-simple': ('核心改造点', '生效场景', '不涉及范围'),
    'print-adjustment': ('核心改造点', '打印场景', '不涉及范围'),
    'data-management': ('核心改造点', '操作场景', '不涉及范围'),
}
CONCISE_V2_ANALYSIS_DESCRIPTION_REQUIREMENTS = {
    'existing-ui-simple': ('核心改造点',),
    'print-adjustment': ('核心改造点',),
    'data-management': ('核心改造点',),
}
CONCISE_V3_ANALYSIS_DESCRIPTION_REQUIREMENTS = {
    'report': ('报表分析',),
    'third-party-debug': ('联调说明',),
    'third-party-adjustment': ('接口调整方案',),
    'third-party-new': ('业务节点与触发时机', '字段与返回数据处理', '业务改造与控制'),
    'existing-ui-simple': ('界面优化方案',),
    'existing-query-simple': ('查询调整方案',),
    'existing-query-adjustment': ('查询规则与展示', '影响范围与控制'),
    'existing-complex': ('改造条线与内容', '改造流程与范围', '风险与项目注意事项'),
    'new-feature': ('功能与交互方案', '控制与数据安全', '数据及第三方影响'),
    'print-adjustment': ('打印调整方案',),
    'bug-fix': ('问题与条件', '修复方案与影响'),
    'data-management': ('数据管理方案',),
    'permission-config': ('权限参数方案',),
    'performance': ('性能优化方案',),
    'mobile-adaptation': ('多端适配方案',),
}
ANALYSIS_DESCRIPTION_PROFILES = {'concise-v1', 'concise-v2', 'concise-v3'}
ANALYSIS_DECISION_SUMMARY_REQUIREMENTS = (
    '决策结论', '生效路径与条件', '决策边界', '验收要点',
)
ANALYSIS_EMPHASIS_LABELS = {
    '菜单路径',
    '核心改造点', '修改方案', '改造内容', '功能方案', '修复方案', '优化方案',
    '修改内容', '现有接口变更', '操作规则', '协作事项',
    '报表分析', '联调说明', '接口调整方案', '业务节点与触发时机',
    '字段与返回数据处理', '业务改造与控制', '界面优化方案', '查询调整方案',
    '查询规则与展示', '影响范围与控制', '改造条线与内容', '改造流程与范围',
    '风险与项目注意事项', '功能与交互方案', '控制与数据安全', '数据及第三方影响',
    '打印调整方案', '问题与条件', '修复方案与影响', '数据管理方案',
    '权限参数方案', '性能优化方案', '多端适配方案',
}
ANALYSIS_PATH_VALUE_RE = re.compile(r'^菜单路径：\s*(\S(?:.*?\S)?)\s*；\s*操作路径：\s*(\S(?:.*\S)?)$')
ANALYSIS_BANNED_PHRASES = (
    '请开发处理', '已和项目沟通，关闭', '没问题，关闭',
    '要求的详细分析，包含分析结果、疑问及要求完成效果等', '优化XXX功能',
)
ITERATION_ANALYSIS_CLOSURE_REQUIREMENTS = {
    '现状基线': ('用户与场景', '当前行为或规则', '既有边界'),
    '问题与目标': ('触发条件', '业务影响', '根因判断', '业务目标'),
    '差异与范围': ('现状', '目标状态', '保持不变项', '受影响触点'),
    '方案取舍': ('推荐方案', '替代方案或不适用', '选择理由'),
    '成功衡量与非目标': ('成功衡量', '非目标'),
}
ITERATION_ANALYSIS_CLOSURE_BANNED_PHRASES = ('候选实现', '未确认根因', '按现有逻辑')
TRACEABILITY_HEADING = '四、范围—方案—验收追踪'
TRACEABILITY_HEADERS = ('ID', '范围/改动点', '方案/目标行为', '验收场景与结果', '结论状态', '依据或缺口')
TRACEABILITY_STATUSES = {'已证实', '合理假设', '待业务确认'}
ANALYSIS_GAP_FIELDS = ('id', 'topic', 'missing', 'impact', 'question', 'options', 'allow_other')
EVIDENCE_GAP_FIELDS = ('id', 'topic', 'missing', 'impact', 'owner', 'next_action')
EVIDENCE_GAP_OWNERS = {'研发', '知识库治理', '产品'}
EVIDENCE_GAP_KINDS = {'technical', 'background'}
EVIDENCE_GAP_TYPES = {
    'WIKI_TOPIC_MISSING', 'WIKI_BROKEN_LINK', 'TFS_SNAPSHOT_LAG', 'TFS_SEARCH_TRUNCATED',
    'GITNEXUS_REPO_MISSING', 'GITNEXUS_EMBEDDINGS_DISABLED', 'DB_SCHEMA_PARTIAL',
    'DB_RELATION_PARTIAL', 'EXISTING_IMPL_LOCATION', 'SIMILAR_IMPL_DEDUP',
    'PRODUCT_ROUTE_UNRESOLVED', 'SOURCE_MCP_UNAVAILABLE', 'SOURCE_SCOPE_PARTIAL',
    'SOURCE_VERIFICATION_INCOMPLETE', 'DATABASE_MCP_UNAVAILABLE',
}
QC_EVIDENCE_RESOLUTION_FIELDS = ('id', 'initial_gap', 'resolution', 'evidence_refs')
TFS_REQUIREMENTS_FINDING_FIELDS = ('work_item_id', 'fact', 'state', 'source_tool')
TFS_REQUIREMENTS_FINDING_STATES = {'已证实', '候选', '未确认'}
TFS_REQUIREMENTS_TOOLS = {
    'get_requirements_summary', 'get_related_work_items', 'search_requirements', 'get_work_item',
}
TFS_REQUIREMENTS_CONFIRMED_TOOLS = {'get_related_work_items', 'get_work_item'}
TFS_MATURITY_STATES = {'设想', '分析确认', '已落地'}
HUMAN_EVIDENCE_FIELDS = ('conclusion', 'evidence', 'boundary')
# evidence-loop-v2 采集状态契约（四源同构）：区分「已命中 / 完整查询后无命中 / 覆盖不完整 / 来源不可用」。
# 只有 coverage_status=COMPLETE ∧ stop_reason=exhausted ∧ 未截断 的 NO_HIT 才能支撑「无相似/无现有实现」负面结论。
EVIDENCE_ACQUISITION_SOURCES = ('tfs_requirements', 'wiki', 'gitnexus', 'db_knowledge')
EVIDENCE_AVAILABILITY = {'READY', 'UNAVAILABLE'}
EVIDENCE_COVERAGE_STATUS = {'COMPLETE', 'PARTIAL', 'OUT_OF_SCOPE', 'UNKNOWN'}
EVIDENCE_QUERY_STATUS = {'HIT', 'NO_HIT', 'SKIPPED', 'ERROR'}
EVIDENCE_STOP_REASONS = {'exhausted', 'verified_hit', 'hard_limit', 'source_gap', 'not_applicable'}
ATTACHMENT_CONVERTERS = {
    'markitdown', 'builtin-fallback', 'libreoffice+markitdown',
    'libreoffice+builtin-fallback', '人工视觉读取',
}
ATTACHMENT_RUNTIME_MODES = {'fixed-image', 'managed-host', 'builtin-only'}
CHECKLIST_ITEM_FIELDS = ('id', 'question', 'options', 'allow_other')
MAX_QC_ITEMS = 5
SINGLE_CONFIRMATION_POLICY = 'qc-single-batch-v1'
KNOWLEDGE_ROUTE_STATUSES = {
    'RESOLVED', 'AREA_UNMAPPED', 'AREA_AMBIGUOUS', 'PROFILE_MISSING', 'PROFILE_INVALID',
}
KNOWLEDGE_ROUTE_ROLES = ('requirements_history', 'code_graph', 'source_code', 'database')
SOURCE_CODE_TOOLS = {'search_source', 'search_symbol'}
DATABASE_KNOWLEDGE_TOOLS = {
    'search_knowledge', 'get_table_knowledge', 'traverse_graph', 'inspect_node',
}
IMPLEMENTATION_IMPACTS = {
    'none', 'ui-presentation', 'field-assignment', 'api-contract', 'business-logic',
    'data-read-write', 'database-schema', 'data-migration', 'reporting-statistics',
    'data-permission', 'database-performance', 'database-script',
}
SOURCE_REQUIRED_IMPACTS = IMPLEMENTATION_IMPACTS - {'none', 'ui-presentation'}
DATABASE_REQUIRED_IMPACTS = {
    'data-read-write', 'database-schema', 'data-migration', 'reporting-statistics',
    'data-permission', 'database-performance', 'database-script',
}
BUSINESS_RULE_COVERAGE_DIMENSIONS = {
    'presentation', 'empty_value', 'maintenance_granularity', 'historical_data',
}
BUSINESS_RULE_COVERAGE_STATUSES = {'CONFIRMED', 'DEFAULTED', 'NOT_APPLICABLE'}
TRACEABLE_CONFIRMATION_SOURCES = {
    'work-item', 'attachment', 'inherited-pm-answer', 'confirmed-product-rule',
}
BUSINESS_RULE_COVERAGE_SOURCES = {
    *TRACEABLE_CONFIRMATION_SOURCES, 'presentation-default', 'not-applicable',
}
GENERAL_RULE_COVERAGE_DIMENSIONS = {
    'scope', 'workflow', 'business_semantics', 'business_rules',
    'permissions', 'exceptions', 'acceptance',
}
GENERAL_RULE_COVERAGE_STATUSES = {'CONFIRMED', 'NOT_APPLICABLE'}
GENERAL_ALWAYS_REQUIRED_DIMENSIONS = {'scope', 'business_semantics', 'acceptance'}
UI_BASELINE_SOURCE_FAMILIES = {
    'wiki': 'product-knowledge',
    'runtime-observation': 'runtime',
    'attachment': 'runtime',
    'code-graph': 'implementation',
    'source-code': 'implementation',
}

# 字段流转：指派人账号解析（身份字段必须 WINNING\account 格式；bare display name TFS 报「未知标识」HTTP400）
ASSIGNEE_FULL_RE = re.compile(r'<([^>]+)>')                      # Dev.Leader 全格式里的 <WINNING\account>
ASSIGNEE_WINNING_RE = re.compile(r'^WINNING\\[A-Za-z][A-Za-z0-9_]*$')
ASSIGNEE_PAREN_RE = re.compile(r'\(([A-Za-z][A-Za-z0-9_]*)\)')   # 回退 display(account) 取账号
QC_RULE_SOURCE = 'pre-qc-v1'
ANALYSIS_RULE_SOURCES = {'fallback-v1', 'evidence-loop-v1', 'evidence-loop-v2'}
LEGACY_RULE_SOURCES = {'pre-qc-v1', 'fallback'}


def extract_analysis_description_markdown(content):
    """提取需求分析报告中唯一的“分析者描述”二级章节。"""
    headings = list(re.finditer(r'^##\s+(.+?)\s*$', content, re.MULTILINE))
    matches = [heading for heading in headings if heading.group(1) == '三、分析者描述']
    if len(matches) != 1:
        raise ValueError('需求分析报告必须且只能包含一个“## 三、分析者描述”章节')
    start = matches[0].end()
    following = next((heading.start() for heading in headings if heading.start() > start), len(content))
    return content[start:following]


def _plain_analysis_text(value):
    """移除受控 Markdown 行内标记后再转义为 HTML 文本。"""
    plain = re.sub(r'\*\*([^*]+)\*\*', r'\1', value.strip()).replace('`', '')
    return html.escape(plain, quote=True)


def render_analysis_description_html(content, analysis_profile=None, run_id=None):
    """将受控分析者描述 Markdown 转成 TFS 编辑器可读的基础 HTML。"""
    section = re.sub(r'<!--.*?-->', '', extract_analysis_description_markdown(content), flags=re.DOTALL)
    if analysis_profile == 'concise-v3':
        numbered_lines = re.findall(r'^\s*([1-9][0-9]*)\.\s+(\S.*?)\s*$', section, re.MULTILINE)
        numbers = [int(number) for number, _ in numbered_lines]
        if numbers and (len(numbers) < 2 or len(numbers) > 8
                        or numbers != list(range(1, len(numbers) + 1))):
            raise ValueError('concise-v3 多条变更内容必须使用从 1 开始连续的 2–8 项有序编号')
    rendered = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        category = re.fullmatch(r'###\s+([a-z-]+)（(.+)）', line)
        if category:
            if analysis_profile != 'concise-v3':
                rendered.append(f'<div>{_plain_analysis_text(category.group(2))}</div>')
            continue
        field = re.fullmatch(r'-\s+\*\*(.+?)\*\*：\s*(\S.*?)\s*', line)
        if field:
            label = _plain_analysis_text(field.group(1))
            value = _plain_analysis_text(field.group(2))
            if analysis_profile == 'concise-v3' and (
                    label in {'需求类别', '路径'}
                    or label in ANALYSIS_DECISION_SUMMARY_REQUIREMENTS):
                raise ValueError(f'concise-v3 分析者描述不得包含“{label}”')
            if label in ANALYSIS_EMPHASIS_LABELS:
                rendered.append(f'<div><strong>{label}：</strong>{value}</div>')
            else:
                rendered.append(f'<div>{label}：{value}</div>')
            continue
        structured_numbered = re.fullmatch(
            r'([1-9][0-9]*)\.\s+\*\*(.+?)\*\*：\s*(\S.*?)\s*', line)
        if structured_numbered and analysis_profile == 'concise-v3':
            number = structured_numbered.group(1)
            label = _plain_analysis_text(structured_numbered.group(2))
            value = _plain_analysis_text(structured_numbered.group(3))
            rendered.append(f'<div>&nbsp;&nbsp;&nbsp;&nbsp;{number}、<strong>{label}：</strong>{value}</div>')
            continue
        numbered = re.fullmatch(r'([1-9][0-9]*)\.\s+(\S.*?)\s*', line)
        if numbered:
            if analysis_profile == 'concise-v3':
                raise ValueError('concise-v3 编号变更项必须按“1. **改动点**：内容”填写')
            rendered.append(f'<div>{numbered.group(1)}. {_plain_analysis_text(numbered.group(2))}</div>')
            continue
        raise ValueError(f'分析者描述含不支持的 Markdown 行：{line}')
    if not rendered:
        raise ValueError('分析者描述不能为空')
    marker = f'<!-- auto-req-run:{html.escape(run_id, quote=True)} -->' if run_id else ''
    return marker + '<div><br></div>' + ''.join(rendered) + '<div><br></div>'


def legacy_analysis_body(content):
    """复现旧执行器在富文本字段中留下的错误 Markdown 正文。"""
    without_marker = re.sub(r'<!-- auto-req-run:[^>]+-->\s*', '', content)
    return without_marker.lstrip().replace('>', '&gt;')


def result(ok, **kwargs):
    return {'ok': ok, **kwargs}


def resolve_assignee_to_winning(dev_leader_value='', fallback=''):
    """从 Winning.Dev.Leader 全格式（首选）或 fallback 稳定提取 WINNING\\account。

    身份字段（System.AssignedTo 等）写入必须用 `WINNING\\账号` 格式，bare display name
    TFS 必报「未知标识」HTTP400（2026-07-28 工作项 259681 实测）。本函数把 TFS 读回的
    全格式 `account(中文名) <WINNING\\account>` 或速查表的 `display(account)` 收敛为可写值。

    优先级：Dev.Leader 的 <...> 段 > fallback 已是 WINNING\\x > fallback 的 <...> 段 > fallback (account)。
    不可解析（裸 display name 无账号信息 / 格式异常）返回 None —— 调用方据此跳过指派，绝不猜。
    """
    def _clean(v):
        v = (v or '').strip()
        return v or None
    # 1) Dev.Leader 全格式优先：<WINNING\account>
    m = ASSIGNEE_FULL_RE.search(dev_leader_value or '')
    if m:
        cand = _clean(m.group(1))
        if cand and ASSIGNEE_WINNING_RE.fullmatch(cand):
            return cand
    # 2) fallback 已是 WINNING\x
    fb = _clean(fallback)
    if fb and ASSIGNEE_WINNING_RE.fullmatch(fb):
        return fb
    # 3) fallback 含 <WINNING\account>
    if fb:
        m = ASSIGNEE_FULL_RE.search(fb)
        if m:
            cand = _clean(m.group(1))
            if cand and ASSIGNEE_WINNING_RE.fullmatch(cand):
                return cand
        # 4) fallback 含 (account)
        m = ASSIGNEE_PAREN_RE.search(fb)
        if m:
            return 'WINNING\\' + m.group(1)
    return None


def read_plan(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('计划根节点必须是 JSON object')
    return data


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_exclusive(path, payload):
    with open(path, 'x', encoding='utf-8') as output:
        json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write('\n')


def _write_json_atomic(path, payload):
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix='.run-state-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write('\n')
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _plan_name(work_item_id, run_id):
    return f'执行计划_{work_item_id}_{run_id}.json'


def _status_name(work_item_id, run_id):
    return f'运行状态_{work_item_id}_{run_id}.json'


def _canonical_run_dir(work_item_id, run_id, process_root=None):
    root = os.path.abspath(process_root or os.path.join(os.getcwd(), '过程文件'))
    return os.path.join(root, str(work_item_id), run_id)


def _run_status_payload(work_item_id, run_id, status, **details):
    updated_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return {
        'schema': RUN_STATUS_SCHEMA,
        'work_item_id': work_item_id,
        'run_id': run_id,
        'session_id': run_id,
        'thread_id': run_id,
        'status': status,
        'updated_at_utc': updated_at,
        'history': [{'status': status, 'at_utc': updated_at}],
        **details,
    }


def _set_run_status(work_item_id, run_id, status, process_root=None, **details):
    run_dir = _canonical_run_dir(work_item_id, run_id, process_root)
    status_path = os.path.join(run_dir, _status_name(work_item_id, run_id))
    payload = _run_status_payload(work_item_id, run_id, status, **details)
    if os.path.isfile(status_path) and not os.path.islink(status_path):
        errors = []
        current = _read_json_object(status_path, 'run_status', errors)
        if (not errors and current.get('work_item_id') == work_item_id
                and current.get('run_id') == run_id):
            payload['history'] = list(current.get('history') or []) + payload['history']
    _write_json_atomic(status_path, payload)
    return status_path


def _set_meta_run_status(meta, plan, status, **details):
    payload = _run_status_payload(plan['work_item_id'], plan['run_id'], status, **details)
    path = os.path.join(
        meta['run_dir'], _status_name(plan['work_item_id'], plan['run_id']))
    if os.path.isfile(path) and not os.path.islink(path):
        errors = []
        current = _read_json_object(path, 'run_status', errors)
        if (not errors and current.get('work_item_id') == plan['work_item_id']
                and current.get('run_id') == plan['run_id']):
            payload['history'] = list(current.get('history') or []) + payload['history']
    _write_json_atomic(path, payload)
    return path


def get_run_status(work_item_id, run_id, process_root=None):
    """只按 run_id 读取规范状态；禁止按工作项回退到其它运行。"""
    if (not isinstance(work_item_id, int) or work_item_id <= 0
            or not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id)):
        return result(False, error_code='RUN_ID_CONTEXT_MISMATCH',
                      error='work_item_id 或 run_id 格式无效')
    run_dir = _canonical_run_dir(work_item_id, run_id, process_root)
    status_path = os.path.join(run_dir, _status_name(work_item_id, run_id))
    if not os.path.isfile(status_path) or os.path.islink(status_path):
        return result(False, error_code='RUN_NOT_FOUND', error='未知 run_id')
    errors = []
    status = _read_json_object(status_path, 'run_status', errors)
    if (errors or status.get('schema') != RUN_STATUS_SCHEMA
            or status.get('work_item_id') != work_item_id
            or status.get('run_id') != run_id
            or status.get('session_id') != run_id
            or status.get('thread_id') != run_id):
        return result(False, error_code='RUN_ID_CONTEXT_MISMATCH',
                      error='运行状态身份与请求不一致')
    return result(True, **status)


def redis_status_projection_for_run(run_id, fields):
    """FastAPI /status 的 Redis 辅助镜像闸：只接受同一 run_id。"""
    if not isinstance(fields, dict):
        return None
    redis_run_id = fields.get('run_id')
    if isinstance(redis_run_id, bytes):
        redis_run_id = redis_run_id.decode('utf-8', errors='replace')
    return fields if redis_run_id == run_id else None


def init_run(work_item_id, process_root=None):
    """原子创建一次分析运行；run_id 只能由执行器生成。"""
    if not isinstance(work_item_id, int) or work_item_id <= 0:
        raise ValueError('work_item_id 必须是正整数')
    root = os.path.abspath(process_root or os.path.join(os.getcwd(), '过程文件'))
    item_dir = os.path.join(root, str(work_item_id))
    os.makedirs(item_dir, exist_ok=True)
    for _ in range(128):
        run_id = (f'run_{tfs.beijing_timestamp("%Y%m%d_%H%M%S")}_'
                  f'{work_item_id}_{secrets.token_hex(4)}')
        run_dir = os.path.join(item_dir, run_id)
        try:
            os.mkdir(run_dir)
        except FileExistsError:
            continue
        receipt_name = f'运行回执_{work_item_id}_{run_id}.json'
        receipt_path = os.path.join(run_dir, receipt_name)
        status_path = os.path.join(run_dir, _status_name(work_item_id, run_id))
        receipt = {
            'schema': RUN_RECEIPT_SCHEMA,
            'work_item_id': work_item_id,
            'run_id': run_id,
            'run_dir': os.path.realpath(run_dir),
            'created_at_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        }
        try:
            _write_json_exclusive(receipt_path, receipt)
            _write_json_exclusive(
                status_path, _run_status_payload(work_item_id, run_id, 'INITIALIZED'))
        except Exception:
            try:
                if os.path.exists(receipt_path):
                    os.unlink(receipt_path)
                if os.path.exists(status_path):
                    os.unlink(status_path)
                os.rmdir(run_dir)
            except OSError:
                pass
            raise
        return result(True, work_item_id=work_item_id, run_id=run_id,
                      session_id=run_id, thread_id=run_id,
                      status_url=f'/api/v1/session/{run_id}/status',
                      status='INITIALIZED', run_dir=run_dir, run_receipt={
                          'path': receipt_name,
                          'sha256': sha256_file(receipt_path),
                      })
    raise RuntimeError('RUN_ID_COLLISION_EXHAUSTED：连续碰撞次数过多，未能创建运行目录')


def _analysis_ref_name(work_item_id, run_id):
    return f'分析结果_{work_item_id}_{run_id}.json'


def _receipt_name(work_item_id, run_id):
    return f'运行回执_{work_item_id}_{run_id}.json'


def _validate_ref_object(value, field, expected_name, errors):
    if not isinstance(value, dict) or set(value) != {'path', 'sha256'}:
        errors.append(f'{field} 必须精确包含 path、sha256')
        return None
    if not is_local_artifact_name(value.get('path')) or value.get('path') != expected_name:
        errors.append(f'{field}.path 必须为 {expected_name}')
    digest = value.get('sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        errors.append(f'{field}.sha256 必须为 64 位小写十六进制摘要')
    return value


def _validate_bound_run(plan, plan_path, expected_profile, errors):
    """校验新运行的规范目录、计划文件名与不可复制回执身份。"""
    wid = plan.get('work_item_id')
    run_id = plan.get('run_id')
    if not isinstance(wid, int) or wid <= 0:
        errors.append('work_item_id 必须是正整数')
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        errors.append('run_id 必须为 8-80 位字母、数字、- 或 _')
    if (not isinstance(wid, int) or wid <= 0
            or not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id)):
        return None
    run_dir = os.path.dirname(os.path.abspath(plan_path))
    if (os.path.basename(run_dir) != run_id
            or os.path.basename(os.path.dirname(run_dir)) != str(wid)):
        errors.append('RUN_ID_CONTEXT_MISMATCH/SOURCE_RUN_DIRECTORY_MISMATCH：计划必须位于 过程文件/<id>/<run_id>/')
    if os.path.basename(plan_path) != _plan_name(wid, run_id):
        errors.append(f'RUN_ID_CONTEXT_MISMATCH：计划文件名必须为 {_plan_name(wid, run_id)}')
    receipt_ref = (_validate_ref_object(
        plan.get('run_receipt'), 'run_receipt', _receipt_name(wid, run_id), errors)
                   if 'run_receipt' in plan else None)
    if plan.get('plan_profile') != expected_profile:
        errors.append(f'plan_profile 必须为 {expected_profile}')
    if receipt_ref is None:
        return None
    receipt_path_value = receipt_ref.get('path')
    receipt_digest = receipt_ref.get('sha256')
    if (receipt_path_value != _receipt_name(wid, run_id)
            or not isinstance(receipt_digest, str)
            or not re.fullmatch(r'[0-9a-f]{64}', receipt_digest)):
        return None
    receipt_path = os.path.join(run_dir, receipt_path_value)
    receipt = None
    if not os.path.isfile(receipt_path):
        errors.append(f'run_receipt 文件不存在：{receipt_path}')
    elif os.path.islink(receipt_path):
        errors.append('run_receipt 不得为符号链接')
    elif sha256_file(receipt_path) != receipt_digest:
        errors.append('run_receipt 摘要不一致')
    else:
        receipt = _read_json_object(receipt_path, 'run_receipt', errors)
    if receipt is not None:
        if (receipt.get('schema') != RUN_RECEIPT_SCHEMA
                or receipt.get('work_item_id') != wid
                or receipt.get('run_id') != run_id):
            errors.append('RUN_ID_CONTEXT_MISMATCH：run_receipt 身份与计划不一致')
        if (not isinstance(receipt.get('created_at_utc'), str)
                or not receipt['created_at_utc'].endswith('Z')):
            errors.append('run_receipt.created_at_utc 必须为非空 UTC 时间')
        if receipt.get('run_dir') != os.path.realpath(run_dir):
            errors.append('RUN_ID_CONTEXT_MISMATCH/SOURCE_RUN_DIRECTORY_MISMATCH：运行回执未绑定当前规范目录')
    if errors:
        return None
    return {
        'run_dir': run_dir,
        'receipt_path': receipt_path,
        'plan_path': os.path.abspath(plan_path),
        'plan_sha256': sha256_file(plan_path),
    }


def materialize_run_bound(plan, plan_path):
    """校验 SKIP/QC 终局的运行回执绑定，返回现有完整计划结构。"""
    errors = []
    required = ('version', 'plan_profile', 'work_item_id', 'run_id', 'run_receipt',
                'skill', 'expected_rev', 'expected_state', 'verdict', 'rules_source', 'artifacts')
    for field in required:
        if field not in plan:
            errors.append(f'缺少字段 {field}')
    if plan.get('version') != PLAN_VERSION:
        errors.append(f'{RUN_BOUND_PROFILE} 仅允许 version={PLAN_VERSION}')
    if plan.get('verdict') not in ROUTING_VERDICTS | QC_VERDICTS:
        errors.append(f'{RUN_BOUND_PROFILE} 仅允许 SKIP-ANALYSIS、NEED-INFO、NEED-REVIEW')
    meta = _validate_bound_run(plan, plan_path, RUN_BOUND_PROFILE, errors)
    if errors:
        return None, None, errors
    expanded = copy.deepcopy(plan)
    expanded.pop('plan_profile', None)
    expanded.pop('run_receipt', None)
    meta['snapshot_sha256'] = meta['plan_sha256']
    return expanded, meta, []


def _render_report(snapshot):
    """从结构化分析快照确定性渲染需求分析报告。"""
    report = snapshot.get('report')
    errors = []
    if not isinstance(report, dict):
        return '', ['analysis_result.report 必须为对象']
    closure = report.get('closure')
    description = report.get('analysis_description')
    traceability = report.get('traceability')
    if not isinstance(closure, dict) or set(closure) != set(ITERATION_ANALYSIS_CLOSURE_REQUIREMENTS):
        errors.append('analysis_result.report.closure 必须精确覆盖五个迭代分析闭环章节')
    if not isinstance(description, dict):
        errors.append('analysis_result.report.analysis_description 必须为对象')
        description = {}
    menu_path = description.get('menu_path')
    categories = description.get('categories')
    if not isinstance(menu_path, str) or not menu_path.strip():
        errors.append('analysis_result.report.analysis_description.menu_path 必须为非空字符串')
    if not isinstance(categories, list) or not categories:
        errors.append('analysis_result.report.analysis_description.categories 必须为非空数组')
        categories = []
    category_names = []
    for index, category in enumerate(categories, start=1):
        if not isinstance(category, dict):
            errors.append(f'analysis_description.categories[{index}] 必须为对象')
            continue
        name = category.get('category')
        items = category.get('items')
        if not isinstance(name, str) or name not in ANALYSIS_DESCRIPTION_REQUIREMENTS:
            errors.append(f'analysis_description.categories[{index}].category 不合法')
        else:
            category_names.append(name)
        if (not isinstance(items, list) or not items
                or not all(isinstance(item, dict)
                           and isinstance(item.get('label'), str) and item['label'].strip()
                           and isinstance(item.get('content'), str) and item['content'].strip()
                           for item in items)):
            errors.append(f'analysis_description.categories[{index}].items 必须为非空的 label/content 对象数组')
    if len(category_names) != len(set(category_names)):
        errors.append('analysis_description.categories.category 不可重复')
    if not isinstance(traceability, list) or not traceability:
        errors.append('analysis_result.report.traceability 必须为非空数组')
        traceability = []
    trace_fields = ('id', 'scope', 'behavior', 'acceptance', 'status', 'basis')
    for index, row in enumerate(traceability, start=1):
        if (not isinstance(row, dict) or set(row) != set(trace_fields)
                or not all(isinstance(row.get(field), str) and row[field].strip()
                           and '|' not in row[field] and '\n' not in row[field]
                           for field in trace_fields)):
            errors.append(f'analysis_result.report.traceability[{index}] 必须精确包含 6 个非空且不含表格分隔符的字段')
    if errors:
        return '', errors

    lines = [
        '# 需求分析报告',
        f'<!-- auto-req-run:{snapshot["run_id"]} -->',
        '',
        '## 一、分析结论',
        f'- **终局**：{snapshot.get("verdict", "")}',
        '',
        '## 二、迭代分析闭环',
    ]
    for heading, labels in ITERATION_ANALYSIS_CLOSURE_REQUIREMENTS.items():
        section = closure.get(heading)
        if not isinstance(section, dict) or set(section) != set(labels):
            errors.append(f'analysis_result.report.closure.{heading} 必须精确包含：{list(labels)}')
            continue
        lines.append(f'### {heading}')
        for label in labels:
            value = section.get(label)
            if not isinstance(value, str) or not value.strip():
                errors.append(f'analysis_result.report.closure.{heading}.{label} 必须为非空字符串')
            else:
                lines.append(f'- **{label}**：{value.strip()}')
    lines.extend(['', '## 三、分析者描述', f'- **菜单路径**：{menu_path.strip()}'])
    for category in categories:
        if not isinstance(category, dict):
            continue
        lines.append(f'### {category.get("category", "")}（业务分析）')
        for item in category.get('items') or []:
            if isinstance(item, dict):
                lines.append(f'- **{item.get("label", "")}**：{item.get("content", "").strip()}')
    lines.extend([
        '', f'## {TRACEABILITY_HEADING}',
        '| ' + ' | '.join(TRACEABILITY_HEADERS) + ' |',
        '| ' + ' | '.join('---' for _ in TRACEABILITY_HEADERS) + ' |',
    ])
    for row in traceability:
        lines.append('| ' + ' | '.join(row[field].strip() for field in trace_fields) + ' |')
    return '\n'.join(lines) + '\n', errors


def _read_json_object(path, field, errors):
    try:
        with open(path, 'r', encoding='utf-8') as source:
            value = json.load(source)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f'{field} 无法读取：{exc}')
        return None
    if not isinstance(value, dict):
        errors.append(f'{field} 根节点必须为 JSON object')
        return None
    return value


def materialize_analysis_ref(plan, plan_path):
    """校验回执/快照引用，并生成兼容现有验证器的完整 v2 计划。"""
    errors = []
    required = ('version', 'plan_profile', 'work_item_id', 'run_id',
                'expected_rev', 'expected_state', 'run_receipt', 'analysis_result')
    for field in required:
        if field not in plan:
            errors.append(f'缺少字段 {field}')
    if plan.get('version') != PLAN_VERSION:
        errors.append(f'{ANALYSIS_REF_PROFILE} 仅允许 version={PLAN_VERSION}')
    if plan.get('plan_profile') != ANALYSIS_REF_PROFILE:
        errors.append(f'plan_profile 必须为 {ANALYSIS_REF_PROFILE}')
    wid = plan.get('work_item_id')
    run_id = plan.get('run_id')
    if not isinstance(wid, int) or wid <= 0:
        errors.append('work_item_id 必须是正整数')
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        errors.append('run_id 必须为 8-80 位字母、数字、- 或 _')
    if not isinstance(plan.get('expected_rev'), int) or plan.get('expected_rev', 0) < 1:
        errors.append('expected_rev 必须是正整数')
    if not isinstance(plan.get('expected_state'), str) or not plan.get('expected_state', '').strip():
        errors.append('expected_state 必须是非空字符串')
    if not isinstance(wid, int) or wid <= 0 or not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        return None, None, errors
    run_dir = os.path.dirname(os.path.abspath(plan_path))
    if os.path.basename(run_dir) != run_id or os.path.basename(os.path.dirname(run_dir)) != str(wid):
        errors.append('RUN_ID_CONTEXT_MISMATCH/SOURCE_RUN_DIRECTORY_MISMATCH：瘦计划必须位于 过程文件/<id>/<run_id>/')
    if os.path.basename(plan_path) != _plan_name(wid, run_id):
        errors.append(f'RUN_ID_CONTEXT_MISMATCH：计划文件名必须为 {_plan_name(wid, run_id)}')
    receipt_ref = (_validate_ref_object(
        plan.get('run_receipt'), 'run_receipt', _receipt_name(wid, run_id), errors)
                   if 'run_receipt' in plan else None)
    analysis_ref = (_validate_ref_object(
        plan.get('analysis_result'), 'analysis_result', _analysis_ref_name(wid, run_id), errors)
                    if 'analysis_result' in plan else None)
    if receipt_ref is None or analysis_ref is None:
        return None, None, errors
    expected_refs = (
        ('run_receipt', receipt_ref, _receipt_name(wid, run_id)),
        ('analysis_result', analysis_ref, _analysis_ref_name(wid, run_id)),
    )
    if any(
            ref.get('path') != expected_name
            or not isinstance(ref.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', ref.get('sha256'))
            for _, ref, expected_name in expected_refs):
        return None, None, errors
    receipt_path = os.path.join(run_dir, receipt_ref['path'])
    snapshot_path = os.path.join(run_dir, analysis_ref['path'])
    readable = {}
    for field, path, expected_digest in (
            ('run_receipt', receipt_path, receipt_ref['sha256']),
            ('analysis_result', snapshot_path, analysis_ref['sha256'])):
        if not os.path.isfile(path):
            errors.append(f'{field} 文件不存在：{path}')
        elif os.path.islink(path):
            errors.append(f'{field} 不得为符号链接')
        elif sha256_file(path) != expected_digest:
            errors.append(f'{field} 摘要不一致')
        else:
            readable[field] = path
    receipt = (_read_json_object(readable['run_receipt'], 'run_receipt', errors)
               if 'run_receipt' in readable else None)
    snapshot = (_read_json_object(readable['analysis_result'], 'analysis_result', errors)
                if 'analysis_result' in readable else None)
    if receipt is not None:
        expected_identity = {'work_item_id': wid, 'run_id': run_id}
        if (receipt.get('schema') != RUN_RECEIPT_SCHEMA
                or any(receipt.get(key) != value for key, value in expected_identity.items())):
            errors.append('run_receipt 身份与瘦计划不一致')
        if (not isinstance(receipt.get('created_at_utc'), str)
                or not receipt['created_at_utc'].endswith('Z')):
            errors.append('run_receipt.created_at_utc 必须为非空 UTC 时间')
        if receipt.get('run_dir') != os.path.realpath(run_dir):
            errors.append('RUN_ID_CONTEXT_MISMATCH/SOURCE_RUN_DIRECTORY_MISMATCH：运行回执未绑定当前规范目录')
    if snapshot is None:
        return None, None, errors
    expected_identity = {'work_item_id': wid, 'run_id': run_id}
    if (snapshot.get('schema') != ANALYSIS_RESULT_SCHEMA
            or any(snapshot.get(key) != value for key, value in expected_identity.items())):
        errors.append('analysis_result 身份与瘦计划不一致')
    if (not isinstance(snapshot.get('generated_at_utc'), str)
            or not snapshot['generated_at_utc'].endswith('Z')):
        errors.append('analysis_result.generated_at_utc 必须为非空 UTC 时间')
    forbidden = {'version', 'skill', 'expected_rev', 'expected_state', 'tags', 'state_to',
                 'rules_source', 'artifacts', 'analysis_description',
                 'plan_profile', 'run_receipt', 'analysis_result'}
    present = sorted(forbidden & set(snapshot))
    if present:
        errors.append(f'analysis_result 不得包含执行字段：{present}')
    verdict = snapshot.get('verdict')
    if verdict not in ANALYSIS_VERDICTS:
        errors.append(f'analysis_result.verdict 必须为 {sorted(ANALYSIS_VERDICTS)} 之一')
    if snapshot.get('confirmation_policy') != SINGLE_CONFIRMATION_POLICY:
        errors.append(
            f'analysis_result.confirmation_policy 必须为 {SINGLE_CONFIRMATION_POLICY!r}，'
            'PM 可回答的方案或验收歧义必须回退 QC NEED-REVIEW')
    report_content, report_errors = _render_report(snapshot)
    errors.extend(report_errors)
    if errors:
        return None, None, errors
    semantic = {
        key: copy.deepcopy(value) for key, value in snapshot.items()
        if key not in {'schema', 'work_item_id', 'run_id', 'created_at_utc', 'generated_at_utc',
                       'verdict', 'report'}
    }
    categories = [entry['category'] for entry in snapshot['report']['analysis_description']['categories']]
    expanded = {
        **semantic,
        'version': PLAN_VERSION,
        'run_id': run_id,
        'skill': 'auto-req-analysis',
        'work_item_id': wid,
        'expected_rev': plan.get('expected_rev'),
        'expected_state': plan.get('expected_state'),
        'verdict': verdict,
        'rules_source': {'qc': QC_RULE_SOURCE, 'analysis': 'evidence-loop-v2'},
        'analysis_description': {'categories': categories},
        'generated_at_utc': snapshot.get('generated_at_utc', snapshot.get('created_at_utc', '')),
    }
    tags, state_to, _ = expected_for(expanded)
    expanded['tags'] = sorted(tags)
    expanded['state_to'] = state_to
    report_name = f'需求分析报告_{wid}_{run_id}.md'
    expanded['artifacts'] = [{'kind': 'change-plan', 'path': report_name}]
    meta = {
        'run_dir': run_dir,
        'receipt_path': receipt_path,
        'snapshot_path': snapshot_path,
        'snapshot_sha256': analysis_ref['sha256'],
        'report_path': os.path.join(run_dir, report_name),
        'report_content': report_content,
        'plan_path': os.path.abspath(plan_path),
        'plan_sha256': sha256_file(plan_path),
    }
    return expanded, meta, []


def _validate_materialized_plan(expanded, meta, check_files=True):
    if not check_files:
        return validate_plan(expanded, os.path.join(meta['run_dir'], 'plan.json'), check_files=False)
    with tempfile.TemporaryDirectory(prefix='analysis-ref-validate-') as directory:
        report_name = expanded['artifacts'][0]['path']
        report_path = os.path.join(directory, report_name)
        with open(report_path, 'w', encoding='utf-8') as output:
            output.write(meta['report_content'])
        return validate_plan(expanded, os.path.join(directory, 'plan.json'), check_files=True)


def _classify_ref_errors(errors):
    source_tokens = ('run_receipt', 'RUN_ID_CONTEXT_MISMATCH', '工作项', 'expected_rev', 'expected_state')
    source_errors = [error for error in errors if any(token in error for token in source_tokens)]
    analysis_errors = [error for error in errors if error not in source_errors]
    return {'analysis': analysis_errors, 'source': source_errors, 'execution': []}


def _ensure_frozen_analysis(meta, plan):
    """冻结快照与报告；同 run 的不同摘要永久拒绝。"""
    if sha256_file(meta['snapshot_path']) != meta['snapshot_sha256']:
        raise ValueError('analysis_result 摘要在冻结前发生变化')
    freeze_path = os.path.join(
        meta['run_dir'], f'分析冻结_{plan["work_item_id"]}_{plan["run_id"]}.json')
    freeze = {
        'schema': 'analysis-finalization-v1',
        'work_item_id': plan['work_item_id'],
        'run_id': plan['run_id'],
        'analysis_result_sha256': meta['snapshot_sha256'],
        'report_sha256': hashlib.sha256(meta['report_content'].encode('utf-8')).hexdigest(),
    }
    if os.path.exists(freeze_path):
        existing = _read_json_object(freeze_path, 'analysis_finalization', [])
        if existing != freeze:
            raise ValueError('RUN_ID_ALREADY_FINALIZED：同一 run_id 已冻结为不同分析摘要')
    else:
        try:
            _write_json_exclusive(freeze_path, freeze)
        except FileExistsError:
            return _ensure_frozen_analysis(meta, plan)
    if os.path.exists(meta['report_path']):
        with open(meta['report_path'], 'r', encoding='utf-8') as source:
            if source.read() != meta['report_content']:
                raise ValueError('RUN_ID_ALREADY_FINALIZED：同一 run_id 的报告内容与冻结快照不一致')
    else:
        try:
            with open(meta['report_path'], 'x', encoding='utf-8') as output:
                output.write(meta['report_content'])
        except FileExistsError:
            return _ensure_frozen_analysis(meta, plan)
    return freeze_path


def _ensure_frozen_plan(meta, plan):
    """冻结本 run 的计划文件摘要；任何 profile 都不能改写同一 run 的终局。"""
    if sha256_file(meta['plan_path']) != meta['plan_sha256']:
        raise ValueError('RUN_ID_ALREADY_FINALIZED：计划在校验后发生变化')
    freeze_path = os.path.join(
        meta['run_dir'], f'计划冻结_{plan["work_item_id"]}_{plan["run_id"]}.json')
    freeze = {
        'schema': 'run-plan-finalization-v1',
        'work_item_id': plan['work_item_id'],
        'run_id': plan['run_id'],
        'plan_sha256': meta['plan_sha256'],
    }
    if os.path.exists(freeze_path):
        errors = []
        existing = _read_json_object(freeze_path, 'plan_finalization', errors)
        if errors or existing != freeze:
            raise ValueError('RUN_ID_ALREADY_FINALIZED：同一 run_id 已冻结为不同计划摘要')
    else:
        try:
            _write_json_exclusive(freeze_path, freeze)
        except FileExistsError:
            return _ensure_frozen_plan(meta, plan)
    return freeze_path


def artifact_path(plan_path, artifact):
    return os.path.join(os.path.dirname(os.path.abspath(plan_path)), artifact['path'])


def is_local_artifact_name(value):
    """只接受计划同目录下的文件名，拒绝绝对路径和目录穿越。"""
    return (isinstance(value, str) and value not in ('', '.', '..')
            and not os.path.isabs(value) and os.path.basename(value) == value)


def expected_artifact_filename(plan, kind):
    wid = plan['work_item_id']
    run_id = plan['run_id']
    analysis_source = plan.get('rules_source')
    if isinstance(analysis_source, dict):
        analysis_source = analysis_source.get('analysis')
    analysis_filename = (
        f'需求分析报告_{wid}_{run_id}.md'
        if analysis_source == 'evidence-loop-v2'
        else f'变更方案_{wid}_{run_id}.md'
    )
    names = {
        'qc-followup': f'待补充信息_{wid}_{run_id}.json',
        # change-plan 是稳定的机器契约；evidence-loop-v2 起仅更新用户可见文件名。
        'change-plan': analysis_filename,
        'manual-followup': f'待确认清单_{wid}_{run_id}.md',
    }
    return names.get(kind)


def allowed_artifact_filenames(plan, kind):
    """返回可回放文件名；新报告名优先，旧“变更方案”仅作历史兼容。"""
    preferred = expected_artifact_filename(plan, kind)
    if preferred is None:
        return set()
    allowed = {preferred}
    if kind == 'change-plan' and preferred.startswith('需求分析报告_'):
        allowed.add(f'变更方案_{plan["work_item_id"]}_{plan["run_id"]}.md')
    return allowed


def validate_rules_source(plan, errors):
    """新计划记录两阶段规则版本；历史字符串仅作回放兼容。"""
    source = plan['rules_source']
    if isinstance(source, str):
        if plan['version'] != 1:
            errors.append('v2 计划的 rules_source 必须为分阶段对象')
            return
        if source not in LEGACY_RULE_SOURCES:
            errors.append('历史 rules_source 仅允许 pre-qc-v1 或 fallback')
        return
    if not isinstance(source, dict):
        errors.append('rules_source 必须为分阶段对象，或兼容历史字符串')
        return
    expected = {'qc': QC_RULE_SOURCE}
    if plan['verdict'] in ANALYSIS_VERDICTS:
        expected['analysis'] = None
    if set(source) != set(expected):
        errors.append(f'rules_source 必须包含字段：{sorted(expected)}')
        return
    if source.get('qc') != QC_RULE_SOURCE:
        errors.append(f'rules_source.qc 必须为 {QC_RULE_SOURCE!r}')
    if 'analysis' in expected:
        allowed = ({'evidence-loop-v1', 'evidence-loop-v2'}
                   if plan['version'] == PLAN_VERSION else ANALYSIS_RULE_SOURCES)
        if source.get('analysis') not in allowed:
            errors.append(f'rules_source.analysis 必须为 {sorted(allowed)}')


def validate_checklist(plan, errors):
    """校验 QC inline 清单可被责任方和下游直接使用。"""
    checklist = plan.get('checklist')
    if not isinstance(checklist, dict):
        errors.append('QC 计划必须含 checklist 对象')
        return
    for field in ('work_item', 'responsible', 'generated_at_utc', 'next'):
        if not isinstance(checklist.get(field), str) or not checklist[field].strip():
            errors.append(f'checklist.{field} 必须为非空字符串')
    if checklist.get('verdict') != plan['verdict']:
        errors.append('checklist.verdict 必须与计划 verdict 一致')
    expected_tag = 'PM-AI-QC-NEED-INFO' if plan['verdict'] == 'NEED-INFO' else 'PM-AI-QC-NEED-REVIEW'
    if checklist.get('tag') != expected_tag:
        errors.append('checklist.tag 必须与计划终局标签一致')
    items = checklist.get('items')
    if not isinstance(items, list) or not items:
        errors.append('checklist.items 必须为非空数组')
        return
    if len(items) > MAX_QC_ITEMS:
        errors.append(f'checklist.items 最多 {MAX_QC_ITEMS} 项；请合并同类问题并保留会改变实现或验收的最高优先项')
    ids = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append(f'checklist.items[{index}] 必须为对象')
            continue
        missing = [field for field in CHECKLIST_ITEM_FIELDS if field not in item]
        if missing:
            errors.append(f'checklist.items[{index}] 缺少字段：{missing}')
            continue
        item_id = item['id']
        if not isinstance(item_id, str) or not ANALYSIS_GAP_ID_RE.fullmatch(item_id):
            errors.append(f'checklist.items[{index}].id 必须为稳定的字母开头标识')
        elif single_confirmation_enabled(plan) and re.fullmatch(r'q[0-9]+', item_id):
            errors.append(
                f'checklist.items[{index}].id 在单次集中确认策略下必须使用语义稳定 ID；'
                '不得按本轮顺序使用 q1/q2（示例：q-scope、q-value-rule、q-acceptance）')
        elif item_id in ids:
            errors.append(f'checklist.items.id 不可重复：{item_id}')
        else:
            ids.add(item_id)
        if not isinstance(item['question'], str) or not item['question'].strip():
            errors.append(f'checklist.items[{index}].question 必须为非空字符串')
        if (not isinstance(item['options'], list) or not item['options']
                or not all(isinstance(option, str) and option.strip() for option in item['options'])):
            errors.append(f'checklist.items[{index}].options 必须为非空字符串数组')
        if not isinstance(item['allow_other'], bool):
            errors.append(f'checklist.items[{index}].allow_other 必须为布尔值')


def single_confirmation_enabled(plan):
    return plan.get('confirmation_policy') == SINGLE_CONFIRMATION_POLICY


def validate_confirmation_policy(plan, errors):
    """新计划把全部 PM 业务确认前置到 QC；字段缺失时按历史契约回放。"""
    policy = plan.get('confirmation_policy')
    if policy is None:
        return
    if policy != SINGLE_CONFIRMATION_POLICY:
        errors.append(f'confirmation_policy 仅允许 {SINGLE_CONFIRMATION_POLICY!r}')
        return
    if plan['verdict'] in ROUTING_VERDICTS or plan['verdict'] == 'PASS':
        errors.append('confirmation_policy 仅用于 QC NEED-* 或分析终局计划')
    if plan['verdict'] in ANALYSIS_VERDICTS and plan.get('analysis_gaps'):
        errors.append(
            '单次集中确认策略要求分析计划 analysis_gaps=[]；'
            'PM 可回答的业务缺口必须回退 QC NEED-REVIEW 并合并进 checklist')
    kb = plan.get('kb')
    if not isinstance(kb, dict):
        errors.append('新策略的非 SKIP 计划必须含 kb 对象')
        return
    if not kb:
        errors.append('新策略的非 SKIP 计划 kb 对象不得为空')
        return
    if kb:
        if not isinstance(kb.get('ready'), bool):
            errors.append('新策略的 kb.ready 必须明确代码图谱是否就绪')
        if not isinstance(kb.get('source_ready'), bool):
            errors.append('新策略的 kb.source_ready 必须明确源码 MCP 是否就绪')
        if not isinstance(kb.get('source_required'), bool):
            errors.append('新策略的 kb.source_required 必须明确本轮是否需要源码核验')
        if not isinstance(kb.get('database_ready'), bool):
            errors.append('新策略的 kb.database_ready 必须明确数据库图谱是否就绪')
        rules_source = plan.get('rules_source')
        if (isinstance(rules_source, dict)
                and rules_source.get('analysis') == 'evidence-loop-v2'
                and not isinstance(kb.get('database_required'), bool)):
            errors.append('evidence-loop-v2 的 kb.database_required 必须明确本轮是否需要数据库图谱核验')
        for index, finding in enumerate(kb.get('findings') or [], start=1):
            if (not isinstance(finding, dict)
                    or finding.get('source_type') not in ('code', 'database')):
                errors.append(
                    f'新策略的 kb.findings[{index}].source_type 必须为 code 或 database')
            if isinstance(finding, dict):
                for field in HUMAN_EVIDENCE_FIELDS:
                    if not isinstance(finding.get(field), str) or not finding[field].strip():
                        errors.append(
                            f'新策略的 kb.findings[{index}].{field} 必须为非空字符串，'
                            '供 Redis 佐证清单直接展示')
                if finding.get('source_tool') in TFS_REQUIREMENTS_TOOLS:
                    errors.append(
                        f'新策略的 kb.findings[{index}].source_tool 属于需求历史工具；'
                        '必须改写到 tfs_requirements.findings，不得混入代码图谱')
        tools_used = kb.get('tools_used') or []
        source_tools_used = SOURCE_CODE_TOOLS & set(tools_used) if isinstance(tools_used, list) else set()
        source_findings = [
            finding for finding in (kb.get('findings') or [])
            if isinstance(finding, dict) and finding.get('source_tool') in SOURCE_CODE_TOOLS
        ]
        for index, finding in enumerate(kb.get('findings') or [], start=1):
            if (isinstance(finding, dict) and finding.get('source_tool') in SOURCE_CODE_TOOLS
                    and finding.get('source_type') != 'code'):
                errors.append(
                    f'源码 MCP finding kb.findings[{index}].source_type 必须为 code')
            if (isinstance(finding, dict) and finding.get('source_tool') in SOURCE_CODE_TOOLS
                    and finding.get('source_tool') not in source_tools_used):
                errors.append(
                    f'源码 MCP finding kb.findings[{index}].source_tool 必须出现在 tools_used')
        if source_tools_used and kb.get('source_required') is not True:
            errors.append('调用 search_source/search_symbol 时 kb.source_required 必须为 true')
        if source_findings and kb.get('source_required') is not True:
            errors.append('携带源码 MCP finding 时 kb.source_required 必须为 true')
        if source_findings and kb.get('source_ready') is not True:
            errors.append('携带源码 MCP finding 时 kb.source_ready 必须为 true')
        if kb.get('source_required') is True and kb.get('source_ready') is True:
            if not source_tools_used:
                errors.append('kb.source_required=true 且源码 MCP 就绪时必须实际调用 '
                              'search_source 或 search_symbol')
            if not source_findings:
                errors.append('kb.source_required=true 且源码 MCP 就绪时必须留下源码 finding')

    for source in ('wiki', 'tfs_requirements'):
        evidence = plan.get(source)
        if not isinstance(evidence, dict):
            continue
        for index, finding in enumerate(evidence.get('findings') or [], start=1):
            if not isinstance(finding, dict):
                continue
            for field in HUMAN_EVIDENCE_FIELDS:
                if not isinstance(finding.get(field), str) or not finding[field].strip():
                    errors.append(
                        f'新策略的 {source}.findings[{index}].{field} 必须为非空字符串，'
                        '供 Redis 佐证清单直接展示')


def validate_general_rule_coverage(plan, errors):
    """evidence-loop-v2 的通用七面必须逐面确认或明确不适用。"""
    rules_source = plan.get('rules_source')
    if (not isinstance(rules_source, dict)
            or rules_source.get('analysis') != 'evidence-loop-v2'):
        return

    coverage = plan.get('general_rule_coverage')
    if not isinstance(coverage, dict):
        errors.append('evidence-loop-v2 分析计划必须含 general_rule_coverage 对象')
        return
    if set(coverage) != GENERAL_RULE_COVERAGE_DIMENSIONS:
        errors.append(
            'general_rule_coverage 必须精确覆盖 scope、workflow、business_semantics、'
            'business_rules、permissions、exceptions、acceptance')
    for dimension in sorted(GENERAL_RULE_COVERAGE_DIMENSIONS):
        decision = coverage.get(dimension)
        if not isinstance(decision, dict):
            errors.append(f'general_rule_coverage.{dimension} 必须为对象')
            continue
        status = decision.get('status')
        source = decision.get('source')
        if status not in GENERAL_RULE_COVERAGE_STATUSES:
            errors.append(
                f'general_rule_coverage.{dimension}.status 必须为 '
                f'{sorted(GENERAL_RULE_COVERAGE_STATUSES)} 之一；通用七面不得使用默认值')
        if not isinstance(decision.get('basis'), str) or not decision['basis'].strip():
            errors.append(f'general_rule_coverage.{dimension}.basis 必须为非空字符串')
        if status == 'CONFIRMED' and source not in TRACEABLE_CONFIRMATION_SOURCES:
            errors.append(f'general_rule_coverage.{dimension}=CONFIRMED 必须声明可追溯确认来源')
        elif status == 'NOT_APPLICABLE' and source != 'not-applicable':
            errors.append(f'general_rule_coverage.{dimension}=NOT_APPLICABLE 必须声明 not-applicable')
        if dimension in GENERAL_ALWAYS_REQUIRED_DIMENSIONS and status != 'CONFIRMED':
            errors.append(f'general_rule_coverage.{dimension} 不允许 NOT_APPLICABLE，必须有明确依据')


def validate_implementation_evidence(plan, errors):
    """evidence-loop-v2 将实现影响、专项四面与源码/数据库取证绑定。"""
    rules_source = plan.get('rules_source')
    if (not isinstance(rules_source, dict)
            or rules_source.get('analysis') != 'evidence-loop-v2'):
        return

    impacts = plan.get('implementation_impacts')
    if (not isinstance(impacts, list) or not impacts
            or not all(isinstance(item, str) and item in IMPLEMENTATION_IMPACTS
                       for item in impacts)
            or len(impacts) != len(set(impacts))):
        errors.append(
            'evidence-loop-v2 的 implementation_impacts 必须为非空、不重复的实现影响白名单子集')
        impacts = []
    elif 'none' in impacts and len(impacts) != 1:
        errors.append('implementation_impacts=none 时不得同时声明其它实现影响')

    coverage = plan.get('business_rule_coverage')
    if not isinstance(coverage, dict):
        errors.append('evidence-loop-v2 分析计划必须含 business_rule_coverage 对象')
        coverage = {}
    elif set(coverage) != BUSINESS_RULE_COVERAGE_DIMENSIONS:
        errors.append(
            'business_rule_coverage 必须精确覆盖 presentation、empty_value、'
            'maintenance_granularity、historical_data')
    for dimension in BUSINESS_RULE_COVERAGE_DIMENSIONS:
        decision = coverage.get(dimension)
        if not isinstance(decision, dict):
            if coverage:
                errors.append(f'business_rule_coverage.{dimension} 必须为对象')
            continue
        status = decision.get('status')
        source = decision.get('source')
        if status not in BUSINESS_RULE_COVERAGE_STATUSES:
            errors.append(
                f'business_rule_coverage.{dimension}.status 必须为 '
                f'{sorted(BUSINESS_RULE_COVERAGE_STATUSES)} 之一')
        if not isinstance(decision.get('basis'), str) or not decision['basis'].strip():
            errors.append(f'business_rule_coverage.{dimension}.basis 必须为非空字符串')
        if source not in BUSINESS_RULE_COVERAGE_SOURCES:
            errors.append(
                f'business_rule_coverage.{dimension}.source 必须为 '
                f'{sorted(BUSINESS_RULE_COVERAGE_SOURCES)} 之一')
        elif status == 'CONFIRMED' and source not in TRACEABLE_CONFIRMATION_SOURCES:
            errors.append(f'business_rule_coverage.{dimension}=CONFIRMED 必须声明可追溯确认来源')
        elif status == 'DEFAULTED' and source != 'presentation-default':
            errors.append(f'business_rule_coverage.{dimension}=DEFAULTED 仅允许 presentation-default')
        elif status == 'NOT_APPLICABLE' and source != 'not-applicable':
            errors.append(f'business_rule_coverage.{dimension}=NOT_APPLICABLE 必须声明 not-applicable')

    impacts_set = set(impacts)
    if impacts_set & DATABASE_REQUIRED_IMPACTS:
        for dimension in ('empty_value', 'maintenance_granularity', 'historical_data'):
            decision = coverage.get(dimension)
            if isinstance(decision, dict) and decision.get('status') == 'DEFAULTED':
                errors.append(
                    f'数据读写/数据库影响需求不得默认 business_rule_coverage.{dimension}；'
                    '须由工作项、附件、已继承 PM 答案或已证实产品规则确认，'
                    '否则回退 QC NEED-REVIEW')

    kb = plan.get('kb')
    if not isinstance(kb, dict):
        return
    source_required = bool(impacts_set & SOURCE_REQUIRED_IMPACTS)
    database_required = bool(impacts_set & DATABASE_REQUIRED_IMPACTS)
    if kb.get('source_required') is not source_required:
        errors.append(
            f'implementation_impacts={sorted(impacts_set)} 要求 '
            f'kb.source_required={str(source_required).lower()}')
    if kb.get('database_required') is not database_required:
        errors.append(
            f'implementation_impacts={sorted(impacts_set)} 要求 '
            f'kb.database_required={str(database_required).lower()}')

    tools_used = kb.get('tools_used') or []
    tools_set = set(tools_used) if isinstance(tools_used, list) else set()
    database_tools_used = DATABASE_KNOWLEDGE_TOOLS & tools_set
    database_findings = [
        finding for finding in (kb.get('findings') or [])
        if isinstance(finding, dict) and finding.get('source_type') == 'database'
    ]
    for index, finding in enumerate(database_findings, start=1):
        if finding.get('source_tool') not in DATABASE_KNOWLEDGE_TOOLS:
            errors.append(
                f'evidence-loop-v2 的数据库 finding[{index}].source_tool 必须为 '
                f'{sorted(DATABASE_KNOWLEDGE_TOOLS)} 之一')
        elif finding.get('source_tool') not in tools_set:
            errors.append(
                f'evidence-loop-v2 的数据库 finding[{index}].source_tool 必须出现在 tools_used')
    if database_tools_used and kb.get('database_required') is not True:
        errors.append('调用数据库图谱工具时 kb.database_required 必须为 true')
    if database_findings and kb.get('database_required') is not True:
        errors.append('携带数据库 finding 时 kb.database_required 必须为 true')
    if kb.get('database_required') is True and kb.get('database_ready') is True:
        if not database_tools_used:
            errors.append('kb.database_required=true 且数据库图谱就绪时必须实际调用数据库图谱工具')
        if not database_findings:
            errors.append('kb.database_required=true 且数据库图谱就绪时必须留下数据库 finding')

    gaps = plan.get('evidence_gaps') or []
    gap_types = {
        gap.get('type') for gap in gaps if isinstance(gap, dict) and isinstance(gap.get('type'), str)
    }
    if (kb.get('source_required') is True and kb.get('source_ready') is False
            and not gap_types & {'SOURCE_MCP_UNAVAILABLE', 'SOURCE_SCOPE_PARTIAL',
                                 'SOURCE_VERIFICATION_INCOMPLETE'}):
        errors.append('源码核验必需但不可用时，evidence_gaps 必须记录源码不可用或覆盖缺口')
    if (kb.get('database_required') is True and kb.get('database_ready') is False
            and not gap_types & {'DATABASE_MCP_UNAVAILABLE', 'DB_SCHEMA_PARTIAL',
                                 'DB_RELATION_PARTIAL'}):
        errors.append('数据库核验必需但不可用时，evidence_gaps 必须记录数据库不可用或覆盖缺口')

    acquisition = plan.get('evidence_acquisition')
    if isinstance(acquisition, dict):
        database = acquisition.get('db_knowledge')
        if (database_required and isinstance(database, dict)
                and database.get('query_status') == 'SKIPPED'):
            errors.append(
                '数据库核验必需时 evidence_acquisition.db_knowledge 不得为 SKIPPED；'
                '须实际查询，或如实记录来源不可用/覆盖缺口')


def validate_ui_baseline(plan, errors):
    """界面 AUTO 计划须用至少两类独立证据闭合当前 UI 基线。"""
    baseline = plan.get('ui_baseline')
    required = (
        plan.get('verdict') == 'AUTO-ANA'
        and 'field-ui-copy' in (plan.get('auto_scopes') or [])
        and single_confirmation_enabled(plan)
    )
    if baseline is None:
        if required:
            errors.append(
                '新策略 AUTO-ANA 的 field-ui-copy 范围必须含 ui_baseline，'
                '并由产品知识、运行观察、实现证据中至少两类交叉证实')
        return
    if not isinstance(baseline, dict):
        errors.append('ui_baseline 必须为对象')
        return
    sources = baseline.get('sources')
    if not isinstance(sources, list) or not sources:
        errors.append('ui_baseline.sources 必须为非空数组')
        return

    families = set()
    identities = set()
    kb_findings = (plan.get('kb') or {}).get('findings') or []
    for index, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            errors.append(f'ui_baseline.sources[{index}] 必须为对象')
            continue
        source_type = source.get('type')
        ref = source.get('ref')
        if source_type not in UI_BASELINE_SOURCE_FAMILIES:
            errors.append(
                f'ui_baseline.sources[{index}].type 必须为 '
                f'{sorted(UI_BASELINE_SOURCE_FAMILIES)} 之一')
            continue
        if not isinstance(ref, str) or not ref.strip():
            errors.append(f'ui_baseline.sources[{index}].ref 必须为非空字符串')
            continue
        identity = (source_type, ref.strip())
        if identity in identities:
            errors.append(f'ui_baseline.sources[{index}] 与前项重复')
            continue
        identities.add(identity)
        families.add(UI_BASELINE_SOURCE_FAMILIES[source_type])

        if source_type == 'wiki':
            if not re.fullmatch(r'wiki:[0-9]+', ref):
                errors.append(f'ui_baseline.sources[{index}] 的 wiki ref 必须为 wiki:<索引>')
            elif not is_confirmed_evidence_ref(plan, ref):
                errors.append(
                    f'ui_baseline.sources[{index}] 的 {ref} 必须指向 wiki.findings 的已证实项')
        elif source_type in {'code-graph', 'source-code'}:
            if not re.fullmatch(r'kb:[0-9]+', ref):
                errors.append(f'ui_baseline.sources[{index}] 的实现证据 ref 必须为 kb:<索引>')
                continue
            if not is_confirmed_evidence_ref(plan, ref):
                errors.append(
                    f'ui_baseline.sources[{index}] 的 {ref} 必须指向 kb.findings 的已证实项')
                continue
            finding = kb_findings[int(ref[3:])]
            is_source_code = finding.get('source_tool') in SOURCE_CODE_TOOLS
            if source_type == 'source-code' and not is_source_code:
                errors.append(
                    f'ui_baseline.sources[{index}] 的 source-code ref 必须指向源码 MCP finding')
            if source_type == 'code-graph' and is_source_code:
                errors.append(
                    f'ui_baseline.sources[{index}] 的 code-graph ref 不得指向源码 MCP finding')

    if required and len(families) < 2:
        errors.append(
            '新策略 AUTO-ANA 的 field-ui-copy 范围要求 ui_baseline 至少覆盖两类独立证据：'
            '产品知识、运行观察、实现证据；同一类多条不能互相交叉证实')


def validate_knowledge_route(plan, errors):
    """新非 SKIP 计划必须快照产品路由；历史计划保持兼容。"""
    route = plan.get('knowledge_route')
    if plan['verdict'] in ROUTING_VERDICTS:
        if route not in (None, {}):
            errors.append('SKIP-ANALYSIS 计划不得声明 knowledge_route')
        return
    if not single_confirmation_enabled(plan):
        return
    if not isinstance(route, dict):
        errors.append('新策略的非 SKIP 计划必须含 knowledge_route 对象')
        return
    status = route.get('status')
    if status not in KNOWLEDGE_ROUTE_STATUSES:
        errors.append(f'knowledge_route.status 必须为 {sorted(KNOWLEDGE_ROUTE_STATUSES)} 之一')
        return
    area = route.get('area')
    if not isinstance(area, str) or not area.strip():
        errors.append('knowledge_route.area 必须为非空字符串')
    servers = route.get('servers')
    if status == 'RESOLVED':
        for field in ('product_id', 'product_name'):
            if not isinstance(route.get(field), str) or not route[field].strip():
                errors.append(f'knowledge_route.{field} 在 RESOLVED 时必须为非空字符串')
        if not isinstance(route.get('profile_version'), int) or route['profile_version'] < 1:
            errors.append('knowledge_route.profile_version 在 RESOLVED 时必须为正整数')
        if not isinstance(servers, dict) or set(servers) != set(KNOWLEDGE_ROUTE_ROLES):
            errors.append('knowledge_route.servers 在 RESOLVED 时必须精确包含四类 MCP 角色')
        elif not all(value is None or isinstance(value, str) and value.strip()
                     for value in servers.values()):
            errors.append('knowledge_route.servers 的值必须为非空 server name 或 null')
    else:
        if servers != {}:
            errors.append('knowledge_route 未解析时 servers 必须为空对象，禁止默认或跨产品回退')
        for source in ('kb', 'tfs_requirements', 'wiki'):
            value = plan.get(source)
            if not isinstance(value, dict):
                continue
            if value.get('ready') is True or value.get('tools_used') or value.get('findings'):
                errors.append(
                    f'knowledge_route 未解析时 {source} 不得声明就绪、调用工具或携带 finding')
        if plan['verdict'] == 'AUTO-ANA':
            errors.append('AUTO-ANA 要求 knowledge_route.status=RESOLVED，禁止跨产品或默认 MCP')


def expected_for(plan):
    """终局契约按 verdict 分派（不再按 skill 名）。

    合并后新 run 用 skill='auto-req-analysis'；旧 skill 名 'auto-req-qc'
    仅作审计标签与向后兼容，终局契约一律由 verdict 决定。PASS 保留仅用于兼容旧 PASS 计划，
    新 SKILL.md 不再产出 PASS 计划（PASS 为内部阶段闸）。
    """
    verdict = plan['verdict']
    if verdict in QC_VERDICTS:
        tag = 'PM-AI-QC-NEED-INFO' if verdict == 'NEED-INFO' else 'PM-AI-QC-NEED-REVIEW'
        return ({tag}, None, {'qc-followup'})
    if verdict in ROUTING_VERDICTS:
        return (set(), None, set())
    if verdict == 'PASS':
        return (set(), None, set())
    if verdict in ANALYSIS_VERDICTS:
        required_kinds = {'change-plan'}
        if plan.get('analysis_gaps') and not single_confirmation_enabled(plan):
            required_kinds.add('manual-followup')
        if verdict == 'AUTO-ANA':
            return ({'PM-AI-AUTO-ANA'}, '已分析', required_kinds)
        if verdict == 'MANUAL-REVIEW':
            return ({'PM-AI-MANUAL-REVIEW'}, None, required_kinds)
        return ({'PM-AI-MANUAL-REVIEW', 'PM-AI-STOP-AUTO'}, None, required_kinds)
    raise ValueError(f'不支持 verdict={verdict!r}')


def validate_analysis_gaps(plan, errors):
    """校验仅供 PM 补齐业务分析信息的待确认项。"""
    gaps = plan.get('analysis_gaps')
    if not isinstance(gaps, list):
        errors.append('分析计划必须含 analysis_gaps 数组（无缺口时填 []）')
        return []

    verdict = plan['verdict']
    if verdict == 'AUTO-ANA' and gaps:
        errors.append('AUTO-ANA 的 analysis_gaps 必须为空数组')
    if gaps and verdict not in ('MANUAL-REVIEW', 'MANUAL-REVIEW-STOP'):
        errors.append('非空 analysis_gaps 仅允许 MANUAL-REVIEW 或 MANUAL-REVIEW-STOP')

    ids = set()
    for index, gap in enumerate(gaps, start=1):
        if not isinstance(gap, dict):
            errors.append(f'analysis_gaps[{index}] 必须为对象')
            continue
        missing = [field for field in ANALYSIS_GAP_FIELDS if field not in gap]
        if missing:
            errors.append(f'analysis_gaps[{index}] 缺少字段：{missing}')
            continue
        gap_id = gap['id']
        if not isinstance(gap_id, str) or not ANALYSIS_GAP_ID_RE.fullmatch(gap_id):
            errors.append(f'analysis_gaps[{index}].id 必须为稳定的字母开头标识')
        elif gap_id in ids:
            errors.append(f'analysis_gaps.id 不可重复：{gap_id}')
        else:
            ids.add(gap_id)
        for field in ('topic', 'missing', 'impact', 'question'):
            if not isinstance(gap[field], str) or not gap[field].strip():
                errors.append(f'analysis_gaps[{index}].{field} 必须为非空字符串')
        if (not isinstance(gap['options'], list) or not gap['options']
                or not all(isinstance(option, str) and option.strip() for option in gap['options'])):
            errors.append(f'analysis_gaps[{index}].options 必须为非空字符串数组')
        if not isinstance(gap['allow_other'], bool):
            errors.append(f'analysis_gaps[{index}].allow_other 必须为布尔值')
    return gaps


def validate_evidence_gaps(plan, errors):
    """校验仅供研发或知识库治理补齐的内部证据缺口。"""
    if plan['version'] != PLAN_VERSION or plan['verdict'] not in ANALYSIS_VERDICTS:
        return []
    gaps = plan.get('evidence_gaps')
    if not isinstance(gaps, list):
        errors.append('v2 分析计划必须含 evidence_gaps 数组（无缺口时填 []）')
        return []
    if gaps and plan['verdict'] == 'AUTO-ANA':
        errors.append('AUTO-ANA 的 evidence_gaps 必须为空数组')
    ids = set()
    for index, gap in enumerate(gaps, start=1):
        if not isinstance(gap, dict):
            errors.append(f'evidence_gaps[{index}] 必须为对象')
            continue
        missing = [field for field in EVIDENCE_GAP_FIELDS if field not in gap]
        if missing:
            errors.append(f'evidence_gaps[{index}] 缺少字段：{missing}')
            continue
        gap_id = gap['id']
        if not isinstance(gap_id, str) or not ANALYSIS_GAP_ID_RE.fullmatch(gap_id):
            errors.append(f'evidence_gaps[{index}].id 必须为稳定的字母开头标识')
        elif gap_id in ids:
            errors.append(f'evidence_gaps.id 不可重复：{gap_id}')
        else:
            ids.add(gap_id)
        for field in ('topic', 'missing', 'impact', 'next_action'):
            if not isinstance(gap[field], str) or not gap[field].strip():
                errors.append(f'evidence_gaps[{index}].{field} 必须为非空字符串')
        if gap['owner'] not in EVIDENCE_GAP_OWNERS:
            errors.append(f'evidence_gaps[{index}].owner 必须为 {sorted(EVIDENCE_GAP_OWNERS)} 之一')
        # type/kind 为可选字段（缺口分类与来源区分）；存在时校验取值。
        if 'type' in gap and gap['type'] not in EVIDENCE_GAP_TYPES:
            errors.append(f'evidence_gaps[{index}].type 必须为 {sorted(EVIDENCE_GAP_TYPES)} 之一')
        if 'kind' in gap and gap['kind'] not in EVIDENCE_GAP_KINDS:
            errors.append(f'evidence_gaps[{index}].kind 必须为 {sorted(EVIDENCE_GAP_KINDS)} 之一')
    return gaps


def validate_tfs_requirements(plan, errors):
    """校验可选的 TFS 需求历史证据对象。"""
    evidence = plan.get('tfs_requirements')
    if evidence is None:
        return
    if not isinstance(evidence, dict):
        errors.append('tfs_requirements 必须为对象')
        return
    required = ('ready', 'coverage', 'tools_used', 'findings', 'note')
    missing = [field for field in required if field not in evidence]
    if missing:
        errors.append(f'tfs_requirements 缺少字段：{missing}')
        return
    if not isinstance(evidence['ready'], bool):
        errors.append('tfs_requirements.ready 必须为布尔值')
    if not isinstance(evidence['coverage'], dict):
        errors.append('tfs_requirements.coverage 必须为对象')
    elif evidence['ready'] and not evidence['coverage']:
        errors.append('tfs_requirements.ready=true 时 coverage 不得为空')
    tools_used = evidence['tools_used']
    if (not isinstance(tools_used, list)
            or not all(isinstance(tool, str) and tool in TFS_REQUIREMENTS_TOOLS for tool in tools_used)
            or len(tools_used) != len(set(tools_used))):
        errors.append(f'tfs_requirements.tools_used 必须为不重复的工具白名单子集：{sorted(TFS_REQUIREMENTS_TOOLS)}')
        tools_used = []
    elif evidence['ready'] and 'get_requirements_summary' not in tools_used:
        errors.append('tfs_requirements.ready=true 时 tools_used 必须包含 get_requirements_summary')
    if not isinstance(evidence['note'], str) or not evidence['note'].strip():
        errors.append('tfs_requirements.note 必须为非空字符串')
    findings = evidence['findings']
    if not isinstance(findings, list):
        errors.append('tfs_requirements.findings 必须为数组')
        return
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f'tfs_requirements.findings[{index}] 必须为对象')
            continue
        missing = [field for field in TFS_REQUIREMENTS_FINDING_FIELDS if field not in finding]
        if missing:
            errors.append(f'tfs_requirements.findings[{index}] 缺少字段：{missing}')
            continue
        if not isinstance(finding['work_item_id'], int) or finding['work_item_id'] <= 0:
            errors.append(f'tfs_requirements.findings[{index}].work_item_id 必须为正整数')
        if not isinstance(finding['fact'], str) or not finding['fact'].strip():
            errors.append(f'tfs_requirements.findings[{index}].fact 必须为非空字符串')
        if not isinstance(finding['state'], str) or finding['state'] not in TFS_REQUIREMENTS_FINDING_STATES:
            errors.append(
                f'tfs_requirements.findings[{index}].state 必须为 '
                f'{sorted(TFS_REQUIREMENTS_FINDING_STATES)} 之一')
        source_tool = finding['source_tool']
        if not isinstance(source_tool, str) or source_tool not in TFS_REQUIREMENTS_TOOLS:
            errors.append(
                f'tfs_requirements.findings[{index}].source_tool 必须为 '
                f'{sorted(TFS_REQUIREMENTS_TOOLS)} 之一')
        elif source_tool not in tools_used:
            errors.append(f'tfs_requirements.findings[{index}].source_tool 必须出现在 tools_used')
        if (finding['state'] == '已证实'
                and (not isinstance(source_tool, str)
                     or source_tool not in TFS_REQUIREMENTS_CONFIRMED_TOOLS)):
            errors.append(
                f'tfs_requirements.findings[{index}] 只有 get_work_item 或 '
                'get_related_work_items 的结果可标为已证实')
        # maturity 为可选字段；存在时校验取值，且「已落地」不得基于未证实/设想级证据。
        maturity = finding.get('maturity')
        if maturity is not None:
            if maturity not in TFS_MATURITY_STATES:
                errors.append(
                    f'tfs_requirements.findings[{index}].maturity 必须为 '
                    f'{sorted(TFS_MATURITY_STATES)} 之一')
            elif maturity == '已落地' and finding['state'] != '已证实':
                errors.append(
                    f'tfs_requirements.findings[{index}].maturity=已落地 要求 state=已证实'
                    '（设想级证据不得支撑「已落地」结论）')


def validate_existing_feature(plan, errors):
    """校验「功能已存在」声明（一套公版假设下，既有能力已满足本次诉求）。

    可选顶层对象 existing_feature：satisfied=true 表示查重命中 maturity=已落地
    的历史方案且其能力覆盖本次需求诉求，本次无需新增改动。此时终局必须
    MANUAL-REVIEW（人工确认；非高风险故不加 STOP-AUTO）——本函数硬闸阻止
    satisfied=true ∧ verdict=AUTO-ANA 的自动放行，与 kb.dedup_ran 等 AUTO
    硬闸一致（缺一即“改判 MANUAL-REVIEW”）。字段缺省向后兼容。
    """
    feature = plan.get('existing_feature')
    if feature is None:
        return
    if not isinstance(feature, dict):
        errors.append('existing_feature 必须为对象')
        return
    satisfied = feature.get('satisfied')
    if not isinstance(satisfied, bool):
        errors.append('existing_feature.satisfied 必须为布尔值')
        satisfied = None  # 无法判定，跳过硬闸继续校验其它子字段
    if 'requirement_ids' in feature:
        ids = feature['requirement_ids']
        if (not isinstance(ids, list)
                or not all(isinstance(i, int) and i > 0 for i in ids)
                or len(ids) != len(set(ids))):
            errors.append('existing_feature.requirement_ids 若存在必须为不重复的正整数数组（已实现该功能的需求号）')
    if 'note' in feature:
        note = feature['note']
        if not isinstance(note, str) or not note.strip():
            errors.append('existing_feature.note 若存在必须为非空字符串')
    # 硬闸：功能已存在 → 不得 AUTO-ANA（须人工确认，非高风险故不加 STOP-AUTO）。
    if satisfied is True and plan['verdict'] == 'AUTO-ANA':
        errors.append(
            'existing_feature.satisfied=true 表示本次诉求已被既有能力满足（功能已存在），'
            '禁止 AUTO-ANA，须改判 MANUAL-REVIEW 并在分析者描述标注“该功能已存在”'
            '（附需求号）；属信息确认而非高风险，故不加 STOP-AUTO')


def validate_evidence_acquisition(plan, errors):
    """evidence-loop-v2 采集状态契约：如实记录四源采集，禁止覆盖不全却声明无命中/已查重。

    四态区分：已命中 / 完整查询后无命中 / 覆盖不完整 / 来源不可用。只有
    coverage_status=COMPLETE ∧ stop_reason=exhausted ∧ 未截断 的 query_status=NO_HIT
    才能支撑「无相似需求 / 无现有实现」等负面结论。v1 计划（evidence-loop-v1）不触发。
    """
    if plan.get('rules_source', {}).get('analysis') != 'evidence-loop-v2':
        return
    if plan['verdict'] not in ANALYSIS_VERDICTS:
        return
    acquisition = plan.get('evidence_acquisition')
    if not isinstance(acquisition, dict):
        errors.append(
            'evidence-loop-v2 分析计划必须含 evidence_acquisition 对象'
            '（记录 tfs_requirements/wiki/gitnexus/db_knowledge 四源采集状态）')
        return
    missing = [s for s in EVIDENCE_ACQUISITION_SOURCES
               if s not in acquisition or not isinstance(acquisition[s], dict)]
    if missing:
        errors.append(
            f'evidence_acquisition 缺少来源或格式错误：{missing}'
            '（四源均须出现并为对象；未触发的源用 query_status=SKIPPED）')
        return
    enums = (
        ('availability', EVIDENCE_AVAILABILITY),
        ('coverage_status', EVIDENCE_COVERAGE_STATUS),
        ('query_status', EVIDENCE_QUERY_STATUS),
        ('stop_reason', EVIDENCE_STOP_REASONS),
    )
    for source in EVIDENCE_ACQUISITION_SOURCES:
        entry = acquisition[source]
        for field, allowed in enums:
            if entry.get(field) not in allowed:
                errors.append(
                    f'evidence_acquisition.{source}.{field} 必须为 {sorted(allowed)} 之一')
        queries = entry.get('queries')
        if not isinstance(queries, list):
            errors.append(f'evidence_acquisition.{source}.queries 必须为数组')
            queries = []
        for i, query in enumerate(queries, start=1):
            if not isinstance(query, dict):
                errors.append(f'evidence_acquisition.{source}.queries[{i}] 必须为对象')
                continue
            terms = query.get('terms')
            terms_ok = (isinstance(terms, str) and terms.strip()) or (
                isinstance(terms, list) and terms
                and all(isinstance(t, str) and t.strip() for t in terms))
            if not terms_ok:
                errors.append(
                    f'evidence_acquisition.{source}.queries[{i}] 缺少非空 terms（查询词/条件）')
            if 'truncated' in query and not isinstance(query['truncated'], bool):
                errors.append(f'evidence_acquisition.{source}.queries[{i}].truncated 必须为布尔值')
        # 查询状态与查询记录的相干性：HIT/NO_HIT 必须留有查询；SKIPPED 查询可空。
        query_status = entry.get('query_status')
        if query_status in {'HIT', 'NO_HIT'} and not queries:
            errors.append(
                f'evidence_acquisition.{source}.query_status={query_status} 时 queries 不得为空')
        # 约束 3：COMPLETE 须 availability=READY ∧ stop_reason=exhausted ∧ 无截断。
        if entry.get('coverage_status') == 'COMPLETE':
            if entry.get('availability') != 'READY':
                errors.append(
                    f'evidence_acquisition.{source}.coverage_status=COMPLETE 要求 availability=READY')
            if entry.get('stop_reason') != 'exhausted':
                errors.append(
                    f'evidence_acquisition.{source}.coverage_status=COMPLETE 要求 stop_reason=exhausted')
            if any(isinstance(q, dict) and q.get('truncated') is True for q in queries):
                errors.append(
                    f'evidence_acquisition.{source}.coverage_status=COMPLETE 但存在 truncated=true 的查询；'
                    '截断时只能 PARTIAL/UNKNOWN')
        # 约束 4：覆盖不全不得声明无命中。
        if query_status == 'NO_HIT' and entry.get('coverage_status') != 'COMPLETE':
            errors.append(
                f'evidence_acquisition.{source}.query_status=NO_HIT 要求 coverage_status=COMPLETE'
                '（覆盖不全不得声明无命中）')
    # 约束 1：未执行查询不得写 dedup_ran=true。
    kb = plan.get('kb')
    if isinstance(kb, dict) and kb.get('dedup_ran') is True:
        tfs_queries = acquisition['tfs_requirements'].get('queries') or []
        if not tfs_queries:
            errors.append(
                'kb.dedup_ran=true 时 evidence_acquisition.tfs_requirements.queries 不得为空'
                '（未执行查询不得声明已查重）')
    # 约束 2：AUTO-ANA 须查重覆盖完整或源在范围外；覆盖不全不得自动放行。
    if plan['verdict'] == 'AUTO-ANA':
        coverage = acquisition['tfs_requirements'].get('coverage_status')
        if coverage not in {'COMPLETE', 'OUT_OF_SCOPE'}:
            errors.append(
                'AUTO-ANA 要求 evidence_acquisition.tfs_requirements.coverage_status ∈ '
                '{COMPLETE, OUT_OF_SCOPE}（查重覆盖不全不得自动放行）；覆盖不全改判 MANUAL-REVIEW')


def validate_attachments(plan, errors):
    """校验附件来源、逐文件终态和新运行时审计；旧计划可省略新增字段。"""
    attachments = plan.get('attachments')
    if attachments is None:
        return
    if not isinstance(attachments, dict):
        errors.append('attachments 必须为对象')
        return
    if not isinstance(attachments.get('ready'), bool):
        errors.append('attachments.ready 必须为布尔值')
    lists = {}
    for field in ('downloaded', 'parsed', 'skipped', 'errors'):
        value = attachments.get(field)
        if not isinstance(value, list):
            errors.append(f'attachments.{field} 必须为数组')
            value = []
        lists[field] = value

    names = {}
    for field, values in lists.items():
        current = []
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                errors.append(f'attachments.{field}[{index}] 必须为对象')
                continue
            name = item.get('name')
            if not isinstance(name, str) or not name.strip():
                errors.append(f'attachments.{field}[{index}].name 必须为非空字符串')
                continue
            current.append(name)
        if len(current) != len(set(current)):
            errors.append(f'attachments.{field}.name 不可重复')
        names[field] = current

    downloaded = set(names['downloaded'])
    outcomes = names['parsed'] + names['skipped'] + names['errors']
    if len(outcomes) != len(set(outcomes)):
        errors.append('每个附件只能出现在 parsed/skipped/errors 的一个终态中')
    if downloaded != set(outcomes):
        errors.append('attachments 的每个 downloaded 文件必须且只能有一个解析终态')
    expected_ready = not names['skipped'] and not names['errors'] and downloaded == set(names['parsed'])
    if isinstance(attachments.get('ready'), bool) and attachments['ready'] != expected_ready:
        errors.append(f'attachments.ready 必须与逐文件终态一致，期望为 {expected_ready}')

    preflight = attachments.get('preflight')
    enriched = preflight is not None
    for index, item in enumerate(lists['parsed']):
        if not isinstance(item, dict):
            continue
        if item.get('status') != 'parsed':
            errors.append(f'attachments.parsed[{index}].status 必须为 parsed')
        chain = item.get('converter_chain', item.get('converter'))
        if chain is not None and (not isinstance(chain, str)
                                  or chain not in ATTACHMENT_CONVERTERS):
            errors.append(
                f'attachments.parsed[{index}] 的转换链必须为 {sorted(ATTACHMENT_CONVERTERS)} 之一')
        if enriched:
            enriched_chain = item.get('converter_chain')
            if (not isinstance(enriched_chain, str)
                    or enriched_chain not in ATTACHMENT_CONVERTERS):
                errors.append(f'attachments.parsed[{index}].converter_chain 缺失或不合法')
            if item.get('runtime_mode') not in ATTACHMENT_RUNTIME_MODES:
                errors.append(f'attachments.parsed[{index}].runtime_mode 缺失或不合法')
            if not isinstance(item.get('tool_versions'), dict):
                errors.append(f'attachments.parsed[{index}].tool_versions 必须为对象')

    for field in ('skipped', 'errors'):
        for index, item in enumerate(lists[field]):
            if not isinstance(item, dict):
                continue
            if not isinstance(item.get('reason'), str) or not item['reason'].strip():
                errors.append(f'attachments.{field}[{index}].reason 必须为非空字符串')
            if enriched and not isinstance(item.get('blocking'), bool):
                errors.append(f'attachments.{field}[{index}].blocking 必须为布尔值')

    if not enriched:
        return
    if not isinstance(preflight, dict):
        errors.append('attachments.preflight 必须为对象')
        return
    for field in ('requested_formats', 'blocked_formats', 'install_required', 'warnings'):
        value = preflight.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append(f'attachments.preflight.{field} 必须为字符串数组')
    if not isinstance(preflight.get('capabilities'), dict):
        errors.append('attachments.preflight.capabilities 必须为对象')
    if preflight.get('runtime_mode') not in ATTACHMENT_RUNTIME_MODES:
        errors.append('attachments.preflight.runtime_mode 不合法')
    cache_key = preflight.get('runtime_cache_key')
    if cache_key is not None and (not isinstance(cache_key, str) or not cache_key.strip()):
        errors.append('attachments.preflight.runtime_cache_key 必须为非空字符串或 null')
    runtime_dir = preflight.get('runtime_dir')
    if runtime_dir is not None and (not isinstance(runtime_dir, str) or not runtime_dir.strip()):
        errors.append('attachments.preflight.runtime_dir 必须为非空字符串或 null')
    installations = preflight.get('installations')
    if not isinstance(installations, list):
        errors.append('attachments.preflight.installations 必须为数组')
        return
    for index, item in enumerate(installations):
        if not isinstance(item, dict):
            errors.append(f'attachments.preflight.installations[{index}] 必须为对象')
            continue
        for field in ('group', 'manager', 'started_at', 'finished_at', 'status'):
            if not isinstance(item.get(field), str) or not item[field].strip():
                errors.append(f'attachments.preflight.installations[{index}].{field} 必须为非空字符串')
        if item.get('status') not in {'installed', 'failed'}:
            errors.append(f'attachments.preflight.installations[{index}].status 不合法')
        if not isinstance(item.get('packages'), list) or not item.get('packages') or not all(
                isinstance(package, str) and package.strip() for package in item.get('packages', [])):
            errors.append(f'attachments.preflight.installations[{index}].packages 必须为非空字符串数组')
        forbidden = set(item) & {'command', 'env', 'environment'}
        if forbidden:
            errors.append(
                f'attachments.preflight.installations[{index}] 不得记录命令或环境：{sorted(forbidden)}')
        if re.search(r'(?i)\b(?:TFS_PAT|PAT|TOKEN|PASSWORD)=\S+', str(item.get('error', ''))):
            errors.append(f'attachments.preflight.installations[{index}].error 含未脱敏敏感值')


def is_confirmed_evidence_ref(plan, value):
    """确认 KB/wiki/需求历史索引引用的 finding 已被证实。"""
    if not isinstance(value, str) or not EVIDENCE_REF_RE.fullmatch(value):
        return False
    if value.startswith('kb:'):
        source, index = 'kb', int(value[3:])
    elif value.startswith('wiki:'):
        source, index = 'wiki', int(value[5:])
    elif value.startswith('req:'):
        source, index = 'tfs_requirements', int(value[4:])
    else:
        return False
    findings = plan.get(source, {}).get('findings') if isinstance(plan.get(source), dict) else None
    if not isinstance(findings, list) or index >= len(findings) or not isinstance(findings[index], dict):
        return False
    if source == 'kb':
        return findings[index].get('state') == '已证实'
    if source == 'wiki':
        return findings[index].get('state') in {'已证实', 'wiki-确认'}
    return (findings[index].get('state') == '已证实'
            and findings[index].get('source_tool') in TFS_REQUIREMENTS_CONFIRMED_TOOLS)


def validate_qc_evidence_resolution(plan, errors):
    """校验初判 NEED-REVIEW 被 KB/wiki 已证实证据逐项消除的审计记录。"""
    resolution = plan.get('qc_evidence_resolution')
    if resolution is None:
        return
    if plan['version'] != PLAN_VERSION or plan['verdict'] not in ANALYSIS_VERDICTS:
        errors.append('qc_evidence_resolution 仅允许出现在 v2 分析计划')
        return
    if not isinstance(resolution, dict):
        errors.append('qc_evidence_resolution 必须为对象')
        return
    if resolution.get('initial_verdict') != 'NEED-REVIEW':
        errors.append('qc_evidence_resolution.initial_verdict 必须为 NEED-REVIEW')
    if resolution.get('post_evidence_verdict') != 'PASS':
        errors.append('qc_evidence_resolution.post_evidence_verdict 必须为 PASS')
    items = resolution.get('items')
    if not isinstance(items, list) or not items:
        errors.append('qc_evidence_resolution.items 必须为非空数组')
        return
    ids = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append(f'qc_evidence_resolution.items[{index}] 必须为对象')
            continue
        missing = [field for field in QC_EVIDENCE_RESOLUTION_FIELDS if field not in item]
        if missing:
            errors.append(f'qc_evidence_resolution.items[{index}] 缺少字段：{missing}')
            continue
        item_id = item['id']
        if not isinstance(item_id, str) or not ANALYSIS_GAP_ID_RE.fullmatch(item_id):
            errors.append(f'qc_evidence_resolution.items[{index}].id 必须为稳定的字母开头标识')
        elif item_id in ids:
            errors.append('qc_evidence_resolution.items.id 不可重复')
        else:
            ids.add(item_id)
        for field in ('initial_gap', 'resolution'):
            if not isinstance(item[field], str) or not item[field].strip():
                errors.append(f'qc_evidence_resolution.items[{index}].{field} 必须为非空字符串')
        refs = item['evidence_refs']
        if (not isinstance(refs, list) or not refs
                or not all(isinstance(value, str) for value in refs)):
            errors.append(f'qc_evidence_resolution.items[{index}].evidence_refs 必须为非空数组')
            continue
        if len(refs) != len(set(refs)):
            errors.append(f'qc_evidence_resolution.items[{index}].evidence_refs 不可重复')
        for value in refs:
            if (isinstance(value, str) and value.startswith('req:')) or not is_confirmed_evidence_ref(plan, value):
                errors.append(
                    f'qc_evidence_resolution.items[{index}] 的 {value} 必须指向 kb/wiki.findings 的已证实项')


def validate_evidence_refs(plan, errors):
    """校验 v2 闭环结论到工作项、KB、wiki 或需求历史 finding 的内部追溯。"""
    if plan['version'] != PLAN_VERSION or plan['verdict'] not in ANALYSIS_VERDICTS:
        return
    refs = plan.get('evidence_refs')
    expected = set(ITERATION_ANALYSIS_CLOSURE_REQUIREMENTS)
    if not isinstance(refs, dict) or set(refs) != expected:
        errors.append(f'evidence_refs 必须精确覆盖闭环章节：{sorted(expected)}')
        return

    for heading, values in refs.items():
        if (not isinstance(values, list) or not values
                or not all(isinstance(value, str) and EVIDENCE_REF_RE.fullmatch(value) for value in values)):
            errors.append(f'evidence_refs.{heading} 必须为非空且合法的证据引用数组')
            continue
        if len(values) != len(set(values)):
            errors.append(f'evidence_refs.{heading} 不可重复')
        for value in values:
            if value.startswith('kb:') and not is_confirmed_evidence_ref(plan, value):
                errors.append(f'evidence_refs.{heading} 引用的 {value} 必须指向 kb.findings 的已证实项')
            if value.startswith('wiki:') and not is_confirmed_evidence_ref(plan, value):
                errors.append(f'evidence_refs.{heading} 引用的 {value} 必须指向 wiki.findings 的已证实项')
            if value.startswith('req:') and not is_confirmed_evidence_ref(plan, value):
                errors.append(
                    f'evidence_refs.{heading} 引用的 {value} 必须指向 '
                    'tfs_requirements.findings 的已证实项')

    if plan['verdict'] == 'AUTO-ANA':
        for heading in ('现状基线', '差异与范围', '方案取舍'):
            values = refs.get(heading, [])
            if not any(isinstance(value, str) and value.startswith('kb:') for value in values):
                errors.append(f'AUTO-ANA 的 evidence_refs.{heading} 必须包含 kb: 已证实证据引用')


def validate_analysis_description(plan, plan_path, check_files, errors):
    """校验分析者描述的受控类别及其 Markdown 业务维度。"""
    metadata = plan.get('analysis_description')
    if not isinstance(metadata, dict):
        errors.append('分析计划必须含 analysis_description 对象')
        return
    categories = metadata.get('categories')
    if not isinstance(categories, list) or not categories or not all(isinstance(value, str) for value in categories):
        errors.append('analysis_description.categories 必须为非空字符串数组')
        return
    if len(categories) != len(set(categories)):
        errors.append('analysis_description.categories 不可重复')
    invalid = sorted(set(categories) - set(ANALYSIS_DESCRIPTION_REQUIREMENTS))
    if invalid:
        errors.append(f'analysis_description.categories 含不支持类别：{invalid}')
    profile = plan.get('analysis_profile')
    if profile is not None:
        if profile not in ANALYSIS_DESCRIPTION_PROFILES:
            errors.append(f'analysis_profile 仅允许：{sorted(ANALYSIS_DESCRIPTION_PROFILES)}')
        elif profile == 'concise-v3' and plan.get('version') != PLAN_VERSION:
            errors.append(f'concise-v3 仅允许 version={PLAN_VERSION} 的新分析计划')
        elif (profile in {'concise-v1', 'concise-v2'}
              and (len(categories) != 1
                   or categories[0] not in CONCISE_ANALYSIS_DESCRIPTION_REQUIREMENTS)):
            errors.append(f'{profile} 仅允许单一 existing-ui-simple / print-adjustment / data-management 类别')
    if not check_files or invalid:
        return

    change = next((item for item in plan['artifacts']
                   if isinstance(item, dict) and item.get('kind') == 'change-plan'), None)
    if not isinstance(change, dict) or not isinstance(change.get('path'), str):
        return
    if not is_local_artifact_name(change['path']):
        return
    path = artifact_path(plan_path, change)
    if not os.path.isfile(path):
        return
    source = os.path.basename(path)
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    review_content = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)
    validate_iteration_analysis_closure(plan, review_content, errors)
    if '## 三、分析者描述' not in review_content:
        errors.append('需求分析报告缺少“## 三、分析者描述”区')
        return source
    try:
        analysis_section = re.sub(
            r'<!--.*?-->', '', extract_analysis_description_markdown(content), flags=re.DOTALL)
    except ValueError as exc:
        errors.append(str(exc))
        return source
    if plan['version'] == PLAN_VERSION and profile != 'concise-v3':
        for label in ANALYSIS_DECISION_SUMMARY_REQUIREMENTS:
            matches = re.findall(
                rf'^\s*-\s*\*\*{re.escape(label)}\*\*：\s*\S.*$',
                analysis_section, re.MULTILINE)
            if len(matches) != 1:
                errors.append(f'分析者描述必须且只能包含一个非空“{label}”决策摘要')
    elif profile == 'concise-v3':
        for label in ANALYSIS_DECISION_SUMMARY_REQUIREMENTS:
            if re.search(rf'^\s*-\s*\*\*{re.escape(label)}\*\*：', analysis_section, re.MULTILINE):
                errors.append(f'concise-v3 分析者描述不得包含“{label}”决策摘要')

    declared_match = re.search(r'^- \*\*需求类别\*\*：(?P<value>.+)$', analysis_section, re.MULTILINE)
    if profile == 'concise-v3':
        if declared_match:
            errors.append('concise-v3 分析者描述不得包含“需求类别”正文行')
    else:
        declared = set(re.findall(r'`([a-z-]+)`', declared_match.group('value'))) if declared_match else set()
        if declared != set(categories):
            errors.append('需求分析报告“需求类别”必须与 analysis_description.categories 完全一致')

    path_values = re.findall(
        r'^\s*-\s*\*\*路径\*\*：\s*(\S.*?)\s*$', analysis_section, re.MULTILINE)
    menu_path_values = re.findall(
        r'^\s*-\s*\*\*菜单路径\*\*：\s*(\S.*?)\s*$', analysis_section, re.MULTILINE)
    if profile == 'concise-v3':
        if path_values:
            errors.append('concise-v3 分析者描述不得包含固定“路径”行')
        if len(menu_path_values) != 1:
            errors.append('concise-v3 分析者描述必须且只能包含一个非空“菜单路径”')
        numbered_lines = re.findall(
            r'^\s*([1-9][0-9]*)\.\s+(\S.*?)\s*$', analysis_section, re.MULTILINE)
        numbers = [int(number) for number, _ in numbered_lines]
        if numbers and (len(numbers) < 2 or len(numbers) > 8
                        or numbers != list(range(1, len(numbers) + 1))):
            errors.append('concise-v3 多条变更内容必须使用从 1 开始连续的 2–8 项有序编号')
        for number, value in numbered_lines:
            if not re.fullmatch(r'\*\*(.+?)\*\*：\s*\S.*', value):
                errors.append(
                    f'concise-v3 编号变更项 {number} 必须按“{number}. **改动点**：内容”填写')
    elif len(path_values) != 1:
        errors.append('分析者描述必须且只能包含一个非空“路径”')
    elif not ANALYSIS_PATH_VALUE_RE.fullmatch(path_values[0]):
        errors.append('分析者描述“路径”必须按“菜单路径：...；操作路径：...”填写')

    sections = {}
    category_content = analysis_section if profile == 'concise-v3' else review_content
    headers = list(re.finditer(r'^### ([a-z-]+)(?:\s|（|$)', category_content, re.MULTILINE))
    if profile == 'concise-v3':
        first_header = re.search(r'^### [a-z-]+(?:\s|（|$)', analysis_section, re.MULTILINE)
        preamble = analysis_section[:first_header.start()] if first_header else analysis_section
        preamble_labels = re.findall(
            r'^\s*-\s*\*\*(.+?)\*\*：', preamble, re.MULTILINE)
        if any(label != '菜单路径' for label in preamble_labels):
            errors.append('concise-v3 除“菜单路径”外的业务维度必须写在对应类别标题下')
        if re.search(r'^\s*[1-9][0-9]*\.\s+', preamble, re.MULTILINE):
            errors.append('concise-v3 编号变更项必须写在对应类别标题下')
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(category_content)
        sections[header.group(1)] = category_content[header.end():end]
    if set(sections) != set(categories):
        errors.append('需求分析报告三级标题类别必须与 analysis_description.categories 完全一致')
    for category in categories:
        section = sections.get(category, '')
        if profile == 'concise-v3':
            requirements = CONCISE_V3_ANALYSIS_DESCRIPTION_REQUIREMENTS[category]
        elif profile == 'concise-v1' and category in CONCISE_ANALYSIS_DESCRIPTION_REQUIREMENTS:
            requirements = CONCISE_ANALYSIS_DESCRIPTION_REQUIREMENTS[category]
        elif profile == 'concise-v2' and category in CONCISE_V2_ANALYSIS_DESCRIPTION_REQUIREMENTS:
            requirements = CONCISE_V2_ANALYSIS_DESCRIPTION_REQUIREMENTS[category]
        else:
            requirements = ANALYSIS_DESCRIPTION_REQUIREMENTS[category]
        if profile == 'concise-v3':
            labels = re.findall(r'^[ \t]*-[ \t]*\*\*(.+?)\*\*：', section, re.MULTILINE)
            extras = sorted(set(labels) - set(requirements))
            if extras:
                errors.append(f'分析类别 {category} 含 concise-v3 非法维度：{extras}')
        for label in requirements:
            field = re.compile(rf'^[ \t]*-[ \t]*\*\*{re.escape(label)}\*\*：[ \t]*\S+', re.MULTILINE)
            matches = field.findall(section)
            if not matches:
                errors.append(f'分析类别 {category} 缺少非空维度“{label}”')
            elif profile == 'concise-v3' and len(matches) != 1:
                errors.append(f'分析类别 {category} 必须且只能包含一个“{label}”维度')

    if re.search(r'<[^>\r\n]+>', review_content) or re.search(r'\bTODO\b', review_content, re.IGNORECASE):
        errors.append('需求分析报告不得包含模板占位符或 TODO')
    for phrase in ANALYSIS_BANNED_PHRASES:
        if phrase in review_content:
            errors.append(f'需求分析报告包含空泛描述：{phrase}')

    return source


def validate_analysis_traceability(plan, content, errors):
    """校验 v2 业务范围、方案、验收和结论状态的一对一追踪。"""
    source = plan.get('rules_source')
    if (not isinstance(source, dict)
            or source.get('analysis') not in {'evidence-loop-v1', 'evidence-loop-v2'}):
        return

    matches = list(re.finditer(rf'^##\s+{re.escape(TRACEABILITY_HEADING)}\s*$', content, re.MULTILINE))
    if len(matches) != 1:
        errors.append(f'需求分析报告必须且只能包含一个“## {TRACEABILITY_HEADING}”区')
        return
    start = matches[0].end()
    following = re.search(r'^##\s+', content[start:], re.MULTILINE)
    section = content[start:start + following.start()] if following else content[start:]
    rows = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith('|'):
            continue
        cells = [cell.strip() for cell in stripped.strip('|').split('|')]
        if all(re.fullmatch(r':?-{3,}:?', cell or '') for cell in cells):
            continue
        rows.append(cells)
    if not rows or tuple(rows[0]) != TRACEABILITY_HEADERS:
        errors.append(f'“{TRACEABILITY_HEADING}”必须使用表头：' + ' | '.join(TRACEABILITY_HEADERS))
        return
    data_rows = rows[1:]
    if not data_rows:
        errors.append(f'“{TRACEABILITY_HEADING}”至少需要一条追踪项')
        return

    gap_ids = {gap.get('id') for gap in plan.get('analysis_gaps', []) if isinstance(gap, dict)}
    pending_gap_ids = []
    ids = set()
    for index, row in enumerate(data_rows, start=1):
        if len(row) != len(TRACEABILITY_HEADERS) or not all(row):
            errors.append(f'“{TRACEABILITY_HEADING}”第 {index} 行的 6 个单元格均须非空')
            continue
        item_id, _, _, _, conclusion_status, basis = row
        if not ANALYSIS_GAP_ID_RE.fullmatch(item_id):
            errors.append(f'“{TRACEABILITY_HEADING}”第 {index} 行 ID 必须是稳定的字母开头标识')
        elif item_id in ids:
            errors.append(f'“{TRACEABILITY_HEADING}”ID 不可重复：{item_id}')
        else:
            ids.add(item_id)
        if conclusion_status not in TRACEABILITY_STATUSES:
            errors.append(f'“{TRACEABILITY_HEADING}”第 {index} 行结论状态必须为 {sorted(TRACEABILITY_STATUSES)} 之一')
            continue
        if conclusion_status == '合理假设' and not basis.startswith('呈现类默认：'):
            errors.append(f'“{TRACEABILITY_HEADING}”第 {index} 行合理假设的依据必须以“呈现类默认：”说明边界')
        if conclusion_status == '待业务确认':
            match = re.fullmatch(r'analysis-gap:([A-Za-z][A-Za-z0-9_-]{0,63})', basis)
            if not match or match.group(1) not in gap_ids:
                errors.append(f'“{TRACEABILITY_HEADING}”第 {index} 行待业务确认必须关联既有 analysis-gap:<id>')
            else:
                pending_gap_ids.append(match.group(1))
        elif basis.startswith('analysis-gap:'):
            errors.append(f'“{TRACEABILITY_HEADING}”第 {index} 行只有待业务确认可关联 analysis-gap')

    if len(pending_gap_ids) != len(set(pending_gap_ids)):
        errors.append(f'“{TRACEABILITY_HEADING}”中的 analysis-gap 引用不可重复')
    if set(pending_gap_ids) != gap_ids:
        errors.append(f'“{TRACEABILITY_HEADING}”必须与 analysis_gaps 一对一关联')


def validate_iteration_analysis_closure(plan, content, errors):
    """校验 evidence-loop 需求分析报告的业务分析闭环。"""
    source = plan.get('rules_source')
    if (not isinstance(source, dict)
            or source.get('analysis') not in {'evidence-loop-v1', 'evidence-loop-v2'}):
        return

    for heading, labels in ITERATION_ANALYSIS_CLOSURE_REQUIREMENTS.items():
        matches = list(re.finditer(rf'^###\s+{re.escape(heading)}\s*$', content, re.MULTILINE))
        if len(matches) != 1:
            errors.append(f'迭代分析闭环必须且只能包含一个“### {heading}”章节')
            continue
        start = matches[0].end()
        next_heading = re.search(r'^#{1,3}\s+', content[start:], re.MULTILINE)
        section = content[start:start + next_heading.start()] if next_heading else content[start:]
        for label in labels:
            field = re.compile(rf'^[ \t]*-[ \t]*\*\*{re.escape(label)}\*\*：[ \t]*\S+', re.MULTILINE)
            if not field.search(section):
                errors.append(f'迭代分析闭环“{heading}”缺少非空字段“{label}”')

    closure_matches = list(re.finditer(r'^##\s+二、迭代分析闭环\s*$', content, re.MULTILINE))
    if len(closure_matches) != 1:
        errors.append('需求分析报告必须且只能包含一个“## 二、迭代分析闭环”区')
    closure_start = closure_matches[0] if closure_matches else None
    if not closure_start:
        return
    closure_end = re.search(r'^##\s+', content[closure_start.end():], re.MULTILINE) if closure_start else None
    closure = (content[closure_start.end():closure_start.end() + closure_end.start()]
               if closure_start and closure_end else content[closure_start.end():] if closure_start else '')
    for phrase in ITERATION_ANALYSIS_CLOSURE_BANNED_PHRASES:
        if phrase in closure:
            errors.append(f'迭代分析闭环不得以“{phrase}”替代业务结论')


def validate_manual_followup(plan, plan_path, gaps, check_files, errors):
    """校验 Markdown 待确认清单只覆盖 analysis_gaps，不重复自动标签。"""
    artifacts = [artifact for artifact in plan['artifacts']
                 if isinstance(artifact, dict) and artifact.get('kind') == 'manual-followup']
    if len(artifacts) > 1:
        errors.append('analysis 计划最多只能有一个 manual-followup 附件')
        return
    if not gaps or not artifacts or not check_files:
        return
    artifact = artifacts[0]
    if not isinstance(artifact.get('path'), str):
        return
    if not is_local_artifact_name(artifact['path']):
        return
    path = artifact_path(plan_path, artifact)
    if not os.path.isfile(path):
        return
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    if '## 需要确认的需求分析信息' not in content:
        errors.append('待确认清单缺少“需要确认的需求分析信息”章节')
    if 'PM-AI-' in content:
        errors.append('待确认清单不得包含自动标签')
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        marker = f'<!-- analysis-gap:{gap.get("id", "")} -->'
        if marker not in content:
            errors.append(f'待确认清单缺少 analysis_gaps 项：{gap.get("id", "")}')
            continue
        section = content.split(marker, 1)[1]
        section = re.split(r'^###\s+', section, maxsplit=1, flags=re.MULTILINE)[0]
        for label in ('缺失信息', '对分析/验收的影响', '需确认的问题', '候选口径', '允许自由补充'):
            if not re.search(rf'^[ \t]*-[ \t]*\*\*{re.escape(label)}\*\*：[ \t]*\S+',
                             section, re.MULTILINE):
                errors.append(f'待确认清单 {gap.get("id", "")} 缺少非空字段“{label}”')


def _validation_warning(code, field, message):
    return {'code': code, 'field': field, 'message': message}


def _normalize_plan_for_validation(plan):
    """只归一化内存副本；原计划文件和冻结摘要保持不变。"""
    normalized = copy.deepcopy(plan)
    warnings = []
    normalizations = []
    if not isinstance(normalized, dict):
        return normalized, warnings, normalizations
    attachments = normalized.get('attachments')
    parsed = attachments.get('parsed') if isinstance(attachments, dict) else None
    if not isinstance(parsed, list):
        return normalized, warnings, normalizations
    sequence_map = {
        ('libreoffice', 'markitdown'): 'libreoffice+markitdown',
        ('libreoffice', 'builtin-fallback'): 'libreoffice+builtin-fallback',
    }
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        chain = item.get('converter_chain')
        if not isinstance(chain, list):
            continue
        canonical = None
        if len(chain) == 1 and chain[0] in ATTACHMENT_CONVERTERS:
            canonical = chain[0]
        elif tuple(chain) in sequence_map:
            canonical = sequence_map[tuple(chain)]
        if canonical is None:
            continue
        field = f'attachments.parsed[{index}].converter_chain'
        item['converter_chain'] = canonical
        source_shape = '单元素数组' if len(chain) == 1 else '组合数组'
        warnings.append(_validation_warning(
            'ATTACHMENT_CHAIN_NORMALIZED', field,
            f'已从{source_shape}归一化为 {canonical}'))
        normalizations.append({'field': field, 'before': chain, 'after': canonical})
    return normalized, warnings, normalizations


def _warning_for_nonblocking_error(plan, error):
    verdict = plan.get('verdict') if isinstance(plan, dict) else None
    if ('attachments.parsed[' in error
            and ('转换链' in error or 'converter_chain' in error)):
        match = re.search(r'attachments\.parsed\[([0-9]+)\]', error)
        field = (f'attachments.parsed[{match.group(1)}].converter_chain'
                 if match else 'attachments.parsed[].converter_chain')
        return _validation_warning('ATTACHMENT_CHAIN_INVALID', field, error)
    if (verdict != 'AUTO-ANA'
            and error == 'kb.source_required=true 且源码 MCP 就绪时必须留下源码 finding'):
        return _validation_warning(
            'SOURCE_FINDING_MISSING', 'kb.findings',
            '非自动放行终局缺少非决定性源码 finding；保留为证据审计告警')
    if (verdict != 'AUTO-ANA'
            and error == 'tfs_requirements.ready=true 时 tools_used 必须包含 get_requirements_summary'):
        evidence = plan.get('tfs_requirements') if isinstance(plan, dict) else None
        if isinstance(evidence, dict) and evidence.get('findings'):
            return _validation_warning(
                'REQUIREMENTS_PROBE_NOT_RECORDED', 'tfs_requirements.tools_used',
                '需求历史已有 finding，但 tools_used 未记录探活工具；不伪造调用记录')
    if verdict != 'AUTO-ANA' and re.fullmatch(
            r'源码 MCP finding kb\.findings\[[0-9]+\]\.source_tool 必须出现在 tools_used',
            error):
        return _validation_warning(
            'TOOL_USAGE_NOT_RECORDED', 'kb.tools_used',
            '源码 finding 已存在，但 tools_used 未记录对应探活工具；不伪造调用记录')
    if (verdict != 'AUTO-ANA'
            and error == 'kb.source_required=true 且源码 MCP 就绪时必须实际调用 '
                         'search_source 或 search_symbol'
            and any(isinstance(finding, dict)
                    and finding.get('source_tool') in SOURCE_CODE_TOOLS
                    for finding in (plan.get('kb') or {}).get('findings') or [])):
        return _validation_warning(
            'TOOL_USAGE_NOT_RECORDED', 'kb.tools_used',
            '源码 finding 已存在，但 tools_used 未记录对应探活工具；不伪造调用记录')
    if verdict != 'AUTO-ANA' and re.fullmatch(
            r'tfs_requirements\.findings\[[0-9]+\]\.source_tool 必须出现在 tools_used',
            error):
        return _validation_warning(
            'TOOL_USAGE_NOT_RECORDED', 'tfs_requirements.tools_used',
            '需求历史 finding 已存在，但 tools_used 未记录对应工具；不伪造调用记录')
    return None


def _finalize_validation(plan, raw, warnings=None, normalizations=None):
    blocking = []
    downgraded = list(warnings or [])
    for error in raw.get('errors') or []:
        warning = _warning_for_nonblocking_error(plan, error)
        if warning:
            downgraded.append(warning)
        else:
            blocking.append(error)
    for warning in raw.get('warnings') or []:
        if warning not in downgraded:
            downgraded.append(warning)
    unique_warnings = []
    warning_keys = set()
    for warning in downgraded:
        key = (warning.get('code'), warning.get('field'))
        if key in warning_keys:
            continue
        warning_keys.add(key)
        unique_warnings.append(warning)
    downgraded = unique_warnings
    merged_normalizations = list(raw.get('normalizations') or [])
    for normalization in normalizations or []:
        if normalization not in merged_normalizations:
            merged_normalizations.append(normalization)
    classified = raw.get('errors_by_class') or {
        'analysis': list(blocking), 'source': [], 'execution': []}
    blocking_set = set(blocking)
    blocking_by_class = {
        category: [error for error in classified.get(category, []) if error in blocking_set]
        for category in ('analysis', 'source', 'execution')
    }
    if blocking and not any(blocking_by_class.values()):
        blocking_by_class['analysis'] = list(blocking)
    validation = {
        'decision': 'REJECT' if blocking else 'PASS',
        'blocking_errors': blocking_by_class,
        'warnings': downgraded,
        'normalizations': merged_normalizations,
    }
    return {
        **raw,
        'ok': not blocking,
        'errors': blocking,
        'errors_by_class': blocking_by_class,
        'warnings': downgraded,
        'normalizations': merged_normalizations,
        'validation': validation,
    }


def validate_plan(plan, plan_path, check_files=True):
    normalized, warnings, normalizations = _normalize_plan_for_validation(plan)
    raw = _validate_plan_core(normalized, plan_path, check_files)
    return _finalize_validation(normalized, raw, warnings, normalizations)


def _validate_plan_core(plan, plan_path, check_files=True):
    if isinstance(plan, dict) and plan.get('plan_profile') == RUN_BOUND_PROFILE:
        expanded, meta, ref_errors = materialize_run_bound(plan, plan_path)
        if ref_errors:
            return result(False, errors=ref_errors,
                          errors_by_class=_classify_ref_errors(ref_errors))
        validation = validate_plan(expanded, plan_path, check_files)
        validation['plan_profile'] = RUN_BOUND_PROFILE
        validation['validation_sources'] = {
            **(validation.get('validation_sources') or {}),
            'run_receipt': os.path.basename(meta['receipt_path']),
        }
        validation['errors_by_class'] = {
            'analysis': list(validation.get('errors') or []),
            'source': [],
            'execution': [],
        }
        return validation
    if isinstance(plan, dict) and plan.get('plan_profile') == ANALYSIS_REF_PROFILE:
        expanded, meta, ref_errors = materialize_analysis_ref(plan, plan_path)
        if ref_errors:
            return result(False, errors=ref_errors,
                          errors_by_class=_classify_ref_errors(ref_errors))
        validation = _validate_materialized_plan(expanded, meta, check_files)
        validation['plan_profile'] = ANALYSIS_REF_PROFILE
        validation['validation_sources'] = {
            **(validation.get('validation_sources') or {}),
            'run_receipt': os.path.basename(meta['receipt_path']),
            'analysis_result': os.path.basename(meta['snapshot_path']),
        }
        validation['errors_by_class'] = {
            'analysis': list(validation.get('errors') or []),
            'source': [],
            'execution': [],
        }
        return validation
    errors = []
    required = ('version', 'run_id', 'skill', 'work_item_id', 'expected_rev', 'expected_state',
                'verdict', 'rules_source', 'artifacts')
    for key in required:
        if key not in plan:
            errors.append(f'缺少字段 {key}')
    if errors:
        return result(False, errors=errors)

    if plan['version'] not in SUPPORTED_PLAN_VERSIONS:
        errors.append(f"version 必须为 {sorted(SUPPORTED_PLAN_VERSIONS)} 之一")
    if plan['skill'] not in ('auto-req-analysis', 'auto-req-qc'):
        errors.append('skill 只能是 auto-req-analysis（合并后新 run）或 auto-req-qc（向后兼容历史质控计划）')
    if not isinstance(plan['work_item_id'], int) or plan['work_item_id'] <= 0:
        errors.append('work_item_id 必须是正整数')
    if not isinstance(plan['expected_rev'], int) or plan['expected_rev'] < 1:
        errors.append('expected_rev 必须是正整数')
    if not isinstance(plan['expected_state'], str) or not plan['expected_state'].strip():
        errors.append('expected_state 必须是非空字符串')
    if not isinstance(plan['run_id'], str) or not RUN_ID_RE.fullmatch(plan['run_id']):
        errors.append('run_id 必须为 8-80 位字母、数字、- 或 _')
    if not isinstance(plan['artifacts'], list):
        errors.append('artifacts 必须是数组')
    if errors:
        return result(False, errors=errors)

    validate_rules_source(plan, errors)
    validate_confirmation_policy(plan, errors)
    validate_knowledge_route(plan, errors)

    try:
        expected_tags, expected_state_to, required_kinds = expected_for(plan)
    except ValueError as exc:
        return result(False, errors=[str(exc)])
    raw_tags = plan.get('tags')
    if (not isinstance(raw_tags, list) or not all(isinstance(tag, str) and tag.strip() for tag in raw_tags)
            or len(raw_tags) != len(set(raw_tags))):
        errors.append('tags 必须为不重复的非空字符串数组')
        raw_tags = []
    tags = set(raw_tags)
    if tags != expected_tags:
        errors.append(f'标签必须精确为 {sorted(expected_tags)}，当前为 {sorted(tags)}')
    if plan.get('state_to') != expected_state_to:
        errors.append(f'state_to 必须为 {expected_state_to!r}')

    auto_scopes = plan.get('auto_scopes')
    if plan['verdict'] == 'AUTO-ANA':
        if not isinstance(auto_scopes, list) or not auto_scopes or not set(auto_scopes) <= AUTO_SCOPES:
            errors.append(f'AUTO-ANA 的 auto_scopes 必须为非空白名单子集：{sorted(AUTO_SCOPES)}')
    elif auto_scopes not in (None, []):
        errors.append('仅 AUTO-ANA 计划可以声明 auto_scopes')

    if plan['verdict'] in ROUTING_VERDICTS:
        if not isinstance(plan.get('skip_reason'), str) or not plan['skip_reason'].strip():
            errors.append('SKIP-ANALYSIS 计划必须含非空 skip_reason')
        for field in ('checklist', 'analysis_description', 'analysis_profile', 'analysis_gaps',
                      'evidence_refs', 'evidence_gaps', 'auto_scopes', 'assignee_to',
                      'tfs_requirements', 'existing_feature', 'ui_baseline',
                      'implementation_impacts', 'general_rule_coverage',
                      'business_rule_coverage',
                      'evidence_acquisition'):
            if field in plan and plan[field] not in (None, [], {}):
                errors.append(f'SKIP-ANALYSIS 计划不得声明 {field}')

    # assignee_to：AUTO-ANA 自动指派（System.AssignedTo）。仅 AUTO-ANA 允许非空；
    # 匹配不出唯一人时省略该字段（指派留空、在描述/审计说明候选，不阻断流转）。
    # 写入格式必须可解析为 WINNING\账号（运行时优先取 Winning.Dev.Leader，本字段为回退；
    # bare display name 写入必报「未知标识」HTTP400，故此处前置拦截）。
    assignee_to = plan.get('assignee_to')
    if assignee_to is not None:
        if not isinstance(assignee_to, str) or not assignee_to.strip():
            errors.append('assignee_to 若存在必须是非空字符串（System.AssignedTo 写入值）')
        elif plan['verdict'] != 'AUTO-ANA':
            errors.append('assignee_to 仅允许出现在 AUTO-ANA 计划（其它终局不自动指派）')
        elif not resolve_assignee_to_winning('', assignee_to):
            errors.append('assignee_to 必须可解析为 WINNING\\账号（如 WINNING\\zhang_dong，'
                          '或含 <WINNING\\account> / (account) 形态）；bare display name 不可用')

    # AUTO-ANA 是无人工兜底的自动放行路径：硬要求 KB 就绪、相似实现查重实跑，
    # 且优化类类别须在 kb.findings 含至少一条 state=已证实 的现有实现锚点。
    # 任一不满足 → 改判 MANUAL-REVIEW（safe-side，属信息缺口而非高风险，故不加 STOP-AUTO）。
    # 注意：kb 仅对 AUTO-ANA 必填；MANUAL-REVIEW 路径维持 KB 缺位不升级原则。
    if plan['verdict'] == 'AUTO-ANA':
        kb = plan.get('kb')
        if not isinstance(kb, dict) or kb.get('ready') is not True or kb.get('dedup_ran') is not True:
            errors.append(
                'AUTO-ANA 要求 kb.ready=true 且 kb.dedup_ran=true（相似实现查重必须实际执行）；'
                '缺失或未执行时改判 MANUAL-REVIEW')
        else:
            categories = plan.get('analysis_description', {}).get('categories') or []
            hit = set(categories) & OPTIMIZATION_CATEGORIES
            if hit and not any(isinstance(f, dict) and f.get('state') == '已证实'
                               for f in kb.get('findings') or []):
                errors.append(
                    f'AUTO-ANA 的优化类类别 {sorted(hit)} 须在 kb.findings 含至少一条 '
                    'state=已证实 的现有实现锚点；缺失时改判 MANUAL-REVIEW '
                    '并在 evidence_gaps 记录“现有实现定位”缺口')
            if kb.get('source_required') is True:
                source_confirmed = any(
                    isinstance(finding, dict)
                    and finding.get('source_tool') in SOURCE_CODE_TOOLS
                    and finding.get('source_type') == 'code'
                    and finding.get('state') == '已证实'
                    for finding in kb.get('findings') or [])
                if kb.get('source_ready') is not True or not source_confirmed:
                    errors.append(
                        'AUTO-ANA 在 kb.source_required=true 时要求 source_ready=true 且至少一条 '
                        'search_source/search_symbol 的 state=已证实 源码 finding；'
                        '否则改判 MANUAL-REVIEW 并记录源码核验缺口')

    validate_tfs_requirements(plan, errors)
    validate_attachments(plan, errors)
    validate_existing_feature(plan, errors)

    analysis_gaps = []
    analysis_source = None
    if plan['verdict'] in ANALYSIS_VERDICTS:
        analysis_source = validate_analysis_description(plan, plan_path, check_files, errors)
        analysis_gaps = validate_analysis_gaps(plan, errors)
        validate_evidence_gaps(plan, errors)
        validate_general_rule_coverage(plan, errors)
        validate_implementation_evidence(plan, errors)
        validate_evidence_refs(plan, errors)
        validate_ui_baseline(plan, errors)
        validate_qc_evidence_resolution(plan, errors)
        validate_evidence_acquisition(plan, errors)
        if check_files:
            change = next((item for item in plan['artifacts']
                           if isinstance(item, dict) and item.get('kind') == 'change-plan'), None)
            if isinstance(change, dict) and is_local_artifact_name(change.get('path')):
                path = artifact_path(plan_path, change)
                if os.path.isfile(path):
                    with open(path, 'r', encoding='utf-8') as f:
                        validate_analysis_traceability(plan, f.read(), errors)
    elif plan['verdict'] in QC_VERDICTS:
        validate_checklist(plan, errors)

    kinds = set()
    seen_kinds = set()
    for artifact in plan['artifacts']:
        if not isinstance(artifact, dict) or not isinstance(artifact.get('kind'), str):
            errors.append('每个 artifact 必须含 kind 字符串')
            continue
        kind = artifact['kind']
        kinds.add(kind)
        if kind in seen_kinds:
            errors.append(f'artifact.kind 不可重复：{kind}')
        seen_kinds.add(kind)
        has_path = 'path' in artifact
        has_filename = 'filename' in artifact
        expected_filename = expected_artifact_filename(plan, kind)
        if expected_filename is None:
            errors.append(f'不支持 artifact.kind：{kind}')
            continue
        if has_path and has_filename:
            errors.append('artifact 不可同时含 path 和 filename')
            continue
        if isinstance(artifact.get('path'), str):
            path_name = artifact['path']
            if kind == 'qc-followup':
                errors.append('qc-followup 必须为 inline filename，不可使用 path')
            if not is_local_artifact_name(path_name):
                errors.append(f'附件 path 必须是计划同目录文件名：{path_name}')
                continue
            allowed_filenames = allowed_artifact_filenames(plan, kind)
            if path_name not in allowed_filenames:
                suffix = ''
                if len(allowed_filenames) > 1:
                    legacy_names = sorted(allowed_filenames - {expected_filename})
                    suffix = f'；历史回放兼容 {"、".join(legacy_names)}'
                errors.append(f'附件文件名必须为 {expected_filename}{suffix}')
                continue
            if check_files:
                path = artifact_path(plan_path, artifact)
                if not os.path.isfile(path):
                    errors.append(f'附件不存在：{path}')
                    continue
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                marker = f'<!-- auto-req-run:{plan["run_id"]} -->'
                if marker not in content:
                    errors.append(f'附件缺少运行标记：{path}')
        elif isinstance(artifact.get('filename'), str):
            filename = artifact['filename']
            if kind != 'qc-followup':
                errors.append(f"inline 附件暂仅支持 qc-followup：{kind}")
            if not is_local_artifact_name(filename):
                errors.append(f'附件 filename 必须为文件名：{filename}')
            elif filename != expected_filename:
                errors.append(f'附件文件名必须为 {expected_filename}')
        else:
            errors.append('每个 artifact 必须含 path（磁盘文件）或 filename（inline）字符串')
    if kinds != required_kinds:
        errors.append(f'附件类型必须精确为 {sorted(required_kinds)}，当前为 {sorted(kinds)}')
    if plan['verdict'] in ANALYSIS_VERDICTS:
        validate_manual_followup(plan, plan_path, analysis_gaps, check_files, errors)
    return result(not errors, errors=errors, expected_tags=sorted(expected_tags),
                  state_to=expected_state_to,
                  validation_sources={'analysis_description': analysis_source})


def _load_execution_checkpoint(meta, plan):
    if not meta:
        return None
    path = os.path.join(
        meta['run_dir'], f'执行检查点_{plan["work_item_id"]}_{plan["run_id"]}.json')
    if not os.path.isfile(path) or os.path.islink(path):
        return None
    errors = []
    checkpoint = _read_json_object(path, 'execution_checkpoint', errors)
    if (errors or checkpoint.get('schema') != 'execution-checkpoint-v1'
            or checkpoint.get('work_item_id') != plan['work_item_id']
            or checkpoint.get('run_id') != plan['run_id']
            or checkpoint.get('analysis_result_sha256') != meta['snapshot_sha256']):
        return None
    return checkpoint


def _update_execution_checkpoint(meta, plan, client, completed_actions, observed_item=None):
    item = observed_item
    if item is None:
        raw = tfs.fetch_raw(client, plan['work_item_id'])
        item = tfs.map_workitem(raw)
    checkpoint = {
        'schema': 'execution-checkpoint-v1',
        'work_item_id': plan['work_item_id'],
        'run_id': plan['run_id'],
        'analysis_result_sha256': meta['snapshot_sha256'],
        'last_rev': item['rev'],
        'last_state': item['state'],
        'completed_actions': list(completed_actions),
    }
    path = os.path.join(
        meta['run_dir'], f'执行检查点_{plan["work_item_id"]}_{plan["run_id"]}.json')
    fd, temporary = tempfile.mkstemp(prefix='.execution-checkpoint-', dir=meta['run_dir'])
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(checkpoint, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write('\n')
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return checkpoint


def preflight(client, plan, resume_checkpoint=None):
    raw = tfs.fetch_raw(client, plan['work_item_id'])
    item = tfs.map_workitem(raw)
    if item['workItemType'] != '需求':
        return result(False, error=f"工作项类型必须为 '需求'，实际为 {item['workItemType']!r}")
    same_run_checkpoint = bool(
        isinstance(resume_checkpoint, dict)
        and resume_checkpoint.get('last_rev') == item['rev']
        and resume_checkpoint.get('last_state') == item['state'])
    if item['rev'] != plan['expected_rev']:
        if not same_run_checkpoint:
            return result(False, error=f"工作项版本已变化：计划为 rev {plan['expected_rev']}，当前为 rev {item['rev']}；请重新生成计划")
    if item['state'] != plan['expected_state']:
        if not same_run_checkpoint:
            return result(False, error=f"工作项状态已变化：计划为 {plan['expected_state']!r}，当前为 {item['state']!r}；请重新生成计划")
    return result(True, raw=raw, work_item=item, resumed=same_run_checkpoint)


def checked_call(action, fn, *args):
    response = fn(*args)
    if not response.get('ok'):
        raise RuntimeError(f"{action} 失败：{response.get('error', response)}")
    return {'action': action, 'result': response}


def record_failure(plan, error, run_mode, state_from='', state_to='', actions=None, extra=None,
                   audit_group='runs'):
    """尽量为已知工作项记录失败；审计自身失败不能掩盖原始错误。"""
    if (not isinstance(plan, dict) or not isinstance(plan.get('work_item_id'), int)
            or plan['work_item_id'] <= 0 or not isinstance(plan.get('run_id'), str)
            or not RUN_ID_RE.fullmatch(plan['run_id'])):
        return None
    details = {
        'run_mode': run_mode,
        'error': str(error),
        'actions': actions or [],
        'plan': extra.get('plan', '') if extra else '',
        'attachments': plan.get('attachments'),
        'analysis_description': plan.get('analysis_description'),
        'analysis_gaps': plan.get('analysis_gaps'),
        'evidence_refs': plan.get('evidence_refs'),
        'evidence_gaps': plan.get('evidence_gaps'),
        'implementation_impacts': plan.get('implementation_impacts'),
        'general_rule_coverage': plan.get('general_rule_coverage'),
        'business_rule_coverage': plan.get('business_rule_coverage'),
        'evidence_acquisition': plan.get('evidence_acquisition'),
        'ui_baseline': plan.get('ui_baseline'),
        'qc_evidence_resolution': plan.get('qc_evidence_resolution'),
        'qc_recheck': plan.get('qc_recheck'),
        'confirmation_policy': plan.get('confirmation_policy'),
        'wiki': plan.get('wiki'),
        'tfs_requirements': plan.get('tfs_requirements'),
    }
    if extra:
        details.update(extra)
    try:
        audit = tfs.record(plan.get('skill', 'auto-req-analysis'), plan['work_item_id'], 'ERROR',
                           plan.get('tags', []), state_from or plan.get('expected_state', ''), state_to,
                           '', details, plan['run_id'], audit_group=audit_group)
        return audit.get('audit')
    except Exception:
        return None


def _resolve_collection(config_path, collection_override=None):
    """尽力解析 TFS collection 供失败路径发布 Redis(状态完整性)；任何异常返回 None，绝不掩盖原始错误。"""
    try:
        return tfs.load_config(config_path, None, collection_override, None).get('collection')
    except Exception:
        return None


def failure_result(plan, error, run_mode, state_from='', state_to='', actions=None, extra=None,
                   collection=None, config_path=None, command='apply', audit_group='runs', **kwargs):
    output = result(False, error=str(error), **kwargs)
    if actions is not None:
        output['actions'] = actions
    # redis 先于审计计算，使失败轮的 redis 结果（ok/reason/in_scope）一并落审计 extra。
    redis_block = {'in_scope': bool(collection and config_path)}
    if redis_block['in_scope']:
        redis_block.update(redis_client.publish_failure(plan, error, run_mode, collection, config_path))
    output['redis'] = redis_block
    extra = dict(extra or {})
    extra['command'] = command
    extra['redis'] = redis_block
    audit = record_failure(
        plan, error, run_mode, state_from, state_to, actions, extra, audit_group=audit_group)
    if audit:
        output['audit'] = audit
    return output


def upload_inline_artifact(client, plan, artifact, dry_run):
    """物化 inline 附件（qc-followup，源自顶层 `checklist`）并上传，上传后清理临时文件。

    正文 = 运行标记行 + `checklist` 的 JSON 序列化；文件名取 `artifact['filename']` 以稳定作为去重键。
    inline 附件的运行标记由本函数注入（而非写在磁盘文件里），因此 validate 对 inline 跳过 marker-in-file 检查。
    """
    body = (f'<!-- auto-req-run:{plan["run_id"]} -->\n'
            + json.dumps(plan['checklist'], ensure_ascii=False, indent=2))
    directory = tempfile.mkdtemp(prefix='auto-req-inline-')
    path = os.path.join(directory, artifact['filename'])
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body)
        return tfs.upload_attachment(client, plan['work_item_id'], path, dry_run)
    finally:
        try:
            os.unlink(path)
            os.rmdir(directory)
        except OSError:
            pass


def apply_field_flow(client, wid, dry_run, fallback_assignee=None, assignee_override=None,
                     run_id='', before_action=None, on_action_error=None, on_action=None):
    """对工作项执行已验证的「字段流转」（2026-07-28 工作项 259681 实测通过）。

    自包含：执行时现读 TFS 取 expected_date / 当前 state / teamProject / Winning.Dev.Leader，
    按 finishDate ≤ expected_date 选迭代（排期取向=最早：取 earliest = finishDate 最小者，
    有多个满足条件的迭代时优先排到更早的那个；与质控时效用的 matched=最晚 相反，各取所需）。依次：
      write-field IterationPath(若有 earliest) → write-field StartDate(今天)
      → write-field FinishDate(earliest.finish) → set-state 活动 → set-state 已分析
      → set-assignee(WINNING\\account 若解析得出)。
    每步复用 tfs_client 原语（各自 fetch_raw + revision_guard，天然防并发）。

    指派优先级：assignee_override（flow --assignee 显式指定）> Winning.Dev.Leader > fallback_assignee
    （AUTO-ANA 的 plan.assignee_to 规则提示）。

    失败策略（遵循 EXECUTION_CONTRACT：不强转、不回滚、按步 degrade）：
      - 迭代查询失败 / 无 expected_date / 无 earliest → 降级：跳过迭代与起止日期，继续状态+指派。
      - write-field / set-state 失败 → checked_call 抛 RuntimeError 中断（核心正确性，不降级）。
      - set-assignee 失败 → 降级记 error 动作，不中断（指派与终局解耦；此时 WI 多已到终态）。
      - 入口 state=='已分析' → 跳过两步 set-state（仅补字段+指派，不倒退）。
    返回 list[dict]（每项 {'action','result'}），供 apply_plan extend 或 flow_item 独立审计。
    """
    actions = []

    def append_action(entry):
        actions.append(entry)
        if on_action:
            on_action(entry)

    def execute_action(action, function, *args):
        if before_action:
            before_action(action)
        try:
            return checked_call(action, function, *args)
        except Exception as exc:
            if on_action_error:
                on_action_error(exc)
            raise
        finally:
            client.pop('_pipeline_source_guard', None)

    raw = tfs.fetch_raw(client, wid)
    item = tfs.map_workitem(raw)
    state = item['state']
    expected_date = item['expectedDate']
    team_project = item['teamProject']

    # 1) 迭代 + 起止日期（可降级段：查询失败/无期望日/无 earliest → 跳过，不阻断流转）
    #    排期取向=最早：有多个 finish≤期望 的迭代时优先排到 finishDate 最小者（earliest），
    #    而非质控时效用的 matched（最晚）。两者语义相反，各取所需（见 field-flow.md / pre-qc-rules §三.3）。
    iteration_path = None
    finish_date = None
    if expected_date:
        try:
            it = tfs.list_iterations(client, team_project, expected_date)
            earliest = it.get('earliest') if isinstance(it, dict) and it.get('ok') else None
            if isinstance(earliest, dict):
                iteration_path = earliest.get('path')
                finish = earliest.get('finish') or ''
                finish_date = tfs.beijing_date(finish).isoformat() if finish else None
        except Exception:
            pass  # 迭代查询失败 → 降级，继续状态流转
    today = tfs.beijing_date().isoformat()
    if iteration_path:
        append_action(execute_action('write-field:IterationPath', tfs.write_field,
                                     client, wid, tfs.F_ITERATION, iteration_path, 'replace', dry_run))
        append_action(execute_action('write-field:StartDate', tfs.write_field,
                                     client, wid, tfs.F_START_DATE, today, 'replace', dry_run))
        if finish_date:
            append_action(execute_action('write-field:FinishDate', tfs.write_field,
                                         client, wid, tfs.F_FINISH_DATE, finish_date, 'replace', dry_run))

    # 2) 两步状态机（入口已是「已分析」则跳过，避免倒退 PATCH 被 TFS 拒）
    if state != '已分析':
        if state != '活动':
            append_action(execute_action('set-state:活动', tfs.set_state, client, wid, '活动', dry_run))
        append_action(execute_action('set-state:已分析', tfs.set_state, client, wid, '已分析', dry_run))

    # 3) 指派（可降级段，最后一步；assignee_override > Dev.Leader > fallback）
    assignee = None
    if assignee_override:
        assignee = resolve_assignee_to_winning('', assignee_override)
    if not assignee:
        assignee = resolve_assignee_to_winning(item.get('devLeader', ''), fallback_assignee or '')
    if assignee:
        try:
            append_action(execute_action('set-assignee', tfs.set_assignee, client, wid, assignee, dry_run))
        except RuntimeError as exc:
            actions.append({'action': 'set-assignee', 'result': {'ok': False, 'error': str(exc)}})
    return actions


def analysis_only_result(plan, meta, reason, collection, config_path,
                         error_code='EXECUTION_NOT_READY', work_item='', validation=None):
    """分析已完成但 TFS 执行资格不足：发布本轮分析，不发布 ERROR。"""
    analysis_desc_html = ''
    if meta.get('report_content'):
        analysis_desc_html = render_analysis_description_html(
            meta['report_content'], plan.get('analysis_profile'), plan.get('run_id'))
    finalization_sha = meta.get('snapshot_sha256') or meta.get('plan_sha256')
    validation_result = copy.deepcopy(validation or {
        'decision': 'PASS',
        'blocking_errors': {'analysis': [], 'source': [], 'execution': []},
        'warnings': [], 'normalizations': [],
    })
    validation_result['decision'] = 'ANALYSIS_ONLY'
    execution_blockers = {'analysis': [], 'source': [], 'execution': []}
    blocker_class = ('source' if error_code in {'SOURCE_CHANGED', 'SOURCE_NOT_READY'}
                     else 'execution')
    execution_blockers[blocker_class].append(str(reason))
    validation_result['blocking_errors'] = execution_blockers
    redis_result = {'in_scope': bool(collection and config_path)}
    if redis_result['in_scope']:
        redis_result.update(redis_client.publish_plan(
            plan, 'analysis-only', collection, config_path,
            analysis_description_html=analysis_desc_html, work_item=work_item))
    details = {
        'run_mode': 'analysis-only',
        'command': 'apply',
        'applied': False,
        'error_code': error_code,
        'reason': str(reason),
        'analysis_result_sha256': finalization_sha,
        'redis': redis_result,
        'actions': [],
        'validation': validation_result,
        'warning_count': len(validation_result.get('warnings') or []),
    }
    audit_path = None
    try:
        audit_path = tfs.record(
            plan['skill'], plan['work_item_id'], plan['verdict'], plan.get('tags', []),
            plan.get('expected_state', ''), '', '', details, plan['run_id']).get('audit')
    except Exception:
        pass
    output = result(
        True, applied=False, mode='analysis-only', run_mode='analysis-only', actions=[],
        verdict=plan['verdict'], tags=plan.get('tags', []), state_to=plan.get('state_to'),
        analysis_description=analysis_desc_html,
        error_code=error_code, reason=str(reason), retryable=False,
        redis=redis_result, analysis_result_sha256=finalization_sha,
        validation=validation_result,
        errors_by_class=execution_blockers)
    if error_code == 'SOURCE_CHANGED':
        output['requires_new_run'] = True
    if audit_path:
        output['audit'] = audit_path
    return output


def apply_plan(plan, plan_path, execute, config_path, pat_override=None, collection_override=None,
               project_override=None, legacy_replay=False):
    """受约束计划执行原语；无回执历史计划必须显式 legacy_replay=True。"""
    profile = plan.get('plan_profile') if isinstance(plan, dict) else None
    if legacy_replay is True and profile in {RUN_BOUND_PROFILE, ANALYSIS_REF_PROFILE}:
        return result(False, error_code='LEGACY_REPLAY_NOT_ALLOWED',
                      error='回执绑定的新运行不得通过 apply-legacy 执行')
    if legacy_replay is False and profile not in {RUN_BOUND_PROFILE, ANALYSIS_REF_PROFILE}:
        return result(False, error_code='LEGACY_PLAN_REQUIRES_EXPLICIT_REPLAY',
                      error='无运行回执的历史计划仅允许 apply-legacy --plan 显式维护回放',
                      applied=False, actions=[], redis={'in_scope': False})
    failure_collection = (None if legacy_replay is True
                          else _resolve_collection(config_path, collection_override))
    audit_group = 'legacy-runs' if legacy_replay is True else 'runs'
    ref_meta = None
    analysis_ref = profile == ANALYSIS_REF_PROFILE
    if analysis_ref:
        expanded, ref_meta, ref_errors = materialize_analysis_ref(plan, plan_path)
        if ref_errors:
            classified = _classify_ref_errors(ref_errors)
            rejected = _finalize_validation(
                plan, result(False, errors=ref_errors, errors_by_class=classified))
            return failure_result(
                plan, '计划校验失败：' + '; '.join(ref_errors), 'validate',
                extra={'validation_errors': ref_errors,
                       'errors_by_class': classified,
                       'validation': rejected['validation']},
                errors=ref_errors,
                errors_by_class=classified,
                validation=rejected['validation'],
                command='apply', audit_group=audit_group,
                collection=failure_collection, config_path=config_path,
                error_code=('RUN_ID_CONTEXT_MISMATCH'
                            if any('RUN_ID_CONTEXT_MISMATCH' in error or 'run_receipt' in error
                                   for error in ref_errors)
                            else 'PLAN_VALIDATION_FAILED'))
        plan = expanded
        validation = _validate_materialized_plan(plan, ref_meta, check_files=True)
    elif profile == RUN_BOUND_PROFILE:
        expanded, ref_meta, ref_errors = materialize_run_bound(plan, plan_path)
        if ref_errors:
            classified = _classify_ref_errors(ref_errors)
            rejected = _finalize_validation(
                plan, result(False, errors=ref_errors, errors_by_class=classified))
            return failure_result(
                plan, '计划校验失败：' + '; '.join(ref_errors), 'validate',
                extra={'validation_errors': ref_errors, 'errors_by_class': classified,
                       'validation': rejected['validation']},
                errors=ref_errors, errors_by_class=classified, command='apply',
                validation=rejected['validation'],
                audit_group=audit_group, error_code='RUN_ID_CONTEXT_MISMATCH',
                collection=failure_collection, config_path=config_path)
        plan = expanded
        validation = validate_plan(plan, plan_path, check_files=True)
    else:
        validation = validate_plan(plan, plan_path, check_files=True)
    if not validation['ok']:
        return failure_result(plan, '计划校验失败：' + '; '.join(validation['errors']), 'validate',
                              extra={'validation_errors': validation['errors'],
                                     'validation_sources': validation.get('validation_sources'),
                                     'validation': validation['validation']},
                              errors=validation['errors'],
                              validation=validation['validation'],
                              collection=failure_collection,
                              config_path=config_path,
                              audit_group=audit_group,
                              errors_by_class={
                                  'analysis': validation['errors'], 'source': [], 'execution': []})

    if ref_meta:
        try:
            _ensure_frozen_plan(ref_meta, plan)
            if analysis_ref:
                _ensure_frozen_analysis(ref_meta, plan)
            _set_meta_run_status(
                ref_meta, plan, 'FROZEN',
                warning_count=len(validation.get('warnings') or []))
        except (OSError, ValueError) as exc:
            error = str(exc)
            rejected_validation = copy.deepcopy(validation['validation'])
            rejected_validation['decision'] = 'REJECT'
            rejected_validation['blocking_errors']['analysis'].append(error)
            return failure_result(
                plan, error, 'validate', errors=[error],
                errors_by_class={'analysis': [error], 'source': [], 'execution': []},
                validation=rejected_validation,
                extra={'validation': rejected_validation},
                collection=None, config_path=config_path, command='apply',
                audit_group=audit_group,
                error_code=('RUN_ID_ALREADY_FINALIZED'
                            if 'RUN_ID_ALREADY_FINALIZED' in error else 'PLAN_VALIDATION_FAILED'))

    run_mode = 'legacy-replay' if legacy_replay is True else ('execute' if execute else 'dry-run')
    if ref_meta:
        _set_meta_run_status(
            ref_meta, plan, 'APPLYING', execute=bool(execute),
            warning_count=len(validation.get('warnings') or []))
    try:
        client = tfs.load_config(config_path, pat_override, collection_override, project_override)
        if legacy_replay is not True:
            failure_collection = client.get('collection') or failure_collection
        readiness = tfs.precheck(client)
        if not readiness.get('ok'):
            if ref_meta:
                return analysis_only_result(
                    plan, ref_meta, f"TFS precheck 失败：{readiness.get('error', readiness)}",
                    failure_collection, config_path, validation=validation['validation'])
            return failure_result(plan, f"TFS precheck 失败：{readiness.get('error', readiness)}", run_mode,
                                  collection=failure_collection, config_path=config_path,
                                  audit_group=audit_group)
        resume_checkpoint = _load_execution_checkpoint(ref_meta, plan)
        gate = preflight(client, plan, resume_checkpoint=resume_checkpoint)
        if not gate['ok']:
            if ref_meta:
                gate_error = gate.get('error', gate)
                source_changed = ('版本已变化' in str(gate_error) or '状态已变化' in str(gate_error))
                return analysis_only_result(
                    plan, ref_meta, gate_error, failure_collection, config_path,
                    error_code='SOURCE_CHANGED' if source_changed else 'SOURCE_NOT_READY',
                    validation=validation['validation'])
            return failure_result(plan, gate.get('error', gate), run_mode,
                                  collection=failure_collection, config_path=config_path,
                                  audit_group=audit_group)
    except Exception as exc:
        if ref_meta:
            return analysis_only_result(
                plan, ref_meta, exc, failure_collection, config_path,
                validation=validation['validation'])
        return failure_result(plan, exc, run_mode,
                              collection=failure_collection, config_path=config_path,
                              audit_group=audit_group)

    dry_run = not execute
    expected_tags = set(validation['expected_tags'])
    if plan['verdict'] in ANALYSIS_VERDICTS:
        owned_tags = ANALYSIS_TAGS
    elif plan['verdict'] in QC_VERDICTS:
        owned_tags = QC_TAGS
    else:
        # 路由跳过终局只记审计/Redis，不清理或新增任何 TFS 标签。
        owned_tags = set()
    actions = []
    observed_write_entries = []
    tfs_write_started = False
    checkpoint_actions = list(
        (resume_checkpoint or {}).get('completed_actions') or []) if ref_meta else []
    owned_rev = gate['work_item'].get('rev')
    owned_state = gate['work_item'].get('state')

    def checkpoint_before_action(_action):
        if not execute or not ref_meta:
            return
        current = tfs.map_workitem(tfs.fetch_raw(client, plan['work_item_id']))
        if current['rev'] != owned_rev or current['state'] != owned_state:
            raise RuntimeError(
                f'SOURCE_CHANGED_DURING_EXECUTION：执行器上次观测 rev/state='
                f'{owned_rev}/{owned_state}，当前={current["rev"]}/{current["state"]}')
        client['_pipeline_source_guard'] = {
            'work_item_id': plan['work_item_id'],
            'rev': owned_rev, 'state': owned_state,
        }

    def checkpoint_after_action(entry):
        nonlocal owned_rev, owned_state, tfs_write_started
        if not execute or not ref_meta or entry['result'].get('noop') is True:
            return
        tfs_write_started = True
        observed_write_entries.append(entry)
        post_rev = entry['result'].get('post_rev')
        post_state = entry['result'].get('post_state')
        if isinstance(post_rev, int) and not isinstance(post_rev, bool):
            if post_rev == owned_rev:
                if post_state not in (None, owned_state):
                    # rev 不变而 state 变化是不一致信号，不能当作等效 no-op 放行
                    raise RuntimeError(
                        f'POST_WRITE_CONFIRMATION_FAILED：动作 {entry["action"]} 写后 rev '
                        f'未变（{owned_rev}）但 state 由 {owned_state} 变为 {post_state}')
                # TFS 确认写后无 revision：内容已就位（如仅 run 标记注释差异，TFS 落库即剥）
                # → 等效 no-op，不推进 owned rev/state，但计入检查点避免续跑重做。
                checkpoint_actions.append(entry['action'])
                _update_execution_checkpoint(
                    ref_meta, plan, client, checkpoint_actions,
                    observed_item={'rev': owned_rev, 'state': owned_state})
                return
            current = {'rev': post_rev, 'state': post_state or owned_state}
        else:
            current = None
            # 兼容未返回 revision 的旧 TFS 响应；短暂等待服务端回读可见，
            # 不再假设每个业务动作的 revision 必须恰好只增加 1。
            for delay in (0, 0.1, 0.25):
                if delay:
                    time.sleep(delay)
                candidate = tfs.map_workitem(tfs.fetch_raw(client, plan['work_item_id']))
                if candidate['rev'] > owned_rev:
                    current = candidate
                    break
        if current is None or current['rev'] < owned_rev:
            raise RuntimeError(
                f'POST_WRITE_CONFIRMATION_FAILED：动作 {entry["action"]} 已返回成功，'
                f'但未取得写后 revision（写前 rev {owned_rev}）')
        owned_rev, owned_state = current['rev'], current['state']
        checkpoint_actions.append(entry['action'])
        _update_execution_checkpoint(
            ref_meta, plan, client, checkpoint_actions, observed_item=current)

    def apply_action(action, function, *args):
        nonlocal tfs_write_started
        checkpoint_before_action(action)
        try:
            entry = checked_call(action, function, *args)
        except Exception as exc:
            if 'SOURCE_CHANGED_DURING_EXECUTION' not in str(exc):
                tfs_write_started = tfs_write_started or bool(execute and ref_meta)
            raise
        finally:
            client.pop('_pipeline_source_guard', None)
        checkpoint_after_action(entry)
        return entry

    def field_flow_action_error(exc):
        nonlocal tfs_write_started
        if 'SOURCE_CHANGED_DURING_EXECUTION' not in str(exc):
            tfs_write_started = tfs_write_started or bool(execute and ref_meta)

    def field_flow_checkpoint(entry):
        checkpoint_after_action(entry)

    analysis_desc_html = ''  # 分析终局下=写入 TFS 的分析者描述 HTML，供 Redis 镜像
    # 重跑收敛：清掉 QC + 分析两阶段任一历史标签里不在本次 expected 的；
    # SKIP-ANALYSIS 的 owned_tags 为空 → cleanup_scope 为空，跳过清理（零写入保持）
    cleanup_scope = (QC_TAGS | ANALYSIS_TAGS) if owned_tags else set()
    # 覆盖重跑：作废此前下游人工通过标签（SKIP 同上豁免）
    invalidate_tags = sorted(set(gate['work_item']['tags']) & DOWNSTREAM_PASSED_TAGS) if owned_tags else []
    try:
        for tag in invalidate_tags:
            actions.append(apply_action(f'invalidate-tag:{tag}', tfs.remove_tag, client, plan['work_item_id'], tag, dry_run))
        for tag in sorted((set(gate['work_item']['tags']) & cleanup_scope) - expected_tags):
            actions.append(apply_action(f'remove-tag:{tag}', tfs.remove_tag, client, plan['work_item_id'], tag, dry_run))

        if plan['verdict'] in ANALYSIS_VERDICTS:
            change = next(a for a in plan['artifacts'] if a['kind'] == 'change-plan')
            path = artifact_path(plan_path, change)
            rendered = render_analysis_description_html(
                tfs.parse_value('@' + path), plan.get('analysis_profile'),
                plan.get('run_id') if ref_meta else None)
            analysis_desc_html = rendered  # 同一份 HTML 写 TFS + Redis，保证三者同源一致
            actions.append(apply_action('write-detail-analysis', tfs.replace_detail_analysis_section,
                                        client, plan['work_item_id'], rendered, dry_run))

        for artifact in plan['artifacts']:
            if isinstance(artifact.get('path'), str):
                actions.append(apply_action(f"upload:{artifact['kind']}", tfs.upload_attachment, client,
                                            plan['work_item_id'], artifact_path(plan_path, artifact), dry_run))
            else:
                actions.append(apply_action(f"upload:{artifact['kind']}", upload_inline_artifact,
                                            client, plan, artifact, dry_run))
        artifact_kinds = {artifact['kind'] for artifact in plan['artifacts']}
        if (plan['verdict'] in ANALYSIS_VERDICTS
                and single_confirmation_enabled(plan)
                and artifact_kinds == {'change-plan'}):
            cleanup_args = [client, plan['work_item_id'],
                            os.path.basename(artifact_path(plan_path, change)), dry_run]
            if ref_meta:
                cleanup_args.append(artifact_path(plan_path, change))
            actions.append(apply_action(
                'cleanup:analysis-artifacts', tfs.cleanup_analysis_attachments,
                *cleanup_args))
        for tag in sorted(expected_tags):
            actions.append(apply_action(f'add-tag:{tag}', tfs.add_tag, client, plan['work_item_id'], tag, dry_run))
        # AUTO-ANA 跑完整字段流转（迭代/起止日期 → 活动 → 已分析 → 指派），与手工流程一致；
        # 替换旧的单步 set-state + 原样传 display name（必报「未知标识」）的不完整实现。
        # 仅 AUTO-ANA（expected_for 里只有它返回非 None state_to）；指派优先 Winning.Dev.Leader。
        if plan['verdict'] == 'AUTO-ANA':
            actions.extend(apply_field_flow(client, plan['work_item_id'], dry_run,
                                            fallback_assignee=plan.get('assignee_to'),
                                            run_id=plan.get('run_id', ''),
                                            before_action=checkpoint_before_action if ref_meta else None,
                                            on_action_error=field_flow_action_error if ref_meta else None,
                                            on_action=field_flow_checkpoint if ref_meta else None))
    except Exception as exc:
        if ref_meta and not tfs_write_started:
            error_code = ('SOURCE_CHANGED' if 'SOURCE_CHANGED_DURING_EXECUTION' in str(exc)
                          else 'EXECUTION_NOT_READY')
            return analysis_only_result(
                plan, ref_meta, exc, failure_collection, config_path,
                error_code=error_code, validation=validation['validation'])
        failure_actions = list(actions)
        for entry in observed_write_entries:
            if entry not in failure_actions:
                failure_actions.append(entry)
        execution_error_code = (
            'POST_WRITE_CONFIRMATION_FAILED'
            if 'POST_WRITE_CONFIRMATION_FAILED' in str(exc) else None)
        return failure_result(plan, exc, run_mode, plan['expected_state'],
                              validation['state_to'] or '', failure_actions,
                              extra={'errors_by_class': {
                                  'analysis': [], 'source': [], 'execution': [str(exc)]},
                                     'validation': validation['validation'],
                                     'partial_write': bool(observed_write_entries)},
                              errors_by_class={
                                  'analysis': [], 'source': [], 'execution': [str(exc)]},
                              validation=validation['validation'],
                              collection=failure_collection, config_path=config_path,
                              audit_group=audit_group,
                              **({'error_code': execution_error_code}
                                 if execution_error_code else {}))

    _wi = gate.get('work_item') or {}
    work_item_label = f"{_wi.get('id', '')} {_wi.get('title', '')}".strip()
    if legacy_replay is True:
        redis_result = {'in_scope': False, 'reason': 'legacy replay 不发布正常 Redis 结果'}
    else:
        redis_result = redis_client.publish_plan(
            plan, run_mode, client['collection'], config_path,
            analysis_description_html=analysis_desc_html, work_item=work_item_label)
        redis_result['in_scope'] = True
    audit = tfs.record(plan['skill'], plan['work_item_id'], plan['verdict'], sorted(expected_tags),
                       plan['expected_state'], validation['state_to'] or '', '',
                       {'run_mode': run_mode, 'command': 'apply', 'rules_source': plan['rules_source'],
                        'auto_scopes': plan.get('auto_scopes', []), 'kb': plan.get('kb'), 'wiki': plan.get('wiki'),
                        'tfs_requirements': plan.get('tfs_requirements'),
                        'iteration': plan.get('iteration'), 'attachments': plan.get('attachments'),
                        'analysis_description': plan.get('analysis_description'),
                        'analysis_profile': plan.get('analysis_profile'),
                        'analysis_gaps': plan.get('analysis_gaps'),
                        'evidence_refs': plan.get('evidence_refs'),
                        'evidence_gaps': plan.get('evidence_gaps'),
                        'implementation_impacts': plan.get('implementation_impacts'),
                        'general_rule_coverage': plan.get('general_rule_coverage'),
                        'business_rule_coverage': plan.get('business_rule_coverage'),
                        'evidence_acquisition': plan.get('evidence_acquisition'),
                        'ui_baseline': plan.get('ui_baseline'),
                        'qc_evidence_resolution': plan.get('qc_evidence_resolution'),
                        'assignee_to': plan.get('assignee_to'),
                        'qc_recheck': plan.get('qc_recheck'),
                        'confirmation_policy': plan.get('confirmation_policy'),
                        'skip_reason': plan.get('skip_reason'),
                        'invalidated_passed_tags': invalidate_tags,
                        'validation': validation['validation'],
                        'warning_count': len(validation.get('warnings') or []),
                        'plan': os.path.abspath(plan_path),
                        'redis': redis_result,
                        'actions': actions}, plan['run_id'], audit_group=audit_group)
    next_action = None
    if plan['verdict'] == 'PASS':
        next_action = {'kind': 'run-skill', 'skill': 'auto-req-analysis', 'work_item_id': plan['work_item_id']}
    return result(True, applied=bool(execute), mode=run_mode, run_mode=run_mode,
                  verdict=plan['verdict'], tags=sorted(expected_tags),
                  state_to=validation['state_to'],
                  analysis_description=analysis_desc_html or None,
                  actions=actions, audit=audit['audit'],
                  next_action=next_action, redis=redis_result,
                  validation=validation['validation'])


def _apply_run_once(work_item_id, run_id, execute, config_path, pat_override=None,
                    collection_override=None, project_override=None, process_root=None):
    """普通执行唯一入口：只加载服务端 run_id 对应规范目录中的唯一计划。"""
    if (not isinstance(work_item_id, int) or work_item_id <= 0
            or not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id)):
        return result(False, error_code='RUN_ID_CONTEXT_MISMATCH',
                      error='work_item_id 或 run_id 格式无效', applied=False,
                      actions=[], redis={'in_scope': False})
    run_dir = _canonical_run_dir(work_item_id, run_id, process_root)
    known = get_run_status(work_item_id, run_id, process_root)
    if not known.get('ok'):
        return result(False, error_code='RUN_ID_CONTEXT_MISMATCH',
                      error='run_id 不是 init-run 创建的已知规范运行',
                      work_item_id=work_item_id, run_id=run_id,
                      applied=False, actions=[], redis={'in_scope': False})
    current_status = known.get('status')
    if current_status in {'ANALYZING', 'APPLYING'}:
        return result(False, error_code='RUN_ALREADY_IN_PROGRESS',
                      error='同一 run_id 已在执行中，拒绝重复进入和重复生成审计',
                      work_item_id=work_item_id, run_id=run_id,
                      applied=False, actions=[], redis={'in_scope': False})
    if current_status == 'FAILED':
        return result(False, error_code='RUN_TERMINAL_REQUIRES_NEW_RUN',
                      error='该 run_id 已失败终结；重新分析必须创建新 run_id',
                      requires_new_run=True, work_item_id=work_item_id, run_id=run_id,
                      applied=False, actions=[], redis={'in_scope': False})
    if current_status == 'ANALYSIS_ONLY' and known.get('error_code') == 'SOURCE_CHANGED':
        return result(False, error_code='RUN_TERMINAL_REQUIRES_NEW_RUN',
                      error='该 run_id 因 SOURCE_CHANGED 已终结；重新分析必须创建新 run_id',
                      requires_new_run=True, work_item_id=work_item_id, run_id=run_id,
                      applied=False, actions=[], redis={'in_scope': False})
    plan_path = os.path.join(run_dir, _plan_name(work_item_id, run_id))
    if not os.path.isfile(plan_path):
        return result(False, error_code='RUN_NOT_READY',
                      error='规范运行目录中的执行计划尚未生成',
                      work_item_id=work_item_id, run_id=run_id,
                      applied=False, actions=[], redis={'in_scope': False})
    if os.path.islink(plan_path):
        return result(False, error_code='RUN_ID_CONTEXT_MISMATCH',
                      error='规范执行计划不得为符号链接',
                      work_item_id=work_item_id, run_id=run_id,
                      applied=False, actions=[], redis={'in_scope': False})
    try:
        plan = read_plan(plan_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return result(False, error_code='RUN_ID_CONTEXT_MISMATCH', error=str(exc),
                      work_item_id=work_item_id, run_id=run_id,
                      applied=False, actions=[], redis={'in_scope': False})
    if current_status == 'COMPLETED' and (known.get('applied') or not execute):
        return result(True, mode='already-completed', run_mode='already-completed',
                      verdict=known.get('verdict', plan.get('verdict')),
                      tags=known.get('tags', plan.get('tags', [])),
                      state_to=known.get('state_to'),
                      applied=bool(known.get('applied')), idempotent=True, actions=[],
                      work_item_id=work_item_id, run_id=run_id,
                      redis={'in_scope': False, 'reason': '同一 run 已完成，不重复发布或生成审计'})
    _set_run_status(work_item_id, run_id, 'ANALYZING', process_root)
    if plan.get('work_item_id') != work_item_id or plan.get('run_id') != run_id:
        output = result(False, error_code='RUN_ID_CONTEXT_MISMATCH',
                        error='计划身份与 CLI 运行上下文不一致',
                        applied=False, actions=[], redis={'in_scope': False})
    else:
        output = apply_plan(
            plan, plan_path, execute, config_path, pat_override,
            collection_override, project_override, legacy_replay=False)
    output['work_item_id'] = work_item_id
    output['run_id'] = run_id
    if output.get('error_code') == 'RUN_NOT_READY':
        return output
    if output.get('error_code') == 'SOURCE_CHANGED':
        terminal = 'FAILED'
    elif output.get('ok') and output.get('run_mode') == 'analysis-only':
        terminal = 'ANALYSIS_ONLY'
    elif output.get('ok'):
        terminal = 'COMPLETED'
    else:
        terminal = 'FAILED'
    try:
        _set_run_status(
            work_item_id, run_id, terminal, process_root,
            applied=bool(output.get('applied')),
            error_code=output.get('error_code'),
            verdict=output.get('verdict'),
            tags=output.get('tags', []),
            state_to=output.get('state_to'),
            run_mode=output.get('run_mode'),
            warning_count=len((output.get('validation') or {}).get('warnings') or []))
    except OSError:
        pass
    return output


def apply_run(work_item_id, run_id, execute, config_path, pat_override=None,
              collection_override=None, project_override=None, process_root=None):
    """以规范运行目录内的原子锁串行化普通 apply，避免并发重复审计。"""
    if (not isinstance(work_item_id, int) or work_item_id <= 0
            or not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id)):
        return _apply_run_once(
            work_item_id, run_id, execute, config_path, pat_override,
            collection_override, project_override, process_root)
    run_dir = _canonical_run_dir(work_item_id, run_id, process_root)
    if not os.path.isdir(run_dir):
        return _apply_run_once(
            work_item_id, run_id, execute, config_path, pat_override,
            collection_override, project_override, process_root)
    lock_dir = os.path.join(run_dir, '.pipeline-apply-lock')
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        return result(False, error_code='RUN_ALREADY_IN_PROGRESS',
                      error='同一 run_id 已有 apply 调用持有执行锁，拒绝重复生成审计',
                      work_item_id=work_item_id, run_id=run_id,
                      applied=False, actions=[], redis={'in_scope': False})
    try:
        return _apply_run_once(
            work_item_id, run_id, execute, config_path, pat_override,
            collection_override, project_override, process_root)
    finally:
        try:
            os.rmdir(lock_dir)
        except OSError:
            pass


def apply_legacy_plan(plan, plan_path, execute, config_path, pat_override=None,
                      collection_override=None, project_override=None):
    """显式维护回放；永不发布正常 Redis，审计隔离到 legacy-runs。"""
    return apply_plan(
        plan, plan_path, execute, config_path, pat_override, collection_override,
        project_override, legacy_replay=True)


def repair_analysis_placement(plan, plan_path, execute, config_path, pat_override=None, collection_override=None, project_override=None):
    """修复旧执行器将 Markdown 错写到需求分析字段的既有运行。"""
    validation = validate_plan(plan, plan_path, check_files=True)
    if not validation['ok']:
        return failure_result(plan, '计划校验失败：' + '; '.join(validation['errors']), 'repair-validate',
                              extra={'validation_errors': validation['errors'],
                                     'validation_sources': validation.get('validation_sources')},
                              errors=validation['errors'])
    if plan['skill'] != 'auto-req-analysis':
        return failure_result(plan, 'repair-analysis-placement 仅支持 auto-req-analysis 计划', 'repair-validate')

    run_mode = 'repair-execute' if execute else 'repair-dry-run'
    try:
        client = tfs.load_config(config_path, pat_override, collection_override, project_override)
        readiness = tfs.precheck(client)
        if not readiness.get('ok'):
            return failure_result(plan, f"TFS precheck 失败：{readiness.get('error', readiness)}", run_mode)
        current = tfs.map_workitem(tfs.fetch_raw(client, plan['work_item_id']))
        if current['workItemType'] != '需求':
            return failure_result(plan, f"工作项类型必须为 '需求'，实际为 {current['workItemType']!r}",
                                  run_mode, current.get('state', ''))
    except Exception as exc:
        return failure_result(plan, exc, run_mode)

    change = next(a for a in plan['artifacts'] if a['kind'] == 'change-plan')
    content = tfs.parse_value('@' + artifact_path(plan_path, change))
    dry_run = not execute
    actions = []
    try:
        rendered = render_analysis_description_html(content, plan.get('analysis_profile'))
        actions.append(checked_call('replace-detail-analysis', tfs.replace_detail_analysis_section,
                                    client, plan['work_item_id'], rendered, dry_run))
        actions.append(checked_call('remove-legacy-analysis', tfs.remove_legacy_analysis_append,
                                    client, plan['work_item_id'], legacy_analysis_body(content), dry_run))
    except Exception as exc:
        return failure_result(plan, exc, run_mode, current['state'], '', actions,
                              {'plan': os.path.abspath(plan_path)})

    audit = tfs.record(plan['skill'], plan['work_item_id'], 'REPAIR-ANALYSIS-PLACEMENT', [],
                       current['state'], '', '',
                       {'run_mode': run_mode,
                        'plan': os.path.abspath(plan_path), 'actions': actions}, plan['run_id'])
    return result(True, mode=run_mode, actions=actions,
                  audit=audit['audit'])


def _flow_audit(verdict, wid, state_from, state_to, actions, run_id, assignee_override, exc=None):
    """字段流转专用审计落盘（过程文件/<wid>/runs/），与 apply_plan 审计同构。"""
    details = {'run_mode_source': 'flow', 'actions': actions, 'assignee_override': assignee_override}
    if exc is not None:
        details['error'] = str(exc)
    try:
        return tfs.record('auto-req-analysis', wid, verdict, [], state_from, state_to, '', details, run_id).get('audit')
    except Exception:
        return None


def flow_item(wid, execute, config_path, expected_rev=None, expected_state=None,
              assignee_override=None, run_id='', pat_override=None, collection_override=None, project_override=None):
    """对任意工作项单独跑字段流转（与 verdict 回写解耦）。

    用于 MANUAL-REVIEW 等人工确认后推流到「已分析」（如 2026-07-28 的 259681）。
    可选 expected_rev/state 闸（传了走硬校验防并发覆盖）；不传则靠每步原语自带的 revision_guard。
    assignee_override（WINNING\\account）优先于 Winning.Dev.Leader。
    """
    run_mode = 'flow-execute' if execute else 'flow-dry-run'
    try:
        client = tfs.load_config(config_path, pat_override, collection_override, project_override)
        readiness = tfs.precheck(client)
        if not readiness.get('ok'):
            return result(False, error=f"TFS precheck 失败：{readiness.get('error', readiness)}", mode=run_mode)
        current = tfs.map_workitem(tfs.fetch_raw(client, wid))
        if current['workItemType'] != '需求':
            return result(False, error=f"工作项类型必须为 '需求'，实际为 {current['workItemType']!r}", mode=run_mode)
        if expected_rev is not None and current['rev'] != expected_rev:
            return result(False, error=f"工作项版本已变化：期望 rev {expected_rev}，当前 rev {current['rev']}；请重新确认", mode=run_mode)
        if expected_state and current['state'] != expected_state:
            return result(False, error=f"工作项状态已变化：期望 {expected_state!r}，当前 {current['state']!r}", mode=run_mode)
    except Exception as exc:
        return result(False, error=str(exc), mode=run_mode)

    state_from = current['state']
    run_id = run_id or f"flow_{tfs.beijing_timestamp('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    dry_run = not execute
    try:
        actions = apply_field_flow(client, wid, dry_run,
                                   assignee_override=assignee_override, run_id=run_id)
    except Exception as exc:
        audit = _flow_audit('ERROR', wid, state_from, '已分析', [], run_id, assignee_override, exc)
        return result(False, error=str(exc), mode=run_mode, actions=[], audit=audit)
    audit = _flow_audit('FIELD-FLOW', wid, state_from, '已分析', actions, run_id, assignee_override)
    return result(True, mode=run_mode, actions=actions, audit=audit)


def _run_flow(args):
    """解析 flow 子命令参数：--id 主接口；--plan 兼容（取 wid/run_id/expected_*）。"""
    if args.id is not None:
        wid = args.id
        run_id = args.run_id or ''
    else:
        plan = read_plan(args.plan)
        wid = plan['work_item_id']
        run_id = args.run_id or plan.get('run_id', '')
        if args.expected_rev is None:
            args.expected_rev = plan.get('expected_rev')
        if not args.expected_state:
            args.expected_state = plan.get('expected_state')
    return flow_item(wid, args.execute, args.config, args.expected_rev, args.expected_state,
                     args.assignee, run_id, args.pat, args.collection, args.project)


def main():
    parser = argparse.ArgumentParser(description='auto-req TFS 计划校验与受约束执行器')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init-run', help='为一次新的分析触发原子生成 run_id 与运行回执')
    init.add_argument('--id', type=int, required=True, help='工作项 ID；run_id 不接受调用方传入')
    validate = sub.add_parser('validate')
    validate.add_argument('--plan', required=True)
    apply = sub.add_parser('apply', help='按服务端 run_id 加载规范目录中的唯一新计划')
    apply.add_argument('--id', type=int, required=True, help='工作项 ID')
    apply.add_argument('--run-id', required=True, help='init-run 返回并由编排器内部传播的 run_id')
    apply.add_argument('--config', default=os.path.join(tfs.SCRIPT_DIR, 'tfs-config.json'))
    apply.add_argument('--pat', default=None, help='临时 PAT；缺省用 TFS_PAT 或配置 tfs.pat')
    apply.add_argument('--collection', default=None, help='临时 collection；缺省用 TFS_COLLECTION 或配置 tfs.collection')
    apply.add_argument('--project', default=None, help='临时 project；缺省用 TFS_PROJECT 或配置 tfs.project')
    apply.add_argument('--execute', action='store_true', help='显式允许写 TFS；缺省只 dry-run')
    legacy = sub.add_parser('apply-legacy', help='显式维护回放无回执历史计划')
    legacy.add_argument('--plan', required=True)
    legacy.add_argument('--config', default=os.path.join(tfs.SCRIPT_DIR, 'tfs-config.json'))
    legacy.add_argument('--pat', default=None)
    legacy.add_argument('--collection', default=None)
    legacy.add_argument('--project', default=None)
    legacy.add_argument('--execute', action='store_true')
    status = sub.add_parser('status', help='只按工作项和 run_id 读取规范运行状态')
    status.add_argument('--id', type=int, required=True)
    status.add_argument('--run-id', required=True)
    repair = sub.add_parser('repair-analysis-placement')
    repair.add_argument('--plan', required=True)
    repair.add_argument('--config', default=os.path.join(tfs.SCRIPT_DIR, 'tfs-config.json'))
    repair.add_argument('--pat', default=None, help='临时 PAT；缺省用 TFS_PAT 或配置 tfs.pat')
    repair.add_argument('--collection', default=None, help='临时 collection；缺省用 TFS_COLLECTION 或配置 tfs.collection')
    repair.add_argument('--project', default=None, help='临时 project；缺省用 TFS_PROJECT 或配置 tfs.project')
    repair.add_argument('--execute', action='store_true', help='显式允许写 TFS；缺省只 dry-run')
    flow = sub.add_parser('flow', help='对任意工作项单独跑字段流转（迭代/日期/活动→已分析/指派）')
    fg = flow.add_mutually_exclusive_group(required=True)
    fg.add_argument('--id', type=int, help='工作项 ID（主接口，无需计划）')
    fg.add_argument('--plan', help='兼容：从计划取 work_item_id/run_id/expected_rev/expected_state')
    flow.add_argument('--config', default=os.path.join(tfs.SCRIPT_DIR, 'tfs-config.json'))
    flow.add_argument('--pat', default=None, help='临时 PAT；缺省用 TFS_PAT 或配置 tfs.pat')
    flow.add_argument('--collection', default=None, help='临时 collection；缺省用 TFS_COLLECTION 或配置 tfs.collection')
    flow.add_argument('--project', default=None, help='临时 project；缺省用 TFS_PROJECT 或配置 tfs.project')
    flow.add_argument('--expected-rev', type=int, default=None, help='可选版本闸；不传则靠每步 revision_guard')
    flow.add_argument('--expected-state', default=None, help='可选状态闸')
    flow.add_argument('--assignee', default=None, help='可选指派 WINNING\\\\account，优先于 Winning.Dev.Leader')
    flow.add_argument('--run-id', default=None, help='审计 run_id；缺省自动生成 flow_<ts>_<hex>')
    flow.add_argument('--execute', action='store_true', help='显式允许写 TFS；缺省只 dry-run')
    args = parser.parse_args()
    try:
        if args.command == 'init-run':
            output = init_run(args.id)
        elif args.command == 'status':
            output = get_run_status(args.id, args.run_id)
        elif args.command == 'flow':
            output = _run_flow(args)
        elif args.command == 'apply':
            output = apply_run(
                args.id, args.run_id, args.execute, args.config,
                args.pat, args.collection, args.project)
        else:
            plan = read_plan(args.plan)
            if args.command == 'validate':
                output = validate_plan(plan, args.plan)
                if not output['ok']:
                    audit = record_failure(plan, '计划校验失败：' + '; '.join(output['errors']), 'validate',
                                         extra={'validation_errors': output['errors'],
                                                'validation_sources': output.get('validation_sources'),
                                                'validation': output.get('validation'),
                                                'plan': os.path.abspath(args.plan),
                                                'command': 'validate',
                                                'redis': {'in_scope': False}},
                                         audit_group=(
                                             'runs' if plan.get('plan_profile') in {
                                                 RUN_BOUND_PROFILE, ANALYSIS_REF_PROFILE}
                                             else 'legacy-runs'))
                    if audit:
                        output['audit'] = audit
            elif args.command == 'repair-analysis-placement':
                output = repair_analysis_placement(plan, args.plan, args.execute, args.config,
                                                   getattr(args, 'pat', None), getattr(args, 'collection', None))
            elif args.command == 'apply-legacy':
                output = apply_legacy_plan(
                    plan, args.plan, args.execute, args.config,
                    args.pat, args.collection, args.project)
            else:
                raise ValueError(f'未知命令：{args.command}')
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        output = result(False, error=str(exc))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    sys.exit(0 if output.get('ok') else 1)


if __name__ == '__main__':
    main()
