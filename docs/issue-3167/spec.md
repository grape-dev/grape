# Issue #3167 Spec：引擎/设备页 500 —— `_org_unassigned` 对 int `org_id` 调用 `.strip()`

> 来源：Issue #3167（承接 #3165）｜目标分支 `arch-weekly-11`｜优先级 P1

## 背景

`GET /api/settings/agent-runtimes` 与 `GET /api/settings/runtime-devices` 在
「个人组织视图 + 历史库（`org_id` 以 integer 存储）」条件下必现 500：

```
AttributeError: 'int' object has no attribute 'strip'
```

根因在 `src/domains/mgmt/routes/settings.py`：

1. `_org_unassigned(v)`（`settings.py:2997`）实现为
   `(v.get("org_id") or "").strip() in ("", "0", "None")`，历史库 integer `org_id`
   行走到这里即抛 `AttributeError` → 端点 500，整个引擎/设备页不可用；
2. 同文件其余可见性比较 `(v.get("org_id") or "") == _org_id` / `== _filter_org_id`
   不报错但**静默不等**（`'2' == 2` 为 `False`），把本应可见的引擎/设备过滤掉，
   比 500 更难发现（同源类型缺陷）；
3. 写入侧已由 `939c3f105` 治理（新行写 `''`、不再 `int()` 强转），但历史 integer
   行只能靠读取侧兜住。

## 目标

- 两个端点在上述环境下返回 200，日志不再出现 `'int' object has no attribute 'strip'`；
- 同一组织下 int 型与 str 型 `org_id` 的可见性判定完全一致，引擎/设备不被静默过滤。

## 范围

### In Scope

- `src/domains/mgmt/routes/settings.py`
  - 新增 `_org_key(v)`：`org_id` 归一为可比较字符串（接受行 dict 或裸值）；
  - `_org_unassigned()` 改为 `_org_key(v) == ""`，保留哨兵族（`''`/`'0'`/`'None'`/
    `0`/`None`）同权语义与 `.strip()` 去空白语义；
  - 统一 `get_agent_runtimes` / `get_runtime_devices` 内 `org_id` 可见性比较口径
    （`settings.py` 内全部 "行/凭据 org_id == 当前用户 org_id" 判定点）。
- `tests/mgmt/test_issue_3167_org_unassigned_int.py`：`_org_unassigned` 六值单测 +
  `_org_key` int/str 等价 + 端点级回归（个人组织 + 历史 int 行不再 500 且可见）。

### Out of Scope

- 历史 integer `org_id` 的一次性数据迁移（可选加固，独立跟进）；
- 写入侧归一（已由 `939c3f105` 完成）；
- 非个人组织严格隔离策略与 Issue #338 / #371 / #430 的 repo scope 豁免口径调整；
- 前端错误提示文案与其它 settings 端点。

## 方案

读取侧类型归一：所有 `org_id` 先归一为字符串再判定哨兵/归属，一处实现、多点复用。

```python
def _org_key(v) -> str:
    if isinstance(v, dict):
        v = v.get("org_id")
    if not v:                      # None / '' / 0 / 0.0 → 未归属哨兵
        return ""
    key = str(v).strip()
    return "" if key in ("0", "None") else key
```

- 哨兵族折叠为 `""`：`0`、`"0"`、`" 0 "`、`""`、`None`、`"None"` 一律 `""`
  （与历史 `_org_unassigned` 判定集合同权，`0` 与 `"0"` 必须同权）；
- 真实归属值归一为去空白字符串：`5` / `"5"` / `" 5 "` 一律 `"5"`，与
  `user.org_id`（TEXT）比较恒等；
- `_org_unassigned(v)` = `_org_key(v) == ""`，保留原注释语义，消除 `.strip()` 崩溃点。

调用点统一为 `_org_key(行) == _org_key(当前用户 org_id)`；端点内预计算
`_org_self_key` / `_org_filter_key` 避免逐行重复转换。分支条件、哨兵集合、
repo scope 豁免（#371/#430）与个人归属 worker 豁免（#338）一律不变。

## 验收标准

- [ ] 个人组织视图 + 历史 integer `org_id` 库，`GET /api/settings/agent-runtimes`
      与 `GET /api/settings/runtime-devices` 均 200，无 `'int' object has no attribute 'strip'`。
- [ ] `_org_unassigned`：`{"org_id": 0}` / `{"org_id": "0"}` / `{"org_id": ""}` /
      `{"org_id": None}` 均 `True`；`{"org_id": 5}` / `{"org_id": "5"}` 均 `False`。
- [ ] 同一组织下 int 型与 str 型 `org_id` 可见性判定一致（端点级断言）。
- [ ] 非个人组织严格隔离与 #338/#371/#430 repo scope 豁免不回归。
- [ ] 现有相关测试全量通过。
