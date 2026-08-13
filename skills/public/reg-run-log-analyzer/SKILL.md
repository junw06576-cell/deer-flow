---
name: reg-run-log-analyzer
description: 分析 DeerFlow 需求分析任务（reg-auto-req-analysis）的 agent 执行日志，复盘每次 run 的步骤与结果。当用户要求分析/复盘/查看某个 TFS 需求工作项的 run 日志（如"分析 262327 的日志"、"看 262327 这次 run 干了啥"、"262327 日志复盘"、"查一下日志"）时使用，参数为需求工作项 ID。
---

# Run 日志分析器

复盘一次需求分析 run 的全过程：每一步做了什么、结果如何、是否异常、最终终局，输出结构化分析报告。

## 适用场景

用户想了解某个需求（如 262327）在 DeerFlow 中自动需求分析的执行过程和结果时。典型问法：

- "分析 262327 的日志"
- "看 262327 这次 run 干了啥"
- "262327 日志复盘一下"
- "查一下最近的需求分析日志"

## 执行步骤

### 1. 确认目标

- 从用户话里提取需求工作项 ID（纯数字，如 262327），构造 thread_id = `WN_PH-Platform-{需求ID}`
- 若用户给了 run_id，则按 run_id 过滤
- 若用户只说"最近的日志"，先执行下方定位命令找到最近的 run

### 2. 拉取日志（沙箱内已挂载宿主 docker.sock）

**方式一（首选）：用自带脚本直连 Docker API**（沙箱镜像可能没有 docker CLI，此方式零依赖）

```bash
# 按 thread 拉最近 24 小时日志（默认）
python3 /mnt/skills/public/reg-run-log-analyzer/_lib/fetch_gateway_logs.py WN_PH-Platform-262327

# 拉不到时放宽时间范围
python3 /mnt/skills/public/reg-run-log-analyzer/_lib/fetch_gateway_logs.py WN_PH-Platform-262327 --since-hours 72

# 加大尾部行数（日志量大时）
python3 /mnt/skills/public/reg-run-log-analyzer/_lib/fetch_gateway_logs.py WN_PH-Platform-262327 --tail 2000

# 定位最近的 run（列出最近创建的 run）
python3 /mnt/skills/public/reg-run-log-analyzer/_lib/fetch_gateway_logs.py Run-created
```

**方式二（备选）：若沙箱内 docker CLI 可用**

```bash
docker logs deer-flow-gateway --since 24h 2>&1 | grep "WN_PH-Platform-262327"
```

约束：

- 输出超过约 300 行时，用 `| head -200` / `| tail -100` 分段查看，避免一次读不完
- 同一需求一天内可能多次 run，注意按 `Run created: run_id=...` 行切分，逐个分析
- 若脚本报 `Permission denied`：docker.sock 权限问题，按下方"权限修复"处理
- 若脚本报 `/var/run/docker.sock 不存在`：socket 未挂载，检查 config.yaml sandbox.mounts 是否已加 docker.sock 挂载且 gateway 已重启

**权限修复（沙箱用户无权限访问 docker.sock 时，宿主上执行一次，重启后需重设）**：

```bash
chmod 666 /var/run/docker.sock
# 持久化（可选）：写入 docker socket systemd 配置后重启 docker 才稳定生效
# mkdir -p /etc/systemd/system/docker.socket.d
# printf '[Socket]\nSocketMode=0666\n' > /etc/systemd/system/docker.socket.d/override.conf
# systemctl daemon-reload && systemctl restart docker
```

### 3. 逐段解析

按 `references/analysis-rules.md` 的步骤类型字典和结果判定规则，把日志拆成"步骤-结果"清单：
识别每个 run 的边界、每步动作类型、pass/拦截/异常、终局标签。

### 4. 输出报告

按 `references/analysis-rules.md` 的报告模板输出，包含四部分：
① 这是什么（首次/重分析、需求标题、run_id 清单）→ ② 分步结果表（时间 | 步骤 | 干了啥 | 结果）→ ③ 关键观察（异常/拦截/可疑点，附证据）→ ④ 一句话结论（是否正常完成、有无产物、是否写回 TFS、需要人工跟进的点）。

## 注意事项

- 硬闸拦截（`禁止覆盖` / `PM-AI-MANUAL-PASSED`）是**设计内保护机制，不是故障**，报告中要说明被什么标签挡住、为什么
- 若 run 只有准备步骤（拉需求/下附件/查迭代）就收尾、无任何产物，判定为"正常收尾但未执行完"，需提示用户去服务器确认 run 真实状态
- 报告主体用业务语言，命令/标签保留原文作为证据
