# 自动需求执行计划契约

`pipeline.py` 将模型生成的判定与 TFS 写入分开：skill 只生成方案、清单和计划；执行器负责预检、并发保护、幂等写入与审计。

## 计划文件

生成 `执行计划_<工作项ID>_<run_id>.json`（质控的"待补充信息"作为顶层 `checklist` 字段内嵌于此文件，不再单列 `.md`）。`run_id` 为 8-80 位字母、数字、`-` 或 `_`；所有附件文件名和正文均必须包含同一运行标记：

```html
<!-- auto-req-run:<run_id> -->
```

> **附件两种形态**：① **inline**（质控 `qc-followup`）—— artifact 只能带 `filename:"待补充信息_<id>_<run_id>.json"`、无 `path`；正文由执行器从顶层 `checklist` 物化，运行标记由执行器注入；② **磁盘文件**（分析 `change-plan`；历史计划还可能有 `manual-followup`）—— `path` 只能是计划同目录的精确文件名 `变更方案_<id>_<run_id>.md` / `待确认清单_<id>_<run_id>.md`，禁止绝对路径、子目录和 `..`，标记写在文件正文里，`validate` 实查文件存在与标记。新运行启用 `confirmation_policy=qc-single-batch-v1` 后不再生成 `manual-followup`。

最小结构：

```json
{
  "version": 1,
  "run_id": "20260720_abc12345",
  "skill": "auto-req-analysis",
  "work_item_id": 228549,
  "expected_rev": 17,
  "expected_state": "活动",
  "verdict": "NEED-INFO",
  "confirmation_policy": "qc-single-batch-v1",
  "tags": ["PM-AI-QC-NEED-INFO"],
  "state_to": null,
  "rules_source": {"qc": "pre-qc-v1"},
  "checklist": {
    "work_item": "228549 <标题>", "verdict": "NEED-INFO", "tag": "PM-AI-QC-NEED-INFO",
    "responsible": "实施/创建者", "generated_at_utc": "2026-07-20",
    "items": [
      {"id": "q-scope", "question": "<要补充的问题>", "options": ["<推荐选项1>", "<推荐选项2>"], "allow_other": true}
    ],
    "passed": [],
    "next": "责任方补充后重跑 auto-req-analysis 228549，通过后才进入分析"
  },
  "artifacts": [
    {"kind": "qc-followup", "filename": "待补充信息_228549_20260720_abc12345.json"}
  ]
}
```

> **新非 SKIP 必填字段 `knowledge_route`**：路由快照结构为 `{status, area, product_id?, product_name?, profile_version?, servers}`，`status` 只允许 `RESOLVED|AREA_UNMAPPED|AREA_AMBIGUOUS|PROFILE_MISSING|PROFILE_INVALID`。`RESOLVED` 时产品字段必须完整，`servers` 必须精确包含 `requirements_history|code_graph|source_code|database`；未解析时固定 `servers={}`，不得携带任何产品 MCP 调用或 finding。`SKIP-ANALYSIS` 不解析产品且不得携带该字段。校验器只校验计划内快照契约，不用当前路由文件重新解释历史计划。
>
> **新非 SKIP 必填字段 `kb`**：`{ready: bool, source_ready: bool, source_required: bool, database_ready: bool, tools_used: [...], findings: [...], note?: "..."}`。`ready` 仅表示当前产品代码图谱，`source_ready` 表示当前产品源码 MCP 工具已注入，`source_required` 表示本轮结论依赖源码核验。每条 finding 必须含 `conclusion/evidence/boundary`，分别回答“证明了什么/依据在哪里/不能证明什么”。源码 finding 必须使用 `source_tool=search_source|search_symbol`、`source_type=code`，对应工具必须出现在 `tools_used`；需求历史工具只能进入 `tfs_requirements.findings`。`source_required=true ∧ source_ready=true` 必须实际调用源码工具并留下 finding；AUTO 还要求至少一条 `state=已证实`。历史计划缺少新增字段时继续按旧契约读取，Redis 按工具名和原定位字段保守兼容。质控 KB 发现经现有核对维度参与，不直接产生新 verdict。
>
> **可选字段 `tfs_requirements`**：`{ready: bool, coverage: {...}, tools_used: [...], findings: [...], note: "..."}`，记录只读需求历史 MCP 的覆盖范围和发现。新策略的 `findings` 每项必须含 `{work_item_id, fact, state, source_tool, conclusion, evidence, boundary}`，`state` 取 `已证实|候选|未确认`；`findings[]` 可带可选 `maturity`（取 `设想|分析确认|已落地`，定义见 `_lib/knowledge-base.md` §六）。只有 `get_work_item` 或 `get_related_work_items` 返回且标为 `已证实` 的 finding 可被 `req:<索引>` 引用；其中**落地级引用**（声称"已落地"、用于查重/相似实现命中/方案成熟度判断）还须 `maturity=已落地`，`maturity=设想`（描述里的解决设想）不得用于支持"已实现/已存在方案"结论。`search_requirements` 结果和 summary 统计不能升级为可引用事实。该对象可省略；存在时由 `pipeline.py` 校验并透传到成功/失败审计。它不覆盖实时 `fetch`，不证明当前实现，不进入 `qc_evidence_resolution`，也不能满足 AUTO 的 `kb:` 门槛。
>
> **可选字段 `existing_feature`**（功能已存在声明）：`{satisfied: bool, requirement_ids?: [不重复正整数], note?: "..."}`。`satisfied=true` 表示查重命中 `state=已证实` 且 `maturity=已落地` 的历史方案、其能力覆盖本次诉求（**一套公版假设**：同一产品线视为一套公版能力，不按项目/客户/版本拆分查重）；此时终局**必须 `MANUAL-REVIEW`**（不得 `AUTO-ANA`，非高风险故不加 `STOP-AUTO`），`pipeline.py` 硬闸拒绝 `satisfied=true ∧ verdict=AUTO-ANA`。`requirement_ids` 为已实现该功能的需求号（不重复正整数数组）；`note` 为说明/边界。可省略（向后兼容）；存在时由 `pipeline.py` 校验结构并透传到审计。它不进 `qc_evidence_resolution`、不替代 AUTO 的 `kb:` 门槛、**不写入 Redis 摘要**（路由信号已由 verdict=MANUAL-REVIEW 承载）。

> **可选字段 `iteration`**（质控时效判定）：`{project, matched: {path, start, finish}|null, timing: "normal|caution|unavailable"}`，记录时效实查的迭代匹配结果（见 `../../references/pre-qc-rules.md §三.3`）。同样由 `pipeline.py` 透传、`validate` 不强制，向后兼容。

> **可选字段 `attachments`**（工作项原始附件证据）：`{ready: bool, download_dir: "附件", downloaded:[{name, path, sha256, size, content_type, content_encoding?}], parsed:[{name, status, output?, converter?, converter_chain?, runtime_mode?, tool_versions?, note?}], skipped:[{name, reason, blocking?}], errors:[{name, reason, blocking?}], preflight?:{requested_formats, capabilities, install_required, installations, runtime_dir, runtime_cache_key, runtime_mode, blocked_formats, warnings}}`。来源附件先用 `tfs_client.py download-attachments` 只读下载，再由官方 `attachment_runtime.py` 按实际格式准备隔离依赖并逐文件转换；存在 `preflight` 时，逐文件终态、转换链、运行模式和安装审计均受校验，旧计划可省略新增字段。Python 依赖统一锁定为 MarkItDown 0.1.7；旧版 `.doc` 由 LibreOffice 转 `.docx` 后进入正式链。具体边界见 `../attachment-evidence.md`；只有 `status=parsed` 的内容可作为判定辅助证据。该字段由 `pipeline.py` 原样透传到审计，不新增 TFS 写入或 artifact 类型。
>
> **质控必填字段 `checklist`**（质控 NEED-INFO/NEED-REVIEW 的待补充清单，即 inline `qc-followup` 的附件源）：`{work_item, verdict, tag, responsible, generated_at_utc, items:[…], next}`。顶层字段均为非空，且 `verdict`/`tag` 必须与计划一致；`items[]` 目标 1–3 个、最多 5 个决策主题，元素 = `{id, question, options:[…], allow_other, responsible?, category?, primary?, why?}`，其中前四项必填、`id` 唯一、`options` 非空。执行器物化上传时把 `checklist` 序列化为附件正文（`待补充信息_<id>_<run_id>.json`）并注入运行标记。PASS 不带 `checklist`/附件。

> **新运行必填字段 `confirmation_policy`**：固定为 `qc-single-batch-v1`，用于声明“一次集中确认”边界。该策略要求所有 PM 可回答、且会改变业务方向、入口/位置、范围边界、取值/聚合口径、异常处置或验收结果的问题都在 QC PASS 前合并进同一 `checklist`；回答后的重跑按稳定 `item.id` 继承已确认答案，除非当前 TFS 修订或新附件与原答案冲突，否则不得重复询问。分析阶段只能输出已证实结论或明确标注边界的合理假设，`analysis_gaps` 必须为 `[]`；技术定位、覆盖不足和实现证据缺口写入 `evidence_gaps`，可进入 MANUAL 人工技术复核但不再向用户生成确认清单。字段缺失仅作为历史计划兼容，未知值一律拒绝。

> **分析必填字段 `analysis_description`**：仅 `auto-req-analysis` 使用，结构为 `{categories: ["report", ...]}`。`categories` 必须为非空、无重复的受控类别数组，并与 `change-plan` 的“## 三、分析者描述”三级标题完全一致。新计划使用 `analysis_profile=concise-v3`：正文必须在类别标题前包含且只包含一个非空 `- **菜单路径**：...`；不得包含“需求类别”、旧版“路径（菜单路径+操作路径）”或“决策结论 / 生效路径与条件 / 决策边界 / 验收要点”，再按类别输出 1–3 行有效业务结论。菜单路径来自工作项、已解析附件或唯一菜单索引；无菜单入口时写“无菜单入口：<明确触发方式>”。类别标题只供附件结构和机器校验，写入 TFS 时不渲染；菜单路径和业务维度均安全渲染。操作步骤、位置、触发条件和边界直接写入对应方案行。执行器拒绝路径缺失/重复、缺失维度、模板占位符、`TODO` 和已列明空泛话术，不按总字数判定；执行时整段替换 `System.Description` 中 `【分析者描述】` 至 `【开发者描述】` 之间的内容。完整 `change-plan` 仍只作为 Markdown 附件上传，绝不写入 `Winning.Demand.Analysis`。类别清单与写作规则见 `../../references/analysis-description-writing-rules.md`。
> **可选字段 `analysis_profile`**：允许 `concise-v1`、`concise-v2` 或 `concise-v3`。新计划固定使用 `concise-v3`，支持全部受控类别和多类别组合；`concise-v1` / `concise-v2` 只兼容单一 `existing-ui-simple` / `print-adjustment` / `data-management` 历史计划，无 profile 时继续使用历史完整维度及公共摘要/路径格式。
>
> **分析必填字段 `analysis_gaps`**：仅 `auto-req-analysis` 使用；始终为数组。启用 `confirmation_policy=qc-single-batch-v1` 的新计划必须填 `[]`，不得生成 `manual-followup`；若分析时才发现 PM 可回答的业务歧义，必须回退为 QC `NEED-REVIEW`，把问题并入 inline `checklist` 后结束本轮。历史计划未带该策略时，仍兼容非空元素 `{id, topic, missing, impact, question, options, allow_other}` 及唯一 `manual-followup` 磁盘附件；附件逐项包含 `<!-- analysis-gap:<id> -->` 与五项非空字段，且不得出现 `PM-AI-` 自动标签。

> **v2 分析必填字段 `evidence_refs` / `evidence_gaps`**：`evidence_refs` 是 `{闭环章节: ["work-item"|"req:<索引>"|"kb:<索引>"|"wiki:<索引>", ...]}`，必须精确覆盖“现状基线、问题与目标、差异与范围、方案取舍、成功衡量与非目标”；`req:` 必须指向 `tfs_requirements.findings` 中由 `get_work_item|get_related_work_items` 核验的 `state=已证实` 历史工作项事实（落地级引用——声称已落地/查重命中/方案成熟度——还须 `maturity=已落地`，见 `_lib/knowledge-base.md` §六），KB 索引必须指向 `state=已证实` 的 finding，wiki 索引必须指向 `state=wiki-确认`（历史兼容 `已证实`）的 finding。`AUTO-ANA` 的现状基线、差异与范围、方案取舍还必须各引用至少一条 `kb:<索引>`，`req:` 不能替代。`evidence_gaps` 始终为数组，元素为 `{id, topic, missing, impact, owner, next_action}` + 可选 `{type, kind}`；`owner` 允许 `研发|知识库治理|产品`（背景采集缺口用 `产品`），可选 `kind ∈ {technical,background}`、`type` 取稳定缺口枚举（`WIKI_TOPIC_MISSING`/`WIKI_BROKEN_LINK`/`TFS_SNAPSHOT_LAG`/`TFS_SEARCH_TRUNCATED`/`GITNEXUS_REPO_MISSING`/`GITNEXUS_EMBEDDINGS_DISABLED`/`DB_SCHEMA_PARTIAL`/`DB_RELATION_PARTIAL`/`EXISTING_IMPL_LOCATION`/`SIMILAR_IMPL_DEDUP`）；记录现有实现、相似能力、调用路径、方案取舍或**背景采集缺口**（wiki 未覆盖/TFS 范围外/图谱覆盖不全）。非空 `evidence_gaps` 禁止 AUTO-ANA，但不生成 `manual-followup`、不写 TFS；二者均由执行器透传到运行审计。
>
> **界面 AUTO 必填字段 `ui_baseline`**：新策略 `AUTO-ANA` 且 `auto_scopes` 含 `field-ui-copy` 时必须填写 `{sources:[{type, ref, note?}, ...]}`。`type` 允许 `wiki|runtime-observation|attachment|code-graph|source-code`，分别归入产品知识、运行观察、实现证据三类；至少覆盖两类，同类多条只算一类。`wiki` 的 `ref` 必须是已证实 `wiki:<索引>`，`code-graph|source-code` 的 `ref` 必须是已证实 `kb:<索引>`，其中 `source-code` 必须指向 `search_source|search_symbol` finding。`runtime-observation|attachment` 的 `ref` 写可追溯页面/URL/截图或已解析附件标识。wiki 是按需优先源，不是固定必需源；wiki 未覆盖但运行观察 + 实现证据闭合时仍可 AUTO。字段缺失或只有一类证据时改判 `MANUAL-REVIEW` 并记“界面现状交叉证据不足”。历史计划和非 `field-ui-copy` 范围可省略；存在时执行器校验并透传审计。
>
> **新缺口类型**：`PRODUCT_ROUTE_UNRESOLVED`、`SOURCE_MCP_UNAVAILABLE`、`SOURCE_SCOPE_PARTIAL`、`SOURCE_VERIFICATION_INCOMPLETE`。不扩展现有四源 `evidence_acquisition`；产品由 `knowledge_route` 标识，源码核验状态保留在 `kb`。
>
> **v2 可选规则源 `evidence-loop-v2` + 必填 `evidence_acquisition`**：`rules_source.analysis` 可取 `evidence-loop-v2`（与 `evidence-loop-v1` 并列，v1 计划不带 `evidence_acquisition` 仍通过、向后兼容）。选用 v2 时，分析计划**必须**新增顶层 `evidence_acquisition` 对象，按四源 `tfs_requirements`/`wiki`/`gitnexus`/`db_knowledge` 如实记录采集状态；每源结构 `{availability: READY|UNAVAILABLE, coverage_status: COMPLETE|PARTIAL|OUT_OF_SCOPE|UNKNOWN, query_status: HIT|NO_HIT|SKIPPED|ERROR, queries:[{terms, truncated?, returned?, total?, verified_ids?}], stop_reason: exhausted|verified_hit|hard_limit|source_gap|not_applicable}`，未触发的源用 `query_status=SKIPPED`、`queries:[]`。**四态区分**：已命中 / 完整查询后无命中 / 覆盖不完整 / 来源不可用——只有 `coverage_status=COMPLETE ∧ stop_reason=exhausted ∧ 未截断` 的 `query_status=NO_HIT` 才能支撑「无相似需求 / 无现有实现」负面结论。校验器硬约束：①`kb.dedup_ran=true` 时 `tfs_requirements.queries` 不得为空（未查询不得声明已查重）；②`AUTO-ANA` 须 `tfs_requirements.coverage_status ∈ {COMPLETE, OUT_OF_SCOPE}`（查重覆盖不全不得自动放行，改判 MANUAL-REVIEW 并记治理缺口）；③`coverage_status=COMPLETE` 须 `availability=READY ∧ stop_reason=exhausted ∧ 无 truncated=true`（截断/无分页/索引过期/目标仓未覆盖只能 PARTIAL/UNKNOWN）；④`query_status=NO_HIT` 须 `coverage_status=COMPLETE`；⑤`tfs_requirements.findings` 的 `maturity=已落地` 须 `state=已证实`（设想级证据不得支撑已落地）。该对象由执行器透传到审计，不新增 TFS 写入或 artifact。

> **v2 变更方案追踪表**：必须且只能有一个“## 四、范围—方案—验收追踪”区，表头固定为 `ID | 范围/改动点 | 方案/目标行为 | 验收场景与结果 | 结论状态 | 依据或缺口`。每行的范围、方案和验收均非空。启用 `qc-single-batch-v1` 时，结论状态只允许“已证实 / 合理假设”，合理假设的依据须以“呈现类默认：”说明适用边界；出现无法形成这两类结论的业务项时回退 QC。历史计划仍兼容“待业务确认”及其与 `analysis-gap:<id>`、`analysis_gaps` 的一对一关联。此表保留业务语言，不暴露技术定位。

> **可选字段 `qc_evidence_resolution`**：仅用于“初判 `NEED-REVIEW` 经 KB/wiki 补证后复核为 PASS、进入阶段二”的 v2 分析计划，结构为 `{initial_verdict:"NEED-REVIEW", post_evidence_verdict:"PASS", items:[{id, initial_gap, resolution, evidence_refs:["kb:<索引>"|"wiki:<索引>"]}]}`。`items` 非空、ID 唯一；每项引用必须指向 KB 的 `已证实` 或 wiki 的 `wiki-确认`（历史兼容 `已证实`）finding，不能使用 `work-item`、`req:`、候选或推断。历史需求只能说明过去工作项事实，不能单独证明当前规则并放行 QC。执行器校验后将该字段写入运行审计；它是放行理由，不生成附件、不改变阶段二终局规则。

> **计划版本与规则来源**：v2 为当前新计划版本。QC 终局为 `{"qc":"pre-qc-v1"}`；分析终局为 `{"qc":"pre-qc-v1","analysis":"evidence-loop-v1"}`（默认）或 `{"qc":"pre-qc-v1","analysis":"evidence-loop-v2"}`（新增；选用 v2 须带 `evidence_acquisition`，见上节）。v1 及其 `fallback-v1` 分阶段规则源、字符串 `pre-qc-v1` / `fallback` 仅为历史回放兼容；v2 不接受旧规则源，故无法跳过证据闭环。

> **可选字段 `qc_recheck`**：仅当阶段二重新发现范围、取值口径或既有路径等 spec 歧义时填写 `analysis-spec-ambiguity`。此时终局必须回退为质控 `NEED-REVIEW` 并使用 QC inline `checklist`；不得用 MANUAL 分析计划或 `analysis_gaps` 代替。执行器将该字段透传到审计。

> **可选字段 `assignee_to`**（AUTO-ANA 自动指派·回退源）：仅 `AUTO-ANA` 计划可填，值为 `System.AssignedTo` 的写入值，**必须可解析为 `WINNING\账号` 格式**（如 `WINNING\zhang_dong`，或含 `<WINNING\account>` / `(account)` 形态）；bare display name（如「舒予」「张栋」）TFS 必报「未知标识」HTTP400，`validate` 前置拦截。由 `references/assignee-rules.md` 按 产品×版本×模块 匹配唯一开发组长得出；匹配不出（范围外产品 / 混合版本且模块不清 / 命中多人）时**省略该字段**、在分析者描述与审计写候选，**不阻断 AUTO-ANA**。
>
> **运行时指派优先级**（见 [字段流转](field-flow.md)）：`--assignee` 显式覆盖 > 工作项 `Winning.Dev.Leader` 字段（首选，TFS 已指定的开发组长）> 计划 `assignee_to`（规则提示，回退）。`Winning.Dev.Leader` 读回格式 `account(中文名) <WINNING\account>`，执行器自动提取 `WINNING\account`。指派失败（HTTP≠200）**降级记 error 动作、不中断、不回滚状态**（指派与终局解耦）。

合并后 `auto-req-analysis` skill 的合法终局按 `verdict` 分派（执行器 `expected_for` 不再按 `skill` 名区分；`skill` 字段新 run 用 `auto-req-analysis`，旧 `auto-req-qc`/`auto-req-analysis` 仅向后兼容历史计划）：

- `NEED-INFO`：标签仅 `PM-AI-QC-NEED-INFO`，`state_to=null`，附件 inline `qc-followup`（顶层 `checklist` 物化）。质控挡回，不进分析。
- `NEED-REVIEW`：标签仅 `PM-AI-QC-NEED-REVIEW`，`state_to=null`，附件 inline `qc-followup`。这是 KB/wiki 补证与全量复核后的**最终**质控挡回；初判 NEED-REVIEW 若被逐项已证实证据消除，则不生成此计划而进入分析计划并写 `qc_evidence_resolution`。
- `SKIP-ANALYSIS`：分析准入排除；`tags=[]`、`state_to=null`、`artifacts=[]`，必须带非空 `skip_reason`，且不得带 checklist、分析描述或 gaps。`apply` 只做只读预检并记录审计/Redis，动作列表为空；即使显式 `--execute` 也不得新增、删除标签或产生其它 TFS 写入。
- `AUTO-ANA`：标签仅 `PM-AI-AUTO-ANA`，`state_to` 为 `已分析`，附件仅 `change-plan`，`analysis_gaps=[]`；必须额外声明非空 `auto_scopes`，且其值只能来自 `field-ui-copy`、`unit-test-completion`、`config-enum-adjustment`。这表示全部变更都落在字段级 UI/文案、单测补齐、配置/枚举调整这三类白名单内。质控 PASS 后分析完成的自动流转终局。执行器在「加标签」后跑**完整字段流转**（见 [字段流转](field-flow.md)：写迭代路径/开始日期/完成日期 → 转「活动」→ 转「已分析」→ 指派；指派优先 `Winning.Dev.Leader`、回退可选 `assignee_to`）。
- `MANUAL-REVIEW`：标签仅 `PM-AI-MANUAL-REVIEW`，`state_to=null`；始终有 `change-plan`。新运行 `analysis_gaps=[]` 且不带 `manual-followup`；人工仅复核风险、非 AUTO 范围或技术证据缺口。历史计划仍按非空 `analysis_gaps` 兼容 `manual-followup`。
- `MANUAL-REVIEW-STOP`：在前项基础上增加 `PM-AI-STOP-AUTO`，附件规则相同；新运行同样不再产生第二轮用户确认清单。

`PASS` **不再是终态**：合并后 `PASS` 是 `auto-req-analysis` 内部阶段闸（质控通过即继续分析，不产出独立计划、不 apply）。`pipeline.py` 仍保留 `verdict=PASS` 的契约（空标签/空附件/`state_to=null`）与 `next_action` 生成，仅用于校验/回放历史 `auto-req-qc` PASS 计划；新 run 不再产出 PASS 计划。

## 完整 JSON 骨架（按 verdict · 生成计划的单一权威 schema）

> 生成 `执行计划_<id>_<run_id>.json` 时**按本节对应 verdict 的骨架填写，无需翻历史样例**。所有 verdict 共有的基础必填顶层字段（`validate_plan`）：`version, run_id, skill, work_item_id, expected_rev, expected_state, verdict, rules_source, tags, state_to, artifacts`；除 `SKIP-ANALYSIS` 外的新运行还必须写 `confirmation_policy=qc-single-batch-v1`、`knowledge_route` 和含四个就绪/依赖布尔字段的 `kb`。历史计划缺少新字段时仍兼容读取。`<...>` 为占位，需替换为真实值。

### SKIP-ANALYSIS（纯联调/支持准入排除 · v1）

```json
{
  "version": 1,
  "run_id": "<8-80位 [A-Za-z0-9_-]>",
  "skill": "auto-req-analysis",
  "work_item_id": 260120,
  "expected_rev": 1,
  "expected_state": "已建议",
  "verdict": "SKIP-ANALYSIS",
  "rules_source": {"qc": "pre-qc-v1"},
  "tags": [],
  "state_to": null,
  "generated_at_utc": "<ISO UTC>",
  "skip_reason": "接口已开发完成，本工作项仅安排现场联调，无新增或调整业务分析面。",
  "artifacts": []
}
```

> 本终局不得带 `knowledge_route`、`kb`、`checklist`、`analysis_description`、`analysis_profile`、`analysis_gaps`、`evidence_refs`、`evidence_gaps`、`ui_baseline`、`auto_scopes` 或 `assignee_to`。

### NEED-INFO / NEED-REVIEW（质控挡回 · v1）

NEED-INFO 与 NEED-REVIEW 结构相同，仅 `tag`/`responsible` 不同：NEED-INFO → `PM-AI-QC-NEED-INFO` + 实施/创建者；NEED-REVIEW → `PM-AI-QC-NEED-REVIEW` + 按命中规则（产品/研发负责人/现场/排期方/非本组流程）。

```json
{
  "version": 1,
  "run_id": "<8-80位 [A-Za-z0-9_-]>",
  "skill": "auto-req-analysis",
  "work_item_id": 259535,
  "expected_rev": 2,
  "expected_state": "已建议",
  "verdict": "NEED-REVIEW",
  "confirmation_policy": "qc-single-batch-v1",
  "tags": ["PM-AI-QC-NEED-REVIEW"],
  "state_to": null,
  "rules_source": {"qc": "pre-qc-v1"},
  "generated_at_utc": "2026-07-29T02:34:45Z",
  "knowledge_route": {"status": "RESOLVED", "area": "NETHIS5.5", "product_id": "cloudhis-v56", "product_name": "云HIS 5.6", "profile_version": 1, "servers": {"requirements_history": "tfs-requirements", "code_graph": "gitnexus-team", "source_code": "cloudhis-source", "database": "db-knowledge"}},
  "iteration": {"team_project": "<teamProject>", "expected_date": "<期望日期>", "pimis_priority": "<C级(一般)>", "matched": {"path": "<迭代路径>", "name": "<V6.0.x>", "start": "<ISO>", "finish": "<ISO>"}, "code_commit_cutoff": "<finish-7天>", "today": "<今日>", "timing": "正常处理", "note": "<时效判定依据>"},
  "attachments": {"ready": true, "download_dir": "附件", "downloaded": [], "parsed": [], "skipped": [], "errors": [], "note": "<附件证据摘要>"},
  "kb": {"ready": true, "source_ready": true, "source_required": false, "database_ready": false, "tools_used": ["list_repos", "query"], "findings": [{"entity": "<命名实体>", "state": "候选", "source_tool": "query", "source_type": "code", "conclusion": "<证明了什么>", "evidence": "<依据位置>", "boundary": "<不能证明什么>"}], "note": "<三态摘要>"},
  "checklist": {
    "work_item": "<id> <标题>",
    "verdict": "NEED-REVIEW",
    "tag": "PM-AI-QC-NEED-REVIEW",
    "responsible": "<产品 + 研发负责人 等>",
    "generated_at_utc": "2026-07-29T02:34:45Z",
    "items": [
      {"id": "q-scope", "priority": "P0", "category": "<规则分类>", "responsible": "<该条责任方>", "primary": true, "question": "<责任方可直接回答的问题>", "why": "<为何需要>", "options": ["<推荐选项1>", "<推荐选项2>"], "allow_other": true}
    ],
    "notes": ["<非阻断提示>"],
    "passed": ["<已通过项>"],
    "kb_note": "<图谱事实|推断|未确认 摘要>",
    "next": "责任方补充后重新触发质控（重跑 auto-req-analysis <id>），通过后才进入分析"
  },
  "artifacts": [
    {"kind": "qc-followup", "filename": "待补充信息_<id>_<run_id>.json"}
  ]
}
```

> `checklist.items[]` 必填字段（`validate_checklist`）：`id`（稳定字母开头、唯一）、`question`（非空）、`options`（非空字符串数组）、`allow_other`（布尔）；可选：`priority`/`category`/`responsible`/`primary`/`why`。新策略的 `id` 必须按业务决策语义稳定命名（如 `q-scope`、`q-value-rule`），不得使用会随排序变化的 `q1/q2`。`checklist.verdict`/`tag` 必须与计划一致；`items` 不可为空。`qc-followup` 为 inline 附件——只带 `filename`、无 `path`，正文由执行器从 `checklist` 物化并注入运行标记。

### AUTO-ANA（质控 PASS → 分析自动流转 · v2）

```json
{
  "version": 2,
  "run_id": "<8-80位>",
  "skill": "auto-req-analysis",
  "work_item_id": 260049,
  "expected_rev": 2,
  "expected_state": "已建议",
  "verdict": "AUTO-ANA",
  "confirmation_policy": "qc-single-batch-v1",
  "state_to": "已分析",
  "tags": ["PM-AI-AUTO-ANA"],
  "rules_source": {"qc": "pre-qc-v1", "analysis": "evidence-loop-v1"},
  "knowledge_route": {"status": "RESOLVED", "area": "NETHIS5.5", "product_id": "cloudhis-v56", "product_name": "云HIS 5.6", "profile_version": 1, "servers": {"requirements_history": "tfs-requirements", "code_graph": "gitnexus-team", "source_code": "cloudhis-source", "database": "db-knowledge"}},
  "auto_scopes": ["field-ui-copy"],
  "assignee_to": "WINNING\\<账号>",
  "analysis_description": {"categories": ["existing-ui-simple"]},
  "analysis_profile": "concise-v3",
  "analysis_gaps": [],
  "evidence_gaps": [],
  "evidence_refs": {
    "现状基线": ["work-item", "kb:0"],
    "问题与目标": ["work-item"],
    "差异与范围": ["work-item", "kb:0"],
    "方案取舍": ["kb:0"],
    "成功衡量与非目标": ["work-item"]
  },
  "ui_baseline": {"sources": [{"type": "code-graph", "ref": "kb:0"}, {"type": "runtime-observation", "ref": "受控页面：<菜单/URL>", "note": "<采集时间、项目/版本与可见状态>"}]},
  "kb": {"ready": true, "source_ready": true, "source_required": false, "database_ready": false, "dedup_ran": true, "tools_used": ["list_repos", "query", "context"], "findings": [{"entity": "<已证实实体>", "state": "已证实", "source_tool": "query", "source_type": "code", "conclusion": "<证明了什么>", "evidence": "<依据位置>", "boundary": "<不能证明什么>"}], "note": "<查重与定位摘要>"},
  "wiki": {"ready": true, "modules_matched": ["<模块>"], "findings": [{"entity": "<业务语义>", "state": "wiki-确认", "source": "<wiki路径>", "conclusion": "<业务规则结论>", "evidence": "<wiki 路径与章节>", "boundary": "<Wiki 不能替代的实现核验>"}], "note": "<wiki 佐证摘要>"},
  "iteration": {"team_project": "<teamProject>", "expected_date": "<期望日期>", "matched_iteration": "<V6.0.x>", "timing": "正常处理", "pimis": "C级"},
  "attachments": [{"name": "<附件名>", "source": "<tfs|external>", "parsed": true, "parse_method": "<visual|...>", "note": "<证据摘要>"}],
  "artifacts": [
    {"kind": "change-plan", "path": "变更方案_<id>_<run_id>.md"}
  ],
  "generated_at": "2026-07-29T10:03:19Z",
  "audit_note": "<QC=PASS... → 分析 AUTO-ANA(...)>"
}
```

> AUTO-ANA 硬约束：`knowledge_route.status=RESOLVED`；`analysis_gaps=[]` 且 `evidence_gaps=[]`；`auto_scopes` 为允许子集；`evidence_refs` 的现状基线/差异与范围/方案取舍各至少一条 `kb:<已证实索引>`；`kb.ready=true` 且 `kb.dedup_ran=true`。`source_required=true` 时还必须 `source_ready=true`、实际调用源码工具并至少一条已证实源码 finding。`auto_scopes` 含 `field-ui-copy` 时还必须有覆盖至少两类独立证据的 `ui_baseline.sources`。不得声明 `existing_feature.satisfied=true`。

### MANUAL-REVIEW / MANUAL-REVIEW-STOP（分析·人工复核 · v2）

MANUAL-REVIEW-STOP = 在 MANUAL-REVIEW 基础上 `tags` 追加 `PM-AI-STOP-AUTO`（命中 6 类高风险）。新运行固定 `analysis_gaps=[]`，不带 `manual-followup`；业务决策未闭合时应回退 QC，不得生成本骨架。历史计划仍兼容非空 gaps 与 `manual-followup`。

```json
{
  "version": 2,
  "run_id": "<8-80位>",
  "skill": "auto-req-analysis",
  "work_item_id": 259735,
  "expected_rev": 1,
  "expected_state": "已建议",
  "verdict": "MANUAL-REVIEW",
  "confirmation_policy": "qc-single-batch-v1",
  "tags": ["PM-AI-MANUAL-REVIEW"],
  "state_to": null,
  "rules_source": {"qc": "pre-qc-v1", "analysis": "evidence-loop-v1"},
  "knowledge_route": {"status": "RESOLVED", "area": "NETHIS5.5", "product_id": "cloudhis-v56", "product_name": "云HIS 5.6", "profile_version": 1, "servers": {"requirements_history": "tfs-requirements", "code_graph": "gitnexus-team", "source_code": "cloudhis-source", "database": "db-knowledge"}},
  "analysis_description": {"categories": ["<受控类别>"]},
  "analysis_gaps": [],
  "evidence_gaps": [
    {"id": "eg1", "topic": "<技术证据主题>", "missing": "<缺失的实现证据>", "impact": "<对技术定位或自动执行的影响>", "owner": "研发", "next_action": "<人工复核动作>", "kind": "technical"}
  ],
  "evidence_refs": {"现状基线": ["work-item"], "问题与目标": ["work-item"], "差异与范围": ["work-item"], "方案取舍": ["work-item"], "成功衡量与非目标": ["work-item"]},
  "kb": {"ready": true, "source_ready": false, "source_required": true, "database_ready": false, "dedup_ran": true, "tools_used": ["list_repos", "query"], "findings": [{"entity": "<实体>", "state": "已证实", "source_tool": "query", "source_type": "code", "conclusion": "<证明了什么>", "evidence": "<依据位置>", "boundary": "<当前证据限制>"}], "note": "<源码 MCP 未就绪，已记 SOURCE_MCP_UNAVAILABLE>"},
  "iteration": {"team_project": "<teamProject>", "expected_date": "<期望日期>", "matched_iteration": "<V6.0.x>", "timing": "正常处理", "pimis": "C级"},
  "attachments": [{"name": "<附件名>", "source": "tfs", "parsed": true, "note": "<证据摘要>"}],
  "artifacts": [
    {"kind": "change-plan", "path": "变更方案_<id>_<run_id>.md"}
  ],
  "generated_at": "2026-07-29T03:14:12Z",
  "audit_note": "<分析 MANUAL-REVIEW(...)>"
}
```

> 新策略下 `analysis_gaps=[]`。`evidence_gaps[]`（`validate_evidence_gaps`）：`id`/`topic`/`missing`/`impact`/`owner`（研发|知识库治理|产品）/`next_action`；不生成附件、不写 TFS。历史计划的 `analysis_gaps[]` 必填字段仍为 `id`/`topic`/`missing`/`impact`/`question`/`options`/`allow_other`，非空仅允许 MANUAL-REVIEW(-STOP)，并要求对应 `manual-followup`；磁盘 artifact 的 `path` 必须是计划同目录精确文件名。

## 结果存储（Redis·可选）

`apply` 成功后（dry-run 与 execute 都会）自动把执行结果摘要写入 Redis Hash，按 collection + 工作项覆盖式存最新，供 TFS-BUDDY / DeerFlow / 看板 / 调试用 `HGETALL` 一步查询。纯标准库 socket（`skills/reg-auto-req-analysis/_lib/tfs/redis_client.py`），**连不上或写入失败自动降级**（`redis.ok=false` + reason，记入审计 extra 与 `apply` 返回），**绝不阻断 apply、绝不回滚已写的 TFS**。

- **key**：`auto-req:qc:plan:<collection>:<work_item_id>`；索引 `auto-req:qc:ids:<collection>`。collection 原样取本次最终生效的 TFS collection（`--collection` / `TFS_COLLECTION` / 配置默认值）。
- **字段**：固定摘要为 `run_id / verdict / tags / state_to / generated_at_utc / run_mode`；所有终局写 `work_item`；`SKIP-ANALYSIS` 额外保存 `skip_reason`；`NEED-*` 额外投影 `next/checklist`；分析终局额外保存 `analysis_description` 与 `knowledge`。`knowledge` 只含 `summary/source_status/evidence_list` 人读佐证清单：`summary` 保留一句结论和按需 `coverage`；`source_status` 按历史需求、产品 Wiki、代码图谱、源码、数据库知识顺序固定展示命中、未就绪、已查询无佐证或本轮未使用；每条 evidence 含中文来源、状态、结论、证据定位、边界及可选历史成熟度。不存无引用价值的编号，也不重复保存 `route/code_graph/source_code/database/wiki/history` 机器分区。可选来源仅声明未就绪、但本轮未实际查询/依赖时，不生成覆盖提示，但仍进入 `source_status`。完整路由、工具、ready、原始 finding 和覆盖状态从计划/审计读取；历史计划按工具名和原定位字段保守生成清单。终局覆盖同一 Hash 时清理不再适用的字段；不存 `plan_json`、本地绝对路径或可由审计取得的重复数据。
- **配置**：`tfs-config.json` 可选 `redis` 段（host/port/db/password/ttl_seconds，缺省 127.0.0.1:6379 无认证）；`REDIS_URL` 或 `REDIS_HOST/PORT/DB/PASSWORD` 环境变量临时覆盖，不写回。
- **查询**：只使用官方入口 `python3 skills/reg-auto-req-analysis/_lib/tfs/redis_client.py hgetall <collection> <id>`。工作项流程禁止调用 `redis-cli` 或临时脚本直接查询/写入；旧 Key 不再读写，也不由代码删除。
- **对接方案**：详见仓库根 [`../../../../与 deerflow 对接接口文档/redis-integration.md`](../../../../与 deerflow 对接接口文档/redis-integration.md)（供 TFS-BUDDY 消费的 key/字段/编排决策/降级回退完整说明；该文档非 skill 运行时读取）。

## 命令

pipeline 的全部子命令（`validate` / `apply` / `repair-analysis-placement` / `flow`）、精确签名与 `--execute` / `--config` / `--plan` 用法见 [`../COMMANDS.md`](../COMMANDS.md)（单一权威索引）。`--plan` 路径取 `过程文件/<id>/<run_id>/执行计划_<id>_<run_id>.json`（运行目录相对）。

> **PAT**：`tfs-config.json` 的 `tfs.pat` 是字面 PAT（兜底默认；本文件 gitignore 不入库）。任一命令可用 `--pat <PAT>` 或环境变量 `TFS_PAT` **临时**覆盖（仅本次调用，不写回配置）；优先级 `--pat > TFS_PAT > tfs.pat`。
>
> **collection**：`tfs-config.json` 的 `tfs.collection` 是兜底默认。任一命令可用 `--collection <集合>` 或环境变量 `TFS_COLLECTION` **临时**覆盖（用于分析其它集合下的工作项，仅本次调用、不写回配置）；优先级 `--collection > TFS_COLLECTION > tfs.collection`。覆盖值沿用同一 `percent-encode` 路径编进 `base_url`，附件 URL 校验与迭代查询自动随之生效。

执行器会拒绝非「需求」工作项、版本或状态已变化的工作项、以及已有 `PM-AI-MANUAL-PASSED` 下游终局标签的工作项。质控或分析重跑时，执行器会清除同一阶段不再适用的旧标签；`SKIP-ANALYSIS` 例外，绝不清理或新增任何 TFS 标签。附件按文件名去重，分析者描述按目标区段整段替换。`repair-analysis-placement` 是受控修复：它不写标签、附件或状态，只在详细信息区段替换成功后，移除完全匹配且位于 `Winning.Demand.Analysis` 末尾的旧 Markdown；不匹配则拒绝删除。

`next_action` 事件（仅 `verdict=PASS` 时 emit；合并后新 run 不产出 PASS 计划，故新 run 不触发）：

```json
{"next_action": {"kind": "run-skill", "skill": "auto-req-analysis", "work_item_id": 228549}}
```

合并前由 TFS-BUDDY / DeerFlow 消费该事件启动分析 skill；**合并后**对话/手动场景由 `auto-req-analysis` 单 skill 内两阶段自驱（质控 PASS 直接继续分析，一个 `run_id` 贯穿），编排器场景则一次调用 `auto-req-analysis` 跑完全链路。执行器对历史 `auto-req-qc` PASS 计划仍 emit `next_action` 作兼容；执行器不会凭空生成变更方案，也不会让模型在写入中途询问人工。
