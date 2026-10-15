# Issue #338 方案设计（plan）

## 改动点（均为个人组织分支，非个人组织分支不动）

1. `get_agent_runtimes` 引擎循环（settings.py ~L599）：
   引擎行 org_id 非本人组织时，先查其凭据：若 `scope=user 且 owner_name=当前用户`（个人归属 worker 承载引擎）则放行，否则维持原 is_org_personal 判定。
2. `get_agent_runtimes` 设备视图 cred_map 过滤（~L694）：保留 `org_id ∈ {0, 本人组织}` 或 `scope=user 且 owner=当前用户` 的凭据。
3. `get_runtime_devices` cred_map 过滤（~L994）：同 2。

## 安全性论证
- 放行条件与 `_make_visible_credential_fn` 的 user scope 判定（owner_name == uname）同源且更严格（同时要求 scope=user）；
- 下游 `_visible_credential` 仍执行 owner 精确比对，非本人设备无法通过；
- 非个人组织（严格 org_id 隔离）分支未改动；
- 与 dashboard 个人组织视图既有可见性语义一致，无横向放大。

## 测试
tests/test_issue_338_worker_visibility.py：6 个用例覆盖
1. 本人个人组织视图可见 user scope worker 设备（含承载引擎）
2. agent-runtimes 引擎/devices 双视图均可见
3. global 设备/引擎不回退
4. 其他用户（bob）不可见（无权限放大）
5. 非个人组织严格隔离不回退
6. 过滤条件单元级防回归断言
