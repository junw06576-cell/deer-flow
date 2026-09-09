# Skill 产物落盘与前端返回机制

## 一句话结论

产物要在前端对话里被"看见"（出文件卡片、可点击下载），必须同时做两件事：

1. **把文件写到 `/mnt/user-data/outputs/`**
2. **收尾调用 `present_files` 工具**

两步缺一不可。只写文件不调 `present_files` = 白写，前端看不到。

## 三个目录，各司其职（别写错）

| 目录 | 用途 | 前端能返回吗 |
|---|---|---|
| `/mnt/user-data/uploads` | 用户上传的附件 | 否 |
| `/mnt/user-data/workspace` | 临时文件、过程文件 | 否 |
| `/mnt/user-data/outputs` | **最终交付物** | ✅ 能 |

skill 要交付给用户的产物，一律落 **outputs**，不要落 workspace。

## 为什么必须在 SKILL.md 里"显式写"落点，别靠默认

框架的 system prompt 里默认就有一条规则："最终交付物放 outputs，并用 present_files 呈现"。但**不能指望这个默认**，原因有两个：

1. **分类歧义**：默认规则只约束"最终交付物"去 outputs，没告诉模型"你的哪份产物算最终交付物"。审计类、过程类产物（比如质控凭证、中间清单）很容易被模型当成"临时文件"写到 workspace，落点跑偏。
2. **present_files 白名单卡死 outputs**：`present_files` 只认 outputs 目录内的文件。文件落到 workspace 再去 present，会直接报错，前端拿不到。

**所以：SKILL.md 里要显式写死落点 + 显式写 present_files，把模型的裁量空间压到零。**

## Skill 作者要做的事（两条硬约束）

在 SKILL.md 里加这两句：

1. **落点**：产物必须写到 `/mnt/user-data/outputs/<子目录>/`（建议按 run_id 或文件名建子目录，避免同名覆盖）。
2. **收尾**：产物写完、自检通过后，调用 `present_files` 工具，把产物路径传进去。

### 修改示例（对照）

**改之前（有坑：落点不对、没 present，前端看不到）**

```markdown
## 产物落盘
分析完成后，把三份产物写入 `过程文件/<id>/<run_id>/` 目录。
```

问题：`过程文件/` 不在 outputs 里，`present_files` 白名单不认，前端出不了卡片。

**改之后（正确：落 outputs + present）**

```markdown
## 产物落盘与呈现（硬约束）
1. 三份产物全部写入 `/mnt/user-data/outputs/<run_id>/`：
   - 分析报告_<id>_<run_id>.md
   - 输入载荷_<id>_<run_id>.json
   - 待确认清单_<id>_<run_id>.md
2. 三份全部写完、结构自检通过后，调用 `present_files` 工具，
   把以上三个文件的**绝对路径**作为列表一次性传入。
3. 禁止落 `/mnt/user-data/workspace` 或其他目录。
```

两个改动点：① 落点从自定义目录改成 outputs；② 收尾补一句 `present_files`。

## 完整链路（文件怎么被前端看见）

```
1. skill 写文件        → /mnt/user-data/outputs/<run_id>/xxx.md
2. skill 调 present_files → 路径登记进 thread.state.artifacts
3. 前端渲染             → 读 artifacts 数组，出文件卡片
4. 用户点击             → GET /api/threads/{tid}/artifacts/{path} 下载/预览
```

`present_files` 是**唯一**把路径登记进 `artifacts` 状态的入口。文件卡片、markdown 里的 `/mnt/` 链接、内联图片，全都依赖这个登记动作。

## 注意事项

- **outputs 是 thread 私有目录，随 `DELETE /threads` 一起删除**。如果产物需要长期留痕、跨会话可查，不能只写 outputs，要另外落到持久目录（如宿主机挂载目录），outputs 只作为"本次交付展示"用。
- `present_files` 接收的是**文件路径列表**，不是文件内容，多份产物一次传全。
- 产物是"最终交付物"才 present；纯过程文件、临时脚本不要 present。

## 参考实现

- 前端文件卡片组件：`frontend/src/components/workspace/artifacts/artifact-file-list.tsx`
- `present_files` 工具：`backend/packages/harness/deerflow/tools/builtins/present_file_tool.py`
- 默认落点规则（system prompt）：`backend/packages/harness/deerflow/agents/lead_agent/prompt.py`
