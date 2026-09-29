# 方案：修复 Agent 环境变量 ACCOUNT / REPO_NAME 注入错误

> 状态：技术方案（2026-09-29，基于 grape-skills `temp/fix-agent-env-account-reponame.md` 核对后落档）｜分支 weekly-05-lyz
> 现象：vod-cli 执行日志中 `ACCOUNT="grape-dev/grape"`（被注入为 owner/repo 全名）、`REPO_NAME` 为空 → agent 被迫手动 `export ACCOUNT="grape-dev" REPO_NAME="grape"` 并反复重试，浪费步骤。
> 目标：确保注入给 agent 的 `ACCOUNT`（=owner）、`REPO_NAME`（=repo 名）、`REPO_FULL_NAME`（=全名）各自正确、来源唯一，技能可直接使用、无需推导。

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

### 2.1 注入层：统一拆分 owner / repo / full_name

**复用现有 `src.repo_key.split_owner_repo(owner_repo) -> (owner, repo)`**（已存在，兼容 `owner/repo` / `gitcode:owner/repo`，不需新 helper）。

三处 env_extra 统一改为：

```python
from src.repo_key import split_owner_repo  # 模块顶部引入
owner, repo_name = split_owner_repo(repository)
env_extra = {
    "REPO_FULL_NAME": repository,   # 保持全名（无前缀语义）
    "ACCOUNT": (evt.get("account") or owner).split("/")[0],  # 强制 owner 段
    "REPO_NAME": repo_name,
    "GRAPE_SERVER_URL": _resolve_server_url(runtime_id),
}
```

L2685（direct 分支 exec_account）同样：

```python
owner, repo_name = split_owner_repo(repository or exec_account)
env_extra = {
    "REPO_FULL_NAME": repository,
    "ACCOUNT": (exec_account or owner).split("/")[0],
    "REPO_NAME": repo_name,
}
```

要点：
- `ACCOUNT` 强制取 owner 段（若 account 实为全名，`split("/")[0]` 兜底）；
- 新增 `REPO_NAME` = repo 名；
- `REPO_FULL_NAME` 不变（全名）。

### 2.2 兜底：evt account 归一

`if not account` 兜底改为取 owner 段：

```python
if not account and evt.get("repository"):
    _o, _ = split_owner_repo(evt["repository"])
    evt["account"] = _o
```

避免后续任何路径拿到全名作 ACCOUNT。

### 2.3 worker 侧

确认 worker `agent.py`/引擎 get_env 不覆盖这三变量（worker 侧一般不加 ACCOUNT/REPO_NAME，由服务端 env_extra 注入）——核对后如有一致性需求则同用 `split_owner_repo`。

### 2.4 文档同步

`help/usage/agent-environment-variables.md`：
- `ACCOUNT` 语义明确为「仓库属主（owner，如 `Jason7602`；从 repository 拆分，不含 `/repo`）」；
- 新增 `REPO_NAME` 行（repo 名，服务端注入，技能直接用）。

---

## 三、影响面

| 影响 | 说明 |
|------|------|
| 技能侧 | vod/issue/coding/qa/deploy runbook 已用 `$ACCOUNT`（owner）+ `$REPO_NAME`——修复后无需手动推导 |
| 现有依赖 ACCOUNT 的代码 | 需 grep `ACCOUNT` 用作全名的地方（build 提示/im 通知等）核对语义 |
| A2A payload | A2A `account` 字段是否走同源拆分？核对 webhook/`account` 归一为 owner |

---

## 四、验证

1. 触发 issue_opened → 注入 env：`ACCOUNT=grape-dev`、`REPO_NAME=grape`、`REPO_FULL_NAME=grape-dev/grape`。
2. 回归 vod/issue 处理不再出现「ACCOUNT 为全名导致 export 修正」。
3. 测试：`test_split_repo_parts`（复用 `split_owner_repo`；覆盖 `owner/repo` / `gitcode:owner/repo` / URL 输入）；`test_event_env_extra` 断言 ACCOUNT/REPO_NAME。
4. 技能侧抽样：vod-cli Step 3 env 自检直接正确，无需修正。

---

## 五、技能侧兜底（可选，注入修复的补充）

在 runbook Prerequisites/Step 0 前加 env 自检/修正（幂等，双重保险）：

```bash
export ACCOUNT="${ACCOUNT%/*}"            # 若被注入成全名，强制取 owner 段
export REPO_NAME="${REPO_NAME:-${REPO_FULL_NAME#*/}}"   # 为空时从全名推导
```

---

## 六、本次落档修订（相对 grape-skills 原方案）

- **helper 选择**：不新增 `split_repo_parts`，复用 `src.repo_key.split_owner_repo`（已存在、功能等价、少一份冗余）。
- **行号校准**：注入点为 L255 / L2011 / L2685（合并 weekly-05 后实测），`REPO_NAME` 三处均未注入确认。
- **范围**：本方案仅 `event_pipeline.py` 三处 env_extra + account 兜底归一 + 文档，不动技能逻辑。