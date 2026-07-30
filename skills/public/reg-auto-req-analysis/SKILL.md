---
name: reg-auto-req-analysis
description: "This skill should be used when a TFS 需求工作项需要自动化质控 + 需求分析一体化处理。It runs non-interactively: 阶段一质控（NEED-INFO/NEED-REVIEW/PASS），PASS 闸后阶段二分析（AUTO-ANA/MANUAL-REVIEW），一个 run_id 贯穿，最终产出一份受约束的 TFS 执行计划。"
allowed-tools:
  - bash
required-secrets:
  - REDIS_URL
---

# 自动化需求处理（auto-req-analysis）

## 核心原则

> **质控 → [PASS 闸] → 分析，一个 `run_id` 贯穿 → 跑到终局判定 → 写回 TFS → 停止。全程无交互。**
> 你是 AI 自动开发链路「质控+分析」一体 skill。入参 = TFS 工作项 ID。**两阶段、一份计划**：
>
> - **阶段一·质控**：判 `NEED-INFO`（信息缺失，交实施/创建者）/ `NEED-REVIEW`（需产品补充）/ `PASS`（合理完整）。
>   - `NEED-INFO`/`NEED-REVIEW` → 打 QC 标签 + 写待补充清单回 TFS → **停，不进阶段二**。
>   - `PASS` → **不产计划、不 apply**，直接进入阶段二（PASS 仅是内部阶段闸，不再是终态）。
> - **阶段二·分析**：判 `AUTO-ANA`（允许自动 → 状态自动流转「已分析」）/ `MANUAL-REVIEW`（状态不变，规则命中敏感则 +`PM-AI-STOP-AUTO`）。
>
> 一个 `run_id` 贯穿两阶段；最终只在运行末尾产出**一份** `执行计划_<id>_<run_id>.json`，`verdict` = 最终终局（NEED-* 或分析终局）。

## ⛔ 硬约束：禁止交互

**禁止使用 `AskUserQuestion`，禁止向用户提问、禁止等待确认。** 信息缺失/存疑时：阶段一 → 打 QC 标签 + 写清单回 TFS + 停；阶段二 → 记录 `analysis_gaps`（若有）+ 自动打标签 + 停。由责任方在 TFS 处理后重新触发，而不是你在对话里问。

| 借口（rationalization） | 现实 |
|---|---|
| "信息有点歧义，最好跟用户确认一下" | ❌ 不问。歧义 → 阶段一 `NEED-REVIEW`，写清单交产品。 |
| "缺接口文档，问创建者要一下" | ❌ 不问。缺核心信息 → 阶段一 `NEED-INFO`，清单写回 TFS。 |
| "质控 PASS 了，评估完最好让人看一眼再分析" | ❌ 不停顿。PASS 即进阶段二；需人看 = 阶段二 `MANUAL-REVIEW`。 |
| "AUTO 还是 MANUAL 把握不准，确认下" | ❌ 不问。按置信度启发式（或规则）判；不确信 → `MANUAL-REVIEW`。 |
| "这个改动有点敏感，问问产品" | ❌ 不问。敏感 → `MANUAL-REVIEW`（规则命中则 +`STOP-AUTO`）。 |
| "状态流转影响大，确认一下再转" | ❌ 不问。判 `AUTO-ANA` 即转；不能转/不该转就 `MANUAL-REVIEW`。 |

**红旗——出现就停手检查：** 想调 `AskUserQuestion`、想输出选项让用户选、想"暂停等一下"、把判定权交回人。**一律改为：写清单/记 gaps + 打标签 + 停。**

---

## 运行产物与目录（强制）

每次运行先生成唯一 `run_id`，并创建 `过程文件/<id>/<run_id>/`（一个 run 一套目录，贯穿两阶段）。本次**手工生成的所有产物只能放在该目录**；来源附件只读下载到其 `附件/` 子目录、可读副本放 `附件解析/`，二者都不是待上传的 skill artifact。禁止在仓库根目录生成计划或附件。执行器审计另由 `pipeline.py` 写入 `过程文件/<id>/runs/`，无需 skill 创建。唯一例外是跨运行的追加式测试日志 `过程文件/测试日志.md`，它不是 skill artifact，按 `_lib/testing-log.md` 记录。

| 最终终局 | skill 产物 | TFS 附件 |
|---|---|---|
| `NEED-INFO` / `NEED-REVIEW`（阶段一挡回） | `执行计划_<id>_<run_id>.json`；顶层 `checklist` JSON | inline `qc-followup`：执行器临时物化并上传 `待补充信息_<id>_<run_id>.json`，不保留手工 `.md` |
| `AUTO-ANA` / `MANUAL-REVIEW` / `MANUAL-REVIEW-STOP`（阶段二完成） | `执行计划_<id>_<run_id>.json`、`变更方案_<id>_<run_id>.md`；有 `analysis_gaps` 时另加 `待确认清单_<id>_<run_id>.md` | `change-plan`（变更方案）；有 gaps 时再加 `manual-followup` |

`PASS` **不产计划**（内部阶段闸）。质控的 `checklist` 是待补充清单的唯一权威源，必须内嵌在计划 JSON；分析阶段不写顶层 `checklist`，其待确认项是 `manual-followup` 磁盘 Markdown 附件（artifact 带 `path`，正文带运行标记）。artifact 的 `path`/`filename` 只写同目录文件名；附件文件名与正文都必须含同一 `run_id` 运行标记。

---

## 流程（按顺序，一步不跳）

前置：确认 TFS 配置就绪——`python3 skills/reg-auto-req-analysis/_lib/tfs/tfs_client.py precheck` 返回 `ok:true`（失败则 **降级**，见末尾）。

### 阶段一·质控

1. **建立本次证据目录并读工作项**：生成 `run_id`，创建 `过程文件/<id>/<run_id>/`，再执行 `python3 skills/reg-auto-req-analysis/_lib/tfs/tfs_client.py fetch <id>` → 解析返回的 `workItem`（title/state/description/analyzerDesc/tags/…）。字段**严格取自脚本返回的 JSON**，禁止臆测、禁止凭记忆填。
2. **处理来源附件（必做）**：读 `_lib/attachment-evidence.md`，依次执行 `python3 skills/reg-auto-req-analysis/_lib/tfs/tfs_client.py download-attachments <id> --output-dir 过程文件/<id>/<run_id>/附件 --include-external` 与 `python3 skills/reg-auto-req-analysis/_lib/tfs/attachment_converter.py --input-dir 过程文件/<id>/<run_id>/附件 --output-dir 过程文件/<id>/<run_id>/附件解析`，按该规范阅读内置转换结果。`--include-external` 只读取配置了 HTTPS 白名单和独立认证的 ERP 外链，绝不把 TFS PAT 发往外部；仅已白名单 ERP 因环境代理不可达时可配置 `bypass_proxy=true`。未启用、认证缺失或越界链接均记为限制。把解析结果写入计划顶层 `attachments`（阶段二生成计划时带上）；已解析内容参与后续核对，未解析/跳过项只记录限制，不能假称已读，也不直接新增终局。
3. **加载质控规则**：读 `config/qc-rules.md` 和 `references/pre-qc-rules.md`。先执行需求类型、PIMIS 优先级、期望日期、标题项目归属（AI 自主判断·字典仅参考）等**事前质控**；未被事前质控覆盖的通用信息完整性与合理性，再用 `references/qc-checklist.md` 补充核对。QC 终局计划写 `rules_source={"qc":"pre-qc-v1"}`。
   - **字段类需求**：需要知识库佐证时，按 `_lib/knowledge-base.md` 与 `_lib/module-location-recipe.md` §6 完成"业务同义词 → 页面/载荷 → SQL 读写 → 正式表列 → 目标查询缺口"闭环；证据不足只记候选或未确认，不直接改变质控终局。
   - **时效判定（期望日期）实查**：调 `python3 skills/reg-auto-req-analysis/_lib/tfs/tfs_client.py list-iterations --project <工作项 teamProject> --expected-date <期望日期>` 取官方迭代日历，按 `finishDate < 期望日期` 判（见 `config/qc-rules.md §2`、`references/pre-qc-rules.md §三.3`）。**`--project` 用工作项的 `teamProject`，不是配置里的 project**。查询失败 → 降级交排期方，不阻断。
   - **有目的 KB 发现链（可选·不直接判终局）**：读 `_lib/knowledge-base.md` 探活；当需求**触及判定面**（命中高风险早筛 ①②⑤⑥，**或**描述含可映射到 schema/code 的业务概念：结算/费用/医保/上传/校验/报表/接口/明细/账单等）时，按 `references/qc-kb-recipe.md` 跑发现链（先按 `workItem.area` 精确筛共享菜单索引 → 按 `.wiki-schema.md` §Query 查 wiki 取业务语义/子系统名（`_lib/wiki-kb-recipe.md` §2-3，未覆盖该模块则记限制继续）→ `list_repos` 固定 repo → "业务对象→业务动作→技术锚点"收敛 → `context`/`route_map` 建立关系）。发现按**已证实/候选/未确认**与**图谱事实/推断/未确认**写入 `checklist.items`（命名实体的选择式问题）/ `kb_note` / `kb.findings`。**不直接判终局**：不加 verdict 类别、不加 `qc-rules.md` 的 KB 专属判定步；但发现（含疑似重复/已实现）可写成清单条目，经现有维度（`qc-checklist.md` #3 可识别性/#5 重复）参与终局，存疑即 NEED-REVIEW。纯文字无业务实体且非高风险的需求不查（主动跳过，非降级）。代码图谱未就绪则降级跳过，审计记 `kb_ready=false`。
4. **评估质控终局**：按事前质控的范围、时效、内容规则，再做通用信息完整性与合理性核对，得到 `NEED-INFO` / `NEED-REVIEW` / `PASS`。所有命中项都写入清单，不能只记录第一项；附件中的已证实信息按文件名写入对应结论的 `why`。**spec 清晰度闸**（范围/边界、取值/数据源口径、与现有路径一致性，`qc-checklist.md` #7-#9）任一存疑 → `NEED-REVIEW` 挡回产品——**PASS 进阶段二的应 spec 已清**。**高风险早筛命中**（①②⑤等）同样按 spec 清晰度二分：spec 清晰 → `PASS`（由分析阶段 `PM-AI-STOP-AUTO` 兜底人工复核，质控不挡回）；spec 不清 → `NEED-REVIEW`。详见 `config/qc-rules.md` §3。
5. **质控分支**：
   - `NEED-INFO` / `NEED-REVIEW` → **在此生成计划**（顶层 `checklist`，结构见 `references/qc-checklist.md §二`，inline `qc-followup` 带 `filename` 无 `path`，`state_to=null`，`checklist` 内**不写**运行标记——由执行器物化时注入）→ 跳到步骤 10（validate + apply）→ 测试日志 → **停，不进阶段二**。
   - `PASS` → **不产计划、不 apply**，直接进入阶段二。

### 阶段二·分析（仅质控 PASS 后）

6. **加载分析规则 + 知识库（图谱=内部佐证）**：读 `config/analysis-rules.md` 与 `_lib/knowledge-base.md`。先按 `workItem.area` 精确筛共享菜单索引；再按 `.wiki-schema.md` §Query 查 wiki 取业务语义/子系统名（`_lib/wiki-kb-recipe.md`，wiki 仅作内部佐证、不进变更方案正文，未覆盖则记限制继续）；唯一产品范围内的菜单 repo/API 只作候选，再调代码图谱 `list_repos` 确认实际 `repo`（已知项目优先单仓库，跨项目才用 `cloudhis-unified`），检查索引时间、节点/流程数与 `embeddings` 状态；区域未配置、菜单索引未命中或范围不唯一时记录限制后继续，不猜测产品。再按 `_lib/module-location-recipe.md` 用"业务对象 → 业务动作 → 技术锚点"两轮检索。对候选按 `query → context → route_map（有精确 API 路径时）→ context` 建立入口和调用关系；`processes` 或 `route_map` 为空时继续用 `context` 验证，不能据此否定模块。仅在"对象与动作匹配 + 可追踪入口 + 明确关系"同时成立时记为**已证实**；其余明确标为**候选**或**未确认**，不得按名称猜测。结果落 `kb.findings`，用于印证 AUTO/MANUAL 的**业务改动类型**判断、查重、高风险定性佐证；仓库、类、方法、接口、Mapper、表字段等**技术定位不打印进变更方案正文**。仅当需求涉及字段、迁移、统计口径、数据权限、性能或数据库脚本时，才以已证实的表名/字段名/Mapper/SQL 为锚点联查数据库图谱；字段类需求必须按 `module-location-recipe.md` §6 执行闭环。数据库不可用或无命中不影响代码级分析。规则空/`TODO` → 走**内置置信度启发式**（见 `references/confidence-heuristic.md`）；分析计划写 `rules_source={"qc":"pre-qc-v1","analysis":"fallback-v1"}`，并审计 `kb_ready`。
7. **生成变更方案并识别分析缺口（PM/业务视角）**：先读 `references/analysis-description-writing-rules.md`，仅按工作项与已解析附件证据选择一项或多项受控类别。再用 `references/change-plan-template.md` 在既有运行目录产出 `变更方案_<id>_<run_id>.md`：固定写 `## 三、分析者描述`、需求类别行、唯一非空的 `路径` 行和每个类别的三级标题；`路径` 必须同时包含业务"菜单路径"和"操作路径"，不得包含技术仓库、代码符号或接口。逐项写齐该类别必需维度。**再做必需维度完整性扫描**：对命中类别的每个必需维度，逐项检查工作项/已解析附件是否说清。说清的写入方案；未说清的按 `references/confidence-heuristic.md` 的「呈现类 vs 正确性类」分流——**呈现类**缺口（格式/文案/顺序/空值等）用合理默认兜进 §六假设、**不进 `analysis_gaps`**；仅**正确性类**缺口（金额取数/计算精度/业务规则/异常处置/合规/权限）生成一个 `analysis_gaps` 项，只写 PM 可补齐的业务问题，禁写技术实现或代码定位。**`analysis_gaps` 不得含 spec 类问题**（改动落地层/范围边界/与现有路径一致性——这些归阶段一质控 `references/qc-checklist.md` #7-9）。若分析再次发现 spec 歧义，**立即回退为质控 `NEED-REVIEW`**：生成 QC inline `checklist`（责任方产品，逐条写明本次发现），计划不带 `change-plan`、`analysis_gaps` 或 `manual-followup`，在审计写 `qc_recheck=analysis-spec-ambiguity` 后按步骤 10 停止；绝不以 MANUAL 计划或 analysis gaps 代替质控清单。文件名与正文必须包含 `<!-- auto-req-run:<run_id> -->`。
   - **查询能力强制分流**：凡新增或调整查询条件、筛选/匹配逻辑、查询结果列表或其字段，必须选 `existing-query-adjustment`，逐项说明“条件输入 → 匹配与组合 → 结果字段与展示 → 空值/无结果 → 关联视图与权限 → 验收”闭环；先以工作项、已解析附件或已核实页面判断结果列表是否已有同一业务标识字段：已有则明确“复用既有字段、不新增列”，仅在已证实缺失时才新增列；没有证据不得从“新增查询条件”推断“新增结果列”。
8. **判定分析终局**（按优先级，先命中先生效）：
   - **命中 6 类高风险**（`config/analysis-rules.md` §2：①患者安全 ②支付结算 ③数据迁移 ④首次发布新模块 ⑤合规强监管 ⑥架构级改动）→ `MANUAL-REVIEW` + `PM-AI-STOP-AUTO`。**最高优先，覆盖启发式**：即使改动小、信息全也强制人工，**绝不 `AUTO-ANA`**。
   - **否则**先过 `config/analysis-rules.md` §1 的 AUTO 白名单：全部变更只能是字段级 UI/文案调整、单测补齐、配置/枚举调整中的一类或多类；再满足 `references/confidence-heuristic.md` 的改动可枚举 + 正确性类验收口径明确（呈现类可用合理默认兜底） + 改动量小 + 相似需求命中条件 → `AUTO-ANA`。任一不满足 → `MANUAL-REVIEW`（不加 `STOP-AUTO`）。
9. **生成分析计划**：在同一运行目录创建 `执行计划_<id>_<run_id>.json`，固定 `expected_rev`、`expected_state`、终局、标签、附件、分阶段 `rules_source`、`attachments`、必填 `analysis_description: {"categories": ["<受控类别>"]}` 和必填 `analysis_gaps` 数组（`AUTO-ANA` 必须为 `[]`；非空缺口必须用 MANUAL 终局，并据 `references/manual-review-template.md` 生成 `manual-followup`；没有缺口时不得生成该附件）。`AUTO-ANA` 计划必须写入非空 `auto_scopes`（白名单子集）。类别数组必须非空、无重复，并与方案需求类别行和三级标题完全一致。把阶段一的 `attachments`/`kb`/`wiki`/`iteration` 证据一并带上（一个 run_id 贯穿）。`change-plan` 与 `manual-followup` 的 artifact `path` 必须是精确的同目录文件名，分别为 `变更方案_<id>_<run_id>.md` 和 `待确认清单_<id>_<run_id>.md`；禁止绝对路径、`..` 或其他运行目录。
10. **校验并执行**：先运行 `pipeline.py validate`。`pipeline.py apply` 缺省只 dry-run；由 TFS-BUDDY / DeerFlow 的受控任务显式传 `--execute` 才写 TFS。执行器只能校验显式 `--execute`，**不能自行识别调用者身份**；编排器必须通过 PAT 权限、宿主机访问控制或外层授权策略限制该参数。**不向对话用户请求确认。**详见 `_lib/tfs/EXECUTION_CONTRACT.md`。
11. **追加测试日志（必做）**：每次 validate、dry-run、execute、TFS 回读验证或因环境阻断停止后，按 `_lib/testing-log.md` 向 `过程文件/测试日志.md` 追加**一条**记录（一个 `run_id` 覆盖质控+分析全链路）；写明需求号、质控结果、标签、菜单清单与知识图谱的作用和边界、待确认信息、是否 PASS、分析者描述、测试时间、产物和异常。质控挡回（未进分析）时分析者描述填"不适用"。

### 分支表（每条分支的精确动作，按序执行）

**质控挡回分支**（`NEED-INFO`/`NEED-REVIEW`）：

| 终局 | 标签 | 动作 |
|---|---|---|
| `NEED-INFO` | `PM-AI-QC-NEED-INFO` | `checklist.items` 逐条列**问题 + 推荐选项（选择式）**，**指向实施/创建者**；inline `qc-followup`，`state_to=null`。执行器物化清单上传、写标签、审计后停止。 |
| `NEED-REVIEW` | `PM-AI-QC-NEED-REVIEW` | 同 `checklist` 结构，责任方按命中规则写产品、研发负责人、现场、排期方或非本组流程。KB 发现链命中的命名实体（含疑似重复/已实现）可写为 `items`（三态标注在 `why`），经现有维度（#3/#5）参与终局。 |

**分析完成分支**（先做公共写回：执行器从 `change-plan` 只提取"## 三、分析者描述"，受控 Markdown 转义渲染为基础 HTML，整段写入 `System.Description` 的 `【分析者描述】`与`【开发者描述】`之间；完整 `change-plan` 保持 Markdown 仅作附件上传。缺少/重复/顺序异常的区段标题即失败停止，绝不改写 `Winning.Demand.Analysis`）：

| 终局 | 标签 | 动作（公共写回之后） |
|---|---|---|
| `AUTO-ANA` | `PM-AI-AUTO-ANA` | 计划 `verdict=AUTO-ANA`、`state_to=已分析`、artifact 仅 `change-plan`、`analysis_gaps=[]`、非空 `auto_scopes`。执行器最后添加标签并流转状态。 |
| `MANUAL-REVIEW` | `PM-AI-MANUAL-REVIEW`（高风险则 `MANUAL-REVIEW-STOP` 再加 `PM-AI-STOP-AUTO`） | `state_to=null`；高风险用 `verdict=MANUAL-REVIEW-STOP`。`analysis_gaps=[]` 时仅上传变更方案并自动打标签；非空时才生成 `待确认清单_<id>_<run_id>.md` 并加 `manual-followup` artifact。清单只写 PM 可补齐、会影响规则/范围/计算精度/异常处置/验收的业务信息；绝不写标签、终局、高风险定性、自动开发或状态流转确认。 |

> **状态机**：质控 `NEED-*` 与分析 `MANUAL-REVIEW*` **绝不流转状态**（等补充/PM 确认后重新触发）；只有 `AUTO-ANA` 自动流转到「已分析」。`AUTO-ANA` 分支 `set-state` 放最后；若它失败 → 执行器记 `ERROR`，状态未转，交人工，**不强转**。

---

## 规则接口（外置）

- `config/qc-rules.md`：质控运行时判定顺序、字段映射、终局与责任方映射。
- `config/analysis-rules.md`：§1 AUTO 白名单 + 非高风险细判、§2 高风险清单、§4 图谱=内部佐证。审计须记录 `rules_source` 与 `auto_scopes`。
- `references/pre-qc-rules.md`：完整事前质控规则与项目关键词字典；查重仍为预留，不参与当前判定。
- `references/qc-checklist.md`：未被事前质控覆盖时的通用核对兜底 + 待补充清单模板。
- `references/qc-kb-recipe.md`：质控知识库「有目的发现链」操作手册（不直接判终局）。
- `references/confidence-heuristic.md`：分析 AUTO 启发式（4 项）+ 呈现类 vs 正确性类缺口分流。
- `references/change-plan-template.md`：**PM/业务视角**变更方案模板（技术定位不进正文）。
- `references/analysis-description-writing-rules.md`：分析者描述业务路径、受控类别、必需维度、反空泛规则（受 `pipeline.py validate` 静态校验）。
- `references/manual-review-template.md`：分析待确认清单模板（仅 PM 业务信息）。
- `_lib/high-risk-categories.md`：6 类高风险单一权威源（质控早筛 ①②⑤⑥、分析定性兜 ③④⑥，共用）。
- `_lib/attachment-evidence.md`：来源附件只读下载、转换、三态记录与证据边界。
- `_lib/knowledge-base.md`：知识库（GitNexus 代码+数据库图谱 + 产品 wiki）接入规范（探活/三态/降级）；质控做有目的发现链·不直接判终局、分析作内部佐证，未就绪则跳过。读序 menu-index→wiki→GitNexus→db，GitNexus 主干+兜底、wiki 静默降级。
- `_lib/module-location-recipe.md`：精准定位后端模块方法论（质控用佐证子集、分析用完整版）。
- `_lib/wiki-kb-recipe.md`：产品 wiki（业务语义层）接入方法；查询遵从仓库根 `.wiki-schema.md` §Query，两阶段共用，仅作内部佐证不进正文。
- 质控**绝不修改 TFS 状态**；需求类型为软件质量、时效不可处理/谨慎处理都停止在阶段一并通过清单交对应责任方处理。

## 失败降级（安全网）

任一异常（TFS 错误 / 规则加载失败 / 计划校验失败 / `precheck` 失败 / `preflight` 失败 / `set-state` 失败 / 产出不合规）→ **立即停**；只要计划含有效工作项 ID 和 `run_id`，执行器就记录 `ERROR` 审计；无法解析计划身份时仅输出错误，不能伪造审计。**绝不继续后续动作、绝不流转状态、绝不强转**。并发版本冲突必须重新 fetch 并生成计划，不能盲目重试写入。阶段二失败不影响 TFS（计划未 apply 即未写）。

## 审计

每次执行由 `pipeline.py` 记录 `过程文件/<id>/runs/run_<ts>_<run_id>.json`（终局/标签/状态/规则来源/动作结果）。失败也记；同一秒并发运行不覆盖。

---

## 命令速查（相对仓库根 `auto-dev-work/`）

```bash
python3 skills/reg-auto-req-analysis/_lib/tfs/tfs_client.py precheck
python3 skills/reg-auto-req-analysis/_lib/tfs/tfs_client.py list-iterations --project <teamProject> [--expected-date 2026-08-31]
python3 skills/reg-auto-req-analysis/_lib/tfs/tfs_client.py fetch <id>
python3 skills/reg-auto-req-analysis/_lib/tfs/tfs_client.py download-attachments <id> --output-dir 过程文件/<id>/<run_id>/附件 --include-external
python3 skills/reg-auto-req-analysis/_lib/tfs/attachment_converter.py --input-dir 过程文件/<id>/<run_id>/附件 --output-dir 过程文件/<id>/<run_id>/附件解析
python3 skills/reg-auto-req-analysis/_lib/tfs/pipeline.py validate --plan 过程文件/<id>/<run_id>/执行计划_<id>_<run_id>.json
python3 skills/reg-auto-req-analysis/_lib/tfs/pipeline.py apply --plan 过程文件/<id>/<run_id>/执行计划_<id>_<run_id>.json
python3 skills/reg-auto-req-analysis/_lib/tfs/pipeline.py apply --plan 过程文件/<id>/<run_id>/执行计划_<id>_<run_id>.json --execute
# PAT：缺省用配置 tfs.pat；任一命令可加 --pat <PAT> 或用 TFS_PAT 环境变量临时覆盖（不写回配置）
# collection：缺省用配置 tfs.collection；任一命令可加 --collection <集合> 或用 TFS_COLLECTION 环境变量临时覆盖（仅本次、不写回；用于分析其它集合下的工作项）
```

## 不做什么

- 不做交互、不调 `AskUserQuestion`、不输出选项菜单。
- 质控 PASS 不产独立计划、不 apply（PASS 是内部阶段闸，直接进阶段二）。
- 不强转状态、不在异常时继续。
- 不做 auto-dev 点火、不做 7 类/Stop-List（出范围）。`PM-AI-STOP-AUTO` 在本仓只打标签，其"禁止自动开发"的下游效果不在本 skill。
- 不复用《汇报版》逻辑（已出范围）。
