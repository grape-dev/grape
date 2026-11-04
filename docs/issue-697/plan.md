# 方案设计（Issue #697）

判定语义比较适合 LLM，代码侧只做结构化输出契约 + 门禁（与 Spec 决策一致：A + 结构化输出契约）。

## 改动点

| # | 文件 | 改动 |
|---|------|------|
| 1 | src/runtime/scheduler.py | ① 新增 `AUTOPILOT_DEDUP_RULE`（根因三要素 + 三级判定 + owner 确认 + 级联关闭守护文案，模块级常量供复用）；② `_build_autopilot_prompt` 末尾注入该规则；③ `parse_findings` 为 finding 补 `dedup_level`/`dedup_note` 默认字段（透传） |
| 2 | src/api/routes/autopilot.py | ① `POST /polish` 润色 prompt 注入同一规则（要求润色保留判重双维度要求）；② `DispatchBody` 增 `dedup_confirmed`；③ `_dispatch_finding_core` 门禁：`dedup_level=suspected_duplicate` 且未确认 → 409「疑似重复需 owner 确认」；确认后标记 `dedup_confirmed=true` 落 findings_json；疑似/差异说明追加进派发描述 |
| 3 | src/prompt_builder.py | issue summary 注入处追加 `ISSUE_DEDUP_ROOT_CAUSE_RULE`（判重必须比对根因三要素；仅标题相似 → 疑似重复，通知 owner agent 确认；resolve 级联关闭对未确认疑似重复不生效） |
| 4 | src/api/static/js/pages/autopilot.js | ① issue_patrol 模板 instruction 重写（双维度判重步骤 + 分级输出）；② findings 渲染对 `dedup_level=suspected_duplicate` 显示「疑似重复」徽标 |
| 5 | src/api/static/locales/{zh,en}/autopilot.json | 模板描述 + findings 徽标文案 |
| 6 | tests/（新增） | tests/test_issue_697_dedup_root_cause.py + tests/frontend/test_issue_697_dedup_template.js |

## 判定级别取值
- `exact_duplicate`：标题相似 + 根因三要素一致 → 可自动并入（挂靠）
- `suspected_duplicate`：仅标题相似、根因不一致 → 挂起，通知 owner agent 确认
- `not_duplicate`：不重复

## 回归 fixture
#446（--repo 参数报错 + runbook 降级指引缺失）vs #126（注入 GRAPE_SCENE_ID 404）；#179、#277 同类标题相似根因不同的样本。测试断言：prompt 门禁规则注入 + suspected 门禁 409 + parse_findings 透传。
