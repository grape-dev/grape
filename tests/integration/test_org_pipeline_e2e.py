"""Issue #2153: organization pipeline end-to-end integration tests.

覆盖组织主体链路：provisioning → 关联 2 仓库 → 组织需求 → 下派 2 目标仓库 →
子任务完成/失败 → 聚合。四类断言：completed / partial_failed / cancelled / 幂等。

约束（详见 docs/issue-2153/plan.md）：mock 仅限 VCS 建仓/托管/bot token（adapter 层）
与 worker 派发入口；保留真实 DB 与真实 ``org_dispatch`` 状态机；子任务状态经真实状态迁移
推进（禁止直改 dispatch status 或直调聚合）；每用例独立 tmp_path SQLite。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

# 目标仓库与场景（固定值，保证确定性）
TARGET_A_KEY = "gitcode:issue2153/svc-a"
TARGET_B_KEY = "gitcode:issue2153/svc-b"
SCENE_TARGET_A = "scene-2153-svc-a"
SCENE_TARGET_B = "scene-2153-svc-b"


def test_org_pipeline_e2e_happy_path_and_partial_failed(org_e2e, monkeypatch):
    """主链路：2 target → running → 1 completed + 1 failed → partial_failed → 2 completed。"""
    e2e = org_e2e

    # 1) 团队组织 provisioning → ready（mock 建仓/hosting）
    e2e.create_org("issue2153-team")
    assert e2e.org_repo_key and e2e.org_scene_id

    # 2) 关联 2 个目标仓库 → GET /api/orgs/{id}/repos 可见
    e2e.link_target_repo(TARGET_A_KEY, SCENE_TARGET_A)
    e2e.link_target_repo(TARGET_B_KEY, SCENE_TARGET_B)
    linked = {row["repo_key"] for row in e2e.list_org_repos()}
    assert {TARGET_A_KEY, TARGET_B_KEY} <= linked

    # 3) 组织仓库内创建组织需求
    e2e.create_org_issue()

    # 4) 下派 2 个目标仓库 → 2 target running，子 Issue 与 target 一一对应
    resp = e2e.dispatch([TARGET_A_KEY, TARGET_B_KEY])
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["status"] == "running", created
    by_repo = {t["repo_key"]: t for t in created["targets"]}
    assert set(by_repo) == {TARGET_A_KEY, TARGET_B_KEY}
    assert all(t["status"] == "running" for t in created["targets"])
    assert all(t["child_issue_number"] > 0 for t in created["targets"])

    child_a = by_repo[TARGET_A_KEY]["child_issue_number"]
    child_b = by_repo[TARGET_B_KEY]["child_issue_number"]
    # 子 Issue 与 target 一一对应，且每个目标仓库恰好 1 个子 Issue
    assert {(rk, n) for rk, n in e2e.created_child_issues} == {
        (TARGET_A_KEY, child_a), (TARGET_B_KEY, child_b),
    }
    assert len(e2e.child_issue_rows(TARGET_A_KEY)) == 1
    assert len(e2e.child_issue_rows(TARGET_B_KEY)) == 1

    dispatch_id = created["dispatch_id"]

    # 5) 子任务 A 真实完成迁移 + 子任务 B 走真实超时失败 → dispatch=partial_failed
    e2e.complete_child_issue(TARGET_A_KEY, child_a)
    from src.domains.mgmt import org_dispatch

    original_timeout = org_dispatch.TARGET_TIMEOUT_SECONDS
    monkeypatch.setattr(org_dispatch, "TARGET_TIMEOUT_SECONDS", 0)  # 收缩超时窗口，避免真实等待
    e2e.sweep()
    partial = e2e.dispatch_detail(dispatch_id)
    assert partial["status"] == "partial_failed", partial
    partial_targets = {t["repo_key"]: t for t in partial["targets"]}
    assert partial_targets[TARGET_A_KEY]["status"] == "completed"
    assert partial_targets[TARGET_B_KEY]["status"] == "failed"
    assert "timeout" in partial_targets[TARGET_B_KEY]["error"]

    # 6) 恢复超时窗口 → retry 失败目标（复用同一子 Issue）→ 完成 → dispatch=completed
    monkeypatch.setattr(org_dispatch, "TARGET_TIMEOUT_SECONDS", original_timeout)
    retry = e2e.client.post(
        f"/api/orgs/{e2e.org_id}/dispatches/{dispatch_id}/targets/{TARGET_B_KEY}/retry"
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["target"]["status"] == "running"
    assert retry.json()["target"]["child_issue_number"] == child_b

    e2e.complete_child_issue(TARGET_B_KEY, child_b)
    e2e.sweep()
    final = e2e.dispatch_detail(dispatch_id)
    assert final["status"] == "completed", final
    assert all(t["status"] == "completed" for t in final["targets"])
    # 端到端只创建了 2 个子 Issue（重试复用，不新建）
    assert len(e2e.created_child_issues) == 2


def test_repeat_dispatch_is_idempotent(org_e2e):
    """同 targets 重复下派：终态复用既有 batch，targets 集合一致、子 Issue 数量不变。"""
    e2e = org_e2e
    e2e.create_org("issue2153-idem")
    e2e.link_target_repo(TARGET_A_KEY, SCENE_TARGET_A)
    e2e.link_target_repo(TARGET_B_KEY, SCENE_TARGET_B)
    e2e.create_org_issue(number=21531, title="幂等组织需求")

    first = e2e.dispatch([TARGET_A_KEY, TARGET_B_KEY], org_issue_number=21531)
    assert first.status_code == 201, first.text
    first_body = first.json()
    dispatch_id = first_body["dispatch_id"]
    by_repo = {t["repo_key"]: t for t in first_body["targets"]}
    before_counts = {
        TARGET_A_KEY: len(e2e.child_issue_rows(TARGET_A_KEY)),
        TARGET_B_KEY: len(e2e.child_issue_rows(TARGET_B_KEY)),
    }
    assert before_counts == {TARGET_A_KEY: 1, TARGET_B_KEY: 1}

    # 在途重复下派 → 409，未新建子 Issue
    busy = e2e.dispatch([TARGET_B_KEY, TARGET_A_KEY], org_issue_number=21531)
    assert busy.status_code == 409, busy.text
    assert busy.json()["detail"]["error_code"] == "DISPATCH_IN_PROGRESS"
    assert len(e2e.created_child_issues) == 2

    # 真实推进到终态：两个子任务完成 → sweeper 聚合 completed
    for key in (TARGET_A_KEY, TARGET_B_KEY):
        e2e.complete_child_issue(key, by_repo[key]["child_issue_number"])
    e2e.sweep()
    assert e2e.dispatch_detail(dispatch_id)["status"] == "completed"

    # 终态后同 targets（顺序无关）重复下派 → 复用既有 batch
    again = e2e.dispatch([TARGET_B_KEY, TARGET_A_KEY], org_issue_number=21531)
    assert again.status_code == 200, again.text
    again_body = again.json()
    assert again_body["dispatch_id"] == dispatch_id
    assert {t["repo_key"] for t in again_body["targets"]} == {TARGET_A_KEY, TARGET_B_KEY}
    after_counts = {
        TARGET_A_KEY: len(e2e.child_issue_rows(TARGET_A_KEY)),
        TARGET_B_KEY: len(e2e.child_issue_rows(TARGET_B_KEY)),
    }
    assert after_counts == before_counts
    assert len(e2e.created_child_issues) == 2


def test_cancel_dispatch_cancels_children(org_e2e):
    """下派后 cancel：子任务取消，dispatch=cancelled。"""
    e2e = org_e2e
    e2e.create_org("issue2153-cancel")
    e2e.link_target_repo(TARGET_A_KEY, SCENE_TARGET_A)
    e2e.link_target_repo(TARGET_B_KEY, SCENE_TARGET_B)
    e2e.create_org_issue(number=21532, title="取消组织需求")

    created = e2e.dispatch([TARGET_A_KEY, TARGET_B_KEY], org_issue_number=21532)
    assert created.status_code == 201, created.text
    dispatch_id = created.json()["dispatch_id"]

    resp = e2e.client.post(f"/api/orgs/{e2e.org_id}/dispatches/{dispatch_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled", resp.json()

    detail = e2e.dispatch_detail(dispatch_id)
    assert detail["status"] == "cancelled", detail
    assert all(t["status"] == "cancelled" for t in detail["targets"])
    for key in (TARGET_A_KEY, TARGET_B_KEY):
        rows = e2e.child_issue_rows(key)
        assert len(rows) == 1
        assert rows[0][1] == "cancelled"
