#!/usr/bin/env python3
"""Deterministically close out one run's experience-memory decision."""

import argparse
import datetime
import fcntl
import json
import os
import re
import tempfile
import unicodedata
from zoneinfo import ZoneInfo


HEADER = """# skill 经验记忆

> 每次产生可复用经验自动追加一条；启动时读作先验，不进 TFS、不入 Git。
> 使用：先按\"类别 + 场景\"筛选；命中\"不适用\"则跳过，其余仅作提示，仍须核对当前证据和现行规则。
> 冲突：被较新条目\"取代\"引用的旧条目不再应用；无取代关系时不写该字段。
"""

MEMORY_CATEGORIES = {
    '准入·路由', '质控·spec清晰度', '质控·时效', '质控·查重',
    '分析·描述', '分析·方案', '计划·结构校验', '执行·字段流转',
}
PERSISTABLE_DIAGNOSES = {
    '信息判断错误', '输出质量不好', '准入错误', '规则/KB缺口', '规则·KB缺口',
}
DIAGNOSIS_CATEGORIES = PERSISTABLE_DIAGNOSES | {'信息不足·合理gap', '其他'}
REQUEST_KEYS = {
    'work_item_id', 'run_id', 'round_diagnosis_categories', 'runtime_lesson',
    'reason', 'candidate',
}
CANDIDATE_KEYS = {
    'title', 'category', 'scenario', 'practice', 'not_applicable',
    'otherwise', 'replaces', 'note',
}
H2_RE = re.compile(
    r'^##\s+(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?\s+·\s+.+?)\s*$', re.MULTILINE)


class MemoryInputError(ValueError):
    """The closeout request violates the skill-memory contract."""


def _text(value, field, required=True):
    if value is None and not required:
        return ''
    if not isinstance(value, str):
        raise MemoryInputError(f'{field} 必须是字符串')
    value = value.strip()
    if required and not value:
        raise MemoryInputError(f'{field} 不能为空')
    if '\n' in value or '\r' in value:
        raise MemoryInputError(f'{field} 必须是单行文本')
    return value


def _normalize(value):
    value = unicodedata.normalize('NFKC', value)
    return ' '.join(value.split()).casefold()


def _validate_request(request):
    if not isinstance(request, dict):
        raise MemoryInputError('输入根节点必须是 JSON 对象')
    extra = set(request) - REQUEST_KEYS
    if extra:
        raise MemoryInputError('输入包含未允许字段：' + ', '.join(sorted(extra)))

    work_item_id = request.get('work_item_id')
    if not isinstance(work_item_id, int) or isinstance(work_item_id, bool) or work_item_id <= 0:
        raise MemoryInputError('work_item_id 必须是正整数')
    run_id = _text(request.get('run_id'), 'run_id')
    reason = _text(request.get('reason'), 'reason')

    diagnoses = request.get('round_diagnosis_categories', [])
    if not isinstance(diagnoses, list) or any(not isinstance(item, str) for item in diagnoses):
        raise MemoryInputError('round_diagnosis_categories 必须是字符串数组')
    diagnoses = [item.strip() for item in diagnoses if item.strip()]
    unknown_diagnoses = set(diagnoses) - DIAGNOSIS_CATEGORIES
    if unknown_diagnoses:
        raise MemoryInputError('未知重分析诊断类别：' + ', '.join(sorted(unknown_diagnoses)))
    runtime_lesson = request.get('runtime_lesson', False)
    if not isinstance(runtime_lesson, bool):
        raise MemoryInputError('runtime_lesson 必须是布尔值')

    should_persist = runtime_lesson or bool(PERSISTABLE_DIAGNOSES.intersection(diagnoses))
    candidate = request.get('candidate')
    if should_persist and candidate is None:
        raise MemoryInputError('命中可沉淀触发条件时 candidate 不能为空')
    if not should_persist and candidate is not None:
        raise MemoryInputError('未命中可沉淀触发条件时 candidate 必须为 null')

    validated_candidate = _validate_candidate(candidate) if candidate is not None else None
    return {
        'work_item_id': work_item_id,
        'run_id': run_id,
        'reason': reason,
        'candidate': validated_candidate,
    }


def _validate_candidate(candidate):
    if not isinstance(candidate, dict):
        raise MemoryInputError('candidate 必须是 JSON 对象或 null')
    extra = set(candidate) - CANDIDATE_KEYS
    if extra:
        raise MemoryInputError('candidate 包含未允许字段：' + ', '.join(sorted(extra)))

    category = _text(candidate.get('category'), 'candidate.category')
    if category not in MEMORY_CATEGORIES:
        raise MemoryInputError('candidate.category 不在受控词汇中')
    replaces = candidate.get('replaces', [])
    if not isinstance(replaces, list) or any(not isinstance(item, str) for item in replaces):
        raise MemoryInputError('candidate.replaces 必须是字符串数组')

    return {
        'title': _text(candidate.get('title'), 'candidate.title'),
        'category': category,
        'scenario': _text(candidate.get('scenario'), 'candidate.scenario'),
        'practice': _text(candidate.get('practice'), 'candidate.practice'),
        'not_applicable': _text(candidate.get('not_applicable'), 'candidate.not_applicable', False),
        'otherwise': _text(candidate.get('otherwise'), 'candidate.otherwise', False),
        'replaces': [_text(item, 'candidate.replaces[]') for item in replaces],
        'note': _text(candidate.get('note'), 'candidate.note', False),
    }


def _entries(content):
    matches = list(H2_RE.finditer(content))
    result = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        block = content[match.end():end]
        fields = {}
        for line in block.splitlines():
            field = re.match(r'^-\s+([^：]+)：(.*)$', line)
            if field:
                fields[field.group(1).strip()] = field.group(2).strip()
        result.append({'heading': match.group(1), 'fields': fields})
    return result


def _fingerprint(candidate):
    return tuple(_normalize(candidate[key]) for key in ('category', 'title', 'scenario', 'practice'))


def _entry_fingerprint(entry):
    title = entry['heading'].split(' · ', 1)[1] if ' · ' in entry['heading'] else entry['heading']
    fields = entry['fields']
    return tuple(_normalize(value) for value in (
        fields.get('类别', ''), title, fields.get('场景', ''), fields.get('做法', ''),
    ))


def _render(candidate, date):
    lines = [
        f"## {date} · {candidate['title']}",
        f"- 类别：{candidate['category']}",
        f"- 场景：{candidate['scenario']}",
    ]
    if candidate['not_applicable']:
        lines.append(f"- 不适用：{candidate['not_applicable']}")
    lines.append(f"- 做法：{candidate['practice']}")
    if candidate['otherwise']:
        lines.append(f"- 否则：{candidate['otherwise']}")
    if candidate['replaces']:
        lines.append('- 取代：' + '；'.join(candidate['replaces']))
    if candidate['note']:
        lines.append(f"- 备注：{candidate['note']}")
    return '\n'.join(lines) + '\n'


def _active_count(entries):
    replaced = set()
    for entry in entries:
        value = entry['fields'].get('取代', '')
        replaced.update(item.strip() for item in value.split('；') if item.strip())
    return sum(1 for entry in entries if entry['heading'] not in replaced)


def process_request(request, memory_path, date=None):
    data = _validate_request(request)
    candidate = data['candidate']
    if candidate is None:
        return {
            'ok': True, 'status': 'NOT_APPLICABLE', 'reason': data['reason'],
            'entry_title': None, 'active_count': None, 'maintenance_required': False,
            'work_item_id': data['work_item_id'], 'run_id': data['run_id'],
        }

    parent = os.path.dirname(os.path.abspath(memory_path))
    os.makedirs(parent, exist_ok=True)
    with open(memory_path, 'a+', encoding='utf-8') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.seek(0)
        content = stream.read()
        existing = _entries(content)
        fingerprint = _fingerprint(candidate)
        duplicate = next((entry for entry in existing
                          if _entry_fingerprint(entry) == fingerprint), None)
        if duplicate is not None:
            active_count = _active_count(existing)
            return {
                'ok': True, 'status': 'DEDUP_NOOP', 'reason': data['reason'],
                'entry_title': duplicate['heading'], 'active_count': active_count,
                'maintenance_required': active_count > 40,
                'work_item_id': data['work_item_id'], 'run_id': data['run_id'],
            }

        headings = {entry['heading'] for entry in existing}
        missing_replacements = set(candidate['replaces']) - headings
        if missing_replacements:
            raise MemoryInputError('candidate.replaces 未找到目标：' + '；'.join(sorted(missing_replacements)))

        same_title = [entry['heading'] for entry in existing
                      if _normalize(entry['heading'].split(' · ', 1)[-1]) == _normalize(candidate['title'])]
        if same_title and not set(same_title).intersection(candidate['replaces']):
            raise MemoryInputError('同名经验已有不同内容；须用 replaces 明确取代关系')

        date = date or datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        entry = _render(candidate, date)
        if not content:
            stream.write(HEADER + '\n')
        elif not content.endswith('\n'):
            stream.write('\n')
        stream.write(entry + '\n')
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        updated = _entries(stream.read())
        expected_heading = f"{date} · {candidate['title']}"
        if expected_heading not in {item['heading'] for item in updated}:
            raise OSError('经验记忆追加后回读未找到新条目')
        active_count = _active_count(updated)
        return {
            'ok': True, 'status': 'APPENDED', 'reason': data['reason'],
            'entry_title': expected_heading, 'active_count': active_count,
            'maintenance_required': active_count > 40,
            'work_item_id': data['work_item_id'], 'run_id': data['run_id'],
        }


def _write_result(path, result):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.skill-memory-', suffix='.json', dir=parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description='处理一次 skill 经验记忆收尾')
    parser.add_argument('record', choices=['record'])
    parser.add_argument('--input', required=True, help='经验处理请求 JSON')
    parser.add_argument('--result', required=True, help='经验处理结果 JSON')
    parser.add_argument('--memory-file', default='过程文件/经验记忆.md')
    args = parser.parse_args(argv)

    result = None
    try:
        with open(args.input, encoding='utf-8') as stream:
            request = json.load(stream)
        result = process_request(request, args.memory_file)
    except (OSError, json.JSONDecodeError, MemoryInputError) as exc:
        result = {'ok': False, 'status': 'FAILED', 'reason': str(exc), 'entry_title': None}

    try:
        _write_result(args.result, result)
    except OSError as exc:
        result = {'ok': False, 'status': 'FAILED', 'reason': f'结果文件写入失败：{exc}',
                  'entry_title': None}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
