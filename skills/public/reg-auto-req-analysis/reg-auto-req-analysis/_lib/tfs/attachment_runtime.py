#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为附件转换器准备隔离依赖并执行逐文件转换。"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import uuid

import attachment_converter as converter


PYTHON_VERSION = '3.12.13'
SKILL_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONVERTER_PATH = pathlib.Path(__file__).with_name('attachment_converter.py')
LOCK_FILES = {
    'python-markitdown': SKILL_ROOT / 'runtime' / 'requirements-attachments.lock',
}
MARKITDOWN_FORMATS = {'.doc', '.docx', '.xls', '.xlsx', '.pdf', '.pptx'}
MARKITDOWN_DIRECT_FORMATS = MARKITDOWN_FORMATS - {'.doc'}
LIBREOFFICE_FORMATS = {'.doc', '.xls'}
INSTALL_TIMEOUT_SECONDS = 900


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def _redact(value):
    text = str(value or '')
    text = re.sub(r'(?i)(https?://)([^/@\s]+)@', r'\1***@', text)
    text = re.sub(
        r'(?i)\b(TFS_PAT|PAT|TOKEN|PASSWORD|PROXY_AUTHORIZATION)=([^\s]+)',
        r'\1=***', text)
    return text[-1000:]


def _inventory_extensions(input_dir):
    root = pathlib.Path(input_dir)
    if not root.is_dir():
        raise ValueError(f'附件目录不存在: {input_dir}')
    return sorted({path.suffix.lower() for path in root.iterdir()
                   if path.is_file() and not path.is_symlink()})


def _requirement_groups(extensions):
    return ['python-markitdown'] if set(extensions) & MARKITDOWN_FORMATS else []


def _lock_digest(groups):
    digest = hashlib.sha256()
    for group in sorted(groups):
        path = LOCK_FILES[group]
        if not path.is_file():
            raise ValueError(f'附件依赖锁文件不存在: {path}')
        digest.update(group.encode('utf-8'))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _cache_key(groups):
    system = platform.system().lower()
    machine = platform.machine().lower()
    return f'{system}-{machine}-py{PYTHON_VERSION}-{_lock_digest(groups)[:16]}'


def _default_runtime_root():
    return pathlib.Path.cwd() / '过程文件' / '.runtime' / 'attachments'


def _venv_python(runtime_dir):
    return pathlib.Path(runtime_dir) / 'venv' / 'bin' / 'python'


def _python_version(executable):
    try:
        completed = subprocess.run(
            [str(executable), '--version'], check=False, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'Python\s+([0-9.]+)', completed.stdout or completed.stderr)
    return match.group(1) if completed.returncode == 0 and match else None


def _runtime_valid(runtime_dir, cache_key):
    runtime_dir = pathlib.Path(runtime_dir)
    manifest_path = runtime_dir / 'runtime-manifest.json'
    python = _venv_python(runtime_dir)
    if not manifest_path.is_file() or not python.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    return (manifest.get('status') == 'ready'
            and manifest.get('cache_key') == cache_key
            and manifest.get('python_version') == PYTHON_VERSION)


def _installation(group, manager, packages, command, timeout=INSTALL_TIMEOUT_SECONDS, env=None):
    started = _utc_now()
    record = {
        'group': group,
        'manager': manager,
        'packages': list(packages),
        'started_at': started,
    }
    try:
        completed = subprocess.run(
            list(command), check=False, capture_output=True, text=True,
            timeout=timeout, env=env)
        record.update({
            'status': 'installed' if completed.returncode == 0 else 'failed',
            'exit_code': completed.returncode,
            'error': '' if completed.returncode == 0 else _redact(
                completed.stderr or completed.stdout),
        })
    except subprocess.TimeoutExpired as exc:
        record.update({'status': 'failed', 'exit_code': None,
                       'error': f'安装超时（{timeout}s）：{_redact(exc.stderr or exc.stdout)}'})
    except OSError as exc:
        record.update({'status': 'failed', 'exit_code': None, 'error': _redact(exc)})
    record['finished_at'] = _utc_now()
    return record


def _system_install_commands(system, package_manager):
    if system == 'Darwin' and package_manager:
        return [[package_manager, 'install', '--cask', 'libreoffice']]
    if system == 'Linux' and package_manager:
        prefix = [] if os.geteuid() == 0 else [shutil.which('sudo') or '', '-n']
        if prefix and not prefix[0]:
            return []
        apt = [package_manager]
        return [prefix + apt + ['update'], prefix + apt + [
            'install', '-y', 'libreoffice-calc', 'libreoffice-writer', 'fonts-noto-cjk']]
    return []


def _ensure_libreoffice(extensions, installations, warnings):
    if not set(extensions) & LIBREOFFICE_FORMATS:
        return converter._soffice_info()
    current = converter._soffice_info()
    if current.get('ready'):
        return current
    system = platform.system()
    if system == 'Darwin':
        manager = shutil.which('brew')
        packages = ['libreoffice']
        manager_name = 'homebrew'
    elif system == 'Linux':
        manager = shutil.which('apt-get')
        packages = ['libreoffice-calc', 'libreoffice-writer', 'fonts-noto-cjk']
        manager_name = 'apt'
    else:
        manager = None
        packages = []
        manager_name = 'unsupported'
    commands = _system_install_commands(system, manager)
    if not commands:
        warnings.append(f'{system or "未知平台"} 缺少受支持的 LibreOffice 自动安装入口')
        return current
    for command in commands:
        record = _installation('libreoffice', manager_name, packages, command)
        installations.append(record)
        if record['status'] != 'installed':
            warnings.append('LibreOffice 自动安装失败；继续执行其它附件解析链')
            return converter._soffice_info()
    current = converter._soffice_info()
    if not current.get('ready'):
        warnings.append('LibreOffice 安装命令成功，但 soffice 版本验证失败')
    return current


def _find_python(runtime_root, installations, warnings):
    candidates = [os.environ.get('AUTO_REQ_PYTHON312', ''), shutil.which('python3.12')]
    for candidate in candidates:
        if candidate and _python_version(candidate) == PYTHON_VERSION:
            return candidate
    uv = shutil.which('uv')
    if not uv:
        system = platform.system()
        manager = shutil.which('brew') if system == 'Darwin' else shutil.which('apt-get')
        packages = ['uv']
        commands = []
        if system == 'Darwin' and manager:
            commands = [[manager, 'install', 'uv']]
        elif system == 'Linux' and manager:
            prefix = [] if os.geteuid() == 0 else [shutil.which('sudo') or '', '-n']
            if not prefix or prefix[0]:
                commands = [prefix + [manager, 'update'], prefix + [manager, 'install', '-y', 'uv']]
        for command in commands:
            record = _installation('python-bootstrap', 'homebrew' if system == 'Darwin' else 'apt',
                                   packages, command)
            installations.append(record)
            if record['status'] != 'installed':
                break
        uv = shutil.which('uv')
    if not uv:
        warnings.append(f'未找到 Python {PYTHON_VERSION} 或 uv，无法创建隔离运行时')
        return None
    install_dir = pathlib.Path(runtime_root) / 'python'
    install_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment['UV_PYTHON_INSTALL_DIR'] = str(install_dir)
    record = _installation(
        'python-runtime', 'uv', [f'python=={PYTHON_VERSION}'],
        [uv, 'python', 'install', PYTHON_VERSION, '--install-dir', str(install_dir), '--no-bin'],
        env=environment)
    installations.append(record)
    if record['status'] != 'installed':
        warnings.append(f'Python {PYTHON_VERSION} 自动安装失败')
        return None
    try:
        completed = subprocess.run(
            [uv, 'python', 'find', PYTHON_VERSION, '--managed-python', '--no-project'],
            check=False, capture_output=True, text=True, timeout=30, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    candidate = completed.stdout.strip() if completed and completed.returncode == 0 else ''
    if candidate and _python_version(candidate) == PYTHON_VERSION:
        return candidate
    warnings.append(f'Python {PYTHON_VERSION} 安装后未找到可执行文件')
    return None


@contextlib.contextmanager
def _cache_lock(runtime_root, cache_key):
    pathlib.Path(runtime_root).mkdir(parents=True, exist_ok=True)
    lock_path = pathlib.Path(runtime_root) / f'.{cache_key}.lock'
    with lock_path.open('a+', encoding='utf-8') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _smoke_runtime(python, input_dir, groups, environment):
    completed = subprocess.run(
        [str(python), str(CONVERTER_PATH), '--precheck', '--input-dir', str(input_dir)],
        check=False, capture_output=True, text=True, timeout=60, env=environment)
    try:
        readiness = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'附件运行时烟测未返回 JSON：{_redact(completed.stderr)}') from exc
    if 'python-markitdown' in groups and not readiness.get('markitdown', {}).get('ready'):
        raise RuntimeError('MarkItDown Python 依赖烟测失败')
    blocked = set(readiness.get('blocked_formats', [])) & MARKITDOWN_DIRECT_FORMATS
    if blocked:
        raise RuntimeError(f'MarkItDown 格式烟测失败：{sorted(blocked)}')
    return readiness


def _build_runtime(stage, base_python, groups, input_dir, cache_key, installations, environment):
    venv_dir = pathlib.Path(stage) / 'venv'
    completed = subprocess.run(
        [str(base_python), '-m', 'venv', str(venv_dir)], check=False,
        capture_output=True, text=True, timeout=120)
    if completed.returncode != 0:
        raise RuntimeError(f'创建附件虚拟环境失败：{_redact(completed.stderr or completed.stdout)}')
    python = venv_dir / 'bin' / 'python'
    for group in groups:
        lock_path = LOCK_FILES[group]
        record = _installation(
            group, 'pip', ['markitdown[xls,xlsx,pdf,docx,pptx]==0.1.7'],
            [str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
             '--require-hashes', '--no-deps', '-r', str(lock_path)],
            env=environment)
        installations.append(record)
        if record['status'] != 'installed':
            raise RuntimeError(f'{group} 锁定依赖安装失败：{record["error"]}')
    readiness = _smoke_runtime(python, input_dir, groups, environment)
    manifest = {
        'status': 'ready',
        'cache_key': cache_key,
        'python_version': PYTHON_VERSION,
        'groups': groups,
        'lock_sha256': _lock_digest(groups),
        'created_at': _utc_now(),
        'smoke': readiness,
    }
    (pathlib.Path(stage) / 'runtime-manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def prepare_runtime(input_dir, runtime_root=None):
    extensions = _inventory_extensions(input_dir)
    groups = _requirement_groups(extensions)
    runtime_root = pathlib.Path(runtime_root or _default_runtime_root()).resolve()
    installations = []
    warnings = []
    soffice = _ensure_libreoffice(extensions, installations, warnings)
    environment = os.environ.copy()
    environment['AUTO_REQ_RUNTIME_MODE'] = 'managed-host' if groups else 'builtin-only'
    if soffice.get('path'):
        environment['AUTO_REQ_SOFFICE'] = soffice['path']

    if not groups:
        readiness = converter.precheck(input_dir)
        readiness.update({
            'install_required': [], 'installations': installations,
            'runtime_dir': None, 'runtime_cache_key': None,
            'runtime_mode': 'builtin-only', 'warnings': warnings + readiness.get('warnings', []),
        })
        return {'python': sys.executable, 'environment': environment, 'preflight': readiness}

    cache_key = _cache_key(groups)
    runtime_dir = runtime_root / cache_key
    install_required = [] if _runtime_valid(runtime_dir, cache_key) else list(groups)
    with _cache_lock(runtime_root, cache_key):
        if not _runtime_valid(runtime_dir, cache_key):
            if runtime_dir.exists():
                shutil.rmtree(runtime_dir)
            base_python = _find_python(runtime_root, installations, warnings)
            if base_python:
                stage = runtime_root / f'.{cache_key}.tmp-{uuid.uuid4().hex}'
                try:
                    stage.mkdir(parents=True)
                    _build_runtime(stage, base_python, groups, input_dir, cache_key,
                                   installations, environment)
                    os.replace(stage, runtime_dir)
                except Exception as exc:
                    warnings.append(_redact(exc))
                    if stage.exists():
                        shutil.rmtree(stage)
            else:
                warnings.append('隔离 Python 不可用；将继续执行宿主内置解析链')

    if _runtime_valid(runtime_dir, cache_key):
        python = str(_venv_python(runtime_dir))
        readiness = _smoke_runtime(python, input_dir, groups, environment)
        mode = 'managed-host'
    else:
        python = sys.executable
        readiness = converter.precheck(input_dir)
        mode = environment['AUTO_REQ_RUNTIME_MODE'] = 'builtin-only'
    readiness.update({
        'install_required': install_required,
        'installations': installations,
        'runtime_dir': str(runtime_dir) if runtime_dir.exists() else None,
        'runtime_cache_key': cache_key,
        'runtime_mode': mode,
        'warnings': warnings + readiness.get('warnings', []),
    })
    return {'python': python, 'environment': environment, 'preflight': readiness}


def convert(input_dir, output_dir, max_bytes, runtime_root=None):
    prepared = prepare_runtime(input_dir, runtime_root)
    completed = subprocess.run(
        [prepared['python'], str(CONVERTER_PATH), '--input-dir', str(input_dir),
         '--output-dir', str(output_dir), '--max-bytes', str(max_bytes)],
        check=False, capture_output=True, text=True, timeout=3600,
        env=prepared['environment'])
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError:
        output = {'total': 0, 'converted': 0, 'needs_read': [], 'skipped': 0,
                  'errors': 1, 'files': [],
                  'error': f'附件转换器未返回 JSON：{_redact(completed.stderr or completed.stdout)}'}
    output['ok'] = completed.returncode == 0 and not output.get('error')
    output['preflight'] = prepared['preflight']
    return output


def main():
    parser = argparse.ArgumentParser(description='TFS 附件隔离运行时与自动依赖修复入口')
    subparsers = parser.add_subparsers(dest='command', required=True)
    for name in ('precheck', 'convert'):
        command = subparsers.add_parser(name)
        command.add_argument('--input-dir', required=True)
        command.add_argument('--runtime-root')
        if name == 'convert':
            command.add_argument('--output-dir', required=True)
            command.add_argument('--max-bytes', type=int, default=converter.DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    try:
        if args.command == 'precheck':
            result = prepare_runtime(args.input_dir, args.runtime_root)['preflight']
        else:
            if args.max_bytes <= 0:
                raise ValueError('max_bytes 必须为正整数')
            result = convert(args.input_dir, args.output_dir, args.max_bytes, args.runtime_root)
    except Exception as exc:
        result = {'ok': False, 'error': _redact(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get('ok') else 1)


if __name__ == '__main__':
    main()
