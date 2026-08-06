# 运行手册（reg-auto-req-analysis）

本文件承载工作项处理的详细顺序。硬约束以 [`../SKILL.md`](../SKILL.md) 为准，命令签名以 [`COMMANDS.md`](COMMANDS.md) 为准；两者冲突时立即停止并记录问题，不自行选择旁路。

## 目录

- 0–3：启动、读取工作项、准入、附件
- 4–5：质控初判、补证与终局
- 6–9：分析证据、方案、终局与计划
- 10–11：校验执行与测试日志
- 分支动作速查

## 0. 启动

1. 从仓库根目录完整读取 `SKILL.md`、`_lib/COMMANDS.md` 和本文件。
2. 执行 `tfs_client.py precheck`，必须返回 `ok:true`。
3. 生成唯一 `run_id`，创建 `过程文件/<id>/<run_id>/`。一个 run_id 贯穿质控、分析、计划、执行和日志。

## 1. Fetch 工作项

执行 `tfs_client.py fetch <id>`，只从返回的 `workItem` 读取 title、state、description、analyzerDesc、tags、rev、teamProject、area 等字段；禁止凭记忆补字段。

## 2. 先做准入路由

读取 `config/qc-rules.md` §1b/1c 和 `references/pre-qc-rules.md` §1b。只有“接口已开发、本次纯联调/调试/现场配合”且不改变业务规则、字段映射、接口契约、异常处置或验收逻辑时，判 `SKIP-ANALYSIS`。

- SKIP 计划使用空 `tags`、空 `artifacts`、`state_to=null` 和非空 `skip_reason`，不下载附件、不跑 KB、不生成清单或分析者描述。
- 既有功能结果错误、为空或条件分支不一致属于 BUG 语义，不能因标题写“优化/调整”而跳过。

## 3. 处理来源附件

未命中 SKIP 时读取 `_lib/attachment-evidence.md`，使用 `download-attachments --include-external` 下载到 `附件/`，再用 `attachment_converter.py` 转换到 `附件解析/`。

- 只有白名单 HTTPS ERP 外链可使用独立认证；绝不向外部发送 TFS PAT。
- 只有成功解析的内容参与判断；未解析、跳过或错误项只记录限制，不能假称已读。
- 转换器报告依赖缺失或 `unsupported` 时直接降级，禁止安装依赖。

## 4. 质控初判与补证

1. 读取 `config/qc-rules.md`、`references/pre-qc-rules.md`；未覆盖的通用完整性问题用 `references/qc-checklist.md` 核对。
2. 先检查需求类型、PIMIS、期望日期、项目归属和治标警惕。SKIP、NEED-INFO 及范围/时效/方向根因等客观硬阻断直接早退。
3. 时效实查使用 `list-iterations --project <workItem.teamProject> --expected-date <date>`；查询失败交排期方，不臆测。
4. 只有判定面需要或初判 NEED-REVIEW 可由既有事实消除时，才读取知识库资料并运行发现链。纯文字、无业务实体且非高风险的需求主动跳过 KB。
5. 初判 NEED-REVIEW 仅在每个阻断项都被已证实 `kb:`/`wiki:` finding 直接闭合时才能转 PASS。逐项写“规则/触发条件/范围 → 证据事实 → 放行结论”；候选、名称相似、局部代码存在或未读附件不能放行。

## 5. 结束质控阶段

- `NEED-INFO` / `NEED-REVIEW`：按执行契约生成含顶层 `checklist` 的 QC 计划；inline `qc-followup` 只有 `filename`、没有 `path`，`state_to=null`。随后进入步骤 10，完成后停止。
- `PASS`：不生成 QC 计划、不 apply，直接进入分析。
- 初判 NEED-REVIEW 经补证转 PASS：分析计划必须保留 `qc_evidence_resolution`，每个引用只能指向已证实 KB/wiki finding。

## 6. 建立分析证据闭环

读取分析规则、迭代闭环和知识库规范。顺序为菜单索引 → wiki 业务语义 → GitNexus `list_repos` → `query` → `context` → 有精确 API 时 `route_map` → `context`；数据库图谱只有在已有代码锚点后按需使用。

- 只有“对象与动作匹配 + 可追踪入口 + 明确关系”同时成立才记为已证实。
- `processes`/`route_map` 为空是覆盖缺口，不代表模块不存在。
- 候选源码必须再与部署版本、菜单/路由、现场截图或受控页面观察之一闭合，否则只能写候选。
- 字段或打印取值异常要枚举条件分支与来源；同名字段不能证明唯一数据源。
- 技术定位只进内部证据，不写进 PM 变更方案正文。

## 7. 生成变更方案并分流缺口

按“现状基线 → 问题与目标 → 差异与范围 → 方案取舍 → 成功衡量与非目标”完成闭环，再按写作规则和模板生成 `变更方案_<id>_<run_id>.md`。

- `路径` 同时写菜单路径与操作路径；类别、改动点、风险和验收保持一致。
- 呈现类缺口使用合理默认并写入假设，不进入 `analysis_gaps`；金额、计算精度、业务规则、异常、合规、权限等正确性缺口才进入 `analysis_gaps`。
- 技术实现、代码定位和证据发现缺口只进入 `evidence_gaps`。
- 再次发现 spec 歧义或需求方向/根因存疑时，回退 QC `NEED-REVIEW`，不以 MANUAL 或 analysis gaps 代替。
- 每个原始入口、触发阶段、条件和期望动作必须在方案或“不涉及范围”有唯一落点；复杂状态需求补齐状态×事件×历史状态×动作×验收矩阵。
- 全量加载、跨分页排序、批量导入、弹窗、异步或跨页面联动必须核实数据范围、发生层、组件能力和真实触发时机，证据不足时列候选与取舍。

## 8. 判定分析终局

先检查六类高风险：患者安全、支付结算、数据迁移、首次发布新模块、合规强监管、架构级改动。命中即 `MANUAL-REVIEW-STOP` + `PM-AI-STOP-AUTO`，覆盖所有 AUTO 启发式。

未命中高风险时，只有全部变更落入 AUTO 白名单，且改动可枚举、正确性验收明确、改动量小、相似需求证据满足规则，才判 `AUTO-ANA`；任一不满足判 `MANUAL-REVIEW`。

## 9. 生成唯一执行计划

严格按 `_lib/tfs/EXECUTION_CONTRACT.md` 对应终局骨架生成计划，不翻历史样例。

- 新分析计划固定 `version=2`、`rules_source.analysis=evidence-loop-v1`，并带 `expected_rev`、`expected_state`、`analysis_description`、`analysis_gaps`、`evidence_refs`、`evidence_gaps`。
- 简洁输出仅适用于单一 `existing-ui-simple`、`print-adjustment` 或 `data-management`，新计划使用 `analysis_profile=concise-v2`；`concise-v1` 只兼容历史计划。
- AUTO 的 `analysis_gaps`、`evidence_gaps` 均为空，`auto_scopes` 非空；现状基线、差异与范围、方案取舍各至少引用一条已证实 `kb:` finding。
- `analysis_gaps` 非空时使用 MANUAL 终局并生成唯一 `manual-followup`；为空时不得生成该附件。
- `assignee_to` 只在 AUTO 唯一匹配时填写 `WINNING\账号`；匹配不出则省略，不阻断 AUTO。

## 10. 校验与执行

1. 先运行 `pipeline.py validate --plan <plan>`。
2. 再运行 `pipeline.py apply --plan <plan>`；缺省只 dry-run。
3. 只有 TFS-BUDDY/DeerFlow 等受控编排器明确授权时才增加 `--execute`。对话用户的普通请求不是 execute 授权。
4. 执行后按需要重新 fetch，核对 revision、状态、标签和字段；不能把命令成功等同于分析正确。

pipeline 自动发布 Redis 摘要；不得直接调用 Redis 写命令。Redis 不可用只记录降级，不阻断或回滚 TFS 动作。

## 11. 追加测试日志

每次 validate、dry-run、execute、TFS 回读或环境阻断后，按 `_lib/testing-log.md` 向 `过程文件/测试日志.md` 追加一条记录，不覆盖历史。记录需求号、run_id、质控结果、终局、标签、证据边界、待确认信息、分析者描述、时间、产物和异常；未进入分析时分析者描述写“不适用”。

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
