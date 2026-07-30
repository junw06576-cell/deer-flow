# 2026-07-28 排查总结

## 环境

- 服务器：Linux，Docker Compose 部署 DeerFlow
- 端口 2026（Nginx）→ deerflow-service:8003 → Gateway:8001
- 网络：自定义 deer-flow bridge（192.168.201.0/24）
- devnet 云桌面，没有 AI 工具，全靠盲打

---

## 问题一：POST /api/v1/analysis 报 500

**原因**：`host.docker.internal` 是 Docker Desktop 专有的 DNS 名，Linux Docker Engine **不自带**。deerflow-service 容器里配的 `REDIS_URL=redis://host.docker.internal:26380/0` 解析不了。

**验证**：
```bash
docker run --rm alpine:latest ping -c 2 host.docker.internal
# → ping: bad address 'host.docker.internal'

docker run --rm --add-host host.docker.internal:host-gateway alpine:latest ping -c 2 host.docker.internal
# → 64 bytes from 10.18.0.1: icmp_seq=0 ttl=64 time=0.151 ms
```

**解决**：`docker-compose.yaml` 中 deerflow-service 加 `extra_hosts: host.docker.internal:host-gateway`。

---

## 问题二：Sandbox（skill）也连不上 Redis

**原因**：Sandbox 容器是 Gateway 动态 `docker run` 创建的，没有 `extra_hosts`，同样解析不了 `host.docker.internal`。

**解决**：改 `local_backend.py` 的 `_start_container()`，加 `--add-host host.docker.internal:host-gateway`。重新 build gateway 镜像后生效。

---

## 问题三：Agent 总是手写 Redis 代码，不用 pipeline.py

**原因**：AI agent 倾向于自己写内联 Python 脚本操作 Redis（`from redis_client import redis_client`，然后 `redis_client()` 当构造函数调），而不是执行 `pipeline.py validate`。这个调用是错的——`redis_client` 是模块不是类，`publish_plan()` 函数才是正确入口。agent 反复读源码、反复试错，消耗了大量工具调用。

**解决**：SKILL.md 加了两条硬约束：
1. **禁止读 `pipeline.py` / `redis_client.py` 的源码**，直接跑命令
2. **禁止手写任何 Redis 连接代码**（socket / 内联 Python）

同时发现 Sandbox 实际读的技能文件不是全局 `/opt/deer-flow/skills/`，而是用户私有副本 `/opt/deer-flow/backend/.deer-flow/users/auto-req-service/skills/`。**两个路径都要同步**。

---

## 问题四：GraphRecursionError（Recursion limit of 100 reached）

**原因**：`backend/packages/harness/deerflow/client.py` 第 235 行的默认值：

```python
recursion_limit=overrides.get("recursion_limit", 100),  # 默认就是 100
```

`config.yaml` 的 `max_recursion_limit: 1000` 只是**服务器上限**，不是实际值。调用方（deerflow-service）没传 `recursion_limit`，所以锁死在 100。

agent 跑完 QC 分析后还需要调工具写 Redis / apply 计划，100 步不够用。

**解决**：在 `auto-analysis-agent` 的配置文件 `/opt/deer-flow/agents/auto-analysis-agent/config.yaml` 中加 `recursion_limit: 300`，让 Gateway 使用更大的值。

---

## 根因汇总

| # | 问题 | 根因 | 修复 |
|---|------|------|------|
| 1 | deerflow-service 500 | Linux 无 host.docker.internal | extra_hosts |
| 2 | sandbox 连不上 Redis | 动态容器无 --add-host | 改 local_backend.py |
| 3 | agent 乱写 Redis 代码 | SKILL.md 约束不够硬 | 加硬约束 + 命令速查 |
| 4 | 跑不完就超时 | recursion_limit 默认 100 | agent 配置改 300 |

## 注意事项

1. 改 `local_backend.py` 后要重新 build gateway 镜像
2. `SKILL.md` 要同步两份（全局 + auto-req-service 用户私有）
3. Docker 容器默认 UTC 时间，调 `TZ=Asia/Shanghai` 看日志方便
4. 重装 / 重建环境时这些改动都要重新配置
