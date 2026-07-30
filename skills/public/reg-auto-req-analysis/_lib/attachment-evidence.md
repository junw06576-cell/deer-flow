# 工作项附件证据处理规范

适用于 `auto-req-analysis` 的质控与分析两阶段。附件是工作项字段的**辅助证据**：已成功提取的内容必须参与质控或分析；未能读取的附件必须显式记录，不能假装已分析，也不会单独改变既有 TFS 状态机。

## 1. 下载范围与目录

在生成 `run_id` 并创建本次 `过程文件/<id>/<run_id>/` 后，先执行：

```bash
# skill 始终带 --include-external，以一并提取白名单外链与内嵌截图；完整签名见 _lib/COMMANDS.md
python3 skills/reg-auto-req-analysis/_lib/tfs/tfs_client.py download-attachments <id> \
  --output-dir 过程文件/<id>/<run_id>/附件 --include-external
```

- 代码缺省（不加 `--include-external`）只下载 TFS 工作项 relations 中的 `AttachedFile`，只接受当前 TFS collection 的 URL；**skill 始终带 `--include-external`** 以读取详细信息中的 ERP 样例链接与**内嵌截图**。它同时提取 `<a href>` 外链与 `<img src>` 内嵌图片（典型如 `assist.winning.com.cn` 公开图片 CDN——非 AttachedFile、也非锚点链接，缺省会漏抓），仅允许 `tfs-config.json` 的 `external_attachments.allowed_hosts` 中的 HTTPS 主机（支持精确主机与 `*.suffix` 通配符，如 `*.winning.com.cn` 一次覆盖 `assist.winning.com.cn`、`weberp.winning.com.cn` 等所有同后缀子域；不匹配裸域或近似域），逐跳校验重定向，认证仅从其专用环境变量读取，绝不复用 TFS PAT。ERP 链接若先返回附件列表页，只提取其中同主机 `/attachment/download/` 的实际下载链接，列表页不作为来源附件保存；`<img src>` 单文件图片不二次解析、直接保存。若该已白名单主机无法经环境代理访问，可显式设 `bypass_proxy=true`，该设置只作用于外链请求。单文件默认上限 20 MiB，并输出文件名、SHA-256、大小、类型、跳过项和错误。
- 不得把来源附件上传回 TFS，也不得把来源附件列为执行计划的 `artifacts`；`artifacts` 仍只描述本 skill 的交付物。
- 未加 `--include-external`、未启用外链下载、认证缺失、主机不在白名单或重定向越界时，描述字段中的 ERP/外部附件链接登记为 `skipped`；不得用未成功下载的外链替代 TFS 附件或把其内容当作已读。

## 2. 提取与阅读

在 `附件解析/` 下保存可读副本，文件名保留原附件名并加 `.md`。按下列顺序处理：

1. `.md`、`.txt`、`.json`、`.yaml`、`.yml`、`.csv`、`.xml`、`.html`、`.log`：直接读取原文；HTML 先忽略脚本和样式噪音。
2. 先执行内置转换器：`python3 skills/reg-auto-req-analysis/_lib/tfs/attachment_converter.py --input-dir 过程文件/<id>/<run_id>/附件 --output-dir 过程文件/<id>/<run_id>/附件解析`。它核心只依赖 Python 标准库，转换 `.md`、`.txt`、`.json`、`.yaml`、`.yml`、`.csv`、`.xml`、`.html`、`.log`、`.docx`、`.xlsx`；`.pdf` 在本机装有 PyMuPDF(`fitz`) 时提取文本，未安装则降级 `skipped`；再读取转换结果。
3. `.png`、`.jpg`、`.jpeg`、`.webp`：内置转换器标为 `needs_read`，再用图像查看能力读取；在本次分析摘要中记录可观察到的页面、字段、流程或接口信息。看不清则记 `unparsed`。
4. `.pdf`：内置转换器在装有 PyMuPDF(`fitz`) 时提取文本，成功记 `parsed`；未安装、加密无密码或扫描件/图片型 PDF 记 `skipped`（`reason` 注明），不得凭文件名推断内容。
5. `.doc`、`.xls`、`.ppt`、`.pptx`、压缩包、可执行文件、受密码保护文件、超限文件或未知格式：内置转换器不依赖系统命令或外部 skill，标为 `skipped` 或 `unparsed` 与原因；不得凭文件名推断内容。

每个来源文件只可处于 `parsed`、`unparsed` 或 `skipped` 之一。只有 `parsed` 可成为需求结论的依据；图片成功查看同样记为 `parsed`，并写明“人工视觉读取”。

内置转换器返回的 `converted` 只有在已读取生成的 `.md` 后才映射为计划中的 `parsed`；`needs_read` 经成功图像查看后映射为 `parsed`，否则为 `unparsed`；`error` 映射为 `unparsed`，`unsupported`/`skipped` 映射为 `skipped`。

## 3. 使用边界与审计

计划 JSON 必须包含可选顶层 `attachments`：

```json
{
  "ready": true,
  "download_dir": "附件",
  "downloaded": [{"name": "接口说明.docx", "path": "附件/接口说明.docx", "sha256": "...", "size": 1234, "content_type": "..."}],
  "parsed": [{"name": "接口说明.docx", "status": "parsed", "output": "附件解析/接口说明.docx.md", "note": "提取到入参、出参和回调规则"}],
  "skipped": [],
  "errors": []
}
```

- `ready=true` 仅表示所有已下载附件均已解析或没有附件；任一未解析、跳过或下载错误即为 `false`。
- 质控：把附件中已证实的验收、范围、接口、数据对象写入现有核对结论；缺少且附件无法读取时，按既有完整性规则决定是否 `NEED-INFO`，不新增附件专属终局。
- 分析：把已证实的业务规则、接口契约、流程和验收条件融入变更方案；未解析附件作为 §六假设/限制或人工复核依据，不编造技术细节。
- 计划的 `attachments` 由执行器透传至 `过程文件/<id>/runs/` 审计；不触发状态流转、标签变更或额外 TFS 写入。
