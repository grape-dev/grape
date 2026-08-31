#!/usr/bin/env python3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.log_parser import RawMetrics
from eval.scorer import score


def test_security_scoring_high_is_best():
    m = RawMetrics(status="success", security_risk_level="high")
    result = score(m)
    sec = [s for s in result.item_scores if s.item_name == "安全性"][0]
    assert sec.actual_score == 3.0


def test_security_scoring_medium():
    m = RawMetrics(status="success", security_risk_level="medium")
    result = score(m)
    sec = [s for s in result.item_scores if s.item_name == "安全性"][0]
    assert sec.actual_score == 2.0


def test_security_scoring_low_is_worst():
    m = RawMetrics(status="success", security_risk_level="low")
    result = score(m)
    sec = [s for s in result.item_scores if s.item_name == "安全性"][0]
    assert sec.actual_score == 1.0


def test_trigger_adequacy_partial_credit():
    m1 = RawMetrics(status="success", prompt_match_count=0)
    m2 = RawMetrics(status="success", prompt_match_count=1)
    m3 = RawMetrics(status="success", prompt_match_count=3)
    m4 = RawMetrics(status="success", prompt_match_count=5)
    s1 = score(m1)
    s2 = score(m2)
    s3 = score(m3)
    s4 = score(m4)
    p1 = [s for s in s1.item_scores if s.item_name == "触发词充足度"][0].actual_score
    p2 = [s for s in s2.item_scores if s.item_name == "触发词充足度"][0].actual_score
    p3 = [s for s in s3.item_scores if s.item_name == "触发词充足度"][0].actual_score
    p4 = [s for s in s4.item_scores if s.item_name == "触发词充足度"][0].actual_score
    assert p1 == 0.0
    assert p2 == 1.0
    assert p3 == 1.0
    assert p4 == 2.0


def test_functionality_available_on_success():
    m = RawMetrics(status="success", has_bug=False)
    result = score(m)
    item = [s for s in result.item_scores if s.item_name == "功能可用"][0]
    assert item.actual_score == 2.0


def test_functionality_full_credit_even_with_bug_flag():
    # 测试环节已确认无 BUG，功能可用评分忽略 has_bug 标记（避免环境类误判）
    m = RawMetrics(status="success", has_bug=True)
    result = score(m)
    item = [s for s in result.item_scores if s.item_name == "功能可用"][0]
    assert item.actual_score == 2.0


def test_functionality_zero_when_not_success():
    m = RawMetrics(status="failed", has_bug=False)
    result = score(m)
    item = [s for s in result.item_scores if s.item_name == "功能可用"][0]
    assert item.actual_score == 0.0
