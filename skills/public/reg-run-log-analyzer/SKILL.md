---
name: reg-run-log-analyzer
description: 复盘一次 reg-auto-req-analysis-offline（离线需求分析）run 的执行过程与产物。当用户要分析/复盘某个离线 run（"分析 run_20260902_143210_242042_a3f81c"、"复盘 242042 这次离线 run 干了啥"、"242042 离线日志复盘"、"查一下最近的需求分析 run"）时使用，参数为 run_id 或工作项号。优先读磁盘产物与跨运行留痕，沙箱零依赖；可选 docker.sock 网关日志做步骤级补充。
---

# 离线 Run 日志分析器

复盘一次 `reg-auto-req-analysis-offline` run 的全过程：每一步做了什么、产出什么、终局如何、是否异常、需要谁跟进。输出结构化复盘报告。

> 离线版与生产版（连 TFS/Redis）不同：**不写 TFS、不连 Redis、没有 `PM-AI-MANUAL-PASSED`/`禁止覆盖` 硬闸**。复盘的权威数据源是 run 目录三份产物 + 跨运行留痕（测试日志），而非网关流水。步骤词典与报告模板见 `references/analysis-rules.md`。

## 适用场景

用户想了解某个需求在 DeerFlow 离线模式下自动需求分析的执行过程和结果。典型问法：

- "分析 run_20260902_143210_242042_a3f81c"
- "复盘 242042 这次离线 run 干了啥"
- "242042 离线日志复盘一下"
- "查一下最近的需求分析 run"

## 沙箱执行约束（必读）

- 本 skill 在沙箱中运行，**命令必须能在沙箱跑通**。首选方案是读磁盘产物 + 调用自带零依赖脚本 `analyze_offline_run.py`（纯标准库，不需要 docker）。
- 线上模式 run 目录 `/mnt/user-data/outputs/<run_id>/` 与跨运行留痕 `/mnt/skills/过程文件/` 是挂载卷，跨 run 持久、可直接读。
- 本地模式 run 目录 `过程文件/<id>/<run_id>/` 在 workspace，同样可直接读。
- `fetch_gateway_logs.py`（docker.sock）**仅作可选补充**：在沙箱没挂 docker.sock 时整步跳过，不影响主复盘。它抓的是网关步骤级 `[SandboxAudit]`，用来还原"离线 skill 在它自己沙箱里具体执行了哪些命令"，属于过程细节增强，不是必需。

## 执行步骤

### 1. 确认目标

- **已知 run_id**：正则 `^run_\d{8}_\d{6}_\d+_[0-9a-fA-F]{4,8}$`，直接用它定位。
- **只有工作项号**（纯数字，如 242042）：走 `--by-item` 列出该号下所有 run（新→旧），让用户/自己选目标；同一需求多次 run 要逐个复盘。
- **"最近的 run"**：用 `--by-item` 不行时，先 `analyze_offline_run.py` 不带 id 不行——改为按工作项号或先问用户要 run_id / 工作项号（离线 run 不保证有统一 thread 索引）。

### 2. 拉取并结构化（沙箱零依赖，首选）

用自带脚本一次性定位 + 读产物 + 抽跨运行留痕。脚本自动探测线上/本地模式。

```bash
# 已知 run_id：定位并复盘（含 run 目录产物、测试日志条目、可选审计记录）
python3 /mnt/skills/public/reg-run-log-analyzer/_lib/analyze_offline_run.py run_20260902_143210_242042_a3f81c

# 本地模式（脚本随 skill 在 workspace）：同上，路径换成相对/绝对 workspace 路径
python3 skills/public/reg-run-log-analyzer/_lib/analyze_offline_run.py run_20260902_143210_242042_a3f81c

# 按工作项号找最近 run
python3 /mnt/skills/public/reg-run-log-analyzer/_lib/analyze_offline_run.py 242042 --by-item

# 重跑离线 skill 自带结构自检，确认产物是否通过收尾闸门
python3 /mnt/skills/public/reg-run-log-analyzer/_lib/analyze_offline_run.py run_20260902_143210_242042_a3f81c --check

# 机器可读 JSON（需要逐字段处理时）
python3 /mnt/skills/public/reg-run-log-analyzer/_lib/analyze_offline_run.py run_20260902_143210_242042_a3f81c --json
```

脚本输出已覆盖：模式、终局 verdict 及含义、执行窗口与总耗时、建议标签/流转、知识状态与 warnings、改动点、run 目录文件清单、结构自检结果（加 `--check`）、跨运行测试日志条目、审计记录（若存在）。

### 3. （可选）网关步骤级追踪

仅当磁盘产物不够、需要还原"离线 skill 在它自己沙箱里具体跑了哪些命令"时，用 docker.sock 脚本抓网关日志。沙箱未挂 docker.sock 则跳过本步。

```bash
# 首选：零依赖直连 docker.sock
python3 /mnt/skills/public/reg-run-log-analyzer/_lib/fetch_gateway_logs.py <thread_id> --since-hours 72 --tail 2000
# 备选：沙箱内有 docker CLI 时
docker logs deer-flow-gateway --since 72h 2>&1 | grep "<run_id 或 thread_id>"
```

- 离线 run 走 DeerFlow gateway 流式链路，网关日志里会有 `Run created`/步骤与 `[SandboxAudit] "command":...`。
- 按 `references/analysis-rules.md` 第二节「网关步骤级线索」把命令归类：路由/经验候选/结构自检/建 run。
- 若报 `Permission denied` 或 `/var/run/docker.sock 不存在`：docker.sock 未挂，**直接跳过本步**，主复盘已基于磁盘产物完成。

### 4. 逐段解析

按 `references/analysis-rules.md`：
- 用 run_id 形态与产物映射确认 run 边界（步骤一）。
- 把产物映射到离线 flow 步骤 0–9（步骤二字典），形成"做了什么"。
- 按结果判定表（步骤三）标注每个阶段 pass/拦截/异常/降级。
- 用终局速查（步骤四）解释 verdict 含义。

### 5. 输出报告

按 `references/analysis-rules.md` 第五节模板，四部分：
① 这是什么（模式/工作项/run_id 清单/执行窗口）→ ② 分步结果表（按离线步骤 0–9：步骤 | 干了啥 | 结果/证据）→ ③ 关键观察（NEED-* 挡点、MANUAL-REVIEW-STOP 高风险、自检 FAIL、DEGRADED/warnings、时序异常，附证据）→ ④ 一句话结论（是否收尾、终局、产物、自检、需谁跟进）。

## 注意事项

- `NEED-INFO`/`NEED-REVIEW` 是**质控双轮收敛的设计内状态，不是失败**；报告中说明被什么判据挡下、责任方是谁、下一步（责任方补充后重触发新 run）。
- `MANUAL-REVIEW(-STOP)` 是技术证据不足或命中高风险的**强制人工移交**，不是异常。
- run 目录只有 `输入载荷`/`调用入参`、无任何产物 → 判定"建 run 但未收尾"（中断/超时/沙箱并发上限），提示去服务器确认 run 真实状态。
- 报告主体用业务语言；verdict / 标签 / 证据 ID（`kb:/wiki:/req:`）/ 闭合标识（`CLOSE/PROVIDE`）保留原文作证据。
- 离线版**没有** `PM-AI-MANUAL-PASSED`/`禁止覆盖`/`DOWNSTREAM_TERMINAL_TAGS` 这类 TFS/Redis 硬闸，不要把 NEED-/MANUAL 误读成被外部拦截。
