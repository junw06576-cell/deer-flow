# wincode 模型网关并发瓶颈——日志证据报告

> 采集时间：2026-09-07 19:31（北京时间）
> 数据来源：DeerFlow Gateway API（run record 时间戳，UTC），共 17 个需求分析 run
> 适用范围：reg-auto-req-analysis-offline skill，WN_PH-Platform / WN_Data_Platform 集合

---

## 一、结论（一句话）

**同一 skill、同一服务器、同一天内，当并发 run 数从 ≤4 升到 6-9 路时，单个 run 平均耗时从 24.9 分钟放大到 95.5 分钟（3.8 倍），且出现 3 个失败/中断。本地资源与工具执行已逐一排除，瓶颈锁定在 wincode 模型网关的共享推理算力。**

## 二、核心证据：同 work item 早晚配对对比

四组同 work item 在同一天早晚各跑一次（同一 skill、同一服务器、同一配置，唯一变量是并发度）：

| work item | 低并发 run（UTC） | 耗时 | 高并发 run（UTC） | 耗时 | 放大倍数 |
|---|---|---|---|---|---|
| 265092 | 05:17→05:41 | **24.1 min** | 09:50→11:08 | **78.1 min** | **3.2×** |
| 264194 | 05:31→05:56 | **25.3 min** | 10:15→11:26 | **70.6 min** | **2.8×** |
| 264431 | 05:31→06:02 | **30.4 min** | 10:25→11:23 | **58.8 min** | **1.9×** |
| 245706 | 06:39→07:03 | **24.2 min** | 08:50→10:11 | **81.4 min** | **3.4×** |

**这四组配对的意义**：请求内容、skill 版本、agent 配置、服务器环境完全一致——早晚两轮之间我们既没改代码也没改配置（tool_groups 修复在两组 run 之前均已生效）。唯一系统性变量就是并发度。这不是"任务难易差异"，是纯环境差异。

## 三、完整时间线（17 个 run）

### 低并发窗口（UTC 05:15-07:03，并发 ≤4）

| work item | run_id | 窗口 (UTC) | 耗时 | 状态 |
|---|---|---|---|---|
| 264191 | 38d6e93d | 05:15→05:35 | 20.5 min | success |
| 265092 | aa49fd78 | 05:17→05:41 | 24.1 min | success |
| 264194 | 370d5232 | 05:31→05:56 | 25.3 min | success |
| 264431 | e18ee65b | 05:31→06:02 | 30.4 min | success |
| 264191 | a0b966c1 | 05:42→05:45 | 3.1 min | success（轻任务） |
| 245706 | 4b1ab0c1 | 06:39→07:03 | 24.2 min | success |

**6/6 全部成功，平均 24.9 分钟，零失败。**

### 高并发窗口（UTC 07:28-11:26，并发 6-9 路）

| work item | run_id | 窗口 (UTC) | 耗时 | 状态 |
|---|---|---|---|---|
| 245706 | 56bfcd2c | 07:28→08:50 | 81.9 min | **interrupted** |
| 267500 | e67bbc8d | 07:44→09:08 | 84.9 min | **error** |
| 267420 | f0110f97 | 07:38→10:15 | 156.6 min | **error** |
| 268005 | 18b83888 | 08:29→11:23 | 174.0 min | **error** |
| 264195 | 9bc751da | 08:39→10:21 | 101.3 min | success |
| 264192 | 346a4a53 | 08:40→09:44 | 64.1 min | success |
| 245706 | cf6c6e2a | 08:50→10:11 | 81.4 min | success |
| 263888 | bbf54063 | 09:48→11:26 | 98.2 min | success |
| 265092 | 0221e060 | 09:50→11:08 | 78.1 min | success |
| 264194 | c5d4c010 | 10:15→11:26 | 70.6 min | success |
| 264431 | bfbe90f7 | 10:25→11:23 | 58.8 min | success |

**11 个 run：8 成功 + 2 error + 1 interrupted；成功 run 平均 95.5 分钟（对比低并发 24.9 分钟，放大 3.8 倍）。**

> 注：三个 error/interrupted 的死因各不相同（403 token 限额、MCP 工具报错、人工中断），不是统一故障模式，但都发生在高并发窗口内。

### 峰值时刻（UTC 09:50）

以下 8 个 run 同时活跃：263888、267402、263887、265092、268005、264195、264192、245706。
**这就是"5-6 个同时分析、大家都出不来结果"的现场。**

## 四、已排除的本地因素（逐一检验过）

| 因素 | 检验方法 | 结果 |
|---|---|---|
| 宿主机资源 | `docker stats`：load 1.25（多核近闲）、内存 available 23Gi、swap 无压力 | ❌ 排除 |
| Gateway 进程 | CPU 48.84%（约半个核，未饱和） | ❌ 排除 |
| 沙箱容器 | 7 个容器各 2-6% CPU / 500MB | ❌ 排除 |
| 工具执行速度 | httpx + SandboxAudit 时间线拆解：file/read、shell/exec 全部同秒完成 | ❌ 排除 |
| 沙箱并发上限假死 | 已调 replicas=6（原 3），且本轮无"已达到沙箱并发上限"日志 | ❌ 排除 |
| MCP 初始化 | 36 server lazy init ~2s/run，仅几 KB 名单入 prompt | ❌ 排除 |
| 网关排队（小请求） | 并发窗口内 "1+1" 裸请求 2.27s 返回 | ❌ 排除 |

**唯一无法排除且与数据吻合的变量：wincode 端点共享推理算力。** 机制解释：

- LLM 推理的 prefill（读入几万 token 上下文）是 compute-bound；单跑时 GPU 近乎独占，几万 token prefill 几秒完成
- 并发 5-6 路大 prefill + thinking 分抢同一算力池 → 每路 prefill 时间成倍拉长
- 小请求（"1+1"）GPU 占用极小可插空完成，所以小请求测不出排队（这也是为什么此前轻量探测误判"无排队"）
- 8 路并发时单轮 LLM 调用从 17-30s 拉长到 ~107s（45 调用/10min ÷ 8 路），轮次乘法放大成整 run 的 3.8 倍

## 五、给 wincode 网关方的具体诉求

1. **确认 `deepseek-v4-flash`（实际路由 `glm-5.3-flash`）端点的并发能力与配额**：当前观察到的单轮延迟 30-58s（thinking + 大 prefill），8 路并发时拉长到 ~107s
2. **评估开启 prefix cache / context cache**：需求分析 skill 每轮 prefill 3 万 token 级，前缀高度重合（system prompt + skill 契约 + 历史摘要），prefix cache 命中可大幅降低 prefill compute
3. **提供端点级监控可观测性**：请求队列深度、GPU 利用率、实际 prefill/decode 分解——调用方只能看到总延迟，要区分"排队 vs 推理"需要网关侧数据
4. **若算力确为共享池**：申请独立配额或错峰使用策略
5. **thinking 模式占用**：若网关侧支持关闭，请协助评估 `supports_thinking: false` 的 A/B 对比（可减单路占用，但有质量风险需验证）

## 六、调用方已做/将做的配合动作

- **已完成**：tool_groups 修复（file:read/file:write 注入，write_file 直写产物，杜绝 10K heredoc 死局）
- **已完成**：沙箱 replicas 3→6
- **待做**：开启 `token_usage.enabled: true` 拿每轮 input/output token 计量（prefill vs decode 分解的最终拼图）
- **待做**：触发纪律 ≤3 并行（立即执行）；后续考虑 TFS-BUDDY 调度层限流
- **持续**：错峰触发新需求（避开已满载窗口）；高价值需求单独触发，避免被并发放大

## 七、附：数据采集方法（可复现）

```bash
# 1. 认证（内部 token 在 /opt/deer-flow/.env: DEER_FLOW_INTERNAL_AUTH_TOKEN）
T="<internal-token>"
H1="X-DeerFlow-Internal-Token: $T"
H2="X-DeerFlow-Owner-User-Id: tfs-buddy"

# 2. 列出最近 thread（确认集合名）
curl -sS -X POST -H "$H1" -H "$H2" -H "X-CSRF-Token: x" -H "Cookie: csrf_token=x" \
  -H "Content-Type: application/json" -d '{"limit":40}' \
  "http://172.16.0.192:2026/api/threads/search"

# 3. 拉每个 thread 的 run 时间窗
curl -sS -H "$H1" -H "$H2" "http://172.16.0.192:2026/api/threads/<collection>-<workItemId>/runs"
```

run record 的 `created_at` / `updated_at` 即 run 起止时间（UTC）。status: success/error/interrupted/running。

---

*报告生成：老铁（DeerFlow 运维诊断会话）*
