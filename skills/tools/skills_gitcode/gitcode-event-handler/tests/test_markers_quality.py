"""质量检查 / 幂等缓存 / marker 常量测试(S2/P1/S3)。"""
from unittest import mock

import gitcode_event_handler as h


# ===== S3: MARKERS 常量 =====
def test_markers_are_real_substrings_of_comments():
    """marker 必须是评论正文真实子串(历史教训 #163/#170/1.4)。"""
    cases = [
        (h.MARKERS["dup_followup"], "🔁 该需求与 #1 重复, 已在原 issue 中跟进"),
        (h.MARKERS["dup_closed_similar"], "ℹ️ 该需求与已关闭的 #1 相似"),
        (h.MARKERS["dup_pr_covered"], "📦 该功能点已有 PR 实现: [#1](https://x)"),
        (h.MARKERS["dup_skill_covered"], "📦 该功能点已有 skill 实现: `x`"),
        (h.MARKERS["repo_has_skill"], "📦 该需求对应的 skill 已在仓库中开发过"),
        (h.MARKERS["open_pr_has_skill"], "📦 该 skill 已有 PR 提交"),
        (h.MARKERS["competitor_cloud"], "☁️ 该需求面向其他云厂商"),
        (h.MARKERS["unrelated_repo"], "该 Issue 与仓库项目范围无关"),
        (h.MARKERS["codeowner_assigned"], "已按 codeowner 映射分配给 兜底负责人"),
        (h.MARKERS["codeowner_assigned"], "已按 codeowner 映射分配(依据:**提交历史**)给主要开发者 @x 处理"),  # _assign_contributor 变体
        (h.MARKERS["bot_fix_requested"], "🤖 已转为机器人修复, 任务已派发给开发小队。"),  # 2026-08-11 修正: 旧文案"已转为机器人处理"不含 marker
        (h.MARKERS["need_info"], "⚠️ 需求信息不完整: 正文仅 9 字"),
        (h.MARKERS["dev_in_progress_lock"], "⚠️ 该需求已在开发中(Multica 任务 SKI-1), 对 issue 的修改**不生效**, 开发仍按原需求进行"),
        (h.MARKERS["comment_change_rej"], "评论中的变更要求(**改成**)**不生效**, 开发仍按原需求进行"),
        (h.MARKERS["bug_need_skill_name"], "未提供具体的 skill 名称"),
        (h.MARKERS["bug_bad_skill_fmt"], "`huawei-cloud-ascend` 不是完整的 skill 名称"),
        (h.MARKERS["bug_skill_not_found"], "未找到与 `x` 完全匹配的 skill"),
        (h.MARKERS["bug_repo_no_skill"], "仓库中不存在 skill `x`"),
        (h.MARKERS["opt_need_skill_name"], "未明确指定要优化的 skill"),
        (h.MARKERS["pr_review_notified"], "PR 审查未通过, 已通知开发小队进行处理"),
        (h.MARKERS["pr_review_fix_req"], "@lccode 请根据上方 PR 审查报告修改后重新提交。"),
        (h.MARKERS["pr_merged_dev_notice"], "✅ PR #1 已合入, @lccode 请关注后续处理。"),
        (h.MARKERS["pr_merged_done"], "✅ 该需求已开发完成并合入主仓 (PR #1 已合并)。"),
        (h.MARKERS["pr_merged_issue_open"], "📌 关联的 PR #1 已合入, 但该 issue 还有其他 PR 在处理中, 本 issue 暂不关闭, 待全部 PR 合入后关闭。"),
        (h.MARKERS["pr_review_report"], "## PR 审查报告"),
    ]
    for marker, body in cases:
        assert marker in body, f"marker '{marker}' 不在正文 '{body[:40]}' 中(幂等会失效)"


def test_waiting_prompt_markers_use_constants():
    assert all(m in h.MARKERS.values() for m in h.WAITING_PROMPT_MARKERS)


# ===== P1: 评论缓存 =====
def test_comments_cache_hit():
    h._COMMENTS_CACHE.clear()
    calls = [0]

    def fake_get(path):
        calls[0] += 1
        return [{"body": "[skills_bot] 🔁 该需求与 #1 重复, 已在原 issue 中跟进"}]

    with mock.patch.object(h, "gitcode_get", side_effect=fake_get):
        assert h._issue_has_bot_comment("r", 1, h.MARKERS["dup_followup"]) is True
        assert h._issue_has_bot_comment("r", 1, h.MARKERS["dup_followup"]) is True
        assert calls[0] == 1  # 缓存命中, 只拉一次


def test_comments_cache_invalidated_on_post():
    h._COMMENTS_CACHE.clear()
    calls = [0]

    def fake_get(path):
        calls[0] += 1
        return []

    with mock.patch.object(h, "gitcode_get", side_effect=fake_get), \
         mock.patch.object(h, "gitcode_post", return_value={"id": 1}):
        h._issue_has_bot_comment("r", 2, "x")      # 拉一次
        assert calls[0] == 1
        h.gitcode_issue_comment("r", 2, "[skills_bot] 已发")  # 发布 -> 失效
        h._issue_has_bot_comment("r", 2, "x")      # 重新拉
        assert calls[0] == 2


def test_pr_comments_cache_separate():
    h._COMMENTS_CACHE.clear()
    calls = [0]

    def fake_get(path):
        calls[0] += 1
        return [{"body": "[skills_bot] PR 审查未通过, 已通知开发小队进行处理"}]

    with mock.patch.object(h, "gitcode_get", side_effect=fake_get):
        assert h._pr_has_bot_comment("r", 1, h.MARKERS["pr_review_notified"]) is True
        assert calls[0] == 1


# ===== S2: need_info 统一函数 =====
def test_need_info_requirement():
    inserted = {}

    def fake_insert(data):
        inserted.update(data)
        return 555

    with mock.patch.object(h, "gitcode_issue_comment", return_value=1), \
         mock.patch.object(h, "_issue_has_bot_comment", return_value=False):
        r = h._need_info_requirement(
            "r", 900, 999, "https://x/900", "t", "b",
            "lccode", "汪", "2026-08-09", "2026-08-09", "正文仅 9 字")
        assert r["action"] == "need_info" and r["requirement_id"] is None
        assert inserted == {}  # 方案A: 不再入库, inserted 未被写
