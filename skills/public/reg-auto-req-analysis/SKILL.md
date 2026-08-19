---
name: reg-auto-req-analysis
description: "This skill should be used when a TFS 需求工作项需要自动化准入路由、质控与需求分析一体化处理。It runs non-interactively: 先排除纯联调支持（SKIP-ANALYSIS），再做阶段一质控（NEED-INFO/NEED-REVIEW/PASS），PASS 闸后阶段二分析（AUTO-ANA/MANUAL-REVIEW），一个 run_id 贯穿，最终产出一份受约束的 TFS 执行计划。"
---

# 自动化需求处理（auto-req-analysis）

## 启动检查（步骤 0，禁止跳过）

在执行任何工作项命令前，从仓库根目录**完整读取** [`_lib/COMMANDS.md`](_lib/COMMANDS.md) 和 [`_lib/RUNBOOK.md`](_lib/RUNBOOK.md)。随后执行 `tfs_client.py precheck`；任一文件不可读或 precheck 失败时按失败关闭处理，不得自行补工具。此外，尽力读取 `过程文件/经验记忆.md`（跨运行经验先验，见 [`_lib/skill-memory.md`](_lib/skill-memory.md)）：该文件不存在或不可读属正常（首轮无此文件），仅记 `memory=absent` 并继续，**不纳入上一句"任一文件不可读→失败关闭"**。

## ⛔ 硬约束

1. **只用官方入口**：TFS、Redis 和知识库只使用 `COMMANDS.md` 列出的脚本、子命令或 MCP 工具。本 skill 的工作项流程不得直接调用 `tfs_client.py` 写子命令；TFS 写入只能来自校验通过的 `pipeline.py apply`，Redis 写入只能由 pipeline 内部发布。
2. **禁止临时客户端和旁路命令**：不得使用 `python3 -c`、Python heredoc、临时 Python/shell/jq 脚本、`redis-cli` 或直接 `curl` 操作 TFS/Redis，也不得为解读官方 JSON 输出另写脚本。
3. **安装只走附件运行器**：只有官方 `attachment_runtime.py` 可按实际附件格式，在 `过程文件/.runtime/attachments/` 的隔离缓存中安装锁定 Python 依赖，并按允许列表补齐 LibreOffice。模型、临时脚本和其它步骤仍不得执行 `pip`、`uv pip`、`brew`、`apt` 等安装命令。
4. **禁止交互**：不得使用 `AskUserQuestion`、向用户提问或等待确认。新运行固定使用 `confirmation_policy=qc-single-batch-v1`：全部 PM 可回答、且会改变业务方案或验收的缺口在 `QC PASS` 前合并进同一份 checklist；分析计划固定 `analysis_gaps=[]`。重分析诊断所需的"上轮补充信息"由编排器随调用参数传入（见 [`references/round-diagnosis-rules.md`](references/round-diagnosis-rules.md)），同样不得以交互方式获取。
5. **产品路由与源码核验**：非 SKIP 工作项必须先按 `System.AreaPath` 首级（缺失才用 `teamProject`）唯一路由到 `product_id`，再调用该产品 profile 的 MCP；未解析时禁止回退默认产品或借用其它产品服务。禁止无锚点全仓搜索和大范围阅读源码。源码核验须先取得“精确 repo + API/路径/符号/技术字面量”锚点：优先来自当前产品 GitNexus，也可来自已解析附件、当前工作项明确技术信息、菜单索引或 wiki 中可追溯的精确锚点；随后才可用当前产品源码 MCP 的 `search_symbol` / `search_source` 有限核验 3–5 个关键文件。GitNexus 不可用时，源码 MCP 只核验既有精确锚点，不替代模块发现、跨层关系或查重；候选代码不得当作现场、部署版本或已上线事实。
6. **写入失败关闭**：`apply` 默认 dry-run；只有受控编排器明确授权的 `--execute` 才能真实写 TFS。异常、版本冲突或状态不符时立即停止，绝不强转或继续后续动作。
7. **官方能力缺失就停止**：`COMMANDS.md` 没有对应能力时，记录 `ERROR`/`evidence_gap` 后停止；附件依赖缺失只允许由官方运行器自修复，不得自行实现替代客户端、安装包或绕开执行器。
8. **经验处理状态不得省略**：每轮收尾都必须调用官方 `skill_memory.py record` 并落一份 `经验处理结果_<id>_<run_id>.json`；没有可沉淀经验也要得到 `NOT_APPLICABLE`。命中可沉淀诊断却未提供候选、写入失败或结果文件缺失时状态为 `FAILED`，须写入测试日志但不改变 TFS 终局。

## 核心状态机

每次触发先调用 `pipeline.py init-run --id <id>`，只接受执行器返回的 `run_id` 和 `run_receipt`；调用方不得自行指定或复用旧 ID。一次运行只使用一个 `run_id`，最终只生成一份 `执行计划_<id>_<run_id>.json`。同一冻结计划的 dry-run、后续授权 execute 和网络类幂等续跑仍复用本轮 ID，不属于重新分析。

| 阶段 | 终局 | 后续动作 |
|---|---|---|
| 准入 | `SKIP-ANALYSIS` | 零 TFS 写入；记录审计与可选 Redis 摘要后停止 |
| 质控 | `NEED-INFO` / `NEED-REVIEW` | checklist、QC 标签、`state_to=null`；停止，不进分析 |
| 质控 | `PASS` | 内部闸门，不产计划、不 apply；直接进入分析 |
| 分析 | `AUTO-ANA` | 需求分析报告 + 标签；受控流转到「已分析」 |
| 分析 | `MANUAL-REVIEW` / `MANUAL-REVIEW-STOP` | 需求分析报告；无新增用户待确认清单；`state_to=null` |

高风险规则优先于 AUTO 启发式。质控 `NEED-*` 与分析 `MANUAL-REVIEW*` 永不流转状态；只有 `AUTO-ANA` 可以执行完整字段流转。

## 高层流程

严格按 [`_lib/RUNBOOK.md`](_lib/RUNBOOK.md) 的 0–11 步执行：

1. 读取命令索引与运行手册，完成 precheck。
2. 创建运行目录并 fetch 工作项，先判 `SKIP-ANALYSIS`；非 SKIP 再解析产品 MCP 路由并检查该 profile 的工具注入状态。
3. 未跳过时处理附件，完成质控初判，按需补充当前产品的需求历史、KB/wiki、GitNexus、受控源码与数据库证据，并做一次业务决策预演。字段赋值、接口契约、模板绑定或数据读写一经触发，源码/数据库取证要求独立于最终 AUTO/MANUAL 终局，不得因已命中高风险或 MANUAL 而提前停止。
4. 将全部 PM 可回答的业务缺口一次性合并进 QC checklist；仅在缺口为零时 PASS 进入分析。
5. 完成证据闭环、需求分析报告、缺口分流和 AUTO/MANUAL 判定。
6. SKIP/QC 终局生成带回执的 `run-bound-v1` 计划；分析终局冻结 `分析结果_<id>_<run_id>.json` 并生成 `analysis-ref-v1` 瘦计划。正常流程只调用一次 `pipeline.py apply --id <id> --run-id <run_id>`（缺省 dry-run，经授权才 `--execute`），不得传任意计划路径。apply 将问题分为 `REJECT / ANALYSIS_ONLY / WARNING`：只有业务结论或运行身份不可信才拒绝，执行条件不足保留本轮结果，纯格式/非决定性审计告警继续执行。`validate` 只供维护诊断，历史执行只能显式使用 `apply-legacy`。
7. 收尾先记录重分析诊断，再用 `skill_memory.py record` 取得明确经验处理状态，最后追加测试日志；本轮调用入参含 `human_feedback` / `additional_info` 时须原文留痕，均空时明确写“无”。

> 重跑（同工作项有更早分析轮）且编排器传入"上轮补充信息"时，额外执行「重分析诊断」并在收尾追加到 `过程文件/skill-feedback.md`；详见 RUNBOOK 步骤 1 与收尾区、[`references/round-diagnosis-rules.md`](references/round-diagnosis-rules.md)。首轮或无补充时自动跳过，不影响任何终局。

## 产物边界

所有手工产物只能放在 `init-run` 创建的 `过程文件/<id>/<run_id>/`；来源附件放 `附件/`，解析副本放 `附件解析/`。运行目录必须保留 `运行回执_<id>_<run_id>.json`；新分析终局只维护一份不可覆盖的 `分析结果_<id>_<run_id>.json`，报告、分析者描述与 Redis 摘要均由它确定性投影。执行器审计放 `过程文件/<id>/runs/`；经验处理请求/结果放本轮目录。跨运行测试记录只追加到 `过程文件/测试日志.md`，重分析诊断（仅重跑 + 有补充时）追加到 `过程文件/skill-feedback.md`，跨运行经验先验读取自 / 由官方写入器追加到 `过程文件/经验记忆.md`。这些副记录均非 TFS、不入分析快照、不进执行器审计。

| 终局 | 必需产物 |
|---|---|
| `SKIP-ANALYSIS` | 计划 JSON；无 TFS 附件 |
| `NEED-INFO` / `NEED-REVIEW` | 计划 JSON + 顶层 inline `checklist` |
| `AUTO-ANA` / `MANUAL-REVIEW*` | 计划 JSON + 需求分析报告；新策略不生成分析阶段待确认清单 |

`PASS` 不是终态，不产独立计划。新 SKIP/QC 计划固定 `version=2`、`plan_profile=run-bound-v1` 并引用同目录运行回执；新分析瘦计划固定 `version=2`、`plan_profile=analysis-ref-v1`，只引用同目录运行回执和分析快照的文件名与 SHA-256。标签、状态、报告附件名、分析者描述、Redis 投影与 AUTO 流转均由执行器推导。无回执完整 v1/v2 计划只允许 `apply-legacy --plan` 维护回放，不得进入普通 run、正常 Redis 或 `/status`。

新策略分析终局经授权 execute 时，固定先写分析者描述、上传本轮 `需求分析报告_<id>_<run_id>.md`，再收敛 TFS 中本工作项的受控 AI 附件：历史 `变更方案`、`需求分析报告`、`待确认清单`、`待补充信息` 全部解绑，仅保留唯一的本轮报告。用户上传的需求附件、截图、接口文档等不动，TFS 底层附件 Blob 不删除；本地各轮 `过程文件`、审计和测试日志也全部保留。每个真实写动作以 TFS PATCH 响应中的写后 revision/state 更新执行检查点，不以紧随其后的 GET 或“必须恰好 +1”误判成功写入。清理失败立即停止，不加终局标签、不流转状态、不发布新的 Redis 终局；`NEED-*`、`SKIP-ANALYSIS` 与需保留本轮 `manual-followup` 的历史计划回放不执行清理。

## 按条件读取规则

只读取当前阶段需要的文件；不要一次性加载全部引用。

| 时机 | 必读文件 |
|---|---|
| 始终 | [`_lib/COMMANDS.md`](_lib/COMMANDS.md)、[`_lib/RUNBOOK.md`](_lib/RUNBOOK.md)；以及 `过程文件/经验记忆.md`（跨运行经验先验，见 [`_lib/skill-memory.md`](_lib/skill-memory.md)：**尽力读取，缺失/为空属正常、不触发失败关闭**；提示性先验，不新增终局分支——同 round-diagnosis §5 性质） |
| 准入与质控 | [`config/qc-rules.md`](config/qc-rules.md)、[`references/pre-qc-rules.md`](references/pre-qc-rules.md)、[`references/qc-checklist.md`](references/qc-checklist.md)；仅标题归属存疑时读 [`references/project-keyword-dictionary.md`](references/project-keyword-dictionary.md) |
| 附件与补证 | 未跳过时读 [`_lib/attachment-evidence.md`](_lib/attachment-evidence.md)；先用 `build_menu_business_index.py resolve-route` 唯一确定产品 MCP profile。需要历史需求、关联工作项、验收背景或 KB/wiki 补证时再读 [`_lib/knowledge-base.md`](_lib/knowledge-base.md)、[`references/qc-kb-recipe.md`](references/qc-kb-recipe.md)、[`_lib/module-location-recipe.md`](_lib/module-location-recipe.md)、[`_lib/wiki-kb-recipe.md`](_lib/wiki-kb-recipe.md)；**判断是否触及判定面（KB/wiki 触发）或构造当前产品 GitNexus/db 查询词时读 [`_lib/business-keyword-dictionary.md`](_lib/business-keyword-dictionary.md)**（业务概念→同义词/拼音缩写/英文领域名/子系统·表前缀）。wiki 固定从 [`wiki/index.md`](../../wiki/index.md) 进入，按主题读文章，关键事实再沿文章 `Raw` 链接复核 |
| 质控决策预演与 PASS 后分析 | [`config/analysis-rules.md`](config/analysis-rules.md)、[`references/iteration-analysis-closure.md`](references/iteration-analysis-closure.md)、[`references/analysis-description-writing-rules.md`](references/analysis-description-writing-rules.md)、[`references/confidence-heuristic.md`](references/confidence-heuristic.md)、[`references/change-plan-template.md`](references/change-plan-template.md)；[`references/manual-review-template.md`](references/manual-review-template.md) 仅用于历史计划兼容，AUTO 指派时读 [`references/assignee-rules.md`](references/assignee-rules.md) |
| 生成、校验和执行计划 | [`_lib/tfs/EXECUTION_CONTRACT.md`](_lib/tfs/EXECUTION_CONTRACT.md)；涉及字段流转时读 [`_lib/tfs/field-flow.md`](_lib/tfs/field-flow.md)；结束时读 [`_lib/testing-log.md`](_lib/testing-log.md) |
| 重跑诊断（有上轮分析 + 编排器传入补充） | [`references/round-diagnosis-rules.md`](references/round-diagnosis-rules.md) |

六类高风险以 [`_lib/high-risk-categories.md`](_lib/high-risk-categories.md) 为单一权威源。

## 新计划固定约束

- QC 计划使用 `rules_source={"qc":"pre-qc-v1"}` 并通过 `run-bound-v1` 绑定回执；新分析快照固定按 `evidence-loop-v2` 保存四源 `evidence_acquisition`，瘦计划不重复这些业务字段；执行器物化时固定推导 `rules_source={"qc":"pre-qc-v1","analysis":"evidence-loop-v2"}`。`evidence-loop-v1` 和历史完整 v2 计划仅允许显式维护回放；禁止恢复 evidence-loop-v3 或 compose/outbox/replay 链路。
- 新 QC 与分析计划必须带 `confirmation_policy="qc-single-batch-v1"`。QC checklist 目标 1–3 个业务决策主题、最多 5 个；P0 不得丢弃，超出时合并同一决策面。
- 分析计划必须带 `analysis_gaps=[]`、`evidence_refs`、`evidence_gaps`、`implementation_impacts`，并完成两层覆盖：`general_rule_coverage` 固定检查范围、流程、业务含义、业务规则、权限、异常、验收七面；`business_rule_coverage` 专项检查展示、空值、维护粒度、历史数据四面。每面必须声明 `status/source/basis`，确认来源只允许工作项、附件、已继承 PM 答案或已证实产品规则；通用七面不得使用默认值，范围、业务含义、验收必须明确确认，其余可在真正不涉及时标明不适用。专项四面仅呈现细节可默认；数据读写/数据库影响下的空值保存/清空语义、维护粒度和历史数据不得默认。缺少可追溯确认且会改变方案或验收时必须回退 QC，但不得把“逐面检查”机械转成“逐面询问”。v2 需求分析报告还必须以“范围—方案—验收追踪”表逐项闭合改动点、目标行为和验收结果；新策略只允许“已证实 / 合理假设”，不得出现“待业务确认”；AUTO 的 `evidence_gaps` 必须为空且带非空 `auto_scopes`。
- 新分析计划固定使用 `analysis_profile=concise-v3`：分析者描述必须先单列一个非空“菜单路径”，再写按类别裁剪后的业务结论；不写“需求类别”、旧版“路径（菜单路径+操作路径）”或四项决策摘要。菜单入口取自工作项、已解析附件或唯一菜单索引；无菜单入口的接口/任务须写“无菜单入口：<明确触发方式>”。操作步骤、显示位置、触发条件和边界直接合并到对应方案行。一个维度内只有一项变更时保持单行；存在 2–8 个独立变更点时必须按 `1. **改动点**：内容` 起连续编号，TFS 显示为 `1、2、3`。完整闭环、证据和追踪表留在需求分析报告其它章节。
- `concise-v1` / `concise-v2` 与无 profile 的完整格式仅用于历史计划兼容。
- PM 可补齐的业务正确性信息统一在 QC checklist 一次性确认；技术发现缺口只放 `evidence_gaps`，不得生成 TFS 待确认附件。`analysis_gaps` 非空与 `manual-followup` 仅兼容历史计划。
- 新的非 SKIP 计划必须带 `knowledge_route` 快照产品、Area、profile 版本和四类 server name；未解析时 `servers={}`，禁止携带任何产品 MCP 调用或 finding，AUTO-ANA 必须 `status=RESOLVED`。
- 新计划的 `kb` 分别记录当前产品代码图谱 `ready`、源码 MCP `source_ready`、本轮是否依赖源码核验 `source_required`、数据库图谱 `database_ready` 与本轮是否依赖数据库核验 `database_required`；每条 `kb/wiki/tfs_requirements.findings` 都必须带人读字段 `conclusion`（证明了什么）、`evidence`（依据在哪里）、`boundary`（不能证明什么），`kb.findings` 还必须带 `source_type=code|database`。字段赋值/API/业务逻辑/SQL/模板绑定命中时 `source_required=true`；数据读写、schema、迁移、统计、数据权限、数据库性能或脚本命中时 `database_required=true`。对应服务就绪时必须实际调用并留下 finding，不就绪时必须写明确 `evidence_gap`；高风险/MANUAL 终局不豁免。源码 finding 的 `source_tool` 只允许 `search_source|search_symbol`；需求历史工具只能写入 `tfs_requirements.findings`。Redis `knowledge` 只保存 `summary/source_status/evidence_list` 人读佐证清单；`source_status` 固定展示历史需求、产品 Wiki、代码图谱、源码、数据库知识的命中/未就绪/已查询无佐证/本轮未使用状态，方便查看完整佐证路径。不重复保存 route、工具名、服务器和原始 finding；完整机器证据保留在计划/审计。历史计划按旧字段保守生成清单。
- 新策略的 `AUTO-ANA` 若 `auto_scopes` 含 `field-ui-copy`，必须带 `ui_baseline.sources`：从产品知识（wiki/Raw）、运行观察（受控页面/截图/已解析附件）、实现证据（GitNexus/源码）三类中至少取两类交叉证实。同类多条只算一类；wiki 是按需优先源，不是固定必需源。
- **功能已存在 → MANUAL-REVIEW**：查重命中 `state=已证实` 且 `maturity=已落地`、能力覆盖本次诉求（一套公版，不按项目/版本拆分）→ "功能已存在" → `MANUAL-REVIEW`（不得 `AUTO-ANA`，由 `pipeline.py` 硬闸 `existing_feature.satisfied=true` 强制）；分析者描述须以"该功能已存在"+需求号领起，计划声明 `existing_feature.satisfied=true`（`requirement_ids` 为已落地需求号）。仅相似而本次是新增量改动不触发，仍可 AUTO。

## 失败关闭

运行身份、冻结摘要、业务终局或 AUTO 决定性证据异常按 `REJECT` 停止且不发布伪结论。可确定归一化的附件转换链、已有 finding 但漏记探活工具、以及 NEED/MANUAL 非决定性源码 finding 缺失只记 `WARNING`，写入 apply 返回、运行状态和审计，继续安全确定的 Redis/TFS 动作，不得要求重跑分析；同类问题若被 AUTO 或业务结论直接依赖仍是硬错误。产品路由未解析时不得调用默认或其它产品 MCP：各来源标未就绪，AUTO 候选降 MANUAL，QC/MANUAL 仅在不依赖缺失证据时按既有降级规则继续。附件运行时安装或单文件转换失败按 `_lib/attachment-evidence.md` 逐文件降级。分析已冻结、但 TFS precheck/执行资格未满足时按 `ANALYSIS_ONLY` 返回 `ok=true, applied=false, actions=[]` 并发布本轮结果，不调用 `publish_failure`；revision/state 变化返回 `SOURCE_CHANGED`、`retryable=false`，必须重新 fetch 并用 `init-run` 开新轮。只有 TFS 写动作已经开始后失败才发布 Redis `ERROR`，且不得改动冻结快照、报告或 verdict。普通 apply 不得再次进入 `FAILED` 或正在执行的 run；已完成 execute 的重复投递直接返回幂等结果，不重复写 Redis 或生成审计。历史完整计划仅在 `apply-legacy` 下维持原执行语义，固定 `run_mode=legacy-replay` 且不发布正常 Redis。

## 不做什么

- 不做 auto-dev 点火，不做 7 类/Stop-List；`PM-AI-STOP-AUTO` 在本仓只打标签。
- 不把仓库、类、方法、接口、Mapper、表字段等技术定位写进 PM 需求分析报告正文。
