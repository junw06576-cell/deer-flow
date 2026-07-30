# Auto Analysis Agent 脚本复用强制化设计

## 1. 背景

`auto-analysis-agent` 执行自动需求质控与分析时，会启用
`reg-auto-req-analysis` skill。该 skill 已提供 TFS 读取、附件下载与转换、
执行计划校验、TFS 写回、Redis 查询等脚本，但 agent 仍可能使用通用 `bash`
编写临时 Python、Shell 或 PowerShell 脚本，重复实现已有能力。

当前约束主要是模型提示：

- Agent 的 `SOUL.md` 要求优先使用 skill 自带脚本。
- Skill 的 `_lib/COMMANDS.md` 要求写脚本前先查已有命令。
- Skill 的 `allowed-tools` 允许使用 `bash`。

这些都是软约束。只要 agent 仍拥有通用 `bash`，模型就保留自行组织命令和创建
脚本的能力，无法从机制上保证脚本复用。

## 2. 目标与非目标

### 2.1 目标

1. 正常执行链路不允许 agent 创建或执行临时脚本。
2. 固定复用 `reg-auto-req-analysis` 已登记的脚本和子命令。
3. 将流程编排、路径校验、执行顺序、重试和写回控制从模型移入确定性代码。
4. 模型只负责无法完全规则化的质控和分析判断，并输出受 Schema 约束的数据。
5. TFS PAT 等秘密不进入模型可见工具参数、日志或最终响应。
6. 保留结构化诊断和迁移期止血能力。

### 2.2 非目标

1. 不在本改造中重写 `tfs_client.py`、`attachment_converter.py` 或
   `pipeline.py` 已有业务能力。
2. 不修改所有 DeerFlow agent 的全局 Bash 行为。
3. 不允许诊断能力演变成另一个任意命令执行入口。
4. 不依赖继续强化提示词来实现安全边界。

## 3. 现状与根因

### 3.1 Agent 配置只限制工具组，不限制 Bash 内部命令

`agents/auto-analysis-agent/config.yaml` 当前配置 `tool_groups: [bash]`。
工具组可以控制 agent 是否获得 `bash`，但无法表达“仅能执行 skill 内某几个脚本”。

### 3.2 Skill 白名单不等于 Skill 强制激活

Agent 配置中的：

```yaml
skills:
  - reg-auto-req-analysis
```

表示该 skill 可被发现和激活，并不意味着运行时必然先加载 skill，也不意味着仅凭
`allowed-tools` 就能约束每一条 Bash 命令。

### 3.3 主流程仍由模型编排

`reg-auto-req-analysis` 已将若干底层操作脚本化，但以下职责仍由模型承担：

- 选择和拼接命令；
- 建立运行目录；
- 决定命令执行顺序；
- 解读命令输出；
- 生成计划和产物；
- 处理异常与降级。

模型在这些步骤中可能认为临时脚本更方便，从而绕过已有脚本。

## 4. 方案总览

本设计保留三个层次：

| 方案 | 定位 | 是否保留通用 Bash | 根治程度 |
|---|---|---:|---:|
| A：专用领域工具 | 最终目标架构 | 否 | 完全根治 |
| B：领域工具 + 受限诊断 | A 的可选生产扩展 | 否 | 完全根治 |
| C：Bash 白名单中间件 | 迁移期止血 | 是 | 降低发生率 |

推荐路径为：必要时先部署 C 止血，并行建设 A；A 上线后移除 Bash；确有自动诊断
需求时再增加 B。C 不作为长期架构保留。

## 5. 方案 A：专用领域工具

### 5.1 架构

```text
deerflow-service trusted request context
                    │
                    ▼
            auto-analysis-agent
                    │
                    │ run_auto_req_analysis
                    ▼
       AutoReqAnalysisOrchestrator
          ├─ RegisteredCommandRunner
          ├─ EvidenceCollector
          ├─ StructuredDecisionProvider
          ├─ PlanBuilder
          └─ ResultVerifier
                    │
                    ▼
 reg-auto-req-analysis existing scripts
                    │
                    ▼
              TFS + Redis
```

Agent 只调用一个领域工具。该边界不能仅靠 `tool_groups` 实现，因为 DeerFlow 的
built-in、MCP、ACP、subagent 和 agent 自修改工具可能在工具组过滤后继续加入。
运行时必须对 `auto-analysis-agent` 应用服务端硬白名单：

```text
visible tools   = {run_auto_req_analysis}
executable tools = {run_auto_req_analysis}
```

除领域工具外，必须关闭或拒绝 `bash`、`update_agent`、MCP、ACP、subagent、memory
工具以及其他 built-in。硬白名单既作用于模型绑定的工具列表，也作用于执行路由，
避免通过伪造 tool call 调用未展示工具。

### 5.2 新增模块

建议新增：

```text
backend/packages/harness/deerflow/auto_req_analysis/
├── __init__.py
├── orchestrator.py
├── contracts.py
├── command_runner.py
├── evidence.py
├── plan_builder.py
├── paths.py
└── errors.py

backend/packages/harness/deerflow/tools/builtins/
└── auto_req_analysis_tool.py

backend/packages/harness/deerflow/agents/middlewares/
└── agent_tool_allowlist_middleware.py
```

职责如下：

- `orchestrator.py`：实现固定状态机，协调全部步骤。
- `contracts.py`：定义工具输入、模型决策、阶段结果和最终结果。
- `command_runner.py`：只运行静态登记的已有脚本。
- `evidence.py`：将 TFS、附件、迭代、知识库结果标准化为模型证据。
- `plan_builder.py`：根据模型决策构造受约束的计划和 Markdown 产物。
- `paths.py`：解析 skill 根目录和本次运行目录，阻断目录穿越。
- `errors.py`：定义稳定错误码、可重试性和降级语义。
- `auto_req_analysis_tool.py`：向 agent 暴露唯一领域工具。
- `agent_tool_allowlist_middleware.py`：对该 agent 执行模型可见与运行时双重硬白名单，
  并禁止通过 `update_agent` 重加工具。

### 5.3 工具接口

模型可见接口：

```python
run_auto_req_analysis(
    work_item_id: int
) -> AutoReqAnalysisResult
```

工具不得接收：

- 任意命令字符串；
- 任意脚本路径；
- 任意输出目录；
- TFS PAT；
- Redis 密码；
- `execute` 或任何写入授权开关；
- collection、project、Redis key/namespace 或租户选择器；
- 可供模型覆盖的 Skill 根目录。

写入授权、collection、project、Redis namespace、租户/用户归属和凭据选择全部从
Gateway 注入的已认证可信运行上下文读取。工具必须验证 `work_item_id` 属于该上下文
允许的 collection/tenant；可信上下文缺失、调用者无写权限或作用域不匹配时一律
fail closed。秘密从服务端凭据存储或受保护环境读取。来自普通用户消息或模型参数的
同名字段不得提升为可信上下文。

建议返回：

```json
{
  "status": "completed",
  "work_item_id": 228549,
  "run_id": "run_20260730_xxx",
  "stage": "applied",
  "verdict": "AUTO-ANA",
  "redis_key": "auto-req:qc:plan:collection:228549",
  "message": "Requirement analysis completed",
  "diagnostics": []
}
```

### 5.4 固定状态机

```text
INIT
  → PRECHECK
  → FETCH
  → DOWNLOAD_ATTACHMENTS
  → CONVERT_ATTACHMENTS
  → LOAD_RULES
  → COLLECT_EVIDENCE
  → INSPECT_IMAGES
  → MODEL_DECISION
  → BUILD_ARTIFACTS
  → BUILD_PLAN
  → VALIDATE
  → APPLY
  → VERIFY_RESULT
  → COMPLETED
```

状态转换由代码控制。模型不能跳过 `VALIDATE`、直接调用 `APPLY`，也不能在某一步
失败后自行选择替代命令。

### 5.5 证据适配器

`EvidenceCollector` 必须覆盖原 skill 要求的全部证据源，而不是只覆盖 Python 脚本：

| 证据源 | 受控适配方式 | 不可用时 |
|---|---|---|
| TFS 字段/迭代/附件 | `RegisteredCommandRunner` | 按 skill 规则降级或终止 |
| Skill 配置与 references | 固定只读资源加载器 | 必需规则缺失则终止 |
| 产品 wiki | 固定 wiki 根目录的只读索引/检索适配器 | 记录 `wiki_ready=false` |
| GitNexus | 服务端专用客户端，仅暴露 skill 所需查询 | 记录 `kb_ready=false` |
| db-knowledge | 服务端专用客户端，且仅在规则命中时调用 | 记录限制后继续 |
| 菜单索引 | 已登记 `lookup` 命令 | 记录范围未确认 |
| 图片附件 | 受控图片读取器传给支持视觉的决策模型 | 标记 `unparsed`，不得假称已读 |

知识库适配器不作为 agent 工具暴露；它们由 orchestrator 服务端调用，并固定
endpoint、仓库/租户范围、超时、返回大小和允许操作。图片只从当前运行附件目录读取，
经过类型、大小和路径校验后作为受控多模态输入。

结构化模型调用必须继承请求的取消信号，并配置总超时、单次超时、token 上限、有限
重试和 tracing 关联 ID。模型只收到已脱敏、带证据引用的标准化输入；超时或不支持
视觉时按 attachment evidence 规则标记未解析，不允许转写临时图片脚本。

### 5.6 现有脚本注册表

`RegisteredCommandRunner` 使用静态注册表，而不是接收完整命令：

```python
COMMANDS = {
    "tfs.precheck": CommandSpec("_lib/tfs/tfs_client.py", "precheck"),
    "tfs.fetch": CommandSpec("_lib/tfs/tfs_client.py", "fetch"),
    "tfs.download_attachments": CommandSpec(
        "_lib/tfs/tfs_client.py",
        "download-attachments",
    ),
    "tfs.list_iterations": CommandSpec(
        "_lib/tfs/tfs_client.py",
        "list-iterations",
    ),
    "attachments.convert": CommandSpec(
        "_lib/tfs/attachment_converter.py",
        None,
    ),
    "menu.lookup": CommandSpec(
        "_lib/build_menu_business_index.py",
        "lookup",
    ),
    "pipeline.validate": CommandSpec(
        "_lib/tfs/pipeline.py",
        "validate",
    ),
    "pipeline.apply": CommandSpec(
        "_lib/tfs/pipeline.py",
        "apply",
    ),
    "redis.ping": CommandSpec("_lib/tfs/redis_client.py", "ping"),
    "redis.hgetall": CommandSpec("_lib/tfs/redis_client.py", "hgetall"),
}
```

执行要求：

- 使用参数数组调用子进程，不使用 `shell=True`；
- 每个参数按命令定义单独校验；
- 只使用发布时安装的 immutable public skill，固定版本或内容 digest；不得解析到
  用户可编辑的 custom skill；
- 启动前和执行前验证包 digest，拒绝符号链接、junction、reparse point 和任何
  解析后越出固定 skill 根目录的文件；
- 使用已打开文件句柄或等效机制避免校验后替换（TOCTOU）；
- 运行产物必须位于当前 `<work_item_id>/<run_id>` 目录；
- 子进程使用最小环境变量白名单，不继承无关凭据；
- 禁止 `--pat`、`--config`、任意 `--collection` 和任意输出路径覆盖；所需值由
  orchestrator 根据可信上下文生成；
- 外部附件下载仅允许 skill 既有 HTTPS 白名单、独立认证和大小限制，绝不复用
  TFS PAT；
- stdout 必须符合对应 JSON 契约；
- 超时、退出码、JSON 错误转换为稳定错误码；
- 日志不记录 PAT、Redis 密码、需求正文和附件正文。

### 5.7 模型职责

模型只接收标准化证据，并返回受约束决策：

```python
class AnalysisDecision(BaseModel):
    verdict: Literal[
        "NEED-INFO",
        "NEED-REVIEW",
        "AUTO-ANA",
        "MANUAL-REVIEW",
        "MANUAL-REVIEW-STOP",
    ]
    checklist: list[ChecklistItem]
    categories: list[str]
    analysis_gaps: list[AnalysisGap]
    change_plan: ChangePlan | None
    evidence_refs: list[str]
```

模型不能：

- 调用命令；
- 指定脚本或文件路径；
- 直接写 TFS 或 Redis；
- 决定是否跳过计划校验；
- 将未提供的证据标记为已确认。

结构不合法时由 orchestrator 进行有限次数的结构化重试。超过限制后返回
`MODEL_OUTPUT_INVALID`，不得进入临时脚本补救流程。

### 5.8 工具注册与 Agent 配置

在根应用配置中注册：

```yaml
tool_groups:
  - name: auto_req_analysis

tools:
  - name: run_auto_req_analysis
    group: auto_req_analysis
    use: deerflow.tools.builtins.auto_req_analysis_tool:run_auto_req_analysis_tool
```

修改 Agent 配置为：

```yaml
name: auto-analysis-agent
description: Requirement evaluation and TFS write-back.
skills:
  - reg-auto-req-analysis
tool_groups:
  - auto_req_analysis
recursion_limit: 100
```

必须删除 `bash` 工具组。

此外，在 lead-agent 构造和工具执行入口增加 agent 专属硬白名单。该 agent：

- `include_mcp=False`；
- 不装载 ACP 工具；
- `subagent_enabled=False`；
- 不装载 memory、`update_agent` 和其他 built-in 工具；
- 运行时拒绝除 `run_auto_req_analysis` 外的任何 tool call；
- 不允许 API 或 agent 自修改接口在运行期间扩大其工具组。

### 5.9 Agent 与 Skill 提示调整

`SOUL.md` 只保留简单且可验证的调用契约：

```markdown
For every valid auto_req_analysis request:

1. Call run_auto_req_analysis exactly once.
2. Pass only the non-secret routing fields defined by the tool schema.
3. Do not reproduce or bypass the workflow.
4. Return the structured tool result without changing its verdict.
5. If the tool fails, return its structured error. Do not attempt an alternative.
```

`SKILL.md` 调整为：

- 自动 agent 场景必须调用 `run_auto_req_analysis`；
- 主文档保留 QC、分析、证据和写作规范；
- 命令明细继续由 `_lib/COMMANDS.md` 维护；
- `allowed-tools` 改为 `run_auto_req_analysis`；
- 不再要求模型手工编排已有脚本。

Skill 的 `allowed-tools` 是第二层约束，不能替代 Agent 工具组收口。

## 6. 方案 B：领域工具加受限诊断

### 6.1 定位

方案 B 是 A 的生产扩展，不重新开放 `bash`。它为失败场景提供结构化、只读、可审计的
诊断能力。

### 6.2 诊断接口

```python
diagnose_auto_req_analysis(
    work_item_id: int,
    run_id: str | None,
    checks: list[
        Literal[
            "tfs_precheck",
            "redis_ping",
            "skill_files",
            "run_artifacts",
            "last_audit",
            "script_help",
        ]
    ],
) -> DiagnosticsResult
```

它不能接收命令字符串、路径或凭据。
collection、tenant 和可访问的运行目录仍从可信上下文派生；传入的 `run_id` 必须属于
同一工作项和可信作用域，否则拒绝。

### 6.3 允许行为

- TFS `precheck` 和必要的只读 `fetch`；
- Redis `ping`、`hgetall`；
- 检查当前工作项和 `run_id` 的产物；
- 读取脱敏审计摘要；
- 获取已登记脚本的 `--help`；
- 校验 skill 必需文件是否存在和是否发生版本变化。

### 6.4 禁止行为

- TFS 写操作；
- `pipeline apply --execute`；
- 创建、修改或删除文件；
- 执行注册表之外的子进程；
- 访问任意外部 URL；
- 返回凭据、完整需求正文或附件正文；
- 访问其他工作项或其他运行目录。

### 6.5 调用规则

```text
run_auto_req_analysis
  ├─ completed → 返回结果
  └─ failed
       ├─ retryable → orchestrator 内部重试
       └─ non-retryable / retries exhausted
            → diagnose_auto_req_analysis
            → 返回结构化故障摘要
```

重试由领域工具内部完成，不允许 agent 自行重复调用以绕开限制。

Agent 工具组：

```yaml
tool_groups:
  - auto_req_analysis
  - auto_req_diagnostics
```

依旧不包含 `bash`。

## 7. 方案 C：迁移期 Bash 白名单

### 7.1 定位

方案 C 用于 A 尚未完成时快速降低临时脚本出现率。由于 shell 语法和解释器组合存在
大量绕过方式，它不能提供与 A/B 相同的保证。

### 7.2 Agent 专属策略中间件

建议新增：

```text
backend/packages/harness/deerflow/agents/middlewares/
└── agent_bash_policy_middleware.py
```

仅在 `agent_name == "auto-analysis-agent"` 时启用，不影响其他 agent。

### 7.3 机器可读命令策略

新增：

```text
skills/public/reg-auto-req-analysis/command-policy.yaml
```

示例：

```yaml
version: 1

commands:
  - id: tfs.fetch
    script: _lib/tfs/tfs_client.py
    subcommand: fetch
    positional:
      - type: integer

  - id: pipeline.validate
    script: _lib/tfs/pipeline.py
    subcommand: validate
    required_options:
      - --plan

  - id: pipeline.apply
    script: _lib/tfs/pipeline.py
    subcommand: apply
    required_options:
      - --plan
    protected_options:
      --execute:
        requires_trusted_authorization: true
```

中间件必须解析 token 并按 Schema 校验，不能只做字符串前缀匹配。

`COMMANDS.md` 应由该策略生成，或增加一致性测试，避免机器策略和人类文档漂移。

方案 C 同样只能执行发布时固定版本或 digest 的 public skill。策略加载和命令执行都
必须拒绝 symlink/junction/reparse point、校验包 digest、使用最小子进程环境，并禁止
模型提供 `--pat`、`--config`、`--collection` 和任意输出目录。否则即使命令名称在
白名单中，仍可能通过配置覆盖或可变脚本取得越权能力。

### 7.4 拒绝规则

至少拒绝：

- `python -c`、`python3 -c`；
- PowerShell `-Command`；
- heredoc、here-string；
- 管道、重定向和命令替换；
- `&&`、`||`、`;` 等命令组合；
- 创建或执行临时 `.py`、`.sh`、`.ps1`；
- `jq`、`awk`、`sed` 等重新实现已有处理；
- `curl`、`wget` 和任意下载；
- `pip install` 等安装操作；
- 运行 skill 根目录之外的脚本；
- 复制合法脚本后从其他路径执行；
- 未获可信授权的 `apply --execute`。

被策略拒绝后，本次执行应终止并返回 `COMMAND_POLICY_DENIED`，不允许模型换一种语法
继续尝试。

### 7.5 审计

每次 Bash 调用记录：

```json
{
  "agent_name": "auto-analysis-agent",
  "work_item_id": 228549,
  "run_id": "run_xxx",
  "decision": "allow",
  "policy_command": "pipeline.validate",
  "argv_hash": "sha256:...",
  "timestamp": "2026-07-30T..."
}
```

审计不得包含 PAT、Redis 密码、需求正文或附件正文。

### 7.6 退出条件

满足以下条件后删除该 agent 的 Bash 权限和专属白名单中间件：

1. A 的领域工具覆盖全部正常流程；
2. A 的集成测试通过；
3. 生产灰度期内没有依赖 Bash 补救的执行；
4. 必要诊断已由 B 覆盖。

## 8. 错误处理契约

统一错误结果：

```json
{
  "status": "failed",
  "work_item_id": 228549,
  "run_id": "run_xxx",
  "stage": "validate",
  "error": {
    "code": "PLAN_VALIDATION_FAILED",
    "message": "Generated plan did not satisfy the execution contract",
    "retryable": false
  },
  "redis_key": "auto-req:...",
  "diagnostics": []
}
```

### 8.1 重试与恢复规则

重试由 orchestrator 按阶段执行，agent 本身不得重复调用领域工具。每个
`work_item_id + trusted tenant` 同一时间只允许一个活动运行，通过租约锁防止并发重复；
锁具有 fencing token，过期执行者不能继续写入。

| 错误码 | 阶段内重试 | 运行身份与行为 |
|---|---:|---|
| `INVALID_REQUEST` | 0 | 不创建 run |
| `AUTHORIZATION_CONTEXT_MISSING` | 0 | fail closed |
| `CREDENTIALS_UNAVAILABLE` | 0 | 不暴露凭据细节 |
| `TFS_PRECHECK_FAILED` | 最多 2 次，仅瞬时错误 | 同一 run；业务配置错误按 skill 降级 |
| `ATTACHMENT_DOWNLOAD_FAILED` | 最多 2 次 | 同一 run；耗尽后记录限制 |
| `ATTACHMENT_CONVERSION_FAILED` | 0 | 标记未解析后按规则继续 |
| `MODEL_OUTPUT_INVALID` | 最多 2 次结构修复 | 同一 run；耗尽后终止且 `retryable=false` |
| `PLAN_VALIDATION_FAILED` | 0 | 禁止 apply |
| `TFS_CONFLICT` | 0 次复用旧计划 | 废弃旧计划；重新 fetch 与重新决策时创建新 attempt，并保留父 run 关联 |
| `REDIS_WRITE_FAILED` | 最多 3 次 | TFS 已写则状态为 `PARTIAL_SUCCESS`，只重试 Redis，不重放 TFS |
| `COMMAND_POLICY_DENIED` | 0 | 迁移期立即终止 |
| `CANCELLED` / `TIMEOUT` | 0 | 写入终止标记并释放租约 |
| `INTERNAL_ERROR` | 0 | 返回关联 ID，不返回堆栈和秘密 |

### 8.2 持久化检查点

每个阶段完成后持久化：

```json
{
  "run_id": "run_xxx",
  "attempt": 1,
  "stage": "apply",
  "stage_status": "completed",
  "tfs_applied": true,
  "redis_written": false,
  "fencing_token": 17
}
```

恢复规则：

- TFS 未写入：从最后一个已完成的只读阶段继续，必要时重新验证 revision。
- TFS 已写、Redis 未写：只验证 TFS 幂等标记并重试 Redis。
- Apply 中途崩溃：先用 run marker 查询 TFS 实际状态，不盲目重放。
- 原证据 revision 已变化：废弃旧计划，建立关联的新 attempt 并完整重算。
- 最终状态区分 `COMPLETED`、`BUSINESS_TERMINAL`、`PARTIAL_SUCCESS`、
  `FAILED` 和 `CANCELLED`。

工具内部应区分“业务终局”和“系统失败”。`NEED-INFO`、`NEED-REVIEW`、
`MANUAL-REVIEW` 是成功产生的业务终局，不得被包装成系统错误。

## 9. 测试设计

### 9.1 单元测试

建议新增：

```text
backend/tests/auto_req_analysis/test_command_runner.py
backend/tests/auto_req_analysis/test_orchestrator.py
backend/tests/auto_req_analysis/test_contracts.py
backend/tests/auto_req_analysis/test_paths.py
backend/tests/tools/test_auto_req_analysis_tool.py
backend/tests/agents/test_auto_analysis_agent_tools.py
```

覆盖：

- agent 工具集中不存在 `bash`；
- 模型可见工具严格等于 `{run_auto_req_analysis}`；
- 直接构造未展示的 `bash`、`update_agent`、MCP、ACP、memory 或 subagent tool call
  会在执行入口被拒绝；
- 只能调用登记的脚本和子命令；
- 所有路径穿越被拒绝；
- 可变 custom skill、digest 不匹配、symlink/junction 和校验后替换被拒绝；
- 子进程不经过 shell；
- 子进程环境不泄露无关凭据，且拒绝配置/collection/PAT/输出路径覆盖；
- PAT 不出现在 Schema、日志和返回值；
- 模型伪造 write authorization、collection、Redis namespace 或 tenant 无效；
- 跨租户和跨 collection 的 work item 被拒绝；
- 模型结构异常不会触发脚本补救；
- `validate` 失败时绝不调用 `apply`；
- 相同 `run_id` 重试保持幂等；
- 每个阶段失败映射到稳定错误码。
- 超时和取消能终止子进程、写检查点并释放租约。

### 9.2 集成测试

使用假的 TFS、Redis 和模型决策器验证：

1. `NEED-INFO` 正常写回；
2. `NEED-REVIEW` 正常写回；
3. `AUTO-ANA` 先校验、后执行、再验证 Redis；
4. `MANUAL-REVIEW` 生成正确附件；
5. TFS revision 冲突按预期重试或终止；
6. Redis 失败不回滚已经成功的 TFS 写入；
7. 附件无法解析时只记录限制，不伪造已读结论；
8. 整个执行期间没有创建临时脚本。
9. GitNexus、db-knowledge、wiki、规则文件和图片证据均通过受控适配器消费，并遵守
   skill 的降级语义。
10. 并发重复请求只产生一个有效写入者。
11. TFS 写入后崩溃或 Redis 失败能恢复为完成或明确的 `PARTIAL_SUCCESS`，不重复
    写 TFS。

### 9.3 方案 B 测试

- 诊断工具不能执行写命令；
- 非失败场景不调用诊断；
- 诊断只能访问当前工作项与运行目录；
- 所有诊断结果完成脱敏；
- 诊断行为写入审计。

### 9.4 方案 C 绕过测试

- 命令连接符；
- 路径 `..` 和符号链接；
- 大小写与空白变形；
- `python -c`；
- heredoc 和 PowerShell here-string；
- PowerShell 间接启动 Python；
- 复制合法脚本后执行；
- 为合法子命令附加未知参数；
- 缺少可信授权的 `--execute`；
- 日志与审计脱敏。

## 10. 文档与配置同步

实现时同步修改：

- `agents/auto-analysis-agent/config.yaml`；
- `agents/auto-analysis-agent/SOUL.md`；
- `skills/public/reg-auto-req-analysis/SKILL.md`；
- `skills/public/reg-auto-req-analysis/_lib/COMMANDS.md`；
- 根 `config.example.yaml` 的工具组和工具注册示例；
- `backend/AGENTS.md` 的领域工具与权限边界说明；
- 必要时更新面向部署人员的配置文档。

若 `command-policy.yaml` 被引入，`COMMANDS.md` 不得成为另一份独立维护的命令真相源。
应通过生成或一致性测试确保二者不会漂移。

## 11. 发布与迁移

### 阶段 0：建立观测基线

- 统计该 agent 当前 Bash 调用；
- 标记临时脚本、内联解释器和已有脚本调用；
- 确认正常执行需要覆盖的命令集合。

### 阶段 1：可选的 C 止血

- 上线 agent 专属 Bash 白名单；
- 开启策略拒绝审计；
- 保持现有业务流程不变；
- 对误拦截只更新机器策略，不允许 agent 自主绕过。

### 阶段 2：A 影子运行

- 领域工具以 dry-run 或影子模式执行；
- 对比旧流程与新流程的 verdict、计划和产物；
- 不进行双重 TFS 写入。

### 阶段 3：A 灰度切换

- 小范围让 agent 只使用 `run_auto_req_analysis`；
- 监控成功率、错误码、TFS 冲突和 Redis 写入；
- 回滚单位是工具组配置，不恢复模型编写脚本能力。

### 阶段 4：移除 Bash

- 从 `auto-analysis-agent` 删除 `bash` 工具组；
- 将领域工具设为唯一正常执行入口；
- 删除方案 C 的 agent 专属策略。

### 阶段 5：按需增加 B

- 只有明确存在自动诊断需求时才增加诊断工具；
- 诊断工具继续遵守只读、枚举检查和脱敏原则。

## 12. 验收标准

1. `auto-analysis-agent` 模型可见和运行时可执行的工具都严格等于
   `{run_auto_req_analysis}`，不仅是不包含 `bash`。
2. 正常请求只调用一次 `run_auto_req_analysis`。
3. 执行过程中不创建或执行临时脚本。
4. 所有底层脚本调用均来自静态注册表。
5. `pipeline validate` 未通过时不可能执行 `apply`。
6. PAT 和 Redis 秘密不进入模型、日志、审计或响应。
7. 写权限、collection、Redis namespace、tenant 和凭据完全来自可信服务端上下文；
   模型无法扩大作用域。
8. 每次运行都有唯一 `run_id`、明确 attempt、持久化检查点和稳定结果结构。
9. 所有业务终局、部分成功和系统错误可以被下游可靠区分。
10. 全部必需证据源有受控适配器和明确降级行为。
11. 只执行固定版本或 digest 的 immutable public skill，拒绝可变脚本和链接绕过。
12. 方案 B 启用时，诊断工具不具备写入或任意命令能力。
13. 方案 C 仅存在于迁移期，并有明确删除条件。

## 13. 最终决策

- **A 是目标架构**：专用领域工具负责确定性编排，Agent 移除 Bash。
- **B 是可选扩展**：仅在生产确有自动排障需求时提供结构化只读诊断。
- **C 是迁移方案**：用于 A 完成前止血，不能替代 A，也不长期保留。

该决策把“是否复用已有脚本”从模型偏好转换为运行时能力边界，从而根治 agent
自行编写脚本的问题。
