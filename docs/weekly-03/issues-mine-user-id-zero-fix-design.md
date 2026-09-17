# 「我的需求」看不到自己创建的需求（issue.user_id 错存为 0）修复方案 v3

> 目标：修复「需求 ID #193（本人创建）在『我的需求』不可见」。
> **方向（评审确认）**：根因是**归属字段数据错误**（`issue.user_id` 应=创建人=7，实为 0），**聚焦修数据 + 修建档路径**，
> **不做查询兜底**（`mine OR user_id=0` 会把 user_id=0 需求误展示给所有用户，属查错，已否决）。
> **根因已实测确认**（2026-09-17，生产库核对）：非「映射当时不存在」，而是 `session_store._sync_issue_fields(ensure_row=True)`
> **最小建档硬编码 `user_id=0`**，先于 `ensure_issue_task` 执行后固化。
> 状态：**设计（待实施）**；分支：`weekly-03-lyz`

---

## 一、现象与实测证据

- 生产「需求 ID #193」= `issue.id=193`：`gitcode:Jason7602/hello-fastapi-13` #4，`user_id=0`，`assignee=Jason7602`，
  `created_by=''`，`status=processing`，`stage=vod`，`scene_id=cloud-native-cli`，`created_at=2026-09-17 02:22:16`。
- **映射时间线（排除旧假设）**：`unified_user_mapping('Jason7602')` 的 `updated_at = 2026-09-15 19:31`，
  **早于** #193 创建（09-17 02:22）约 1.5 天 → 「当时没映射」**不成立**。
- **存量规模（生产实测）**：`user_id=0 且 assignee 可映射到 user_id>0` 共 **6 行**：
  `id ∈ {184, 188, 190, 191, 193, 194}`（全为 Jason7602，且 193/194 同仓 `hello-fastapi-13` #4/#5——模板仓库+场景体验路径同源）。
  `id ∈ {56,189,192}` assignee 为空/不可映射，user_id=0 属正常（不处理）。

## 二、根因（已定位，代码 + 数据交叉确认）

### 2.1 直接来源：`session_store._sync_issue_fields(ensure_row=True)` 最小建档硬编码 user_id=0

`src/api/session_store.py:118-125`：

```python
if ensure_row:
    c.execute(
      "INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) "
      "SELECT 0, ?, ?, ?, 'active' WHERE NOT EXISTS (...)",   # ← user_id 恒为 0
      (assignee, repo_key, _num, repo_key, _num))
```

- `set_issue_stage` / `set_issue_scene`（stage/scene 收编写入）都会以 `ensure_row=True` 建**最小卡片行**；
- 若该写入**先于** `ensure_issue_task`（任务中心建档）发生（pipeline/onboarding/场景体验路径常见，
  如 issue_opened → set_issue_stage('vod') 先建行），行即被建成 `user_id=0`；
- 随后 `ensure_issue_task` 幂等复用该行（tasks.py:200-222），**只在「assignee 变更」时 UPDATE user_id**
  → `user_id=0` 固化。

### 2.2 旁证：`ensure_issue_task` 正常建档会给正确 user_id

`tasks.py:186`：`user_id = _get_user_id_by_gitcode(assignee)`（映射在 → 7）。同仓 #2/#3 的 user_id=7 佐证；
恰好 #4/#5 走了 minimal-row 先行路径 → 0。两种路径并存导致同仓不同归属。

### 2.3 ❌ 已否决：mine 查询兜底 `OR user_id=0`

`my_issues`（issues.py:710）现为 `user_id=?`——**保持不动**。
`ensure_issue_task` docstring 里「P2 拍板 mine 查 OR user_id=0」与 `user_id=0` 无法归属个人 →
兜底会把不属于自己的需求摊给所有用户（查错）；该注释不再作为依据。

---

## 三、修复方案（修数据 + 修建档路径，不动查询）

### 3.1 修建档路径（治本）：`_sync_issue_fields(ensure_row=True)` 按 assignee 解析 user_id

`session_store.py` minimize 建档改为「先尝试解析 assignee→user_id，解析不到才 0」：

```python
"INSERT INTO issue (user_id, assignee, repo_key, issue_number, status) "
"SELECT COALESCE((SELECT m.user_id FROM unified_user_mapping m "
"                WHERE m.gitcode_user = ? AND m.user_id > 0 LIMIT 1), 0), ?, ?, ?, 'active' "
"WHERE NOT EXISTS (SELECT 1 FROM issue WHERE repo_key=? AND issue_number=?)",
(params: assignee, assignee, repo_key, _num, repo_key, _num)
```

- 保留「解析不到→0」兜底（未注册用户仍 0），但不凭空丢归属；
- 该 INSERT 为幂等最小行，语义不变（仅 user_id 来源更正确）。

### 3.2 防再犯（补丁）：`ensure_issue_task` 幂等分支回补 user_id

`tasks.py` 幂等分支（命中已有行）中，`user_id=0 且 assignee 已可映射` → 回补：

```python
if not _existing_user_id and target:
    _uid_now = _get_user_id_by_gitcode(target) or 0
    if _uid_now:
        c.execute("UPDATE issue SET user_id=? WHERE id=?", (_uid_now, task_id))
```

（仅补 0→已映射 id；不动既有正确归属。）

### 3.3 数据回填（治存量，6 行 → 7）

`scripts/backfill_issue_user_id.py`（`--dry-run` / `--apply`，幂等可重跑）：

```sql
UPDATE issue
SET user_id = (SELECT m.user_id FROM unified_user_mapping m
               WHERE m.gitcode_user = issue.assignee AND m.user_id > 0 LIMIT 1)
WHERE issue.user_id = 0
  AND issue.assignee != ''
  AND EXISTS (SELECT 1 FROM unified_user_mapping m
              WHERE m.gitcode_user = issue.assignee AND m.user_id > 0);
```

- dry-run 输出受影响行（id/repo/#/assignee/待填 user_id）人工核对（预期即 §一 的 6 行）；
- 不改 assignee/created_by 等其它字段；`id ∈ {56,189,192}`（不可映射）不在范围内。

---

## 四、实施步骤

| 步 | 内容 | 产出 |
|----|------|------|
| P1 | 3.1 session_store ensure_row 按 assignee 解析 user_id + 3.2 ensure_issue_task 幂等回补 | session_store.py / tasks.py |
| P2 | 3.3 回填脚本（dry-run/apply） | `scripts/backfill_issue_user_id.py` |
| P3 | tests：ensure_row 建行 user_id 断言（assignee 已映射→id；未映射→0）+ ensure_issue_task 回补 + 回填脚本 dry-run 结果 | 测试 |
| P4 | 生产：`backfill --dry-run` 核对 6 行 → `--apply` → 部署 → 回归 | #193/#194 在 mine 可见 |

## 五、验证

1. 单测：`_sync_issue_fields(ensure_row=True, assignee='Jason7602')` 建行 user_id=7（映射存在）/ assignee 未知 → 0。
2. 单测：已存在 user_id=0 + assignee 可映射 → `ensure_issue_task` 复用后回补 user_id。
3. 生产 dry-run 列 6 行，apply 后 SQL 复查 `user_id=0 且可映射`=0；#193/#194 → 7。
4. 部署后：Jason7602 登录，「我的需求」出现 #193/#194；**其它用户 mine 不含它们**（无兜底泄漏）。
5. 全量回归 `test_openapi` / `test_issues_api` / `test_tasks`。

## 六、影响面与风险

| 项 | 影响 |
|----|------|
| 建档 | 新需求（尤其模板/场景体验路径）从生成起即正确归属 user_id，不再出现「仓库可见但 mine 不可见」 |
| 数据 | 回填仅修 6 行「0→已映射 id」；不动不可映射行；幂等可回滚 |
| mine 查询 | **零改动**（保持 `user_id=?`，无兜底泄漏） |
| 越权面 | assignee 归属即创建人/责任人的既有语义，不新增权限 |
| 风险 | 同名多 GitCode 名映射边界 → dry-run 人工核对；`set_issue_scene/stage` 高频路径改动后需回归 pipeline |