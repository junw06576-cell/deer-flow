# reg-auto-req-analysis 运行环境对比：Claude Code vs deerflow-service

> 整理时间：2026-08-14 | 目的：给 skill 制作者对齐两种编排环境的行为差异，识别 skill 中的隐式环境假设
> 依据：skill 源码（TFS 最新版 8115f4f）、deerflow-service 源码、172.16.0.192 实际运行日志（262298 事故复盘）

---

## 一、两种环境一句话定位

| 环境 | 本质 | skill 扮演的角色 |
|---|---|---|
| **Claude Code** | 交互式开发工具，人类直接对话 | 一个 slash command，人在旁边看结果、随时介入 |
| **deerflow-service** | 无人值守的 HTTP 调度服务 | 一个后台任务，跑完只靠 Redis 落盘结果判定成败，**没有任何人看过程** |

**核心差异一句话**：Claude Code 里 skill 跑得"对不对"由人判断；deerflow-service 里由 **Redis `run_id` 是否变化**这一个信号判定。后者对 skill 的确定性要求高一个量级。

---

## 二、逐维度对比

### 1. 调用链

```
Claude Code:
  用户 /reg-auto-req-analysis <id> [payload]
    → skill 在本地/工作区直接执行（无中间层）

deerflow-service:
  TFS-BUDDY → POST /api/v1/analysis (X-API-Key)
    → deerflow-service 组装 prompt（首轮/重提/人工反馈 3 种模板）
    → POST /api/threads/{collection}-{wid}/runs/wait  ← 阻塞最长 1800s
    → Gateway 后台 run_agent（agent_name=auto-analysis-agent, non_interactive=true）
    → sandbox 容器内执行 skill
    → publish_plan 写 Redis（HSET auto-req:qc:plan:{coll}:{wid}，含 run_id）
    → wait 返回 → deerflow-service 对账 run_id 变化 → COMPLETED / FAILED
    → TFS-BUDDY GET 轮询读 Redis 结果
```

### 2. 执行环境

| 维度 | Claude Code | deerflow-service（sandbox 容器） |
|---|---|---|
| 脚本所在目录 | 本地目录（**可写**） | `/mnt/skills/public/reg-auto-req-analysis`（**readonly 挂载**） |
| 工作根目录 | `auto-dev-work/`（文档约定） | `/mnt/user-data/workspace/auto-dev-work`（可写，绑定期 thread 数据） |
| `skills/` 相对路径 | 真实目录/拷贝 | **软链接** `skills -> /mnt/skills/public`（仅容器内有效，宿主机 broken） |
| 配置文件 tfs-config.json | 本地放一份（gitignore） | public 里**只有 template**（gitignore 不入库），且**写不进去**（readonly） |
| 命令审计 | 无 | SandboxAudit：记录每条命令；违规命令（如 `python3 -c` 改状态文件）**直接 BLOCK** |
| 命令输出可见性 | 正常可见 | agent 可见；但**网关日志只记命令字符串不记输出**，运维排查靠倒推 |
| 环境变量注入 | 用户 shell 环境 | 容器创建时注入 `REDIS_HOST/REDIS_PORT`（值来自 deerflow 配置）；**TFS_PAT/TFS_COLLECTION/TFS_PROJECT 不自动注入**，agent 得自己拼 |
| 超时 | 无硬限制 | `runs/wait` 阻塞 **1800s 上限** + `recursion_limit: 500` |
| 时区 | 本机 | gateway 日志 UTC，沙箱北京时间 |

### 3. 连接参数来源（关键）

skill 侧参数优先级（tfs_client.py:141-155 实测）：

| 参数 | 优先级 | 是否有 env 通道 |
|---|---|---|
| PAT | `--pat > TFS_PAT > tfs.pat` | ✅ 有 |
| collection | `--collection > TFS_COLLECTION > tfs.collection` | ✅ 有（但配置文件仍需非空，否则 CONFIG_INCOMPLETE） |
| project | `--project > TFS_PROJECT > tfs.project` | ✅ 有（同上） |
| **server / port** | **仅配置文件** | ❌ **没有 env 通道** |
| Redis 连接 | `REDIS_URL > REDIS_HOST/PORT/DB/PASSWORD > 配置 redis.* > 默认` | ✅ 完全有 |

**硬约束**：配置文件不存在 → `CONFIG_NOT_FOUND` 直接退出；`server/port/collection/project` 任一为空 → `CONFIG_INCOMPLETE`。**所以 tfs-config.json 在任何环境下都是必需品，PAT 可以走 env。**

### 4. 结果回传机制

| 环境 | 结果怎么回到调用方 |
|---|---|
| Claude Code | 对话里直接看报告 + TFS 已回写，人眼确认 |
| deerflow-service | **只认 Redis**：`publish_plan` 成功写 `run_id` 且值变化 → COMPLETED；否则 FAILED（报"plan run_id 未更新"）。HTTP 响应体不可信（曾出假成功事故），故意不看 |

### 5. 交互性

- skill 自身声明：**全程非交互**（硬约束第 4 条，禁 AskUserQuestion）
- deerflow-service：`context.non_interactive=true` 强制摘掉 `ask_clarification` 工具 → 两层非交互，一致，无冲突

### 6. MCP 工具

- 产品 MCP（tfs-requirements / gitnexus-team / cloudhis-source / db-knowledge）两种环境都靠外部配置注入（Claude Code 的 MCP 配置 / deerflow 的 extensions_config.json）
- 差异：Claude Code 可随时增删 MCP 重启生效；deerflow 的 skill 文档明确"会话启动时注入、当前会话不热加载，新装 MCP 须重启会话"

---

## 三、skill 中的隐式环境假设（给作者的重点）

以下假设在 Claude Code 里成立/无害，但在 deerflow-service 下**不成立或有风险**，262298 事故逐条实证过：

| # | 隐式假设 | 在 deerflow-service 下的实际 | 风险 |
|---|---|---|---|
| 1 | **"脚本同目录 tfs-config.json 可以创建/修改"** | public 是 readonly 挂载，`cp template → tfs-config.json` 必失败（Permission denied）；且 gitignore 导致 template 才是常态 | 高：agent 按文档提示补配置 → 撞墙 → 试错烧时间 |
| 2 | **"从 auto-dev-work/ 用相对路径 skills/... 能跑到脚本"** | 靠软链接（容器内有效）。链接一旦异常/被删，所有相对路径命令 No such file | 高：agent 陷入 find/grep 逆向工程 |
| 3 | **"配置连接参数可由模型按需传"** | server/port 无 env 通道，必须 --config 指向工作区文件；PAT 明文拼进命令行（日志可见，审计风险） | 中：配置来源混乱 |
| 4 | **"运行状态/计划文件可读可写"** | 可写，但 **SandboxAudit 会 BLOCK 用 python3 -c 修改状态/计划文件的行为**（262298 第二轮实测被拦） | 高：违反契约 + 被审计拦截 |
| 5 | **"run 可以长时间运行"** | runs/wait 1800s 硬上限，试错成本被放大；agent 反复 --help/grep 源码会耗尽预算 | 中：超时假失败 |
| 6 | **"结果由人/对话确认"** | 无人看过程，只认 Redis run_id；**任何终局都必须 publish_plan**，否则假失败 | 高：判定机制依赖 |
| 7 | **"命令签名以当前文档为准"** | 文档与脚本可能不同版本（apply 从 --plan 演进到 --id/--run-id），agent 读旧文档跑新脚本就逆向工程 | 高：版本漂移 |

---

## 四、给 skill 制作者的改进建议（按优先级）

1. **补"对接契约"文档章节**：明确 deerflow-service 的调用方式（runs/wait 阻塞、Redis run_id 对账、1800s 超时、non_interactive），让作者知道下游怎么消费结果。
2. **路径用法双轨化**：COMMANDS.md 的 CWD 段补充绝对路径用法 `python3 /mnt/skills/public/reg-auto-req-analysis/_lib/tfs/...`（沙箱兼容），相对路径保留给本地；并说明 `auto-dev-work/skills` 软链接机制。
3. **配置引导写死**：明确"配置一律用 `--config` 指向工作区 `tfs-config-<id>.json`（放 auto-dev-work 根），PAT 用 `TFS_PAT` env，**禁止 cp 到脚本目录**"。
4. **补硬禁令**：禁止用 `python3 -c`/临时脚本修改运行状态文件或执行计划 JSON（deerflow 的 SandboxAudit 会 BLOCK，且属契约违规）。
5. **env 支持补全**：`tfs.server/tfs.port` 无 env 通道，建议补 `TFS_SERVER/TFS_PORT`（或 `TFS_BASE_URL`），彻底支持"零配置文件"部署，也让 PAT 不落盘。
6. **命令签名版本说明**：apply（--id/--run-id）与 apply-legacy（--plan）的演进关系写清楚，注明旧文档对应旧脚本，避免 agent 跨版本逆向工程。
7. **终局必发布 Redis**：在 RUNBOOK 中强调每个终局（含 NEED-INFO/NEED-REVIEW/MANUAL-REVIEW/SKIP）都要走到 `publish_plan`，因为 deerflow-service 以 `run_id` 变化为唯一成功信号。

---

## 五、环境速查（排障用）

| 项 | Claude Code | deerflow-service |
|---|---|---|
| 服务地址 | - | `http://172.16.0.192:2026/deerflow-service/api/v1/analysis` |
| 超时 | - | runs/wait 1800s；recursion_limit 500 |
| Redis key | - | `auto-req:qc:plan:{collection}:{wid}`（HGETALL 可查） |
| 成功判定 | 人 | Redis `run_id` 变化（baseline 对比） |
| 审计 | - | SandboxAudit（BLOCK 级：python3 -c 改状态/计划文件） |
| 时区 | 本机 | 网关日志 UTC（+8=北京）、沙箱北京 |
| 相关文档 | skill 内 COMMANDS/RUNBOOK | `docs/my-docs/deerflow-service与Agent交互逻辑.md` |
