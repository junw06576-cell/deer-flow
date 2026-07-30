# 分析规则（analysis-rules）

> `auto-req-analysis` 启动时加载本文件。
> §2 高风险清单（6 类）已由**中心负责人**给定并签字担责；非高风险项按本文件、`references/confidence-heuristic.md` 和 `references/iteration-analysis-closure.md` 判定。新生成的 v2 分析计划审计记 `rules_source.analysis=evidence-loop-v1`；`fallback-v1` 只保留 v1 历史计划回放兼容。

## 判定优先级（重要，先命中先生效）

1. **命中 §2 高风险清单（6 类）** → `MANUAL-REVIEW` + `PM-AI-STOP-AUTO`。
   最高优先，**覆盖启发式**：即使改动小、信息完整，也强制人工确认、状态不自动流转、**绝不 `AUTO-ANA`**。
2. 否则按 §1 / `confidence-heuristic.md`：全满足 → `AUTO-ANA`；任一不满足 → `MANUAL-REVIEW`（**不加** `STOP-AUTO`）。

---

## 1. AUTO / MANUAL 判据（仅对**非高风险**项）

### AUTO-ANA 白名单（必须完全落在其中）

仅当**全部变更**属于下列一类或多类，且同时满足 `references/confidence-heuristic.md` 的全部条件，才可判 `AUTO-ANA`：

| `auto_scopes` 值 | 允许的变更 | 不允许的边界 |
|---|---|---|
| `field-ui-copy` | 既有字段的 UI 展示、标签、提示文案、格式或顺序调整 | 新增/修改业务流程、计算规则、权限、接口、数据写入 |
| `unit-test-completion` | 为既有确定行为补充或修正单元测试，生产代码行为不变 | 借测试名义修改业务逻辑、集成测试环境、测试数据迁移 |
| `config-enum-adjustment` | 既有配置项或枚举项的低风险调整，影响范围和回退方式可枚举 | 计费、临床、权限、审计、不可逆数据、数据库 schema、跨系统配置 |

> 任一改动不属于白名单、同时包含未列出的改动，或无法明确归类 → `MANUAL-REVIEW`。`AUTO-ANA` 计划必须写入非空 `auto_scopes`（仅能使用上表值）并在变更方案中逐项说明依据。
> **迭代分析闭环前置**：先按 `iteration-analysis-closure.md` 形成现状、问题、差异、方案取舍、成功衡量和非目标。方向、根因、业务范围、取数口径或权限口径不清 → 回退 QC `NEED-REVIEW`；现有实现、相似能力或方案取舍未证实 → `MANUAL-REVIEW` 并按规则记录仅审计的 `evidence_gaps`。不得用候选实现、未确认根因或“按现有逻辑”替代业务结论。
> **AUTO-ANA 额外硬前置**：`kb.ready=true` 且 `kb.dedup_ran=true`（相似实现查重必须实际执行，落审计字段 `kb.dedup_ran`）；优化类类别（`existing-*`、`bug-fix`、`print-adjustment`、`data-management`、`permission-config`、`performance`、`mobile-adaptation`）还须在 `kb.findings` 至少一条 `state=已证实` 锚定被改现有模块/入口（见 §下文「现有实现基线」）。KB 未就绪 / 查重未跑 / 无已证实锚点 → 改判 `MANUAL-REVIEW`（不加 `STOP-AUTO`，属信息缺口而非高风险）。此条由 `pipeline.py` 强制校验。
> **治标警惕（根因/方向存疑→不分析、回退质控）**：需求描述含性能/异常抱怨（卡顿/慢/等待/超时/延迟/崩溃/数据错乱等）但变更方案为**使用层规避**（上限/提醒/分批/限制/阈值等）而非根因优化时，**不得判 AUTO-ANA**，也**不得选 `performance` 类别把规避方案写成"优化方案"兜底、或用 `existing-ui-simple` 等类别绕过根因**——按 `SKILL.md` 回退通路回质控 `NEED-REVIEW`（产品 + 研发负责人），审计 `qc_recheck=analysis-rootcause-mismatch`。详见 `references/qc-checklist.md` §一 #10。
> **AUTO-ANA 指派**：判 AUTO-ANA 后另按 `references/assignee-rules.md`（产品×版本×模块→开发组长）匹配唯一指派人，写入计划字段 `assignee_to`（display name）；范围外产品/混合版本且模块不清/命中多人时省略该字段、在分析者描述与审计写候选，**不阻断 AUTO-ANA**（指派与终局置信度解耦）。版本号取工作项 `version` 字段（`Winning.Prod.Version`），**不从迭代路径**判断。

## 2. 高风险清单（→ `MANUAL-REVIEW` + `PM-AI-STOP-AUTO`）

> **权威来源：`../_lib/high-risk-categories.md`**（6 类，两阶段共用，中心负责人签字）。
> 分析阶段基于**变更方案**做定性判定（能兜住质控早筛看不出的 ③ 数据迁移 / ④ 首次发布 / ⑥ 架构级等）。命中任一类 → 状态**不**自动流转 + 打 `PM-AI-MANUAL-REVIEW` + `PM-AI-STOP-AUTO`，交人工。

## 3. 需求分类规则

当前不按产品线额外分流；需求类别决定分析者描述必需维度，闭环结论决定 QC 回退、MANUAL-REVIEW 或 AUTO-ANA。

## 4. 知识库接线说明（非规则·图谱为内部佐证）

> **权威来源：`../_lib/knowledge-base.md`**（GitNexus 代码图谱 + 数据库图谱 + 产品 wiki，两阶段共用）。
> **代码图谱定位是内部佐证，不进 PM 视角变更方案正文**——变更方案是业务视角（见 `references/change-plan-template.md`）。“分析者描述”的 `路径` 仅写菜单路径和操作路径，不写仓库、代码符号、接口、Mapper 或表字段。
> **产品 wiki 同样作业务内部佐证**（业务规则/流程/数据口径/验收），结果落 `wiki.findings`、**不进正文**；查询见 `../_lib/wiki-kb-recipe.md`，读序在 GitNexus 之前。**代码事实为准、wiki 仅补充——二者冲突时以 GitNexus 为准**（代码库最准）。

- **内部定位**：按 `../_lib/module-location-recipe.md` 完整方法（探活→三组词→从流程选模块→候选记录+3 文件验证→双闸确认）定位主模块/调用链/关键文件/配置关联，结果落 **`kb.findings`**（三态 + 置信度），**用于**：① 印证 AUTO/MANUAL 的**业务改动类型**判断；② 查重（支撑 `references/confidence-heuristic.md` 第 4 项）；③ 高风险定性佐证（③④⑥，要改动面才看得出）。**不打印进变更方案正文**（类/方法/调用链/接口/Mapper/表字段不入产物）。
- **AUTO/MANUAL 从业务改动描述判**：改文案/补单测/调配置（白名单内）→ 倾向 AUTO；改业务规则/流程/数据写入 → MANUAL；代码图谱内部印证。
- **一键调用**：完整定位可套 `module-location-recipe.md` **§8 一键 prompt**（输出 → 内部 `kb.findings`，非交付正文）。
- **知识库未就绪**：第 4 项跳过、技术定位写“待 dev-design 确认”（落 `kb.findings`，非产物待确认）；业务菜单路径和操作路径仍按工作项、已解析附件及菜单索引生成，无法确认时如实写“未确认”。**非 AUTO-ANA 路径**不因此判 MANUAL；**AUTO-ANA 路径**仍要求 KB 就绪 + 查重执行（见 §1），缺失则降 `MANUAL-REVIEW`。审计记 `kb_ready`。
