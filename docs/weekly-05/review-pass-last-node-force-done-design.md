# 方案：review-pass 末节点触达时强制归一 done（processing 归一 + done 前置放宽）

> 状态：技术方案（2026-10-08 修订，并入 #2266 复诊「方向 A」）｜目标分支：weekly-05-lyz
> 动机：
> 1. `review-pass` 的 `target=processing` 语义是「用户过审、流水线继续跑后续节点」。若当前 `issue.stage` 已是**流水线最后一个节点**（`next="done"`），没有后续节点可续——`status=processing` 后无人推进，需求卡片永久卡在「进行中」。应在**末节点可推定**时把误传的 `processing` 归一为 `done`。
> 2. **#2266 复诊**：末节点 Agent 正确传了 `target=done`，但返回 `noop`——因为 `_review_pass` 只在 `issue.status=='pending_review'` 时生效，而该需求（CLI 流水线）**从未进入 `pending_review`**（status=`active`）；随后 Agent 合入 PR #6 成功，`pull_request_merged` webhook 又因 `self_loop`（bot 自合）被跳过 → 无任何链路置 issue 终态（实证见 §1.3）。
> 范围：仅 `review-pass`；`review-reject` 的 `processing`（打回继续改）在末节点仍有意义，**不归一**。

---

## 一、现状与依据（代码核对 2026-10-07 / 2026-10-08 复诊）

### 1.1 现状：`_review_pass` 前置过严 + 不感知流水线位置

`issues_center.py _review_pass(repo_key, issue_number, target, evidence)`：
- `target == "done"` → 仅当 `status='pending_review'` 时 `status=completed` + `completed_reason='review_passed'` + `completed_at` + `set_issue_stage(..., 'done')` → 返回 `passed_done`；否则 `noop`；
- `target == "processing"` → 仅当 `status='pending_review'` 时 `status=processing` → 返回 `passed_processing`；否则 `noop`；
- 非 `pending_review` 一律 `noop`。

`target` 由 Agent/CLI 在请求体传入（`_ReviewPassBody.target`，issues.py:996，默认 `"processing"`），服务端不推断、不校验语义。

### 1.2 #2266 实证（hello-calculator-13 / Issue #11，2026-10-08）

- 场景 `cloud-native-cli`：`vod→issue→coding→qa→done`，**qa 为末节点（next=done）**；
- 用户评论「✅ 合入确认」→ qa Agent（seq 2266）调用 `grape issues review-pass --issue_id 226 --json '{"target":"done","evidence":...}'`；
- 服务端日志：`POST /api/issues/226/review-pass status=200`（内部 `noop`）——当时 `issue.status='active'` ≠ `pending_review`，`WHERE status='pending_review'` 0 行；
- Agent 误判「状态已在 pending_review」，继续 `pulls merge` 合入 PR #6（merged=true sha=b420af3e）+ 回评论 + `grape tasks complete`；
- `pull_request_merged` webhook 被跳过：`reason=self_loop, detail=grape_bot`（bot 自己合入）；
- **结果：PR 已合入（GitCode 真值），但 issue.status='active'、stage='qa'，需求卡片永不 done。**

> 兄弟 Issue #8/#9 同样出现 `尝试 review-pass（noop）与 pulls merge（405/...）`，为系统性现象。

### 1.3 末节点判定的现成模式（可直接复用）

- pipeline 节点 `next` 最后置 `"done"`：`pipeline_config.py:55` `next_id = node_ids[i+1] if i+1 < len(node_ids) else "done"`；
- 流水线已完成同一判据：`service.py:1685-1687`
  ```python
  for _n in _nodes:
      if _n.get("id") == _ag and _n.get("next") == "done":
          set_issue_stage(..., "pending_review", _ag)
  ```
- 另有 `get_next_node(scene_id, current_node_id)`（pipeline_config.py:336），返回 `next` 为 `"done"` 时即末节点。

---

## 二、方案设计

### 2.1 归一规则一：target=processing 末节点触达 → done（已随本方案首个实现落地）

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

### 2.2 归一规则二（方向 A，本次增补）：target=done 前置放宽（#2266）

```
target == "done"
  → 1) status ∈ (pending_review)                       → 原契约：completed + stage=done（不变）
  → 2) 上述未命中，但当前 stage 为流水线末节点
       且 status ∈ (active, processing, awaiting_input)  → **放宽**：同样 completed + stage=done
       日志：review-pass normalized last-node done-from-active (repo/#issue, evidence)
  → 3) 其余（含非末节点 + 非 pending_review）           → noop（保持原语义，防中间节点误终态）
```

- **作用域限定末节点**：防 coding/qa 等中间节点 Agent 误传 done 就提前终态跳过后续节点；只有「交付点在末节点、用户已确认合入」才放行（对齐 #2266 场景）；
- `status` 终态（completed/cancelled）天然不命中（不在集合）→ noop，幂等；多任务父任务 stage=done 时 `_is_last_stage_node` 返回 False → 不误伤。
- CLI/技能无感（服务端兜底）。

### 2.3 抽取 helper（与 service.py 判据同源，便于复用）

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


def _complete_issue_done(repo_key: str, issue_number: int, evidence: str,
                         statuses: tuple = ("pending_review",)) -> bool:
    """把给定 status 集合内的问题置 completed + stage=done（合入/收尾）。返回是否变更。"""
    with db_lock:
        c = db.cursor()
        _ph = ",".join("?" * len(statuses))
        c.execute(
            f"UPDATE issue SET status='completed', completed_reason='review_passed', completed_at=datetime('now') "
            f"WHERE repo_key=? AND issue_number=? AND status IN ({_ph})",
            (repo_key, issue_number, *statuses))
        db.commit()
        _changed = c.rowcount > 0
    if _changed:
        try:
            from src.api.session_store import set_issue_stage
            set_issue_stage(repo_key, f"#{issue_number}", "done", "agent")
        except Exception as _se:
            log.warning(f"review_pass stage=done failed {repo_key}/#{issue_number}: {_se}")
        log.info(f"Issue review-pass(done): {repo_key}/#{issue_number} evidence={evidence[:60]}")
    return _changed
```

### 2.4 接入位置（`_review_pass` 终态逻辑重写）

```python
def _review_pass(repo_key: str, issue_number: int, target: str = "processing",
                 evidence: str = "") -> str:
    try:
        if target == "done":
            if _is_last_stage_node(repo_key, issue_number):
                # 末节点：兼容 pending_review，且放宽至 active/processing/awaiting_input（方向 A）
                if _complete_issue_done(repo_key, issue_number, evidence,
                                        ("pending_review", "active", "processing", "awaiting_input")):
                    return "passed_done"
                return "noop"
            # 非末节点：保持原契约（仅 pending_review 可退出），防中间节点误终态
            return "passed_done" if _complete_issue_done(repo_key, issue_number, evidence,
                                                          ("pending_review",)) else "noop"
        if _is_last_stage_node(repo_key, issue_number):
            # 末节点 + processing → 归一 done（规则一）
            log.info(f"Issue review-pass normalized last-node target=processing→done: "
                     f"{repo_key}/#{issue_number} evidence={evidence[:60]}")
            return "passed_done" if _complete_issue_done(repo_key, issue_number, evidence,
                                                          ("pending_review", "active", "processing", "awaiting_input")) else "noop"
        with db_lock:  # 原 processing 分支（仅 pending_review 可回 processing）
            c = db.cursor()
            c.execute(
                "UPDATE issue SET status='processing', completed_reason=NULL, completed_at=NULL "
                "WHERE repo_key=? AND issue_number=? AND status='pending_review'",
                (repo_key, issue_number))
            db.commit()
            _changed = c.rowcount > 0
        return "passed_processing" if _changed else "noop"
    except Exception as _e:
        log.warning(f"review_pass failed {repo_key}/#{issue_number}: {_e}")
        return "noop"
```

> 说明：`target=done` 先查末节点（末节点 → 放宽集合 + 兼容 pending_review）；非末节点仍只允许 pending_review。`target=processing` 末节点归一 done（规则一）保持不变。

---

## 三、各模块改动

| 模块 | 改动 |
|------|------|
| `src/api/issues_center.py` | 已有 `_is_last_stage_node`；本次把 `_complete_pending_review` 泛化为 `_complete_issue_done(statuses=...)`；`_review_pass` 按 §2.4 重写（done 前置放宽 + processing 归一） |
| `tests/` | 见 §五（补 #2266 场景：末节点 status=active + target=done → completed） |
| 文档 | `help/api/11-issues.md` review-pass 节补一句「末节点触达时 processing 归一 done；done 若未处于待审核但对齐末节点交付也放行」 |

---

## 四、影响面 / 边界

| 场景 | 行为 |
|------|------|
| 显式 `target=done` + `pending_review` | 不变（completed + stage=done） |
| **显式 `target=done` + 末节点 + `active/processing/awaiting_input`** | **放行 done**（本轮修复 #2266） |
| `target=done` + 非末节点 + 非 pending_review | 不变 noop（防中间节点误终态） |
| 末节点 + `target=processing` | 归一 done（规则一，先前已实现） |
| 中继/循环节点（next≠done） | 不归一 → processing 继续 |
| `stage` 空/未知 / 已 done / pending_review | `_is_last_stage_node` 保守 False；`done` 前置路径退化回仅 pending_review |
| `review-reject` | **不归一、不受影响**（打回继续改，末节点也有意义） |
| 多任务父任务 | complete_task 已另有 done 规则；父任务 stage=done → False，不误伤 |
| 幂等 | status 终态（completed/cancelled 不在集合）→ noop |
| 未来配置加后置节点（next 改真 id） | 判定自然失效 → 不强（配置驱动） |

---

## 五、验证

| 用例 | 断言 |
|------|------|
| UT-01 末节点 + target=processing → completed / `passed_done` / stage=done | 规则一 |
| UT-02 中继节点 + target=processing → processing / `passed_processing` / stage 不变 | 不误伤 |
| **UT-0A 末节点 + status=active + target=done → completed / `passed_done` / stage=done（#2266 场景）** | 规则二 |
| **UT-0B 中继节点 + status=active + target=done → noop / 状态不变（防中间误终态）** | 规则二守卫 |
| UT-03 状态 already completed/cancelled + 任意 target → noop / 不变 | 幂等 |
| UT-04 stage 空/未知 → done 退化 pending_review 优先 | 守卫 |
| UT-05 显式 target=done + pending_review（非末节点）→ 原 done 分支 | 兼容 |
| 回归 | review-reject、pending-review、webhook 自动恢复不受影响 |

---

## 六、落档修订说明

- 2026-10-08 增补「方向 A」：#2266 实证 PR 已合入但 issue 不 done 的根因是 `target=done` 前置过严（非 pending_review 一律 noop）+ `pull_request_merged` 因 self_loop 被跳过。服务端放宽「末节点 + 活跃态 + target=done」即收尾，不改 CLI/技能契约。
- 放宽面收敛到**末节点**：交付收尾点在末节点，防中间节点 Agent 误终态跳过后续流水线。
- 复用 service.py:1685-1687 同一「`next=='done'`」判据，语义对齐流水线自身；保守默认：stage 不可推定不猜。
- `review-reject` 明确排除；多任务父任务经 stage 判定天然不受影响。