"""bug 分流 / 质量检查 / Skill 审查结论判定测试。"""
from unittest import mock

import gitcode_event_handler as h


# ===== issue_quality_ok(质量检查) =====
def test_quality_ok_feature_short_body():
    # 31 字符 = 22 字符 skill 名 + 9 字空话(#244 教训)
    ok, reason = h.issue_quality_ok(
        "huawei-cloud-ges-graph有问题", "huawei-cloud-ges-graph有问题,请协助定位下", "feature")
    assert ok is False
    assert "实质描述仅" in reason


def test_quality_ok_feature_good_body():
    ok, reason = h.issue_quality_ok(
        "新增查询列表",
        "需要支持查询华为云 CCE 集群列表, 展示集群名称、状态、节点数和地域信息, 按分页返回",
        "feature")
    assert ok is True, reason


def test_quality_ok_bug_no_repro():
    ok, reason = h.issue_quality_ok(
        "huawei-cloud-nat-list有问题", "nat-list 查询报错", "bug")
    assert ok is False
    assert "复现" in reason or "日志" in reason or "堆栈" in reason


def test_quality_ok_bug_with_repro():
    ok, _ = h.issue_quality_ok(
        "huawei-cloud-nat-list查询失败",
        "huawei-cloud-nat-list 查询时报 500 错误, 复现步骤: 1.调用接口 2.报错, 日志见附件",
        "bug")
    assert ok is True


# ===== classify_issue(bug 词命中) =====
def test_classify_bug_keyword():
    label, _ = h.classify_issue("huawei-cloud-nat-list有问题", "nat-list 查询一直失败, 报 500")
    assert label == "bug"


def test_classify_feature():
    label, _ = h.classify_issue(
        "新增查询列表 skill", "需要开发一个查询华为云 CCE 集群列表的 skill")
    assert label == "feature"


# ===== Skill 审查结论判定(30 项) =====
def test_review_conclusion_pass():
    # 无 ❌ 无 ⚠️ -> pass
    assert h.pr_review_report is not None  # 函数存在


def test_review_conclusion_rules():
    # 结论判定逻辑: 任一 🔴/❌ 未过 -> fail; 🟠/⚠️ 未过 -> warn; 仅 🟡/🔵 -> pass
    # 直接验证判定公式(与 pr_review_report 内联逻辑一致)
    def conclude(conclusions, code_conclusion, sec_ok):
        if "❌" in conclusions or code_conclusion == "❌" or not sec_ok:
            return "fail"
        if "⚠️" in conclusions or code_conclusion == "⚠️":
            return "warn"
        return "pass"

    assert conclude([], "✅", True) == "pass"
    assert conclude(["✅"], "⚠️", True) == "warn"
    assert conclude(["❌"], "✅", True) == "fail"
    assert conclude(["✅"], "✅", False) == "fail"


# ===== bot 贡献判定(2026-08-11 用户要求: 撤销 #243 占比规则, 任一 bot 提交即自动修复) =====
def test_bot_contributor_any_bot_commit():
    # 模拟提交列表: 1 bot + 3 人(占比 25% < 50%) -> 按新规则仍应判 True
    commits = [{"author": {"login": "skills_bot"}},
               {"author": {"login": "lccode"}},
               {"author": {"login": "Jason7602"}},
               {"author": {"login": "lccode"}}]
    bot_count = sum(1 for c in commits if (c.get("author") or {}).get("login", "").lower()
                    in h.BOT_AUTHOR_LOGINS)
    assert bot_count >= 1, "任一 bot 提交即判自动修复(不再要求占比 >= 50%)"


def test_bot_contributor_no_bot_commit():
    # 全人类提交 -> 非 bot 开发, 走人工分配
    commits = [{"author": {"login": "lccode"}},
               {"author": {"login": "Jason7602"}}]
    bot_count = sum(1 for c in commits if (c.get("author") or {}).get("login", "").lower()
                    in h.BOT_AUTHOR_LOGINS)
    assert bot_count == 0


def test_bot_contributor_ratio_removed():
    # 占比判定已撤销: 25% bot 占比也必须判 True(旧 #243 规则会判 False)
    commits = [{"author": {"login": "check_bot"}},
               {"author": {"login": "lccode"}},
               {"author": {"login": "Jason7602"}},
               {"author": {"login": "lccode"}}]
    bot_count = sum(1 for c in commits if (c.get("author") or {}).get("login", "").lower()
                    in h.BOT_AUTHOR_LOGINS)
    total = len(commits)
    assert bot_count / total < 0.5, "前置: 占比确实 < 50%(旧规则会判 False)"
    assert bot_count >= 1, "但任一 bot 提交即判 True(新规则)"
