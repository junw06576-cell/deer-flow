# DeerFlow 接入 RAGFlow 知识库工具操作手册

本文档用于指导在不修改 DeerFlow 核心代码的前提下，复用 `ragflow_tools.py`，让 DeerFlow 通过 RAGFlow Assistant 查询知识库内容。

## 1. 目标

调用链路：

```text
DeerFlow 用户提问
  -> DeerFlow 调用 ragflow_chat 工具
  -> ragflow_chat 调用 RAGFlow Assistant
  -> RAGFlow Assistant 检索已绑定的知识库
  -> RAGFlow 返回答案
  -> DeerFlow 展示给用户
```

推荐方式：

```text
在 RAGFlow 中创建一个全量知识库 Assistant
将所有需要开放给 DeerFlow 的 Dataset 绑定到该 Assistant
DeerFlow 只配置该 Assistant 的 Chat ID
```

这样新增或移除知识库时，只需要在 RAGFlow 页面调整 Assistant 绑定的 Dataset，不需要频繁修改 DeerFlow 配置。

## 2. 前置条件

需要已完成：

- DeerFlow 已正常启动。
- RAGFlow 已正常启动。
- DeerFlow 容器可以访问 RAGFlow。
- RAGFlow 中已有 Dataset，且文档解析完成。
- 已拿到同事提供的 `ragflow_tools.py`。

示例环境：

```text
DeerFlow 地址：http://172.16.0.192:2026
RAGFlow 地址：http://172.16.0.192:9380
DeerFlow Gateway 容器：deer-flow-gateway
RAGFlow 容器：docker-ragflow-cpu-1
```

## 3. RAGFlow 创建全量知识库 Assistant

在 RAGFlow 页面操作：

1. 登录 RAGFlow。
2. 确认所有要开放给 DeerFlow 的 Dataset 已创建。
3. 确认 Dataset 中的文档已经解析完成。
4. 创建一个 Assistant，例如：

```text
DeerFlow-All-KB
```

5. 在该 Assistant 中绑定所有需要开放给 DeerFlow 的 Dataset。
6. 保存 Assistant。
7. 找到 Assistant 的 API、Integration 或 OpenAI-compatible 调用信息。
8. 复制该 Assistant 的 `chat_id`。
9. 在 RAGFlow 中生成或复制 API Key。

最终需要两个值：

```text
RAGFLOW_API_KEY
RAGFLOW_CHAT_ID
```

## 4. 测试 RAGFlow Assistant API

在 RAGFlow 所在服务器上执行：

```bash
export RAGFLOW_API_KEY='你的RAGFlow_API_Key'
export RAGFLOW_CHAT_ID='你的Chat_ID'
```

测试 OpenAI-compatible Chat API：

```bash
curl -i "http://127.0.0.1:9380/api/v1/openai/$RAGFLOW_CHAT_ID/chat/completions" \
  -H "Authorization: Bearer $RAGFLOW_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ragflow",
    "messages": [
      {"role": "user", "content": "请根据知识库回答：有哪些资料可以查询？"}
    ],
    "stream": false
  }'
```

如果返回 `200` 且响应中包含 `choices`，说明 RAGFlow Assistant API 可用。

如果返回 `401`，检查 API Key。

如果返回 `404`，检查 Chat ID 或 API 路径。

## 5. 测试 DeerFlow 容器访问 RAGFlow

从 DeerFlow Gateway 容器中访问 RAGFlow：

```bash
docker exec deer-flow-gateway sh -lc "curl -i http://172.16.0.192:9380"
```

如果返回 HTML、`200`、`302` 等响应，说明网络可达。

如果不通，可以尝试：

```bash
docker exec deer-flow-gateway sh -lc "curl -i http://host.docker.internal:9380 || true"
```

哪个地址通，后续 `.env` 中的 `RAGFLOW_BASE_URL` 就使用哪个。

推荐优先使用：

```env
RAGFLOW_BASE_URL=http://172.16.0.192:9380
```

## 6. 放置 `ragflow_tools.py`

将同事提供的 `ragflow_tools.py` 放到 DeerFlow 后端目录：

```text
/opt/deer-flow/backend/ragflow_tools.py
```

确认容器内能看到：

```bash
docker exec deer-flow-gateway sh -lc "ls -lah /app/backend/ragflow_tools.py"
```

验证 Python 可以 import：

```bash
docker exec deer-flow-gateway sh -lc "cd /app/backend && python -c 'import ragflow_tools; print(ragflow_tools.ragflow_chat.name)'"
```

如果 `python` 不存在，使用：

```bash
docker exec deer-flow-gateway sh -lc "cd /app/backend && uv run python -c 'import ragflow_tools; print(ragflow_tools.ragflow_chat.name)'"
```

正常输出类似：

```text
ragflow_chat
```

## 7. 配置 DeerFlow `.env`

编辑 DeerFlow `.env`：

```bash
cd /opt/deer-flow
vi .env
```

增加：

```env
RAGFLOW_API_KEY=你的RAGFlow_API_Key
RAGFLOW_BASE_URL=http://172.16.0.192:9380
RAGFLOW_CHAT_ID=你的Chat_ID
```

如果暂时只使用 `ragflow_chat`，可以不配置 `RAGFLOW_DATASET_IDS`。

原因：

```text
ragflow_chat 使用 RAGFLOW_CHAT_ID
知识库范围由 RAGFlow Assistant 绑定的 Dataset 决定
```

如果也要使用 `ragflow_search`，需要配置 Dataset ID：

```env
RAGFLOW_DATASET_IDS=id1,id2,id3
```

注意：同事版本的 `ragflow_tools.py` 中，`ragflow_search` 不会在 `RAGFLOW_DATASET_IDS` 为空时自动查询全部知识库。

## 8. 注册 DeerFlow 工具组

编辑 DeerFlow `config.yaml`：

```bash
cd /opt/deer-flow
vi config.yaml
```

找到：

```yaml
tool_groups:
- name: web
- name: file:read
- name: file:write
- name: bash
```

增加：

```yaml
- name: ragflow
```

结果示例：

```yaml
tool_groups:
- name: web
- name: file:read
- name: file:write
- name: bash
- name: ragflow
```

## 9. 注册 RAGFlow 工具

在 `config.yaml` 的 `tools:` 下追加：

```yaml
- name: ragflow_chat
  use: ragflow_tools:ragflow_chat
  group: ragflow

- name: ragflow_list_datasets
  use: ragflow_tools:ragflow_list_datasets
  group: ragflow

- name: ragflow_search
  use: ragflow_tools:ragflow_search
  group: ragflow
```

如果只想最小可用，可以先只注册：

```yaml
- name: ragflow_chat
  use: ragflow_tools:ragflow_chat
  group: ragflow
```

建议三者都注册，方便后续调试和查看知识库列表。

## 10. 重启 DeerFlow

重启 Gateway：

```bash
docker restart deer-flow-gateway
```

如果页面工具列表或功能状态有缓存，也重启前端：

```bash
docker restart deer-flow-frontend
```

## 11. 验证环境变量

执行：

```bash
docker exec deer-flow-gateway sh -lc 'echo RAGFLOW_BASE_URL=$RAGFLOW_BASE_URL; echo RAGFLOW_CHAT_ID=$RAGFLOW_CHAT_ID; test -n "$RAGFLOW_API_KEY" && echo RAGFLOW_API_KEY=已配置 || echo RAGFLOW_API_KEY=未配置'
```

预期：

```text
RAGFLOW_BASE_URL=http://172.16.0.192:9380
RAGFLOW_CHAT_ID=你的Chat_ID
RAGFLOW_API_KEY=已配置
```

## 12. 检查工具加载日志

查看 DeerFlow Gateway 日志：

```bash
docker logs --tail=300 deer-flow-gateway | grep -Ei 'ragflow|tool|import|error|traceback|exception'
```

如果没有报错，说明工具加载大概率正常。

常见错误：

```text
ModuleNotFoundError: No module named 'ragflow_tools'
```

说明 `ragflow_tools.py` 放置路径不对，或者 Gateway 容器无法 import。

```text
No module named 'httpx'
```

说明容器环境缺少 `httpx`。DeerFlow 通常已包含该依赖，如确实缺失，需要安装或改用已有 HTTP 客户端。

## 13. 在 DeerFlow 页面测试

新建 DeerFlow 对话，输入：

```text
请使用 RAGFlow 知识库回答：目前知识库里有哪些资料？
```

或更明确：

```text
调用 ragflow_chat 查询知识库：请总结当前知识库中的主要内容。
```

如果工具正常，DeerFlow 应调用 `ragflow_chat`，由 RAGFlow Assistant 检索其绑定的知识库并返回答案。

## 14. 从 DeerFlow 容器直接测试 RAGFlow Chat API

如果页面调用不稳定，可以先在 DeerFlow 容器内直接测试：

```bash
docker exec deer-flow-gateway sh -lc 'curl -i "$RAGFLOW_BASE_URL/api/v1/openai/$RAGFLOW_CHAT_ID/chat/completions" \
  -H "Authorization: Bearer $RAGFLOW_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"ragflow\",\"messages\":[{\"role\":\"user\",\"content\":\"测试知识库\"}],\"stream\":false}"'
```

如果这条能返回 `200` 和 `choices`，说明 RAGFlow API、网络和 Key 都正常。

## 15. 常见问题

### 15.1 返回 `RAGFLOW_CHAT_ID not configured`

原因：

- `.env` 没写 `RAGFLOW_CHAT_ID`。
- 修改 `.env` 后没有重启 `deer-flow-gateway`。
- Gateway 容器没有读取到新的环境变量。

处理：

```bash
docker restart deer-flow-gateway
docker exec deer-flow-gateway sh -lc 'echo $RAGFLOW_CHAT_ID'
```

### 15.2 返回 `Connection error: Cannot reach`

原因：

- `RAGFLOW_BASE_URL` 对 DeerFlow 容器不可达。
- RAGFlow 没启动。
- 端口未映射或防火墙阻断。

处理：

```bash
docker exec deer-flow-gateway sh -lc "curl -i http://172.16.0.192:9380"
```

### 15.3 返回 `401 Unauthorized`

原因：

- `RAGFLOW_API_KEY` 错误。
- API Key 复制不完整。
- 请求头格式不正确。

处理：

```bash
curl -i "http://127.0.0.1:9380/api/v1/datasets" \
  -H "Authorization: Bearer 你的RAGFlow_API_Key"
```

### 15.4 返回 `No response generated`

原因：

- RAGFlow Assistant 没绑定知识库。
- 知识库文档未解析完成。
- Assistant 的模型配置不可用。
- RAGFlow 后端回答为空。

处理：

- 在 RAGFlow 页面直接测试 Assistant。
- 确认 Dataset 已绑定。
- 确认文档解析状态完成。
- 确认 RAGFlow 模型配置可用。

### 15.5 `ragflow_search` 提示没有 Dataset ID

原因：

```text
ragflow_search 依赖 RAGFLOW_DATASET_IDS 或调用时传入 dataset_ids。
```

处理：

```env
RAGFLOW_DATASET_IDS=id1,id2,id3
```

如果目标是查询所有知识库，更推荐使用 `ragflow_chat`，并在 RAGFlow Assistant 中绑定所有知识库。

## 16. 推荐最终配置形态

DeerFlow `.env`：

```env
RAGFLOW_API_KEY=你的RAGFlow_API_Key
RAGFLOW_BASE_URL=http://172.16.0.192:9380
RAGFLOW_CHAT_ID=全量知识库Assistant的Chat_ID
```

DeerFlow `config.yaml`：

```yaml
tool_groups:
- name: web
- name: file:read
- name: file:write
- name: bash
- name: ragflow

tools:
- name: ragflow_chat
  use: ragflow_tools:ragflow_chat
  group: ragflow

- name: ragflow_list_datasets
  use: ragflow_tools:ragflow_list_datasets
  group: ragflow

- name: ragflow_search
  use: ragflow_tools:ragflow_search
  group: ragflow
```

## 17. 关键提醒

- RAGFlow API Key 只放 DeerFlow 后端 `.env`，不要放到前端页面。
- 如果要查所有知识库，优先用 RAGFlow Assistant 绑定所有 Dataset，然后 DeerFlow 调 `ragflow_chat`。
- `ragflow_search` 需要显式 Dataset ID，不会自动查全部知识库。
- 修改 `.env` 或 `config.yaml` 后，需要重启 `deer-flow-gateway`。
- 如果工具未被调用，可以在提问中明确写“调用 ragflow_chat 查询知识库”进行测试。

