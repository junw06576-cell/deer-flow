# AI 索引维护输出规范

## JSON 结构

```json
{
  "schema_version": "1.0",
  "source_commit": "40位Git提交SHA",
  "directory_aliases": [
    {
      "directory": "01-产品策划中心/00-云HIS",
      "aliases": ["基层医疗", "云EMR", "移动护理", "移动查房"],
      "evidence_paths": [
        "01-产品策划中心/00-云HIS/01-云HIS、云EMR、移动护理、移动查房系统功能清单【2025.12.31】.md"
      ]
    }
  ],
  "warnings": []
}
```

## 字段约束

- `schema_version`：固定为字符串 `1.0`。
- `source_commit`：原样复制请求中的 40 位 Git SHA。
- `directory_aliases`：完整目录别名集合，按 `directory` 排序。
- `directory`：基础清单中真实存在的相对目录；不得以 `/` 开头。
- `aliases`：去重并排序；每项 2 至 20 个字符；每个目录最多 8 项。
- `evidence_paths`：1 至 5 个基础清单中真实存在、属于目标目录且非占位的 Markdown 路径。
- `warnings`：字符串数组；没有警告时输出空数组。

## 禁止字段

任何层级都不得出现：

`summary`、`description`、`answer`、`facts`、`content`、`features`、
`price`、`new_path`、`delete_path`、`external_source`。

## 输出边界

只输出 JSON 对象本身。不要使用：

不要添加“下面是结果”、Markdown 代码围栏或任何解释文字。

不要根据模型记忆创建未出现在输入目录、标题或证据文件中的别名。
