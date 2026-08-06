# DeerFlow 运维手册

适用场景：DeerFlow 使用 Docker Compose 部署，常见部署目录为 `/opt/deer-flow`，前端入口端口通常为 `2026`，Gateway 应用端口通常为 `8001`。

## 0. 进入部署目录

```bash
cd /opt/deer-flow
```

如实际部署目录不同，请替换为真实路径。

## 1. 查看容器状态

查看正在运行的 DeerFlow 容器：

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep deer-flow
```

查看包含已退出容器在内的状态：

```bash
docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Image}}\t{{.Command}}" | grep deer-flow
```

如果使用项目名 `deer-flow-dev` 和开发 compose 文件：

```bash
docker compose -p deer-flow-dev -f docker/docker-compose-dev.yaml ps
```

## 2. 查看日志

Gateway 后端日志：

```bash
docker logs --tail=300 deer-flow-gateway
```

持续跟踪 Gateway 日志：

```bash
docker logs -f --tail=100 deer-flow-gateway
```

前端日志：

```bash
docker logs --tail=200 deer-flow-frontend
```

Nginx 日志：

```bash
docker logs --tail=200 deer-flow-nginx
```

如果 nginx 容器名不确定：

```bash
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}" | grep -Ei 'nginx|deer-flow'
```

## 3. 快速过滤关键错误

Gateway 常见错误：

```bash
docker logs --tail=500 deer-flow-gateway 2>&1 | grep -Ei 'error|traceback|exception|failed|uvicorn|fastapi|config|yaml|openai|api_key|base_url|connection|timeout|unauthorized|numpy|module'
```

前端常见错误：

```bash
docker logs --tail=300 deer-flow-frontend 2>&1 | grep -Ei 'error|failed|blocked|cross-origin|webpack-hmr|env\.|auth'
```

Nginx 常见错误：

```bash
docker logs --tail=300 deer-flow-nginx 2>&1 | grep -Ei 'error|connect|refused|upstream|bad gateway|timeout'
```

## 4. 验证 Gateway 是否监听 8001

在 Gateway 容器内访问应用端口：

```bash
docker exec deer-flow-gateway sh -lc "curl -i http://127.0.0.1:8001/ || true"
```

判断：

- 返回 `401`、`404` 或 JSON：说明 Gateway 应用已进入应用层。
- 返回 `Connection refused`：说明容器里没有服务监听 `8001`。
- 命令卡住或超时：可能是进程挂起、端口阻塞或应用启动异常。

如果容器内没有 `ss`、`netstat`、`ps` 等工具，可以用 `/proc` 检查端口。`8001` 的十六进制端口是 `1F41`：

```bash
docker exec deer-flow-gateway sh -lc "cat /proc/net/tcp /proc/net/tcp6 | grep -i ':1F41' || true"
```

有输出通常代表存在 `8001` 监听或连接；无输出则说明没有监听。

## 5. Gateway 容器运行但无日志、8001 不通

这是 Gateway 排查的重点场景。

先看容器状态、镜像和启动命令：

```bash
docker ps -a --filter name=deer-flow-gateway --format "table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Command}}"
```

看 Docker 配置里的入口命令：

```bash
docker inspect deer-flow-gateway --format '{{json .Config.Entrypoint}} {{json .Config.Cmd}}'
```

看 1 号进程实际命令：

```bash
docker exec deer-flow-gateway sh -lc "cat /proc/1/cmdline | tr '\0' ' '; echo"
```

看 1 号进程状态：

```bash
docker exec deer-flow-gateway sh -lc "cat /proc/1/status | sed -n '1,60p'"
```

查看入口脚本内容：

```bash
docker exec deer-flow-gateway sh -lc "ls -l /usr/local/bin && sed -n '1,240p' /usr/local/bin/docker-entrypoint* 2>/dev/null || true"
```

查看挂载和目录是否正常：

```bash
docker inspect deer-flow-gateway --format '{{json .Mounts}}'
docker exec deer-flow-gateway sh -lc "pwd; ls -la /app; ls -la /app/backend 2>/dev/null || true; ls -la /app/config.yaml /app/.env 2>/dev/null || true"
```

常见原因：

- 容器入口脚本卡在安装依赖、等待文件、等待数据库或迁移步骤。
- Gateway 应用启动命令没有执行到。
- `config.yaml`、`.env` 或后端目录挂载异常。
- 容器命令被 compose 覆盖，启动的不是 Gateway 服务。

## 6. 验证 Nginx 到 Gateway 链路

从宿主机访问统一入口：

```bash
curl -i http://127.0.0.1:2026/api/v1/auth/setup-status
```

正常情况通常返回：

```text
HTTP/1.1 200 OK
```

响应体类似：

```json
{"needs_setup":false}
```

如果返回 `502 Bad Gateway`，优先检查：

```bash
docker logs --tail=300 deer-flow-nginx
docker logs --tail=300 deer-flow-gateway
docker exec deer-flow-gateway sh -lc "curl -i http://127.0.0.1:8001/ || true"
```

如果 `8001` 不通，先修 Gateway；如果 `8001` 正常，再查 nginx upstream 配置。

## 7. 验证登录接口

将账号密码替换为实际管理员账号：

```bash
curl -i -X POST http://127.0.0.1:2026/api/v1/auth/login/local \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "username=admin@example.com" \
  --data-urlencode "password=Admin@123"
```

正常情况返回 `HTTP/1.1 200 OK`，响应中通常包含：

```json
{"expires_in":604800,"needs_setup":false}
```

## 8. 检查模型配置

查看 `.env` 中模型相关变量：

```bash
grep -nE 'OPENAI|ANTHROPIC|DEEPSEEK|MODEL|BASE_URL|API_BASE|API_KEY' .env
```

查看 `config.yaml` 中模型配置：

```bash
sed -n '20,45p' config.yaml
```

OpenAI 兼容模型配置通常类似：

```yaml
models:
- name: deepseek-v4-flash
  display_name: Wincode / deepseek-v4-flash
  use: langchain_openai:ChatOpenAI
  model: deepseek-v4-flash
  api_key: $OPENAI_API_KEY
  base_url: $OPENAI_BASE_URL
```

`.env` 通常包含：

```env
OPENAI_API_KEY=your-token
OPENAI_BASE_URL=https://example.com/v1
OPENAI_API_BASE=https://example.com/v1
```

注意：

- 修改 `.env` 后需要重启 Gateway。
- 如果页面直接提示网关不可用，优先确认 Gateway 是否监听 `8001`，再排查模型地址。
- 如果 Gateway 启动阶段会初始化模型，错误的模型配置也可能导致 Gateway 启动失败，以 Gateway 日志为准。

## 9. 检查认证和访问地址配置

查看认证相关变量：

```bash
grep -nE 'BETTER_AUTH|TRUSTED_ORIGINS|CORS|GATEWAY' .env
```

常见配置：

```env
BETTER_AUTH_URL=http://172.16.0.192:2026
DEER_FLOW_INTERNAL_GATEWAY_BASE_URL=http://localhost:8001
DEER_FLOW_TRUSTED_ORIGINS=http://localhost:3000,http://localhost:2026,http://172.16.0.192:2026
```

注意：

- `BETTER_AUTH_URL` 应与浏览器实际访问入口一致。
- 如果通过 IP 访问，例如 `http://172.16.0.192:2026`，不要只配置 `localhost`。
- 如果前后端分离部署，需要正确设置 `GATEWAY_CORS_ORIGINS` 或 `DEER_FLOW_TRUSTED_ORIGINS`。

## 10. 检查 Next.js 开发源拦截

如果前端日志出现 `Blocked cross-origin request`、`allowedDevOrigins` 或 `webpack-hmr` 相关错误：

```bash
docker logs --tail=300 deer-flow-frontend 2>&1 | grep -Ei 'allowedDevOrigins|Blocked cross-origin|webpack-hmr'
```

需要检查前端 `next.config.js` 的 `allowedDevOrigins` 是否包含当前访问 IP 或域名，然后重启前端：

```bash
docker restart deer-flow-frontend
```

## 11. 重启服务

只重启 Gateway：

```bash
docker restart deer-flow-gateway
```

只重启前端：

```bash
docker restart deer-flow-frontend
```

只重启 Nginx：

```bash
docker restart deer-flow-nginx
```

整套停止和启动：

```bash
make docker-stop
make docker-start
```

使用 compose 重建 Gateway：

```bash
docker compose -p deer-flow-dev -f docker/docker-compose-dev.yaml up -d --force-recreate gateway
```

如果服务名不是 `gateway`，先用下面命令确认服务名：

```bash
docker compose -p deer-flow-dev -f docker/docker-compose-dev.yaml ps
```

## 12. 检查 compose 配置

查看 compose 文件最终展开结果：

```bash
docker compose -p deer-flow-dev -f docker/docker-compose-dev.yaml config
```

只查看 Gateway 段：

```bash
docker compose -p deer-flow-dev -f docker/docker-compose-dev.yaml config | sed -n '/gateway:/,/^[^ ]/p'
```

查找 compose 文件：

```bash
find /opt/deer-flow -maxdepth 3 -iname '*compose*.yml' -o -iname '*compose*.yaml'
```

## 13. 检查 NumPy X86_V2 问题

如果 Gateway 日志出现 NumPy 或 CPU 指令集相关错误，执行：

```bash
docker exec deer-flow-gateway sh -lc "cd /app/backend && uv run python -c 'import numpy; print(numpy.__version__); print(numpy.__file__)'"
```

如果出现类似错误：

```text
RuntimeError: NumPy was built with baseline optimizations: (X86_V2)
```

说明当前 NumPy wheel 与服务器 CPU 指令集不兼容，需要更换兼容版本或从源码构建。

## 14. 浏览器侧排查

访问入口：

```text
http://172.16.0.192:2026/
```

登录页：

```text
http://172.16.0.192:2026/login
```

浏览器开发者工具检查：

```text
F12 -> Console
F12 -> Network -> Fetch/XHR
```

重点看：

- `/api/v1/auth/setup-status`
- `/api/v1/auth/login/local`
- `/api/*`

如果接口返回 `502`，查 nginx 和 Gateway；如果返回 `401` 或 `403`，查认证配置；如果请求地址不对，查前端环境变量和反代配置。

## 15. 推荐排查顺序

页面显示“网关不可用”时，按下面顺序排查：

1. `docker ps -a` 确认容器是否运行。
2. `docker logs deer-flow-gateway` 看 Gateway 是否报错。
3. `curl http://127.0.0.1:8001/` 确认 Gateway 容器内应用是否监听。
4. `curl http://127.0.0.1:2026/api/v1/auth/setup-status` 确认 nginx 到 Gateway 链路。
5. 检查 `.env`、`config.yaml`、`BETTER_AUTH_URL`、模型配置。
6. 重启 Gateway；必要时重启前端和 nginx。
7. 如果 Gateway 无日志且 8001 不通，重点检查入口脚本、1 号进程、挂载目录和 compose command。
