# DeerFlow 2.0 需求分析单 Skill + 服务封装方案

## 目录

1. [总体架构](#1-总体架构)
2. [Skill 契约](#2-skill-契约)
3. [Redis 数据契约](#3-redis-数据契约)
4. [FastAPI 服务封装层](#4-fastapi-服务封装层)
5. [TFS-BUDDY 调用流程](#5-tfs-buddy-调用流程)
6. [API 接口规范](#6-api-接口规范)
7. [部署配置](#7-部署配置)
8. [实施步骤](#8-实施步骤)
9. [生产环境改造](#9-生产环境改造)
10. [附录：安全边界](#10-附录安全边界)

---

## 1. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│                       TFS-BUDDY                              │
│                                                              │
│  1. 提交需求质控任务                                          │
│  2. 轮询任务状态（completed 后直接拿到 checklist）               │
└──────────────────────────┬───────────────────────────────────┘
                           │ REST API
┌──────────────────────────▼───────────────────────────────────┐
│                    FastAPI 服务封装层                          │
│                                                              │
│  POST /api/v1/analysis                                     │
│    - 提交 DeerFlow 执行任务，返回 task_id                       │
│                                                              │
│  GET /api/v1/analysis/{task_id}                             │
│    - 查询任务状态；completed 时从 Redis 读取并嵌入 checklist     │
└──────────────────────────┬───────────────────────────────────┘
                           │ DeerFlow LangGraph API
┌──────────────────────────▼───────────────────────────────────┐
│                    DeerFlow 2.0 Agent                         │
│                                                              │
│  Agent: req-analysis                                          │
│  Skill: auto-req-analysis                                     │
│                                                              │
│  auto-req-analysis 完成：                                      │
│    - TFS 工作项读取                                            │
│    - 附件解析                                                  │
│    - 知识库/代码图谱查重                                       │
│    - 质控问题生成                                              │
│    - 将完整结果写入 Redis                                      │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                         Redis                                │
│                                                              │
│  key: auto-req:qc:plan:{collection}:{work_item_id}             │
│  value: 完整 JSON 结果；FastAPI 查询接口读取 checklist 节点       │
└──────────────────────────────────────────────────────────────┘
```

**核心设计原则：**

- **一个 Agent，一个 Skill**：`req-analysis` Agent 下挂 `auto-req-analysis`，由该 Skill 统一完成需求质控。
- **Skill 负责质控结果落 Redis**：需求评估结果以 Redis 中的结构化 JSON 为准，`auto-req-analysis` 写入完整质控结果。
- **FastAPI 只做调度和查询**：提交接口负责触发 DeerFlow；查询接口从 Redis 读取指定 key，取其中 `checklist` 节点返回给 TFS-BUDDY。
- **thread_id 仍由 TFS-BUDDY 上下文构造**：建议保持 `thread_id = "{collection_name}-{work_item_id}"`，便于 DeerFlow 侧对同一工作项复用上下文。
- **TFS 参数不硬编码**：`collection_name`、`work_item_id`、`tfs_project`、`tfs_pat` 仍由 TFS-BUDDY 每次请求传入；部署级固定配置只放服务地址、Redis 地址等。
- **异步 + 轮询**：Skill 执行可能较久，FastAPI 提交后立即返回 `task_id`；TFS-BUDDY 轮询任务完成时，`GET /api/v1/analysis/{task_id}` 自动从 Redis 读取并嵌入 checklist，无需额外请求。
- **单一查询接口**：任务状态查询与 checklist 获取合二为一——`completed` 状态响应附带完整 checklist 数据。

---

## 2. Skill 契约

### 2.1 Skill 名称

`auto-req-analysis`

### 2.2 Skill 职责

`auto-req-analysis` 是需求自动质控 Skill，负责一次性完成需求早筛和补充问题生成：

1. 根据 `collection_name`、`work_item_id`、`tfs_project`、`tfs_pat` 获取 TFS 工作项。
2. 下载并解析工作项附件。
3. 根据内置质控规则、知识库、代码图谱等上下文判断需求是否需要补充信息。
4. 生成完整 JSON 结果。
5. 将完整结果写入 Redis：

```text
auto-req:qc:plan:{collection}:{work_item_id}
```

示例：

```text
auto-req:qc:plan:WN_Data_Platform:258288
```

### 2.3 Skill 输入

FastAPI 调用 DeerFlow Agent 时，message 使用 JSON 字符串，建议结构如下：

```json
{
  "action": "auto_req_analysis",
  "collection_name": "WN_Data_Platform",
  "work_item_id": 258288,
  "tfs_project": "NETHIS5.5",
  "tfs_pat": "******",
  "redis_key": "auto-req:qc:plan:WN_Data_Platform:258288"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `action` | 是 | 固定为 `auto_req_analysis`，用于 Skill 识别任务 |
| `collection_name` | 是 | TFS 集合名称 |
| `work_item_id` | 是 | TFS 工作项 ID |
| `tfs_project` | 是 | TFS 项目名称 |
| `tfs_pat` | 是 | 当前用户或调用方的 TFS PAT |
| `redis_key` | 是 | FastAPI 按统一规则生成后传给 Skill，避免双方 key 规则漂移 |

### 2.4 Skill 输出

Skill 的文本响应只作为任务执行日志或调试信息使用，FastAPI 的业务结果以 Redis 数据为准。

推荐 Skill 返回简短执行摘要：

```json
{
  "status": "success",
  "redis_key": "auto-req:qc:plan:WN_Data_Platform:258288",
  "message": "需求质控结果已写入 Redis"
}
```

FastAPI 的业务查询结果以 Redis 中的 JSON 为准。

---

## 3. Redis 数据契约

### 3.1 Key 规则

```text
auto-req:qc:plan:{collection}:{work_item_id}
```

| 占位符 | 来源 | 示例 |
|--------|------|------|
| `{collection}` | TFS-BUDDY 传入的 `collection_name` | `WN_Data_Platform` |
| `{work_item_id}` | TFS 工作项 ID | `258288` |

### 3.2 Value 结构

Redis value 为完整 JSON 字符串。结构以 [p.json](p.json) 为准，核心形态如下：

```json
{
  "version": 1,
  "run_id": "20260723151602",
  "skill": "auto-req-analysis",
  "work_item_id": 258288,
  "expected_rev": 3,
  "expected_state": "已建议",
  "verdict": "NEED-REVIEW",
  "tags": ["PM-AI-QC-NEED-REVIEW"],
  "state_to": null,
  "rules_source": "pre-qc-v1",
  "checklist": {
    "work_item": "258288 宁夏妇幼云HIS项目对接信用付接口",
    "verdict": "NEED-REVIEW",
    "tag": "PM-AI-QC-NEED-REVIEW",
    "responsible": "产品",
    "generated_at_utc": "2026-07-23",
    "items": [
      {
        "id": "q1",
        "category": "高风险·支付结算(②)",
        "responsible": "产品",
        "primary": true,
        "question": "请确认业务规则与资金流...",
        "why": "命中支付结算高风险②...",
        "options": ["选项1", "选项2"],
        "allow_other": true
      }
    ],
    "passed": ["已通过检查项"],
    "kb_note": "知识库检索摘要",
    "notes": ["补充说明"],
    "next": "下一步建议"
  },
  "artifacts": [],
  "kb": {},
  "iteration": {},
  "attachments": {}
}
```

### 3.3 FastAPI 返回节点

查询任务接口在 `completed` 状态时，会从 Redis value 中读取 `checklist` 节点并嵌入响应。

如果 Redis 中 value 不存在 `checklist`，接口应返回 `500` 或明确的业务错误，表示 Skill 写入结果不符合契约。

### 3.4 过期时间

建议 Redis key 设置 TTL：7 天。

```text
TTL = 604800 seconds
```

该周期覆盖产品补充信息和联调复查的常见节奏；如 TFS-BUDDY 需要长期留痕，应由 TFS-BUDDY 或 TFS 附件/评论持久化最终结果。

---

## 4. FastAPI 服务封装层

### 4.1 目录结构

```text
deerflow-service/
├── main.py
├── config.py
├── models.py
├── services/
│   ├── deerflow_client.py
│   ├── redis_client.py
│   └── task_manager.py
├── routers/
│   └── analysis.py          # POST 提交 + GET 查询（含 checklist 嵌入）
├── middleware/
│   └── auth.py
└── requirements.txt
```

评估状态与 checklist 均以 Redis 中 `auto-req-analysis` 写入结果为准。

### 4.2 配置

```python
import os
from dotenv import load_dotenv

load_dotenv()

DEERFLOW_BASE_URL = os.getenv("DEERFLOW_BASE_URL", "http://172.16.0.192:2026")
DEERFLOW_LANGGRAPH_URL = f"{DEERFLOW_BASE_URL}/api/langgraph"
API_KEY = os.getenv("API_KEY", "tfs-buddy-secret-key")

AGENT_NAME = "req-analysis"
SKILL_NAME = "auto-req-analysis"

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_QC_KEY_PREFIX = "auto-req:qc:plan"
REDIS_QC_TTL_SECONDS = 604800
```

### 4.3 Redis Key 工具函数

```python
def build_qc_plan_key(collection_name: str, work_item_id: int) -> str:
    return f"auto-req:qc:plan:{collection_name}:{work_item_id}"
```

### 4.4 DeerFlow 调用

FastAPI 只负责触发 Agent，不解析业务结果：

```python
message = json.dumps({
    "action": "auto_req_analysis",
    "collection_name": req.collection_name,
    "work_item_id": req.work_item_id,
    "tfs_project": req.tfs_project,
    "tfs_pat": req.tfs_pat,
    "redis_key": build_qc_plan_key(req.collection_name, req.work_item_id),
}, ensure_ascii=False)

client.run_agent(
    collection_name=req.collection_name,
    work_item_id=req.work_item_id,
    message=message,
    agent_name="req-analysis",
)
```

### 4.5 任务状态查询（含 checklist 嵌入）

```python
def get_task_and_checklist(task_id: str):
    # 1. 查任务状态
    task_data = redis_client.get(f"task:{task_id}")
    if not task_data:
        raise HTTPException(status_code=404, detail="任务不存在")

    task = json.loads(task_data)
    response = {
        "task_id": task_id,
        "status": task["status"],
        "error": task.get("error"),
    }

    # 2. completed 时从 Redis 读取 checklist 并嵌入
    if task["status"] == "completed":
        redis_key = task.get("redis_key", "")
        if redis_key:
            raw = redis_client.get(redis_key)
            if raw:
                payload = json.loads(raw)
                checklist = payload.get("checklist")
                if checklist:
                    response["checklist"] = checklist
                else:
                    # Redis 结果缺少 checklist，标记业务异常
                    response["status"] = "failed"
                    response["error"] = "质控结果缺少 checklist 节点"
            else:
                # Redis key 已过期或不存在
                response["status"] = "failed"
                response["error"] = f"质控结果已过期或不存在: {redis_key}"

    return response
```

---

## 5. TFS-BUDDY 调用流程

```text
1. TFS-BUDDY 获取 collection_name、work_item_id、tfs_project、tfs_pat
2. POST /api/v1/analysis 提交任务
3. GET /api/v1/analysis/{task_id} 轮询，直到 completed 或 failed
   - completed 时响应直接携带 checklist 数据
4. TFS-BUDDY 展示 checklist.items 中的问题，或根据 verdict/tag 进入后续流程
```

伪代码：

```javascript
async function runAutoReqAnalysis(collectionName, workItemId, tfsPat, tfsProject) {
  const submitResp = await fetch("http://deerflow-service:8080/api/v1/analysis", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": "tfs-buddy-secret-key",
    },
    body: JSON.stringify({
      collection_name: collectionName,
      work_item_id: workItemId,
      tfs_pat: tfsPat,
      tfs_project: tfsProject,
    }),
  });

  const submitted = await submitResp.json();
  const taskId = submitted.task_id;

  let task;
  do {
    await sleep(3000);
    const taskResp = await fetch(`http://deerflow-service:8080/api/v1/analysis/${taskId}`, {
      headers: { "X-API-Key": "tfs-buddy-secret-key" },
    });
    task = await taskResp.json();
  } while (task.status === "pending" || task.status === "processing");

  if (task.status === "failed") {
    throw new Error(task.error || "auto-req-analysis failed");
  }

  // completed 时响应已直接携带 checklist，无需额外请求
  return task.checklist;
}
```

---

## 6. API 接口规范

### 6.1 提交需求质控任务

**POST `/api/v1/analysis`**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `collection_name` | string | 是 | TFS 集合名称 |
| `work_item_id` | int | 是 | TFS 工作项 ID |
| `tfs_pat` | string | 是 | TFS Personal Access Token |
| `tfs_project` | string | 是 | TFS 项目名称 |

响应：

```json
{
  "task_id": "f99d3d8e2b6f4d7a9c0a6f4e5c3a1b22",
  "status": "pending"
}
```

### 6.2 查询任务状态（含 checklist）

**GET `/api/v1/analysis/{task_id}`**

响应（`completed` 时携带 `checklist`）：

```json
{
  "task_id": "f99d3d8e2b6f4d7a9c0a6f4e5c3a1b22",
  "status": "completed",
  "error": null,
  "checklist": {
    "work_item": "258288 宁夏妇幼云HIS项目对接信用付接口",
    "verdict": "NEED-REVIEW",
    "tag": "PM-AI-QC-NEED-REVIEW",
    "responsible": "产品",
    "generated_at_utc": "2026-07-23",
    "items": [
      {
        "id": "q1",
        "category": "高风险·支付结算(②)",
        "responsible": "产品",
        "primary": true,
        "question": "信用付属医保信用支付结算...",
        "why": "标题及附件接口规范触及支付结算高风险②...",
        "options": ["额度由医保/金融机构授信，HIS仅按接口发起支付请求"],
        "allow_other": true
      }
    ],
    "passed": [],
    "kb_note": "",
    "notes": [],
    "next": "产品补充上述业务规则/范围/一致性后，重跑 auto-req-analysis"
  }
}
```

响应（`pending`/`processing` 时不携带 `checklist`）：

```json
{
  "task_id": "f99d3d8e2b6f4d7a9c0a6f4e5c3a1b22",
  "status": "pending",
  "error": null
}
```

响应（`failed` 时携带错误信息，无 `checklist`）：

```json
{
  "task_id": "f99d3d8e2b6f4d7a9c0a6f4e5c3a1b22",
  "status": "failed",
  "error": "DeerFlow 执行超时"
}
```

任务状态：

| 状态 | 说明 |
|------|------|
| `pending` | 已提交，等待执行 |
| `processing` | DeerFlow Agent 执行中 |
| `completed` | 执行完成，响应携带 `checklist` |
| `failed` | 执行失败，查看 `error`（若`checklist`缺失也会转为`failed`） |

### 6.3 认证

所有接口需要在 Header 中携带：

```text
X-API-Key: tfs-buddy-secret-key
```

### 6.4 错误码

| 状态码 | 说明 |
|--------|------|
| 200 | 成功 |
| 400 | 参数错误 |
| 401 | 认证失败 |
| 404 | 任务不存在 |
| 500 | DeerFlow 执行错误、Redis JSON 解析失败、结果缺少 `checklist` |
| 504 | DeerFlow 执行超时 |

---

## 7. 部署配置

### 7.1 Skill 上传

```bash
mkdir -p /opt/deer-flow/skills
# 将 auto-req-analysis/ 放到 /opt/deer-flow/skills/
```

### 7.2 Agent 配置

`/opt/deer-flow/agents/req-analysis/config.yaml`：

```yaml
name: req-analysis
display_name: 需求分析
description: 需求自动质控、补充问题生成，并将结果写入 Redis。
skills:
  - auto-req-analysis
tool_groups:
  - bash
```

### 7.3 Docker 挂载

```yaml
services:
  gateway:
    volumes:
      - /opt/deer-flow/skills:/mnt/skills
      - /opt/deer-flow/agents:/app/agents
```

重建 Gateway：

```bash
cd /opt/deer-flow
docker compose -p deer-flow-dev -f docker/docker-compose-dev.yaml up -d --force-recreate gateway
```

### 7.4 环境变量

DeerFlow / Skill 侧需要能访问 Redis：

```env
REDIS_URL=redis://redis:6379/0
```

FastAPI 服务侧同样配置 Redis：

```env
DEERFLOW_BASE_URL=http://172.16.0.192:2026
API_KEY=tfs-buddy-secret-key
REDIS_URL=redis://redis:6379/0
```

---

## 8. 实施步骤

### 第 1 步：准备 Skill

准备 `auto-req-analysis` Skill，覆盖 TFS 工作项读取、附件解析、知识库查重、质控问题生成、Redis 写入能力。

### 第 2 步：实现 Redis 写入

`auto-req-analysis` 执行完成后，将完整 JSON 写入：

```text
auto-req:qc:plan:{collection}:{work_item_id}
```

写入内容需包含顶层 `checklist` 节点，结构参考 [p.json](p.json)。

### 第 3 步：实现 FastAPI 服务

1. 提交接口继续异步触发 DeerFlow。
2. 任务查询接口在 `completed` 时从 Redis 读取并嵌入 `checklist`。

### 第 4 步：联调

1. 手动调用 DeerFlow，确认 `auto-req-analysis` 能写入 Redis。
2. 手动读取 Redis key，确认 JSON 可解析且包含 `checklist`。
3. 调用 FastAPI 提交任务并轮询完成，确认 completed 响应携带正确的 checklist。
4. TFS-BUDDY 直接从轮询响应中提取 checklist 展示。

---

## 9. 生产环境改造

### 9.1 Redis 作为强依赖

Redis 是 Skill 与 FastAPI 之间的业务数据交换通道，生产环境必须配置 Redis。

建议：

- Redis value 使用 JSON 字符串，编码 UTF-8。
- key TTL 默认 7 天。
- 写入时使用 `SETEX`，避免长期堆积。
- FastAPI 查询接口只读 Redis，不修改 Skill 写入结果。

### 9.2 TaskManager

异步任务状态仍建议使用 Redis，key 可保持：

```text
task:{task_id}
```

任务结果只保存调度状态和 `redis_key`（供状态查询接口在 `completed` 时读取 checklist），不保存完整 checklist，避免重复存储。

### 9.3 DeerFlow Checkpoint

如并发使用规模扩大，DeerFlow checkpoint 建议从 SQLite 切换到 PostgreSQL，避免 SQLite 单写锁导致排队。

```yaml
database:
  backend: postgresql
  postgresql_url: postgresql://deerflow:deerflow123@postgres:5432/deerflow
```

### 9.4 并发限流

建议 FastAPI 层控制 Agent run 并发数：

```python
import asyncio

agent_run_semaphore = asyncio.Semaphore(5)
```

同一工作项重复提交时，可根据 Redis key 或任务状态做幂等保护，避免同一个 `collection + work_item_id` 被并发重复分析。

---

## 10. 附录：安全边界

| 限制项 | 建议值 | 说明 |
|--------|--------|------|
| 单次 Skill 超时 | 300s | 超时后任务标记 failed |
| 单轮问题数 | 10 | `checklist.items` 超过 10 个时优先保留高风险/主问题 |
| Redis TTL | 7 天 | 覆盖常规补充确认周期 |
| 同工作项并发 | 1 | 同一个 `collection + work_item_id` 同时只允许一个分析任务 |
| PAT 日志 | 禁止明文输出 | `tfs_pat` 不写日志、不写 Redis |

