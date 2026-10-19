# 方案：需求中心自家建需求不可见（#244 org_id 归属错误）修复

> ⚠️ 本文档已按开源发布脱敏规范处理：真实 GitCode 用户名、仓库名、服务器 IP、组织名/组织 slug
> 均替换为 `<占位符>`；issue 编号、org 数字 id 等非身份标识仅保留结构性引用，不含明文凭据。

> 状态：待实施（2026-10-15)｜目标分支：weekly-07-lyz（单机生产部署分支）
> 事件：`issue.id=244`（repo `gitcode:<gitcode用户名>/<示例仓库>`、GitCode #4、creator=<gitcode用户名>/user_id=7）
> 在创建者的需求中心看不到；DB 实证其 `org_id=32`，而同仓库其它卡片为 `org_id=2`，
> org 隔离过滤（mine）将其隐藏。
> 核心判断：**`issue.org_id` 保留为一等字段**——未来支持「组织级需求」（可不关联具体仓库），
> 此时 org 是需求的独立归属，不从仓库推导；但对「已关联仓库」的需求，org 默认继承仓库托管
> `managed_repos.org_id`（单一来源），**绝不允许用请求上下文 org 静默写库**。

---

## 一、问题现象与证据（单机生产 <单机生产IP>）

`issue` 表（权威卡片）：

| 字段 | 值 | 判断 |
|------|-----|------|
| id | 244 | 卡片存在 |
| user_id | 7（unified_user_mapping 7=<gitcode用户名>） | ✅ 创建人正确 |
| assignee | <gitcode用户名> | ✅ |
| repo_key / issue_number | `gitcode:<gitcode用户名>/<示例仓库>` / GitCode `#4` | ✅ |
| **org_id** | **32** | ❌ 异常（同仓库 id129/232 均为 **2**） |
| status / stage | processing / vod | 正常 |

组织归属对照：

- `organization`：`id=2` `<个人组织>`（slug `personal-<创建者id>`，owner=**7**，`is_personal=1`）；`id=31` <团队组织>（owner=7，is_personal=0）；`id=32` `slug=personal-<他人id>`（owner=**2**，is_personal=1，2026-10-15 08:35 新建——恰在 #244 创建 09:07 前半小时）。
- `org_member`：user 7 ∈ org {2, 31}；**user 7 ∉ org 32**。
- `managed_repos`：`<示例仓库>` org_id=**2**。

需求中心 `mine`（issues.py:1280-1288）：`user_id=当前用户`；若当前组织非 personal（如团队 org 31）则追加 `org_id=当前组织` → **#244（org 32）被过滤**，看不到。

---

## 二、根因

`create_task_via_issue`（issues_center.py:1370）写卡时：

```python
_org_id = user.get("org_id", 0)                 # ← 请求上下文组织
... ensure_issue_task(..., org_id=_org_id)      # ← 直接透传
```

`ensure_issue_task` 仅当 `org_id==0` 才回退 `managed_repos.org_id`（issues_center.py:255-264）。于是：

- 请求上下文的 `user["org_id"]` 被**优先信任**；
- 该请求的 org 上下文解析到了 org 32（一个刚创建的、属于 user 2 的 personal 空间，display 名还误植成 "<异常个人组织>"），与仓库托管 org（2）和创建者个人 org（2）都不一致；
- 卡片以错误 org_id=32 落库 → 创建者在团队/正确组织视图的 `mine` 中不可见。

**本质：`issue.org_id` 是需求的一等归属字段（保留），但「已关联仓库的需求」其 org 必须来自仓库托管
`managed_repos.org_id`；`create_task` 用请求上下文 org 写入是非法旁路，导致缓存/归属漂移（#244）。**

> 进一步分析（org_id 保留为一等字段的语义）：
> - **org 是仓库的属性，也是需求的独立维度**：仓库↔组织绑定在 `managed_repos.org_id`；但对「组织级需求」
>   （未来可不关联具体仓库，`repo_key` 为空/可选），org 无法从仓库推导，必须作为显式一等字段存在 →
>   **保留 `issue.org_id` 列**，放弃「删列/JOIN 化」方向。
> - **请求上下文的 org 口径是「查看过滤」，不是归属**：`user["org_id"]` 来自 `require_org_member`（当前选中的
>   工作空间），仅用于 `mine` 等按当前组织过滤；personal 视图本来就不按 org 过滤。它永远不该作为默认写进 issue。
> - **写入策略按需求类型二分**：
>   - repo 关联需求：org = `managed_repos.org_id`（仓库权威，单一来源），不读请求 org；
>   - 组织级需求（未来）：org = 显式选定的组织，必须经归属性校验（`org_member`/创建权限），防串货。
>   - 两者都禁止「当前工作空间 org 静默写库」这条共享旁路。

---

## 三、修复方案

### 方案 A（数据纠正，先行）：#244 及其同类卡片 org 归属校准

```sql
-- 按「issue.org_id ≠ 仓库托管 org」校准为仓库托管 org（唯一来源 = managed_repos.org_id）
UPDATE issue
SET org_id = (SELECT org_id FROM managed_repos mr WHERE mr.repo_key = issue.repo_key LIMIT 1)
WHERE org_id != COALESCE((SELECT org_id FROM managed_repos mr2 WHERE mr2.repo_key = issue.repo_key LIMIT 1), -1)
  AND EXISTS (SELECT 1 FROM managed_repos mr3 WHERE mr3.repo_key = issue.repo_key AND mr3.org_id > 0);
```

- 校准口径 = **仓库托管 org**（不再以"是否属于创建者"为准——org 本就来自 repo，不来自人）。
- 连同排查 `org_id=32` 的其它卡片（同批次/同路径创建）一并校准。
- org 32 幽灵组织（display "<异常个人组织>" 但 owner=2）单独核实，必要时在组织侧清理。

### 方案 B（代码加固）：org 写入按「是否关联仓库」二分，杜绝请求 org 静默写库

**原则：保留 `issue.org_id` 一等字段；repo 关联需求只认仓库 org；org 级需求（未来）走显式 org + 归属性校验。**

```python
def _resolve_issue_org_id(repo_key: str, explicit_org: int = 0) -> int:
    """解析需求归属组织。

    - repo 关联需求（repo_key 非空）：org = managed_repos.org_id（仓库权威，单一来源）；
      绝不取请求上下文 org。
    - 组织级需求（未来，repo_key 空且有 explicit_org）：校验请求者有该 org 成员/创建权限后
      采用 explicit_org（防串货），无权限 → 拒绝或回退 0。
    - 无托管记录 / 无显式 org → 0（个人视图豁免 org 过滤，创建者仍可见）。
    """
    from src.user.org_db import get_org_id_by_repo_key
    if repo_key:
        return get_org_id_by_repo_key(repo_key) or 0
    if explicit_org:
        # org 级需求：显式 org 必须通过请求者归属性校验（此处由调用方传校验结果/抛错）
        return explicit_org
    return 0
```

- `create_task_via_issue` 删除 `_org_id = user.get("org_id", 0)` 写入路径，改经 `_resolve_issue_org_id(repo_key)`（repo 场景忽略请求 org）。
- `ensure_issue_task` 显式 org 只接受仓库值；「org_id=0 → managed_repos」兜底保留（其它调用方不受影响）。
- 预留组织级需求入口：待未来实现时经 `explicit_org` + `require_org_member` 校验后落库。
- 请求 org 只留作查询过滤（`mine`/列表侧 `require_org_member` 口径不变；personal 视图不过滤）。

### 方案 C（观测/自愈，可选）：repo 关联需求 org 一致性核对

- 写入或周期核对时，若 repo 关联却 `issue.org_id != managed_repos.org_id` → 校准为仓库值 + 打 `WARN`（`issue=#{n} repo={} drifts to {repo_org}`），防止未来任何绕过路径再次漂移。
- 可作为运维定时任务（复用 main.py 周期 purge 同款循环）。与「组织级需求（无 repo）」不冲突：后者不参与本核对。

---

## 四、各模块改动

| 模块 | 改动 |
|------|------|
| `src/api/issues_center.py` | 新增 `_resolve_issue_org_id(repo_key, explicit_org=0)`（repo 场景只读 `managed_repos.org_id`；org 级预留显式 org + 校验）；`create_task_via_issue` 删除 `user.get("org_id")` 写入路径（方案 B） |
| `data`（单机生产 SQLite / 集群 MySQL） | 方案 A 校准脚本：repo 关联需求 `issue.org_id` 校准为仓库托管 org；排查 org 32 批次 |
| `src/api/main.py`（可选） | 方案 C：周期一致性核对（repo 关联需求 `issue.org_id != managed_repos.org_id` → 校准 + WARN） |
| `tests/` | 新增：repo 关联写库 org == `managed_repos.org_id`（请求 org=32 不生效）；repo 无托管 → org=0；create_task 不再读请求 org；explicit_org 无权限被拒（org 级预留） |

---

## 五、影响面 / 兼容性 / 安全

| 项 | 说明 |
|----|------|
| 兼容 | 仅影响「org_id 写入来源」；正常路径（仓库有托管 org）结果与预期一致（= 仓库 org） |
| 隔离 | org 边界由仓库托管关系唯一决定，杜绝「请求 org 串货」这类旁路 |
| 现有卡片 | 个人组织视图（personal）不受 org 过滤影响，任何 org 都可见 |
| 历史 | 方案 A 只校准 `org_id != 仓库 org` 的异常卡片；仓库换 org 的历史需求按「跟随仓库」语义随方案 A/整体更新 |

---

## 六、验证

| 用例 | 断言 |
|------|------|
| UT：`_resolve_issue_org_id(repo 托管 org=2)` | 返回 2（无论请求上下文 org 为何） |
| UT：repo 无托管记录 | 返回 0（个人视图仍可见） |
| UT：create_task 写库流程 | 不再出现 `user.get("org_id")` 写入；repo 关联 `issue.org_id = managed_repos.org_id` |
| UT：请求 org=32 干扰 | 写库 org 仍 = 2（请求 org 不生效） |
| UT（org 级预留）：`explicit_org` 无成员权限 | 拒绝/回落，不写库串货 |
| E2E（单机生产） | 校准后 <gitcode用户名> 在需求中心（个人/默认视图）能看到 #244；团队 org31 视图按仓库归 2 决定 |
| 回归 | 新建 repo 关联需求 org = 仓库 org；同仓库其它卡片不变；全量套件 branch-only 失败=0 |

---

## 七、风险提示 / 待办留档

- **部署顺序**：先跑方案 A（数据校准）→ 再发方案 B（及可选 C），1~2 个提交分别评审。
- 方案 A 是 SQLite/MySQL 双方言手写 UPDATE，先在备份库演练；执行前 `cp data/sessions.db` 备份。
- org 32 幽灵组织（display "<异常个人组织>" / owner=2 / slug personal-<他人id>）需单独排查来源（疑似预植/命名串扰），可能还有同批多个 personal 空间。
- **`issue.org_id` 保留为一等字段**（组织级需求未来可无仓库），不做删列/JOIN 化；
  未来实现组织级需求时按「显式 org + 归属性校验」落库。
- 与「双事件去重」「阶段停 issue」问题相互独立，不互相依赖。