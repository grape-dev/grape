# A2A 场景节点校验修复设计（Scene nodes 为单一事实源）

> 状态：设计方案（v1，2026-09-23）｜分支 weekly-04-lyz
> 关联：`src/api/routes/webhook.py`（`/api/a2a`）、`src/api/pipeline_config.py`、`help/api/06-scenes.md`
> 触发：生产事件 `Seq #2195`（gitcode:Jason7602/hello-calculator-13，issue #4）——场景已删 coding，但 A2A 仍转发 coding 并 failed

---

## 1. 背景与问题

### 1.1 现象

- issue #4 绑定项目场景 `cloud-native-cli`（应用市场场景所得，`scene_id=场景名`），其 `devops_pipeline_scene.nodes_json=[]`（用户已删除 coding 节点）。
- `/api/a2a` 交办 coding 时，场景节点校验**仍放行** → 创建事件 `Seq #2195`（agent=coding、skill=''）→ 执行失败（事件 status=failed、无 agent_tasks 行）。
- 预期：场景 nodes 已无 coding，A2A 应返回 `skipped: not_in_scene`，**不派发**（`nodes` 是流水线的单一事实源，见 `06-scenes.md`）。

### 1.2 生产实证

| 项 | 值 |
|----|-----|
| issue #4 场景 | `scene_id='cloud-native-cli'`（非 `custom-` 前缀） |
| 项目场景 nodes | `devops_pipeline_scene(scene_id='cloud-native-cli', repo_key)` 的 `nodes_json='[]'`（空） |
| 官方 `pipelines.json['scenes']['cloud-native-cli']` | 仍含 `coding`（vod→issue→coding→qa→sre） |
| A2A 校验结果 | `_agent_in_scene(coding)=True` → 放行 → 建 seq 2195 |
| seq 2195 | `failed`、skill=''（coding 节点/技能无法解析） |

### 1.3 问题本质

A2A 场景节点校验**把官方便利当成了真相源**，未以「项目场景行」为准：

```python
_scene_id_check = get_issue_scene(...).scene_id or "default"
if _scene_id_check.startswith("custom-"):
    ... 查 devops_pipeline_scene.scene_id(无 repo_key) → 项目场景 nodes
# else：走 official pipelines.json(scene_id 或 default) → 官方含 coding → 放行
```

- **`custom-` 前缀限定错误**：项目场景 `scene_id` 由应用市场时的「场景名」或 `custom-{uuid}` 两态（`scenes.py apply_market_scene`）；只认 `custom-` 前缀会把按名命名的项目场景漏判成官方。
- **DB 查询缺 `repo_key`**：跨仓库同名 `scene_id` 会取错项目场景行。
- **项目场景节点为空**：空 `nodes_json` 本应等于「任何 agent 都不在场景」，但当前分支根本不会走到项目场景行。

---

## 2. 目标

- `/api/a2a` 的场景节点校验**以「项目场景行」为单一事实源**；项目场景 nodes 为空 → 目标 agent 不在场景 → **拦截（不派发）**。
- 移除 `custom-` 前缀限定，统一：项目场景行优先 → 官方 → default。
- 不影响：官方/未应用场景的回退、agent 启用检查、A2A 鉴权。

---

## 3. 修复方案（`src/api/routes/webhook.py`）

替换场景节点校验块（L299-333）为三级解析：

```python
if issue_number:
    try:
        from src.session_manager import db, db_lock
        from src.api.session_store import get_issue_scene
        _scene_id_check, _ = get_issue_scene(repo_key, f"#{issue_number}")
        _scene_id_check = _scene_id_check or "default"
        _scene_nodes_check = None
        # 1) 项目场景行（单一事实源；带 repo_key；空 nodes 也认，视为「无节点」）
        with db_lock:
            c = db.cursor()
            c.execute("SELECT nodes_json FROM devops_pipeline_scene "
                      "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                      (_scene_id_check, repo_key))
            _row = c.fetchone()
        if _row and _row[0]:
            import json as _json
            _scene_nodes_check = _json.loads(_row[0])
        elif _row:
            _scene_nodes_check = []          # 项目场景存在但 nodes 为空
        # 2) 官方 pipelines.json
        if _scene_nodes_check is None:
            import os as _os, json as _json
            _pipe_path = _os.path.join(... "configs", "pipelines.json")
            with open(_pipe_path, "r", encoding="utf-8") as _f:
                _pipes = _json.load(_f)
            _scene_def = _pipes.get("scenes", {}).get(_scene_id_check) \
                or _pipes.get("scenes", {}).get("default") or {}
            _scene_nodes_check = _scene_def.get("nodes") or []
        # 3) 目标 agent 是否在场景 nodes
        _agent_in_scene = any(n.get("id") == agent_name for n in _scene_nodes_check)
        if not _agent_in_scene:
            log.info(f"A2A: agent '{agent_name}' not in scene '{_scene_id_check}' for issue=#{issue_number}, skipped (nodes={[n.get('id') for n in _scene_nodes_check]})")
            return JSONResponse(status_code=200, content={"status": "ok", "seq": 0, "agent": agent_name, "skipped": "not_in_scene", "scene": _scene_id_check})
    except Exception as _se:
        log.warning(f"A2A: scene node check failed: {_se}")
```

关键点：
- **项目场景行存在且 nodes 为空** → `nodes=[]` → 目标 agent 必不在场景 → 拦截（这正是 seq 2195 场景）。
- 项目场景行不存在 → 回退官方/default（原有回退语义保留）。
- **不再有 `custom-` 前缀分支**；`repo_key` 过滤补齐。

---

## 4. 验收标准

| # | 验收项 |
|---|--------|
| 1 | 项目场景 nodes 无 coding（含空 nodes）→ A2A 交办 coding 返回 `{skipped:"not_in_scene", seq:0}`，**不创建事件** |
| 2 | 项目场景 nodes 含 coding → 正常派发（行为不回归） |
| 3 | 官方场景（无项目场景行）→ 按官方 nodes 判定（回退语义不回归） |
| 4 | 跨仓库同 scene_id：按 repo_key 取各自项目场景，互不串扰 |
| 5 | agent 启用检查 / A2A 鉴权不变 |

## 5. 测试用例

| # | 用例 | 前置 | 断言 |
|---|------|------|------|
| T1 | 项目场景空 nodes → 拦截 coding | 造 issue.scene_id=sceneA；devops_pipeline_scene(sceneA,repo)=nodes[] | A2A 返回 skipped，无新事件 |
| T2 | 项目场景含 coding → 放行 | nodes=含 coding | 正常派发 |
| T3 | 无项目场景行 → 官方判定 | scene_id 官方含 coding | 放行（回退） |
| T4 | 跨仓库同名 scene_id 隔离 | 两仓库同 scene_id 不同 nodes | 各自按 repo_key 取 nodes |
| T5 | 既有 gitcode 技能 A2A 不回归 | 正常场景 | 事件创建成功 |

载体：`tests/test_a2a_scene_check.py`（mock `get_issue_scene`/DB 行）或并入 `test_a2a_queue.py`。

## 6. 风险与回退

| 风险 | 等级 | 缓解 |
|------|------|------|
| 官方回退语义变化 | Low | 仅改变「项目场景优先」顺序，官方仅在无项目场景行时兜底 |
| 空 nodes 误拦截（项目误删全部节点） | Med | 属预期（nodes=单事实源）；如需可后续加「允许跳过校验」开关 |
| 存量污染事件（seq 2195 类型） | Low | 数据修补脚本（可选，见 §8） |

回退：revert 该 commit；A2A 校验恢复现有逻辑。

## 7. 附带修正（可选）

- `help/api/06-scenes.md`：A2A 场景校验说明——「目标 agent 须在当前场景 nodes 内，否则 A2A 返回 `skipped: not_in_scene`」。

## 8. 待办/未决

- [ ] 已污染事件（如 seq 2195）是否清理/回填（`DELETE` 或标记）——评估后另立脚本。
- [ ] 空 nodes 场景是否需要管理端提示（用户删除全部节点后的 UX 预警）。