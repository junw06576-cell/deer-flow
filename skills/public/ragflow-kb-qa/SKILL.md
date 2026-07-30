---
name: ragflow-kb-qa
description: >-
  【必须加载】RAGFlow 向量知识库问答。使用 ragflow_search 检索
  卫宁产品数据集(bd5a81564bc211f18b994fc5dc852303)和湖南医共体数据集(412c05aa83f311f19538895b6af4b32b)。
  每次检索必须同时传入双数据集。禁止使用 web_search/read_file 检索产品信息。相似度阈值0.2。
  加载后必须 read_file 本 SKILL.md 获取完整约束。
---

# RAGFlow 向量知识库问答

## ⚠️ 执行规则（必须遵守）

### 硬约束（禁止违反）

1. **禁止 web_search**：绝对不要调用 `web_search` 工具。所有产品信息必须来自 RAGFlow 知识库。违反此规则将导致回答无效
2. **禁止文件系统工具**：本 Agent 不使用 `read_file`、`search_file`、`search_content` 检索产品信息（这些属于 wiki-kb）
3. **双数据集强制规则**：每次 `ragflow_search` 调用必须同时传入以下两个数据集：
   - `bd5a81564bc211f18b994fc5dc852303`（卫宁平台标准方案）
   - `412c05aa83f311f19538895b6af4b32b`（湖南医共体指南）
4. **最多 2 次检索（节约 embedding 资源）**：每个用户问题最多 2 次 `ragflow_search`（1 次初始检索 + 1 次优化重试）。2 次后仍无结果则直接告知用户
5. **标注相似度**：每条检索结果必须标注来源文档和相似度分数
6. **禁止调用 ragflow_list_datasets**：不要使用该工具做前置诊断，直接检索即可。数据集已在配置中固定

### 工具清单

| 工具 | 用途 | 何时用 |
|------|------|--------|
| `ragflow_search` | 文档块语义检索 | 查找具体事实、段落、数据 |
| ~~`ragflow_list_datasets`~~ | — | **禁止在问答流程中使用** |
| ~~`ragflow_chat`~~ | — | **不稳定，禁止使用（避免额外embedding消耗）** |
| ~~`web_search`~~ | — | **禁止使用** |
| ~~`read_file`~~ | — | **禁止用于产品问答** |

## 数据集

### 默认双数据集（未指定时强制传入）

| 数据集名称 | 数据集 ID | 内容范围 |
|-----------|----------|---------|
| 卫宁平台标准方案 | `bd5a81564bc211f18b994fc5dc852303` | 卫宁产品方案、区域卫生平台、wiki 知识库 |
| 湖南医共体指南 | `412c05aa83f311f19538895b6af4b32b` | 湖南省紧密型县域医共体技术方案、数据标准、接口规范、数据字典 |

### 调用方式

每次 `ragflow_search` 调用必须使用以下参数格式：
```
ragflow_search(
    query="用户问题",
    dataset_ids=["bd5a81564bc211f18b994fc5dc852303", "412c05aa83f311f19538895b6af4b32b"],
    similarity_threshold=0.2,
    vector_similarity_weight=0.5
)
```

## 检索工作流（严格限制 embedding 调用次数）

### 核心原则

每次 `ragflow_search` 调用都会触发一次 RAGFlow retrieval，进而在 RAGFlow 服务端产生一次 query embedding。为节约 embedding 资源，严格限制调用次数。

### 第 1 次：初始检索（唯一检索）

使用用户原始问题或稍加提炼的关键词，一次性传入双数据集。用最精炼的 query 覆盖用户意图：
```
ragflow_search(query="用户问题关键词", dataset_ids=["bd5a8156...", "412c05aa..."])
```

如果返回了有效结果（chunks 数 > 0），直接用结果回答用户。不要再发起额外检索。

### 第 2 次：优化重试（仅当第 1 次返回为空或明显不相关时）

- 用不同的角度或更具体的关键词重写 query（仅 1 次）
- 仍然使用相同的双数据集 ID
- 若第 2 次仍然无结果，直接告知用户"当前知识库未检索到相关内容"，不要再尝试第 3 次

### 结果呈现

```
根据 RAGFlow 检索结果：
- [内容1]（来源：文档名，相似度：0.XX）
- [内容2]（来源：文档名，相似度：0.XX）
...
```

- 相似度 < 0.3：标注"[低匹配 sim=X.XX]"，提醒用户结果仅供参考
- 无结果：明确告知用户，建议切换到 wiki-kb 尝试

## 与 wiki-kb 的互补关系

| 问题类型 | 推荐智能体 |
|----------|-----------|
| 精确产品名称/功能查询 | wiki-kb |
| 报价/计费方案 | wiki-kb |
| 结构化方案对比 | wiki-kb |
| 模糊语义查询 | **ragflow-kb（本技能）** |
| 原始文档全文搜索 | **ragflow-kb（本技能）** |
| 跨文档关联检索 | **ragflow-kb（本技能）** |
| 治理指标/考核标准 | **ragflow-kb（本技能）** |

## 故障处理

| 错误 | 处理 |
|------|------|
| `API_KEY not configured` | 告知用户配置 RAGFLOW_API_KEY |
| `Connection error` | RAGFlow 服务未启动，告知用户检查 Docker |
| `Embedding service unavailable` | embedding 模型 API 异常，禁止重试，直接告知用户服务暂时不可用 |
| `No relevant content found` | RAGFlow 未找到相关内容（可能是 embedding 服务异常），仅重试 1 次，仍无结果则建议尝试 wiki-kb |
