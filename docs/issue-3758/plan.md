# Plan: Issue #3758

## 方案

### 1. repogit 新增 `resolve_work_branch`

- 入口：`resolve_work_branch(repo_key, configured, token="") -> (effective, note)`。
- 数据源：VCS 分支列表（经 `VcsApiClient`，平台卡驱动，支持 gitcode/github/gitlab 等）。
- 校验语义：
  - 分支在列表内 → 原样返回，`note=""`；
  - 显式确认不在列表 → 回退默认分支（API `default_branch` → `main`/`master`），返回含原配置值 / 仓库 / 生效分支的 note；
  - 不可核实（无 token、请求失败、分页截断）→ 保持原配置，`note=""`。
- 本地 bare mirror 优先（无网络开销）；mirror 超 TTL 先 `fetch --prune`；mirror 未命中目标分支时强制走远端复核，避免「平台外新建分支」被误判不存在。
- 结果按 repo 缓存（成功 90s / 失败 30s）。

### 2. webhook 调用点对齐

- `webhook.py` 顶部模块级导入 `resolve_work_branch`：符号缺失时进程启动即失败（fail-fast），不再被请求内 `except Exception` 吞掉。
- 调用点异常捕获收窄到网络类（`OSError`/`RuntimeError`/`TimeoutError`），保持原配置；`ImportError`/`AttributeError` 等代码缺陷直接暴露。

### 3. 测试 seam

- seam 定在 `resolve_work_branch` 公开入口与 `_cross_pipeline_handoff` 交办结果（`carried_context` / 事件 payload）。
- 单测 monkeypatch `repogit.list_repo_branches` 返回固定分支集合 / `None`，覆盖回退与不可核实两条路径，不触网。
- 导入冒烟断言 `webhook.resolve_work_branch is repogit.resolve_work_branch`。

## 风险

| 风险 | 缓解 |
|------|------|
| 分支列表查询失败误回退 | 仅显式确认不存在才回退；失败/截断保持原配置 |
| 本地 mirror 陈旧误判 | 超 TTL fetch；未命中强制远端复核 |
| 校验入口两套口径 | 收敛到 repogit 单一公开入口 |