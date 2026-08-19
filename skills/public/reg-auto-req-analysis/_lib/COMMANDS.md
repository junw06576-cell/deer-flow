# 命令索引（单一权威 · auto-req-analysis）

> 本文件是本 skill 全部可复用命令的**唯一权威索引**：7 个脚本、全部子命令、知识库工具与各自适用场景。
> 别处在散文里提到的命令都是叙述，以本文件为准。

## 运行前必读（硬纪律）

处理任何工作项前完整读取本文件，并遵守：

1. **只使用本文件列出的脚本、子命令和 MCP 工具**；拿不准签名时先跑 `--help`。
2. **不得临时实现 TFS/Redis/知识库客户端**，不得使用 `python3 -c`、Python heredoc、临时 Python/shell/jq 脚本、`redis-cli` 或直接 `curl` 操作 TFS/Redis。
3. **安装只走官方附件运行器**：模型和其它脚本不得执行 `pip`、`uv pip`、`npm`、`brew`、`apt`；`attachment_runtime.py` 可按静态允许列表自动准备附件依赖，并返回完整安装审计。
4. **工作项流程的 TFS 写入只走 `pipeline.py apply`**；Redis 写入只由 pipeline 内部发布。不得直接调用 `tfs_client.py` 写子命令或 Redis 写命令绕过计划校验。
5. 本文件没有所需能力时，记录 `ERROR`/`evidence_gap` 后停止，禁止自行补脚本、装包或寻找旁路。

**纯解读既有命令的 JSON 输出**——直接在上下文里完成，不得为此手写脚本。典型：
- `list-iterations` 返回的 `matched`/`earliest` → 按 `config/qc-rules.md §2`、`_lib/tfs/field-flow.md §4 条硬约束` 在上下文里做 timing 判定与 earliest/matched 取舍；
- 附件转换器返回的 `converted`/`needs_read`/`error`/`unsupported` → 按 `_lib/attachment-evidence.md §2` 映射 `parsed`/`unparsed`/`skipped`（图片 `needs_read` 还要视觉读取，**不可脚本化**）；
- `fetch` 返回的 `workItem` → 直接读字段；
- `rev` 比较 / `expected_rev` 取值 → 上下文算术。

**产品 MCP 先路由再调用，知识库用 MCP 工具，产品 wiki 用 grep**——非 SKIP 先以 `resolve-route` 将 Area 唯一路由到 `product_id`，只使用该 profile 的需求历史 / GitNexus / 源码 / 数据库工具；见末节。没有也无需 `.py` 客户端，别为 KB 工作找/写 Python。

## CWD

所有命令从**仓库根 `auto-dev-work/`** 运行（`过程文件/...` 等相对路径按 CWD 解析）。若此前 `cd` 改过 CWD，先回仓库根再执行。

## skill 调用参数

`/reg-auto-req-analysis <work_item_id> [payload JSON]` —— `<work_item_id>` 在前，后接可选 payload JSON（不含 `work_item_id`）；也接受把 `work_item_id` 放进 JSON 的完整写法（编排器请求体常用）。裸 `<work_item_id>` = 无补充、用 `tfs-config.json` 默认连接（首轮）。

```
# 写法一（slash 常用）：id 在前 + payload JSON（仅补充字段）
/reg-auto-req-analysis 258829 {"human_feedback":[{"question_id":"q1","question":"...","answer":"..."}],"additional_info":"..."}
# 写法二（完整 JSON，编排器 /evaluation 请求体）
{"collection_name":"WN_PH-Platform","work_item_id":258829,"tfs_project":"NETHIS5.5","tfs_pat":"<PAT>","human_feedback":[{"question_id":"q1","question":"...","answer":"..."}],"additional_info":"..."}
```

- `work_item_id`：必填。
- `run_id`：请求体禁止传入。FastAPI 必须先执行 `init-run`，再把返回 ID 作为 session/thread/CLI 内部上下文传播；网络重试使用 HTTP `Idempotency-Key`，不是复用请求体中的 run_id。
- `human_feedback`：可选（A），新策略下优先回答上轮 QC `checklist.items`；历史计划仍兼容 `待确认清单`/`analysis_gaps`。每项 `{question_id, question, answer}`；`question_id` 对齐稳定 item/gap id（如新策略 `q-scope`/历史 `g1`），`question` 原文自描述。已回答 ID 除非与当前 revision/新附件直接冲突，否则不得重复询问。字段原文须写入本轮 `过程文件/测试日志.md` 的“用户手动反馈”。
- `additional_info`：可选（B），超出上轮所问的额外反馈/纠正（文本）；原文同样写入本轮测试日志。A/B 均空时日志写“无”，连接参数不写入。
- `collection_name` / `tfs_project` / `tfs_pat`：可选连接参数。编排器运行时优先注入环境变量 `TFS_COLLECTION`/`TFS_PROJECT`/`TFS_PAT`（见 `与 deerflow 对接接口文档/tfs-buddy-interface.md` §7）；slash 交互时由模型按需传 `--collection`/`--project`/`--pat`。缺省回落 `tfs-config.json`。
- skill 据此分 A/B，并在同工作项有更早分析轮时触发「重分析诊断」，见 `references/round-diagnosis-rules.md` §1。无更早分析轮或两项均空时自动忽略，不影响任何终局。全程非交互取得，不得用 `AskUserQuestion` 等方式索取。

## 通用可选参数（tfs_client / pipeline 子命令均支持）

| 参数 | 缺省 | 说明 |
|---|---|---|
| `--config <path>` | 脚本同目录 `tfs-config.json` | 配置文件路径 |
| `--pat <PAT>` 或 `TFS_PAT` | 配置 `tfs.pat` | 临时覆盖 PAT，**不写回配置**；优先级 `--pat > TFS_PAT > tfs.pat` |
| `--collection <集合>` 或 `TFS_COLLECTION` | 配置 `tfs.collection` | 临时覆盖 collection（分析其它集合工作项），**不写回**；优先级 `--collection > TFS_COLLECTION > tfs.collection` |
| `--project <项目>` 或 `TFS_PROJECT` | 配置 `tfs.project` | 临时覆盖 project，**不写回**；优先级 `--project > TFS_PROJECT > tfs.project` |

`pipeline.py apply/repair/flow` 的 `--config/--pat/--collection/--project` 同义；`tfs_client.py` 经 `conn_parent` 让每个子命令都带 `--pat/--collection/--project`（`list-iterations` 的 `--project` 必填）。

## tfs_client.py（读 / 写标签 / 转状态 / 写字段 / 传附件 / 审计）

`python3 skills/reg-auto-req-analysis/_lib/tfs/tfs_client.py <子命令> ...` —— 标准库 `urllib`，无第三方依赖。下表写子命令供执行器维护和受控诊断使用；**本 skill 的正常工作项流程不得直接调用它们写 TFS，必须使用 `pipeline.py apply`**。

| 子命令 | 签名 | 何时用 |
|---|---|---|
| `precheck` | `precheck` | 跑 skill 前连通性 + 鉴权自检 |
| `fetch` | `fetch <id>` | 读工作项字段（**唯一字段来源**，禁止臆测）；`expectedDate` 为北京时间 ISO，`expectedDateRaw` 保留 TFS 原值 |
| `download-attachments` | `download-attachments <id> --output-dir <DIR> [--include-external] [--max-bytes N]` | 下载来源附件。`--include-external` 代码缺省 **off**，**skill 始终带**以一并提取白名单外链与内嵌 `<img>`；`--max-bytes` 单文件上限，缺省 20 MiB |
| `list-iterations` | `list-iterations --project <teamProject> [--expected-date <日期或 ISO 时间>]` | 时效/排期实查。期望时间先转北京时间业务日期；`--project` **用工作项的 teamProject**；返回 `matched`（最晚能赶上）+ `earliest`（提交截止未过里 finishDate 最小者，排期用）。解析失败返回 `ok:false`，**timing 判定在上下文做** |
| `add-tag` | `add-tag <id> --tag <PM-AI-*> [--dry-run]` | 打标签（仅限 `PM_AI_TAGS` 白名单） |
| `remove-tag` | `remove-tag <id> --tag <PM-AI-*> [--dry-run]` | 移除标签（重跑清旧标签） |
| `set-state` | `set-state <id> --state <状态> [--dry-run]` | 单步转状态（AUTO-ANA 多步流转由 `pipeline flow` 统一跑，不手写多步） |
| `set-assignee` | `set-assignee <id> --assignee 'WINNING\账号' [--dry-run]` | 单步指派（必须 `WINNING\账号` 格式，bare 中文名 TFS 报未知标识） |
| `write-field` | `write-field <id> --field <F> --value <TEXT\|@file.md> [--mode replace\|append] [--marker <RUN>] [--dry-run]` | 写字段；`@file.md` 读文件内容；`--marker` 做 `run_id` 幂等防重写 |
| `upload-attachment` | `upload-attachment <id> --file <PATH> [--dry-run]` | 上传 skill 产物附件（按文件名去重） |
| `record` | `record --skill <s> --id <id> --verdict <V> [--tag <TAG>...] [--state-from ..] [--state-to ..] [--trace ..] [--extra '{}'] [--run-id ..]` | 手工审计落盘（`pipeline apply` 自动审计时无需手调） |

## pipeline.py（校验 / 受约束执行 / 字段流转）

`python3 skills/reg-auto-req-analysis/_lib/tfs/pipeline.py <子命令> ...` —— 计划校验 + 受约束执行器；缺省 dry-run，仅显式 `--execute` 才写 TFS。

| 子命令 | 签名 | 何时用 |
|---|---|---|
| `init-run` | `init-run --id <工作项ID>` | 每次新分析触发的第一步；执行器原子生成 `run_id`、目录和运行回执。无 `--run-id` 参数，调用方不得指定或复用旧 ID |
| `validate` | `validate --plan <执行计划.json>` | 维护诊断入口，**不访问 TFS**；使用与 apply 相同的 `REJECT/WARNING` 分类并一次返回全部可判定问题，正常新流程不单独调用 |
| `apply` | `apply --id <工作项ID> --run-id <服务端run_id> [--execute]` | 新流程唯一校验/执行入口；只加载 `过程文件/<id>/<run_id>/执行计划_<id>_<run_id>.json`，不接受 `--plan`，不扫描或回退历史目录。内存归一化后按 `REJECT/ANALYSIS_ONLY/WARNING` 分层；WARNING 继续安全动作。计划未就绪返回 `RUN_NOT_READY` 且保持 `INITIALIZED`；正在执行返回 `RUN_ALREADY_IN_PROGRESS`；失败终局返回 `RUN_TERMINAL_REQUIRES_NEW_RUN`；已完成的重复投递幂等返回且不重复生成审计 |
| `apply-legacy` | `apply-legacy --plan <历史计划.json> [--execute]` | 仅维护回放无回执完整 v1/v2；固定 `run_mode=legacy-replay`，不发布正常 Redis，审计写入 `过程文件/<id>/legacy-runs/` |
| `status` | `status --id <工作项ID> --run-id <run_id>` | 只读取该规范运行的状态文件；未知 ID 返回 `RUN_NOT_FOUND`，禁止按工作项查找最新轮兜底 |
| `repair-analysis-placement` | `repair-analysis-placement --plan <执行计划.json> [--execute]` | 修旧执行器错追到「需求分析」字段的 Markdown（受控：不写标签/状态/附件） |
| `flow` | `flow (--id <N> \| --plan <执行计划.json>) [--expected-rev <N>] [--expected-state <S>] [--assignee 'WINNING\账号'] [--run-id ..] [--execute]` | 独立字段流转（迭代/日期/活动→已分析/指派）。`--expected-rev`/`--assignee` 均**可选**：不传则每步 `revision_guard` 兜底 / 指派优先 `Winning.Dev.Leader` |

普通 `apply` 的计划路径由执行器根据 `--id + --run-id` 唯一推导，调用方不得提供路径。同一冻结计划从 dry-run 转授权 execute 时复用原 run_id；WARNING 不属于重新分析，不得新建 run 或反复 validate。再次触发 skill 才必须重新 `init-run`。无回执历史计划只能显式 `apply-legacy`。详见 `_lib/tfs/EXECUTION_CONTRACT.md`、`_lib/tfs/field-flow.md`。

## attachment_runtime.py（正式附件入口 · 自动依赖修复 + 转换）

正式工作项只调用该入口。它先盘点实际格式，在 `过程文件/.runtime/attachments/<cache-key>/` 创建或复用 Python 3.12.13 隔离环境，按哈希锁安装 Python 依赖；`.xls/.doc` 缺 LibreOffice 时再按 macOS/Homebrew 或 Debian/Ubuntu apt 允许列表补齐。单项安装失败只影响对应格式，其它文件继续转换：

```bash
python3 skills/reg-auto-req-analysis/_lib/tfs/attachment_runtime.py precheck \
  --input-dir 过程文件/<id>/<run_id>/附件
```

```bash
python3 skills/reg-auto-req-analysis/_lib/tfs/attachment_runtime.py convert \
  --input-dir 过程文件/<id>/<run_id>/附件 \
  --output-dir 过程文件/<id>/<run_id>/附件解析 [--max-bytes N]
```

返回 `preflight`（实际格式、能力、安装记录、缓存键、运行模式、阻断格式和警告）以及逐文件结果。成功项带 `converter_chain`、`runtime_mode`、`tool_versions`。固定镜像仍是最高复现等级；宿主自动修复使用同一锁文件并审计实际版本。镜像构建、缓存和安装边界见 `runtime/README.md`。

## attachment_converter.py（运行器内部转换器）

运行器内部调用；维护和定向诊断可用 `--precheck --input-dir <DIR>` 或 `--input-dir/--output-dir`。正常工作项不得绕过 `attachment_runtime.py`。逐文件链为：PDF→MarkItDown→已有 PyMuPDF 降级；DOCX/XLSX→MarkItDown→内置解析器；XLS→MarkItDown→LibreOffice 转 XLSX→MarkItDown/内置解析器；DOC→LibreOffice 转 DOCX→MarkItDown/内置解析器；PPTX→MarkItDown。旧版 PPT 仍不支持。

## redis_client.py（结果存储查询）

`python3 skills/reg-auto-req-analysis/_lib/tfs/redis_client.py <子命令> [--config <path>]`

本脚本只公开探活和查询。Redis 写入由 `pipeline.py apply` 内部完成；没有独立写入命令时必须停止，不得用 `redis-cli`、`python3 -c` 或临时脚本补写。

| 子命令 | 签名 | 何时用 |
|---|---|---|
| `ping` | `ping` | Redis 探活 |
| `hgetall` | `hgetall <collection> <id>` | 查某工作项最新执行结果摘要（TFS-BUDDY/看板/调试） |

## build_menu_business_index.py（菜单业务索引）

`python3 skills/reg-auto-req-analysis/_lib/build_menu_business_index.py <子命令> ...`

| 子命令 | 签名 | 何时用 |
|---|---|---|
| `build` | `build [--sources <menu-sources.json>] [--output <menu-business-index.json>]` | 按 `menu-sources.json` 构建 area→菜单索引（无子命令时按缺省 `--sources`/`--output` 构建） |
| `lookup` | `lookup --area <area> [--index <menu-business-index.json>]` | **按 TFS 区域查候选产品**（area 取 `System.AreaPath` 首级，缺失才用 `teamProject`）。返回 `{ok, area, count, products}`（仅产品级）；代码定位锚点走 wiki 子系统名/仓库名 + GitNexus `query`→`context`，不依赖菜单级字段 |
| `resolve-route` | `resolve-route --area <area> [--index <menu-business-index.json>] [--routes <product-mcp-routes.json>]` | **唯一解析产品 MCP profile**。返回 `route_status/product_id/product_name/profile_version/servers`；未映射、歧义、缺 profile 或非法 profile 时 `ok=false, servers={}`，禁止默认或跨产品回退 |

## skill_memory.py（经验记忆收尾）

`python3 skills/reg-auto-req-analysis/_lib/skill_memory.py record --input <请求.json> --result <结果.json>`

每轮步骤 11 **必须调用一次**。请求与结果固定放在本轮目录：

```bash
python3 skills/reg-auto-req-analysis/_lib/skill_memory.py record \
  --input 过程文件/<id>/<run_id>/经验处理请求_<id>_<run_id>.json \
  --result 过程文件/<id>/<run_id>/经验处理结果_<id>_<run_id>.json
```

请求只允许 `work_item_id`、`run_id`、`round_diagnosis_categories`、`runtime_lesson`、`reason`、`candidate`；不得放 `tfs_pat`、连接参数或原始用户反馈。`candidate` 为 `null` 表示本轮不沉淀；命中可沉淀诊断类别或 `runtime_lesson=true` 时必须提供符合 `_lib/skill-memory.md` 的候选条目。脚本负责受控类别校验、文件锁、精确归一化去重、append-only 写入、回读计数和结果文件原子落盘。

```json
{
  "work_item_id": 262214,
  "run_id": "<run_id>",
  "round_diagnosis_categories": ["信息判断错误"],
  "runtime_lesson": false,
  "reason": "批量场景遗漏可跨需求复用",
  "candidate": {
    "title": "分析前必须枚举单条与批量场景",
    "category": "分析·方案",
    "scenario": "同一入口同时支持单条处理与批量处理时。",
    "practice": "分别核对选择、跳过、汇总和结果反馈，不能用单条路径代替批量路径。",
    "replaces": []
  }
}
```

结果状态固定为：`APPENDED`（已追加）、`DEDUP_NOOP`（已有同条经验）、`NOT_APPLICABLE`（本轮不适用）、`FAILED`（输入/权限/写入失败）。`FAILED` 返回非零，但只影响经验副记录：测试日志须显式记录后继续，不得改变 TFS verdict、标签、状态或执行结果。结果文件缺失等同 `FAILED`。

## 产品知识源 —— 先路由，再用 MCP 工具 / 文本源

产品 MCP 注册表固定为 `config/product-mcp-routes.json`，以菜单索引的稳定 `product_id` 为键。非 SKIP 工作项先运行 `resolve-route`，只使用返回 profile 的原生工具；不得按下表 server name 猜默认产品。探活/三态/降级见 `_lib/knowledge-base.md`。

当前 `cloudhis-v56` profile 如下；这些地址是 CloudHIS 当前实例示例，不是其它产品的全局默认：

| 角色 / 当前 server | 工具 | 何时用 |
|---|---|---|
| 需求历史 `tfs-requirements` | `get_requirements_summary` / `get_related_work_items` / `search_requirements` / `get_work_item` | 补充当前产品最近采集范围内的历史背景、显式关系、验收条件和变更原因；实时当前项字段仍只认 `fetch` |
| 代码图谱 `gitnexus-team` | `list_repos` / `query` / `context` / `route_map` / `impact` / `cypher` | 当前产品模块定位、调用链、查重、影响分析（主干 + 兜底） |
| 源码检索 `cloudhis-source` | `search_source` / `search_symbol` | 仅在当前产品 GitNexus 给出精确 repo/路径/符号锚点后，核验条件分支、字段赋值、配置、SQL 或模板等实现细节；最多 3–5 个关键文件 |
| 数据库图谱 `db-knowledge` | `search_knowledge` / `get_table_knowledge` / `traverse_graph` / `inspect_node` | 当前产品字段/迁移/统计口径（**按需**，须先有代码锚点；字段类按 `_lib/module-location-recipe.md §6` 闭环） |
| 产品 wiki | **读/grep `wiki/`**（先完整读 `wiki/index.md` → 按门诊/住院/电子病历/平台/数据库定位文章 → 关键事实按需沿 `Raw` 相对链接复核），非 MCP | 业务语义/规则/口径/验收 + 子系统名（GitNexus **之前**的业务语义层；未覆盖记限制继续） |

当前 CloudHIS 源码 MCP 地址为 `http://172.16.0.192:4752/mcp`，只读且由服务端限制白名单仓库/允许路径；无命中、截断、白名单外或路径拒绝只算覆盖不足，不得推断源码不存在。`tfs-requirements` 调用规则：先用 `get_requirements_summary` 探活并记录覆盖范围；显式关系用 `get_related_work_items`；`search_requirements` 命中只算候选，必须再用 `get_work_item` 读取正文/验收/按需修订历史后才可标为已证实。接入、降级和引用规则见 `_lib/knowledge-base.md`。

其它接入/查询方法：`_lib/module-location-recipe.md`、`_lib/wiki-kb-recipe.md`。

## --help

每个脚本都支持 `--help`；`tfs_client.py` / `pipeline.py` 的**每个子命令**也支持 `python3 <script>.py <子命令> --help`（如 `tfs_client.py fetch --help`、`pipeline.py flow --help`）。拿不准签名时先跑 `--help`，不要凭记忆写命令。
