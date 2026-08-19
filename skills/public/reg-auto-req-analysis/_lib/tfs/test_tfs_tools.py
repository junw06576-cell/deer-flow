import datetime
import copy
import os
import sys
import tempfile
import unittest
import gzip
import json
import pathlib
import shutil
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest import mock


sys.path.insert(0, os.path.dirname(__file__))
import pipeline  # noqa: E402
import tfs_client as tfs  # noqa: E402
import attachment_converter as converter  # noqa: E402
import attachment_runtime as attachment_runtime  # noqa: E402
import redis_client  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import build_menu_business_index as menu_index  # noqa: E402
import skill_memory  # noqa: E402


def resolved_knowledge_route():
    return {
        'status': 'RESOLVED',
        'area': 'NETHIS5.5',
        'product_id': 'cloudhis-v56',
        'product_name': '云HIS 5.6',
        'profile_version': 1,
        'servers': {
            'requirements_history': 'tfs-requirements',
            'code_graph': 'gitnexus-team',
            'source_code': 'cloudhis-source',
            'database': 'db-knowledge',
        },
    }


class TfsClientTests(unittest.TestCase):
    def test_fetch_raw_consumes_pipeline_guard_and_rejects_stale_revision(self):
        raw = {'id': 1, 'rev': 6, 'fields': {tfs.F_STATE: '已建议'}}
        client = {'_pipeline_source_guard': {
            'work_item_id': 1, 'rev': 5, 'state': '已建议',
        }}
        with mock.patch.object(tfs, 'wit_retry', return_value=(200, raw)):
            with self.assertRaisesRegex(RuntimeError, 'SOURCE_CHANGED_DURING_EXECUTION'):
                tfs.fetch_raw(client, 1)
        self.assertNotIn('_pipeline_source_guard', client)

        client['_pipeline_source_guard'] = {
            'work_item_id': 1, 'rev': 6, 'state': '已建议',
        }
        with mock.patch.object(tfs, 'wit_retry', return_value=(200, raw)):
            self.assertIs(tfs.fetch_raw(client, 1), raw)
        self.assertNotIn('_pipeline_source_guard', client)

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

    def test_replace_detail_analysis_section_returns_patch_revision(self):
        old = '<div>【分析者描述】</div><div>旧内容</div>'
        raw = {'id': 1, 'rev': 8, 'fields': {tfs.F_DESCRIPTION: old}}
        patched = {'id': 1, 'rev': 10, 'fields': {tfs.F_STATE: '已建议'}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'patch_workitem', return_value=(200, patched)):
            response = tfs.replace_detail_analysis_section(
                {}, 1, '<div>新内容</div>', False)
        self.assertTrue(response['ok'])
        self.assertEqual(response['post_rev'], 10)
        self.assertEqual(response['post_state'], '已建议')

    def test_replace_detail_analysis_section_ignores_run_marker_only_difference(self):
        # 263409 回归：TFS HTML 字段落库剥 run 标记注释——只差标记 ≠ 内容差异，
        # 必须按 no-op 处理，不发会被 TFS 静默吞掉（不建 revision）的无效 PATCH。
        section = '<div><br></div><div>内容</div><div><br></div>'
        old = '<div>【分析者描述】</div>' + section
        rendered = '<!-- auto-req-run:run_x -->' + section
        raw = {'id': 1, 'rev': 8, 'fields': {tfs.F_DESCRIPTION: old}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'patch_workitem') as patch:
            response = tfs.replace_detail_analysis_section({}, 1, rendered, False)
        self.assertTrue(response['ok'])
        self.assertTrue(response['noop'])
        self.assertIn('已是目标内容', response['msg'])
        patch.assert_not_called()

    def test_replace_detail_analysis_section_rejects_invalid_section_markers(self):
        # 只有“分析者描述”是硬要求：重复或缺完整 HTML 块标签才拒绝；
        # “开发者描述”缺失或乱序不再阻断写入（见 appends/replaces 用例）。
        for description in (
                '<div>【分析者描述】</div><div>【分析者描述】</div>',   # 重复分析者描述
                '【分析者描述】裸文本无 HTML 块标签'):                  # 缺完整 HTML 块标签
            raw = {'id': 1, 'rev': 4, 'fields': {tfs.F_DESCRIPTION: description}}
            with self.subTest(description=description), \
                    mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                    mock.patch.object(tfs, 'patch_workitem') as patch:
                response = tfs.replace_detail_analysis_section({}, 1, '<div>新内容</div>', False)
            self.assertFalse(response['ok'])
            patch.assert_not_called()

    def test_replace_detail_analysis_section_appends_when_markers_absent(self):
        # 非模板工作项：详细信息无【分析者描述】→ 末尾追加分析者描述区段，不新增【开发者描述】
        old = '<div>附件</div><p>一、必填字段</p><p>原始需求正文</p>'
        raw = {'id': 1, 'rev': 4, 'fields': {tfs.F_DESCRIPTION: old}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'patch_workitem', return_value=(200, {})) as patch:
            response = tfs.replace_detail_analysis_section({}, 1, '<div>新内容</div>', False)
        self.assertTrue(response['ok'])
        self.assertTrue(response.get('appended'))
        value = patch.call_args.args[3][0]['value']
        self.assertEqual(value, old + '<div>【分析者描述】</div><div>新内容</div>')
        self.assertEqual(value.count('【分析者描述】'), 1)
        self.assertNotIn('【开发者描述】', value)

    def test_replace_detail_analysis_section_append_then_idempotent(self):
        old = '<div>附件</div><p>原始需求正文</p>'
        appended = old + '<div>【分析者描述】</div><div>新内容</div>'
        raw = {'id': 1, 'rev': 4, 'fields': {tfs.F_DESCRIPTION: appended}}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'patch_workitem') as patch:
            response = tfs.replace_detail_analysis_section({}, 1, '<div>新内容</div>', False)
        self.assertTrue(response['ok'])
        self.assertTrue(response.get('noop'))
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

    def test_attachment_noop_requires_same_filename_and_content(self):
        client = {
            'base_url': 'http://example/tfs/collection/project',
            'server': 'example', 'port': 80, 'collection': 'collection',
            'project': 'project', 'pat': 'x',
        }
        name = '需求分析报告_1_run_12345678.md'
        relation = {'rel': 'AttachedFile',
                    'url': 'http://example/tfs/collection/_apis/wit/attachments/old?fileName=' + urllib.parse.quote(name)}
        raw = {'id': 1, 'rev': 8, 'fields': {}, 'relations': [relation]}

        def response(content):
            opened = mock.MagicMock()
            opened.__enter__.return_value.read.return_value = content
            return opened

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, name)
            with open(path, 'wb') as output:
                output.write(b'current')
            with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                    mock.patch.object(tfs.urllib.request, 'urlopen',
                                      return_value=response(b'current')), \
                    mock.patch.object(tfs, 'wit_http') as upload:
                same = tfs.upload_attachment(client, 1, path, False)
            self.assertTrue(same['noop'])
            upload.assert_not_called()

            with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                    mock.patch.object(tfs.urllib.request, 'urlopen',
                                      return_value=response(b'stale')):
                changed = tfs.upload_attachment(client, 1, path, True)
        self.assertTrue(changed['replace_same_name'])
        self.assertFalse(changed.get('noop', False))

        malicious = dict(raw)
        malicious['relations'] = [{
            'rel': 'AttachedFile',
            'url': 'http://example/untrusted?fileName=' + urllib.parse.quote(name),
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, name)
            with open(path, 'wb') as output:
                output.write(b'current')
            with mock.patch.object(tfs, 'fetch_raw', return_value=malicious), \
                    mock.patch.object(tfs.urllib.request, 'urlopen') as urlopen:
                rejected = tfs.upload_attachment(client, 1, path, True)
        self.assertFalse(rejected['ok'])
        urlopen.assert_not_called()

        project_scoped = (
            'http://example/tfs/collection/project/_apis/wit/attachments/old?fileName='
            + urllib.parse.quote(name))
        self.assertTrue(tfs._attachment_url_allowed(client, project_scoped))

    def test_cleanup_can_keep_the_relation_matching_frozen_report_digest(self):
        client = {'server': 'example', 'port': 80, 'collection': 'collection', 'pat': 'x'}
        keep = '需求分析报告_1_run_new_1234.md'
        raw = {'id': 1, 'rev': 8, 'relations': [
            {'rel': 'AttachedFile',
             'url': 'http://example/tfs/collection/_apis/wit/attachments/old?fileName=' + urllib.parse.quote(keep)},
            {'rel': 'AttachedFile',
             'url': 'http://example/tfs/collection/_apis/wit/attachments/new?fileName=' + urllib.parse.quote(keep)},
        ]}

        def response(content):
            opened = mock.MagicMock()
            opened.__enter__.return_value.read.return_value = content
            return opened

        verified = {'id': 1, 'rev': 9, 'relations': [raw['relations'][1]]}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, keep)
            with open(path, 'wb') as output:
                output.write(b'current')
            with mock.patch.object(tfs, 'fetch_raw', side_effect=[raw, verified]), \
                    mock.patch.object(tfs.urllib.request, 'urlopen',
                                      side_effect=[response(b'stale'), response(b'current')]), \
                    mock.patch.object(tfs, 'patch_workitem', return_value=(200, {})) as patch:
                cleaned = tfs.cleanup_analysis_attachments(
                    client, 1, keep, False, expected_path=path)
        self.assertTrue(cleaned['ok'])
        operations = patch.call_args.args[3]
        self.assertEqual(operations, [{'op': 'remove', 'path': '/relations/0'}])

    def test_cleanup_analysis_attachments_dry_run_only_targets_controlled_names(self):
        def attached(name):
            return {'rel': 'AttachedFile',
                    'url': 'http://example/attachment?fileName=' + urllib.parse.quote(name)}

        keep = '需求分析报告_1_run_new_1234.md'
        raw = {'id': 1, 'rev': 8, 'relations': [
            {'rel': 'Hyperlink', 'url': 'http://example/business'},
            attached('变更方案_1_run_old_1234.md'),
            attached(keep),
            attached(keep),
            attached('待确认清单_1_run_old_1234.md'),
            attached('待补充信息_1_run_old_1234.json'),
            attached('接口说明.md'),
            attached('变更方案_2_run_old_1234.md'),
            attached('变更方案_1_short.md'),
        ]}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'patch_workitem') as patch:
            response = tfs.cleanup_analysis_attachments({}, 1, keep, True)
        self.assertTrue(response['ok'])
        self.assertEqual(response['removed'], [
            '变更方案_1_run_old_1234.md', keep,
            '待确认清单_1_run_old_1234.md', '待补充信息_1_run_old_1234.json'])
        self.assertFalse(response['verified'])
        patch.assert_not_called()

    def test_cleanup_analysis_attachments_execute_removes_descending_and_verifies(self):
        def attached(name):
            return {'rel': 'AttachedFile',
                    'url': 'http://example/attachment?fileName=' + urllib.parse.quote(name)}

        keep = '需求分析报告_1_run_new_1234.md'
        before = {'id': 1, 'rev': 8, 'relations': [
            attached('变更方案_1_run_old_1234.md'),
            attached(keep),
            attached('业务附件.pdf'),
            attached('待确认清单_1_run_old_1234.md'),
            attached(keep),
            attached('待补充信息_1_run_old_1234.json'),
        ]}
        after = {'id': 1, 'rev': 9, 'relations': [attached(keep), attached('业务附件.pdf')]}
        with mock.patch.object(tfs, 'fetch_raw', side_effect=[before, after]) as fetch, \
                mock.patch.object(tfs, 'wit_retry', return_value=(200, {})) as request:
            response = tfs.cleanup_analysis_attachments({}, 1, keep, False)
        self.assertTrue(response['ok'])
        self.assertTrue(response['verified'])
        self.assertEqual(fetch.call_count, 2)
        payload = request.call_args.args[3]
        self.assertEqual(payload[0], {'op': 'test', 'path': '/rev', 'value': 8})
        self.assertEqual([operation['path'] for operation in payload[1:]],
                         ['/relations/5', '/relations/4', '/relations/3', '/relations/0'])

    def test_cleanup_analysis_attachments_execute_noop_still_rereads(self):
        keep = '需求分析报告_1_run_new_1234.md'
        raw = {'id': 1, 'rev': 8, 'relations': [{
            'rel': 'AttachedFile',
            'url': 'http://example/attachment?fileName=' + urllib.parse.quote(keep),
        }]}
        with mock.patch.object(tfs, 'fetch_raw', side_effect=[raw, raw]) as fetch, \
                mock.patch.object(tfs, 'patch_workitem') as patch:
            response = tfs.cleanup_analysis_attachments({}, 1, keep, False)
        self.assertTrue(response['ok'])
        self.assertTrue(response['noop'])
        self.assertTrue(response['verified'])
        self.assertEqual(fetch.call_count, 2)
        patch.assert_not_called()

    def test_cleanup_analysis_attachments_fails_closed_for_missing_current_or_patch_conflict(self):
        keep = '需求分析报告_1_run_new_1234.md'
        old = {'id': 1, 'rev': 8, 'relations': [{
            'rel': 'AttachedFile',
            'url': 'http://example/attachment?fileName=' + urllib.parse.quote('变更方案_1_run_old_1234.md'),
        }]}
        with mock.patch.object(tfs, 'fetch_raw', return_value=old), \
                mock.patch.object(tfs, 'patch_workitem') as patch:
            missing = tfs.cleanup_analysis_attachments({}, 1, keep, False)
        self.assertFalse(missing['ok'])
        patch.assert_not_called()

        current = {'id': 1, 'rev': 8, 'relations': old['relations'] + [{
            'rel': 'AttachedFile',
            'url': 'http://example/attachment?fileName=' + urllib.parse.quote(keep),
        }]}
        with mock.patch.object(tfs, 'fetch_raw', return_value=current), \
                mock.patch.object(tfs, 'patch_workitem', return_value=(412, {'message': 'rev changed'})):
            conflict = tfs.cleanup_analysis_attachments({}, 1, keep, False)
        self.assertFalse(conflict['ok'])
        self.assertIn('HTTP 412', conflict['error'])

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
            legacy = tfs.record(
                'auto-req-analysis', 1, 'MANUAL-REVIEW', [], '', '', '', {},
                'run_legacy_1234', audit_group='legacy-runs')
            self.assertIn(os.path.join('1', 'legacy-runs'), legacy['audit'])

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
            self.assertNotIn('apis', index['menus'][0])
            self.assertNotIn('tfs_area_values', index['menus'][0])
            duplicate = dict(manifest)
            duplicate['sources'] = [dict(item) for item in manifest['sources']]
            duplicate['sources'][1]['tfs_area_values'] = ['AREA-A']
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(duplicate, f)
            with self.assertRaisesRegex(ValueError, '区域只能归属一个产品'):
                menu_index.build_index(manifest_path)

    def test_product_mcp_route_selects_only_the_area_product(self):
        index = {
            'products': [
                {'product_id': 'product-a', 'product_name': '产品 A',
                 'tfs_area_values': ['AREA-A']},
                {'product_id': 'product-b', 'product_name': '产品 B',
                 'tfs_area_values': ['AREA-B']},
            ],
        }

        def profile(prefix, port):
            return {
                role: {
                    'enabled': True,
                    'server_name': f'{prefix}-{role}',
                    'url': f'http://127.0.0.1:{port + offset}/mcp',
                    'tools': [f'{role}_tool'],
                }
                for offset, role in enumerate(menu_index.MCP_ROLES)
            }

        routes = {'version': 1, 'profiles': {
            'product-a': profile('a', 4700),
            'product-b': profile('b', 4800),
        }}
        result = menu_index.resolve_mcp_route(index, routes, 'AREA-B')
        self.assertTrue(result['ok'])
        self.assertEqual(result['route_status'], 'RESOLVED')
        self.assertEqual(result['product_id'], 'product-b')
        self.assertEqual(result['servers']['source_code'], 'b-source_code')
        self.assertNotIn('a-', json.dumps(result))

    def test_cloudhis_area_resolves_to_current_profile(self):
        result = menu_index.resolve_mcp_route(
            menu_index.read_json(menu_index.DEFAULT_OUTPUT),
            menu_index.read_json(menu_index.DEFAULT_ROUTES),
            'NETHIS5.5',
        )
        self.assertEqual(result, {
            'ok': True,
            'route_status': 'RESOLVED',
            'area': 'NETHIS5.5',
            'product_id': 'cloudhis-v56',
            'product_name': '云HIS 5.6',
            'profile_version': 1,
            'servers': {
                'requirements_history': 'tfs-requirements',
                'code_graph': 'gitnexus-team',
                'source_code': 'cloudhis-source',
                'database': 'db-knowledge',
            },
        })

    def test_product_mcp_route_returns_stable_unresolved_statuses_without_servers(self):
        profile = {
            role: {'enabled': False, 'reason': '尚未提供'}
            for role in menu_index.MCP_ROLES
        }
        routes = {'version': 1, 'profiles': {'product-a': profile}}
        unmapped = menu_index.resolve_mcp_route({'products': []}, routes, 'UNKNOWN')
        self.assertEqual(unmapped['route_status'], 'AREA_UNMAPPED')
        self.assertEqual(unmapped['servers'], {})

        ambiguous_index = {'products': [
            {'product_id': 'product-a', 'product_name': 'A', 'tfs_area_values': ['AREA']},
            {'product_id': 'product-b', 'product_name': 'B', 'tfs_area_values': ['AREA']},
        ]}
        ambiguous = menu_index.resolve_mcp_route(ambiguous_index, routes, 'AREA')
        self.assertEqual(ambiguous['route_status'], 'AREA_AMBIGUOUS')
        self.assertEqual(ambiguous['servers'], {})

        missing = menu_index.resolve_mcp_route({
            'products': [{'product_id': 'product-b', 'product_name': 'B',
                          'tfs_area_values': ['AREA-B']}],
        }, routes, 'AREA-B')
        self.assertEqual(missing['route_status'], 'PROFILE_MISSING')
        self.assertEqual(missing['servers'], {})

    def test_product_mcp_route_rejects_invalid_profile(self):
        valid_role = {'enabled': True, 'server_name': 'shared',
                      'url': 'http://127.0.0.1:4700/mcp', 'tools': ['tool']}
        missing_role = {'version': 1, 'profiles': {
            'product-a': {'requirements_history': valid_role},
        }}
        with self.assertRaisesRegex(ValueError, 'MCP 角色不完整'):
            menu_index.validate_mcp_routes(missing_role)

        disabled_without_reason = {'version': 1, 'profiles': {
            'product-a': {
                role: {'enabled': False} for role in menu_index.MCP_ROLES
            },
        }}
        with self.assertRaisesRegex(ValueError, '禁用时必须含非空 reason'):
            menu_index.validate_mcp_routes(disabled_without_reason)

        invalid_url = {'version': 1, 'profiles': {
            'product-a': {
                role: {'enabled': True, 'server_name': f'a-{role}',
                       'url': 'file:///tmp/mcp', 'tools': ['tool']}
                for role in menu_index.MCP_ROLES
            },
        }}
        with self.assertRaisesRegex(ValueError, r'有效 HTTP\(S\) URL'):
            menu_index.validate_mcp_routes(invalid_url)

        def profile(prefix):
            return {
                role: {'enabled': True, 'server_name': 'duplicate' if role == 'source_code'
                       else f'{prefix}-{role}', 'url': 'http://127.0.0.1:4700/mcp',
                       'tools': ['tool']}
                for role in menu_index.MCP_ROLES
            }
        duplicate_server = {'version': 1, 'profiles': {
            'product-a': profile('a'), 'product-b': profile('b'),
        }}
        with self.assertRaisesRegex(ValueError, '不可跨产品重复'):
            menu_index.validate_mcp_routes(duplicate_server)

        with tempfile.TemporaryDirectory() as directory:
            route_path = os.path.join(directory, 'routes.json')
            with open(route_path, 'w', encoding='utf-8') as f:
                f.write('{"version":1,"profiles":{"product-a":{},"product-a":{}}}')
            with self.assertRaisesRegex(ValueError, 'JSON 键不可重复: product-a'):
                menu_index.read_json(route_path, reject_duplicate_keys=True)

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
        if analysis_profile == 'concise-v3':
            lines.append('- **菜单路径**：业务管理 > 测试功能')
        else:
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
        plan, plan_path, artifact_path = self.write_analysis_plan(
            directory, categories, run_id=run_id,
            analysis_rule='evidence-loop-v1', analysis_profile='concise-v3')
        plan['confirmation_policy'] = pipeline.SINGLE_CONFIRMATION_POLICY
        plan['kb']['database_ready'] = False
        plan['kb']['source_ready'] = True
        plan['kb']['source_required'] = False
        plan['knowledge_route'] = self.resolved_knowledge_route()
        for finding in plan['kb']['findings']:
            finding['source_type'] = 'code'
            finding['conclusion'] = '已定位测试功能的现有实现入口'
            finding['evidence'] = finding['entity']
            finding['boundary'] = '仅证明测试代码图谱中的入口，不代表现场已部署。'
        if 'field-ui-copy' in plan['auto_scopes']:
            plan['ui_baseline'] = {
                'sources': [
                    {'type': 'code-graph', 'ref': 'kb:0'},
                    {'type': 'runtime-observation', 'ref': '受控测试页面：业务管理 > 测试功能'},
                ]
            }
        return plan, plan_path, artifact_path

    def test_current_ui_auto_requires_two_independent_baseline_source_families(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            plan.pop('ui_baseline')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('必须含 ui_baseline' in error for error in result['errors']))

            plan['kb']['source_required'] = True
            plan['kb']['tools_used'].append('search_symbol')
            plan['kb']['findings'].append({
                'entity': '测试源码锚点', 'state': '已证实',
                'source_tool': 'search_symbol', 'source_type': 'code',
                'conclusion': '源码中存在测试保存入口',
                'evidence': '测试源码锚点',
                'boundary': '仅证明受控仓库源码，不代表现场已部署。',
            })
            plan['ui_baseline'] = {
                'sources': [
                    {'type': 'code-graph', 'ref': 'kb:0'},
                    {'type': 'source-code', 'ref': 'kb:1'},
                ]
            }
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('至少覆盖两类独立证据' in error for error in result['errors']))

    def test_current_ui_auto_allows_runtime_plus_code_without_wiki(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            self.assertNotIn('wiki', plan)
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def resolved_knowledge_route(self):
        return resolved_knowledge_route()

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
        plan, plan_path, artifact = self.write_current_analysis_plan(
            directory, categories, run_id=run_id)
        plan['rules_source']['analysis'] = 'evidence-loop-v2'
        report_name = f'需求分析报告_1_{run_id}.md'
        report_path = os.path.join(directory, report_name)
        os.replace(artifact, report_path)
        with open(report_path, 'r', encoding='utf-8') as f:
            report_content = f.read()
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report_content.replace('# 变更方案\n', '# 需求分析报告\n', 1))
        plan['artifacts'][0]['path'] = report_name
        plan['implementation_impacts'] = ['ui-presentation']
        plan['general_rule_coverage'] = {
            'scope': {
                'status': 'CONFIRMED', 'source': 'work-item',
                'basis': '工作项已明确改动范围。',
            },
            'workflow': {
                'status': 'NOT_APPLICABLE', 'source': 'not-applicable',
                'basis': '本测试计划不改变业务流程。',
            },
            'business_semantics': {
                'status': 'CONFIRMED', 'source': 'work-item',
                'basis': '工作项已明确字段业务含义。',
            },
            'business_rules': {
                'status': 'NOT_APPLICABLE', 'source': 'not-applicable',
                'basis': '本测试计划不新增业务计算或判断规则。',
            },
            'permissions': {
                'status': 'NOT_APPLICABLE', 'source': 'not-applicable',
                'basis': '本测试计划不改变角色与数据权限。',
            },
            'exceptions': {
                'status': 'NOT_APPLICABLE', 'source': 'not-applicable',
                'basis': '本测试计划不引入新的异常分支。',
            },
            'acceptance': {
                'status': 'CONFIRMED', 'source': 'work-item',
                'basis': '工作项已明确可验证的验收结果。',
            },
        }
        plan['business_rule_coverage'] = {
            'presentation': {
                'status': 'DEFAULTED',
                'source': 'presentation-default',
                'basis': '呈现类默认：沿用当前页面布局。',
            },
            'empty_value': {
                'status': 'DEFAULTED',
                'source': 'presentation-default',
                'basis': '呈现类默认：沿用当前空值展示。',
            },
            'maintenance_granularity': {
                'status': 'NOT_APPLICABLE',
                'source': 'not-applicable',
                'basis': '本测试计划不修改数据维护。',
            },
            'historical_data': {
                'status': 'NOT_APPLICABLE',
                'source': 'not-applicable',
                'basis': '本测试计划不影响历史数据。',
            },
        }
        plan['kb']['database_required'] = False
        plan['evidence_acquisition'] = (
            acquisition if acquisition is not None else self.complete_acquisition())
        return plan, plan_path, report_path

    def write_analysis_ref_plan(self, directory, work_item_id=1):
        process_root = os.path.join(directory, '过程文件')
        initialized = pipeline.init_run(work_item_id, process_root=process_root)
        run_id = initialized['run_id']
        run_dir = initialized['run_dir']
        full, _, report_path = self.write_v2_analysis_plan(
            run_dir, ['existing-ui-simple'], run_id=run_id)
        full['work_item_id'] = work_item_id
        full['expected_rev'] = 5
        full['expected_state'] = '已建议'
        semantic = {
            key: value for key, value in full.items()
            if key not in {
                'version', 'run_id', 'skill', 'work_item_id', 'expected_rev', 'expected_state',
                'verdict', 'tags', 'state_to', 'rules_source', 'analysis_description', 'artifacts',
            }
        }
        closure = {
            heading: {label: f'{heading}已明确{label}。' for label in labels}
            for heading, labels in pipeline.ITERATION_ANALYSIS_CLOSURE_REQUIREMENTS.items()
        }
        snapshot = {
            'schema': pipeline.ANALYSIS_RESULT_SCHEMA,
            'work_item_id': work_item_id,
            'run_id': run_id,
            'generated_at_utc': '2026-08-14T00:00:00Z',
            'verdict': full['verdict'],
            **semantic,
            'report': {
                'closure': closure,
                'analysis_description': {
                    'menu_path': '业务管理 > 测试功能',
                    'categories': [{
                        'category': 'existing-ui-simple',
                        'items': [{
                            'label': '界面优化方案',
                            'content': '在测试功能页面调整既有提示，不改变数据写入。',
                        }],
                    }],
                },
                'traceability': [{
                    'id': 'R1', 'scope': '测试功能提示',
                    'behavior': '按已确认规则调整提示',
                    'acceptance': '满足条件时显示调整后的提示',
                    'status': '已证实', 'basis': '工作项',
                }],
            },
        }
        snapshot_name = f'分析结果_{work_item_id}_{run_id}.json'
        snapshot_path = os.path.join(run_dir, snapshot_name)
        with open(snapshot_path, 'w', encoding='utf-8') as output:
            json.dump(snapshot, output, ensure_ascii=False, indent=2)
        os.unlink(report_path)
        plan = {
            'version': 2,
            'plan_profile': pipeline.ANALYSIS_REF_PROFILE,
            'work_item_id': work_item_id,
            'run_id': run_id,
            'expected_rev': 5,
            'expected_state': '已建议',
            'run_receipt': initialized['run_receipt'],
            'analysis_result': {
                'path': snapshot_name,
                'sha256': pipeline.sha256_file(snapshot_path),
            },
        }
        plan_path = os.path.join(run_dir, f'执行计划_{work_item_id}_{run_id}.json')
        with open(plan_path, 'w', encoding='utf-8') as output:
            json.dump(plan, output, ensure_ascii=False, indent=2)
        return plan, plan_path, snapshot_path

    def write_run_bound_plan(self, directory, work_item_id=1, verdict='SKIP-ANALYSIS'):
        process_root = os.path.join(directory, '过程文件')
        initialized = pipeline.init_run(work_item_id, process_root=process_root)
        run_id = initialized['run_id']
        plan = {
            'version': 2,
            'plan_profile': pipeline.RUN_BOUND_PROFILE,
            'run_receipt': initialized['run_receipt'],
            'run_id': run_id,
            'skill': 'auto-req-analysis',
            'work_item_id': work_item_id,
            'expected_rev': 5,
            'expected_state': '已建议',
            'verdict': verdict,
            'tags': [],
            'state_to': None,
            'rules_source': {'qc': 'pre-qc-v1'},
            'artifacts': [],
        }
        if verdict == 'SKIP-ANALYSIS':
            plan['skip_reason'] = '仅安排已开发接口联调，无新增业务分析范围。'
        else:
            tag = ('PM-AI-QC-NEED-INFO' if verdict == 'NEED-INFO'
                   else 'PM-AI-QC-NEED-REVIEW')
            plan['tags'] = [tag]
            plan['checklist'] = {
                'work_item': f'{work_item_id} 测试需求',
                'verdict': verdict,
                'tag': tag,
                'responsible': '产品',
                'generated_at_utc': '2026-08-14T00:00:00Z',
                'next': '补充后使用新的 Idempotency-Key 重新触发',
                'items': [{
                    'id': 'q1', 'question': '请确认业务口径？',
                    'options': ['口径一', '口径二'], 'allow_other': True,
                }],
            }
            filename = f'待补充信息_{work_item_id}_{run_id}.json'
            plan['artifacts'] = [{'kind': 'qc-followup', 'filename': filename}]
        plan_path = os.path.join(
            initialized['run_dir'], f'执行计划_{work_item_id}_{run_id}.json')
        with open(plan_path, 'w', encoding='utf-8') as output:
            json.dump(plan, output, ensure_ascii=False, indent=2)
        return initialized, plan, plan_path

    def add_nonblocking_warning_inputs(self, plan):
        """263409 回归：格式/审计缺口不改变 NEED-REVIEW 业务终局。"""
        plan['confirmation_policy'] = pipeline.SINGLE_CONFIRMATION_POLICY
        plan['checklist']['items'][0]['id'] = 'q-business-rule'
        plan['knowledge_route'] = resolved_knowledge_route()
        plan['kb'] = {
            'ready': True, 'source_ready': True, 'source_required': True,
            'database_ready': True, 'dedup_ran': True,
            'tools_used': ['query', 'search_source'],
            'findings': [{
                'entity': '病案信息手术信息入口', 'state': '已证实',
                'source_tool': 'query', 'source_type': 'code',
                'conclusion': '已定位手术信息入口',
                'evidence': '代码图谱入口证据',
                'boundary': '不证明麻醉四要素校验的具体实现',
            }],
        }
        plan['tfs_requirements'] = {
            'ready': True,
            'coverage': {'state_filter': '已关闭/已验证'},
            'tools_used': ['get_work_item'],
            'findings': [{
                'work_item_id': 113518,
                'fact': '麻醉四要素同填同空规则已落地',
                'state': '已证实', 'maturity': '已落地',
                'source_tool': 'get_work_item',
                'conclusion': '历史需求证实同填同空规则',
                'evidence': '需求113518正文',
                'boundary': '不证明当前源码实现位置',
            }],
            'note': '已核验历史需求正文。',
        }
        plan['attachments'] = {
            'ready': True,
            'downloaded': [{'name': '历史报告.md'}],
            'parsed': [{
                'name': '历史报告.md', 'status': 'parsed',
                'converter': 'builtin-fallback',
                'converter_chain': ['builtin-fallback'],
            }],
            'skipped': [], 'errors': [],
        }
        return plan

    def test_init_run_always_creates_unique_receipted_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, '过程文件')
            first = pipeline.init_run(263409, root)
            second = pipeline.init_run(263409, root)
            self.assertNotEqual(first['run_id'], second['run_id'])
            self.assertNotEqual(first['run_dir'], second['run_dir'])
            self.assertRegex(
                first['run_id'], r'^run_[0-9]{8}_[0-9]{6}_263409_[0-9a-f]{8}$')
            receipt_path = os.path.join(first['run_dir'], first['run_receipt']['path'])
            self.assertTrue(os.path.isfile(receipt_path))
            self.assertEqual(pipeline.sha256_file(receipt_path), first['run_receipt']['sha256'])
            self.assertEqual(first['session_id'], first['run_id'])
            self.assertEqual(first['thread_id'], first['run_id'])
            status = pipeline.get_run_status(263409, first['run_id'], root)
            self.assertTrue(status['ok'])
            self.assertEqual(status['status'], 'INITIALIZED')
            self.assertEqual(status['session_id'], first['run_id'])
            self.assertEqual(status['thread_id'], first['run_id'])

    def test_apply_run_never_falls_back_when_canonical_plan_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, '过程文件')
            initialized = pipeline.init_run(263409, root)
            with mock.patch.object(tfs, 'load_config') as load_config, \
                    mock.patch.object(redis_client, 'publish_plan') as publish:
                response = pipeline.apply_run(
                    263409, initialized['run_id'], False, 'config.json', process_root=root)
            status = pipeline.get_run_status(263409, initialized['run_id'], root)
        self.assertFalse(response['ok'])
        self.assertEqual(response['error_code'], 'RUN_NOT_READY')
        self.assertEqual(status['status'], 'INITIALIZED')
        load_config.assert_not_called()
        publish.assert_not_called()

    def test_failed_run_rejects_repeat_without_reentering_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, '过程文件')
            initialized, _, _ = self.write_run_bound_plan(
                directory, work_item_id=263409)
            with mock.patch.object(pipeline, 'apply_plan', return_value={
                    'ok': False, 'error': '模拟执行失败', 'actions': [],
                    'redis': {'in_scope': False}}) as apply_plan:
                first = pipeline.apply_run(
                    263409, initialized['run_id'], True, 'config.json', process_root=root)
                repeated = pipeline.apply_run(
                    263409, initialized['run_id'], True, 'config.json', process_root=root)
            status = pipeline.get_run_status(263409, initialized['run_id'], root)
        self.assertFalse(first['ok'])
        self.assertEqual(status['status'], 'FAILED')
        self.assertFalse(repeated['ok'])
        self.assertEqual(repeated['error_code'], 'RUN_TERMINAL_REQUIRES_NEW_RUN')
        self.assertTrue(repeated['requires_new_run'])
        apply_plan.assert_called_once()

    def test_concurrent_apply_lock_rejects_duplicate_without_pipeline_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, '过程文件')
            initialized, _, _ = self.write_run_bound_plan(
                directory, work_item_id=263409)
            os.mkdir(os.path.join(initialized['run_dir'], '.pipeline-apply-lock'))
            with mock.patch.object(pipeline, 'apply_plan') as apply_plan:
                response = pipeline.apply_run(
                    263409, initialized['run_id'], True, 'config.json', process_root=root)
        self.assertFalse(response['ok'])
        self.assertEqual(response['error_code'], 'RUN_ALREADY_IN_PROGRESS')
        apply_plan.assert_not_called()

    def test_completed_execute_retry_returns_idempotently_without_new_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, '过程文件')
            initialized, _, _ = self.write_run_bound_plan(
                directory, work_item_id=263409)
            with mock.patch.object(pipeline, 'apply_plan', return_value={
                    'ok': True, 'applied': True, 'run_mode': 'execute',
                    'actions': [], 'redis': {'in_scope': True}}) as apply_plan:
                first = pipeline.apply_run(
                    263409, initialized['run_id'], True, 'config.json', process_root=root)
                repeated = pipeline.apply_run(
                    263409, initialized['run_id'], True, 'config.json', process_root=root)
        self.assertTrue(first['ok'])
        self.assertTrue(repeated['ok'])
        self.assertTrue(repeated['idempotent'])
        self.assertEqual(repeated['run_mode'], 'already-completed')
        self.assertEqual(repeated['actions'], [])
        apply_plan.assert_called_once()

    def test_status_redis_projection_never_returns_another_run(self):
        current = 'run_20260814_120000_263409_deadbeef'
        other = {'run_id': 'run_20260813_120000_263409_cafebabe', 'verdict': 'AUTO-ANA'}
        self.assertIsNone(pipeline.redis_status_projection_for_run(current, other))
        matching = {'run_id': current.encode('utf-8'), 'verdict': 'MANUAL-REVIEW'}
        self.assertIs(pipeline.redis_status_projection_for_run(current, matching), matching)

    def test_apply_run_propagates_one_identity_and_records_state_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, '过程文件')
            initialized, _, _ = self.write_run_bound_plan(directory, work_item_id=263409)
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={
                        'ok': True, 'work_item': {'id': 263409, 'title': '测试', 'tags': []}}), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_run(
                    263409, initialized['run_id'], False, 'config.json', process_root=root)
            status = pipeline.get_run_status(263409, initialized['run_id'], root)
        self.assertTrue(response['ok'])
        self.assertEqual(response['run_id'], initialized['run_id'])
        self.assertEqual(status['session_id'], initialized['run_id'])
        self.assertEqual(status['thread_id'], initialized['run_id'])
        self.assertEqual(status['status'], 'COMPLETED')
        self.assertEqual([entry['status'] for entry in status['history']], [
            'INITIALIZED', 'ANALYZING', 'FROZEN', 'APPLYING', 'COMPLETED'])

    def test_263409_nonblocking_validation_warnings_continue_apply_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, '过程文件')
            initialized, plan, plan_path = self.write_run_bound_plan(
                directory, work_item_id=263409, verdict='NEED-REVIEW')
            self.add_nonblocking_warning_inputs(plan)
            with open(plan_path, 'w', encoding='utf-8') as output:
                json.dump(plan, output, ensure_ascii=False, indent=2)

            checked = pipeline.validate_plan(plan, plan_path)
            self.assertTrue(checked['ok'], checked['errors'])
            self.assertEqual(checked['validation']['decision'], 'PASS')
            self.assertEqual({warning['code'] for warning in checked['warnings']}, {
                'ATTACHMENT_CHAIN_NORMALIZED',
                'SOURCE_FINDING_MISSING',
                'REQUIREMENTS_PROBE_NOT_RECORDED',
            })
            self.assertEqual(checked['normalizations'][0]['after'], 'builtin-fallback')

            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={
                        'ok': True,
                        'work_item': {'id': 263409, 'title': '测试', 'tags': []}}), \
                    mock.patch.object(tfs, 'upload_attachment', return_value={'ok': True}), \
                    mock.patch.object(tfs, 'add_tag', return_value={'ok': True}), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}) as publish, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}) as record:
                response = pipeline.apply_run(
                    263409, initialized['run_id'], False, 'config.json', process_root=root)
            status = pipeline.get_run_status(263409, initialized['run_id'], root)

        self.assertTrue(response['ok'])
        self.assertEqual(response['verdict'], 'NEED-REVIEW')
        self.assertEqual(response['validation']['decision'], 'PASS')
        self.assertEqual(len(response['validation']['warnings']), 3)
        self.assertEqual(status['warning_count'], 3)
        self.assertEqual(status['status'], 'COMPLETED')
        self.assertEqual(record.call_args.args[7]['warning_count'], 3)
        publish.assert_called_once()

    def test_attachment_chain_bad_types_are_warnings_not_exceptions(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base, plan_path = self.write_run_bound_plan(
                directory, work_item_id=263409, verdict='NEED-REVIEW')
            self.add_nonblocking_warning_inputs(base)
            for chain in (['unknown', 'chain'], {'tool': 'builtin'}, 'shell-command'):
                plan = copy.deepcopy(base)
                plan['attachments']['parsed'][0]['converter_chain'] = chain
                checked = pipeline.validate_plan(plan, plan_path, check_files=False)
                self.assertTrue(checked['ok'], checked['errors'])
                self.assertIn('ATTACHMENT_CHAIN_INVALID', {
                    warning['code'] for warning in checked['warnings']})

    def test_manual_terminal_missing_recorded_probe_is_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manual, plan_path = self.write_run_bound_plan(
                directory, work_item_id=263409, verdict='NEED-REVIEW')
            self.add_nonblocking_warning_inputs(manual)
            manual['kb']['findings'] = [{
                'entity': '麻醉规则源码', 'state': '已证实',
                'source_tool': 'search_source', 'source_type': 'code',
                'conclusion': '已定位当前校验逻辑',
                'evidence': 'Service.java#validate',
                'boundary': '不证明产品待确认的目标值',
            }]
            manual['kb']['tools_used'] = ['query']
            checked = pipeline.validate_plan(manual, plan_path, check_files=False)
            self.assertTrue(checked['ok'], checked['errors'])
            self.assertIn('TOOL_USAGE_NOT_RECORDED', {
                warning['code'] for warning in checked['warnings']})

    def test_run_bound_source_change_ends_run_without_error_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, '过程文件')
            initialized, _, _ = self.write_run_bound_plan(
                directory, work_item_id=263409, verdict='NEED-REVIEW')
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={
                        'ok': False, 'error': '工作项版本已变化：计划 rev 5，当前 rev 6'}), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}) as publish, \
                    mock.patch.object(redis_client, 'publish_failure') as publish_failure, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_run(
                    263409, initialized['run_id'], False, 'config.json', process_root=root)
            status = pipeline.get_run_status(263409, initialized['run_id'], root)
        self.assertTrue(response['ok'])
        self.assertEqual(response['error_code'], 'SOURCE_CHANGED')
        self.assertTrue(response['requires_new_run'])
        self.assertEqual(response['validation']['decision'], 'ANALYSIS_ONLY')
        self.assertTrue(response['validation']['blocking_errors']['source'])
        self.assertEqual(status['status'], 'FAILED')
        publish.assert_called_once()
        publish_failure.assert_not_called()

    def test_apply_run_rejects_forged_complete_v2_without_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, '过程文件')
            initialized = pipeline.init_run(263409, root)
            run_id = initialized['run_id']
            plan = {
                'version': 2, 'run_id': run_id, 'skill': 'auto-req-analysis',
                'work_item_id': 263409, 'expected_rev': 5, 'expected_state': '已建议',
                'verdict': 'SKIP-ANALYSIS', 'tags': [], 'state_to': None,
                'rules_source': {'qc': 'pre-qc-v1'}, 'artifacts': [],
                'skip_reason': '历史计划伪装为本轮输出。',
            }
            plan_path = os.path.join(initialized['run_dir'], f'执行计划_263409_{run_id}.json')
            with open(plan_path, 'w', encoding='utf-8') as output:
                json.dump(plan, output, ensure_ascii=False, indent=2)
            with mock.patch.object(tfs, 'load_config') as load_config, \
                    mock.patch.object(redis_client, 'publish_plan') as publish:
                response = pipeline.apply_run(
                    263409, run_id, False, 'config.json', process_root=root)
        self.assertFalse(response['ok'])
        self.assertEqual(response['error_code'], 'LEGACY_PLAN_REQUIRES_EXPLICIT_REPLAY')
        load_config.assert_not_called()
        publish.assert_not_called()

    def test_run_bound_qc_and_skip_require_receipt_and_canonical_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            for verdict in ('SKIP-ANALYSIS', 'NEED-REVIEW'):
                _, plan, plan_path = self.write_run_bound_plan(
                    directory, work_item_id=263409, verdict=verdict)
                checked = pipeline.validate_plan(plan, plan_path)
                self.assertTrue(checked['ok'], checked['errors'])
                missing = copy.deepcopy(plan)
                missing.pop('run_receipt')
                checked = pipeline.validate_plan(missing, plan_path)
                self.assertFalse(checked['ok'])
                self.assertIn('缺少字段 run_receipt', checked['errors'])

    def test_run_bound_plan_digest_is_frozen_per_run(self):
        with tempfile.TemporaryDirectory() as directory:
            _, thin, plan_path = self.write_run_bound_plan(directory, work_item_id=263409)
            expanded, meta, errors = pipeline.materialize_run_bound(thin, plan_path)
            self.assertEqual(errors, [])
            pipeline._ensure_frozen_plan(meta, expanded)
            pipeline._ensure_frozen_plan(meta, expanded)
            thin['skip_reason'] = '不同的终局摘要。'
            with open(plan_path, 'w', encoding='utf-8') as output:
                json.dump(thin, output, ensure_ascii=False, indent=2)
            expanded, changed_meta, errors = pipeline.materialize_run_bound(thin, plan_path)
            self.assertEqual(errors, [])
            with self.assertRaisesRegex(ValueError, 'RUN_ID_ALREADY_FINALIZED'):
                pipeline._ensure_frozen_plan(changed_meta, expanded)

    def test_apply_legacy_is_explicit_isolated_and_does_not_publish_redis(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={
                        'ok': True, 'work_item': {'tags': []}}), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section',
                                      return_value={'ok': True}), \
                    mock.patch.object(tfs, 'upload_attachment', return_value={'ok': True}), \
                    mock.patch.object(tfs, 'add_tag', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'apply_field_flow', return_value=[]), \
                    mock.patch.object(redis_client, 'publish_plan') as publish, \
                    mock.patch.object(redis_client, 'publish_failure') as publish_failure, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'legacy.json'}) as record:
                response = pipeline.apply_legacy_plan(
                    plan, plan_path, False, 'config.json')
        self.assertTrue(response['ok'])
        self.assertEqual(response['run_mode'], 'legacy-replay')
        self.assertFalse(response['redis']['in_scope'])
        self.assertEqual(record.call_args.kwargs['audit_group'], 'legacy-runs')
        publish.assert_not_called()
        publish_failure.assert_not_called()

    def test_init_run_is_unique_under_concurrency_and_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, '过程文件')
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: pipeline.init_run(263409, root), range(16)))
            self.assertEqual(len({item['run_id'] for item in results}), 16)

            with mock.patch.object(tfs, 'beijing_timestamp', return_value='20260814_120000'), \
                    mock.patch.object(pipeline.secrets, 'token_hex',
                                      side_effect=['deadbeef', 'deadbeef', 'cafebabe']):
                first = pipeline.init_run(9, root)
                second = pipeline.init_run(9, root)
            self.assertTrue(first['run_id'].endswith('_deadbeef'))
            self.assertTrue(second['run_id'].endswith('_cafebabe'))

    def test_analysis_ref_requires_receipt_identity_and_matching_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_ref_plan(directory)
            valid = pipeline.validate_plan(plan, plan_path)
            self.assertTrue(valid['ok'], valid['errors'])

            missing = json.loads(json.dumps(plan))
            missing.pop('run_receipt')
            checked = pipeline.validate_plan(missing, plan_path)
            self.assertFalse(checked['ok'])
            self.assertIn('缺少字段 run_receipt', checked['errors'])

            wrong_path = os.path.join(directory, 'plan.json')
            checked = pipeline.validate_plan(plan, wrong_path)
            self.assertFalse(checked['ok'])
            self.assertTrue(any('SOURCE_RUN_DIRECTORY_MISMATCH' in error
                                for error in checked['errors']))

    def test_analysis_ref_reports_all_immediately_determinable_shape_errors(self):
        malformed = {
            'version': 2,
            'plan_profile': pipeline.ANALYSIS_REF_PROFILE,
            'work_item_id': 263409,
            'run_id': 'bad',
            'expected_rev': 'x',
            'expected_state': '',
            'analysis_result': {},
        }
        checked = pipeline.validate_plan(malformed, '/tmp/plan.json')
        self.assertFalse(checked['ok'])
        joined = '\n'.join(checked['errors'])
        self.assertIn('缺少字段 run_receipt', joined)
        self.assertIn('run_id 必须为', joined)
        self.assertIn('expected_rev 必须是正整数', joined)
        self.assertIn('expected_state 必须是非空字符串', joined)

    def test_analysis_ref_collects_independent_reference_errors_in_one_pass(self):
        run_id = 'run_20260814_120000_263409_deadbeef'
        malformed = {
            'version': 2,
            'plan_profile': pipeline.ANALYSIS_REF_PROFILE,
            'work_item_id': 263409,
            'run_id': run_id,
            'expected_rev': 'x',
            'expected_state': '',
            'analysis_result': {},
        }
        with tempfile.TemporaryDirectory() as directory:
            run_dir = os.path.join(directory, '263409', run_id)
            os.makedirs(run_dir)
            plan_path = os.path.join(run_dir, f'执行计划_263409_{run_id}.json')
            checked = pipeline.validate_plan(malformed, plan_path)
        self.assertFalse(checked['ok'])
        joined = '\n'.join(checked['errors'])
        self.assertIn('缺少字段 run_receipt', joined)
        self.assertIn('expected_rev 必须是正整数', joined)
        self.assertIn('expected_state 必须是非空字符串', joined)
        self.assertIn('analysis_result 必须精确包含 path、sha256', joined)

    def test_analysis_ref_receipt_cannot_be_replayed_from_copied_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_ref_plan(directory)
            copied = os.path.join(directory, 'copied', '1', plan['run_id'])
            os.makedirs(os.path.dirname(copied), exist_ok=True)
            shutil.copytree(os.path.dirname(plan_path), copied)
            copied_plan = os.path.join(copied, os.path.basename(plan_path))
            checked = pipeline.validate_plan(plan, copied_plan)
        self.assertFalse(checked['ok'])
        self.assertTrue(any('运行回执未绑定当前规范目录' in error
                            for error in checked['errors']))

    def test_analysis_ref_freeze_rejects_changed_snapshot_but_allows_same_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, snapshot_path = self.write_analysis_ref_plan(directory)
            expanded, meta, errors = pipeline.materialize_analysis_ref(plan, plan_path)
            self.assertEqual(errors, [])
            pipeline._ensure_frozen_analysis(meta, expanded)
            pipeline._ensure_frozen_analysis(meta, expanded)

            with open(snapshot_path, 'r', encoding='utf-8') as source:
                snapshot = json.load(source)
            snapshot['report']['analysis_description']['categories'][0]['items'][0]['content'] = '不同结论。'
            with open(snapshot_path, 'w', encoding='utf-8') as output:
                json.dump(snapshot, output, ensure_ascii=False, indent=2)
            plan['analysis_result']['sha256'] = pipeline.sha256_file(snapshot_path)
            expanded, changed_meta, errors = pipeline.materialize_analysis_ref(plan, plan_path)
            self.assertEqual(errors, [])
            with self.assertRaisesRegex(ValueError, 'RUN_ID_ALREADY_FINALIZED'):
                pipeline._ensure_frozen_analysis(changed_meta, expanded)

    def test_analysis_ref_requires_generated_utc_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, snapshot_path = self.write_analysis_ref_plan(directory)
            with open(snapshot_path, 'r', encoding='utf-8') as source:
                snapshot = json.load(source)
            snapshot.pop('generated_at_utc')
            with open(snapshot_path, 'w', encoding='utf-8') as output:
                json.dump(snapshot, output, ensure_ascii=False, indent=2)
            plan['analysis_result']['sha256'] = pipeline.sha256_file(snapshot_path)
            checked = pipeline.validate_plan(plan, plan_path)
        self.assertFalse(checked['ok'])
        self.assertTrue(any('generated_at_utc' in error for error in checked['errors']))

    def test_analysis_ref_materialize_failure_publishes_redis_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_ref_plan(directory)
            plan.pop('analysis_result')
            with open(plan_path, 'w', encoding='utf-8') as output:
                json.dump(plan, output, ensure_ascii=False, indent=2)
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(redis_client, 'publish_failure',
                                      return_value={'ok': True, 'key': 'k', 'fields': 7}) as publish_failure, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(plan, plan_path, False, 'config.json')
        self.assertFalse(response['ok'])
        self.assertEqual(response['error_code'], 'PLAN_VALIDATION_FAILED')
        self.assertIn('缺少字段 analysis_result', response['errors'])
        self.assertTrue(response['redis']['in_scope'])
        publish_failure.assert_called_once()
        self.assertEqual(publish_failure.call_args.args[3], 'C')

    def test_materialized_plan_content_validation_failure_publishes_redis_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, snapshot_path = self.write_analysis_ref_plan(directory)
            with open(snapshot_path, 'r', encoding='utf-8') as source:
                snapshot = json.load(source)
            snapshot['general_rule_coverage']['acceptance']['status'] = 'PENDING'
            with open(snapshot_path, 'w', encoding='utf-8') as output:
                json.dump(snapshot, output, ensure_ascii=False, indent=2)
            plan['analysis_result']['sha256'] = pipeline.sha256_file(snapshot_path)
            with open(plan_path, 'w', encoding='utf-8') as output:
                json.dump(plan, output, ensure_ascii=False, indent=2)
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(redis_client, 'publish_failure',
                                      return_value={'ok': True, 'key': 'k', 'fields': 7}) as publish_failure, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(plan, plan_path, False, 'config.json')
        self.assertFalse(response['ok'])
        self.assertTrue(any('general_rule_coverage' in error for error in response['errors']))
        self.assertTrue(response['redis']['in_scope'])
        publish_failure.assert_called_once()
        self.assertEqual(publish_failure.call_args.args[3], 'C')

    def test_analysis_ref_execution_block_publishes_analysis_only_without_tfs_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_ref_plan(directory)
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': False, 'error': 'not ready'}), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}) as publish, \
                    mock.patch.object(redis_client, 'publish_failure') as publish_failure, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section') as write_description, \
                    mock.patch.object(tfs, 'upload_attachment') as upload:
                response = pipeline.apply_plan(plan, plan_path, True, 'config.json')
        self.assertTrue(response['ok'])
        self.assertFalse(response['applied'])
        self.assertEqual(response['run_mode'], 'analysis-only')
        self.assertEqual(response['actions'], [])
        self.assertEqual(response['validation']['decision'], 'ANALYSIS_ONLY')
        self.assertTrue(response['validation']['blocking_errors']['execution'])
        self.assertEqual(publish.call_args.args[1], 'analysis-only')
        publish_failure.assert_not_called()
        write_description.assert_not_called()
        upload.assert_not_called()

    def test_analysis_ref_source_change_is_terminal_and_requires_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_ref_plan(directory)
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={
                        'ok': False, 'error': '工作项版本已变化：计划为 rev 5，当前为 rev 6'}), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                    mock.patch.object(redis_client, 'publish_failure') as publish_failure, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(plan, plan_path, False, 'config.json')
        self.assertTrue(response['ok'])
        self.assertFalse(response['applied'])
        self.assertEqual(response['error_code'], 'SOURCE_CHANGED')
        self.assertFalse(response['retryable'])
        self.assertTrue(response['requires_new_run'])
        publish_failure.assert_not_called()

    def test_preflight_allows_only_same_frozen_run_to_resume_its_own_revision_changes(self):
        run_id = 'run_20260814_120000_1_deadbeef'
        plan = {
            'work_item_id': 1, 'run_id': run_id, 'expected_rev': 5,
            'expected_state': '已建议', 'verdict': 'AUTO-ANA',
            'rules_source': {'qc': 'pre-qc-v1', 'analysis': 'evidence-loop-v2'},
        }
        report = f'需求分析报告_1_{run_id}.md'
        raw = {'id': 1, 'rev': 9, 'fields': {
            tfs.F_DESCRIPTION: f'<div>【分析者描述】</div><!-- auto-req-run:{run_id} -->',
        }, 'relations': [{
            'rel': 'AttachedFile',
            'url': 'http://example/attachment?fileName=' + urllib.parse.quote(report),
        }]}
        item = {'workItemType': '需求', 'rev': 9, 'state': '已分析', 'tags': []}
        checkpoint = {'last_rev': 9, 'last_state': '已分析'}
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'map_workitem', return_value=item):
            resumed = pipeline.preflight({}, plan, resume_checkpoint=checkpoint)
            strict = pipeline.preflight({}, plan)
        self.assertTrue(resumed['ok'])
        self.assertTrue(resumed['resumed'])
        self.assertFalse(strict['ok'])

        item['state'] = '关闭'
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'map_workitem', return_value=item):
            unexpected = pipeline.preflight({}, plan, resume_checkpoint=checkpoint)
        self.assertFalse(unexpected['ok'])

        item['state'] = '已分析'
        item['rev'] = 10
        with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'map_workitem', return_value=item):
            externally_changed = pipeline.preflight({}, plan, resume_checkpoint=checkpoint)
        self.assertFalse(externally_changed['ok'])

    def test_same_frozen_analysis_ref_supports_dry_run_execute_and_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_ref_plan(directory)
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={
                        'ok': True, 'work_item': {'id': 1, 'title': '测试需求',
                                                  'rev': 5, 'state': '已建议', 'tags': []}}), \
                    mock.patch.object(tfs, 'fetch_raw', return_value={'id': 1}), \
                    mock.patch.object(tfs, 'map_workitem', return_value={
                        'rev': 5, 'state': '已建议'}), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section',
                                      return_value={'ok': True, 'noop': True}), \
                    mock.patch.object(tfs, 'upload_attachment',
                                      return_value={'ok': True, 'noop': True}), \
                    mock.patch.object(tfs, 'cleanup_analysis_attachments',
                                      return_value={'ok': True, 'noop': True}), \
                    mock.patch.object(tfs, 'add_tag', return_value={'ok': True, 'noop': True}), \
                    mock.patch.object(pipeline, 'apply_field_flow', return_value=[]), \
                    mock.patch.object(pipeline, '_update_execution_checkpoint', return_value={}), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                    mock.patch.object(redis_client, 'publish_failure') as publish_failure, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                dry_run = pipeline.apply_plan(plan, plan_path, False, 'config.json')
                execute = pipeline.apply_plan(plan, plan_path, True, 'config.json')
                repeated = pipeline.apply_plan(plan, plan_path, True, 'config.json')
                report = os.path.join(os.path.dirname(plan_path),
                                      f'需求分析报告_1_{plan["run_id"]}.md')
                self.assertTrue(os.path.isfile(report))
        self.assertTrue(dry_run['ok'])
        self.assertFalse(dry_run['applied'])
        self.assertTrue(execute['ok'])
        self.assertTrue(execute['applied'])
        self.assertTrue(repeated['ok'])
        self.assertTrue(repeated['applied'])
        publish_failure.assert_not_called()

    def test_noop_before_external_change_stays_source_changed_analysis_only(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, snapshot_path = self.write_analysis_ref_plan(directory)
            with open(snapshot_path, 'r', encoding='utf-8') as source:
                snapshot = json.load(source)
            snapshot['verdict'] = 'MANUAL-REVIEW'
            snapshot.pop('auto_scopes', None)
            with open(snapshot_path, 'w', encoding='utf-8') as output:
                json.dump(snapshot, output, ensure_ascii=False, indent=2)
            plan['analysis_result']['sha256'] = pipeline.sha256_file(snapshot_path)

            observed = iter([
                {'rev': 5, 'state': '已建议'},
                {'rev': 6, 'state': '已建议'},
            ])
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={
                        'ok': True, 'work_item': {'id': 1, 'title': '测试需求',
                                                  'rev': 5, 'state': '已建议', 'tags': []}}), \
                    mock.patch.object(tfs, 'fetch_raw', return_value={'id': 1}), \
                    mock.patch.object(tfs, 'map_workitem', side_effect=lambda _raw: next(observed)), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section',
                                      return_value={'ok': True, 'noop': True}), \
                    mock.patch.object(tfs, 'upload_attachment') as upload, \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                    mock.patch.object(redis_client, 'publish_failure') as publish_failure, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(plan, plan_path, True, 'config.json')

        self.assertTrue(response['ok'])
        self.assertFalse(response['applied'])
        self.assertEqual(response['run_mode'], 'analysis-only')
        self.assertEqual(response['error_code'], 'SOURCE_CHANGED')
        self.assertTrue(response['requires_new_run'])
        upload.assert_not_called()
        publish_failure.assert_not_called()

    def test_execution_checkpoint_binds_resume_to_last_executor_observed_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            thin, plan_path, _ = self.write_analysis_ref_plan(directory)
            plan, meta, errors = pipeline.materialize_analysis_ref(thin, plan_path)
            self.assertEqual(errors, [])
            raw = {'id': 1, 'rev': 7, 'fields': {}}
            item = {'workItemType': '需求', 'rev': 7, 'state': '已建议', 'tags': []}
            with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                    mock.patch.object(tfs, 'map_workitem', return_value=item):
                pipeline._update_execution_checkpoint(
                    meta, plan, {'collection': 'C'}, ['write-detail-analysis'])
                checkpoint = pipeline._load_execution_checkpoint(meta, plan)
                resumed = pipeline.preflight({}, plan, resume_checkpoint=checkpoint)
            self.assertTrue(resumed['ok'])
            self.assertTrue(resumed['resumed'])

            item['rev'] = 8
            with mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                    mock.patch.object(tfs, 'map_workitem', return_value=item):
                changed = pipeline.preflight({}, plan, resume_checkpoint=checkpoint)
            self.assertFalse(changed['ok'])

    def test_execute_checkpoint_allows_repeat_but_rejects_later_external_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            thin, plan_path, snapshot_path = self.write_analysis_ref_plan(directory)
            with open(snapshot_path, 'r', encoding='utf-8') as source:
                snapshot = json.load(source)
            snapshot['verdict'] = 'MANUAL-REVIEW'
            snapshot.pop('auto_scopes', None)
            with open(snapshot_path, 'w', encoding='utf-8') as output:
                json.dump(snapshot, output, ensure_ascii=False, indent=2)
            thin['analysis_result']['sha256'] = pipeline.sha256_file(snapshot_path)

            live = {'rev': 5, 'state': '已建议', 'tags': []}
            noop = {'value': False}

            def fetch(_client, _wid):
                return {'id': 1, 'fields': {}, 'relations': []}

            def mapped(_raw):
                return {
                    'id': 1, 'title': '测试需求', 'workItemType': '需求',
                    'rev': live['rev'], 'state': live['state'], 'tags': list(live['tags']),
                }

            def action(*_args, **_kwargs):
                if noop['value']:
                    return {'ok': True, 'noop': True}
                live['rev'] += 1
                return {'ok': True}

            def add_tag(*_args, **_kwargs):
                response = action()
                if not response.get('noop'):
                    live['tags'] = ['PM-AI-MANUAL-REVIEW']
                return response

            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(tfs, 'fetch_raw', side_effect=fetch), \
                    mock.patch.object(tfs, 'map_workitem', side_effect=mapped), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section', side_effect=action), \
                    mock.patch.object(tfs, 'upload_attachment', side_effect=action), \
                    mock.patch.object(tfs, 'cleanup_analysis_attachments', side_effect=action), \
                    mock.patch.object(tfs, 'add_tag', side_effect=add_tag), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                    mock.patch.object(redis_client, 'publish_failure') as publish_failure, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                first = pipeline.apply_plan(thin, plan_path, True, 'config.json')
                self.assertTrue(first['ok'], first)
                self.assertEqual(live['rev'], 9)
                noop['value'] = True
                repeated = pipeline.apply_plan(thin, plan_path, True, 'config.json')
                self.assertTrue(repeated['ok'], repeated)
                self.assertEqual(live['rev'], 9)
                live['rev'] = 10
                external = pipeline.apply_plan(thin, plan_path, True, 'config.json')
        self.assertTrue(external['ok'])
        self.assertEqual(external['error_code'], 'SOURCE_CHANGED')
        self.assertTrue(external['requires_new_run'])
        publish_failure.assert_not_called()

    def test_execute_checkpoint_uses_patch_revision_without_stale_post_get(self):
        with tempfile.TemporaryDirectory() as directory:
            thin, plan_path, snapshot_path = self.write_analysis_ref_plan(directory)
            with open(snapshot_path, 'r', encoding='utf-8') as source:
                snapshot = json.load(source)
            snapshot['verdict'] = 'MANUAL-REVIEW'
            snapshot.pop('auto_scopes', None)
            with open(snapshot_path, 'w', encoding='utf-8') as output:
                json.dump(snapshot, output, ensure_ascii=False, indent=2)
            thin['analysis_result']['sha256'] = pipeline.sha256_file(snapshot_path)

            live = {'rev': 8, 'state': '已建议'}

            def mapped(_raw):
                return {'rev': live['rev'], 'state': live['state']}

            def write_description(*_args):
                # PATCH 响应是写后权威身份；revision 不要求业务动作恰好只增加 1。
                live['rev'] = 10
                return {'ok': True, 'post_rev': 10, 'post_state': '已建议'}

            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={
                        'ok': True, 'work_item': {'id': 1, 'title': '测试需求',
                                                  'rev': 8, 'state': '已建议', 'tags': []}}), \
                    mock.patch.object(tfs, 'fetch_raw', return_value={'id': 1}) as fetch, \
                    mock.patch.object(tfs, 'map_workitem', side_effect=mapped), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section',
                                      side_effect=write_description), \
                    mock.patch.object(tfs, 'upload_attachment',
                                      return_value={'ok': True, 'noop': True}), \
                    mock.patch.object(tfs, 'cleanup_analysis_attachments',
                                      return_value={'ok': True, 'noop': True}), \
                    mock.patch.object(tfs, 'add_tag',
                                      return_value={'ok': True, 'noop': True}), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                    mock.patch.object(redis_client, 'publish_failure') as publish_failure, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(thin, plan_path, True, 'config.json')
            materialized, meta, errors = pipeline.materialize_analysis_ref(thin, plan_path)
            self.assertEqual(errors, [])
            checkpoint = pipeline._load_execution_checkpoint(meta, materialized)

        self.assertTrue(response['ok'], response)
        self.assertEqual(checkpoint['last_rev'], 10)
        self.assertIn('write-detail-analysis', checkpoint['completed_actions'])
        # 四个动作各一次写前检查；写描述后不再额外 GET 验证 +1。
        self.assertEqual(fetch.call_count, 4)
        publish_failure.assert_not_called()

    def test_execute_checkpoint_treats_unchanged_post_rev_as_confirmed_noop(self):
        # 263409 回归：内容仅差 run 标记注释（TFS 落库即剥）时 PATCH 200 但不建 revision，
        # post_rev == 写前 rev 是 TFS 确认的等效 no-op，不得误判为写后确认失败。
        with tempfile.TemporaryDirectory() as directory:
            thin, plan_path, snapshot_path = self.write_analysis_ref_plan(directory)
            with open(snapshot_path, 'r', encoding='utf-8') as source:
                snapshot = json.load(source)
            snapshot['verdict'] = 'MANUAL-REVIEW'
            snapshot.pop('auto_scopes', None)
            with open(snapshot_path, 'w', encoding='utf-8') as output:
                json.dump(snapshot, output, ensure_ascii=False, indent=2)
            thin['analysis_result']['sha256'] = pipeline.sha256_file(snapshot_path)

            def mapped(_raw):
                return {'rev': 8, 'state': '已建议'}

            def write_description(*_args):
                # TFS 接受 PATCH 但未创建新 revision（无字段值变更）。
                return {'ok': True, 'post_rev': 8, 'post_state': '已建议'}

            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={
                        'ok': True, 'work_item': {'id': 1, 'title': '测试需求',
                                                  'rev': 8, 'state': '已建议', 'tags': []}}), \
                    mock.patch.object(tfs, 'fetch_raw', return_value={'id': 1}), \
                    mock.patch.object(tfs, 'map_workitem', side_effect=mapped), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section',
                                      side_effect=write_description), \
                    mock.patch.object(tfs, 'upload_attachment',
                                      return_value={'ok': True, 'noop': True}), \
                    mock.patch.object(tfs, 'cleanup_analysis_attachments',
                                      return_value={'ok': True, 'noop': True}), \
                    mock.patch.object(tfs, 'add_tag',
                                      return_value={'ok': True, 'noop': True}), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                    mock.patch.object(redis_client, 'publish_failure') as publish_failure, \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(thin, plan_path, True, 'config.json')
            materialized, meta, errors = pipeline.materialize_analysis_ref(thin, plan_path)
            self.assertEqual(errors, [])
            checkpoint = pipeline._load_execution_checkpoint(meta, materialized)

        self.assertTrue(response['ok'], response)
        self.assertNotIn('POST_WRITE_CONFIRMATION_FAILED', str(response))
        # 等效 no-op：检查点 rev 不推进，但动作计入已完成，续跑无需重做。
        self.assertEqual(checkpoint['last_rev'], 8)
        self.assertIn('write-detail-analysis', checkpoint['completed_actions'])
        publish_failure.assert_not_called()

    def test_unconfirmed_successful_write_is_visible_in_failure_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            thin, plan_path, snapshot_path = self.write_analysis_ref_plan(directory)
            with open(snapshot_path, 'r', encoding='utf-8') as source:
                snapshot = json.load(source)
            snapshot['verdict'] = 'MANUAL-REVIEW'
            snapshot.pop('auto_scopes', None)
            with open(snapshot_path, 'w', encoding='utf-8') as output:
                json.dump(snapshot, output, ensure_ascii=False, indent=2)
            thin['analysis_result']['sha256'] = pipeline.sha256_file(snapshot_path)

            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={
                        'ok': True, 'work_item': {'id': 1, 'title': '测试需求',
                                                  'rev': 8, 'state': '已建议', 'tags': []}}), \
                    mock.patch.object(tfs, 'fetch_raw', return_value={'id': 1}), \
                    mock.patch.object(tfs, 'map_workitem', return_value={
                        'rev': 8, 'state': '已建议'}), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section',
                                      return_value={'ok': True}), \
                    mock.patch.object(redis_client, 'publish_failure', return_value={'ok': True}), \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}) as record:
                response = pipeline.apply_plan(thin, plan_path, True, 'config.json')

        self.assertFalse(response['ok'])
        self.assertEqual(response['error_code'], 'POST_WRITE_CONFIRMATION_FAILED')
        self.assertEqual([entry['action'] for entry in response['actions']], [
            'write-detail-analysis'])
        self.assertTrue(record.call_args.args[7]['partial_write'])

    def test_263409_ambiguities_stay_in_one_qc_confirmation_batch(self):
        fixture = os.path.join(os.path.dirname(__file__), 'fixtures', '263409_ambiguity.json')
        with open(fixture, 'r', encoding='utf-8') as source:
            payload = json.load(source)
        self.assertEqual(payload['schema'], 'qc-ambiguity-regression-v1')
        self.assertNotIn('run_id', payload)
        ids = {item['id'] for item in payload['required_items']}
        self.assertEqual(ids, {
            'q-empty-value', 'q-maintenance-mode', 'q-surgery-rule', 'q-data-scope',
        })
        self.assertEqual(payload['expected_verdict'], 'NEED-REVIEW')
        self.assertIn('自动填充无或-', payload['forbidden_conclusions'])

    def test_v1_evidence_loop_plan_still_passes_without_evidence_acquisition(self):
        # 向后兼容：evidence-loop-v1 计划不要求 evidence_acquisition
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(
                directory, ['existing-ui-simple'], analysis_rule='evidence-loop-v1')
            self.assertNotIn('evidence_acquisition', plan)
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_v2_legacy_change_plan_filename_still_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, report_path = self.write_v2_analysis_plan(
                directory, ['existing-ui-simple'])
            legacy_name = f'变更方案_1_{plan["run_id"]}.md'
            legacy_path = os.path.join(directory, legacy_name)
            os.replace(report_path, legacy_path)
            plan['artifacts'][0]['path'] = legacy_name
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_v2_complete_acquisition_plan_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(directory, ['existing-ui-simple'])
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_v2_requires_implementation_impacts_and_business_rule_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(
                directory, ['existing-ui-simple'])
            plan.pop('implementation_impacts')
            plan.pop('business_rule_coverage')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('implementation_impacts' in error for error in result['errors']))
            self.assertTrue(any('business_rule_coverage' in error for error in result['errors']))

    def test_v2_requires_general_seven_dimension_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(
                directory, ['existing-ui-simple'])
            plan.pop('general_rule_coverage')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('general_rule_coverage' in error for error in result['errors']))

    def test_v2_general_scope_semantics_and_acceptance_require_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(
                directory, ['existing-ui-simple'])
            plan['general_rule_coverage']['scope'] = {
                'status': 'NOT_APPLICABLE', 'source': 'not-applicable',
                'basis': '错误地认为范围不适用。',
            }
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any(
                'general_rule_coverage.scope 不允许 NOT_APPLICABLE' in error
                for error in result['errors']))

    def test_v2_general_dimensions_reject_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(
                directory, ['existing-ui-simple'])
            plan['general_rule_coverage']['workflow'] = {
                'status': 'DEFAULTED', 'source': 'presentation-default',
                'basis': '错误地默认沿用当前流程。',
            }
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any(
                '通用七面不得使用默认值' in error for error in result['errors']))

    def test_v2_business_rule_confirmation_requires_traceable_source(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(
                directory, ['existing-ui-simple'])
            plan['business_rule_coverage']['presentation'] = {
                'status': 'CONFIRMED', 'basis': '产品已确认展示方式。'}
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any(
                'business_rule_coverage.presentation.source' in error
                for error in result['errors']))

    def test_v2_data_write_requires_source_and_database_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(
                directory, ['existing-ui-simple'])
            plan['implementation_impacts'] = [
                'ui-presentation', 'field-assignment', 'api-contract', 'data-read-write',
            ]
            plan['business_rule_coverage']['empty_value'] = {
                'status': 'CONFIRMED', 'source': 'work-item',
                'basis': '工作项确认允许清空并保存为 null。'}
            plan['business_rule_coverage']['maintenance_granularity'] = {
                'status': 'CONFIRMED', 'source': 'work-item',
                'basis': '工作项确认按明细独立维护。'}
            plan['business_rule_coverage']['historical_data'] = {
                'status': 'CONFIRMED', 'source': 'work-item',
                'basis': '工作项确认历史数据不迁移。'}
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('kb.source_required=true' in error for error in result['errors']))
            self.assertTrue(any('kb.database_required=true' in error for error in result['errors']))

            plan['kb']['source_required'] = True
            plan['kb']['database_required'] = True
            plan['kb']['database_ready'] = True
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('必须实际调用 search_source' in error for error in result['errors']))
            self.assertTrue(any('必须实际调用数据库图谱工具' in error for error in result['errors']))

            plan['kb']['tools_used'].extend(['search_symbol', 'search_knowledge'])
            plan['kb']['findings'].extend([
                {
                    'entity': 'repo-a:src/SaveService.java#save', 'state': '已证实',
                    'source_tool': 'search_symbol', 'source_type': 'code',
                    'conclusion': '源码已确认字段进入保存链路',
                    'evidence': 'SaveService.save 方法体',
                    'boundary': '只证明受控仓库源码，不代表现场部署。',
                },
                {
                    'entity': 'HIS.dbo.TEST.ZTNR', 'state': '已证实',
                    'source_tool': 'search_knowledge', 'source_type': 'database',
                    'conclusion': '数据库图谱已确认目标字段与正式表',
                    'evidence': 'Table TEST / Column ZTNR',
                    'boundary': '只证明图谱快照，不代表现场数据质量。',
                },
            ])
            plan['evidence_acquisition']['db_knowledge'] = {
                'availability': 'READY', 'coverage_status': 'OUT_OF_SCOPE',
                'query_status': 'SKIPPED', 'queries': [], 'stop_reason': 'not_applicable',
            }
            skipped = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(skipped['ok'])
            self.assertTrue(any(
                'evidence_acquisition.db_knowledge 不得为 SKIPPED' in error
                for error in skipped['errors']))
            plan['evidence_acquisition']['db_knowledge'] = {
                'availability': 'READY', 'coverage_status': 'COMPLETE',
                'query_status': 'HIT',
                'queries': [{'terms': 'TEST.ZTNR', 'truncated': False}],
                'stop_reason': 'exhausted',
            }
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

    def test_v2_data_write_rejects_defaulted_empty_maintenance_and_history_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_v2_analysis_plan(
                directory, ['existing-ui-simple'])
            plan['implementation_impacts'] = ['data-read-write']
            plan['business_rule_coverage']['empty_value'] = {
                'status': 'DEFAULTED', 'source': 'presentation-default',
                'basis': '默认空值保存为空串。'}
            plan['business_rule_coverage']['maintenance_granularity'] = {
                'status': 'DEFAULTED', 'source': 'presentation-default',
                'basis': '默认按当前维护粒度。'}
            plan['business_rule_coverage']['historical_data'] = {
                'status': 'DEFAULTED', 'source': 'presentation-default',
                'basis': '默认不处理历史数据。'}
            plan['kb']['source_required'] = True
            plan['kb']['database_required'] = True
            plan['kb']['database_ready'] = True
            plan['kb']['tools_used'].extend(['search_symbol', 'search_knowledge'])
            plan['kb']['findings'].extend([
                {
                    'entity': 'repo-a:src/SaveService.java#save', 'state': '已证实',
                    'source_tool': 'search_symbol', 'source_type': 'code',
                    'conclusion': '源码已确认保存链路', 'evidence': 'SaveService.save',
                    'boundary': '不代表现场部署。',
                },
                {
                    'entity': 'HIS.dbo.TEST.ZTNR', 'state': '已证实',
                    'source_tool': 'search_knowledge', 'source_type': 'database',
                    'conclusion': '数据库已确认字段', 'evidence': 'TEST.ZTNR',
                    'boundary': '不代表现场数据质量。',
                },
            ])
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('empty_value' in error for error in result['errors']))
            self.assertTrue(any('maintenance_granularity' in error for error in result['errors']))
            self.assertTrue(any('historical_data' in error for error in result['errors']))

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
            response = pipeline.apply_plan(
                plan, '/tmp/skip-plan.json', True, 'config.json', legacy_replay=True)
        self.assertTrue(response['ok'])
        self.assertEqual(response['actions'], [])
        self.assertEqual(response['run_mode'], 'legacy-replay')
        self.assertFalse(response['redis']['in_scope'])
        publish.assert_not_called()
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

    def test_concise_v3_requires_single_menu_path(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            with open(artifact_path, 'r', encoding='utf-8') as f:
                original = f.read()

            without_menu = original.replace('- **菜单路径**：业务管理 > 测试功能\n', '')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(without_menu)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'concise-v3 分析者描述必须且只能包含一个非空“菜单路径”',
                result['errors'])

            no_menu_entry = original.replace(
                '- **菜单路径**：业务管理 > 测试功能',
                '- **菜单路径**：无菜单入口：由第三方接口回调触发')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(no_menu_entry)
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

            duplicate_menu = original.replace(
                '- **菜单路径**：业务管理 > 测试功能\n',
                '- **菜单路径**：业务管理 > 测试功能\n'
                '- **菜单路径**：重复菜单\n')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(duplicate_menu)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'concise-v3 分析者描述必须且只能包含一个非空“菜单路径”',
                result['errors'])

    def test_concise_v3_numbered_change_points_are_contiguous_and_structured(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            with open(artifact_path, 'r', encoding='utf-8') as f:
                original = f.read()
            numbered = original.replace(
                '- **界面优化方案**：已明确界面优化方案\n',
                '- **界面优化方案**：包含以下两项界面调整。\n'
                '1. **状态标识**：在患者卡片展示状态标识。\n'
                '2. **费用信息**：在患者卡片补充费用信息。\n')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(numbered)
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])
            rendered = pipeline.render_analysis_description_html(numbered, 'concise-v3')
            self.assertIn('<div>&nbsp;&nbsp;&nbsp;&nbsp;1、<strong>状态标识：</strong>在患者卡片展示状态标识。</div>', rendered)
            self.assertIn('<div>&nbsp;&nbsp;&nbsp;&nbsp;2、<strong>费用信息：</strong>在患者卡片补充费用信息。</div>', rendered)
            self.assertIn('&nbsp;&nbsp;&nbsp;&nbsp;1、', rendered)

            skipped = numbered.replace('2. **费用信息**', '3. **费用信息**')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(skipped)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'concise-v3 多条变更内容必须使用从 1 开始连续的 2–8 项有序编号',
                result['errors'])

            malformed = numbered.replace('1. **状态标识**：', '1. 状态标识：')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(malformed)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'concise-v3 编号变更项 1 必须按“1. **改动点**：内容”填写',
                result['errors'])

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
        plan['confirmation_policy'] = pipeline.SINGLE_CONFIRMATION_POLICY
        plan['knowledge_route'] = self.resolved_knowledge_route()
        plan['kb'] = {'ready': False, 'source_ready': False, 'source_required': False,
                      'database_ready': False, 'tools_used': [], 'findings': []}
        # 普通需求仍目标 1-3 个主题；复杂需求允许最多 5 个主题。
        decision_ids = ['q-scope', 'q-value-rule', 'q-business-rule',
                        'q-permission-exception', 'q-acceptance']
        plan['checklist']['items'] = [
            {'id': decision_id, 'question': f'问题 {index}?',
             'options': ['口径 A', '口径 B'], 'allow_other': True}
            for index, decision_id in enumerate(decision_ids, start=1)
        ]
        self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])
        plan['checklist']['items'].append(
            {'id': 'q-extra', 'question': '问题 6?',
             'options': ['口径 A', '口径 B'], 'allow_other': True})
        result = pipeline.validate_plan(plan, plan_path)
        self.assertFalse(result['ok'])
        self.assertIn('checklist.items 最多 5 项', result['errors'][0])
        plan['checklist']['items'] = [{
            'id': 'q1', 'question': '范围是什么?',
            'options': ['全部'], 'allow_other': True,
        }]
        result = pipeline.validate_plan(plan, plan_path)
        self.assertFalse(result['ok'])
        self.assertTrue(any('语义稳定 ID' in error for error in result['errors']))
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

    def test_current_non_skip_plan_requires_resolved_product_route(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            plan.pop('kb')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('必须含 kb 对象' in error for error in result['errors']))

            plan, plan_path, _ = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            plan.pop('knowledge_route')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('必须含 knowledge_route' in error for error in result['errors']))

            plan['knowledge_route'] = {
                'status': 'PROFILE_MISSING', 'area': 'OTHER', 'servers': {},
            }
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('不得声明就绪' in error for error in result['errors']))
            self.assertTrue(any('AUTO-ANA 要求 knowledge_route.status=RESOLVED' in error
                                for error in result['errors']))

    def test_current_source_verification_contract_and_auto_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

            plan['kb']['source_required'] = True
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('必须实际调用' in error for error in result['errors']))

            plan['kb']['tools_used'].append('search_symbol')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertIn(
                'kb.source_required=true 且源码 MCP 就绪时必须留下源码 finding',
                result['errors'])

            plan['kb']['findings'].append({
                'entity': 'repo-a:src/Service.java#save',
                'state': '候选',
                'source_tool': 'search_symbol',
                'source_type': 'code',
                'conclusion': '源码中存在保存入口候选',
                'evidence': 'repo-a:src/Service.java#save',
                'boundary': '尚未核验方法体，不能证明目标规则已经实现。',
            })
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('state=已证实 源码 finding' in error for error in result['errors']))

            plan['kb']['findings'][-1]['state'] = '已证实'
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

            plan['kb']['findings'][-1]['source_type'] = 'database'
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('源码 MCP finding' in error for error in result['errors']))

    def test_current_findings_require_human_evidence_fields_and_separate_history(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            plan['kb']['findings'][0].pop('conclusion')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('conclusion 必须为非空字符串' in error
                                for error in result['errors']))

            plan['kb']['findings'][0]['conclusion'] = '已定位现有测试入口'
            plan['kb']['findings'][0]['source_tool'] = 'get_work_item'
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('必须改写到 tfs_requirements.findings' in error
                                for error in result['errors']))

    def test_unresolved_route_forbids_cross_product_mcp_evidence(self):
        run_id = 'run_12345678'
        plan = {
            'version': 2, 'run_id': run_id, 'skill': 'auto-req-analysis',
            'work_item_id': 1, 'expected_rev': 5, 'expected_state': '活动',
            'verdict': 'NEED-REVIEW', 'tags': ['PM-AI-QC-NEED-REVIEW'],
            'state_to': None, 'rules_source': {'qc': 'pre-qc-v1'},
            'confirmation_policy': pipeline.SINGLE_CONFIRMATION_POLICY,
            'knowledge_route': {'status': 'AREA_UNMAPPED', 'area': 'UNKNOWN', 'servers': {}},
            'kb': {'ready': False, 'source_ready': False, 'source_required': False,
                   'database_ready': False, 'tools_used': ['query'],
                   'findings': [{'entity': 'other-product', 'state': '候选',
                                 'source_tool': 'query', 'source_type': 'code',
                                 'conclusion': '其它产品存在候选实现',
                                 'evidence': 'other-product',
                                 'boundary': '不得作为当前产品证据'}]},
            'checklist': {
                'work_item': '1 测试需求', 'verdict': 'NEED-REVIEW',
                'tag': 'PM-AI-QC-NEED-REVIEW', 'responsible': '产品',
                'generated_at_utc': '2026-08-10T00:00:00Z', 'next': '补充后重跑',
                'items': [{'id': 'q-scope', 'question': '产品范围是什么？',
                           'options': ['当前产品'], 'allow_other': True}],
            },
            'artifacts': [{'kind': 'qc-followup',
                           'filename': f'待补充信息_1_{run_id}.json'}],
        }
        result = pipeline.validate_plan(plan, os.path.join(tempfile.gettempdir(), 'plan.json'))
        self.assertFalse(result['ok'])
        self.assertTrue(any('不得声明就绪、调用工具或携带 finding' in error
                            for error in result['errors']))

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

    def test_single_confirmation_policy_rejects_analysis_business_gaps(self):
        gap = {
            'id': 'gap-amount-scope', 'topic': '金额范围', 'missing': '两类金额的取值范围',
            'impact': '无法确定校验边界', 'question': '两类金额是否均限定当前结算范围？',
            'options': ['均限定当前结算范围'], 'allow_other': True,
        }
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            plan.update({
                'verdict': 'MANUAL-REVIEW',
                'tags': ['PM-AI-MANUAL-REVIEW'],
                'state_to': None,
                'analysis_gaps': [gap],
            })
            plan.pop('auto_scopes')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('单次集中确认策略要求分析计划 analysis_gaps=[]' in error
                                for error in result['errors']))

            plan['analysis_gaps'] = []
            self.assertTrue(pipeline.validate_plan(plan, plan_path)['ok'])

            plan['kb'].pop('database_ready')
            plan['kb']['findings'][0].pop('source_type')
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('kb.database_ready' in error for error in result['errors']))
            self.assertTrue(any('source_type 必须为 code 或 database' in error
                                for error in result['errors']))
            plan['kb']['database_ready'] = False
            plan['kb']['findings'][0]['source_type'] = 'code'

            plan['confirmation_policy'] = 'unknown-policy'
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertTrue(any('confirmation_policy 仅允许' in error for error in result['errors']))

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
                response = pipeline.apply_plan(
                    plan, plan_path, False, 'config.json', legacy_replay=True)
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
            '- **菜单路径**：费用管理 > 费用录入',
            '### existing-ui-simple（既有功能简单界面优化）',
            '- **界面优化方案**：费用录入页保存后显示“A <script>alert(1)</script>”，不改变数据写入。',
            '## 四、范围—方案—验收追踪',
        ])
        rendered = pipeline.render_analysis_description_html(content, 'concise-v3')
        self.assertIn('<strong>菜单路径：</strong>费用管理 &gt; 费用录入', rendered)
        self.assertIn('<strong>界面优化方案：</strong>', rendered)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', rendered)
        self.assertNotIn('existing-ui-simple', rendered)
        self.assertNotIn('既有功能简单界面优化', rendered)
        self.assertNotIn('需求类别', rendered)
        self.assertNotIn('操作路径', rendered)
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
                response = pipeline.apply_plan(
                    plan, plan_path, False, 'config.json', legacy_replay=True)
        self.assertTrue(response['ok'])
        self.assertIn('<div>', replace_detail.call_args.args[2])
        write_field.assert_not_called()

    def test_apply_current_analysis_cleans_attachments_before_terminal_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight',
                                      return_value={'ok': True, 'work_item': {'tags': []}}), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section',
                                      return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'upload_attachment',
                                      return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'cleanup_analysis_attachments',
                                      return_value={'ok': True, 'id': 1, 'verified': False}) as cleanup, \
                    mock.patch.object(tfs, 'add_tag', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(pipeline, 'apply_field_flow', return_value=[]), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(
                    plan, plan_path, False, 'config.json', legacy_replay=True)
        self.assertTrue(response['ok'])
        self.assertEqual([action['action'] for action in response['actions']], [
            'write-detail-analysis', 'upload:change-plan', 'cleanup:analysis-artifacts',
            'add-tag:PM-AI-AUTO-ANA'])
        cleanup.assert_called_once_with(
            mock.ANY, 1, os.path.basename(artifact_path), True)

    def test_apply_current_analysis_upload_or_cleanup_failure_stops_terminal_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_current_analysis_plan(directory, ['existing-ui-simple'])
            common = [
                mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}),
                mock.patch.object(tfs, 'precheck', return_value={'ok': True}),
                mock.patch.object(pipeline, 'preflight',
                                  return_value={'ok': True, 'work_item': {'tags': []}}),
                mock.patch.object(tfs, 'replace_detail_analysis_section',
                                  return_value={'ok': True, 'id': 1}),
                mock.patch.object(tfs, 'record', return_value={'audit': 'error-audit.json'}),
            ]
            for patcher in common:
                patcher.start()
            try:
                with mock.patch.object(tfs, 'upload_attachment',
                                       return_value={'ok': False, 'error': 'upload failed'}), \
                        mock.patch.object(tfs, 'cleanup_analysis_attachments') as cleanup, \
                        mock.patch.object(tfs, 'add_tag') as add_tag, \
                        mock.patch.object(pipeline, 'apply_field_flow') as field_flow, \
                        mock.patch.object(redis_client, 'publish_plan') as publish, \
                        mock.patch.object(redis_client, 'publish_failure',
                                          return_value={'ok': True}) as pub_fail:
                    upload_failure = pipeline.apply_plan(
                        plan, plan_path, True, 'config.json', legacy_replay=True)
                self.assertFalse(upload_failure['ok'])
                cleanup.assert_not_called()
                add_tag.assert_not_called()
                field_flow.assert_not_called()
                # legacy-replay 成功和失败都不覆盖正常 Redis 通信结果。
                publish.assert_not_called()
                pub_fail.assert_not_called()
                self.assertIn('redis', upload_failure)

                with mock.patch.object(tfs, 'upload_attachment',
                                       return_value={'ok': True, 'id': 1}), \
                        mock.patch.object(tfs, 'cleanup_analysis_attachments',
                                          return_value={'ok': False, 'error': 'rev changed'}), \
                        mock.patch.object(tfs, 'add_tag') as add_tag, \
                        mock.patch.object(pipeline, 'apply_field_flow') as field_flow, \
                        mock.patch.object(redis_client, 'publish_plan') as publish, \
                        mock.patch.object(redis_client, 'publish_failure',
                                          return_value={'ok': True}) as pub_fail:
                    cleanup_failure = pipeline.apply_plan(
                        plan, plan_path, True, 'config.json', legacy_replay=True)
                self.assertFalse(cleanup_failure['ok'])
                self.assertEqual([action['action'] for action in cleanup_failure['actions']],
                                 ['write-detail-analysis', 'upload:change-plan'])
                add_tag.assert_not_called()
                field_flow.assert_not_called()
                publish.assert_not_called()
                pub_fail.assert_not_called()
                self.assertIn('redis', cleanup_failure)
            finally:
                for patcher in reversed(common):
                    patcher.stop()

    def test_apply_need_review_and_historical_manual_followup_do_not_cleanup(self):
        run_id = 'run_12345678'
        need_plan = {
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
            'artifacts': [{'kind': 'qc-followup',
                           'filename': f'待补充信息_1_{run_id}.json'}],
        }
        with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                mock.patch.object(pipeline, 'preflight',
                                  return_value={'ok': True, 'work_item': {'tags': []}}), \
                mock.patch.object(pipeline, 'upload_inline_artifact',
                                  return_value={'ok': True, 'id': 1}), \
                mock.patch.object(tfs, 'cleanup_analysis_attachments') as cleanup, \
                mock.patch.object(tfs, 'add_tag', return_value={'ok': True, 'id': 1}), \
                mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
            need_response = pipeline.apply_plan(
                need_plan, '/tmp/plan.json', False, 'config.json', legacy_replay=True)
        self.assertTrue(need_response['ok'])
        cleanup.assert_not_called()

        gap = {
            'id': 'gap-amount-scope', 'topic': '金额范围', 'missing': '金额取值范围',
            'impact': '无法确定边界', 'question': '是否限定当前结算范围？',
            'options': ['限定', '不限定'], 'allow_other': True,
        }
        with tempfile.TemporaryDirectory() as directory:
            historical, historical_path = self.make_manual_plan(directory, [gap])
            followup = f'待确认清单_1_{historical["run_id"]}.md'
            historical['artifacts'].append({'kind': 'manual-followup', 'path': followup})
            with open(os.path.join(directory, followup), 'w', encoding='utf-8') as output:
                output.write('\n'.join([
                    '# 需求分析待确认项',
                    f'<!-- auto-req-run:{historical["run_id"]} -->',
                    '## 需要确认的需求分析信息',
                    '### gap-amount-scope · 金额范围',
                    '<!-- analysis-gap:gap-amount-scope -->',
                    '- **缺失信息**：金额取值范围',
                    '- **对分析/验收的影响**：无法确定边界',
                    '- **需确认的问题**：是否限定当前结算范围？',
                    '- **候选口径**：限定',
                    '- **允许自由补充**：是',
                ]))
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight',
                                      return_value={'ok': True, 'work_item': {'tags': []}}), \
                    mock.patch.object(tfs, 'replace_detail_analysis_section',
                                      return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'upload_attachment',
                                      return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'cleanup_analysis_attachments') as cleanup, \
                    mock.patch.object(tfs, 'add_tag', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                historical_response = pipeline.apply_plan(
                    historical, historical_path, False, 'config.json', legacy_replay=True)
        self.assertTrue(historical_response['ok'])
        cleanup.assert_not_called()

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
                response = pipeline.apply_plan(
                    plan, plan_path, False, 'config.json', legacy_replay=True)
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
                response = pipeline.apply_plan(
                    plan, plan_path, False, 'config.json', legacy_replay=True)
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
                response = pipeline.apply_plan(
                    plan, plan_path, False, 'config.json', legacy_replay=True)
        self.assertFalse(response['ok'])
        self.assertEqual(response['audit'], 'error-audit.json')
        self.assertEqual(record.call_args.args[2], 'ERROR')
        self.assertEqual(record.call_args.args[7]['tfs_requirements'], plan['tfs_requirements'])

    def test_preflight_no_longer_blocks_downstream_passed_tag(self):
        # PM-AI-MANUAL-PASSED 不再硬挡；preflight 只校验类型/rev/状态，放行带该标签的工作项
        plan = {'work_item_id': 1, 'expected_rev': 5, 'expected_state': '已建议'}
        item = {'workItemType': '需求', 'rev': 5, 'state': '已建议',
                'tags': ['PM-AI-MANUAL-PASSED', 'PM-AI-AUTO-ANA']}
        with mock.patch.object(tfs, 'fetch_raw', return_value={'id': 1}), \
                mock.patch.object(tfs, 'map_workitem', return_value=item):
            gate = pipeline.preflight({'base_url': 'x'}, plan)
        self.assertTrue(gate['ok'])
        self.assertEqual(gate['work_item']['tags'], item['tags'])

    def test_apply_invalidates_downstream_passed_tag_on_non_skip_rerun(self):
        raw_with_leader = {'id': 1, 'rev': 5, 'fields': {
            'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
            'Demand.Expected.date': '2026-09-06T16:00:00Z',
            'Winning.Dev.Leader': 'zhang_dong(张栋) <WINNING\\zhang_dong>'}}
        iter_resp = {'ok': True, 'earliest': {'path': 'NETHIS5.5\\2026\\V6.0.2608.28',
                                              'finish': '2026-08-28T00:00:00Z'}}
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_analysis_plan(directory, ['existing-ui-simple'])
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight', return_value={
                        'ok': True, 'work_item': {'tags': ['PM-AI-MANUAL-PASSED']}}), \
                    mock.patch.object(tfs, 'remove_tag', return_value={'ok': True, 'id': 1}) as remove_tag, \
                    mock.patch.object(tfs, 'replace_detail_analysis_section', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'upload_attachment', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'add_tag', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'fetch_raw', return_value=raw_with_leader), \
                    mock.patch.object(tfs, 'list_iterations', return_value=iter_resp), \
                    mock.patch.object(tfs, 'write_field', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'set_state', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'set_assignee', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}) as record:
                response = pipeline.apply_plan(
                    plan, plan_path, False, 'config.json', legacy_replay=True)
        self.assertTrue(response['ok'])
        names = [a['action'] for a in response['actions']]
        # 作废下游通过标签是清旧标签阶段的第一步，先于写分析者描述
        self.assertEqual(names[0], 'invalidate-tag:PM-AI-MANUAL-PASSED')
        remove_tag.assert_any_call(mock.ANY, 1, 'PM-AI-MANUAL-PASSED', True)
        self.assertEqual(record.call_args.args[7]['invalidated_passed_tags'], ['PM-AI-MANUAL-PASSED'])

    def test_apply_skip_analysis_does_not_invalidate_passed_tag(self):
        plan = {
            'version': 1, 'run_id': 'run_skip_1234', 'skill': 'auto-req-analysis',
            'work_item_id': 1, 'expected_rev': 5, 'expected_state': '已建议',
            'verdict': 'SKIP-ANALYSIS', 'rules_source': {'qc': 'pre-qc-v1'},
            'tags': [], 'state_to': None, 'artifacts': [],
            'skip_reason': '接口已开发完成，本工作项仅安排现场联调，无新增业务分析面。',
        }
        with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                mock.patch.object(pipeline, 'preflight', return_value={
                    'ok': True, 'work_item': {'tags': ['PM-AI-MANUAL-PASSED']}}), \
                mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}) as record, \
                mock.patch.object(tfs, 'remove_tag') as remove_tag:
            response = pipeline.apply_plan(
                plan, '/tmp/skip-plan.json', True, 'config.json', legacy_replay=True)
        self.assertTrue(response['ok'])
        self.assertEqual(response['actions'], [])
        remove_tag.assert_not_called()
        self.assertEqual(record.call_args.args[7]['invalidated_passed_tags'], [])

    def test_apply_converges_cross_phase_qc_tag_on_analysis_rerun(self):
        # 分析重跑跨阶段收敛：清掉遗留的 QC 标签，只留本次 AUTO-ANA
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, _ = self.write_current_analysis_plan(
                directory, ['existing-ui-simple'])
            with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                    mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                    mock.patch.object(pipeline, 'preflight',
                                      return_value={'ok': True, 'work_item': {'tags': ['PM-AI-QC-NEED-REVIEW']}}), \
                    mock.patch.object(tfs, 'remove_tag', return_value={'ok': True, 'id': 1}) as remove_tag, \
                    mock.patch.object(tfs, 'replace_detail_analysis_section',
                                      return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'upload_attachment',
                                      return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(tfs, 'cleanup_analysis_attachments',
                                      return_value={'ok': True, 'id': 1, 'verified': False}), \
                    mock.patch.object(tfs, 'add_tag', return_value={'ok': True, 'id': 1}), \
                    mock.patch.object(pipeline, 'apply_field_flow', return_value=[]), \
                    mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                    mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
                response = pipeline.apply_plan(
                    plan, plan_path, False, 'config.json', legacy_replay=True)
        self.assertTrue(response['ok'])
        names = [a['action'] for a in response['actions']]
        self.assertIn('remove-tag:PM-AI-QC-NEED-REVIEW', names)
        self.assertIn('add-tag:PM-AI-AUTO-ANA', names)
        remove_tag.assert_any_call(mock.ANY, 1, 'PM-AI-QC-NEED-REVIEW', True)

    def test_apply_qc_rerun_removes_leftover_analysis_tag(self):
        # QC 重跑跨阶段收敛：清掉遗留的分析标签，只留本次 NEED-REVIEW
        run_id = 'run_qc_cross_1234'
        need_plan = {
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
            'artifacts': [{'kind': 'qc-followup',
                           'filename': f'待补充信息_1_{run_id}.json'}],
        }
        with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                mock.patch.object(pipeline, 'preflight',
                                  return_value={'ok': True, 'work_item': {'tags': ['PM-AI-AUTO-ANA']}}), \
                mock.patch.object(pipeline, 'upload_inline_artifact',
                                  return_value={'ok': True, 'id': 1}), \
                mock.patch.object(tfs, 'remove_tag', return_value={'ok': True, 'id': 1}) as remove_tag, \
                mock.patch.object(tfs, 'add_tag', return_value={'ok': True, 'id': 1}), \
                mock.patch.object(redis_client, 'publish_plan', return_value={'ok': True}), \
                mock.patch.object(tfs, 'record', return_value={'audit': 'audit.json'}):
            response = pipeline.apply_plan(
                need_plan, '/tmp/plan.json', False, 'config.json', legacy_replay=True)
        self.assertTrue(response['ok'])
        names = [a['action'] for a in response['actions']]
        self.assertIn('remove-tag:PM-AI-AUTO-ANA', names)
        self.assertIn('add-tag:PM-AI-QC-NEED-REVIEW', names)
        remove_tag.assert_any_call(mock.ANY, 1, 'PM-AI-AUTO-ANA', True)

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
            result = pipeline.apply_plan(
                plan, os.path.join(tempfile.gettempdir(), 'plan.json'), False,
                'config.json', legacy_replay=True)
        self.assertTrue(result['ok'])
        self.assertEqual(record.call_args.args[7]['attachments'], plan['attachments'])
        # wiki 审计字段透传到 record 的 extra 字典
        self.assertEqual(record.call_args.args[7]['wiki'], plan['wiki'])
        self.assertEqual(record.call_args.args[7]['tfs_requirements'], plan['tfs_requirements'])
        # 历史维护回放仍保留业务证据审计，但不覆盖正常 Redis 通信结果。
        self.assertFalse(record.call_args.args[7]['redis']['in_scope'])
        self.assertFalse(result['redis']['in_scope'])
        self.assertEqual(record.call_args.args[7]['command'], 'apply')
        publish.assert_not_called()

    def test_validate_plan_reports_analysis_description_source(self):
        # validation_errors 不再含来源文件名（保持精确匹配），改由审计 validation_sources
        # 指出分析者描述校验读取的是哪份 .md，回答“是 .md 还是 JSON content”的歧义。
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_current_analysis_plan(directory, ['report'])
            missing = pipeline.CONCISE_V3_ANALYSIS_DESCRIPTION_REQUIREMENTS['report'][0]
            with open(artifact_path, 'r', encoding='utf-8') as f:
                content = f.read().replace(f'- **{missing}**：已明确{missing}\n', '')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(content)
            result = pipeline.validate_plan(plan, plan_path)
            self.assertFalse(result['ok'])
            self.assertEqual(
                result.get('validation_sources'),
                {'analysis_description': os.path.basename(artifact_path)})

    def test_failure_result_records_redis_block_command_and_sources(self):
        plan = {'work_item_id': 1, 'run_id': 'run_12345678', 'skill': 'auto-req-analysis'}
        extra = {'validation_errors': ['X'],
                 'validation_sources': {'analysis_description': '变更方案_1_run_12345678.md'}}
        # in_scope=True：collection+config_path 齐备时，失败轮也把 redis 结果（ok/reason）落审计
        with mock.patch.object(redis_client, 'publish_failure',
                               return_value={'ok': False, 'reason': 'ConnectionRefusedError'}) as pub, \
                mock.patch.object(pipeline, 'record_failure', return_value='audit.json') as rf:
            out = pipeline.failure_result(plan, '计划校验失败：X', 'validate',
                                          collection='NETHIS5.5', config_path='cfg.json', extra=dict(extra))
        self.assertTrue(out['redis']['in_scope'])
        self.assertEqual(out['redis']['reason'], 'ConnectionRefusedError')
        pub.assert_called_once()
        recorded = rf.call_args.args[6]  # failure_result 经 extra 把 command/redis/sources 传给 record_failure
        self.assertEqual(recorded['command'], 'apply')
        self.assertEqual(recorded['redis']['reason'], 'ConnectionRefusedError')
        self.assertEqual(recorded['validation_sources']['analysis_description'], '变更方案_1_run_12345678.md')
        # in_scope=False：无 collection 时绝不碰 redis，但 command 仍标注
        with mock.patch.object(redis_client, 'publish_failure') as pub2, \
                mock.patch.object(pipeline, 'record_failure', return_value='audit.json'):
            out2 = pipeline.failure_result(plan, 'err', 'validate', extra=dict(extra))
        self.assertFalse(out2['redis']['in_scope'])
        pub2.assert_not_called()

    def test_validate_subcommand_audit_marks_command_and_no_redis_scope(self):
        # validate 子命令从不写 redis（只读），审计须自标 command='validate'、redis.in_scope=False，
        # 以与“apply 校验失败”这类同样 run_mode='validate' 但会写 redis 的轮次区分。
        with tempfile.TemporaryDirectory() as directory:
            plan, plan_path, artifact_path = self.write_current_analysis_plan(directory, ['report'])
            missing = pipeline.CONCISE_V3_ANALYSIS_DESCRIPTION_REQUIREMENTS['report'][0]
            with open(artifact_path, 'r', encoding='utf-8') as f:
                content = f.read().replace(f'- **{missing}**：已明确{missing}\n', '')
            with open(artifact_path, 'w', encoding='utf-8') as f:
                f.write(content)
            # main() 经 read_plan 从磁盘读计划，须先把 plan.json 落盘
            with open(plan_path, 'w', encoding='utf-8') as f:
                json.dump(plan, f, ensure_ascii=False)
            with mock.patch.object(tfs, 'record', return_value={'audit': 'v.json'}) as record, \
                    mock.patch.object(sys, 'exit'), \
                    mock.patch('builtins.print'), \
                    mock.patch.object(sys, 'argv', ['pipeline.py', 'validate', '--plan', plan_path]):
                pipeline.main()
            details = record.call_args.args[7]
            self.assertEqual(details['command'], 'validate')
            self.assertEqual(details['redis'], {'in_scope': False})
            self.assertEqual(details['validation_sources'],
                             {'analysis_description': os.path.basename(artifact_path)})

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
        self.assertTrue(invalid['ok'])
        self.assertIn('ATTACHMENT_CHAIN_INVALID', {
            warning['code'] for warning in invalid['warnings']})

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
                                'config.json', 'pat-x', 'CollX', 'ProjX', legacy_replay=True)
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

    def test_flow_item_no_longer_blocks_downstream_passed_tag(self):
        raw = {'id': 2, 'rev': 7, 'fields': {
            'System.State': '已建议', 'System.TeamProject': 'NETHIS5.5',
            'System.WorkItemType': '需求',
            'System.Tags': 'PM-AI-MANUAL-PASSED; PM-AI-AUTO-ANA',
            'Demand.Expected.date': '2026-09-06T16:00:00Z',
            'Winning.Dev.Leader': 'zhang_dong(张栋) <WINNING\\zhang_dong>'}}
        iter_resp = {'ok': True, 'earliest': {'path': 'NETHIS5.5\\2026\\V6.0.2608.28',
                                              'finish': '2026-08-28T00:00:00Z'}}
        with mock.patch.object(tfs, 'load_config', return_value={'collection': 'C'}), \
                mock.patch.object(tfs, 'precheck', return_value={'ok': True}), \
                mock.patch.object(tfs, 'fetch_raw', return_value=raw), \
                mock.patch.object(tfs, 'list_iterations', return_value=iter_resp), \
                mock.patch.object(tfs, 'write_field', return_value={'ok': True}), \
                mock.patch.object(tfs, 'set_state', return_value={'ok': True}), \
                mock.patch.object(tfs, 'set_assignee', return_value={'ok': True}), \
                mock.patch.object(tfs, 'record', return_value={'audit': 'a.json'}):
            response = pipeline.flow_item(2, False, 'config.json')
        self.assertTrue(response['ok'])
        self.assertNotIn('禁止覆盖', response.get('error', ''))


class SkillMemoryTests(unittest.TestCase):
    def request(self, **overrides):
        request = {
            'work_item_id': 262214,
            'run_id': 'run_262214_20260811',
            'round_diagnosis_categories': ['信息判断错误'],
            'runtime_lesson': False,
            'reason': '批量场景遗漏可跨需求复用',
            'candidate': {
                'title': '分析前必须枚举单条与批量场景',
                'category': '分析·方案',
                'scenario': '同一入口同时支持单条处理与批量处理时。',
                'practice': '分别核对选择、跳过、汇总和结果反馈，不能用单条路径代替批量路径。',
                'replaces': [],
            },
        }
        request.update(overrides)
        return request

    def test_skill_memory_appends_header_and_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, '经验记忆.md')
            result = skill_memory.process_request(self.request(), path, date='2026-08-11')
            content = pathlib.Path(path).read_text(encoding='utf-8')
        self.assertEqual(result['status'], 'APPENDED')
        self.assertEqual(result['active_count'], 1)
        self.assertIn('# skill 经验记忆', content)
        self.assertIn('## 2026-08-11 · 分析前必须枚举单条与批量场景', content)

    def test_skill_memory_retry_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, '经验记忆.md')
            skill_memory.process_request(self.request(), path, date='2026-08-11')
            result = skill_memory.process_request(self.request(), path, date='2026-08-12')
            content = pathlib.Path(path).read_text(encoding='utf-8')
        self.assertEqual(result['status'], 'DEDUP_NOOP')
        self.assertEqual(content.count('## '), 1)

    def test_skill_memory_deduplicates_legacy_heading_with_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory, '经验记忆.md')
            path.write_text(
                '# skill 经验记忆\n\n'
                '## 2026-08-06 13:23 · 分析前必须枚举单条与批量场景\n'
                '- 类别：分析·方案\n'
                '- 场景：同一入口同时支持单条处理与批量处理时。\n'
                '- 做法：分别核对选择、跳过、汇总和结果反馈，不能用单条路径代替批量路径。\n',
                encoding='utf-8')
            result = skill_memory.process_request(self.request(), str(path), date='2026-08-11')
            content = path.read_text(encoding='utf-8')
        self.assertEqual(result['status'], 'DEDUP_NOOP')
        self.assertEqual(content.count('## '), 1)

    def test_skill_memory_not_applicable_does_not_create_memory_file(self):
        request = self.request(round_diagnosis_categories=['信息不足·合理gap'],
                               reason='只补齐已识别的信息缺口', candidate=None)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, '经验记忆.md')
            result = skill_memory.process_request(request, path)
            exists = os.path.exists(path)
        self.assertEqual(result['status'], 'NOT_APPLICABLE')
        self.assertFalse(exists)

    def test_skill_memory_persistable_diagnosis_requires_candidate(self):
        request = self.request(candidate=None)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(skill_memory.MemoryInputError):
                skill_memory.process_request(request, os.path.join(directory, '经验记忆.md'))

    def test_skill_memory_replacement_keeps_append_only_and_counts_active(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, '经验记忆.md')
            first = skill_memory.process_request(self.request(), path, date='2026-08-11')
            old_heading = first['entry_title']
            replacement = self.request(candidate={
                'title': '分析前必须逐一枚举单条与批量场景',
                'category': '分析·方案',
                'scenario': '入口支持单条、批量或混合选择时。',
                'practice': '分别闭合各场景的选择、跳过、汇总和结果反馈。',
                'replaces': [old_heading],
            })
            result = skill_memory.process_request(replacement, path, date='2026-08-12')
            content = pathlib.Path(path).read_text(encoding='utf-8')
        self.assertEqual(result['status'], 'APPENDED')
        self.assertEqual(result['active_count'], 1)
        self.assertIn(f'- 取代：{old_heading}', content)
        self.assertEqual(content.count('## '), 2)

    def test_skill_memory_failure_is_written_to_result_file(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = os.path.join(directory, 'input.json')
            result_path = os.path.join(directory, 'result.json')
            pathlib.Path(input_path).write_text(json.dumps(self.request(), ensure_ascii=False),
                                                encoding='utf-8')
            with mock.patch.object(skill_memory, 'process_request', side_effect=PermissionError('denied')):
                exit_code = skill_memory.main([
                    'record', '--input', input_path, '--result', result_path,
                    '--memory-file', os.path.join(directory, '经验记忆.md'),
                ])
            result = json.loads(pathlib.Path(result_path).read_text(encoding='utf-8'))
        self.assertEqual(exit_code, 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('denied', result['reason'])

    def test_skill_memory_rejects_connection_fields(self):
        request = self.request(tfs_pat='secret')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(skill_memory.MemoryInputError):
                skill_memory.process_request(request, os.path.join(directory, '经验记忆.md'))

    def test_skill_memory_rejects_unknown_diagnosis_category(self):
        request = self.request(round_diagnosis_categories=['信息判断错'])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(skill_memory.MemoryInputError):
                skill_memory.process_request(request, os.path.join(directory, '经验记忆.md'))


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

    def test_publish_failure_writes_error_marker_and_clears_stale_success_fields(self):
        commands = []

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def execute(self, *args):
                commands.append(args)
                return 1

        # 上一轮成功(AUTO-ANA)已落 analysis_description/knowledge/checklist/next/skip_reason 等残留；
        # publish_failure 必须覆盖 verdict=ERROR 并清掉这些字段，使失败状态自洽完整(状态完整性)。
        checklist = {'work_item': '1 标题', 'generated_at_utc': '2026-08-13T00:00:00Z'}
        plan = {'work_item_id': 1, 'run_id': 'run_fail_0001', 'verdict': 'AUTO-ANA',
                'tags': ['PM-AI-AUTO-ANA'], 'state_to': '已分析', 'checklist': checklist}
        with mock.patch.object(redis_client, '_Connection', return_value=Connection()), \
                mock.patch.object(redis_client, 'load_redis_config', return_value={'ttl_seconds': 0}):
            response = redis_client.publish_failure(plan, RuntimeError('上传失败：rev changed'),
                                                    'execute', 'CollectionA', 'config.json')
        self.assertTrue(response['ok'])
        hset = commands[0]
        self.assertEqual(hset[:2], ('HSET', 'auto-req:qc:plan:CollectionA:1'))
        fields = dict(zip(hset[2::2], hset[3::2]))
        # 失败摘要：本轮 run_id + verdict=ERROR + error + 空 tags/state_to + 时间/run_mode + work_item 标签
        self.assertEqual(fields['run_id'], 'run_fail_0001')
        self.assertEqual(fields['verdict'], 'ERROR')
        self.assertEqual(fields['error'], '上传失败：rev changed')
        self.assertEqual(fields['tags'], '')
        self.assertEqual(fields['state_to'], '')
        self.assertEqual(fields['run_mode'], 'execute')
        self.assertEqual(fields['generated_at_utc'], '2026-08-13T00:00:00Z')
        self.assertEqual(fields['work_item'], '1 标题')
        # 与成功摘要同键同索引
        self.assertIn(('SADD', 'auto-req:qc:ids:CollectionA', '1'), commands)
        # 清掉上一轮成功残留动作字段(work_item 因有标签保留，不在删除之列)
        for stale in ('checklist', 'next', 'skip_reason', 'analysis_description', 'knowledge'):
            self.assertIn(('HDEL', 'auto-req:qc:plan:CollectionA:1', stale), commands)
        self.assertNotIn(('HDEL', 'auto-req:qc:plan:CollectionA:1', 'work_item'), commands)

    def test_publish_failure_returns_not_ok_on_missing_inputs_or_errors(self):
        plan = {'work_item_id': 1, 'run_id': 'run_fail_0002', 'verdict': 'AUTO-ANA',
                'tags': [], 'state_to': '已分析'}
        # 缺 collection → 不写
        self.assertFalse(redis_client.publish_failure(plan, 'e', 'execute', '', 'config.json')['ok'])
        # 缺 work_item_id → 不写
        no_id = dict(plan, work_item_id='')
        self.assertFalse(redis_client.publish_failure(no_id, 'e', 'execute', 'CollectionA', 'config.json')['ok'])
        # 连接抛异常 → {ok:false,reason}，绝不向上抛
        class Boom:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def execute(self, *args):
                raise OSError('connection refused')

        with mock.patch.object(redis_client, '_Connection', return_value=Boom()), \
                mock.patch.object(redis_client, 'load_redis_config', return_value={'ttl_seconds': 0}):
            response = redis_client.publish_failure(plan, 'e', 'execute', 'CollectionA', 'config.json')
        self.assertFalse(response['ok'])
        self.assertIn('connection refused', response['reason'])

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
            'knowledge_route': resolved_knowledge_route(),
            'kb': {'ready': True, 'source_ready': True, 'source_required': True,
                   'database_ready': True,
                   'tools_used': ['query', 'context', 'search_symbol', 'get_table_knowledge'],
                   'dedup_ran': True,
                   'findings': [{'entity': '初核=医嘱校对 adviceProofread', 'state': '已证实',
                                 'source_tool': 'query+context', 'source_type': 'code',
                                 'conclusion': '已定位医嘱校对的现有实现入口',
                                 'evidence': '初核=医嘱校对 adviceProofread 调用链',
                                 'boundary': '只证明代码图谱关系，不代表现场已部署。',
                                 'note': '冗长备注应丢弃'},
                                {'entity': 'batwce:Service.java#save', 'state': '已证实',
                                 'source_tool': 'search_symbol', 'source_type': 'code',
                                 'conclusion': '源码中存在医嘱保存入口',
                                 'evidence': 'batwce:Service.java#save',
                                 'boundary': '只证明受控仓库源码，不代表现场部署版本。'},
                                {'entity': 'HIS.dbo.ZY_ADVICE.ADVICE_TYPE', 'state': '已证实',
                                 'source_tool': 'get_table_knowledge',
                                 'source_type': 'database',
                                 'conclusion': '医嘱类型由 ADVICE_TYPE 字段承载',
                                 'evidence': 'HIS.dbo.ZY_ADVICE.ADVICE_TYPE',
                                 'boundary': '只证明知识图谱命中的字段结构。'}],
                   'note': '代码图谱就绪'},
            'wiki': {'ready': True, 'modules_matched': ['住院护士站'],
                     'findings': [{'entity': '初核业务规则', 'state': 'wiki-确认',
                                   'source': 'wiki/topics/住院.md',
                                   'conclusion': '初核属于住院医嘱校对业务',
                                   'evidence': 'wiki/topics/住院.md 初核业务规则',
                                   'boundary': 'Wiki 说明业务语义，不替代实现核验。'}]},
            'tfs_requirements': {'ready': True, 'coverage': {'collection': 'x'},
                                 'findings': [{'work_item_id': 260001, 'fact': '既有出院带药提示',
                                               'state': '已证实', 'maturity': '已落地',
                                               'source_tool': 'get_work_item',
                                               'conclusion': '历史需求曾建设出院带药提示',
                                               'evidence': 'TFS 260001 完整正文与验收记录',
                                               'boundary': '只证明历史记录，不代表当前版本仍已部署。'}]},
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
        # knowledge：只保留人读总览、来源状态与佐证清单；机器明细留在计划/审计
        knowledge = json.loads(fields['knowledge'])
        self.assertEqual(set(knowledge), {'summary', 'source_status', 'evidence_list'})
        self.assertEqual(knowledge['summary'], {'text': '云HIS 5.6：5 条已确认'})
        self.assertEqual(knowledge['source_status'], {
            '历史需求': '已命中（1 条）',
            '产品 Wiki': '已命中（1 条）',
            '代码图谱': '已命中（1 条）',
            '源码': '已命中（1 条）',
            '数据库知识': '已命中（1 条）',
        })
        self.assertEqual(knowledge['evidence_list'][0], {
            'source': '代码图谱', 'status': '已证实',
            'conclusion': '已定位医嘱校对的现有实现入口',
            'evidence': '初核=医嘱校对 adviceProofread 调用链',
            'boundary': '只证明代码图谱关系，不代表现场已部署。'})
        self.assertEqual(knowledge['evidence_list'][-1]['maturity'], '已落地')
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
        # 四类来源缺失或为空 dict → {}
        self.assertEqual(redis_client._knowledge_summary({}), {})
        self.assertEqual(redis_client._knowledge_summary({'kb': {}, 'wiki': {}, 'tfs_requirements': {}}), {})
        self.assertEqual(redis_client._human_source_status(True, [], used=True),
                         '已查询，无可展示佐证')
        self.assertEqual(redis_client._human_source_status(False, [], required=True),
                         '需核验但未就绪')
        plan = {
            'knowledge_route': resolved_knowledge_route(),
            'kb': {'ready': True, 'source_ready': True, 'source_required': True,
                   'database_ready': False,
                   'tools_used': ['query', 'search_symbol', 'search_knowledge'], 'dedup_ran': True,
                   'findings': [{'entity': 'e1', 'state': '已证实', 'source_tool': 'query',
                                 'source_type': 'code',
                                 'conclusion': '已定位保存入口',
                                 'evidence': '代码图谱调用链 e1',
                                 'boundary': '仅证明代码图谱关系。', 'note': 'drop'},
                                {'entity': 'repo:src/A.java#save', 'state': '候选',
                                 'source_tool': 'search_symbol', 'source_type': 'code'},
                                {'entity': 'table_a.column_b', 'state': '未确认',
                                 'source_tool': 'search_knowledge',
                                 'source_type': 'database'}], 'note': 'drop'},
            'wiki': {'ready': False, 'modules_matched': ['m1'],
                     'findings': [{'entity': 'e2', 'state': 'wiki-冲突', 'source': 'p.md'}]},
            'tfs_requirements': {'ready': True, 'coverage': {'collection': 'x'},
                                 'findings': [{'work_item_id': 9, 'fact': 'f', 'state': '候选',
                                               'maturity': '', 'source_tool': 'search_requirements'}]},
            'evidence_acquisition': {
                'gitnexus': {'coverage_status': 'PARTIAL', 'query_status': 'HIT'},
                'db_knowledge': {'availability': 'READY', 'coverage_status': 'UNKNOWN',
                                 'query_status': 'HIT'},
            },
        }
        summary = redis_client._knowledge_summary(plan)
        self.assertEqual(set(summary), {'summary', 'source_status', 'evidence_list'})
        self.assertEqual(summary['summary']['text'], '云HIS 5.6：1 条已确认，4 条待核实')
        self.assertEqual(summary['summary']['coverage'], [
            '数据库知识：未就绪，本轮无完整数据库佐证。',
            '产品 Wiki：未就绪或未覆盖，本轮 Wiki 佐证受限。',
        ])
        self.assertEqual(summary['source_status'], {
            '历史需求': '已命中（1 条）',
            '产品 Wiki': '已命中（1 条），但来源未就绪',
            '代码图谱': '已命中（1 条）',
            '源码': '已命中（1 条）',
            '数据库知识': '已命中（1 条），但来源未就绪',
        })
        self.assertEqual(summary['evidence_list'][0]['conclusion'], '已定位保存入口')
        self.assertNotIn('id', summary['evidence_list'][0])
        # 核心代码图谱未就绪时，只保留简短覆盖提示。
        self.assertEqual(
            redis_client._knowledge_summary({'kb': {'ready': False, 'tools_used': [], 'findings': []}}),
            {'summary': {'text': '当前需求：暂无可展示佐证',
                         'coverage': ['代码图谱：未就绪，本轮无完整代码图谱佐证。']},
             'source_status': {
                 '历史需求': '本轮未使用',
                 '产品 Wiki': '本轮未使用',
                 '代码图谱': '未就绪',
                 '源码': '本轮未使用',
                 '数据库知识': '本轮未使用',
             },
             'evidence_list': []})

        # 历史计划没有 source_type 时，仍按数据库工具名自动拆分。
        legacy = redis_client._knowledge_summary({
            'kb': {'ready': True, 'tools_used': ['context', 'get_table_knowledge'],
                   'findings': [
                       {'entity': 'Service.save', 'state': '已证实', 'source_tool': 'context'},
                       {'entity': 'TABLE_A.COL_B', 'state': '候选',
                        'source_tool': 'get_table_knowledge'},
                   ]}
        })
        self.assertEqual([f['conclusion'] for f in legacy['evidence_list']],
                         ['Service.save', 'TABLE_A.COL_B'])
        self.assertEqual([f['source'] for f in legacy['evidence_list']],
                         ['代码图谱', '数据库知识'])

        # 历史需求工具即使误写在旧 kb 中，也不能显示为代码图谱佐证。
        mixed_legacy = redis_client._knowledge_summary({
            'kb': {'ready': True, 'tools_used': ['query', 'get_work_item'],
                   'findings': [
                       {'entity': 'Service.query', 'state': '已证实', 'source_tool': 'query'},
                       {'entity': '需求97743-住院病人卡片费用信息', 'state': '已证实',
                        'source_tool': 'get_work_item'},
                   ]}
        })
        self.assertEqual(mixed_legacy['evidence_list'][0]['conclusion'], 'Service.query')
        self.assertEqual(mixed_legacy['evidence_list'][1]['source'], '历史需求')

        # 仅声明可选来源未就绪、但本轮未查询/未依赖时，不制造覆盖提示。
        optional_sources = redis_client._knowledge_summary({
            'knowledge_route': resolved_knowledge_route(),
            'kb': {'ready': True, 'source_ready': True, 'source_required': False,
                   'database_ready': False, 'tools_used': ['query', 'context'],
                   'findings': [
                       {'entity': 'YpqlServiceImpl.getQllsDtoList', 'state': '已证实',
                        'source_tool': 'context', 'source_type': 'code'},
                       {'entity': 'YpbsServiceImpl.getWmBsmxList', 'state': '已证实',
                        'source_tool': 'context', 'source_type': 'code'},
                       {'entity': 'YpbyServiceImpl.getWmBymxList', 'state': '已证实',
                        'source_tool': 'context', 'source_type': 'code'},
                       {'entity': 'ReportMapper.xml', 'state': '候选',
                        'source_tool': 'query', 'source_type': 'code'},
                   ]},
            'wiki': {'ready': False, 'modules_matched': [], 'findings': []},
            'tfs_requirements': {'ready': True, 'findings': []},
        })
        self.assertEqual(optional_sources['summary'],
                         {'text': '云HIS 5.6：3 条已确认，1 条待核实'})
        self.assertNotIn('coverage', optional_sources['summary'])
        self.assertEqual(set(optional_sources), {'summary', 'source_status', 'evidence_list'})
        self.assertEqual(optional_sources['source_status'], {
            '历史需求': '本轮未使用',
            '产品 Wiki': '未就绪',
            '代码图谱': '已命中（4 条）',
            '源码': '本轮未使用',
            '数据库知识': '未就绪',
        })
        self.assertEqual(len(optional_sources['evidence_list']), 4)
        self.assertTrue(all(set(finding) == {
            'source', 'status', 'conclusion', 'evidence', 'boundary'}
                            for finding in optional_sources['evidence_list']))

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
            # knowledge 按实际存在的来源投影；本用例没有数据库发现，所以不出现 database。
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
