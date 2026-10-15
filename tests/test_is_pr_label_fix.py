# -*- coding: utf-8 -*-
"""需求详情页 is_pr 标签误判修复 — 拟真验证。

场景（真实数据形态，复刻示例仓库 #3/#4 的历史误判数据）：
- 普通 Issue #N：issue_opened 事件 + 后续 pull_request_merged（PR 交付该 issue）
  → is_pr 应为 False（详情页显示 Issue，链接 /issues/N）
- 跨仓 A2A PR 形态需求：只有 pull_request_opened 事件挂 #N（无 issue 生命周期事件）
  → is_pr 应为 True（issue_number 实际是 PR 号，链接 /pull/N）
"""
import pytest
import json
import os

from src.session_manager import db, db_lock

_REPO = "gitcode:XOcto/simispr-repo"


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (_REPO,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (_REPO,))
        c.execute("DELETE FROM issue_state WHERE repo_key=?", (_REPO,))
        db.commit()


def _seed_events(rows):
    """rows: [(event_type, issue_id)]"""
    with db_lock:
        c = db.cursor()
        for et, iid in rows:
            c.execute(
                "INSERT INTO events (repo_key, event_type, issue_id, title, status, created_at) "
                "VALUES (?,?,?,?, 'completed', datetime('now'))",
                (_REPO, et, iid, f"sim {et}"))
        db.commit()


def _call():
    from src.api.issues_center import _batch_fetch_pr_issue_keys
    keys = [(_REPO, "#3"), (_REPO, "#7"), (_REPO, "#99")]
    return _batch_fetch_pr_issue_keys(keys)


class TestIsPrLabel:
    @pytest.fixture(autouse=True)
    def _setup(self):
        _clean()
        yield
        _clean()

    def test_real_issue_with_delivery_pr_not_flagged(self):
        """普通 Issue + PR 交付关联（#3: issue_opened + pull_request_merged）→ 不算 PR"""
        _seed_events([
            ("issue_opened", "#3"),
            ("pull_request_merged", "#3"),   # PR resolved #3 → 交付关联
        ])
        hits = _call()
        print(f"\n[拟真] Issue#3+PR交付 → is_pr命中: {hits}")
        assert (_REPO, "#3") not in hits, "真实 Issue 的 PR 交付关联不应判为 PR"

    def test_a2a_pr_form_requirement_flagged(self):
        """跨仓 A2A PR 形态需求（#7: 仅 pull_request_opened）→ 是 PR"""
        _seed_events([
            ("pull_request_opened", "#7"),
            ("pull_request_updated", "#7"),
        ])
        hits = _call()
        print(f"\n[拟真] A2A PR#7 → is_pr命中: {hits}")
        assert (_REPO, "#7") in hits, "A2A PR 形态需求应判为 PR"

    def test_mixed_both_correct(self):
        """两种形态并存：#3 判 Issue、#7 判 PR、#99 无事件不命中"""
        _seed_events([
            ("issue_opened", "#3"),
            ("issue_comment", "#3"),
            ("pull_request_review", "#3"),   # 待审核阶段 PR 事件
            ("pull_request_merged", "#3"),
            ("pull_request_opened", "#7"),
        ])
        hits = _call()
        print(f"\n[拟真] 混合 → is_pr命中: {hits}")
        assert (_REPO, "#3") not in hits
        assert (_REPO, "#7") in hits
        assert (_REPO, "#99") not in hits

    def test_qa_review_stage_pr_event_not_flagged(self):
        """主人的原始场景: 需求流转到待审核(pull_request_review) → 不变 PR"""
        _seed_events([
            ("issue_opened", "#3"),
            ("pull_request_review", "#3"),   # 待审核
        ])
        hits = _call()
        print(f"\n[拟真] 待审核阶段 → is_pr命中: {hits}")
        assert (_REPO, "#3") not in hits, "待审核阶段 PR 审查事件不应把 Issue 错标为 PR"
