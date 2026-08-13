# 192 服务器 DeerFlow 运维手册

## 背景信息

| 项目 | 说明 |
|------|------|
| **部署服务器** | `172.16.0.192` |
| **项目目录** | `/opt/deer-flow` |
| **启动方式** | `make up`（Docker Compose 部署，走 `scripts/deploy.sh`） |
| **服务入口** | `http://172.16.0.192:2026` |
| **主要容器** | `deer-flow-gateway`（核心服务）、`deer-flow-frontend`（前端）、`deer-flow-nginx`（反向代理）、`deer-flow-redis`（缓存） |

> **📝 使用说明**：本手册中的所有命令均已登录服务器后的直接可执行命令，无需加 `ssh` 前缀。直接在终端粘贴执行即可。

> **🔐 API 认证说明**：当前部署启用了 BETTER_AUTH 登录认证，`/api/*` 接口（除 `/health`、`/docs` 等）都需要认证，**裸 curl 会返回 `401 Unauthorized`**。本手册所有 curl 命令统一通过内部 token 认证，执行前先取 token：
>
> ```bash
> # 从 .env 读取内部 token（每次新开终端会话都需要重新执行）
> > ```
>
> 之后手册中的 curl 命令均带 `-H "X-DeerFlow-Internal-Token: $TOKEN"` 使用。

---

## 场景一：更换模型 API Key

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

打开浏览器访问 `http://172.16.0.192:2026`，发送一条消息确认模型正常回复。如报错可查看日志：

```bash
docker logs deer-flow-gateway --tail 50
```

常见错误日志关键词：`401`、`Authentication`、`Invalid API key`，出现这些说明 key 仍不正确。

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

# 方式B（修正版）：管理员登录 + CSRF 双重提交触发热重载（不重启容器）
#   说明：/api/skills/reload 是 admin-only 的 POST，内部 token 调不动，必须走管理员 session
ADMIN_EMAIL="${DEER_FLOW_ADMIN_EMAIL:-506391157@qq.com}"; ADMIN_PASS="${DEER_FLOW_ADMIN_PASSWORD:-Admin@123}"
CJ="/tmp/deerflow_reload.cookies"; rm -f "$CJ"
curl -s -c "$CJ" --data-urlencode "username=$ADMIN_EMAIL" --data-urlencode "password=$ADMIN_PASS" http://127.0.0.1:2026/api/v1/auth/login/local
CSRF=$(grep csrf_token "$CJ" | awk '{print $NF}')
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -X POST http://127.0.0.1:2026/api/skills/reload
rm -f "$CJ"
```

#### 验证

```bash
# 从 API 确认技能是否可见（GET 免 CSRF，用内部 token 即可）
TOKEN=$(grep '^DEER_FLOW_INTERNAL_AUTH_TOKEN=' /opt/deer-flow/.env | sed 's/.*=//')
curl -s -H "X-DeerFlow-Internal-Token: $TOKEN" http://127.0.0.1:2026/api/skills | python3 -m json.tool | grep '"name"'
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

# 方式B（修正版）：管理员登录 + CSRF 双重提交触发热重载（不重启容器）
#   说明：/api/skills/reload 是 admin-only 的 POST，内部 token 调不动，必须走管理员 session
ADMIN_EMAIL="${DEER_FLOW_ADMIN_EMAIL:-506391157@qq.com}"; ADMIN_PASS="${DEER_FLOW_ADMIN_PASSWORD:-Admin@123}"
CJ="/tmp/deerflow_reload.cookies"; rm -f "$CJ"
curl -s -c "$CJ" --data-urlencode "username=$ADMIN_EMAIL" --data-urlencode "password=$ADMIN_PASS" http://127.0.0.1:2026/api/v1/auth/login/local
CSRF=$(grep csrf_token "$CJ" | awk '{print $NF}')
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -X POST http://127.0.0.1:2026/api/skills/reload
rm -f "$CJ"
```

#### 验证

```bash
# 确认列表中已无该 skill（TOKEN 已取过可跳过第一行）
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
# 方式B（修正版）：管理员登录 + CSRF 双重提交触发热重载（不重启容器）
#   说明：/api/skills/reload 是 admin-only 的 POST，内部 token 调不动，必须走管理员 session
ADMIN_EMAIL="${DEER_FLOW_ADMIN_EMAIL:-506391157@qq.com}"; ADMIN_PASS="${DEER_FLOW_ADMIN_PASSWORD:-Admin@123}"
CJ="/tmp/deerflow_reload.cookies"; rm -f "$CJ"
curl -s -c "$CJ" --data-urlencode "username=$ADMIN_EMAIL" --data-urlencode "password=$ADMIN_PASS" http://127.0.0.1:2026/api/v1/auth/login/local
CSRF=$(grep csrf_token "$CJ" | awk '{print $NF}')
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -X POST http://127.0.0.1:2026/api/skills/reload
rm -f "$CJ"
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
docker logs deer-flow-gateway 2>&1 | grep "SandboxAudit" | grep "thread_id" | python3 -c "
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

## 场景九：从 TFS 仓库拉取更新 SKILL（以 reg-auto-req-analysis 为例）

### 背景

| 项目 | 说明 |
|------|------|
| **TFS 仓库** | `http://tfs2018-web.winning.com.cn:8080/tfs/WinCode/Skill/_git/reg-auto-req-analysis`（skill 的提交仓库） |
| **服务器目标** | `/opt/deer-flow/skills/public/reg-auto-req-analysis`（部署消费方，目录只读挂载给沙箱） |
| **同步方向** | **TFS → 服务器，纯拉取，绝不推送** |

> **🔑 认证说明**：TFS 2018 非交互环境 git 认证只能走「空用户名 + PAT 注入 Authorization 头」。**不要用 credential store 的空用户名条目**（`http://:PAT@host`）——在 Linux 服务器的 git 上不兼容，会回退交互式账号密码提示导致认证失败。以下脚本已内置正确认证方式。

---

### 操作步骤

#### 1. 将脚本放到服务器（本地源文件：`scripts/pull-reg-auto-req-analysis.sh`）

可直接 scp 上传，或服务器上直接创建：

```bash
cat > /opt/deer-flow/scripts/pull-reg-auto-req-analysis.sh << 'SCRIPT_EOF'
#!/usr/bin/env bash
# pull-reg-auto-req-analysis.sh
# 从 TFS 仓库拉取 reg-auto-req-analysis skill 到 DeerFlow 服务器（纯拉取，绝不推送）
#
# 两种模式自动判断：
#   - 目标目录已是 git 仓库（remote 指向 TFS）→ 增量 pull（日常更新走这里）
#   - 目标目录不存在 / 不是 git 仓库 / remote 不对 → 挪走旧目录 + 全量 clone（首次或重置）
#
# 用法（二选一，推荐方式 1，PAT 不进 shell history）：
#   1. export TFS_PAT="你的PAT" && bash pull-reg-auto-req-analysis.sh
#   2. bash pull-reg-auto-req-analysis.sh "你的PAT"
set -uo pipefail

SKILL_DIR="/opt/deer-flow/skills/public/reg-auto-req-analysis"
REPO_URL="http://tfs2018-web.winning.com.cn:8080/tfs/WinCode/Skill/_git/reg-auto-req-analysis"

# ---- PAT 来源：环境变量优先，其次脚本参数 ----
PAT="${TFS_PAT:-}"
[ -z "$PAT" ] && [ $# -ge 1 ] && PAT="$1"
if [ -z "$PAT" ]; then
  echo "错误：未提供 PAT。用法：export TFS_PAT=xxx 后执行，或 bash 本脚本 \"PAT\""
  exit 1
fi

# TFS 2018 basic auth：直接注入 Authorization 头
# （credential store 的空用户名条目在 Linux git 上不兼容，会回退交互提示，故不用）
AUTH_B64="$(printf '%s' ":$PAT" | base64 -w0)"
gitc() { git -c http.extraHeader="AUTHORIZATION: Basic ${AUTH_B64}" "$@"; }

echo "===== [1/5] 探测远程仓库 ====="
if ! gitc ls-remote "$REPO_URL" >/dev/null 2>&1; then
  echo "  ✘ 远程仓库不可访问（认证失败或仓库不存在），中止"
  exit 1
fi
echo "  ✔ 远程仓库可访问"

echo "===== [2/5] 判断本地仓库状态 ====="
CUR_REMOTE="$(git -C "$SKILL_DIR" remote get-url origin 2>/dev/null || true)"
if [ -d "$SKILL_DIR/.git" ] && [ "$CUR_REMOTE" = "$REPO_URL" ]; then
  echo "  ✔ 已是 TFS git 仓库 → 增量 pull 模式"
  echo "===== [3/5] git pull ====="
  cd "$SKILL_DIR" || exit 1
  if gitc pull origin master; then
    echo "===== 完成 ====="
    git log --oneline -3
    echo "（本脚本不执行任何 push，TFS 仓库不会被改动）"
    exit 0
  else
    echo "  ✘ pull 失败：可能本地有未提交修改，请先检查: git -C $SKILL_DIR status"
    exit 1
  fi
fi
if [ -d "$SKILL_DIR" ]; then
  echo "  ⚠ 目录存在但不是 TFS 仓库（remote=${CUR_REMOTE:-无}）→ 全量 clone 模式"
else
  echo "  - 目录不存在 → 全量 clone 模式"
fi

TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${SKILL_DIR}.old.${TS}"

echo "===== [4/5] 挪走现有目录 ====="
if [ -d "$SKILL_DIR" ]; then
  mv "$SKILL_DIR" "$BACKUP_DIR"
  echo "  ✔ 已挪走: $BACKUP_DIR"
fi

echo "===== [5/5] clone 远程仓库 ====="
if ! gitc clone "$REPO_URL" "$SKILL_DIR"; then
  echo "  ✘ clone 失败"
  if [ -d "$BACKUP_DIR" ]; then
    rm -rf "$SKILL_DIR" 2>/dev/null
    mv "$BACKUP_DIR" "$SKILL_DIR"
    echo "  ↺ 已回滚到原目录"
  fi
  exit 1
fi
echo "  ✔ clone 完成"

echo "===== 验证 ====="
git -C "$SKILL_DIR" log --oneline -5
echo "  remote: $(git -C "$SKILL_DIR" remote get-url origin 2>/dev/null)"
echo "  顶层条目: $(ls "$SKILL_DIR" | tr '\n' ' ')"
echo ""
echo "完成。确认无误后可删除备份目录："
[ -d "$BACKUP_DIR" ] && echo "  rm -rf \"$BACKUP_DIR\""
echo "（本脚本不执行任何 push，TFS 仓库不会被改动）"
SCRIPT_EOF
chmod +x /opt/deer-flow/scripts/pull-reg-auto-req-analysis.sh
```

#### 2. 执行拉取

```bash
cd /opt/deer-flow/skills/public
export TFS_PAT="你的PAT"
bash /opt/deer-flow/scripts/pull-reg-auto-req-analysis.sh
unset TFS_PAT
```

脚本自动判断两种模式：

| 场景 | 行为 |
|------|------|
| 目录已是 TFS git 仓库（日常增量更新） | 直接 `git pull`，不挪不删 |
| 目录不存在 / 不是仓库 / remote 不对（首次或重置） | 挪走旧目录为 `.old.时间戳` + 全量 clone，失败自动回滚 |

> **🔥 已增强**：脚本在 pull / clone 成功后**会自动热加载**——以管理员身份登录（`DEER_FLOW_ADMIN_EMAIL` / `DEER_FLOW_ADMIN_PASSWORD`，默认 `506391157@qq.com` / `Admin@123`）拿到 session + CSRF 后，调用 `POST http://127.0.0.1:2026/api/skills/reload` 让 Gateway 重新扫描最新 skill，无需手动重启容器。若登录失败或接口异常，脚本仅提示 skill 文件已就位、不自动重启 gateway，留待 Gateway 下次自然重载或人工处理。步骤 1 内嵌脚本已同步此逻辑；若按手册重建该文件，请确保包含此热加载步骤。
#### 3. 验证

```bash
# 拉取后确认版本和提交历史
git -C /opt/deer-flow/skills/public/reg-auto-req-analysis log --oneline -5

# 确认 remote 干净（不应含 PAT）
git -C /opt/deer-flow/skills/public/reg-auto-req-analysis remote get-url origin

# 顶层结构完整（应有 SKILL.md / _lib / config / references / runtime）
ls /opt/deer-flow/skills/public/reg-auto-req-analysis/
```

确认无误后删除备份目录：

```bash
rm -rf /opt/deer-flow/skills/public/reg-auto-req-analysis.old.*
```

### 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| pull 失败 | 服务器上有人手动改过 skill 文件（未提交修改） | `git -C /opt/deer-flow/skills/public/reg-auto-req-analysis status` 查看，处理后重试 |
| `Authentication failed` / 弹出账号密码提示 | 认证方式不对（用了 store 空用户名）或 PAT 失效 | 确认使用脚本内置的 Authorization 头注入方式；更换有效 PAT |
| `repository not found` | 仓库路径错误或未创建 | 核对 `REPO_URL`，确认仓库在 `WinCode/Skill` 项目下存在 |
| 拉下来缺文件（如无 `runtime/`） | TFS 仓库版本落后 | 对比 `.old.*` 备份，确认以哪边为准；仓库内容以 TFS 提交为准 |

> **注意**：`_lib/tfs/tfs-config.json`（含 PAT 的运行时配置）**不进仓库**（`.gitignore` 排除，仅提交 template），拉取后不会出现在目录中。skill 实际运行用的配置在 `/opt/deer-flow/auto-dev-work/skills/` 副本中，不受 public 目录更新影响。

---

> *更多运维场景待补充。*
