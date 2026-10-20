# 解除托管权限控制方案（unmanage-permission-guard-design）

## 一、事故回放（发生了什么）

用户对 agent 说：「帮我把所有仓库的托管都解除掉」。

agent 的执行链是：

1. 从平台的用户侧接口读出「这个用户能看到的所有仓库」——GET /repos/managed（`list_managed_repos`）。
2. 对每个仓库调用 POST /repos/remove（`remove_managed_repo`）。
3. 每调用一次就删一条 `managed_repos` 行、拆 webhook、把 bot 从仓库协作者里移除、
   并触发 Issue #340 的级联清理（Issue 卡片、流水线、场景、知识基线等全部连带删除）。

**问题出在第 1 步和第 2 步之间没有闸门：**

- 「能看到」≠「有权解除」。个人组织的口径（docs/weekly-06 方案 A）下，
  /repos/managed 返回的不只是自己组织托管的仓库，还包括托管在其他个人组织的仓库
  （VCS 协作者同步、甚至从未实际工作过的仓库也会出现在列表里）。
- POST /repos/remove 只校验 `require_no_session_token`（挡住 agent 的 tk_ 会话 token，
  API Key 和 Web JWT 都放行）+ `check_repo_access` 的**读口径**（managed_repos 里存在即通过，
  是"能看"的口径，不是"能删"的口径）。
- 于是一个拿着 API Key 的 agent，把列表里 20+ 个仓库挨个调了一遍 /repos/remove，
  包括共同参与的、甚至从没工作过的仓库——每个都删成功了。

一句话总结根因：**读权限（能看到仓库）和写权限（能解除托管）共用同一个校验，
且「批量解除」没有任何二次确认或数量闸门。**

## 二、方案总览（三道闸门，层层收紧）

原则：不做兜底，直接拒绝。agent 拿不准就该停下来问人，而不是替人猜。

```
请求 POST /repos/remove
        │
        ▼
闸门一：角色校验 —— 只有"我的仓库"才能解除
        managed_repos 行的归属（org/owner 维度）vs 当前用户角色
        共同参与的仓库（user_repos.is_admin=0）→ 403
        │
        ▼
闸门二：单次上限 —— 一次只允许解除一个
        防批量遍历。API 无批量端点，单次调用上限=1（现状即如此，方案里显式固化）
        │
        ▼
闸门三：高危确认 —— 高危上下文要求显式确认字段
        auth_type=api_key（agent 凭据）时，body 必须携带 confirm: true
        并逐仓回显影响范围；JWT 网页端维持现状（有 UI 确认弹窗）
        │
        ▼
        执行（现有级联清理不变）+ 审计日志增强
```

## 三、详细设计

### 3.1 闸门一：角色校验（核心，堵住本次事故）

**现状**：`remove_managed_repo`（src/api/routes/repos.py）拿到 repo_key 后直接进入
token 分流、webhook 拆除、级联清理，唯一校验是 `check_repo_access`——
它只查 `user_db.get_managed_repos(uid)` 里有没有这个 repo_key，即"用户能看到"。

**改动**：在 /repos/remove 入口（provider 分流之前）加一个 `_check_unmanage_permission`：

判定规则（从宽到严）：

| 用户与仓库的关系 | 判定方式 | 能否解除托管 |
|---|---|---|
| 自己个人组织托管的仓库 | managed_repos.org_id 是用户的个人组织 | ✅ 可以 |
| 自己团队组织托管的仓库，且用户是该组织的 Admin | org 成员表 org_role=admin | ✅ 可以 |
| 共同参与（VCS 协作者同步进来的） | user_repos.is_admin=0 | ❌ 403，提示"仅仓库归属方可解除托管" |
| 从未工作过、仅因大堂口径可见 | user_repos 无 is_admin=1 行 | ❌ 403 |

具体实现要点：

1. 查 `managed_repos.org_id`，与用户当前 org_id 比对：
   - 同组织（团队组织）→ 查用户在组织内的角色，Admin 放行，Member 拒绝；
   - 是个人组织且属于本人 → 放行（自己托管的）；
   - 其他情况（托管在别人的组织）→ 拒绝，错误信息明确：
     `"该仓库由他人托管，仅仓库归属方可解除（当前身份：协作者/访客）"`。
2. `user_repos.is_admin` 只作为 VCS 侧的参考信号，**不作为放行依据**
   （协作者同步时 is_admin=1 的行也存在，见 get_managed_repos 注释，
   但托管归属判定以平台 org 维度为准，VCS 协作者不等于平台归属者）。
3. 该校验同时适用于 gitcode: / github: 两个 repo_key 候选确定之后、任何副作用之前。

预期效果：本次事故中的「共同参与的仓库」「从没工作过的仓库」全部被 403 挡下；
用户只能解除真正归属于自己（或自己是 Admin 的团队组织）的仓库。

### 3.2 闸门二：单次上限

现状 /repos/remove 是单仓接口，风险在于 agent 会自己循环调用。

**改动**：
1. 显式在接口文档（summary/description）声明：本接口单次仅处理一个仓库，
   无批量能力，任何批量需求必须逐仓由用户确认。
2. 新增轻量速率闸门：同一用户对 /repos/remove 的调用，
   滚动窗口内（默认 5 分钟）最多 N 次（默认 3，配置项 `UNMANAGE_RATE_LIMIT`，
   源码默认空走环境变量，遵循主人惯例）。超限返回 429：
   `"解除托管操作过于频繁，如确需批量解除请逐仓确认后在网页端操作"`。
3. 实现：复用 `distributed_locks` 表存窗口计数（键 `unmanage:{uid}`），
   避免引入新组件；单机部署退化为同表同行，语义不变。

### 3.3 闸门三：高危确认（agent 凭据通道）

**现状**：`require_no_session_token` 挡了 tk_（agent 会话 token），但 `grape_` API Key
畅通无阻——本次事故 agent 大概率就是拿 API Key 调的。

**改动**：当 `auth_type == "api_key"` 时，/repos/remove 要求：

1. body 增加 `confirm: bool = False` 字段；`confirm != True` → 400，
   错误信息附上该仓库的**影响范围清单**（复用 cascade_cleanup 的 dry_run 能力）：
   ```
   即将解除 {owner}/{repo} 并级联删除：Issue 卡片 N 条、流水线场景 M 个、
   知识基线 X 份……请确认后携带 confirm=true 重试
   ```
2. 这是给 agent 的"停下来问人"信号：agent 看到影响清单 + 确认要求，
   正确行为是回到用户面前确认，而不是默默重试。
3. JWT（网页端）不加 confirm 要求（UI 已有删除确认弹窗，不重复打扰）。
4. tk_ session token 维持 403（require_no_session_token 现状不变）。

### 3.4 审计与恢复兜底（留痕）

1. /repos/remove 成功后，在现有的 Issue #340 级联清理审计之外，
   追加一条独立的 `unmanage_audit` 记录：操作者 uid/username、auth_type、
   repo_key、时间、级联清理统计摘要、请求来源 IP。
2. 不做"撤销"功能（失败不兜底原则）；但审计行让事后追溯"谁在什么时候用什么凭据解除了什么"
   有完整证据链，配合 GitCode 侧 webhook 重建（repair_host 已有能力）可人工恢复托管关系。
3. 审计表 DDL 追加进 db_setup（幂等 CREATE TABLE IF NOT EXISTS）。

## 四、改动清单（实施范围）

| 文件 | 改动 | 预估行数 |
|---|---|---|
| src/api/routes/repos.py | /repos/remove 增加闸门一角色校验 + 闸门三 confirm 字段 + 审计写入 | ~80 |
| src/api/deps.py | 无改动（复用现有依赖） | 0 |
| src/user/db.py | 新增 unmanage_audit 表 DDL + 写入/查询函数 | ~40 |
| src/api/routes/repos.py | 闸门二速率计数（复用 distributed_locks） | ~30 |
| tests/test_api_routes.py | 新增用例：协作者 403 / 非归属 403 / api_key 无 confirm 400 / 速率 429 / 正常归属放行 | ~120 |
| docs/weekly-08/ | 本设计文档 | 本文 |

总改动约 270 行（不含文档），集中在 /repos/remove 一条路径，不触碰其他接口。

## 五、测试计划

只跑相关文件（主人惯例）：

```
pytest tests/test_api_routes.py -k "remove or unmanage" -x
pytest tests/test_security_hardening.py -x
```

用例矩阵：

| 场景 | 期望 |
|---|---|
| 本人个人组织托管的仓库，JWT | 200 正常解除 |
| 团队组织 Admin，JWT | 200 正常解除 |
| 团队组织 Member（协作者），JWT | 403 |
| 个人组织 B 托管、A 可见（大堂口径），A 发起 | 403 |
| api_key 无 confirm | 400 + 影响清单 |
| api_key + confirm=true + 归属者 | 200 |
| tk_ session token | 403（现状维持） |
| 5 分钟内第 4 次调用 | 429 |

## 六、兼容性影响评估

1. **网页端正常用户**：归属者解除托管 → 行为不变；协作者本来在 UI 上就不该能解除
   （前端按 is_admin 隐藏按钮，本次是后端补齐），无感知。
2. **agent**：拿 API Key 解除"自己真正拥有的仓库"需要两步（先看影响清单再 confirm），
   多一次交互，属预期收紧；解除他人仓库直接 403，无法批量。
3. **CLI/自动化脚本**：如内部脚本依赖 /repos/remove，需加 confirm 字段——
   平台内 scripts/ 已 grep 确认无调用方，影响面为零。
4. **rate limit**：单机部署分布式锁退化为本地行锁，语义一致。

## 七、待主人确认的点

1. 速率窗口（5 分钟 3 次）数值是否合适？
2. api_key 通道的 confirm 机制是否也要覆盖 PUT /repos/bot-token、PUT /repos/deploy-url
   等其他托管变更操作（本方案先只做 /repos/remove，最小化改动）？
3. 审计表是否需要平台 Admin 可见的查询接口（本期只落表+写入，查询接口下期）？
