"""巡检：任务 completed 但未声明下一棒，必须登记断链（原来静默丢失）。

线上形态（2026-11-17，Issue #1）：vod 任务已 completed、事件已 completed，
但 Issue 仍停在 vod 阶段、状态 processing，事件总数始终为 1 —— 因为 vod 的
交办请求被 A2A 端点 400 拒掉（多级 namespace 被 `count("/") != 1` 判非法），
agent 输出里 `next_step` 与 `actions` 均为空。

`detect_broken_chains` 原来对 "next_step 不以 a2a 开头" 直接 `continue`，于是：
- 这类断链不产生 finding → 不进补偿队列 → Issue 永久停在原阶段，零告警；
- 紧随其后的 `target_agent 为空 → "下一棒意图不可解析，人工核对"` 成为死代码，
  而 `patrol_compensate` 里对应的 `needs_human` 分支（target_unresolvable）
  因此永远执行不到。

修复口径：用场景节点定义（`expected_next_agent`）判断"这一棒本应有下一棒"，
是则登记 `reason=no_next_step`；末棒（next=done / handoff!=a2a）正常收尾不报，
避免误报让补偿把已收敛的链复活。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from src.api import patrol as patrol_mod  # noqa: E402

REPO = "gitcode:biz/jmbos/devops/ops-inspection"
ISSUE = "#1"
TID = "t_a4d87632347a"


def _old(minutes: int = 30) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def patched(monkeypatch):
    """把断链检测的数据访问层全部替换成脚本化函数（不碰真实库）。"""
    state = {
        "rows": [],
        "texts": {},
        "expects": (False, ""),
        "l1": False,
        "l2": False,
        "terminal": "",
    }
    monkeypatch.setattr(patrol_mod, "patrol_runtime_config",
                        lambda: {"confirm_minutes": 10, "interval_seconds": 300,
                                 "lookback_mode": "runtime", "compensate_enabled": True})
    monkeypatch.setattr(patrol_mod, "_iter_completed_tasks",
                        lambda cutoff, chunk=500: list(state["rows"]))
    monkeypatch.setattr(patrol_mod, "_fetch_result_texts",
                        lambda tids: dict(state["texts"]))
    monkeypatch.setattr(patrol_mod, "issue_terminal",
                        lambda repo, issue: state["terminal"])
    monkeypatch.setattr(patrol_mod, "expected_next_agent",
                        lambda repo, issue, agent: state["expects"])
    monkeypatch.setattr(patrol_mod, "l1_event_exists",
                        lambda *a, **kw: state["l1"])
    monkeypatch.setattr(patrol_mod, "l2_downstream_exists",
                        lambda *a, **kw: state["l2"])
    monkeypatch.setattr(patrol_mod, "_subtask_parent", lambda *a, **kw: "")
    return state


def _run(state):
    return patrol_mod.detect_broken_chains("", known=set())


class TestCompletedWithoutNextStep:
    def test_vod_finished_without_next_step_is_flagged(self, patched):
        """vod 完成但 next_step 空、场景本应交给 issue → 必须登记断链。"""
        patched["rows"] = [(TID, REPO, ISSUE, "vod", _old())]
        patched["texts"] = {TID: json.dumps({"conclusion": "已完成", "actions": []},
                                            ensure_ascii=False)}
        patched["expects"] = (True, "issue")

        findings = _run(patched)

        assert len(findings) == 1, findings
        f = findings[0]
        assert f["kind"] == "broken_chain"
        assert f["task_id"] == TID
        detail = json.loads(f["detail"])
        assert detail["reason"] == "no_next_step"
        assert detail["target_agent"] == ""
        assert detail["expected_next"] == "issue"
        assert "a2a-redo" in f["suggestion"], f["suggestion"]

    def test_terminal_stage_completion_not_flagged(self, patched):
        """末棒正常收尾（sre，next=done）不报——否则补偿会复活已收敛的链。"""
        patched["rows"] = [(TID, REPO, ISSUE, "sre", _old())]
        patched["texts"] = {TID: json.dumps({"conclusion": "部署完成", "actions": []})}
        patched["expects"] = (False, "")

        assert _run(patched) == []

    def test_downstream_task_exists_not_flagged(self, patched):
        """交棒其实成功了、只是没写 next_step（下游任务已起）→ 非断链。"""
        patched["rows"] = [(TID, REPO, ISSUE, "vod", _old())]
        patched["texts"] = {TID: ""}
        patched["expects"] = (True, "issue")
        patched["l2"] = True

        assert _run(patched) == []

    def test_issue_already_terminal_still_skipped(self, patched):
        """人已收敛 issue → 非断链（终检前置，不因新分支被绕过）。"""
        patched["rows"] = [(TID, REPO, ISSUE, "vod", _old())]
        patched["texts"] = {TID: ""}
        patched["expects"] = (True, "issue")
        patched["terminal"] = "completed"

        assert _run(patched) == []


class TestDeclaredHandoffRegression:
    def test_declared_a2a_still_uses_self_report(self, patched):
        """agent 明确声明 a2a-issue 且 L1/L2 都 miss → 行为与原来完全一致。"""
        patched["rows"] = [(TID, REPO, ISSUE, "vod", _old())]
        patched["texts"] = {TID: json.dumps({"next_step": "a2a-issue"})}
        patched["expects"] = (True, "issue")

        findings = _run(patched)

        assert len(findings) == 1
        detail = json.loads(findings[0]["detail"])
        assert detail["target_agent"] == "issue"
        assert "reason" not in detail, "自报可解析时不应打 no_next_step 标记"
        assert findings[0]["suggestion"] == "补发交棒 → issue"

    def test_declared_handoff_present_not_flagged(self, patched):
        """自报交棒 + L1 命中（交棒已发生）→ 不报。"""
        patched["rows"] = [(TID, REPO, ISSUE, "vod", _old())]
        patched["texts"] = {TID: json.dumps({"next_step": "a2a-issue"})}
        patched["expects"] = (True, "issue")
        patched["l1"] = True

        assert _run(patched) == []


class TestExpectedNextAgent:
    """场景节点判定：只认节点定义，解析不到不乐观判定。"""

    def _patch_scene(self, monkeypatch, nodes, scene_id="custom-x", fallback=False):
        monkeypatch.setattr("src.api.session_store.get_issue_stage",
                            lambda repo, issue: {"scene_id": scene_id} if scene_id else {})
        monkeypatch.setattr("src.api.pipeline_config.get_pipeline_config",
                            lambda sid, repo_key="": {"nodes": nodes, "fallback": fallback})

    def test_vod_expects_issue(self, monkeypatch):
        self._patch_scene(monkeypatch, [
            {"id": "vod", "next": "issue", "handoff": "a2a"},
            {"id": "issue", "next": "done", "handoff": "complete"},
        ])
        assert patrol_mod.expected_next_agent(REPO, ISSUE, "vod") == (True, "issue")

    def test_last_node_does_not_expect(self, monkeypatch):
        self._patch_scene(monkeypatch, [
            {"id": "vod", "next": "issue", "handoff": "a2a"},
            {"id": "sre", "next": "done", "handoff": "complete"},
        ])
        assert patrol_mod.expected_next_agent(REPO, ISSUE, "sre") == (False, "")

    def test_matches_node_tag_too(self, monkeypatch):
        """节点 id 与 tag 不同名时也要能定位（历史场景两种写法都有）。"""
        self._patch_scene(monkeypatch, [
            {"id": "n1", "tag": "vod", "next": "n2", "handoff": "a2a"},
        ])
        assert patrol_mod.expected_next_agent(REPO, ISSUE, "vod") == (True, "n2")

    def test_unknown_agent_not_judged(self, monkeypatch):
        self._patch_scene(monkeypatch, [{"id": "vod", "next": "issue", "handoff": "a2a"}])
        assert patrol_mod.expected_next_agent(REPO, ISSUE, "nobody") == (False, "")

    def test_fallback_scene_not_judged(self, monkeypatch):
        """场景解析不到（fallback）时不做乐观判定：宁可漏报不误报。"""
        self._patch_scene(monkeypatch, [{"id": "vod", "next": "issue", "handoff": "a2a"}],
                          fallback=True)
        assert patrol_mod.expected_next_agent(REPO, ISSUE, "vod") == (False, "")

    def test_missing_scene_id_not_judged(self, monkeypatch):
        self._patch_scene(monkeypatch, [{"id": "vod", "next": "issue", "handoff": "a2a"}],
                          scene_id="")
        assert patrol_mod.expected_next_agent(REPO, ISSUE, "vod") == (False, "")

    def test_exception_degrades_silently(self, monkeypatch):
        monkeypatch.setattr("src.api.session_store.get_issue_stage",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        assert patrol_mod.expected_next_agent(REPO, ISSUE, "vod") == (False, "")
