# 重分析诊断规则（round-diagnosis）

> 单一权威源：同一工作项**重跑**时，依据编排器传入的"上轮补充信息"，自动诊断上一轮分析为何不好、归类并记录到 `过程文件/skill-feedback.md`，供 skill 迭代。
> 触发与落位见 [`../_lib/RUNBOOK.md`](../_lib/RUNBOOK.md)（步骤 1 检测/加载、收尾区诊断/记录）；硬约束见 [`../SKILL.md`](../SKILL.md)。

## 0. 性质与边界

- **全程非交互**：补充信息由编排器（TFS-BUDDY/DeerFlow）或人工随调用参数传入，**绝不**用 `AskUserQuestion`、不追问、不等确认。
- **零 TFS 写入、零 pipeline.py 改动**：诊断纯属 skill 侧判断，只追加到 `过程文件/skill-feedback.md` 这一份 markdown 副记录；不进计划 JSON、不进 `run_*.json` 审计白名单、不写 TFS、不生成附件。
- **向后兼容**：首轮或重跑未带补充信息时该步骤直接跳过，不影响任何既有终局/契约。
- **聚焦"上轮分析不好"**：只对上轮产出了分析产物（`verdict ∈ {AUTO-ANA, MANUAL-REVIEW, MANUAL-REVIEW-STOP}`）的轮次诊断；上轮为 `SKIP-ANALYSIS`/`NEED-*`（QC 挡回、无分析产物）在 v1 不诊断。

## 1. 补充信息入口契约

入参**直接传 JSON**（不套 `supplement` 外层）。两种写法等价，任选其一：

**写法一（slash 命令常用）**：`<work_item_id>` 在前，后接可选的补充 JSON（**不含** `work_item_id`）：
```
/reg-auto-req-analysis 258829 {"human_feedback":[{"question_id":"q1","question":"...","answer":"..."}],"additional_info":"..."}
```

**写法二（编排器 /evaluation 请求体常用）**：一个完整 JSON，`work_item_id` 在内：
```
{
  "collection_name": "WN_PH-Platform", "work_item_id": 258829, "tfs_project": "NETHIS5.5", "tfs_pat": "<PAT>",
  "human_feedback": [ {"question_id": "q1", "question": "...", "answer": "..."}, … ],
  "additional_info": "自由文本"
}
```

补充信息分两类：
- **A｜`human_feedback`**：新策略下对上轮 QC `checklist.items` 逐条回答；历史计划兼容 `待确认清单`/`analysis_gaps`。每项 `{question_id, question, answer}`；`question_id` 对齐稳定 item/gap id（如新策略 `q-scope`/历史 `g1`，作主键），`question` 为上轮原问题文本、自描述兜底，`answer` 成对。QC 回答继承与禁止重复询问由 RUNBOOK 步骤 1 执行，不要求上轮必须已有分析产物。
- **B｜`additional_info`**：超出上轮所问的额外反馈/纠正（如"分析者描述格式不满意"）。
- `collection_name`/`tfs_project`/`tfs_pat` 属连接参数，由运行时注入环境变量，**不参与 A/B 诊断**。

- `work_item_id` 必填；`human_feedback`/`additional_info` 均可选，重跑带补充时才出现。
- 有 `human_feedback` 时直接按字段分 A/B；**只有 `additional_info` 整段文本、无 `human_feedback`** 时，按 §2 加载的上轮待确认清单匹配分区（对得上→A，对不上→B，不强行归 A）。
- 首轮（无上轮）或两者均空 → 补充为空 → **跳过诊断**。
- **裸 id 简写**：`258829`（非 JSON、后无补充 JSON）= 仅 `work_item_id`、无补充。

> 该契约对人工 `/reg-auto-req-analysis` 与编排器重触发统一适用。编排器侧（FastAPI `/evaluation` 请求体）如何承载该 JSON 属下游接线（见接口文档 §11 TBD），本规则只定义 skill 侧解析。

**解析（步骤 1）**，按优先级：
1. 整段入参为 JSON 对象且含 `work_item_id` → 写法二：取 `work_item_id` + 可选 `human_feedback`/`additional_info`（连接字段 `collection_name`/`tfs_project`/`tfs_pat` 交给运行时/命令行，不进诊断）。
2. 否则取首个连续数字段为 `work_item_id`；剩余文本 trim 后：
   - 为空 → 无补充（裸 id）；
   - 是 JSON 对象 → 写法一：取其 `human_feedback`/`additional_info`；
   - 否则 → 整段并入 `additional_info`（文本兜底）。

## 2. 重跑检测与上轮加载（步骤 1 尾部）

1. 列 `过程文件/<id>/` 下除 `runs/` 外的子目录，按目录名时间戳排序。存在更早子目录 → 判定**重跑**。
2. **上轮** = 紧邻本轮之前、且其 `执行计划_<id>_<prev_run>.json` 的 `verdict ∈ {AUTO-ANA, MANUAL-REVIEW, MANUAL-REVIEW-STOP}` 的那一轮（即产出了 `变更方案`/`分析者描述`）。找不到满足条件的上轮 → 跳过诊断。
3. 加载上轮产物用于对照：
   - `执行计划_<id>_<prev_run>.json`：`verdict`、`analysis_description`、`analysis_gaps`、`evidence_refs`、`evidence_gaps`、`auto_scopes`、`existing_feature`；
   - `变更方案_<id>_<prev_run>.md`：业务结论与方案行；
   - `待确认清单_<id>_<prev_run>.md`（若有）：上轮向 PM 提的问题（含 topic/问题文本，是 §1 文本兜底 A/B 分区的"尺子"；结构化形式自带 question，无需靠它查问题）。

**门控（跳过诊断的条件，审计记 `round_diagnosis=skipped:<reason>`）**：

| 条件 | reason |
|---|---|
| 无更早子目录（首轮） | `no_prior_round` |
| 有更早轮但均无分析产物（上轮被 QC 挡回/SKIP） | `prior_not_analysis` |
| 重跑但补充信息为空 | `no_supplement` |

命中任一即跳过，不写 `skill-feedback.md`。否则置 `round_diagnosis=pending`，按 §3 完成 A/B 分区与分类诊断（结论作步骤 6–8 上下文），记录在步骤 11。

## 3. 诊断分类法（扩展 6 类，可多选）

先按 §1/§2 把补充信息分到 **A（回答了上轮确认项）/ B（额外增加的信息）**，再对照"上轮产物"判定命中的类别。**一次诊断可命中多类**；每个命中类别必须带**证据**（引用上轮产物具体片段 + 用户补充要点，并标注该要点来自 A 还是 B）和**改进指向**（具体规则文件 + 章节）。

| 类别 | 识别信号（用户补充 vs 上轮输出） | 改进指向（待改规则文件） |
|---|---|---|
| **信息判断错误** | 补充**纠正**了上轮变更方案的某个业务结论；或指出上轮照搬实施预设未质疑 / 臆造维度参数 / 高风险类别误判(边界泛化) / 查重误判(功能已存在 vs 新增) | [`analysis-description-writing-rules.md`](analysis-description-writing-rules.md) §一(批判审视)、[`../_lib/high-risk-categories.md`](../_lib/high-risk-categories.md)、[`../config/analysis-rules.md`](../config/analysis-rules.md)(查重/现有功能) |
| **输出质量不好** | 补充针对**产物本身**：分析者描述格式不满意 / 排版啰嗦重点不突出 / 维度凑数 / 待确认发散超纲 | [`change-plan-template.md`](change-plan-template.md)(排版规范)、[`analysis-description-writing-rules.md`](analysis-description-writing-rules.md)(维度)、[`manual-review-template.md`](manual-review-template.md)(收敛) |
| **信息不足·合理gap** | 补充属于 **A**、**仅补全上轮 flagged 的缺失信息且未纠正其前提**，上轮挡回/挂 MANUAL 合理 → **非 skill 缺陷** | 无 skill 改动；记录"需求信息缺失" |
| **准入错误** | 补充揭示本属联调/支持单/纯 BUG 却被纳入分析，或反之被误 `SKIP-ANALYSIS` | [`../config/qc-rules.md`](../config/qc-rules.md) §1b/1c、[`pre-qc-rules.md`](pre-qc-rules.md) |
| **规则/KB缺口** | 补充提供的事实**无任何规则文件覆盖**；或 KB/wiki 未覆盖致证据不足；或规则 `fallback` | 命中的具体规则文件 / [`../_lib/knowledge-base.md`](../_lib/knowledge-base.md) / [`../_lib/wiki-kb-recipe.md`](../_lib/wiki-kb-recipe.md) |
| **其他** | 工具/环境阻断、附件公网外链未解析等执行层问题 | [`../_lib/attachment-evidence.md`](../_lib/attachment-evidence.md) 等 |

判定要点：
- **A/B 只是来源标签，不等于缺陷与否**：A 的回答若**纠正了上轮错误前提**（如 skill 基于错误假设发问、照搬治标方案——例：问"上限 36 还是 48"，PM 答"根本不该加上限"），仍判"信息判断错误"，不算合理 gap；只有"纯补全 flagged 缺口、不动前提"的 A 才是非缺陷。B（额外信息）多指向缺陷，按信号归入对应类别。
- "信息不足·合理gap" 显式标记为**非缺陷**，避免和真缺陷混在一起污染改进队列。
- 无法确定具体类别时落"其他"，并在证据里写明待查点，不得强行归类。
- 改进指向必须落到**具体文件 + 章节**，便于维护者直接定位改动点。

## 4. 记录：`过程文件/skill-feedback.md`

- 位置：`过程文件/skill-feedback.md`（与 `过程文件/测试日志.md` 同级）。
- 性质：**append-only**、**不进 TFS、不入 Git**（运行产物，本机审计）。
- 每次重分析诊断追加**一条**，键 = `需求号 + 本轮 run_id`；重跑开新条目，不覆盖历史（与测试日志同构）。
- 首次诊断时创建文件并写表头说明（见下方"文件头"）。
- 诊断在收尾区（步骤 11 测试日志前后）执行并写入；置 `round_diagnosis=done`。

**文件头（首次创建时写一次）**：

```md
# skill 反馈与改进记录

> 每次重分析诊断自动追加一条；不进 TFS、不入 Git。
> 维护者：按"诊断类别 + 改进指向"分组即可得到数据驱动的 skill 改进 backlog。
```

**条目模板**：

```md
## <诊断时间> · 重分析诊断 · <需求号>
- 上轮 run_id / 本轮 run_id / 上轮终局
- 诊断类别（可多选）：信息判断错误｜输出质量不好｜信息不足·合理gap｜准入错误｜规则/KB缺口｜其他
- 证据：
  - 上轮输出问题点：<引用 变更方案/分析者描述/待确认清单 具体片段>
  - 用户本轮补充要点：<来自编排器传入的补充信息，按 A（回答确认项）/ B（额外）标注来源>
- 改进指向：<待改规则文件 + 章节>（"信息不足·合理gap" 写"非 skill 缺陷，需求信息缺失"）
- 本轮是否已据此修正：是/否 + 简述
- 备注（可选）
```

- "本轮是否已据此修正"：在本轮分析已完成后填写；如本轮已针对该诊断规避/修正写"是 + 简述"，否则"否"。
- 时间戳取北京时间；同一 `work_item_id + run_id` 只一条，重跑开新条目。

**可沉淀类别的经验蒸馏（与 `经验记忆.md` 联动）**：当本次诊断命中可沉淀类别——`信息判断错误`/`输出质量不好`/`准入错误`/`规则·KB缺口` 之一时，除按上述模板写 `skill-feedback.md` 外，**另**按 [`../_lib/skill-memory.md`](../_lib/skill-memory.md) §3/§4 把"改进指向"蒸馏为一条祈使句经验条目，追加到 `过程文件/经验记忆.md`（RUNBOOK 步骤 11 执行）。`信息不足·合理gap`（非缺陷）与 `其他`（执行层/环境）**不沉淀**。此联动不改变本节上述任何字段或触发逻辑，仅追加一份平行产出；`round_diagnosis=pending` 门控与 `done` 置位仍由本节既有规则决定。

## 5. 与本轮分析的关系（轻量、非强制）

诊断结论同时作为本轮分析的上下文：在步骤 6–8（分析）中，若上轮诊断为某类，本轮**须规避重犯**（如上轮"输出质量不好-分析者描述格式"，本轮重点守 [`analysis-description-writing-rules.md`](analysis-description-writing-rules.md) 的维度/单行格式；上轮"信息判断错误-照搬预设"，本轮必做批判审视）。

- 这是**提示性**上下文，**不新增终局分支、不改 verdict 映射、不绕过任何硬约束**。
- 诊断本身的成立不依赖本轮结果；"本轮是否修正"只是记录字段。
- 启动时读入的 `过程文件/经验记忆.md` 经验先验与本节诊断上下文**同构**（均提示性、不新增终局分支、不改 verdict 映射）；memory 中由既往同类诊断蒸馏来的条目，本轮一并规避。
