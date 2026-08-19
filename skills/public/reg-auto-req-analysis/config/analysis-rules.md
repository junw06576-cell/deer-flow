# 分析规则（analysis-rules）

> `auto-req-analysis` 启动时加载本文件。
> §2 高风险清单（6 类）已由**中心负责人**给定并签字担责；非高风险项按本文件、`references/confidence-heuristic.md` 和 `references/iteration-analysis-closure.md` 判定。新生成的 v2 分析计划审计记 `rules_source.analysis=evidence-loop-v2`；`evidence-loop-v1` / `fallback-v1` 只保留历史计划回放兼容。

## 判定优先级（重要，先命中先生效）

1. **命中 §2 高风险清单（6 类）** → `MANUAL-REVIEW` + `PM-AI-STOP-AUTO`。
   最高优先，**覆盖启发式**：即使改动小、信息完整，也强制人工确认、状态不自动流转、**绝不 `AUTO-ANA`**。
2. **功能已存在（一套公版）** → `MANUAL-REVIEW`（**不加** `STOP-AUTO`）。查重命中 `state=已证实` 且 `maturity=已落地` 的历史方案，且其能力覆盖本次诉求时，**不得 `AUTO-ANA`**；分析者描述须标注“该功能已存在”+需求号，计划声明 `existing_feature.satisfied=true`。由 `pipeline.py` 硬闸强制（`satisfied=true ∧ verdict=AUTO-ANA` 报错）。仅相似而本次是新增量改动（既有能力未完整覆盖本次诉求）不触发本档，仍按第 3 档。
3. 否则按 §1 / `confidence-heuristic.md`：全满足 → `AUTO-ANA`；任一不满足 → `MANUAL-REVIEW`（**不加** `STOP-AUTO`）。

> **终局不缩短证据链**：高风险或非白名单只决定 AUTO/MANUAL/STOP，不取消已由 `implementation_impacts` 触发的源码与数据库核验。字段赋值、API 契约、模板绑定、数据读写、schema/迁移/统计/权限/数据库性能或脚本命中后，必须完成对应只读取证，或以明确的不可用/覆盖缺口结束；不得以“反正已经 MANUAL”为停止理由。

---

## 1. AUTO / MANUAL 判据（仅对**非高风险**项）

### AUTO-ANA 白名单（必须完全落在其中）

仅当**全部变更**属于下列一类或多类，且同时满足 `references/confidence-heuristic.md` 的全部条件，才可判 `AUTO-ANA`：

| `auto_scopes` 值 | 允许的变更 | 不允许的边界 |
|---|---|---|
| `field-ui-copy` | 既有字段的 UI 展示、标签、提示文案、格式或顺序调整 | 新增/修改业务流程、计算规则、权限、接口、数据写入 |
| `unit-test-completion` | 为既有确定行为补充或修正单元测试，生产代码行为不变 | 借测试名义修改业务逻辑、集成测试环境、测试数据迁移 |
| `config-enum-adjustment` | 既有配置项或枚举项的低风险调整，影响范围和回退方式可枚举 | 计费、临床、权限、审计、不可逆数据、数据库 schema、跨系统配置 |

> 任一改动不属于白名单、同时包含未列出的改动，或无法明确归类 → `MANUAL-REVIEW`。`AUTO-ANA` 计划必须写入非空 `auto_scopes`（仅能使用上表值）并在需求分析报告中逐项说明依据。
> **迭代分析闭环前置**：先按 `iteration-analysis-closure.md` 形成现状、问题、差异、方案取舍、成功衡量和非目标。方向、根因、业务范围、取数口径或权限口径不清 → 回退 QC `NEED-REVIEW`；现有实现、相似能力或方案取舍未证实 → `MANUAL-REVIEW` 并按规则记录仅审计的 `evidence_gaps`。不得用候选实现、未确认根因或“按现有逻辑”替代业务结论。
> **两层业务覆盖前置**：新计划必须先以 `general_rule_coverage` 闭合范围、流程、业务含义、业务规则、权限、异常、验收，再在字段/接口/持久化需求中以 `business_rule_coverage` 闭合展示、空值、维护粒度、历史数据。逐面检查不等于逐面询问；有可追溯依据或明确不适用就不生成问题，只有剩余 PM 决策回退 QC。
> **单次集中确认**：新计划固定 `confirmation_policy=qc-single-batch-v1`。任何 PM 可回答且会改变方案或验收的业务缺口，无论在质控预演还是阶段二才发现，都必须回退 QC `NEED-REVIEW` 并合并进唯一 checklist；分析计划固定 `analysis_gaps=[]`，不得生成 `manual-followup`。高风险、非 AUTO 白名单或技术证据不足仍可判 MANUAL，但不再次向用户提问。
> **AUTO-ANA 额外硬前置**：`knowledge_route.status=RESOLVED`、`kb.ready=true` 且 `kb.dedup_ran=true`（相似实现查重必须实际执行）；优化类类别还须在 `kb.findings` 至少一条 `state=已证实` 锚定被改现有模块/入口。`kb.source_required=true` 时还必须 `source_ready=true`、实际调用 `search_source|search_symbol` 并至少一条已证实源码 finding。任一不满足 → 改判 `MANUAL-REVIEW`（不加 `STOP-AUTO`，属技术证据缺口）。此条由 `pipeline.py` 强制校验。
> **界面 AUTO 基线硬前置**：新策略 `AUTO-ANA` 的 `auto_scopes` 含 `field-ui-copy` 时，必须用 `ui_baseline.sources` 从产品知识（wiki/Raw）、运行观察（受控页面/截图/已解析附件）、实现证据（GitNexus/源码）三类中至少取两类交叉证实现状；同类多条只算一类。wiki 是按需优先源，不是固定必需源。无法闭合两类 → `MANUAL-REVIEW` 并记“界面现状交叉证据不足”。此条由 `pipeline.py` 强制校验。
> **功能已存在排除**：`existing_feature.satisfied=true`（一套公版下，既有能力已覆盖本次诉求）时禁止 `AUTO-ANA`（由 `pipeline.py` 强制）；须改判 `MANUAL-REVIEW` 并在分析者描述以“该功能已存在”+需求号领起。与上一条同属 AUTO-ANA 硬前置，缺一即降级。
> **治标警惕（根因/方向存疑→不分析、回退质控）**：需求描述含性能/异常抱怨（卡顿/慢/等待/超时/延迟/崩溃/数据错乱等）但报告中的推荐方案为**使用层规避**（上限/提醒/分批/限制/阈值等）而非根因优化时，**不得判 AUTO-ANA**，也**不得选 `performance` 类别把规避方案写成"优化方案"兜底、或用 `existing-ui-simple` 等类别绕过根因**——按 `SKILL.md` 回退通路回质控 `NEED-REVIEW`（产品 + 研发负责人），审计 `qc_recheck=analysis-rootcause-mismatch`。详见 `references/qc-checklist.md` §一 #10。
> **BUG 语义优先于标题/类型**：既有功能出现错误、字段条件分支有值/为空不一致或结果偏离既有预期时，即使工作项类型为功能性、标题写“优化/调整”，也优先选 `bug-fix`；不得包装成新功能、打印调整或界面优化。字段/打印异常须枚举全部取值分支；未闭环来源进入 `evidence_gaps` 并判 MANUAL。
> **现有设计规则也是证据**：界面颜色、标签、图标、列位或交互方式不能只按实施方案默认。须核实当前页或同类页面的既有规则；只定位到组件、不清楚设计规则时记 `evidence_gaps`，不得 AUTO-ANA。
> **AUTO-ANA 指派**：判 AUTO-ANA 后另按 `references/assignee-rules.md`（产品×版本×模块→开发组长）匹配唯一指派人，写入计划字段 `assignee_to`（display name）；范围外产品/混合版本且模块不清/命中多人时省略该字段、在分析者描述与审计写候选，**不阻断 AUTO-ANA**（指派与终局置信度解耦）。版本号取工作项 `version` 字段（`Winning.Prod.Version`），**不从迭代路径**判断。

## 2. 高风险清单（→ `MANUAL-REVIEW` + `PM-AI-STOP-AUTO`）

> **权威来源：`../_lib/high-risk-categories.md`**（6 类，两阶段共用，中心负责人签字）。
> 分析阶段基于**需求分析报告**做定性判定（能兜住质控早筛看不出的 ③ 数据迁移 / ④ 首次发布 / ⑥ 架构级等）。命中任一类 → 状态**不**自动流转 + 打 `PM-AI-MANUAL-REVIEW` + `PM-AI-STOP-AUTO`，交人工。

## 3. 需求分类规则

当前不按产品线额外分流（**一套公版假设**：同一产品线视为一套公版能力，查重不按项目/客户/版本拆分）；需求类别决定分析者描述必需维度，闭环结论决定 QC 回退、MANUAL-REVIEW 或 AUTO-ANA。命中已落地且能力覆盖本次诉求的既有能力 → “功能已存在” → MANUAL-REVIEW（见 §判定优先级第 2 项）。

## 4. 知识库接线说明（非规则·图谱为内部佐证）

> **权威来源：`../_lib/knowledge-base.md`**（产品路由 + 当前产品的 TFS 需求历史 / GitNexus 代码图谱 / 受控源码 / 数据库图谱 / wiki，两阶段共用）。
> **需求历史只作业务追溯佐证**：`tfs-requirements` 用于历史背景、关联工作项、验收条件和变更原因；已核验 finding 可用 `req:<索引>` 补充 `evidence_refs`。它不得覆盖实时 `fetch`、不得证明当前代码实现、不得进入 `qc_evidence_resolution`，也不得满足 AUTO 的 `kb.ready` / `kb.dedup_ran` / `kb:` 现有实现证据。
> **代码图谱定位与受控源码核验都是内部佐证，不进 PM 视角需求分析报告正文**。GitNexus 负责发现、关系和查重；源码 MCP 只在当前产品“精确 repo + API/路径/符号/技术字面量”锚点后核验 3–5 个关键文件，锚点优先来自 GitNexus，也可来自已解析附件、工作项明确技术信息、菜单索引或 wiki。GitNexus 不可用时源码只核验已有锚点，不替代发现和查重；二者均不证明现场部署。“分析者描述”只写菜单路径和操作路径，不写仓库、代码符号、接口、Mapper 或表字段。
> **产品 wiki 同样作业务内部佐证**，结果落 `wiki.findings`、不进正文；子系统名/仓库名只作当前产品 GitNexus 候选锚点。

- **内部定位**：按 `../_lib/module-location-recipe.md` 完整方法（探活→三组词→从流程选模块→候选记录+3 文件验证→双闸确认）定位主模块/调用链/关键文件/配置关联，结果落 **`kb.findings`**（三态 + 置信度），**用于**：① 印证 AUTO/MANUAL 的**业务改动类型**判断；② 查重（支撑 `references/confidence-heuristic.md` 第 4 项）；③ 高风险定性佐证（③④⑥，要改动面才看得出）。**不打印进需求分析报告正文**（类/方法/调用链/接口/Mapper/表字段不入产物）。
- **AUTO/MANUAL 从业务改动描述判**：改文案/补单测/调配置（白名单内）→ 倾向 AUTO；改业务规则/流程/数据写入 → MANUAL；代码图谱内部印证。
- **一键调用**：完整定位可套 `module-location-recipe.md` **§8 一键 prompt**（输出 → 内部 `kb.findings`，非交付正文）。
- **知识库未就绪**：第 4 项跳过、技术定位写“待 dev-design 确认”（落 `kb.findings`，非产物待确认）；业务菜单路径和操作路径仍按工作项、已解析附件及菜单索引生成，无法确认时如实写“未确认”。**非 AUTO-ANA 路径**不因此判 MANUAL；**AUTO-ANA 路径**仍要求 KB 就绪 + 查重执行（见 §1），缺失则降 `MANUAL-REVIEW`。审计记 `kb_ready`。
