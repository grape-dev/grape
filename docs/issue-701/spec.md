# Spec — Issue #701 worker 名称展示统一（凭据自定义名唯一来源）

## 目标
全平台所有用户可见的 worker/设备名称展示统一只取 `runtime_credentials.name`（凭据自定义名）；
移除 `rc.name` 为空时回退 `device_name`/`host_name` 的逻辑；空名显示占位符「未命名设备」。
为 #465（PR #650/#655）的延伸收口。

## 范围
- In：前端 worker 名称展示位、后端展示名下发口径、#465 修复处回退移除、测试补齐与回归。
- Out：凭据名称录入流程、`device_name` 字段/DB 删除迁移、引擎实例唯一名 `instance_name()`
  （`{engine}-{device|credN}` 派生标识，被 `resolve_instance_id`/`events.runtime` 回写匹配依赖，
  属标识符而非展示名，本 Issue 不动）、本机直连引擎（credential_id=0，无凭据）的 host_name 标注。

## 验收
- 展示位置清单（见 plan.md）逐项改造。
- 所有展示位取 `rc.name`，无 device_name/host_name 回退。
- `rc.name` 空时显示「未命名设备」（i18n：`common.unnamed_worker`）。
- #465 既有用例更新回归通过；新增统一取值函数与空名场景测试通过。
