# 产品 wiki 接入方法（wiki-kb-recipe）

> **共享权威源**：`auto-req-analysis` 的阶段一质控与阶段二分析共读本文件。wiki 查询方法以仓库根 [`.wiki-schema.md`](../../../.wiki-schema.md) 为准；探活、三态、降级和审计字段以 [`knowledge-base.md`](knowledge-base.md) 为准。本文件只规定 skill **何时读 wiki、读什么、怎么消费、与 GitNexus 的读序和兜底**。
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

- **触发条件同 KB 发现链**：需求命中高风险早筛（①②⑤⑥），**或**描述含可映射业务概念（结算/费用/医保/上传/校验/报表/接口/明细/账单等）时读 wiki。纯文字、无业务实体且非高风险的需求**不查**（主动跳过，非降级）。
- **读序位置**：在 menu-index 区域收敛**之后**、GitNexus 证据链**之前**。先懂业务语义，再用代码事实地面验证。
- wiki 处于早期：`sources/` 已有部分模块文档，`topics/`/`entities/` 可能仍空。**"wiki 无该模块页面"不是负面发现**，记 `wiki-未覆盖` 后继续，绝不阻断。

## §2 怎么读（遵从 `.wiki-schema.md` §Query）

按仓库根 [`.wiki-schema.md`](../../../.wiki-schema.md) 的 Query 规则，**不要另造方法**：

1. 读仓库根 [`index.md`](../../../index.md) 定位相关条目；`index.md` 缺失时直接 grep。
2. 用 Grep 在仓库根 `wiki/` 下搜模块/业务域关键词，并用 `.wiki-schema.md` 的**别名词表**扩词（搜索任一词同时搜同组别名）。
3. 阅读命中的 2–3 页（早期主要落在 `wiki/sources/{日期}-{模块}.md`），综合提取。
4. 每条结论**标注来源页面**（引用 `wiki/sources/xxx.md`）。

只读 `wiki/` 与 `raw/`（`raw/` 是原始素材，AI 只读不改）。不调任何 MCP、不写 wiki、不回流。

## §3 提取什么 + 三态标注

从命中页面提取：

- **业务规则 / 流程 / 数据口径 / 验收口径 / 既有实现描述**（喂判定）。
- **子系统名 / 仓库名**（→ GitNexus `query`/`context` 的首选技术锚点）。
- **菜单匹配率 / 菜单编码约定**（→ 印证 menu-index）。

每条结论三态标注：

| 状态 | 含义 |
|---|---|
| **wiki-确认** | wiki 页面明确支撑该实体/规则（引用页面） |
| **wiki-冲突** | wiki 与需求假设**或与 GitNexus 代码事实**矛盾（→ 质控 spec 闸信号 / 分析风险标记；**与代码冲突时一律以代码为准**，wiki 记为疑似过时/待更新） |
| **wiki-未覆盖** | wiki 无该模块页面或沉默（不是负面发现，降级继续） |

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

- **wiki 缺失/未覆盖该模块** → 记 `wiki.ready=false` 或 `wiki.note=未覆盖`，静默回退今天的 GitNexus+menu-index 行为，**绝不阻断、绝不转 ERROR、绝不因此判 MANUAL**。
- **GitNexus 不可用** → wiki 仍可供业务判断（AUTO/MANUAL 改动类型、高风险定性、需求与既有规则冲突），但**不替代**代码定位与查重（那两项按 `knowledge-base.md` §三既有降级跳过）。
- 三者全不可用 → 回到今天的行为（menu-index + 默认核对 / 启发式）。

## §6 审计字段（计划 JSON 可选 `wiki` 对象）

```json
"wiki": {
  "ready": true,
  "modules_matched": ["药房药库"],
  "findings": [
    {"entity": "药房=发药点 / 药库=中心库 对称结构", "state": "wiki-确认", "source": "wiki/sources/2026-07-27-药房药库.md"},
    {"entity": "子系统 batmed-yfyk-base-frame", "state": "wiki-确认", "source": "wiki/sources/2026-07-27-药房药库.md"}
  ],
  "note": "wiki 就绪，命中药房药库模块；子系统名已作 GitNexus query 锚点"
}
```

wiki 未就绪/未覆盖时 `"ready": false` 并在 `note` 写原因；由 `pipeline.py` 透传到 `过程文件/<id>/runs/run_*.json`，不在 `validate` 的 required 集内，向后兼容。

## §7 一句话 prompt（执行者）

```text
按 .wiki-schema.md §Query 查 wiki：读 index.md → grep wiki/（别名词表扩词）→ 读命中 2-3 页。
提取业务规则/流程/数据口径/验收 + 子系统名（作 GitNexus 锚点）+ 菜单匹配率；三态标注 wiki-确认/wiki-冲突/wiki-未覆盖。
质控写 checklist.items/kb_note/wiki.findings 不判终局；分析只落 wiki.findings 作内部佐证、不进正文。无该模块页记 wiki-未覆盖，降级继续，不阻断。
```
