# 附件解析固定镜像与隔离运行时

附件 Python 能力只维护一个直接依赖：

```text
markitdown[xls,xlsx,pdf,docx,pptx]==0.1.7
```

`requirements-attachments.txt` 是人工维护的直接依赖，`requirements-attachments.lock` 是由它生成的完整传递依赖与哈希锁。锁文件条目较多是为了可复现安装和供应链校验，不表示需要人工分别维护这些包。

仅在显式升级版本时由维护者重建锁文件：

```bash
uv pip compile runtime/requirements-attachments.txt --python-version 3.12 \
  --generate-hashes --no-annotate --no-header \
  -o runtime/requirements-attachments.lock
```

正式工作项统一调用 `_lib/tfs/attachment_runtime.py`。运行器按实际附件格式，在 Git 忽略的 `过程文件/.runtime/attachments/<cache-key>/` 创建 Python 3.12.13 隔离环境；只有 `.doc/.xls` 且没有可用 LibreOffice 时，才通过受控 Homebrew 或 apt 命令补齐系统工具。完整安装成功并通过烟测后才原子发布缓存，损坏或不完整缓存不会复用。

解析链如下：

- `.docx/.xlsx/.xls/.pdf/.pptx`：MarkItDown 0.1.7 为主链；DOCX/XLSX 保留标准库兜底，PDF 可使用宿主已有 PyMuPDF 逐文件降级。
- `.doc`：LibreOffice 转 `.docx`，再走 MarkItDown 或内置 DOCX 解析器。
- `.xls`：MarkItDown 失败时，LibreOffice 转 `.xlsx` 后再走 MarkItDown 或内置 XLSX 解析器。
- `.ppt`：仍不支持；不得把旧版 PPT 误报为已解析。

固定镜像仍是最高复现等级；宿主隔离运行时用于环境缺失时的自动修复。两者使用同一哈希锁。

## 固定镜像构建与发布

```bash
cd skills/reg-auto-req-analysis
docker build -f runtime/Dockerfile -t reg-auto-req-analysis-office:0.1.0 .
docker image inspect reg-auto-req-analysis-office:0.1.0 --format '{{.Id}}'
```

发布时记录构建后的 image digest，不允许目标机器改用同名浮动 tag。基础镜像固定为 Docker Official Image `python:3.12.13-slim-bookworm` 的 digest；LibreOffice Writer/Calc 固定为 Debian bookworm 包版本。Python wheel 哈希、最终 Python 包和系统包清单保存在镜像 `/opt/runtime-manifest/`。

## 固定镜像运行

```bash
docker run --rm --network none reg-auto-req-analysis-office@sha256:<发布摘要> --precheck
```

```bash
docker run --rm --network none \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  -v /绝对路径/附件:/work/input:ro \
  -v /绝对路径/附件解析:/work/output:rw \
  reg-auto-req-analysis-office@sha256:<发布摘要> \
  --input-dir /work/input --output-dir /work/output
```

镜像不包含 TFS PAT 或 `tfs-config.json`。MarkItDown 关闭插件和云端转换，只接收本地文件；LibreOffice 使用隔离的临时用户配置。

## 宿主隔离运行时

```bash
python3 skills/reg-auto-req-analysis/_lib/tfs/attachment_runtime.py convert \
  --input-dir 过程文件/<id>/<run_id>/附件 \
  --output-dir 过程文件/<id>/<run_id>/附件解析
```

输出中的 `preflight` 记录实际格式、能力、安装组、包管理器、固定包名、开始和结束时间、结果、运行时目录、缓存键、阻断格式和脱敏警告。安装失败只影响依赖该能力的文件，其它格式继续处理。模型和 RUNBOOK 其它步骤仍禁止自行执行任意安装命令。

## 验收

固定镜像构建日志必须包含 `--precheck` 和 `office-runtime-smoke: OK`。宿主运行时必须显示 MarkItDown 版本 `0.1.7`，相同 cache key 的第二次运行不得重复安装。`.doc` 验收必须出现 LibreOffice 转换链；`.pptx` 验收必须出现 MarkItDown 转换链。
