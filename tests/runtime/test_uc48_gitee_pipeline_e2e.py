"""UC48-UC49（D104）：gitee 实弹流水线端到端 + 重启中断重派闭环——行为锁。

实弹 E2E（webhook→装配→执行→gitee 回写）属运行时验证，档案见
docs/weekly-09/multi-platform-decouple/d104-gitee-pipeline-e2e-design.md；
本文件锁关键源码锚点与纯函数语义，防回归：
- 字符串 ident 装配链（D102-G/O）不被回潮；
- 中断任务 result 文案（重派入口提示）不被改动；
- redispatch 端点与审计留痕在场；
- 流水线代码零平台字面量（解耦核心预期）。
"""
import os, sys, re

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)


def _read(rel):
    return open(os.path.join(_REPO_ROOT, rel), encoding="utf-8").read()


def test_uc48_string_ident_assembly_chain():
    """UC48：字符串 issue ident 全链路保形——校验闸（D102-G）与传输层保形（D102-O）在场。"""
    webhook = _read("src/domains/runtime/routes/webhook.py")
    assert "_is_valid_issue_number" in webhook, "A2A 入口 ident 校验闸在场"
    assert re.search(r"alnum|isalnum", webhook), "校验语义=字母数字形态（isdigit 门槛已退役）"
    git_repo = _read("src/api/routes/git_repo.py") if os.path.exists(
        os.path.join(_REPO_ROOT, "src/api/routes/git_repo.py")) else ""
    if git_repo:
        assert '"number: int"' not in git_repo.replace(" ", ""), "传输层 number 不再锁 int"


def test_uc48_pipeline_zero_platform_literal():
    """UC48：解耦核心预期——流水线装配链（pipelines.json 场景图）零平台字面量；
    event_pipeline 的平台收敛点唯一（D29/S49 评论投递族）。"""
    import json
    pipes = json.load(open(os.path.join(_REPO_ROOT, "configs/pipelines.json"), encoding="utf-8"))
    blob = json.dumps(pipes, ensure_ascii=False)
    for plat in ("gitee", "github", "huawei"):
        assert plat not in blob, f"场景图出现平台字面量 {plat}（装配必须平台无关）"
    ep = _read("src/api/event_pipeline.py")
    # D29/S49 收敛注释在场（唯一允许的平台字面量点=评论投递族收敛说明）
    assert "评论投递族原先" in ep, "平台字面量收敛锚点（D29/S49）缺失"


def test_uc49_interrupt_result_semantics():
    """UC49：重启中断任务的 result 文案（重派入口提示）不被改动。"""
    ex = _read("src/domains/runtime/agents/executor.py")
    assert '_SHUTDOWN_EVENT_REASON = "任务被系统重启/停机中断，可能未完成，请重派"' in ex, \
        "中断文案常量缺失（运维重派依赖该提示定位）"


def test_uc49_redispatch_endpoint_and_audit():
    """UC49：重派端点与管理端审计留痕在场（#587）。"""
    ev = _read("src/domains/runtime/routes/events.py")
    assert '"/{seq}/redispatch"' in ev, "redispatch 端点缺失"
    assert "_audit_admin_action" in ev, "重派必须审计留痕（#587）"


def test_uc49_qa_skill_wiring():
    """UC48：qa 阶段在场景图上有节点且衔接 coding→qa→后续（livefire 跑通的链）。"""
    import json
    pipes = json.load(open(os.path.join(_REPO_ROOT, "configs/pipelines.json"), encoding="utf-8"))
    nodes = {n["id"]: n for n in pipes["scenes"]["default"]["nodes"]}
    assert "qa" in nodes, "qa 节点缺失"
    assert nodes["coding"]["next"] == "qa", "coding→qa 衔接缺失"
    assert nodes["qa"].get("next"), "qa 无下游衔接"
