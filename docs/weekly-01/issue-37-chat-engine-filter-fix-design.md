# Issue #37 修复方案：Chat Agent 引擎配置泄露其他用户引擎

> Issue：https://gitcode.com/grape-dev/grape/issues/37
> 类型：P1 Bug | 状态：spec-ready（待 coding 实现）
> 作者：VOD/Issue Agent 根因分析 + 本地代码复核
> 日期：2026-09-05

## 一、问题概述

Chat Agent 的引擎配置下拉框出现了**非本机直连、且不属于当前登录用户**的引擎。
按设计意图（`chat.py` 注释「Chat 后端可选面：本机直连 + 个人引擎 scope=user，显式选择」），
下拉应只包含：

1. **本机直连引擎**（`credential_id=0`，已安装且健康）
2. **当前登录用户的个人引擎**（`scope='user'` + `status='active'` + `owner_name=当前用户`）

现状是**所有用户的 `scope='user'` 引擎都会出现在列表中**，存在越权可见（数据泄露）风险。

## 二、根因

`src/api/routes/chat.py` 的 `get_chat_config` 函数（依赖注入用户后，查询个人引擎部分）SQL **缺少两个过滤条件**：

```sql
-- 现状（缺过滤，→ 所有用户的个人引擎 + 已撤销凭据引擎都出现）
SELECT ar.id, ar.name, ar.version, rc.device_name, rc.owner_name, rc.id
FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
WHERE rc.scope = 'user'
```

→ 缺少 `rc.owner_name = ?`（用户归属过滤）
→ 缺少 `rc.status = 'active'`（凭据状态过滤）

## 三、参照实现（仓库内已有正确写法）

- `src/runtime/engine_select.py` `query_engines` 函数（~L410）：
  ```sql
  WHERE r.credential_id>0 AND rc.scope='user' AND rc.status='active' AND rc.owner_name=? ORDER BY r.id
  ```
  传入参数 `(username,)`。
- 同一 `get_chat_config` 函数内技能查询也正确按 `owner_id=?` 过滤。

> 凭据创建处 `src/api/routes/settings.py`（~L489）：user scope 凭据的 `owner_name` 记录为**当前用户 username**，`owner_id` 留空。因此按 `owner_name` 过滤即可。

## 四、修复方案

改动位置：`src/api/routes/chat.py` → `get_chat_config` 内个人引擎查询 SQL。

**修改后 SQL**：

```sql
SELECT ar.id, ar.name, ar.version, rc.device_name, rc.owner_name, rc.id
FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
WHERE rc.scope = 'user' AND rc.status='active' AND rc.owner_name = ?
```

**传参**：`(user.get("username") or "",)`。

（`user` 为 `get_current_user` 依赖注入结果，含 `username` 字段。）

## 五、改动与测试清单

| # | 项 | 说明 |
|---|----|------|
| 1 | `src/api/routes/chat.py` `get_chat_config` | SQL 加 2 个过滤条件 + 传参 |
| 2 | 新增单测 | 覆盖：仅返回当前用户 `scope=user & active` 引擎；他人引擎不出现；revoked 引擎不出现 |
| 3 | 现有 `test_get_config` | 回归：`_backends` 结构不变（name/display_name/scope/owner/runtime_id） |

## 六、验收标准

- [ ] Chat Agent 引擎下拉只显示本机直连 + 当前用户个人引擎
- [ ] 其他用户的个人引擎不再出现
- [ ] 已撤销（revoked）凭据的引擎不再出现
- [ ] 现有测试 `test_get_config` 通过（`_backends` 结构不变）
- [ ] 新增过滤逻辑单测通过

## 七、风险

- 低风险：仅收紧 SQL 查询条件，不影响本机直连引擎（`credential_id=0`）逻辑，不触碰写操作。
- `username` 为空时查询返回空列表 → 可接受（等价于「未登录个人引擎」）。
- 与 `engine_select.py:410` 语义保持一致，后续可考虑抽公共查询，本期不做（out of scope）。

## 八、处理流水线状态

- ✅ VOD Agent：根因分析已结构化（comment id=188297775）
- ✅ Issue Agent：四维可开发性分析 → ready，Spec 已发布（comment id=188297946）
- ⚠️ A2A 交办 coding：服务端返回 `skipped:not_in_scene`（场景 `custom-e7ffba11` 未注册 coding agent）
  → **尚无人实际开发**，本方案即 coding 可直接落地的实施依据。

## 附：Bug 代码原文位置

`src/api/routes/chat.py` `get_chat_config`：
```
约 L1036-1040（new-version3 分支当前版本）：
  c.execute("""
      SELECT ar.id, ar.name, ar.version, rc.device_name, rc.owner_name, rc.id
      FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id
      WHERE rc.scope = 'user'
  """)
```