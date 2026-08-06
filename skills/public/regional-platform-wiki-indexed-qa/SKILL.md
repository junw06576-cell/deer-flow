---
name: regional-platform-wiki-indexed-qa
description: >-
  【必须加载·优先级最高】区域卫生信息平台 V6.0 Wiki 索引问答。第一步必须读取
  /mnt/knowledge/regional-platform-wiki-indexed/wiki_index.md，再在索引列出的范围内使用
  read_file、grep 和 glob 检索同一固定根目录下的 Markdown 正文。任何请求都禁止访问
  /mnt/knowledge、父目录、其他产品目录、Web 或 RAGFlow；固定路径不可用时失败关闭。
---

# 区域平台 Wiki 索引问答

## 最高优先级：固定范围与失败关闭

以下规则对每一次请求生效，包括“检索知识库目录”“有哪些知识库”“列出目录”“查看文件”等元问题：

1. 唯一索引是 `/mnt/knowledge/regional-platform-wiki-indexed/wiki_index.md`。
2. 唯一正文根目录是 `/mnt/knowledge/regional-platform-wiki-indexed/`，其中只允许索引列出的 13 份正文。
3. 禁止对 `/mnt/knowledge`、`/mnt/knowledge/`、任何父目录或兄弟目录执行 `ls`、`glob`、`grep` 或 `read_file`。
4. 固定索引或正文根目录不存在、不可读或返回异常时，立即停止并报告配置错误；禁止退回全库目录寻找替代路径。
5. “检索知识库目录”只表示读取固定索引并返回其中的 13 份区域平台资料；不得输出文件系统根目录、其他 Wiki 产品目录或 RAGFlow 数据集。

目录类问题的固定流程：

```text
read_file("/mnt/knowledge/regional-platform-wiki-indexed/wiki_index.md")
→ 只整理索引中的标题、资料类型和相对路径
→ 不再调用 ls、glob、grep 或任何 RAGFlow 工具
```

## 事实边界

唯一事实来源是：

- `/mnt/knowledge/regional-platform-wiki-indexed/wiki_index.md` 列出的 Markdown 正文。
- 正文必须位于 `/mnt/knowledge/regional-platform-wiki-indexed/` 下且路径不能包含 `..`。

业务范围限定为区域卫生信息平台 V6.0 及其湖仓一体方案，主要包括产品介绍、产品特点、功能清单、建设方案、技术参数、控标参数、售前宣讲、演示脚本、演示控标点、硬件资源，以及湖仓一体功能清单、建设方案和解决方案。

索引只负责定位，不能单独作为产品事实来源。

## 工具边界

允许：

- `read_file`
- `grep`
- `glob`

禁止：

- `web_search`、`web_fetch`
- `ragflow_search`、`ragflow_list_datasets`、`ragflow_chat`
- `write_file`、`str_replace`、`bash`
- 读取索引未列出的知识库、目录或文件
- 访问 `/mnt/knowledge` 根目录、父目录或其他挂载
- 根据模型记忆补全功能、参数、版本、金额、账号、密码、地址或案例

## 固定工作流

### 1. 强制读取索引

第一步必须执行：

```text
read_file("/mnt/knowledge/regional-platform-wiki-indexed/wiki_index.md")
```

根据标题、相对路径、资料类型、关键词和检索别名选择 1 至 3 个候选文件。标记为占位、停用或未入库的条目不能作为事实来源。

索引链接必须是相对于固定正文根目录的安全相对路径，不能以 `/` 开头或包含 `..`。不满足条件时按配置错误处理。

### 2. 缩小正文范围

优先读取最相关候选。长文档先在候选文件或候选目录内使用 `grep` 搜索核心词、同义词和标题词，再读取命中位置附近正文。`glob` 只能用于 `/mnt/knowledge/regional-platform-wiki-indexed/` 内且必须受索引候选约束。

最多读取 5 个正文文件。达到上限仍没有充分证据时，按未命中处理。

### 3. 判断证据

回答前检查：

- 每项结论均有 Markdown 正文直接依据。
- 数字、日期、版本、硬件规格、端口、账号和密码与正文完全一致。
- 同一主题出现冲突时，说明冲突及各自来源，不自行裁决。
- “技术参数”“控标参数”“功能清单”不能互相冒充；按用户问题选择对应资料类型。
- 涉及湖仓一体时，优先使用“湖仓一体方案”目录下资料，并说明它与区域平台通用资料的关系。

### 4. 输出

先直接回答，再列真实正文来源，不描述内部检索过程。

```markdown
来源：
- 《文档标题》：`/mnt/knowledge/regional-platform-wiki-indexed/相对路径.md`
```

涉及账号、密码、内网地址或 VPN 要求时，仅在用户明确询问相关内容时按正文回答；不得猜测缺失项，也不要在无关回答中主动展示。

## 未命中与异常

索引无候选、只有占位条目、正文无直接依据、文件读取失败或超过读取上限时，回答：

```text
当前区域平台 Wiki 知识库中未找到能够支持该问题的明确资料。
已检查目录索引和相关候选正文，不使用其他知识库补充回答。
```

固定挂载或索引异常时使用：

```text
区域平台索引知识库配置异常：固定索引或正文目录不可用。
为避免访问其他知识库，已停止检索。
```

只命中部分内容时，分别说明“已找到”和“仍缺少”的信息。
