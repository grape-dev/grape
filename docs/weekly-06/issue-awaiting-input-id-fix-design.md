# 方案：修复 `awaiting-input` 调用失败——issue_id 语义 + 服务端归一 + 集群 base 核对

> 状态：技术方案（2026-10-12）｜分支：weekly-06-lyz
> 动机：集群 vod agent 对 `grape-dev/grape #303`（卡片 id=`2351`）调 `grape issues awaiting-input --issue_id 4416368` 返回 403；集群主服务日志无该请求（id 不存在于 `grape_system_db`，max≈2351），实际打到其它端点/服务被 403 拦截。同仓 10-09 曾正常（400/422 校验错、非 403）→ 机制可用，问题在参数语义 + 环境指向 + 服务端裸比较。
> 目标：① 让 agent 明确「`--issue_id` = 需求卡片主键（非 issue number）」；② 服务端作用域比较做 `to_repo_key` 归一 + 可选按 `(repo_key, issue_number)` 兜底；③ 集群核对 CLI 实际 base_url 与主服务一致。

---

## 1. 现状与证据（集群核对 2026-10-12）

| 项 | 值 |
|----|----|
| `grape-dev/grape #303` 卡片 | id=`2351`，repo=`gitcode:grape-dev/grape`，stage=vod |
| agent 传参 | `--issue_id 4416368`（不存在）/ `--issue_id 303`（issue number，非 PK） |
| 服务端按 id 查询 | `SELECT ... WHERE id=4416368` → **无行**（应 404） |
| 集群主服务日志（10-12 03:39Z） | 无 `/api/issues/4416368|303/awaiting-input` 请求 → 403 非来自该端点 |
| 端点契约 | `POST /api/issues/{issue_id}/awaiting-input`，`issue_id`=**需求卡片主键**（issue-awaiting-input-api-design.md） |

根因三叠：**① id 语义用错（关键）；② CLI/server 实际 base 与主服务不一致（403 来源）；③ 服务端仓库作用域裸比较（`gitcode:` 前缀差异易错位）**。

---

## 2. 方案设计

### 2.1 服务端注入 `GRAPE_ISSUE_ID`（首选，agent 免查 id）

在 `event_pipeline` env_extra 装配处（`ISSUE_NUMBER` 同处，L2300 / L3046 附近）追加：

```python
# 需求卡片 id（issue 表主键），awaiting-input/pending-review/review-pass 的 --issue_id 用此值
_card_id = ""
try:
    _n = str(_issue_num).lstrip("#")
    if _n.isdigit():
        with db_lock:
            _r = db.cursor().execute(
                "SELECT id FROM issue WHERE repo_key=? AND issue_number=? LIMIT 1",
                (repo_key, int(_n))).fetchone()
        if _r:
            _card_id = str(_r[0])
except Exception:
    pass
env_extra["GRAPE_ISSUE_ID"] = _card_id
```

- 语义：`GRAPE_ISSUE_ID` = 需求卡片主键（整数），与 `ISSUE_NUMBER`（GitCode number）**不同**；agent 直接 `grape issues awaiting-input --issue_id "$GRAPE_ISSUE_ID"`。
- 文档同步：`help/usage/agent-environment-variables.md` 在 `ISSUE_NUMBER` 下新增 `GRAPE_ISSUE_ID` 行。

### 2.2 runbook / SKILL 指引（兜底，短期必做）

`grape-vod-cli` / `grape-issue-cli` 的 SKILL（及 `issue-explicit-awaiting-input-api-design.md` 示例）追加硬性 Step：

```
调用 awaiting-input / pending-review / review-pass 前：
  1) 首选：GRAPE_ISSUE_ID（服务端已注入需求卡片 id）
     grape issues awaiting-input --issue_id "$GRAPE_ISSUE_ID" --json '{"questions":"…","reason":"…"}'
  2) 兜底（环境缺失时才查）：
     grape issues detail --owner $REPO_OWNER --repo $REPO_NAME --number $ISSUE_NUMBER --json-out
     从返回的 id 字段取「卡片 id」再调用
⚠️ 严禁把 ISSUE_NUMBER 当作 --issue_id；GRAPE_ISSUE_ID / ISSUE_NUMBER / GitCode number 语义不同。
```

### 2.3 服务端加固（`src/api/routes/issues.py`）

**a. 仓库作用域归一**（消除 `gitcode:` 前缀错位）：
`issue_awaiting_input`（L855-872）与 `_session_devops_issue`（pending/review 共用，L45-80 区间）中：
```python
from src.repo_key import to_repo_key
if to_repo_key(repo_key) != to_repo_key(session_repo):
    raise HTTPException(403, ...)
```
（`to_repo_key` 对带/不带 `gitcode:` 前缀幂等归一）

**b. 按 number 兜底（可选子项）**：`{issue_id}` 若在 issue 表按 PK 查不到，且该值为 number 语义时无法推断 repo → 不自动兜底（避免歧义）；改为**报错信息明确**：
- `404 需求不存在` 时附 `hint=awaiting-input 需传需求卡片 id，可先 grape issues detail --number {n} 取 id`，让 agent 一眼看懂。

### 2.4 集群 base 核对（运维/CLI 配置）

- 核对 agent 会话 的 `GRAPE_SERVER_URL` / CLI `server_base_url` 与集群主服务 `grape.example.com` 一致；区分 worker 上报基址 `worker.grape.example.com`（仅 worker 注册用）。
- 校验主服务 `/api/issues/{id}/awaiting-input` 是否已随部署生效（`grape refresh` 后 `grape issues awaiting-input --help` 可见）。
- 若集群节点与主服务版本不一致（旧 weekly-04），升/对齐后重测「卡片 id 2351」调用。

---

## 3. 各模块改动

| 位置 | 改动 |
|------|------|
| `src/api/event_pipeline.py` | env_extra 装配处（`ISSUE_NUMBER` 同点，L2300 / L3046 附近）注入 `GRAPE_ISSUE_ID`（按 repo+issue_number 查卡片 id）（2.1） |
| `src/api/routes/issues.py` | awaiting-input 与 `_session_devops_issue` 的 repo 比较加 `to_repo_key` 归一；404 提示改可读 hint（2.3） |
| `help/usage/agent-environment-variables.md` | `ISSUE_NUMBER` 下新增 `GRAPE_ISSUE_ID` 行（已同步） |
| `skills/*/SKILL.md` + runbook（独立技能仓库） | waiting-input 前置用 `$GRAPE_ISSUE_ID`；缺失才 detail 查 id（2.2） |
| 文档 | `issue-explicit-awaiting-input-api-design.md` / `help/api/11-issues.md` 示例补「用 GRAPE_ISSUE_ID」 |
| 集群运维 | 核对 server_base_url / 端点版本（2.4） |

---

## 4. 影响面 / 边界

| 项 | 说明 |
|----|------|
| 服务端归一 | 向后兼容（`to_repo_key` 幂等）；消除前缀错位 403；不影响已带前缀正常调用 |
| number 兜底 | 不做自动兜底（避免 repo 歧义），仅优化报错提示 |
| runbook | 收敛为「优先 `$GRAPE_ISSUE_ID`，缺失才 detail 取 id」，一次性消除误传 number/乱编 id |
| 新 env | 增量注入 `GRAPE_ISSUE_ID`，无破坏（缺失时为空串，runbook 兜底查 id） |
| 集群 | 不依赖本方案即验证（用 2351 直试）；base 核对属运维巡检 |

---

## 5. 验证

| 用例 | 断言 |
|------|------|
| UT-00 env 注入：任务 env_extra 含 `GRAPE_ISSUE_ID` = 该 (repo, issue) 卡片 id；无卡片 → 空串 | 注入 |
| UT-01 `awaiting-input` 传正确卡片 id（repo 前缀一致）→ 200 changed=true | 主路径 |
| UT-02 token repo 无前缀 vs 卡片带 `gitcode:` → `to_repo_key` 归一后 200（回归前为 403） | 归一 |
| UT-03 id 不存在 → 404，detail.message 含「先 grape issues detail --number 取 id」hint | 提示 |
| UT-04 id 存在但 repo 不一致（归一后仍不同）→ 403 | 越权守卫 |
| 集群手工 | `grape issues awaiting-input --issue_id "$GRAPE_ISSUE_ID"` → 200（待集群验证） |

---

## 6. 落档修订说明

- 核心结论：**issue_id=卡片主键而非 number**；之前失败的组合（4416368 不存在 / 303 非 PK）+ CLI 打到旧端点 → 403。
- 服务端归一为长期护栏；runbook 为立即闭环；集群 base 核对为环境护栏。
- 验证以「对 2351 直测」为一条线贯穿三个层面。