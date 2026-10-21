# Spec: Issue #400 — chat agent 引擎选择显示异常（单选语义 + 实际引擎可感知）

> 级别：bug 修复 | 复杂度：medium | 基线：功能分支 | 2026-10-21

## 1. Objective

接入个人机器（个人级引擎）后，chat agent 引擎选择出现"多个引擎同时选中"的显示，
且用户无法感知会话实际使用的引擎。本次修复目标：

- 引擎选择恢复**单选语义**：UI 中同一时刻只有一个引擎处于选中态；
- 会话**实际使用引擎可感知**：选中态/顶栏明确标识当前会话实际绑定并执行的引擎；
- **显示与实际执行一致**：端点返回的 selectedEngine 与会话引擎指针（runtime_id）一致，
  含「本机直连 + 个人级引擎」并存的场景。

## 2. 根因

1. 前端选中态推断不可靠：`src/api/static/js/pages/chat.js` 的 `_buildModelPicker` 用
   `b.name === curType || b.name.startsWith(curType + '-') || curType === b.name.toLowerCase()`
   判断选中。本机直连项 `name=codex` 与个人引擎实例项 `name=codex-<device>` 都满足
   `startsWith(curType + '-')` → 两个项同时打勾（多选显示）。
2. 单选中数据源缺失：`GET /api/chat/config` 只返回 `backend.type`（引擎**类型名**，
   由 `_chat_config_row_engine` 取 `agent_runtimes.name`），不返回会话引擎指针
   `runtime_id` 对应的唯一引擎。前端无法区分"类型同名"与"实际实例"，只能靠名称前缀猜。
3. 候选未做唯一键归一：用户接入个人机器可能产生同名/同实例多行（历史重复行、
   user/repo 双档），列表出现重复项，进一步放大误判。
4. 会话创建时引擎指针同步缺失：`devops_agent_session`（chat 会话）创建时未采用
   chat 配置指针 `chat_agent_session.runtime_id`，与 `guard_issue_session_pointer`
   的"指针 = 实际执行引擎"语义不对齐。

## 3. 方案（三层修复）

### 层 1：候选唯一键归一（src/runtime/engine_select.py）
- 新增 `engine_unique_key(m)`：以引擎身份派生归一唯一键
  - 本机直连（credential_id=0）：`local:{engine_type}`
  - 个人/远端：`{scope}:{instance_name}`（instance_name 由 name+device+credential_id 派生，
    与 `src/runtime/instances.py::instance_name` 同源；canonical account identity 对齐 <canonical_account_id>，
    以引擎身份而非 owner 字符串做键）
- 新增 `dedupe_engines(members)`：按唯一键去重（保留首个/权威行），供候选过滤复用；
- 在 `_apply_filters` / `resolve_local_engine` / `get_scene_pool_engine_ids` 等候选收口处应用，
  保证同一候选身份只出现一次（单选约束）。

### 层 2：接口返回唯一 selectedEngine（src/api/routes/chat.py）
- `GET /api/chat/config`：
  - `_backends` 构建后按唯一键去重（合并同实例重复行，个人机器 user/repo 双档合并为单条）；
  - 新增 `selectedEngine` 字段：以 `cfg.runtime_id`（会话引擎指针）为准解析唯一引擎
    （`{runtime_id, name, display_name, scope, owner}`）；指针缺失/失效时按 backend 名精确匹配
    候选，仍返回单一对象或 null，绝不返回数组。
- 前端选中态以 `selectedEngine.runtime_id` 为权威（回退：无指针时精确 name 匹配，删除 startsWith 前缀匹配）。

### 层 3：会话引擎指针一致性（src/api/session_store.py）
- `get_or_create_agent_session` 增加可选 `runtime_id` 参数：chat 会话创建时把
  `chat_agent_session.runtime_id`（用户配置指针）传给该参数，落 `devops_agent_session.runtime_id`，
  使会话指针 == 实际执行引擎（与 `guard_issue_session_pointer` 语义一致）；
- chat.py 三处 `get_or_create_agent_session(f"chat:{user['id']}", "chat", backend_name)` 调用
  同步传入运行时解析出的 runtime_id（未显式传入时保持既有行为，不影响其他调用方）。

### 前端（src/api/static/js/pages/chat.js）
- 引擎选择器渲染：单选语义（唯一 active 项），选中依据 `selectedEngine.runtime_id` 精确匹配，
  删除 `startsWith` 宽松匹配；
- 顶栏/选择器回显实际引擎名：`chat-hdr` 增加引擎徽标 `#chatEngineBadge`，
  `loadChatConfigData` / `_saveModelConfig` 后刷新显示实际引擎 display_name；
- 切换引擎后重新拉取 /config（权威 selectedEngine），避免本地推断与后端指针分叉。

## 4. Acceptance Criteria

- [x] 接入个人机器后，chat agent 引擎选择不出现"多个引擎同时选中"（同一时刻唯一 active）
- [x] UI 可明确看到当前会话实际使用的引擎（唯一选中态 + 顶栏引擎徽标）
- [x] 显示与实际执行引擎一致（selectedEngine.runtime_id == 会话执行 runtime_id，
      含本机直连 + 个人级并存场景）
- [x] 现有 chat / engine_select / session_store 测试全过
- [x] 新增测试覆盖：候选去重（user/repo 双档）、selectedEngine 唯一性、指针一致性、前端单选渲染

## 5. Scope

In：chat.py / engine_select.py / session_store.py / chat.js（+chat.css 徽标样式）。
Out：#249 / #253 / 引擎健康探活 / 远端引擎生命周期。

## 6. Risks

- 会话指针改动影响既有会话兼容 → 仅新增可选参数、默认行为不变，旧 COLR 回归测试兜底；
- 前端单选改造交互习惯 → 保持 iOS 风格，选中态唯一 + 切换即时刷新。