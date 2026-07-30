#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 TFS 工作项附件转换为 Markdown（核心仅 Python 标准库）。

支持文本、HTML/XML、CSV/JSON、DOCX 和 XLSX；图片交给调用方视觉读取。
PDF 在本机装有 PyMuPDF(`fitz`) 时提取文本，未安装则降级为 unsupported——不写
requirements、不硬依赖，避免迁移时产生隐式运行依赖或把无法读取的内容误当成证据。
旧版 Office(.doc/.xls) 和 PPT 仍返回 unsupported。
"""
import argparse
import csv
import json
import os
import sys
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET


DEFAULT_MAX_BYTES = 20 * 1024 * 1024
TEXT_EXTENSIONS = {'.md', '.txt', '.log', '.yaml', '.yml'}
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}
UNSUPPORTED_EXTENSIONS = {'.doc', '.xls', '.ppt', '.pptx', '.zip', '.rar', '.7z', '.exe'}


class _Unsupported(Exception):
    """可选依赖缺失等导致的「无法解析」——映射为 unsupported 而非 error。"""
W_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
SHEET_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
PKG_REL_NS = '{http://schemas.openxmlformats.org/package/2006/relationships}'


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
        return _read_text(path)
    if extension == '.json':
        return json.dumps(json.loads(_read_text(path)), ensure_ascii=False, indent=2)
    if extension == '.csv':
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            return _markdown_table(list(csv.reader(f)))
    if extension == '.html':
        parser = _HtmlText()
        parser.feed(_read_text(path))
        return parser.text()
    if extension == '.xml':
        with open(path, 'rb') as f:
            root = ET.fromstring(f.read())
        return '\n'.join(text.strip() for text in root.itertext() if text.strip())
    if extension == '.docx':
        return _docx_to_markdown(path, max_bytes)
    if extension == '.xlsx':
        return _xlsx_to_markdown(path, max_bytes)
    if extension == '.pdf':
        return _pdf_to_markdown(path, max_bytes)
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
        content = _convert_content(path, extension, max_bytes).strip()
        os.makedirs(output_dir, exist_ok=True)
        output = os.path.join(output_dir, f'{name}.md')
        with open(output, 'w', encoding='utf-8') as f:
            f.write(f'# 附件：{name}\n\n{content}\n')
        return {'name': name, 'status': 'converted', 'output': output, 'size': size}
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
        if os.path.isfile(path):
            files.append(convert_file(path, output_dir, max_bytes))
    return {
        'total': len(files),
        'converted': sum(item['status'] == 'converted' for item in files),
        'needs_read': [item['name'] for item in files if item['status'] == 'needs_read'],
        'skipped': sum(item['status'] in {'skipped', 'unsupported'} for item in files),
        'errors': sum(item['status'] == 'error' for item in files),
        'files': files,
    }


def main():
    parser = argparse.ArgumentParser(description='TFS 附件内置 Markdown 转换器（仅标准库）')
    parser.add_argument('--input-dir', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    try:
        output = convert_directory(args.input_dir, args.output_dir, args.max_bytes)
    except ValueError as exc:
        output = {'total': 0, 'converted': 0, 'needs_read': [], 'skipped': 0, 'errors': 1,
                  'files': [], 'error': str(exc)}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    sys.exit(0 if not output.get('error') else 1)


if __name__ == '__main__':
    main()
