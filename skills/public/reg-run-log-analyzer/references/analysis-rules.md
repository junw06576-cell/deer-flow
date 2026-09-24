# Run 日志分析规则（离线版 · reg-auto-req-analysis-offline）

> 供 reg-run-log-analyzer 使用。目标：把一次离线需求分析 run 的磁盘产物 + 跨运行留痕 +（可选）网关步骤日志，变成"步骤-结果"结构化复盘。

离线版与生产版（连 TFS/Redis）本质不同：**没有 TFS 写回、没有 Redis 状态、终局不是 `PM-AI-MANUAL-PASSED`/`禁止覆盖` 这类硬闸**。复盘的权威数据源是 run 目录下的三份产物与跨运行留痕，而非网关流水。

## 一、run 边界识别

- **run_id 形态**：`run_<YYYYMMDD>_<HHMMSS>_<工作项id>_<4-8位hex>`（正则 `^run_\d{8}_\d{6}_\d+_[0-9a-fA-F]{4,8}$`）。例：`run_20260902_143210_242042_a3f81c`。
- **两套落盘位置**（自动探测，详见 SKILL.md）：
  - 线上：`/mnt/user-data/outputs/<run_id>/`；跨运行留痕在 `/mnt/skills/过程文件/`（测试日志.md / 经验候选池.md / skill-feedback.md）。
  - 本地：`过程文件/<id>/<run_id>/`；跨运行留痕在 `过程文件/`（同 workspace `过程文件/` 根）。
- **一次分析 = 一个 run_id**：贯穿三产物文件名、输入/调用审计文件、运行标记、经验处理文件。同一工作项可能多轮 run（重分析/补充反馈），按 run_id 逐个复盘。
- **定位方式**：已知 run_id 直接定位；只有工作项号时用 `analyze_offline_run.py <id> --by-item` 列出该号下所有 run（新→旧），再选目标。

## 二、步骤类型字典（按离线 flow 0–9 归类）

离线 skill 的核心流程是 SKILL.md 的「核心流程（0–9）」。复盘时按这些步骤标签把产物映射成"做了什么"：

| 步骤 | 离线流程动作 | 在本 run 的判据（来自产物） |
|---|---|---|
| 0 读契约 | 校验规范载荷、读七源速查块 | `输入载荷_<run_id>.json` / `调用入参_<run_id>.json` 存在且结构校验通过；`check_deliverables` 不报 `INLINE_PAYLOAD_*` |
| 1 建 run | 探测模式 + 生成 run_id + 落盘输入 | run 目录存在、run_id 含 `<id>` 第三段、START_UTC 与 `published_at_utc` 一致 |
| 2 准入路由 | 纯联调/支持 → `SKIP-ANALYSIS` | verdict=`SKIP-ANALYSIS` 且 `skip_reason` 非空；无报告/清单 |
| 3 质控初判 | 知识前置拷问 + qc-rules 判定 | verdict 落在 `NEED-INFO`/`NEED-REVIEW`（出待确认清单）/ 或内部 `PASS` 进分析；`knowledge.source_status` 首源 `tfs-requirements` 状态可反映是否查了需求历史 |
| 4 KB 补证 | 命中判定面时按发现链榨干 | `knowledge.source_status` 五类（code-graph / source-code / database / wiki）状态非"本轮未使用"即表示调用了对应 MCP；`evidence_list` 非空即产出证据 |
| 5 QC 终局 | 交互确认/收敛 | `checklist` 对象非空（items ≤5、带 `CLOSE/PROVIDE` 标识）；非 QC 终局 `checklist=null` |
| 6 证据闭环+CP | 每项改动落 `CP-01…`，证据写 `knowledge.evidence_list` | `change_point_ids` 非空数组；报告 §五/§六 CP 与 `change_point_ids` 一一对应 |
| 7 写分析报告 | 直写七段报告 | `需求分析报告_<run_id>.md` 存在且含运行标记、七段标题、§六 含 `CP-01` |
| 8 写结果+自检+交付 | 写 `offline-result-v1` + 跑 `check_deliverables` + present_files | `需求处理结果_<run_id>.json` 存在、`schema=offline-result-v1`、`actionable=false`；测试日志「测试项与结果」记自检通过/失败 |
| 9 收尾留痕 | 反馈诊断 + 经验候选 record + 测试日志追加 | `经验处理_<run_id>.json` 存在；测试日志有 `auto-req-test-log:<run_id>` 条目；末条带经验候选四态 |

> 网关步骤级线索（可选）：走 `fetch_gateway_logs.py` 抓 `[SandboxAudit] "command":...`，按命令本体归类：
> `build_menu_business_index.py resolve-route`=产品路由（步骤 4 前置）；`skill_memory.py record`=经验候选（步骤 9②）；
> `check_deliverables.py`=结构自检（步骤 8）；`openssl rand -hex`/日期拼 run_id=建 run（步骤 1）。

## 三、结果判定

| 现象 | 判定 |
|---|---|
| `verdict` ∈ `AUTO-ANA` | 自动分析通过，建议标签 `PM-AI-AUTO-ANA`、state_to=已分析 |
| `verdict` ∈ `MANUAL-REVIEW(-STOP)` | 需人工复核；STOP 为命中高风险类别（单一权威源 `_lib/high-risk-categories.md`），强制移交人工 |
| `verdict` ∈ `NEED-INFO`/`NEED-REVIEW` | 质控未过闸，出待确认清单，需责任方补充后重触发新 run（非失败，是设计内收敛） |
| `verdict` = `SKIP-ANALYSIS` | 纯联调/支持，仅结果 + `skip_reason` |
| `knowledge.status` = `DEGRADED` | 知识源有异常（如五源未就绪、某源查询失败），看 `warnings` 与 `source_status` |
| `knowledge.warnings` 非空 | 非致命留痕，需人工留意（如五源未就绪、证据缺口收敛说明） |
| `evidence_gaps` 非空（报告 §六 CP）或 `MANUAL-REVIEW` 强制 | 技术证据不足，回退人工复核，**不是异常** |
| `check_deliverables` 输出 FAIL | 结构自检未过：缺产物/键集合漂移/CP 追踪失配/证据引用不可回查/描述不一致；3 轮仍 FAIL 记 `warnings` 后交付 |
| run 目录只有 `输入载荷`/`调用入参` 无任何产物 | **"建 run 但未收尾"**：可能 run 中断/超时（GraphRecursionError/沙箱并发上限），提示去服务器确认 run 真实状态 |

## 四、终局标签速查（离线 verdict）

| 标签 | 含义 | 产物组合 |
|---|---|---|
| `SKIP-ANALYSIS` | 纯联调/支持，无新增分析面 | 仅结果（skip_reason） |
| `NEED-INFO` | 质控缺材料 | 结果（checklist）+ 待确认清单 |
| `NEED-REVIEW` | 需责任方决策/复核 | 结果（checklist）+ 待确认清单 |
| `PASS` | 内部质控闸通过（不写盘，进阶段二） | 不直接出现，分析终局才落盘 |
| `AUTO-ANA` | 自动分析通过 | 报告 + 结果（knowledge+change_point_ids） |
| `MANUAL-REVIEW` | 需人工复核 | 报告 + 结果（knowledge+change_point_ids） |
| `MANUAL-REVIEW-STOP` | 命中高风险，停止自动 | 报告 + 结果 |

> 离线版**没有** `PM-AI-MANUAL-PASSED`/`禁止覆盖`/`DOWNSTREAM_TERMINAL_TAGS` 这类 TFS/Redis 硬闸。任何"被拦截"都是 QC 双轮收敛（NEED-*）或高风险早筛（STOP），属设计内，报告中要说明被哪个标签/规则挡下、为什么。

## 五、报告模板

### ① 这是什么
- 需求：{工作项 id} {标题（取自需求处理结果 work_item 或输入载荷 title）}；类型：首次分析 / 重分析（对比上轮产物或载荷含 human_feedback）
- 模式：线上（/mnt/user-data/outputs）/ 本地（过程文件/<id>/）
- run_id 清单（多 run 逐个分析）；执行窗口（published_at_utc ～ generated_at_utc）+ 总耗时

### ② 分步结果表（按离线步骤 0–9）
| 步骤 | 干了啥 | 结果/证据 |
|---|---|---|
| 0 读契约 | 载荷校验 + 七源速查 | 是否通过（check_deliverables INLINE_PAYLOAD_*） |
| 1 建 run | 模式探测 + run_id | 模式、START_UTC 一致 |
| 2 准入 | 是否 SKIP | SKIP 原因 / 进入质控 |
| 3 质控初判 | 知识前置 + qc 判定 | PASS / NEED-* |
| 4 KB 补证 | 发现链榨干 | 五源状态、evidence_list 条数 |
| 5 QC 终局 | 双轮收敛 | checklist 条目数/候选标识 |
| 6 证据闭环 | CP 落点 | change_point_ids、§五/§六 一致 |
| 7 写报告 | 七段报告 | 报告存在 + 运行标记 |
| 8 写结果+自检 | offline-result-v1 + check_deliverables | 自检 PASS/FAIL、present_files |
| 9 收尾留痕 | 经验候选 + 测试日志 | 经验处理文件、测试日志条目、四态 |

### ③ 关键观察
- 异常/拦截/可疑点各列出，附产物原文证据（verdict、warnings、`source_status`、`evidence_list` 边界）
- 有 NEED-*：说明被什么判据挡下、责任方是谁、下一步（重触发新 run）
- 有 MANUAL-REVIEW-STOP：点明命中哪类高风险
- 结构自检若 FAIL：列出未过项，提示是否 3 轮仍 FAIL 后交付
- 常见疑点：五源未就绪（DEGRADED）、evidence_gaps 非空强制 MANUAL、work_item.id 与 run_id 第三段不一致、published/generated 时序异常

### ④ 一句话结论
{是否正常收尾} + {终局} + {有无产物：报告/结果/清单} + {结构自检是否通过} + {需要人工跟进的点：责任方补充/人工复核/高风险移交}
