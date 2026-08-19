# 运行手册（reg-auto-req-analysis）

本文件承载工作项处理的详细顺序。硬约束以 [`../SKILL.md`](../SKILL.md) 为准，命令签名以 [`COMMANDS.md`](COMMANDS.md) 为准；两者冲突时立即停止并记录问题，不自行选择旁路。

## 目录

- 0–3：启动、读取工作项、准入、附件
- 4–5：质控初判、补证与终局
- 6–9：分析证据、方案、终局与计划
- 10–11：校验执行、重分析诊断、经验处理与测试日志
- 分支动作速查

## 0. 启动

1. 从仓库根目录完整读取 `SKILL.md`、`_lib/COMMANDS.md` 和本文件。此外尽力读取 `过程文件/经验记忆.md` 作经验先验（见 [`_lib/skill-memory.md`](skill-memory.md)）：该文件不存在或为空属正常（首轮无此文件），仅记 `memory=absent` 并继续，**不触发失败关闭**（不在上一句"任一文件不可读→失败关闭"范围内）；读到的条目作为提示性先验在步骤 1–11 适用，不新增终局分支、不改 verdict 契约。
2. 执行 `tfs_client.py precheck`，必须返回 `ok:true`。
3. FastAPI 收到新的业务触发后调用 `pipeline.py init-run --id <id>`，只使用返回的唯一 `run_id`、运行目录和回执；该 ID 同时作为 FastAPI session ID、DeerFlow/LangGraph thread ID 和 skill 上下文。请求体不得传入、自造或复用旧 ID；同一次 HTTP 投递重试由稳定 `Idempotency-Key` 返回原 run，新业务重分析必须使用新 key 并重新 init。一个 run_id 贯穿本轮质控、分析、计划、执行、状态和日志；同一冻结计划的 dry-run、授权 execute 与幂等续跑继续复用。
4. 启动阶段不预设或探测全局产品 MCP；产品必须在 fetch 并排除 SKIP 后由 Area 唯一路由。MCP 工具仍在会话启动时注入、当前会话不热加载，新装的 MCP 须重启会话再生效。

## 1. Fetch 工作项

执行 `tfs_client.py fetch <id>`，只从返回的 `workItem` 读取 title、state、description、analyzerDesc、tags、rev、teamProject、area 等当前字段；禁止凭记忆补字段。需求历史 MCP 即使返回同一工作项，也只能补充历史背景、关系和变更原因，不得覆盖本次 fetch。

**重跑检测与上轮加载（重分析诊断前置）**：fetch 后按 [`../references/round-diagnosis-rules.md`](../references/round-diagnosis-rules.md) §1/§2 处理。先按 §1 解析调用入参（优先 JSON，取 `work_item_id` + 可选 `human_feedback`/`additional_info`；连接字段 `collection_name`/`tfs_project`/`tfs_pat` 交给运行时/命令行；否则裸 id + 文本兜底）；再列 `过程文件/<id>/` 下除 `runs/` 外的子目录并按时间戳排序。仅当存在更早的**分析轮**（其计划 `verdict ∈ {AUTO-ANA, MANUAL-REVIEW, MANUAL-REVIEW-STOP}`）**且** `human_feedback`/`additional_info` 非空时，加载该上轮的 `执行计划_*.json`/`需求分析报告_*.md`（兼容 `变更方案_*.md`）/`待确认清单_*.md`，读 §3 分类法完成诊断（类别 + 证据 + 改进指向），置 `round_diagnosis=pending` 并把结论作为步骤 6–8 的上下文（须规避重犯，见该文件 §5）。任一门控不满足则记 `round_diagnosis=skipped:<no_prior_round|prior_not_analysis|no_supplement>` 并跳过，不影响后续步骤。诊断的**记录**在步骤 11 完成。

**QC 答案继承（单次集中确认）**：若最近一轮是带 `confirmation_policy=qc-single-batch-v1` 的 `NEED-INFO/NEED-REVIEW`，加载其顶层 `checklist`，按稳定 `items[].id` 将本轮 `human_feedback` 合并为已确认业务证据。已回答 ID 不得重复询问；只有当前 TFS revision/附件新增事实与答案直接冲突时才可重新列入，并在 `why` 写明冲突证据。未回答项保留原 ID，与本轮新发现的业务决策一起去重、合并后形成唯一清单。

**用户反馈留痕快照**：解析调用入参时同步保留 `human_feedback` / `additional_info` 的反馈字段原文，供步骤 11 写入测试日志。不得只保留诊断摘要；首轮、上轮非分析终局或 `round_diagnosis=skipped:*` 也不豁免。未传或空值统一记为“无”，`collection_name` / `tfs_project` / `tfs_pat` 等连接参数不得进入该快照。

## 2. 先做准入路由

读取 `config/qc-rules.md` §1b/1c 和 `references/pre-qc-rules.md` §1b。只有“接口已开发、本次纯联调/调试/现场配合”且不改变业务规则、字段映射、接口契约、异常处置或验收逻辑时，判 `SKIP-ANALYSIS`。

- SKIP 计划使用空 `tags`、空 `artifacts`、`state_to=null` 和非空 `skip_reason`，不下载附件、不跑需求历史或 KB、不生成清单或分析者描述。
- 既有功能结果错误、为空或条件分支不一致属于 BUG 语义，不能因标题写“优化/调整”而跳过。

**非 SKIP 产品 MCP 路由（禁止省略）**：取 fetch 返回的 `area`（`System.AreaPath` 首级，缺失时已由客户端回退 `teamProject`），调用 `build_menu_business_index.py resolve-route --area <area>`。`RESOLVED` 时将返回的产品、profile 版本和四类 server name 快照到 `knowledge_route`，随后只检查这些 server 的原生工具是否注入；某角色缺失只标该来源未就绪。`AREA_UNMAPPED` / `AREA_AMBIGUOUS` / `PROFILE_MISSING` / `PROFILE_INVALID` 时 `servers={}`，禁止调用任何候选、默认或其它产品 MCP；各来源标未就绪，AUTO 候选降 MANUAL，QC/MANUAL 仅在不依赖缺失证据时按既有降级规则继续。

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
4. 只有判定面需要、需要历史背景/关联项/验收来源，或初判 NEED-REVIEW 可由既有事实消除时，才读取当前产品知识资料并运行发现链。顺序为当前工作项与附件 → 当前 profile 需求历史 → 菜单索引 → 按需 wiki 业务语义 → 当前 profile GitNexus → 按需受控源码 → 按需数据库；纯文字、无业务实体且非高风险的需求主动跳过。源码须已有“精确 repo + API/路径/符号/技术字面量”锚点且当前实现细节可能直接闭合质控阻断；锚点优先来自 GitNexus，也可来自已解析附件、工作项明确技术信息、菜单索引或 wiki。GitNexus 不可用时只允许核验既有精确锚点，不得把源码 MCP 当模块发现或查重替代。
5. 需求历史先用 `get_requirements_summary` 固定覆盖范围；显式关系用 `get_related_work_items`，检索结果用 `search_requirements` 收敛后必须经 `get_work_item` 读取完整正文/验收才可标为已证实。**查重须按 `references/qc-kb-recipe.md` Step R 执行多组关键词 + 限定 `state`∈{已验证,已关闭} + get_work_item 核验 + 按 `changed_date` 倒序复查（勿漏最新条目；锚点仍按“最直接覆盖本次诉求”选定，不为“新”而牵强），缺一不可；命中须标 `maturity`（设想/分析确认/已落地，定义见 `_lib/knowledge-base.md` §六），只有 `maturity=已落地` 才算历史方案已落地，`maturity=设想` 不得当已落地依据。**历史相似项在质控阶段只补充清单和背景（查重质控仍为预留，不单独改变质控 verdict）；分析阶段命中 `maturity=已落地` 且能力覆盖本次诉求时，须按“功能已存在”判 MANUAL-REVIEW 并声明 `existing_feature.satisfied=true`（见 Step 8 与 `references/confidence-heuristic.md` 第 4 项）。
6. 初判 NEED-REVIEW 仅在每个阻断项都被已证实 `kb:`/`wiki:` finding 直接闭合时才能转 PASS。`req:` 只能说明历史需求事实，不能进入 `qc_evidence_resolution`。逐项写“规则/触发条件/范围 → 证据事实 → 放行结论”；候选、名称相似、局部代码存在或未读附件不能放行。
7. **两层业务决策预演**：在最终 PASS 前，按 `iteration-analysis-closure.md` 与 `confidence-heuristic.md` 先搭建“现状—目标—差异—方案—验收”草案。第一层逐面检查范围、流程、业务含义/取值、业务规则、权限、异常和验收；范围、业务含义、验收必须有明确来源，其余不涉及时须说明不适用，通用七面不得用默认值兜底。第二层只在新增/调整字段、接口参数、模板绑定或数据持久化时检查：①展示触发与位置；②空值的展示、必填及清空语义；③维护对象粒度（头/明细、单条/批量、新增/编辑/清空）；④历史数据策略（迁移/回填/兼容/后续可维护）。纯文案式展示与占位可按呈现类默认；空值保存/清空语义、维护粒度和历史数据会改变数据模型、范围或验收，不得默认。已有工作项、附件、继承 PM 答案或已证实产品规则可直接闭合；只有 PM 可回答且答案会改变方案或验收的剩余缺口才合并进本轮 QC checklist。技术定位/证据缺口只记内部候选，不向 PM 提问。普通需求目标 1–3 个决策主题，复杂需求最多 5 个，P0 不得因上限丢弃。

## 5. 结束质控阶段

- `NEED-INFO` / `NEED-REVIEW`：按执行契约生成含顶层 `checklist` 的 QC 计划；inline `qc-followup` 只有 `filename`、没有 `path`，`state_to=null`。随后进入步骤 10，完成后停止。
- `PASS`：仅当业务决策预演后 PM 可回答缺口为零；不生成 QC 计划、不 apply，直接进入分析。
- 初判 NEED-REVIEW 经补证转 PASS：分析计划必须保留 `qc_evidence_resolution`，每个引用只能指向已证实 KB/wiki finding。

## 6. 建立分析证据闭环

读取分析规则、迭代闭环和知识库规范。顺序为当前产品需求历史 MCP（按需补背景/关系/验收）→ 菜单索引 → 按触发条件读取 wiki 业务语义 → 当前产品 GitNexus `list_repos` → `query` → `context` → 有精确 API 时 `route_map` → `context` → 按需源码 MCP `search_symbol` / `search_source`；数据库图谱只有在已有代码锚点后按需使用。

分析开始先生成 `implementation_impacts`。命中字段赋值、API 契约、业务逻辑、数据读写、schema、迁移、统计、数据权限、数据库性能或脚本时，源码/数据库取证要求由影响类型直接触发，**不得因高风险或 MANUAL 终局已确定而提前停止**。源码先沿 GitNexus 做两轮收敛取得精确锚点，再核验入口、编排、落库 3–5 个关键文件；数据类再从已证实代码锚点联查正式表、列和 READS/WRITES。服务可用就实际查询并形成 finding；不可用或覆盖不足才进入 `evidence_gaps`。

**界面/布局类需求的两类证据闭合**：需求涉及界面展示/布局/增减字段或交互时，从产品知识（wiki/Raw）、运行观察（受控页面/可追溯截图/已解析附件）、实现证据（GitNexus/源码）三类中至少取两类交叉证实现状；同类多条只算一类。wiki 命中业务字典、高风险或确需业务规则/布局语义时按触发条件读取，是优先源但不是固定必需源。新策略的 `AUTO-ANA + field-ui-copy` 将所用来源写入 `ui_baseline.sources`。

- `search_requirements` 结果只算候选；`get_work_item` 或 `get_related_work_items` 确认的历史事实可写 `tfs_requirements.findings`（须标 `maturity`），但不证明当前实现或已上线——`state=已证实` 只表事实核实，是否已落地由 `maturity=已落地` 判定（见 `_lib/knowledge-base.md` §六）。
- 历史需求与实时 fetch、现场事实或当前代码冲突时保留冲突并形成缺口，不自行选择历史版本。
- 只有“对象与动作匹配 + 可追踪入口 + 明确关系”同时成立才记为已证实。
- `processes`/`route_map` 为空是覆盖缺口，不代表模块不存在。
- 源码 MCP 只在取得“精确 repo + API/路径/符号/技术字面量”锚点后核验入口、编排、落库等 3–5 个关键文件：已知符号优先 `search_symbol`，精确路径/字面量才用 `search_source`；锚点优先来自 GitNexus，也可来自已解析附件、工作项明确技术信息、菜单索引或 wiki。不得用业务词无锚点扫仓；GitNexus 不可用时源码只核验既有精确锚点，不替代发现、跨层关系或查重。
- 源码 MCP 的已证实 finding 只证明所检索白名单仓库/允许路径中的代码事实；仍须再与部署版本、菜单/路由、现场截图或受控页面观察之一闭合，才能描述现场或部署行为。无命中、截断、越出白名单或路径拒绝只能记覆盖不足。
- 字段或打印取值异常要枚举条件分支与来源；同名字段不能证明唯一数据源。
- 技术定位只进内部证据，不写进 PM 需求分析报告正文。
- 代码图谱、源码与数据库发现仍统一写入计划 `kb`，但新计划必须分别填写 `ready`、`source_ready`、`source_required`、`database_ready`、`database_required`；源码 finding 的 `source_tool` 固定为 `search_source|search_symbol`，每条 finding 带 `source_type=code|database`。`kb/wiki/tfs_requirements.findings` 均须补齐 `conclusion/evidence/boundary`，让消费方直接看懂“证明了什么/依据在哪/不能证明什么”；历史需求工具不得写进 `kb`。Redis 只生成 `summary/source_status/evidence_list` 人读清单；`source_status` 固定展示五类来源，即使未就绪或本轮未使用也保留，以呈现完整佐证路径。route、tools、服务器和原始 finding 留在计划/审计。可选来源仅声明未就绪、但本轮未查询/未依赖时，不生成 `summary.coverage`，但仍在 `source_status` 如实显示。

## 7. 生成需求分析报告并分流缺口

按“现状基线（界面类须交叉证实）→ 问题与目标（含痛点核对与需求方方案合理性评估）→ 差异与范围 → 方案取舍 → 成功衡量与非目标”完成闭环，先冻结结构化 `分析结果_<id>_<run_id>.json`。`需求分析报告`、分析者描述和 Redis 摘要不得单独撰写，由执行器从快照确定性渲染。

- `concise-v3` 在类别标题前单列唯一“菜单路径”；取自工作项、已解析附件或唯一菜单索引，不得从代码路径猜测。无菜单入口时写“无菜单入口：<明确触发方式>”。操作步骤写入对应方案行；同一维度只有一项变更时保持单行，存在 2–8 个独立变更点时按 `1. **改动点**：内容` 从 1 连续编号，TFS 显示为 `1、2、3`。类别、改动点、风险和验收保持一致。历史 profile 继续使用旧版“路径（菜单路径+操作路径）”。
- 呈现类缺口使用合理默认并写入假设；金额、计算精度、业务规则、异常、合规、权限等 PM 可回答的正确性缺口回退 QC `NEED-REVIEW`，合并进唯一 checklist。
- 技术实现、代码定位和证据发现缺口只进入 `evidence_gaps`。
- 再次发现 spec 歧义或需求方向/根因存疑时，回退 QC `NEED-REVIEW` 并写 `qc_recheck=analysis-spec-ambiguity`，不以 MANUAL 或 `analysis_gaps` 代替。
- 每个原始入口、触发阶段、条件和期望动作必须在方案或“不涉及范围”有唯一落点；复杂状态需求补齐状态×事件×历史状态×动作×验收矩阵。
- 全量加载、跨分页排序、批量导入、弹窗、异步或跨页面联动必须核实数据范围、发生层、组件能力和真实触发时机，证据不足时列候选与取舍。
- **需求方方案合理性评估（必做）**：现状基线和痛点核对之后、方案取舍之前，对每个需求点判断合理性与必要性（见 `references/iteration-analysis-closure.md` §2）；**PM 需决策的方向/位置/范围/业务实现口径存疑→回退 `NEED-REVIEW` 并合并进 checklist；仅技术实现或证据定位存疑→`evidence_gap` + MANUAL-REVIEW**，不得把方向问题降级为技术缺口，不照搬需求方方案。

## 8. 判定分析终局

先检查六类高风险：患者安全、支付结算、数据迁移、首次发布新模块、合规强监管、架构级改动。命中即 `MANUAL-REVIEW-STOP` + `PM-AI-STOP-AUTO`，覆盖所有 AUTO 启发式。

高风险只决定终局与标签，不是证据采集停止条件；步骤 6 已触发的源码/数据库取证必须完成或形成明确覆盖缺口后，才能生成 MANUAL 计划。

未命中高风险时，只有全部变更落入 AUTO 白名单，且改动可枚举、正确性验收明确、改动量小、相似需求证据满足规则，才判 `AUTO-ANA`；任一不满足判 `MANUAL-REVIEW`。

## 9. 生成唯一执行计划

严格按 `_lib/tfs/EXECUTION_CONTRACT.md` 对应终局骨架生成计划，不翻历史样例。

- 新 SKIP/QC 计划固定 `version=2`、`plan_profile=run-bound-v1` 并引用本轮回执；QC 仍带 `confirmation_policy=qc-single-batch-v1`。新分析业务字段写入 `analysis-result-v1` 快照：verdict、菜单路径/改动点、闭环、追踪、`analysis_gaps=[]`、证据/缺口、四源 `evidence_acquisition`、`implementation_impacts`、通用七面与专项四面。瘦计划固定 `version=2`、`plan_profile=analysis-ref-v1`，只带 work item 身份、`expected_rev/state` 及回执/快照的文件名和 SHA-256；执行器固定按 `evidence-loop-v2` 物化完整计划。
- 新的非 SKIP 计划必须带 `knowledge_route`；路由未解析时 `servers={}` 且不得携带任何产品 MCP 工具调用或 finding，AUTO-ANA 只允许 `status=RESOLVED`。新分析计划的 `kb` 同时声明 `source_required` 与 `database_required`，并与 `implementation_impacts` 一致。
- 新分析计划统一使用 `analysis_profile=concise-v3`，按类别输出 1–3 行有效业务结论；`concise-v1` / `concise-v2` 与无 profile 的完整格式只兼容历史计划。
- AUTO 的 `analysis_gaps`、`evidence_gaps` 均为空，`auto_scopes` 非空；现状基线、差异与范围、方案取舍各至少引用一条已证实 `kb:` finding。
- AUTO 的 `auto_scopes` 含 `field-ui-copy` 时必须带 `ui_baseline.sources`，并覆盖产品知识、运行观察、实现证据中的至少两类独立来源；wiki 未覆盖本身不降级，只有两类现状证据无法闭合时才降 `MANUAL-REVIEW` 并记录缺口。
- 已证实历史需求可用 `req:<索引>` 补充 `evidence_refs`；它不能替代 AUTO 要求的 `kb:` 引用，也不能用于 `qc_evidence_resolution`。
- 新分析计划不得生成 `manual-followup`；`analysis_gaps` 非空只用于历史计划回放兼容。
- `assignee_to` 只在 AUTO 唯一匹配时填写 `WINNING\账号`；匹配不出则省略，不阻断 AUTO。

## 10. 校验与执行

1. 正常流程只运行一次 `pipeline.py apply --id <id> --run-id <run_id>`；执行器只加载规范目录中的唯一计划，不接受任意 `--plan`，不扫描或回退历史目录。apply 内部先对内存副本做安全归一化，再一次性校验运行回执、计划摘要、分析快照和 TFS revision/state；不得按“validate → 修字段 → validate → apply”反复试错。缺省只 dry-run；`validate --plan` 仅用于维护诊断。
2. 只有 TFS-BUDDY/DeerFlow 等受控编排器明确授权时，才对同一冻结计划增加 `--execute`；不重新分析、不新建 run_id。对话用户的普通请求不是 execute 授权。
3. 新策略分析终局的 apply 固定按“冻结快照并生成报告 → 写分析者描述 → 上传本轮需求分析报告 → 清理历史受控 AI 附件 → 加终局标签 → AUTO 字段流转 → 发布 Redis”执行。快照、报告摘要与 TFS 同名附件内容摘要一致时均 noop；每个真实非-noop TFS 写动作后以该动作 PATCH 成功响应中的 revision/state 记录本 run 执行检查点（快照摘要 + 写后 rev/state），不得以紧随其后的 GET 或“revision 必须恰好 +1”误判成功写入。续跑仅在 live rev/state 与检查点精确一致时放行，之后出现任何外部 revision/state 变化仍返回 `SOURCE_CHANGED`。
4. 校验按三级处理：身份/冻结/业务终局/AUTO决定性证据不可信为 `REJECT`；分析可信但 TFS precheck/执行资格失败为 `ANALYSIS_ONLY`，返回 `ok=true, applied=false, actions=[]` 并发布本轮结果；纯格式和非决定性审计缺口为 `WARNING`，继续安全确定的 Redis/TFS 动作。WARNING 不生成 ERROR、不要求重新分析；revision/state 变化另返回 `SOURCE_CHANGED`、`retryable=false`、`requires_new_run=true`，重新 fetch 后必须开启新 run_id。
5. `NEED-INFO` / `NEED-REVIEW` 不清理旧分析附件；待最终方案形成时一次性收敛。无回执历史计划不得进入普通 apply；维护回放只能显式 `apply-legacy --plan`，不发布正常 Redis，审计隔离到 `legacy-runs/`。
6. 执行后按需要重新 fetch，核对 revision、状态、标签和字段；不能把命令成功等同于分析正确。本地 `过程文件/<id>/<run_id>/`、`runs/` 审计和测试日志始终保留，继续供追溯和重分析诊断读取。运行状态只按 run_id 查询，覆盖 `INITIALIZED → ANALYZING → FROZEN → APPLYING → COMPLETED|ANALYSIS_ONLY|FAILED`；不得从工作项“最新轮”回退。普通 apply 先用规范目录原子锁串行化同一 run；锁冲突或 `ANALYZING/APPLYING` 返回 `RUN_ALREADY_IN_PROGRESS`，`FAILED` 返回 `RUN_TERMINAL_REQUIRES_NEW_RUN`，均不再进入执行或新建审计；`COMPLETED` 且已 execute 的重复投递直接幂等返回。dry-run 完成后的首次授权 execute 仍允许同 run 继续。

pipeline 自动发布 Redis 摘要；不得直接调用 Redis 写命令。Redis 不可用只记录降级，不阻断或回滚 TFS 动作。只有 TFS 写动作已经开始后失败才发布 `ERROR`；执行错误不得修改冻结快照、报告或 verdict。

## 11. 记录诊断、处理经验记忆并追加测试日志

**重分析诊断记录（仅 `round_diagnosis=pending` 时）**：在本轮分析/计划完成后，按 [`../references/round-diagnosis-rules.md`](../references/round-diagnosis-rules.md) §4 向 `过程文件/skill-feedback.md` 追加一条（首次创建时先写文件头）。填齐"上轮 run_id / 本轮 run_id / 上轮终局 / 诊断类别 / 证据 / 改进指向 / 本轮是否已据此修正"，追加后置 `round_diagnosis=done`。`skipped:*` 或首轮不写该文件；该文件不进 TFS、不入计划 JSON。

**经验记忆处理（每轮必做，写入仍按条件）**：依据 [`_lib/skill-memory.md`](skill-memory.md) 生成 `经验处理请求_<id>_<run_id>.json`。重分析诊断类别原样放 `round_diagnosis_categories`；本轮另有跨需求、非业务特定的 skill 层经验时置 `runtime_lesson=true`；命中任一可沉淀触发时提供一条蒸馏候选，否则 `candidate=null` 并说明原因。调用 `COMMANDS.md` 的 `skill_memory.py record`，读取 `经验处理结果_<id>_<run_id>.json`，只接受 `APPENDED|DEDUP_NOOP|NOT_APPLICABLE|FAILED`。结果文件缺失、命令未调用或命中可沉淀类别却无候选，一律视为 `FAILED`。失败只记副记录异常并继续，**不得作为 TFS ERROR 或改变本轮终局**。

**追加测试日志（最后执行）**：每次 validate、dry-run、execute、TFS 回读或环境阻断后，按 `_lib/testing-log.md` 向 `过程文件/测试日志.md` 追加记录，不覆盖历史；本轮最后一条记录必须在经验处理后追加并带“经验记忆处理”状态及结果路径，作为收尾完成标记。记录需求号、run_id、质控结果、终局、标签、证据边界、待确认信息、用户手动反馈、分析者描述、时间、产物和异常；未进入分析时分析者描述写“不适用”。“用户手动反馈”必须逐字保留本轮 `human_feedback` 的 `question_id/question/answer` 与 `additional_info`，按规范化 JSON 写入；两者均空写“无”，不得因重分析诊断跳过而省略。`FAILED` 时在“异常与改进”记录原因；`maintenance_required=true` 时记录“经验记忆待维护”，二者均非阻断。

## 分支动作速查

| 终局 | 标签 | 状态与附件 |
|---|---|---|
| `SKIP-ANALYSIS` | 无 | `state_to=null`，无附件、无 TFS 写动作 |
| `NEED-INFO` | `PM-AI-QC-NEED-INFO` | `state_to=null`，inline QC checklist 指向实施/创建者 |
| `NEED-REVIEW` | `PM-AI-QC-NEED-REVIEW` | `state_to=null`，inline QC checklist 指向责任方 |
| `AUTO-ANA` | `PM-AI-AUTO-ANA` | change-plan；完整字段流转到「已分析」；指派失败可降级 |
| `MANUAL-REVIEW` | `PM-AI-MANUAL-REVIEW` | `state_to=null`；change-plan；新策略无用户待确认附件 |
| `MANUAL-REVIEW-STOP` | `PM-AI-MANUAL-REVIEW` + `PM-AI-STOP-AUTO` | `state_to=null`；其余同 MANUAL |

全部问题在一次 apply 中归集到 `validation={decision,blocking_errors,warnings,normalizations}`；兼容字段 `errors/errors_by_class` 只保留真正阻断项。已知附件转换链数组只在内存中归一化；未知转换元数据不得抛异常。NEED/MANUAL 缺非决定性源码 finding、或需求历史已有 finding 但漏记探活工具时只告警，AUTO 同类缺口仍拒绝。若 `write-field`/`set-state` 等 TFS 写动作开始后失败，记录 ERROR，不强转；revision/state 冲突返回 `SOURCE_CHANGED`，重新 fetch 后用新 run_id 重建快照和计划，不复用旧计划盲重试。
