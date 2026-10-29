# 独立 Regional Wiki MCP 服务

服务与 DeerFlow 同机独立运行，复用 Gateway 镜像的 Python 依赖，通过只读挂载使用本仓库的 Wiki 工具，不读取 DeerFlow 配置或模型密钥。它提供 Streamable HTTP `/mcp/`，使用单独的 Bearer 令牌。调用结果为工具原有的 JSON 文本。调用方应遵循 `skills/public/regional-llm-wiki-qa/SKILL.md` 中的检索和证据规则。

## 192 服务器准备

检索排序扩展由 `backend/integrations/regional_wiki_search.py` 提供。部署本版时须一并提供
`backend/integrations/` 目录；Compose 将其只读挂到独立容器的 `/service/integrations`。
服务的搜索入口使用该扩展，另外三个工具仍使用现有 Reader。默认事实查询排除纯占位页，
显式 `scope="catalog"` 才包含目录缺资料项。仅更新编译器或导出不会改变旧 Reader 排序；
Gateway 配置绑定与独立 MCP 服务须分别核验，详见 `docs/regional-wiki-search-ranking.md`。

1. 按 `docs/regional-llm-wiki-schema1-compat-deploy.md` 完成 schema-1 快照导出、完整校验及发布。单独授权 MCP 服务的 UID/GID 只读遍历快照及未来新发布的文件。现有导出权限会限制 ACL mask，必须每次发布后复核。此项新增对独立服务的授权，保持 Gateway、sandbox 和 provisioner 原有挂载不变。
2. 保持与本地仓库一致的目录结构：将本目录的 `server.py`、`compose.yaml` 上传到服务器的 `/opt/deer-flow/services/regional_wiki_mcp/`，将 `backend/packages/harness/deerflow/tools/builtins/regional_llm_wiki_tool.py` 上传到 `/opt/deer-flow/backend/packages/harness/deerflow/tools/builtins/`。工具文件会只读挂载到独立容器，不改镜像和 Gateway。确认 `deer-flow-gateway:core2` 镜像包含 MCP 1.x SDK、LangChain 和 `deerflow.config.app_config.set_app_config`；Gateway 容器本身不重建或重启。若服务器目标路径已有文件，先核对内容和本地改动，再决定如何更新，勿直接覆盖。
3. 准备仅管理员可读的独立环境文件（例如 `/opt/deer-flow-wiki-mcp.env`），不要复用 DeerFlow 的内部认证令牌：

   ```dotenv
   REGIONAL_LLM_WIKI_COMPAT_ROOT=/实际路径/runtime/agent-access-compat-v1
   WIKI_MCP_UID=授权读取的数字UID
   WIKI_MCP_GID=授权读取的数字GID
   WIKI_MCP_TOKEN=至少32位随机ASCII令牌
   ```

   可用 `openssl rand -hex 32` 生成令牌，设置环境文件权限为 `0600`。不要将它提交到仓库，也不要在日志中打印。令牌赋予读取全部导出内容的权限。

4. 使用单独的 Compose 项目启动，不修改 DeerFlow 的 Compose 参数或 DooD 配置：

   ```bash
   cd /opt/deer-flow
   docker compose --env-file /opt/deer-flow-wiki-mcp.env -p regional-wiki-mcp -f services/regional_wiki_mcp/compose.yaml config --quiet
   docker compose --env-file /opt/deer-flow-wiki-mcp.env -p regional-wiki-mcp -f services/regional_wiki_mcp/compose.yaml up -d
   docker compose --env-file /opt/deer-flow-wiki-mcp.env -p regional-wiki-mcp -f services/regional_wiki_mcp/compose.yaml logs --tail 50
   ```

容器的 `/wiki` 为只读绑定挂载，文件系统也为只读。按 192 服务器部署需求，服务绑定 `172.16.0.192:18765`，客户端地址为 `http://172.16.0.192:18765/mcp/`，请求头为 `Authorization: Bearer <独立令牌>`。HTTP 不加密令牌或 Wiki 内容，应限制访问网络；推荐后续配置 TLS。SDK 只允许指定的服务地址和本机地址作为 Host，并拒绝未允许的浏览器 Origin。

## 验收

- 使用 MCP 客户端列出四个工具；无令牌和错误令牌必须返回 401。
- 依次调用 `get_llm_wiki_release_state`、`search_llm_wiki`、`read_llm_wiki_page` 和 `read_llm_wiki_evidence`；确认状态、版本、页面及证据均正确。基本状态为 `ok` 不替代完整离线校验。
- 对实际服务 UID/GID 测试当前和下次发布后的目录遍历、页面和证据读取权限；缺失或损坏快照应返回工具错误，不应提供旧资料。
- 查询 Docker 挂载，确认只有 schema-1 快照、服务脚本和 Wiki 工具脚本被挂载，均只读；不挂载 DeerFlow 配置、Docker socket、sandbox 或旧知识库。

停止独立服务使用相同 `--env-file`、项目名及 `-f` 参数执行 `down`；不会停止 DeerFlow。令牌变更后重新创建独立服务，并同步更新客户端。
