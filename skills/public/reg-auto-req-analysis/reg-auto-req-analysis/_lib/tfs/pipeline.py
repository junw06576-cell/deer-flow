#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证并执行 auto-req-qc / auto-req-analysis 的受约束 TFS 计划。

默认只 dry-run；只有显式传入 --execute 才会写 TFS。计划中的自然语言判断
（质控结论、变更方案）由 skill 生成，本脚本只负责校验计划和可恢复地执行写入。
"""
import argparse
import html
import json
import os
import re
import sys
import tempfile
import uuid

import tfs_client as tfs
import redis_client


PLAN_VERSION = 2
SUPPORTED_PLAN_VERSIONS = {1, PLAN_VERSION}
RUN_ID_RE = re.compile(r'^[A-Za-z0-9_-]{8,80}$')
ANALYSIS_GAP_ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
EVIDENCE_REF_RE = re.compile(r'^(?:work-item|kb:[0-9]+|wiki:[0-9]+|req:[0-9]+)$')
QC_TAGS = {'PM-AI-QC-NEED-INFO', 'PM-AI-QC-NEED-REVIEW'}
ANALYSIS_TAGS = {'PM-AI-AUTO-ANA', 'PM-AI-MANUAL-REVIEW', 'PM-AI-STOP-AUTO'}
DOWNSTREAM_TERMINAL_TAGS = {'PM-AI-MANUAL-PASSED'}
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
}
QC_EVIDENCE_RESOLUTION_FIELDS = ('id', 'initial_gap', 'resolution', 'evidence_refs')
TFS_REQUIREMENTS_FINDING_FIELDS = ('work_item_id', 'fact', 'state', 'source_tool')
TFS_REQUIREMENTS_FINDING_STATES = {'已证实', '候选', '未确认'}
TFS_REQUIREMENTS_TOOLS = {
    'get_requirements_summary', 'get_related_work_items', 'search_requirements', 'get_work_item',
}
TFS_REQUIREMENTS_CONFIRMED_TOOLS = {'get_related_work_items', 'get_work_item'}
TFS_MATURITY_STATES = {'设想', '分析确认', '已落地'}
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
MAX_QC_ITEMS = 3

# 字段流转：指派人账号解析（身份字段必须 WINNING\account 格式；bare display name TFS 报「未知标识」HTTP400）
ASSIGNEE_FULL_RE = re.compile(r'<([^>]+)>')                      # Dev.Leader 全格式里的 <WINNING\account>
ASSIGNEE_WINNING_RE = re.compile(r'^WINNING\\[A-Za-z][A-Za-z0-9_]*$')
ASSIGNEE_PAREN_RE = re.compile(r'\(([A-Za-z][A-Za-z0-9_]*)\)')   # 回退 display(account) 取账号
QC_RULE_SOURCE = 'pre-qc-v1'
ANALYSIS_RULE_SOURCES = {'fallback-v1', 'evidence-loop-v1', 'evidence-loop-v2'}
LEGACY_RULE_SOURCES = {'pre-qc-v1', 'fallback'}


def extract_analysis_description_markdown(content):
    """提取变更方案中唯一的“分析者描述”二级章节。"""
    headings = list(re.finditer(r'^##\s+(.+?)\s*$', content, re.MULTILINE))
    matches = [heading for heading in headings if heading.group(1) == '三、分析者描述']
    if len(matches) != 1:
        raise ValueError('变更方案必须且只能包含一个“## 三、分析者描述”章节')
    start = matches[0].end()
    following = next((heading.start() for heading in headings if heading.start() > start), len(content))
    return content[start:following]


def _plain_analysis_text(value):
    """移除受控 Markdown 行内标记后再转义为 HTML 文本。"""
    plain = re.sub(r'\*\*([^*]+)\*\*', r'\1', value.strip()).replace('`', '')
    return html.escape(plain, quote=True)


def render_analysis_description_html(content, analysis_profile=None):
    """将受控分析者描述 Markdown 转成 TFS 编辑器可读的基础 HTML。"""
    section = re.sub(r'<!--.*?-->', '', extract_analysis_description_markdown(content), flags=re.DOTALL)
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
        numbered = re.fullmatch(r'([1-9][0-9]*)\.\s+(\S.*?)\s*', line)
        if numbered:
            rendered.append(f'<div>{numbered.group(1)}. {_plain_analysis_text(numbered.group(2))}</div>')
            continue
        raise ValueError(f'分析者描述含不支持的 Markdown 行：{line}')
    if not rendered:
        raise ValueError('分析者描述不能为空')
    return '<div><br></div>' + ''.join(rendered) + '<div><br></div>'


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


def artifact_path(plan_path, artifact):
    return os.path.join(os.path.dirname(os.path.abspath(plan_path)), artifact['path'])


def is_local_artifact_name(value):
    """只接受计划同目录下的文件名，拒绝绝对路径和目录穿越。"""
    return (isinstance(value, str) and value not in ('', '.', '..')
            and not os.path.isabs(value) and os.path.basename(value) == value)


def expected_artifact_filename(plan, kind):
    wid = plan['work_item_id']
    run_id = plan['run_id']
    names = {
        'qc-followup': f'待补充信息_{wid}_{run_id}.json',
        'change-plan': f'变更方案_{wid}_{run_id}.md',
        'manual-followup': f'待确认清单_{wid}_{run_id}.md',
    }
    return names.get(kind)


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
        if plan.get('analysis_gaps'):
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
        if chain is not None and chain not in ATTACHMENT_CONVERTERS:
            errors.append(
                f'attachments.parsed[{index}] 的转换链必须为 {sorted(ATTACHMENT_CONVERTERS)} 之一')
        if enriched:
            if item.get('converter_chain') not in ATTACHMENT_CONVERTERS:
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
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    review_content = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)
    validate_iteration_analysis_closure(plan, review_content, errors)
    if '## 三、分析者描述' not in review_content:
        errors.append('变更方案缺少“## 三、分析者描述”区')
        return
    try:
        analysis_section = re.sub(
            r'<!--.*?-->', '', extract_analysis_description_markdown(content), flags=re.DOTALL)
    except ValueError as exc:
        errors.append(str(exc))
        return
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
            errors.append('变更方案“需求类别”必须与 analysis_description.categories 完全一致')

    path_values = re.findall(
        r'^\s*-\s*\*\*路径\*\*：\s*(\S.*?)\s*$', analysis_section, re.MULTILINE)
    if profile == 'concise-v3':
        if path_values:
            errors.append('concise-v3 分析者描述不得包含固定“路径”行')
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
        if re.search(r'^\s*-\s*\*\*.+?\*\*：', preamble, re.MULTILINE):
            errors.append('concise-v3 业务维度必须写在对应类别标题下')
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(category_content)
        sections[header.group(1)] = category_content[header.end():end]
    if set(sections) != set(categories):
        errors.append('变更方案三级标题类别必须与 analysis_description.categories 完全一致')
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
        errors.append('变更方案不得包含模板占位符或 TODO')
    for phrase in ANALYSIS_BANNED_PHRASES:
        if phrase in review_content:
            errors.append(f'变更方案包含空泛描述：{phrase}')


def validate_analysis_traceability(plan, content, errors):
    """校验 v2 业务范围、方案、验收和结论状态的一对一追踪。"""
    source = plan.get('rules_source')
    if not isinstance(source, dict) or source.get('analysis') != 'evidence-loop-v1':
        return

    matches = list(re.finditer(rf'^##\s+{re.escape(TRACEABILITY_HEADING)}\s*$', content, re.MULTILINE))
    if len(matches) != 1:
        errors.append(f'变更方案必须且只能包含一个“## {TRACEABILITY_HEADING}”区')
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
    """校验 evidence-loop-v1 变更方案的业务分析闭环。"""
    source = plan.get('rules_source')
    if not isinstance(source, dict) or source.get('analysis') != 'evidence-loop-v1':
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
        errors.append('变更方案必须且只能包含一个“## 二、迭代分析闭环”区')
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


def validate_plan(plan, plan_path, check_files=True):
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
                      'tfs_requirements', 'existing_feature'):
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

    validate_tfs_requirements(plan, errors)
    validate_attachments(plan, errors)
    validate_existing_feature(plan, errors)

    analysis_gaps = []
    if plan['verdict'] in ANALYSIS_VERDICTS:
        validate_analysis_description(plan, plan_path, check_files, errors)
        analysis_gaps = validate_analysis_gaps(plan, errors)
        validate_evidence_gaps(plan, errors)
        validate_evidence_refs(plan, errors)
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
            if path_name != expected_filename:
                errors.append(f'附件文件名必须为 {expected_filename}')
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
    return result(not errors, errors=errors, expected_tags=sorted(expected_tags), state_to=expected_state_to)


def preflight(client, plan):
    raw = tfs.fetch_raw(client, plan['work_item_id'])
    item = tfs.map_workitem(raw)
    if item['workItemType'] != '需求':
        return result(False, error=f"工作项类型必须为 '需求'，实际为 {item['workItemType']!r}")
    if item['rev'] != plan['expected_rev']:
        return result(False, error=f"工作项版本已变化：计划为 rev {plan['expected_rev']}，当前为 rev {item['rev']}；请重新生成计划")
    if item['state'] != plan['expected_state']:
        return result(False, error=f"工作项状态已变化：计划为 {plan['expected_state']!r}，当前为 {item['state']!r}；请重新生成计划")
    blocked = sorted(set(item['tags']) & DOWNSTREAM_TERMINAL_TAGS)
    if blocked:
        return result(False, error=f'工作项已有下游终局标签 {blocked}，禁止覆盖')
    return result(True, raw=raw, work_item=item)


def checked_call(action, fn, *args):
    response = fn(*args)
    if not response.get('ok'):
        raise RuntimeError(f"{action} 失败：{response.get('error', response)}")
    return {'action': action, 'result': response}


def record_failure(plan, error, run_mode, state_from='', state_to='', actions=None, extra=None):
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
        'qc_evidence_resolution': plan.get('qc_evidence_resolution'),
        'qc_recheck': plan.get('qc_recheck'),
        'wiki': plan.get('wiki'),
        'tfs_requirements': plan.get('tfs_requirements'),
    }
    if extra:
        details.update(extra)
    try:
        audit = tfs.record(plan.get('skill', 'auto-req-analysis'), plan['work_item_id'], 'ERROR',
                           plan.get('tags', []), state_from or plan.get('expected_state', ''), state_to,
                           '', details, plan['run_id'])
        return audit.get('audit')
    except Exception:
        return None


def failure_result(plan, error, run_mode, state_from='', state_to='', actions=None, extra=None, **kwargs):
    output = result(False, error=str(error), **kwargs)
    if actions is not None:
        output['actions'] = actions
    audit = record_failure(plan, error, run_mode, state_from, state_to, actions, extra)
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


def apply_field_flow(client, wid, dry_run, fallback_assignee=None, assignee_override=None, run_id=''):
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
        actions.append(checked_call('write-field:IterationPath', tfs.write_field,
                                    client, wid, tfs.F_ITERATION, iteration_path, 'replace', dry_run))
        actions.append(checked_call('write-field:StartDate', tfs.write_field,
                                    client, wid, tfs.F_START_DATE, today, 'replace', dry_run))
        if finish_date:
            actions.append(checked_call('write-field:FinishDate', tfs.write_field,
                                        client, wid, tfs.F_FINISH_DATE, finish_date, 'replace', dry_run))

    # 2) 两步状态机（入口已是「已分析」则跳过，避免倒退 PATCH 被 TFS 拒）
    if state != '已分析':
        if state != '活动':
            actions.append(checked_call('set-state:活动', tfs.set_state, client, wid, '活动', dry_run))
        actions.append(checked_call('set-state:已分析', tfs.set_state, client, wid, '已分析', dry_run))

    # 3) 指派（可降级段，最后一步；assignee_override > Dev.Leader > fallback）
    assignee = None
    if assignee_override:
        assignee = resolve_assignee_to_winning('', assignee_override)
    if not assignee:
        assignee = resolve_assignee_to_winning(item.get('devLeader', ''), fallback_assignee or '')
    if assignee:
        try:
            actions.append(checked_call('set-assignee', tfs.set_assignee, client, wid, assignee, dry_run))
        except RuntimeError as exc:
            actions.append({'action': 'set-assignee', 'result': {'ok': False, 'error': str(exc)}})
    return actions


def apply_plan(plan, plan_path, execute, config_path, pat_override=None, collection_override=None, project_override=None):
    validation = validate_plan(plan, plan_path, check_files=True)
    if not validation['ok']:
        return failure_result(plan, '计划校验失败：' + '; '.join(validation['errors']), 'validate',
                              extra={'validation_errors': validation['errors']},
                              errors=validation['errors'])

    run_mode = 'execute' if execute else 'dry-run'
    try:
        client = tfs.load_config(config_path, pat_override, collection_override, project_override)
        readiness = tfs.precheck(client)
        if not readiness.get('ok'):
            return failure_result(plan, f"TFS precheck 失败：{readiness.get('error', readiness)}", run_mode)
        gate = preflight(client, plan)
        if not gate['ok']:
            return failure_result(plan, gate.get('error', gate), run_mode)
    except Exception as exc:
        return failure_result(plan, exc, run_mode)

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
    analysis_desc_html = ''  # 分析终局下=写入 TFS 的分析者描述 HTML，供 Redis 镜像
    try:
        for tag in sorted((set(gate['work_item']['tags']) & owned_tags) - expected_tags):
            actions.append(checked_call(f'remove-tag:{tag}', tfs.remove_tag, client, plan['work_item_id'], tag, dry_run))

        if plan['verdict'] in ANALYSIS_VERDICTS:
            change = next(a for a in plan['artifacts'] if a['kind'] == 'change-plan')
            path = artifact_path(plan_path, change)
            rendered = render_analysis_description_html(
                tfs.parse_value('@' + path), plan.get('analysis_profile'))
            analysis_desc_html = rendered  # 同一份 HTML 写 TFS + Redis，保证三者同源一致
            actions.append(checked_call('write-detail-analysis', tfs.replace_detail_analysis_section,
                                        client, plan['work_item_id'], rendered, dry_run))

        for artifact in plan['artifacts']:
            if isinstance(artifact.get('path'), str):
                actions.append(checked_call(f"upload:{artifact['kind']}", tfs.upload_attachment, client,
                                            plan['work_item_id'], artifact_path(plan_path, artifact), dry_run))
            else:
                actions.append(checked_call(f"upload:{artifact['kind']}", upload_inline_artifact,
                                            client, plan, artifact, dry_run))
        for tag in sorted(expected_tags):
            actions.append(checked_call(f'add-tag:{tag}', tfs.add_tag, client, plan['work_item_id'], tag, dry_run))
        # AUTO-ANA 跑完整字段流转（迭代/起止日期 → 活动 → 已分析 → 指派），与手工流程一致；
        # 替换旧的单步 set-state + 原样传 display name（必报「未知标识」）的不完整实现。
        # 仅 AUTO-ANA（expected_for 里只有它返回非 None state_to）；指派优先 Winning.Dev.Leader。
        if plan['verdict'] == 'AUTO-ANA':
            actions.extend(apply_field_flow(client, plan['work_item_id'], dry_run,
                                            fallback_assignee=plan.get('assignee_to'),
                                            run_id=plan.get('run_id', '')))
    except Exception as exc:
        return failure_result(plan, exc, run_mode, plan['expected_state'],
                              validation['state_to'] or '', actions)

    _wi = gate.get('work_item') or {}
    work_item_label = f"{_wi.get('id', '')} {_wi.get('title', '')}".strip()
    redis_result = redis_client.publish_plan(plan, run_mode, client['collection'], config_path,
                                             analysis_description_html=analysis_desc_html,
                                             work_item=work_item_label)
    audit = tfs.record(plan['skill'], plan['work_item_id'], plan['verdict'], sorted(expected_tags),
                       plan['expected_state'], validation['state_to'] or '', '',
                       {'run_mode': run_mode, 'rules_source': plan['rules_source'],
                        'auto_scopes': plan.get('auto_scopes', []), 'kb': plan.get('kb'), 'wiki': plan.get('wiki'),
                        'tfs_requirements': plan.get('tfs_requirements'),
                        'iteration': plan.get('iteration'), 'attachments': plan.get('attachments'),
                        'analysis_description': plan.get('analysis_description'),
                        'analysis_profile': plan.get('analysis_profile'),
                        'analysis_gaps': plan.get('analysis_gaps'),
                        'evidence_refs': plan.get('evidence_refs'),
                        'evidence_gaps': plan.get('evidence_gaps'),
                        'qc_evidence_resolution': plan.get('qc_evidence_resolution'),
                        'assignee_to': plan.get('assignee_to'),
                        'qc_recheck': plan.get('qc_recheck'),
                        'skip_reason': plan.get('skip_reason'),
                        'plan': os.path.abspath(plan_path),
                        'redis': redis_result,
                        'actions': actions}, plan['run_id'])
    next_action = None
    if plan['verdict'] == 'PASS':
        next_action = {'kind': 'run-skill', 'skill': 'auto-req-analysis', 'work_item_id': plan['work_item_id']}
    return result(True, mode=run_mode, actions=actions, audit=audit['audit'],
                  next_action=next_action, redis=redis_result)


def repair_analysis_placement(plan, plan_path, execute, config_path, pat_override=None, collection_override=None, project_override=None):
    """修复旧执行器将 Markdown 错写到需求分析字段的既有运行。"""
    validation = validate_plan(plan, plan_path, check_files=True)
    if not validation['ok']:
        return failure_result(plan, '计划校验失败：' + '; '.join(validation['errors']), 'repair-validate',
                              extra={'validation_errors': validation['errors']},
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
        blocked = sorted(set(current['tags']) & DOWNSTREAM_TERMINAL_TAGS)
        if blocked:
            return result(False, error=f'工作项已有下游终局标签 {blocked}，禁止覆盖', mode=run_mode)
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
    validate = sub.add_parser('validate')
    validate.add_argument('--plan', required=True)
    apply = sub.add_parser('apply')
    apply.add_argument('--plan', required=True)
    apply.add_argument('--config', default=os.path.join(tfs.SCRIPT_DIR, 'tfs-config.json'))
    apply.add_argument('--pat', default=None, help='临时 PAT；缺省用 TFS_PAT 或配置 tfs.pat')
    apply.add_argument('--collection', default=None, help='临时 collection；缺省用 TFS_COLLECTION 或配置 tfs.collection')
    apply.add_argument('--project', default=None, help='临时 project；缺省用 TFS_PROJECT 或配置 tfs.project')
    apply.add_argument('--execute', action='store_true', help='显式允许写 TFS；缺省只 dry-run')
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
        if args.command == 'flow':
            output = _run_flow(args)
        else:
            plan = read_plan(args.plan)
            if args.command == 'validate':
                output = validate_plan(plan, args.plan)
                if not output['ok']:
                    audit = record_failure(plan, '计划校验失败：' + '; '.join(output['errors']), 'validate',
                                         extra={'validation_errors': output['errors'],
                                                'plan': os.path.abspath(args.plan)})
                    if audit:
                        output['audit'] = audit
            elif args.command == 'repair-analysis-placement':
                output = repair_analysis_placement(plan, args.plan, args.execute, args.config,
                                                   getattr(args, 'pat', None), getattr(args, 'collection', None))
            else:
                output = apply_plan(plan, args.plan, args.execute, args.config,
                                    getattr(args, 'pat', None), getattr(args, 'collection', None))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        output = result(False, error=str(exc))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    sys.exit(0 if output.get('ok') else 1)


if __name__ == '__main__':
    main()
