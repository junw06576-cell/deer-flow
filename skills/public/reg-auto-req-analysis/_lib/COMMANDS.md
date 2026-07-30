# 命令索引（单一权威 · auto-req-analysis）

> 本文件是本 skill 全部可复用命令的**唯一权威索引**：5 个脚本、全部子命令、知识库工具与各自适用场景。
> 别处在散文里提到的命令都是叙述，以本文件为准。

## 写脚本前必读（纪律）

写任何临时脚本（Python / shell / jq / grep 拼接）之前，按序：

1. **先查本文件**找候选命令；
2. 对候选脚本跑 `python3 <script>.py --help`（tfs_client / pipeline 的子命令可 `python3 <script>.py <子命令> --help`）确认签名；
3. **能复用既有命令就绝不新写**。

**纯解读既有命令的 JSON 输出**——不要为此手写脚本，直接在上下文里读 JSON 完成。典型：
- `list-iterations` 返回的 `matched`/`earliest` → 按 `config/qc-rules.md §2`、`_lib/tfs/field-flow.md §4 条硬约束` 在上下文里做 timing 判定与 earliest/matched 取舍；
- 附件转换器返回的 `converted`/`needs_read`/`error`/`unsupported` → 按 `_lib/attachment-evidence.md §2` 映射 `parsed`/`unparsed`/`skipped`（图片 `needs_read` 还要视觉读取，**不可脚本化**）；
- `fetch` 返回的 `workItem` → 直接读字段；
- `rev` 比较 / `expected_rev` 取值 → 上下文算术。

**知识库（GitNexus / db-knowledge）用 MCP 工具，产品 wiki 用 grep**——见末节，没有也无需 `.py` 客户端，别为 KB 工作找/写 Python。

## CWD

所有命令从**仓库根 `auto-dev-work/`** 运行（`过程文件/...` 等相对路径按 CWD 解析）。若此前 `cd` 改过 CWD，先回仓库根再执行。

## 通用可选参数（tfs_client / pipeline 子命令均支持）

| 参数 | 缺省 | 说明 |
|---|---|---|
| `--config <path>` | 脚本同目录 `tfs-config.json` | 配置文件路径 |
| `--pat <PAT>` 或 `TFS_PAT` | 配置 `tfs.pat` | 临时覆盖 PAT，**不写回配置**；优先级 `--pat > TFS_PAT > tfs.pat` |
| `--collection <集合>` 或 `TFS_COLLECTION` | 配置 `tfs.collection` | 临时覆盖 collection（分析其它集合工作项），**不写回**；优先级 `--collection > TFS_COLLECTION > tfs.collection` |

`pipeline.py apply/repair/flow` 的 `--config/--pat/--collection` 同义；`tfs_client.py` 经 `conn_parent` 让每个子命令都带 `--pat/--collection`。

## tfs_client.py（读 / 写标签 / 转状态 / 写字段 / 传附件 / 审计）

`python3 skills/reg-auto-req-analysis/_lib/tfs/tfs_client.py <子命令> ...` —— 标准库 `urllib`，无第三方依赖；所有写操作支持 `--dry-run`。

| 子命令 | 签名 | 何时用 |
|---|---|---|
| `precheck` | `precheck` | 跑 skill 前连通性 + 鉴权自检 |
| `fetch` | `fetch <id>` | 读工作项字段（**唯一字段来源**，禁止臆测） |
| `download-attachments` | `download-attachments <id> --output-dir <DIR> [--include-external] [--max-bytes N]` | 下载来源附件。`--include-external` 代码缺省 **off**，**skill 始终带**以一并提取白名单外链与内嵌 `<img>`；`--max-bytes` 单文件上限，缺省 20 MiB |
| `list-iterations` | `list-iterations --project <teamProject> [--expected-date <YYYY-MM-DD>]` | 时效/排期实查。`--project` **用工作项的 teamProject**；给 `--expected-date` 返回 `matched`（最晚能赶上）+ `earliest`（提交截止未过里 finishDate 最小者，排期用）。**timing 判定在上下文做** |
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
| `validate` | `validate --plan <执行计划.json>` | 只校验计划 + 附件，**不访问 TFS** |
| `apply` | `apply --plan <执行计划.json> [--execute]` | dry-run（缺省）访问 TFS 预检/读但不写；`--execute` 才写（需编排器授权） |
| `repair-analysis-placement` | `repair-analysis-placement --plan <执行计划.json> [--execute]` | 修旧执行器错追到「需求分析」字段的 Markdown（受控：不写标签/状态/附件） |
| `flow` | `flow (--id <N> \| --plan <执行计划.json>) [--expected-rev <N>] [--expected-state <S>] [--assignee 'WINNING\账号'] [--run-id ..] [--execute]` | 独立字段流转（迭代/日期/活动→已分析/指派）。`--expected-rev`/`--assignee` 均**可选**：不传则每步 `revision_guard` 兜底 / 指派优先 `Winning.Dev.Leader` |

`--plan` 路径取 `过程文件/<id>/<run_id>/执行计划_<id>_<run_id>.json`（运行目录相对）。详见 `_lib/tfs/EXECUTION_CONTRACT.md`、`_lib/tfs/field-flow.md`。

## attachment_converter.py（扁平，无子命令）

```bash
python3 skills/reg-auto-req-analysis/_lib/tfs/attachment_converter.py \
  --input-dir 过程文件/<id>/<run_id>/附件 --output-dir 过程文件/<id>/<run_id>/附件解析 [--max-bytes N]
```

核心纯标准库；`.pdf` 在本机装 PyMuPDF(`fitz`) 时提取，未装降级 `unsupported`。返回 `converted`/`needs_read`/`error`/`unsupported`/`skipped` —— **三态映射（parsed/unparsed/skipped）在上下文按 `_lib/attachment-evidence.md §2` 做**（图片 `needs_read` 要视觉读取）。

## redis_client.py（结果存储查询）

`python3 skills/reg-auto-req-analysis/_lib/tfs/redis_client.py <子命令> [--config <path>]`

| 子命令 | 签名 | 何时用 |
|---|---|---|
| `ping` | `ping` | Redis 探活 |
| `hgetall` | `hgetall <collection> <id>` | 查某工作项最新执行结果摘要（TFS-BUDDY/看板/调试） |

## build_menu_business_index.py（菜单业务索引）

`python3 skills/reg-auto-req-analysis/_lib/build_menu_business_index.py <子命令> ...`

| 子命令 | 签名 | 何时用 |
|---|---|---|
| `build` | `build [--sources <menu-sources.json>] [--output <menu-business-index.json>]` | 按 `menu-sources.json` 构建 area→菜单索引（无子命令时按缺省 `--sources`/`--output` 构建） |
| `lookup` | `lookup --area <area> [--index <menu-business-index.json>]` | **按 TFS 区域查候选产品**（area 取 `System.AreaPath` 首级，缺失才用 `teamProject`）。返回 `{ok, area, count, products}`；唯一命中时其 `repo`/`module_url`/`apis` 作 GitNexus 候选锚点 |

## 知识库 —— 是 MCP 工具 / 文本源，**不是 `.py` 脚本**

别为 KB 工作找或写 Python；按下列接口用原生 MCP 工具。探活/三态/降级见 `_lib/knowledge-base.md`。

| 来源 | 工具 | 何时用 |
|---|---|---|
| 代码图谱 `gitnexus-team` | `list_repos` / `query` / `context` / `route_map` / `impact` / `cypher` | 模块定位、调用链、查重、影响分析（主干 + 兜底） |
| 数据库图谱 `db-knowledge` | `search_knowledge` / `get_table_knowledge` / `traverse_graph` / `inspect_node` | 字段/迁移/统计口径（**按需**，须先有代码锚点；字段类按 `_lib/module-location-recipe.md §6` 闭环） |
| 产品 wiki | **grep `wiki/`**（遵仓库根 `.wiki-schema.md` §Query；读 `index.md` → 别名词表扩词 → 读命中页），非 MCP | 业务语义/规则/口径/验收 + 子系统名（GitNexus **之前**的业务语义层；未覆盖记限制继续） |

接入/查询方法：`_lib/knowledge-base.md`、`_lib/module-location-recipe.md`、`_lib/wiki-kb-recipe.md`。

## --help

每个脚本都支持 `--help`；`tfs_client.py` / `pipeline.py` 的**每个子命令**也支持 `python3 <script>.py <子命令> --help`（如 `tfs_client.py fetch --help`、`pipeline.py flow --help`）。拿不准签名时先跑 `--help`，不要凭记忆写命令。
