# 质控知识库有目的发现链（qc-kb-recipe）

> 供质控阶段使用：发现可让质控清单更具体的历史工作项、业务规则和现有实体，但不直接产生新 verdict。需求历史工具、三态、降级和数据库联查边界以 [`../_lib/knowledge-base.md`](../_lib/knowledge-base.md) 为准；模块证据判定以 [`../_lib/module-location-recipe.md`](../_lib/module-location-recipe.md) 为准；产品 wiki（业务语义）查询以 [`../_lib/wiki-kb-recipe.md`](../_lib/wiki-kb-recipe.md) 为准。

## 何时跑

需求命中高风险早筛（①②⑤⑥），**或**描述/标题/验收**触及 [`../_lib/business-keyword-dictionary.md`](../_lib/business-keyword-dictionary.md) 中任一业务概念**（标准名/同义词/旧称/拼音缩写任一）时运行——**不再限于"结算/费用/医保/上传/校验/报表/接口/明细/账单"9 词枚举**（该枚举漏掉对账/转科/处方/发药/库存/床位/退药等大量同义词需求，是 0 命中的主因）；**初判 `NEED-REVIEW` 且疑义可能由既有业务规则、数据语义或路径证实消除时也运行**。仅纯联调准入跳过、`NEED-INFO`、范围排除、PIMIS/时效和 §6a 根因方向等硬终局锁定后跳过整个发现链（见 `../config/qc-rules.md` 与 SKILL.md 步骤 3）。判"纯文字、无业务实体且非高风险"而跳过时，**必须写明"对照字典检查了哪些概念、为何判定未命中"**（记 `kb.note`），使"真无实体"与"没认出实体"可区分；这不是降级。

## 发现链（单需求约 ≤10 次调用，信号够即停）

> **读序**：实时工作项/附件 → 产品路由 → 按需当前产品需求历史 → 菜单索引 → 产品 wiki → 当前产品代码图谱 → 按需受控源码 → 按需数据库。路由未解析时不得调用任何产品 MCP；wiki 发现同源写 `checklist.items`/`kb_note`/`wiki.findings`，不判终局。

### Step R：按需补充需求历史

> **分析阶段默认执行**：进入分析阶段后，相似实现/历史方案查重**默认执行**（`AUTO-ANA` 硬要求 `kb.dedup_ran=true`；选用 `evidence-loop-v2` 时还须把查询记进 `evidence_acquisition.tfs_requirements.queries`，且 `AUTO-ANA` 须 `coverage_status∈{COMPLETE,OUT_OF_SCOPE}`）。只有纯联调、硬终局锁定或确不涉及历史背景时才 `SKIPPED` 并写明原因——**不得仅靠 `dedup_ran=true` 布尔声明而留空查询记录**（v2 由 `pipeline.py` 强制：查询为空则校验失败）。

1. 需要历史背景、显式关系、既有验收或相似需求线索时，先调 `get_requirements_summary` 固定覆盖范围；范围外或快照过旧记 `tfs_requirements.ready=false`/限制后继续。
2. 当前项关系用 `get_related_work_items`；同类历史用 `search_requirements` 检索。搜索摘要只记候选，必须再用 `get_work_item` 读取完整正文、验收和按需历史后才可记已证实。
3. **查重质量四要求（强制，权威定义见 `../_lib/knowledge-base.md` §四）**：查“同类历史方案/相似已实现”时必须同时满足——①**多组关键词**：`search_requirements` 至少 2 组正交维度（业务对象+动作 / 菜单+字段 / 现象+条件 / 规则名），不得 1 组泛化词命中即停，每组 query 与命中数记入 `tfs_requirements.note`；②**限定终态**：历史相似需求查重**只考虑 `state`∈{已验证,已关闭}** 的工作项——`search_requirements` 用 `state` 参数过滤到这两个状态；其它状态（已建议/活动/已解决等未达终态）不作为相似需求查重依据，最多作背景线索；③**get_work_item 核验**：`search` 命中必须经 `get_work_item` 读完整正文，核对工作项状态/开发者描述/变更原因后才判 `maturity`，只看搜索摘要不得判；④**时间倒序复查（防锚定老公版，不替换相关性）**：`search_requirements` 按相关度返回（相关度高 ≠ 最新），命中集合须再按 `changed_date`（缺失时取 `created_date`）倒序复查一遍，确保最新条目（尤其近一年/当年）不被相关度排序埋没；锚点仍选“最直接覆盖本次诉求”的已落地候选——仅当最新候选更直接相关时才优先它（公版演进、旧需求可能被替代/升级），**不得为“新”而牵强匹配**（最新条目不覆盖、仅较旧命中覆盖时仍选较旧那个）；每个候选均须经 ③get_work_item 核验 maturity。
4. **方案成熟度 `maturity`（强制标注，取值见 `../_lib/knowledge-base.md` §六）**：`已落地`=工作项状态∈{已验证,已关闭} 且有开发者描述/数据库脚本/代码合并记录；`分析确认`=有分析者描述但实现未完成/不明；`设想`=仅描述/验收/复现步骤里提及的解决思路（如"解决：…""建议…""应当…"）。**只有 `state=已证实` 且 `maturity=已落地` 才可声称"历史方案已落地"或作为查重命中/相似实现依据；`maturity=设想`（典型如描述里一句"解决：…"）只能作"历史上有过类似设想"的背景线索，不得当已落地方案或方案成熟度证据。** **一套公版下，若该已落地能力覆盖本次诉求 → 判"功能已存在"**：分析须声明 `existing_feature.satisfied=true`、在描述以"该功能已存在"+需求号领起、判 MANUAL-REVIEW（不得 AUTO-ANA，见 `../config/analysis-rules.md` §判定优先级与 `confidence-heuristic.md` 第 4 项反向条款）；仅相似而本次是新增量改动（既有能力未完整覆盖本次诉求）则不触发，仍可走 AUTO 第 4 项正向条件。
5. 结果写入顶层 `tfs_requirements`；已证实只表示历史工作项事实，不表示当前规则仍生效或代码已实现。历史相似项可使清单更具体；**质控阶段的查重仍为预留，不单独改变质控 verdict**（NEED-INFO/NEED-REVIEW），也不能进入 `qc_evidence_resolution`。**但分析阶段命中 `maturity=已落地` 且能力覆盖本次诉求时，须按"功能已存在"判 MANUAL-REVIEW、在描述标注"该功能已存在"+需求号、声明 `existing_feature.satisfied=true`（见 `confidence-heuristic.md` 第 4 项反向条款、`analysis-rules.md` §判定优先级）——这是分析终局规则，非质控 verdict 改变。**

### Step 0：探活并固定范围

1. 先用 `resolve-route` 固定 `knowledge_route`；只在 `RESOLVED` 时调用 `knowledge_route.servers.code_graph` 对应服务的 `list_repos`，从实时清单选定明确 `repo`，不硬编码仓库名。记录索引时间、节点/流程数和 `embeddings`；质控只需目标 repo 的探活与覆盖摘要，不必处理全部 stats。
2. 代码图谱不可用、超时或重建中：记 `kb_ready=false`，按 `config/qc-rules.md` 默认规则继续，不阻断、不转 ERROR。
3. 数据库工具只在 Step 3 需要时探活；数据库不可用不影响代码侧佐证。

### Step 1：代码图谱优先定位

1. 分别检索业务对象、业务动作、技术锚点，勿将需求原文整句塞入查询。查询词以 [`../_lib/business-keyword-dictionary.md`](../_lib/business-keyword-dictionary.md) 的同义词/拼音缩写/英文领域名扩展（`embeddings=0` 下中文语义召回弱）。
2. **覆盖度回环（强制）**：至少 **2 组正交角度**（业务对象 / 业务动作 / 技术锚点 / 菜单+字段），每组记 query+命中数+是否截断（落 `evidence_acquisition.gitnexus.queries`）。停止：(a) ≥1 已证实闭合核心问题，**或** (b) ≥2 角度空且已试下条回退。**首轮窄命中不得直接停手**（可能是错目标），须第二角度交叉验证。
3. **首轮空回退链（强制）**：首轮 `query` 全空时不得记缺口了事，依次试 ①字典同义词/拼音缩写 ②`cypher` 结构查 ③`impact` 反向找调用方 ④`traverse_graph` ⑤换 repo；仍空才记"代码图谱覆盖缺口"。
4. 从候选页面/API/符号提取精确 API 路径、Controller、DTO、Mapper 或方法名，再做第二轮检索。
5. 对候选使用 `query → context → route_map（有精确 API 时）→ context` 建立入口和关系。`processes` 或 `route_map` 为空时，继续用 `context` 查 Controller、Service、Mapper 的调用边。
6. 质控只需证明对象/动作、可追踪入口和明确关系；不做完整调用链枚举。高风险需估计影响面时可调用 `impact`；接口风险分层可调用 `route_map`。

### Step 2：判定与记录

- 对象与动作匹配、存在入口且有明确关系：记为**已证实**。
- 仅名称/检索匹配，缺入口或关系：记为**候选**并写缺失证据。
- 图谱无关系或索引覆盖不足：记为**未确认**；不得按名称猜测后端、表或调用链。

每条同时标注“图谱事实 / 推断 / 未确认”。将命名实体用于补强现有 `qc-checklist.md` #3 可识别性或 #5 重复问题，不新增 KB 专属规则或 verdict。

### Step 2.5：按条件核验源码

只当已有 GitNexus 精确 repo、路径或符号锚点，且方法体、条件分支、字段赋值、配置、SQL 或模板绑定可直接消除质控阻断时，置 `kb.source_required=true` 并调用当前 profile 的源码 MCP。已知符号用 `search_symbol`，精确路径/字面量用 `search_source`，每轮最多 3–5 个关键文件。禁止无锚点全仓搜索，GitNexus 不可用时不得用源码 MCP 代替发现。无命中、截断、白名单外或路径拒绝只记覆盖不足，不证明实现不存在。

### Step 3：按需数据库联查

仅在需求涉及字段、迁移、统计口径、数据权限、性能或数据库脚本时运行：

1. 用已证实的表名、字段名、Mapper、方法名或 SQL 片段查询 `Table`、`Column`、`CodeSymbol` 或 `SQLStatement`，限制较小 `limit`；仅对确认候选调用 `get_table_knowledge`。
2. 精确锚点无命中时，再用业务缩写、同义词或命名转换查询 `Table`，按 `database_alias` 查表详情。
3. 两轮均无结果时，记"数据库图谱覆盖缺口；真实表/字段待 Mapper SQL 映射确认"（落 `evidence_acquisition.db_knowledge`，`coverage_status=PARTIAL/UNKNOWN`）。不得用名称相似的表补链，也不得因数据库无命中否定代码侧发现。**负面推断禁令**：`search_knowledge` 无命中只代表"图谱未覆盖/未命中"，**不等同于"库不存在该表/字段"**——覆盖不全时严禁下负面结论，只有 `coverage_status=COMPLETE ∧ exhausted ∧ 未截断` 的无命中才允许。

### Step 4：写发现与复核

- `checklist.items`：把抽象问题变为带实体名和证据状态的选择式问题。
- `checklist.kb_note`：简洁汇总已证实、候选、未确认及图谱限制。
- 顶层 `kb.findings`：写 `[{entity, state, source_tool, source_type}]` 供审计和下游机读；`source_type` 固定为 `code|database`。`kb.ready` 表示代码图谱，`kb.source_ready` 表示源码 MCP 工具已注入，`kb.source_required` 表示本轮结论依赖源码核验，`kb.database_ready` 表示数据库图谱。源码 finding 的 `source_tool` 仅允许 `search_source|search_symbol` 且 `source_type=code`。

初判 `NEED-REVIEW` 时，先逐项判断该 finding 是否直接回答初判问题。只有 `kb:` 或 `wiki:` 的**已证实** finding 同时满足“同业务范围、现状规则明确、与需求不冲突、无剩余正确性/方向类缺口”时，才在后续分析计划写 `qc_evidence_resolution.items[]` 并复核为 `PASS`。不得用 `work-item`、候选/推断、名称相似或只定位到技术实体的发现放行。

历史相似需求在**质控阶段**只作为清单与审计线索，不单独改变质控 verdict；疑似已实现的 KB/wiki 事实仍按既有维度处理，存疑时按既有规则判 `NEED-REVIEW`。**分析阶段**若判定既有能力已满足本次诉求（一套公版、`maturity=已落地`），须按"功能已存在"判 MANUAL-REVIEW 并在描述标注（见上 Step R 第 4–5 步与 `confidence-heuristic.md` 第 4 项）。本 skill 只读需求历史和知识库，不写回。
