"""classify_issue 加固测试(2026-08-13 #120 事故修复):
1. 强 bug 信号(评测标题/正文报错等)直接判 bug, 跳过 LLM 复核
2. LLM 复核改判 feature 必须有需求证据词, 否则保持 bug
3. 创建时自带 feature/bug 标签优先, 跳过重新分类
"""
from unittest import mock

import gitcode_event_handler as h


# ===== 改动2: 强 bug 信号直接判 bug(跳过 LLM) =====
def test_strong_bug_eval_title():
    # #120 事故现场: 评测问题单标题 -> 直接 bug, 不调 LLM
    with mock.patch.object(h, "_llm_classify", side_effect=AssertionError("不应调 LLM")):
        label, reasons = h.classify_issue(
            "[huawei-cloud-monitoring-query]基本功评测发现1条问题", "正文内容")
    assert label == "bug"
    assert any("强 bug 信号" in r for r in reasons)


def test_strong_bug_body_error():
    # 正文强 bug 词(报错) -> 直接 bug
    with mock.patch.object(h, "_llm_classify", side_effect=AssertionError("不应调 LLM")):
        label, _ = h.classify_issue("查询RDS监控数据", "查询时报错, 未穷举不支持的服务列表")
    assert label == "bug"


def test_strong_bug_no_llm_review():
    # 强信号命中时 LLM 复核不应被调用(核心: 杜绝 #120 式改判)
    with mock.patch.object(h, "_llm_classify", side_effect=AssertionError("强信号不应走 LLM 复核")):
        label, _ = h.classify_issue("huawei-cloud-xxx 查询报错", "查询失败")
    assert label == "bug"


def test_feature_not_affected_by_strong_signal():
    # 正常需求不受强信号影响
    label, _ = h.classify_issue("新增查询列表 skill", "需要开发一个查询华为云 CCE 集群列表的 skill")
    assert label == "feature"


# ===== 改动1: LLM 复核改判 feature 需需求证据 =====
def test_llm_reclassify_no_evidence_keeps_bug():
    # LLM 判 feature 但 body 无需求证据词 -> 保持 bug(#120 根因)
    with mock.patch.object(h, "_llm_classify",
                           return_value=("feature", "LLM 认为是需求")):
        label, reasons = h.classify_issue("nat-list 查询有问题", "返回的结果数量不对")
    assert label == "bug"
    assert any("无需求证据" in r for r in reasons)


def test_llm_reclassify_with_evidence_accepts():
    # LLM 判 feature 且 body 含需求证据词(新增/支持XX功能) -> 采纳
    with mock.patch.object(h, "_llm_classify",
                           return_value=("feature", "LLM 认为是新增需求")):
        label, reasons = h.classify_issue(
            "nat-list 查询有问题", "需要新增支持查询 nat 列表的功能, 请开发")
    assert label == "feature"
    assert any("有需求证据" in r for r in reasons)


def test_llm_confirm_bug_keeps_bug():
    # LLM 复核一致判 bug -> 保持 bug
    with mock.patch.object(h, "_llm_classify",
                           return_value=("bug", "LLM 确认是缺陷")):
        label, reasons = h.classify_issue("nat-list 查询有问题", "返回结果数量不对")
    assert label == "bug"


def test_llm_fail_keeps_rule_bug():
    # LLM 复核失败(fail-open) -> 保持规则 bug
    with mock.patch.object(h, "_llm_classify", return_value=(None, "API 超时")):
        label, reasons = h.classify_issue("nat-list 查询有问题", "返回结果数量不对")
    assert label == "bug"


# ===== 改动3: 创建时标签优先 =====
def test_created_label_payload_feature():
    # payload 自带 feature 标签 -> 直接 feature, 跳过 classify
    payload = {
        "object_attributes": {"labels": ["feature", "triaged"], "iid": 999},
        "project": {"path_with_namespace": "g30074593/skillspackage"},
    }
    with mock.patch.object(h, "classify_issue", side_effect=AssertionError("不应重新分类")):
        label, reasons = h._created_label_override(
            payload, "g30074593/skillspackage", 999, "xxx有问题", "查询报错")
    assert label == "feature"
    assert any("创建时自带标签" in r for r in reasons)


def test_created_label_payload_bug():
    payload = {
        "object_attributes": {"labels": ["bug", "triaged"], "iid": 998},
        "project": {"path_with_namespace": "g30074593/skillspackage"},
    }
    label, _ = h._created_label_override(
        payload, "g30074593/skillspackage", 998, "新增一个skill", "开发查询功能")
    assert label == "bug"


def test_created_label_api_fallback():
    # payload 无标签 -> API 兜底返回 bug
    payload = {
        "object_attributes": {"iid": 997},
        "project": {"path_with_namespace": "g30074593/skillspackage"},
    }
    with mock.patch.object(h, "_api_issue_labels", return_value=["bug", "triaged"]):
        label, _ = h._created_label_override(
            payload, "g30074593/skillspackage", 997, "xxx", "yyy")
    assert label == "bug"


def test_created_label_none_falls_through():
    # 无标签 -> 返回 None, 由调用方走 classify_issue
    payload = {"object_attributes": {"iid": 996}}
    with mock.patch.object(h, "_api_issue_labels", return_value=[]):
        label, _ = h._created_label_override(
            payload, "g30074593/skillspackage", 996, "新增一个skill", "开发查询")
    assert label is None
