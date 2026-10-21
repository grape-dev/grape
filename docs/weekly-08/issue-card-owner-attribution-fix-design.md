# 方案：需求卡 user_id（归属）以「创建身份」为准，修复空 assignee 反查命中默认账号 [weekly-08]

> 状态：**待实施**｜目标分支：weekly-08（部署分支 weekly-08-lyz）
> **敏感信息声明**：本文不含真实 IP、域名、账号、用户 ID、仓库名或 token（一律 `<xxx>` 占位）。
> 现场：环境 2（`<env_host>`）一个 GitHub 仓库 `<owner>/<repo>` 的首个需求（GitHub Issue `#<issue_num>`，
> 内部卡 `issue.id=<card_id>`）**只在仓库页可见，需求中心「我的需求」不可见**。
> 直接原因：卡 `user_id=<default_acc_id>`（默认承载账号），而创建者登录身份为 `<creator_acc_id>`；
> 「我的需求」按 `user_id=user.id` 过滤。

---

## 一、问题现象

- 托管 GitHub 仓库后，用户（登录 `grape_accounts.id=<creator_acc_id>` = 平台用户名 `<vcs_login>`）发起首个需求；
- 卡 `issue.id=<card_id>`：`user_id=<default_acc_id>`、`assignee='<vcs_login>'`、`org_id=<org_id>`，
  stage=vod、status=processing（处理正常）；
- 同需求任务 `agent_tasks.user_id=<creator_acc_id>`（**对的**）——卡与任务归属割裂；
- 需求中心「我的需求」= `list_mine` 带 `user_id=user.id` 过滤 → 卡(default) 不出；仓库页 `list_repo_issues`
  不过滤 → 只在此可见。

## 二、根因（代码出处 + 实测）

### 2.1 卡归属不是「创建身份」，而是「assignee 字符串反查」

`ensure_issue_task`（`src/api/issues_center.py`）签名**没有 user_id**，卡归属由
`assignee` 字符串经 `_get_user_id_by_gitcode(target)` 反推；复制/新建（`create_task_via_issue`）在调用 ensure 时
**只传 assignee 字符串、丢弃登录 `user["id"]`**。字符串一为空/解析错，归属就歪。

### 2.2 空 assignee 的补卡路径会命中「任意空登录用户」

`session_store._sync_issue_fields(ensure_row=True)` 补卡 INSERT（`src/api/session_store.py`）：

```sql
SELECT COALESCE((SELECT m.user_id FROM unified_user_mapping m
                 WHERE (m.gitcode_user = ? OR m.github_login = ?) AND m.user_id > 0 LIMIT 1), 0), ...
```
- assignee 为空 → 条件退化为 `(gitcode_user='' OR github_login='')`，命中所有空登录名行；
- **无 ORDER BY**，SQLite `LIMIT 1` 任意取一行 → 环境实测返回任意空登录账号的 id
  （本环境命中 `<default_acc_id>=<default_account>`）。

### 2.3 复用分支不回补「非 0 但错误」的归属

`ensure_issue_task` 复用已有卡时仅 `if not _existing_user_id:` 回补（`src/api/issues_center.py`）；
`user_id=<default_acc_id>` 这种「非 0 错误值」不会被纠正 → 归属固化。

### 2.4 实证（环境 2）

| 查询 | 结果 |
|---|---|
| `SELECT user_id FROM unified_user_mapping WHERE (gitcode_user='' OR github_login='') AND user_id>0 LIMIT 1` | `<default_acc_id>` |
| `_get_user_id_by_gitcode('<vcs_login>')`（带 grape_accounts EXISTS 守卫） | `<creator_acc_id>` |
| 卡 `issue.id=<card_id>.user_id` | `<default_acc_id>` |
| 同需求 `agent_tasks.user_id` | `<creator_acc_id>` |

## 三、方案设计

### 3.1 核心：建档归属以「创建身份」为准（给 ensure 传 user_id）

- `ensure_issue_task` 新增可选参数 **`user_id: int = 0`**：
  - `user_id>0` → 权威归属（grape 新建等有登录上下文的路径使用），assignee 字符串仅作展示/引用；
  - `user_id<=0` → 维持现状：assignee → sender → owner 反查（webhook 无登录场景）。
- `create_task_via_issue`（对外统一创建入口）调用 ensure 时**显式传登录 `user["id"]`**；复制/其它「有 user」入口同步传递。

### 3.2 Webhook/无登录场景兜底

- 无显式 user_id：`assignee/sender → _get_user_id_by_gitcode`（带账号守卫）；映射不到 → `user_id=0`
  （任务中心 mine 已有 `user_id=? OR user_id=0` 兜底展示语义，行为不变）。

### 3.3 修 session_store 补卡子查询（防空 assignee 命中任意用户）

- assignee 为空 → **不反查，直接 `user_id=0`**；
- assignee 非空 → 收紧映射：仅 `gitcode_user=? OR github_login=?` 且值非空、`ORDER BY user_id`、
  加 `EXISTS grape_accounts` 守卫（与 `_get_user_id_by_gitcode` 对齐）；
- 补卡后被 `set_issue_stage` 的 UPDATE 只改 stage 等，不改 user_id（保持）。

### 3.4 ensure 复用分支归属校准（非 0 错误值也修）

- 复用已有卡时：显式传入/解析出的归属 `_uid` 与 `_existing_user_id` 不一致，且
  `_existing_user_id` 为 `0` **或等于默认/承载账号 id** 时，**校准为 `_uid`**；
- `assignee` 展示同步更新（保留）。

### 3.5 现状数据校准

- `UPDATE issue SET user_id=<creator_acc_id> WHERE id=<card_id>`（附 org 校验），使「我的需求」立即可见。

## 四、各模块改动

| 模块 | 改动 |
|------|------|
| `src/api/issues_center.py` | `ensure_issue_task` 加 `user_id` 参数 + 优先级（显式 > 反查 > 0）；复用分支归属校准；`create_task_via_issue` 显式传 `user["id"]` |
| `src/api/session_store.py` | `_sync_issue_fields` ensure_row：空 assignee→0；非空收窄（非空匹配 + ORDER BY + EXISTS 守卫） |
| `src/api/routes/tasks.py` | `webhook_ensure_task` 同步 `user_id` 参数（同 issues_center 逻辑） |
| `tests/` | 见 §六 |
| （数据） | 现存错误卡 `user_id` 校准（运维执行） |

## 五、影响面 / 风险

| 项 | 说明 |
|---|---|
| 新建/复制需求 | user_id 变为显式创建人（更正确）；webhook 回声/重复建档幂等不受影响 |
| webhook 导入 | assignee/sender 反查兜底保留；失效/空照 :=0（`mine` 0 兜底展示，无回归） |
| 既有正确卡 | 仅「user_id 非0 且等于承载账号」或 0 且解析不一致时才校准，正常卡不动 |
| org | 归属仍以仓库托管 org 为准（ensure 内逻辑不变） |

## 六、测试（测试先行，TDD）

| ID | 场景 | 断言 |
|---|---|---|
| QA-01 | `create_task_via_issue` 以 user.id=<creator> 创建（任何 provider） | 卡 `issue.user_id==<creator>`（不再依赖字符串映射） |
| QA-02 | 卡复用：已有卡 user_id=<default>，再次 ensure（解析=创建人） | 卡被校准为 `user_id=<creator>` |
| QA-03 | 卡 user_id=0，再次 ensure（解析=创建人） | 校准为创建人（原逻辑保留） |
| QA-04 | webhook issue_opened（sender 可映射） | 卡 user_id=映射 id |
| QA-05 | webhook，sender 映射不到 | 卡 user_id=0（mine 0 兜底展示，不起默认账号） |
| QA-06 | `_sync_issue_fields` ensure_row，assignee='' | 补卡 user_id=0，**不得命中空登录用户** |
| QA-07 | `_sync_issue_fields` ensure_row，assignee=<vcs_login> | 补卡 user_id=映射 id（非空+守卫生效） |
| QA-08 | 回归：mine 按 user 过滤、仓库页列表不过滤 | 行为不回退 |

后端测试落地 `tests/test_issue_owner_attribution.py`（patch
`src.api.issues_center._get_user_id_by_gitcode`/`session_store` 走真实映射）。

## 七、提交约定

- `fix(issues): issue card ownership uses creator user_id (not assignee string remap)`
- 前端无改动；bump 如涉及仍按既有 `?v=` 约定。
- docs：`docs/weekly-08/issue-card-owner-attribution-fix-design.md`

## 附：历史与分支影响

- 空 assignee 补卡子查询由历史提交 `4bc7cf536`（曾修复「归属错存为 0 → mine 看不到」）引入，**main、
  weekly-07-clean/lyz、weekly-08、weekly-08-lyz 全部存在**；
- `ensure_issue_task` 无 `user_id` 参数为该函数诞生起的设计取向；
- 修复需同步到上述分支。