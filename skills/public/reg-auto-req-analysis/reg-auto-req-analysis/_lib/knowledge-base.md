# 知识库接入规范（需求历史 + GitNexus 代码图谱 + 数据库图谱）

> **共享规范**：`auto-req-analysis` 的**阶段一质控**（有目的发现链）与**阶段二分析**（核心依赖）共用本文件作为知识库接入的单一权威来源。需求历史、代码/数据库图谱均由只读 MCP 提供；改这里，两阶段自动生效。
>
> 知识库以 **MCP 服务**提供，由 **skill 执行者（LLM agent）按需调用其原生 MCP 工具**——不写 Python 客户端、不引入新依赖（`_lib` 保持纯 urllib）。
>
> 此外还有第四个知识源**产品 wiki**（业务语义层，非 MCP、本地 markdown，入口为仓库根 [`wiki/index.md`](../../../wiki/index.md)，skill 侧接入见 [`wiki-kb-recipe.md`](wiki-kb-recipe.md)）。本文件的探活/三态/降级/审计原则同样适用于它，读序见 §四、消费见 §五、审计字段见 §六。

---

## 一、三个 MCP 服务

| 来源 | MCP URL | 默认范围 | 工具 | 用途 |
| ---- | ------- | ------------ | ---- | ---- |
| **需求历史** | `http://172.16.0.192:4751/mcp` | `WN_PH-Platform/NETHIS5.5` 最近采集范围 | `get_requirements_summary` / `get_related_work_items` / `search_requirements` / `get_work_item` | 历史业务背景、工作项关系、验收条件、附件清单与变更原因 |
| **代码图谱** | `http://172.16.0.192:4747/api/mcp` | 实时 `list_repos` 选定（不硬编码；旧默认 `cloudhis-unified` 不在仓库清单，已废止） | `list_repos` / `query` / `context` / `route_map` / `impact` / `cypher` | 路由、调用链、影响分析、跨项目代码、模块/类/方法定位 |
| **数据库图谱** | `http://172.16.0.192:4748/mcp` | `db-knowledge` | `search_knowledge` / `get_table_knowledge` / `traverse_graph` | 表、字段、读写关系、文档说明 |

> `tfs-requirements` 是历史快照，不是当前工作项实时读取入口。当前字段、revision 和 state 始终以 `tfs_client.py fetch` 为准；当前代码实现以 GitNexus/受控源码为准。

---

## 二、接入前提（环境配置，非 skill 代码）

在 **Codex** 执行 skill 时，先注册三个全局 HTTP MCP，重启 Codex 后用 `/mcp` 检查。MCP 工具在会话启动时注入；当前会话不会热加载新配置：

```bash
codex mcp add gitnexus-team --url http://172.16.0.192:4747/api/mcp
codex mcp add db-knowledge --url http://172.16.0.192:4748/mcp
codex mcp add tfs-requirements --url http://172.16.0.192:4751/mcp
codex mcp list   # 三者均应显示 enabled
```

在 **Claude Code** 执行 skill 时，改用对应的 user-scope 配置：

```bash
claude mcp add --transport http gitnexus-team --scope user http://172.16.0.192:4747/api/mcp
claude mcp add --transport http db-knowledge --scope user http://172.16.0.192:4748/mcp
claude mcp add --transport http tfs-requirements --scope user http://172.16.0.192:4751/mcp
claude mcp list
```

无论使用哪个客户端，都必须在新会话的工具列表里看见对应 MCP 工具后，才可将该来源标为就绪。

---

## 三、就绪检测 + 降级（关键安全网）

skill 在用到对应知识源前**先探活并固定覆盖范围**：

1. **工具是否存在**：分别检查 `tfs-requirements`、`gitnexus-team`、`db-knowledge` 的原生工具；某一来源缺失只标该来源未就绪。
2. **需求历史探活**：仅在需要历史背景、关系、验收或变更原因时调用一次 `get_requirements_summary`，把 collection、project、起止时间和最近采集时间写入 `tfs_requirements.coverage`。覆盖范围外或快照过旧只能记限制。
3. **代码探活**：调用 `list_repos`，按 menu/wiki 候选**从实时返回的仓库清单中选定 `repo`**（不硬编码仓库名；实测 `cloudhis-unified` 不在清单，旧文档该默认值已废止，菜单候选 repo 必须实际存在才采用）。记录 `indexedAt`、节点数、流程数与 `embeddings` 状态——**当前全部仓库 `embeddings=0`，中文语义召回弱，查询词必须走精确锚点 + [`business-keyword-dictionary.md`](business-keyword-dictionary.md) 同义词/拼音缩写**；索引稍旧但可查询时可继续，并标注其局限。
4. **数据库探活**：仅在本需求确实需要数据库联查时，调用一次轻量 `search_knowledge`。数据库不是普通模块定位或需求分析的前置条件。

**降级原则（必须遵守）**：

- 代码图谱**不可用绝不阻断 skill、绝不转 ERROR**。未就绪 → 记 `kb_ready=false`，按各自默认路径继续：
  - **质控**：跳过 KB 发现链，按 `config/qc-rules.md` 默认判定。
  - **分析**：跳过图谱定位与相似实现命中，走 `references/confidence-heuristic.md` 启发式（第 4 项"相似需求命中"在 KB 未就绪时跳过，不因此判 MANUAL）。
- 数据库图谱未就绪或无命中时，保留已证实的代码级结论，记入 `kb.note`；除非需求本身以数据库事实为必要前提，否则不得因此转 ERROR、判 MANUAL 或否定模块。
- **需求历史 MCP** 缺失、超时、范围外或快照过旧时，记 `tfs_requirements.ready=false` 与具体原因后继续；不得因此转 ERROR、改变 verdict 或否定当前工作项。与实时 fetch、当前附件、现场事实或代码事实冲突时并列记录冲突并形成缺口，不自行选择历史版本。
- **产品 wiki**（业务语义层，见 `wiki-kb-recipe.md`）缺失或未覆盖该模块时，记 `wiki.ready=false` 或 `wiki.note=未覆盖`，回退 GitNexus+菜单索引，**绝不阻断、绝不转 ERROR**。**例外·界面/布局类需求**：wiki 是界面现状基线的必需交叉证实源之一（见 `../references/iteration-analysis-closure.md` §1）；未覆盖时记 `evidence_gap`（"界面现状缺 wiki 交叉证实"），AUTO-ANA 候选降为 MANUAL-REVIEW，MANUAL 路径不因此升级（与下条「不升级」原则的 AUTO 例外同等处理）。GitNexus 不可用时 wiki 仍可供业务判断，但不替代代码定位与查重。
- 探活失败要**显式记进审计**（`tfs_requirements.ready=false` / `kb_ready=false` / `wiki.ready=false` 或具体原因），不能静默。
- **「不升级」原则仅适用 MANUAL 路径**：上述"绝不因此判 MANUAL"只对 `MANUAL-REVIEW` 生效。`AUTO-ANA` 是无人工兜底的自动放行路径，硬要求 `kb.ready=true` 且 `kb.dedup_ran=true`（相似实现查重实际执行）；优化类类别还须 `kb.findings` 至少一条 `state=已证实` 锚定被改现有模块/入口。缺失则降 `MANUAL-REVIEW`（不加 `STOP-AUTO`，属信息缺口而非高风险）。由 `pipeline.py` 强制校验。

---

## 四、调用规范

- **需求历史在当前项/附件之后读取**：实时 `fetch` 与成功解析的当前附件完成后，按需调用 `get_requirements_summary` 固定覆盖范围。当前工作项存在显式关系时优先 `get_related_work_items`；需要查同类历史时以业务对象、动作、菜单或字段调用 `search_requirements`。搜索摘要、名称相似和汇总统计只算候选；只有再经 `get_work_item` 读取完整正文/验收/按需精选修订历史，或由 `get_related_work_items` 返回明确关系的事实，才可标为已证实。
- **查重质量四要求（权威定义·两阶段共用）**：查“同类历史方案/相似已实现”时必须同时满足：①**多组关键词**——`search_requirements` 至少 2 组正交维度（业务对象+动作 / 菜单+字段 / 现象+条件 / 规则名），不得 1 组泛化词命中即停，每组 query 与命中数记入 `tfs_requirements.note`；②**限定终态**——历史相似需求查重**只考虑 `state`∈{已验证,已关闭}** 的工作项；其它状态（已建议/活动/已解决等未达终态）不作为相似需求查重依据，最多作背景线索；③**get_work_item 核验**——`search` 命中必须经 `get_work_item` 读完整正文，核对工作项状态/开发者描述/变更原因后才判 `maturity`（定义见 §六），只看搜索摘要不得判；④**时间倒序复查（防锚定老公版，不替换相关性）**——`search_requirements` 按**相关度**返回，相关度高 ≠ 最新；命中集合须再按 `changed_date`（缺失时取 `created_date`）**倒序复查一遍**，确保最新条目（尤其近一年/当年）不被相关度排序埋没。**最终锚点仍选“最直接覆盖本次诉求”的已落地候选**（覆盖度/相关性是选择主标准）：仅当最新候选与本次诉求**更直接相关**时才优先它（公版能力随版本演进，旧需求可能已被近期需求替代或升级）；**不得为“新”而牵强匹配**——若最新条目不覆盖本次诉求、仅有较旧命中覆盖，仍选较旧那个。每个候选（无论新旧）均须经 ③`get_work_item` 核验 maturity。操作步骤见 `../references/qc-kb-recipe.md` Step R。
- **需求历史只证明工作项事实**：已证实 finding 表示“某历史工作项/关系/验收或变更记录确实如此”，不表示需求已上线、仍生效或当前实现如此。历史事实可补充业务背景、问题目标、范围与验收来源；当前字段仍以实时 `fetch` 为准，当前业务规则需 KB/wiki/现场闭合，当前实现需 GitNexus/受控源码闭合。**`state=已证实` 只表示工作项事实已核实，不等同于方案已落地——是否已落地另由 `maturity` 字段判定（见 §六）；不得用 `maturity=设想` 的 finding 支持"已实现/已存在方案"结论。**
- **一套公版假设（强制）**：当前不按项目/客户/版本拆分查重——同一产品线视为一套公版能力，落地能力视为当前环境普遍具备。命中 `state=已证实` 且 `maturity=已落地` 的历史方案时，须进一步判断其能力是否**覆盖/可满足本次需求诉求**：覆盖 → "功能已存在" → 分析终局 MANUAL-REVIEW（见 §六"功能已存在判定"与 `references/confidence-heuristic.md` 第 4 项反向条款，pipeline 硬闸 `existing_feature.satisfied=true ∧ verdict=AUTO-ANA` 强制）；仅相似但本次是**新的增量改动**（如列已存在、本次调其顺序/文案/默认值，既有能力未完整覆盖本次诉求）则不构成"功能已存在"，仍走 AUTO 第 4 项正向条件。
- **菜单索引先按区域收敛（辅助候选）**：若 `skills/reg-auto-req-analysis/_lib/menu-business-index.json` 存在，先取工作项 `area`（优先 `System.AreaPath` 首级；缺失时才用 `teamProject`）精确匹配索引的 `tfs_area_values`——用 `build_menu_business_index.py lookup --area <area>`（见 `_lib/COMMANDS.md`）一步查候选产品，**不要手写 jq/grep 读索引 JSON**。唯一命中时，菜单的 `repo`、`module_url`、`apis` 仅作为 GitNexus 的候选锚点；`matched` 可带技术候选，`route-not-found` / `no-module` 只带业务词和菜单路径。区域未配置、索引不存在或范围不唯一时，记录“菜单索引范围未确认”，继续原有 GitNexus 检索，绝不猜测产品。
- **产品 wiki 取业务语义（GitNexus 之前）**：区域收敛后，完整读 `wiki/index.md`，按门诊/住院/电子病历/平台/数据库定位文章并解析相对链接；用于 QC 放行、高风险或 AUTO 关键证据的事实再沿文章 `Raw` 链接按需复核。取模块的业务规则/流程/数据口径/验收 + **子系统名/仓库名**。子系统名/仓库名仅作 GitNexus 的**候选锚点**，不替代 `query → context → route_map/context` 证据链；**wiki 叙述与代码事实冲突时以 GitNexus 为准（代码库最准、wiki 仅补充）**；wiki 未覆盖该模块或关键 Raw 不可用时记录限制后继续，不阻断（详见 `wiki-kb-recipe.md`）。
- **先定 repo，再检索**：不得省略仓库范围直接全局搜索，也不得将第一次命中的文件当结论。
- **两轮收敛**：先分别查询业务对象、业务动作、技术锚点；从前端页面/API 或候选符号提取 API 路径、Controller、DTO、表名等精确锚点后，再做第二轮查询。`embeddings: 0`（当前全部仓库实测为 0）时**优先精确术语、路径和符号名，并以 [`business-keyword-dictionary.md`](business-keyword-dictionary.md) 的同义词/拼音缩写/英文领域名扩查询词**，不能依赖纯中文语义召回。
- **覆盖度回环（强制·修复"一次性采集"漏召回）**：每个被触发的知识源至少跑 **2 组正交查询角度**（业务对象 / 业务动作 / 技术锚点 / 菜单+字段 / 规则名），每组记 query+命中数+是否截断（落 `evidence_acquisition.<源>.queries`，见 §六）。停止条件：(a) ≥1 已证实 finding 闭合核心问题，**或** (b) ≥2 角度空 **且**已试过下条回退链。**首轮有窄命中不得直接停手**——窄命中可能是错目标，仍须第二角度交叉验证。
- **首轮空回退链（强制）**：代码 `query` 首轮全空时**不得记缺口了事**，依次试 ①字典同义词/拼音缩写 ②`cypher` 结构查（按表/类/关系） ③`impact` 反向找调用方 ④`traverse_graph` ⑤换 repo（按菜单/wiki 候选）。仍空才记"代码图谱覆盖缺口"。db 侧已有"中文无命中不能停止"（见下），代码侧同等强制。
- **证据链优先**：按 `query → context → route_map（有精确 API 路径时）→ context` 追踪入口与调用/导入关系。`processes` 或 `route_map` 为空不代表业务不存在；继续对命中的 Controller、Service、Mapper 使用 `context` 验证调用边。
- **按需联查数据库**：只有新增/修改字段、数据迁移、统计口径、数据权限、性能优化或数据库脚本等数据需求，才从已证实的表名、字段名、Mapper、方法名或 SQL 片段开始 `search_knowledge`（限定 `Table` / `Column` / `CodeSymbol` / `SQLStatement`，较小 `limit`）并仅对确认候选调用 `get_table_knowledge`。没有已建立的代码 SQL/契约关系时，不能把代码和同名/近名表写成同一流程。
- **字段反向定位闭环**：字段类需求按“业务词及同义词 → 页面绑定字段/提交参数 → DTO/Mapper/SQL → 正式表列 → 目标查询是否已返回”逐层取证。中文业务词无命中时，必须扩展产品同义词、代码注释用语、缩写和命名转换；只有页面或载荷语义与数据库 `Column`、在用 `READS`/`WRITES` 或 SQL 同时闭环，才把字段记为已证实。缺任一层时只能记候选或未确认。
- **正式表筛选**：同名表、日期后缀表、备份表和测试表仅作候选；必须用在用仓库的代码读写关系、SQL 来源和业务键确认正式表。不得因列名相同，把 `_bak`、`_test`、日期后缀或其他近似表写成当前业务表。
- **覆盖缺口停止条件**：精确代码锚点无命中后，再以业务缩写、同义词或数据库命名转换查询 `Table`/`Column`，并按返回的 `database_alias` 查表；仍无结果才记录“数据库图谱覆盖缺口”，把真实表/字段待 Mapper SQL 映射列为未确认。定位到字段后仍须反查目标查询；字段存在但目标 SQL 未读取时，应记录“目标数据链缺字段”，不能误判为需求已实现。
- **DB 负面推断禁令（强制）**：数据库图谱覆盖率未知（实测外键关系稀疏），`search_knowledge` 无命中**只代表"图谱未覆盖/未命中"，不等同于"库不存在该表/字段"**。覆盖不全时只能记 `evidence_acquisition.db_knowledge.coverage_status=PARTIAL/UNKNOWN` + 缺口（`DB_SCHEMA_PARTIAL`/`DB_RELATION_PARTIAL`），**严禁**写成"数据库无此表/字段"的负面结论；只有 `coverage_status=COMPLETE ∧ exhausted ∧ 未截断` 的无命中才允许下负面判断。
- **证据三态标注（强制）**：每条来自 KB 的结论必须标注来源——
  - **图谱事实**：有明确节点/关系支撑（引用实体名）。
  - **推断**：基于图谱事实 + 推理得出。
  - **未确认**：图谱无依据 → **写"未确认"，不猜测、不编造**。

菜单索引由 `python3 skills/reg-auto-req-analysis/_lib/build_menu_business_index.py` 根据 `menu-sources.json` 构建。每个菜单源必须显式声明 `product_id`、`product_name`、`tfs_area_values` 和原始文件路径；一个区域只能归属一个产品。它是可迁移的产品入口索引，不是 GitNexus 图谱事实，也不允许替代 `query → context → route_map/context` 的证据链。

---

## 五、各 skill 怎么用本规范

### 阶段一质控 —— 有目的发现链·不直接判终局

- **需求历史怎么用**：仅在需要历史背景、显式关系、既有验收或相似需求线索时按需查询。结果可使 `checklist.items` 更具体，并记录到 `tfs_requirements.findings`；当前“需求查重**质控**”仍为预留项，相似历史工作项不能单独改变**质控** verdict，也不能用于 `qc_evidence_resolution` 放行。（分析阶段另论：命中 `maturity=已落地` 且能力覆盖本次诉求 → “功能已存在” → MANUAL-REVIEW，见 §四/§六与 `references/confidence-heuristic.md` 第 4 项。）
- **触发条件（判定面·字典驱动）**：需求命中高风险早筛（`high-risk-categories.md` ①②⑤⑥），**或**描述/标题/验收**触及 [`business-keyword-dictionary.md`](business-keyword-dictionary.md) 中任一业务概念**（标准名/同义词/旧称/拼音缩写任一字面或近义）时，跑发现链。字典作**召回辅助而非封闭清单**——字典之外的真实业务词仍可识别；字典覆盖门诊/住院/电子病历/平台/医保结算/检查检验/数据库表前缀等域。**不再限于"结算/费用/医保/上传/校验/报表/接口/明细/账单"9 词枚举**（旧枚举漏掉对账/转科/处方/发药/库存/床位/退药等大量同义词需求）。**不再要求"已给具体表/字段名"**——用户侧业务词≠schema 标识符。判"纯文字、无业务实体且非高风险"而跳过时，**必须在 `kb.note` 写明"对照字典检查了哪些概念、为何判定未命中"**，使"真无实体"与"没认出实体"可区分（主动跳过，非降级）。
- **做什么（发现链，见 `../references/qc-kb-recipe.md`）**：先用代码图谱按三组词和精确技术锚点定位可追踪入口/关系；涉及数据变更时，才用已证实代码锚点锁定表、列和 SQL 读写关系。`route_map` 或流程缺失时回退 `context`，不得按名称推定后端或数据表。
- **结果去向**：发现写入 `checklist.items`（命名实体的选择式问题）/ `checklist.kb_note`（三态摘要）/ `kb.findings`（机读）。
- **wiki 同源**：产品 wiki 的业务语义发现同样写入 `checklist.items`/`kb_note`/`wiki.findings`，经现有维度（#3 可识别性 / #5 重复）参与终局；**spec 清晰度闸**——wiki 表明的既有规则与需求假设冲突 → `NEED-REVIEW`。见 `wiki-kb-recipe.md` §4。
- **与终局的关系（精化）**：**不直接判终局**——不加 verdict 类别、**不在 `qc-rules.md` 新增 KB 专属判定步**；但 KB 发现（含疑似重复/已实现）可写成清单条目，经现有维度（`qc-checklist.md` #3 可识别性 / #5 重复）参与终局，存疑即 NEED-REVIEW。**图谱无命中也是有效发现**：如实记"已做判定性查询（工具+关键词），图谱无该侧数据"，比"未做判定性查询"是审计改进。

### 阶段二分析 —— 内部佐证·PM 视角产物

- **需求历史是业务追溯佐证**：经 `get_work_item` 或明确关系确认的 finding 可用 `req:<索引>` 引用到 v2 `evidence_refs`，用于历史背景、问题目标、范围或验收来源。它不进入 PM 正文的技术定位，不替代 `kb:`，也不能满足 `kb.ready`、`kb.dedup_ran` 或 AUTO 的现有实现锚点。
- **代码图谱定位是内部佐证，不进变更方案正文**。变更方案/分析者描述是 **PM/业务视角**（业务场景/规则/验收/范围；见 `change-plan-template.md`）；其中 `路径` 只写业务菜单路径和操作路径。代码图谱按 `module-location-recipe.md` 完整方法**内部定位**主模块/调用链/关键文件，结果落 `kb.findings`（已证实/候选/未确认 + 三态），用于：印证 AUTO/MANUAL 的业务改动类型判断、查重（`confidence-heuristic.md` 第 4 项）、高风险定性佐证（③④⑥），不把仓库、类、方法、接口、Mapper 或表字段写入正文。
- 相似已实现需求命中：由代码图谱支撑 `confidence-heuristic.md` 第 4 项。
- **wiki 作业务内部佐证**：wiki 的业务规则/流程/数据口径同样落 `wiki.findings`，只作内部佐证、**不进变更方案正文**；其子系统名/仓库名仅供 GitNexus 锚定，可写进 testing-log 的"知识图谱作用"区。见 `wiki-kb-recipe.md` §4。
- KB 未就绪 → 走启发式（第 4 项跳过），定位写"待 dev-design 确认"落 `kb.findings`（非产物待确认）。**AUTO-ANA 路径**仍要求 KB 就绪 + 查重执行 +（优化类）已证实锚点（见 §三），缺失则降 `MANUAL-REVIEW`；MANUAL 路径不因此升级。

---

## 六、审计字段（需求历史、图谱与 wiki）

需求历史使用独立的可选 `tfs_requirements` 对象，不能并入 `kb.findings`：

```json
"tfs_requirements": {
  "ready": true,
  "coverage": {
    "collection": "WN_PH-Platform",
    "project": "NETHIS5.5",
    "created_from": "<summary 返回值>",
    "created_before": "<summary 返回值>",
    "latest_run_at": "<summary 返回值>"
  },
  "tools_used": ["get_requirements_summary", "search_requirements", "get_work_item"],
  "findings": [
    {"work_item_id": 260001, "fact": "历史需求明确了保存后的验收条件", "state": "已证实", "source_tool": "get_work_item", "maturity": "已落地"},
    {"work_item_id": 259999, "fact": "标题与当前对象相似，正文尚未核验", "state": "候选", "source_tool": "search_requirements", "maturity": "设想"}
  ],
  "note": "历史需求只补背景与验收；当前字段已使用实时 fetch"
}
```

`state` 取 `已证实` / `候选` / `未确认`，表示**证据是否经核验**（`search_requirements`/`get_requirements_summary` 只能产生 `候选`；只有 `get_work_item`/`get_related_work_items` 核验后才可标 `已证实`）。`req:` 不允许出现在 `qc_evidence_resolution`，也不能替代 AUTO 要求的 `kb:` 引用。`pipeline.py` 校验这些边界，并把对象透传到成功/失败审计。

`findings[]` 新增**可选**字段 `maturity`，表示**方案本身的成熟度**，与 `state`（证据是否核实）正交——三态取 `设想` / `分析确认` / `已落地`：
- `设想`：来自描述/验收/复现步骤的解决思路（"建议/可以考虑/应当/解决：…"），无实现证据；
- `分析确认`：工作项有分析者描述、方案已明确，但实现状态不明或未完成；
- `已落地`：工作项状态∈{已验证,已关闭} **且** 有开发者描述/数据库脚本/代码合并记录等实现证据。
省略时按 `分析确认`（保守）对待；`pipeline.py` 校验取值合法（∈ 三态），且 `maturity=已落地` 必须 `state=已证实`（设想级证据不得支撑已落地）。**关键约束**：只有 `state=已证实` **且** `maturity=已落地` 才可声称“历史方案已落地”；`maturity=设想` 即使 `state=已证实` 也只能作“历史上有过类似设想”的背景线索，**不得**用于支持“已实现/已存在方案/查重命中”结论（描述里的解决设想 ≠ 已落地实现）。

`req:<索引>` 引用分两级：**背景级**（业务背景/问题目标/范围/验收来源）`state=已证实` 即可；**落地级**（声称"已落地"、用于查重/相似实现命中/方案成熟度判断）必须 `state=已证实` 且 `maturity=已落地`。

**"功能已存在"判定（一套公版）**：当某条 finding `state=已证实 ∧ maturity=已落地`，**且其能力覆盖本次需求诉求**时，判"功能已存在"——本次无需新增改动，分析终局必须 `MANUAL-REVIEW`（不得 `AUTO-ANA`，由 `pipeline.py` 硬闸 `existing_feature.satisfied=true ∧ verdict=AUTO-ANA` 强制）；分析者描述须以"该功能已存在"领起并附需求号（如"需求 260647 已实现…"），结构化声明落顶层 `existing_feature: {satisfied: true, requirement_ids: [260647], note: "…"}`。仅相似而本次是新增量改动（既有能力未完整覆盖本次诉求）不构成"功能已存在"，仍可走 AUTO 第 4 项正向条件。

代码/数据库图谱继续使用现有 `kb` 对象：

```json
"kb": {
  "ready": true,
  "dedup_ran": true,
  "tools_used": ["list_repos", "search_knowledge", "get_table_knowledge", "query", "context"],
  "findings": [
    {"entity": "DischargedSettlementController.saveSettlement 调用链", "state": "已证实", "source_tool": "context"},
    {"entity": "费用明细/结算源表", "state": "未确认", "source_tool": "search_knowledge"}
  ],
  "note": "代码+数据库图谱均就绪；发现链命中出院结算控制器，DB 侧费用明细/结算表无命中"
}
```

代码图谱未就绪时 `"ready": false` 并在 `note` 写原因；仅数据库未就绪时保留代码级 `ready` 状态并写明限制。由 `pipeline.py` 透传到 `过程文件/<id>/runs/run_*.json`。`findings` 供审计/下游机读提取；质控具体发现写入 `checklist.items` 与 `kb_note`，分析技术发现只作内部佐证，不写入 PM 视角变更方案正文。分析者描述的业务菜单路径和操作路径来自工作项、已解析附件或唯一菜单索引，不等同于代码图谱技术发现。`dedup_ran`（bool）表示相似实现查重是否实际执行（KB 未就绪时为 false；AUTO-ANA 硬要求 true）。`findings[].state` 同时承载**证据三态**（图谱事实/推断/未确认）与**结论三态**（已证实/候选/未确认，定义见 `module-location-recipe.md` §5）；`AUTO-ANA` 的优化类类别须至少一条 `state=已证实` 锚定被改现有模块/入口——仅"图谱事实"等证据态不足以放行（由 `pipeline.py` 强制）。

**产品 wiki 的审计用并列的可选 `wiki` 对象**（语义与图谱不同，故独立字段；同样不在 `validate` 的 required 内、由 `pipeline.py` 透传）：

```json
"wiki": {
  "ready": true,
  "modules_matched": ["药房药库"],
  "findings": [
    {"entity": "门诊发药和退药归药房管理，药库出库归药库库存管理", "state": "wiki-确认", "source": "wiki/门诊/门诊药品流转.md"}
  ],
  "note": "命中门诊药品流转；关键事实已复核 raw/平台/药房药库.md，子系统名 batmed-yfyk-base-frame 已作 GitNexus query 锚点"
}
```

`state` 取 `wiki-确认` / `wiki-冲突` / `wiki-未覆盖`；`source` 为 wiki 页面相对路径。wiki 未就绪/未覆盖时 `"ready": false` 并在 `note` 写原因。字段定义详见 `wiki-kb-recipe.md` §6。

**v2 采集状态契约（`evidence_acquisition`，选用 `analysis=evidence-loop-v2` 时必填）**：上述 `kb`/`wiki`/`tfs_requirements` 记"发现了什么"，`evidence_acquisition` 额外记"**怎么查的、查全没有**"——按四源 `tfs_requirements`/`wiki`/`gitnexus`/`db_knowledge` 各填 `{availability, coverage_status, query_status, queries:[{terms, truncated, returned, total, verified_ids}], stop_reason}`，把每组查询词、命中数、是否截断、停止原因结构化留下，使"图谱无数据"与"没查/没查全"可区分。只有 `coverage_status=COMPLETE ∧ stop_reason=exhausted ∧ 未截断` 的 `query_status=NO_HIT` 才支撑负面结论；截断/未覆盖/未查只能 `PARTIAL/UNKNOWN`。校验器硬约束与字段结构详见 [`tfs/EXECUTION_CONTRACT.md`](tfs/EXECUTION_CONTRACT.md)「evidence-loop-v2」节。

---

## 七、后端模块定位方法论

精准定位"需求对应的后端模块/符号"的完整方法见 [`module-location-recipe.md`](module-location-recipe.md)（与本文同目录，两 skill 共读）：

- **口诀**：先搜业务语义，后看流程分组，再沿调用链验证；文件名只作线索，不作结论。
- **质控**跑其**佐证子集**（探活 + 三组词查询 + 可追踪入口/明确关系，不做完整调用链/源码验证）；**分析**跑**完整方法**（+ 从候选建立证据链 + 关键文件验证 + 结果 schema）**作内部佐证**——结果落 `kb.findings`、不进 PM 视角变更方案正文。

本文件规定"**怎么接 KB**"（探活/三态/降级/审计），`module-location-recipe.md` 规定"**怎么定位模块**"——两者互补。

## 八、产品 wiki 接入方法

业务语义层知识源（产品 wiki）的接入——何时读、如何从 `wiki/index.md` 定位主题文章并按需沿 `Raw` 复核、两阶段怎么消费、与 GitNexus 的读序和兜底——见 [`wiki-kb-recipe.md`](wiki-kb-recipe.md)（与本文同目录，两阶段共读）。本文件的降级/审计原则同样适用。

**读序**：`实时 fetch/当前附件 → tfs-requirements（历史需求事实，按需）→ menu-index（结构收敛）→ wiki（业务语义）→ GitNexus（代码事实）→ db-knowledge（按需）`；**实时 fetch 管当前项、GitNexus 管当前实现，历史需求与 wiki 只在各自证据域补充**。

---

> **边界**：本 skill 只**读**需求历史、图谱和 wiki，不写、不回流。需求历史或图谱的维护不在 skill 写入范围。
