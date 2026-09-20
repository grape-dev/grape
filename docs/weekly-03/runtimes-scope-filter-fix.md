# 运行时管理页面筛选标签"个人"缺失与筛选失效问题分析与方案

## 一、问题定位

### 1.1 现象

1. 访问 `#runtimes` 页面第一页，筛选标签中没有"个人"选项，翻到其他页后才出现
2. 点击"个人"标签后，筛选结果不完整，只显示当前页的数据，漏掉了其他页的 user scope 引擎

### 1.2 根因

**前端筛选标签从当前页数据动态生成，筛选也是前端本地过滤——而后端按分页返回数据。**

#### 问题一：筛选标签缺失

前端 `runtimes.js` 第 274 行：

```js
const _hasScope = (sc) => {
    const list = _runtimeTab === 'engines' ? (data.runtimes||[]) : devices;
    return list.some(e => (e.scope || (e.is_local ? 'local' : 'global')) === sc || ...);
};
```

`data.runtimes` 是后端返回的**当前页数据**（如 page=1, page_size=20）。如果第一页 20 条数据中没有 `scope='user'` 的引擎，`_hasScope('user')` 返回 false，"个人"标签就不显示。

后端返回数据没有按 scope 排序，scope='user' 的引擎分散在各页中，只有翻到包含 user 引擎的页时标签才出现。

#### 问题二：筛选结果不完整

前端 `runtimes.js` 第 318 行：

```js
const filtered = runtimes.filter(rt => {
    // ... 仅过滤当前页的 runtimes 数组
    if (_rtFilterScope && (rt.scope || ...) !== _rtFilterScope) return false;
    ...
});
```

筛选是前端本地 filter，只过滤当前页数据。后端接口 `/settings/agent-runtimes` 不支持 `scope` 查询参数，点击"个人"后不会重新请求后端，所以只能看到当前页的 user 引擎，其他页的被漏掉。

#### 后端接口现状

```
GET /api/settings/agent-runtimes?page=1&page_size=20
```

后端只支持 `page` 和 `page_size` 参数，不支持 `scope` 参数。排序方式是全量过滤后按原始顺序切片，没有按 scope 分组排序。

---

## 二、解决方案

### 方案：后端支持 scope 筛选参数 + 前端改为服务端筛选

#### 2.1 后端：agent-runtimes 接口增加 scope 参数

文件：`src/api/routes/settings.py`，函数 `get_agent_runtimes`

```python
def get_agent_runtimes(user: dict = Depends(require_no_session_token),
                       page: int = 1, page_size: int = 0,
                       scope: str = Query("", description="按 scope 筛选: global/user/repo/local")):
```

在 visible_runtimes 构建完成后、分页之前，增加 scope 过滤：

```python
    # scope 筛选（服务端过滤）
    if scope:
        if scope == "local":
            visible_runtimes = [rt for rt in visible_runtimes if rt.get("is_local")]
        else:
            visible_runtimes = [rt for rt in visible_runtimes 
                              if (rt.get("scope") or "global") == scope]
```

返回数据中增加可用 scope 列表（供前端生成筛选标签，不再依赖当前页数据）：

```python
    # 返回所有可见引擎的 scope 分布（供前端生成筛选标签）
    _avail_scopes = list(set(
        ("local" if rt.get("is_local") else (rt.get("scope") or "global"))
        for rt in visible_runtimes
    ))
    
    # 分页
    total_runtimes = len(visible_runtimes)
    if page_size and page_size > 0:
        _page = max(page, 1)
        _offset = (_page - 1) * page_size
        paged_runtimes = visible_runtimes[_offset:_offset + page_size]
        return {"runtimes": paged_runtimes, "devices": devices,
                "total": total_runtimes, "page": _page, "page_size": page_size,
                "avail_scopes": _avail_scopes}
    return {"runtimes": visible_runtimes, "devices": devices, 
            "total": total_runtimes, "avail_scopes": _avail_scopes}
```

#### 2.2 前端：筛选标签改为服务端数据驱动 + 点击时重新请求后端

文件：`src/api/static/js/pages/runtimes.js`

**修改一：筛选标签从后端返回的 avail_scopes 生成**

修改前：
```js
const _hasScope = (sc) => {
    const list = _runtimeTab === 'engines' ? (data.runtimes||[]) : devices;
    return list.some(e => ...);
};
```

修改后：
```js
// 筛选标签从后端返回的 avail_scopes 生成（不再依赖当前页数据）
const _availScopes = data.avail_scopes || [];
const _hasScope = (sc) => _availScopes.includes(sc);
```

**修改二：点击 scope 标签时重新请求后端**

修改前：
```js
function _rtToggleScope(s) {
    _rtFilterScope = _rtFilterScope === s ? '' : s;
    // 仅触发前端重新渲染（本地过滤当前页数据）
    renderRuntimes(_rtLastData);
}
```

修改后：
```js
function _rtToggleScope(s) {
    _rtFilterScope = _rtFilterScope === s ? '' : s;
    _rtPage = 1;  // 重置到第一页
    _loadRuntimesData();  // 重新请求后端（带 scope 参数）
}
```

**修改三：_loadRuntimesData 传递 scope 参数**

修改前：
```js
function _loadRuntimesData() {
    var qs = '?page=' + _rtPage + '&page_size=' + _rtPageSize;
    API.get('/settings/agent-runtimes' + qs).then(function(d) { ... });
}
```

修改后：
```js
function _loadRuntimesData() {
    var qs = '?page=' + _rtPage + '&page_size=' + _rtPageSize;
    if (_rtFilterScope) qs += '&scope=' + encodeURIComponent(_rtFilterScope);
    API.get('/settings/agent-runtimes' + qs).then(function(d) { ... });
}
```

**修改四：renderEngines 中去掉前端 scope 过滤**

修改前：
```js
const filtered = runtimes.filter(rt => {
    if (_rtFilterScope && (rt.scope || (rt.is_local ? 'local' : 'global')) !== _rtFilterScope) return false;
    ...
});
```

修改后：
```js
const filtered = runtimes.filter(rt => {
    // scope 筛选已由后端处理，前端不再过滤
    // 保留类型筛选和搜索（这些仍可前端处理，或后续也移到后端）
    ...
});
```

---

## 三、修改清单

| 步骤 | 文件 | 修改内容 |
|------|------|---------|
| 1 | src/api/routes/settings.py | get_agent_runtimes 增加 scope 参数 + 返回 avail_scopes |
| 2 | src/api/static/js/pages/runtimes.js | 筛选标签从 avail_scopes 生成 |
| 3 | src/api/static/js/pages/runtimes.js | _rtToggleScope 改为重新请求后端 |
| 4 | src/api/static/js/pages/runtimes.js | _loadRuntimesData 传递 scope 参数 |
| 5 | src/api/static/js/pages/runtimes.js | renderEngines 去掉前端 scope 过滤 |

---

## 四、预期效果

1. 第一页就能看到所有可用 scope 标签（全局/仓库/个人/本机），因为标签来源从后端 avail_scopes 获取
2. 点击"个人"标签后，后端只返回 scope=user 的引擎，分页也是筛选后的分页，结果完整
3. 清除筛选后恢复全量分页

---

## 五、验证

1. 打开 #runtimes 第一页，确认"个人"标签可见
2. 点击"个人"，确认结果只包含 scope=user 的引擎，且 total 数正确
3. 翻页确认所有页都是 user scope 的引擎
4. 清除筛选，确认恢复全量数据
5. 确认其他筛选标签（类型、场景、标签）不受影响
