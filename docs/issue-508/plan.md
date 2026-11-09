# Issue #508 方案设计（Plan）

> 依据：docs/issue-508/spec.md ｜ 根因：PUT 侧 backend 无类型校验直传落库 → 深层 TypeError → 500。

## 方案总述

路由函数内显式校验（不改请求 Pydantic 模型，改动面最小，与 Spec 技术路线一致）：`backend` 仅接受字符串，其余一律 `HTTPException(400)`；同区域 `skill` 字段补同构类型防御；错误码 400（与仓库现有路由习惯一致，本 Issue 口径：**拒绝对象形态，不归一**）。

## 改动点

### 1. src/api/routes/chat.py `update_chat_config`（~L1581）

`backend` 分支（替换两行直传）：

- `body["backend"]` 为 `str`（且非空）→ 原样写入（旧前端路径，行为不变）；
- 其他任何类型（dict/list/int/float/bool）→ `HTTPException(400, "backend 必须为字符串")`；
- 空串/None/缺键 → 跳过写入（与现状一致，空 body 普通保存仍 200）。

注意保持现有真值判断语义：`body.get("backend")` 为空（"" / None）时跳过，不写 kwargs。

### 2. 同区域 `skill` 字段防御（~L1601）

`elif body.get("skill") is not None:` 旧方式兼容分支——`body["skill"]` 非 str（list/dict/int）→ 400。字符串路径（含空串清空技能）行为不变。

（`skill_id` / `runtime_id` 已有各自的处理路径：`int()` 转换带 try/except 兜底，畸形值静默归 0/不写，不触发 500——核对无需改动。）

### 3. tests/test_chat_config_backend_validation.py（新增）

复用 tests/test_chat_config_im_preserve.py 的 fixture 模式（TestClient + dependency_overrides + 真实 sqlite 种子行）：

1. `test_backend_object_rejected_400`：`{"backend": {"type": "opencode"}}` → 400 + detail 提示；
2. `test_backend_string_ok_200`：`{"backend": "opencode"}` → 200（回归）；
3. `test_backend_malformed_all_400`：list / int / 空对象 / `{"type":123}` / float / bool → 4xx；
4. `test_backend_malformed_not_saved`：畸形 400 后配置不落库（GET 仍为原值）；
5. `test_skill_non_string_rejected_400`：`{"skill": ["codereview"]}` → 400；
6. `test_skill_string_path_unchanged`：`{"skill": "codereview", "skill_scope": "official"}` → 200（回归）；
7. `test_empty_body_still_200`：空 body / `{"backend": ""}` → 200 不写 backend。

## 测试 seam 声明

公开 HTTP seam：`PUT /api/chat/config`（TestClient 直打，不 mock 内部函数）——与 #431/#504 测试同 seam 同模式。

## TDD 提交拆分

1. `test(#508)`：先写失败测试（对象形态当前 500 → 断言 400，红）+ spec/plan/task 文档；
2. `feat(#508)`：路由层 backend 显式校验 + skill 防御，最小实现使其通过（绿）。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 旧前端仍传字符串 backend | 字符串路径不变，既有 #431 用例钉住 |
| 与 #504 归一语义分歧 | 见 spec §5：两 Issue 独立验收，本 Issue 显式 400 为权威口径，PR body 注明 |
| skill 防御误伤 | 仅类型拒绝，字符串含空串路径零变化 |

## 任务拆解（Task）

| # | 任务 | 状态 |
|---|------|------|
| 1 | 失败测试（对象形态 400 断言 + 全分支 + 回归） | 待做 → 提交 test(#508) |
| 2 | chat.py backend 显式校验 + skill 防御 | 待做 → 提交 feat(#508) |
| 3 | 回归：新测试全绿 + chat config 相关既有测试全绿 | 待做 |
| 4 | 自审查 5 轴 + pre-pr 增量检查 | 待做 |
| 5 | 建 PR（base=weekly-09）→ Issue 评论 → A2A QA → tasks complete | 待做 |
