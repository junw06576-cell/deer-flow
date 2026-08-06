# 字段流转（field-flow）

> 把一个「需求」工作项从 已建议 推进到 已分析 并排期/指派的受控流程，落地《需求分析方法.md》步骤 16-17-19。
> 2026-07-28 在工作项 259681 实测通过（MANUAL-REVIEW 人工推流）。本文件是该流程的单一权威说明，`pipeline.py::apply_field_flow` 是其实现。

## 两种调用方式（共享同一实现 `apply_field_flow`）

1. **AUTO-ANA 自动**：`apply_plan` 对 `verdict=AUTO-ANA` 的计划，在「加标签」之后自动跑完整字段流转（替换旧的单步 `set-state` + 原样传 display name 的不完整实现）。
2. **独立 `flow` 子命令**：`pipeline.py flow --id <wid> [--execute]`，对任意工作项（含 MANUAL-REVIEW 人工确认后推流）单独跑，与 verdict/标签/描述/附件回写解耦。可选 `--plan`（兼容，取 wid/run_id/expected_*）。

## 流转时序（6 步，每步各自 `fetch_raw` 取最新 rev + `revision_guard` 防并发）

| # | 动作 | 字段 / 命令 | 值 |
|---|---|---|---|
| 1 | 写迭代路径 | `System.IterationPath` | 目标迭代 = **earliest**（`finishDate ≤ 北京时间期望日期` 中 **finishDate 最小者**；期望日取自 `Demand.Expected.date`） |
| 2 | 写开始日期 | `Microsoft.VSTS.Scheduling.StartDate` | 当前北京时间日期（`YYYY-MM-DD`） |
| 3 | 写完成日期 | `Microsoft.VSTS.Scheduling.FinishDate` | `earliest.finish` 转北京时间后的日期（`YYYY-MM-DD`） |
| 4 | 转状态 → 活动 | `set-state 活动` | TFS 自动填 `Microsoft.VSTS.Common.ActivatedBy`/`ActivatedDate` |
| 5 | 转状态 → 已分析 | `set-state 已分析` | TFS 自动填 `Winning.AnalysisBy`/`Winning.AnalysisDate`（终态） |
| 6 | 指派 | `set-assignee` | `WINNING\账号`（见下方指派优先级） |

步骤 1-3 仅当算得出 earliest 迭代才执行；否则降级跳过迭代与起止日期，继续 4-6。

> 上表「字段 / 命令」列为操作语义；可运行的完整命令（含 `pipeline.py flow` 一键跑完 6 步、`--expected-rev` / `--assignee` 均可选）见 [`../COMMANDS.md`](../COMMANDS.md)。

## 4 条硬约束（踩坑，务必遵守）

1. **身份字段写入必须 `WINNING\账号` 格式**：`System.AssignedTo`、`Winning.AnalysisBy` 等身份型字段，写入值必须是 `WINNING\zhang_dong` 这样的账号格式。bare display name（「张栋」「刘云飞」）TFS 一律报「字段"X"的标识值"Y"是未知标识」HTTP 400。读回时 TFS 自动解析成 `account(中文名) <WINNING\account>` 全格式。`assignee-rules.md` 给的是中文名+括号账号，实际写入要转 `WINNING\账号`（`pipeline.resolve_assignee_to_winning` 自动转换）。

2. **激活/分析者 + 日期由 TFS 转态自动填，绝不手写**：`ActivatedBy`/`ActivatedDate` 在转「活动」时自动填（=当前 PAT 身份/now）；`Winning.AnalysisBy`/`Winning.AnalysisDate` 在转「已分析」时自动填。手写它们会失败：`AnalysisDate` 报「InvalidNotEmpty」规则错、`AnalysisBy` 报「未知标识」。→ 只需转状态，身份/日期让 TFS 自动填。

3. **状态两步走（已建议→活动→已分析）更安全**：直转 已建议→已分析 未验证；两步已证明可行，且确保 ActivatedBy/Date 被填。`set-state` 的 noop 逻辑（cur==target 跳过）天然覆盖已处「活动」/「已分析」的情况；**入口已是「已分析」则跳过两步 set-state**（避免倒退 PATCH 被 TFS 拒），仅补字段+指派。

4. **迭代 finish 严禁 > 期望日期**：交付迭代必须在期望日期前完成。`list_iterations` 在 `finishDate ≤ expected_date` 的候选里同时算两个派生迭代，**取向相反、各管各的**：
   - **`earliest`**（**提交截止未过**的候选里 finishDate 最小者）——**字段流转排期用这个**：下界 = `finishDate − 7 天 ≥ 今天`（代码提交截止还没过，排除来不及开发 / 已结束的历史迭代，避免选到 2022 这种老迭代）；多个满足时优先排到更早的那个（例：期望 260907、今天 07-28，候选 260814/260828 → 排 260814；260731 提交截止已过 → 排除）。
   - **`matched`**（finishDate 最大者，最晚能赶上，**不加下界**——取 max 天然不受历史迭代污染）——质控「时效余量」基准（见 `references/pre-qc-rules.md §三.3` 的 `matched.finish − 7 天` 代码提交截止），**排期不用它**。
   earliest 无可排候选（都过了提交截止）时为 None（matched 仍可能存在）→ 跳过迭代与日期，不阻断状态流转；无期望日期 / 查询失败同理。

## 指派优先级

`--assignee` 显式覆盖 > `Winning.Dev.Leader` 字段（首选，TFS 已指定的开发组长）> 计划 `assignee_to`（`assignee-rules.md` 规则提示，回退）。三者都经 `resolve_assignee_to_winning` 收敛为 `WINNING\account`；都解析不出 → 跳过指派记候选，不阻断。

## 边界与降级（遵循 EXECUTION_CONTRACT：不强转、不回滚、按步 degrade）

| 场景 | 处理 |
|---|---|
| WI 已「已分析」 | 跳过两步 set-state，仅补字段+指派（幂等，不倒退） |
| WI「活动」 | 只 set-state 已分析（set-state 活动 noop） |
| WI「已建议」 | 活动→已分析 两步 |
| 其它态（已关闭等） | set-state 被 TFS 状态机拒 → `checked_call` 抛错中断+ERROR，**不强转** |
| 无 `Demand.Expected.date` / 无 earliest / 迭代查询失败 | 跳过迭代+开始/完成日期，继续状态+指派 |
| 多个满足 finish≤期望 的迭代 | 排期取 `earliest`（提交截止未过里 finishDate 最小者）；**不取** `matched`（最晚，那是质控时效基准） |
| 所有候选都过了提交截止（earliest=None） | matched 仍可能存在；排期跳过迭代与日期，继续状态+指派（不阻断） |
| `Winning.Dev.Leader` 空/不可解析 且无 fallback | 跳过指派记候选，不阻断 |
| 指派 HTTP400（身份解析失败） | 降级记 error 动作，**不中断**（WI 已到终态，指派与终局解耦） |
| write-field / set-state 失败 | `checked_call` 抛错中断+ERROR（核心正确性，不降级） |
| 并发改 WI（mid-flow） | 该步 `revision_guard` test /rev 失败 → 中断 |
| dry-run | 每原语返预演不 PATCH；flow 返 actions 预演 |
| 重跑（已终态+字段已设） | write_field 覆写同值、set_state noop（幂等可接受） |

## 时间口径

`fetch` 保留 TFS 原始 `expectedDateRaw`，并把 `expectedDate` 转为 `Asia/Shanghai (+08:00)` ISO 时间。`list_iterations` 对纯日期、UTC ISO、带偏移 ISO 统一先转换为北京时间业务日期，再与迭代截止日比较；例如 `2026-09-16T16:00:00Z` 按 2026-09-17 处理。无法解析的期望时间或迭代截止时间返回 `ok:false`，不得继续猜测排期。`generated_at_utc` 仍保持 UTC 契约。

## 审计

- **AUTO-ANA 路径**：flow 的 actions 已 extend 进 `apply_plan` 的 actions，随 `tfs.record(verdict=AUTO-ANA, ...)` 落 `过程文件/<wid>/runs/`。
- **独立 flow**：`flow_item` 生成 `run_id`（`flow_<YYYYMMDDHHMMSS>_<hex8>`，缺省自动），调 `tfs.record(verdict='FIELD-FLOW', state_from, state_to='已分析', ...)` 落同一 `runs/` 目录，与其它审计同构可检索。

## 验证记录

- 2026-07-28 工作项 259681（云阳卫健委-物资台账月结明细导出）：rev 4→10，已建议→活动→已分析，迭代 V6.0.2608.28，指派 张栋（取自 Winning.Dev.Leader），开始 07-28 / 完成 08-28，激活/分析者+日期由转态自动填为 PAT 身份 lyf7(刘云飞)。详见 `过程文件/测试日志.md` 条目 `log-259681-auto-259681-20260728-5a15c-flow`。
