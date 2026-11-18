# Issue #3167 方案设计

## 问题定位

| 位置 | 现状 | 缺陷 |
|------|------|------|
| `settings.py:3001` | `(v.get("org_id") or "").strip() in ("", "0", "None")` | int `org_id` → `AttributeError` → 端点 500 |
| `settings.py:1014` | `if rt_org_id != _filter_org_id` | int 行 vs str 用户组织 → 静默不等，引擎被过滤 |
| `settings.py:1018` | `rt_org_id != _org_id` | 同上（个人组织分支） |
| `settings.py:1151/1153` | `(…get("org_id") or "") == _filter_org_id` | 同上（设备 cred_map / cached） |
| `settings.py:1164/1167` | `… == _org_id` / `in ("", _org_id)` | 同上（个人组织分支） |
| `settings.py:1681/1684` | 同上（agent-runtimes 非个人组织分支） | 同上 |
| `settings.py:1693/1694/1697` | 同上（agent-runtimes 个人组织分支） | 同上 |

`settings.py:1007` 的 `if rt_org_id in ("0", 0)` 是同一类型问题的手工局部补丁，
归一集中后不再需要。

## 设计

单一归一函数（模块级，供全部判定点复用）：

```python
def _org_key(v) -> str:
    """org_id 归一为可比较字符串（Issue #3167）。"""
    if isinstance(v, dict):
        v = v.get("org_id")
    if not v:                      # None / '' / 0 / 0.0：未归属哨兵族
        return ""
    key = str(v).strip()
    return "" if key in ("0", "None") else key
```

要点：

1. **`not v` 覆盖数值 0 与 `0.0`**：与旧实现 `(x or "")` 的假值语义逐值等价，
   避免 float/其他假值行在归一后行为漂移。
2. **`key in ("0", "None")`**：保留 TEXT 列历史遗留哨兵（迁移保值留下的
   `'0'`、旧代码写下的 `'None'`）与 `.strip()` 去空白语义（`" 0 "` → `""`）。
3. **不做数字解析**：真实组织 ID 一律按字符串比较，`5`/`"5"` 归一为 `"5"`，
   与 `user.org_id`（TEXT）恒等；不引入 `int()` 强转（会把 `''` 变异常，
   与 `939c3f105` 的写入侧口径冲突）。

调用点改造（端点内预计算，避免逐行转换）：

```python
_org_self_key   = _org_key(_org_id)       # 当前用户组织
_org_filter_key = _org_key(_filter_org_id)  # 严格隔离分支的目标组织（个人组织为 ""）
```

- 严格隔离分支：`not _org_unassigned(v) and _org_key(v) == _org_filter_key`
- 个人组织分支：`_org_unassigned(v) or _org_key(v) == _org_self_key`
- 设备缓存过滤：`_org_key(rt) == _org_filter_key` /
  `_org_key(rt) in ("", _org_self_key)`
- 逐行可见性：`rt_org_id = _org_key(rt)`，其后比较与 `is_org_personal(rt_org_id)`
  一律用归一值；`settings.py:1007` 的 `in ("0", 0)` 手工折叠由 `_org_key` 覆盖后删除。

不变量（保持不变）：

- 分支条件与哨兵集合不变，非个人组织仍严格隔离；
- #371/#430 repo scope 豁免、#338 个人归属 worker 豁免的**判定顺序与条件**不变；
- 不新增查询、不改 schema、不改写入侧。

## 测试设计

`tests/mgmt/test_issue_3167_org_unassigned_int.py`：

1. **六值单测**（Spec 验收）：`0` / `"0"` / `""` / `None` → `True`；
   `5` / `"5"` → `False`；补 `" 0 "` / `"None"` / `0.0` 哨兵族与 `" 5 "` 脏值。
2. **int/str 等价**：`_org_key({"org_id": 5}) == _org_key({"org_id": "5"}) == "5"`；
   `_org_key({"org_id": 0}) == _org_key({"org_id": ""}) == ""`。
3. **端点级回归**：种子个人组织 + `org_id` 为 int 的凭据/引擎行，断言
   `get_agent_runtimes()` / `get_runtime_devices()` 不抛 `AttributeError`
   且该引擎/设备可见（覆盖 500 根因 + 静默过滤）。

## 风险与回滚

| 风险 | 处置 |
|------|------|
| 归一后哨兵集合不全 → 误判 | 六值 + 哨兵族单测逐项锁死；集合与旧实现集合完全一致 |
| `'0'` 行在设备视图被放行 | 与 `_org_unassigned`、`settings.py:1004-1008` 注释语义一致（`'0'` 与 `''` 同义），非放宽 |
| 改动面扩大 | 仅类型归一，无分支/查询/豁免变化；单文件可整体回滚 |
