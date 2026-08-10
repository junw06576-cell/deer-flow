# 产品知识源接入规范（产品路由 + 需求历史 + GitNexus + 受控源码 + 数据库）

> **共享规范**：`auto-req-analysis` 的**阶段一质控**（有目的发现链）与**阶段二分析**（核心依赖）共用本文件作为产品知识源接入的单一权威来源。非 SKIP 工作项先按 Area 唯一路由 `product_id`，再使用该产品 profile 的需求历史、代码图谱、源码和数据库只读 MCP；改这里，两阶段自动生效。
>
> 知识库以 **MCP 服务**提供，由 **skill 执行者（LLM agent）按需调用其原生 MCP 工具**——不写 Python 客户端、不引入新依赖（`_lib` 保持纯 urllib）。
>
> 此外还有**产品 wiki**（业务语义层，非 MCP、本地 markdown，入口为仓库根 [`wiki/index.md`](../../../wiki/index.md)，skill 侧接入见 [`wiki-kb-recipe.md`](wiki-kb-recipe.md)）。本文件的探活/三态/降级/审计原则同样适用于它，读序见 §四、消费见 §五、审计字段见 §六。

---

## 一、产品 MCP profile

四类角色固定，但 server name、URL 和实际产品范围由 [`../config/product-mcp-routes.json`](../config/product-mcp-routes.json) 按 `product_id` 配置。下表仅是当前 `cloudhis-v56` profile，不是全局默认：

| 角色 / 当前 server | 当前 MCP URL | 默认范围 | 工具 | 用途 |
| ---- | ------- | ------------ | ---- | ---- |
| **需求历史 / `tfs-requirements`** | `http://172.16.0.192:4751/mcp` | `WN_PH-Platform/NETHIS5.5` 最近采集范围 | `get_requirements_summary` / `get_related_work_items` / `search_requirements` / `get_work_item` | 历史业务背景、工作项关系、验收条件、附件清单与变更原因 |
| **代码图谱 / `gitnexus-team`** | `http://172.16.0.192:4747/api/mcp` | 实时 `list_repos` 选定 | `list_repos` / `query` / `context` / `route_map` / `impact` / `cypher` | 路由、调用链、影响分析、模块/类/方法定位 |
| **受控源码 / `cloudhis-source`** | `http://172.16.0.192:4752/mcp` | 服务端白名单仓库 + 允许路径 | `search_source` / `search_symbol` | 当前产品精确 repo + API/路径/符号锚点后的条件分支、字段赋值、配置、SQL、模板和方法实现核验 |
| **数据库图谱 / `db-knowledge`** | `http://172.16.0.192:4748/mcp` | 当前 CloudHIS 数据库图谱 | `search_knowledge` / `get_table_knowledge` / `traverse_graph` / `inspect_node` | 表、字段、读写关系、文档说明 |

> `tfs-requirements` 是历史快照，不是当前工作项实时读取入口。当前字段、revision 和 state 始终以 `tfs_client.py fetch` 为准。GitNexus 负责发现、关系和查重；源码 MCP 只核验已有精确锚点（锚点优先来自 GitNexus，也可来自工作项、附件、菜单或 wiki 的可追溯技术信息），不替代无锚点发现；二者均不证明现场部署版本或已上线状态。

---

## 二、产品路由与环境配置

产品归属由菜单索引中的 `tfs_area_values` 唯一确定：非 SKIP fetch 后取 `area`，调用 `build_menu_business_index.py resolve-route --area <area>`。只有 `route_status=RESOLVED` 才可使用返回 profile；其它状态 `servers={}`，不得回退 CloudHIS 或其它产品。当前 `cloudhis-v56` 在 **Codex** 的注册示例如下，重启 Codex 后用 `/mcp` 检查：

```bash
codex mcp add gitnexus-team --url http://172.16.0.192:4747/api/mcp
codex mcp add db-knowledge --url http://172.16.0.192:4748/mcp
codex mcp add tfs-requirements --url http://172.16.0.192:4751/mcp
codex mcp add cloudhis-source --url http://172.16.0.192:4752/mcp
codex mcp list
```

在 **Claude Code** 执行 skill 时，改用对应的 user-scope 配置：

```bash
claude mcp add --transport http gitnexus-team --scope user http://172.16.0.192:4747/api/mcp
claude mcp add --transport http db-knowledge --scope user http://172.16.0.192:4748/mcp
claude mcp add --transport http tfs-requirements --scope user http://172.16.0.192:4751/mcp
claude mcp add --transport http cloudhis-source --scope user http://172.16.0.192:4752/mcp
claude mcp list
```

无论使用哪个客户端，都必须在新会话工具列表里看见**已解析 profile 对应 server** 的工具后，才可将该来源标为就绪；`mcp list` 的 enabled 只证明配置存在。未来产品使用唯一 server name（建议 `<product_id>-<role>`）注册，新增产品只扩 Area 映射、profile、客户端注册和路由测试。

---

## 三、就绪检测 + 降级（关键安全网）

skill 在非 SKIP fetch 后**先解析产品，再探活该 profile 并固定覆盖范围**：

1. **产品路由**：`resolve-route` 必须唯一返回 `product_id` 和四类 server name；未解析时禁止调用任何产品 MCP，记录 `PRODUCT_ROUTE_UNRESOLVED`。AUTO-ANA 必须路由成功。
2. **工具是否存在**：只检查已解析 profile 的需求历史、代码图谱、源码、数据库原生工具；某一来源缺失只标该来源未就绪，绝不借用其它产品同角色服务。
3. **需求历史探活**：仅在需要历史背景、关系、验收或变更原因时调用一次 `get_requirements_summary`，把 collection、project、起止时间和最近采集时间写入 `tfs_requirements.coverage`。覆盖范围外或快照过旧只能记限制。
4. **代码探活**：调用当前 profile 的 `list_repos`，按 menu/wiki 候选从实时清单选定 `repo`；记录索引时间和覆盖状态。
5. **源码就绪**：启动只检查 `search_source` / `search_symbol` 是否注入，写 `kb.source_ready`；只有 `kb.source_required=true` 且已有当前产品“精确 repo + API/路径/符号/技术字面量”锚点时才实际查询。锚点优先来自 GitNexus，也可来自已解析附件、工作项明确技术信息、菜单索引或 wiki。
6. **数据库探活**：仅在本需求确实需要数据库联查时调用轻量查询。数据库不是普通模块定位或分析前置条件。

**降级原则（必须遵守）**：

- **产品路由未解析时不做跨产品降级**：`knowledge_route.status` 如实记录，`servers={}`；`kb/tfs_requirements/wiki` 不得声明就绪、工具调用或 finding。AUTO 候选降 MANUAL；QC/MANUAL 仅在当前项、附件等既有证据足够且不依赖产品知识时继续。
- 代码图谱**不可用绝不阻断 skill、绝不转 ERROR**。未就绪 → 记 `kb_ready=false`，按各自默认路径继续：
  - **质控**：跳过 KB 发现链，按 `config/qc-rules.md` 默认判定。
  - **分析**：跳过图谱定位与相似实现命中，走 `references/confidence-heuristic.md` 启发式（第 4 项"相似需求命中"在 KB 未就绪时跳过，不因此判 MANUAL）。
- 数据库图谱未就绪或无命中时，保留已证实的代码级结论，记入 `kb.note`；除非需求本身以数据库事实为必要前提，否则不得因此转 ERROR、判 MANUAL 或否定模块。
- **源码 MCP** 缺失时记 `kb.source_ready=false`；若 `source_required=false` 不额外改变终局。若结论依赖源码细节则保持 `source_required=true`：质控不得用缺失源码闭合阻断，分析写 `SOURCE_MCP_UNAVAILABLE` / `SOURCE_SCOPE_PARTIAL` / `SOURCE_VERIFICATION_INCOMPLETE`，AUTO 候选降 MANUAL。GitNexus 不可用时，已有当前产品精确 repo + 技术锚点仍可有限核验，但不得把源码 MCP 当无锚点发现、跨层关系或查重替代。
- **需求历史 MCP** 缺失、超时、范围外或快照过旧时，记 `tfs_requirements.ready=false` 与具体原因后继续；不得因此转 ERROR、改变 verdict 或否定当前工作项。与实时 fetch、当前附件、现场事实或代码事实冲突时并列记录冲突并形成缺口，不自行选择历史版本。
- **产品 wiki**（业务语义层，见 `wiki-kb-recipe.md`）缺失或未覆盖该模块时，记 `wiki.ready=false` 或 `wiki.note=未覆盖`，回退 GitNexus、受控源码和菜单索引，**绝不因 wiki 缺失本身阻断或转 ERROR**。界面/布局类须从产品知识、运行观察、实现证据三类中至少取两类交叉证实；只有两类无法闭合时才记 `evidence_gap` 并将 AUTO-ANA 候选降为 MANUAL-REVIEW。GitNexus 不可用时 wiki 仍可供业务判断，但不替代代码关系与查重。
- 探活失败要**显式记进审计**（`tfs_requirements.ready=false` / `kb_ready=false` / `wiki.ready=false` 或具体原因），不能静默。
- **「不升级」原则仅适用 MANUAL 路径**：上述"绝不因此判 MANUAL"只对 `MANUAL-REVIEW` 生效。`AUTO-ANA` 是无人工兜底的自动放行路径，硬要求 `kb.ready=true` 且 `kb.dedup_ran=true`（相似实现查重实际执行）；优化类类别还须 `kb.findings` 至少一条 `state=已证实` 锚定被改现有模块/入口。缺失则降 `MANUAL-REVIEW`（不加 `STOP-AUTO`，属信息缺口而非高风险）。由 `pipeline.py` 强制校验。

---

## 四、调用规范

- **先路由产品**：非 SKIP 先执行 `resolve-route`，将结果快照到 `knowledge_route`；后续每次调用都必须核对 server name 属于该快照。Area 未映射、歧义、profile 缺失/非法时不猜产品、不调用任何产品 MCP。
- **需求历史在当前项/附件之后读取**：实时 `fetch` 与成功解析的当前附件完成后，按需调用 `get_requirements_summary` 固定覆盖范围。当前工作项存在显式关系时优先 `get_related_work_items`；需要查同类历史时以业务对象、动作、菜单或字段调用 `search_requirements`。搜索摘要、名称相似和汇总统计只算候选；只有再经 `get_work_item` 读取完整正文/验收/按需精选修订历史，或由 `get_related_work_items` 返回明确关系的事实，才可标为已证实。
- **查重质量四要求（权威定义·两阶段共用）**：查“同类历史方案/相似已实现”时必须同时满足：①**多组关键词**——`search_requirements` 至少 2 组正交维度（业务对象+动作 / 菜单+字段 / 现象+条件 / 规则名），不得 1 组泛化词命中即停，每组 query 与命中数记入 `tfs_requirements.note`；②**限定终态**——历史相似需求查重**只考虑 `state`∈{已验证,已关闭}** 的工作项；其它状态（已建议/活动/已解决等未达终态）不作为相似需求查重依据，最多作背景线索；③**get_work_item 核验**——`search` 命中必须经 `get_work_item` 读完整正文，核对工作项状态/开发者描述/变更原因后才判 `maturity`（定义见 §六），只看搜索摘要不得判；④**时间倒序复查（防锚定老公版，不替换相关性）**——`search_requirements` 按**相关度**返回，相关度高 ≠ 最新；命中集合须再按 `changed_date`（缺失时取 `created_date`）**倒序复查一遍**，确保最新条目（尤其近一年/当年）不被相关度排序埋没。**最终锚点仍选“最直接覆盖本次诉求”的已落地候选**（覆盖度/相关性是选择主标准）：仅当最新候选与本次诉求**更直接相关**时才优先它（公版能力随版本演进，旧需求可能已被近期需求替代或升级）；**不得为“新”而牵强匹配**——若最新条目不覆盖本次诉求、仅有较旧命中覆盖，仍选较旧那个。每个候选（无论新旧）均须经 ③`get_work_item` 核验 maturity。操作步骤见 `../references/qc-kb-recipe.md` Step R。
- **需求历史只证明工作项事实**：已证实 finding 表示“某历史工作项/关系/验收或变更记录确实如此”，不表示需求已上线、仍生效或当前实现如此。历史事实可补充业务背景、问题目标、范围与验收来源；当前字段仍以实时 `fetch` 为准，当前业务规则需 KB/wiki/现场闭合，当前实现需 GitNexus/受控源码闭合。**`state=已证实` 只表示工作项事实已核实，不等同于方案已落地——是否已落地另由 `maturity` 字段判定（见 §六）；不得用 `maturity=设想` 的 finding 支持"已实现/已存在方案"结论。**
- **一套公版假设（强制）**：当前不按项目/客户/版本拆分查重——同一产品线视为一套公版能力，落地能力视为当前环境普遍具备。命中 `state=已证实` 且 `maturity=已落地` 的历史方案时，须进一步判断其能力是否**覆盖/可满足本次需求诉求**：覆盖 → "功能已存在" → 分析终局 MANUAL-REVIEW（见 §六"功能已存在判定"与 `references/confidence-heuristic.md` 第 4 项反向条款，pipeline 硬闸 `existing_feature.satisfied=true ∧ verdict=AUTO-ANA` 强制）；仅相似但本次是**新的增量改动**（如列已存在、本次调其顺序/文案/默认值，既有能力未完整覆盖本次诉求）则不构成"功能已存在"，仍走 AUTO 第 4 项正向条件。
- **菜单索引先按区域收敛（辅助候选）**：若 `skills/reg-auto-req-analysis/_lib/menu-business-index.json` 存在，先取工作项 `area`（优先 `System.AreaPath` 首级；缺失时才用 `teamProject`）精确匹配索引的 `tfs_area_values`——用 `build_menu_business_index.py lookup --area <area>`（见 `_lib/COMMANDS.md`）一步查候选产品，**不要手写 jq/grep 读索引 JSON**。唯一命中时拿到产品级候选；代码定位锚点走 wiki 子系统名/仓库名 + GitNexus `query`→`context`，不再依赖菜单级 `repo`/`module_url`/`apis`。区域未配置、索引不存在或范围不唯一时，记录“菜单索引范围未确认”，继续原有 GitNexus 检索，绝不猜测产品。
- **产品 wiki 按需取业务语义**：区域收敛后，仅在命中业务概念/高风险或确需业务规则、流程、口径、验收与布局语义时，完整读 `wiki/index.md`，按业务域定位文章并解析相对链接；用于 QC 放行、高风险或 AUTO 关键证据的事实再沿文章 `Raw` 链接按需复核。子系统名/仓库名可作技术候选锚点，但不替代 GitNexus 关系证据；wiki 与已核验代码冲突时，实现事实以代码为准，只有影响业务规则、范围或验收正确性时才进入 QC spec 闸（详见 `wiki-kb-recipe.md`）。
- **先定 repo，再检索**：不得省略仓库范围直接全局搜索，也不得将第一次命中的文件当结论。
- **两轮收敛**：先分别查询业务对象、业务动作、技术锚点；从前端页面/API 或候选符号提取 API 路径、Controller、DTO、表名等精确锚点后，再做第二轮查询。`embeddings: 0`（当前全部仓库实测为 0）时**优先精确术语、路径和符号名，并以 [`business-keyword-dictionary.md`](business-keyword-dictionary.md) 的同义词/拼音缩写/英文领域名扩查询词**，不能依赖纯中文语义召回。
- **覆盖度回环（强制·修复"一次性采集"漏召回）**：每个被触发的知识源至少跑 **2 组正交查询角度**（业务对象 / 业务动作 / 技术锚点 / 菜单+字段 / 规则名），每组记 query+命中数+是否截断（落 `evidence_acquisition.<源>.queries`，见 §六）。停止条件：(a) ≥1 已证实 finding 闭合核心问题，**或** (b) ≥2 角度空 **且**已试过下条回退链。**首轮有窄命中不得直接停手**——窄命中可能是错目标，仍须第二角度交叉验证。
- **首轮空回退链（强制）**：代码 `query` 首轮全空时**不得记缺口了事**，依次试 ①字典同义词/拼音缩写 ②`cypher` 结构查（按表/类/关系） ③`impact` 反向找调用方 ④`traverse_graph` ⑤换 repo（按菜单/wiki 候选）。仍空才记"代码图谱覆盖缺口"。db 侧已有"中文无命中不能停止"（见下），代码侧同等强制。
- **证据链优先**：按 `query → context → route_map（有精确 API 路径时）→ context` 追踪入口与调用/导入关系。`processes` 或 `route_map` 为空不代表业务不存在；继续对命中的 Controller、Service、Mapper 使用 `context` 验证调用边。
- **受控源码只作精确锚点后核验**：当质控阻断或分析结论依赖条件分支、字段赋值、配置常量、SQL、模板绑定或方法体细节时，置 `kb.source_required=true`。先取得当前产品“精确 repo + API/路径/符号/技术字面量”锚点；锚点优先来自 GitNexus，也可来自已解析附件、工作项明确技术信息、菜单索引或 wiki。已知类/方法/符号优先 `search_symbol`；`search_source` 只使用允许路径、精确字面量或技术锚点。每轮最多核验入口、编排、落库等 3–5 个关键文件。调用和发现写入 `kb.tools_used/findings`，源码 finding 使用 `source_tool=search_source|search_symbol, source_type=code`。
- **源码负面推断禁令**：无命中、截断、白名单外或路径拒绝只代表本次覆盖不足，不等于仓库/产品不存在实现。源码结果只证明被检索仓库路径在该时点的内容，不证明部署版本、现场行为或已上线。GitNexus 与源码结果冲突时记录索引时间/路径/符号差异并形成缺口，不静默选择一方。
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
- **源码怎么用**：仅当初判阻断可能由当前实现细节直接消除时，在当前产品精确 repo + 技术锚点后条件调用；锚点优先来自 GitNexus，也可来自已解析附件、工作项明确技术信息、菜单索引或 wiki。源码 finding 可通过既有 `kb:` 引用闭合“仓库代码事实”，但不能单独闭合部署、现场页面或当前运行行为。
- **结果去向**：发现写入 `checklist.items`（命名实体的选择式问题）/ `checklist.kb_note`（三态摘要）/ `kb.findings`（机读事实 + `conclusion/evidence/boundary` 人读说明）。
- **wiki 同源**：产品 wiki 的业务语义发现同样写入 `checklist.items`/`kb_note`/`wiki.findings`，经现有维度（#3 可识别性 / #5 重复）参与终局；**spec 清晰度闸**仅处理影响业务规则、范围或验收正确性的冲突 → `NEED-REVIEW`。单纯实现位置描述过时，以已核验代码为准并记知识治理缺口。见 `wiki-kb-recipe.md` §4。
- **与终局的关系（精化）**：**不直接判终局**——不加 verdict 类别、**不在 `qc-rules.md` 新增 KB 专属判定步**；但 KB 发现（含疑似重复/已实现）可写成清单条目，经现有维度（`qc-checklist.md` #3 可识别性 / #5 重复）参与终局，存疑即 NEED-REVIEW。**图谱无命中也是有效发现**：如实记"已做判定性查询（工具+关键词），图谱无该侧数据"，比"未做判定性查询"是审计改进。

### 阶段二分析 —— 内部佐证·PM 视角产物

- **需求历史是业务追溯佐证**：经 `get_work_item` 或明确关系确认的 finding 可用 `req:<索引>` 引用到 v2 `evidence_refs`，用于历史背景、问题目标、范围或验收来源。它不进入 PM 正文的技术定位，不替代 `kb:`，也不能满足 `kb.ready`、`kb.dedup_ran` 或 AUTO 的现有实现锚点。
- **代码图谱发现 + 受控源码核验均为内部佐证，不进变更方案正文**。变更方案/分析者描述是 **PM/业务视角**（业务场景/规则/验收/范围；见 `change-plan-template.md`）；`concise-v3` 单列的“菜单路径”只写业务菜单入口，操作步骤写入对应方案行。GitNexus 定位主模块/调用链，源码 MCP 只在 `source_required=true` 时核验精确实现；结果统一落 `kb.findings`，用于印证 AUTO/MANUAL、查重与高风险定性，不把仓库、类、方法、接口、Mapper 或表字段写入正文。
- 相似已实现需求命中：由代码图谱支撑 `confidence-heuristic.md` 第 4 项。
- **wiki 作业务内部佐证**：wiki 的业务规则/流程/数据口径同样落 `wiki.findings`，只作内部佐证、**不进变更方案正文**；其子系统名/仓库名仅供 GitNexus 锚定，可写进 testing-log 的"知识图谱作用"区。见 `wiki-kb-recipe.md` §4。
- KB 未就绪 → 走启发式（第 4 项跳过），定位写"待 dev-design 确认"落 `kb.findings`（非产物待确认）。**AUTO-ANA 路径**仍要求 KB 就绪 + 查重执行 +（优化类）已证实锚点（见 §三），缺失则降 `MANUAL-REVIEW`；MANUAL 路径不因此升级。

---

## 六、审计字段（产品路由、需求历史、图谱、源码与 wiki）

新的非 SKIP 计划先记录产品路由快照；不保存默认路由，也不依赖未来配置重新解释历史计划：

```json
"knowledge_route": {
  "status": "RESOLVED",
  "area": "NETHIS5.5",
  "product_id": "cloudhis-v56",
  "product_name": "云HIS 5.6",
  "profile_version": 1,
  "servers": {
    "requirements_history": "tfs-requirements",
    "code_graph": "gitnexus-team",
    "source_code": "cloudhis-source",
    "database": "db-knowledge"
  }
}
```

未解析状态使用 `AREA_UNMAPPED|AREA_AMBIGUOUS|PROFILE_MISSING|PROFILE_INVALID`，固定 `servers={}`；不得携带产品 MCP 调用或 finding。

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
    {"work_item_id": 260001, "fact": "历史需求明确了保存后的验收条件", "state": "已证实", "source_tool": "get_work_item", "maturity": "已落地", "conclusion": "历史需求已明确保存后的验收条件", "evidence": "TFS 260001 完整正文与验收标准", "boundary": "只证明历史需求记录，不代表当前版本仍已部署"},
    {"work_item_id": 259999, "fact": "标题与当前对象相似，正文尚未核验", "state": "候选", "source_tool": "search_requirements", "maturity": "设想", "conclusion": "存在一条标题相似的历史需求候选", "evidence": "TFS 搜索结果 259999", "boundary": "正文尚未核验，不能作为已实现或已落地依据"}
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
  "source_ready": true,
  "source_required": true,
  "database_ready": true,
  "dedup_ran": true,
  "tools_used": ["list_repos", "query", "context", "search_symbol", "search_knowledge", "get_table_knowledge"],
  "findings": [
    {"entity": "DischargedSettlementController.saveSettlement 调用链", "state": "已证实", "source_tool": "context", "source_type": "code", "conclusion": "已定位出院结算保存的现有调用入口", "evidence": "DischargedSettlementController.saveSettlement 调用链", "boundary": "只证明代码图谱关系，不代表现场已部署"},
    {"entity": "repo:src/SettlementService.java#checkSettlementBusiness", "state": "已证实", "source_tool": "search_symbol", "source_type": "code", "conclusion": "源码中存在结算业务校验入口", "evidence": "repo:src/SettlementService.java#checkSettlementBusiness", "boundary": "只证明受控仓库当前源码，不代表现场部署版本"},
    {"entity": "费用明细/结算源表", "state": "未确认", "source_tool": "search_knowledge", "source_type": "database", "conclusion": "费用明细可能来源于结算相关表", "evidence": "数据库知识图谱候选：费用明细/结算源表", "boundary": "关系尚未确认，不能据此确定落库表或判断其它表不存在"}
  ],
  "note": "当前产品代码图谱和源码 MCP 就绪；源码已核验精确符号，数据库侧仍有覆盖缺口"
}
```

代码图谱、源码 MCP、数据库图谱状态分别写 `ready`、`source_ready`、`database_ready`；`source_required` 明确本轮是否需要源码级核验。`source_required=true ∧ source_ready=true` 时必须实际调用源码工具并留下 finding；AUTO 还要求至少一条源码 finding `state=已证实`，否则降 MANUAL。新计划的每条 finding 必须用 `source_type=code|database` 明确归属，并补齐 `conclusion/evidence/boundary`；需求历史工具只能写入 `tfs_requirements.findings`。历史计划缺少新增字段时兼容读取。Redis `knowledge` 只投影 `summary/source_status/evidence_list` 人读佐证；`source_status` 固定保留五类来源的精简状态，完整 ready、route、tools、原始 finding 与覆盖状态从计划/审计读取。其余 finding、正文与 AUTO 锚点边界沿用原规则。

**产品 wiki 的审计用并列的可选 `wiki` 对象**（语义与图谱不同，故独立字段；同样不在 `validate` 的 required 内、由 `pipeline.py` 透传）：

```json
"wiki": {
  "ready": true,
  "modules_matched": ["药房药库"],
  "findings": [
    {"entity": "门诊发药和退药归药房管理，药库出库归药库库存管理", "state": "wiki-确认", "source": "wiki/门诊/门诊药品流转.md", "conclusion": "门诊发药和退药由药房管理，药库出库归药库库存管理", "evidence": "wiki/门诊/门诊药品流转.md 对应业务规则", "boundary": "Wiki 说明业务口径，不替代当前实现和部署核验"}
  ],
  "note": "命中门诊药品流转；关键事实已复核 raw/平台/药房药库.md，子系统名 batmed-yfyk-base-frame 已作 GitNexus query 锚点"
}
```

`state` 取 `wiki-确认` / `wiki-冲突` / `wiki-未覆盖`；`source` 为 wiki 页面相对路径。wiki 未就绪/未覆盖时 `"ready": false` 并在 `note` 写原因。字段定义详见 `wiki-kb-recipe.md` §6。

**v2 采集状态契约（`evidence_acquisition`，选用 `analysis=evidence-loop-v2` 时必填）**：上述 `kb`/`wiki`/`tfs_requirements` 记"发现了什么"，`evidence_acquisition` 额外记"**怎么查的、查全没有**"——按四源 `tfs_requirements`/`wiki`/`gitnexus`/`db_knowledge` 各填 `{availability, coverage_status, query_status, queries:[{terms, truncated, returned, total, verified_ids}], stop_reason}`，把每组查询词、命中数、是否截断、停止原因结构化留下，使"图谱无数据"与"没查/没查全"可区分。只有 `coverage_status=COMPLETE ∧ stop_reason=exhausted ∧ 未截断` 的 `query_status=NO_HIT` 才支撑负面结论；截断/未覆盖/未查只能 `PARTIAL/UNKNOWN`。校验器硬约束与字段结构详见 [`tfs/EXECUTION_CONTRACT.md`](tfs/EXECUTION_CONTRACT.md)「evidence-loop-v2」节。

---

## 七、后端模块定位方法论

精准定位并按需核验“需求对应的后端模块/符号”的完整方法见 [`module-location-recipe.md`](module-location-recipe.md)（与本文同目录，两阶段共读）：

- **口诀**：先搜业务语义，后看流程分组，再沿调用链验证；文件名只作线索，不作结论。
- **质控**跑其**佐证子集**；只有现有实现细节可能直接闭合阻断时才在精确锚点后查源码。**分析**跑完整方法，并在 `source_required=true` 时用源码 MCP 核验最多 3–5 个关键文件——结果落 `kb.findings`、不进 PM 视角变更方案正文。

本文件规定"**怎么接 KB**"（探活/三态/降级/审计），`module-location-recipe.md` 规定"**怎么定位模块**"——两者互补。

## 八、产品 wiki 接入方法

业务语义层知识源（产品 wiki）的接入——何时读、如何从 `wiki/index.md` 定位主题文章并按需沿 `Raw` 复核、两阶段怎么消费、与 GitNexus 的读序和兜底——见 [`wiki-kb-recipe.md`](wiki-kb-recipe.md)（与本文同目录，两阶段共读）。本文件的降级/审计原则同样适用。

**读序**：`实时 fetch → 产品路由 → 当前附件 → 当前产品需求历史（按需）→ menu-index → wiki（按需业务语义）→ 当前产品 GitNexus → 当前产品源码 MCP（按需精确核验）→ 当前产品数据库（按需）`；实时 fetch 管当前项，GitNexus 管发现、关系和查重，源码 MCP 管已有精确锚点的实现核验，历史需求与 wiki 只在各自证据域补充。

---

> **边界**：本 skill 只**读**已路由产品的需求历史、图谱、源码和 wiki，不写、不回流。任何路由失败都不得借用其它产品；知识源维护不在 skill 写入范围。
