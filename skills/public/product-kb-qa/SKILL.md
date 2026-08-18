---
name: product-kb-qa
description: >-
  基于 /mnt/knowledge 下的 WiNEX Region 产品 Wiki 回答产品、方案、功能、
  技术参数、合规、市场销售和通用素材问题。适用于挂载了
  winex-region-playbook 稀疏工作区（仅 00-工作台 至 04-通用素材）的
  reg-wiki-kb Agent。必须先读取 wiki_index.md，再使用 read_file、grep、glob
  定位原文，形成结构化证据 JSON 并校验后再回答；禁止使用 Web、RAGFlow
  或通用知识补写产品事实。
allowed-tools:
  - read_file
  - grep
  - glob
---

# 产品知识库问答

## 强制规则

1. 每次处理知识库问题，第一步必须调用：

   ```text
   read_file(
     description="读取产品知识库索引",
     path="/mnt/knowledge/wiki_index.md"
   )
   ```

2. 只以 `/mnt/knowledge/wiki_index.md` 和 `/mnt/knowledge/wiki/` 中可读取的原文为依据。
3. 禁止调用 `web_search`、`web_fetch`、`ragflow_search`、`ragflow_list_datasets` 或 `ragflow_chat` 补充产品事实。
4. 禁止根据通用知识猜测产品功能、数字、版本、报价、客户或实施细节。
5. 最多读取 5 个 Wiki 页面，不含 `wiki_index.md`；`sources` 和 `related` 页面也计入上限。
6. 回答末尾必须列出实际读取并用于回答的完整来源路径。
7. 知识库为只读数据源，不得修改 `/mnt/knowledge` 中的任何文件。
8. 如果 `read_file`、`grep` 或 `glob` 因 sandbox、挂载、权限或文件不存在而失败，将内部证据状态设为 `knowledge_base_unavailable`，立即说明知识库当前不可用并停止；不得改用 Web、RAGFlow、Bash 或其他数据源继续回答。
9. 在生成最终回答前，必须先根据实际读取内容形成内部证据 JSON，并通过本 Skill 的证据校验门槛。
10. 最终回答只能使用证据 JSON 中的 `facts`；不得从模型记忆、检索过程中的印象或未记录内容补充事实。
11. 证据 JSON 默认仅作为内部中间结果，不向用户展示；只有用户明确要求时才输出。

## 可用工具

| 工具 | 用途 |
|---|---|
| `read_file` | 读取索引和候选 Wiki 页面 |
| `grep` | 索引未命中时，在 Wiki 正文中按关键词或正则兜底检索 |
| `glob` | 索引和正文检索均未命中时，按文件名模式寻找候选页面 |

不要调用未注册的 `search_content` 或 `search_file`。

## 知识库范围

```text
/mnt/knowledge/
├── wiki_index.md
└── wiki/
    ├── 00-工作台/
    ├── 01-产品中心/
    ├── 02-资质与合规/
    ├── 03-市场与销售/
    └── 04-通用素材/
```

`01-产品中心` 包含区域平台、云HIS、基本公卫、家医签约、健康体检、健康管理、妇幼保健、智慧公卫和小产品等产品线。

根目录知识库首页、`05-持久化知识沉淀` 和 `06-产品设计原型` 不在当前稀疏工作区中，不得引用。

## 两阶段流程

### 阶段一：检索与证据整理

#### 1. 读取索引

读取 `/mnt/knowledge/wiki_index.md`，根据标题、摘要和 `keywords` 匹配用户问题，通常选择 1～3 个候选页面。

索引中的相对路径以 `wiki/` 开头。调用工具时，将其转换为完整路径：

```text
wiki/01-产品中心/区域平台/区域平台-产品介绍.md
→ /mnt/knowledge/wiki/01-产品中心/区域平台/区域平台-产品介绍.md
```

#### 2. 索引未命中时兜底

先搜索正文：

```text
grep(
  description="在产品 Wiki 正文中检索关键词",
  pattern="<关键词>",
  path="/mnt/knowledge/wiki",
  glob="**/*.md"
)
```

仍未命中时，再按文件名查找：

```text
glob(
  description="按文件名寻找候选 Wiki 页面",
  pattern="**/*<关键词>*.md",
  path="/mnt/knowledge/wiki"
)
```

仅将工具实际返回且位于 `/mnt/knowledge/wiki/` 下的文件作为候选。

#### 3. 读取候选页面

使用 `read_file` 读取候选页面。优先选择与问题直接相关的页面；只有确有必要时才读取 frontmatter 中的 `sources` 或 `related` 页面，并遵守 5 页总上限。

#### 4. 形成证据 JSON

完成候选页面读取后，先在内部形成以下结构。字段必须来自实际工具结果和已读取原文；不要复制大段原文，只保留支持回答所需的最小事实。

```json
{
  "query": "用户原始问题",
  "status": "found",
  "index_read": true,
  "pages_read_count": 2,
  "pages_read": [
    {
      "path": "/mnt/knowledge/wiki/01-产品中心/示例页面.md",
      "relevance": "该页面直接说明用户询问的功能"
    }
  ],
  "facts": [
    {
      "id": "F1",
      "claim": "可直接用于回答的知识库事实",
      "source_path": "/mnt/knowledge/wiki/01-产品中心/示例页面.md",
      "section": "功能说明",
      "scope": null
    }
  ],
  "conflicts": [],
  "gaps": []
}
```

字段约束：

- `status` 只能是 `found`、`not_found` 或 `knowledge_base_unavailable`。
- `pages_read` 记录实际读取的 Wiki 页面，不包含 `wiki_index.md`。
- `facts` 中每项事实必须指向 `pages_read` 中的一个 `source_path`。
- `conflicts` 中每项使用 `topic`、`description` 和 `source_paths`，记录不同页面对同一事项的差异，不判断哪个版本更准确。
- `gaps` 使用字符串列表，记录用户问题中知识库未覆盖或无法确认的部分。
- `status=found` 时，`facts` 至少包含一项。
- `status=not_found` 时，`facts` 必须为空。
- `status=knowledge_base_unavailable` 时，立即停止检索，不生成产品事实。

#### 5. 通过证据校验门槛

生成最终回答前逐项检查：

1. `status` 为 `found` 或 `not_found` 时，`index_read` 必须为 `true`；知识库入口读取失败时允许为 `false`，但 `status` 必须是 `knowledge_base_unavailable`。
2. `pages_read_count` 必须等于 `pages_read` 的实际数量，且不得超过 5。
3. 所有页面路径必须位于 `/mnt/knowledge/wiki/` 下。
4. 每个 `claim` 必须能从对应 `source_path` 的已读取原文中直接得到支持。
5. `facts` 不得引用未读取页面，不得包含推测、常识补全或跨页面拼接出的新结论。
6. 页面冲突必须进入 `conflicts`，未覆盖内容必须进入 `gaps`。
7. 任一项不通过时，修正证据 JSON；无法修正则按未命中或知识库不可用处理，不进入正常回答。

### 阶段二：基于证据回答

- 只使用校验通过的 `facts` 回答用户问题，避免堆砌无关页面内容。
- 数字、版本、功能边界和结论必须与 `facts` 及对应原文一致。
- 根据 `scope` 说明适用条件，根据 `gaps` 明确未覆盖内容。
- 可保留有助于理解的表格、代码块或 Mermaid 图。
- `conflicts` 非空时，明确指出页面差异，不自行判断哪个版本更新。
- 来源列表只取 `facts.source_path` 去重后的路径，不列仅用于排查但未支持回答的候选页面。
- 不向用户展示证据 JSON，除非用户明确要求查看。

## 报价与敏感信息

- 报价类回答必须附加：`⚠️ 报价信息需人工复核，以正式商务文件为准。`
- 不暴露具体客户名称、合同金额或其他敏感商业信息，除非原文明确标记为公开案例。
- 未找到报价时，说明知识库未收录，并建议联系商务团队；不要估算。

## 未命中处理

若在索引和最多 5 个候选页面中仍未找到依据：

1. 明确回答：`产品知识库（Wiki）中未找到相关内容。`
2. 可建议用户切换到其他已授权的知识库智能体继续检索。
3. 不调用 Web 或 RAGFlow，不用通用知识补全答案。

## 来源格式

回答末尾统一使用：

```text
来源：
- /mnt/knowledge/wiki/<实际页面路径>
- /mnt/knowledge/wiki/<实际页面路径>
```

只列出实际读取并用于回答的页面，不把 `wiki_index.md` 作为产品事实来源。
