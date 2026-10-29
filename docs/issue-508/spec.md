# Issue #508 需求文档（Spec）

> 目标分支：weekly-09 ｜ 依据：Issue #508 原文 + issue-agent Spec 评论（Issue 评论）。已核对 #504（同根因独立 Issue）在 feat/issue-504 分支的修法，两者改动同构、语义互补，无冲突。

## 1. 问题

`PUT /api/chat/config` 传入非字符串 `backend`（对象/数组/数字等）返回 **500**，应为 4xx（400）。

- 复现 body：`{"backend": {"type": "opencode"}}` → 500（`{"code":500,"message":"服务器内部错误..."}`）。
- 根因链（已在目标分支代码核验）：
  1. GET /config 返回 `backend` 为**对象形态** `{"type": cfg.get("backend","")}`（chat.py:1455-1458）；
  2. PUT `update_chat_config`（chat.py:1581-1582）`kwargs["backend"] = body["backend"]` **无类型校验直传**；
  3. `set_chat_config`（session_manager.py:1113）→ `_resolve_chat_runtime_id(data.get("backend") or "")`（str 形参）；
  4. `"-" not in backend`（session_manager.py:1072）等对 dict/int/list 抛 TypeError → 500。

## 2. 需求（In Scope）

- `update_chat_config` 入口对 `backend` 归一/校验：**字符串直传**；`{"type": "<str>"}`（GET 返回形态）**归一为字符串**；其余非字符串（数组/数字/空对象/`{"type":123}` 等）**raise HTTPException(400)** 并附明确提示。
- 同根因区域其它直传 kwargs 字段（`skill`）一致防御：非字符串拒绝 400，不落库（对齐 #504 修法：归一化 + 类型防御一并覆盖）。
- 新增测试覆盖各分支与回归路径。

## 3. Out of Scope

- GET /api/chat/config 返回结构变更（保持现状，仅 PUT 侧防御）。
- 前端调用方适配改造。
- #504 的独立验收（同根因独立 Issue，各自回归保障）。

## 4. 验收标准

- [ ] `{"backend":{"type":"opencode"}}` → **400** + 明确提示（本 Issue 立场：拒绝，见 §5）
- [ ] `{"backend":"opencode"}` → 200 不回归
- [ ] `{"backend":["opencode"]}`、`{"backend":123}`、`{"backend":{}}`、`{"backend":{"type":123}}` → 4xx
- [ ] 畸形输入不落库、不破坏既有配置
- [ ] `skill` 非字符串 → 400
- [ ] 所有现有测试通过；新增测试覆盖全部分支

## 5. 关键立场说明（与 #504 的分歧点）

#504（普通保存 500 修复）将 `{"type":"opencode"}` 归一为 `"opencode"` 后落库（200）。**本 Issue Spec 明确**：`{"backend":{"type":"opencode"}}` → **400 拒绝**（Issue 标题即"非字符串返回 4xx"）。两个 Issue 验收口径不同——本分支按本 Issue Spec 实现：**仅纯字符串接受**，对象形态一律 400。

> 备注：#504 与 #508 是同根因的两个独立 Issue，各自分支独立实现、独立验收；若两 PR 先后合入 weekly-09，以后合入者为准（本 Issue 显式 400 拒绝更严格，语义收窄安全）。

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 拒绝 `{"type":...}` 影响依赖 #504 归一行为的调用方 | 两 Issue 独立验收；本 Issue Spec 为权威口径，PR body 注明分歧 |
| skill 防御误伤合法调用 | 仅类型校验不改语义；字符串路径零变化，既有测试钉住 |
