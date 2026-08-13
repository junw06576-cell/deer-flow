# deerflow-service 排查速查手册

> 适用场景：`http://172.16.0.192:2026/deerflow-service/api/v1/analysis` 出问题时的排查指南

---

## 一、架构速览（一张图看懂）

```
用户/前端/TFS
    │
    ▼
Nginx (端口 2026)
    │  /deerflow-service/ → proxy_pass
    ▼
deerflow-service (容器，内部端口 8003)
    │  ├─ POST /api/v1/analysis → 提交分析任务
    │  └─ GET  /api/v1/analysis/{task_id} → 轮询结果
    │
    ├── Redis (host.docker.internal:26380)
    │       └─ 存储任务状态 + QC 分析结果
    │
    └── DeerFlow Gateway (gateway:8001)
            └─ LangGraph agent 执行需求分析
```

**关键关系**：
- deerflow-service 自己用 3 样东西：**Redis**（存任务状态 + QC 结果 + 活跃 run 注册表 `run:active`）、**DeerFlow Gateway**（跑 agent）、**API Key 认证**
- 它只是个"调度员"——收请求，交给 DeerFlow agent 干活，结果写 Redis

---

## 二、常用 Docker 命令（就这几个，别记太多）

### 看状态
```bash
# 所有容器是不是都活着
docker ps | grep deer-flow

# 只看 deerflow-service
docker ps | grep deer-flow-service
```

### 看日志（大部分时候够用）
```bash
# 看最近 100 行
docker logs --tail 100 deer-flow-service

# 实时跟踪（按 Ctrl+C 退出，不删容器）
docker logs -f deer-flow-service
```

### 重启（不改代码时用这个，快！）
```bash
# 只重启 deerflow-service，不用 make up
docker stop deer-flow-service && docker rm deer-flow-service && docker compose -f docker/docker-compose.yaml up -d deerflow-service --no-deps
```

### 完整重构（改了代码 / Dockerfile 才用）
```bash
make up
```

### 删了重建（最彻底）
```bash
make down && make up
```

---

## 三、关键配置文件在哪

| 文件 | 作用 | 典型问题 |
|------|------|----------|
| `docker/docker-compose.yaml` | 容器配置：端口、环境变量、网络 | **REDIS_URL 配错**（#1 罪魁祸首） |
| `deerflow-service/.env` | 运行时环境变量（各环境的 Redis/Token） | 变量没生效或被覆盖 |
| `deerflow-service/main.py` | FastAPI 启动入口，注册路由 | 一般不动它 |
| `deerflow-service/routers/analysis.py` | API 逻辑：提交任务、轮询结果 | 500 的 traceback 基本指向这 |
| `deerflow-service/services/task_manager.py` | 任务管理：Redis 版 / 内存版 | Redis 连不上时在这报错 |
| `deerflow-service/services/deerflow_client.py` | 调用 DeerFlow Gateway 的客户端 | Gateway 挂了时在这报错 |
| `deerflow-service/config.py` | 读取环境变量 | REDIS_URL / API_KEY 来源 |

---

## 四、常见错误速查

### 报错 1：`redis.exceptions.ConnectionError: Error -2 connecting to host.docker.internal:26380`

**原因**：`host.docker.internal` 是 Docker Desktop（Windows/Mac）专用，Linux Docker 不认

**解决**：
1. 在 `docker-compose.yaml` 的 `deerflow-service` 下面加：
   ```yaml
   extra_hosts:
     - "host.docker.internal:host-gateway"
   ```
2. 然后只重启：
   ```bash
   docker stop deer-flow-service && docker rm deer-flow-service && docker compose -f docker/docker-compose.yaml up -d deerflow-service --no-deps
   ```

---

### 报错 2：任务提交成功（返回 task_id），但轮询一直 pending

**原因**：`_run_analysis_task` 在后台执行，可能卡住了

**排查**：
```bash
# 看日志里有没有 exception
docker logs --tail 50 deer-flow-service
```

常见子原因：
- run 在 Gateway 后台执行，状态由 `run_poller` 调度线程统一轮询（10s 间隔、总超时 1800s）；任务长期 `processing` → 检查 Gateway 容器与 run 状态
- `DEERFLOW_BASE_URL=http://gateway:8001` 不通 → 检查 Gateway 容器是否活着

---

### 报错 3：`HTTPException: 404 Task xxx not found`

**原因**：Redis 没有这个 task_id

**排查**：
- 用的是 Redis 版还是内存版？看 `.env` 里 `REDIS_URL` 有没有值
- 如果配了 Redis，任务可能已过期（24 小时自动删）
- 如果没配 Redis，容器重启后内存版的数据会丢

---

### 报错 4：`httpx.ConnectError / timeout` 从 deerflow_client 报出

**原因**：DeerFlow Gateway 容器没起来或网络不通

**排查**：
```bash
# 确认 Gateway 活着
docker ps | grep deer-flow-gateway

# 从容器内部测连通
docker exec deer-flow-service curl -s http://gateway:8001/api/v1/health
```

---

## 五、测试 API 是否正常

在服务器上：

```bash
# 1. 先测 health
curl -i http://127.0.0.1:8003/api/v1/health

# 2. 再提交一个分析任务
curl -i -X POST http://127.0.0.1:8003/api/v1/analysis \
  -H "Content-Type: application/json" \
  -H "X-API-Key: tfs-buddy-secret-key" \
  -d '{
    "collection_name": "Test_Project",
    "work_item_id": 99999,
    "tfs_project": "测试项目",
    "tfs_pat": "test-token",
    "redis_url": ""
  }'

# 3. 拿到 task_id 后轮询结果
curl -i http://127.0.0.1:8003/api/v1/analysis/{task_id} \
  -H "X-API-Key: tfs-buddy-secret-key"
```

---

## 六、一句话口诀

> **500 看日志 → Redis 连不上先查 extra_hosts → 任务不动查 Gateway → 还不行贴 traceback**

---

*生成时间：2026-07-28 | 对应 deerflow-service v2.0.0*
