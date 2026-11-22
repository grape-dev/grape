# Issue #3731 方案设计：feedback→Issue 转换修复

> 目标合入分支：`arch-weekly-11-ljt` | 依赖：`src/kernel/repo_key.py`（只读契约）、`src/domains/mgmt/public`（数据原语）

## 方案概述

以 `parse_repo_key() -> (provider, owner, repo)` 为**唯一解析契约**，把「路由解析 / 标题截断 / 失败升级」三处口径收敛，消除建单与查重之间的二次解析分叉；失败轮次落到 `user_feedback`（跨 worker 共享库唯一可信状态），日志按阈值升级。

## 实现路径

### 1. 路由解包（根因 A）

`_repo_target()`：

```python
provider, owner, repo = parse_repo_key(repo_key)   # 3 元组契约，删除紧随其后的 split_owner_repo() 二次解析
return (provider or "gitcode", owner or "", repo or "")
```

`_repo_bot_token()`：`provider = parse_repo_key(repo_key)[0] or "gitcode"`。

- `parse_repo_key("")` 已返回 `("gitcode","","")`，空键默认语义不变（保留 `or "gitcode"`）。
- 全仓 `parse_repo_key` 调用点复核：其余均为 3 元解包或 `[0]` 下标取值，仅本文件两处 2 元解包（验收第 1 条）。

### 2. 标题截断 + 查重同键（根因 B）

```python
_MAX_TITLE_LEN = 200
_TITLE_ELLIPSIS = "…"

def _clip_title(title) -> str:
    t = (title or "").strip()
    return t if len(t) <= _MAX_TITLE_LEN else t[:_MAX_TITLE_LEN - 1] + _TITLE_ELLIPSIS
```

- `_create_provider_issue()` 组装 payload 时 `title=_clip_title(title)`（平台侧 400 永不再现）。
- `_find_existing_open_issue()`：`want = _norm_title(_clip_title(title))` —— 与建单侧**同一入参**，
  截断后同键必然命中复用，避免「截断后重复建单」。

### 3. 失败可观测（持久化计数）

新增列（`mgmt` 域 owner 表）：

```sql
ALTER TABLE user_feedback ADD COLUMN convert_fail_count INTEGER NOT NULL DEFAULT 0
```

登记到 `mgmt/migrations_ddl.py::ALTERS_USER_FEEDBACK_COLUMNS`（`db_setup` 启动时逐条幂等执行，SQLite / MySQL 同一句）。

数据原语（`src/domains/mgmt/public/__init__.py`，具名 SQL + `_any_tx`，任意拓扑）：

| 原语 | SQL 语义 |
|------|---------|
| `feedback_pending_rows` | SELECT 增补第 16 列 `convert_fail_count` |
| `feedback_bump_convert_fail(fid) -> int` | `convert_fail_count=COALESCE(...,0)+1` 后同事务读回累计值 |
| `feedback_mark_converted` / `..._from_claim` | 成功回写时**同时** `convert_fail_count=0`（成功后清零） |

转换器侧：

```python
def _note_failure(fid, reason, provider="", owner="", repo="", prev_count=0) -> None:
    times = _mgmt_public.feedback_bump_convert_fail(fid)      # 失败不阻塞主流程（try/except）
    # prev_count：本轮自增前的持久化计数（_fetch_pending 读回），用于判定「跨越阈值」
    msg = f"feedback→issue: {reason} (fid={fid}, consecutive={times}/{threshold})"
    log.error(msg) if _fail_is_escalation(times, prev_count) else log.warning(msg)
```

- 三条**真失败**路径（路由解析异常 / 无 bot token / create failed）统一走 `_note_failure`，
  **保留原有可检索子串** `target resolve failed` / `create failed`（Grafana 口径不变）；
- ERROR 只在「跨越阈值」那一轮发一次（`_fail_is_escalation`：`prev < threshold <= times`），
  其后各轮回到 WARNING——否则单行永久卡死时每轮 1 条 ERROR（阈值 3 + interval 300s →
  288 条/天），与「消除 20.7 万条噪声」的目标背反；
- **策略跳过 / 不可路由不计连败**（`_feedback_routeable`）：`GRAPE_FEEDBACK_OFFICIAL_ISSUE=0`
  的官方目标为空、repo/user 路由缺 `repo_key`，均为「有意不建单 / 数据缺失」而非转换失败，
  只 skip 不计数，保持 Spec Out of Scope 的开关语义不变；
- 阈值 `FEEDBACK_CONVERT_FAIL_ESCALATE`（默认 3，最小 1）。

## 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 解包方式 | 下标取值 / 全解包 | 全解包 | 一次解析同时产出 owner/repo，消除二次解析口径分叉（本次事故成因） |
| 截断实现 | 建单侧内联 / 共用函数 | 共用函数 | 建单与查重必须同键，否则截断后仍重复建单 |
| 失败计数 | 进程内存 / 复用 fail_summary / 新增列 | 新增 `convert_fail_count` 列 | 多 worker 共享库，进程内计数无效；`fail_summary` 是 Agent 上报原文（会进公开 Issue 正文），复用会覆盖数据并污染正文；新增一列语义单一、admin 可直接查 |
| 日志升级阈值 | 硬编码 / env 可调 | env 可调（默认 3） | 与 Spec「默认 3」一致，便于不同部署调整 |

## 影响面

| 文件 | 改动 |
|------|------|
| `src/api/feedback_issue_converter.py` | 解包、截断、查重键、失败计数与升级 |
| `src/domains/mgmt/public/__init__.py` | 3 条 SQL 增补/新增 + 2 个公开原语 + 导出登记 |
| `src/domains/mgmt/migrations_ddl.py` | user_feedback 新增计数列 |
| `tests/runtime/test_feedback_issue_converter.py` | 新增 4 组用例；修正 1 条与 #673 语义冲突的历史用例 |

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 依赖新列（未迁移库） | 中 | ALTER 在 `init_database()` 幂等执行；`_note_failure` 全程 try/except，计数失败只丢可观测性、不影响建单主链路 |
| 截断后同前缀长标题碰撞 | 中 | 查重与建单共用同一函数，碰撞即复用（幂等，符合目标） |
| 全解包改变 owner/repo 解析路径 | 中 | 单测覆盖空键 / 裸键 / 带 provider 前缀键三态，与旧返回值逐项比对 |