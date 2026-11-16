"""F14 守卫：禁用模板 → 引擎下线传导（sandbox_provider_report 读 sandbox_templates.enabled）。

背景（2026-11-13 E2E 实证）：settings 的 sandbox-templates/{id}/toggle 只改
sandbox_templates.enabled，而 provider-report 的引擎注册链路不读该列——禁用
75s+ 后 binding 仍 detected、云引擎行仍 online，「绑定引擎将下线」的 tooltip
承诺无兑现端（凭据卡/引擎卡不同步的根因）。

修复：provider-report 展开模板引擎时取 enabled；禁用模板 → binding 降级
disabled + agent_runtimes（cloud, 同 provider+template）置 offline，并 continue
不再 upsert 引擎。
"""
import inspect


def _report_src() -> str:
    from src.domains.runtime.routes import runtime as rt
    return inspect.getsource(rt.sandbox_provider_report)


def test_report_reads_template_enabled():
    src = _report_src()
    assert "SELECT id, enabled FROM sandbox_templates" in src, \
        "provider-report 必须读取 sandbox_templates.enabled（F14：禁用传导）"


def test_report_offlines_disabled_template_engines():
    src = _report_src()
    assert "UPDATE agent_runtimes SET status='offline'" in src and \
           "runtime_type='cloud' AND provider_id=? AND template_id=?" in src, \
        "禁用模板必须下线绑定云引擎行（凭据卡/引擎卡同步的数据源根因）"
    assert "UPDATE engine_template_binding SET status='disabled'" in src, \
        "禁用模板 binding 应降级 disabled（manual 语义不受影响——只降 detected/空状态）"


def test_disabled_branch_skips_engine_upsert():
    src = _report_src()
    # 禁用分支必须在 ENGINE_REGISTRY 引擎循环之前 continue（不再注册引擎）
    disabled_idx = src.find("UPDATE agent_runtimes SET status='offline'")
    continue_idx = src.find("continue", disabled_idx)
    registry_idx = src.find("ENGINE_REGISTRY", disabled_idx)
    assert disabled_idx != -1 and continue_idx != -1 and registry_idx != -1
    assert continue_idx < registry_idx, "禁用分支须在引擎白名单循环前 continue"
