# 实施计划：help 目录独立化

## 任务分解

### T1: help/usage/ — 9 个 markdown 拆分
- 从 help.html 提取 9 个章节，逐章转换为 markdown
- 转换映射：h1→#, h2→##, h3→###, ul→-, ol→1., pre→```, table→md table, card→blockquote
- FAQ 交互展开转为静态标题+正文

### T2: help/api/ — 13 文件原样复制
- `cp docs/openapi/*.md help/api/`
- 校验文件数与内容一致

### T3: help.html 迁移注释
- 在 `<head>` 后插入 HTML 注释说明文档已迁移至 `help/` 目录

### T4: 测试用例
- 验证 help/ 目录结构
- 验证 usage/ 9 文件存在且无残留 HTML 标签
- 验证 api/ 13 文件与源一致（diff）
- 验证 help.html 含迁移注释

### T5: 自审查 + PR + A2A QA
