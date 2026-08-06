#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto-req-qc / auto-req-analysis 共享 TFS 客户端（标准库 urllib，无第三方依赖）

只做四件事：读工作项、写标签、转状态、写字段、传附件 + 一次审计落盘。
所有写操作支持 --dry-run（只读 + 打印预期动作，不写 TFS）。
HTTP 纪律移植自 reg-req-breakdown/scripts/tfs_batch_upload.py：
  - 按端点显式区分 Content-Type（WIQL/GET 用 json；工作项 PATCH 用 json-patch+json；附件用 octet-stream）
  - 网络错误(-1)/服务端错误(>=500) 重试 3 次
  - PAT 默认从 tfs-buddy.db.sys_users.pat 取（account 默认 lyf）；也支持 '$ENV' / 字面值（config 里 auth 字段控制）

所有命令统一打印 JSON 到 stdout；硬错误（配置缺失/PAT 未设）exit 1。
字段引用名（refname）确认自 reg-req-analysis/scripts/tfs-auto-complete.js：
  需求分析 = Winning.Demand.Analysis；详细信息 = System.Description；标题=System.Title；状态=System.State；
  指派人=System.AssignedTo ; 描述=System.Description ; 迭代=System.IterationPath ;
  标签=System.Tags（读-改-写）。

用法：
  python3 tfs_client.py fetch <id> [--config PATH]
  python3 tfs_client.py download-attachments <id> --output-dir 过程文件/<id>/<run_id>/附件 [--include-external] [--max-bytes 20971520] [--config PATH]
  python3 tfs_client.py add-tag <id> --tag PM-AI-AUTO-ANA [--config PATH] [--dry-run]
  python3 tfs_client.py remove-tag <id> --tag PM-AI-QC-NEED-INFO [--config PATH] [--dry-run]
  python3 tfs_client.py set-state <id> --state 已分析 [--config PATH] [--dry-run]
  python3 tfs_client.py write-field <id> --field Winning.Demand.Analysis --value @file.md|--value TEXT [--mode replace|append] [--marker RUN_MARKER] [--config PATH] [--dry-run]
  python3 tfs_client.py upload-attachment <id> --file path/to.md [--config PATH] [--dry-run]
  python3 tfs_client.py precheck [--config PATH]
  python3 tfs_client.py list-iterations --project <teamProject> [--expected-date 2026-08-31] [--config PATH]
  python3 tfs_client.py record --skill auto-req-qc --id 228549 --verdict NEED-INFO [--tag ...] [--state-from S] [--state-to S] [--trace PATH] [--extra '{"k":"v"}'] [--run-id ID]
"""
import argparse
import base64
import datetime
import gzip
import hashlib
import html.parser
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 脚本在 skills/reg-auto-req-analysis/_lib/tfs/ 下；BUNDLE_ROOT = skills/reg-auto-req-analysis/（迁移单位=skill 目录）
BUNDLE_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, '..', '..'))
# 过程文件根（运行产物，落在仓库根、迁移单位之外）：BUNDLE_ROOT 上两级 = 仓库根；record() 按 <工作项>/runs/ 落审计
PROCESS_DIR = os.path.join(os.path.dirname(os.path.dirname(BUNDLE_ROOT)), '过程文件')
API = '4.1'
DEFAULT_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8), name='Asia/Shanghai')

# 字段引用名（确认自 tfs-auto-complete.js）
F_REQUIREMENT_ANALYSIS = 'Winning.Demand.Analysis'
# 兼容旧调用方；自动分析流程不再向该字段写入内容。
F_ANALYZER_DESC = F_REQUIREMENT_ANALYSIS
F_TITLE = 'System.Title'
F_STATE = 'System.State'
F_ASSIGNED_TO = 'System.AssignedTo'
F_DESCRIPTION = 'System.Description'
F_ITERATION = 'System.IterationPath'
F_AREA_PATH = 'System.AreaPath'
F_TAGS = 'System.Tags'
F_PRODUCT = 'Winning.Product.Name'
F_REQUIREMENT_TYPE = 'Microsoft.VSTS.CMMI.RequirementType'
F_PIMIS_PRIORITY = 'Pmis.Demand.Priority'
F_EXPECTED_DATE = 'Demand.Expected.date'
F_VERSION = 'Winning.Prod.Version'  # 版本号字段（5.5/5.6 判定来源；不从迭代路径判断）
F_START_DATE = 'Microsoft.VSTS.Scheduling.StartDate'   # 开始日期（字段流转写当前日期）
F_FINISH_DATE = 'Microsoft.VSTS.Scheduling.FinishDate'  # 完成/交付日期（字段流转写迭代截止日期）
F_DEV_LEADER = 'Winning.Dev.Leader'  # 开发组长/负责人（字段流转指派首选来源；读回 account(中文名) <WINNING\account>）

# PM-AI-* 标签集（整体方案）
PM_AI_TAGS = {
    'PM-AI-QC-NEED-INFO', 'PM-AI-QC-NEED-REVIEW',
    'PM-AI-AUTO-ANA', 'PM-AI-MANUAL-REVIEW', 'PM-AI-STOP-AUTO', 'PM-AI-MANUAL-PASSED',
}


# ---------------- output helpers ----------------
def emit(obj):
    """统一 JSON 输出。"""
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def fail(msg, code='ERROR', exit_code=1):
    emit({'ok': False, 'error': msg, 'code': code})
    sys.exit(exit_code)


def parse_tfs_datetime(value):
    """解析 TFS ISO 时间并转换为北京时间；纯日期按业务日期原样解释。"""
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time(), tzinfo=BEIJING_TZ)
    else:
        raw = str(value or '').strip()
        if not raw:
            raise ValueError('时间值为空')
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            return datetime.datetime.combine(datetime.date.fromisoformat(raw), datetime.time(),
                                             tzinfo=BEIJING_TZ)
        normalized = raw[:-1] + '+00:00' if raw.endswith(('Z', 'z')) else raw
        try:
            parsed = datetime.datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f'无法解析 TFS 时间: {raw!r}') from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ)


def beijing_iso(value):
    """返回北京时间 ISO；输入为纯日期时保留 YYYY-MM-DD 形态。"""
    raw = str(value or '').strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        return raw
    return parse_tfs_datetime(value).isoformat(timespec='seconds')


def beijing_date(value=None):
    """返回北京时间业务日期；不传值时使用当前北京时间。"""
    if value is None:
        return datetime.datetime.now(BEIJING_TZ).date()
    return parse_tfs_datetime(value).date()


def beijing_timestamp(fmt):
    return datetime.datetime.now(BEIJING_TZ).strftime(fmt)


# ---------------- config ----------------
def load_config(path, pat_override=None, collection_override=None, project_override=None):
    """加载配置。PAT 优先级：pat_override(--pat) > 环境变量 TFS_PAT > 配置 tfs.pat(字面)。
    collection 优先级：collection_override(--collection) > 环境变量 TFS_COLLECTION > 配置 tfs.collection。
    project 优先级：project_override(--project) > 环境变量 TFS_PROJECT > 配置 tfs.project。

    动态 PAT / collection / project 仅本次调用临时使用，不写回配置；配置里的值是兜底默认。
    """
    if not os.path.exists(path):
        fail(f"配置文件不存在: {path}（从 tfs-config.template.json 复制一份）", 'CONFIG_NOT_FOUND')
    with open(path, 'r', encoding='utf-8') as f:
        c = json.load(f)
    t = c.get('tfs', {})
    for k in ('server', 'port', 'collection', 'project'):
        if not t.get(k):
            fail(f"配置缺字段 tfs.{k}", 'CONFIG_INCOMPLETE')
    pat = pat_override or os.environ.get('TFS_PAT') or t.get('pat')
    if not pat:
        fail('未提供 PAT：请在配置 tfs.pat 填入，或用 --pat / 环境变量 TFS_PAT 临时传入', 'PAT_MISSING')
    collection = collection_override or os.environ.get('TFS_COLLECTION') or t['collection']
    project = project_override or os.environ.get('TFS_PROJECT') or t['project']
    external_attachments = c.get('external_attachments', {})
    if external_attachments and not isinstance(external_attachments, dict):
        fail('external_attachments 必须是对象', 'CONFIG_INCOMPLETE')
    return {
        'server': t['server'], 'port': t['port'],
        'collection': collection, 'project': project,
        'base_url': f"http://{t['server']}:{t['port']}/tfs/{urllib.parse.quote(collection)}/{urllib.parse.quote(project)}",
        'pat': pat,
        'external_attachments': external_attachments,
    }


def auth_header(pat):
    return 'Basic ' + base64.b64encode((':' + pat).encode()).decode()


# ---------------- http（按端点区分 Content-Type）----------------
CT = {'patch': 'application/json-patch+json', 'json': 'application/json', 'octet': 'application/octet-stream'}


def wit_http(client, method, path, body=None, raw=None, ctype='patch'):
    url = client['base_url'] + path
    headers = {'Authorization': auth_header(client['pat']), 'Content-Type': CT[ctype]}
    if raw is not None:
        data = raw
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    else:
        data = None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        text = resp.read().decode('utf-8', errors='replace')
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = text
        return resp.status, parsed
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return -1, repr(e)


def wit_retry(client, method, path, body=None, raw=None, ctype='patch', attempts=3):
    """网络错误(-1)或服务端错误(>=500)重试。"""
    last = (-1, 'no attempt')
    for a in range(attempts):
        st, data = wit_http(client, method, path, body, raw, ctype)
        if st != -1 and st < 500:
            return st, data
        last = (st, data)
        if a < attempts - 1:
            time.sleep(2 * (a + 1))
    return last


# ---------------- read ----------------
def fetch_raw(client, wid):
    st, data = wit_retry(client, 'GET',
                         f'/_apis/wit/workitems/{wid}?$expand=relations&api-version={API}', ctype='json')
    if st != 200 or not isinstance(data, dict):
        fail(f"读取工作项 {wid} 失败 (HTTP {st}): {str(data)[:300]}", 'FETCH_FAILED')
    return data


def map_workitem(raw):
    f = raw.get('fields', {})
    tags_raw = f.get(F_TAGS, '') or ''
    tags = [t.strip() for t in tags_raw.replace(',', ';').split(';') if t.strip()]
    area_path = f.get(F_AREA_PATH, '') or ''
    expected_date_raw = f.get(F_EXPECTED_DATE) or f.get('Custom.ExpectedDate') or None
    expected_date = expected_date_raw
    if expected_date_raw:
        try:
            expected_date = beijing_iso(expected_date_raw)
        except ValueError:
            # 保留原值供 list_iterations 失败关闭，同时避免 fetch 丢失现场数据。
            pass
    # 菜单源按 TFS 区域首级配置；旧工作项无 AreaPath 时才兼容 teamProject。
    area = area_path.split('\\', 1)[0] if area_path else f.get('System.TeamProject', '')
    return {
        'id': raw.get('id'),
        'rev': raw.get('rev'),
        'workItemType': f.get('System.WorkItemType', ''),
        'title': f.get(F_TITLE, ''),
        'state': f.get(F_STATE, ''),
        'assignedTo': f.get(F_ASSIGNED_TO, ''),
        'iterationPath': f.get(F_ITERATION, ''),
        'teamProject': f.get('System.TeamProject', ''),
        'areaPath': area_path,
        'area': area,
        'areaSource': F_AREA_PATH if area_path else 'System.TeamProject',
        'demandType': f.get(F_REQUIREMENT_TYPE, ''),
        'pimisPriority': f.get(F_PIMIS_PRIORITY, ''),
        'description': f.get(F_DESCRIPTION, ''),
        'analyzerDesc': f.get(F_ANALYZER_DESC, ''),
        'tags': tags,
        'expectedDate': expected_date,
        'expectedDateRaw': expected_date_raw,
        'productName': f.get(F_PRODUCT, ''),
        'version': f.get(F_VERSION, ''),
        'devLeader': f.get(F_DEV_LEADER, '') or '',
    }


# ---------------- write ----------------
def revision_guard(raw):
    """用工作项 revision 防止并发读改写静默覆盖。"""
    rev = raw.get('rev')
    return [] if rev is None else [{"op": "test", "path": "/rev", "value": rev}]


def attachment_names(raw):
    """从工作项 relations 提取已关联附件文件名，供重跑去重。"""
    names = set()
    for relation in raw.get('relations', []) or []:
        if relation.get('rel') != 'AttachedFile':
            continue
        url = relation.get('url', '')
        filename = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get('fileName', [''])[0]
        if filename:
            names.add(urllib.parse.unquote(filename))
    return names


def attachment_descriptors(raw):
    """列出工作项 AttachedFile 关系，不读取或下载文件。"""
    attachments = []
    for index, relation in enumerate(raw.get('relations', []) or [], start=1):
        if relation.get('rel') != 'AttachedFile':
            continue
        url = relation.get('url', '')
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        name = urllib.parse.unquote(query.get('fileName', [''])[0])
        if not name:
            name = (relation.get('attributes') or {}).get('name', '')
        attachments.append({
            'name': _safe_attachment_name(name, index),
            'url': url,
            'comment': (relation.get('attributes') or {}).get('comment', ''),
        })
    return attachments


class _DescriptionLinkParser(html.parser.HTMLParser):
    """提取详细信息中的锚点链接；只保留链接文本作审计提示。"""
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() not in ('a', 'el-link'):
            return
        href = dict(attrs).get('href')
        if href:
            self._href = href.strip()
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() in ('a', 'el-link') and self._href:
            self.links.append({'url': self._href, 'label': ''.join(self._text).strip()})
            self._href = None
            self._text = []


def description_link_descriptors(raw):
    """从详细信息提取外部样例链接；不在此处做跨站请求。"""
    parser = _DescriptionLinkParser()
    try:
        parser.feed((raw.get('fields', {}) or {}).get(F_DESCRIPTION, '') or '')
        parser.close()
    except Exception:
        return []
    return [
        {
            'name': _safe_attachment_name(link['label'] or f'external_attachment_{index}', index),
            'url': link['url'],
            'comment': '详细信息中的外部附件链接',
            'source': 'description-link',
        }
        for index, link in enumerate(parser.links, start=1)
    ]


class _DescriptionImageParser(html.parser.HTMLParser):
    """提取详细信息中的 <img src> 内嵌图片；图片无链接文本，名称取 URL 末段。"""

    def __init__(self):
        super().__init__()
        self.srcs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != 'img':
            return
        src = dict(attrs).get('src')
        if src:
            self.srcs.append(src.strip())


def description_image_descriptors(raw):
    """从详细信息提取 <img src> 内嵌图片候选；仅在 --include-external 时受控下载。

    TFS 富文本常以 <img src="https://assist.winning.com.cn/..."> 内嵌截图，这类
    公开图片 CDN 既非 AttachedFile 也非 <a> 锚点（_DescriptionLinkParser 只取 href），
    需单独提取。仅保留 http/https 绝对地址（跳过 data: URI 与相对路径）；最终下载
    仍受 external_attachments.allowed_hosts 白名单与逐跳重定向校验约束。
    """
    parser = _DescriptionImageParser()
    try:
        parser.feed((raw.get('fields', {}) or {}).get(F_DESCRIPTION, '') or '')
        parser.close()
    except Exception:
        return []
    descriptors = []
    for index, src in enumerate(parser.srcs, start=1):
        parsed = urllib.parse.urlparse(src)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            continue
        url_tail = os.path.basename(parsed.path) or f'image_{index}'
        descriptors.append({
            'name': _safe_attachment_name(urllib.parse.unquote(url_tail), index),
            'url': src,
            'comment': '详细信息中的内嵌图片',
            'source': 'description-image',
        })
    return descriptors


def external_download_link_descriptors(content, base_url):
    """从 ERP 附件列表页提取实际下载链接；只接受明确的附件下载路径。"""
    parser = _DescriptionLinkParser()
    try:
        parser.feed(content.decode('utf-8', errors='replace'))
        parser.close()
    except Exception:
        return []
    descriptors = []
    for index, link in enumerate(parser.links, start=1):
        url = urllib.parse.urljoin(base_url, link['url'])
        parsed = urllib.parse.urlparse(url)
        if '/attachment/download/' not in parsed.path:
            continue
        query = urllib.parse.parse_qs(parsed.query)
        name = urllib.parse.unquote(query.get('fileName', [link['label']])[0])
        descriptors.append({
            'name': _safe_attachment_name(name or f'external_attachment_{index}', index),
            'url': url,
            'comment': 'ERP 附件列表页中的下载链接',
            'source': 'external-link-page',
        })
    return descriptors


def _safe_attachment_name(name, index):
    """将服务端文件名收敛为单个安全文件名，防止路径穿越。"""
    cleaned = os.path.basename((name or '').replace('\\', '/').replace('\x00', '')).strip()
    if cleaned in ('', '.', '..'):
        return f'attachment_{index}'
    return cleaned


def _attachment_url_allowed(client, url):
    """只接受当前 TFS collection 的附件 URL，避免 relation 触发跨站下载。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'http' or not parsed.hostname:
        return False
    if parsed.hostname.lower() != str(client['server']).lower():
        return False
    try:
        port = parsed.port or 80
    except ValueError:
        return False
    if port != int(client['port']):
        return False
    collection = urllib.parse.quote(str(client['collection']), safe='')
    prefix = f'/tfs/{collection}/_apis/wit/attachments/'
    return parsed.path.startswith(prefix)


def _host_matches_allowlist(hostname, hosts):
    """主机是否命中白名单；支持精确主机与 ``*.suffix`` 通配符。

    ``*.winning.com.cn`` 匹配任意 ``<label>.winning.com.cn``（至少一层子域），
    不匹配裸域 ``winning.com.cn`` 或 ``evilwinning.com.cn`` 等近似域。
    """
    hostname = hostname.lower()
    for raw in hosts:
        host = str(raw).lower()
        if host.startswith('*.'):
            suffix = host[2:]
            if suffix and hostname.endswith('.' + suffix):
                return True
        elif host == hostname:
            return True
    return False


def _external_attachment_url_allowed(external_config, url):
    """仅允许 HTTPS 且命中配置主机白名单（支持 ``*.suffix`` 通配符）的外链。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        return False
    hosts = external_config.get('allowed_hosts', []) if isinstance(external_config, dict) else []
    return _host_matches_allowlist(parsed.hostname, hosts)


def _redacted_url(url):
    """审计保留来源定位，不保存查询串中的可能令牌。"""
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))


def _external_auth_headers(external_config):
    """外链认证只读取专用环境变量，绝不复用 TFS PAT。"""
    auth = external_config.get('auth', {}) if isinstance(external_config, dict) else {}
    if not auth:
        return {'Accept': '*/*'}, None
    if not isinstance(auth, dict):
        return None, 'external_attachments.auth 必须是对象'
    auth_type = auth.get('type', 'none')
    if auth_type == 'none':
        return {'Accept': '*/*'}, None
    env_name = auth.get('env', '')
    if not isinstance(env_name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env_name):
        return None, '外链认证环境变量名无效'
    credential = os.environ.get(env_name)
    if not credential:
        return None, f'缺少外链认证环境变量 {env_name}'
    if auth_type == 'cookie':
        return {'Accept': '*/*', 'Cookie': credential}, None
    if auth_type == 'bearer':
        return {'Accept': '*/*', 'Authorization': f'Bearer {credential}'}, None
    return None, f'不支持的外链认证类型 {auth_type}'


class _ExternalRedirectHandler(urllib.request.HTTPRedirectHandler):
    """逐跳验证重定向，防止允许的 ERP 链接跳转到任意站点。"""
    def __init__(self, external_config):
        super().__init__()
        self.external_config = external_config

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _external_attachment_url_allowed(self.external_config, newurl):
            raise urllib.error.HTTPError(newurl, code, '外链重定向目标不在白名单', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _content_disposition_name(headers, fallback, index):
    value = headers.get('Content-Disposition', '') if headers else ''
    for part in value.split(';'):
        key, sep, raw = part.strip().partition('=')
        if sep and key.lower() in ('filename', 'filename*'):
            raw = raw.strip().strip('"').strip("'")
            if raw.lower().startswith("utf-8''"):
                raw = raw[7:]
            return _safe_attachment_name(urllib.parse.unquote(raw), index)
    return _safe_attachment_name(fallback, index)


def _available_attachment_path(directory, filename):
    """避免同名源附件覆盖；返回仍位于目标目录中的路径。"""
    stem, suffix = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    serial = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f'{stem}_{serial}{suffix}')
        serial += 1
    return candidate


def download_attachments(client, wid, output_dir, max_bytes=DEFAULT_ATTACHMENT_MAX_BYTES,
                         include_external=False):
    """下载 TFS 附件；可显式受控下载详细信息中的 ERP 样例链接。"""
    if max_bytes <= 0:
        fail('max_bytes 必须为正整数', 'BAD_MAX_BYTES')
    raw = fetch_raw(client, wid)
    descriptors = [(descriptor, 'tfs') for descriptor in attachment_descriptors(raw)]
    external_config = client.get('external_attachments', {})
    if include_external:
        descriptors.extend((descriptor, 'external') for descriptor in description_link_descriptors(raw))
        descriptors.extend((descriptor, 'external') for descriptor in description_image_descriptors(raw))
    os.makedirs(output_dir, exist_ok=True)
    downloaded, skipped, errors = [], [], []
    for index, (descriptor, source) in enumerate(descriptors, start=1):
        name = descriptor['name']
        url = descriptor['url']
        if source == 'tfs' and not _attachment_url_allowed(client, url):
            skipped.append({'name': name, 'reason': '附件 URL 不属于当前 TFS collection，未下载'})
            continue
        if source == 'external':
            if not external_config.get('enabled', False):
                skipped.append({'name': name, 'source': source, 'source_url': _redacted_url(url),
                                'reason': '外部附件下载未启用'})
                continue
            if not _external_attachment_url_allowed(external_config, url):
                skipped.append({'name': name, 'source': source, 'source_url': _redacted_url(url),
                                'reason': '外部附件 URL 不在 HTTPS 主机白名单，未下载'})
                continue
            headers, auth_error = _external_auth_headers(external_config)
            if auth_error:
                skipped.append({'name': name, 'source': source, 'source_url': _redacted_url(url),
                                'reason': auth_error})
                continue
            handlers = [_ExternalRedirectHandler(external_config)]
            if external_config.get('bypass_proxy', False):
                handlers.insert(0, urllib.request.ProxyHandler({}))
            opener = urllib.request.build_opener(*handlers)
        else:
            headers = {'Authorization': auth_header(client['pat']), 'Accept': '*/*'}
            opener = None
        request = urllib.request.Request(url, method='GET', headers=headers)
        try:
            response_context = opener.open(request, timeout=60) if opener else urllib.request.urlopen(request, timeout=60)
            with response_context as response:
                content_length = response.headers.get('Content-Length')
                if content_length and int(content_length) > max_bytes:
                    skipped.append({'name': name, 'reason': f'文件超过 {max_bytes} bytes 限制'})
                    continue
                content = response.read(max_bytes + 1)
                content_encoding = response.headers.get('Content-Encoding', '').lower()
                if content_encoding == 'gzip':
                    with gzip.GzipFile(fileobj=io.BytesIO(content)) as compressed:
                        content = compressed.read(max_bytes + 1)
                if len(content) > max_bytes:
                    skipped.append({'name': name, 'reason': f'文件超过 {max_bytes} bytes 限制'})
                    continue
                if source == 'external':
                    children = external_download_link_descriptors(content, url)
                    if children:
                        descriptors.extend((child, 'external') for child in children)
                        continue
                actual_name = _content_disposition_name(response.headers, name, index)
                path = _available_attachment_path(output_dir, actual_name)
                with open(path, 'wb') as f:
                    f.write(content)
                downloaded.append({
                    'name': os.path.basename(path),
                    'path': path,
                    'size': len(content),
                    'sha256': hashlib.sha256(content).hexdigest(),
                    'content_type': response.headers.get('Content-Type', ''),
                    'content_encoding': content_encoding,
                    'comment': descriptor['comment'],
                    'source': source,
                    **({'source_url': _redacted_url(url)} if source == 'external' else {}),
                })
        except Exception as exc:
            error = {'name': name, 'reason': str(exc)[:300]}
            if source == 'external':
                error.update({'source': source, 'source_url': _redacted_url(url)})
            errors.append(error)
    return {
        'ok': True,
        'work_item_id': wid,
        'output_dir': output_dir,
        'total': len(descriptors),
        'downloaded': downloaded,
        'skipped': skipped,
        'errors': errors,
    }


def patch_workitem(client, wid, raw, operations):
    """提交受 revision 保护的 PATCH；并发变更时明确失败而非覆盖。"""
    payload = revision_guard(raw) + operations
    return wit_retry(client, 'PATCH', f'/_apis/wit/workitems/{wid}?api-version={API}', payload, ctype='patch')


def add_tag(client, wid, tag, dry_run):
    if tag not in PM_AI_TAGS:
        fail(f"非法标签 {tag}，允许: {sorted(PM_AI_TAGS)}", 'BAD_TAG')
    wi = fetch_raw(client, wid)
    cur = map_workitem(wi)
    existing = cur['tags']
    if tag in existing:
        return {'ok': True, 'id': wid, 'tag': tag, 'before': existing, 'after': existing,
                'noop': True, 'dry_run': dry_run, 'msg': '标签已存在，跳过'}
    after = existing + [tag]
    if dry_run:
        return {'ok': True, 'id': wid, 'tag': tag, 'before': existing, 'after': after,
                'noop': False, 'dry_run': True, 'msg': '[dry-run] 将写入标签'}
    st, data = patch_workitem(client, wid, wi,
                              [{"op": "replace", "path": f"/fields/{F_TAGS}", "value": ';'.join(after)}])
    if st != 200:
        return {'ok': False, 'id': wid, 'tag': tag, 'before': existing, 'error': f'HTTP {st}: {str(data)[:200]}'}
    return {'ok': True, 'id': wid, 'tag': tag, 'before': existing, 'after': after, 'dry_run': False}


def remove_tag(client, wid, tag, dry_run):
    if tag not in PM_AI_TAGS:
        fail(f"非法标签 {tag}，允许: {sorted(PM_AI_TAGS)}", 'BAD_TAG')
    wi = fetch_raw(client, wid)
    existing = map_workitem(wi)['tags']
    if tag not in existing:
        return {'ok': True, 'id': wid, 'tag': tag, 'before': existing, 'after': existing,
                'noop': True, 'dry_run': dry_run, 'msg': '标签不存在，跳过'}
    after = [item for item in existing if item != tag]
    if dry_run:
        return {'ok': True, 'id': wid, 'tag': tag, 'before': existing, 'after': after,
                'noop': False, 'dry_run': True, 'msg': '[dry-run] 将移除标签'}
    st, data = patch_workitem(client, wid, wi,
                              [{"op": "replace", "path": f"/fields/{F_TAGS}", "value": ';'.join(after)}])
    if st != 200:
        return {'ok': False, 'id': wid, 'tag': tag, 'before': existing,
                'error': f'HTTP {st}: {str(data)[:200]}'}
    return {'ok': True, 'id': wid, 'tag': tag, 'before': existing, 'after': after, 'dry_run': False}


def set_state(client, wid, state, dry_run):
    wi = fetch_raw(client, wid)
    cur_state = map_workitem(wi)['state']
    if cur_state == state:
        return {'ok': True, 'id': wid, 'before': cur_state, 'after': cur_state,
                'noop': True, 'dry_run': dry_run, 'msg': '状态已是目标值，跳过'}
    if dry_run:
        return {'ok': True, 'id': wid, 'before': cur_state, 'after': state,
                'noop': False, 'dry_run': True, 'msg': '[dry-run] 将流转状态'}
    st, data = patch_workitem(client, wid, wi,
                              [{"op": "replace", "path": f"/fields/{F_STATE}", "value": state}])
    if st != 200:
        # TFS 会校验状态机；非法流转会在此报错 → 调用方据此降级（绝不强转）
        return {'ok': False, 'id': wid, 'before': cur_state, 'target': state,
                'error': f'HTTP {st}: {str(data)[:200]}'}
    return {'ok': True, 'id': wid, 'before': cur_state, 'after': state, 'dry_run': False}


def set_assignee(client, wid, assignee, dry_run):
    wi = fetch_raw(client, wid)
    cur = map_workitem(wi)['assignedTo']
    if dry_run:
        return {'ok': True, 'id': wid, 'field': F_ASSIGNED_TO, 'before': cur,
                'after': assignee, 'dry_run': True, 'msg': '[dry-run] 将指派'}
    st, data = patch_workitem(client, wid, wi,
                              [{"op": "replace", "path": f"/fields/{F_ASSIGNED_TO}", "value": assignee}])
    if st != 200:
        # 多为身份解析失败（display name/账号 TFS 不接受）→ 调用方据此降级，绝不强写
        return {'ok': False, 'id': wid, 'field': F_ASSIGNED_TO, 'before': cur, 'target': assignee,
                'error': f'HTTP {st}: {str(data)[:200]}'}
    return {'ok': True, 'id': wid, 'field': F_ASSIGNED_TO, 'before': cur, 'after': assignee, 'dry_run': False}


def write_field(client, wid, field, value, mode, dry_run, marker=''):
    wi = fetch_raw(client, wid)
    old = wi.get('fields', {}).get(field, '') or ''
    if marker and marker in old:
        return {'ok': True, 'id': wid, 'field': field, 'mode': mode, 'noop': True,
                'dry_run': dry_run, 'msg': '检测到同一 run 标记，跳过重复写入'}
    if mode == 'append' and old:
        new_value = old.rstrip() + '\n\n' + value
    else:
        new_value = value
    if dry_run:
        return {'ok': True, 'id': wid, 'field': field, 'mode': mode,
                'old_len': len(old), 'new_len': len(new_value), 'dry_run': True, 'msg': '[dry-run] 将写字段'}
    st, data = patch_workitem(client, wid, wi,
                              [{"op": "replace", "path": f"/fields/{field}", "value": new_value}])
    if st != 200:
        return {'ok': False, 'id': wid, 'field': field, 'error': f'HTTP {st}: {str(data)[:200]}'}
    return {'ok': True, 'id': wid, 'field': field, 'mode': mode,
            'old_len': len(old), 'new_len': len(new_value), 'dry_run': False}


def _detail_section_bounds(value, start_title, end_title):
    """返回两个详细信息区段标题之间可替换内容的字符边界。"""
    if value.count(start_title) != 1 or value.count(end_title) != 1:
        raise ValueError(f'详细信息必须各含一个“{start_title}”和“{end_title}”标题')
    start_title_at = value.index(start_title)
    end_title_at = value.index(end_title)
    if start_title_at >= end_title_at:
        raise ValueError(f'详细信息中“{start_title}”必须位于“{end_title}”之前')

    start_open = value.rfind('<', 0, start_title_at)
    start_close = value.find('</', start_title_at)
    start_close_end = value.find('>', start_close)
    end_open = value.rfind('<', 0, end_title_at)
    if min(start_open, start_close, start_close_end, end_open) < 0:
        raise ValueError('详细信息的分析者描述标题必须位于完整 HTML 块标签中')
    if not (start_open < start_title_at < start_close < start_close_end < end_open < end_title_at):
        raise ValueError('详细信息的分析者描述区段 HTML 结构异常')
    return start_close_end + 1, end_open


def replace_detail_analysis_section(client, wid, html_content, dry_run):
    """整段替换详细信息中“分析者描述”与“开发者描述”之间的富文本。"""
    wi = fetch_raw(client, wid)
    old = wi.get('fields', {}).get(F_DESCRIPTION, '') or ''
    try:
        start, end = _detail_section_bounds(old, '【分析者描述】', '【开发者描述】')
    except ValueError as exc:
        return {'ok': False, 'id': wid, 'field': F_DESCRIPTION, 'error': str(exc)}
    new_value = old[:start] + html_content + old[end:]
    if new_value == old:
        return {'ok': True, 'id': wid, 'field': F_DESCRIPTION, 'noop': True,
                'dry_run': dry_run, 'msg': '分析者描述已是目标内容，跳过重复写入'}
    if dry_run:
        return {'ok': True, 'id': wid, 'field': F_DESCRIPTION,
                'old_len': len(old), 'new_len': len(new_value), 'dry_run': True,
                'msg': '[dry-run] 将替换详细信息中的分析者描述'}
    st, data = patch_workitem(client, wid, wi,
                              [{'op': 'replace', 'path': f'/fields/{F_DESCRIPTION}', 'value': new_value}])
    if st != 200:
        return {'ok': False, 'id': wid, 'field': F_DESCRIPTION, 'error': f'HTTP {st}: {str(data)[:200]}'}
    return {'ok': True, 'id': wid, 'field': F_DESCRIPTION,
            'old_len': len(old), 'new_len': len(new_value), 'dry_run': False}


def _canonical_legacy_markdown(value):
    return re.sub(r'\n{3,}', '\n\n', value.strip())


def remove_legacy_analysis_append(client, wid, expected_body, dry_run):
    """移除一次已知的错误 Markdown 追加；不匹配时拒绝删除。"""
    wi = fetch_raw(client, wid)
    old = wi.get('fields', {}).get(F_REQUIREMENT_ANALYSIS, '') or ''
    title = expected_body.lstrip().split('\n', 1)[0]
    if not title or old.count(title) != 1:
        return {'ok': False, 'id': wid, 'field': F_REQUIREMENT_ANALYSIS,
                'error': '需求分析字段不含唯一的预期旧 Markdown 标题，拒绝清理'}
    start = old.index(title)
    actual_body = old[start:]
    if _canonical_legacy_markdown(actual_body) != _canonical_legacy_markdown(expected_body):
        return {'ok': False, 'id': wid, 'field': F_REQUIREMENT_ANALYSIS,
                'error': '需求分析字段标题后的内容与预期旧 Markdown 不完全一致，拒绝清理'}
    new_value = old[:start].rstrip()
    if not new_value:
        return {'ok': False, 'id': wid, 'field': F_REQUIREMENT_ANALYSIS,
                'error': '清理会删除需求分析字段的全部既有内容，拒绝执行'}
    if dry_run:
        return {'ok': True, 'id': wid, 'field': F_REQUIREMENT_ANALYSIS,
                'old_len': len(old), 'new_len': len(new_value), 'dry_run': True,
                'msg': '[dry-run] 将移除错误追加的 Markdown'}
    st, data = patch_workitem(client, wid, wi,
                              [{'op': 'replace', 'path': f'/fields/{F_REQUIREMENT_ANALYSIS}', 'value': new_value}])
    if st != 200:
        return {'ok': False, 'id': wid, 'field': F_REQUIREMENT_ANALYSIS,
                'error': f'HTTP {st}: {str(data)[:200]}'}
    return {'ok': True, 'id': wid, 'field': F_REQUIREMENT_ANALYSIS,
            'old_len': len(old), 'new_len': len(new_value), 'dry_run': False}


def upload_attachment(client, wid, file_path, dry_run):
    if not os.path.exists(file_path):
        fail(f"附件不存在: {file_path}", 'FILE_NOT_FOUND')
    file_name = os.path.basename(file_path)
    current = fetch_raw(client, wid)
    if file_name in attachment_names(current):
        return {'ok': True, 'id': wid, 'file': file_name, 'noop': True, 'dry_run': dry_run,
                'msg': '同名附件已关联，跳过重复上传'}
    if dry_run:
        return {'ok': True, 'id': wid, 'file': file_name, 'dry_run': True, 'msg': '[dry-run] 将上传附件'}
    with open(file_path, 'rb') as f:
        content = f.read()
    up = '/_apis/wit/attachments?api-version=%s&uploadType=simple&fileName=%s' % (API, urllib.parse.quote(file_name))
    # 上传请求超时后无法知道服务端是否已保存附件；不自动重试，交恢复流程去重处理。
    st, data = wit_http(client, 'POST', up, raw=content, ctype='octet')
    if st not in (200, 201) or not isinstance(data, dict) or 'id' not in data:
        return {'ok': False, 'id': wid, 'file': file_name, 'error': f'upload HTTP {st}: {str(data)[:200]}'}
    att_id = data['id']
    # TFS 返回的 url 是权威地址；少数旧版服务器未返回时才使用已编码的项目 base_url 回退。
    att_url = data.get('url') or f"{client['base_url']}/_apis/wit/attachments/{att_id}?fileName={urllib.parse.quote(file_name)}"
    payload = [{"op": "add", "path": "/relations/-",
                "value": {"rel": "AttachedFile", "url": att_url, "attributes": {"comment": "AI 分析产出"}}}]
    st2, _ = patch_workitem(client, wid, current, payload)
    if st2 != 200:
        return {'ok': False, 'id': wid, 'file': file_name, 'attachment_id': att_id, 'error': f'link HTTP {st2}'}
    return {'ok': True, 'id': wid, 'file': file_name, 'attachment_id': att_id, 'dry_run': False}


def precheck(client):
    """连通性 + 鉴权自检：GET 项目工作项类型。"""
    st, data = wit_retry(client, 'GET', f'/_apis/wit/workitemtypes?api-version={API}', ctype='json')
    if st != 200 or not isinstance(data, dict):
        return {'ok': False, 'project': client['project'], 'error': f'HTTP {st}: {str(data)[:200]}'}
    types = [t.get('name') for t in data.get('value', []) if t.get('name')]
    has_req = '需求' in types
    return {'ok': True, 'project': client['project'], 'workItemTypes': types, 'hasRequirementType': has_req}


def fetch_iteration_tree(client, project):
    """GET 指定项目的迭代分类节点树；返回叶子迭代 [{path, name, start, finish}]。

    base_url 带的是 config.project，这里要查任意 project（取自工作项 teamProject，
    与 config.project 不同），故用 server/port/collection 重拼一个 scoped client，
    复用 wit_retry / auth_header。日期在节点 attributes.startDate/finishDate；
    迭代节点无 path 字段，按 name 递归拼接；只叶子节点带日期。
    """
    scoped = dict(client, base_url=f"http://{client['server']}:{client['port']}/tfs/"
                  f"{urllib.parse.quote(client['collection'])}/{urllib.parse.quote(project)}")
    st, data = wit_retry(scoped, 'GET',
                         f'/_apis/wit/classificationnodes/iterations?api-version={API}&$depth=10',
                         ctype='json')
    if st != 200 or not isinstance(data, dict):
        return {'ok': False, 'project': project, 'error': f'HTTP {st}: {str(data)[:200]}'}
    leaves = []

    def walk(node, prefix):
        name = node.get('name', '')
        full = (prefix + '\\' + name) if prefix else name
        children = node.get('children') or []
        if not children:
            attrs = node.get('attributes') or {}
            leaves.append({'path': full, 'name': name,
                           'start': attrs.get('startDate'), 'finish': attrs.get('finishDate')})
        for child in children:
            walk(child, full)

    walk(data, '')
    return {'ok': True, 'project': project, 'iterations': leaves}


def list_iterations(client, project, expected_date=None, today=None):
    """列迭代；若给 expected_date，额外算两个派生迭代（候选均要求 finishDate ≤ 期望日）：

    - matched：finishDate 最大者（最晚能赶上的迭代）——质控「时效余量」基准（pre-qc-rules §三.3）。
    - earliest：在「提交截止未过」的候选里取 finishDate 最小者——字段流转「排期」取向（field-flow.md）：
      下界 = finishDate − 7 天 ≥ 今天（代码提交截止还没过，排除来不及开发 / 已结束的历史迭代）。
      多个满足条件的迭代时优先把需求排到更早的那个（例：期望 260907，候选 2608.14/2608.28 → 排 2608.14）。
    expected_date 与 today 均按北京时间业务日期解释；today 缺省取当前北京时间，单测可传固定值。
    matched 候选不加下界（取 max 不受历史污染）；
    earliest 无可排候选时为 None（此时 matched 仍可能存在）。
    """
    tree = fetch_iteration_tree(client, project)
    if not tree.get('ok'):
        return tree
    iters = tree['iterations']
    matched = None
    earliest = None
    if expected_date:
        try:
            exp = beijing_date(expected_date)
        except (TypeError, ValueError) as exc:
            return {'ok': False, 'project': project, 'count': len(iters), 'iterations': iters,
                    'matched': None, 'earliest': None, 'error': str(exc)}
        dated_candidates = []
        for iteration in iters:
            finish = iteration.get('finish')
            if not finish:
                continue
            try:
                finish_date = beijing_date(finish)
            except (TypeError, ValueError) as exc:
                return {'ok': False, 'project': project, 'count': len(iters), 'iterations': iters,
                        'matched': None, 'earliest': None,
                        'error': f"迭代 {iteration.get('path') or iteration.get('name')!r} 截止时间无效: {exc}"}
            if finish_date <= exp:
                dated_candidates.append((iteration, finish_date))
        candidates = dated_candidates
        if candidates:
            matched = max(candidates, key=lambda item: item[1])[0]
            today_date = beijing_date(today) if today is not None else beijing_date()
            deadline = today_date + datetime.timedelta(days=7)
            schedulable = [item for item in candidates if item[1] >= deadline]
            if schedulable:
                earliest = min(schedulable, key=lambda item: item[1])[0]
    return {'ok': True, 'project': project, 'count': len(iters),
            'iterations': iters, 'matched': matched, 'earliest': earliest}


# ---------------- audit ----------------
def record(skill, wid, verdict, tags, state_from, state_to, trace, extra, run_id=''):
    wid_seg = str(wid) if wid else '_no_id'
    runs_dir = os.path.join(PROCESS_DIR, wid_seg, 'runs')
    os.makedirs(runs_dir, exist_ok=True)
    ts = beijing_timestamp('%Y%m%d_%H%M%S')
    run_id = run_id or uuid.uuid4().hex
    if not all(c.isalnum() or c in '-_' for c in run_id):
        fail('run_id 只能包含字母、数字、-、_', 'BAD_RUN_ID')
    entry = {
        'ts': ts,
        'run_id': run_id,
        'skill': skill,
        'id': wid,
        'verdict': verdict,
        'tags': tags or [],
        'state_from': state_from,
        'state_to': state_to,
        'trace': trace,
        'extra': extra or {},
    }
    path = os.path.join(runs_dir, f"run_{ts}_{run_id}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)
    return {'ok': True, 'audit': path, 'entry': entry}


# ---------------- CLI ----------------
def parse_value(raw):
    """--value @file.md 读取文件内容；否则按字面值。"""
    if raw.startswith('@'):
        p = raw[1:]
        if not os.path.exists(p):
            fail(f"value 指向的文件不存在: {p}", 'FILE_NOT_FOUND')
        with open(p, 'r', encoding='utf-8') as f:
            return f.read()
    return raw


def main():
    ap = argparse.ArgumentParser(description='auto-req-qc / auto-req-analysis 共享 TFS 客户端')
    ap.add_argument('--config', default=os.path.join(SCRIPT_DIR, 'tfs-config.json'))
    sub = ap.add_subparsers(dest='cmd', required=True)
    # --pat / --collection / --project 经 parents 让每个子命令都带，可放在子命令后；临时覆盖、不写回配置。
    conn_parent = argparse.ArgumentParser(add_help=False)
    conn_parent.add_argument('--pat', default=None, help='临时 PAT（不写回配置）；缺省用 TFS_PAT 环境变量或配置 tfs.pat')
    conn_parent.add_argument('--collection', default=None, help='临时 collection（不写回配置）；缺省用 TFS_COLLECTION 环境变量或配置 tfs.collection')
    conn_parent.add_argument('--project', default=None, help='临时 project（不写回配置）；缺省用 TFS_PROJECT 环境变量或配置 tfs.project')

    sp = sub.add_parser('fetch', parents=[conn_parent]); sp.add_argument('id')
    sp = sub.add_parser('download-attachments', parents=[conn_parent]); sp.add_argument('id'); sp.add_argument('--output-dir', required=True); sp.add_argument('--include-external', action='store_true', help='受控下载详细信息中命中白名单的外部附件链接'); sp.add_argument('--max-bytes', type=int, default=DEFAULT_ATTACHMENT_MAX_BYTES)
    sp = sub.add_parser('add-tag', parents=[conn_parent]); sp.add_argument('id'); sp.add_argument('--tag', required=True); sp.add_argument('--dry-run', action='store_true')
    sp = sub.add_parser('remove-tag', parents=[conn_parent]); sp.add_argument('id'); sp.add_argument('--tag', required=True); sp.add_argument('--dry-run', action='store_true')
    sp = sub.add_parser('set-state', parents=[conn_parent]); sp.add_argument('id'); sp.add_argument('--state', required=True); sp.add_argument('--dry-run', action='store_true')
    sp = sub.add_parser('set-assignee', parents=[conn_parent]); sp.add_argument('id'); sp.add_argument('--assignee', required=True); sp.add_argument('--dry-run', action='store_true')
    sp = sub.add_parser('write-field', parents=[conn_parent]); sp.add_argument('id'); sp.add_argument('--field', required=True); sp.add_argument('--value', required=True); sp.add_argument('--mode', choices=['replace', 'append'], default='replace'); sp.add_argument('--marker', default=''); sp.add_argument('--dry-run', action='store_true')
    sp = sub.add_parser('upload-attachment', parents=[conn_parent]); sp.add_argument('id'); sp.add_argument('--file', required=True); sp.add_argument('--dry-run', action='store_true')
    sub.add_parser('precheck', parents=[conn_parent])
    sp = sub.add_parser('list-iterations', parents=[conn_parent]); sp.add_argument('--expected-date', default=None)
    sp = sub.add_parser('record', parents=[conn_parent]); sp.add_argument('--skill', required=True); sp.add_argument('--id', required=True); sp.add_argument('--verdict', required=True)
    sp.add_argument('--tag', action='append', default=[]); sp.add_argument('--state-from', default=''); sp.add_argument('--state-to', default='')
    sp.add_argument('--trace', default=''); sp.add_argument('--extra', default='{}'); sp.add_argument('--run-id', default='')

    args = ap.parse_args()

    if args.cmd == 'precheck':
        client = load_config(args.config, args.pat, args.collection, args.project)
        emit(precheck(client)); return

    if args.cmd == 'list-iterations':
        if not args.project:
            ap.error('list-iterations 需要 --project（或 TFS_PROJECT 环境变量）')
        client = load_config(args.config, args.pat, args.collection, args.project)
        emit(list_iterations(client, args.project, args.expected_date)); return

    if args.cmd == 'record':
        try:
            extra = json.loads(args.extra)
        except Exception:
            extra = {'_raw_extra': args.extra}
        emit(record(args.skill, args.id, args.verdict, args.tag, args.state_from, args.state_to, args.trace, extra, args.run_id))
        return

    client = load_config(args.config, args.pat, args.collection, args.project)
    wid = int(args.id)

    if args.cmd == 'fetch':
        emit({'ok': True, 'workItem': map_workitem(fetch_raw(client, wid))})
    elif args.cmd == 'download-attachments':
        emit(download_attachments(client, wid, args.output_dir, args.max_bytes, args.include_external))
    elif args.cmd == 'add-tag':
        emit(add_tag(client, wid, args.tag, args.dry_run))
    elif args.cmd == 'remove-tag':
        emit(remove_tag(client, wid, args.tag, args.dry_run))
    elif args.cmd == 'set-state':
        emit(set_state(client, wid, args.state, args.dry_run))
    elif args.cmd == 'set-assignee':
        emit(set_assignee(client, wid, args.assignee, args.dry_run))
    elif args.cmd == 'write-field':
        emit(write_field(client, wid, args.field, parse_value(args.value), args.mode, args.dry_run, args.marker))
    elif args.cmd == 'upload-attachment':
        emit(upload_attachment(client, wid, args.file, args.dry_run))


if __name__ == '__main__':
    main()
