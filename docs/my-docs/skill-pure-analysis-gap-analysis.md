# reg-auto-req-analysis-test 改造差距分析

> 核验对象：`E:\03-Vibe-Coding\deer-flow\skills\public\reg-auto-req-analysis-test\`
> 对照基准：`docs/my-docs/skill-pure-analysis-engine-plan.md`（v1.1 纯分析引擎方案）
> 核验时间：2026-08-31

## 一句话结论

**未达到预期（首验 + 2026-08-31 18:49 复验结论一致）。** 该 test skill 仍是改造前的"全能型"版本（自己读写 TFS、写 Redis、调 pipeline.py、产出 JSON 执行计划），v1.1 方案的"纯分析引擎"改造（去 TFS/Redis IO、自然语言+标记输出、只落盘 md）**完全未实施**。注意：`skills/public/reg-auto-req-analysis-test/` 在 git 中为 **untracked（游离副本）**，无版本追溯；其 SKILL.md 修改时间虽为 18:46，但内容与首验时一致，核心改造点一项未做（详见文末复验章节）。

## 逐项核验

| # | v1.1 方案要求 | 实测结果 | 判定 |
|---|---|---|---|
| 1 | 新增 `_lib/input-contract.md` | 文件不存在 | ❌ 未做 |
| 2 | 新增 `_lib/output-format.md`（自然语言+标记） | 文件不存在 | ❌ 未做 |
| 3 | 新增 `_lib/io-boundary.md` | 文件不存在 | ❌ 未做 |
| 4 | 删除 `_lib/tfs/redis_client.py` | 仍在 | ❌ 未删 |
| 5 | 删除 `_lib/tfs/EXECUTION_CONTRACT.md` | 仍在（且被 SKILL.md §111 引用） | ❌ 未删 |
| 6 | 删除 `_lib/tfs/tfs-config.json` / `.template.json` | 仍在 | ❌ 未删 |
| 7 | 删除 `_lib/tfs/test_tfs_tools.py` | 仍在 | ❌ 未删 |
| 8 | 输出改用 `<phase>/<verdict>/<tags>` 等标记 | SKILL.md 全文 0 处使用新标记 | ❌ 未改 |
| 9 | 产物改为 `变更方案_<id>_<run_id>.md` | 仍用旧命名 `分析结果_/分析草稿_/执行计划_*` | ❌ 未改 |
| 10 | SKILL.md 去除 TFS/Redis/pipeline/apply/validate | SKILL.md 仍含 32 处相关引用（tfs_client.py/pipeline.py/Redis/apply/validate/执行计划） | ❌ 未改 |
| 11 | description 改为"纯分析引擎" | 仍写"最终产出一份受约束的 TFS 执行计划""TFS/Redis 是否写入" | ❌ 未改 |

## 关键现象

- **SKILL.md 启动检查（步骤 0）仍要求**：`tfs_client.py precheck`、从 bundle 执行 pipeline 命令、`pipeline.py apply --execute` 真实写 TFS。
- **硬约束 §1-§2** 仍规定"TFS、Redis 和知识库只使用 COMMANDS.md 列出的脚本"——即 skill 自己直接驱动 TFS/Redis IO，与"TFS-BUDDY 接管 IO"的架构**直接冲突**。
- 产物边界仍要求生成 `分析结果_<id>_<run_id>.json`（report 块）→ 由 executor 物化 Markdown → 走 `pipeline.py apply`。这与方案"不再生成 `执行计划_*.json`、只落盘 `变更方案_*.md`"相反。
- 新输出格式（`自然语言 + <phase>/<verdict> 标记`、SSE 流中供 TFS-BUDDY 正则抽取）**全文件 0 处实现**。

## 风险

若当前 `auto-analysis-agent`（或 TFS-BUDDY 调用的 agent）指向此 skill，则运行时会：
1. 自行调 `tfs_client.py` / `pipeline.py` 写 TFS、写标签、传附件、流转状态——**等于没改造**；
2. 自行写 Redis——与"去 Redis、改流式"目标背道而驰；
3. 产出 JSON 执行计划而非 md——TFS-BUDDY 侧据 v1.1 写的"解析 `<artifact>` 标记+读 `变更方案_*.md`"逻辑**完全对接不上**。

## 建议

1. **确认该 test skill 的用途**：它大概率是改造前的旧副本/快照（description 自称"测试副本"）。如果是误当成"已调整版"，应丢弃或重新定位。
2. **按方案 Phase 1→4 真正落地改造**（而非拷贝旧版）：
   - Phase 1：补 `input-contract.md` / `output-format.md` / `io-boundary.md`
   - Phase 2：重写 SKILL.md（删 TFS/Redis 命令，改自然语言+标记驱动）
   - Phase 3：更新 `attachment-evidence.md` / `testing-log.md`
   - Phase 4：删除 `redis_client.py` / `EXECUTION_CONTRACT.md` / `tfs-config*.json` / `test_tfs_tools.py`
3. 改造完成后，再让 TFS-BUDDY 的 SSE 解析层（v1.1 §4.6）与之对接，端到端联调。

> 注：TFS-BUDDY 侧链路（流式消费 SSE、手动回写附件按钮、agent 名 env 化）此前已按新架构落地；但 skill 本体未改造，导致两端对不上。

---

## 复验（2026-08-31 18:49，针对 18:46 的"修改"）

**复验触发**：用户反馈 test skill 似乎已被修改（SKILL.md mtime 18:46），要求复验是否达到纯分析引擎预期。

**复验方法**：
1. 核对目录结构（`find _lib` + 关键文件存在性），对照首验 11 项硬指标；
2. 全文 grep SKILL.md 是否出现 v1.1 新标记/新架构词（`纯分析引擎` / `input-contract` / `output-format` / `io-boundary` / `<phase>` / `<verdict>` / `<artifact>` / `TFS-BUDDY` / `变更方案_`）；
3. 检查 git 状态确认修改来源与可追溯性。

**复验结论：仍未达到预期，18:46 的"修改"未实施任何 v1.1 改造。**

### 复验证据

| # | 核验项（v1.1 要求） | 复验结果（18:46 后） | 判定 |
|---|---|---|---|
| 1 | 新增 `_lib/input-contract.md` | **MISSING** | ❌ |
| 2 | 新增 `_lib/output-format.md` | **MISSING** | ❌ |
| 3 | 新增 `_lib/io-boundary.md` | **MISSING** | ❌ |
| 4 | 删除 `_lib/tfs/redis_client.py` | **EXISTS** | ❌ |
| 5 | 删除 `_lib/tfs/EXECUTION_CONTRACT.md` | **EXISTS**（且 SKILL.md §产物边界 仍引用其 §156） | ❌ |
| 6 | 删除 `_lib/tfs/tfs-config.json` / `.template.json` | **EXISTS** | ❌ |
| 7 | 删除 `_lib/tfs/test_tfs_tools.py` | **EXISTS** | ❌ |
| 8 | 输出改用 `<phase>/<verdict>/<artifact>` 等标记 | SKILL.md 全文 grep 上述标记 → **0 匹配** | ❌ |
| 9 | 产物改为 `变更方案_<id>_<run_id>.md` | SKILL.md 硬约束§10 仍要求生成 `分析结果_*`、`执行计划_*`；grep `变更方案_` → **0 匹配** | ❌ |
| 10 | SKILL.md 去除 TFS/Redis/pipeline/apply/validate | 启动检查(§步骤0)仍要求 `tfs_client.py precheck`；硬约束§1 仍规定 TFS/Redis 只走 COMMANDS.md；全文仍含 pipeline/apply/执行计划 引用 | ❌ |
| 11 | description 改为"纯分析引擎" | 仍写"最终产出一份受约束的 TFS 执行计划""TFS/Redis 是否写入" | ❌ |

### 关键补充发现

- **test skill 是 git untracked 游离副本**：`git status` 显示 `?? skills/public/reg-auto-req-analysis-test/`，无提交历史。18:46 的 mtime 仅为本地写入，无法追溯"改了什么"；且 SKILL.md 核心章节（description / 启动检查 / 硬约束 / 产物边界）与首验时逐字一致，说明这次"修改"并未触及 v1.1 改造点，可能只是同步/拷贝触发的 mtime 变化，或只改了与纯分析引擎无关的边缘内容（如 references 下新增的 run-failure-diagnosis / round-diagnosis-rules 等文档——这些属既有流程，非 v1.1 改造）。
- **两端仍对不上**：TFS-BUDDY 侧已按 v1.1 新架构就绪（流式消费 SSE、手动回写附件、agent env 化），但 skill 本体仍是旧全能型——若指向此 skill 运行，会自行写 TFS/Redis、产出 JSON 执行计划，与 TFS-BUDDY 的"解析 `<artifact>` + 读 `变更方案_*.md`"逻辑完全对接不上。

### 复验后的建议（与首验一致，强调）

1. **丢弃或重新定位这个 test 副本**：它不是"已调整版"，实质是改造前旧快照。继续在此副本上"修补"只会混淆。
2. **按 v1.1 Phase 1→4 真正落地改造**（详见 `docs/my-docs/skill-pure-analysis-engine-plan.md`）：
   - Phase 1：补 `_lib/input-contract.md` / `output-format.md` / `io-boundary.md`
   - Phase 2：重写 SKILL.md（删 TFS/Redis 命令，改自然语言 + 标记驱动，改 description）
   - Phase 3：更新 `attachment-evidence.md` / `testing-log.md`
   - Phase 4：删除 `redis_client.py` / `EXECUTION_CONTRACT.md` / `tfs-config*.json` / `test_tfs_tools.py`
3. 改造完成的 skill 应纳入 git 跟踪（去除 untracked 状态），再让 TFS-BUDDY 的 SSE 解析层与之端到端联调。

---

## 复验（2026-09-01 08:56，针对 08:55 的"更新"）

**复验触发**：用户反馈 test skill 已更新（SKILL.md mtime 09-01 08:55），要求重新核验是否达到纯分析引擎预期。

**复验方法**：
1. `find _lib` + 关键文件存在性核对（3 契约文件 / 4 应删 tfs-redis 文件）；
2. 全文 grep SKILL.md 是否出现 v1.1 新标记（`纯分析引擎` / `input-contract` / `output-format` / `io-boundary` / `<phase>` / `<verdict>` / `<artifact>` / `TFS-BUDDY` / `变更方案_` / `自然语言` / `标记输出`）；
3. 重读 SKILL.md 全文（182 行）核对 description / 启动检查 / 硬约束 / 产物边界是否转型。

**复验结论：仍未达到预期，08:55 的"更新"未实施任何 v1.1 改造。**

### 复验证据

| # | 核验项（v1.1 要求） | 复验结果（08:55 后） | 判定 |
|---|---|---|---|
| 1 | 新增 `_lib/input-contract.md` | **MISSING** | ❌ |
| 2 | 新增 `_lib/output-format.md` | **MISSING** | ❌ |
| 3 | 新增 `_lib/io-boundary.md` | **MISSING** | ❌ |
| 4 | 删除 `_lib/tfs/redis_client.py` | **EXISTS** | ❌ |
| 5 | 删除 `_lib/tfs/EXECUTION_CONTRACT.md` | **EXISTS**（SKILL.md §产物边界/§按条件读取 仍引用其 §156） | ❌ |
| 6 | 删除 `_lib/tfs/tfs-config.json` / `.template.json` | **EXISTS** | ❌ |
| 7 | 删除 `_lib/tfs/test_tfs_tools.py` | **EXISTS** | ❌ |
| 8 | 输出改用 `<phase>/<verdict>/<artifact>` 标记 | grep 上述 11 个标记词 → **0 实质匹配**（唯一命中行 15 是"只返回 `CONFIG_NOT_FOUND`"的"只返回"二字，非 v1.1 标记） | ❌ |
| 9 | 产物改为 `变更方案_<id>_<run_id>.md` | SKILL.md 硬约束§10 仍要求 `分析结果_*`/`执行计划_*`；grep `变更方案_` → **0 匹配** | ❌ |
| 10 | SKILL.md 去除 TFS/Redis/pipeline/apply | 启动检查(§步骤0)仍要求 `tfs_client.py precheck`；§1 仍规定 TFS/Redis 只走 COMMANDS.md；全文仍含 pipeline/apply/执行计划/Redis 引用（如 §139 Redis `CORE_READY`/`FINAL` 发布、`pipeline.py apply` 物化报告） | ❌ |
| 11 | description 改为"纯分析引擎" | 仍写"最终产出一份受约束的 TFS 执行计划""TFS/Redis 是否写入" | ❌ |

### 关键补充发现

- **SKILL.md 08:55 更新后仍是旧全能型结构**：重读全文确认 description、启动检查（precheck/capture-source）、硬约束（TFS/Redis 只走官方入口）、产物边界（`分析结果_*.json` → executor 物化 → `pipeline.py apply`）、核心状态机（`pipeline.py init-run`/`bind-trace`/`diagnose-run`）等**全部保留**，与首验/18:49 复验时逐字一致。08:55 的 mtime 变化未带来任何纯分析引擎转型。
- **_lib/tfs/ 目录完整保留**：除上面 4 个应删文件外，`pipeline.py` / `tfs_client.py` / `attachment_runtime.py` / `attachment_converter.py` / `analysis_contract.py` / `field-flow.md` 等均在，skill 仍自带完整 TFS/Redis 写入链路。
- **inline-source-contract.md 仍在**（非新增）：这是既有"离线内联源"分支契约，TFS-BUDDY 走此分支不查 TFS；但它**不是** v1.1 要求的纯分析引擎契约（v1.1 要的是 `input-contract`/`output-format`/`io-boundary` 三个新文件 + SKILL.md 去 TFS/Redis 改造）。inline-source-contract 只是让 skill 在"传 work_item object"时跳过 TFS 查询，并未去掉写 TFS/Redis 的其余分支，也未改成自然语言+标记输出。
- **结论不变**：这个 test 副本实质仍是改造前旧快照，08:55 的"更新"未触及 v1.1 改造点。继续在此副本上修补无意义，应按 `skill-pure-analysis-engine-plan.md` Phase 1→4 真做一遍，或丢弃重建。

---

## 231198 走错 skill 的精确定位（2026-09-01 10:34）

**本机 vs 服务器对拍结论**：本地 `reg-auto-req-analysis-test/SKILL.md` 当前含纯分析标记（第 24 行「离线源永久隔离」、第 148 行「参数含 `work_item` object → 只走 `_lib/inline-source-contract.md`，不执行 precheck」）；而服务器 231198 日志（2026-09-01 10:05 跑）的行为是**全能版**（precheck 查 TFS、`list-iterations` 匹配到提交的 9 月迭代之外的 7 月迭代、写 Redis、读 `/mnt/skills/public/reg-auto-req-analysis/SKILL.md`、触发 `[FORCED STOP] Tool bash called 50 times`）。**因此本机 -test 已纯分析，服务器 -test 文件 BODY 仍是全能版 → 部署内容错位，与 agent 绑定无关。**

### DeerFlow skill 解析机制（代码实证）

skill 不是按目录名解析，而是按 **SKILL.md frontmatter 的 `name` 字段**解析，且解析后**无条件信任文件 BODY**：

1. `skills/parser.py:151` 把 `Skill.name` 设为 `metadata.get("name")`（frontmatter 字段），与目录名解耦；
2. `skills/storage/skill_storage.py:257` 用 `skills_by_name[skill.name] = skill` 去重，匹配键是 frontmatter name；
3. `agents/lead_agent/agent.py:429` `available_skills = set(agent_config.skills)`；`:444` 用 `skill.name in available_skills` 严格交集过滤；
4. 过滤通过后，注入的是该 Skill 的 **BODY 文本**（即 SKILL.md 正文），DeerFlow 不校验正文内容是否符合"纯分析"预期。

→ **绑定决定"加载哪个文件"，但不保证"文件内容对不对"。** 这是 Jun 觉得"可怕"的根本原因：agent 绑定 -test 是必要但不充分条件，文件 BODY 是部署落地的，没有任何内容完整性校验。

### thread 复用是烟雾弹（已证伪）

`thread_id` 由 `collection-workItemId` 确定性生成（`gatewayStreamService.js:241`），旧 thread 会被复用；但 thread 只存**对话历史**，不存 agent/skill 绑定。skill 在每次 run 时按 `agent_name → config.skills → frontmatter name` 重新解析（`services.py` 的 `start_run → resolve_agent_factory → make_lead_agent`）。即便 231198 复用了某次非 -test 跑过的 thread，也只会在新 run 上下文里堆叠历史，**不会改变本次加载的 skill**。

### 两种服务器端故障模式（行为完全一致，需上服务器区分）

231198 的"agent 名 -test + 跑出全能版"只有两种服务器端成因：

| # | 故障点 | 验证命令（服务器 `/mnt` 下） |
|---|---|---|
| A | `auto-analysis-agent-test/config.yaml` 的 `skills:` 实际绑定了 `reg-auto-req-analysis`（非 -test） | `grep -A2 skills /mnt/agents/auto-analysis-agent-test/config.yaml` |
| B | `skills/public/reg-auto-req-analysis-test/SKILL.md` 的 frontmatter `name` 是 `reg-auto-req-analysis-test`（过过滤），但 BODY 是全能版（被拷贝/覆盖错内容） | `grep -c "inline-source-contract\|apply --offline" /mnt/skills/public/reg-auto-req-analysis-test/SKILL.md`（应 >0；若为 0 则 BODY 是全能版） |

> 注：若服务器 -test 文件的 frontmatter `name` 也被写成 `reg-auto-req-analysis`（拷贝时连 name 都没改），则 `skill.name in available_skills` 为 False → 该 skill 被过滤掉 → agent 拿到的是**无 skill 的通用行为**，而非全能版。而 231198 实测是明确的全能版工作流，故可排除此情形，锁定为 **A 或 B**，且 frontmatter name 必为 `reg-auto-req-analysis-test`。

### 根因小结

- **不是** DeerFlow 解析 bug，**不是** thread 复用导致，**不是** agent 绑定错（日志 `Agent: auto-analysis-agent-test` 证明路由正确）。
- **是** 服务器部署内容错位：绑定正确地指向了 `reg-auto-req-analysis-test` 这个文件，但该文件的 BODY 是全能版（或 agent config 的 skills 列表本身指向了非 -test）。
- 修复方向：上服务器确认上述 A/B 两项；若是 B，把本机这份纯分析版 `reg-auto-req-analysis-test/SKILL.md` 同步到服务器的 `/mnt/skills/public/reg-auto-req-analysis-test/SKILL.md`；若是 A，改服务器 agent config 的 `skills:`。

---

## 全量日志实锤（app-2026-09-01.log，2026-09-01 10:39 复核）

> 数据源：`E:\03-Vibe-Coding\tfs-server\backend\logs\app-2026-09-01.log`（tfs-server 全量日志，含 GatewayStream 代理的 SSE 流式输出）。本次从全量日志里完整还原了 231198 的执行过程，比 `analysis_log_231198.txt`（仅 agent 侧工具调用）信息更全。

### 231198 完整执行时间线（2026-09-01）

| 时间 | 事件 | 证据行 |
|---|---|---|
| 10:04:33 | 用户打开 231198 需求页：TFS 批量查询 + 本地库 `stats_requirements` 查询 + 附件元数据查询 | 17169-17214 |
| 10:05:58 | 附件元数据同步（4 个附件，含 1 个 `AI 分析产出` md）+ 需求数据 `INSERT OR REPLACE` → 触发 `[GatewayStream] 开始流式分析`，`agentName=auto-analysis-agent-test`，`tid=WN_Data_Platform-231198` | 17675-17768 |
| 10:05:59 | Gateway 返回 200，`text/event-stream`；`run_id=324f5375`；插入 `ai_analysis_tasks`（run_mode=**foreground**，status=**processing**） | 17773-17796 |
| 10:05:59 起 | agent 流式输出：**加载的 skill 是 `reg-auto-req-analysis`（非 -test）** —— 读 `/mnt/skills/public/reg-auto-req-analysis/_lib/COMMANDS.md`、`config/qc-rules.md`；执行 precheck（`Precheck passed - TFS connectivity confirmed`）；`list-iterations` 拿到 3-4 月旧迭代；QC 判 `NEED-REVIEW`，应用到 TFS 标签 `PM-AI-QC-NEED-REVIEW`，写 `待补充清单_*` 到 `过程文件/` | 17812/17827/17852/17877/17941/41965/42762 |
| ⚠️ 全程 | system-reminder **`current_date=2026-07-30, Thursday`**（应为 2026-09-01） | 17812/17916/18000/18108/41984 |
| 10:06:00 | `ai_analysis_poll` 定时任务轮询 `324f5375` → **HTTP_500**（run 还在跑，poll 端点报错，独立 bug） | 18115-18156 |
| 10:06:39 | chunk 1250 → **`[FORCED STOP] Tool bash called 50 times`**，agent 被强停 | 41977-41980 |
| 10:08:55 | 后端兜底把 `report_markdown` 落库（内容开头 `Let me read the raw full skill c...`，约 5499 字，是 agent 中途思考碎片，**非真实报告**）；status **始终未从 processing 变更** | 90564-90588 |

### 三个实锤结论

**1. 走错 skill —— 全量日志直接坐实（之前只是推断，现在有流式内证）**
SSE 流里 agent 实际加载并执行的 skill 就是非 -test 版：
- frontmatter 原文 `name: reg-auto-req-analysis`（`app-2026-09-01.log:17941`）；
- 读的是 `/mnt/skills/public/reg-auto-req-analysis/_lib/COMMANDS.md`（`:17827`）与 `/mnt/skills/public/reg-auto-req-analysis/config/qc-rules.md`（`:17877`）——**非 -test 目录**；
- 行为含 precheck（`:17852`）、写 Redis（`:17912` `r.setex(redis_key...)`）、回写 TFS 标签（`:41965` `{"ok":true,"id":231198,"tag":"PM-AI-QC-NEED-REVIEW"}`）。

→ 服务器上 `auto-analysis-agent-test` agent **实际加载的是 `reg-auto-req-analysis`（非 -test）**。这把 §231198 精确定位里的"A 或 B"收敛为：**A（agent config 的 `skills:` 实际绑了非 -test）最可能是根因**；因为若 -test 文件 frontmatter 是 `reg-auto-req-analysis-test`（过过滤）而只是 BODY 错，那么流里出现的 frontmatter 应是 `reg-auto-req-analysis-test`、读取路径应是 `reg-auto-req-analysis-test/...`。而实测 frontmatter 与读取路径**都是非 -test**，说明要么 config 直接绑了非 -test（A），要么 -test 目录是一份 frontmatter 都未改的 verbatim 非 -test 拷贝（B 的极端态）。两种都指向"**服务器 -test 部署 = 非 -test 内容**"。仍需上服务器 `grep -A2 skills /mnt/agents/auto-analysis-agent-test/config.yaml` 一锤定音。

**2. thread 复用确实惹了祸 —— 但不是"走错 skill"，而是"日期被冻结在 07-30"（修正此前"烟雾弹"结论）**
- 231198 的 `current_date` 全程是 `2026-07-30`，而**同日、同服务器、同 skill 的另一 run（244266，09:28 触发）正确显示 `2026-09-01`**（`app-2026-09-01.log:4931`）。
- 231198 的 thread `WN_Data_Platform-231198` 早在 **2026-07-30** 就被创建过（证据：`待补充清单_231198_run231198` 附件 `created_date=2026-07-30T13:25:45`、comment=`AI 分析产出`，行 17456/17726；以及此前的 `analysis_log_231198.txt`）。复用该 thread 时，DeerFlow 把 thread 创建时的 `current_date` 系统提醒一并带进了 09-01 的 run。
- 后果：`list-iterations` 拿到的迭代最晚 finish 在 2026-04，而"今天"被当成 07-30 → 时效 QC 判定"已过期/时间紧张" → 错判 `NEED-REVIEW`。**这正是用户直觉"旧 thread 导致问题"的真相，只是机制是日期冻结、不是 skill 切换。**
- 修复建议：每次 run 强制以**运行时真实日期**覆盖 `current_date`（在 `gatewayStreamService.js` 的 runPayload 或 DeerFlow system 消息里注入 `datetime.now()`），不要在复用 thread 时沿用历史 system-reminder。

**3. 强停 + 脏落库 + 状态卡死（三个次生 bug）**
- agent 在 50 次 bash 调用处被 `[FORCED STOP]`（`:41980`），这是非 -test skill 疯狂调 bash 读文件/写文件/查 TFS 导致的，进一步证明加载的不是纯分析版。
- 强停后，后端兜底落库的 `report_markdown` 开头是 `Let me read the raw full skill c...`（agent 中途思考碎片），不是可用的 QC 报告 → 脏数据。
- `ai_analysis_tasks` 的 `status` **从插入到日志末尾始终是 `processing`**，没有任何 `→completed/error/force_stopped` 的收尾更新（grep `UPDATE ai_analysis_tasks SET status` 全文件 0 命中）。前端会一直显示"分析中"。
- `ai_analysis_poll` 在 run 进行中轮询返回 HTTP_500（`app-2026-09-01.log:18156`），poll 端点对"进行中"状态处理有 bug，需单独修。

### 修正后的根因排序

1. **P0 部署内容错位**：服务器 `auto-analysis-agent-test` 实际加载 `reg-auto-req-analysis`（非 -test）。`agentName` 路由正确，但 skill 内容是全能版 → 自读 TFS、写 Redis、回写标签、狂调 bash → 触发 50 次强停。
2. **P0 thread 复用冻结 current_date**：复用 07-30 旧 thread 把"今天"锁成 07-30，导致时效/迭代 QC 整体误判。
3. **P1 状态机收尾缺失**：强停/流结束后 `status` 不收尾，脏 `report_markdown` 入库。
4. **P2 poll 端点 500**：run 进行中轮询报错。

> 注：结论 1 与"231198 精确定位"章节一致（部署错位），结论 2 修正了原"thread 是烟雾弹"的说法——thread 复用虽不切换 skill，但会冻结 `current_date`，是本次错判的独立贡献因素。
