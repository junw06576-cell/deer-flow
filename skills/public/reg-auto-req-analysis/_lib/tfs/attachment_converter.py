#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 TFS 工作项附件转换为 Markdown。

MarkItDown 0.1.7 是 DOCX/XLS/XLSX/PDF/PPTX 的优先解析器；DOCX/XLSX 可
直接走标准库兜底，旧版 DOC/XLS 先经 LibreOffice 转为 OOXML 后也可走兜底。
已有 PyMuPDF 时可作为 PDF 逐文件降级链。依赖准备由 attachment_runtime.py
负责，本模块只做能力探测和逐文件转换。
"""
import argparse
import csv
import importlib.metadata
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET


DEFAULT_MAX_BYTES = 20 * 1024 * 1024
OFFICE_TIMEOUT_SECONDS = 60
EXPECTED_MARKITDOWN_VERSION = '0.1.7'
TEXT_EXTENSIONS = {'.md', '.txt', '.log', '.yaml', '.yml'}
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}
UNSUPPORTED_EXTENSIONS = {'.ppt', '.zip', '.rar', '.7z', '.exe'}
MARKITDOWN_MODULES = {
    'core': ('markitdown',),
    'docx': ('mammoth', 'lxml'),
    'xlsx': ('openpyxl', 'pandas'),
    'xls': ('pandas', 'xlrd'),
    'pdf': ('pdfminer', 'pdfplumber'),
    'pptx': ('pptx',),
}
OFFICE_EXTENSIONS = {'.doc', '.docx', '.xls', '.xlsx'}


class _Unsupported(Exception):
    """可选依赖缺失等导致的「无法解析」——映射为 unsupported 而非 error。"""


class _MarkItDownFailure(Exception):
    """MarkItDown 已安装但未能转换当前文件，可按格式进入受控兜底。"""


W_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
SHEET_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
PKG_REL_NS = '{http://schemas.openxmlformats.org/package/2006/relationships}'


def _module_ready(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _soffice_executable():
    configured = os.environ.get('AUTO_REQ_SOFFICE', '').strip()
    if configured and os.path.isfile(configured):
        return configured
    discovered = shutil.which('soffice')
    if discovered:
        return discovered
    candidates = []
    if sys.platform == 'darwin':
        candidates.append('/Applications/LibreOffice.app/Contents/MacOS/soffice')
    return next((value for value in candidates if value and os.path.isfile(value)), None)


def _soffice_info():
    executable = _soffice_executable()
    if not executable:
        return {'ready': False, 'path': None, 'version': None}
    try:
        completed = subprocess.run(
            [executable, '--version'], check=False, capture_output=True, text=True,
            timeout=10)
        version = (completed.stdout or completed.stderr).strip().splitlines()
        return {'ready': completed.returncode == 0, 'path': executable,
                'version': version[0] if version else None}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'ready': False, 'path': executable, 'version': None, 'error': str(exc)[:300]}


def _requested_extensions(input_dir=None):
    if input_dir is None:
        return sorted(OFFICE_EXTENSIONS)
    if not os.path.isdir(input_dir):
        return []
    return sorted({os.path.splitext(name)[1].lower()
                   for name in os.listdir(input_dir)
                   if os.path.isfile(os.path.join(input_dir, name))})


def precheck(input_dir=None):
    """按本次实际附件检查可用转换链；固定镜像状态只作为运行来源审计。"""
    modules = {
        group: {name: _module_ready(name) for name in names}
        for group, names in MARKITDOWN_MODULES.items()
    }
    markitdown_ready = all(modules['core'].values())
    try:
        markitdown_version = importlib.metadata.version('markitdown') if markitdown_ready else None
    except importlib.metadata.PackageNotFoundError:
        markitdown_version = None
        markitdown_ready = False
    version_ready = markitdown_version == EXPECTED_MARKITDOWN_VERSION
    markitdown_ready = markitdown_ready and version_ready
    soffice = _soffice_info()
    runtime_image = os.environ.get('AUTO_REQ_RUNTIME_IMAGE', '').strip()
    markitdown_formats = {
        '.docx': markitdown_ready and all(modules['docx'].values()),
        '.xlsx': markitdown_ready and all(modules['xlsx'].values()),
        '.xls': markitdown_ready and all(modules['xls'].values()),
    }
    fitz_ready = _module_ready('fitz')
    markitdown_pdf = markitdown_ready and all(modules['pdf'].values())
    markitdown_pptx = markitdown_ready and all(modules['pptx'].values())
    capabilities = {
        '.docx': {'ready': True, 'chains': ['markitdown', 'builtin-fallback']
                  if markitdown_formats['.docx'] else ['builtin-fallback']},
        '.xlsx': {'ready': True, 'chains': ['markitdown', 'builtin-fallback']
                  if markitdown_formats['.xlsx'] else ['builtin-fallback']},
        '.xls': {'ready': markitdown_formats['.xls'] or soffice['ready'], 'chains': []},
        '.doc': {'ready': soffice['ready'], 'chains': []},
        '.pdf': {'ready': markitdown_pdf or fitz_ready, 'chains': []},
        '.pptx': {'ready': markitdown_pptx, 'chains': ['markitdown'] if markitdown_pptx else []},
    }
    if markitdown_pdf:
        capabilities['.pdf']['chains'].append('markitdown')
    if fitz_ready:
        capabilities['.pdf']['chains'].append('pymupdf')
    if markitdown_formats['.xls']:
        capabilities['.xls']['chains'].append('markitdown')
    if soffice['ready']:
        capabilities['.xls']['chains'].append('libreoffice+ooxml-parser')
        capabilities['.doc']['chains'].append('libreoffice+ooxml-parser')
    for extension in sorted(TEXT_EXTENSIONS | {'.json', '.csv', '.html', '.xml'}):
        capabilities[extension] = {'ready': True, 'chains': ['builtin-fallback']}
    for extension in sorted(IMAGE_EXTENSIONS):
        capabilities[extension] = {'ready': True, 'chains': ['visual-read']}
    for extension in sorted(UNSUPPORTED_EXTENSIONS):
        capabilities[extension] = {'ready': False, 'chains': []}

    requested = _requested_extensions(input_dir)
    blocked = [extension for extension in requested
               if not capabilities.get(extension, {'ready': False})['ready']]
    formats = {extension: capabilities[extension]['ready'] for extension in OFFICE_EXTENSIONS}
    runtime_mode = os.environ.get('AUTO_REQ_RUNTIME_MODE', '').strip()
    if not runtime_mode:
        runtime_mode = 'fixed-image' if runtime_image else 'builtin-only'
    warnings = []
    if not runtime_image:
        warnings.append('未使用固定镜像；转换器将记录实际宿主工具与版本')
    return {
        'ok': not blocked,
        'requested_formats': requested,
        'capabilities': capabilities,
        'blocked_formats': blocked,
        'install_required': sorted({
            'libreoffice' if extension in {'.doc', '.xls'} else 'python-markitdown'
            for extension in blocked if extension in {'.pdf', '.pptx', '.doc', '.xls'}
        }),
        'runtime_mode': runtime_mode,
        'fixed_runtime_verified': bool(runtime_image),
        'runtime_image': runtime_image,
        'markitdown': {'ready': markitdown_ready, 'version': markitdown_version,
                       'expected_version': EXPECTED_MARKITDOWN_VERSION,
                       'modules': modules},
        'libreoffice': soffice,
        'pymupdf': {'ready': fitz_ready},
        'formats': formats,
        'missing': blocked,
        'warnings': warnings,
        'environment_error': '',
    }


class _HtmlText(HTMLParser):
    BLOCKS = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, _attrs):
        if tag in {'script', 'style'}:
            self.hidden_depth += 1
        if tag in self.BLOCKS and not self.hidden_depth:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'} and self.hidden_depth:
            self.hidden_depth -= 1
        if tag in self.BLOCKS and not self.hidden_depth:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)

    def text(self):
        lines = [' '.join(line.split()) for line in ''.join(self.parts).splitlines()]
        return '\n\n'.join(line for line in lines if line)


def _read_text(path):
    with open(path, 'rb') as f:
        data = f.read()
    for encoding in ('utf-8-sig', 'utf-8', 'gb18030'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def _escape_cell(value):
    return ' '.join(str(value).replace('|', '\\|').splitlines()).strip()


def _markdown_table(rows):
    width = max((len(row) for row in rows), default=0)
    if not width:
        return ''
    normalized = [list(row) + [''] * (width - len(row)) for row in rows]
    header = normalized[0]
    lines = [
        '| ' + ' | '.join(_escape_cell(cell) or f'列{index + 1}' for index, cell in enumerate(header)) + ' |',
        '| ' + ' | '.join('---' for _ in range(width)) + ' |',
    ]
    for row in normalized[1:]:
        lines.append('| ' + ' | '.join(_escape_cell(cell) for cell in row) + ' |')
    return '\n'.join(lines)


def _zip_guard(archive, max_bytes):
    total = sum(info.file_size for info in archive.infolist())
    if total > max_bytes:
        raise ValueError(f'解压后内容超过 {max_bytes} bytes 限制')


def _clean_markitdown_output(content, extension):
    content = (content or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if extension in {'.xls', '.xlsx'}:
        content = re.sub(r'(?<=\|)\s*NaN\s*(?=\|)', ' ', content)
    if not content:
        raise _Unsupported('未提取到可读内容')
    return content


def _markitdown_to_markdown(path, extension, max_bytes):
    if extension in {'.docx', '.xlsx', '.pptx'}:
        with zipfile.ZipFile(path) as archive:
            _zip_guard(archive, max_bytes)
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise _Unsupported('固定运行镜像缺少 MarkItDown，无法解析该格式') from exc
    try:
        # 只传本地文件路径；不启用插件、云端 Document Intelligence 或 Content Understanding。
        result = MarkItDown(enable_plugins=False).convert(os.path.abspath(path))
    except Exception as exc:
        raise _MarkItDownFailure(str(exc)[:300]) from exc
    return _clean_markitdown_output(getattr(result, 'text_content', ''), extension)


def _libreoffice_then_parse(path, source_extension, target_extension, max_bytes):
    executable = _soffice_executable()
    if not executable:
        raise _Unsupported(f'{source_extension} 需固定运行镜像内的 LibreOffice')
    with tempfile.TemporaryDirectory(prefix='auto-req-office-') as directory:
        profile = os.path.join(directory, 'profile')
        output_dir = os.path.join(directory, 'output')
        temp_dir = os.path.join(directory, 'tmp')
        os.makedirs(profile)
        os.makedirs(output_dir)
        os.makedirs(temp_dir)
        profile_uri = pathlib.Path(profile).resolve().as_uri()
        command = [
            executable,
            f'-env:UserInstallation={profile_uri}',
            '--headless', '--safe-mode', '--nologo', '--nodefault', '--norestore', '--nolockcheck',
            '--convert-to', target_extension.lstrip('.'), '--outdir', output_dir,
            os.path.abspath(path),
        ]
        environment = os.environ.copy()
        environment['TMPDIR'] = temp_dir
        environment['XDG_CACHE_HOME'] = temp_dir
        environment['XDG_CONFIG_HOME'] = profile
        try:
            completed = subprocess.run(
                command, check=False, capture_output=True, text=True,
                timeout=OFFICE_TIMEOUT_SECONDS, env=environment)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f'LibreOffice 转换超时（{OFFICE_TIMEOUT_SECONDS}s）') from exc
        except OSError as exc:
            raise RuntimeError(f'LibreOffice 启动失败: {exc}') from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or '').strip()
            raise RuntimeError(f'LibreOffice 转换失败: {detail[:300]}')
        expected = os.path.join(
            output_dir, os.path.splitext(os.path.basename(path))[0] + target_extension)
        if not os.path.isfile(expected):
            raise RuntimeError('LibreOffice 未生成预期的中间文件')
        if os.path.getsize(expected) > max_bytes:
            raise ValueError(f'LibreOffice 中间文件超过 {max_bytes} bytes 限制')
        try:
            content = _markitdown_to_markdown(expected, target_extension, max_bytes)
            return content, 'libreoffice+markitdown'
        except (_Unsupported, _MarkItDownFailure):
            if target_extension == '.xlsx':
                return _xlsx_to_markdown(expected, max_bytes), 'libreoffice+builtin-fallback'
            if target_extension == '.docx':
                return _docx_to_markdown(expected, max_bytes), 'libreoffice+builtin-fallback'
            raise


def _docx_to_markdown(path, max_bytes):
    with zipfile.ZipFile(path) as archive:
        _zip_guard(archive, max_bytes)
        document = ET.fromstring(archive.read('word/document.xml'))
    body = document.find(f'{W_NS}body')
    if body is None:
        return ''
    blocks = []
    for child in body:
        if child.tag == f'{W_NS}p':
            text = ''.join(child.itertext()).strip()
            if text:
                blocks.append(text)
        elif child.tag == f'{W_NS}tbl':
            rows = []
            for tr in child.findall(f'{W_NS}tr'):
                rows.append([''.join(tc.itertext()).strip() for tc in tr.findall(f'{W_NS}tc')])
            table = _markdown_table(rows)
            if table:
                blocks.append(table)
    return '\n\n'.join(blocks)


def _column_number(cell_reference):
    letters = ''.join(ch for ch in cell_reference if ch.isalpha()).upper()
    number = 0
    for char in letters:
        number = number * 26 + ord(char) - ord('A') + 1
    return number


def _xlsx_to_markdown(path, max_bytes):
    with zipfile.ZipFile(path) as archive:
        _zip_guard(archive, max_bytes)
        shared = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
            shared = [''.join(item.itertext()) for item in root.findall(f'{SHEET_NS}si')]

        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        rels = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        targets = {
            rel.attrib['Id']: rel.attrib['Target'].lstrip('/')
            for rel in rels.findall(f'{PKG_REL_NS}Relationship')
        }
        sections = []
        for sheet in workbook.findall(f'.//{SHEET_NS}sheet'):
            rel_id = sheet.attrib.get(f'{REL_NS}id')
            target = targets.get(rel_id, '')
            if target and not target.startswith('xl/'):
                target = 'xl/' + target
            if not target or target not in archive.namelist():
                continue
            root = ET.fromstring(archive.read(target))
            rows = []
            for row in root.findall(f'.//{SHEET_NS}sheetData/{SHEET_NS}row'):
                values = {}
                for cell in row.findall(f'{SHEET_NS}c'):
                    index = _column_number(cell.attrib.get('r', 'A1'))
                    cell_type = cell.attrib.get('t', '')
                    if cell_type == 'inlineStr':
                        value = ''.join(cell.itertext())
                    else:
                        raw = cell.findtext(f'{SHEET_NS}v', default='')
                        value = shared[int(raw)] if cell_type == 's' and raw else raw
                    values[index] = value
                if values:
                    rows.append([values.get(i, '') for i in range(1, max(values) + 1)])
            title = sheet.attrib.get('name', 'Sheet')
            sections.append(f'## {title}\n\n{_markdown_table(rows) or "（空表）"}')
    return '\n\n'.join(sections)


def _pdf_to_markdown(path, max_bytes):
    try:
        import fitz  # PyMuPDF：可选依赖，未安装由调用方降级
    except ImportError as exc:
        raise _Unsupported('PDF 需 PyMuPDF(fitz)，当前环境未安装') from exc
    if os.path.getsize(path) > max_bytes:
        raise ValueError(f'文件超过 {max_bytes} bytes 限制')
    doc = fitz.open(path)
    try:
        if doc.is_encrypted and not doc.authenticate(''):
            raise _Unsupported('PDF 加密且无可用密码，无法解析')
        pages = []
        for index, page in enumerate(doc):
            text = page.get_text('text').strip()
            if text:
                pages.append(f'## 第{index + 1}页\n\n{text}')
        if not pages:
            raise _Unsupported('PDF 未提取到文本，可能为扫描件/图片型 PDF')
        return '\n\n'.join(pages)
    finally:
        doc.close()


def _convert_content(path, extension, max_bytes):
    if extension in TEXT_EXTENSIONS:
        return _read_text(path), 'builtin-fallback'
    if extension == '.json':
        return json.dumps(json.loads(_read_text(path)), ensure_ascii=False, indent=2), 'builtin-fallback'
    if extension == '.csv':
        return _markdown_table(list(csv.reader(_read_text(path).splitlines()))), 'builtin-fallback'
    if extension == '.html':
        parser = _HtmlText()
        parser.feed(_read_text(path))
        return parser.text(), 'builtin-fallback'
    if extension == '.xml':
        with open(path, 'rb') as f:
            root = ET.fromstring(f.read())
        return '\n'.join(text.strip() for text in root.itertext() if text.strip()), 'builtin-fallback'
    if extension == '.docx':
        try:
            return _markitdown_to_markdown(path, extension, max_bytes), 'markitdown'
        except (_Unsupported, _MarkItDownFailure):
            return _docx_to_markdown(path, max_bytes), 'builtin-fallback'
    if extension == '.xlsx':
        try:
            return _markitdown_to_markdown(path, extension, max_bytes), 'markitdown'
        except (_Unsupported, _MarkItDownFailure):
            return _xlsx_to_markdown(path, max_bytes), 'builtin-fallback'
    if extension == '.xls':
        try:
            return _markitdown_to_markdown(path, extension, max_bytes), 'markitdown'
        except (_Unsupported, _MarkItDownFailure) as primary_error:
            try:
                return _libreoffice_then_parse(path, extension, '.xlsx', max_bytes)
            except _Unsupported:
                raise primary_error
    if extension == '.doc':
        return _libreoffice_then_parse(path, extension, '.docx', max_bytes)
    if extension == '.pdf':
        try:
            return _markitdown_to_markdown(path, extension, max_bytes), 'markitdown'
        except (_Unsupported, _MarkItDownFailure):
            return _pdf_to_markdown(path, max_bytes), 'builtin-fallback'
    if extension == '.pptx':
        return _markitdown_to_markdown(path, extension, max_bytes), 'markitdown'
    raise ValueError(f'当前内置转换器不支持 {extension or "无扩展名"}')


def convert_file(path, output_dir, max_bytes=DEFAULT_MAX_BYTES):
    name = os.path.basename(path)
    extension = os.path.splitext(name)[1].lower()
    size = os.path.getsize(path)
    if size > max_bytes:
        return {'name': name, 'status': 'skipped', 'reason': f'文件超过 {max_bytes} bytes 限制', 'size': size}
    if extension in IMAGE_EXTENSIONS:
        return {'name': name, 'status': 'needs_read', 'reason': '图片须由视觉读取', 'size': size}
    if extension in UNSUPPORTED_EXTENSIONS:
        return {'name': name, 'status': 'unsupported', 'reason': f'内置转换器不支持 {extension}', 'size': size}
    try:
        content, converter = _convert_content(path, extension, max_bytes)
        content = content.strip()
        if not content:
            raise _Unsupported('未提取到可读内容')
        os.makedirs(output_dir, exist_ok=True)
        output = os.path.join(output_dir, f'{name}.md')
        with open(output, 'w', encoding='utf-8') as f:
            f.write(f'# 附件：{name}\n\n{content}\n')
        return {'name': name, 'status': 'converted', 'output': output, 'size': size,
                'converter': converter, 'converter_chain': converter,
                'runtime_mode': os.environ.get(
                    'AUTO_REQ_RUNTIME_MODE',
                    'fixed-image' if os.environ.get('AUTO_REQ_RUNTIME_IMAGE') else 'builtin-only'),
                'tool_versions': _tool_versions(extension, converter)}
    except _Unsupported as exc:
        return {'name': name, 'status': 'unsupported', 'reason': str(exc)[:300], 'size': size}
    except Exception as exc:
        return {'name': name, 'status': 'error', 'reason': str(exc)[:300], 'size': size}


def convert_directory(input_dir, output_dir, max_bytes=DEFAULT_MAX_BYTES):
    if max_bytes <= 0:
        raise ValueError('max_bytes 必须为正整数')
    files = []
    if not os.path.isdir(input_dir):
        return {'total': 0, 'converted': 0, 'needs_read': [], 'skipped': 0, 'errors': 1,
                'files': [], 'error': f'目录不存在: {input_dir}'}
    for name in sorted(os.listdir(input_dir)):
        path = os.path.join(input_dir, name)
        if os.path.islink(path):
            files.append({'name': name, 'status': 'skipped', 'reason': '不读取符号链接',
                          'size': 0})
        elif os.path.isfile(path):
            files.append(convert_file(path, output_dir, max_bytes))
    return {
        'total': len(files),
        'converted': sum(item['status'] == 'converted' for item in files),
        'needs_read': [item['name'] for item in files if item['status'] == 'needs_read'],
        'skipped': sum(item['status'] in {'skipped', 'unsupported'} for item in files),
        'errors': sum(item['status'] == 'error' for item in files),
        'files': files,
    }


def _tool_versions(extension, converter):
    versions = {'python': sys.version.split()[0]}
    if 'markitdown' in converter:
        try:
            versions['markitdown'] = importlib.metadata.version('markitdown')
        except importlib.metadata.PackageNotFoundError:
            pass
    if 'libreoffice' in converter:
        version = _soffice_info().get('version')
        if version:
            versions['libreoffice'] = version
    if extension == '.pdf':
        try:
            versions['pymupdf'] = importlib.metadata.version('PyMuPDF')
        except importlib.metadata.PackageNotFoundError:
            pass
    return versions


def main():
    parser = argparse.ArgumentParser(description='TFS 附件 Markdown 转换器')
    parser.add_argument('--precheck', action='store_true', help='按实际附件检查可用转换链')
    parser.add_argument('--input-dir')
    parser.add_argument('--output-dir')
    parser.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    if args.precheck:
        output = precheck(args.input_dir)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        sys.exit(0 if output['ok'] else 1)
    if not args.input_dir or not args.output_dir:
        output = {'total': 0, 'converted': 0, 'needs_read': [], 'skipped': 0, 'errors': 1,
                  'files': [], 'error': '转换模式必须同时提供 --input-dir 和 --output-dir'}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        sys.exit(1)
    try:
        output = convert_directory(args.input_dir, args.output_dir, args.max_bytes)
    except ValueError as exc:
        output = {'total': 0, 'converted': 0, 'needs_read': [], 'skipped': 0, 'errors': 1,
                  'files': [], 'error': str(exc)}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    sys.exit(0 if not output.get('error') else 1)


if __name__ == '__main__':
    main()
