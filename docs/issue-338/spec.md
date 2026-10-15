# Issue #338 需求文档（spec）

## 问题
用户（如 songspace）在「Agent 运行时」页面：个人引擎可见，但名下 workers 设备不可见。

## 根因
worker 凭据（runtime_credentials）创建时绑定创建时刻所在组织的 `org_id`（个人组织上下文创建 → 0，公司组织上下文创建 → 该组织 id）。`/settings/agent-runtimes`、`/settings/runtime-devices` 两端点在「个人组织」视图按 `org_id ∈ {0, 本人个人组织}` 过滤凭据/引擎，把用户自己的 user scope worker 设备错误滤掉。

对照：dashboard 个人组织视图的既定语义为「global + 自己的 user scope worker」（见 dashboard.py worker 查询 SQL），settings 两端点遗漏了 user scope 的归属豁免。

## 目标
- 个人组织视图：scope=user 且 owner=当前用户的 worker 凭据/引擎不受 org_id 隔离，可见性与个人引擎一致
- 非个人组织：维持严格 org_id 隔离（不变）
- 非本人 user scope 设备仍不可见（`_visible_credential` owner 判定收敛，无权限放大）
- global 设备/引擎行为不回退

## 范围
- In：src/api/routes/settings.py（agent-runtimes / runtime-devices 两端点的组织过滤分支）
- Out：权限策略变更、workers 注册/注销流程、引擎健康检查
