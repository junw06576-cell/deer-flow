# RAGFlow + DeerFlow 集成部署手册

本文档用于指导在已有 DeerFlow 环境旁路部署 RAGFlow，并通过 API、Skill 或 Tool 与 DeerFlow 配合使用。

## 1. 目标架构

```text
用户浏览器
  |
  |-- http://172.16.0.192:2026  -> DeerFlow
  |
  |-- http://172.16.0.192:9380  -> RAGFlow

DeerFlow Gateway
  |
  |-- 调用 RAGFlow API
      http://172.16.0.192:9380/api/v1/...
```

建议初期采用：

- DeerFlow 和 RAGFlow 分开 Docker Compose 部署。
- DeerFlow 使用 `2026` 端口。
- RAGFlow 使用 `9380` 端口。
- 跑通后再考虑统一 Nginx、统一域名或接入同一个 Docker network。

## 2. 环境要求

服务器最低建议：

```text
CPU：>= 4 核
内存：>= 16 GB，建议 32 GB
磁盘：>= 50 GB，建议 100 GB+
系统：Linux x86_64
Docker：>= 24.0.0
Docker Compose：>= v2.26.1
```

检查命令：

```bash
uname -m
docker --version
docker compose version
free -h
df -h
```

RAGFlow 默认依赖：

```text
MySQL
Elasticsearch 或 Infinity
MinIO
Redis
RAGFlow Server
```

注意：

- RAGFlow 官方预构建镜像主要面向 x86 架构，ARM64 服务器需要额外确认兼容性。
- RAGFlow 资源占用明显高于 DeerFlow，内存不足时容易出现 Elasticsearch、MySQL 或解析任务异常。

## 3. 部署 RAGFlow

进入部署目录：

```bash
cd /opt
git clone https://github.com/infiniflow/ragflow.git
cd /opt/ragflow/docker
```

设置 Elasticsearch 需要的内核参数：

```bash
sysctl vm.max_map_count
sudo sysctl -w vm.max_map_count=262144
```

永久生效：

```bash
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf
```

确认 Docker 配置文件：

```bash
ls -lah
```

如果存在 `.env.example`，复制为 `.env`：

```bash
cp .env.example .env
```

如果当前版本已经自带 `.env`，则直接编辑 `.env`。

## 4. 修改 RAGFlow 访问端口

RAGFlow 默认可能占用宿主机 `80`。建议改为 `9380`，避免与已有服务冲突。

编辑 compose 文件：

```bash
vi docker-compose.yml
```

找到类似配置：

```yaml
ports:
  - "80:80"
```

改为：

```yaml
ports:
  - "9380:80"
```

如果 compose 中区分 `ragflow-cpu`、`ragflow-gpu`，需要确认实际启用的是哪个服务，并修改对应服务的端口。

启动：

```bash
cd /opt/ragflow/docker
docker compose -f docker-compose.yml up -d
```

查看状态：

```bash
docker compose -f docker-compose.yml ps
```

查看相关容器：

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -Ei 'ragflow|mysql|redis|minio|elastic|infinity'
```

查看 RAGFlow 日志：

```bash
docker logs -f ragflow-server
```

如果容器名不是 `ragflow-server`：

```bash
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}" | grep ragflow
```

浏览器访问：

```text
http://172.16.0.192:9380
```

## 5. RAGFlow 初始化

打开 RAGFlow 页面后，按顺序完成：

1. 注册管理员账号。
2. 进入模型配置。
3. 配置聊天模型 LLM。
4. 配置 Embedding 模型。
5. 创建知识库 Dataset。
6. 上传测试文档。
7. 等待文档解析完成。
8. 创建 Chat Assistant。
9. 生成 API Key。

如果 DeerFlow 已经使用 Wincode OpenAI 兼容网关，RAGFlow 的 LLM 可以尝试同样配置：

```text
Provider：OpenAI-compatible
Base URL：https://wincode.winning.com.cn/ai/v1
API Key：Wincode token
Chat Model：deepseek-v4-flash
```

Embedding 模型需要单独确认。RAGFlow 的知识库检索依赖 Embedding，如果没有可用 Embedding，文档解析、向量化和检索问答可能无法正常工作。

## 6. 验证 RAGFlow API

在 RAGFlow 页面生成 API Key 后，在服务器上测试：

```bash
export RAGFLOW_BASE_URL=http://127.0.0.1:9380
export RAGFLOW_API_KEY='你的RAGFlow_API_Key'
```

测试 datasets 接口：

```bash
curl -i "$RAGFLOW_BASE_URL/api/v1/datasets" \
  -H "Authorization: Bearer $RAGFLOW_API_KEY"
```

如果返回 `401`，说明 API Key 不正确、未启用，或请求头格式不对。

创建 Chat Assistant 后，记录 `chat_id`，测试 OpenAI-compatible 接口：

```bash
curl -i "$RAGFLOW_BASE_URL/api/v1/openai/<chat_id>/chat/completions" \
  -H "Authorization: Bearer $RAGFLOW_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ragflow",
    "messages": [
      {"role": "user", "content": "请根据知识库回答：测试文档讲了什么？"}
    ],
    "stream": false
  }'
```

能返回知识库问答结果后，再接入 DeerFlow。

## 7. DeerFlow 到 RAGFlow 网络连通

从 DeerFlow Gateway 容器内测试访问 RAGFlow：

```bash
docker exec deer-flow-gateway sh -lc "curl -i http://172.16.0.192:9380"
```

如果能返回 HTML 或 HTTP 响应，说明网络连通。

不要在 DeerFlow 容器里使用：

```text
http://127.0.0.1:9380
```

因为该地址指向 DeerFlow Gateway 容器自身，而不是宿主机。

可选：将 DeerFlow 和 RAGFlow 加入同一个 Docker network。

```bash
docker network create ai-net
docker network connect ai-net deer-flow-gateway
docker network connect ai-net ragflow-server
```

然后 DeerFlow 可以通过容器名访问：

```bash
docker exec deer-flow-gateway sh -lc "curl -i http://ragflow-server"
```

## 8. DeerFlow 配置 RAGFlow 环境变量

建议在 DeerFlow 的 `.env` 中增加：

```env
RAGFLOW_BASE_URL=http://172.16.0.192:9380
RAGFLOW_API_KEY=你的RAGFlow_API_Key
RAGFLOW_CHAT_ID=你的chat_id
```

如果两个容器已接入同一个 Docker network，也可以使用：

```env
RAGFLOW_BASE_URL=http://ragflow-server
```

修改 `.env` 后重启 DeerFlow Gateway：

```bash
cd /opt/deer-flow
docker restart deer-flow-gateway
```

确认变量进入容器：

```bash
docker exec deer-flow-gateway sh -lc 'echo RAGFLOW_BASE_URL=$RAGFLOW_BASE_URL; echo RAGFLOW_CHAT_ID=$RAGFLOW_CHAT_ID; test -n "$RAGFLOW_API_KEY" && echo RAGFLOW_API_KEY=已配置 || echo RAGFLOW_API_KEY=未配置'
```

## 9. 通过 Skill 引导 DeerFlow 使用 RAGFlow

如果 DeerFlow 已启用 skill 扫描：

```yaml
skills:
  container_path: /mnt/skills

skill_scan:
  enabled: true
```

创建自定义 skill：

```bash
mkdir -p /opt/deer-flow/skills/ragflow-query
vi /opt/deer-flow/skills/ragflow-query/SKILL.md
```

写入：

```markdown
# ragflow-query

## Description
当用户需要查询企业知识库、制度文档、项目资料、运维手册、产品资料时，使用 RAGFlow 知识库进行检索问答。

## Instructions
- 与内部文档、知识库、制度、项目资料相关的问题，优先查询 RAGFlow。
- RAGFlow 地址：由环境变量 RAGFLOW_BASE_URL 提供。
- RAGFlow Chat ID：由环境变量 RAGFLOW_CHAT_ID 提供。
- 使用 RAGFlow OpenAI-compatible Chat API。
- API 路径：/api/v1/openai/<chat_id>/chat/completions。
- 请求头：Authorization: Bearer <RAGFLOW_API_KEY>。
- 如果 RAGFlow 未返回可靠答案，明确说明知识库未覆盖，不要编造。
```

确认 DeerFlow 容器内能看到 skill：

```bash
docker exec deer-flow-gateway sh -lc "find /mnt/skills -maxdepth 3 -type f -name 'SKILL.md' -print 2>/dev/null || true"
```

如果 `/opt/deer-flow/skills` 没有挂载到 `/mnt/skills`，需要在 DeerFlow compose 的 gateway 服务中增加挂载：

```yaml
volumes:
  - /opt/deer-flow/skills:/mnt/skills
```

重建 Gateway：

```bash
cd /opt/deer-flow
docker compose -p deer-flow-dev -f docker/docker-compose-dev.yaml up -d --force-recreate gateway
```

重启 DeerFlow：

```bash
docker restart deer-flow-gateway deer-flow-frontend
```

注意：Markdown skill 更多是行为指导，不一定等同于可执行 HTTP 工具。生产集成建议继续封装正式 Tool。

## 10. 生产级 Tool 集成建议

建议在 DeerFlow 中增加一个工具：

```text
ragflow_query(question: string) -> answer
```

工具内部调用：

```text
POST {RAGFLOW_BASE_URL}/api/v1/openai/{RAGFLOW_CHAT_ID}/chat/completions
Authorization: Bearer {RAGFLOW_API_KEY}
Content-Type: application/json
```

请求体示例：

```json
{
  "model": "ragflow",
  "messages": [
    {
      "role": "user",
      "content": "用户问题"
    }
  ],
  "stream": false
}
```

工具返回：

```text
RAGFlow 生成的回答，以及可用时的引用来源。
```

推荐调用逻辑：

```text
用户问题
  -> DeerFlow 判断是否需要查内部知识库
  -> 调用 ragflow_query 工具
  -> RAGFlow 检索知识库并生成答案
  -> DeerFlow 整合输出
```

## 11. RAGFlow 日常运维

启动：

```bash
cd /opt/ragflow/docker
docker compose -f docker-compose.yml up -d
```

停止：

```bash
cd /opt/ragflow/docker
docker compose -f docker-compose.yml stop
```

查看状态：

```bash
docker compose -f docker-compose.yml ps
```

查看日志：

```bash
docker compose -f docker-compose.yml logs -f --tail=100
```

重启：

```bash
docker compose -f docker-compose.yml restart
```

配置变更后启动：

```bash
docker compose -f docker-compose.yml up -d
```

注意不要轻易执行：

```bash
docker compose -f docker-compose.yml down -v
```

`-v` 会删除 volume，可能清空 MySQL、MinIO、Elasticsearch 等数据。

## 12. 数据备份建议

RAGFlow 至少需要关注：

```text
MySQL 数据
MinIO 文件
Elasticsearch 或 Infinity 索引
RAGFlow 配置文件
```

建议备份：

```bash
cd /opt/ragflow/docker
docker compose -f docker-compose.yml ps
```

确认 volume 名称：

```bash
docker volume ls | grep -Ei 'ragflow|mysql|minio|elastic|infinity|redis'
```

生产环境建议做定期快照或使用独立存储。

## 13. 常见问题

### 13.1 RAGFlow 页面打不开

检查端口：

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep ragflow
curl -i http://127.0.0.1:9380
```

查看日志：

```bash
docker compose -f /opt/ragflow/docker/docker-compose.yml logs --tail=200
```

### 13.2 Elasticsearch 启动失败

检查：

```bash
sysctl vm.max_map_count
```

如果小于 `262144`：

```bash
sudo sysctl -w vm.max_map_count=262144
```

### 13.3 文档上传后无法解析或无法检索

重点检查：

- Embedding 模型是否配置。
- Embedding API Key 是否有效。
- 文档解析任务是否完成。
- RAGFlow server 日志是否有模型调用错误。

### 13.4 DeerFlow 容器访问不了 RAGFlow

不要使用容器内的 `127.0.0.1`。

测试宿主机 IP：

```bash
docker exec deer-flow-gateway sh -lc "curl -i http://172.16.0.192:9380"
```

或使用共享 Docker network：

```bash
docker network connect ai-net deer-flow-gateway
docker network connect ai-net ragflow-server
docker exec deer-flow-gateway sh -lc "curl -i http://ragflow-server"
```

### 13.5 RAGFlow API 返回 401

检查：

- API Key 是否来自 RAGFlow 页面。
- 请求头是否为 `Authorization: Bearer <key>`。
- 是否调用了正确的 RAGFlow 地址。
- API Key 是否被重置或删除。

## 14. 推荐落地顺序

1. 检查服务器资源和 Docker 版本。
2. 设置 `vm.max_map_count=262144`。
3. clone RAGFlow。
4. 修改 RAGFlow 端口为 `9380:80`。
5. 启动 RAGFlow。
6. 打开 `http://172.16.0.192:9380`。
7. 配置 LLM 和 Embedding。
8. 创建知识库并上传测试文档。
9. 创建 Chat Assistant 和 API Key。
10. 用 curl 测通 RAGFlow API。
11. 从 DeerFlow 容器 curl RAGFlow 地址。
12. 配置 DeerFlow skill 或 tool 调用 RAGFlow。
13. 最后再做用户权限、知识库分组、备份和监控。

## 15. 关键提醒

- RAGFlow 能否真正检索，Embedding 模型是关键。
- LLM 负责回答，Embedding 负责文档入库、向量化和召回。
- 初期不要把 DeerFlow 和 RAGFlow 强行合并到一个 compose 文件，先分开跑通更容易排查。
- DeerFlow 容器访问 RAGFlow 时，优先使用宿主机 IP 或共享 Docker network，不要使用 `127.0.0.1`。
- `docker compose down -v` 会删除 volume，生产环境慎用。

