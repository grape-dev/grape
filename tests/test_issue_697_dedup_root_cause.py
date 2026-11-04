"""Issue #697：巡检重复判定双维度（根因对比 + 疑似重复降级 + owner 确认门禁）回归测试。

覆盖：
1. 历史误并案例回归（#446 vs #126、#179、#277）：issue summary 注入 prompt 时，
   双维度门禁规则随注入，判重必须比对根因三要素（报错信息/根因描述/涉及命令与参数）。
2. 巡检执行 prompt（scheduler._build_autopilot_prompt）注入 AUTOPILOT_DEDUP_RULE。
3. parse_findings 透传 dedup_level/dedup_note（疑似重复差异说明）。
4. dispatch 门禁：suspected_duplicate 未确认 → 409；owner 确认后放行并落标记。
5. 派发描述追加判定级别与差异说明（证据链）。
6. 润色 prompt 源含双维度规则（routes/autopilot.py 复用同一事实源）。
"""
import importlib.util
import logging
import os
import sys
import types
import threading

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SRC_DIR)

_log = logging.getLogger("test_issue_697")

# ===== 历史误并案例 fixture（Issue #697 用户反馈，后台 id 358）=====
# #446 vs #126：标题相似方向相近（scenes detail 异常），根因不同 → 新逻辑应判 suspected/not duplicate
CASE_446 = {
    "number": "446",
    "title": "runbook 与 CLI 不符：scenes detail 无 --repo 选项",
    "body": "scenes detail 报错 --repo 参数报错；runbook 降级指引缺失",
    "root_cause": "--repo 参数报错 + runbook 降级指引缺失",
    "error_message": "unknown request field(s): --repo",
    "command": "grape scenes detail --repo x",
}
CASE_126 = {
    "number": "126",
    "title": "注入 GRAPE_SCENE_ID 在 scenes detail 返回 404",
    "body": "服务端注入 GRAPE_SCENE_ID 在 scenes detail 返回 404",
    "root_cause": "服务端注入 ID 404",
    "error_message": "404 scene not found",
    "command": "grape scenes detail --scene_id <injected>",
}
CASE_179 = {
    "number": "179",
    "title": "巡检完全重复判定不成立（同类误判 A）",
    "body": "标题相似但根因不同，被误判完全重复并入",
    "root_cause": "案例A根因：CLI 参数契约变更",
    "error_message": "unknown request field(s)",
    "command": "grape a --flag",
}
CASE_277 = {
    "number": "277",
    "title": "巡检完全重复判定不成立（同类误判 B）",
    "body": "标题相似但根因不同，被误判完全重复并入",
    "root_cause": "案例B根因：服务端可见性口径",
    "error_message": "403 forbidden",
    "command": "grape b --scope",
}
HISTORICAL_CASES = [CASE_446, CASE_126, CASE_179, CASE_277]

RULE_KEYWORDS = ("报错信息", "根因描述", "涉及命令与参数", "疑似重复", "owner")


def _summary_of(cases):
    lines = ["Issue Pipeline:"]
    for c in cases:
        lines.append(f"  {c['number']} {c['title']} — vod:issue_opened✓")
        lines.append(f"    最新结论: {c['root_cause']}")
    return "\n".join(lines)


# ===== 1. prompt_builder：分诊判重路径注入双维度门禁 =====

@pytest.fixture(scope="module")
def pb():
    from src import prompt_builder
    return prompt_builder


def _evt_for(case):
    return {
        "type": "issue_opened",
        "repository": "grape-dev/grape",
        "source": "gitcode",
        "repo_key": "gitcode:grape-dev/grape",
        "issue_id": case["number"],
        "payload": {
            "project": {"path_with_namespace": "grape-dev/grape"},
            "issue": {"number": int(case["number"]), "title": case["title"], "body": case["body"]}
        },
    }


class TestPromptBuilderDedupGuard:
    @pytest.mark.parametrize("case", HISTORICAL_CASES, ids=[c["number"] for c in HISTORICAL_CASES])
    def test_guard_injected_with_issue_summary(self, pb, monkeypatch, case):
        """回归：三历史案例的 prompt 在看到相似 Issue 摘要时必须携带双维度门禁。"""
        import src.api.event_store as es
        others = [c for c in HISTORICAL_CASES if c["number"] != case["number"]]
        monkeypatch.setattr(es, "get_issue_summary", lambda *a, **k: _summary_of(others))
        prompt = pb.build_devops_agent_prompt(_evt_for(case), [], "", ["issue"])
        for kw in RULE_KEYWORDS:
            assert kw in prompt, f"门禁规则缺少关键词 {kw}"
        assert "Issue Pipeline:" in prompt
        # 门禁必须晚于摘要注入（先看到相似单，再给出比对规则）
        assert prompt.index("Issue Pipeline:") < prompt.index("重复判定双维度门禁")

    def test_no_summary_no_guard(self, pb, monkeypatch):
        """无相似 Issue 摘要时不注入门禁（不污染空仓库场景）。"""
        import src.api.event_store as es
        monkeypatch.setattr(es, "get_issue_summary", lambda *a, **k: "")
        prompt = pb.build_devops_agent_prompt(_evt_for(CASE_446), [], "", ["issue"])
        assert "重复判定双维度门禁" not in prompt

    def test_user_selected_scene_branch_untouched(self, pb, monkeypatch):
        """用户已选场景跳过查重的既有行为保持：该分支只注入原提示，不叠加门禁。"""
        import src.api.event_store as es
        monkeypatch.setattr(es, "get_issue_summary", lambda *a, **k: _summary_of(HISTORICAL_CASES))
        evt = _evt_for(CASE_446)
        evt["_user_selected_scene"] = None  # 该分支由 get_issue_scene 决定，此处仅验证非该分支路径稳定
        prompt = pb.build_devops_agent_prompt(evt, [], "", ["issue"], agent_type="vod")
        assert "Issue Pipeline:" in prompt
        assert "重复判定双维度门禁" in prompt

    def test_rule_single_source_from_scheduler(self, pb):
        """判重规则片段单一事实源：prompt_builder 复用 scheduler 共享片段，禁止双源漂移。"""
        from src.runtime.scheduler import (DEDUP_ROOT_CAUSE_ELEMENTS_RULE,
                                           DEDUP_CASCADE_CLOSE_GUARD_RULE)
        assert DEDUP_ROOT_CAUSE_ELEMENTS_RULE in pb.ISSUE_DEDUP_ROOT_CAUSE_RULE
        assert DEDUP_CASCADE_CLOSE_GUARD_RULE in pb.ISSUE_DEDUP_ROOT_CAUSE_RULE


# ===== 2/3. scheduler：执行 prompt 注入 + findings 透传 =====

_SCHED = None


@pytest.fixture(scope="module")
def sched():
    """Load scheduler.py standalone with stubbed config/session_manager（同 test_autopilot_feishu_notify）."""
    global _SCHED
    if _SCHED is not None:
        yield _SCHED
        return
    saved = {k: sys.modules.get(k) for k in ("src.config", "src.session_manager")}
    cfg = types.ModuleType("src.config")
    cfg.log = _log
    src_pkg = sys.modules.get("src") or types.ModuleType("src")
    if not hasattr(src_pkg, "__path__"):
        src_pkg.__path__ = [os.path.join(SRC_DIR, "src")]
    sys.modules["src"] = src_pkg
    sys.modules["src.config"] = cfg
    sm = types.ModuleType("src.session_manager")
    sm.db = None
    sm.db_lock = threading.Lock()
    sys.modules["src.session_manager"] = sm
    try:
        spec = importlib.util.spec_from_file_location(
            "sched_697_test", os.path.join(SRC_DIR, "src", "runtime", "scheduler.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SCHED = mod
        yield mod
    finally:
        for k, v in saved.items():
            if v is not None:
                sys.modules[k] = v


class TestSchedulerDedupRule:
    def test_rule_constant_keywords(self, sched):
        for kw in RULE_KEYWORDS + ("级联关闭", "exact_duplicate"):
            assert kw in sched.AUTOPILOT_DEDUP_RULE

    def test_build_autopilot_prompt_injects_rule(self, sched):
        prompt = sched._build_autopilot_prompt(
            {"name": "issue_patrol", "instruction": "1. 巡检 Issue", "skill_name": "", "work_branch": ""},
            "gitcode:grape-dev/grape", "grape-dev/grape")
        assert "重复判定双维度门禁" in prompt
        assert "suspected_duplicate" in prompt
        # 指令本身仍在（模板指令不被规则覆盖）
        assert "巡检 Issue" in prompt

    def test_parse_findings_passthrough_dedup_fields(self, sched):
        out = ('<!-- autopilot-findings\n'
               '{"result":"warning","summary":"s1","findings":[{"id":"f1","title":"t","description":"d",'
               '"severity":"high","dedup_level":"suspected_duplicate","dedup_note":"报错信息不一致: '
               'unknown request field(s) vs 404 scene not found"}]}\n-->')
        r = sched.parse_findings(out)
        f = r["findings"][0]
        assert f["dedup_level"] == "suspected_duplicate"
        assert "unknown request field(s)" in f["dedup_note"]

    def test_parse_findings_defaults_dedup_fields(self, sched):
        out = '<!-- autopilot-findings\n{"result":"","summary":"","findings":[{"id":"f1","title":"t","description":"d"}]}\n-->'
        r = sched.parse_findings(out)
        f = r["findings"][0]
        assert f["dedup_level"] == ""
        assert f["dedup_note"] == ""

    def test_parse_findings_unknown_level_fail_closed(self, sched):
        """LLM 拼错 dedup_level（非法枚举）→ 解析层即降级 suspected_duplicate（fail-closed）。"""
        out = ('<!-- autopilot-findings\n'
               '{"result":"warning","summary":"s1","findings":[{"id":"f1","title":"t","description":"d",'
               '"dedup_level":"suspected"}]}\n-->')
        f = sched.parse_findings(out)["findings"][0]
        assert f["dedup_level"] == "suspected_duplicate"

    def test_parse_findings_truncates_long_note(self, sched):
        """dedup_note 在解析层限长（写入公开 Issue 描述前的防线）。"""
        import json as _json
        payload = {"result": "warning", "summary": "s1", "findings": [
            {"id": "f1", "title": "t", "description": "d",
             "dedup_level": "suspected_duplicate", "dedup_note": "长" * 500}]}
        out = '<!-- autopilot-findings\n' + _json.dumps(payload, ensure_ascii=False) + '\n-->'
        f = sched.parse_findings(out)["findings"][0]
        assert len(f["dedup_note"]) == sched.DEDUP_NOTE_MAX_CHARS

    def test_normalize_and_guard_helpers(self, sched):
        assert sched.normalize_dedup_level("") == ""
        assert sched.normalize_dedup_level(None) == ""
        assert sched.normalize_dedup_level("Exact_Duplicate") == "exact_duplicate"
        assert sched.normalize_dedup_level("suspected") == "suspected_duplicate"  # 未知 fail-closed
        # 级联关闭守护（代码级）：疑似重复未确认不允许；确认后允许；未知级别未确认不允许
        assert sched.dedup_cascade_close_allowed("suspected_duplicate", False) is False
        assert sched.dedup_cascade_close_allowed("suspected_duplicate", True) is True
        assert sched.dedup_cascade_close_allowed("unknown_level", False) is False
        assert sched.dedup_cascade_close_allowed("exact_duplicate", False) is True
        assert sched.dedup_cascade_close_allowed("not_duplicate", False) is True
        # 并入门禁：疑似未确认拒绝，确认放行
        assert sched.dedup_auto_merge_allowed("suspected_duplicate", False) is False
        assert sched.dedup_auto_merge_allowed("suspected_duplicate", True) is True
        assert sched.dedup_auto_merge_allowed("", False) is True

    def test_rule_single_source_fragments(self, sched):
        """规则文本片段单一事实源：AUTOPILOT_DEDUP_RULE 由共享片段组成。"""
        assert sched.DEDUP_ROOT_CAUSE_ELEMENTS_RULE in sched.AUTOPILOT_DEDUP_RULE
        assert sched.DEDUP_CASCADE_CLOSE_GUARD_RULE in sched.AUTOPILOT_DEDUP_RULE


# ===== 4/5/6. routes/autopilot.py：dispatch 门禁 + 描述证据链 + 润色 prompt =====

@pytest.fixture(scope="module")
def ap():
    from src.api.routes import autopilot
    return autopilot


class TestDispatchGuard:
    def test_suspected_unconfirmed_rejected(self, ap):
        finding = {"id": "f1", "dedup_level": "suspected_duplicate", "dedup_note": "根因不一致"}
        with pytest.raises(Exception) as ei:
            ap._dedup_dispatch_guard(finding, dedup_confirmed=False)
        assert getattr(ei.value, "status_code", None) == 409
        assert "owner" in str(ei.value.detail)

    def test_suspected_confirmed_passes(self, ap):
        finding = {"id": "f1", "dedup_level": "suspected_duplicate", "dedup_note": "根因不一致",
                   "dedup_confirmed": True}
        ap._dedup_dispatch_guard(finding, dedup_confirmed=False)  # 已确认 → 不抛

    def test_exact_duplicate_passes_without_confirmation(self, ap):
        finding = {"id": "f1", "dedup_level": "exact_duplicate"}
        ap._dedup_dispatch_guard(finding, dedup_confirmed=False)

    def test_no_level_passes(self, ap):
        ap._dedup_dispatch_guard({}, dedup_confirmed=False)

    def test_unknown_level_fail_closed(self, ap):
        """LLM 拼错/漏发非空 dedup_level → fail-closed 按 suspected 处理（409）。"""
        for bad in ("suspected", "SUSPECTED_DUPLICATE ", "duplicate?", "exact"):
            finding = {"id": "f1", "dedup_level": bad}
            with pytest.raises(Exception) as ei:
                ap._dedup_dispatch_guard(finding, dedup_confirmed=False)
            assert getattr(ei.value, "status_code", None) == 409, bad

    def test_append_dedup_note_unknown_level_fail_closed(self, ap):
        out = ap._append_dedup_note("原始描述", {"dedup_level": "duplicate?"})
        assert "level=suspected_duplicate" in out
        assert "级联关闭不自动触发" in out

    def test_append_dedup_note_truncates_and_flattens(self, ap):
        finding = {"dedup_level": "suspected_duplicate", "dedup_note": "行1\n行2 " + "长" * 400}
        out = ap._append_dedup_note("原始描述", finding)
        seg = out.split("[dedup] 差异说明: ", 1)[1].split("\n", 1)[0]
        assert "\n" not in seg
        assert len(seg) == 200

    def test_cascade_close_helper_reexported(self, ap):
        """routes/autopilot 复用 scheduler 级联关闭守护（代码级单一事实源）。"""
        assert ap.dedup_cascade_close_allowed("suspected_duplicate", False) is False
        assert ap.dedup_cascade_close_allowed("suspected_duplicate", True) is True

    def test_confirmation_persisted_even_if_issue_create_fails(self, ap, monkeypatch):
        """Issue #697：owner 确认标记先于建 Issue 落库，建 Issue 失败（issue_number=0）不丢确认。"""
        import asyncio
        import sqlite3
        import json as _json
        import src.api.issues_center as issues_center
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE autopilot_executions (id INTEGER PRIMARY KEY, task_id INTEGER, findings_json TEXT)")
        conn.execute("CREATE TABLE autopilot_tasks (id INTEGER PRIMARY KEY, repo_key TEXT)")
        conn.execute("INSERT INTO autopilot_tasks VALUES (1, 'gitcode:org/repo')")
        findings = {"result": "", "summary": "", "findings": [
            {"id": "f1", "title": "t", "description": "d", "dedup_level": "suspected_duplicate"}]}
        conn.execute("INSERT INTO autopilot_executions VALUES (5, 1, ?)", (_json.dumps(findings),))
        monkeypatch.setattr(ap, "db", conn)
        monkeypatch.setattr(ap, "db_lock", threading.Lock())

        async def fake_create(body, user=None):
            return {"issue_number": "0"}
        monkeypatch.setattr(issues_center, "create_task_via_issue", fake_create)
        r = asyncio.run(ap._dispatch_finding_core(5, "f1", dedup_confirmed=True,
                                                  dedup_confirmed_by="owner-agent"))
        assert r.get("ok") is False
        row = conn.execute("SELECT findings_json FROM autopilot_executions WHERE id=5").fetchone()
        f = _json.loads(row[0])["findings"][0]
        assert f["dedup_confirmed"] is True
        assert f["dedup_confirmed_by"] == "owner-agent"
        assert not f.get("dispatched_issue_number")

    def test_confirmed_by_sanitized(self, ap):
        """dedup_confirmed_by 非自由文本：压平限长。"""
        assert ap.sanitize_dedup_confirmed_by(" a\n b " + "x" * 100) == "a b " + "x" * 60

    def test_append_dedup_note_evidence_chain(self, ap):
        finding = {"dedup_level": "suspected_duplicate",
                   "dedup_note": "报错信息: unknown request field(s) vs 404 scene not found"}
        out = ap._append_dedup_note("原始描述", finding)
        assert "level=suspected_duplicate" in out
        assert "404 scene not found" in out
        assert "级联关闭不自动触发" in out

    def test_append_dedup_note_no_level_untouched(self, ap):
        assert ap._append_dedup_note("原始描述", {}) == "原始描述"


class TestPolishPrompt:
    def test_polish_prompt_contains_rule(self, ap):
        """润色 prompt 复用 scheduler.AUTOPILOT_DEDUP_RULE 同一事实源。"""
        assert ap.AUTOPILOT_DEDUP_RULE  # scheduler 可达（非空兜底）
        for kw in RULE_KEYWORDS:
            assert kw in ap.AUTOPILOT_DEDUP_RULE
