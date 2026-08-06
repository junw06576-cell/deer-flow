import datetime
import os
import sys
import tempfile
import unittest
import gzip
import json
import pathlib
import zipfile
from unittest import mock


sys.path.insert(0, os.path.dirname(__file__))
import pipeline  # noqa: E402
import tfs_client as tfs  # noqa: E402
import attachment_converter as converter  # noqa: E402
import attachment_runtime as attachment_runtime  # noqa: E402
import redis_client  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import build_menu_business_index as menu_index  # noqa: E402


class TfsClientTests(unittest.TestCase):
    def test_map_workitem_exposes_pre_qc_fields(self):
        item = tfs.map_workitem({'id': 1, 'rev': 2, 'fields': {
            'Microsoft.VSTS.CMMI.RequirementType': '功能性的',
            'Pmis.Demand.Priority': 'C级(一般)',
            'Demand.Expected.date': '2026-09-16T16:00:00Z',
            'System.IterationPath': '健康医养\\健康管理\\2026年\\V6.0.2607.31',
            'System.AreaPath': 'NETHIS5.5\\2026年',
            'System.TeamProject': 'legacy-project',
            'Winning.Prod.Version': 'V6.0.2606.05',
        }})
        self.assertEqual(item['demandType'], '功能性的')
        self.assertEqual(item['pimisPriority'], 'C级(一般)')
        self.assertEqual(item['expectedDate'], '2026-09-17T00:00:00+08:00')
        self.assertEqual(item['expectedDateRaw'], '2026-09-16T16:00:00Z')
        self.assertEqual(item['areaPath'], 'NETHIS5.5\\2026年')
        self.assertEqual(item['area'], 'NETHIS5.5')
        self.assertEqual(item['areaSource'], 'System.AreaPath')
        self.assertEqual(item['version'], 'V6.0.2606.05')

    def test_map_workitem_uses_team_project_only_when_area_path_missing(self):
        item = tfs.map_workitem({'fields': {'System.TeamProject': 'NETHIS5.5'}})
        self.assertEqual(item['area'], 'NETHIS5.5')
        self.assertEqual(item['areaSource'], 'System.TeamProject')

    def test_tfs_dates_use_beijing_timezone_without_host_timezone_dependency(self):
        self.assertEqual(tfs.beijing_iso('2026-09-16T16:00:00Z'),
                         '2026-09-17T00:00:00+08:00')
        self.assertEqual(tfs.beijing_iso('2026-09-17T00:00:00+08:00'),
                         '2026-09-17T00:00:00+08:00')
        self.assertEqual(tfs.beijing_iso('2026-09-17'), '2026-09-17')
        self.assertEqual(tfs.beijing_iso('2026-09-17T00:00:00'),
                         '2026-09-17T00:00:00+08:00')
        with self.assertRaises(ValueError):
            tfs.beijing_iso('not-a-date')

    def test_write_field_skips_existing_run_marker(self):
        raw = {'id': 1, 'rev': 4, 'fields': {'Winning.Demand.Analysis': 'old\n<!-- auto-req-run:run_12345678 -->'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), mock.patch.object(tfs, 'patch_workitem') as patch:
            response = tfs.write_field({}, 1, tfs.F_ANALYZER_DESC, 'new', 'append', False,
                                       '<!-- auto-req-run:run_12345678 -->')
        self.assertTrue(response['ok'])
        self.assertTrue(response['noop'])
        patch.assert_not_called()

    def test_set_assignee_dry_run_does_not_patch(self):
        raw = {'id': 1, 'rev': 4, 'fields': {tfs.F_ASSIGNED_TO: 'old(旧) <WINNING\\old>'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'patch_workitem') as patch:
            response = tfs.set_assignee({}, 1, '舒予', True)
        self.assertTrue(response['ok'])
        self.assertTrue(response['dry_run'])
        self.assertEqual(response['after'], '舒予')
        patch.assert_not_called()

    def test_set_assignee_success_patches_assigned_to(self):
        raw = {'id': 1, 'rev': 4, 'fields': {tfs.F_ASSIGNED_TO: 'old(旧) <WINNING\\old>'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'patch_workitem', return_value=(200, {})) as patch:
            response = tfs.set_assignee({}, 1, '舒予', False)
        self.assertTrue(response['ok'])
        self.assertEqual(response['before'], 'old(旧) <WINNING\\old>')
        self.assertEqual(response['after'], '舒予')
        ops = patch.call_args.args[3]
        self.assertEqual(ops[-1],
                         {'op': 'replace', 'path': f'/fields/{tfs.F_ASSIGNED_TO}', 'value': '舒予'})

    def test_set_assignee_identity_failure_degrades_without_force(self):
        raw = {'id': 1, 'rev': 4, 'fields': {tfs.F_ASSIGNED_TO: 'old(旧) <WINNING\\old>'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'patch_workitem', return_value=(400, 'identity not found')):
            response = tfs.set_assignee({}, 1, '不存在的人', False)
        self.assertFalse(response['ok'])
        self.assertIn('HTTP 400', response['error'])

    def test_replace_detail_analysis_section_preserves_neighbor_sections(self):
        old = ('<div>【详细调研结果】</div><div>【分析者描述】</div><div>旧内容</div>'
               '<div>【开发者描述】</div><div>开发保留</div>')
        raw = {'id': 1, 'rev': 4, 'fields': {tfs.F_DESCRIPTION: old}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'patch_workitem', return_value=(200, {})) as patch:
            response = tfs.replace_detail_analysis_section({}, 1, '<div>新内容</div>', False)
        self.assertTrue(response['ok'])
        value = patch.call_args.args[3][0]['value']
        self.assertEqual(value, ('<div>【详细调研结果】</div><div>【分析者描述】</div><div>新内容</div>'
                                 '<div>【开发者描述】</div><div>开发保留</div>'))

    def test_replace_detail_analysis_section_rejects_invalid_section_markers(self):
        for description in (
                '<div>【分析者描述】</div>',
                '<div>【开发者描述】</div><div>【分析者描述】</div>',
                '<div>【分析者描述】</div><div>【分析者描述】</div><div>【开发者描述】</div>'):
            raw = {'id': 1, 'rev': 4, 'fields': {tfs.F_DESCRIPTION: description}}
            with self.subTest(description=description), \
                    mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                    mock.patch.object(tfs, 'patch_workitem') as patch:
                response = tfs.replace_detail_analysis_section({}, 1, '<div>新内容</div>', False)
            self.assertFalse(response['ok'])
            patch.assert_not_called()

    def test_remove_legacy_analysis_append_requires_exact_suffix(self):
        body = '# 变更方案\n\n&gt; 说明\n'
        raw = {'id': 1, 'rev': 4, 'fields': {tfs.F_REQUIREMENT_ANALYSIS: '原模板\n\n' + body}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'patch_workitem', return_value=(200, {})) as patch:
            response = tfs.remove_legacy_analysis_append({}, 1, body, False)
        self.assertTrue(response['ok'])
        self.assertEqual(patch.call_args.args[3][0]['value'], '原模板')

        raw['fields'][tfs.F_REQUIREMENT_ANALYSIS] = '原模板\n\n人工补充'
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'patch_workitem') as patch:
            response = tfs.remove_legacy_analysis_append({}, 1, body, False)
        self.assertFalse(response['ok'])
        patch.assert_not_called()

    def test_attachment_uses_encoded_base_url_and_deduplicates_by_filename(self):
        client = {
            'base_url': 'http://example/tfs/collection/%E5%81%A5%E5%BA%B7',
            'server': 'example', 'port': 80, 'collection': 'collection', 'project': '健康', 'pat': 'x',
        }
        raw = {'id': 1, 'rev': 8, 'fields': {}, 'relations': []}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, '方案_run_12345678.md')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('content')
            with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                 mock.patch.object(tfs, 'wit_http', return_value=(201, {'id': 'abc'})), \
                 mock.patch.object(tfs, 'patch_workitem', return_value=(200, {})) as patch:
                response = tfs.upload_attachment(client, 1, path, False)
        self.assertTrue(response['ok'])
        payload = patch.call_args.args[3]
        self.assertEqual(payload[0]['value']['url'],
                         'http://example/tfs/collection/%E5%81%A5%E5%BA%B7/_apis/wit/attachments/abc?fileName=%E6%96%B9%E6%A1%88_run_12345678.md')

    def test_download_attachments_limits_scope_and_records_integrity(self):
        class Response:
            def __init__(self):
                self.payload = gzip.compress(b'content')
                self.headers = {
                    'Content-Length': str(len(self.payload)),
                    'Content-Type': 'text/plain',
                    'Content-Encoding': 'gzip',
                    'Content-Disposition': 'attachment; filename="接口说明.txt"',
                }

            def read(self, _limit):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        client = {'server': 'example', 'port': 80, 'collection': 'collection', 'pat': 'x'}
        raw = {'relations': [
            {'rel': 'AttachedFile',
             'url': 'http://example:80/tfs/collection/_apis/wit/attachments/abc?fileName=%E6%8E%A5%E5%8F%A3.txt',
             'attributes': {'comment': '接口说明'}},
            {'rel': 'AttachedFile', 'url': 'http://outside/tfs/collection/_apis/wit/attachments/nope?fileName=x.txt'},
        ]}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
             mock.patch.object(tfs.urllib.request, 'urlopen', return_value=Response()) as urlopen:
            result = tfs.download_attachments(client, 1, directory)
        self.assertTrue(result['ok'])
        self.assertEqual(result['total'], 2)
        self.assertEqual(len(result['downloaded']), 1)
        self.assertEqual(result['downloaded'][0]['sha256'],
                         'ed7002b439e9ac845f22357d822bac1444730fbdb6016d3ec9432297b9ec9f73')
        self.assertEqual(result['downloaded'][0]['content_encoding'], 'gzip')
        self.assertEqual(len(result['skipped']), 1)
        self.assertIn('不属于当前 TFS collection', result['skipped'][0]['reason'])
        self.assertEqual(urlopen.call_count, 1)

    def test_external_description_attachment_requires_allowlist_and_separate_auth(self):
        class Response:
            headers = {
                'Content-Length': '7',
                'Content-Type': 'application/pdf',
                'Content-Disposition': 'attachment; filename="样例.pdf"',
            }

            def read(self, _limit):
                return b'content'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response()

        raw = {'fields': {tfs.F_DESCRIPTION: '<a href="https://weberp.winning.com.cn/attachment/1">样例</a>'},
               'relations': []}
        client = {'external_attachments': {'enabled': True, 'allowed_hosts': ['weberp.winning.com.cn'], 'bypass_proxy': True,
                                           'auth': {'type': 'cookie', 'env': 'ERP_TEST_COOKIE'}}}
        opener = Opener()
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
             mock.patch.dict(os.environ, {'ERP_TEST_COOKIE': 'sid=test'}, clear=False), \
             mock.patch.object(tfs.urllib.request, 'build_opener', return_value=opener) as build_opener:
            result = tfs.download_attachments(client, 1, directory, include_external=True)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['downloaded'][0]['name'], '样例.pdf')
        self.assertEqual(result['downloaded'][0]['source'], 'external')
        self.assertEqual(result['downloaded'][0]['source_url'], 'https://weberp.winning.com.cn/attachment/1')
        self.assertEqual(opener.request.headers['Cookie'], 'sid=test')
        self.assertIsInstance(build_opener.call_args.args[0], tfs.urllib.request.ProxyHandler)

        client['external_attachments']['allowed_hosts'] = ['allowed.example']
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(tfs, 'fetch_raw', return_value=raw):
            result = tfs.download_attachments(client, 1, directory, include_external=True)
        self.assertEqual(result['downloaded'], [])
        self.assertIn('白名单', result['skipped'][0]['reason'])

    def test_external_attachment_page_follows_only_download_links(self):
        class Response:
            def __init__(self, payload, headers=None):
                self.payload = payload
                self.headers = {'Content-Length': str(len(payload)), **(headers or {})}

            def read(self, _limit):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def __init__(self):
                self.requests = []
                self.responses = [
                    Response('<el-link href="/attachment/download/omis/1?fileName=%E6%A0%B7%E4%BE%8B.docx">样例</el-link>'.encode('utf-8')),
                    Response(b'docx-content'),
                ]

            def open(self, request, timeout):
                self.requests.append(request.full_url)
                return self.responses.pop(0)

        raw = {'fields': {tfs.F_DESCRIPTION: '<a href="https://weberp.winning.com.cn/attachment/view/1">附件</a>'},
               'relations': []}
        client = {'external_attachments': {'enabled': True, 'allowed_hosts': ['weberp.winning.com.cn'],
                                           'auth': {'type': 'none'}}}
        opener = Opener()
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
             mock.patch.object(tfs.urllib.request, 'build_opener', return_value=opener):
            result = tfs.download_attachments(client, 1, directory, include_external=True)
        self.assertEqual(result['total'], 2)
        self.assertEqual([item['name'] for item in result['downloaded']], ['样例.docx'])
        self.assertEqual(len(opener.requests), 2)
        self.assertIn('/attachment/download/', opener.requests[1])

    def test_external_attachment_rejects_redirect_outside_allowlist(self):
        config = {'allowed_hosts': ['weberp.winning.com.cn']}
        handler = tfs._ExternalRedirectHandler(config)
        request = tfs.urllib.request.Request('https://weberp.winning.com.cn/attachment/1')
        with self.assertRaises(tfs.urllib.error.HTTPError) as raised:
            handler.redirect_request(request, None, 302, 'Found', {}, 'https://outside.example/file')
        raised.exception.close()

    def test_external_allowlist_supports_wildcard_suffix(self):
        config = {'allowed_hosts': ['*.winning.com.cn']}
        for host in ('assist.winning.com.cn', 'weberp.winning.com.cn', 'a.b.winning.com.cn'):
            self.assertTrue(tfs._external_attachment_url_allowed(config, f'https://{host}/x'), host)
        for host in ('winning.com.cn', 'evilwinning.com.cn', 'winning.com.cn.evil.example', 'example.com'):
            self.assertFalse(tfs._external_attachment_url_allowed(config, f'https://{host}/x'), host)
        # 精确主机向后兼容
        self.assertTrue(tfs._external_attachment_url_allowed(
            {'allowed_hosts': ['weberp.winning.com.cn']}, 'https://weberp.winning.com.cn/x'))
        # 通配符内的重定向不被拒（逐跳校验复用同一匹配逻辑）
        handler = tfs._ExternalRedirectHandler(config)
        request = tfs.urllib.request.Request('https://assist.winning.com.cn/x')
        try:
            handler.redirect_request(request, None, 302, 'Found', {}, 'https://erp2.winning.com.cn/y')
        except tfs.urllib.error.HTTPError:
            self.fail('通配符白名单内的重定向不应被拒')

    def test_record_uses_run_id_to_avoid_same_second_collision(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(tfs, 'PROCESS_DIR', directory):
            first = tfs.record('auto-req-qc', 1, 'PASS', [], '', '', '', {}, 'run_12345678')
            second = tfs.record('auto-req-qc', 1, 'PASS', [], '', '', '', {}, 'run_87654321')
            self.assertNotEqual(first['audit'], second['audit'])
            self.assertTrue(os.path.exists(first['audit']))
            self.assertTrue(os.path.exists(second['audit']))
            # 审计按工作项分组：落在 <wid>/runs/ 下
            self.assertIn(os.path.join('1', 'runs'), first['audit'])

    def test_list_iterations_parses_attributes_and_matches_by_finishdate(self):
        client = {'server': 's', 'port': 80, 'collection': 'c', 'base_url': 'http://s:80/tfs/c/p', 'pat': 'x'}
        tree = {'name': 'Proj', 'structureType': 'iteration', 'hasChildren': True, 'children': [
            {'name': '2022-2025', 'hasChildren': True, 'children': [
                {'name': '2022', 'hasChildren': True, 'children': [
                    {'name': '2022-06-05', 'hasChildren': False,
                     'attributes': {'startDate': '2022-05-23T00:00:00Z', 'finishDate': '2022-06-05T00:00:00Z'}},
                ]},
            ]},
            {'name': '2026', 'hasChildren': True, 'children': [
                {'name': 'V6.0.2607.31', 'hasChildren': False,
                 'attributes': {'startDate': '2026-07-18T00:00:00Z', 'finishDate': '2026-07-31T00:00:00Z'}},
                {'name': 'V6.0.2608.14', 'hasChildren': False,
                 'attributes': {'startDate': '2026-08-03T00:00:00Z', 'finishDate': '2026-08-14T00:00:00Z'}},
                {'name': 'V6.0.2608.28', 'hasChildren': False,
                 'attributes': {'startDate': '2026-08-17T00:00:00Z', 'finishDate': '2026-08-28T00:00:00Z'}},
                {'name': 'V6.0.2609.11', 'hasChildren': False,
                 'attributes': {'startDate': '2026-08-31T00:00:00Z', 'finishDate': '2026-09-11T00:00:00Z'}},
            ]},
        ]}
        with mock.patch.object(tfs, 'wit_retry', return_value=(200, tree)):
            result = tfs.fetch_iteration_tree(client, 'Proj')
        self.assertTrue(result['ok'])
        self.assertEqual([i['path'] for i in result['iterations']], [
            'Proj\\2022-2025\\2022\\2022-06-05',
            'Proj\\2026\\V6.0.2607.31', 'Proj\\2026\\V6.0.2608.14',
            'Proj\\2026\\V6.0.2608.28', 'Proj\\2026\\V6.0.2609.11'])
        # 场景1：期望 08-31、今天 08-01（代码提交截止 deadline=08-08）
        #   matched=2608.28（最晚·时效基准）；earliest=2608.14（提交截止未过里最早·排期取向）
        #   2022-06-05（历史）与 2607.31（finish 07-31 < deadline 08-08，提交截止已过）被 earliest 排除
        with mock.patch.object(tfs, 'wit_retry', return_value=(200, tree)):
            res = tfs.list_iterations(client, 'Proj', '2026-08-31', today=datetime.date(2026, 8, 1))
        self.assertEqual(res['matched']['name'], 'V6.0.2608.28')
        self.assertEqual(res['earliest']['name'], 'V6.0.2608.14')
        # 场景2：今天 09-01（deadline=09-08）→ 所有候选提交截止已过，earliest=None，但 matched 仍在
        with mock.patch.object(tfs, 'wit_retry', return_value=(200, tree)):
            res_late = tfs.list_iterations(client, 'Proj', '2026-08-31', today=datetime.date(2026, 9, 1))
        self.assertEqual(res_late['matched']['name'], 'V6.0.2608.28')
        self.assertIsNone(res_late['earliest'])
        # 场景3：期望日早于所有 finishDate（含历史）→ 无候选，matched/earliest 均 None
        with mock.patch.object(tfs, 'wit_retry', return_value=(200, tree)):
            res_empty = tfs.list_iterations(client, 'Proj', '2022-01-01', today=datetime.date(2026, 8, 1))
        self.assertIsNone(res_empty['matched'])
        self.assertIsNone(res_empty['earliest'])

        # TFS UTC 前一日 16:00 是北京时间次日 00:00，必须允许截止日在北京时间期望日当天。
        with mock.patch.object(tfs, 'wit_retry', return_value=(200, tree)):
            res_beijing = tfs.list_iterations(
                client, 'Proj', '2026-08-13T16:00:00Z', today=datetime.date(2026, 8, 1))
        self.assertEqual(res_beijing['matched']['name'], 'V6.0.2608.14')
        self.assertEqual(res_beijing['earliest']['name'], 'V6.0.2608.14')

        with mock.patch.object(tfs, 'wit_retry', return_value=(200, tree)):
            res_invalid = tfs.list_iterations(client, 'Proj', 'bad-date', today=datetime.date(2026, 8, 1))
        self.assertFalse(res_invalid['ok'])
        self.assertIn('无法解析 TFS 时间', res_invalid['error'])

    def test_builtin_attachment_converter_extracts_docx_and_xlsx_without_external_tools(self):
        document_xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
          <w:p><w:r><w:t>接口规则</w:t></w:r></w:p>
          <w:tbl><w:tr><w:tc><w:p><w:r><w:t>字段</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>必填</w:t></w:r></w:p></w:tc></w:tr>
          <w:tr><w:tc><w:p><w:r><w:t>orderNo</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>是</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>'''
        workbook_xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="参数" sheetId="1" r:id="rId1"/></sheets></workbook>'''
        rels_xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'''
        strings_xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>参数</t></si><si><t>取值</t></si><si><t>B105</t></si><si><t>太原采购</t></si></sst>'''
        sheet_xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
          <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
          <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c></row>
        </sheetData></worksheet>'''
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, '附件')
            output = os.path.join(directory, '附件解析')
            os.makedirs(source)
            with zipfile.ZipFile(os.path.join(source, '接口.docx'), 'w') as archive:
                archive.writestr('word/document.xml', document_xml)
            with zipfile.ZipFile(os.path.join(source, '参数.xlsx'), 'w') as archive:
                archive.writestr('xl/workbook.xml', workbook_xml)
                archive.writestr('xl/_rels/workbook.xml.rels', rels_xml)
                archive.writestr('xl/sharedStrings.xml', strings_xml)
                archive.writestr('xl/worksheets/sheet1.xml', sheet_xml)
            result = converter.convert_directory(source, output)
            self.assertEqual(result['converted'], 2)
            self.assertEqual({item['converter'] for item in result['files']}, {'builtin-fallback'})
            with open(os.path.join(output, '接口.docx.md'), encoding='utf-8') as f:
                self.assertIn('orderNo', f.read())
            with open(os.path.join(output, '参数.xlsx.md'), encoding='utf-8') as f:
                self.assertIn('太原采购', f.read())

    def test_attachment_converter_routes_four_office_formats_and_records_converter(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, 'out')
            paths = {}
            for extension in ('.doc', '.docx', '.xls', '.xlsx'):
                path = os.path.join(directory, 'sample' + extension)
                with open(path, 'wb') as f:
                    f.write(b'office')
                paths[extension] = path

            with mock.patch.object(converter, '_markitdown_to_markdown', return_value='MarkItDown 内容'):
                docx = converter.convert_file(paths['.docx'], output)
                xls = converter.convert_file(paths['.xls'], output)
                xlsx = converter.convert_file(paths['.xlsx'], output)
            self.assertEqual(docx['converter'], 'markitdown')
            self.assertEqual(xls['converter'], 'markitdown')
            self.assertEqual(xlsx['converter'], 'markitdown')

            with mock.patch.object(
                    converter, '_libreoffice_then_parse',
                    return_value=('旧版 Word 内容', 'libreoffice+markitdown')) as office:
                doc = converter.convert_file(paths['.doc'], output)
            self.assertEqual(doc['converter'], 'libreoffice+markitdown')
            office.assert_called_once_with(paths['.doc'], '.doc', '.docx', converter.DEFAULT_MAX_BYTES)

    def test_xls_falls_back_to_libreoffice_after_markitdown_content_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'sample.xls')
            with open(path, 'wb') as f:
                f.write(b'legacy-xls')
            with mock.patch.object(
                    converter, '_markitdown_to_markdown',
                    side_effect=converter._MarkItDownFailure('xlrd failed')), \
                    mock.patch.object(
                        converter, '_libreoffice_then_parse',
                        return_value=('表格内容', 'libreoffice+builtin-fallback')):
                result = converter.convert_file(path, os.path.join(directory, 'out'))
        self.assertEqual(result['status'], 'converted')
        self.assertEqual(result['converter'], 'libreoffice+builtin-fallback')
        self.assertEqual(result['converter_chain'], 'libreoffice+builtin-fallback')

    def test_libreoffice_failure_and_timeout_are_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'sample.doc')
            with open(path, 'wb') as f:
                f.write(b'legacy-doc')
            failed = mock.Mock(returncode=1, stdout='', stderr='bad input')
            for outcome, expected in (
                    (failed, 'LibreOffice 转换失败'),
                    (converter.subprocess.TimeoutExpired('soffice', 60), 'LibreOffice 转换超时')):
                with self.subTest(expected=expected), \
                        mock.patch.object(converter.shutil, 'which', return_value='/usr/bin/soffice'), \
                        mock.patch.object(converter.subprocess, 'run', side_effect=[outcome] if outcome is failed else outcome):
                    result = converter.convert_file(path, os.path.join(directory, 'out'))
                self.assertEqual(result['status'], 'error')
                self.assertIn(expected, result['reason'])

    def test_attachment_converter_rejects_oversized_file_and_cleans_excel_nan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'sample.xlsx')
            with open(path, 'wb') as f:
                f.write(b'oversized')
            oversized = converter.convert_file(path, os.path.join(directory, 'out'), max_bytes=2)
            self.assertEqual(oversized['status'], 'skipped')
            self.assertIn('文件超过', oversized['reason'])

        cleaned = converter._clean_markitdown_output('| A | B |\n| --- | --- |\n| 2201A | NaN |', '.xlsx')
        self.assertNotIn('NaN', cleaned)
        self.assertIn('2201A', cleaned)

    def test_attachment_converter_rejects_empty_content_and_routes_only_pptx(self):
        with tempfile.TemporaryDirectory() as directory:
            docx = os.path.join(directory, 'empty.docx')
            pptx = os.path.join(directory, 'slides.pptx')
            ppt = os.path.join(directory, 'legacy.ppt')
            for path in (docx, pptx, ppt):
                with open(path, 'wb') as f:
                    f.write(b'x')
            with mock.patch.object(converter, '_markitdown_to_markdown', return_value=''):
                empty = converter.convert_file(docx, os.path.join(directory, 'out'))
            self.assertEqual(empty['status'], 'unsupported')
            self.assertIn('未提取到可读内容', empty['reason'])
            with mock.patch.object(converter, '_markitdown_to_markdown', return_value='演示内容') as markitdown:
                slides = converter.convert_file(pptx, os.path.join(directory, 'out'))
            self.assertEqual(slides['status'], 'converted')
            self.assertEqual(slides['converter'], 'markitdown')
            markitdown.assert_called_once()
            legacy = converter.convert_file(ppt, os.path.join(directory, 'out'))
            self.assertEqual(legacy['status'], 'unsupported')

    def test_attachment_converter_precheck_reports_actual_format_capabilities(self):
        with mock.patch.object(converter, '_module_ready', return_value=True), \
                mock.patch.object(converter, '_soffice_info', return_value={
                    'ready': True, 'path': '/usr/bin/soffice', 'version': 'LibreOffice 7.4'}), \
                mock.patch.object(converter.importlib.metadata, 'version', return_value='0.1.7'), \
                mock.patch.dict(os.environ, {'AUTO_REQ_RUNTIME_IMAGE': 'office:test'}):
            ready = converter.precheck()
        self.assertTrue(ready['ok'])
        self.assertEqual(ready['markitdown']['version'], '0.1.7')
        self.assertTrue(ready['capabilities']['.pptx']['ready'])
        self.assertTrue(all(ready['formats'].values()))
        self.assertTrue(ready['fixed_runtime_verified'])

        def modules(name):
            return name != 'xlrd'

        with mock.patch.object(converter, '_module_ready', side_effect=modules), \
                mock.patch.object(converter, '_soffice_info', return_value={
                    'ready': True, 'path': '/usr/bin/soffice', 'version': 'LibreOffice 7.4'}), \
                mock.patch.object(converter.importlib.metadata, 'version', return_value='0.1.7'), \
                mock.patch.dict(os.environ, {'AUTO_REQ_RUNTIME_IMAGE': 'office:test'}):
            missing = converter.precheck()
        self.assertTrue(missing['ok'])
        self.assertIn('libreoffice+ooxml-parser', missing['capabilities']['.xls']['chains'])

        with mock.patch.object(converter, '_module_ready', return_value=True), \
                mock.patch.object(converter, '_soffice_info', return_value={
                    'ready': True, 'path': '/usr/bin/soffice', 'version': 'LibreOffice 7.4'}), \
                mock.patch.object(converter.importlib.metadata, 'version', return_value='0.1.7'), \
                mock.patch.dict(os.environ, {}, clear=True):
            host_only = converter.precheck()
        self.assertTrue(host_only['ok'])
        self.assertFalse(host_only['fixed_runtime_verified'])
        self.assertEqual(host_only['runtime_mode'], 'builtin-only')
        self.assertTrue(host_only['warnings'])

    def test_attachment_precheck_does_not_block_pdf_for_unrelated_office_dependency(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(converter, '_module_ready', side_effect=lambda name: name == 'fitz'), \
                mock.patch.object(converter, '_soffice_info', return_value={
                    'ready': False, 'path': None, 'version': None}):
            with open(os.path.join(directory, '规范.pdf'), 'wb') as handle:
                handle.write(b'pdf')
            result = converter.precheck(directory)
        self.assertTrue(result['ok'])
        self.assertEqual(result['requested_formats'], ['.pdf'])
        self.assertEqual(result['blocked_formats'], [])

    def test_attachment_runtime_inventory_cannot_inject_install_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ('正常.pdf', '恶意;brew install bad.xls', '说明.txt'):
                with open(os.path.join(directory, name), 'wb') as handle:
                    handle.write(b'x')
            extensions = attachment_runtime._inventory_extensions(directory)
        self.assertEqual(extensions, ['.pdf', '.txt', '.xls'])
        self.assertEqual(attachment_runtime._requirement_groups(extensions),
                         ['python-markitdown'])

    def test_attachment_dependency_lock_pins_markitdown_with_hashes(self):
        direct = (attachment_runtime.SKILL_ROOT / 'runtime' /
                  'requirements-attachments.txt').read_text(encoding='utf-8').strip()
        locked = attachment_runtime.LOCK_FILES['python-markitdown'].read_text(encoding='utf-8')
        self.assertEqual(direct, 'markitdown[xls,xlsx,pdf,docx,pptx]==0.1.7')
        self.assertIn('markitdown==0.1.7', locked)
        self.assertIn('--hash=sha256:', locked)

    def test_attachment_runtime_rejects_corrupt_cache_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = pathlib.Path(directory)
            python = runtime / 'venv' / 'bin' / 'python'
            python.parent.mkdir(parents=True)
            python.write_text('', encoding='utf-8')
            (runtime / 'runtime-manifest.json').write_text('{bad json', encoding='utf-8')
            self.assertFalse(attachment_runtime._runtime_valid(runtime, 'cache-key'))

    def test_attachment_runtime_system_install_commands_are_fixed(self):
        with mock.patch.object(attachment_runtime.os, 'geteuid', return_value=501), \
                mock.patch.object(attachment_runtime.shutil, 'which', return_value='/usr/bin/sudo'):
            linux = attachment_runtime._system_install_commands('Linux', '/usr/bin/apt-get')
        self.assertEqual(linux[0], ['/usr/bin/sudo', '-n', '/usr/bin/apt-get', 'update'])
        self.assertEqual(linux[1][-3:],
                         ['libreoffice-calc', 'libreoffice-writer', 'fonts-noto-cjk'])
        self.assertEqual(
            attachment_runtime._system_install_commands('Darwin', '/opt/homebrew/bin/brew'),
            [['/opt/homebrew/bin/brew', 'install', '--cask', 'libreoffice']])

    def test_attachment_runtime_redacts_credentials_from_install_errors(self):
        redacted = attachment_runtime._redact(
            'https://user:secret@example.test/simple TFS_PAT=abc TOKEN=def')
        self.assertNotIn('secret', redacted)
        self.assertNotIn('abc', redacted)
        self.assertNotIn('def', redacted)

    def test_attachment_runtime_reuses_valid_cache_without_reinstalling(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'source')
            os.makedirs(source)
            with open(os.path.join(source, '规范.pdf'), 'wb') as handle:
                handle.write(b'pdf')
            with mock.patch.object(attachment_runtime, '_cache_key', return_value='cache-key'), \
                    mock.patch.object(attachment_runtime, '_runtime_valid', return_value=True), \
                    mock.patch.object(attachment_runtime, '_smoke_runtime', return_value={
                        'ok': True, 'requested_formats': ['.pdf'], 'capabilities': {},
                        'blocked_formats': [], 'warnings': []}), \
                    mock.patch.object(attachment_runtime, '_ensure_libreoffice', return_value={
                        'ready': True, 'path': '/usr/bin/soffice', 'version': 'LibreOffice'}), \
                    mock.patch.object(attachment_runtime, '_find_python') as find_python:
                prepared = attachment_runtime.prepare_runtime(
                    source, os.path.join(directory, 'runtime'))
        self.assertEqual(prepared['preflight']['install_required'], [])
        self.assertEqual(prepared['preflight']['installations'], [])
        find_python.assert_not_called()

    def test_attachment_runtime_still_runs_converter_after_prepare_degradation(self):
        prepared = {
            'python': sys.executable,
            'environment': os.environ.copy(),
            'preflight': {'ok': False, 'warnings': ['install failed']},
        }
        completed = mock.Mock(returncode=0, stdout=json.dumps({
            'total': 2, 'converted': 1, 'needs_read': [], 'skipped': 1,
            'errors': 0, 'files': []}), stderr='')
        with mock.patch.object(attachment_runtime, 'prepare_runtime', return_value=prepared), \
                mock.patch.object(attachment_runtime.subprocess, 'run', return_value=completed):
            result = attachment_runtime.convert('in', 'out', 1024)
        self.assertTrue(result['ok'])
        self.assertEqual(result['converted'], 1)
        self.assertEqual(result['preflight']['warnings'], ['install failed'])

    def test_attachment_runtime_doc_system_failure_does_not_discard_python_runtime(self):
        completed = mock.Mock(returncode=1, stdout=json.dumps({
            'ok': False, 'markitdown': {'ready': True}, 'blocked_formats': ['.doc'],
        }), stderr='')
        with mock.patch.object(attachment_runtime.subprocess, 'run', return_value=completed):
            result = attachment_runtime._smoke_runtime(
                sys.executable, 'input', ['python-markitdown'], os.environ.copy())
        self.assertEqual(result['blocked_formats'], ['.doc'])

    def test_attachment_runtime_rejects_missing_direct_markitdown_capability(self):
        completed = mock.Mock(returncode=1, stdout=json.dumps({
            'ok': False, 'markitdown': {'ready': True}, 'blocked_formats': ['.pdf'],
        }), stderr='')
        with mock.patch.object(attachment_runtime.subprocess, 'run', return_value=completed), \
                self.assertRaisesRegex(RuntimeError, '格式烟测失败'):
            attachment_runtime._smoke_runtime(
                sys.executable, 'input', ['python-markitdown'], os.environ.copy())

    def test_menu_index_keeps_products_separate_by_area_and_trims_noise(self):
        def source(mcode, caption):
            return {
                'total_menus': 1,
                'modules': {'业务': {'menus': [{
                    'mcode': mcode, 'pcaption': caption, 'menu_path': f'业务 > {caption}',
                    'business_domain': '业务域', 'match_status': 'matched',
                    'repo': 'repo-a', 'apis': ['/api/example'], 'vue_file': 'noise.vue',
                    'page_type': 'unknown', 'business_keywords': [],
                }]}},
            }

        with tempfile.TemporaryDirectory() as directory:
            for name, value in (('a.json', source('01', '同名菜单')), ('b.json', source('01', '同名菜单'))):
                with open(os.path.join(directory, name), 'w', encoding='utf-8') as f:
                    json.dump(value, f)
            manifest = {
                'sources': [
                    {'product_id': 'product-a', 'product_name': '产品 A',
                     'tfs_area_values': ['AREA-A'], 'source': 'a.json'},
                    {'product_id': 'product-b', 'product_name': '产品 B',
                     'tfs_area_values': ['AREA-B'], 'source': 'b.json'},
                ],
            }
            manifest_path = os.path.join(directory, 'sources.json')
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest, f)
            index = menu_index.build_index(manifest_path)
            self.assertEqual(index['total_menus'], 2)
            self.assertEqual({item['product_id'] for item in index['menus']}, {'product-a', 'product-b'})
            self.assertEqual([product['product_id'] for product in menu_index.products_for_area(index, 'AREA-A')],
                             ['product-a'])
            self.assertEqual(menu_index.products_for_area(index, 'UNKNOWN'), [])
            self.assertNotIn('vue_file', index['menus'][0])
            self.assertNotIn('page_type', index['menus'][0])
            duplicate = dict(manifest)
            duplicate['sources'] = [dict(item) for item in manifest['sources']]
            duplicate['sources'][1]['tfs_area_values'] = ['AREA-A']
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(duplicate, f)
            with self.assertRaisesRegex(ValueError, '区域只能归属一个产品'):
                menu_index.build_index(manifest_path)

    def _write_tfs_config(self, directory, collection='CfgColl', project='健康医养'):
        path = os.path.join(directory, 'tfs-config.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'tfs': {'server': 'tfs.example', 'port': 8080,
                               'collection': collection, 'project': project, 'pat': 'secret'}}, f)
        return path

    def test_load_config_collection_override_rebuilds_base_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_tfs_config(directory, collection='CfgColl')
            # 隔离环境变量，确保测的是 override 本身
            with mock.patch.dict(os.environ, {}, clear=True):
                client = tfs.load_config(path, collection_override='Other-Collection')
        self.assertEqual(client['collection'], 'Other-Collection')
        # 覆盖值编进 base_url；中文 project 仍按既有路径编码（勿重复编码）
        project_encoded = tfs.urllib.parse.quote('健康医养')
        self.assertIn(f'/tfs/Other-Collection/{project_encoded}', client['base_url'])

    def test_load_config_collection_priority_cli_over_env_over_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_tfs_config(directory, collection='CfgColl')
            # 配置默认（无 env、无 override）
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(tfs.load_config(path)['collection'], 'CfgColl')
            # 环境变量 TFS_COLLECTION > 配置
            with mock.patch.dict(os.environ, {'TFS_COLLECTION': 'EnvColl'}, clear=True):
                self.assertEqual(tfs.load_config(path)['collection'], 'EnvColl')
                # CLI override > 环境变量 > 配置
                self.assertEqual(
                    tfs.load_config(path, collection_override='CliColl')['collection'], 'CliColl')

    def test_load_config_does_not_write_collection_back_to_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_tfs_config(directory, collection='CfgColl')
            with open(path, 'r', encoding='utf-8') as f:
                before = f.read()
            with mock.patch.dict(os.environ, {}, clear=True):
                tfs.load_config(path, collection_override='OtherColl')
            with open(path, 'r', encoding='utf-8') as f:
                self.assertEqual(f.read(), before)

    def test_load_config_project_override_rebuilds_base_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_tfs_config(directory, project='CfgProj')
            with mock.patch.dict(os.environ, {}, clear=True):
                client = tfs.load_config(path, project_override='Other-Project')
        self.assertEqual(client['project'], 'Other-Project')
        collection_encoded = tfs.urllib.parse.quote('CfgColl')
        self.assertIn(f'/tfs/{collection_encoded}/{tfs.urllib.parse.quote("Other-Project")}',
                      client['base_url'])

    def test_load_config_project_priority_cli_over_env_over_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_tfs_config(directory, project='CfgProj')
            # 配置默认（无 env、无 override）
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(tfs.load_config(path)['project'], 'CfgProj')
            # 环境变量 TFS_PROJECT > 配置
            with mock.patch.dict(os.environ, {'TFS_PROJECT': 'EnvProj'}, clear=True):
                self.assertEqual(tfs.load_config(path)['project'], 'EnvProj')
                # CLI override > 环境变量 > 配置
                self.assertEqual(
                    tfs.load_config(path, project_override='CliProj')['project'], 'CliProj')

    def test_list_iterations_project_flag_via_main(self):
        # list-iterations 的 --project 迁到共享 conn_parent 后：仍必填，且同时进
        # load_config(project_override) 与 list_iterations(project)。
        with mock.patch.object(tfs, 'load_config', return_value={'base_url': 'u'}) as lc, \
                mock.patch.object(tfs, 'list_iterations', return_value={'ok': True}) as li, \
                mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(sys, 'argv',
                                   ['tfs_client.py', 'list-iterations', '--expected-date', '2026-08-31']):
                self.assertRaises(SystemExit, tfs.main)  # 缺 --project → ap.error
            with mock.patch.object(sys, 'argv',
                                   ['tfs_client.py', '--config', 'c.json',
                                    'list-iterations', '--project', 'TeamProj']):
                tfs.main()
        self.assertEqual(li.call_args.args[1], 'TeamProj')
        self.assertEqual(lc.call_args.args[-1], 'TeamProj')


class PipelinePlanTests(unittest.TestCase):
    def write_analysis_plan(self, directory, categories, run_id='run_12345678',
                            analysis_rule='fallback-v1', analysis_profile=None):
        artifact_name = f'变更方案_1_{run_id}.md'
        plan_path = os.path.join(directory, 'plan.json')
        lines = [
            '# 变更方案',
            f'<!-- auto-req-run:{run_id} -->',
        ]
        if analysis_rule == 'evidence-loop-v1':
            lines.extend([
                '## 二、迭代分析闭环',
                '### 现状基线',
                '- **用户与场景**：测试人员在测试功能编辑记录。',
                '- **当前行为或规则**：当前保存后沿用既有校验。',
                '- **既有边界**：不改变权限和数据写入。',
                '### 问题与目标',
                '- **触发条件**：测试人员保存记录时需要明确提示。',
                '- **业务影响**：减少重复确认和操作误解。',
                '- **根因判断**：现有提示未明确表达保存结果。',
                '- **业务目标**：保存结果可被操作人员直接识别。',
                '### 差异与范围',
                '- **现状**：保存结果提示不清晰。',
                '- **目标状态**：保存后展示明确提示。',
                '- **保持不变项**：保存流程、数据写入和权限保持不变。',
                '- **受影响触点**：测试功能编辑页保存提示。',
                '### 方案取舍',
                '- **推荐方案**：调整既有保存提示文案。',
                '- **替代方案或不适用**：不适用：不涉及新增业务流程。',
                '- **选择理由**：在不改变既有逻辑的前提下满足提示目标。',
                '### 成功衡量与非目标',
                '- **成功衡量**：保存后操作人员可识别成功结果。',
                '- **非目标**：不新增字段、流程、权限或数据修改。',
            ])
        lines.append('## 三、分析者描述')
        if analysis_profile != 'concise-v3':
            lines.extend([
                '- **需求类别**：' + '、'.join(f'`{category}`' for category in categories),
                '- **路径**：菜单路径：业务管理 > 测试功能；操作路径：测试功能 → 编辑 → 保存',
            ])
        if analysis_rule == 'evidence-loop-v1' and analysis_profile != 'concise-v3':
            lines.extend([
                '- **决策结论**：调整既有保存提示，使操作结果可直接识别。',
                '- **生效路径与条件**：测试功能编辑页保存成功后生效。',
                '- **决策边界**：不改变保存流程、数据写入和权限。',
                '- **验收要点**：保存后操作人员可识别成功提示。',
            ])
        for category in categories:
            lines.append(f'### {category}（测试类别）')
            if analysis_profile == 'concise-v1':
                requirements = pipeline.CONCISE_ANALYSIS_DESCRIPTION_REQUIREMENTS[category]
            elif analysis_profile == 'concise-v2':
                requirements = pipeline.CONCISE_V2_ANALYSIS_DESCRIPTION_REQUIREMENTS[category]
            elif analysis_profile == 'concise-v3':
                requirements = pipeline.CONCISE_V3_ANALYSIS_DESCRIPTION_REQUIREMENTS[category]
            else:
                requirements = pipeline.ANALYSIS_DESCRIPTION_REQUIREMENTS[category]
            lines.extend(f'- **{label}**：已明确{label}'
                         for label in requirements)
        if analysis_rule == 'evidence-loop-v1':
            lines.extend([
                '## 四、范围—方案—验收追踪',
                '| ID | 范围/改动点 | 方案/目标行为 | 验收场景与结果 | 结论状态 | 依据或缺口 |',
                '| --- | --- | --- | --- | --- | --- |',
                '| R1 | 保存结果提示 | 保存后展示明确提示 | 保存成功后操作人员可识别结果 | 已证实 | 工作项 |',
            ])
        with open(os.path.join(directory, artifact_name), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        plan = {
            'version': pipeline.PLAN_VERSION if analysis_rule == 'evidence-loop-v1' else 1,
            'run_id': run_id, 'skill': 'auto-req-analysis', 'work_item_id': 1,
            'expected_rev': 5, 'expected_state': '活动', 'verdict': 'AUTO-ANA',
            'tags': ['PM-AI-AUTO-ANA'], 'state_to': '已分析',
            'rules_source': {'qc': 'pre-qc-v1', 'analysis': analysis_rule},
            'auto_scopes': ['field-ui-copy'],
            'analysis_description': {'categories': categories},
            'analysis_gaps': [],
            'kb': {
                'ready': True, 'dedup_ran': True, 'tools_used': ['list_repos'],
                'findings': [{'entity': '测试锚点', 'state': '已证实', 'source_tool': 'context'}],
                'note': 'test',
            },
            'artifacts': [{'kind': 'change-plan', 'path': artifact_name}],
        }
        if analysis_profile is not None:
            plan['analysis_profile'] = analysis_profile
        if analysis_rule == 'evidence-loop-v1':
            plan.update({
                'evidence_refs': {
                    '现状基线': ['kb:0'],
                    '问题与目标': ['work-item'],
                    '差异与范围': ['kb:0'],
                    '方案取舍': ['kb:0'],
                    '成功衡量与非目标': ['work-item'],
                },
                'evidence_gaps': [],
            })
        return plan, plan_path, os.path.join(directory, artifact_name)

    def write_current_analysis_plan(self, directory, categories, run_id='run_12345678'):
        """生成当前 concise-v3 分析计划；旧 helper 默认值保留历史格式测试。"""
        return self.write_analysis_plan(
            directory, categories, run_id=run_id,
            analysis_rule='evidence-loop-v1', analysis_profile='concise-v3')

    def make_manual_plan(self, directory, gaps):
        plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
        plan.update({
            'verdict': 'MANUAL-REVIEW',
            'tags': ['PM-AI-MANUAL-REVIEW'],
            'state_to': None,
            'analysis_gaps': gaps,
        })
        plan.pop('auto_scopes')
        return plan, plan_path

    def requirements_evidence(self, state='已证实', source_tool='get_work_item'):
        return {
            'ready': True,
            'coverage': {
                'collection': 'WN_PH-Platform', 'project': 'NETHIS5.5',
                'created_from': '2023-07-31T00:00:00Z',
                'created_before': '2026-08-01T00:00:00Z',
            },
            'tools_used': list(dict.fromkeys(['get_requirements_summary', source_tool])),
            'findings': [{
                'work_item_id': 260001,
                'fact': '历史需求明确保存后提示的验收条件。',
                'state': state,
                'source_tool': source_tool,
            }],
            'note': '已读取覆盖范围并核验历史工作项正文。',
        }

    def complete_acquisition(self, **overrides):
        """构造四源均 COMPLETE+exhausted+HIT 的合法 evidence_acquisition（v2 基线）。"""
        acq = {}
        for source in pipeline.EVIDENCE_ACQUISITION_SOURCES:
            acq[source] = {
                'availability': 'READY',
                'coverage_status': 'COMPLETE',
                'query_status': 'HIT',
                'queries': [{'terms': '测试查询词', 'truncated': False, 'returned': 1}],
                'stop_reason': 'exhausted',
            }
        for source, override in overrides.items():
            acq[source] = {**acq[source], **override}
        return acq

    def write_v2_analysis_plan(self, directory, categories, run_id='run_12345678',
                               acquisition=None):
        """evidence-loop-v2 计划：复用 v1 证据闭环骨架，换规则源并挂 evidence_acquisition。"""
        plan, plan_path, artifact = self.write_analysis_plan(
            directory, categories, run_id=run_id, analysis_rule='evidence-loop-v1')
        plan['rules_source']['analysis'] = 'evidence-loop-v2'
        plan['evidence_acquisition'] = (
            acquisition if acquisition is not None else self.complete_acquisition())
        return plan, plan_path, artifact

    def test_v1_evidence_loop_plan_still_passes_without_evidence_acquisition(self):
        # 向后兼容：evidence-loop-v1 计划不要求 evidence_acquisition
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            self.assertNotIn('evidence_acquisition', plan)
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_v2_complete_acquisition_plan_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(directory, ['existing-ui-simple'])
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_v2_plan_requires_evidence_acquisition_object(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(directory, ['existing-ui-simple'])
            plan.pop('evidence_acquisition')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('必须含 evidence_acquisition 对象' in e for e in result['errors']))

    def test_v2_plan_requires_all_four_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(directory, ['existing-ui-simple'])
            plan['evidence_acquisition'].pop('db_knowledge')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('evidence_acquisition 缺少来源' in e for e in result['errors']))

    def test_v2_dedup_ran_requires_non_empty_queries(self):
        # 约束1：dedup_ran=true 但 tfs_requirements.queries 为空 → 失败
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(directory, ['existing-ui-simple'])
            plan['evidence_acquisition']['tfs_requirements']['queries'] = []
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('dedup_ran=true' in e and '不得为空' in e for e in result['errors']))

    def test_v2_auto_requires_complete_or_outofscope_dedup_coverage(self):
        # 约束2：AUTO-ANA 查重覆盖 PARTIAL → 失败（须改判 MANUAL）
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(directory, ['existing-ui-simple'])
            plan['evidence_acquisition']['tfs_requirements']['coverage_status'] = 'PARTIAL'
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('AUTO-ANA' in e and 'coverage_status' in e for e in result['errors']))

    def test_v2_complete_requires_exhausted_and_no_truncation(self):
        # 约束3：COMPLETE 须 availability=READY ∧ stop_reason=exhausted ∧ 无截断
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(directory, ['existing-ui-simple'])
            plan['evidence_acquisition']['gitnexus']['stop_reason'] = 'verified_hit'
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('coverage_status=COMPLETE 要求 stop_reason=exhausted' in e
                                for e in result['errors']))
            plan['evidence_acquisition']['gitnexus']['stop_reason'] = 'exhausted'
            plan['evidence_acquisition']['gitnexus']['queries'] = [{'terms': '词', 'truncated': True}]
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('truncated=true' in e for e in result['errors']))

    def test_v2_no_hit_requires_complete_coverage(self):
        # 约束4：query_status=NO_HIT 须 coverage_status=COMPLETE（覆盖不全不得声明无命中）
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(directory, ['existing-ui-simple'])
            plan['evidence_acquisition']['wiki']['query_status'] = 'NO_HIT'
            plan['evidence_acquisition']['wiki']['coverage_status'] = 'PARTIAL'
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('query_status=NO_HIT 要求 coverage_status=COMPLETE' in e
                                for e in result['errors']))

    def test_v2_maturity_landed_requires_confirmed_state(self):
        # 约束5：maturity=已落地 须 state=已证实
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(directory, ['existing-ui-simple'])
            plan['tfs_requirements'] = self.requirements_evidence(state='候选')
            plan['tfs_requirements']['findings'][0]['maturity'] = '已落地'
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('maturity=已落地 要求 state=已证实' in e for e in result['errors']))

    def test_v2_bad_enum_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(directory, ['existing-ui-simple'])
            plan['evidence_acquisition']['wiki']['availability'] = 'MAYBE'
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('evidence_acquisition.wiki.availability 必须为' in e
                                for e in result['errors']))

    def test_evidence_gaps_accept_background_kind_type_and_product_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            plan.update({'verdict': 'MANUAL-REVIEW', 'tags': ['PM-AI-MANUAL-REVIEW'], 'state_to': None})
            plan.pop('auto_scopes')
            plan['evidence_gaps'] = [{
                'id': 'gap-wiki-missing', 'topic': '物资管理 wiki',
                'missing': 'wiki 未覆盖物资管理模块', 'impact': '无法佐证物资业务边界',
                'owner': '产品', 'next_action': '补建 wiki 物资管理文章',
                'type': 'WIKI_TOPIC_MISSING', 'kind': 'background',
            }]
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_evidence_gaps_reject_unknown_type(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            plan.update({'verdict': 'MANUAL-REVIEW', 'tags': ['PM-AI-MANUAL-REVIEW'], 'state_to': None})
            plan.pop('auto_scopes')
            plan['evidence_gaps'] = [{
                'id': 'gap-bad', 'topic': 'x', 'missing': 'y', 'impact': 'z',
                'owner': '研发', 'next_action': '补', 'type': 'NOT_A_REAL_TYPE'}]
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('evidence_gaps[1].type 必须为' in e for e in result['errors']))

    def test_evidence_loop_plan_requires_complete_closure_and_keeps_legacy_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy, legacy_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            self.assertTrue(pipeline.validate_plan(legacy, legacy_path)['ok'])
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_evidence_loop_requires_decision_summary_and_scope_solution_acceptance_traceability(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            with open(artifact_path, 'r', encoding='utf-8') as f:
                original = f.read()

            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(original.replace('- **验收要点**：保存后操作人员可识别成功提示。\n', ''))
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('分析者描述必须且只能包含一个非空“验收要点”决策摘要', result['errors'])

            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(original.replace('| 已证实 | 工作项 |', '| 待业务确认 | 工作项 |'))
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('待业务确认必须关联既有 analysis-gap:<id>' in error
                                for error in result['errors']))

            gap = {
                'id': 'gap-prompt-rule', 'topic': '提示规则', 'missing': '保存失败时是否沿用既有提示',
                'impact': '无法确定失败场景验收', 'question': '保存失败时是否沿用既有提示？',
                'options': ['沿用既有提示'], 'allow_other': True,
            }
            plan.update({
                'verdict': 'MANUAL-REVIEW', 'tags': ['PM-AI-MANUAL-REVIEW'], 'state_to': None,
                'analysis_gaps': [gap],
            })
            plan.pop('auto_scopes')
            followup_name = f'待确认清单_1_{plan["run_id"]}.md'
            plan['artifacts'].append({'kind': 'manual-followup', 'path': followup_name})
            with open(os.path.join(directory, followup_name), 'w', encoding='utf-8') as f:
                f.write('\n'.join([
                    f'<!-- auto-req-run:{plan["run_id"]} -->',
                    '## 需要确认的需求分析信息',
                    '### gap-prompt-rule · 提示规则',
                    '<!-- analysis-gap:gap-prompt-rule -->',
                    '- **缺失信息**：保存失败时是否沿用既有提示',
                    '- **对分析/验收的影响**：无法确定失败场景验收',
                    '- **需确认的问题**：保存失败时是否沿用既有提示？',
                    '- **候选口径**：沿用既有提示',
                    '- **允许自由补充**：是',
                ]))
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(original.replace('| 已证实 | 工作项 |', '| 待业务确认 | analysis-gap:gap-prompt-rule |'))
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_skip_analysis_is_terminal_and_has_no_tfs_mutation_contract(self):
        plan = {
            'version': 1, 'run_id': 'run_skip_1234', 'skill': 'auto-req-analysis',
            'work_item_id': 1, 'expected_rev': 5, 'expected_state': '已建议',
            'verdict': 'SKIP-ANALYSIS', 'rules_source': {'qc': 'pre-qc-v1'},
            'tags': [], 'state_to': None, 'artifacts': [],
            'skip_reason': '接口已开发完成，本工作项仅安排现场联调，无新增业务分析面。',
        }
        result = pipeline.validate_plan(plan, '/tmp/skip-plan.json')
        self.assertTrue(result['ok'])
        self.assertEqual(result['expected_tags'], [])
        self.assertIsNone(result['state_to'])
        self.assertEqual(pipeline.expected_for(plan), (set(), None, set()))

        plan.pop('skip_reason')
        result = pipeline.validate_plan(plan, '/tmp/skip-plan.json')
        self.assertFalse(result['ok'])
        self.assertIn('SKIP-ANALYSIS 计划必须含非空 skip_reason', result['errors'])

        plan['skip_reason'] = '仅联调'
        plan['checklist'] = {'items': []}
        result = pipeline.validate_plan(plan, '/tmp/skip-plan.json')
        self.assertFalse(result['ok'])
        self.assertIn('SKIP-ANALYSIS 计划不得声明 checklist', result['errors'])

    def test_apply_skip_analysis_records_result_without_tfs_mutation(self):
        plan = {
            'version': 1, 'run_id': 'run_skip_1234', 'skill': 'auto-req-analysis',
            'work_item_id': 1, 'expected_rev': 5, 'expected_state': '已建议',
            'verdict': 'SKIP-ANALYSIS', 'rules_source': {'qc': 'pre-qc-v1'},
            'tags': [], 'state_to': None, 'artifacts': [],
            'skip_reason': '接口已开发完成，本工作项仅安排现场联调，无新增业务分析面。',
        }
        with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                mock.patch.object(pipeline, 'preflight',
                                  return_value={'ok': True, 'work_item': {'tags': ['PM-AI-AUTO-ANA']}}), \
                mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}) as publish, \
                mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}), \
                mock.patch.object(tfs, 'remove_tag') as remove_tag, \
                mock.patch.object(tfs, 'add_tag') as add_tag, \
                mock.patch.object(tfs, 'upload_attachment') as upload_attachment, \
                mock.patch.object(tfs, 'replace_detail_analysis_section') as replace_detail, \
                mock.patch.object(tfs, 'write_field') as write_field, \
                mock.patch.object(tfs, 'set_state') as set_state, \
                mock.patch.object(tfs, 'set_assignee') as set_assignee:
            response = pipeline.apply_plan(plan, '/tmp/skip-plan.json', True, 'config.json')
        self.assertTrue(response['ok'])
        self.assertEqual(response['actions'], [])
        publish.assert_called_once_with(plan, 'execute', 'C', 'config.json',
                                        analysis_description_html='', work_item='')
        for mutation in (remove_tag, add_tag, upload_attachment, replace_detail,
                         write_field, set_state, set_assignee):
            mutation.assert_not_called()

    def test_concise_profile_reduces_simple_analysis_dimensions_and_keeps_legacy_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            concise, concise_path, concise_artifact = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1',
                analysis_profile='concise-v1')
            self.assertTrue(pipeline.validate_plan(concise, concise_path)['ok'])
            with open(concise_artifact, 'r', encoding='utf-8') as f:
                content = f.read()
            self.assertIn('- **核心改造点**：', content)
            self.assertNotIn('- **参数控制**：', content)

        with tempfile.TemporaryDirectory() as directory:
            invalid, invalid_path, _ = self.write_analysis_plan(
                directory, ['bug-fix'], analysis_rule='evidence-loop-v1')
            invalid['analysis_profile'] = 'concise-v1'
            result = pipeline.validate_plan(invalid, invalid_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'concise-v1 仅允许单一 existing-ui-simple / print-adjustment / data-management 类别',
                result['errors'])

        with tempfile.TemporaryDirectory() as directory:
            concise_v2, concise_v2_path, concise_v2_artifact = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1',
                analysis_profile='concise-v2')
            self.assertTrue(pipeline.validate_plan(concise_v2, concise_v2_path)['ok'])
            with open(concise_v2_artifact, 'r', encoding='utf-8') as f:
                content = f.read()
            self.assertIn('- **核心改造点**：', content)
            self.assertNotIn('- **生效场景**：', content)
            self.assertNotIn('- **不涉及范围**：', content)

    def test_concise_v3_supports_all_categories_and_multiple_categories(self):
        categories = tuple(pipeline.CONCISE_V3_ANALYSIS_DESCRIPTION_REQUIREMENTS)
        self.assertEqual(set(categories), set(pipeline.ANALYSIS_DESCRIPTION_REQUIREMENTS))
        for category in categories:
            with tempfile.TemporaryDirectory() as directory:
                plan, plan_path, _ = self.write_current_analysis_plan(directory, [category])
                self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'], category)

        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_current_analysis_plan(
                directory, ['report', 'third-party-new', 'existing-complex'])
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_concise_v3_rejects_missing_category_dimensions(self):
        for category, requirements in pipeline.CONCISE_V3_ANALYSIS_DESCRIPTION_REQUIREMENTS.items():
            with tempfile.TemporaryDirectory() as directory:
                plan, plan_path, artifact_path = self.write_current_analysis_plan(directory, [category])
                missing = requirements[0]
                with open(artifact_path, 'r', encoding='utf-8') as f:
                    content = f.read().replace(f'- **{missing}**：已明确{missing}\n', '')
                with open(artifact_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                result = pipeline.validate_plan(plan, plan_path)
                self.assertFalse(result['ok'], category)
                self.assertIn(f'分析类别 {category} 缺少非空维度“{missing}”', result['errors'])

    def test_concise_v3_rejects_public_metadata_and_decision_summaries(self):
        forbidden = {
            '需求类别': '`existing-ui-simple`',
            '路径': '菜单路径：业务管理；操作路径：编辑 → 保存',
            '决策结论': '调整保存提示。',
            '生效路径与条件': '保存成功后生效。',
            '决策边界': '不改变数据写入。',
            '验收要点': '保存后显示新提示。',
        }
        for label, value in forbidden.items():
            with tempfile.TemporaryDirectory() as directory:
                plan, plan_path, artifact_path = self.write_current_analysis_plan(
                    directory, ['existing-ui-simple'])
                with open(artifact_path, 'r', encoding='utf-8') as f:
                    content = f.read().replace(
                        '## 三、分析者描述\n',
                        f'## 三、分析者描述\n- **{label}**：{value}\n')
                with open(artifact_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                result = pipeline.validate_plan(plan, plan_path)
                self.assertFalse(result['ok'], label)
                self.assertTrue(any(label in error for error in result['errors']), result['errors'])

    def test_concise_v3_rejects_legacy_version_and_extra_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            plan['version'] = 1
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('concise-v3 仅允许 version=2 的新分析计划', result['errors'])

            plan['version'] = pipeline.PLAN_VERSION
            with open(artifact_path, 'r', encoding='utf-8') as f:
                content = f.read().replace(
                    '- **界面优化方案**：已明确界面优化方案\n',
                    '- **界面优化方案**：已明确界面优化方案\n- **影响范围**：重复展开\n')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(content)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                "分析类别 existing-ui-simple 含 concise-v3 非法维度：['影响范围']",
                result['errors'])

    def test_concise_v3_allows_business_sections_after_analysis_description(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            with open(artifact_path, 'r', encoding='utf-8') as f:
                content = f.read() + '\n## 五、业务规则与变更范围\n- **业务规则**：保存后显示明确提示。\n'
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(content)
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_v2_plan_cannot_bypass_evidence_loop_with_fallback_rule_source(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['version'] = pipeline.PLAN_VERSION
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                "rules_source.analysis 必须为 ['evidence-loop-v1', 'evidence-loop-v2']",
                result['errors'])

    def test_evidence_refs_bind_auto_conclusions_and_evidence_gaps_stay_internal(self):
        evidence_gap = {
            'id': 'evidence-current-entry', 'topic': '现有入口定位',
            'missing': '未找到现有保存提示的已证实入口', 'impact': '无法证明复用边界',
            'owner': '研发', 'next_action': '补充入口与调用关系证据后重新分析',
        }
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            plan['evidence_refs']['方案取舍'] = ['work-item']
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('AUTO-ANA 的 evidence_refs.方案取舍 必须包含 kb: 已证实证据引用', result['errors'])

            plan['evidence_refs']['方案取舍'] = ['kb:1']
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('evidence_refs.方案取舍 引用的 kb:1 必须指向 kb.findings 的已证实项', result['errors'])

            plan['evidence_refs']['方案取舍'] = ['kb:0']
            plan['evidence_gaps'] = [evidence_gap]
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('AUTO-ANA 的 evidence_gaps 必须为空数组', result['errors'])

            plan.update({
                'verdict': 'MANUAL-REVIEW', 'tags': ['PM-AI-MANUAL-REVIEW'], 'state_to': None,
            })
            plan.pop('auto_scopes')
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_requirement_history_refs_require_confirmed_detail_or_relation(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            plan['tfs_requirements'] = self.requirements_evidence()
            plan['evidence_refs']['问题与目标'] = ['work-item', 'req:0']
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

            plan['tfs_requirements']['findings'][0]['state'] = '候选'
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'evidence_refs.问题与目标 引用的 req:0 必须指向 '
                'tfs_requirements.findings 的已证实项', result['errors'])

            plan['tfs_requirements']['findings'][0]['state'] = '未确认'
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'evidence_refs.问题与目标 引用的 req:0 必须指向 '
                'tfs_requirements.findings 的已证实项', result['errors'])

            for source_tool in ('search_requirements', 'get_requirements_summary'):
                plan['tfs_requirements'] = self.requirements_evidence(source_tool=source_tool)
                result = pipeline.validate_plan(plan, plan_path)
                self.assertFalse(result['ok'])
                self.assertIn(
                    'tfs_requirements.findings[0] 只有 get_work_item 或 '
                    'get_related_work_items 的结果可标为已证实', result['errors'])

            plan['tfs_requirements'] = self.requirements_evidence()
            plan['evidence_refs']['问题与目标'] = ['req:1']
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'evidence_refs.问题与目标 引用的 req:1 必须指向 '
                'tfs_requirements.findings 的已证实项', result['errors'])

            plan['evidence_refs']['问题与目标'] = ['req:0']
            plan.pop('tfs_requirements')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'evidence_refs.问题与目标 引用的 req:0 必须指向 '
                'tfs_requirements.findings 的已证实项', result['errors'])

    def test_requirement_history_cannot_release_qc_or_replace_auto_code_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            plan['tfs_requirements'] = self.requirements_evidence()
            plan['qc_evidence_resolution'] = {
                'initial_verdict': 'NEED-REVIEW',
                'post_evidence_verdict': 'PASS',
                'items': [{
                    'id': 'history-only',
                    'initial_gap': '当前业务范围未证实。',
                    'resolution': '历史需求描述了相似范围。',
                    'evidence_refs': ['req:0'],
                }],
            }
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'qc_evidence_resolution.items[1] 的 req:0 必须指向 '
                'kb/wiki.findings 的已证实项', result['errors'])

            plan.pop('qc_evidence_resolution')
            for heading in ('现状基线', '差异与范围', '方案取舍'):
                plan['evidence_refs'][heading] = ['req:0']
            plan.pop('kb')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'AUTO-ANA 要求 kb.ready=true 且 kb.dedup_ran=true',
                '\n'.join(result['errors']))
            self.assertIn(
                'AUTO-ANA 的 evidence_refs.现状基线 必须包含 kb: 已证实证据引用',
                result['errors'])

    def test_qc_evidence_resolution_requires_confirmed_kb_or_wiki_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            plan['qc_evidence_resolution'] = {
                'initial_verdict': 'NEED-REVIEW',
                'post_evidence_verdict': 'PASS',
                'items': [{
                    'id': 'dispensing-boundary',
                    'initial_gap': '门诊发退药与药库出库的业务边界未明确。',
                    'resolution': '已证实门诊发退药归药房管理，药库出库归药库库存管理。',
                    'evidence_refs': ['kb:0'],
                }],
            }
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

            plan['qc_evidence_resolution']['items'][0]['evidence_refs'] = ['work-item']
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'qc_evidence_resolution.items[1] 的 work-item 必须指向 kb/wiki.findings 的已证实项',
                result['errors'])

            plan['wiki'] = {
                'findings': [{'entity': '门诊发退药与药库出库的业务边界', 'state': 'wiki-确认',
                              'source': 'wiki/门诊/门诊药品流转.md'}]
            }
            plan['qc_evidence_resolution']['items'][0]['evidence_refs'] = ['wiki:0']
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

            plan['qc_evidence_resolution']['post_evidence_verdict'] = 'NEED-REVIEW'
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('qc_evidence_resolution.post_evidence_verdict 必须为 PASS', result['errors'])

    def test_evidence_loop_rejects_missing_or_empty_closure_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            with open(artifact_path, 'r', encoding='utf-8') as f:
                content = f.read().replace('### 方案取舍\n', '', 1)
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(content)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('迭代分析闭环必须且只能包含一个“### 方案取舍”章节', result['errors'])

            content = content.replace('- **成功衡量**：保存后操作人员可识别成功结果。', '- **成功衡量**：', 1)
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(content)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('迭代分析闭环“成功衡量与非目标”缺少非空字段“成功衡量”', result['errors'])

    def test_evidence_loop_rejects_placeholder_business_conclusions_and_preserves_tfs_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            with open(artifact_path, 'r', encoding='utf-8') as f:
                content = f.read().replace('调整既有保存提示文案。', '按现有逻辑。', 1)
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(content)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('迭代分析闭环不得以“按现有逻辑”替代业务结论', result['errors'])

            rendered = pipeline.render_analysis_description_html(content)
            self.assertIn('<div>需求类别：existing-ui-simple</div>', rendered)
            self.assertNotIn('现状基线', rendered)

    def test_qc_followup_must_be_inline_and_matching_terminal_shape(self):
        run_id = 'run_12345678'
        with tempfile.TemporaryDirectory() as directory:
            artifact_name = f'待补充信息_1_{run_id}.md'
            artifact_path = os.path.join(directory, artifact_name)
            plan_path = os.path.join(directory, 'plan.json')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(f'<!-- auto-req-run:{run_id} -->\n')
            plan = {
                'version': 1, 'run_id': run_id, 'skill': 'auto-req-qc', 'work_item_id': 1,
                'expected_rev': 5, 'expected_state': '活动', 'verdict': 'NEED-INFO',
                'tags': ['PM-AI-QC-NEED-INFO'], 'state_to': None, 'rules_source': 'fallback',
                'artifacts': [{'kind': 'qc-followup', 'path': artifact_name}],
            }
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
            plan['artifacts'] = [{'kind': 'qc-followup', 'filename': f'待补充信息_1_{run_id}.json'}]
            plan['checklist'] = {
                'work_item': '1 测试需求', 'verdict': 'NEED-INFO',
                'tag': 'PM-AI-QC-NEED-INFO', 'responsible': '实施/创建者',
                'generated_at_utc': '2026-07-27T00:00:00Z', 'next': '补充后重新触发',
                'items': [{'id': 'q1', 'question': '请补充需求范围',
                           'options': ['全部机构', '指定机构'], 'allow_other': True}],
            }
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])
            plan['tags'] = []
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_qc_inline_followup_validates_without_disk_file(self):
        run_id = 'run_12345678'
        plan = {
            'version': 1, 'run_id': run_id, 'skill': 'auto-req-qc', 'work_item_id': 1,
            'expected_rev': 5, 'expected_state': '活动', 'verdict': 'NEED-REVIEW',
            'tags': ['PM-AI-QC-NEED-REVIEW'], 'state_to': None, 'rules_source': 'pre-qc-v1',
            'checklist': {
                'work_item': '1 测试需求', 'verdict': 'NEED-REVIEW',
                'tag': 'PM-AI-QC-NEED-REVIEW', 'responsible': '产品',
                'generated_at_utc': '2026-07-27T00:00:00Z', 'next': '补充后重新触发',
                'items': [{'id': 'q1', 'question': '校验口径?',
                           'options': ['拦截', '纠正'], 'allow_other': True}],
            },
            'artifacts': [{'kind': 'qc-followup', 'filename': f'待补充信息_1_{run_id}.json'}],
        }
        plan_path = os.path.join(tempfile.gettempdir(), 'plan.json')
        # inline qc-followup：无需磁盘文件即可通过
        self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])
        # 问题数量硬上限：超过 3 项必须先合并/收敛
        plan['checklist']['items'] = [
            {'id': f'q{index}', 'question': f'问题 {index}?',
             'options': ['口径 A', '口径 B'], 'allow_other': True}
            for index in range(1, 5)
        ]
        result = pipeline.validate_plan(plan, plan_path)
        self.assertFalse(result['ok'])
        self.assertIn('checklist.items 最多 3 项', result['errors'][0])
        # 缺 checklist.items → 不通过
        plan['checklist'] = {'responsible': '产品'}
        self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
        # filename 缺 run_id → 不通过
        plan['checklist'] = {
            'work_item': '1 测试需求', 'verdict': 'NEED-REVIEW',
            'tag': 'PM-AI-QC-NEED-REVIEW', 'responsible': '产品',
            'generated_at_utc': '2026-07-27T00:00:00Z', 'next': '补充后重新触发',
            'items': [{'id': 'q1', 'question': '校验口径?',
                       'options': ['拦截', '纠正'], 'allow_other': True}],
        }
        plan['artifacts'] = [{'kind': 'qc-followup', 'filename': 'no_runid.json'}]
        self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_merged_skill_accepts_qc_and_analysis_verdicts(self):
        """合并后 skill='auto-req-analysis' 同时接受质控 NEED-* 与分析终局（按 verdict 分派）。"""
        run_id = 'run_12345678'
        plan_path = os.path.join(tempfile.gettempdir(), 'plan.json')
        qc_plan = {
            'version': 1, 'run_id': run_id, 'skill': 'auto-req-analysis', 'work_item_id': 1,
            'expected_rev': 5, 'expected_state': '活动', 'verdict': 'NEED-REVIEW',
            'tags': ['PM-AI-QC-NEED-REVIEW'], 'state_to': None,
            'rules_source': {'qc': 'pre-qc-v1'},
            'checklist': {
                'work_item': '1 测试需求', 'verdict': 'NEED-REVIEW',
                'tag': 'PM-AI-QC-NEED-REVIEW', 'responsible': '产品',
                'generated_at_utc': '2026-07-27T00:00:00Z', 'next': '补充后重新触发',
                'items': [{'id': 'q1', 'question': '范围?',
                           'options': ['全部', '指定机构'], 'allow_other': True}],
            },
            'artifacts': [{'kind': 'qc-followup', 'filename': f'待补充信息_1_{run_id}.json'}],
        }
        # skill='auto-req-analysis' + 质控 verdict 通过（合并关键：原 analysis skill 名现可持 QC 终局）
        self.assertTrue(pipeline.validate_plan(qc_plan, plan_path)['ok'])
        # skill='auto-req-analysis' + 分析 verdict 通过
        with tempfile.TemporaryDirectory() as directory:
            analysis_plan, analysis_path, _ = self.write_analysis_plan(
                directory, ['existing-ui-simple'], run_id)
            self.assertTrue(pipeline.validate_plan(analysis_plan, analysis_path)['ok'])
        # 未知 skill 名仍被拒
        qc_plan['skill'] = 'auto-req-legacy'
        self.assertFalse(pipeline.validate_plan(qc_plan, plan_path)['ok'])

    def test_wiki_audit_field_is_optional_and_accepted(self):
        """产品 wiki 审计字段是可选的：历史计划不带 wiki 仍通过；带 wiki 也能通过（向后兼容）。"""
        run_id = 'run_12345678'
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'], run_id)
            # 历史计划不带 wiki → 通过
            self.assertNotIn('wiki', plan)
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])
            # 带可选 wiki 审计字段 → 同样通过（字段不在 validate required 集内）
            plan['wiki'] = {
                'ready': True,
                'modules_matched': ['药房药库'],
                'findings': [{'entity': '门诊发退药归药房管理，药库出库归药库库存管理', 'state': 'wiki-确认',
                              'source': 'wiki/门诊/门诊药品流转.md'}],
                'note': '命中门诊药品流转；已复核 raw/平台/药房药库.md',
            }
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_tfs_requirements_audit_field_is_optional_and_validated(self):
        """历史计划可省略需求历史；新计划携带时必须满足正式证据结构。"""
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            self.assertNotIn('tfs_requirements', plan)
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

            plan['tfs_requirements'] = self.requirements_evidence()
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

            plan['tfs_requirements']['coverage'] = {}
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('tfs_requirements.ready=true 时 coverage 不得为空', result['errors'])

            plan['tfs_requirements'] = self.requirements_evidence()
            plan['tfs_requirements']['tools_used'].remove('get_requirements_summary')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'tfs_requirements.ready=true 时 tools_used 必须包含 get_requirements_summary',
                result['errors'])

    def test_plan_rejects_unsafe_artifact_and_duplicate_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['artifacts'][0]['path'] = '../变更方案_1_run_12345678.md'
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['tags'].append('PM-AI-AUTO-ANA')
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['artifacts'].append(dict(plan['artifacts'][0]))
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_rules_source_requires_known_structured_values(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['rules_source'] = {'qc': 'pre-qc-v1', 'analysis': 'unknown'}
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_upload_inline_artifact_materializes_marker_then_cleans_up(self):
        run_id = 'run_12345678'
        plan = {
            'run_id': run_id, 'work_item_id': 1,
            'checklist': {'responsible': '产品', 'items': [
                {'id': 'q1', 'question': '校验口径?', 'options': ['拦截']}]},
        }
        artifact = {'kind': 'qc-followup', 'filename': f'待补充信息_1_{run_id}.json'}
        captured = {}

        def fake_upload(client, wid, path, dry_run):
            with open(path, 'r', encoding='utf-8') as f:
                captured['body'] = f.read()
            captured['path'] = path
            return {'ok': True, 'id': wid, 'file': os.path.basename(path), 'dry_run': dry_run}

        with mock.patch.object(tfs, 'upload_attachment', side_effect=fake_upload):
            response = pipeline.upload_inline_artifact({}, plan, artifact, True)
        self.assertTrue(response['ok'])
        # 物化正文含运行标记 + checklist 内容
        self.assertIn(f'<!-- auto-req-run:{run_id} -->', captured['body'])
        self.assertIn('校验口径', captured['body'])
        self.assertEqual(response['file'], f'待补充信息_1_{run_id}.json')
        # 上传后临时文件已清理
        self.assertFalse(os.path.exists(captured['path']))

    def test_auto_analysis_requires_allowlisted_scope(self):
        run_id = 'run_12345678'
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'], run_id)
            plan.pop('auto_scopes')
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
            plan['auto_scopes'] = ['field-ui-copy']
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_auto_analysis_requires_kb_ready_and_dedup_ran(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            # 缺 kb → 不通过
            plan.pop('kb')
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
            # kb.ready=false → 不通过
            plan['kb'] = {'ready': False, 'dedup_ran': True,
                          'findings': [{'entity': 'x', 'state': '已证实', 'source_tool': 'context'}]}
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
            # dedup_ran=false → 不通过
            plan['kb'] = {'ready': True, 'dedup_ran': False,
                          'findings': [{'entity': 'x', 'state': '已证实', 'source_tool': 'context'}]}
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
            # 齐备 → 通过
            plan['kb'] = {'ready': True, 'dedup_ran': True,
                          'findings': [{'entity': 'x', 'state': '已证实', 'source_tool': 'context'}]}
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_manual_review_tolerates_kb_not_ready(self):
        # 回归：MANUAL-REVIEW 路径维持「KB 缺位不升级」原则
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path = self.make_manual_plan(directory, [])
            plan['kb'] = {'ready': False, 'dedup_ran': False, 'findings': []}
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])
            plan.pop('kb')
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_auto_analysis_optimization_category_requires_proven_finding(self):
        for category in ('existing-ui-simple', 'existing-query-simple', 'bug-fix', 'performance'):
            with tempfile.TemporaryDirectory() as directory:
                plan, plan_path, _ = self.write_analysis_plan(directory, [category])
                # findings 空 → 不通过
                plan['kb'] = {'ready': True, 'dedup_ran': True, 'findings': []}
                self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
                # 仅 未确认 → 不通过
                plan['kb']['findings'] = [{'entity': 'x', 'state': '未确认', 'source_tool': 'context'}]
                self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
                # 加一条 已证实 → 通过
                plan['kb']['findings'].append(
                    {'entity': '锚点', 'state': '已证实', 'source_tool': 'context'})
                self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_auto_analysis_non_optimization_category_skips_baseline_check(self):
        # third-party-new 属新功能类（非 OPTIMIZATION_CATEGORIES）：无 已证实 锚点也通过
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['third-party-new'])
            plan['kb'] = {'ready': True, 'dedup_ran': True, 'findings': [
                {'entity': 'x', 'state': '未确认', 'source_tool': 'context'}]}
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_auto_analysis_with_only_evidence_state_fails(self):
        # 仅「证据三态」图谱事实不满足：AUTO-ANA 须「结论三态」已证实
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['kb'] = {'ready': True, 'dedup_ran': True, 'findings': [
                {'entity': 'x', 'state': '图谱事实', 'source_tool': 'context'}]}
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_existing_feature_satisfied_blocks_auto_ana(self):
        # 功能已存在（existing_feature.satisfied=true）→ 硬闸禁止 AUTO-ANA
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['existing_feature'] = {
                'satisfied': True, 'requirement_ids': [260647],
                'note': '需求 260647 已实现退药数量列前置'}
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any(
                '禁止 AUTO-ANA' in e and '功能已存在' in e and '改判 MANUAL-REVIEW' in e
                for e in result['errors']))

    def test_existing_feature_satisfied_allowed_on_manual_review(self):
        # MANUAL-REVIEW 可声明 existing_feature（不触发硬闸）
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path = self.make_manual_plan(directory, [])
            plan['existing_feature'] = {'satisfied': True, 'requirement_ids': [260647]}
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_existing_feature_absent_backward_compatible(self):
        # 字段缺省 → 既有计划行为不变
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            self.assertNotIn('existing_feature', plan)
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_existing_feature_satisfied_false_allowed_on_auto(self):
        # satisfied=false（相似但本次是新增量改动）→ 显式未满足，不阻断 AUTO
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['existing_feature'] = {'satisfied': False, 'note': '相似但本次是新增量改动'}
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_existing_feature_must_be_object(self):
        for bad in ('yes', [{'satisfied': True}]):
            with tempfile.TemporaryDirectory() as directory:
                plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
                plan['existing_feature'] = bad
                result = pipeline.validate_plan(plan, plan_path)
                self.assertFalse(result['ok'])
                self.assertTrue(any('existing_feature 必须为对象' in e for e in result['errors']))

    def test_existing_feature_requirement_ids_must_be_unique_positive_ints(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path = self.make_manual_plan(directory, [])
            plan['existing_feature'] = {
                'satisfied': True, 'requirement_ids': [0, 260647, 260647]}
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any(
                'requirement_ids' in e and '正整数' in e for e in result['errors']))

    def test_existing_feature_note_must_be_nonempty(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path = self.make_manual_plan(directory, [])
            plan['existing_feature'] = {'satisfied': True, 'note': '   '}
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('note' in e and '非空字符串' in e for e in result['errors']))

    def test_skip_analysis_rejects_existing_feature(self):
        plan = {
            'version': 1, 'run_id': 'run_skip_ef', 'skill': 'auto-req-analysis',
            'work_item_id': 1, 'expected_rev': 5, 'expected_state': '已建议',
            'verdict': 'SKIP-ANALYSIS', 'rules_source': {'qc': 'pre-qc-v1'},
            'tags': [], 'state_to': None, 'artifacts': [],
            'skip_reason': '仅联调，无新增业务分析面。',
            'existing_feature': {'satisfied': True, 'requirement_ids': [260647]},
        }
        result = pipeline.validate_plan(plan, '/tmp/skip-ef.json')
        self.assertFalse(result['ok'])
        self.assertIn('SKIP-ANALYSIS 计划不得声明 existing_feature', result['errors'])

    def test_assignee_to_only_allowed_on_auto_ana(self):
        with tempfile.TemporaryDirectory() as directory:
            # AUTO-ANA 携带 assignee_to → 通过
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['assignee_to'] = 'WINNING\\shuyu'
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])
            # MANUAL-REVIEW 携带 assignee_to → 不通过（仅 AUTO-ANA 自动指派）
            manual, manual_path = self.make_manual_plan(directory, [])
            manual['assignee_to'] = 'WINNING\\shuyu'
            self.assertFalse(pipeline.validate_plan(manual, manual_path)['ok'])
            manual.pop('assignee_to')
            self.assertTrue(pipeline.validate_plan(manual, manual_path)['ok'])

    def test_assignee_to_must_be_nonempty_and_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            # 省略（匹配不出）→ 通过
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            self.assertNotIn('assignee_to', plan)
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])
            # 空串/纯空白 → 不通过
            plan['assignee_to'] = ''
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
            plan['assignee_to'] = '   '
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
            plan['assignee_to'] = 'WINNING\\shuyu'
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_optimization_categories_require_regression_impact_dimension(self):
        dim_line = '- **现有行为/数据影响**：已明确现有行为/数据影响\n'
        for category in ('existing-ui-simple', 'existing-query-adjustment', 'print-adjustment',
                         'data-management', 'permission-config', 'performance', 'mobile-adaptation'):
            with tempfile.TemporaryDirectory() as directory:
                plan, plan_path, artifact_path = self.write_analysis_plan(directory, [category])
                with open(artifact_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                content = content.replace(dim_line, '')
                with open(artifact_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_bug_fix_dimension_unchanged(self):
        # bug-fix 不新增维度（已有 现有逻辑影响），防重复计数
        self.assertEqual(
            pipeline.ANALYSIS_DESCRIPTION_REQUIREMENTS['bug-fix'],
            ('问题现象', '复现步骤或已知条件', '涉及功能', '修复方案', '现有逻辑影响'))

    def test_analysis_plan_requires_empty_analysis_gaps_for_auto(self):
        gap = {
            'id': 'gap-amount-scope', 'topic': '金额范围', 'missing': '两类金额的取值范围',
            'impact': '无法确定校验边界', 'question': '两类金额是否均限定当前结算范围？',
            'options': ['均限定当前结算范围'], 'allow_other': True,
        }
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['analysis_gaps'] = [gap]
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
            plan.pop('analysis_gaps')
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])
            plan['analysis_gaps'] = ['not-an-object']
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_manual_analysis_without_gaps_uses_change_plan_only(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path = self.make_manual_plan(directory, [])
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])
            plan['artifacts'].append({
                'kind': 'manual-followup', 'path': f'待确认清单_1_{plan["run_id"]}.md',
            })
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_apply_manual_stop_without_gaps_keeps_automatic_tag_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path = self.make_manual_plan(directory, [])
            plan.update({
                'verdict': 'MANUAL-REVIEW-STOP',
                'tags': ['PM-AI-MANUAL-REVIEW', 'PM-AI-STOP-AUTO'],
            })
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'DefaultCollection'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={'ok': True, 'work_item': {'tags': []}}), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'upload_attachment', return_value={'ok': True, 'id': 1}) as upload, \
                    mock.patch.object(tfs, 'add_tag', return_value={'ok': True, 'id': 1}) as add_tag, \
                    mock.patch.object(tfs, 'set_state') as set_state, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(plan, plan_path, False, 'config.json')
        self.assertTrue(response['ok'])
        upload.assert_called_once()
        self.assertEqual(add_tag.call_count, 2)
        set_state.assert_not_called()

    def test_manual_analysis_gaps_require_safe_followup(self):
        gap = {
            'id': 'gap-amount-scope', 'topic': '金额范围', 'missing': '两类金额的取值范围',
            'impact': '无法确定校验边界', 'question': '两类金额是否均限定当前结算范围？',
            'options': ['均限定当前结算范围', '按医保上传范围'], 'allow_other': True,
        }
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path = self.make_manual_plan(directory, [gap])
            followup_name = f'待确认清单_1_{plan["run_id"]}.md'
            plan['artifacts'].append({'kind': 'manual-followup', 'path': followup_name})
            followup_path = os.path.join(directory, followup_name)
            with open(followup_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join([
                    '# 需求分析待确认项',
                    f'<!-- auto-req-run:{plan["run_id"]} -->',
                    '## 需要确认的需求分析信息',
                    '### gap-amount-scope · 金额范围',
                    '<!-- analysis-gap:gap-amount-scope -->',
                    '- **缺失信息**：两类金额的取值范围',
                    '- **对分析/验收的影响**：无法确定校验边界',
                    '- **需确认的问题**：两类金额是否均限定当前结算范围？',
                    '- **候选口径**：均限定当前结算范围',
                    '- **允许自由补充**：是',
                ]))
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

            with open(followup_path, 'r', encoding='utf-8') as f:
                content = f.read().replace('- **候选口径**：均限定当前结算范围', '- **候选口径**：')
            with open(followup_path, 'w', encoding='utf-8') as f:
                f.write(content)
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

            with open(followup_path, 'w', encoding='utf-8') as f:
                f.write(content.replace('- **候选口径**：', '- **候选口径**：均限定当前结算范围'))

            with open(followup_path, 'a', encoding='utf-8') as f:
                f.write('\nPM-AI-MANUAL-REVIEW\n')
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

            with open(followup_path, 'r', encoding='utf-8') as f:
                content = f.read().replace('<!-- analysis-gap:gap-amount-scope -->', '')
            with open(followup_path, 'w', encoding='utf-8') as f:
                f.write(content)
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_analysis_description_accepts_simple_ui_query_and_multiple_third_party_categories(self):
        with tempfile.TemporaryDirectory() as directory:
            simple, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            self.assertTrue(pipeline.validate_plan(simple, plan_path)['ok'])
        with tempfile.TemporaryDirectory() as directory:
            simple_query, plan_path, _ = self.write_analysis_plan(directory, ['existing-query-simple'])
            self.assertTrue(pipeline.validate_plan(simple_query, plan_path)['ok'])
        with tempfile.TemporaryDirectory() as directory:
            query, plan_path, _ = self.write_analysis_plan(directory, ['existing-query-adjustment'])
            self.assertTrue(pipeline.validate_plan(query, plan_path)['ok'])
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(
                directory, ['third-party-adjustment', 'third-party-new'])
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_existing_query_adjustment_rejects_missing_result_field(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_analysis_plan(
                directory, ['existing-query-adjustment'])
            with open(artifact_path, 'r', encoding='utf-8') as f:
                content = f.read().replace('- **结果字段与展示**：已明确结果字段与展示\n', '')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(content)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('分析类别 existing-query-adjustment 缺少非空维度“结果字段与展示”', result['errors'])

    def test_existing_query_simple_rejects_missing_out_of_scope_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_analysis_plan(
                directory, ['existing-query-simple'])
            with open(artifact_path, 'r', encoding='utf-8') as f:
                content = f.read().replace('- **不涉及范围**：已明确不涉及范围\n', '')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(content)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('分析类别 existing-query-simple 缺少非空维度“不涉及范围”', result['errors'])

    def test_analysis_description_rejects_missing_invalid_mismatched_or_incomplete_content(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan.pop('analysis_description')
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

            plan['analysis_description'] = {'categories': ['unknown-category']}
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

            plan['analysis_description'] = {'categories': ['existing-ui-simple']}
            with open(artifact_path, 'r', encoding='utf-8') as f:
                content = f.read().replace('`existing-ui-simple`', '`report`', 1)
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(content)
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

            plan, plan_path, artifact_path = self.write_analysis_plan(directory, ['existing-ui-simple'])
            with open(artifact_path, 'r', encoding='utf-8') as f:
                content = f.read().replace('- **参数控制**：已明确参数控制\n', '')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(content)
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

            with open(artifact_path, 'a', encoding='utf-8') as f:
                f.write('<待填写>\n请开发处理\n')
            self.assertFalse(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_analysis_description_requires_menu_and_operation_path(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_analysis_plan(
                directory, ['existing-ui-simple'])
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

            with open(artifact_path, 'r', encoding='utf-8') as f:
                original = f.read()
            without_path = original.replace(
                '- **路径**：菜单路径：业务管理 > 测试功能；操作路径：测试功能 → 编辑 → 保存\n', '')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(without_path)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('分析者描述必须且只能包含一个非空“路径”', result['errors'])

            missing_operation = original.replace(
                '菜单路径：业务管理 > 测试功能；操作路径：测试功能 → 编辑 → 保存',
                '菜单路径：业务管理 > 测试功能')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(missing_operation)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('分析者描述“路径”必须按“菜单路径：...；操作路径：...”填写', result['errors'])

            duplicate_path = original.replace(
                '### existing-ui-simple',
                '- **路径**：菜单路径：重复菜单；操作路径：重复操作\n### existing-ui-simple')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(duplicate_path)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn('分析者描述必须且只能包含一个非空“路径”', result['errors'])

    def test_render_analysis_description_html_uses_only_safe_basic_html(self):
        content = '\n'.join([
            '# 变更方案',
            '<!-- auto-req-run:run_12345678 -->',
            '## 三、分析者描述',
            '- **需求类别**：`existing-ui-simple`',
            '- **路径**：菜单路径：费用管理 > 费用录入；操作路径：费用录入 → 保存',
            '### existing-ui-simple（既有功能简单界面优化）',
            '- **需求背景**：A <script>alert(1)</script>',
            '- **涉及功能**：费用管理',
            '- **修改方案**：调整提示',
            '- **参数控制**：无',
            '- **显示与交互位置**：保存前',
            '- **影响范围**：当前页面',
            '## 四、业务规则与变更范围',
        ])
        rendered = pipeline.render_analysis_description_html(content)
        self.assertIn('<div>既有功能简单界面优化</div>', rendered)
        self.assertIn('<div>路径：菜单路径：费用管理 &gt; 费用录入；操作路径：费用录入 → 保存</div>', rendered)
        self.assertIn('<strong>修改方案：</strong>调整提示', rendered)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', rendered)
        self.assertNotIn('###', rendered)
        self.assertNotIn('**', rendered)
        self.assertNotIn('`', rendered)
        self.assertNotIn('## 四、', rendered)

    def test_render_concise_v3_outputs_only_business_dimensions(self):
        content = '\n'.join([
            '# 变更方案',
            '## 三、分析者描述',
            '### existing-ui-simple（既有功能简单界面优化）',
            '- **界面优化方案**：费用录入页保存后显示“A <script>alert(1)</script>”，不改变数据写入。',
            '## 四、范围—方案—验收追踪',
        ])
        rendered = pipeline.render_analysis_description_html(content, 'concise-v3')
        self.assertIn('<strong>界面优化方案：</strong>', rendered)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', rendered)
        self.assertNotIn('existing-ui-simple', rendered)
        self.assertNotIn('既有功能简单界面优化', rendered)
        self.assertNotIn('需求类别', rendered)
        self.assertNotIn('路径：', rendered)
        self.assertNotIn('决策结论', rendered)

        invalid = content.replace(
            '### existing-ui-simple（既有功能简单界面优化）',
            '- **路径**：菜单路径：费用管理；操作路径：费用录入 → 保存\n'
            '### existing-ui-simple（既有功能简单界面优化）')
        with self.assertRaisesRegex(ValueError, 'concise-v3 分析者描述不得包含“路径”'):
            pipeline.render_analysis_description_html(invalid, 'concise-v3')

    def test_render_analysis_description_html_allows_flat_numbered_change_points(self):
        content = '\n'.join([
            '# 变更方案',
            '## 三、分析者描述',
            '- **需求类别**：`existing-complex`',
            '- **路径**：菜单路径：诊断信息；操作路径：编辑诊断 → 保存',
            '### existing-complex（既有功能复杂调整）',
            '- **涉及条线与模块**：住院诊断与医保登记提醒。',
            '- **改造内容**：见以下两项。',
            '1. **主诊断标识**：在已确认的主诊断旁展示图标。',
            '2. **保存提醒**：满足已确认触发条件后展示提醒。',
            '- **改造流程**：保存后按已确认规则处理。',
            '- **改造范围**：住院诊断。',
            '- **风险与项目注意事项**：人工复核。',
            '## 四、业务规则与变更范围',
        ])
        rendered = pipeline.render_analysis_description_html(content)
        self.assertIn('<div>1. 主诊断标识：在已确认的主诊断旁展示图标。</div>', rendered)
        self.assertIn('<div>2. 保存提醒：满足已确认触发条件后展示提醒。</div>', rendered)

    def test_apply_analysis_writes_detail_section_not_requirement_analysis(self):
        # 用 MANUAL-REVIEW 计划：同样写描述区段（ANALYSIS_VERDICTS 都写），但不触发 AUTO-ANA 字段流转，
        # 聚焦验证「描述写 System.Description 而非 Winning.Demand.Analysis」。
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path = self.make_manual_plan(directory, [])
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'DefaultCollection'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={'ok': True, 'work_item': {'tags': []}}), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section',
                                      return_value={'ok': True, 'id': 1}) as replace_detail, \
                    mock.patch.object(tfs, 'write_field') as write_field, \
                    mock.patch.object(tfs, 'upload_attachment', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'add_tag', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(plan, plan_path, False, 'config.json')
        self.assertTrue(response['ok'])
        self.assertIn('<div>', replace_detail.call_args.args[2])
        write_field.assert_not_called()

    def test_apply_auto_ana_runs_field_flow_and_skips_assignee_when_unresolvable(self):
        raw_with_leader = {'id': 1, 'rev': 5, 'fields': {
            'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
            'Demand.Expected.date': '2026-09-06T16:00:00Z',
            'Winning.Dev.Leader': 'zhang_dong(张栋) <WINNING\\zhang_dong>'}}
        iter_resp = {'ok': True, 'earliest': {'path': 'NETHIS5.5\\2026\\V6.0.2608.28',
                                              'finish': '2026-08-28T00:00:00Z'}}
        with tempfile.TemporaryDirectory() as directory:
            # AUTO-ANA 跑完整字段流转；assignee_to 为回退，Dev.Leader 优先
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['assignee_to'] = 'WINNING\\shuyu'
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={'ok': True, 'work_item': {'tags': []}}), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'upload_attachment', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'add_tag', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'fetch_raw', return_value=raw_with_leader), \
                    mock.patch.object(tfs, 'list_iterations', return_value=iter_resp), \
                    mock.patch.object(tfs, 'write_field', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'set_state', return_value={'ok': True, 'id': 1}) as set_state, \
                    mock.patch.object(tfs, 'set_assignee', return_value={'ok': True, 'id': 1}) as set_assignee, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(plan, plan_path, False, 'config.json')
            self.assertTrue(response['ok'])
            names = [a['action'] for a in response['actions']]
            # 完整顺序：写描述→上传→加标签→迭代→开始日期→完成日期→活动→已分析→指派
            self.assertEqual(names, [
                'write-detail-analysis', 'upload:change-plan', 'add-tag:PM-AI-AUTO-ANA',
                'write-field:IterationPath', 'write-field:StartDate', 'write-field:FinishDate',
                'set-state:活动', 'set-state:已分析', 'set-assignee'])
            set_state.assert_any_call(mock.ANY, 1, '活动', True)
            set_state.assert_any_call(mock.ANY, 1, '已分析', True)
            # Dev.Leader 优先于 plan.assignee_to 回退
            set_assignee.assert_called_once_with(mock.ANY, 1, 'WINNING\\zhang_dong', True)

            # 无 Dev.Leader 且无 assignee_to → 不指派（仍流转）
            raw_no_leader = {'id': 1, 'rev': 5, 'fields': {
                'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
                'Demand.Expected.date': '2026-09-06T16:00:00Z'}}
            plan.pop('assignee_to')
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={'ok': True, 'work_item': {'tags': []}}), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'upload_attachment', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'add_tag', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'fetch_raw', return_value=raw_no_leader), \
                    mock.patch.object(tfs, 'list_iterations', return_value=iter_resp), \
                    mock.patch.object(tfs, 'write_field', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'set_state', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'set_assignee') as set_assignee_absent, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(plan, plan_path, False, 'config.json')
            self.assertTrue(response['ok'])
            names = [a['action'] for a in response['actions']]
            self.assertIn('set-state:已分析', names)
            self.assertNotIn('set-assignee', names)
            set_assignee_absent.assert_not_called()

    def test_apply_preflight_failure_records_error_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            plan['tfs_requirements'] = self.requirements_evidence()
            with mock.patch.object(tfs, 'load_config', return_value={}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={'ok': False, 'error': '版本已变化'}), \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'error-audit.json'}) as record:
                response = pipeline.apply_plan(plan, plan_path, False, 'config.json')
        self.assertFalse(response['ok'])
        self.assertEqual(response['audit'], 'error-audit.json')
        self.assertEqual(record.call_args.args[2], 'ERROR')
        self.assertEqual(record.call_args.args[7]['tfs_requirements'], plan['tfs_requirements'])

    def test_repair_analysis_placement_uses_strict_two_step_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            raw = {'id': 1, 'rev': 6, 'fields': {
                'System.WorkItemType': '需求', 'System.State': '活动', 'System.Tags': '',
            }}
            with mock.patch.object(tfs, 'load_config', return_value={}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section',
                                      return_value={'ok': True, 'id': 1, 'dry_run': True}) as replace_detail, \
                    mock.patch.object(tfs, 'remove_legacy_analysis_append',
                                      return_value={'ok': True, 'id': 1, 'dry_run': True}) as remove_legacy, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.repair_analysis_placement(plan, plan_path, False, 'config.json')
        self.assertTrue(response['ok'])
        self.assertIn('<div>', replace_detail.call_args.args[2])
        self.assertTrue(remove_legacy.call_args.args[2].startswith('# 变更方案'))

    def test_pipeline_audit_includes_attachment_evidence(self):
        plan = {
            'version': 1, 'run_id': 'run_12345678', 'skill': 'auto-req-qc', 'work_item_id': 1,
            'expected_rev': 5, 'expected_state': '活动', 'verdict': 'PASS',
            'tags': [], 'state_to': None, 'rules_source': 'pre-qc-v1', 'artifacts': [],
            'attachments': {'ready': True, 'downloaded': [], 'parsed': [], 'skipped': [], 'errors': []},
            'wiki': {'ready': True, 'modules_matched': ['药房药库'],
                     'findings': [], 'note': '命中药房药库模块'},
            'tfs_requirements': self.requirements_evidence(),
        }
        with mock.patch.object(tfs, 'load_config', return_value={'collection': 'DefaultCollection'}), \
             mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
             mock.patch.object(pipeline, 'preflight', return_value={'ok': True, 'work_item': {'tags': []}}), \
             mock.patch.object(redis_client, 'publish_plan',
                               return_value={'ok': True, 'key': 'auto-req:qc:plan:DefaultCollection:1', 'fields': 6}) as publish, \
             mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}) as record:
            result = pipeline.apply_plan(plan, os.path.join(tempfile.gettempdir(), 'plan.json'), False, 'config.json')
        self.assertTrue(result['ok'])
        self.assertEqual(record.call_args.args[7]['attachments'], plan['attachments'])
        # wiki 审计字段透传到 record 的 extra 字典
        self.assertEqual(record.call_args.args[7]['wiki'], plan['wiki'])
        self.assertEqual(record.call_args.args[7]['tfs_requirements'], plan['tfs_requirements'])
        # redis 结果降级不阻断，并入审计 extra 与返回
        self.assertEqual(record.call_args.args[7]['redis']['key'], 'auto-req:qc:plan:DefaultCollection:1')
        self.assertEqual(result['redis']['key'], 'auto-req:qc:plan:DefaultCollection:1')
        publish.assert_called_once_with(plan, 'dry-run', 'DefaultCollection', 'config.json',
                                        analysis_description_html='', work_item='')

    def test_pipeline_validates_enriched_attachment_runtime_audit(self):
        plan = {
            'version': 1, 'run_id': 'run_12345678', 'skill': 'auto-req-qc', 'work_item_id': 1,
            'expected_rev': 5, 'expected_state': '活动', 'verdict': 'PASS',
            'tags': [], 'state_to': None, 'rules_source': 'pre-qc-v1', 'artifacts': [],
            'attachments': {
                'ready': True,
                'downloaded': [{'name': '规范.pdf'}],
                'parsed': [{
                    'name': '规范.pdf', 'status': 'parsed', 'output': '附件解析/规范.pdf.md',
                    'converter': 'builtin-fallback', 'converter_chain': 'builtin-fallback',
                    'runtime_mode': 'managed-host', 'tool_versions': {'pymupdf': '1.27.2.3'},
                }],
                'skipped': [], 'errors': [],
                'preflight': {
                    'requested_formats': ['.pdf'],
                    'capabilities': {'.pdf': {'ready': True, 'chains': ['pymupdf']}},
                    'install_required': ['python-markitdown'],
                    'installations': [{
                        'group': 'python-markitdown', 'manager': 'pip',
                        'packages': ['markitdown[xls,xlsx,pdf,docx,pptx]==0.1.7'],
                        'started_at': '2026-08-03T00:00:00Z',
                        'finished_at': '2026-08-03T00:00:01Z',
                        'status': 'installed', 'exit_code': 0, 'error': '',
                    }],
                    'runtime_dir': '过程文件/.runtime/attachments/cache',
                    'runtime_cache_key': 'cache', 'runtime_mode': 'managed-host',
                    'blocked_formats': [], 'warnings': [],
                },
            },
        }
        result = pipeline.validate_plan(plan, os.path.join(tempfile.gettempdir(), 'plan.json'), False)
        self.assertTrue(result['ok'], result['errors'])

        plan['attachments']['parsed'][0]['converter_chain'] = 'shell-command'
        invalid = pipeline.validate_plan(plan, os.path.join(tempfile.gettempdir(), 'plan.json'), False)
        self.assertFalse(invalid['ok'])
        self.assertTrue(any('转换链' in error or 'converter_chain' in error
                            for error in invalid['errors']))

    def test_pipeline_rejects_attachment_with_multiple_terminal_outcomes(self):
        plan = {
            'version': 1, 'run_id': 'run_12345678', 'skill': 'auto-req-qc', 'work_item_id': 1,
            'expected_rev': 5, 'expected_state': '活动', 'verdict': 'PASS',
            'tags': [], 'state_to': None, 'rules_source': 'pre-qc-v1', 'artifacts': [],
            'attachments': {
                'ready': False, 'downloaded': [{'name': 'a.pdf'}],
                'parsed': [{'name': 'a.pdf', 'status': 'parsed', 'converter': 'builtin-fallback'}],
                'skipped': [{'name': 'a.pdf', 'reason': 'duplicate'}], 'errors': [],
            },
        }
        result = pipeline.validate_plan(plan, os.path.join(tempfile.gettempdir(), 'plan.json'), False)
        self.assertFalse(result['ok'])
        self.assertTrue(any('一个终态' in error for error in result['errors']))

    def test_apply_plan_threads_collection_override_to_load_config(self):
        plan = {
            'version': 1, 'run_id': 'run_12345678', 'skill': 'auto-req-qc', 'work_item_id': 1,
            'expected_rev': 5, 'expected_state': '活动', 'verdict': 'PASS',
            'tags': [], 'state_to': None, 'rules_source': 'pre-qc-v1', 'artifacts': [],
        }
        with mock.patch.object(tfs, 'load_config', return_value={'collection': 'CollX'}) as load_config, \
                mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                mock.patch.object(pipeline, 'preflight',
                                  return_value={'ok': True, 'work_item': {'tags': []}}), \
                mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
            pipeline.apply_plan(plan, os.path.join(tempfile.gettempdir(), 'plan.json'), False,
                                'config.json', 'pat-x', 'CollX', 'ProjX')
        # pat/collection/project override 透传给 load_config
        self.assertEqual(load_config.call_args.args, ('config.json', 'pat-x', 'CollX', 'ProjX'))

    # -------- 字段流转：resolve_assignee_to_winning --------

    def test_resolve_assignee_to_winning_extracts_account(self):
        r = pipeline.resolve_assignee_to_winning
        # Dev.Leader 全格式（首选）
        self.assertEqual(r('zhang_dong(张栋) <WINNING\\zhang_dong>'), 'WINNING\\zhang_dong')
        # 回退：display(account)
        self.assertEqual(r('', '张栋(zhang_dong)'), 'WINNING\\zhang_dong')
        # 回退：已是 WINNING\x
        self.assertEqual(r('', 'WINNING\\shuyu'), 'WINNING\\shuyu')
        # 回退：<WINNING\account>
        self.assertEqual(r('', '<WINNING\\zhang_dong>'), 'WINNING\\zhang_dong')
        # 裸 display name（无账号信息）→ None，绝不猜
        self.assertIsNone(r('', '舒予'))
        # 中文括号内容（非账号）→ None
        self.assertIsNone(r('', '某(张栋)'))
        # Dev.Leader 优先于 fallback
        self.assertEqual(r('zhang_dong(张栋) <WINNING\\zhang_dong>', 'WINNING\\other'), 'WINNING\\zhang_dong')

    # -------- 字段流转：apply_field_flow --------

    def test_apply_field_flow_full_sequence_dry_run(self):
        raw = {'id': 1, 'rev': 5, 'fields': {
            'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
            'Demand.Expected.date': '2026-09-06T16:00:00Z',
            'Winning.Dev.Leader': 'zhang_dong(张栋) <WINNING\\zhang_dong>'}}
        iter_resp = {'ok': True, 'earliest': {'path': 'NETHIS5.5\\2026\\V6.0.2608.28', 'finish': '2026-08-28T00:00:00Z'}}
        local_date = lambda value=None: datetime.date(2026, 8, 28) if value else datetime.date(2026, 8, 1)
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'list_iterations', return_value=iter_resp), \
                mock.patch.object(tfs, 'beijing_date', side_effect=local_date), \
                mock.patch.object(tfs, 'write_field', return_value={'ok': True}) as write_field, \
                mock.patch.object(tfs, 'set_state', return_value={'ok': True}) as set_state, \
                mock.patch.object(tfs, 'set_assignee', return_value={'ok': True}) as set_assignee:
            actions = pipeline.apply_field_flow({'collection': 'C'}, 1, True)
        self.assertEqual([a['action'] for a in actions], [
            'write-field:IterationPath', 'write-field:StartDate', 'write-field:FinishDate',
            'set-state:活动', 'set-state:已分析', 'set-assignee'])
        set_state.assert_any_call(mock.ANY, 1, '活动', True)
        set_state.assert_any_call(mock.ANY, 1, '已分析', True)
        set_assignee.assert_called_once_with(mock.ANY, 1, 'WINNING\\zhang_dong', True)
        start_call = next(c for c in write_field.call_args_list if c.args[2] == tfs.F_START_DATE)
        self.assertEqual(start_call.args[3], '2026-08-01')
        for c in write_field.call_args_list:  # dry_run=True 透传给每个 write_field
            self.assertTrue(c.args[-1])

    def test_apply_field_flow_picks_earliest_iteration_when_multiple_match(self):
        """排期取向=最早：多个 finish≤期望 迭代时，IterationPath/FinishDate 写 earliest（08-14）而非 matched（08-28）。"""
        raw = {'id': 1, 'rev': 5, 'fields': {
            'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
            'Demand.Expected.date': '2026-09-06T16:00:00Z'}}
        # matched=最晚(08-28,时效基准) / earliest=最早(08-14,排期取向)；排期应取 earliest
        iter_resp = {
            'ok': True,
            'matched': {'path': 'NETHIS5.5\\2026\\V6.0.2608.28', 'finish': '2026-08-28T00:00:00Z'},
            'earliest': {'path': 'NETHIS5.5\\2026\\V6.0.2608.14', 'finish': '2026-08-14T00:00:00Z'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'list_iterations', return_value=iter_resp), \
                mock.patch.object(tfs, 'write_field', return_value={'ok': True}) as write_field, \
                mock.patch.object(tfs, 'set_state', return_value={'ok': True}), \
                mock.patch.object(tfs, 'set_assignee'):
            pipeline.apply_field_flow({'collection': 'C'}, 1, True)
        iter_call = next(c for c in write_field.call_args_list if c.args[2] == tfs.F_ITERATION)
        self.assertEqual(iter_call.args[3], 'NETHIS5.5\\2026\\V6.0.2608.14')   # 排到 08-14
        finish_call = next(c for c in write_field.call_args_list if c.args[2] == tfs.F_FINISH_DATE)
        self.assertEqual(finish_call.args[3], '2026-08-14')                     # 完成 08-14

    def test_apply_field_flow_no_expected_date_skips_iteration_and_dates(self):
        raw = {'id': 1, 'rev': 5, 'fields': {
            'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
            'Winning.Dev.Leader': 'zhang_dong(张栋) <WINNING\\zhang_dong>'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'list_iterations') as list_iter, \
                mock.patch.object(tfs, 'write_field') as write_field, \
                mock.patch.object(tfs, 'set_state', return_value={'ok': True}), \
                mock.patch.object(tfs, 'set_assignee', return_value={'ok': True}):
            actions = pipeline.apply_field_flow({'collection': 'C'}, 1, True)
        names = [a['action'] for a in actions]
        self.assertNotIn('write-field:IterationPath', names)
        self.assertIn('set-state:已分析', names)
        list_iter.assert_not_called()
        write_field.assert_not_called()

    def test_apply_field_flow_matched_none_skips_dates_and_assignee(self):
        raw = {'id': 1, 'rev': 5, 'fields': {
            'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
            'Demand.Expected.date': '2026-09-06T16:00:00Z'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'list_iterations', return_value={'ok': True, 'earliest': None}), \
                mock.patch.object(tfs, 'write_field') as write_field, \
                mock.patch.object(tfs, 'set_state', return_value={'ok': True}), \
                mock.patch.object(tfs, 'set_assignee') as set_assignee:
            actions = pipeline.apply_field_flow({'collection': 'C'}, 1, True)
        names = [a['action'] for a in actions]
        self.assertNotIn('write-field:IterationPath', names)
        self.assertNotIn('set-assignee', names)   # 无 Dev.Leader 无 fallback → 不指派
        write_field.assert_not_called()
        set_assignee.assert_not_called()

    def test_apply_field_flow_state_already_analyzed_skips_set_state(self):
        raw = {'id': 1, 'rev': 5, 'fields': {
            'System.State': '已分析', 'System.TeamProject': 'NETHIS5.5',
            'Demand.Expected.date': '2026-09-06T16:00:00Z',
            'Winning.Dev.Leader': 'zhang_dong(张栋) <WINNING\\zhang_dong>'}}
        iter_resp = {'ok': True, 'earliest': {'path': 'P', 'finish': '2026-08-28T00:00:00Z'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'list_iterations', return_value=iter_resp), \
                mock.patch.object(tfs, 'write_field', return_value={'ok': True}), \
                mock.patch.object(tfs, 'set_state', return_value={'ok': True}) as set_state, \
                mock.patch.object(tfs, 'set_assignee', return_value={'ok': True}):
            actions = pipeline.apply_field_flow({'collection': 'C'}, 1, True)
        names = [a['action'] for a in actions]
        self.assertNotIn('set-state:活动', names)
        self.assertNotIn('set-state:已分析', names)
        self.assertIn('write-field:IterationPath', names)   # 仍补字段
        set_state.assert_not_called()

    def test_apply_field_flow_state_active_only_sets_analyzed(self):
        raw = {'id': 1, 'rev': 5, 'fields': {'System.State': '活动', 'System.TeamProject': 'NETHIS5.5'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'list_iterations'), \
                mock.patch.object(tfs, 'write_field'), \
                mock.patch.object(tfs, 'set_state', return_value={'ok': True}) as set_state, \
                mock.patch.object(tfs, 'set_assignee'):
            pipeline.apply_field_flow({'collection': 'C'}, 1, True)
        self.assertEqual([c.args[-2] for c in set_state.call_args_list], ['已分析'])

    def test_apply_field_flow_assignee_failure_degrades_not_raises(self):
        raw = {'id': 1, 'rev': 5, 'fields': {
            'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
            'Winning.Dev.Leader': 'zhang_dong(张栋) <WINNING\\zhang_dong>'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'list_iterations'), \
                mock.patch.object(tfs, 'write_field'), \
                mock.patch.object(tfs, 'set_state', return_value={'ok': True}), \
                mock.patch.object(tfs, 'set_assignee', return_value={'ok': False, 'error': 'HTTP 400 未知标识'}):
            actions = pipeline.apply_field_flow({'collection': 'C'}, 1, False)   # execute
        # 指派失败被降级为 error 动作，整体不抛
        assignee_action = next(a for a in actions if a['action'] == 'set-assignee')
        self.assertFalse(assignee_action['result']['ok'])

    def test_apply_field_flow_write_field_failure_raises(self):
        raw = {'id': 1, 'rev': 5, 'fields': {
            'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
            'Demand.Expected.date': '2026-09-06T16:00:00Z'}}
        iter_resp = {'ok': True, 'earliest': {'path': 'P', 'finish': '2026-08-28T00:00:00Z'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'list_iterations', return_value=iter_resp), \
                mock.patch.object(tfs, 'write_field', return_value={'ok': False, 'error': 'HTTP 400'}), \
                mock.patch.object(tfs, 'set_state'), \
                mock.patch.object(tfs, 'set_assignee'):
            with self.assertRaises(RuntimeError):
                pipeline.apply_field_flow({'collection': 'C'}, 1, False)

    # -------- 字段流转：flow_item + CLI --------

    def test_flow_item_dry_run_records_field_flow_audit(self):
        raw = {'id': 2, 'rev': 7, 'fields': {
            'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
            'System.WorkItemType': '需求', 'System.Tags': '',
            'Demand.Expected.date': '2026-09-06T16:00:00Z',
            'Winning.Dev.Leader': 'zhang_dong(张栋) <WINNING\\zhang_dong>'}}
        iter_resp = {'ok': True, 'earliest': {'path': 'P', 'finish': '2026-08-28T00:00:00Z'}}
        with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'list_iterations', return_value=iter_resp), \
                mock.patch.object(tfs, 'write_field', return_value={'ok': True}), \
                mock.patch.object(tfs, 'set_state', return_value={'ok': True}), \
                mock.patch.object(tfs, 'set_assignee', return_value={'ok': True}), \
                mock.patch.object(tfs, 'beijing_timestamp', return_value='20260801000506'), \
                mock.patch.object(tfs, 'record', return_value={'audit': 'flow-audit.json'}) as record:
            response = pipeline.flow_item(2, False, 'config.json')
        self.assertTrue(response['ok'])
        self.assertEqual(response['mode'], 'flow-dry-run')
        record.assert_called_once()
        self.assertEqual(record.call_args.args[2], 'FIELD-FLOW')   # verdict
        self.assertEqual(record.call_args.args[4], '已建议')         # state_from
        self.assertEqual(record.call_args.args[5], '已分析')         # state_to
        self.assertTrue(record.call_args.args[8].startswith('flow_20260801000506_'))

    def test_flow_item_expected_rev_gate_blocks_before_flow(self):
        raw = {'id': 2, 'rev': 9, 'fields': {
            'System.WorkItemType': '需求', 'System.Tags': '', 'System.State': '已建议'}}
        with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'record') as record:
            response = pipeline.flow_item(2, False, 'config.json', expected_rev=7)
        self.assertFalse(response['ok'])
        self.assertIn('版本已变化', response['error'])
        record.assert_not_called()

    def test_flow_item_assignee_override_overrides_dev_leader(self):
        raw = {'id': 2, 'rev': 7, 'fields': {
            'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
            'System.WorkItemType': '需求', 'System.Tags': '',
            'Winning.Dev.Leader': 'zhang_dong(张栋) <WINNING\\zhang_dong>'}}
        with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'list_iterations'), \
                mock.patch.object(tfs, 'write_field', return_value={'ok': True}), \
                mock.patch.object(tfs, 'set_state', return_value={'ok': True}), \
                mock.patch.object(tfs, 'set_assignee', return_value={'ok': True}) as set_assignee, \
                mock.patch.object(tfs, 'record', return_value={'audit': 'a.json'}):
            pipeline.flow_item(2, False, 'config.json', assignee_override='WINNING\\other')
        set_assignee.assert_called_once_with(mock.ANY, 2, 'WINNING\\other', True)


class RedisClientTests(unittest.TestCase):
    def test_plan_and_index_keys_are_isolated_by_collection(self):
        self.assertEqual(redis_client.plan_key('CollectionA', 1), 'auto-req:qc:plan:CollectionA:1')
        self.assertEqual(redis_client.plan_key('CollectionB', 1), 'auto-req:qc:plan:CollectionB:1')
        self.assertEqual(redis_client.ids_key('CollectionA'), 'auto-req:qc:ids:CollectionA')

    def test_publish_plan_writes_summary_and_removes_stale_checklist(self):
        commands = []

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def execute(self, *args):
                commands.append(args)
                return 1

        checklist = {'work_item': '1 标题', 'responsible': '产品', 'items': [{'id': 'q1'}],
                     'generated_at_utc': '2026-07-27T00:00:00Z', 'next': '重跑 auto-req-analysis 1'}
        qc_plan = {'work_item_id': 1, 'run_id': 'run_12345678', 'verdict': 'NEED-REVIEW',
                   'tags': ['PM-AI-QC-NEED-REVIEW'], 'state_to': None, 'checklist': checklist}
        analysis_plan = {'work_item_id': 1, 'run_id': 'run_87654321', 'verdict': 'AUTO-ANA',
                         'tags': ['PM-AI-AUTO-ANA'], 'state_to': '已分析'}
        with mock.patch.object(redis_client, '_Connection', return_value=Connection()), \
                mock.patch.object(redis_client, 'load_redis_config', return_value={'ttl_seconds': 0}):
            response = redis_client.publish_plan(qc_plan, 'dry-run', 'CollectionA', 'config.json')
            redis_client.publish_plan(analysis_plan, 'execute', 'CollectionA', 'config.json')
        self.assertTrue(response['ok'])
        hset = commands[0]
        self.assertEqual(hset[:2], ('HSET', 'auto-req:qc:plan:CollectionA:1'))
        fields = dict(zip(hset[2::2], hset[3::2]))
        # NEED-*：摘要 6 字段 + work_item/next 提升顶层 + checklist 仅 {responsible, items}
        self.assertEqual(set(fields), {'run_id', 'verdict', 'tags', 'state_to', 'generated_at_utc',
                                       'run_mode', 'work_item', 'next', 'checklist'})
        self.assertEqual(json.loads(fields['checklist']), {'responsible': '产品', 'items': [{'id': 'q1'}]})
        self.assertEqual(fields['work_item'], '1 标题')
        self.assertEqual(fields['next'], '重跑 auto-req-analysis 1')
        self.assertNotIn('plan_json', fields)
        self.assertNotIn('plan_path', fields)
        self.assertIn(('SADD', 'auto-req:qc:ids:CollectionA', '1'), commands)
        # 分析终局清掉遗留的 checklist/work_item/next/analysis_description/knowledge
        for stale in ('checklist', 'work_item', 'next', 'skip_reason',
                      'analysis_description', 'knowledge'):
            self.assertIn(('HDEL', 'auto-req:qc:plan:CollectionA:1', stale), commands)
        analysis_hset = [command for command in commands if command[0] == 'HSET'][1]
        analysis_fields = dict(zip(analysis_hset[2::2], analysis_hset[3::2]))
        for stale in ('checklist', 'work_item', 'next', 'skip_reason',
                      'analysis_description', 'knowledge'):
            self.assertNotIn(stale, analysis_fields)
        # 该分析 plan 无 kb/description：仅 6 基础字段
        self.assertEqual(set(analysis_fields),
                         {'run_id', 'verdict', 'tags', 'state_to', 'generated_at_utc', 'run_mode'})

    def test_publish_plan_writes_analysis_description_and_knowledge_summary(self):
        commands = []

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def execute(self, *args):
                commands.append(args)
                return 1

        plan = {
            'work_item_id': 1, 'run_id': 'run_auto_1234', 'verdict': 'AUTO-ANA',
            'tags': ['PM-AI-AUTO-ANA'], 'state_to': '已分析',
            'generated_at_utc': '2026-08-04T00:00:00Z',
            'kb': {'ready': True, 'tools_used': ['query', 'context'], 'dedup_ran': True,
                   'findings': [{'entity': '初核=医嘱校对 adviceProofread', 'state': '已证实',
                                 'source_tool': 'query+context', 'note': '冗长备注应丢弃'}],
                   'note': '代码图谱就绪'},
            'wiki': {'ready': True, 'modules_matched': ['住院护士站'],
                     'findings': [{'entity': '初核业务规则', 'state': 'wiki-确认',
                                   'source': 'wiki/topics/住院.md'}]},
            'tfs_requirements': {'ready': True, 'coverage': {'collection': 'x'},
                                 'findings': [{'work_item_id': 260001, 'fact': '既有出院带药提示',
                                               'state': '已证实', 'maturity': '已落地',
                                               'source_tool': 'get_work_item'}]},
        }
        html = '<div><br></div><div>核心改造点：xxx</div><div><br></div>'
        with mock.patch.object(redis_client, '_Connection', return_value=Connection()), \
                mock.patch.object(redis_client, 'load_redis_config', return_value={'ttl_seconds': 0}):
            response = redis_client.publish_plan(plan, 'execute', 'CollectionA', 'config.json',
                                                 analysis_description_html=html,
                                                 work_item='1 医嘱校对-出院带药强提示')
        self.assertTrue(response['ok'])
        fields = dict(zip(commands[0][2::2], commands[0][3::2]))
        # work_item：所有终局统一写入（apply_plan 传 live <id> <标题>）
        self.assertEqual(fields['work_item'], '1 医嘱校对-出院带药强提示')
        # 分析者描述：与写入 TFS 的 HTML 同源同值
        self.assertEqual(fields['analysis_description'], html)
        # knowledge：三类来源精简投影，仅留定位 + 作用字段
        knowledge = json.loads(fields['knowledge'])
        self.assertEqual(set(knowledge), {'code_graph', 'wiki', 'history'})
        self.assertEqual(knowledge['code_graph']['tools'], ['query', 'context'])
        self.assertEqual(knowledge['code_graph']['findings'], [
            {'entity': '初核=医嘱校对 adviceProofread', 'state': '已证实',
             'source_tool': 'query+context'}])
        self.assertNotIn('note', knowledge['code_graph'])
        self.assertNotIn('dedup_ran', knowledge['code_graph'])
        self.assertEqual(knowledge['wiki']['modules'], ['住院护士站'])
        self.assertEqual(knowledge['wiki']['findings'], [
            {'entity': '初核业务规则', 'state': 'wiki-确认', 'source': 'wiki/topics/住院.md'}])
        self.assertEqual(knowledge['history']['findings'], [
            {'work_item_id': 260001, 'fact': '既有出院带药提示',
             'state': '已证实', 'maturity': '已落地'}])
        self.assertNotIn('coverage', knowledge['history'])
        # 原计划对象未被原地修改
        self.assertIn('note', plan['kb'])

        # 切到 NEED-REVIEW（带 kb、不传描述）：analysis_description 应被 HDEL，knowledge 仍在
        commands.clear()
        qc_plan = {'work_item_id': 1, 'run_id': 'run_qc_1234', 'verdict': 'NEED-REVIEW',
                   'tags': ['PM-AI-QC-NEED-REVIEW'], 'state_to': None,
                   'kb': {'ready': True, 'findings': [{'entity': 'e', 'state': '候选',
                            'source_tool': 'search_knowledge'}]}}
        with mock.patch.object(redis_client, '_Connection', return_value=Connection()), \
                mock.patch.object(redis_client, 'load_redis_config', return_value={'ttl_seconds': 0}):
            redis_client.publish_plan(qc_plan, 'dry-run', 'CollectionA', 'config.json')
        hdel_fields = {cmd[2] for cmd in commands
                       if cmd[:2] == ('HDEL', 'auto-req:qc:plan:CollectionA:1')}
        self.assertIn('analysis_description', hdel_fields)
        self.assertNotIn('knowledge', hdel_fields)  # kb 在 → knowledge 保留，不清理
        qc_fields = dict(zip(commands[0][2::2], commands[0][3::2]))
        self.assertIn('knowledge', qc_fields)
        self.assertNotIn('analysis_description', qc_fields)

    def test_knowledge_summary_helper(self):
        # 三类来源缺失或为空 dict → {}
        self.assertEqual(redis_client._knowledge_summary({}), {})
        self.assertEqual(redis_client._knowledge_summary({'kb': {}, 'wiki': {}, 'tfs_requirements': {}}), {})
        plan = {
            'kb': {'ready': True, 'tools_used': ['query'], 'dedup_ran': True,
                   'findings': [{'entity': 'e1', 'state': '已证实', 'source_tool': 'query',
                                 'note': 'drop'}], 'note': 'drop'},
            'wiki': {'ready': False, 'modules_matched': ['m1'],
                     'findings': [{'entity': 'e2', 'state': 'wiki-冲突', 'source': 'p.md'}]},
            'tfs_requirements': {'ready': True, 'coverage': {'collection': 'x'},
                                 'findings': [{'work_item_id': 9, 'fact': 'f', 'state': '候选',
                                               'maturity': '', 'source_tool': 'search_requirements'}]},
        }
        summary = redis_client._knowledge_summary(plan)
        self.assertEqual(set(summary), {'code_graph', 'wiki', 'history'})
        self.assertEqual(summary['code_graph']['tools'], ['query'])
        self.assertEqual(summary['code_graph']['findings'], [
            {'entity': 'e1', 'state': '已证实', 'source_tool': 'query'}])
        self.assertNotIn('dedup_ran', summary['code_graph'])
        self.assertNotIn('note', summary['code_graph'])
        self.assertEqual(summary['wiki']['modules'], ['m1'])
        self.assertEqual(summary['wiki']['findings'], [
            {'entity': 'e2', 'state': 'wiki-冲突', 'source': 'p.md'}])
        self.assertNotIn('coverage', summary['history'])
        self.assertEqual(summary['history']['findings'], [
            {'work_item_id': 9, 'fact': 'f', 'state': '候选', 'maturity': ''}])
        # 存在但 findings 为空：仍报来源键（ready/tools 有用），findings=[]
        self.assertEqual(
            redis_client._knowledge_summary({'kb': {'ready': False, 'tools_used': [], 'findings': []}}),
            {'code_graph': {'ready': False, 'tools': [], 'findings': []}})

    def test_publish_skip_analysis_exposes_reason_and_removes_question_fields(self):
        commands = []

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def execute(self, *args):
                commands.append(args)
                return 1

        plan = {
            'work_item_id': 1, 'run_id': 'run_skip_1234', 'verdict': 'SKIP-ANALYSIS',
            'tags': [], 'state_to': None, 'generated_at_utc': '2026-07-30T00:00:00Z',
            'skip_reason': '仅安排既有接口联调，无新增业务分析面。',
        }
        with mock.patch.object(redis_client, '_Connection', return_value=Connection()), \
                mock.patch.object(redis_client, 'load_redis_config', return_value={'ttl_seconds': 0}):
            response = redis_client.publish_plan(plan, 'execute', 'CollectionA', 'config.json')
        self.assertTrue(response['ok'])
        fields = dict(zip(commands[0][2::2], commands[0][3::2]))
        self.assertEqual(fields['skip_reason'], plan['skip_reason'])
        self.assertEqual(fields['generated_at_utc'], plan['generated_at_utc'])
        for stale in ('checklist', 'work_item', 'next'):
            self.assertIn(('HDEL', 'auto-req:qc:plan:CollectionA:1', stale), commands)

    def test_publish_plan_projects_lean_checklist(self):
        commands = []

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def execute(self, *args):
                commands.append(args)
                return 1

        checklist = {
            'work_item': '1 标题', 'verdict': 'NEED-REVIEW', 'tag': 'PM-AI-QC-NEED-REVIEW',
            'responsible': '产品 + 研发负责人', 'generated_at_utc': '2026-07-29T00:00:00Z',
            'items': [{'id': 'q1', 'priority': 'P0', 'category': '方向', 'responsible': '产品',
                       'primary': True, 'question': '是否先排查根因？', 'why': '治标警惕',
                       'options': ['修缺陷', '保留限制'], 'allow_other': True}],
            'notes': ['非阻断提示'], 'passed': ['时效正常'], 'kb_note': '图谱事实: ...',
            'next': '重跑 auto-req-analysis 1',
        }
        plan = {'work_item_id': 1, 'run_id': 'run_12345678', 'verdict': 'NEED-REVIEW',
                'tags': ['PM-AI-QC-NEED-REVIEW'], 'state_to': None, 'checklist': checklist}
        with mock.patch.object(redis_client, '_Connection', return_value=Connection()), \
                mock.patch.object(redis_client, 'load_redis_config', return_value={'ttl_seconds': 0}):
            response = redis_client.publish_plan(plan, 'dry-run', 'CollectionA', 'config.json')
        self.assertTrue(response['ok'])
        fields = dict(zip(commands[0][2::2], commands[0][3::2]))
        # work_item / next 提升为顶层 Hash 字段
        self.assertEqual(fields['work_item'], '1 标题')
        self.assertEqual(fields['next'], '重跑 auto-req-analysis 1')
        # checklist 只留 responsible + items（items 含 why/options 原样保留）
        stored = json.loads(fields['checklist'])
        self.assertEqual(set(stored), {'responsible', 'items'})
        self.assertEqual(stored['items'], checklist['items'])
        self.assertEqual(stored['responsible'], checklist['responsible'])
        # 其余字段不在 checklist（与外层重复的 verdict/tag/generated_at_utc、内部 kb_note/notes/passed、已提升的 work_item/next）
        for omitted in ('work_item', 'next', 'verdict', 'tag', 'generated_at_utc',
                        'kb_note', 'notes', 'passed'):
            self.assertNotIn(omitted, stored)
        # 原计划 checklist 未被原地修改（TFS 附件 待补充信息 / 审计仍为完整版）
        self.assertEqual(set(plan['checklist']), set(checklist))

    def test_encode_command_resp2_format(self):
        cmd = redis_client.encode_command('HSET', 'k', 'f', 'v')
        self.assertEqual(cmd, b'*4\r\n$4\r\nHSET\r\n$1\r\nk\r\n$1\r\nf\r\n$1\r\nv\r\n')

    def test_parse_redis_url(self):
        self.assertEqual(redis_client._parse_redis_url('redis://127.0.0.1:6379/0'),
                         ('127.0.0.1', 6379, 0, ''))
        self.assertEqual(redis_client._parse_redis_url('redis://:secret@host:6380/2'),
                         ('host', 6380, 2, 'secret'))
        self.assertIsNone(redis_client._parse_redis_url('http://x'))

    def test_load_redis_config_defaults_when_section_absent(self):
        cfg = redis_client.load_redis_config(os.path.join(tempfile.gettempdir(), 'no-such-config.json'))
        self.assertEqual(cfg['host'], '127.0.0.1')
        self.assertEqual(cfg['port'], 6379)
        self.assertEqual(cfg['db'], 0)

    def test_publish_plan_roundtrip_if_redis_up(self):
        config = os.path.join(os.path.dirname(__file__), 'tfs-config.json')
        if not redis_client.ping(config):
            self.skipTest('本机 Redis 未启动，跳过 round-trip')
        wid = 999000999  # 测试专用 id，避免污染真实工作项
        plan = {'version': 1, 'run_id': 'run_test_123', 'skill': 'auto-req-qc', 'work_item_id': wid,
                'expected_rev': 1, 'expected_state': '已建议', 'verdict': 'NEED-REVIEW',
                'tags': ['PM-AI-QC-NEED-REVIEW'], 'state_to': None, 'rules_source': 'pre-qc-v1',
                'checklist': {'work_item': f'{wid} 标题', 'responsible': '产品',
                              'generated_at_utc': '2026-07-23',
                              'items': [{'id': 'q1', 'question': 'q?', 'options': ['a'], 'allow_other': True}],
                              'next': f'重跑 auto-req-analysis {wid}'},
                'artifacts': [], 'kb': {'ready': True}}
        collection = 'auto_req_test'
        try:
            r = redis_client.publish_plan(plan, 'dry-run', collection, config)
            self.assertTrue(r['ok'])
            data = redis_client.hgetall(f'auto-req:qc:plan:{collection}:{wid}', config)
            self.assertEqual(data['verdict'], 'NEED-REVIEW')
            self.assertEqual(data['run_mode'], 'dry-run')
            self.assertIn('PM-AI-QC-NEED-REVIEW', data['tags'])
            self.assertEqual(data['work_item'], f'{wid} 标题')
            self.assertEqual(data['next'], f'重跑 auto-req-analysis {wid}')
            self.assertEqual(json.loads(data['checklist']),
                             {'responsible': '产品', 'items': plan['checklist']['items']})
            self.assertNotIn('plan_json', data)
        finally:
            try:
                with redis_client._Connection(redis_client.load_redis_config(config)) as c:
                    c.execute('DEL', f'auto-req:qc:plan:{collection}:{wid}')
                    c.execute('SREM', f'auto-req:qc:ids:{collection}', wid)
            except Exception:
                pass

    def test_publish_plan_roundtrip_analysis_and_knowledge_if_redis_up(self):
        config = os.path.join(os.path.dirname(__file__), 'tfs-config.json')
        if not redis_client.ping(config):
            self.skipTest('本机 Redis 未启动，跳过 round-trip')
        wid = 999000998  # 测试专用 id，避免污染真实工作项
        plan = {'version': 2, 'run_id': 'run_auto_demo', 'skill': 'auto-req-analysis',
                'work_item_id': wid, 'expected_rev': 1, 'expected_state': '已分析',
                'verdict': 'AUTO-ANA', 'tags': ['PM-AI-AUTO-ANA'], 'state_to': '已分析',
                'rules_source': {'qc': 'pre-qc-v1', 'analysis': 'evidence-loop-v1'},
                'generated_at_utc': '2026-08-04', 'artifacts': [],
                'analysis_description': {'categories': ['existing-ui-simple']},
                'kb': {'ready': True, 'dedup_ran': True, 'tools_used': ['query', 'context'],
                       'findings': [{'entity': '初核=医嘱校对 adviceProofread', 'state': '已证实',
                                     'source_tool': 'query+context'}], 'note': '代码图谱就绪'},
                'wiki': {'ready': True, 'modules_matched': ['住院护士站'],
                         'findings': [{'entity': '初核业务规则', 'state': 'wiki-确认',
                                       'source': 'wiki/topics/住院.md'}]},
                'tfs_requirements': {'ready': True,
                                     'findings': [{'work_item_id': 260001, 'fact': '既有出院带药提示',
                                                   'state': '已证实', 'maturity': '已落地',
                                                   'source_tool': 'get_work_item'}]}}
        html = '<div><br></div><div>核心改造点：在医嘱校对界面增加出院带药强提示</div><div><br></div>'
        collection = 'auto_req_test'
        try:
            r = redis_client.publish_plan(plan, 'execute', collection, config,
                                          analysis_description_html=html,
                                          work_item=f'{wid} 医嘱校对-出院带药强提示（测试）')
            self.assertTrue(r['ok'])
            data = redis_client.hgetall(f'auto-req:qc:plan:{collection}:{wid}', config)
            self.assertEqual(data['verdict'], 'AUTO-ANA')
            self.assertEqual(data['run_mode'], 'execute')
            self.assertIn('PM-AI-AUTO-ANA', data['tags'])
            self.assertEqual(data['work_item'], f'{wid} 医嘱校对-出院带药强提示（测试）')
            self.assertEqual(data['analysis_description'], html)
            # 新字段：knowledge 三类来源「定位 + 作用」精简投影
            knowledge = json.loads(data['knowledge'])
            self.assertEqual(set(knowledge), {'code_graph', 'wiki', 'history'})
            self.assertEqual(knowledge['code_graph']['tools'], ['query', 'context'])
            self.assertEqual(knowledge['wiki']['modules'], ['住院护士站'])
            self.assertEqual(knowledge['history']['findings'][0]['work_item_id'], 260001)
            self.assertNotIn('plan_json', data)
            print('\n=== Redis HGETALL auto-req:qc:plan:%s:%s ===' % (collection, wid))
            print(json.dumps(data, ensure_ascii=False, indent=2))
        finally:
            try:
                with redis_client._Connection(redis_client.load_redis_config(config)) as c:
                    c.execute('DEL', f'auto-req:qc:plan:{collection}:{wid}')
                    c.execute('SREM', f'auto-req:qc:ids:{collection}', wid)
            except Exception:
                pass


if __name__ == '__main__':
    unittest.main()
