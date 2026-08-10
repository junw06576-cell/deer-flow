#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Redis 结果存储客户端（仅 Python 标准库 socket + RESP2）。

把合并后的 auto-req-analysis 执行计划作为「结果摘要」写入 Redis Hash，按 collection + 工作项覆盖式存最新，
供下游（TFS-BUDDY / DeerFlow / 看板 / 调试）用 HGETALL 一步查询某工作项的最新质控结果。

零第三方依赖，与 _lib 纯标准库约束一致（不引入 redis-py、不加 requirements）。
连接或写入失败 → 返回 {ok:false, reason}，**永不抛异常、不阻断 pipeline**（降级，
与 KB / attachment-evidence 降级风格一致）。
"""
import json
import os
import socket
import sys
from urllib.parse import urlparse

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 6379
DEFAULT_DB = 0
DEFAULT_TIMEOUT = 3.0
DEFAULT_CLIENT_NAME = 'auto-dev'
PLAN_KEY_PREFIX = 'auto-req:qc:plan'
IDS_KEY_PREFIX = 'auto-req:qc:ids'
DATABASE_KNOWLEDGE_TOOLS = frozenset({
    'search_knowledge', 'get_table_knowledge', 'traverse_graph', 'inspect_node',
})
SOURCE_CODE_TOOLS = frozenset({'search_source', 'search_symbol'})
REQUIREMENTS_HISTORY_TOOLS = frozenset({
    'get_requirements_summary', 'get_related_work_items', 'search_requirements', 'get_work_item',
})
HUMAN_SOURCE_BOUNDARIES = {
    '代码图谱': '仅证明代码图谱中的模块、入口或关系，不代表现场已部署。',
    '源码': '仅证明受控仓库当前可见源码内容，不代表现场部署版本。',
    '数据库知识': '仅证明数据库知识图谱命中的结构或关系；覆盖不完整时不能据此判断不存在。',
    '产品 Wiki': '用于说明业务规则或模块语义；与已核验实现冲突时，以实现证据为准。',
    '历史需求': '仅证明历史需求记录；不代表当前功能仍有效或已经部署。',
}
CONFIRMED_FINDING_STATES = frozenset({'已证实', 'wiki-确认'})


# ---------------- config ----------------
def _parse_redis_url(url):
    try:
        p = urlparse(url)
        if p.scheme not in ('redis', 'rediss'):
            return None
        host = p.hostname or DEFAULT_HOST
        port = p.port or DEFAULT_PORT
        password = p.password or ''
        db = DEFAULT_DB
        path = p.path.lstrip('/')
        if path.isdigit():
            db = int(path)
        return host, port, db, password
    except Exception:
        return None


def load_redis_config(config_path=None):
    """读 tfs-config.json 的可选 redis 段；环境变量优先；缺省本地无认证。

    优先级：REDIS_URL > REDIS_HOST/PORT/DB/PASSWORD > 配置 redis.* > 默认。
    不写回配置；redis 段缺失或异常 → 用默认，不报错（redis 是可选功能）。
    """
    host, port, db, password, ttl = DEFAULT_HOST, DEFAULT_PORT, DEFAULT_DB, '', 0
    client_name = DEFAULT_CLIENT_NAME
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f).get('redis', {}) or {}
            host = cfg.get('host', host)
            port = int(cfg.get('port', port))
            db = int(cfg.get('db', db))
            password = cfg.get('password', password) or ''
            ttl = int(cfg.get('ttl_seconds', ttl) or 0)
            client_name = cfg.get('client_name', client_name) or client_name
        except (ValueError, OSError, json.JSONDecodeError):
            pass
    url = os.environ.get('REDIS_URL')
    if url:
        parsed = _parse_redis_url(url)
        if parsed:
            host, port, db, password = parsed
    host = os.environ.get('REDIS_HOST', host)
    if os.environ.get('REDIS_PORT'):
        port = int(os.environ['REDIS_PORT'])
    if os.environ.get('REDIS_DB'):
        db = int(os.environ['REDIS_DB'])
    if os.environ.get('REDIS_PASSWORD'):
        password = os.environ['REDIS_PASSWORD']
    if os.environ.get('REDIS_CLIENT_NAME'):
        client_name = os.environ['REDIS_CLIENT_NAME']
    return {'host': host, 'port': port, 'db': db, 'password': password,
            'ttl_seconds': ttl, 'client_name': client_name}


# ---------------- RESP2 编解码 ----------------
def encode_command(*args):
    """将命令参数编码为 RESP2 字节串。"""
    parts = [f'*{len(args)}\r\n'.encode()]
    for a in args:
        b = a if isinstance(a, bytes) else str(a).encode('utf-8')
        parts.append(f'${len(b)}\r\n'.encode() + b + b'\r\n')
    return b''.join(parts)


class _Connection:
    """单次连接上下文：建连 → (AUTH/SELECT) → 顺序执行多命令 → 关闭。"""

    def __init__(self, cfg, timeout=DEFAULT_TIMEOUT):
        self.cfg = cfg
        self.timeout = timeout
        self.sock = None
        self.buf = b''

    def __enter__(self):
        self.sock = socket.create_connection((self.cfg['host'], self.cfg['port']), self.timeout)
        self.sock.settimeout(self.timeout)
        if self.cfg.get('password'):
            self.execute('AUTH', self.cfg['password'])
        if self.cfg.get('db'):
            self.execute('SELECT', self.cfg['db'])
        if self.cfg.get('client_name'):
            try:
                self.execute('CLIENT', 'SETNAME', self.cfg['client_name'])
            except Exception:
                pass  # 连接名为辅助标识，设置失败不影响功能
        return self

    def __exit__(self, *exc):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass

    def _readline(self):
        while b'\r\n' not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError('redis 连接已关闭')
            self.buf += chunk
        line, self.buf = self.buf.split(b'\r\n', 1)
        return line

    def _read_exact(self, n):
        while len(self.buf) < n + 2:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError('redis 连接已关闭')
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n + 2:]
        return data

    def _read_reply(self):
        line = self._readline()
        typ, rest = line[:1], line[1:]
        if typ == b'+':
            return rest.decode('utf-8', errors='replace')
        if typ == b'-':
            raise RuntimeError('redis 错误: ' + rest.decode('utf-8', errors='replace'))
        if typ == b':':
            return int(rest)
        if typ == b'$':
            n = int(rest)
            return None if n == -1 else self._read_exact(n).decode('utf-8', errors='replace')
        if typ == b'*':
            n = int(rest)
            return None if n == -1 else [self._read_reply() for _ in range(n)]
        raise RuntimeError(f'未知 RESP 类型: {line!r}')

    def execute(self, *args):
        self.sock.sendall(encode_command(*args))
        return self._read_reply()


# ---------------- 对外命令 ----------------
def ping(config_path=None, timeout=DEFAULT_TIMEOUT):
    """探活，返回 bool。"""
    try:
        with _Connection(load_redis_config(config_path), timeout) as c:
            return c.execute('PING') == 'PONG'
    except Exception:
        return False


def hgetall(key, config_path=None, timeout=DEFAULT_TIMEOUT):
    """HGETALL → dict（失败抛异常，供调试/单测用）。"""
    with _Connection(load_redis_config(config_path), timeout) as c:
        flat = c.execute('HGETALL', key)
    pairs = flat or []
    return {pairs[i]: pairs[i + 1] for i in range(0, len(pairs) - 1, 2)}


def plan_key(collection, work_item_id):
    """返回按 TFS collection 隔离的计划缓存 Key。"""
    return f'{PLAN_KEY_PREFIX}:{collection}:{work_item_id}'


def ids_key(collection):
    """返回按 TFS collection 隔离的工作项索引 Key。"""
    return f'{IDS_KEY_PREFIX}:{collection}'


def _is_database_knowledge(value):
    """兼容识别数据库图谱来源；新计划优先显式 source_type，旧计划回退工具名。"""
    if not isinstance(value, str):
        return False
    return any(tool in value for tool in DATABASE_KNOWLEDGE_TOOLS)


def _is_source_code_knowledge(value):
    """识别受控源码 MCP 来源，避免与 GitNexus 代码图谱混投影。"""
    return isinstance(value, str) and value in SOURCE_CODE_TOOLS


def _acquisition_attempted(plan, source):
    """只有证据源本轮实际查询过，才把其不可用/覆盖不足展示给人。"""
    acquisition = plan.get('evidence_acquisition')
    if not isinstance(acquisition, dict):
        return False
    status = acquisition.get(source)
    if not isinstance(status, dict):
        return False
    return (status.get('query_status') not in (None, 'SKIPPED')
            or status.get('stop_reason') not in (None, 'not_applicable'))


def _append_human_finding(items, source, finding, fallback_conclusion, fallback_evidence):
    """把来源 finding 变成人可直接阅读的一条佐证；历史计划使用保守回退。"""
    conclusion = finding.get('conclusion') or fallback_conclusion
    evidence = finding.get('evidence') or fallback_evidence
    if not isinstance(conclusion, str) or not conclusion.strip():
        return
    if not isinstance(evidence, str) or not evidence.strip():
        evidence = '原始计划未提供可展示的证据定位'
    item = {
        'source': source,
        'status': finding.get('state') or '未确认',
        'conclusion': conclusion.strip(),
        'evidence': evidence.strip(),
        'boundary': (finding.get('boundary') or HUMAN_SOURCE_BOUNDARIES[source]).strip(),
    }
    if source == '历史需求' and finding.get('maturity'):
        item['maturity'] = finding['maturity']
    items.append(item)


def _human_source_status(ready, findings, used=False, required=False):
    """把证据源机器状态压缩成人能直接理解的一行。"""
    if findings:
        if ready is False:
            return f'已命中（{len(findings)} 条），但来源未就绪'
        return f'已命中（{len(findings)} 条）'
    if ready is False:
        return '需核验但未就绪' if required else '未就绪'
    if used:
        return '已查询，无可展示佐证'
    return '本轮未使用'


def _finish_human_view(projection, human_findings, coverage_notes, source_status):
    """只保留人读总览、来源状态与佐证清单；完整机器明细留在计划和审计。"""
    confirmed = sum(
        1 for finding in human_findings if finding['status'] in CONFIRMED_FINDING_STATES)
    pending = len(human_findings) - confirmed
    route = projection.get('route') or {}
    product = route.get('product_name') or route.get('area') or '当前需求'
    if human_findings:
        parts = []
        if confirmed:
            parts.append(f'{confirmed} 条已确认')
        if pending:
            parts.append(f'{pending} 条待核实')
        text = f'{product}：' + '，'.join(parts)
    else:
        text = f'{product}：暂无可展示佐证'
    human_summary = {'text': text}
    if coverage_notes:
        human_summary['coverage'] = coverage_notes
    projection.clear()
    projection['summary'] = human_summary
    projection['source_status'] = source_status
    projection['evidence_list'] = human_findings


def _knowledge_summary(plan):
    """投影精简的人读佐证总览与清单。

    Redis 不重复保存 route/tools/raw findings；完整机器证据仍在计划和执行审计中。
    新计划提供 conclusion/evidence/boundary，历史计划按原定位字段保守回退。
    """
    summary = {}
    human_findings = []
    coverage_notes = []
    legacy_history_findings = []
    tools = []
    code_findings = []
    source_findings = []
    source_tools = []
    database_findings = []
    database_tools = []
    route = plan.get('knowledge_route')
    if isinstance(route, dict) and route:
        summary['route'] = {
            'status': route.get('status'),
            'area': route.get('area'),
            'product_id': route.get('product_id'),
            'product_name': route.get('product_name'),
            'profile_version': route.get('profile_version'),
            'servers': route.get('servers') or {},
        }
    kb = plan.get('kb')
    if isinstance(kb, dict) and kb:
        findings = [f for f in (kb.get('findings') or []) if isinstance(f, dict)]
        tools = [tool for tool in (kb.get('tools_used') or []) if isinstance(tool, str)]

        def is_database_finding(finding):
            source_type = finding.get('source_type')
            if source_type in ('database', 'db'):
                return True
            if source_type in ('code', 'code_graph'):
                return False
            return _is_database_knowledge(finding.get('source_tool'))

        legacy_history_findings = [
            f for f in findings if f.get('source_tool') in REQUIREMENTS_HISTORY_TOOLS
        ]
        source_findings = [f for f in findings
                           if _is_source_code_knowledge(f.get('source_tool'))
                           and f not in legacy_history_findings]
        code_findings = [f for f in findings
                         if not is_database_finding(f)
                         and not _is_source_code_knowledge(f.get('source_tool'))]
        code_findings = [f for f in code_findings if f not in legacy_history_findings]
        database_findings = [
            f for f in findings if is_database_finding(f) and f not in legacy_history_findings
        ]
        source_tools = [tool for tool in tools if _is_source_code_knowledge(tool)]
        database_tools = [tool for tool in tools if _is_database_knowledge(tool)]

        for finding in code_findings:
            entity = finding.get('entity', '')
            _append_human_finding(
                human_findings, '代码图谱', finding, entity,
                f"{finding.get('source_tool') or '代码图谱'}：{entity}")
        if kb.get('ready') is False:
            coverage_notes.append('代码图谱：未就绪，本轮无完整代码图谱佐证。')

        has_source = ('source_ready' in kb or 'source_required' in kb
                      or source_tools or source_findings)
        if has_source:
            for finding in source_findings:
                entity = finding.get('entity', '')
                _append_human_finding(
                    human_findings, '源码', finding, entity,
                    f"{finding.get('source_tool') or '源码检索'}：{entity}")
            if kb.get('source_required') is True and kb.get('source_ready') is False:
                coverage_notes.append('源码：本轮需要源码核验，但源码服务未就绪。')

        acquisition = plan.get('evidence_acquisition')
        database_status = (acquisition.get('db_knowledge')
                           if isinstance(acquisition, dict) else None)
        has_database = bool(database_tools or database_findings
                            or _acquisition_attempted(plan, 'db_knowledge'))
        if has_database:
            database_ready = kb.get('database_ready')
            if not isinstance(database_ready, bool):
                if isinstance(database_status, dict):
                    database_ready = database_status.get('availability') == 'READY'
                else:
                    database_ready = bool(database_tools or database_findings)
            for finding in database_findings:
                entity = finding.get('entity', '')
                _append_human_finding(
                    human_findings, '数据库知识', finding, entity,
                    f"{finding.get('source_tool') or '数据库知识图谱'}：{entity}")
            if database_ready is False:
                coverage_notes.append('数据库知识：未就绪，本轮无完整数据库佐证。')
    wiki = plan.get('wiki')
    wiki_findings = []
    if isinstance(wiki, dict) and wiki:
        wiki_findings = [
            finding for finding in (wiki.get('findings') or []) if isinstance(finding, dict)
        ]
        for finding in wiki_findings:
            entity = finding.get('entity', '')
            source = finding.get('source', '')
            _append_human_finding(
                human_findings, '产品 Wiki', finding, entity,
                f'{source}：{entity}' if source else entity)
        wiki_relevant = bool(wiki.get('findings') or wiki.get('modules_matched')
                             or _acquisition_attempted(plan, 'wiki'))
        if wiki.get('ready') is False and wiki_relevant:
            coverage_notes.append('产品 Wiki：未就绪或未覆盖，本轮 Wiki 佐证受限。')
    tfs_req = plan.get('tfs_requirements')
    history_findings = []
    if isinstance(tfs_req, dict) and tfs_req:
        history_findings.extend(
            f for f in (tfs_req.get('findings') or []) if isinstance(f, dict))
    history_findings.extend(legacy_history_findings)
    if (isinstance(tfs_req, dict) and tfs_req) or history_findings:
        for finding in history_findings:
            fact = finding.get('fact') or finding.get('entity', '')
            work_item_id = finding.get('work_item_id')
            locator = f'需求 {work_item_id}：{fact}' if work_item_id else fact
            _append_human_finding(human_findings, '历史需求', finding, fact, locator)
        history_relevant = bool(history_findings
                                or isinstance(tfs_req, dict) and tfs_req.get('tools_used')
                                or _acquisition_attempted(plan, 'tfs_requirements'))
        if (isinstance(tfs_req, dict) and tfs_req.get('ready') is False
                and history_relevant):
            coverage_notes.append('历史需求：未就绪，本轮历史佐证受限。')
    database_ready = kb.get('database_ready') if isinstance(kb, dict) else None
    if not isinstance(database_ready, bool):
        acquisition = plan.get('evidence_acquisition')
        database_status = (acquisition.get('db_knowledge')
                           if isinstance(acquisition, dict) else None)
        if isinstance(database_status, dict) and database_status.get('availability'):
            database_ready = database_status.get('availability') == 'READY'
    code_tools = [tool for tool in tools
                  if not _is_database_knowledge(tool)
                  and not _is_source_code_knowledge(tool)
                  and tool not in REQUIREMENTS_HISTORY_TOOLS]
    source_status = {
        '历史需求': _human_source_status(
            tfs_req.get('ready') if isinstance(tfs_req, dict) else None,
            history_findings,
            bool((tfs_req.get('tools_used') if isinstance(tfs_req, dict) else None)
                 or _acquisition_attempted(plan, 'tfs_requirements'))),
        '产品 Wiki': _human_source_status(
            wiki.get('ready') if isinstance(wiki, dict) else None,
            wiki_findings,
            bool((wiki.get('modules_matched') if isinstance(wiki, dict) else None)
                 or _acquisition_attempted(plan, 'wiki'))),
        '代码图谱': _human_source_status(
            kb.get('ready') if isinstance(kb, dict) else None,
            code_findings,
            bool(code_tools or _acquisition_attempted(plan, 'gitnexus'))),
        '源码': _human_source_status(
            kb.get('source_ready') if isinstance(kb, dict) else None,
            source_findings,
            bool(source_tools),
            isinstance(kb, dict) and kb.get('source_required') is True),
        '数据库知识': _human_source_status(
            database_ready,
            database_findings,
            bool(database_tools or _acquisition_attempted(plan, 'db_knowledge'))),
    }
    has_context = any(
        isinstance(plan.get(key), dict) and bool(plan.get(key))
        for key in ('knowledge_route', 'kb', 'wiki', 'tfs_requirements')
    )
    if human_findings or coverage_notes or has_context:
        _finish_human_view(summary, human_findings, coverage_notes, source_status)
    else:
        summary.clear()
    return summary


def publish_plan(plan, run_mode, collection, config_path=None, timeout=DEFAULT_TIMEOUT,
                 analysis_description_html='', work_item=''):
    """写入计划结果摘要 + SADD collection 索引 + 可选 EXPIRE。

    返回 {ok, key, fields} 或 {ok:false, reason}。**永不抛异常**。
    """
    try:
        wid = str(plan.get('work_item_id', ''))
        if not wid:
            return {'ok': False, 'reason': 'plan 缺 work_item_id'}
        if not isinstance(collection, str) or not collection:
            return {'ok': False, 'reason': '缺少 TFS collection'}
        cfg = load_redis_config(config_path)
        key = plan_key(collection, wid)
        checklist = plan.get('checklist') or {}
        generated_at = checklist.get('generated_at_utc', '') if isinstance(checklist, dict) else ''
        if not generated_at:
            generated_at = plan.get('generated_at_utc', '')
        mapping = {
            'run_id': plan.get('run_id', ''),
            'verdict': plan.get('verdict', ''),
            'tags': ','.join(plan.get('tags', [])),
            'state_to': plan.get('state_to') or '',
            'generated_at_utc': generated_at,
            'run_mode': run_mode,
        }
        # 分析者描述正文（与写入 TFS System.Description 的内容同源）；仅分析终局由 pipeline 传入。
        if analysis_description_html:
            mapping['analysis_description'] = analysis_description_html
        # 人读佐证清单；route/tools/ready/raw findings 等机器明细留在计划与审计。
        knowledge = _knowledge_summary(plan)
        if knowledge:
            mapping['knowledge'] = json.dumps(knowledge, ensure_ascii=False)
        # 工作项「<id> <标题>」：所有终局统一写入（apply_plan 传 live 抓取值）；直调回退 checklist.work_item。
        work_item_label = work_item or (checklist.get('work_item', '') if isinstance(checklist, dict) else '')
        if work_item_label:
            mapping['work_item'] = work_item_label
        if plan.get('verdict') in ('NEED-INFO', 'NEED-REVIEW') and isinstance(checklist, dict):
            # Redis 里 checklist 只保留下游确认所需：responsible（谁确认）+ items（确认什么）。
            # next 提升为顶层 Hash 字段；work_item 已由上方统一写入；verdict/tag/generated_at_utc 与外层摘要重复，不进 checklist。
            # 完整 checklist 仍保留在 plan JSON / TFS 附件 待补充信息 / 执行审计。
            mapping['next'] = checklist.get('next', '')
            redis_checklist = {k: v for k, v in checklist.items() if k in ('responsible', 'items')}
            mapping['checklist'] = json.dumps(redis_checklist, ensure_ascii=False)
        elif plan.get('verdict') == 'SKIP-ANALYSIS':
            mapping['skip_reason'] = plan.get('skip_reason', '')
        flat = []
        for k, v in mapping.items():
            flat.extend([k, v])
        with _Connection(cfg, timeout) as c:
            c.execute('HSET', key, *flat)
            for stale in ('checklist', 'work_item', 'next', 'skip_reason',
                          'analysis_description', 'knowledge'):
                if stale not in mapping:
                    c.execute('HDEL', key, stale)
            c.execute('SADD', ids_key(collection), wid)
            if cfg.get('ttl_seconds'):
                c.execute('EXPIRE', key, cfg['ttl_seconds'])
        return {'ok': True, 'key': key, 'fields': len(mapping)}
    except Exception as exc:
        return {'ok': False, 'reason': f'{type(exc).__name__}: {exc}'[:300]}


def main():
    import argparse
    ap = argparse.ArgumentParser(description='Redis 结果存储客户端（仅标准库）')
    ap.add_argument('--config', default=os.path.join(os.path.dirname(__file__), 'tfs-config.json'))
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('ping', help='探活')
    g = sub.add_parser('hgetall', help='按 collection 查询某工作项最新结果')
    g.add_argument('collection')
    g.add_argument('id')
    args = ap.parse_args()
    if args.command == 'ping':
        ok = ping(args.config)
        print(json.dumps({'ok': ok}, ensure_ascii=False))
        sys.exit(0 if ok else 1)
    if args.command == 'hgetall':
        key = plan_key(args.collection, args.id)
        try:
            data = hgetall(key, args.config)
        except Exception as exc:
            print(json.dumps({'ok': False, 'key': key, 'reason': str(exc)[:200]}, ensure_ascii=False))
            return
        print(json.dumps({'ok': True, 'key': key, 'data': data}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
