# 质控知识库有目的发现链（qc-kb-recipe）

> 供质控阶段使用：发现可让质控清单更具体的现有实体，但不直接产生新 verdict。工具、三态、降级和数据库联查边界以 [`../_lib/knowledge-base.md`](../_lib/knowledge-base.md) 为准；模块证据判定以 [`../_lib/module-location-recipe.md`](../_lib/module-location-recipe.md) 为准；产品 wiki（业务语义）查询以 [`../_lib/wiki-kb-recipe.md`](../_lib/wiki-kb-recipe.md) 为准。

## 何时跑

需求命中高风险早筛（①②⑤⑥），或描述含结算、费用、医保、上传、校验、报表、接口、明细、账单等可映射业务概念时运行；**初判 `NEED-REVIEW` 且疑义可能由既有业务规则、数据语义或路径证实消除时也运行**。仅纯联调准入跳过、`NEED-INFO`、范围排除、PIMIS/时效和 §6a 根因方向等硬终局锁定后跳过整个发现链（见 `../config/qc-rules.md` 与 SKILL.md 步骤 3）。纯文字、无业务实体且非高风险的需求主动跳过，记 `kb.note=未触及判定面，跳过`；这不是降级。

## 发现链（单需求约 ≤10 次调用，信号够即停）

> **读序**：产品 wiki 在本代码图谱发现链**之前**读（菜单索引收敛 → wiki 业务语义 → 下方代码图谱 Steps），见 [`../_lib/wiki-kb-recipe.md`](../_lib/wiki-kb-recipe.md)；wiki 发现同源写 `checklist.items`/`kb_note`/`wiki.findings`，不判终局。

### Step 0：探活并固定范围

1. 调用 `gitnexus-team.list_repos`，选择明确 `repo`（已知项目优先单仓库；跨项目才用 `cloudhis-unified`），记录索引时间、节点/流程数和 `embeddings`。**注意 `list_repos` 回包很大（数十 repo × 全量 stats）**：质控仅用于探活 + 选 repo，只需取「图谱就绪」结论与目标 repo 的索引时间/`embeddings`，不必逐条处理所有 stats；若 repo 已由共享菜单索引唯一确定，可只取探活结论。
2. 代码图谱不可用、超时或重建中：记 `kb_ready=false`，按 `config/qc-rules.md` 默认规则继续，不阻断、不转 ERROR。
3. 数据库工具只在 Step 3 需要时探活；数据库不可用不影响代码侧佐证。

### Step 1：代码图谱优先定位

1. 分别检索业务对象、业务动作、技术锚点，勿将需求原文整句塞入查询。
2. 从候选页面/API/符号提取精确 API 路径、Controller、DTO、Mapper 或方法名，再做第二轮检索。`embeddings: 0` 时优先路径和符号名。
3. 对候选使用 `query → context → route_map（有精确 API 时）→ context` 建立入口和关系。`processes` 或 `route_map` 为空时，继续用 `context` 查 Controller、Service、Mapper 的调用边。
4. 质控只需证明对象/动作、可追踪入口和明确关系；不做完整调用链枚举或源码验证。高风险需估计影响面时可调用 `impact`；接口风险分层可调用 `api_impact` 或 `route_map`。

### Step 2：判定与记录

- 对象与动作匹配、存在入口且有明确关系：记为**已证实**。
- 仅名称/检索匹配，缺入口或关系：记为**候选**并写缺失证据。
- 图谱无关系或索引覆盖不足：记为**未确认**；不得按名称猜测后端、表或调用链。

每条同时标注“图谱事实 / 推断 / 未确认”。将命名实体用于补强现有 `qc-checklist.md` #3 可识别性或 #5 重复问题，不新增 KB 专属规则或 verdict。

### Step 3：按需数据库联查

仅在需求涉及字段、迁移、统计口径、数据权限、性能或数据库脚本时运行：

1. 用已证实的表名、字段名、Mapper、方法名或 SQL 片段查询 `Table`、`Column`、`CodeSymbol` 或 `SQLStatement`，限制较小 `limit`；仅对确认候选调用 `get_table_knowledge`。
2. 精确锚点无命中时，再用业务缩写、同义词或命名转换查询 `Table`，按 `database_alias` 查表详情。
3. 两轮均无结果时，记“数据库图谱覆盖缺口；真实表/字段待 Mapper SQL 映射确认”。不得用名称相似的表补链，也不得因数据库无命中否定代码侧发现。

### Step 4：写发现与复核

- `checklist.items`：把抽象问题变为带实体名和证据状态的选择式问题。
- `checklist.kb_note`：简洁汇总已证实、候选、未确认及图谱限制。
- 顶层 `kb.findings`：写 `[{entity, state, source_tool}]` 供审计和下游机读。

初判 `NEED-REVIEW` 时，先逐项判断该 finding 是否直接回答初判问题。只有 `kb:` 或 `wiki:` 的**已证实** finding 同时满足“同业务范围、现状规则明确、与需求不冲突、无剩余正确性/方向类缺口”时，才在后续分析计划写 `qc_evidence_resolution.items[]` 并复核为 `PASS`。不得用 `work-item`、候选/推断、名称相似或只定位到技术实体的发现放行。

疑似重复或已实现只能作为清单条目，经现有维度参与终局；存疑时按既有规则判 `NEED-REVIEW`。本 skill 只读知识库，不写回图谱。
