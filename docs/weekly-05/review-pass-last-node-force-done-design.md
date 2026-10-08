# 方案：review-pass 末节点触达时强制归一 done（target=processing → done）

> 状态：技术方案（2026-10-07）｜目标分支：weekly-05-lyz
> 动机：`review-pass` 的 `target=processing` 语义是「用户过审、流水线继续跑后续节点」。若当前 `issue.stage` 已是**流水线最后一个节点**（`next="done"`），没有后续节点可续——`status=processing` 后无人推进，需求卡片永久卡在「进行中」。应在**末节点可推定**时把误传的 `processing` 归一为 `done`。
> 范围：仅 `review-pass`；`review-reject` 的 `processing`（打回继续改）在末节点仍有意义，**不归一**。

---

## 一、现状与依据（代码核对 2026-10-07）

### 1.1 现状：`_review_pass` 不感知流水线位置

`issues_center.py _review_pass(repo_key, issue_number, target, evidence)`：
- `target == "done"` → `status=completed` + `completed_reason='review_passed'` + `completed_at` + `set_issue_stage(..., 'done')` → 返回 `passed_done`；
- 否则（含默认 `processing`）→ `status=processing`、清 `completed_reason/completed_at` → 返回 `passed_processing`；
- 非 `pending_review` → `noop`。

`target` 由 Agent/CLI 在请求体传入（`_ReviewPassBody.target`，issues.py:996，默认 `"processing"`），服务端不推断、不校验语义。

### 1.2 末节点判定的现成模式（可直接复用）

- pipeline 节点 `next` 最后置 `"done"`：`pipeline_config.py:55` `next_id = node_ids[i+1] if i+1 < len(node_ids) else "done"`；
- 流水线已完成同一判据：`service.py:1685-1687`
  ```python
  for _n in _nodes:
      if _n.get("id") == _ag and _n.get("next") == "done":
          set_issue_stage(..., "pending_review", _ag)
  ```
- 另有 `get_next_node(scene_id, current_node_id)`（pipeline_config.py:336），返回 `next` 为 `"done"` 时即末节点。

### 1.3 问题演示

末节点（如 SRE）A2A 完 → 用户确认 → Agent 误传 `target=processing`
→ `status=processing`、stage 仍 = 末节点 → 无后续节点 → 卡片「进行中」且无自动接单（常见于技能对 review-pass 的 `target` 语义理解偏差 / CLI 默认值）。

---

## 二、方案设计

### 2.1 归一规则（在 `_review_pass` 内、else 分支前）

```
target != "done"
  且 issue.stage 可推定（get_issue_stage → _cur_stage 非空）
  且 stage 非终态（≠ done / pending_review）
  且 该 stage 节点的 next == "done"（get_pipeline_config 节点 / get_next_node）
     → 归一：跳过 processing 分支，直接走 done 分支
       （status=completed + completed_reason='review_passed' + completed_at +
        set_issue_stage(..., 'done', 'agent')，返回 passed_done）
       日志：review-pass normalized last-node target=processing→done (repo/#issue, evidence)
```

- 归一走**既有 done 分支**（复用同一段 SQL + stage 更新），不新写终态逻辑；
- CLI/技能无需改动（服务端兜底归一）。

### 2.2 抽取 helper（与 service.py 判据同源，便于复用）

```python
def _is_last_stage_node(repo_key: str, issue_number: int) -> bool:
    """当前 issue.stage 是否为流水线末节点（next=='done'）。stage 不可推定/未知 → False（保守）。"""
    try:
        from src.api.session_store import get_issue_stage, get_issue_scene
        from src.api.pipeline_config import get_pipeline_config
        _issue_id = f"#{issue_number}"
        _st = get_issue_stage(repo_key, _issue_id) or {}
        _cur = (_st.get("stage") or "").strip()
        if not _cur or _cur in ("done", "pending_review"):
            return False
        _scene = get_issue_scene(repo_key, _issue_id)
        _scene_id = (_scene[0] if _scene and _scene[0] else "") or "default"
        _cfg = get_pipeline_config(_scene_id, repo_key=repo_key) or {}
        for _n in (_cfg.get("nodes") or []):
            if _n.get("id") == _cur:
                return _n.get("next") == "done"
        return False
    except Exception:
        return False
```

### 2.3 接入位置

`_review_pass`（issues_center.py:451）在 `if target == "done"` 分支不满足时：

```python
else:
    if _is_last_stage_node(repo_key, issue_number):
        # 末节点触达：processing 无下游可续 → 归一 done
        ... 复用 done 分支（可直接落入 target='done' 的逻辑）...
        return "passed_done"
    ... 原 processing 分支 ...
```

> 实现上建议把「done 分支」抽成内部小函数后，归一与显式 done 都调用它（避免复制 SQL）。

---

## 三、各模块改动

| 模块 | 改动 |
|------|------|
| `src/api/issues_center.py` | 新增 `_is_last_stage_node(repo_key, issue_number)`；`_review_pass` else 分支前加归一（复用 done 分支逻辑） |
| `tests/` | 见 §五 |
| 文档 | `help/api/11-issues.md` review-pass 节补一句「末节点触达时自动归一 done」 |

---

## 四、影响面 / 边界

| 场景 | 行为 |
|------|------|
| 显式 `target=done` | 不变（走原 done 分支） |
| 末节点 + `target=processing` | **归一 done** → completed + stage=done + sweeper workdir 清理（与用户确认收尾语义一致） |
| 中继/循环节点（next≠done，如 qa 复审） | 不归一 → processing 继续 |
| `stage` 空/未知 / 已 done / pending_review | 保守不归一（`_is_last_stage_node` 返回 False） |
| `review-reject` | **不归一**（打回要 processing 继续改，末节点也有意义） |
| 多任务父任务 | complete_task 已另有 done 规则（service.py:1725-1730），`_is_last_stage_node` 按 issue.stage 权威判定不冲突 |
| 未来配置加后置节点（next 改真 id） | 判定自然失效 → 不强（配置驱动，风险低） |
| 幂等 | 仅 `pending_review` 可退出；非该态仍 `noop` |

---

## 五、验证

| 用例 | 断言 |
|------|------|
| UT-01 末节点（预置 scene 节点 next=done）+ target=processing → completed / `passed_done` / `completed_reason='review_passed'` / `stage=done` | 主路径归一 |
| UT-02 中继节点（next=qa/coding）+ target=processing → `processing` / `passed_processing` / stage 不变 | 不误伤 |
| UT-03 非 pending_review 状态调用 → `noop` / 状态不变 | 幂等 |
| UT-04 stage 空 / 未知 → 保守 processing | 守卫 |
| UT-05 stage=done/pending_review → 不归一（返回既有语义） | 守卫 |
| UT-06 显式 target=done → 原 done 分支不变 | 兼容 |
| 回归 | review-reject、pending-review、webhook 自动恢复不受影响 |

---

## 六、落档修订说明

- 仅服务端归一，不改 CLI/技能契约；`review-reject` 明确排除。
- 复用 service.py:1685-1687 同一「`next=='done'`」判据，语义对齐流水线自身。
- 保守默认：stage 不可推定不猜，宁可持续 processing 也不误终态。