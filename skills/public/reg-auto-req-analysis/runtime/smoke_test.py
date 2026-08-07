#!/usr/bin/env python3
"""固定运行镜像构建期附件能力与 Office 四格式烟测。"""
import os
import pathlib
import subprocess
import sys
import tempfile
import zipfile


SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / '_lib' / 'tfs'))
import attachment_converter as converter  # noqa: E402


def libreoffice_convert(source, extension, output_dir, profile_dir):
    command = [
        'soffice',
        f'-env:UserInstallation={profile_dir.resolve().as_uri()}',
        '--headless', '--safe-mode', '--nologo', '--nodefault', '--norestore', '--nolockcheck',
        '--convert-to', extension, '--outdir', str(output_dir), str(source),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or 'LibreOffice fixture conversion failed')


def write_docx(path):
    parts = {
        '[Content_Types].xml': '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>''',
        '_rels/.rels': '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>''',
        'word/document.xml': '''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>Word 关键内容</w:t></w:r></w:p><w:sectPr/>
</w:body></w:document>''',
    }
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


def write_xlsx(path):
    parts = {
        '[Content_Types].xml': '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>''',
        '_rels/.rels': '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''',
        'xl/workbook.xml': '''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        'xl/_rels/workbook.xml.rels': '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>''',
        'xl/worksheets/sheet1.xml': '''<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>接口编号</t></is></c><c r="B1" t="inlineStr"><is><t>接口名称</t></is></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><t>2201A</t></is></c><c r="B2" t="inlineStr"><is><t>门诊挂号</t></is></c></row>
</sheetData></worksheet>''',
    }
    with zipfile.ZipFile(path, 'w') as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


def main():
    readiness = converter.precheck()
    if not readiness['ok']:
        raise RuntimeError(f'Office precheck failed: {readiness}')
    if readiness['markitdown']['version'] != '0.1.7':
        raise RuntimeError(f'MarkItDown version mismatch: {readiness["markitdown"]}')
    for extension in ('.pdf', '.pptx'):
        if 'markitdown' not in readiness['capabilities'][extension]['chains']:
            raise RuntimeError(f'{extension} MarkItDown capability missing: {readiness}')
    with tempfile.TemporaryDirectory(prefix='auto-req-runtime-smoke-') as directory:
        root = pathlib.Path(directory)
        source = root / 'source'
        parsed = root / 'parsed'
        profile = root / 'fixture-profile'
        source.mkdir()
        parsed.mkdir()
        profile.mkdir()

        docx = source / 'word-source.docx'
        xlsx = source / 'excel-source.xlsx'
        write_docx(docx)
        write_xlsx(xlsx)
        libreoffice_convert(docx, 'doc', source, profile)
        libreoffice_convert(xlsx, 'xls', source, profile)

        result = converter.convert_directory(str(source), str(parsed))
        if result['converted'] != 4 or result['errors'] or result['skipped']:
            raise RuntimeError(f'Office conversion smoke failed: {result}')
        word_outputs = '\n'.join(
            (parsed / f'word-source{extension}.md').read_text(encoding='utf-8')
            for extension in ('.doc', '.docx'))
        sheet_outputs = '\n'.join(
            (parsed / f'excel-source{extension}.md').read_text(encoding='utf-8')
            for extension in ('.xls', '.xlsx'))
        if 'Word 关键内容' not in word_outputs:
            raise RuntimeError('Word conversion smoke output missed expected content')
        if '接口编号' not in sheet_outputs or '2201A' not in sheet_outputs or '门诊挂号' not in sheet_outputs:
            raise RuntimeError('Excel conversion smoke output missed expected table cells')
        methods = {item['name']: item.get('converter') for item in result['files']}
        if methods.get('word-source.doc') != 'libreoffice+markitdown':
            raise RuntimeError(f'Legacy DOC did not use controlled conversion: {methods}')
        for name in ('word-source.docx', 'excel-source.xls', 'excel-source.xlsx'):
            if methods.get(name) != 'markitdown':
                raise RuntimeError(f'{name} did not use MarkItDown primary path: {methods}')
    print('office-runtime-smoke: OK')


if __name__ == '__main__':
    main()
