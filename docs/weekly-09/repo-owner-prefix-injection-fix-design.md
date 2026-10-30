# 修复 direct 分支 `_execute_agent_inner` 的 REPO_OWNER 注入未剥平台前缀导致 git-repo/repos 404 [weekly-09]

> 状态：**已实施（TDD 先红后绿 ✅）**｜目标分支：weekly-09（部署参考 weekly-09-lyz）
> **敏感信息声明**：本文不含真实 IP、域名、账号、仓库名或 token（一律 `<xxx>` 占位；仓库名用 `<owner>/<repo>`）。
> **关联**：官方技能仓 Issue #94（Agent 自动反馈，source=agent；与文档中 `<owner>/<repo>` 占位一致）。

---

## 〇、问题现象与影响

- Agent（vod，任务 `t_<id>`）执行 `issue_opened`（seq=`<seq>`）时，经本地 direct 引擎认领执行，
  任务内 `REPO_OWNER` 环境变量被注入为**带平台前缀**的 `gitcode:<owner>`，而不是纯 owner `<owner>`。
- 将该值传给 `grape git-repo issues comments list` / `git-repo pulls list` / `git-repo tree` /
  `repos project info` 均返回 404/502（URL 编码为 `gitcode%3A`，服务端按资源路径解析不到）；
  而 `grape issues` / `scenes` / `a2a` 可接受该值（内部按 repo_key 全名含前缀兼容）。
- 影响：official 技能（`<skill>`）的本地 direct 任务在 git-repo/repos 系列命令上必然失败，
  降低 Agent 成功率并产生误导性反馈。

---

## 一、根因（已定位）

注入 `REPO_OWNER` 的服务端共 4 处（`src/api/event_pipeline.py`）：

| 行 | 取值写法 | 是否剥前缀 |
|----|---------|-----------|
| 484 / 2829 / 3134（入队/`build_task_env`） | `split_owner_repo(...)` | ✅ 正确剥 `gitcode:` |
| **3815（`_execute_agent_inner` 重建 env）** | `(exec_account or repository).split("/", 1)[0]` | ❌ **未剥前缀** |

- **未使用入队快照**：`_execute_agent_inner` 在 direct 执行时为保证最新上下文**本地重建 env_extra**
  （不读 `agent_tasks.env_extra` 快照——快照本身是正确的 `REPO_OWNER=<owner>`）。
- **坏值来源**：`exec_account = evt.get("account") or repository`；local_claimer `_rebuild_evt_from_task`
  的 `account = task.get("account")` 是**带前缀全名** `gitcode:<owner>/<repo>`。
- **naive split 缺陷**：`"gitcode:<owner>/<repo>".split("/", 1)[0]` = `gitcode:<owner>`（带前缀），
  而 `split_owner_repo`/`parse_repo_key` 会正确剥前缀（`repo_key.py:37`）。

> 测试缺口佐证：`tests/test_agent_env_parts.py:72-85`（`test_direct_branch_env_parts`）只覆盖
> `exec_account` 为**无前缀** `<owner>/<repo>` 的形态，未覆盖带前缀输入 → 缺陷漏网。

---

## 二、目标语义

1. `_execute_agent_inner` 注入的 `REPO_OWNER` 与其余 3 处**口径一致**：恒为**纯 owner**（剥平台前缀）。
2. 若 `exec_account` 是带前缀全名，也能解析出纯 owner；无前缀输入行为不变（零回归）。
   （`exec_account` 恒非空：已在 3798 行由 `evt.get("account") or repository` 兜底，拆分不另设兜底。）
3. 复用既有工具 `src/repo_key.split_owner_repo`（单一前缀剥离事实源，禁止再裸 `str.split("/")`）。
4. `REPO_NAME` 同样由该工具推导，避免 `rsplit("/")` 偶发不一致。

---

## 二.5、修复后各环境变量的取值口径（以带前缀输入 `gitcode:<owner>/<repo>` 为例）

| 环境变量 | 表达式 | 值 | 平台前缀 |
|---------|--------|----|---------|
| `REPO_FULL_NAME` | `repository`（`evt["repository"]`，local_claimer 已剥前缀） | `<owner>/<repo>` | ❌ 无 |
| `REPO_OWNER` | `split_owner_repo(exec_account)[0]` | `<owner>` | ❌ 无 |
| `REPO_NAME` | `split_owner_repo(exec_account)[1]` | `<repo>` | ❌ 无 |
| `ACCOUNT` | `evt.get("actor") or exec_account` | `actor`（`resolve_actor` 兜底到 `"unknown"`，一般纯用户名） | ⚠️ 仅 actor 空时回落 `exec_account`（带前缀全名） |

> **结论**：修复后 `REPO_OWNER` / `REPO_NAME` / `REPO_FULL_NAME` 三者均无平台前缀，git-repo/repos
> 命令不再收到 `gitcode:<owner>`（Issue #94 的 404 消除）。
>
> **ACCOUNT 属独立问题**（不在本 Issue 范围）：语义是"执行身份"，介入当 actor 缺失时才回落带前缀的
> `exec_account`。是否需对 ACCOUNT 统一剥前缀（入队处 486 用 `evt.get("actor") or evt.get("account")`，
> direct 处 3817 用 `evt.get("actor") or exec_account`，两处已一致），取决于下游对 ACCOUNT 的消费预期，
> 与 git-repo/repos 404 无关；如需治理应另开方案。

---

## 三、方案设计

### 3.1 根治：`_execute_agent_inner` 复用 `split_owner_repo`

`src/api/event_pipeline.py:3813-3817`：

```python
# 现状（未剥前缀，direct 分支缺陷源头）
env_extra = {
    "REPO_FULL_NAME": repository,
    "REPO_OWNER": (exec_account or repository).split("/", 1)[0],
    "REPO_NAME": (exec_account or repository).rsplit("/", 1)[-1],
    "ACCOUNT": evt.get("actor") or exec_account or "",
}
```

```python
# 修复：统一走 split_owner_repo（兼容带/不带前缀，repo_key.py:37）。
# exec_account = evt.get("account") or repository 已在 3798 行兜底过，此处无需再 or——直接对单一
# 非空值拆分，消除冗余（exec_account 恒非空：account 有值取 account，无值即 repository）。
from src.repo_key import split_owner_repo
_repo_owner, _repo_name = split_owner_repo(exec_account)
env_extra = {
    "REPO_FULL_NAME": repository,
    "REPO_OWNER": _repo_owner,
    "REPO_NAME": _repo_name,
    "ACCOUNT": evt.get("actor") or exec_account or "",
}
```

- `split_owner_repo("gitcode:<owner>/<repo>")` → `(<owner>, <repo>)`；对无前缀输入同样正确。
- 仅改 `_execute_agent_inner` 一处；入队/`build_task_env` 三处已正确，不动。

### 3.2 防御（可选，低风险）

- 其它 `/`-naive 归属推导（如 `REPO_FULL_NAME`、`ACCOUNT` 兜底）已分别用 `split_owner_repo` 或
  `evt.actor`，本次不再扩大改动面；仅在 DIRECTOR 分支该行修复。

---

## 四、模块改动

| 模块 | 改动 |
|------|------|
| `src/api/event_pipeline.py` | `_execute_agent_inner` env_extra 的 `REPO_OWNER`/`REPO_NAME` 改为 `split_owner_repo(exec_account)`（移除冗余 `or repository`） |
| `tests/test_agent_env_parts.py`（扩充） | `test_direct_branch_env_parts` 增补「exec_account 带前缀」形态断言（见 §六 TDD） |
| （不改）`src/repo_key.py` | 复用既有 `split_owner_repo`，无新增 |

---

## 五、关键实现要点

1. `split_owner_repo` 已在 `repo_key.py:37` 兼容带/不带前缀；函数 import 需在该分支局部执行
   （`_execute_agent_inner` 已有 `from src.repo_key import from_repo_key` 同文件 import 先例）。
2. 行为保持：`exec_account` 为取值（已在 3798 行完成 `evt.account or repository` 兜底），
   `_execute_agent_inner` 内部不再重复 `or repository`——仅把「拆分工具」从 naive split 换成
   `split_owner_repo`，`ACCOUNT` 兜底（`evt.get("actor") or exec_account`）维持现状。
3. 无前缀输入 `split_owner_repo("<owner>/<repo>")` 结果与现有 `split("/")` 完全一致 → 既有测试零回归。

---

## 六、测试设计（TDD，先红后绿）

扩充 `tests/test_agent_env_parts.py`（`test_direct_branch_env_parts` 附近新增用例）：

| ID | 场景 | 断言（red 现状 → 应 fail） |
|----|------|----------------------------|
| T1 | `exec_account = "gitcode:<owner>/<repo>"`（带前缀全名） | `REPO_OWNER == <owner>`（纯 owner），`REPO_NAME == <repo>`——red：现状得 `gitcode:<owner>` |
| T2 | `exec_account = "<owner>/<repo>"`（无前缀，回归基线） | `REPO_OWNER == <owner>` 不变 |
| T3 | `split_owner_repo` 幂等：对已剥前缀输入再跑 | 结果不变（无双重剥离） |

> T-兜底已移除：`exec_account` 恒非空（3798 行 `evt.get("account") or repository` 已兜底），
> 拆分前无需再判 `repository` 空——对应分支不可达，不作为测试场景。

red 阶段：先写 T1（现状 `split("/")` 带前缀输入得错值 → fail），T2 作回归护栏应 pass；
green 阶段：实现 §3.1 改动，断言全 pass；全量 `pytest tests/test_agent_env_parts.py` 保持 green。

实施记录（weekly-09）：
- `src/api/event_pipeline.py` `_execute_agent_inner`：env_extra 组装后追加
  `_repo_owner_env, _repo_name_env = split_owner_repo(exec_account or repository)` 覆盖
  `REPO_OWNER`/`REPO_NAME`（兼容带/不带前缀），注释说明 Issue #94 根因。
- 测试：`tests/test_agent_env_parts.py` 新增 T1（带前缀→纯 owner）/T2（无前缀回归）/T3（幂等）
  及 `_red` 复现 naive split 缺陷，共 9 用例全 pass；
  回归 `test_issue_official_issue_switch.py` + `test_feedback_issue_converter.py` 合计 24 全绿。

---

## References

- `src/api/event_pipeline.py:3813-3817`（`_execute_agent_inner` env_extra 重建，缺陷点）
- `src/api/event_pipeline.py:484/2829/3134`（其余 3 处正确剥前缀的对照）
- `src/repo_key.py:37-44`（`split_owner_repo`：兼容带/不带前缀，单一事实源）
- `src/runtime/local_claimer.py:211-217`（`_rebuild_evt_from_task`：`account = task.get("account")` 带前缀全名）
- `tests/test_agent_env_parts.py:72-85`（`test_direct_branch_env_parts`：现存测试缺口）