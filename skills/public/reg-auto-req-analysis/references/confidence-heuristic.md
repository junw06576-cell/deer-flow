# 分析置信度启发式（非高风险项的 AUTO/MANUAL 判据）

> 给 `auto-req-analysis` 用。
> **先查高风险清单**（`../_lib/high-risk-categories.md`，6 类）——命中 → `MANUAL-REVIEW` + `PM-AI-STOP-AUTO`，**不走本启发式**。
> 本启发式只对**未命中高风险**且**完全落在 `analysis-rules.md` §1 AUTO 白名单**内的项，决定 `AUTO-ANA` vs `MANUAL-REVIEW`。刻意轻量、可解释，不复用《汇报版》7类/点火逻辑（已出范围）。

## 判定规则（仅对非高风险项）

**先满足 AUTO 白名单，再全部满足下表 → `AUTO-ANA`；任一不满足 → `MANUAL-REVIEW`（不加 `STOP-AUTO`）。**

**AUTO-ANA 是无人工复核的自动放行路径**，必须先解析唯一产品路由，并满足 `kb.ready=true` 与 `kb.dedup_ran=true`。若 `kb.source_required=true`，还必须实际调用当前产品的 `search_source|search_symbol`、`source_ready=true` 且有已证实源码 finding。任一不满足时 AUTO 候选降 `MANUAL-REVIEW`。

`auto_scopes` 含 `field-ui-copy` 时还必须填写 `ui_baseline.sources`，从产品知识、运行观察、实现证据三类中至少取两类交叉证实现状；wiki 未覆盖本身不降级，同类多条不能互相充当独立来源。无法闭合两类时降 `MANUAL-REVIEW`。

### 缺口分流：呈现类 vs 正确性类

分析时按命中类别的必需维度（见 `analysis-description-writing-rules.md`）扫描出的"未说清"项，先按下表分流，再套本启发式——**呈现类**缺口用合理默认兜进变更方案 §七"假设"、**不计入**下表"不满足"；**正确性类**缺口回退 QC，合并进单次 checklist。新分析计划固定 `analysis_gaps=[]`。

> 应先完成 `iteration-analysis-closure.md`。闭环中的方向、根因、业务范围、取数口径或权限口径不清属于 QC spec 问题，回退 `NEED-REVIEW`，不降级为本文件的分析缺口；只有需求已清但现有实现、相似能力或方案取舍未证实，才进入 `MANUAL-REVIEW`。

| 类别 | 范围 | 缺口时的处理 |
|---|---|---|
| **呈现类** | 格式、文案、顺序、空值显示、颜色、布局、边界展示（错了只是体验问题） | 用合理默认兜底（写入方案 §七假设及适用边界），**不阻断 AUTO** |
| **正确性类** | 金额取数、计算精度、业务规则、异常处置、合规、权限（错了是资损/bug/违规）。**变更范围或"现有行为/数据影响"维度命中以下关键词之一即按正确性类（不得用呈现类默认兜）：金额、精度、计算、舍入、医保、结算、扣减、回滚、退费、过账、阈值、上限、下限、累计、汇总** | 回退 QC `NEED-REVIEW`，并入唯一 checklist（不得用默认兜） |

> 拿不准属于哪类时，按"错了会不会造成资损/数据错误/违规"判断：会 → 正确性类；不会 → 呈现类。

| # | 条件 | 不满足时 |
|---|---|---|
| 1 | **改动范围可清晰枚举**：能列出具体的页面/字段/接口/报表/配置改动点 | → MANUAL |
| 2 | **有明确验收标准**：**正确性类**验收口径（金额/取数/规则/精度）能从工作项识别；**呈现类**口径（格式/空值/顺序）缺失可用合理默认兜底 | 正确性类口径不明 → MANUAL |
| 3 | **改动量小**：枚举出的改动点数 ≤ `MAX_CHANGE_POINTS`（默认 8） | → MANUAL |
| 4 | **（KB 就绪后）命中相似已实现需求**：代码图谱（见 `../_lib/knowledge-base.md`）能找到同模块已落地方案，**或** `tfs-requirements` 查到 `state=已证实` 且 `maturity=已落地` 的同模块历史方案（经多组关键词 + 按状态过滤 + get_work_item 核验 + 按 `changed_date` 倒序复查勿漏最新条目，见 `_lib/knowledge-base.md` §四/§六）；`maturity=设想` 或仅描述级命中不算已落地。**反向：若该已落地能力覆盖本次诉求（一套公版）→ "功能已存在" → 强制 MANUAL-REVIEW（见下「反向强制」）** | KB 未就绪时**跳过此项**，但**不得判 AUTO-ANA** → 降 `MANUAL-REVIEW`（不加 `STOP-AUTO`，属信息缺口而非高风险） |
| 5 | **现有实现已证实**（仅优化类类别：`existing-*`、`bug-fix`、`print-adjustment`、`data-management`、`permission-config`、`performance`、`mobile-adaptation`）：`kb.findings` 至少一条 `state=已证实` 锚定**被改的现有模块/入口**，并被 `evidence_refs` 引用 | 无法证实现有实现 → `MANUAL-REVIEW`，在仅审计的 `evidence_gaps` 记录“现有实现定位”缺口，交研发或知识库治理处理 |
| 6 | **闭环可验证**：目标状态、保持不变项、成功衡量和非目标均已明确，且类别、变更点、风险与验收与其一致 | 方向/口径不清 → 回退 QC；已清需求但方案取舍未证实 → `MANUAL-REVIEW` |

> 高风险筛查在前，所以"敏感面/患者/支付"等已由高风险清单兜住，这里不再重复列关键词。**单一权威来源 = `../_lib/high-risk-categories.md`。**

### 反向强制：功能已存在 → MANUAL-REVIEW（覆盖 AUTO 白名单）

当第 4 项查重命中 `maturity=已落地` 的历史方案，**且其能力覆盖/可满足本次需求诉求**（一套公版假设，不按项目/客户/版本拆分查重）时，判"功能已存在"：终局**必须 `MANUAL-REVIEW`**（不得 `AUTO-ANA`，不加 `STOP-AUTO`），由 `pipeline.py` 硬闸 `existing_feature.satisfied=true ∧ verdict=AUTO-ANA` 强制报错。分析者描述须以"该功能已存在"+需求号领起；结构化声明 `existing_feature: {satisfied: true, requirement_ids: […], note: "…"}`。

**正向 vs 反向的区别**：第 4 项正向指"命中相似已实现，证明本次改动有可参考的既有能力"——本次仍是**新的增量改动**（如列已存在、本次调整其顺序/文案/默认值，既有能力未完整覆盖本次诉求）；反向指"本次诉求已被既有能力**完整满足**，无需新增改动"。前者支持 AUTO，后者强制 MANUAL。拿不准"覆盖还是仅相似"时按 MANUAL 处理，并在 `existing_feature.note` 写明待人工确认的边界。

## 阈值（可调）

- `MAX_CHANGE_POINTS = 8`：枚举的改动点超过此数 → 第 3 项不满足 → MANUAL。
- 超过阈值不是"错"，只是"不再低风险"，交 PM 确认更稳。

## 判定示例（均为**未命中高风险**的前提下）

- "某字段文案从 A 改 B"（`field-ui-copy`）→ 全满足 → `AUTO-ANA`
- "为已有金额格式化函数补齐边界值单测，不改生产代码"（`unit-test-completion`）→ 全满足 → `AUTO-ANA`
- "调整既有页面状态枚举的展示顺序，可回退"（`config-enum-adjustment`）→ 全满足 → `AUTO-ANA`
- "打印模板某列空值怎么显示、金额保留几位没写" → **呈现类**，用默认（显示 0、2 位小数）兜进 §七假设 → 仍可 `AUTO-ANA`
- "新增一个报表，但**金额统计口径**没写清" → 正确性类口径不明、第 2 项不满足 → `MANUAL-REVIEW`
- "重构某模块导出逻辑，涉及 12 个改动点" → 不属于白名单且第 3 项不满足 → `MANUAL-REVIEW`

（命中高风险的例子见 `analysis-rules.md` §2：如"调整医保结算公式"→命中②→`MANUAL-REVIEW`+`STOP-AUTO`，不走本表。）
