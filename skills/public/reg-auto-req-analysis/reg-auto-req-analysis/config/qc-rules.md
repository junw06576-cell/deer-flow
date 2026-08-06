# 质控规则（qc-rules）

> **状态：已接入事前质控规则。**完整规则、项目关键词字典和更新记录见 `../references/pre-qc-rules.md`；本文件只保留自动化执行时必须遵循的判定顺序与终局映射。
>
> 质控**不修改 TFS 状态**。**质控阶段的**查重仍为预留项，不参与**质控 verdict** 判定（分析阶段的“功能已存在”判定另由 `references/confidence-heuristic.md` 第 4 项反向条款与 `pipeline.py` 硬闸约束，不影响质控）。

## 读取字段

严格从 `fetch` 返回的 JSON 读取，字段映射如下：

| 业务字段 | `workItem` 字段 | 是否允许为空 |
|---|---|---|
| 需求类型 | `demandType` | 否 |
| PIMIS 需求优先级 | `pimisPriority` | 是 |
| 期望日期 | `expectedDate` | 是 |
| 迭代路径 | `iterationPath` | 否 |
| 标题 | `title` | 否 |

`expectedDate` 已由 `fetch` 转为北京时间 ISO，`expectedDateRaw` 仅用于审计原始 TFS 值；时效判断不得再对 UTC 原值直接 `[:10]` 截断。

## 初判优先级（先命中先生效；`SKIP-ANALYSIS` 只记路由原因，其余阻断项先经 `references/qc-checklist.md` §二·收敛规则分流，再按下文 KB/wiki 补证规则复核）

1. **范围排除**：`demandType=软件质量` → `NEED-REVIEW`，清单标记“OUT-OF-SCOPE”，责任方“非本组/测试流程”；不进入分析。
   - **1b. 纯联调/支持准入排除**：描述明确“接口已开发/已与开发沟通，本次仅联调、调试、现场配合”，且没有新增或调整业务规则、字段映射、接口契约、异常处置或验收逻辑 → `SKIP-ANALYSIS`。只生成零标签、零附件、`state_to=null` 的跳过计划并记录 `skip_reason`；不要求补接口文档，不进入附件解析、KB、质控清单或阶段二。若实际提出接口/规则变更，则不命中本条，按正常需求分析。
   - **1c. 需求性质语义纠偏**：即使 `demandType=功能性的`，描述若以“当前错误/为空/不一致/某条件有值另一条件无值/原本应当”为核心，先按既有功能缺陷处理，阶段二优先选 `bug-fix`，不得仅因标题含“优化/调整”改写为功能增强或打印调整。现象、触发条件、受影响分支或数据源未追清时，记录证据缺口并走人工复核；不得补造参数或新功能方案。
2. **时效不可处理或需排期确认**：
   - PIMIS A 级 → `NEED-REVIEW`，责任方“研发负责人”；
   - PIMIS D 级 → `NEED-REVIEW`，责任方“分析与现场”，按 30 日再确认；
   - PIMIS B 级 → `NEED-REVIEW`，责任方“研发负责人”，确认迭代剩余容量与 21 天时效；
   - 期望日期：调 `tfs_client.py list-iterations`（`--project` 用工作项 teamProject、`--expected-date` 可选；完整命令见 `../_lib/COMMANDS.md`）取官方迭代日历的 `matched`（`finishDate ≤ 期望日` 的最近可交付迭代，判定基准见 `references/pre-qc-rules.md §三.3`）。`matched` 存在且当前日期 ≤ `matched.finish − 7天`（代码提交截止）→ 正常处理；时间紧张 → `NEED-REVIEW`（责任方”研发负责人”）；`matched=null`（期望日早于所有迭代 `finishDate`）或查询失败 → `NEED-REVIEW`，责任方”现场/排期方”，不得猜测日期。
3. **高风险早筛**（spec 清晰度二分）：查 `../_lib/high-risk-categories.md`。从需求标题/描述即可识别的高风险（多见 ①②⑤），**按 spec 是否清晰分**（清晰度判据见 `references/qc-checklist.md` §一 #7-#9）：
   - 高风险 + **spec 清晰**（口径/范围/处理/验收可分析）→ **`PASS` 进分析**——由分析阶段②等定性为 `MANUAL-REVIEW` + `PM-AI-STOP-AUTO` 兜底人工复核，**质控不挡回**（描述清晰的高风险需求挡回产品价值低，`STOP-AUTO` 已兜底）。
   - 高风险 + **spec 不清**（业务规则/范围/口径存疑）→ 初判 `NEED-REVIEW`，先按 KB/wiki 补证规则复核；不能逐项由已证实既有规则闭合时，责任方“产品”。
   - 质控早筛**不打 STOP-AUTO**（分析阶段产物）；它只决定“清晰的高风险放行进分析 vs 不清的高风险挡回产品”。
4. **项目归属（AI 自主判断·不做字典限制）**：由 AI 判断标题是否含**可识别的项目/客户/区域/大区归属**——`references/pre-qc-rules.md` 的关键词字典仅作**参考**，非硬性限制，AI 可识别字典之外的合理归属（真实地名/客户名/区域名）。能识别出归属即通过；标题完全无项目/区域/客户/大区归属信息 → `NEED-INFO`，责任方“实施/创建者”，要求补充具体项目、客户或区域信息。
5. **信息完整性**：`demandType`、`iterationPath`、标题缺失，或期望日期非空但无法解析 → `NEED-INFO`；描述、模块/接口/数据对象不可识别也按 `NEED-INFO`。
6. **合理性 / spec 清晰度**：描述内部冲突、业务规则存疑、或 **spec 清晰度任一存疑**（范围/边界、取值/数据源口径、与现有路径一致性，详见 `references/qc-checklist.md` §一 #7-#9）→ 先按 `references/qc-checklist.md` §二·收敛规则二分：**呈现类**存疑 → 默认现状不变（记 `notes`，不单独触发 NEED-REVIEW）；**正确性/方向类**存疑 → 初判 `NEED-REVIEW`，再决定是否可由 KB/wiki 已证实的现状规则逐项闭合。不能闭合时交产品，item 进清单并受 `MAX_QC_ITEMS` 收敛。**意图**：PASS 进分析的 spec 必须已清；已证实既有规则可成为清晰度证据，dev 级细节（具体字段名/mapper）不算 spec 歧义。
   - **6a. 需求方向/根因一致性（治标警惕）**：需求为性能/异常抱怨（卡顿/慢/等待/超时/延迟/崩溃/卡死/无响应/数据错乱/报错）**且** 需求方（常见实施/现场）给的方案为**使用层规避**（上限/提醒/分批/限制/阈值/不超过/最多N条/临时截断/规避/绕过）而非根因优化（加索引/分页/异步/缓存/重构）→ 属**方向类存疑** → `NEED-REVIEW`（责任方 **产品 + 研发负责人**），item 问"是否先做根因排查，而非套上限/提醒"，`priority:P0`，详见 `references/qc-checklist.md` §一 #10。**意图**：异常量级的数据不应致异常卡顿/错误，规避方案治标可能掩盖真实缺陷；PASS 进分析的应是“方向已对、根因已明”，不在分析里再抛根因类待确认。
   - **评估时机**：本规则为纯标题/描述文本可定，在 SKILL.md 步骤 3 的“事前质控文本快扫”中评估；命中为客观硬性 `NEED-REVIEW`，必须交产品 + 研发负责人确认根因，不得由静态 KB/wiki 发现链放行。
7. **通过**：无上述阻断项 → `PASS`，链入分析。

## KB/wiki 补证复核

初判 `NEED-REVIEW` 后，在不存在范围排除、PIMIS/迭代时效、`NEED-INFO`、或 §6a 根因方向等硬阻断时，运行只读菜单索引、产品 wiki 与代码/数据库知识图谱。仅当**每个**初判项都满足以下条件，才将初判改为 `PASS` 并进入阶段二：

1. `kb.findings` 或 `wiki.findings` 有与该项直接相关的**已证实**事实；
2. 事实明确同一业务范围的现状口径、数据语义或既有路径，且与工作项目标不冲突；
3. 复核后没有剩余正确性/方向类缺口；
4. 分析计划写 `qc_evidence_resolution`，逐项记录初判问题、补证结论及 `kb:`/`wiki:` 引用。

工作项原文、名称相似、候选/推断、技术实体存在、或只证明局部页面的事实均不能放行；任一项不满足则保持最终 `NEED-REVIEW` 并写 QC 清单。

> **时效组合规则**：PIMIS 与期望日期同时存在时，任一“不可处理”即 `NEED-REVIEW`；B 级始终至少“谨慎处理”；C 级仅在期望日期“正常处理”时可继续。两字段均为空按普通需求继续，仍需完成后续内容质控。
>
> **迭代日期实查**：迭代日历由 `tfs_client.py list-iterations` 直查 TFS 分类节点 API（`classificationnodes/iterations`）取官方 `finishDate`，**不再从工作项 `System.IterationPath` 字符串解析日期**——迭代路径可能停在项目根（如 `NETHIS5.5`）而无可解析日期。判定基准：`finishDate < 期望日期`（需求要在期望日期**之前**的迭代里交付完），取 `finishDate ≤ 期望日` 中 `finishDate` 最大者。查询失败 / 项目无迭代 → 回到"交排期方/现场补充"路径，记 `iteration.timing=unavailable`，不阻断 skill。
>
> **时效“正常处理”=已通过项（不提示归位、不指定排期方）**：当 `matched` 存在且当前日期 ≤ `matched.finish − 7 天` 时，时效判为“正常处理”，**计入“已通过项”**——不产生 `NEED-REVIEW`、不提示把 `iterationPath` 归位到具体版本节点、也不指定排期方。迭代排期与路径归位由**产品**负责，超出质控判定范围。仅在“谨慎处理 / 不可处理 / 查询失败（`iteration.timing=unavailable`）”时，才按上表产生 `NEED-REVIEW` 并指定对应责任方（研发负责人 / 现场 / 排期方）。

## 责任方与标签

| 终局 | 标签 | 责任方 |
|---|---|---|
| `NEED-INFO` | `PM-AI-QC-NEED-INFO` | 实施/创建者 |
| `NEED-REVIEW` | `PM-AI-QC-NEED-REVIEW` | 产品、研发负责人、现场、排期方或非本组流程；按命中规则写明 |
| `SKIP-ANALYSIS` | 无 | 无；仅记录准入排除原因 |
| `PASS` | 无 | 链入分析 |
