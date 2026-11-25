"""下派详情回归：Issue 详情必须带 repo_key/org_id。

背景：组织下派卡片依赖 `detailView.org_id` 与 `issue_number` 去查
`/api/orgs/{org}/dispatches`；深链/直达详情只拿详情接口，缺 org_id 时
前端会静默不加载下派详情，表现为“看不到下派的仓库和 Issue”。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.runtime.routes.issues import _assemble_issue


def test_assemble_issue_exposes_repo_and_org_context():
    stage_map = {
        "7": {
            "internal_id": 9,
            "stage": "pending_review",
            "owner_agent": "qa",
            "org_id": "org-uuid-7",
        }
    }

    out = _assemble_issue(
        "gitcode:miumiu_020523/grape-test-team", "7", stage_map, {}, {}, {}
    )

    assert out["repo_key"] == "gitcode:miumiu_020523/grape-test-team"
    assert out["org_id"] == "org-uuid-7"
    assert out["number"] == 7


def test_assemble_issue_org_id_empty_when_unowned():
    stage_map = {"3": {"internal_id": 4, "stage": "coding", "owner_agent": "coding"}}

    out = _assemble_issue("gitcode:o/r", "3", stage_map, {}, {}, {})

    assert out["org_id"] == ""
    assert out["repo_key"] == "gitcode:o/r"