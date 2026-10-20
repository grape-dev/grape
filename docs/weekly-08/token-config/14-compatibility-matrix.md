# 向前兼容矩阵 + 双向状态机

> 本文档从主线需求文档 R9.6-R9.8 和设计文档 R22.8-R22.9 提取。
> 基线校准后补充，解决"R9 声称完全不变但实际有行为变更"的问题。

## 需求项

### R9.6 显式兼容矩阵（F1/F2/F3 勘误）

R9.1 原声明"已有全局 Bot Token 的环境完全不变"过强。显式矩阵：

| 存量场景 | 基线行为 | 分支行为 | 变化类型 | 接受理由 |
|---------|---------|---------|---------|---------|
| 路径 A token 获取 | 前三级命中 bot token | 完全一致（host_user_id=0 守卫） | 无变化 | — |
| 路径 A webhook 验证 | 读 gitcode_webhook_token | webhook_secret 空→fallback 原字段 | 无变化 | — |
| 路径 A 自循环检测 | bot 用户名检测 | 标记优先→bot 用户名兼容 | 增加防线 | 不破坏原行为 |
| **F1: OAuth token 读取** | WHERE status=1 | WHERE status IN (1,4) | **全局变更** | status=4 过期 token 供 best-effort，语义更优但未披露 |
| **F3: deps.py 两处分裂** | 两处都 status=1 | :285 保持 status=1；:329 改 IN (1,4) | **同文件分裂** | 应统一或显式说明差异 |
| **F2: A2A trust domain** | bot token 相同才信任 | 新增 host_user_id 相同 | 新增路径 | **已确认安全**：`if _session_host > 0` 守卫排除双 0 |
| D1: session token 残留 | 遮蔽 bot token（同语义） | 遮蔽用户 token（语义不同） | **隐性不兼容** | R22.2 修复：DevopsAgentSessionToken._fetch 加 host_user_id 守卫（路径 B 跳过 session token，与 RepoBotToken/GlobalBotToken 对齐）。**R23.1 补充修复**：_is_user_token_repo 独立函数也加同一守卫（R22.2 修复时遗漏，路径 B + session 残留 → _is_user_token_repo 误返回 False → task token 来源误判） |
| D2: add_managed_repo 默认值 | 无新参数 | 新参数默认 0/空，5 轮审计修 bug | **文档过时** | R9.4 应补充"默认值在 5 个场景下曾被判错，已修复" |

### R9.7 双向状态机矩阵（D3 修复）

`host_user_id × bot_gitcode_token × webhook_secret × webhook_id` 四字段状态机：

| 迁移路径 | host_user_id | bot_gitcode_token | webhook_secret | webhook_id | 触发条件 |
|---------|-------------|-------------------|----------------|------------|---------|
| 初始托管 A | 0 | =全局 bot | '' | 新建 | 有全局 bot token |
| 初始托管 B | =用户 ID | '' | =随机密钥 | 新建 | 无全局 bot token |
| B→A 升级 | 0（清） | =全局 bot（写） | ''（清） | 重建 | 管理员配全局 bot token |
| A→B 降级 | =用户 ID（写） | ''（清） | =随机密钥（写） | 重建 | 全局 bot token 失效 |
| A→A 换 bot | 0（不变） | =新 bot（更新） | ''（不变） | 重建 | 管理员换 bot token |
| A→A 配 repo 级 | 0（不变） | =repo 级（更新） | ''（不变） | 重建 | 用户配仓库级 token |
| A→A 清 repo 级 | 0（不变） | ''（清） | ''（不变） | 不变 | 用户清除仓库级 token |
| B→B 换用户 | =新 ID（更新） | ''（不变） | =新密钥 | 重建 | 用户重新托管 |
| 解绑 | 保留 | ''（不变） | ''（不变） | 不变 | 用户解绑 VCS |
| 删除仓库 | — | — | — | — | 全部清除 |

非法终态（必须避免）：
- host_user_id > 0 且 bot_gitcode_token 非空 → 路径 B 有 bot token（异常）
- host_user_id == 0 且 bot_gitcode_token 空 且 webhook_secret 空 → 孤儿状态
- webhook_id 存在但 password 不匹配 → 验证断裂

### R9.8 支线文档兼容声明（D4 补充）

支线（R15-R21）向前兼容分析不足：
- R16.4.5 只覆盖短路兼容，未分析 Repo.bot_token 返回空串 vs 基线返回过期 token 的消费方差异
- R20.7 对 29 场景只标"将改为"，未分析返回值语义影响
- 支线实现时应补充"基线行为 vs 改造后行为 vs 兼容性分析"三元组

## 设计方案

### F1 修复：status IN (1,4) 显式声明

_get_oauth_token_from_identity 从 status=1 改为 status IN (1,4) 是设计意图：
- status=4 过期 token 供 best-effort（fail_task 发评论）
- 基线上 status=4 拿不到 token → 无法发评论 → 用户无提示
- 分支上能拿到过期 token → 尝试发评论 → 401 → COMMENT_POST_FAILED → dashboard 通知
- 语义更优，但 R9.1 未披露 → R9.6 显式矩阵补披露

### F3 修复：deps.py 两处统一

deps.py:285（GitCode 专用，OAuth 登录）保持 status=1，deps.py:329（通用，token 读取）改为 status IN (1,4)。
建议统一为 status IN (1,4)——OAuth 登录遇到 status=4 时引导重新绑定，不静默返回空。

### D3 修复：状态机 + 非法终态检测

在 add_managed_repo / update_managed_repo_bot_token / auto_upgrade_path_b_repos / repair_host 四个函数中，迁移完成后用非法终态检测做 assert/warning，防止 14 轮审计修的 bug 再次回归。

## 涉及代码文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `src/api/deps.py` _get_oauth_token_from_identity | F1/F3 统一 status 过滤 | :285 和 :329 统一为 status IN (1,4) |
| `src/api/routes/webhook.py` _authorize_a2a | F2 已确认安全 | `if _session_host > 0` 守卫已存在 |
| `src/user/db.py` add_managed_repo | D3 非法终态检测 | 迁移完成后 assert |
| `src/user/db.py` update_managed_repo_bot_token | D3 非法终态检测 | 迁移完成后 assert |
| `src/user/db.py` auto_upgrade_path_b_repos | D3 非法终态检测 | 迁移完成后 assert |
| `src/api/routes/repos.py` repair_host | D3 非法终态检测 | 迁移完成后 assert |

## 验收标准

- [x] F1: _get_oauth_token_from_identity status IN (1,4) 行为在文档中显式声明
- [x] F3: deps.py 两处查询统一或差异显式说明
- [x] F2: A2A trust domain `if _session_host > 0` 守卫在文档中确认
- [ ] D3: 四字段状态机矩阵覆盖所有 10 条迁移路径
- [ ] D3: 3 种非法终态在四个函数中有 assert/warning
- [ ] R9.1 原声明"完全不变"改写为显式兼容矩阵

## 依赖场景

- 依赖 02-expiry-framework（R16 status=4 标记机制）
- 依赖 01-repo-token-config（R15 仓库级 token 配置迁移路径）
- 依赖 10-global-token-swap（R21.1 全局 token 更换迁移路径）

## 上线策略结论（R22.10，灰度方案作废）

**前提**：路径 B 只在"无全局 bot token"时触发。存量环境全局 bot token 已配置 → host_user_id 恒 0 → 全走原逻辑。路径 B 受众是新部署，无存量需保护。回滚 = 配全局 bot token → auto_upgrade 切回路径 A。

**结论：灰度/影子期/白名单作废。必要操作只有三件**：

1. **修 F3**：deps.py:285 和 :329 统一为 status IN (1,4)，几行改动
2. **确认 R10 守卫**：_is_user_token_repo 已覆盖所有 401 入口，路径 A 401 不触发 R10
3. **发布说明披露**：status IN (1,4) / GitHub 401→400 / GitCode 401→400，零代码

**不需要**：灰度/影子期/白名单、回滚开关、feature flag

### R22.10 执行记录

- [x] **F3 修复完成**：deps.py:285 `_get_gitcode_credential_by_gitcode_id` 从 `status=1` 改为 `status IN (1, 4)`，与 :329 `_get_oauth_token_from_identity` 统一。新增测试 `test_cred_matched_by_gitcode_id_when_token_expired` 验证 status=4 时凭据错位兜底仍能命中。
- [x] **R10 守卫确认完成**：`mark_identity_expired` 唯一调用点在 service.py:2558，前面有完整守卫链（exit_code < 0 + 401 关键词匹配 → `_is_user_token_repo` / `_is_github_user_token_repo` 路径 D 判定 → `_hid > 0`）。路径 A 仓库（有 bot token）守卫返回 False → 不触发标记。`external_identities SET status=4` 唯一入口是 `mark_identity_expired`。401 关键词列表覆盖 task 执行链实际 401 文案（git CLI "authentication failed" + API "401 unauthorized"）。
- [x] **F2 守卫确认完成**：webhook.py:327 `if _session_host > 0 and _session_host == _target_host` 守卫已存在，排除双 0 误信任。
- [ ] **发布说明披露**：待发布时补充。
