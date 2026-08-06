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


def publish_plan(plan, run_mode, collection, config_path=None, timeout=DEFAULT_TIMEOUT):
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
        if plan.get('verdict') in ('NEED-INFO', 'NEED-REVIEW') and isinstance(checklist, dict):
            # Redis 里 checklist 只保留下游确认所需：responsible（谁确认）+ items（确认什么）。
            # work_item/next 提升为顶层 Hash 字段；verdict/tag/generated_at_utc 与外层摘要重复，不进 checklist。
            # 完整 checklist 仍保留在 plan JSON / TFS 附件 待补充信息 / 执行审计。
            mapping['work_item'] = checklist.get('work_item', '')
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
            for stale in ('checklist', 'work_item', 'next', 'skip_reason'):
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
