# 知识库接入规范（GitNexus · 代码图谱 + 数据库图谱）

> **共享规范**：`auto-req-analysis` 的**阶段一质控**（有目的发现链）与**阶段二分析**（核心依赖）共用本文件作为知识库接入的单一权威来源。研发维护图谱与 MCP 服务；改这里，两阶段自动生效。
>
> 知识库以 **MCP 服务**提供，由 **skill 执行者（LLM agent）按需调用其原生 MCP 工具**——不写 Python 客户端、不引入新依赖（`_lib` 保持纯 urllib）。
>
> 此外还有第三个知识源**产品 wiki**（业务语义层，非 MCP、本地 markdown，查询方法见仓库根 `.wiki-schema.md` §Query、skill 侧接入见 [`wiki-kb-recipe.md`](wiki-kb-recipe.md)）。本文件的探活/三态/降级/审计原则同样适用于它，读序见 §四、消费见 §五、审计字段见 §六。

---

## 一、两个 MCP 服务

| 图谱 | MCP URL | 默认 project | 工具 | 用途 |
| ---- | ------- | ------------ | ---- | ---- |
| **代码图谱** | `http://172.16.0.192:4747/api/mcp` | `cloudhis-unified` | `list_repos` / `query` / `context` / `route_map` / `impact` / `cypher` | 路由、调用链、影响分析、跨项目代码、模块/类/方法定位 |
| **数据库图谱** | `http://172.16.0.192:4748/mcp` | `db-knowledge` | `search_knowledge` / `get_table_knowledge` / `traverse_graph` | 表、字段、读写关系、文档说明 |

> 源说明见仓库根 `云HIS GitNexus 知识图谱.md`。对应整体方案 §五 知识库 #4（代码）/ #5（数据库），P0，研发维护。

---

## 二、接入前提（环境配置，非 skill 代码）

在 **Codex** 执行 skill 时，先注册两个全局 HTTP MCP，重启 Codex 后用 `/mcp` 检查。MCP 工具在会话启动时注入；当前会话不会热加载新配置：

```bash
codex mcp add gitnexus-team --url http://172.16.0.192:4747/api/mcp
codex mcp add db-knowledge --url http://172.16.0.192:4748/mcp
codex mcp list   # 两者均应显示 enabled
```

在 **Claude Code** 执行 skill 时，改用对应的 user-scope 配置：

```bash
claude mcp add --transport http gitnexus-team --scope user http://172.16.0.192:4747/api/mcp
claude mcp add --transport http db-knowledge --scope user http://172.16.0.192:4748/mcp
claude mcp list
```

无论使用哪个客户端，都必须在新会话的工具列表里看见 `gitnexus-team` / `db-knowledge` 后，才可将图谱标为就绪。

---

## 三、就绪检测 + 降级（关键安全网）

skill 在用到 KB 前**先探活并固定代码仓库范围**：

1. **工具是否存在**：执行者工具列表里若无 `gitnexus-team` 相关工具 → 代码图谱未就绪；`db-knowledge` 缺失只标数据库图谱未就绪。
2. **代码探活**：调用 `list_repos`，先选定 `repo`。已知项目优先单仓库；只有跨项目才用 `cloudhis-unified`。记录 `indexedAt`、节点数、流程数与 `embeddings` 状态；索引稍旧但可查询时可继续，并标注其局限。
3. **数据库探活**：仅在本需求确实需要数据库联查时，调用一次轻量 `search_knowledge`。数据库不是普通模块定位或需求分析的前置条件。

**降级原则（必须遵守）**：

- 代码图谱**不可用绝不阻断 skill、绝不转 ERROR**。未就绪 → 记 `kb_ready=false`，按各自默认路径继续：
  - **质控**：跳过 KB 发现链，按 `config/qc-rules.md` 默认判定。
  - **分析**：跳过图谱定位与相似实现命中，走 `references/confidence-heuristic.md` 启发式（第 4 项"相似需求命中"在 KB 未就绪时跳过，不因此判 MANUAL）。
- 数据库图谱未就绪或无命中时，保留已证实的代码级结论，记入 `kb.note`；除非需求本身以数据库事实为必要前提，否则不得因此转 ERROR、判 MANUAL 或否定模块。
- **产品 wiki**（业务语义层，见 `wiki-kb-recipe.md`）缺失或未覆盖该模块时，记 `wiki.ready=false` 或 `wiki.note=未覆盖`，静默回退 GitNexus+菜单索引，**绝不阻断、绝不转 ERROR、绝不因此判 MANUAL**。GitNexus 不可用时 wiki 仍可供业务判断，但不替代代码定位与查重。
- 探活失败要**显式记进审计**（`kb_ready=false` / `wiki.ready=false` 或具体原因），不能静默。
- **「不升级」原则仅适用 MANUAL 路径**：上述"绝不因此判 MANUAL"只对 `MANUAL-REVIEW` 生效。`AUTO-ANA` 是无人工兜底的自动放行路径，硬要求 `kb.ready=true` 且 `kb.dedup_ran=true`（相似实现查重实际执行）；优化类类别还须 `kb.findings` 至少一条 `state=已证实` 锚定被改现有模块/入口。缺失则降 `MANUAL-REVIEW`（不加 `STOP-AUTO`，属信息缺口而非高风险）。由 `pipeline.py` 强制校验。

---

## 四、调用规范

- **菜单索引先按区域收敛（辅助候选）**：若 `skills/reg-auto-req-analysis/_lib/menu-business-index.json` 存在，先取工作项 `area`（优先 `System.AreaPath` 首级；缺失时才用 `teamProject`）精确匹配索引的 `tfs_area_values`——用 `build_menu_business_index.py lookup --area <area>`（见 `_lib/COMMANDS.md`）一步查候选产品，**不要手写 jq/grep 读索引 JSON**。唯一命中时，菜单的 `repo`、`module_url`、`apis` 仅作为 GitNexus 的候选锚点；`matched` 可带技术候选，`route-not-found` / `no-module` 只带业务词和菜单路径。区域未配置、索引不存在或范围不唯一时，记录“菜单索引范围未确认”，继续原有 GitNexus 检索，绝不猜测产品。
- **产品 wiki 取业务语义（GitNexus 之前）**：区域收敛后，按 `.wiki-schema.md` §Query 查 wiki（读 `index.md` → grep `wiki/` 用别名词表扩词 → 读命中页），取模块的业务规则/流程/数据口径/验收 + **子系统名/仓库名**。子系统名/仓库名仅作 GitNexus 的**候选锚点**，不替代 `query → context → route_map/context` 证据链；**wiki 叙述与代码事实冲突时以 GitNexus 为准（代码库最准、wiki 仅补充）**；wiki 未覆盖该模块记 `wiki-未覆盖` 后继续，不阻断（详见 `wiki-kb-recipe.md`）。
- **先定 repo，再检索**：不得省略仓库范围直接全局搜索，也不得将第一次命中的文件当结论。
- **两轮收敛**：先分别查询业务对象、业务动作、技术锚点；从前端页面/API 或候选符号提取 API 路径、Controller、DTO、表名等精确锚点后，再做第二轮查询。`embeddings: 0` 时优先精确术语、路径和符号名，不能依赖纯中文语义召回。
- **证据链优先**：按 `query → context → route_map（有精确 API 路径时）→ context` 追踪入口与调用/导入关系。`processes` 或 `route_map` 为空不代表业务不存在；继续对命中的 Controller、Service、Mapper 使用 `context` 验证调用边。
- **按需联查数据库**：只有新增/修改字段、数据迁移、统计口径、数据权限、性能优化或数据库脚本等数据需求，才从已证实的表名、字段名、Mapper、方法名或 SQL 片段开始 `search_knowledge`（限定 `Table` / `Column` / `CodeSymbol` / `SQLStatement`，较小 `limit`）并仅对确认候选调用 `get_table_knowledge`。没有已建立的代码 SQL/契约关系时，不能把代码和同名/近名表写成同一流程。
- **字段反向定位闭环**：字段类需求按“业务词及同义词 → 页面绑定字段/提交参数 → DTO/Mapper/SQL → 正式表列 → 目标查询是否已返回”逐层取证。中文业务词无命中时，必须扩展产品同义词、代码注释用语、缩写和命名转换；只有页面或载荷语义与数据库 `Column`、在用 `READS`/`WRITES` 或 SQL 同时闭环，才把字段记为已证实。缺任一层时只能记候选或未确认。
- **正式表筛选**：同名表、日期后缀表、备份表和测试表仅作候选；必须用在用仓库的代码读写关系、SQL 来源和业务键确认正式表。不得因列名相同，把 `_bak`、`_test`、日期后缀或其他近似表写成当前业务表。
- **覆盖缺口停止条件**：精确代码锚点无命中后，再以业务缩写、同义词或数据库命名转换查询 `Table`/`Column`，并按返回的 `database_alias` 查表；仍无结果才记录“数据库图谱覆盖缺口”，把真实表/字段待 Mapper SQL 映射列为未确认。定位到字段后仍须反查目标查询；字段存在但目标 SQL 未读取时，应记录“目标数据链缺字段”，不能误判为需求已实现。
- **证据三态标注（强制）**：每条来自 KB 的结论必须标注来源——
  - **图谱事实**：有明确节点/关系支撑（引用实体名）。
  - **推断**：基于图谱事实 + 推理得出。
  - **未确认**：图谱无依据 → **写"未确认"，不猜测、不编造**。

菜单索引由 `python3 skills/reg-auto-req-analysis/_lib/build_menu_business_index.py` 根据 `menu-sources.json` 构建。每个菜单源必须显式声明 `product_id`、`product_name`、`tfs_area_values` 和原始文件路径；一个区域只能归属一个产品。它是可迁移的产品入口索引，不是 GitNexus 图谱事实，也不允许替代 `query → context → route_map/context` 的证据链。

---

## 五、各 skill 怎么用本规范

### 阶段一质控 —— 有目的发现链·不直接判终局

- **触发条件（判定面）**：需求命中高风险早筛（`high-risk-categories.md` ①②⑤⑥），**或**描述含可映射到 schema/code 的业务概念（结算/费用/医保/上传/校验/报表/接口/明细/账单 …）时，跑发现链。**不再要求"已给具体表/字段名"**——旧触发条件过严，是 0 命中的根因（用户侧业务词≠schema 标识符）。纯文字、无业务实体且非高风险的需求不查（主动跳过，非降级）。
- **做什么（发现链，见 `../references/qc-kb-recipe.md`）**：先用代码图谱按三组词和精确技术锚点定位可追踪入口/关系；涉及数据变更时，才用已证实代码锚点锁定表、列和 SQL 读写关系。`route_map` 或流程缺失时回退 `context`，不得按名称推定后端或数据表。
- **结果去向**：发现写入 `checklist.items`（命名实体的选择式问题）/ `checklist.kb_note`（三态摘要）/ `kb.findings`（机读）。
- **wiki 同源**：产品 wiki 的业务语义发现同样写入 `checklist.items`/`kb_note`/`wiki.findings`，经现有维度（#3 可识别性 / #5 重复）参与终局；**spec 清晰度闸**——wiki 表明的既有规则与需求假设冲突 → `NEED-REVIEW`。见 `wiki-kb-recipe.md` §4。
- **与终局的关系（精化）**：**不直接判终局**——不加 verdict 类别、**不在 `qc-rules.md` 新增 KB 专属判定步**；但 KB 发现（含疑似重复/已实现）可写成清单条目，经现有维度（`qc-checklist.md` #3 可识别性 / #5 重复）参与终局，存疑即 NEED-REVIEW。**图谱无命中也是有效发现**：如实记"已做判定性查询（工具+关键词），图谱无该侧数据"，比"未做判定性查询"是审计改进。

### 阶段二分析 —— 内部佐证·PM 视角产物

- **代码图谱定位是内部佐证，不进变更方案正文**。变更方案/分析者描述是 **PM/业务视角**（业务场景/规则/验收/范围；见 `change-plan-template.md`）；其中 `路径` 只写业务菜单路径和操作路径。代码图谱按 `module-location-recipe.md` 完整方法**内部定位**主模块/调用链/关键文件，结果落 `kb.findings`（已证实/候选/未确认 + 三态），用于：印证 AUTO/MANUAL 的业务改动类型判断、查重（`confidence-heuristic.md` 第 4 项）、高风险定性佐证（③④⑥），不把仓库、类、方法、接口、Mapper 或表字段写入正文。
- 相似已实现需求命中：由代码图谱支撑 `confidence-heuristic.md` 第 4 项。
- **wiki 作业务内部佐证**：wiki 的业务规则/流程/数据口径同样落 `wiki.findings`，只作内部佐证、**不进变更方案正文**；其子系统名/仓库名仅供 GitNexus 锚定，可写进 testing-log 的"知识图谱作用"区。见 `wiki-kb-recipe.md` §4。
- KB 未就绪 → 走启发式（第 4 项跳过），定位写"待 dev-design 确认"落 `kb.findings`（非产物待确认）。**AUTO-ANA 路径**仍要求 KB 就绪 + 查重执行 +（优化类）已证实锚点（见 §三），缺失则降 `MANUAL-REVIEW`；MANUAL 路径不因此升级。

---

## 六、审计字段（写入计划 JSON 的可选 `kb` 对象）

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
    {"entity": "药房=发药点 / 药库=中心库 对称结构", "state": "wiki-确认", "source": "wiki/sources/2026-07-27-药房药库.md"}
  ],
  "note": "命中药房药库模块；子系统名 batmed-yfyk-base-frame 已作 GitNexus query 锚点"
}
```

`state` 取 `wiki-确认` / `wiki-冲突` / `wiki-未覆盖`；`source` 为 wiki 页面相对路径。wiki 未就绪/未覆盖时 `"ready": false` 并在 `note` 写原因。字段定义详见 `wiki-kb-recipe.md` §6。

---

## 七、后端模块定位方法论

精准定位"需求对应的后端模块/符号"的完整方法见 [`module-location-recipe.md`](module-location-recipe.md)（与本文同目录，两 skill 共读）：

- **口诀**：先搜业务语义，后看流程分组，再沿调用链验证；文件名只作线索，不作结论。
- **质控**跑其**佐证子集**（探活 + 三组词查询 + 可追踪入口/明确关系，不做完整调用链/源码验证）；**分析**跑**完整方法**（+ 从候选建立证据链 + 关键文件验证 + 结果 schema）**作内部佐证**——结果落 `kb.findings`、不进 PM 视角变更方案正文。

本文件规定"**怎么接 KB**"（探活/三态/降级/审计），`module-location-recipe.md` 规定"**怎么定位模块**"——两者互补。

## 八、产品 wiki 接入方法

业务语义层知识源（产品 wiki）的接入——何时读、读什么、两阶段怎么消费、与 GitNexus 的读序和兜底——见 [`wiki-kb-recipe.md`](wiki-kb-recipe.md)（与本文同目录，两 skill 共读）。wiki 查询方法以仓库根 `.wiki-schema.md` §Query 为权威；本文件的降级/审计原则同样适用。

**读序**：`menu-index（结构收敛）→ wiki（业务语义）→ GitNexus（代码事实）→ db-knowledge（按需）`；**GitNexus 主干 + 兜底，wiki 静默降级、绝不阻断**。

---

> **边界**：本 skill 只**读** KB，不写、不回流。整体方案第 9 条的"更新知识库"针对 P1 相似需求库且需人工确认触发；GitNexus 图谱由研发维护，不在 skill 写入范围。
