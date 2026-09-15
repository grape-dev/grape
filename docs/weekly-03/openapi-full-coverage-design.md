# WebAPI 全量进 OpenAPI 契约设计方案（CLI 可访问）

> 目标：将当前「不进 openapi.json、CLI 不支持」的 webapi（`get_current_user` 会话认证端点）**全部纳入对外契约**，
> 使 CLI（基于契约生成命令）可访问这些用户管理类接口。
> 依据 `docs/weekly-03/token-scope-classification-design.md`（已实施）：skills 等管理端点归 D 类
> （**user/JWT 可访问、session 不可**），且 token 均有 user_id 可定位调用者。
> 分支：`weekly-03-lyz`；状态：**实施中（A 批已完成并上线）**；日期：2026-09-15

---

## 一、现状

### 1.1 为什么部分接口不在 openapi.json

`src/api/openapi.py` 的契约收录规则（`route_is_public`）：

> 依赖链中**含** `get_current_user_or_api_key` 或 `get_user_or_none` 的路由 → 收录；
> **含 `get_current_user`（Web 会话/JWT）** 的管理/业务路由 → **不进契约**。

初衷（`issue-45-openapi-api-design.md`）：避免内部管理端点经 `/openapi.json`/`/docs` 暴露。

### 1.2 当前被排除的端点数

`get_current_user` 端点共 **162 处**，分布在 18 个路由文件：

| 文件 | 数 | 文件 | 数 | 文件 | 数 |
|------|----|------|----|------|----|
| settings.py | 34 | scenes.py | 24 | runtime.py | 17 |
| chat.py | 12 | events.py | 11 | repos.py | 12 |
| knowledge.py | 10 | devstage.py | 8 | skills.py | 11 |
| tasks.py | 6 | templates.py | 4 | agents.py | 2 |
| feishu_onboard.py | 3 | task_logs.py | 3 | dashboard/pipeline/search/auth | 各 1-2 |

### 1.3 性质分类（抽查结论）

这些端点**全是「用户自身账户/平台管理或操作」**，非内部协议：

| 类 | 例 |
|----|-----|
| 用户凭据/配置管理 | settings 凭据/API Key/通知/OAuth/runtimes/模式/引擎标签 |
| 场景管理 | scenes CRUD / 市场 / 应用镜像 / 体验 |
| 运行时管理 | runtime 任务/会话/凭据 crud / 恢复 / 健康 |
| 技能管理 | upload/delete/like/convert/sub |
| 仓库平台 | repos bot-token / deploy-url / host / project-info |
| 对话/事件操作 | chat 会话 CRUD / events 终止/重派 |
| 知识管理 | knowledge CRUD / 归档 |

> 例外确认：webhook 回调（`webhook.py`）与 runtime 注册/心跳（`/_pub` 或 bot 守卫）**不用 get_current_user**，不受影响。

---

## 二、可行性：token 均能取 user_id

依据 token-scope 设计（已实施），三种对外 token 均携带用户身份可定位调用者：

| 认证 | auth_type | 可定位 userId | 适用 |
|------|-----------|---------------|------|
| JWT（Web 登录） | jwt | `user["id"]` | ✅ 用户管理操作 |
| API Key（`grape_`） | api_key | `user["id"]`（api_tokens 绑定用户） | ✅ CLI 用户操作 |
| session_token（`tk_`） | session_token | `user["id"]=user_id`（可能 0=仓库身份） | ❌ **不应用于账户管理**（非用户本人） |

**故 skills 等 D 类管理端点的 owner 可从 token 取**：`user["id"]` 即归属者——无需额外 owner_id 入参，
**「token 与 owner 不一致」在服务端派生下不存在**（owner = 调用 token 的用户）。

---

## 三、方案：`get_current_user` → `require_no_session_token`

### 3.1 转换方式

将 162 处端点认证依赖 `Depends(get_current_user)` 统一替换为
`Depends(require_no_session_token)`（`deps.py` 已实现）：

- `require_no_session_token` 内部 `Depends(get_current_user_or_api_key)` → **进 `route_is_public` 依赖链**，收录进契约 ✅
- 行为：**JWT / API Key 放行**（用户本人可操作），**session_token → 403** ✅（符合 D 类）
- user_id 由 token 派生，作为 owner/归属——无需新入参

### 3.2 `_PUBLIC_AUTH_DEPS` 是否需加

`require_no_session_token` 依赖链展开含 `get_current_user_or_api_key`，`route_is_public` 已识别——
**无需改 openapi.py**（skills list/detail 已进契约佐证）。仅当后续引入新守卫函数时需同步该集合。

### 3.3 表面改动

| 项 | 改动 |
|----|------|
| 各路由 import | 加 `require_no_session_token`（若未引） |
| 端点 Depends | `get_current_user` → `require_no_session_token` |
| 契约 | `/openapi.json` 自动增补对应 paths（重启/触发 custom_openapi） |
| `EXPECTED_PUBLIC` | `test_openapi.py` 相应补录（契约测试） |

### 3.4 分批落地（风险隔离）

| 批 | 文件 | 说明 | 状态 |
|----|------|------|:---:|
| A | skills.py（11）、scenes.py（24） | 用户技能/场景管理，owner=user | ✅ 已完成（commit `954517ad`） |
| B | settings.py（34）、repos.py（12）、agents.py（2）、knowledge.py（10） | 账号配置/仓库/知识 | 待做 |
| C | runtime.py（17）、tasks.py（6）、events.py（11）、chat.py（12） | 运维/任务操作 | 待做 |
| D | devstage/templates/dashboard/pipeline/search/auth/feishu_onboard/task_logs | 其余 | 待做 |

> 每批独立提交 + 回归；先 A 批验证契约与 owner 派生，再扩 B/C/D。

### A 批实施记录（2026-09-15，已完成并上线）
- `skills.py` 11 处 + `scenes.py` 24 处 `get_current_user` → `require_no_session_token`
- 契约：skills 12 端点 + scenes 17 端点全进 `/openapi.json`（upload/like/convert/CRUD/市场等），CLI 解锁
- `test_openapi`：`EXPECTED_PUBLIC` 补录 34 端点，`NOT_IN_CONTRACT` 移除已开放的 `with-pipeline`/`scene_id`/`skills-upload`
- 验证：A 批守卫端到端 session→403、API Key/JWT 通过（200/404 非 403）；回归 98 passed
- **A 批迁移后 `get_current_user` 残留定义**：如确认某端点需保持 Web-only（内部），可保留 get_current_user 并加注释

---

## 四、风险与安全

| 风险 | 影响 | 缓解 |
|------|------|------|
| 端到端暴露流程/凭据 | `/openapi.json` 列出管理端点，敏感路径公开 | 不需要缓解——契约只标明端点与参数，不泄露 token/密钥；认证仍由 token 守卫保证 |
| session_token 误用管理端点 | 若未全部替换会留失控管理 | `require_no_session_token` 统一禁 session；批量替换绝不遗漏（grep 校验） |
| **owner 越权** | 替换后 owner=token.user_id，无越权面 | 由服务端派生 owner；`check_repo_access`/owner 校验保留 |
| CLI 生成大命令集 | 契约增大 → CLI 命令爆炸 | 契约按 tag 分组；CLI 按需注册 |
| **API Key 可执行管理操作** | API Key 泄露 → 他人可管理 | 属设计意图（用户本人 API Key）；凭据强度/吊销走既有 settings |

> 注意：**JWT 端点在 token-scope D 类本应禁 session 但放行 user**——本次替换完全对齐该语义，不改变 JWT/API Key 行为，
> 仅把「当前只能 Web 会话用」扩展为「用户 API Key/JWT 也能用」。

---

## 五、验证

1. `pytest tests/test_openapi.py`：新增契约端点全绿（EXPECTED_PUBLIC 补录）。
2. `grep 'Depends(get_current_user)' src/api/routes/*.py` 归零（或仅剩明确需 Web-only 的例外）。
3. 抽样端点三认证冒烟：JWT 200 / API Key 200 / session_token 403（D 类语义）。
4. 前端（Web JWT）回归：凭据/场景/技能/运行时段页正常。
5. CLI 生成后 `grape skills ...` 可执行。
6. 全量 pytest。

---

## 六、影响与遗留

| 项 | 影响 |
|----|------|
| 代码 | 162 处依赖替换（`require_no_session_token`） |
| 契约 | openapi.json paths 增补；`test_openapi.EXPECTED_PUBLIC` 补录 |
| CLI | 解锁 skills/scenes/settings/runtime 等命令 |
| 遗留 | 少量「Web-only」例外（若有内部管理端点明确不对外，保留 get_current_user）需逐批确认 |

> **附**：`owner_id` 问题在本方案下自然解决——管理端点 owner 恒等于调用 token 的 user_id，客户端无需传 owner_id。