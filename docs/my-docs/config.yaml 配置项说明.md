# config.yaml 配置项说明

> 对应服务器 `/opt/deer-flow/config.yaml`，逐项解释

---

## 基础

```yaml
config_version: 28
```
配置格式版本号，`make setup` 自动生成，一般不动。

```yaml
log_level: info
```
Gateway 日志级别。`debug` 最详细（性能影响），`info` 正常，`warning` 只报警告。

---

## logging — 日志增强

```yaml
logging:
  enhance:
    enabled: false     # 关
    format: text       # text 或 json
```
开启后记录更详细的结构化日志。排查时才开，平时关。

---

## token_usage / token_budget — Token 用量控制

```yaml
token_usage:
  enabled: false       # 关 — 不统计 token 使用量

token_budget:
  enabled: false       # 关 — 不限制 token 预算
  max_tokens: 200000
  warn_threshold: 0.8
  hard_stop_threshold: 1.0
```
- `enabled: true` → 超过预算会报警或截停
- 你关着合适，auto 任务不要被预算打断

---

## max_recursion_limit — 递归上限

```yaml
max_recursion_limit: 1000
```
**不是实际值**，是**服务器端硬上限**。调用方传的 `recursion_limit` 超过这个值会被 clamp 到 1000。
实际值由客户端（deerflow-service）决定，当前你在 `deerflow_client.py` 里配了 500，但 Gateway 另有 300 的限制。

---

## models — 模型配置

```yaml
models:
- name: deepseek-v4-flash
  display_name: Wincode / deepseek-v4-flash
  use: langchain_openai:ChatOpenAI      # LangChain 集成方式
  model: deepseek-v4-flash               # 模型名（传给 API）
  api_key: $OPENAI_API_KEY              # 从环境变量读取
  base_url: $OPENAI_BASE_URL            # API 地址
  timeout: 600.0                         # 单次 LLM 调用超时（10 分钟）
  max_retries: 2                         # 失败重试次数
  max_tokens: 8192                       # 单次回复最大 token
  supports_vision: false                 # 不支持图片识别
  supports_thinking: true                # 支持思维链（deepseek 特性）
```

---

## tool_groups — 工具组

```yaml
tool_groups:
- name: web             # 网页搜索/抓取
- name: file:read       # 读文件
- name: file:write      # 写文件
- name: bash            # 执行 shell 命令
- name: ragflow         # 知识库检索
```
agent 按组加载工具。`tool_search.enabled: false` 时，agent 只能看到这些组里声明的工具。

---

## tools — 具体工具

| 工具 | 作用 | 分组 |
|------|------|------|
| `web_search` | DuckDuckGo 网页搜索，最多返回 5 条 | web |
| `web_fetch` | Jina AI 抓取网页内容，超时 10 秒 | web |
| `image_search` | 图片搜索 | web |
| `ls` | 列出目录 | file:read |
| `read_file` | 读文件 | file:read |
| `glob` | 文件名搜索，最多 200 条 | file:read |
| `grep` | 文件内容搜索，最多 100 条 | file:read |
| `write_file` | 写文件 | file:write |
| `str_replace` | 替换文件中的文本 | file:write |
| `bash` | 执行 shell 命令，超时 600 秒 | bash |
| `ragflow_chat` | RAGFlow 知识库对话 | ragflow |
| `ragflow_list_datasets` | 列出知识库数据集 | ragflow |
| `ragflow_search` | 搜索知识库 | ragflow |

---

## tool_search — 工具自动发现

```yaml
tool_search:
  enabled: false         # 关
  auto_promote_top_k: 3
```
开：agent 能在 MCP/社区市场里按需发现工具。
关：固定只用上面声明的工具。auto 任务建议关，防止 agent 分心。

---

## tool_output — 大输出外置

```yaml
tool_output:
  enabled: false              # 关 — 所有工具输出直接塞 prompt
  externalize_min_chars: 12000  # 超过这个长度就外置存文件
  preview_head_chars: 2000    # 外置时保留前 2000 字符预览
  preview_tail_chars: 1000    # 外置时保留后 1000 字符预览
  fallback_max_chars: 30000   # 降级模式最大字符
  storage_subdir: .tool-results
  exempt_tools:               # 以下工具不应用外置
  - read_file
  - read_file_tool
```
外置=大输出不塞 prompt 而是存文件。开的话可以节省大量 token，但 agent 需要主动去读文件。

---

## suggestions — 对话建议

```yaml
suggestions:
  enabled: false       # 关
```
开：在对话结尾给用户推荐下一步操作（如"试试搜索 X"）。auto 任务不需要。

---

## input_polish — 输入润色

```yaml
input_polish:
  enabled: false       # 关
  max_chars: 4000
  model_name: null
```
开：用户输入先让 LLM 润色一遍再给 agent。auto 任务不需要。

---

## loop_detection — 循环检测

```yaml
loop_detection:
  enabled: false        # 关 — 目前不检测循环
  warn_threshold: 3     # 同一行为重复 3 次报警
  hard_limit: 5         # 重复 5 次截停
  window_size: 20       # 在最近 20 步内检测
  max_tracked_threads: 100
  tool_freq_warn: 30    # 同一工具调用 30 次报警
  tool_freq_hard_limit: 50  # 50 次截停
```
开：agent 反复做同一件事（如反复 validate→修）时截停，比等 recursion_limit 炸更精准。

---

## read_before_write — 写前读

```yaml
read_before_write:
  enabled: false       # 关
```
开：agent 写文件前必须读过该文件。防止覆盖未知内容。auto 任务按需开启。

---

## safety_finish_reason — 安全终止

```yaml
safety_finish_reason:
  enabled: false       # 关
```
开：LLM 返回安全拒绝时截停。

---

## uploads — 文件上传

```yaml
uploads:
  max_files: 10              # 单次最多上传 10 个文件
  max_file_size: 52428800    # 单文件最大 50MB
  max_total_size: 104857600   # 总大小最大 100MB
  auto_convert_documents: false  # 不自动转换文档
  pdf_converter: auto
```

---

## sandbox — 沙箱配置

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider  # Docker 沙箱
  bash_output_max_chars: 20000      # bash 输出截断长度
  read_file_output_max_chars: 50000  # 读文件截断长度
  ls_output_max_chars: 20000        # ls 输出截断长度
  bash_command_timeout: 600         # 单条命令超时 10 分钟
```
你服务器上手动加了 `environment:` 段用来传 `REDIS_HOST` 和 `REDIS_PORT`（当前本地文件没有，要同步）。

---

## skills — 技能路径

```yaml
skills:
  container_path: /mnt/skills  # sandbox 里的技能挂载路径
```

---

## skill_scan — 技能自动发现

```yaml
skill_scan:
  enabled: false       # 关
```
开：自动扫描 `skills/` 目录发现新技能。关的话需要显式在 agent 配置里声明。

---

## title — 对话标题

```yaml
title:
  enabled: false
  max_words: 6
  max_chars: 60
  model_name: null
```
开：自动给对话生成标题。auto 任务不需要。

---

## summarization — 上下文总结

```yaml
summarization:
  enabled: false           # 关
  trigger:
  - type: tokens
    value: 32000           # token 超 32K 时触发总结
  keep:
    type: messages
    value: 10              # 保留最近 10 条消息
  trim_tokens_to_summarize: 15564
  summary_prompt: null
  skill_file_read_tool_names:
  - read_file
  - read
  - view
  - cat
```
开：当上下文太长时，总结旧内容腾出空间。长会话省 token，但会丢细节。
关：上下文一直积累，不丢信息，但 token 消耗大。

---

## memory — 记忆系统

```yaml
memory:
  enabled: true               # 开 — 刚改的
  injection_enabled: true     # 记忆注入到 agent prompt
  shutdown_flush_timeout_seconds: 30.0
  manager_class: deermem
  mode: middleware             # 后台自动提取（不需要 agent 主动调）
  backend_config:
    storage_path: ''           # 存到 .deer-flow 默认位置
    debounce_seconds: 30       # 每 30 秒批量处理一次记忆
    model:                     # 事实提取用的模型（deepseek-v4-flash）
      provider: openai
      model: deepseek-v4-flash
      api_key: $OPENAI_API_KEY
      base_url: $OPENAI_BASE_URL
    max_facts: 100             # 最多存 100 条事实
    fact_confidence_threshold: 0.7  # 置信度低于 0.7 不存
    max_injection_tokens: 2000      # 注入 prompt 的记忆最多 2000 token
    token_counting: tiktoken
    guaranteed_categories:     # 以下类别**保证**注入
    - correction               # 纠正类事实（agent 犯错改对的经验）
    guaranteed_token_budget: 500     # 纠正类预留 500 token
    staleness_review_enabled: true   # 90 天后审查事实是否过时
    staleness_age_days: 90
    staleness_min_candidates: 3
    staleness_max_removals_per_cycle: 10
    staleness_protected_categories:
    - correction                     # 纠正类不被清理
    staleness_max_lifetime_multiplier: 20.0
    staleness_max_extension_days: 3650
    consolidation_enabled: false     # 不合并同类事实
```
**关键**：`correction` 类事实每次都会注入 prompt，agent 犯错改对的教训会被记住。

---

## agents_api — Agent 管理 API

```yaml
agents_api:
  enabled: true        # 开 — 允许通过 API 管理 agent
```

---

## skill_evolution — 技能进化

```yaml
skill_evolution:
  enabled: false       # 关
  moderation_model_name: null
  security_fail_closed: true
```
开：agent 运行时发现 skill 不够用，能自动改进。实验性功能。

---

## database — 持久化存储

```yaml
database:
  backend: sqlite       # SQLite
  sqlite_dir: .deer-flow/data
```
存储对话、Agent 运行时数据。单机用 SQLite 够用。

---

## run_events — 运行时事件

```yaml
run_events:
  backend: memory       # 内存存储 — Gateway 重启后丢失
  max_trace_content: 10240
  track_token_usage: true
```
推荐改为 `backend: database`，跑的事件记录到数据库，方便事后分析。

---

## scheduler — 定时任务

```yaml
scheduler:
  enabled: false
  poll_interval_seconds: 5
  lease_seconds: 120
  max_concurrent_runs: 3
  min_once_delay_seconds: 60
```
开：支持定时/定期运行 agent。目前关着。

---

## run_ownership — 运行锁

```yaml
run_ownership:
  lease_seconds: 30
  grace_seconds: 10
  heartbeat_enabled: false
```
防止同一线程的并发运行冲突。

---

## authorization — 授权

```yaml
authorization:
  enabled: false       # 关 — 内网环境不需要
```
开：需要登录认证才能用。

---

## channel_connections — IM 渠道

```yaml
channel_connections:
  enabled: false       # 关
  telegram: { enabled: false }
  slack: { enabled: false }
  # 飞书/钉钉/微信/企微 都关
```
开：接入飞书/钉钉/Slack/Telegram 等 IM 平台。
