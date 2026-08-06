# 192 服务器 DeerFlow 运维手册

## 背景信息

| 项目 | 说明 |
|------|------|
| **部署服务器** | `192.16.0.192` |
| **项目目录** | `/opt/deer-flow` |
| **启动方式** | `make up`（Docker Compose 部署，走 `scripts/deploy.sh`） |
| **服务入口** | `http://192.16.0.192:2026` |
| **主要容器** | `deer-flow-gateway`（核心服务）、`deer-flow-frontend`（前端）、`deer-flow-nginx`（反向代理）、`deer-flow-redis`（缓存） |

> **📝 使用说明**：本手册中的所有命令均已登录服务器后的直接可执行命令，无需加 `ssh` 前缀。直接在终端粘贴执行即可。

> **🔐 API 认证说明**：当前部署启用了 BETTER_AUTH 登录认证，`/api/*` 接口（除 `/health`、`/docs` 等）都需要认证，**裸 curl 会返回 `401 Unauthorized`**。本手册所有 curl 命令统一通过内部 token 认证，执行前先取 token：
>
> ```bash
> # 从 .env 读取内部 token（每次新开终端会话都需要重新执行）
> TOKEN=$(grep '^DEER_FLOW_INTERNAL_AUTH_TOKEN=' /opt/deer-flow/.env | sed 's/.*=//')
> ```
>
> 之后手册中的 curl 命令均带 `-H "X-DeerFlow-Internal-Token: $TOKEN"` 使用。

---

## 场景一：更换 API Token

### 背景

当模型供应商的 API Key 过期、轮换或切换供应商时，需要更换 DeerFlow 的 API Token。

当前配置（`config.yaml` 第 22-33 行）：

```yaml
models:
- name: deepseek-v4-flash
  use: langchain_openai:ChatOpenAI
  api_key: $OPENAI_API_KEY       # 从环境变量读取
  base_url: $OPENAI_BASE_URL     # 从环境变量读取
```

Token 存在于 `.env` 文件中，通过 Docker `env_file` 注入容器。

### 操作步骤

#### 1. 编辑 `.env` 文件，替换 API Key

```bash
cd /opt/deer-flow
sed -i 's|^OPENAI_API_KEY=.*|OPENAI_API_KEY=你的新key|' .env
```

#### 2. 只重建 gateway 容器（不 rebuild 镜像，秒级）

```bash
docker compose --env-file .env -p deer-flow -f docker/docker-compose.yaml -f docker/docker-compose.dood.yaml up -d --force-recreate gateway
```

> **⚠️ 注意**：`--env-file .env -p deer-flow` 这两个参数缺一不可。  
> `--env-file .env`：将 `.env` 注入 compose 变量解析，否则 `$DEER_FLOW_CONFIG_PATH` 等变量为空导致报错。  
> `-p deer-flow`：指定项目名与 `make up` 一致，否则网络名冲突。  
> **`-f docker/docker-compose.dood.yaml` 必须保留**：该覆盖文件挂载 Docker socket（DooD），**缺失会导致沙箱容器无法创建**（报 `dial unix /var/run/docker.sock: no such file or directory`），需求分析 run 会秒级失败。本手册所有 gateway 重启命令均已带此参数。

#### 3. 验证

```bash
# 查看容器中环境变量是否已更新为新 key
docker exec deer-flow-gateway env | grep OPENAI_API_KEY

# 查看容器创建时间，确认是新创建的容器
docker inspect deer-flow-gateway --format '{{.Created}}'
```

#### 4. 验收

打开浏览器访问 `http://192.16.0.192:2026`，发送一条消息确认模型正常回复。如报错可查看日志：

```bash
docker logs deer-flow-gateway --tail 50
```

常见错误日志关键词：`401`、`Authentication`、`Invalid API key`，出现这些说明 key 仍不正确。

---

---

## 场景二：添加自定义全局 SKILL

### 背景

DeerFlow 的技能加载机制（`UserScopedSkillStorage`）：

| 技能目录 | 加载方式 | 说明 |
|---------|---------|------|
| `skills/public/` | **始终加载** | 全局可见，对所有用户生效 |
| `skills/custom/` | **仅作为 LEGACY 回退** | 仅当用户从未创建过自定义 skill 时才会加载，且只读 |
| `{DEER_FLOW_HOME}/users/{user_id}/skills/custom/` | **自定义 skill 实际位置** | 用户自己通过 UI 创建或安装的 skill 存在这里 |

> **关键点**：直接把 skill 丢到 `skills/custom/` 大概率不可见。因为一旦用户在 UI 上创建过任何一个自定义 skill，用户的专属目录 `{DEER_FLOW_HOME}/users/{user_id}/skills/custom/` 就存在了，全局 `skills/custom/` 就不再扫描。

---

### 方式一（推荐）：放入 `skills/public/`

所有用户都能看到，不用纠结用户隔离。

```bash
# 创建 skill 目录结构
mkdir -p /opt/deer-flow/skills/public/你的-skill名

# 创建 SKILL.md（必须包含 YAML 前置元数据）
cat > /opt/deer-flow/skills/public/你的-skill名/SKILL.md << 'EOF'
---
name: 你的-skill名
description: 简要描述这个 skill 的作用
---

# 你的 Skill 名称

这里写 skill 的具体指令内容。
EOF
```

**SKILL.md 要求：**
- 必须包含 YAML 前置元数据（`---` 包裹），至少要有 `name` 和 `description`
- `name` 只能用小写字母、数字和连字符（如 `my-custom-skill`）
- skill 目录名必须与 `name` 字段一致
- 每个 skill 一个独立子目录，目录下必须有 `SKILL.md`

**目录结构示例：**
```
/opt/deer-flow/skills/public/
├── data-analysis/
│   ├── SKILL.md
│   └── references/
│       └── template.json
└── my-skill/
    └── SKILL.md
```

#### 重启或重载技能

```bash
# 方式A：重启 gateway 容器（秒级）
docker compose --env-file .env -p deer-flow -f docker/docker-compose.yaml -f docker/docker-compose.dood.yaml up -d --force-recreate gateway

# 方式B：热重载（不重启容器）
curl -s -H "X-DeerFlow-Internal-Token: $TOKEN" -X POST http://192.16.0.192:2026/api/skills/reload
```

#### 验证

```bash
# 从 API 确认技能是否可见
curl -s -H "X-DeerFlow-Internal-Token: $TOKEN" http://192.16.0.192:2026/api/skills | python3 -m json.tool | grep '"name"'
```

---

### 方式二（替代）：放入 `skills/custom/`（LEGACY 回退）

仅适用于**全新用户**（从未在 UI 上创建过自定义 skill 的用户）。一旦用户创建过任何自定义 skill，这个方式失效。

目录结构和 SKILL.md 要求与方式一相同，只是将 `public` 换成 `custom`：

```bash
mkdir -p /opt/deer-flow/skills/custom/你的-skill名
```

---

### 排查：为什么放 `skills/custom/` 不生效

```bash
# 1. 确认目录结构是否正确
find /opt/deer-flow/skills/custom -name "SKILL.md"

# 2. 检查容器内文件是否同步（对比路径）
docker exec deer-flow-gateway ls /app/skills/custom/
docker exec deer-flow-gateway ls /app/backend/.deer-flow/users/

# 3. 查看用户是否有自定义 skill 目录（如果存在，LEGACY 回退被禁用）
docker exec deer-flow-gateway ls -la /app/backend/.deer-flow/users/ 2>/dev/null
# 如果有 users/ 目录，进一步看每个用户有没有自定义 skill
docker exec deer-flow-gateway find /app/backend/.deer-flow/users -type d -name "custom"

# 4. 确认 SKILL.md 格式是否正确（必须有 frontmatter）
docker exec deer-flow-gateway head -10 /app/skills/custom/你的-skill名/SKILL.md
```

### 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 放在 `skills/custom/` 但前端看不到 | 用户已有自定义 skill 目录，LEGACY 回退不生效 | 改用 `skills/public/` |
| 前端能看到了但 agent 调用不到 | 页面需要刷新或重新创建对话 | 刷新前端页面，新建对话 |
| SKILL.md 格式不对 | 缺少 YAML frontmatter 或 name 不符合规范 | 确认文件以 `---` 开头，包含 `name` 和 `description` |

---

---

## 场景三：增加全局自定义智能体（Agent）

### 背景

DeerFlow 的智能体（Agent）加载机制：

| 路径 | 说明 |
|------|------|
| `{DEER_FLOW_HOME}/users/{user_id}/agents/{name}/` | 用户级智能体，通过 UI 创建时自动生成 |
| **`/opt/deer-flow/agents/{name}/`**（推荐） | **全局共享**，所有用户可见，手动创建 |

**优先级**：用户级智能体会覆盖同名的全局智能体。

全局智能体目录 `/opt/deer-flow/agents/` 被挂载到容器内的 `/app/backend/.deer-flow/agents/`。

---

### 目录结构

```
/opt/deer-flow/agents/
├── 你的智能体名/
│   ├── config.yaml     # 必需的：智能体配置
│   └── SOUL.md         # 可选的：人格设定文件
```

### config.yaml 格式

```yaml
name: 智能体名             # 必须，与目录名一致，小写连字符
description: 描述文字      # 必须
skills:                   # 可选，绑定 skill 白名单
  - skill名1
  - skill名2
# tool_groups: 不配则默认加载全部工具组
# tool_groups: [] 则禁用所有工具
# recursion_limit: 500    # 可选，递归限制
```

### SOUL.md 格式（可选）

```markdown
# 我是谁

我是 XXX 智能体...

## 核心原则

- 原则1
- 原则2
```

Soul 文件定义智能体的人格、行为准则，会被注入到系统提示的 `<soul>` 块中。

---

### 操作步骤

#### 1. 创建智能体目录和配置文件

```bash
cd /opt/deer-flow

# 创建目录（目录名必须与 name 一致）
mkdir -p agents/你的智能体名

# 写 config.yaml
cat > agents/你的智能体名/config.yaml << 'EOF'
name: 你的智能体名
description: 这个智能体用来做什么
skills:
  - 要绑定的skill名
EOF
```

#### 2. （可选）写 SOUL.md

```bash
cat > agents/你的智能体名/SOUL.md << 'EOF'
# 我是谁

我是你的智能体名，专门负责 XXX。
EOF
```

#### 3. 重启 gateway 生效

```bash
docker compose --env-file .env -p deer-flow -f docker/docker-compose.yaml -f docker/docker-compose.dood.yaml up -d --force-recreate gateway
```

#### 4. 验证

```bash
# 确认前端能看到新的智能体
# 打开 http://127.0.0.1:2026，在智能体选择列表中应该能看到

# 容器内确认文件存在
docker exec deer-flow-gateway ls /app/backend/.deer-flow/agents/
```

---

### 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 前端看不到新智能体 | 未重启 gateway 或目录名与 `name` 不一致 | 重启 gateway，检查目录名 |
| agent 显示但 skill 没生效 | 旧对话缓存了旧配置 | **开新对话**（旧对话不会重新加载 agent 配置） |
| skill 在系统提示里但 agent 读不了 SKILL.md | sandbox 沙箱未启动 | 检查沙箱服务状态，或确认 skill 的说明已完整注入系统提示 |
| 智能体已有的用户级版本覆盖了全局版 | 用户已通过 UI 创建同名 agent | 删除用户级 agent 或用不同名字 |

---

## 场景四：挂载宿主机目录（如产品 Wiki 知识库）

### 背景

`product-kb-qa` 等 skill 需要读宿主机上的文件（如产品文档 Wiki），路径硬编码为 `/mnt/knowledge/wiki/`。需要同时配置两处：

1. **docker-compose 卷挂载** → 让文件对 gateway 容器可见
2. **config.yaml sandbox mounts** → 让文件对沙箱容器可见（沙箱是隔离的 Docker 容器）

> ⚠️ AioSandbox（DoOD 模式）创建独立的 Docker 容器来执行 `read_file`/`ls`/`bash` 等工具。所以挂载必须同时配置 gateway 容器和沙箱容器两层。

---

### 前置条件：Docker socket 挂载

AioSandbox 需要 Docker socket 才能创建沙箱容器。`make up` 已自动处理，但如果**手动重启 gateway** 必须带上 DooD 覆盖文件：

```bash
# ✅ 正确（带 DooD 覆盖）
docker compose --env-file .env -p deer-flow \
  -f docker/docker-compose.yaml \
  -f docker/docker-compose.dood.yaml \
  up -d --force-recreate gateway

# ❌ 错误（单独 -f docker-compose.yaml 会丢失 Docker socket）
```

---

### 操作步骤

#### 1. 网关层：docker-compose 卷挂载

在 gateway 服务的 `volumes:` 段增加一行：

```yaml
# docker/docker-compose.yaml（生产环境）
volumes:
  - /opt/regional_wiki_kb:/mnt/knowledge:ro
```

```yaml
# docker/docker-compose-dev.yaml（开发环境）
volumes:
  - /opt/regional_wiki_kb:/mnt/knowledge:ro
```

> 确保 `container_path` 与 skill 中硬编码的路径一致（`/mnt/knowledge` 而非 `/mnt/knowledge/wiki`）。目录结构不对时 skill 提示 "未挂载"。

#### 2. 沙箱层：config.yaml sandbox mounts

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  mounts:
    - host_path: /opt/regional_wiki_kb   # ⚠️ 宿主机路径（DooD 模式下）
      container_path: /mnt/knowledge
      read_only: true
  # ... 其他配置
```

> **关键区分**：`host_path` 在非容器模式下取 gateway 进程的路径，但在 DooD 容器模式下取**宿主机路径**，因为 `docker run` 是由宿主机 Docker daemon 执行的。

#### 3. 重启 gateway

```bash
docker compose --env-file .env -p deer-flow \
  -f docker/docker-compose.yaml \
  -f docker/docker-compose.dood.yaml \
  up -d --force-recreate gateway
```

#### 4. 验证

```bash
# 网关层：文件是否可见
docker exec deer-flow-gateway ls /mnt/knowledge/wiki_index.md

# 沙箱层：开新对话发消息，查看沙箱容器是否创建成功
docker ps --format '{{.Names}} {{.Status}}' | grep sandbox

# 日志排查
docker logs deer-flow-gateway 2>&1 | grep -i "sandbox" | grep -i "error\|failed" | tail -5
```

---

## 场景五：删除全局 SKILL

### 操作步骤

1. 删除 skill 目录
2. 重启 gateway 或热重载

```bash
# 1. 删除 skill
rm -rf /opt/deer-flow/skills/public/要删除的skill名

# 2. 方式A：重启 gateway（秒级）
docker compose --env-file .env -p deer-flow -f docker/docker-compose.yaml -f docker/docker-compose.dood.yaml up -d --force-recreate gateway

# 或方式B：热重载（不重启）
curl -s -H "X-DeerFlow-Internal-Token: $TOKEN" -X POST http://127.0.0.1:2026/api/skills/reload
```

#### 验证

```bash
# 确认列表中已无该 skill
curl -s -H "X-DeerFlow-Internal-Token: $TOKEN" http://127.0.0.1:2026/api/skills | python3 -m json.tool | grep '"name"'
```

---

## 场景六：删除用户级自定义 SKILL（同名残留覆盖全局）

### 背景

从 `custom` 挪到 `public` 后，前端仍显示为"自定义"——原因是用户级 `custom/` 目录里还残留了同名 skill，它的优先级高于全局 `public/`。

### 删除方法

```bash
# 1. 先确认用户级 custom 目录里有哪些 skill
docker exec deer-flow-gateway ls /app/backend/.deer-flow/users/你的user_id/skills/custom/

# 2. 删除目标 skill
docker exec deer-flow-gateway rm -rf /app/backend/.deer-flow/users/你的user_id/skills/custom/要删除的skill名

# 3. 刷新前端页面即可（无需重启 gateway）
```

### 验证

刷新前端 skill 列表页面，该 skill 应变为"内置"分类。如仍不更新，可热重载：

```bash
curl -s -H "X-DeerFlow-Internal-Token: $TOKEN" -X POST http://127.0.0.1:2026/api/skills/reload
```

---

## 场景七：查看需求分析任务执行日志

### 背景

`deerflow-service` 提交的需求分析任务，执行过程日志分布在两个地方：

| 日志源 | 容器 | 用途 |
|-------|------|------|
| 服务层日志 | `deer-flow-service` | 任务提交、状态变更、错误 |
| Agent 执行日志 | `deer-flow-gateway` | Agent 运行过程、每一步的耗时 |

---

### 1. 查看任务状态和耗时

```bash
# 根据 task_id 从 Redis 查执行时长
docker exec deer-flow-redis redis-cli HGETALL "task:你的task_id"

# 返回中有 created_at 和 updated_at（Unix 时间戳），两者相减即执行秒数
```

### 2. 查看服务层日志

```bash
docker logs deer-flow-service --tail 30
```

### 3. 查看 AI 需求分析日志（按需求 ID）

以需求 `WN_PH-Platform-261636` 为例：

```bash
# 查看最近 40 条该需求的日志
docker logs deer-flow-gateway 2>&1 | grep "WN_PH-Platform-261636" | head -40

# 动态跟踪该需求的实时日志（Ctrl+C 退出）
docker logs -f deer-flow-gateway 2>&1 | grep --line-buffered "WN_PH-Platform-261636"
```

> **说明**：将 `WN_PH-Platform-261636` 替换为实际的需求工作项 ID 即可查看对应需求的 Agent 执行日志。`--line-buffered` 确保 grep 实时输出匹配行，不会因缓冲区延迟。

### 4. 查看每一步执行耗时

```bash
# 用 SandboxAudit 审计日志分析每个 bash 步骤的耗时
docker logs deer-flow-gateway 2>&1 | grep "SandboxAudit" | grep "线程ID" | python3 -c "
import sys, json, re
lines = []
for line in sys.stdin:
    m = re.search(r'\{.*\}', line)
    if m:
        lines.append(json.loads(m.group()))
lines.sort(key=lambda x: x['timestamp'])
from datetime import datetime
for i, l in enumerate(lines):
    ts = l['timestamp']
    cmd = l['command'][:80]
    v = l['verdict']
    if i > 0:
        gap = (datetime.fromisoformat(ts) - datetime.fromisoformat(lines[i-1]['timestamp'])).total_seconds()
        print(f'  +{gap:.1f}s')
    print(f'{ts} [{v}] {cmd}')
print(f'\nTotal steps: {len(lines)}')
"
```

---

## 场景八：修改 deerflow-service 源码并生效

### 背景

`deerflow-service` 是用 Dockerfile 构建的独立镜像（`docker/docker-compose.yaml` 中 `build: ../deerflow-service`），不是 bind mount 方式挂载的。因此修改源码后，需要重新 build 镜像才能生效。

> 与 `config.yaml`、`docker-compose.yaml` 不同——那俩是通过卷挂载进去的，改完重启容器就生效。

### 操作步骤

#### 1. 修改本地源码

改 `deerflow-service/` 目录下的对应文件。

#### 2. 上传到服务器

将整个 `deerflow-service/` 目录覆盖到服务器的 `/opt/deer-flow/deerflow-service/`。

#### 3. 只重建 deerflow-service 镜像和容器

```bash
# 重新 build 镜像（只 build 这一个服务）
docker compose --env-file .env -p deer-flow \
  -f docker/docker-compose.yaml \
  -f docker/docker-compose.dood.yaml \
  build deerflow-service

# 重新创建容器
docker compose --env-file .env -p deer-flow \
  -f docker/docker-compose.yaml \
  -f docker/docker-compose.dood.yaml \
  up -d --force-recreate deerflow-service
```

> 只 build 这一个服务，不会动其他容器。

#### 4. 验证

```bash
docker logs deer-flow-service --tail 10
```

---

> *更多运维场景待补充。*
