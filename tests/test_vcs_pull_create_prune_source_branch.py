"""PR 创建支持 prune_source_branch（合并后删源分支）的平台侧链路看护。

覆盖四层，缺一层字段就静默丢失：
1. Pydantic 模型 PRCreateBody 收下字段；
2. create_pull 的 neutral 字典显式带上（最容易漏 —— 模型收了、provider 规则写了，
   neutral 没放照样到不了上游，且不报错）；
3. GitCode provider 规则显式声明 prune_source_branch；
4. GitHub provider 规则丢弃该字段（GitHub 建 PR 无此参数，避免发脏字段）。

另含默认值语义：不带该字段的调用方 prune_source_branch=False，与 GitCode 上游
默认（不删分支）一致；GitHub 通道任何取值都不出现在 payload 里。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.domains.runtime.vcs.providers import gitcode, gitee, gitlab, github

CTX = {"owner_repo": "owner/repo"}


def _neutral(**extra):
    base = {"title": "T", "head": "feat/x", "base": "main", "body": "B",
            "related_issue_nums": [], "auto_close_issues": False}
    base.update(extra)
    return base


class TestPruneSourceBranchRules:
    def test_model_accepts_field(self):
        from src.api.git_repo_api import PRCreateBody
        body = PRCreateBody(title="T", head="feat/x", base="main", prune_source_branch=True)
        assert body.prune_source_branch is True
        assert PRCreateBody().prune_source_branch is False

    def test_gitcode_keeps_field_true_and_false(self):
        assert gitcode.translate("pulls", "create", _neutral(prune_source_branch=True), CTX)["prune_source_branch"] is True
        # false 不被 apply_rules 跳过（其空值判定只跳 None/""/空容器），显式 false
        # 与 GitCode 上游默认 false 等价，不影响老调用方语义。
        assert gitcode.translate("pulls", "create", _neutral(prune_source_branch=False), CTX)["prune_source_branch"] is False

    def test_github_drops_field(self):
        for value in (True, False):
            payload = github.translate("pulls", "create", _neutral(prune_source_branch=value), CTX)
            assert "prune_source_branch" not in payload, payload

    def test_gitee_and_gitlab_drop_field(self):
        """arch 分支新增的 gitee/gitlab 通道同样不得透传该字段（它们建 PR 无此参数）。"""
        for mod in (gitee, gitlab):
            payload = mod.translate("pulls", "create", _neutral(prune_source_branch=True), CTX)
            assert "prune_source_branch" not in payload, (mod.__name__, payload)

    def test_github_still_emits_its_own_fields(self):
        """丢弃规则不得连坐其它字段（关联 Issue 的 body 追加语义仍由 _gh_close_body 产生）。"""
        payload = github.translate("pulls", "create",
                                   _neutral(related_issue_nums=[12], prune_source_branch=True), CTX)
        assert "prune_source_branch" not in payload
        assert "#12" in payload.get("body", "")
        # auto_close_issues=true → Closes 语义；false → Related to（#1739 语义不退化）
        closed = github.translate("pulls", "create",
                                  _neutral(related_issue_nums=[12], auto_close_issues=True,
                                           prune_source_branch=True), CTX)
        assert "Closes #12" in closed.get("body", "")
        assert "prune_source_branch" not in closed

    def test_endpoint_passes_field_to_gitcode(self):
        """端到端：POST /git-repo/{owner}/{repo}/pulls 的真实出站 payload 带该字段。"""
        import json as _json
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.deps import get_current_user_or_api_key

        recorded = []

        def _fake_req(method, url, token="", body=None):
            recorded.append({"method": method, "url": url, "body": body})
            return {"ok": True, "title": "t", "number": 1, "echo": {}}

        import src.domains.mgmt.user.repo_manager as rm
        orig_req = rm._req
        rm._req = _fake_req
        app.dependency_overrides[get_current_user_or_api_key] = lambda: {
            "id": 98701, "username": "tester", "auth_type": "session_token",
            "agent_type": "vod", "user_id": 0, "session_key": "s1",
            "repo_key": "gitcode:o/hello"}
        try:
            import src.domains.mgmt.user.db as udb
            orig_gt = udb.get_gitcode_token
            udb.get_gitcode_token = lambda *a, **k: "bot_tok"
            try:
                client = TestClient(app)
                r = client.post("/api/git-repo/o/hello/pulls",
                                json={"title": "T", "head": "feat/x", "base": "main",
                                      "prune_source_branch": True})
                assert r.status_code == 200, r.text
                body = [c["body"] for c in recorded if c["method"] == "POST" and "/pulls" in c["url"]][-1]
                assert body.get("prune_source_branch") is True, body
                # 默认调用方（不带字段）→ false，与 GitCode 上游默认「不删分支」等价
                r2 = client.post("/api/git-repo/o/hello/pulls",
                                 json={"title": "T2", "head": "feat/y", "base": "main"})
                assert r2.status_code == 200, r2.text
                body2 = [c["body"] for c in recorded if c["method"] == "POST" and "/pulls" in c["url"]][-1]
                assert body2.get("prune_source_branch") is False, body2
            finally:
                udb.get_gitcode_token = orig_gt
        finally:
            rm._req = orig_req
            app.dependency_overrides.clear()

    def test_neutral_dict_carries_field(self):
        """源码级看护：neutral 字典必须显式含 prune_source_branch。

        只改模型 + provider 规则、漏改 neutral 是本改动的已知坑，且运行时不报错，
        因此用结构断言代替（取 create_pull 中 neutral = {...} 字面量）。
        """
        src = Path("src/api/git_repo_api.py").read_text(encoding="utf-8")
        blocks = [m.group(1) for m in re.finditer(r"neutral = \{(.*?)\n    \}", src, re.S)]
        assert blocks, "create_pull 的 neutral 字典结构变化，请同步本看护用例"
        # pulls/create 的那一份是唯一带 auto_close_issues 的 neutral 字面量
        pull_blocks = [b for b in blocks if "auto_close_issues" in b]
        assert len(pull_blocks) == 1, f"neutral 字面量定位失败：命中 {len(pull_blocks)} 份"
        assert "prune_source_branch" in pull_blocks[0], "neutral 缺少 prune_source_branch，字段会被丢弃"