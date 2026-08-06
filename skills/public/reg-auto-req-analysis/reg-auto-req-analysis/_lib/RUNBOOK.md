# 运行手册（reg-auto-req-analysis）

本文件承载工作项处理的详细顺序。硬约束以 [`../SKILL.md`](../SKILL.md) 为准，命令签名以 [`COMMANDS.md`](COMMANDS.md) 为准；两者冲突时立即停止并记录问题，不自行选择旁路。

## 目录

- 0–3：启动、读取工作项、准入、附件
- 4–5：质控初判、补证与终局
- 6–9：分析证据、方案、终局与计划
- 10–11：校验执行、测试日志与重分析诊断记录
- 分支动作速查

## 0. 启动

1. 从仓库根目录完整读取 `SKILL.md`、`_lib/COMMANDS.md` 和本文件。
2. 执行 `tfs_client.py precheck`，必须返回 `ok:true`。
3. 生成唯一 `run_id`，创建 `过程文件/<id>/<run_id>/`。一个 run_id 贯穿质控、分析、计划、执行和日志。
4. 检查当前会话是否注入 `tfs-requirements`、`gitnexus-team`、`db-knowledge` 三个 MCP 服务的原生工具；任一缺失立即在对应审计字段标 `ready=false` 并记原因（如"tfs-requirements MCP 未注入本会话，需重启会话"），按 `_lib/knowledge-base.md` §三降级原则继续，**不等到步骤 4/6 才发现 MCP 缺失**。MCP 工具在会话启动时注入、当前会话不热加载，新装的 MCP 须重启会话再生效。

## 1. Fetch 工作项

执行 `tfs_client.py fetch <id>`，只从返回的 `workItem` 读取 title、state、description、analyzerDesc、tags、rev、teamProject、area 等当前字段；禁止凭记忆补字段。需求历史 MCP 即使返回同一工作项，也只能补充历史背景、关系和变更原因，不得覆盖本次 fetch。

**重跑检测与上轮加载（重分析诊断前置）**：fetch 后按 [`../references/round-diagnosis-rules.md`](../references/round-diagnosis-rules.md) §1/§2 处理。先按 §1 解析调用入参（优先 JSON，取 `work_item_id` + 可选 `human_feedback`/`additional_info`；连接字段 `collection_name`/`tfs_project`/`tfs_pat` 交给运行时/命令行；否则裸 id + 文本兜底）；再列 `过程文件/<id>/` 下除 `runs/` 外的子目录并按时间戳排序。仅当存在更早的**分析轮**（其计划 `verdict ∈ {AUTO-ANA, MANUAL-REVIEW, MANUAL-REVIEW-STOP}`）**且** `human_feedback`/`additional_info` 非空时，加载该上轮的 `执行计划_*.json`/`变更方案_*.md`/`待确认清单_*.md`，读 §3 分类法完成诊断（类别 + 证据 + 改进指向），置 `round_diagnosis=pending` 并把结论作为步骤 6–8 的上下文（须规避重犯，见该文件 §5）。任一门控不满足则记 `round_diagnosis=skipped:<no_prior_round|prior_not_analysis|no_supplement>` 并跳过，不影响后续步骤。诊断的**记录**在步骤 11 完成。

## 2. 先做准入路由

读取 `config/qc-rules.md` §1b/1c 和 `references/pre-qc-rules.md` §1b。只有“接口已开发、本次纯联调/调试/现场配合”且不改变业务规则、字段映射、接口契约、异常处置或验收逻辑时，判 `SKIP-ANALYSIS`。

- SKIP 计划使用空 `tags`、空 `artifacts`、`state_to=null` 和非空 `skip_reason`，不下载附件、不跑需求历史或 KB、不生成清单或分析者描述。
- 既有功能结果错误、为空或条件分支不一致属于 BUG 语义，不能因标题写“优化/调整”而跳过。

## 3. 处理来源附件

未命中 SKIP 时读取 `_lib/attachment-evidence.md`，使用 `download-attachments --include-external` 下载到 `附件/`，随后只调用 `attachment_runtime.py convert`。运行器按实际格式创建或复用隔离环境、自动补齐允许列表依赖，再把全部逐文件结果写入 `附件解析/`；不得因某个 Office 依赖缺失而跳过 PDF、文本、图片或其它可解析文件。

- 只有白名单 HTTPS ERP 外链可使用独立认证；绝不向外部发送 TFS PAT。
- 只有成功解析的内容参与判断；未解析、跳过或错误项只记录限制，不能假称已读。
- 安装只能由官方运行器执行，模型不得手工运行包管理器；安装与版本写入 `attachments.preflight`。
- 单个损坏、加密、超限、安装失败或明确不支持的附件按结果降级并记录；正文明确把该文件作为范围、规则或验收来源时标记 `blocking=true`，全部官方链失败后再形成质控缺口。
- 附件取证没有结束前不得生成终局计划、validate 或 dry-run，避免同一 `run_id` 产生中间结论。

## 4. 质控初判与补证

1. 读取 `config/qc-rules.md`、`references/pre-qc-rules.md`；未覆盖的通用完整性问题用 `references/qc-checklist.md` 核对。
2. 先检查需求类型、PIMIS、期望日期、项目归属和治标警惕。SKIP、NEED-INFO 及范围/时效/方向根因等客观硬阻断直接早退。
3. 时效实查使用 `list-iterations --project <workItem.teamProject> --expected-date <date>`；查询失败交排期方，不臆测。
4. 只有判定面需要、需要历史背景/关联项/验收来源，或初判 NEED-REVIEW 可由既有事实消除时，才读取知识库资料并运行发现链。顺序为当前工作项与附件 → `tfs-requirements` 需求历史 → 菜单索引/wiki → GitNexus → 按需 db-knowledge；纯文字、无业务实体且非高风险的需求主动跳过。
5. 需求历史先用 `get_requirements_summary` 固定覆盖范围；显式关系用 `get_related_work_items`，检索结果用 `search_requirements` 收敛后必须经 `get_work_item` 读取完整正文/验收才可标为已证实。**查重须按 `references/qc-kb-recipe.md` Step R 执行多组关键词 + 限定 `state`∈{已验证,已关闭} + get_work_item 核验 + 按 `changed_date` 倒序复查（勿漏最新条目；锚点仍按“最直接覆盖本次诉求”选定，不为“新”而牵强），缺一不可；命中须标 `maturity`（设想/分析确认/已落地，定义见 `_lib/knowledge-base.md` §六），只有 `maturity=已落地` 才算历史方案已落地，`maturity=设想` 不得当已落地依据。**历史相似项在质控阶段只补充清单和背景（查重质控仍为预留，不单独改变质控 verdict）；分析阶段命中 `maturity=已落地` 且能力覆盖本次诉求时，须按“功能已存在”判 MANUAL-REVIEW 并声明 `existing_feature.satisfied=true`（见 Step 8 与 `references/confidence-heuristic.md` 第 4 项）。
6. 初判 NEED-REVIEW 仅在每个阻断项都被已证实 `kb:`/`wiki:` finding 直接闭合时才能转 PASS。`req:` 只能说明历史需求事实，不能进入 `qc_evidence_resolution`。逐项写“规则/触发条件/范围 → 证据事实 → 放行结论”；候选、名称相似、局部代码存在或未读附件不能放行。

## 5. 结束质控阶段

- `NEED-INFO` / `NEED-REVIEW`：按执行契约生成含顶层 `checklist` 的 QC 计划；inline `qc-followup` 只有 `filename`、没有 `path`，`state_to=null`。随后进入步骤 10，完成后停止。
- `PASS`：不生成 QC 计划、不 apply，直接进入分析。
- 初判 NEED-REVIEW 经补证转 PASS：分析计划必须保留 `qc_evidence_resolution`，每个引用只能指向已证实 KB/wiki finding。

## 6. 建立分析证据闭环

读取分析规则、迭代闭环和知识库规范。顺序为需求历史 MCP（按需补背景/关系/验收）→ 菜单索引 → wiki 业务语义 → GitNexus `list_repos` → `query` → `context` → 有精确 API 时 `route_map` → `context`；数据库图谱只有在已有代码锚点后按需使用。

**界面/布局类需求的 wiki 优先**：需求涉及界面展示/布局/增减字段或交互时，wiki 的界面布局描述是现状基线首选交叉证实源（见 `references/iteration-analysis-closure.md` §1、`wiki-kb-recipe.md` §1），不得因字典未命中而跳过。

- `search_requirements` 结果只算候选；`get_work_item` 或 `get_related_work_items` 确认的历史事实可写 `tfs_requirements.findings`（须标 `maturity`），但不证明当前实现或已上线——`state=已证实` 只表事实核实，是否已落地由 `maturity=已落地` 判定（见 `_lib/knowledge-base.md` §六）。
- 历史需求与实时 fetch、现场事实或当前代码冲突时保留冲突并形成缺口，不自行选择历史版本。
- 只有“对象与动作匹配 + 可追踪入口 + 明确关系”同时成立才记为已证实。
- `processes`/`route_map` 为空是覆盖缺口，不代表模块不存在。
- 候选源码必须再与部署版本、菜单/路由、现场截图或受控页面观察之一闭合，否则只能写候选。
- 字段或打印取值异常要枚举条件分支与来源；同名字段不能证明唯一数据源。
- 技术定位只进内部证据，不写进 PM 变更方案正文。

## 7. 生成变更方案并分流缺口

按“现状基线（界面类须交叉证实）→ 问题与目标（含痛点核对与需求方方案合理性评估）→ 差异与范围 → 方案取舍 → 成功衡量与非目标”完成闭环，再按写作规则和模板生成 `变更方案_<id>_<run_id>.md`。

- `路径` 同时写菜单路径与操作路径；类别、改动点、风险和验收保持一致。
- 呈现类缺口使用合理默认并写入假设，不进入 `analysis_gaps`；金额、计算精度、业务规则、异常、合规、权限等正确性缺口才进入 `analysis_gaps`。
- 技术实现、代码定位和证据发现缺口只进入 `evidence_gaps`。
- 再次发现 spec 歧义或需求方向/根因存疑时，回退 QC `NEED-REVIEW`，不以 MANUAL 或 analysis gaps 代替。
- 每个原始入口、触发阶段、条件和期望动作必须在方案或“不涉及范围”有唯一落点；复杂状态需求补齐状态×事件×历史状态×动作×验收矩阵。
- 全量加载、跨分页排序、批量导入、弹窗、异步或跨页面联动必须核实数据范围、发生层、组件能力和真实触发时机，证据不足时列候选与取舍。
- **需求方方案合理性评估（必做）**：现状基线和痛点核对之后、方案取舍之前，对每个需求点判断合理性与必要性（见 `references/iteration-analysis-closure.md` §2）；**方向/位置/范围存疑→回退 `NEED-REVIEW`（产品确认方向）；仅实现细节存疑→`analysis_gap` + MANUAL-REVIEW**，不得把方向问题降级为细节 gap，不照搬需求方方案。

## 8. 判定分析终局

先检查六类高风险：患者安全、支付结算、数据迁移、首次发布新模块、合规强监管、架构级改动。命中即 `MANUAL-REVIEW-STOP` + `PM-AI-STOP-AUTO`，覆盖所有 AUTO 启发式。

未命中高风险时，只有全部变更落入 AUTO 白名单，且改动可枚举、正确性验收明确、改动量小、相似需求证据满足规则，才判 `AUTO-ANA`；任一不满足判 `MANUAL-REVIEW`。

## 9. 生成唯一执行计划

严格按 `_lib/tfs/EXECUTION_CONTRACT.md` 对应终局骨架生成计划，不翻历史样例。

- 新分析计划固定 `version=2`、`rules_source.analysis=evidence-loop-v1`，并带 `expected_rev`、`expected_state`、`analysis_description`、`analysis_gaps`、`evidence_refs`、`evidence_gaps`。
- 新分析计划统一使用 `analysis_profile=concise-v3`，按类别输出 1–3 行有效业务结论；`concise-v1` / `concise-v2` 与无 profile 的完整格式只兼容历史计划。
- AUTO 的 `analysis_gaps`、`evidence_gaps` 均为空，`auto_scopes` 非空；现状基线、差异与范围、方案取舍各至少引用一条已证实 `kb:` finding。
- 已证实历史需求可用 `req:<索引>` 补充 `evidence_refs`；它不能替代 AUTO 要求的 `kb:` 引用，也不能用于 `qc_evidence_resolution`。
- `analysis_gaps` 非空时使用 MANUAL 终局并生成唯一 `manual-followup`；为空时不得生成该附件。
- `assignee_to` 只在 AUTO 唯一匹配时填写 `WINNING\账号`；匹配不出则省略，不阻断 AUTO。

## 10. 校验与执行

1. 先运行 `pipeline.py validate --plan <plan>`。
2. 再运行 `pipeline.py apply --plan <plan>`；缺省只 dry-run。
3. 只有 TFS-BUDDY/DeerFlow 等受控编排器明确授权时才增加 `--execute`。对话用户的普通请求不是 execute 授权。
4. 执行后按需要重新 fetch，核对 revision、状态、标签和字段；不能把命令成功等同于分析正确。

pipeline 自动发布 Redis 摘要；不得直接调用 Redis 写命令。Redis 不可用只记录降级，不阻断或回滚 TFS 动作。

## 11. 追加测试日志与重分析诊断记录

每次 validate、dry-run、execute、TFS 回读或环境阻断后，按 `_lib/testing-log.md` 向 `过程文件/测试日志.md` 追加一条记录，不覆盖历史。记录需求号、run_id、质控结果、终局、标签、证据边界、待确认信息、分析者描述、时间、产物和异常；未进入分析时分析者描述写“不适用”。

**重分析诊断记录（仅 `round_diagnosis=pending` 时）**：在本轮分析/计划完成后，按 [`../references/round-diagnosis-rules.md`](../references/round-diagnosis-rules.md) §4 向 `过程文件/skill-feedback.md` 追加一条（首次创建时先写文件头）。填齐"上轮 run_id / 本轮 run_id / 上轮终局 / 诊断类别 / 证据 / 改进指向 / 本轮是否已据此修正"，追加后置 `round_diagnosis=done`。`skipped:*` 或首轮不写该文件；该文件不进 TFS、不入计划 JSON。

## 分支动作速查

| 终局 | 标签 | 状态与附件 |
|---|---|---|
| `SKIP-ANALYSIS` | 无 | `state_to=null`，无附件、无 TFS 写动作 |
| `NEED-INFO` | `PM-AI-QC-NEED-INFO` | `state_to=null`，inline QC checklist 指向实施/创建者 |
| `NEED-REVIEW` | `PM-AI-QC-NEED-REVIEW` | `state_to=null`，inline QC checklist 指向责任方 |
| `AUTO-ANA` | `PM-AI-AUTO-ANA` | change-plan；完整字段流转到「已分析」；指派失败可降级 |
| `MANUAL-REVIEW` | `PM-AI-MANUAL-REVIEW` | `state_to=null`；change-plan；仅 gaps 非空时有 manual-followup |
| `MANUAL-REVIEW-STOP` | `PM-AI-MANUAL-REVIEW` + `PM-AI-STOP-AUTO` | `state_to=null`；其余同 MANUAL |

任何异常都立即停止。若 `write-field`/`set-state` 失败，记录 ERROR，不强转；并发 revision 冲突重新 fetch 后重建计划，不复用旧计划盲重试。
