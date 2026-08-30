"""判重逻辑测试(K3 统一判重 + 各链行为)。"""
from unittest import mock

import gitcode_event_handler as h


def _dup_check(repo, title, body, number, label, **overrides):
    """调用统一判重入口, 应用 mock 覆盖。"""
    defaults = dict(
        is_optimization_request=False,
        ENABLE_DUP_CHECK=True,
        ENABLE_LLM_DUP_CHECK=True,
        LLM_API_KEY="k",
        _check_llm_api_connectivity=True,  # #70: mock LLM 连通性
        _llm_pr_covers_issue=(False, None, "未覆盖"),
        _llm_skills_covers_issue=(False, None, "未覆盖"),
        _llm_find_bug_duplicate=(None, None, "未发现"),
        _llm_find_feature_duplicate=(None, None, None),
        find_duplicate_issue=(None, None),
    )
    defaults.update(overrides)
    ctx = []
    for k, v in defaults.items():
        if k in ("_llm_pr_covers_issue", "_llm_skills_covers_issue", "_llm_find_bug_duplicate",
                 "_llm_find_feature_duplicate", "find_duplicate_issue"):
            ctx.append(mock.patch.object(h, k, return_value=v))
        elif k == "is_optimization_request":
            ctx.append(mock.patch.object(h, k, return_value=v))
        elif k == "_check_llm_api_connectivity":
            ctx.append(mock.patch.object(h, k, return_value=v))
        elif isinstance(v, bool):
            ctx.append(mock.patch.object(h, k, v))  # 模块级开关直接赋值
        else:
            ctx.append(mock.patch.object(h, k, v))
    with _ctx(ctx):
        return h._dup_check_issue(repo, title, body, number, label)[0]


def _ctx(patches):
    class C:
        def __enter__(self):
            for p in patches:
                p.__enter__()
            return self
        def __exit__(self, *a):
            for p in patches:
                p.__exit__(*a)
    return C()


def test_feature_pr_covered():
    hit = _dup_check("r", "新增查询列表", "开发查询列表功能", 1, "feature",
                     _llm_pr_covers_issue=(True, 164, "PR 已实现"))
    assert hit and hit["kind"] == "pr_covered" and hit["num"] == 164


def test_feature_skill_covered():
    hit = _dup_check("r", "新增查询列表", "开发", 2, "feature",
                     _llm_skills_covers_issue=(True, "huawei-cloud-cce-query", "已有"))
    assert hit and hit["kind"] == "skill_covered"
    assert hit["skill_name"] == "huawei-cloud-cce-query"


def test_bug_llm_same_issue():
    hit = _dup_check("r", "huawei-cloud-nat-list有问题", "nat-list有问题", 3, "bug",
                     _llm_find_bug_duplicate=(
                         {"number": 247, "html_url": "https://x/247"}, "open", "同一问题"))
    assert hit and hit["kind"] == "dup_open" and hit["num"] == 247


def test_bug_llm_unavailable_fallback_rule():
    # LLM 不可用 -> 退化规则(不调 _llm_find_bug_duplicate)
    hit = _dup_check("r", "huawei-cloud-nat-list有问题", "nat-list有问题", 4, "bug",
                     LLM_API_KEY="",
                     find_duplicate_issue=(
                         {"number": 247, "html_url": "https://x/247"}, "open"))
    assert hit and hit["kind"] == "dup_open"


def test_closed_other_remind():
    hit = _dup_check("r", "新增查询列表", "开发", 5, "feature",
                     find_duplicate_issue=(
                         {"number": 100, "html_url": "https://x/100", "state": "closed"},
                         "closed_other"))
    assert hit and hit["kind"] == "dup_remind"


def test_db_similar_removed():
    # 2026-08-18 用户拍板: 链4(DB 相似需求)移除 —— issue #63 实测 DB 相似
    # 把中断任务(repoing + PR 关闭未合入)误判 in_progress 误关真实需求。
    # 移除后: 无 DB 相似检查, 判重只走链1/2/3, 相似需求放行(宁漏不误报)。
    hit = _dup_check("r", "新增查询列表", "开发", 6, "feature")
    assert hit is None, hit


def test_optimization_skips_dup():
    hit = _dup_check("r", "优化 huawei-cloud-cce-query", "优化", 7, "feature",
                     is_optimization_request=True)
    assert hit is None


def test_bug_skips_llm_pr_cover():
    # bug 类不调 LLM PR 覆盖(即使 mock 返回 covered 也不命中)
    hit = _dup_check("r", "huawei-cloud-nat-list有问题", "nat", 8, "bug",
                     _llm_pr_covers_issue=(True, 164, "不应命中"),
                     _llm_find_bug_duplicate=(None, None, "未发现"),
                     find_duplicate_issue=(None, None))
    assert hit is None


# ---- 2026-08-17 修复: bug 报告命中优化词(修改/修复)仍走判重, 优化跳过仅对
# feature 生效(#43/#44/#45 评测问题单标题"问题修改"被 is_optimization_request
# 误判优化类 -> 跳过判重 -> 重复 bug 报告各自建任务, #45 撞 Multica duplicate) ----

def test_bug_with_optimization_words_still_dup_checks():
    # bug + is_optimization_request=True: 判重仍执行, 命中重复 -> dup_closed
    hit = _dup_check("r", "huawei-cloud-dns-managemengt 问题修改",
                     "P0 功能缺陷", 44, "bug",
                     is_optimization_request=True,
                     _llm_find_bug_duplicate=(
                         {"number": 41, "html_url": "https://x/41"}, "closed_dup", "同一问题"))
    assert hit and hit["kind"] == "dup_closed" and hit["num"] == 41


def test_bug_with_optimization_words_no_dup_passes():
    # bug + 优化词, 判重未命中 -> None 放行(判重执行过, 只是没发现重复)
    hit = _dup_check("r", "huawei-cloud-dns-managemengt 问题修改",
                     "P0 功能缺陷", 45, "bug",
                     is_optimization_request=True,
                     _llm_find_bug_duplicate=(None, None, "未发现"))
    assert hit is None


def test_feature_optimization_skips_dup_even_with_candidate():
    # feature 优化类: 即使有重复候选也跳过判重(2.0 规则 3 语义保留)
    hit = _dup_check("r", "优化 huawei-cloud-cce-query", "优化", 7, "feature",
                     is_optimization_request=True,
                     _llm_find_bug_duplicate=(
                         {"number": 41, "html_url": "https://x/41"}, "open", "同一问题"))
    assert hit is None


def test_optimize_words_no_longer_contains_bug_word():
    # "修复"已从 OPTIMIZE_WORDS 移除(纯 bug 语义词, 2026-08-17)
    assert "修复" not in h.OPTIMIZE_WORDS
    # "修改"保留(优化需求核心词), 但 bug 类判重不再受其影响
    assert "修改" in h.OPTIMIZE_WORDS
    # 纯修复意图文本不再命中优化类
    assert h.is_optimization_request(
        "huawei-cloud-dns-managemengt 功能缺陷", "P0 功能缺陷") is False
    # 评测问题单标题"问题修改"仍命中(走优化词), 由 label==bug 兜底保证判重
    assert h.is_optimization_request(
        "huawei-cloud-dns-managemengt 问题修改", "P0 功能缺陷") is True


# ---- Feature dedup: Jaccard + LLM 两阶段判重(三条防线, 2026-08-18) ----

def test_feature_jaccard_llm_dup():
    """feature 类 Jaccard+LLM 判 duplicate -> feature_dup"""
    hit = _dup_check("r", "创建ECS安全组", "需要创建安全组", 81, "feature",
                     _llm_find_feature_duplicate=(
                         {"number": 79, "html_url": "https://x/79"}, "feature_dup", "功能完全相同"))
    assert hit and hit["kind"] == "feature_dup"
    assert hit["num"] == 79


def test_feature_jaccard_llm_related():
    """feature 类 Jaccard+LLM 判 partial/medium -> feature_related"""
    hit = _dup_check("r", "查询ECS安全组", "需要查询", 82, "feature",
                     _llm_find_feature_duplicate=(
                         {"number": 80, "html_url": "https://x/80"}, "feature_related", "功能部分重叠"))
    assert hit and hit["kind"] == "feature_related"
    assert hit["num"] == 80
    assert hit["reason"]  # reason should not be empty


def test_feature_jaccard_llm_no_match():
    """feature 类 Jaccard+LLM 无匹配 fallback 到 find_duplicate_issue"""
    hit = _dup_check("r", "新增查询ECS", "开发", 83, "feature",
                     _llm_find_feature_duplicate=(None, None, None),
                     find_duplicate_issue=(None, None))
    assert hit is None


def test_feature_jaccard_llm_bug_skip():
    """bug 类不调用 _llm_find_feature_duplicate(标签非 feature)"""
    bug_dup = {"number": 41, "html_url": "https://x/41"}
    hit = _dup_check("r", "huawei-cloud-nat有问题", "网络异常", 84, "bug",
                     _llm_find_bug_duplicate=(bug_dup, "open", "同一问题"))
    assert hit and hit["kind"] == "dup_open" and hit["num"] == 41
    # bug 路径不走 feature dedup, 即使 mock 返回也忽略
    # (确认默认 mock (None,None,None) 不影响 bug 路径)


def test_feature_jaccard_llm_exact_match_first():
    """skill 名精确匹配优先于 Jaccard+LLM(feature_dup 是链 3.5, 链 3 先命中)"""
    hit = _dup_check("r", "huawei-cloud-cce-query新功能", "开发", 85, "feature",
                     find_duplicate_issue=(
                         {"number": 100, "html_url": "https://x/100"}, "open"),
                     # even if LLM returns feature_dup, chain 3 (exact) wins
                     _llm_find_feature_duplicate=(
                         {"number": 79, "html_url": "https://x/79"}, "feature_dup", "功能"),
    )
    assert hit and hit["kind"] == "dup_open"
    assert hit["num"] == 100


# ---- 2026-08-18 修复: 判重评论文案按 label 区分 —— bug/问题类用问题语义
# (该问题与 #N 重复/跟进处理), 需求类保持需求语义(#47 评测问题单收到
# "该需求与 #45 重复...避免重复开发"与问题类场景不符) ----

def test_dup_comment_msg_bug_semantics():
    msg = h._dup_comment_msg("bug", "dup", 45, "https://x/45")
    assert "该问题与 [#45]" in msg and "已在原 issue 中跟进处理" in msg
    assert "该需求" not in msg and "避免重复开发" not in msg


def test_dup_comment_msg_bug_remind_semantics():
    msg = h._dup_comment_msg("bug", "remind", 45, "https://x/45", "closed")
    assert "该问题与已关闭的 [#45]" in msg and "复现步骤" in msg
    assert "该需求" not in msg


def test_dup_comment_msg_feature_keeps_original():
    msg = h._dup_comment_msg("feature", "dup", 45, "https://x/45")
    assert "该需求与 [#45]" in msg and "避免重复开发" in msg
    msg2 = h._dup_comment_msg("feature", "remind", 45, "https://x/45", "closed")
    assert "该需求与已关闭的 [#45]" in msg2


def test_dup_comment_msg_marker_compat():
    # 幂等 marker 兼容: bug 文案必须包含 MARKERS 里用于幂等检查的关键词
    bug_dup = h._dup_comment_msg("bug", "dup", 45, "https://x/45")
    bug_rem = h._dup_comment_msg("bug", "remind", 45, "https://x/45", "closed")
    assert "已在原 issue 中跟进" in bug_dup       # dup_followup marker
    assert "与已关闭的" in bug_rem                 # dup_closed_similar marker(共性子串)


# ---- 2026-08-18: 判重评论带任务状态+进展链接(被关闭 issue 用户知道去哪跟进) ----

def test_dup_task_suffix_with_task():
    # #83 用户要求判重回复不体现内部任务信息, 函数统一返回空
    assert h._dup_task_status_suffix("r", 41) == ""


def test_dup_task_suffix_no_task_returns_empty():
    with mock.patch.object(h, "db_find_requirement_by_issue_number",
                           return_value=None):
        assert h._dup_task_status_suffix("r", 45) == ""


def test_dup_task_suffix_query_fail_returns_empty():
    with mock.patch.object(h, "db_find_requirement_by_issue_number",
                           side_effect=Exception("db down")):
        assert h._dup_task_status_suffix("r", 45) == ""
