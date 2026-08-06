# 产品 wiki 接入方法（wiki-kb-recipe）

> **共享权威源**：`auto-req-analysis` 的阶段一质控与阶段二分析共读本文件。产品知识库入口固定为仓库根 [`wiki/index.md`](../../../wiki/index.md)；探活、三态、降级和审计字段以 [`knowledge-base.md`](knowledge-base.md) 为准。本文件只规定 skill **何时读 wiki、读什么、怎么消费、与 GitNexus 的读序和兜底**。
>
> wiki 是 menu-business-index（结构）与 GitNexus（代码事实）之间的**业务语义层**：模块是干嘛的、有什么业务规则/流程/数据口径/验收、既有实现长什么样。它**不是代码图谱、不是数据库字典**，只读、不写、不回流。

## §0 定位

| 知识源 | 内容 | 回答 |
|---|---|---|
| menu-business-index | area→menu→repo/API/match_status（结构） | 这需求属于哪个产品/菜单？ |
| **wiki（本文件）** | 模块→业务规则/流程/数据口径/验收 + 子系统名 | 这模块是干嘛的、有何规则？需求假设与既有规则是否冲突？ |
| GitNexus | 代码 AST/调用链/路由/查重（代码事实） | 代码现在到底怎么实现？在哪？是否已有相似实现？ |

wiki 的业务叙述里常带**子系统名/仓库名**（如 `batmed-yfyk-base-frame`、`batwce-v5.6`）和**菜单匹配率**——前者直接作 GitNexus 的 query 锚点（wiki→GitNexus 桥），后者与 menu-index 互相印证。

> **权威顺序（必须遵守）**：**代码事实（GitNexus）为准，wiki 是补充。wiki 与 GitNexus 冲突时一律以代码为准**——代码库是最准的。wiki 只填补代码沉默处的**业务语义**（业务意图/规则/口径/验收），用来帮你理解"这模块是干嘛的、有什么业务规则"，**不替代、不否定代码事实**；wiki 给的子系统名/仓库名也只是 GitNexus 的**候选锚点**，须经证据链证实。注意：这和"读序上 wiki 先读"不矛盾——先读 wiki 懂业务语义，再用代码地面验证，冲突时代码说了算。

## §1 何时跑（两阶段）

- **触发条件同 KB 发现链（字典驱动）**：需求命中高风险早筛（①②⑤⑥），**或**描述/标题/验收触及 [`business-keyword-dictionary.md`](business-keyword-dictionary.md) 中任一业务概念（标准名/同义词/旧称/拼音缩写任一）时读 wiki。**不再限于"结算/费用/医保/上传/校验/报表/接口/明细/账单"9 词枚举**——该枚举漏掉对账/转科/处方/发药/库存/床位/退药等同义词需求。判"纯文字、无业务实体且非高风险"而跳过时，须在 `wiki.note` 写明"对照字典检查了哪些概念、为何未命中"。
- **界面/布局类需求的 wiki 不可跳过**：当需求涉及界面展示/布局/增减展示字段或列/界面交互时，即使未命中业务字典，也须读 wiki 查找该模块的界面布局描述（`iteration-analysis-closure.md` §1 要求现状基线交叉证实）。wiki 未覆盖该模块时记 `wiki.note=未覆盖` 并按 §5 降级，但不得因"未触发字典"而跳过。
- **读序位置**：在 menu-index 区域收敛**之后**、GitNexus 证据链**之前**。先懂业务语义，再用代码事实地面验证。
- wiki 按 `门诊/`、`住院/`、`电子病历/`、`平台/`、`数据库/` 组织。**"wiki 无该模块页面"不是负面发现**，记 `wiki-未覆盖` 后继续，绝不阻断。

## §2 怎么读（固定入口 + 相对链接）

严格按以下顺序读取，不从历史路径或猜测文件名开始：

1. **完整读取 [`wiki/index.md`](../../../wiki/index.md)**，先掌握文章索引与各主题摘要；索引不可读时记 `wiki.ready=false` 并按 §5 降级，不把全目录 grep 当成等价入口。
2. 根据需求对象、动作和索引摘要，定位 `门诊/`、`住院/`、`电子病历/`、`平台/` 或 `数据库/` 下的文章。索引无法唯一定位时，才在 `wiki/` 内 grep 业务词及需求中已有的同义词，再回到命中文章。
3. 跟随索引或文章中的标准 Markdown 相对链接。链接目标必须相对**当前 Markdown 文件所在目录**解析；不得把链接显示文本当成真实路径，也不得凭文件名改写目标。
4. 阅读命中文章及为理解该事实所需的直接关联页；不要固定读取页数，也不要一次性加载整个主题目录。
5. 读取文章头的 `> Sources:`、`> Raw:`、`> Updated:`：`Sources` 说明素材名称，`Raw` 提供原始文件链接，`Updated` 只表示更新时间、不代表事实权威性。
6. 每条 finding 的 `source` 写命中文章的真实仓库相对路径（如 `wiki/门诊/门诊药品流转.md`），不写旧目录、链接显示文本或 Raw 路径。

只读 `wiki/` 与 `raw/`（`raw/` 是原始素材，AI 只读不改）。不调任何 MCP、不写 wiki、不回流。

### Raw 按需复核

- 普通业务语义由 wiki 文章明确支撑即可标 `wiki-确认`，不为每篇文章加载全部 Raw。
- 以下事实必须沿 `> Raw:` 中与该事实直接相关的链接复核：用于消除 QC 阻断并放行的事实、高风险判定、AUTO 关键证据、wiki 与需求/代码冲突的事实，以及表述存疑的事实。
- `Raw` 链接同样相对当前 wiki 文章解析，只读取与待核事实相关的原始文件；已复核的仓库相对路径写入 `wiki.note`，`wiki.findings[].source` 仍保留 wiki 文章路径。
- 关键事实的 Raw 链接缺失、不可读或未支撑对应结论时，不得标 `wiki-确认`，也不得用于 `qc_evidence_resolution` 或 AUTO 放行：无相反事实时标 `wiki-未覆盖` 并在 `wiki.note` 记录限制；Raw 与文章明确矛盾时标 `wiki-冲突`。

## §3 提取什么 + 三态标注

从命中页面提取：

- **业务规则 / 流程 / 数据口径 / 验收口径 / 既有实现描述**（喂判定）。
- **子系统名 / 仓库名**（→ GitNexus `query`/`context` 的首选技术锚点）。
- **菜单匹配率 / 菜单编码约定**（→ 印证 menu-index）。

每条结论三态标注：

| 状态 | 含义 |
|---|---|
| **wiki-确认** | wiki 页面明确支撑该实体/规则；属于关键事实时还须 Raw 复核通过（引用 wiki 页面，Raw 路径记 `wiki.note`） |
| **wiki-冲突** | wiki 与需求假设、Raw 原文**或与 GitNexus 代码事实**矛盾（→ 质控 spec 闸信号 / 分析风险标记；**与代码冲突时一律以代码为准**，wiki 记为疑似过时/待更新） |
| **wiki-未覆盖** | wiki 无该模块页面、页面沉默，或关键事实缺少可用 Raw 支撑（不是负面发现，记录限制后降级继续） |

不得把 wiki 的叙述当成代码事实；子系统名只作 GitNexus **候选锚点**，须经 `query→context→route_map→context` 证据链证实。**wiki 叙述与 GitNexus 代码事实冲突时，一律以代码为准**（代码库最准），wiki 仅记为业务侧补充/疑似过时。

## §4 两阶段怎么消费（与 `knowledge-base.md` §五对齐）

### 质控 —— 有目的发现·不直接判终局

- wiki 发现同源写入 `checklist.items`（命名实体的选择式问题，如"wiki 说该模块已支持 X，是否重复？"）/ `checklist.kb_note`（三态摘要）/ `wiki.findings`（机读）。
- 经现有维度参与终局：`qc-checklist.md` #3 可识别性、#5 重复；**不加 verdict 类别、不在 `qc-rules.md` 新增 wiki 专属判定步**。
- **spec 清晰度闸**：wiki 表明的既有规则与需求假设冲突 → `NEED-REVIEW` 挡回产品。

### 分析 —— 内部佐证·不进 PM 视角正文

- wiki **仅作内部佐证**，结果落 `wiki.findings`，用于：印证 AUTO/MANUAL 的**业务改动类型**（改文案/补单测/调配置 vs 改业务规则/流程/数据写入）、查重、高风险定性佐证（③④⑥）、需求与既有规则冲突标记。
- **绝不把 wiki 的业务叙述写进 `分析者描述`/变更方案正文**（与代码图谱同铁律）；分析者描述的 `路径` 仍只写业务菜单路径和操作路径，其素材来自工作项、已解析附件或唯一菜单索引。
- wiki 提供的子系统名可写入 testing-log 的"知识图谱作用"区，作 GitNexus 锚点说明。

## §5 读序与兜底（非对称）

**读序**：

```text
menu-index(结构: area→menu→repo/API)
  → wiki(业务语义: module→规则/流程 + 子系统名)
  → GitNexus(代码事实: 定位/证据链/查重)
  → db-knowledge(按需: 字段/迁移/统计)
```

**兜底（GitNexus 主干 + 兜底，wiki 补充）**：

- **wiki 缺失/未覆盖该模块** → 记 `wiki.ready=false` 或 `wiki.note=未覆盖`，回退 GitNexus+menu-index 行为，**绝不阻断、绝不转 ERROR**。**例外·界面/布局类需求**：wiki 是界面现状基线的必需交叉证实源之一（见 `iteration-analysis-closure.md` §1）；wiki 未覆盖时记 `evidence_gap`（"界面现状缺 wiki 交叉证实"），AUTO-ANA 候选降为 MANUAL-REVIEW（与 kb 未就绪同等处理，见 `knowledge-base.md` §三「不升级」原则的 AUTO 例外），MANUAL 路径不因此升级。
- **GitNexus 不可用** → wiki 仍可供业务判断（AUTO/MANUAL 改动类型、高风险定性、需求与既有规则冲突），但**不替代**代码定位与查重（那两项按 `knowledge-base.md` §三既有降级跳过）。
- 三者全不可用 → 回到今天的行为（menu-index + 默认核对 / 启发式）。

## §6 审计字段（计划 JSON 可选 `wiki` 对象）

```json
"wiki": {
  "ready": true,
  "modules_matched": ["药房药库"],
  "findings": [
    {"entity": "门诊发药和退药归药房管理，药库出库归药库库存管理", "state": "wiki-确认", "source": "wiki/门诊/门诊药品流转.md"},
    {"entity": "子系统 batmed-yfyk-base-frame", "state": "wiki-确认", "source": "wiki/门诊/门诊药品流转.md"}
  ],
  "note": "wiki 就绪，命中门诊药品流转；关键事实已复核 raw/平台/药房药库.md，子系统名已作 GitNexus query 锚点"
}
```

wiki 未就绪/未覆盖时 `"ready": false` 并在 `note` 写原因；由 `pipeline.py` 透传到 `过程文件/<id>/runs/run_*.json`，不在 `validate` 的 required 集内，向后兼容。

## §7 一句话 prompt（执行者）

```text
完整读 wiki/index.md → 按门诊/住院/电子病历/平台/数据库定位文章 → 按当前文件解析 Markdown 相对链接；只在索引无法定位时 grep wiki/。
读取 Sources/Raw/Updated；关键事实按需沿 Raw 复核并把 Raw 路径记入 wiki.note，finding.source 始终写 wiki 文章真实相对路径。
提取业务规则/流程/数据口径/验收 + 子系统名（作 GitNexus 锚点）+ 菜单匹配率；三态标注 wiki-确认/wiki-冲突/wiki-未覆盖。
质控写 checklist.items/kb_note/wiki.findings 不判终局；分析只落 wiki.findings 作内部佐证、不进正文。无该模块页记 wiki-未覆盖，降级继续，不阻断。
```
