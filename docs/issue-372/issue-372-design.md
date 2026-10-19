# Issue #372 设计说明：知识正文大小上限默认值 100KB → 5MB

> 承接 #358（PR #511）落地的 `KNOWLEDGE_MAX_CONTENT_BYTES` 配置体系，本次仅调整默认值与报错文案。#358 完整设计见 `docs/weekly-07/issue-358-design.md`。

## 变更内容

1. **默认值**：`src/api/routes/knowledge.py` 中 `_DEFAULT_MAX_CONTENT_BYTES` 由 `100 * 1024` 提升为 `5 * 1024 * 1024`（5242880 = 5MB）。未配置环境变量的部署自动放宽到 5MB。
2. **报错文案**：新增常量 `_CONTENT_LIMIT_CONFIG_HINT = "，可通过环境变量 KNOWLEDGE_MAX_CONTENT_BYTES 调整"`，member 创建、member 更新（knowledge.py）与 agent 写入（events.py add_knowledge）三处超限 400 报错统一追加该提示；上限数值继续由 `_format_content_limit()` KB/MB 自适应输出。
3. **前端兜底**：`src/api/static/js/pages/knowledge.js` 的 `contentLimitBytes` 兜底 102400→5242880、`contentLimitDisplay` '100KB'→'5MB'（stats API 正常时仍以服务端返回为准）。
4. **配置示例**：`.env.example` 注释与示例值更新为 `5242880=5MB`。

## 部署/配置说明

- 不设置 `KNOWLEDGE_MAX_CONTENT_BYTES`：默认 5MB。
- 自定义上限：设置环境变量（字节），如 `KNOWLEDGE_MAX_CONTENT_BYTES=2097152`（2MB）；非数字回退默认 5MB，低于 1024 钳制为 1024。
- 升级注意：仅放宽不收紧，存量知识读写不受影响，无需数据迁移。

## 边界保护（不变）

- prompt 注入侧 `_KNOWLEDGE_INJECT_MAX_CHARS = 8000` 字符截断保持不动，防止 5MB 知识全量注入 LLM context（截断后可通过 read tool 读取全文）。
- `_MIN_CONTENT_BYTES = 1024` 下限钳制逻辑不变。
