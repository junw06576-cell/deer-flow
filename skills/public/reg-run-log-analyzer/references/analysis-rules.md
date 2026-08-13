# Run 日志分析规则

> 供 run-log-analyzer 使用。目标：把原始 agent 执行日志变成"步骤-结果"结构化复盘。

## 一、run 边界识别

- **起点**：`Run created: run_id=<id> thread_id=<thread>`（其前通常还有一条 `Thread created`）
- **终点**：run 结束后异步收尾的 `Memory update queued` → `Memory updated successfully`
- 同一 thread 一天内多次 run：按 run_id 分组，先数清有几个 run，再逐个拆
- thread_id 偶尔有笔误（如 `WN_PH-Platform-261866261866`），以 `Run created` 行的 thread_id 为准

## 二、步骤类型字典（按日志行特征归类）

| 日志特征 | 步骤 | 说明 |
|---|---|---|
| `Thread created` / `Run created` | 启动 | run 生命周期开始 |
| `Starting container` / `Created sandbox` | 沙箱 | Docker 沙箱容器创建/复用 |
| `[SandboxAudit] ... "command":"..."` | 具体动作 | 看命令本体判断动作：fetch=拉需求、ls/cat=查资料、pipeline.py=生成/校验/写回、tfs_client=拉取/附件/迭代、build_menu_business_index.py=知识路由 |
| 出现 `变更方案_*.md` | 产物 | 生成了变更方案文档 |
| 出现 `执行计划_*.json` | 产物 | 生成了执行计划 |
| `validate --plan` | 校验 | 执行计划自检，看输出是否含 ok |
| `apply --plan` | 写回 | 尝试写 TFS/Redis，重点看是否被闸 |
| `Redis` 相关命令 | 状态查询 | 查该需求历史状态 |
| `Memory update queued` → `... successfully` | 收尾 | run 正常结束后的异步记忆更新 |

## 三、结果判定

| 现象 | 判定 |
|---|---|
| `"verdict": "pass"` | 该步审计通过 |
| validate 输出含 `ok` | 计划校验通过 |
| apply 被拒，含 `禁止覆盖` / `DOWNSTREAM_TERMINAL_TAGS` / `PM-AI-MANUAL-PASSED` | **硬闸拦截（设计内保护，非故障）**：需求已被人工放行或已进入下游流程，重分析不得覆盖 |
| 出现 `ERROR` / `Traceback` / `Failed` / `401` | 异常，标注严重度 |
| Redis 查询失败 | 多为 key 不存在（首次需求未写入），不直接判异常，提示人工确认 |
| 全程只有准备步骤就 `Memory update` 收尾、无任何产物 | **"正常收尾但未执行完"**，提示去服务器查 run 真实状态或后续 run |

## 四、终局标签速查

| 标签 | 含义 |
|---|---|
| `PM-AI-MANUAL-REVIEW` | 需人工复核 |
| `PM-AI-STOP-AUTO` | 停止自动流程 |
| `PM-AI-MANUAL-PASSED` | 已人工放行（下游终局，禁止覆盖） |
| `AI-AUTO-DEV` / `AI-AUTO-DONE` / `AI-CODING` | 已进入/完成自动开发 |

## 五、报告模板

### ① 这是什么
- 需求：{ID} {标题}；类型：首次分析 / 重分析（依据是否对比上轮产物、用户是否补充反馈）
- run_id 清单（多个 run 逐个分析）

### ② 分步结果表

| 时间 | 步骤 | 干了啥 | 结果 |

### ③ 关键观察
- 异常/拦截/可疑点各列出，附日志原文证据
- 有拦截时说明被什么终局标签挡下、为什么
- 常见的系统性疑点：配置命名与需求号不一致、redis 端口纠偏、skill 文档/执行代码路径不一致、凭据明文进日志

### ④ 一句话结论
{是否正常完成} + {有无产物} + {是否写回 TFS} + {需要人工跟进的点}
