"""判重 2.0 两阶段改造测试(2026-08-18 用户拍板)。

背景: issue #59 "创建skill的skill" 与已有 huawei-cloud-skill-creator 字面
零交集(中文需求 vs 英文 skill 名), 词表粗筛分数=0 被筛掉 -> LLM 未见到
候选 -> 漏判。两阶段: 阶段1 LLM 语义选候选(分批并发, 不受词表限制),
阶段2 精判(现有逻辑不变); 阶段1 失败回退词表粗筛(旧行为)。
"""
import importlib.util
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mk_products(n=25):
    # 默认 25 个(> batch_size 20 -> 强制 2 批, 验证并发合并去重)
    base = {
        "huawei-cloud-skill-creator": "skills/tools/ai/huawei-cloud-skill-creator",
        "huawei-cloud-ecs-server-list": "skills/compute/ecs/huawei-cloud-ecs-server-list",
        "huawei-cloud-vpc-ip-group-create": "skills/network/ops/huawei-cloud-vpc-ip-group-create",
    }
    for i in range(n - len(base)):
        base[f"huawei-cloud-fill-skill-{i:03d}"] = f"skills/fill/f{i:03d}"
    return base


def _mk_idx():
    idx = {
        "huawei-cloud-skill-creator": {
            "summary": "Six-phase pipeline for creating Huawei Cloud skills"},
        "huawei-cloud-ecs-server-list": {
            "summary": "List ECS servers on Huawei Cloud"},
        "huawei-cloud-vpc-ip-group-create": {
            "summary": "Create IP address groups on Huawei Cloud VPC"},
    }
    for i in range(22):
        idx[f"huawei-cloud-fill-skill-{i:03d}"] = {"summary": f"Fill skill {i}"}
    return idx


# ===== 阶段1 单元: 分批并发选候选 =====
def test_stage1_selects_and_merges():
    mod = _load_handler()
    with mock.patch.object(mod, "ENABLE_LLM_DUP_CHECK", True):
        with mock.patch.object(mod, "LLM_API_KEY", "k"):
            with mock.patch.object(mod, "_get_repo_products",
                                   return_value=_mk_products()):
                with mock.patch.object(mod, "_skill_capability_index",
                                       return_value=_mk_idx()):
                    # 2 批, 各返回候选(去重测试: 两批都选 creator)
                    with mock.patch.object(mod, "http_json",
                                           side_effect=[
                                               {"choices": [{"message": {
                                                   "content": '{"candidates": ["huawei-cloud-skill-creator"]}'}}]},
                                               {"choices": [{"message": {
                                                   "content": '{"candidates": ["huawei-cloud-skill-creator", "huawei-cloud-ecs-server-list"]}'}}]},
                                           ]):
                        out = mod._llm_select_skill_candidates(
                            "r/p", "帮我创建一个创建skill的skill", "")
    names = [n for n, _, _ in out]
    assert "huawei-cloud-skill-creator" in names, names  # 语义候选进清单
    assert names.count("huawei-cloud-skill-creator") == 1  # 去重
    assert len(out) == 2, names
    # summary 附带
    for n, p, s in out:
        assert s, f"{n} 应带 summary"


def test_stage1_empty_batch_means_no_candidate():
    mod = _load_handler()
    with mock.patch.object(mod, "ENABLE_LLM_DUP_CHECK", True):
        with mock.patch.object(mod, "LLM_API_KEY", "k"):
            with mock.patch.object(mod, "_get_repo_products",
                                   return_value=_mk_products()):
                with mock.patch.object(mod, "_skill_capability_index",
                                       return_value=_mk_idx()):
                    with mock.patch.object(mod, "http_json",
                                           return_value={"choices": [{"message": {
                                               "content": '{"candidates": []}'}}]}):
                        out = mod._llm_select_skill_candidates(
                            "r/p", "无关内容", "")
    assert out == [], out  # LLM 判无相关 -> 空列表(非 None)


def test_stage1_majority_failure_returns_none():
    # 失败批次过半 -> None(候选不完整, 回退粗筛)
    mod = _load_handler()
    with mock.patch.object(mod, "ENABLE_LLM_DUP_CHECK", True):
        with mock.patch.object(mod, "LLM_API_KEY", "k"):
            with mock.patch.object(mod, "_get_repo_products",
                                   return_value=_mk_products()):
                with mock.patch.object(mod, "_skill_capability_index",
                                       return_value=_mk_idx()):
                    with mock.patch.object(mod, "http_json",
                                           side_effect=RuntimeError("boom")):
                        out = mod._llm_select_skill_candidates(
                            "r/p", "x", "")
    assert out is None, out  # 全部失败 -> None


# ===== 集成: 阶段1 优先 / 回退粗筛 =====
def test_covers_issue_uses_stage1_candidates():
    mod = _load_handler()
    with mock.patch.object(mod, "ENABLE_LLM_DUP_CHECK", True):
        with mock.patch.object(mod, "LLM_API_KEY", "k"):
            with mock.patch.object(mod, "_get_repo_products",
                                   return_value=_mk_products()):
                with mock.patch.object(mod, "_skill_capability_index",
                                       return_value=_mk_idx()):
                    # 阶段1 返回 creator 候选
                    with mock.patch.object(
                            mod, "_llm_select_skill_candidates",
                            return_value=[
                                ("huawei-cloud-skill-creator",
                                 "skills/tools/ai/huawei-cloud-skill-creator",
                                 "Six-phase pipeline for creating Huawei Cloud skills")]):
                        # 阶段2 精判: LLM 判覆盖
                        with mock.patch.object(mod, "http_json",
                                               return_value={"choices": [{"message": {
                                                   "content": '{"covered": true, "skill_name": "huawei-cloud-skill-creator", "reason": "创建skill"}'}}]}):
                            cov, sk, rs = mod._llm_skills_covers_issue(
                                "r/p", "帮我创建一个创建skill的skill", "")
    assert cov is True and sk == "huawei-cloud-skill-creator", (cov, sk, rs)


def test_covers_issue_stage1_empty_returns_early():
    # 阶段1 判无相关 -> 直接返回未覆盖(不调阶段2)
    mod = _load_handler()
    with mock.patch.object(mod, "ENABLE_LLM_DUP_CHECK", True):
        with mock.patch.object(mod, "LLM_API_KEY", "k"):
            with mock.patch.object(mod, "_get_repo_products",
                                   return_value=_mk_products()):
                with mock.patch.object(mod, "_skill_capability_index",
                                       return_value=_mk_idx()):
                    with mock.patch.object(
                            mod, "_llm_select_skill_candidates",
                            return_value=[]):
                        with mock.patch.object(mod, "http_json") as hj:
                            cov, sk, rs = mod._llm_skills_covers_issue(
                                "r/p", "无关", "")
    assert cov is False and "候选为空" in rs, (cov, sk, rs)
    hj.assert_not_called()  # 不调阶段2


def test_covers_issue_fallback_to_coarse_when_stage1_fails():
    # 阶段1 失败(None) -> 回退词表粗筛(旧行为): issue 含 skill 全名 -> 粗筛命中
    mod = _load_handler()
    with mock.patch.object(mod, "ENABLE_LLM_DUP_CHECK", True):
        with mock.patch.object(mod, "LLM_API_KEY", "k"):
            with mock.patch.object(mod, "_get_repo_products",
                                   return_value=_mk_products()):
                with mock.patch.object(mod, "_skill_capability_index",
                                       return_value=_mk_idx()):
                    with mock.patch.object(
                            mod, "_llm_select_skill_candidates",
                            return_value=None):  # 阶段1 失败
                        with mock.patch.object(mod, "http_json",
                                               return_value={"choices": [{"message": {
                                                   "content": '{"covered": true, "skill_name": "huawei-cloud-ecs-server-list", "reason": "x"}'}}]}):
                            cov, sk, rs = mod._llm_skills_covers_issue(
                                "r/p", "帮我查下 huawei-cloud-ecs-server-list 的功能", "")
    assert cov is True and sk == "huawei-cloud-ecs-server-list", (cov, sk, rs)
