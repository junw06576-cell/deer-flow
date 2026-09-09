# reg-auto-req-analysis-offline 线上化改造方案
# （附件读 uploads / 产物写 outputs）

> 版本：2026-09-03 · 状态：方案评审稿（未动任何代码）
> 依据：本地代码实证（skills/public/reg-auto-req-analysis-offline/ + backend/ 源码 + config.yaml），非推测。

---

## 0. 结论先行（TL;DR）

1. **附件输入**：废除"载荷内联 content+sha256"，改为调用方先经上传接口把附件文件放进线程的 `user-data/uploads/`，skill 在沙箱内经 `/mnt/user-data/uploads/` 按载荷清单对账后读取。
2. **产物输出**：三产物从"本地 `过程文件/<id>/<run_id>/`"改为写入 `/mnt/user-data/outputs/过程文件/<run_id>/`，写完调用内置 `present_files` 工具，前端即可展示文件卡片并下载。
3. **路径策略**：skill 内一律使用线程相对路径——沙箱内线程根为 `/mnt/user-data`，产物锚 `outputs/`，附件锚 `uploads/`；不出现任何宿主机路径。
4. **归档保底（推荐保留）**：产物写完后再 `cp` 一份到 `/mnt/skills/过程文件/`（现有 config.yaml 已将其配置为可写子挂载，对应宿主机 `/opt/deer-flow/auto-dev-work/过程文件`），避免删线程时产物蒸发。
5. 改造集中在 4 个文件：`SKILL.md`、`_lib/input-contract.md`、`_lib/deliverables-contract.md`、（可选增强）`scripts/check_deliverables.py`；另建议 agent 配置的 `tool_groups` 增补文件读写组。

---

## 1. 本地智能体 vs DeerFlow：机制差异

| 维度 | 本地智能体（WorkBuddy / Claude Code） | DeerFlow（192 服务器） |
|---|---|---|
| 执行位置 | 直接跑在用户机器，与用户同一文件系统 | LangGraph run；工具调用进 AioSandbox 容器（`config.yaml:127`），沙箱内只见虚拟路径 `/mnt/*` |
| 线程目录概念 | 无 | 每 run 一个线程目录：宿主机 `{DEER_FLOW_HOME}/users/{owner}/threads/{thread_id}/`，其中 `user-data/` 整体挂进沙箱为 `/mnt/user-data/`（`aio_sandbox_provider.py:322-340`） |
| 工作目录 | 用户指定 cwd，任意可写 | 沙箱固定三区：`/mnt/user-data/{workspace,uploads,outputs}`（`paths.py:106-109`）；bash 命令自动 `cd /mnt/user-data/workspace`（`sandbox/tools.py:1755`） |
| 附件怎么进来 | 载荷 JSON 内联 `content`（≤20MiB markdown） | 调用方 `POST /api/threads/{tid}/uploads`（`uploads.py:299`，thread 不存在也能传，`require_existing=False`），文件落线程 uploads 目录；沙箱内 `/mnt/user-data/uploads/<file>` 可读 |
| 附件感知 | 靠载荷自带 | UploadsMiddleware 每次 run 前自动注入 `<uploaded_files>` 清单（含历史残留文件） |
| 产物怎么出去 | 写本地 `过程文件/`，调用方自己找 | 写 `/mnt/user-data/outputs/`，调 `present_files` 工具登记进 thread state（`artifacts` 字段，`merge_artifacts` 去重累积，`thread_state.py:58-65`）；前端经 `/api/threads/{tid}/artifacts/mnt/user-data/outputs/<file>` 访问，`?download=true` 强制下载（`artifacts.py:159-190`） |
| skill 如何加载 | IDE 发现 bundle，相对链接可直接点开 | SKILL.md **全文注入** SystemMessage（`subagents/executor.py:610-620`）；引用文件须由模型用工具去 `/mnt/skills/public/<skill>/...` 显式读取（skills 挂载只读，`sandbox/tools.py:855-859`） |
| 交互 | `AskUserQuestion` 默认开 | TFS-BUDDY 内部调用恒 `non_interactive`，提问工具在内部调用路径被排除 |
| 路径可见性 | 真实路径 | 宿主机路径对沙箱不可见，且工具输出会把真实路径回写成虚拟路径（mask） |

**一句话**：本地智能体里"路径=用户文件系统"，DeerFlow 里"路径=线程沙箱契约"。这个 skill 的所有路径假设都建立在前者上，这是要改造的根因。

---

## 2. 现状与线上不通之处（实证）

### 2.1 skill 现状的路径假设

- `SKILL.md` 硬约束 2：产物写 `过程文件/<id>/<run_id>/`，禁止临时脚本生成。——改后：`过程文件/<run_id>/`
- `SKILL.md` 核心流程 1：**workspace 解析 = "从 CWD 逐级向上找含 `过程文件/` 的目录，兜底 bundle 上两级"**（本地 auto-dev-work 布局）。
- `_lib/input-contract.md` 模型操作步骤 1-2：载荷/信封原样写 `输入载荷_*` / `调用入参_*` 于 `过程文件/<run_id>/`；附件 `content` 校验 sha256 后写 `附件解析/<安全化文件名>.md`。
- `_lib/deliverables-contract.md` §1：目录 = `<workspace 根>/过程文件/<run_id>/`。
- 附件输入：载荷 `attachments[]` 内联 `content/content_type(text/markdown)/sha256`，20MiB 上限。

### 2.2 在线上为什么不成立

1. **workspace 解析必然失败**：沙箱 CWD 是 `/mnt/user-data/workspace`，逐级向上是 `/mnt/user-data`、`/mnt`，均无 `过程文件/`；兜底 bundle 上两级 = `/mnt/skills`（**只读挂载**，`sandbox/tools.py:855-859`）——产物会试图写进只读区，直接失败。
2. **现有 workaround 不在线程目录**：当前 192 服务器靠 config.yaml 两条自定义挂载绕过（`config.yaml:140-149`）：
   - `/opt/deer-flow/auto-dev-work/过程文件` → `/mnt/skills/过程文件`（可写子挂载，覆盖只读父挂载）；
   - `/opt/deer-flow/auto-dev-work` → `/mnt/user-data/workspace/auto-dev-work`（把 agent 自建产物收口）。
   两条都指向**宿主机统一目录**，不在线程目录里 → 前端文件卡片（thread artifacts 机制）完全看不到，下载只能上服务器拷。
3. **附件内联的隐患**：流式信封 `requirementPayload` 把附件全文塞进 prompt 输入，大附件挤占上下文；且真实"上传"通道（uploads + artifact_url）闲置。
4. **`present_files` 通道闲置**：`present_files` 是内置工具（`tools/tools.py:15-19`，`tool_groups: [bash]` 不过滤内置工具），但它只接受 `/mnt/user-data/outputs/` 下的文件（`present_file_tool.py:33-78`）——产物不写 outputs 就永远用不上。

---

## 3. 改造方案

### 3.1 附件输入侧：改从 `/mnt/user-data/uploads/` 读取

**新输入契约（载荷 `attachments[]`）**：

```json
"attachments": [{
  "filename": "值域字典接口规范.md",
  "sha256": "<uploads 目录中该文件 UTF-8 字节的 SHA-256>",
  "original_url": "http://tfs/_apis/wit/attachments/xxx"   // 可选，仅审计
}]
```

- 删除 `content`、`content_type` 两键（uploads 目录即文件本体；`original_url` 保留为审计痕迹）。
- `filename` 必须与 uploads 目录内的实际文件名一致（注意：上传接口会对文件名做安全化/去重改名，调用方需以实际上传结果为准回填 filename）。
- `sha256` 的作用：**文件完整性校验**。附件上传到 `uploads/` 目录后、run 开始前存在时间窗口，无法保证文件不被篡改（残留、冲突、静默损坏）。skill 在沙箱内对账时 `sha256sum` 比对，摘要不一致则报 `UPLOAD_ATTACHMENT_DIGEST_MISMATCH` 停止，不拿脏数据做分析。调用方对文件负责——上传完成后、触发 run 前对文件内容算 sha256 塞进载荷；skill 不信任任何人，只信任载荷摘要。

**调用链时序（TFS-BUDDY / 前端用户两条路径一致）**：

```
① POST /api/threads/{thread_id}/uploads        （multipart；内部 token + owner 头）
   thread_id = {collection}-{workItemId}，如 WN_Data_Platform-242042
   ← 响应给出安全化后的 filename / virtual_path / artifact_url
② 触发 run（POST /threads/{tid}/runs/stream，载荷 attachments[] 回填①的文件名与 sha256）
③ skill 在沙箱内对账读取
```

**skill 侧对账规则（新增，失败关闭）**：

1. `ls /mnt/user-data/uploads/` 列目录；
2. 载荷声明的每个附件必须存在，否则报 `UPLOAD_ATTACHMENT_MISSING`，停止；
3. 逐个 `sha256sum` 对账，不符报 `UPLOAD_ATTACHMENT_DIGEST_MISMATCH`，停止；
4. 校验通过后把附件内容**原样**写 `outputs/过程文件/<run_id>/附件解析/<安全化文件名>.md` 留痕；
5. **白名单防串台**：uploads 目录里存在、但载荷未声明的文件一律视为噪音忽略，绝不读取（同一 thread 重分析时上一轮附件会残留，且 UploadsMiddleware 注入的清单同样可能含历史文件——只认载荷声明，防旧附件污染本轮证据）。
6. sha256 口径：对**实际上传文件**计算（即调用方转写后的 markdown），不是 TFS 原始 docx——由调用方在生成载荷时计算，skill 只做对账。**skill 不信任任何人，只信任载荷摘要；对账失败则文件已被篡改，拒绝读取。**

**附件格式**：维持"调用方先行转写为 markdown"的既有约定（上传接口的 Office 自动转换 `auto_convert_documents` 当前为 `false`，`config.yaml:124`；如想服务端转换可显式开启，但会在网关主机解析不受信文档，属既有安全权衡，本方案不动）。

### 3.2 产物输出侧：改写 `/mnt/user-data/outputs/`

**新目录契约（线程相对路径）**：

```
/mnt/user-data/                                    ← 沙箱内线程根
├── uploads/                                       ← 调用方上传附件（只读对账源）
└── outputs/                                       ← skill 全部产物（前端文件卡片源）
    └── 过程文件/<run_id>/
        ├── 输入载荷_<run_id>.json
        ├── 调用入参_<run_id>.json            （流式信封时）
        ├── 附件解析/<安全化文件名>.md
        ├── 需求分析报告_<run_id>.md           （分析终局）
        ├── Redis投影_<run_id>.json            （所有终局）
        └── 待确认清单_<run_id>.md             （QC 终局）
```

- 保留 `过程文件/<run_id>/` 层级：run_id 天然隔离多轮分析；与宿主机归档目录结构同名对应，归档动作退化为纯目录复制。
- run_id 规则、运行标记、终局→产物映射、投影 schema（`offline-projection-v1`，`actionable` 恒 `"false"`）**全部不变**。

**交付动作（写完产物后新增两步）**：

1. **present_files 登记卡片**：按终局调用 `present_files`（filepaths 用虚拟绝对路径，如 `/mnt/user-data/outputs/过程文件/<run_id>/需求分析报告_<run_id>.md`）。投影恒登记；报告/清单按终局登记。该工具只接受 outputs 下的文件，子目录合法（`present_file_tool.py:75-80`）。前端随之可展示卡片、在线预览、`?download=true` 下载。
2. **宿主机归档（推荐，保底）**：

```bash
mkdir -p /mnt/skills/过程文件/<run_id>/ && \
cp -r /mnt/user-data/outputs/过程文件/<run_id>/ /mnt/skills/过程文件/<run_id>/
```

   `/mnt/skills/过程文件` 已是现成的可写子挂载（`config.yaml:140-144`，宿主机 `/opt/deer-flow/auto-dev-work/过程文件`），零配置改动。价值：**删线程目录（`DELETE /api/threads/{tid}` 会 rmtree 整个 thread 目录）时产物不蒸发**，且保留现有下游（人肉上服务器取报告）的习惯。用 `cp` 归档而非模型重写，内容零漂移。

### 3.3 路径策略：线程相对路径规范

- **唯一锚点**：`/mnt/user-data`（沙箱视角的线程根）。产物一律 `/mnt/user-data/outputs/...`，附件一律 `/mnt/user-data/uploads/...`。
- **禁止出现**：宿主机真实路径（`/opt/deer-flow`、`users/tfs-buddy/threads/...`）、skill bundle 相对路径解析（"CWD 逐级向上找 `过程文件/`"整套逻辑删除）、`/mnt/skills/过程文件` 作为**主**产物路径（仅作归档 cp 目标）。
- bash 命令内可用相对路径（CWD 已锚 `/mnt/user-data/workspace`）：如 `../outputs/过程文件/.../`，与 lead agent 既有提示一致（`lead_agent/prompt.py:627-628`）。
- skill 引用文件（`_lib/`、`references/` 等）读取路径：`/mnt/skills/public/reg-auto-req-analysis-offline/<相对路径>`——SKILL.md 应显式给出 bundle 根的虚拟路径，替代"相对链接"写法。

### 3.4 文件级改动清单

| 文件 | 改动 |
|---|---|
| `SKILL.md` | ① 硬约束 2 路径改为 `/mnt/user-data/outputs/过程文件/<run_id>/`；② 核心流程 1 删除 workspace 解析，改固定路径 + 附件 uploads 对账；③ 核心流程 8 增加 present_files + 归档 cp；④ 「不做什么」增加"不写 `/mnt/skills`（归档 cp 除外）、不读 uploads 未声明文件"；⑤ 描述改为 DeerFlow 沙箱契约；⑥ 增加环境自检：无 `/mnt/user-data/outputs` 即失败关闭（本地模式不再直接可用） |
| `_lib/input-contract.md` | ① `attachments[]` 契约改三键（filename/sha256/original_url）；② 校验步骤改为"uploads 对账三查"（存在性→sha256→白名单）；③ 模型操作步骤 1-2 的落盘路径改 outputs；④ 新增错误码 `UPLOAD_ATTACHMENT_MISSING`、`UPLOAD_ATTACHMENT_DIGEST_MISMATCH`；⑤ 删除内联体积上限条款（20MiB/64MiB 针对内联，uploads 通道由上传接口的 max_file_size 50MiB/单次 100MiB 约束）；⑥ 文件名模板去掉 `<id>` 前缀，`<run_id>` 作为唯一标识 |
| `_lib/deliverables-contract.md` | §1 目录约定改 outputs 路径：`过程文件/<run_id>/`；§5 自检命令改 `python3 /mnt/skills/public/reg-auto-req-analysis-offline/scripts/check_deliverables.py /mnt/user-data/outputs/过程文件/<run_id>/`；新增"交付动作"节（present_files 清单 + 归档 cp） |
| `scripts/check_deliverables.py` | **零改动可跑**（run 目录由 argv 传入，无内嵌路径假设）。可选增强：`--uploads-dir` 参数做附件对账复核 |
| `agents/auto-analysis-agent-test/config.yaml`（建议项） | `tool_groups: [bash]` → `[bash, file:read, file:write]`：读附件用 `read_file`（有 50000 chars 分页，比 `cat` 稳）、写产物用 `write_file`（比 bash heredoc 少转义坑）。改动后需 `docker restart deer-flow-gateway`（agent config 有进程内缓存） |

**不动**：全部业务规则文件（`config/`、`references/`）、两阶段流程、终局判定、投影 JSON 契约、`check_deliverables.py` 校验逻辑。

### 3.5 生命周期与边界（改完后的行为变化）

| 场景 | 行为 |
|---|---|
| 前端文件卡片 | run 结束即见（artifacts state 经 stream/GET state 暴露）；同 thread 多轮分析**卡片累积**（`merge_artifacts` 只增不减，按路径去重）——旧轮产物在旧 run_id 子目录，不冲突，但 UI 会越滚越多 |
| 删除线程 | outputs 产物随 thread 目录 rmtree **一起删掉**（与现状"删 thread 删不掉过程文件"相反）——归档 cp 是保底 |
| 重分析（thread 复用） | uploads/outputs 均残留上轮文件：附件靠"载荷白名单"防污染；产物靠 run_id 子目录天然隔离 |
| 上下文 | 产物在线程目录内，随 checkpoint 复用不受影响；沙箱销毁（idle_timeout 60s）产物不丢（bind mount 持久） |

---

## 4. 风险与前置确认项

1. **沙箱镜像需有 `python3`**：`check_deliverables.py` 在沙箱内跑（纯标准库）。生产版 skill 的执行器跑在 Gateway 容器内而非沙箱，沙箱镜像是否有 python3 未验证——**上线前先在沙箱 bash 里 `python3 --version` 确认**；若没有，备选：自检降级为模型自查，或调用方在宿主机跑自检。
2. **大报告写入方式**：只有 bash（heredoc）时，长 markdown 单命令写入有转义与超时（600s）风险——建议采纳 3.4 的 `file:write` 工具组扩充。
3. **调用方改造依赖**：TFS-BUDDY 需增加"先上传附件再触发 run"两步逻辑（含回填安全化文件名与 sha256）；这是 skill 外的链路改动，需排期。
4. **本地模式失效**：改造后 skill 为 DeerFlow 沙箱专属（依赖 `/mnt/user-data`），本地 Claude/Codex 直接调用会失败关闭——如需双栖，另行评估，本方案不覆盖。
5. **前端展示产物路径含中文与子目录**：artifacts 路由对子目录与中文文件名已做 RFC 5987 编码处理（`artifacts.py:30-33`），理论无碍，建议上线后用一份带附件的完整 run 实测卡片下载。

---

## 5. 改造后的一次完整执行流（预演）

```
调用方: POST /api/threads/WN_Data_Platform-242042/uploads   (attachment.md)
调用方: POST /api/threads/WN_Data_Platform-242042/runs/stream  (信封含附件三键清单)

沙箱内 skill:
  0. 读契约: /mnt/skills/public/reg-auto-req-analysis-offline/_lib/{input,deliverables}-contract.md
  1. 建 run: outputs/过程文件/run_20260903_102000_ab12cd/
  2. 落审计: 输入载荷_<run_id>.json / 调用入参_<run_id>.json → outputs run 目录
  3. 附件对账: ls uploads → sha256sum 校验 → 原样写 附件解析/
  4. QC → 分析 → 写 投影/报告/清单（按终局映射）→ outputs run 目录
  5. 自检: python3 /mnt/skills/.../check_deliverables.py /mnt/user-data/outputs/过程文件/<run_id>/
  6. present_files([...按终局登记的产物虚拟路径...])
  7. 归档: cp -r outputs run 目录 → /mnt/skills/过程文件/<run_id>/
  8. 会话总结（终局/产物路径/缺口/下一步）

前端: 文件卡片（预览 + 下载）                    宿主机: /opt/deer-flow/auto-dev-work/过程文件/<run_id>/（归档）
```

---

## 6. TFS-BUDDY 传给 Agent 的最终参数结构

> 本节约据：`tfs-server/backend/src/services/gatewayStreamService.js`、`requirementPayloadService.js`，代码实证，非推测。

### 6.1 完整调用链路

```
前端 StreamAnalysisModal.jsx
  → POST /api/ai/analysis/stream (aiStream.js 路由)
    → buildRequirementPayload() (requirementPayloadService.js)     ← 组装需求数据
    → streamAnalysis() (gatewayStreamService.js)                   ← 构造 run payload

      → POST /threads                    创建 Thread
      → POST /threads/{tid}/uploads      上传附件（multipart）
      → POST /threads/{tid}/runs/stream  发起流式分析（最终 run payload）
```

### 6.2 最终传给 Gateway 的 run payload

位置：`gatewayStreamService.js:352-360`

```json
{
  "input": {
    "messages": [
      {
        "role": "user",
        "content": "<三段式组合字符串，见 6.3>"
      }
    ]
  },
  "context": {
    "agent_name": "auto-analysis-agent",
    "thread_id": "WN_Data_Platform-234883",
    "non_interactive": true,
    "secrets": {
      "TFS_PROJECT": "RDA-03-数据集成管理软件"
    }
  },
  "config": {
    "context": {
      "agent_name": "auto-analysis-agent",
      "thread_id": "WN_Data_Platform-234883",
      "non_interactive": true,
      "secrets": {
        "TFS_PROJECT": "RDA-03-数据集成管理软件"
      }
    },
    "recursion_limit": 500
  },
  "stream_mode": ["values", "messages-tuple"],
  "stream_subgraphs": true,
  "on_disconnect": "cancel"
}
```

### 6.3 input.messages[0].content 的构造方式

`content` 不是纯 JSON，而是**三段式指令 + 嵌入 JSON 块**的组合字符串（`gatewayStreamService.js:162-180`）：

```
① 用户补充指令（可选，前端输入框内容）
② SKILL 执行指令：告知必须按契约执行、禁止回写 TFS 等
③ 附件说明：告知附件已传到 /mnt/user-data/uploads/，从 <uploaded_files> 清单按文件名匹配读取
④ 嵌入 JSON 块：\`\`\`json\n{requirementPayload}\n\`\`\`
```

### 6.4 嵌入的 requirementPayload JSON 结构

位置：`requirementPayloadService.js:218-267`

这是 skill 在沙箱内实际解析的结构化数据，嵌入在 `content` 的 JSON 块中：

```json
{
  "work_item": {
    "id": 234883,
    "rev": 2,
    "title": "需求标题",
    "state": "活动",
    "description": "<div>需求描述 HTML 原文</div>",
    "analyzer_desc": "从【分析者描述】标记段提取的纯文本",
    "tags": "PM-AI-MANUAL-REVIEW; 标签1",
    "team_project": "RDA-03-数据集成管理软件",
    "area": "区域路径\\子区域",
    "requirement_type": "需求",
    "priority": "2-重要",
    "expected_date": "2026-09-30",
    "assigned_to": "张三",
    "iteration_path": "项目全名\\迭代路径\\迭代名称"
  },
  "attachments": [
    {
      "filename": "需求文档.md",
      "original_url": "https://tfs.xxx.com/.../v1/AttachedFile/123"
    }
  ],
  "iterations": [
    {
      "name": "Sprint 30",
      "start_date": "2026-09-01",
      "finish_date": "2026-09-30"
    }
  ],
  "context": {
    "collection": "WN_Data_Platform",
    "project": "RDA-03-数据集成管理软件",
    "non_interactive": true
  }
}
```

### 6.5 附件上传与对账机制（关键设计）

**附件正文不出现在 payload JSON 中**，而是通过独立的 uploads 接口上传：

```
步骤                          payload 中的体现
─────────────────────────────────────────────────────────────────
① POST /threads/{tid}/uploads    → 不出现（multipart 独立上传）
  （每个附件作为 files 字段）     → 文件落盘到 uploads 目录
② 载荷 requirementPayload        → attachments[].filename（指针）
   attachments                   → attachments[].original_url（指针）
③ skill 在沙箱内                → 从 /mnt/user-data/uploads/ 读文件
   ls uploads → sha256sum 对账  → 按载荷 filename 匹配
```

**关键约束**：
- payload 中 `attachments[]` 只保留 `filename` + `original_url`，**无 `content`/`sha256` 键**
- 当前 TFS-BUDDY 版本**不计算 sha256**（`requirementPayloadService.js` 未做文件摘要）。方案中的 sha256 对账为新设计，需 TFS-BUDDY 侧增加上传后计算 sha256 并回填载荷的逻辑
- 附件文件名与 uploads 目录文件必须一致：二进制提取文本后重命名为 `.md` 扩展名，如 `接口规范.docx` → `接口规范.md`
- 前端/后端附件转换：文本类直读，二进制类（docx/xlsx/pdf）调用后端提取接口转文本后上传，单附件截断 100,000 字符

### 6.6 Thread 创建参数

位置：`gatewayStreamService.js:64-94`

```
POST /threads
Body: {
  "thread_id": "WN_Data_Platform-234883",      // {collection}-{workItemId}
  "assistant_id": "lead_agent",
  "metadata": {
    "source": "tfs-buddy",
    "collection_name": "WN_Data_Platform",
    "work_item_id": 234883
  }
}
```

### 6.7 字段映射速查

| 最终 run payload 字段 | 含义 | 来源 |
|---|---|---|
| `input.messages[0].content` | 指令 + 嵌入 JSON | `gatewayStreamService.js` buildInputMessage() |
| `context.agent_name` | Agent 名称，默认 `auto-analysis-agent` | `gatewayStreamService.js:38` |
| `context.thread_id` | `{collection}-{workItemId}` | `gatewayStreamService.js:309` |
| `context.non_interactive` | 固定 `true` | `gatewayStreamService.js:346` |
| `context.secrets.TFS_PROJECT` | 项目名 | `gatewayStreamService.js:348-349` |
| `work_item.*` | 需求字段，来自 `stats_requirements` 表 | `requirementPayloadService.js` mapRequirementRow() |
| `attachments[].filename` | 附件文件名（已上传到 uploads 目录） | `requirementPayloadService.js:252` |
| `attachments[].original_url` | 附件 TFS URL | `requirementPayloadService.js:252` |
| `iterations[].name/*` | 迭代信息 | `requirementPayloadService.js` buildIterations() |
| `context.collection/project` | TFS 集合和项目 | `requirementPayloadService.js:260-262` |
