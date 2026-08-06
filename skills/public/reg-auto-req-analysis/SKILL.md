---
name: reg-auto-req-analysis
description: "This skill should be used when a TFS 需求工作项需要自动化准入路由、质控与需求分析一体化处理。It runs non-interactively: 先排除纯联调支持（SKIP-ANALYSIS），再做阶段一质控（NEED-INFO/NEED-REVIEW/PASS），PASS 闸后阶段二分析（AUTO-ANA/MANUAL-REVIEW），一个 run_id 贯穿，最终产出一份受约束的 TFS 执行计划。"
---

# 自动化需求处理（auto-req-analysis）

## 启动检查（步骤 0，禁止跳过）

在执行任何工作项命令前，从仓库根目录**完整读取** [`_lib/COMMANDS.md`](_lib/COMMANDS.md) 和 [`_lib/RUNBOOK.md`](_lib/RUNBOOK.md)。随后执行 `tfs_client.py precheck`；任一文件不可读或 precheck 失败时按失败关闭处理，不得自行补工具。

## ⛔ 硬约束

1. **只用官方入口**：TFS、Redis 和知识库只使用 `COMMANDS.md` 列出的脚本、子命令或 MCP 工具。本 skill 的工作项流程不得直接调用 `tfs_client.py` 写子命令；TFS 写入只能来自校验通过的 `pipeline.py apply`，Redis 写入只能由 pipeline 内部发布。
2. **禁止临时客户端和旁路命令**：不得使用 `python3 -c`、Python heredoc、临时 Python/shell/jq 脚本、`redis-cli` 或直接 `curl` 操作 TFS/Redis，也不得为解读官方 JSON 输出另写脚本。
3. **禁止运行时安装依赖**：不得执行 `pip`、`pip3`、`uv pip`、`npm`、`brew` 等安装命令。依赖缺失时使用已有降级结果（如 `unsupported`）并记录限制。
4. **禁止交互**：不得使用 `AskUserQuestion`、向用户提问或等待确认。信息不足时按规则生成 checklist/`analysis_gaps` 并进入对应终局。
5. **限制源码核验**：禁止无锚点全仓搜索和大范围阅读源码。仅在 GitNexus 已给出精确对象、动作和技术锚点后，按证据指南有限核验关键文件；候选代码不得当作现场或部署事实。
6. **写入必须 `--execute`**：`apply` 默认 dry-run；所有终局分支都必须带 `--execute` 才会真实写 TFS。你在非交互模式下即是编排器，不可省略该参数。异常、版本冲突或状态不符时立即停止，绝不强转或继续后续动作。
7. **官方能力缺失就停止**：`COMMANDS.md` 没有对应能力时，记录 `ERROR`/`evidence_gap` 后停止；不得自行实现替代客户端、安装包或绕开执行器。

## 核心状态机

一次运行只使用一个 `run_id`，最终只生成一份 `执行计划_<id>_<run_id>.json`。

| 阶段 | 终局 | 后续动作 |
|---|---|---|
| 准入 | `SKIP-ANALYSIS` | 零 TFS 写入；记录审计与可选 Redis 摘要后停止 |
| 质控 | `NEED-INFO` / `NEED-REVIEW` | checklist、QC 标签、`state_to=null`；停止，不进分析 |
| 质控 | `PASS` | 内部闸门，不产计划、不 apply；直接进入分析 |
| 分析 | `AUTO-ANA` | 变更方案 + 标签；受控流转到「已分析」 |
| 分析 | `MANUAL-REVIEW` / `MANUAL-REVIEW-STOP` | 变更方案；必要时待确认清单；`state_to=null` |

高风险规则优先于 AUTO 启发式。质控 `NEED-*` 与分析 `MANUAL-REVIEW*` 永不流转状态；只有 `AUTO-ANA` 可以执行完整字段流转。

## 高层流程

严格按 [`_lib/RUNBOOK.md`](_lib/RUNBOOK.md) 的 0–11 步执行：

1. 读取命令索引与运行手册，完成 precheck。
2. 创建运行目录并 fetch 工作项，先判 `SKIP-ANALYSIS`。
3. 未跳过时处理附件，完成质控初判、按需补证与最终复核。
4. 质控挡回则生成 QC 计划并停止；PASS 才进入分析。
5. 完成证据闭环、变更方案、缺口分流和 AUTO/MANUAL 判定。
6. 依据执行契约生成唯一计划；先 validate，再 apply --execute（所有终局都必须带 `--execute`）。
7. 每次 validate、apply、execute、回读或环境阻断后追加测试日志。

## 产物边界

所有手工产物只能放在 `过程文件/<id>/<run_id>/`；来源附件放 `附件/`，解析副本放 `附件解析/`。执行器审计放 `过程文件/<id>/runs/`；跨运行测试记录只追加到 `过程文件/测试日志.md`。

| 终局 | 必需产物 |
|---|---|
| `SKIP-ANALYSIS` | 计划 JSON；无 TFS 附件 |
| `NEED-INFO` / `NEED-REVIEW` | 计划 JSON + 顶层 inline `checklist` |
| `AUTO-ANA` / `MANUAL-REVIEW*` | 计划 JSON + 变更方案；仅 `analysis_gaps` 非空时增加待确认清单 |

`PASS` 不是终态，不产独立计划。artifact 的 `path`/`filename` 只能是同一运行目录内的精确文件名，并包含同一 `run_id` 标记。

## 按条件读取规则

只读取当前阶段需要的文件；不要一次性加载全部引用。

| 时机 | 必读文件 |
|---|---|
| 始终 | [`_lib/COMMANDS.md`](_lib/COMMANDS.md)、[`_lib/RUNBOOK.md`](_lib/RUNBOOK.md) |
| 准入与质控 | [`config/qc-rules.md`](config/qc-rules.md)、[`references/pre-qc-rules.md`](references/pre-qc-rules.md)、[`references/qc-checklist.md`](references/qc-checklist.md)；仅标题归属存疑时读 [`references/project-keyword-dictionary.md`](references/project-keyword-dictionary.md) |
| 附件与补证 | 未跳过时读 [`_lib/attachment-evidence.md`](_lib/attachment-evidence.md)；需要 KB/wiki 补证时再读 [`_lib/knowledge-base.md`](_lib/knowledge-base.md)、[`references/qc-kb-recipe.md`](references/qc-kb-recipe.md)、[`_lib/module-location-recipe.md`](_lib/module-location-recipe.md)、[`_lib/wiki-kb-recipe.md`](_lib/wiki-kb-recipe.md) |
| 质控 PASS 后分析 | [`config/analysis-rules.md`](config/analysis-rules.md)、[`references/iteration-analysis-closure.md`](references/iteration-analysis-closure.md)、[`references/analysis-description-writing-rules.md`](references/analysis-description-writing-rules.md)、[`references/confidence-heuristic.md`](references/confidence-heuristic.md)、[`references/change-plan-template.md`](references/change-plan-template.md)；有 gaps 时读 [`references/manual-review-template.md`](references/manual-review-template.md)，AUTO 指派时读 [`references/assignee-rules.md`](references/assignee-rules.md) |
| 生成、校验和执行计划 | [`_lib/tfs/EXECUTION_CONTRACT.md`](_lib/tfs/EXECUTION_CONTRACT.md)；涉及字段流转时读 [`_lib/tfs/field-flow.md`](_lib/tfs/field-flow.md)；结束时读 [`_lib/testing-log.md`](_lib/testing-log.md) |

六类高风险以 [`_lib/high-risk-categories.md`](_lib/high-risk-categories.md) 为单一权威源。

## 新计划固定约束

- QC 计划使用 `rules_source={"qc":"pre-qc-v1"}`；分析计划固定 `version=2`、`rules_source={"qc":"pre-qc-v1","analysis":"evidence-loop-v1"}`。
- 分析计划必须带 `analysis_gaps`、`evidence_refs`、`evidence_gaps`；AUTO 的两类 gaps 必须为空且带非空 `auto_scopes`。
- 单一 `existing-ui-simple`、`print-adjustment` 或 `data-management` 命中简洁输出时，新计划使用 `analysis_profile=concise-v2`；`concise-v1` 仅用于历史计划兼容。
- `analysis_gaps` 只放 PM 可补齐的业务正确性信息；技术发现缺口只放 `evidence_gaps`，不得生成 TFS 待确认附件。

## 失败关闭

任一 TFS、规则、证据、计划校验、preflight、写字段或转状态异常都立即停止。能识别工作项与 `run_id` 时记录 `ERROR` 审计；不能识别时只输出错误，不伪造审计。并发版本冲突必须重新 fetch 并重建计划，不能盲目重试。

## 不做什么

- 不做 auto-dev 点火，不做 7 类/Stop-List；`PM-AI-STOP-AUTO` 在本仓只打标签。
- 不复用《汇报版》逻辑。
- 不把仓库、类、方法、接口、Mapper、表字段等技术定位写进 PM 变更方案正文。
