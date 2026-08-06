# 自动需求执行计划契约

`pipeline.py` 将模型生成的判定与 TFS 写入分开：skill 只生成方案、清单和计划；执行器负责预检、并发保护、幂等写入与审计。

## 计划文件

生成 `执行计划_<工作项ID>_<run_id>.json`（质控的"待补充信息"作为顶层 `checklist` 字段内嵌于此文件，不再单列 `.md`）。`run_id` 为 8-80 位字母、数字、`-` 或 `_`；所有附件文件名和正文均必须包含同一运行标记：

```html
<!-- auto-req-run:<run_id> -->
```

> **附件两种形态**：① **inline**（质控 `qc-followup`）—— artifact 只能带 `filename:"待补充信息_<id>_<run_id>.json"`、无 `path`；正文由执行器从顶层 `checklist` 物化，运行标记由执行器注入；② **磁盘文件**（分析 `change-plan`/`manual-followup`）—— `path` 只能是计划同目录的精确文件名 `变更方案_<id>_<run_id>.md` / `待确认清单_<id>_<run_id>.md`，禁止绝对路径、子目录和 `..`，标记写在文件正文里，`validate` 实查文件存在与标记。

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
  "tags": ["PM-AI-QC-NEED-INFO"],
  "state_to": null,
  "rules_source": {"qc": "pre-qc-v1"},
  "checklist": {
    "work_item": "228549 <标题>", "verdict": "NEED-INFO", "tag": "PM-AI-QC-NEED-INFO",
    "responsible": "实施/创建者", "generated_at_utc": "2026-07-20",
    "items": [
      {"id": "q1", "question": "<要补充的问题>", "options": ["<推荐选项1>", "<推荐选项2>"], "allow_other": true}
    ],
    "passed": [],
    "next": "责任方补充后重跑 auto-req-analysis 228549，通过后才进入分析"
  },
  "artifacts": [
    {"kind": "qc-followup", "filename": "待补充信息_228549_20260720_abc12345.json"}
  ]
}
```

> **可选字段 `kb`**：`{ready: bool, tools_used: [...], findings?: [...], note: "..."}`，记录本次是否调用知识库及发现链命中（见 `../knowledge-base.md`）。`findings: [{entity, state, source_tool}]`（`state` ∈ 图谱事实/推断/未确认）供审计/下游机读提取。由 `pipeline.py` 透传到审计；`validate` 不强制此字段（不在 `required` 内），故既有计划与新增计划都兼容。KB 具体发现同时写进变更方案/清单附件正文（质控：`checklist.items` 命名实体问题 + `checklist.kb_note` 三态摘要，带运行标记）。质控 KB **不直接判终局**（verdict/tags/state_to 契约不变，pipeline 侧零改动）——发现经现有核对维度（`qc-checklist.md` #3/#5）参与。
>
> **可选字段 `iteration`**（质控时效判定）：`{project, matched: {path, start, finish}|null, timing: "normal|caution|unavailable"}`，记录时效实查的迭代匹配结果（见 `../../references/pre-qc-rules.md §三.3`）。同样由 `pipeline.py` 透传、`validate` 不强制，向后兼容。

> **可选字段 `attachments`**（工作项原始附件证据）：`{ready: bool, download_dir: "附件", downloaded:[{name, path, sha256, size, content_type, content_encoding?}], parsed:[{name, status, output?, note?}], skipped:[{name, reason}], errors:[{name, reason}]}`。来源附件先用 `tfs_client.py download-attachments` 只读下载到本次运行目录，再用本项目的 `attachment_converter.py` 转换，具体边界见 `../attachment-evidence.md`；只有 `status=parsed` 的内容可作为判定辅助证据。该字段由 `pipeline.py` 原样透传到审计，不新增 TFS 写入或 artifact 类型。
>
> **质控必填字段 `checklist`**（质控 NEED-INFO/NEED-REVIEW 的待补充清单，即 inline `qc-followup` 的附件源）：`{work_item, verdict, tag, responsible, generated_at_utc, items:[…], next}`。顶层字段均为非空，且 `verdict`/`tag` 必须与计划一致；`items[]` 最多 3 项，元素 = `{id, question, options:[…], allow_other, responsible?, category?, primary?, why?}`，其中前四项必填、`id` 唯一、`options` 非空。执行器物化上传时把 `checklist` 序列化为附件正文（`待补充信息_<id>_<run_id>.json`）并注入运行标记。PASS 不带 `checklist`/附件。

> **分析必填字段 `analysis_description`**：仅 `auto-req-analysis` 使用，结构为 `{categories: ["report", ...]}`。`categories` 必须为非空、无重复的受控类别数组；其值必须与 `change-plan` 的“## 三、分析者描述”中需求类别行、三级标题完全一致。分析者描述还必须且只能包含一个非空 `路径` 行，格式为 `菜单路径：<业务菜单层级>；操作路径：<业务操作步骤>`；不得写技术仓库、代码符号或接口。执行器校验路径与各类别必需业务维度，并拒绝模板占位符、`TODO` 和已列明空泛话术；不按总字数判定。执行时只提取该 Markdown 章节、转义文本并渲染为基础 HTML，整段替换 `System.Description` 中 `【分析者描述】` 至 `【开发者描述】` 之间的内容；缺少、重复或顺序错误的标题会失败并停止写入。完整 `change-plan` 仍只作为 Markdown 附件上传，绝不写入 `Winning.Demand.Analysis`。类别清单与写作规则见 `../../references/analysis-description-writing-rules.md`。
> **可选字段 `analysis_profile`**：允许 `concise-v1`（历史兼容）或 `concise-v2`，且只能用于单一 `existing-ui-simple` / `print-adjustment` / `data-management` 类别。`concise-v1` 使用三项简洁维度（核心改造点 + 场景 + 不涉及范围）；新计划优先 `concise-v2`，仅强制“核心改造点”，由唯一 `路径` 承担入口与场景，不明显的边界才额外写“不涉及范围”。未声明时继续使用历史完整维度。
>
> **分析必填字段 `analysis_gaps`**：仅 `auto-req-analysis` 使用；始终为数组，无缺口时填 `[]`。每项为 `{id, topic, missing, impact, question, options, allow_other}`，只记录 PM 能补齐、且会影响业务规则、范围、计算/精度、异常处置或验收的业务信息。非空时终局必须为 `MANUAL-REVIEW` 或 `MANUAL-REVIEW-STOP`，计划必须带唯一 `manual-followup` 磁盘附件；为空时不得带该附件，即使终局为 MANUAL。附件必须逐项包含 `<!-- analysis-gap:<id> -->`，并在该条下写齐“缺失信息 / 对分析或验收的影响 / 需确认的问题 / 候选口径 / 允许自由补充”五项非空字段，且不得出现 `PM-AI-` 自动标签。标签、风险判定与状态机由执行器自动处理，不是待确认内容。

> **v2 分析必填字段 `evidence_refs` / `evidence_gaps`**：`evidence_refs` 是 `{闭环章节: ["work-item"|"kb:<索引>"|"wiki:<索引>", ...]}`，必须精确覆盖“现状基线、问题与目标、差异与范围、方案取舍、成功衡量与非目标”；KB 索引必须指向 `state=已证实` 的 finding，wiki 索引必须指向 `state=wiki-确认`（历史兼容 `已证实`）的 finding。`AUTO-ANA` 的现状基线、差异与范围、方案取舍还必须各引用至少一条 `kb:<索引>`。`evidence_gaps` 始终为数组，元素为 `{id, topic, missing, impact, owner, next_action}`；仅允许 `owner=研发|知识库治理`，只记录现有实现、相似能力、调用路径或方案取舍的证据定位缺口。非空 `evidence_gaps` 禁止 AUTO-ANA，但不生成 `manual-followup`、不写 TFS；二者均由执行器透传到运行审计。

> **可选字段 `qc_evidence_resolution`**：仅用于“初判 `NEED-REVIEW` 经 KB/wiki 补证后复核为 PASS、进入阶段二”的 v2 分析计划，结构为 `{initial_verdict:"NEED-REVIEW", post_evidence_verdict:"PASS", items:[{id, initial_gap, resolution, evidence_refs:["kb:<索引>"|"wiki:<索引>"]}]}`。`items` 非空、ID 唯一；每项引用必须指向 KB 的 `已证实` 或 wiki 的 `wiki-确认`（历史兼容 `已证实`）finding，不能使用 `work-item`、候选或推断。执行器校验后将该字段写入运行审计；它是放行理由，不生成附件、不改变阶段二终局规则。

> **计划版本与规则来源**：v2 为当前新计划版本。QC 终局为 `{"qc":"pre-qc-v1"}`；分析终局必须为 `{"qc":"pre-qc-v1","analysis":"evidence-loop-v1"}`。v1 及其 `fallback-v1` 分阶段规则源、字符串 `pre-qc-v1` / `fallback` 仅为历史回放兼容；v2 不接受旧规则源，故无法跳过证据闭环。

> **可选字段 `qc_recheck`**：仅当阶段二重新发现范围、取值口径或既有路径等 spec 歧义时填写 `analysis-spec-ambiguity`。此时终局必须回退为质控 `NEED-REVIEW` 并使用 QC inline `checklist`；不得用 MANUAL 分析计划或 `analysis_gaps` 代替。执行器将该字段透传到审计。

> **可选字段 `assignee_to`**（AUTO-ANA 自动指派·回退源）：仅 `AUTO-ANA` 计划可填，值为 `System.AssignedTo` 的写入值，**必须可解析为 `WINNING\账号` 格式**（如 `WINNING\zhang_dong`，或含 `<WINNING\account>` / `(account)` 形态）；bare display name（如「舒予」「张栋」）TFS 必报「未知标识」HTTP400，`validate` 前置拦截。由 `references/assignee-rules.md` 按 产品×版本×模块 匹配唯一开发组长得出；匹配不出（范围外产品 / 混合版本且模块不清 / 命中多人）时**省略该字段**、在分析者描述与审计写候选，**不阻断 AUTO-ANA**。
>
> **运行时指派优先级**（见 [字段流转](field-flow.md)）：`--assignee` 显式覆盖 > 工作项 `Winning.Dev.Leader` 字段（首选，TFS 已指定的开发组长）> 计划 `assignee_to`（规则提示，回退）。`Winning.Dev.Leader` 读回格式 `account(中文名) <WINNING\account>`，执行器自动提取 `WINNING\account`。指派失败（HTTP≠200）**降级记 error 动作、不中断、不回滚状态**（指派与终局解耦）。

合并后 `auto-req-analysis` skill 的合法终局按 `verdict` 分派（执行器 `expected_for` 不再按 `skill` 名区分；`skill` 字段新 run 用 `auto-req-analysis`，旧 `auto-req-qc`/`auto-req-analysis` 仅向后兼容历史计划）：

- `NEED-INFO`：标签仅 `PM-AI-QC-NEED-INFO`，`state_to=null`，附件 inline `qc-followup`（顶层 `checklist` 物化）。质控挡回，不进分析。
- `NEED-REVIEW`：标签仅 `PM-AI-QC-NEED-REVIEW`，`state_to=null`，附件 inline `qc-followup`。这是 KB/wiki 补证与全量复核后的**最终**质控挡回；初判 NEED-REVIEW 若被逐项已证实证据消除，则不生成此计划而进入分析计划并写 `qc_evidence_resolution`。
- `SKIP-ANALYSIS`：分析准入排除；`tags=[]`、`state_to=null`、`artifacts=[]`，必须带非空 `skip_reason`，且不得带 checklist、分析描述或 gaps。`apply` 只做只读预检并记录审计/Redis，动作列表为空；即使显式 `--execute` 也不得新增、删除标签或产生其它 TFS 写入。
- `AUTO-ANA`：标签仅 `PM-AI-AUTO-ANA`，`state_to` 为 `已分析`，附件仅 `change-plan`，`analysis_gaps=[]`；必须额外声明非空 `auto_scopes`，且其值只能来自 `field-ui-copy`、`unit-test-completion`、`config-enum-adjustment`。这表示全部变更都落在字段级 UI/文案、单测补齐、配置/枚举调整这三类白名单内。质控 PASS 后分析完成的自动流转终局。执行器在「加标签」后跑**完整字段流转**（见 [字段流转](field-flow.md)：写迭代路径/开始日期/完成日期 → 转「活动」→ 转「已分析」→ 指派；指派优先 `Winning.Dev.Leader`、回退可选 `assignee_to`）。
- `MANUAL-REVIEW`：标签仅 `PM-AI-MANUAL-REVIEW`，`state_to=null`；始终有 `change-plan`，仅在 `analysis_gaps` 非空时另有 `manual-followup`。
- `MANUAL-REVIEW-STOP`：在前项基础上增加 `PM-AI-STOP-AUTO`，附件规则相同。

`PASS` **不再是终态**：合并后 `PASS` 是 `auto-req-analysis` 内部阶段闸（质控通过即继续分析，不产出独立计划、不 apply）。`pipeline.py` 仍保留 `verdict=PASS` 的契约（空标签/空附件/`state_to=null`）与 `next_action` 生成，仅用于校验/回放历史 `auto-req-qc` PASS 计划；新 run 不再产出 PASS 计划。

## 完整 JSON 骨架（按 verdict · 生成计划的单一权威 schema）

> 生成 `执行计划_<id>_<run_id>.json` 时**按本节对应 verdict 的骨架填写，无需翻历史样例**。所有 verdict 共有的必填顶层字段（`validate_plan`）：`version, run_id, skill, work_item_id, expected_rev, expected_state, verdict, rules_source, tags, state_to, artifacts`。骨架中标 `可选` 的字段（`iteration`/`attachments`/`kb`/`wiki`/`assignee_to`/`audit_note` 等）按本次证据带上、`validate` 不强制。`<...>` 为占位，需替换为真实值。

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

> 本终局不得带 `checklist`、`analysis_description`、`analysis_profile`、`analysis_gaps`、`evidence_refs`、`evidence_gaps`、`auto_scopes` 或 `assignee_to`。

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
  "tags": ["PM-AI-QC-NEED-REVIEW"],
  "state_to": null,
  "rules_source": {"qc": "pre-qc-v1"},
  "generated_at_utc": "2026-07-29T02:34:45Z",
  "iteration": {"team_project": "<teamProject>", "expected_date": "<期望日期>", "pimis_priority": "<C级(一般)>", "matched": {"path": "<迭代路径>", "name": "<V6.0.x>", "start": "<ISO>", "finish": "<ISO>"}, "code_commit_cutoff": "<finish-7天>", "today": "<今日>", "timing": "正常处理", "note": "<时效判定依据>"},
  "attachments": {"ready": true, "download_dir": "附件", "downloaded": [], "parsed": [], "skipped": [], "errors": [], "note": "<附件证据摘要>"},
  "kb": {"ready": true, "tools_used": ["list_repos", "query"], "findings": [{"entity": "<命名实体>", "state": "候选", "source_tool": "query", "note": "<证据/限制>"}], "note": "<三态摘要>"},
  "checklist": {
    "work_item": "<id> <标题>",
    "verdict": "NEED-REVIEW",
    "tag": "PM-AI-QC-NEED-REVIEW",
    "responsible": "<产品 + 研发负责人 等>",
    "generated_at_utc": "2026-07-29T02:34:45Z",
    "items": [
      {"id": "q1", "priority": "P0", "category": "<规则分类>", "responsible": "<该条责任方>", "primary": true, "question": "<责任方可直接回答的问题>", "why": "<为何需要>", "options": ["<推荐选项1>", "<推荐选项2>"], "allow_other": true}
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

> `checklist.items[]` 必填字段（`validate_checklist`）：`id`（稳定字母开头、唯一）、`question`（非空）、`options`（非空字符串数组）、`allow_other`（布尔）；可选：`priority`/`category`/`responsible`/`primary`/`why`。`checklist.verdict`/`tag` 必须与计划一致；`items` 不可为空。`qc-followup` 为 inline 附件——只带 `filename`、无 `path`，正文由执行器从 `checklist` 物化并注入运行标记。

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
  "state_to": "已分析",
  "tags": ["PM-AI-AUTO-ANA"],
  "rules_source": {"qc": "pre-qc-v1", "analysis": "evidence-loop-v1"},
  "auto_scopes": ["field-ui-copy"],
  "assignee_to": "WINNING\\<账号>",
  "analysis_description": {"categories": ["existing-ui-simple"]},
  "analysis_profile": "concise-v2",
  "analysis_gaps": [],
  "evidence_gaps": [],
  "evidence_refs": {
    "现状基线": ["work-item", "kb:0"],
    "问题与目标": ["work-item"],
    "差异与范围": ["work-item", "kb:0"],
    "方案取舍": ["kb:0"],
    "成功衡量与非目标": ["work-item"]
  },
  "kb": {"ready": true, "dedup_ran": true, "tools_used": ["list_repos", "query", "context"], "findings": [{"entity": "<已证实实体>", "state": "已证实", "source_tool": "query+context"}], "note": "<查重与定位摘要>"},
  "wiki": {"ready": true, "modules_matched": ["<模块>"], "findings": [{"entity": "<业务语义>", "state": "wiki-确认", "source": "<wiki路径>"}], "note": "<wiki 佐证摘要>"},
  "iteration": {"team_project": "<teamProject>", "expected_date": "<期望日期>", "matched_iteration": "<V6.0.x>", "timing": "正常处理", "pimis": "C级"},
  "attachments": [{"name": "<附件名>", "source": "<tfs|external>", "parsed": true, "parse_method": "<visual|...>", "note": "<证据摘要>"}],
  "artifacts": [
    {"kind": "change-plan", "path": "变更方案_<id>_<run_id>.md"}
  ],
  "generated_at": "2026-07-29T10:03:19Z",
  "audit_note": "<QC=PASS... → 分析 AUTO-ANA(...)>"
}
```

> AUTO-ANA 硬约束：`analysis_gaps=[]` 且 `evidence_gaps=[]`；`auto_scopes` 非空、且为 `field-ui-copy`/`unit-test-completion`/`config-enum-adjustment` 子集；`evidence_refs` 的 现状基线/差异与范围/方案取舍 各至少一条 `kb:<已证实索引>`；`kb.ready=true` 且 `kb.dedup_ran=true`（优化类类别还须 `kb.findings` 含 ≥1 条 `state=已证实`）；`assignee_to` 匹配不出唯一人时**省略**（运行时优先 `Winning.Dev.Leader`）。

### MANUAL-REVIEW / MANUAL-REVIEW-STOP（分析·人工复核 · v2）

MANUAL-REVIEW-STOP = 在 MANUAL-REVIEW 基础上 `tags` 追加 `PM-AI-STOP-AUTO`（命中 6 类高风险）。`analysis_gaps` 为空时**不带** `manual-followup`；非空时必须带唯一 `manual-followup`。

```json
{
  "version": 2,
  "run_id": "<8-80位>",
  "skill": "auto-req-analysis",
  "work_item_id": 259735,
  "expected_rev": 1,
  "expected_state": "已建议",
  "verdict": "MANUAL-REVIEW",
  "tags": ["PM-AI-MANUAL-REVIEW"],
  "state_to": null,
  "rules_source": {"qc": "pre-qc-v1", "analysis": "evidence-loop-v1"},
  "analysis_description": {"categories": ["<受控类别>"]},
  "analysis_gaps": [
    {"id": "g1", "topic": "<业务主题>", "missing": "<缺失的业务信息>", "impact": "<对分析/验收的影响>", "question": "<需确认的问题>", "options": ["<候选口径1>"], "allow_other": true}
  ],
  "evidence_gaps": [],
  "evidence_refs": {"现状基线": ["work-item"], "问题与目标": ["work-item"], "差异与范围": ["work-item"], "方案取舍": ["work-item"], "成功衡量与非目标": ["work-item"]},
  "kb": {"ready": true, "dedup_ran": true, "tools_used": ["list_repos", "query"], "findings": [{"entity": "<实体>", "state": "已证实", "source_tool": "query"}], "note": "<摘要>"},
  "iteration": {"team_project": "<teamProject>", "expected_date": "<期望日期>", "matched_iteration": "<V6.0.x>", "timing": "正常处理", "pimis": "C级"},
  "attachments": [{"name": "<附件名>", "source": "tfs", "parsed": true, "note": "<证据摘要>"}],
  "artifacts": [
    {"kind": "change-plan", "path": "变更方案_<id>_<run_id>.md"},
    {"kind": "manual-followup", "path": "待确认清单_<id>_<run_id>.md"}
  ],
  "generated_at": "2026-07-29T03:14:12Z",
  "audit_note": "<分析 MANUAL-REVIEW(...)>"
}
```

> `analysis_gaps[]` 必填字段（`validate_analysis_gaps`）：`id`/`topic`/`missing`/`impact`/`question`/`options`/`allow_other`；非空仅允许 MANUAL-REVIEW(-STOP)。`evidence_gaps[]`（`validate_evidence_gaps`）：`id`/`topic`/`missing`/`impact`/`owner`（研发|知识库治理）/`next_action`；不生成附件、不写 TFS。`change-plan`/`manual-followup` 的 `path` 必须是计划同目录精确文件名。

## 结果存储（Redis·可选）

`apply` 成功后（dry-run 与 execute 都会）自动把执行结果摘要写入 Redis Hash，按 collection + 工作项覆盖式存最新，供 TFS-BUDDY / DeerFlow / 看板 / 调试用 `HGETALL` 一步查询。纯标准库 socket（`skills/reg-auto-req-analysis/_lib/tfs/redis_client.py`），**连不上或写入失败自动降级**（`redis.ok=false` + reason，记入审计 extra 与 `apply` 返回），**绝不阻断 apply、绝不回滚已写的 TFS**。

- **key**：`auto-req:qc:plan:<collection>:<work_item_id>`；索引 `auto-req:qc:ids:<collection>`。collection 原样取本次最终生效的 TFS collection（`--collection` / `TFS_COLLECTION` / 配置默认值）。
- **字段**：固定摘要为 `run_id / verdict / tags / state_to / generated_at_utc / run_mode`；`SKIP-ANALYSIS` 额外保存 `skip_reason`；仅 `NEED-INFO`、`NEED-REVIEW` 额外有：顶层 `work_item`（`<id> <标题>`）与 `next`（责任方补充后的重触发说明），以及 `checklist`（**仅 `{responsible, items}` 投影**）。终局覆盖同一 Hash 时清理不再适用的 `checklist`/`work_item`/`next`/`skip_reason`。完整 checklist 见 plan JSON / TFS 附件 / 审计；不存 `plan_json`、本地绝对路径或可由审计取得的重复数据。
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
