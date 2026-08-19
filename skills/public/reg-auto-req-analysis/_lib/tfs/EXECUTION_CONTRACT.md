# 自动需求执行计划契约

`pipeline.py` 将模型生成的判定与 TFS 写入分开：skill 只生成方案、清单和计划；执行器负责预检、并发保护、幂等写入与审计。

## 计划文件

每次新触发先运行 `pipeline.py init-run --id <工作项ID>`。`run_id` 只能由服务端执行器生成，调用方请求体不得指定；它同时作为 FastAPI session ID、LangGraph thread ID、运行目录与状态查询 ID。执行器以原子目录创建处理并发与碰撞，并生成 `运行回执_<id>_<run_id>.json`。同一 HTTP 投递用稳定 `Idempotency-Key` 复用原 run；新的业务重分析必须使用新 key 并重新 init。同一冻结计划的 dry-run、后续授权 execute 与网络类幂等续跑复用该 ID。

生成 `执行计划_<工作项ID>_<run_id>.json`（质控的"待补充信息"作为顶层 `checklist` 字段内嵌于此文件，不再单列 `.md`）。所有附件文件名和正文均必须包含同一运行标记：

```html
<!-- auto-req-run:<run_id> -->
```

### 新 SKIP/QC 计划（run-bound-v1）

`SKIP-ANALYSIS`、`NEED-INFO`、`NEED-REVIEW` 固定使用 `version=2, plan_profile=run-bound-v1`。计划保留其完整业务字段，但必须增加 `run_receipt={path,sha256}`，并位于规范目录的精确文件名 `执行计划_<id>_<run_id>.json`。缺回执、身份/目录/文件名/SHA 不一致统一按 `RUN_ID_CONTEXT_MISMATCH` 拒绝，TFS/Redis 零写入。

### 新分析瘦计划（analysis-ref-v1）

新分析终局先保存不可覆盖的 `分析结果_<id>_<run_id>.json`（`schema=analysis-result-v1`），结构化记录 verdict、菜单路径、改动点、五段闭环、范围—方案—验收追踪、证据/缺口、四源采集、通用七面与专项四面。报告、分析者描述和 Redis 摘要从该快照确定性生成；快照禁止包含标签、状态、附件、expected revision 等执行字段。不同快照摘要试图复用已冻结 run_id 时返回 `RUN_ID_ALREADY_FINALIZED`。

快照身份字段固定为 `schema/work_item_id/run_id/generated_at_utc/verdict`；必须带 `confirmation_policy=qc-single-batch-v1`。其余顶层业务字段沿用本契约的 evidence-loop-v2 语义字段（包括 `analysis_profile/analysis_gaps/evidence_refs/evidence_gaps/evidence_acquisition/implementation_impacts/general_rule_coverage/business_rule_coverage/knowledge_route/kb` 等）。`report` 精确包含：`closure`（五章节→各必填业务维度）、`analysis_description={menu_path,categories:[{category,items:[{label,content}]}]}`、`traceability=[{id,scope,behavior,acceptance,status,basis}]`。`pipeline apply` 会先把该结构确定性渲染到临时报告并复用完整 v2 validator，一次返回所有可判定错误；调用方不得再手写一份平行 Markdown 结论。

瘦计划固定为：

```json
{
  "version": 2,
  "plan_profile": "analysis-ref-v1",
  "work_item_id": 263409,
  "run_id": "<init-run 返回值>",
  "expected_rev": 4,
  "expected_state": "已建议",
  "run_receipt": {"path": "运行回执_263409_<run_id>.json", "sha256": "<sha256>"},
  "analysis_result": {"path": "分析结果_263409_<run_id>.json", "sha256": "<sha256>"}
}
```

瘦计划、回执和快照必须处于 `过程文件/<id>/<run_id>/`，身份、目录、计划文件名和 SHA-256 全部一致。执行器固定推导 `skill=auto-req-analysis`、`rules_source.analysis=evidence-loop-v2`、标签、`state_to`、报告附件、分析者描述、Redis 投影及 AUTO 字段流转。同一 run 还会冻结计划摘要；不同摘要返回 `RUN_ID_ALREADY_FINALIZED`。普通 `apply --id --run-id` 不兼容无回执完整 v1/v2；历史计划只能显式 `apply-legacy --plan`，固定 `run_mode=legacy-replay`、不发布正常 Redis、审计隔离到 `legacy-runs/`。不得恢复 evidence-loop-v3、compose、outbox 或强一致 replay 链路。

> **附件两种形态**：① **inline**（质控 `qc-followup`）—— artifact 只能带 `filename:"待补充信息_<id>_<run_id>.json"`、无 `path`；正文由执行器从顶层 `checklist` 物化，运行标记由执行器注入；② **磁盘文件**（分析 `change-plan`；历史计划还可能有 `manual-followup`）——新 `evidence-loop-v2` 计划的 `path` 只能是计划同目录的精确文件名 `需求分析报告_<id>_<run_id>.md`；历史计划兼容 `变更方案_<id>_<run_id>.md`，历史待确认附件为 `待确认清单_<id>_<run_id>.md`。禁止绝对路径、子目录和 `..`，标记写在文件正文里，`validate` 实查文件存在与标记。新运行启用 `confirmation_policy=qc-single-batch-v1` 后不再生成 `manual-followup`。

以下最小完整结构仅用于历史诊断与 `apply-legacy` 维护回放，不是新运行模板：

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
> **新非 SKIP 必填字段 `kb`**：`{ready: bool, source_ready: bool, source_required: bool, database_ready: bool, database_required: bool, tools_used: [...], findings: [...], note?: "..."}`。`ready` 仅表示当前产品代码图谱，`source_ready`/`database_ready` 表示对应工具可用，`source_required`/`database_required` 表示本轮实现影响要求对应核验；ready 与 required 不得混用。每条 finding 必须含 `conclusion/evidence/boundary`，分别回答“证明了什么/依据在哪里/不能证明什么”。源码 finding 必须使用 `source_tool=search_source|search_symbol`、`source_type=code`；数据库 finding 使用 `search_knowledge|get_table_knowledge|traverse_graph|inspect_node`、`source_type=database`，对应工具必须出现在 `tools_used`；需求历史工具只能进入 `tfs_requirements.findings`。`required=true ∧ ready=true` 必须实际调用对应工具并留下 finding；服务不可用或覆盖不足时必须写对应 `evidence_gap`。AUTO 还要求源码 finding 至少一条 `state=已证实`。历史计划缺少 `database_required` 时继续按旧契约读取。
>
> **可选字段 `tfs_requirements`**：`{ready: bool, coverage: {...}, tools_used: [...], findings: [...], note: "..."}`，记录只读需求历史 MCP 的覆盖范围和发现。新策略的 `findings` 每项必须含 `{work_item_id, fact, state, source_tool, conclusion, evidence, boundary}`，`state` 取 `已证实|候选|未确认`；`findings[]` 可带可选 `maturity`（取 `设想|分析确认|已落地`，定义见 `_lib/knowledge-base.md` §六）。只有 `get_work_item` 或 `get_related_work_items` 返回且标为 `已证实` 的 finding 可被 `req:<索引>` 引用；其中**落地级引用**（声称"已落地"、用于查重/相似实现命中/方案成熟度判断）还须 `maturity=已落地`，`maturity=设想`（描述里的解决设想）不得用于支持"已实现/已存在方案"结论。`search_requirements` 结果和 summary 统计不能升级为可引用事实。该对象可省略；存在时由 `pipeline.py` 校验并透传到成功/失败审计。它不覆盖实时 `fetch`，不证明当前实现，不进入 `qc_evidence_resolution`，也不能满足 AUTO 的 `kb:` 门槛。
>
> **可选字段 `existing_feature`**（功能已存在声明）：`{satisfied: bool, requirement_ids?: [不重复正整数], note?: "..."}`。`satisfied=true` 表示查重命中 `state=已证实` 且 `maturity=已落地` 的历史方案、其能力覆盖本次诉求（**一套公版假设**：同一产品线视为一套公版能力，不按项目/客户/版本拆分查重）；此时终局**必须 `MANUAL-REVIEW`**（不得 `AUTO-ANA`，非高风险故不加 `STOP-AUTO`），`pipeline.py` 硬闸拒绝 `satisfied=true ∧ verdict=AUTO-ANA`。`requirement_ids` 为已实现该功能的需求号（不重复正整数数组）；`note` 为说明/边界。可省略（向后兼容）；存在时由 `pipeline.py` 校验结构并透传到审计。它不进 `qc_evidence_resolution`、不替代 AUTO 的 `kb:` 门槛、**不写入 Redis 摘要**（路由信号已由 verdict=MANUAL-REVIEW 承载）。

> **可选字段 `iteration`**（质控时效判定）：`{project, matched: {path, start, finish}|null, timing: "normal|caution|unavailable"}`，记录时效实查的迭代匹配结果（见 `../../references/pre-qc-rules.md §三.3`）。同样由 `pipeline.py` 透传、`validate` 不强制，向后兼容。

> **可选字段 `attachments`**（工作项原始附件证据）：`{ready: bool, download_dir: "附件", downloaded:[{name, path, sha256, size, content_type, content_encoding?}], parsed:[{name, status, output?, converter?, converter_chain?, runtime_mode?, tool_versions?, note?}], skipped:[{name, reason, blocking?}], errors:[{name, reason, blocking?}], preflight?:{requested_formats, capabilities, install_required, installations, runtime_dir, runtime_cache_key, runtime_mode, blocked_formats, warnings}}`。来源附件先用 `tfs_client.py download-attachments` 只读下载，再由官方 `attachment_runtime.py` 按实际格式准备隔离依赖并逐文件转换；存在 `preflight` 时，逐文件终态、转换链、运行模式和安装审计均受校验，旧计划可省略新增字段。Python 依赖统一锁定为 MarkItDown 0.1.7；旧版 `.doc` 由 LibreOffice 转 `.docx` 后进入正式链。具体边界见 `../attachment-evidence.md`；只有 `status=parsed` 的内容可作为判定辅助证据。该字段由 `pipeline.py` 原样透传到审计，不新增 TFS 写入或 artifact 类型。
>
> **质控必填字段 `checklist`**（质控 NEED-INFO/NEED-REVIEW 的待补充清单，即 inline `qc-followup` 的附件源）：`{work_item, verdict, tag, responsible, generated_at_utc, items:[…], next}`。顶层字段均为非空，且 `verdict`/`tag` 必须与计划一致；`items[]` 目标 1–3 个、最多 5 个决策主题，元素 = `{id, question, options:[…], allow_other, responsible?, category?, primary?, why?}`，其中前四项必填、`id` 唯一、`options` 非空。执行器物化上传时把 `checklist` 序列化为附件正文（`待补充信息_<id>_<run_id>.json`）并注入运行标记。PASS 不带 `checklist`/附件。

> **新运行必填字段 `confirmation_policy`**：固定为 `qc-single-batch-v1`，用于声明“一次集中确认”边界。该策略要求所有 PM 可回答、且会改变业务方向、入口/位置、范围边界、取值/聚合口径、异常处置或验收结果的问题都在 QC PASS 前合并进同一 `checklist`；回答后的重跑按稳定 `item.id` 继承已确认答案，除非当前 TFS 修订或新附件与原答案冲突，否则不得重复询问。分析阶段只能输出已证实结论或明确标注边界的合理假设，`analysis_gaps` 必须为 `[]`；技术定位、覆盖不足和实现证据缺口写入 `evidence_gaps`，可进入 MANUAL 人工技术复核但不再向用户生成确认清单。字段缺失仅作为历史计划兼容，未知值一律拒绝。

> **evidence-loop-v2 两层业务覆盖必填字段**：第一层 `general_rule_coverage` 必须精确覆盖 `scope|workflow|business_semantics|business_rules|permissions|exceptions|acceptance`，每项为 `{status:"CONFIRMED|NOT_APPLICABLE", source:"work-item|attachment|inherited-pm-answer|confirmed-product-rule|not-applicable", basis:"<依据>"}`。通用七面不得 `DEFAULTED`；`scope`、`business_semantics`、`acceptance` 必须 `CONFIRMED`，其余只有确实不涉及时才可 `NOT_APPLICABLE`。第二层 `business_rule_coverage` 必须精确覆盖 `presentation|empty_value|maintenance_granularity|historical_data`，每项为 `{status:"CONFIRMED|DEFAULTED|NOT_APPLICABLE", source:"work-item|attachment|inherited-pm-answer|confirmed-product-rule|presentation-default|not-applicable", basis:"<依据>"}`；状态与来源必须匹配，不能只写“产品已确认”。数据读写/数据库影响下，空值保存/清空语义、维护粒度和历史数据不得 `DEFAULTED`。任一适用维度没有可追溯依据时必须回退 QC，并按 `q-scope` / `q-workflow` / `q-business-semantics` / `q-business-rules` / `q-permissions` / `q-exceptions` / `q-acceptance` 或专项四面稳定 ID 合并进唯一 checklist；逐面覆盖不得机械生成逐面问题。

> **evidence-loop-v2 实现影响字段 `implementation_impacts`**：`implementation_impacts` 是非空、不重复白名单数组，允许 `none|ui-presentation|field-assignment|api-contract|business-logic|data-read-write|database-schema|data-migration|reporting-statistics|data-permission|database-performance|database-script`；`none` 不得与其它值并存。除 `none/ui-presentation` 外均要求 `source_required=true`；数据读写、schema、迁移、统计、数据权限、数据库性能和脚本还要求 `database_required=true`。

> **分析必填字段 `analysis_description`**：仅 `auto-req-analysis` 使用，结构为 `{categories: ["report", ...]}`。`categories` 必须为非空、无重复的受控类别数组，并与 `change-plan` 的“## 三、分析者描述”三级标题完全一致。新计划使用 `analysis_profile=concise-v3`：正文必须在类别标题前包含且只包含一个非空 `- **菜单路径**：...`；不得包含“需求类别”、旧版“路径（菜单路径+操作路径）”或“决策结论 / 生效路径与条件 / 决策边界 / 验收要点”，再按类别输出 1–3 行有效业务结论。菜单路径来自工作项、已解析附件或唯一菜单索引；无菜单入口时写“无菜单入口：<明确触发方式>”。同一维度存在 2–8 个独立变更点时，须按 `1. **改动点**：内容` 从 1 连续编号，写入 TFS 时渲染为父维度的缩进子项 `1、2、3`（`&nbsp;` 缩进，源码仍顶格）；单项不编号。类别标题只供附件结构和机器校验，写入 TFS 时不渲染；菜单路径和业务维度均安全渲染。操作步骤、位置、触发条件和边界直接写入对应方案行。执行器拒绝路径缺失/重复、编号不连续、编号项格式错误、缺失维度、模板占位符、`TODO` 和已列明空泛话术，不按总字数判定；执行时整段替换 `System.Description` 中 `【分析者描述】` 至 `【开发者描述】` 之间的内容。完整 `change-plan` 仍只作为 Markdown 附件上传，绝不写入 `Winning.Demand.Analysis`。类别清单与写作规则见 `../../references/analysis-description-writing-rules.md`。

> **新策略分析附件收敛（固定执行行为，无计划字段）**：`confirmation_policy=qc-single-batch-v1` 的 `AUTO-ANA / MANUAL-REVIEW / MANUAL-REVIEW-STOP` 在仅产出 `change-plan` 时，apply 顺序固定为“写分析者描述 → 上传本轮需求分析报告 → 清理历史受控 AI 附件 → 加终局标签 → AUTO 字段流转 → Redis”。清理只扫描 `AttachedFile`，且只匹配当前工作项 ID 的 `需求分析报告_<id>_<run_id>.md`、历史 `变更方案_<id>_<run_id>.md`、`待确认清单_<id>_<run_id>.md`、`待补充信息_<id>_<run_id>.json`；保留并验证唯一的本轮报告，同名重复关系只保留一条。execute 通过一次带 revision guard 的 JSON Patch 按 relation index 倒序解绑，随后重新 fetch 验证；不删除附件 Blob，不触碰普通业务附件。dry-run 仅返回拟解绑清单。上传失败不进入清理；清理或回读失败立即失败关闭，本轮报告不回滚，但不加新终局标签、不流转状态、不发布新 Redis。`SKIP-ANALYSIS`、`NEED-*` 及仍要求本轮 `manual-followup` 的历史计划不清理；本地过程文件和审计永不因该动作删除。
> **可选字段 `analysis_profile`**：允许 `concise-v1`、`concise-v2` 或 `concise-v3`。新计划固定使用 `concise-v3`，支持全部受控类别和多类别组合；`concise-v1` / `concise-v2` 只兼容单一 `existing-ui-simple` / `print-adjustment` / `data-management` 历史计划，无 profile 时继续使用历史完整维度及公共摘要/路径格式。
>
> **分析必填字段 `analysis_gaps`**：仅 `auto-req-analysis` 使用；始终为数组。启用 `confirmation_policy=qc-single-batch-v1` 的新计划必须填 `[]`，不得生成 `manual-followup`；若分析时才发现 PM 可回答的业务歧义，必须回退为 QC `NEED-REVIEW`，把问题并入 inline `checklist` 后结束本轮。历史计划未带该策略时，仍兼容非空元素 `{id, topic, missing, impact, question, options, allow_other}` 及唯一 `manual-followup` 磁盘附件；附件逐项包含 `<!-- analysis-gap:<id> -->` 与五项非空字段，且不得出现 `PM-AI-` 自动标签。

> **v2 分析必填字段 `evidence_refs` / `evidence_gaps`**：`evidence_refs` 是 `{闭环章节: ["work-item"|"req:<索引>"|"kb:<索引>"|"wiki:<索引>", ...]}`，必须精确覆盖“现状基线、问题与目标、差异与范围、方案取舍、成功衡量与非目标”；`req:` 必须指向 `tfs_requirements.findings` 中由 `get_work_item|get_related_work_items` 核验的 `state=已证实` 历史工作项事实（落地级引用——声称已落地/查重命中/方案成熟度——还须 `maturity=已落地`，见 `_lib/knowledge-base.md` §六），KB 索引必须指向 `state=已证实` 的 finding，wiki 索引必须指向 `state=wiki-确认`（历史兼容 `已证实`）的 finding。`AUTO-ANA` 的现状基线、差异与范围、方案取舍还必须各引用至少一条 `kb:<索引>`，`req:` 不能替代。`evidence_gaps` 始终为数组，元素为 `{id, topic, missing, impact, owner, next_action}` + 可选 `{type, kind}`；`owner` 允许 `研发|知识库治理|产品`（背景采集缺口用 `产品`），可选 `kind ∈ {technical,background}`、`type` 取稳定缺口枚举（`WIKI_TOPIC_MISSING`/`WIKI_BROKEN_LINK`/`TFS_SNAPSHOT_LAG`/`TFS_SEARCH_TRUNCATED`/`GITNEXUS_REPO_MISSING`/`GITNEXUS_EMBEDDINGS_DISABLED`/`DB_SCHEMA_PARTIAL`/`DB_RELATION_PARTIAL`/`EXISTING_IMPL_LOCATION`/`SIMILAR_IMPL_DEDUP`）；记录现有实现、相似能力、调用路径、方案取舍或**背景采集缺口**（wiki 未覆盖/TFS 范围外/图谱覆盖不全）。非空 `evidence_gaps` 禁止 AUTO-ANA，但不生成 `manual-followup`、不写 TFS；二者均由执行器透传到运行审计。
>
> **界面 AUTO 必填字段 `ui_baseline`**：新策略 `AUTO-ANA` 且 `auto_scopes` 含 `field-ui-copy` 时必须填写 `{sources:[{type, ref, note?}, ...]}`。`type` 允许 `wiki|runtime-observation|attachment|code-graph|source-code`，分别归入产品知识、运行观察、实现证据三类；至少覆盖两类，同类多条只算一类。`wiki` 的 `ref` 必须是已证实 `wiki:<索引>`，`code-graph|source-code` 的 `ref` 必须是已证实 `kb:<索引>`，其中 `source-code` 必须指向 `search_source|search_symbol` finding。`runtime-observation|attachment` 的 `ref` 写可追溯页面/URL/截图或已解析附件标识。wiki 是按需优先源，不是固定必需源；wiki 未覆盖但运行观察 + 实现证据闭合时仍可 AUTO。字段缺失或只有一类证据时改判 `MANUAL-REVIEW` 并记“界面现状交叉证据不足”。历史计划和非 `field-ui-copy` 范围可省略；存在时执行器校验并透传审计。
>
> **新缺口类型**：`PRODUCT_ROUTE_UNRESOLVED`、`SOURCE_MCP_UNAVAILABLE`、`SOURCE_SCOPE_PARTIAL`、`SOURCE_VERIFICATION_INCOMPLETE`、`DATABASE_MCP_UNAVAILABLE`。不扩展现有四源 `evidence_acquisition`；产品由 `knowledge_route` 标识，源码/数据库核验状态保留在 `kb`，覆盖不足继续使用 `DB_SCHEMA_PARTIAL` / `DB_RELATION_PARTIAL`。
>
> **v2 当前规则源 `evidence-loop-v2` + 必填 `evidence_acquisition`**：新分析计划固定使用 `evidence-loop-v2`；`evidence-loop-v1` 仅兼容历史计划。v2 分析计划**必须**新增顶层 `evidence_acquisition` 对象，按四源 `tfs_requirements`/`wiki`/`gitnexus`/`db_knowledge` 如实记录采集状态；每源结构 `{availability: READY|UNAVAILABLE, coverage_status: COMPLETE|PARTIAL|OUT_OF_SCOPE|UNKNOWN, query_status: HIT|NO_HIT|SKIPPED|ERROR, queries:[{terms, truncated?, returned?, total?, verified_ids?}], stop_reason: exhausted|verified_hit|hard_limit|source_gap|not_applicable}`，未触发的源用 `query_status=SKIPPED`、`queries:[]`。**四态区分**：已命中 / 完整查询后无命中 / 覆盖不完整 / 来源不可用——只有 `coverage_status=COMPLETE ∧ stop_reason=exhausted ∧ 未截断` 的 `query_status=NO_HIT` 才能支撑「无相似需求 / 无现有实现」负面结论。校验器硬约束：①`kb.dedup_ran=true` 时 `tfs_requirements.queries` 不得为空；②`AUTO-ANA` 须 `tfs_requirements.coverage_status ∈ {COMPLETE, OUT_OF_SCOPE}`；③`coverage_status=COMPLETE` 须 `availability=READY ∧ stop_reason=exhausted ∧ 无 truncated=true`；④`query_status=NO_HIT` 须 `coverage_status=COMPLETE`；⑤`tfs_requirements.findings` 的 `maturity=已落地` 须 `state=已证实`。高风险/MANUAL 只改变终局，不允许把已触发的源码/数据库源记为 `SKIPPED:not_applicable`。

> **v2 需求分析报告追踪表**：必须且只能有一个“## 四、范围—方案—验收追踪”区，表头固定为 `ID | 范围/改动点 | 方案/目标行为 | 验收场景与结果 | 结论状态 | 依据或缺口`。每行的范围、方案和验收均非空。启用 `qc-single-batch-v1` 时，结论状态只允许“已证实 / 合理假设”，合理假设的依据须以“呈现类默认：”说明适用边界；出现无法形成这两类结论的业务项时回退 QC。历史计划仍兼容“待业务确认”及其与 `analysis-gap:<id>`、`analysis_gaps` 的一对一关联。此表保留业务语言，不暴露技术定位。

> **可选字段 `qc_evidence_resolution`**：仅用于“初判 `NEED-REVIEW` 经 KB/wiki 补证后复核为 PASS、进入阶段二”的 v2 分析计划，结构为 `{initial_verdict:"NEED-REVIEW", post_evidence_verdict:"PASS", items:[{id, initial_gap, resolution, evidence_refs:["kb:<索引>"|"wiki:<索引>"]}]}`。`items` 非空、ID 唯一；每项引用必须指向 KB 的 `已证实` 或 wiki 的 `wiki-确认`（历史兼容 `已证实`）finding，不能使用 `work-item`、`req:`、候选或推断。历史需求只能说明过去工作项事实，不能单独证明当前规则并放行 QC。执行器校验后将该字段写入运行审计；它是放行理由，不生成附件、不改变阶段二终局规则。

> **计划版本与规则来源**：v2 为当前新计划版本。QC 终局通过 `run-bound-v1` 绑定回执，规则源为 `{"qc":"pre-qc-v1"}`；新分析终局通过 `analysis-ref-v1` 引用快照并固定为 `{"qc":"pre-qc-v1","analysis":"evidence-loop-v2"}`。`evidence-loop-v1`、无回执 v1/v2 及其 `fallback-v1` 只允许 `apply-legacy` 维护回放。

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

## 历史完整 JSON 骨架（按 verdict · 仅 apply-legacy）

> 本节仅解释既有 v1/v2 完整计划的诊断与显式维护回放，不是新计划模板。新 SKIP/QC 必须使用 `run-bound-v1`，新分析必须使用 `analysis-result-v1 + analysis-ref-v1`，不得从本节复制完整骨架重新生成。历史计划共有字段为 `version, run_id, skill, work_item_id, expected_rev, expected_state, verdict, rules_source, tags, state_to, artifacts`；普通 apply 返回 `LEGACY_PLAN_REQUIRES_EXPLICIT_REPLAY`。

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
  "rules_source": {"qc": "pre-qc-v1", "analysis": "evidence-loop-v2"},
  "knowledge_route": {"status": "RESOLVED", "area": "NETHIS5.5", "product_id": "cloudhis-v56", "product_name": "云HIS 5.6", "profile_version": 1, "servers": {"requirements_history": "tfs-requirements", "code_graph": "gitnexus-team", "source_code": "cloudhis-source", "database": "db-knowledge"}},
  "auto_scopes": ["field-ui-copy"],
  "assignee_to": "WINNING\\<账号>",
  "analysis_description": {"categories": ["existing-ui-simple"]},
  "analysis_profile": "concise-v3",
  "analysis_gaps": [],
  "implementation_impacts": ["ui-presentation"],
  "general_rule_coverage": {"scope": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已明确生效范围"}, "workflow": {"status": "NOT_APPLICABLE", "source": "not-applicable", "basis": "本次不改变流程"}, "business_semantics": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已明确字段含义"}, "business_rules": {"status": "NOT_APPLICABLE", "source": "not-applicable", "basis": "本次不新增业务规则"}, "permissions": {"status": "NOT_APPLICABLE", "source": "not-applicable", "basis": "本次不改变权限"}, "exceptions": {"status": "NOT_APPLICABLE", "source": "not-applicable", "basis": "本次不新增异常分支"}, "acceptance": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已有可验证结果"}},
  "business_rule_coverage": {"presentation": {"status": "DEFAULTED", "source": "presentation-default", "basis": "呈现类默认：沿用当前页面布局"}, "empty_value": {"status": "DEFAULTED", "source": "presentation-default", "basis": "呈现类默认：沿用当前空值展示"}, "maintenance_granularity": {"status": "NOT_APPLICABLE", "source": "not-applicable", "basis": "本次不修改数据维护"}, "historical_data": {"status": "NOT_APPLICABLE", "source": "not-applicable", "basis": "本次不影响历史数据"}},
  "evidence_gaps": [],
  "evidence_refs": {
    "现状基线": ["work-item", "kb:0"],
    "问题与目标": ["work-item"],
    "差异与范围": ["work-item", "kb:0"],
    "方案取舍": ["kb:0"],
    "成功衡量与非目标": ["work-item"]
  },
  "ui_baseline": {"sources": [{"type": "code-graph", "ref": "kb:0"}, {"type": "runtime-observation", "ref": "受控页面：<菜单/URL>", "note": "<采集时间、项目/版本与可见状态>"}]},
  "kb": {"ready": true, "source_ready": true, "source_required": false, "database_ready": false, "database_required": false, "dedup_ran": true, "tools_used": ["list_repos", "query", "context"], "findings": [{"entity": "<已证实实体>", "state": "已证实", "source_tool": "query", "source_type": "code", "conclusion": "<证明了什么>", "evidence": "<依据位置>", "boundary": "<不能证明什么>"}], "note": "<查重与定位摘要>"},
  "evidence_acquisition": {"tfs_requirements": {"availability": "READY", "coverage_status": "COMPLETE", "query_status": "HIT", "queries": [{"terms": "<查重词>", "truncated": false}], "stop_reason": "exhausted"}, "wiki": {"availability": "READY", "coverage_status": "OUT_OF_SCOPE", "query_status": "SKIPPED", "queries": [], "stop_reason": "not_applicable"}, "gitnexus": {"availability": "READY", "coverage_status": "COMPLETE", "query_status": "HIT", "queries": [{"terms": "<模块锚点>", "truncated": false}], "stop_reason": "exhausted"}, "db_knowledge": {"availability": "READY", "coverage_status": "OUT_OF_SCOPE", "query_status": "SKIPPED", "queries": [], "stop_reason": "not_applicable"}},
  "wiki": {"ready": true, "modules_matched": ["<模块>"], "findings": [{"entity": "<业务语义>", "state": "wiki-确认", "source": "<wiki路径>", "conclusion": "<业务规则结论>", "evidence": "<wiki 路径与章节>", "boundary": "<Wiki 不能替代的实现核验>"}], "note": "<wiki 佐证摘要>"},
  "iteration": {"team_project": "<teamProject>", "expected_date": "<期望日期>", "matched_iteration": "<V6.0.x>", "timing": "正常处理", "pimis": "C级"},
  "attachments": [{"name": "<附件名>", "source": "<tfs|external>", "parsed": true, "parse_method": "<visual|...>", "note": "<证据摘要>"}],
  "artifacts": [
    {"kind": "change-plan", "path": "需求分析报告_<id>_<run_id>.md"}
  ],
  "generated_at": "2026-07-29T10:03:19Z",
  "audit_note": "<QC=PASS... → 分析 AUTO-ANA(...)>"
}
```

> AUTO-ANA 硬约束：`knowledge_route.status=RESOLVED`；`analysis_gaps=[]` 且 `evidence_gaps=[]`；`auto_scopes` 为允许子集；`evidence_refs` 的现状基线/差异与范围/方案取舍各至少一条 `kb:<已证实索引>`；`kb.ready=true` 且 `kb.dedup_ran=true`。`source_required=true` / `database_required=true` 时必须实际调用对应工具并留下 finding。`implementation_impacts`、通用七面 `general_rule_coverage`、专项四面 `business_rule_coverage` 与四源 `evidence_acquisition` 必须通过。`auto_scopes` 含 `field-ui-copy` 时还必须有覆盖至少两类独立证据的 `ui_baseline.sources`。不得声明 `existing_feature.satisfied=true`。

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
  "rules_source": {"qc": "pre-qc-v1", "analysis": "evidence-loop-v2"},
  "knowledge_route": {"status": "RESOLVED", "area": "NETHIS5.5", "product_id": "cloudhis-v56", "product_name": "云HIS 5.6", "profile_version": 1, "servers": {"requirements_history": "tfs-requirements", "code_graph": "gitnexus-team", "source_code": "cloudhis-source", "database": "db-knowledge"}},
  "analysis_description": {"categories": ["<受控类别>"]},
  "analysis_gaps": [],
  "implementation_impacts": ["field-assignment"],
  "general_rule_coverage": {"scope": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已明确适用范围"}, "workflow": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已明确操作流程"}, "business_semantics": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已明确字段业务含义"}, "business_rules": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已明确维护规则"}, "permissions": {"status": "NOT_APPLICABLE", "source": "not-applicable", "basis": "本次不改变权限"}, "exceptions": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已明确失败处理"}, "acceptance": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已有可验证验收"}},
  "business_rule_coverage": {"presentation": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已明确展示入口"}, "empty_value": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已明确空值语义"}, "maintenance_granularity": {"status": "CONFIRMED", "source": "work-item", "basis": "工作项已明确维护对象粒度"}, "historical_data": {"status": "NOT_APPLICABLE", "source": "not-applicable", "basis": "本次不涉及存量历史数据"}},
  "evidence_gaps": [
    {"id": "eg1", "topic": "<技术证据主题>", "missing": "<缺失的实现证据>", "impact": "<对技术定位或自动执行的影响>", "owner": "研发", "next_action": "<人工复核动作>", "kind": "technical", "type": "SOURCE_MCP_UNAVAILABLE"}
  ],
  "evidence_refs": {"现状基线": ["work-item"], "问题与目标": ["work-item"], "差异与范围": ["work-item"], "方案取舍": ["work-item"], "成功衡量与非目标": ["work-item"]},
  "kb": {"ready": true, "source_ready": false, "source_required": true, "database_ready": false, "database_required": false, "dedup_ran": true, "tools_used": ["list_repos", "query"], "findings": [{"entity": "<实体>", "state": "已证实", "source_tool": "query", "source_type": "code", "conclusion": "<证明了什么>", "evidence": "<依据位置>", "boundary": "<当前证据限制>"}], "note": "<源码 MCP 未就绪，已记 SOURCE_MCP_UNAVAILABLE>"},
  "evidence_acquisition": {"tfs_requirements": {"availability": "READY", "coverage_status": "COMPLETE", "query_status": "HIT", "queries": [{"terms": "<查重词>", "truncated": false}], "stop_reason": "exhausted"}, "wiki": {"availability": "READY", "coverage_status": "PARTIAL", "query_status": "ERROR", "queries": [{"terms": "<业务主题>", "truncated": false}], "stop_reason": "source_gap"}, "gitnexus": {"availability": "READY", "coverage_status": "PARTIAL", "query_status": "HIT", "queries": [{"terms": "<模块锚点>", "truncated": false}], "stop_reason": "verified_hit"}, "db_knowledge": {"availability": "READY", "coverage_status": "OUT_OF_SCOPE", "query_status": "SKIPPED", "queries": [], "stop_reason": "not_applicable"}},
  "iteration": {"team_project": "<teamProject>", "expected_date": "<期望日期>", "matched_iteration": "<V6.0.x>", "timing": "正常处理", "pimis": "C级"},
  "attachments": [{"name": "<附件名>", "source": "tfs", "parsed": true, "note": "<证据摘要>"}],
  "artifacts": [
    {"kind": "change-plan", "path": "需求分析报告_<id>_<run_id>.md"}
  ],
  "generated_at": "2026-07-29T03:14:12Z",
  "audit_note": "<分析 MANUAL-REVIEW(...)>"
}
```

> 新策略下 `analysis_gaps=[]`。`evidence_gaps[]`（`validate_evidence_gaps`）：`id`/`topic`/`missing`/`impact`/`owner`（研发|知识库治理|产品）/`next_action`；不生成附件、不写 TFS。历史计划的 `analysis_gaps[]` 必填字段仍为 `id`/`topic`/`missing`/`impact`/`question`/`options`/`allow_other`，非空仅允许 MANUAL-REVIEW(-STOP)，并要求对应 `manual-followup`；磁盘 artifact 的 `path` 必须是计划同目录精确文件名。

## 有效验证分层

正常新流程只调用一次 `apply --id <id> --run-id <run_id>`。执行器在内存副本上先归一化、再一次性收集问题；不改写冻结计划、分析快照或报告，也不要求编排器先独立 `validate`。`validate --plan` 复用同一分类器，仅供维护诊断。

`validation.decision` 取 `PASS|REJECT|ANALYSIS_ONLY`；`WARNING` 是非阻断严重度，以非空 `validation.warnings` 表示。`errors`、`errors_by_class` 和 `validation.blocking_errors` 只包含真正阻断项：

- `REJECT`：运行身份/规范目录/回执/文件名/SHA/冻结摘要不一致，业务结论与 verdict/checklist/标签/状态互相矛盾，或 `AUTO-ANA` 缺决定性证据、仍有 evidence gap。TFS 与 Redis 均零写入。
- `ANALYSIS_ONLY`：分析结果可信，但 TFS precheck、revision/state、权限、网络或安全写入条件不足。发布本轮分析 Redis 镜像，TFS 零写入；仅 `SOURCE_CHANGED` 要求新 run_id。
- `WARNING`：可确定归一化的附件转换链格式、已有 finding 但漏记探活工具、NEED-* / MANUAL 缺非决定性源码 finding，或不影响结论与动作的审计字段问题。继续 Redis 与安全 TFS 动作，不发布 `ERROR`、不要求重分析或新 run_id。同类问题一旦被业务结论直接引用即升级为 blocker；`AUTO-ANA` 不得借 WARNING 绕过证据硬闸。

返回和审计固定包含：

```json
{
  "validation": {
    "decision": "PASS",
    "blocking_errors": {"analysis": [], "source": [], "execution": []},
    "warnings": [{"code": "ATTACHMENT_CHAIN_NORMALIZED", "field": "attachments.parsed[0].converter_chain", "message": "已从单元素数组归一化为 builtin-fallback"}],
    "normalizations": []
  }
}
```

运行状态只保存 `warning_count` 供 `/status/<run_id>` 展示；Redis Key 和字段结构不变，不保存完整 warnings。执行器不得伪造 `tools_used`：漏记已由 finding 证明发生的探活只告警，原计划仍保持原值。

## 写后检查点与重复调用

每个真实非-noop TFS 写原语必须把 PATCH 成功响应中的 `post_rev` 和可用的 `post_state` 返回给 pipeline；pipeline 直接以该写响应更新执行检查点。revision 只要求严格高于该动作写前持有值，不要求业务动作恰好增加 1，也不得依赖紧随 PATCH 的 GET 作为首选写后身份。`post_rev` 等于写前持有值（且 state 不变）是 TFS 确认的等效 no-op——内容已就位，继续执行、不推进检查点 rev，但把动作计入已完成避免续跑重做；rev 不变而 state 变化仍是不一致信号，照常失败关闭。仅为兼容未返回 revision 的旧服务响应，执行器才进行有限次数回读确认；仍不可见时返回 `POST_WRITE_CONFIRMATION_FAILED`，并在失败审计的 `actions` 中保留已经返回成功的动作及 `partial_write=true`，不得伪装成零写入。

TFS HTML 字段落库会剥除 HTML 注释：分析者描述中的 `<!-- auto-req-run:... -->` run 标记仅用于幂等去重，不代表内容差异。分析者描述的 no-op 判定按剥除 run 标记注释后的内容比较，只差标记时不发 PATCH（TFS 对无字段值变更的 PATCH 不创建 revision，发出去也无法从 revision 观测到）。

普通 `apply` 先以规范运行目录内的原子锁串行化同一 run，锁冲突或 `ANALYZING/APPLYING` 均返回 `RUN_ALREADY_IN_PROGRESS`。`FAILED`（含 SOURCE_CHANGED 终局）返回 `RUN_TERMINAL_REQUIRES_NEW_RUN`，不再次调用 apply_plan、不发布 Redis、不生成新审计；`COMPLETED` 且已 execute 的相同投递返回 `already-completed` 幂等结果，同样不重复写入或新建审计。`COMPLETED` dry-run 后的首次授权 execute 继续使用原 run_id；可恢复的 `ANALYSIS_ONLY` 也仍可在执行条件恢复后继续。`RUN_NOT_READY` 保持 `INITIALIZED`，等待同一 run 产物生成。

## 结果存储（Redis·可选）

`apply` 成功后（dry-run 与 execute 都会）自动把执行结果摘要写入 Redis Hash，按 collection + 工作项覆盖式存最新，供 TFS-BUDDY / DeerFlow / 看板 / 调试用 `HGETALL` 一步查询。纯标准库 socket（`skills/reg-auto-req-analysis/_lib/tfs/redis_client.py`），**连不上或写入失败自动降级**（`redis.ok=false` + reason，记入审计 extra 与 `apply` 返回），**绝不阻断 apply、绝不回滚已写的 TFS**。

- **分析已完成但执行资格不足**：新瘦计划在 TFS precheck/preflight 未满足且尚未开始写动作时，仍用 `publish_plan` 写本轮 verdict、分析者描述和知识摘要，`run_mode=analysis-only`，返回 `{ok:true, applied:false, actions:[]}`；不得调用 `publish_failure` 或覆盖为 `ERROR`。revision/state 变化另带 `error_code=SOURCE_CHANGED`、`retryable=false`、`requires_new_run=true`。
- **写动作开始后的失败**：只有普通新 run 的 TFS 写动作已经开始后失败，才向同一 Hash/索引写最小 `verdict=ERROR` + `error` 标记并清理上一轮成功动作字段。`ERROR` 是 Redis 侧专用失败标记，不在分析快照 verdict 契约内。`apply-legacy` 无论成功失败都不发布正常 Redis。
- **key**：`auto-req:qc:plan:<collection>:<work_item_id>`；索引 `auto-req:qc:ids:<collection>`。collection 原样取本次最终生效的 TFS collection（`--collection` / `TFS_COLLECTION` / 配置默认值）。
- **字段**：固定摘要为 `run_id / verdict / tags / state_to / generated_at_utc / run_mode`，其中 `run_mode` 允许 `dry-run|execute|analysis-only`；其余投影字段保持不变。完整路由、工具、ready、原始 finding 和覆盖状态从分析快照/审计读取；Redis 不存 `plan_json` 或本地绝对路径。
- **配置**：`tfs-config.json` 可选 `redis` 段（host/port/db/password/ttl_seconds，缺省 127.0.0.1:6379 无认证）；`REDIS_URL` 或 `REDIS_HOST/PORT/DB/PASSWORD` 环境变量临时覆盖，不写回。
- **查询**：只使用官方入口 `python3 skills/reg-auto-req-analysis/_lib/tfs/redis_client.py hgetall <collection> <id>`。工作项流程禁止调用 `redis-cli` 或临时脚本直接查询/写入；旧 Key 不再读写，也不由代码删除。
- **状态查询**：正常会话状态只按 `run_id` 查询，状态机至少为 `INITIALIZED → ANALYZING → FROZEN → APPLYING → COMPLETED|ANALYSIS_ONLY|FAILED`。FastAPI 若用覆盖式 Redis Hash 辅助 `/status/<run_id>`，必须先核对 Hash 内 `run_id` 与 URL 一致；不一致立即忽略 Redis，不得返回其它轮结果。
- **对接方案**：详见仓库根 [`../../../../与 deerflow 对接接口文档/redis-integration.md`](../../../../与 deerflow 对接接口文档/redis-integration.md)（供 TFS-BUDDY 消费的 key/字段/编排决策/降级回退完整说明；该文档非 skill 运行时读取）。

## 命令

pipeline 的全部子命令（`init-run` / `validate` / `apply` / `apply-legacy` / `status` / `repair-analysis-placement` / `flow`）及精确签名见 [`../COMMANDS.md`](../COMMANDS.md)（单一权威索引）。正常新流程只调用 `init-run` 后的一次 `apply --id --run-id`；`validate --plan` 留作维护诊断，`apply-legacy --plan` 仅作历史维护回放。

> **PAT**：`tfs-config.json` 的 `tfs.pat` 是字面 PAT（兜底默认；本文件 gitignore 不入库）。任一命令可用 `--pat <PAT>` 或环境变量 `TFS_PAT` **临时**覆盖（仅本次调用，不写回配置）；优先级 `--pat > TFS_PAT > tfs.pat`。
>
> **collection**：`tfs-config.json` 的 `tfs.collection` 是兜底默认。任一命令可用 `--collection <集合>` 或环境变量 `TFS_COLLECTION` **临时**覆盖（用于分析其它集合下的工作项，仅本次调用、不写回配置）；优先级 `--collection > TFS_COLLECTION > tfs.collection`。覆盖值沿用同一 `percent-encode` 路径编进 `base_url`，附件 URL 校验与迭代查询自动随之生效。

执行器会拒绝非「需求」工作项、以及版本或状态已变化的工作项。执行器不再因 `PM-AI-MANUAL-PASSED` 下游通过标签拒绝重跑；非 `SKIP-ANALYSIS` 的覆盖重跑会在"清旧标签"阶段作废该标签（记 `invalidate-tag` 动作并写入审计 `invalidated_passed_tags`）。质控或分析重跑时，执行器会清除 QC 与分析两阶段中不在本次终局的旧标签（跨阶段一并收敛，只保留本次 expected 标签）；`SKIP-ANALYSIS` 例外，绝不清理或新增任何 TFS 标签。普通上传按文件名去重；新策略分析终局在本轮方案安全关联后，按上文规则收敛历史受控 AI 附件关系。分析者描述按目标区段整段替换；写入只以「【分析者描述】」为唯一硬要求，「【开发者描述】」为可选软边界（存在且在其后时作终点以保留开发者内容，缺失则替换到描述末尾），「【分析者描述】」本身缺失时向末尾追加新区段，缺失不阻断写入。`repair-analysis-placement` 是受控修复：它不写标签、附件或状态，只在详细信息区段替换成功后，移除完全匹配且位于 `Winning.Demand.Analysis` 末尾的旧 Markdown；不匹配则拒绝删除。

`next_action` 事件（仅 `verdict=PASS` 时 emit；合并后新 run 不产出 PASS 计划，故新 run 不触发）：

```json
{"next_action": {"kind": "run-skill", "skill": "auto-req-analysis", "work_item_id": 228549}}
```

合并前由 TFS-BUDDY / DeerFlow 消费该事件启动分析 skill；**合并后**对话/手动场景由 `auto-req-analysis` 单 skill 内两阶段自驱（质控 PASS 直接继续分析，一个 `run_id` 贯穿），编排器场景则一次调用 `auto-req-analysis` 跑完全链路。执行器对历史 `auto-req-qc` PASS 计划仍 emit `next_action` 作兼容；执行器不会凭空生成需求分析报告，也不会让模型在写入中途询问人工。
