# 方案：修复 Agent 环境变量 ACCOUNT / REPO_OWNER / REPO_NAME 注入语义

> 状态：技术方案（2026-09-29 第二轮：语义拆分）｜分支 weekly-05-lyz
> 现象：vod-cli 执行日志中 `ACCOUNT="grape-dev/grape"`（被注入为 owner/repo 全名）、`REPO_NAME` 为空 → agent 被迫手动 `export ACCOUNT="grape-dev" REPO_NAME="grape"` 并反复重试。
> 目标（评审修正）：区分三个语义——
> - **`REPO_OWNER`** = 仓库属主（组织或个人的泛化概念），从 repository 拆分；
> - **`ACCOUNT`** = 触发 agent 的**个人账号**（actor，取自 webhook 用户），与 owner 解耦；
> - **`REPO_NAME` / `REPO_FULL_NAME`** = repo 名 / 全名。

---

## 一、现状与根因（生产代码核对 2026-09-29）

### 1.1 注入点（`src/api/event_pipeline.py`，3 处 env_extra 组装）

| 当前行号 | 代码 | 问题 |
|----------|------|------|
| L255 | `env_extra = {"REPO_FULL_NAME": repository, "ACCOUNT": evt.get("account") or repository}` | ACCOUNT 直接取 evt.account（可能是全名），无 owner 拆分 |
| L2011 | `env_extra = {..., "ACCOUNT": evt.get("account") or repository}` | 同上 |
| L2685 | `env_extra = {..., "ACCOUNT": exec_account}` | exec_account 需核对是否全名 |

三处均**未注入 `REPO_NAME`**（repo 名缺失）。

### 1.2 account 来源链

- `event_pipeline` 兜底：`if not account: evt["account"] = evt["repository"]` → 当调用方未传 account 时，**ACCOUNT 直接等于仓库全名 `owner/repo`**。
- 各事件入口（webhook/A2A 等）传 account 时可能传的是**仓库全名**而非 owner（如 webhook `account = payload.get("account","") or repository`）。

**根因**：注入链路未执行"从 `repository`（全名）拆出 owner / repo 两段"，而是把 `account`/`repository` 原样当作 ACCOUNT；且 `REPO_NAME` 从未注入。

---

## 二、优化方案

### 2.1 注入层：语义拆分 REPO_OWNER / REPO_NAME / ACCOUNT(actor)

**复用现有 `src.repo_key.split_owner_repo(owner_repo) -> (owner, repo)`**（兼容 `owner/repo` / `gitcode:owner/repo`）。

三处 env_extra 统一改为：

```python
from src.repo_key import split_owner_repo  # 模块顶部引入
owner, repo_name = split_owner_repo(repository)
env_extra = {
    "REPO_FULL_NAME": repository,     # 全名 owner/repo（无前缀语义）
    "REPO_OWNER": owner,              # 仓库属主（组织或个人）
    "REPO_NAME": repo_name,
    "ACCOUNT": evt.get("actor") or evt.get("account") or "",  # 个人账号（webhook 触发人）
    "GRAPE_SERVER_URL": _resolve_server_url(runtime_id),
}
```

L2685（direct 分支 exec_account）同样：

```python
owner, repo_name = split_owner_repo(repository or exec_account)
env_extra = {
    "REPO_FULL_NAME": repository,
    "REPO_OWNER": owner,
    "REPO_NAME": repo_name,
    "ACCOUNT": evt.get("actor") or exec_account or "",
}
```

要点：
- **`REPO_OWNER`** = 仓库属主（`split_owner_repo` 拆分，组织/个人通用，替代原 ACCOUNT=owner）；
- **`ACCOUNT` = 个人账号** → 取 `evt["actor"]`（webhook 触发人用户名），与 owner 解耦；actor 缺失回落 `account`；
- 新增 `REPO_NAME` = repo 名；`REPO_FULL_NAME` 保持全名。

### 2.2 actor 来源（已回答）

`actor` 由各 VCS backend `normalize_event` 提取：
- `src/vcs/gitcode.py:103` `_extract_actor(payload)`：`payload.user_name → sender.username → user.username → issue.author.username`；
- `github.py` / `gitlab.py` normalize 同样产出 `actor`；
- `normalize_event` 返回含 `"actor": actor`（gitcode.py:86-87），进入 `evt["actor"]`（event_pipeline L990）。

故 `evt["actor"]` 已可靠携带；**无需额外 API 查询**（比 owner 更贴近"个人账号"）。

### 2.3 兜底：evt account 归一

`if not account` 兜底保持取 owner 段（供 A2A 传导）：

```python
if not account and evt.get("repository"):
    _o, _ = split_owner_repo(evt["repository"])
    evt["account"] = _o
```

> 注：`evt["account"]` 现仅作 A2A/重派传导兜底，Agent env 的 `ACCOUNT` 已独立取 `actor`，二者解耦。

### 2.4 worker 侧

确认 worker `agent.py`/引擎 get_env 不覆盖这些变量（worker 侧一般不加，由服务端 env_extra 注入）——如有覆盖则同用 `split_owner_repo`。

### 2.5 文档同步

`help/usage/agent-environment-variables.md`：
- `REPO_OWNER` = 仓库属主（组织/个人），从 repository 拆分；
- `ACCOUNT` = 个人账号（webhook 触发人，`actor`），与 owner 解耦；
- `REPO_NAME` / `REPO_FULL_NAME` = repo 名 / 全名。

---

## 三、影响面

| 影响 | 说明 |
|------|------|
| 技能侧 | runbook 若用 `$ACCOUNT` 需核对语义（个人触发人 vs 原 owner 用法）；owner 用途改用 `$REPO_OWNER` |
| 现有依赖 ACCOUNT 的代码 | grep `ACCOUNT` 语义变化（不能再当 owner 用）——需技能同步改用 REPO_OWNER |
| A2A payload | `evt["account"]` 仍作 A2A 兜底传导（owner 段），Agent env 的 `ACCOUNT` 独立取 actor |

---

## 四、验证

1. 触发 issue_opened → 注入 env：`REPO_OWNER=grape-dev`、`REPO_NAME=grape`、`REPO_FULL_NAME=grape-dev/grape`、`ACCOUNT=<触发人>`（个人账号）。
2. 回归技能不再出现「ACCOUNT 为全名导致 export 修正」。
3. 测试：`split_owner_repo` 拆分 + env_extra 断言 `REPO_OWNER`/`REPO_NAME`/`ACCOUNT(actor)`。
4. 技能侧：vod-cli Step 3 env 自检正确。

---

## 五、技能侧兜底（可选，注入修复的补充）

在 runbook Prerequisites/Step 0 前加 env 自检/修正（幂等，双重保险）：

```bash
# REPO_FULL_NAME 全名、REPO_NAME repo 名、REPO_OWNER 属主、ACCOUNT 个人触发人
export REPO_OWNER="${REPO_OWNER:-${REPO_FULL_NAME%/*}}"   # 缺省从全名推导属主
export REPO_NAME="${REPO_NAME:-${REPO_FULL_NAME#*/}}"     # 缺省从全名推导 repo 名
export ACCOUNT="${ACCOUNT:-$REPO_OWNER}"                  # 缺省回落属主（但不再对 ACCOUNT 做 %/* 切分——它已是个人账号）
```

> 注：ACCOUNT 是个人账号（actor），**不要**再 `%/*` 切分（会把它从"个人"截成 owner 段）；owner 用途用 `REPO_OWNER`。

---

## 六、本次落档修订（相对 grape-skills 原方案）

- **helper 选择**：不新增 `split_repo_parts`，复用 `src.repo_key.split_owner_repo`（已存在、功能等价、少一份冗余）。
- **行号校准**：注入点为 L255 / L2011 / L2685（合并 weekly-05 后实测），`REPO_NAME` 三处均未注入确认。
- **范围**：本方案仅 `event_pipeline.py` 三处 env_extra + account 兜底归一 + 文档，不动技能逻辑。