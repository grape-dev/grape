# Spec: Issue #372 — 知识库知识正文大小上限默认值 100KB → 5MB

> 来源：Issue #372（承接 #358 配置体系）；完整 Spec 见 Issue 评论（Issue Agent，2026-10-19）。

## 需求

- 未配置 `KNOWLEDGE_MAX_CONTENT_BYTES` 的默认部署下，5MB 以内知识可成功入库（原来 100KB）。
- 超限 400 报错在「content 不能超过 {N}MB」之外追加配置方法提示：可通过环境变量 `KNOWLEDGE_MAX_CONTENT_BYTES` 调整。
- 三处校验点（member 创建 / member 更新 / agent add_knowledge 写入）口径一致。
- 前端 `knowledge.js` 兜底值同步 5MB（stats API 正常时仍以服务端返回为准）。
- `.env.example` 注释与示例值同步（5242880=5MB）。

## Out of Scope

- `_KNOWLEDGE_INJECT_MAX_CHARS`（prompt 注入截断 8000 字符）不动——防止 5MB 全量注入爆 LLM context。
- `_MIN_CONTENT_BYTES` 下限（1KB）与钳制/回退逻辑不变。
- 管理台 UI 配置入口、DB schema、知识分片存储。

## 验收标准

1. 未配置时 5MB 内知识可入库；超限 400 报错含上限值与 `KNOWLEDGE_MAX_CONTENT_BYTES` 配置方法。
2. 环境变量覆盖生效；非数字回退 5MB、<1KB 钳制 1KB 回归通过。
3. agent 写入与 member 创建/更新报错口径一致；前端兜底 5MB。
4. 存量知识读写不受影响；`.env.example` 与文档体现；现有测试通过 + 新用例覆盖。
