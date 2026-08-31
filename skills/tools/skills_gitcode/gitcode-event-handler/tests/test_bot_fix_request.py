"""评论触发"转机器人修复"逻辑测试(2026-08-11 用户要求)。"""
import importlib.util
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===== is_bot_fix_request 触发词正例 =====
def test_bot_fix_request_positive_variants():
    mod = _load_handler()
    positives = [
        "让机器人修复这个问题",
        "让机器人来处理吧",
        "转机器人修复",
        "转给机器人处理",
        "机器人来修一下",
        "机器人帮忙修复",
        "由机器人开发",
        "@skills_bot 请修复",
        "请机器人处理",
        "叫机器人改一下",
        # ---- #101 实测: markdown mention 链接格式(先剥离链接外壳再匹配) ----
        "[@skills_bot](https://gitcode.com/skills_bot) 帮我处理一下这些BUG",
        "[@skills_bot](https://gitcode.com/skills_bot) 帮我处理一下这些bug",
        # ---- 2026-08-12 issue #104 修复: "请你帮我修复" 组合句式 ----
        # 原正则 (?:请|帮|帮我|麻烦)? 单个可选组匹配不了 "请"+"你"+"帮我",
        # 艾特后落"未找到 Multica 任务"; 已拆组修复
        "[@skills_bot](https://gitcode.com/skills_bot) 请你帮我修复一下这个bug",
        "[@skills_bot](https://gitcode.com/skills_bot) 请你帮我处理这个bug",
        "[@skills_bot](https://gitcode.com/skills_bot) 麻烦你帮我修复",
        "@skills_bot 请你帮我优化一下",
        # ---- 优化类(2026-08-11 扩展) ----
        "让机器人优化一下这个skill",
        "转机器人优化",
        "机器人来优化一下",
        "@skills_bot 请优化",
        "由机器人优化",
        "请机器人优化查询速度",
    ]
    for text in positives:
        assert mod.is_bot_fix_request(text), f"应命中: {text}"


# ===== is_bot_fix_request 反例(普通评论/无关内容) =====
def test_bot_fix_request_negative_variants():
    mod = _load_handler()
    negatives = [
        "这个问题请尽快修复",
        "谢谢",
        "好的",
        "补充一下复现步骤: 1. 执行命令 2. 报错",
        "请开发者处理",
        "这个bug挺严重的",
        "请问修复了吗",
        "+1",
        "根据以上信息, 请相关同学处理",
    ]
    for text in negatives:
        assert not mod.is_bot_fix_request(text), f"不应命中: {text}"


# ===== 空/异常 fail-open =====
def test_bot_fix_request_empty_failopen():
    mod = _load_handler()
    assert mod.is_bot_fix_request("") is False
    assert mod.is_bot_fix_request(None) is False


# ===== 已有任务在执行中 -> 拦截并回复, 不重复建任务(2026-08-11 用户要求) =====
def test_bot_fix_in_progress_blocked():
    mod = _load_handler()
    payload = {
        "object_kind": "note",
        "object_attributes": {
            "id": 888002, "note": "[@skills_bot](https://gitcode.com/skills_bot) 帮我修复一下",
            "noteable_type": "Issue", "author": {"username": "wj911831", "name": "王"},
        },
        "issue": {"id": 4242850, "iid": 103, "title": "test",
                  "description": "bug 详情", "state": "opened",
                  "url": "https://gitcode.com/r/issues/103",
                  "author": {"username": "wj911831", "name": "王"},
                  "created_at": "2026-08-11T19:00:00+08:00",
                  "updated_at": "2026-08-11T19:00:00+08:00"},
        "project": {"path_with_namespace": "g30074593/skillspackage"},
    }
    with mock.patch.object(mod, "db_find_requirement_by_issue_id",
                           return_value={"id": 55, "status": "dispatched",
                                         "multica_issue_id": "m-act",
                                         "multica_issue_number": "SKI-99",
                                         "title": "t", "body": "b"}), \
         mock.patch.object(mod, "gitcode_get", return_value=[]), \
         mock.patch.object(mod, "_multica_cli",
                           return_value=(0, '{"status": "in_progress"}', "")), \
         mock.patch.object(mod, "multica_issue_create",
                           side_effect=AssertionError("执行中不应新建任务")), \
         mock.patch.object(mod, "gitcode_issue_comment", return_value=1) as m_cmt:
        r = mod.handle_event(payload)
    assert r["event"] == "issue_commented"
    assert r["action"] == "bot_fix_in_progress", r
    assert r["multica_issue_id"] == "m-act"
    m_cmt.assert_called_once()
    body = m_cmt.call_args[0][-1]
    assert "正在执行中" in body, f"应回复正在执行中: {body[:80]}"


# ===== 任务已结束(终态) -> 不拦截(走正常后续流程, 不新建也不卡) =====
def test_bot_fix_done_not_blocked():
    """任务 done 时不应返回 bot_fix_in_progress(拦截已放开)。"""
    mod = _load_handler()
    payload = {
        "object_kind": "note",
        "object_attributes": {
            "id": 888003, "note": "[@skills_bot](https://gitcode.com/skills_bot) 帮我修复一下",
            "noteable_type": "Issue", "author": {"username": "wj911831", "name": "王"},
        },
        "issue": {"id": 4242851, "iid": 104, "title": "test",
                  "description": "bug 详情", "state": "opened",
                  "url": "https://gitcode.com/r/issues/104",
                  "author": {"username": "wj911831", "name": "王"},
                  "created_at": "2026-08-11T19:00:00+08:00",
                  "updated_at": "2026-08-11T19:00:00+08:00"},
        "project": {"path_with_namespace": "g30074593/skillspackage"},
    }
    with mock.patch.object(mod, "db_find_requirement_by_issue_id",
                           return_value={"id": 56, "status": "dispatched",
                                         "multica_issue_id": "m-done",
                                         "multica_issue_number": "SKI-98",
                                         "title": "t", "body": "b"}), \
         mock.patch.object(mod, "gitcode_get", return_value=[]), \
         mock.patch.object(mod, "_multica_cli",
                           return_value=(0, '{"status": "done"}', "")), \
         mock.patch.object(mod, "multica_issue_create",
                           side_effect=AssertionError("不应新建")), \
         mock.patch.object(mod, "db_comment_forwarded", return_value=False), \
         mock.patch.object(mod, "multica_issue_comment", return_value={"id": 1}), \
         mock.patch.object(mod, "gitcode_issue_comment", return_value=1):
        r = mod.handle_event(payload)
    assert r["action"] != "bot_fix_in_progress", \
        f"done 任务不应拦截, 实际 {r['action']}"


# ===== 查询失败(查不到任务状态) -> 保守拦截 =====
def test_bot_fix_query_fail_conservative_block():
    mod = _load_handler()
    payload = {
        "object_kind": "note",
        "object_attributes": {
            "id": 888004, "note": "[@skills_bot](https://gitcode.com/skills_bot) 帮我修复",
            "noteable_type": "Issue", "author": {"username": "wj911831", "name": "王"},
        },
        "issue": {"id": 4242852, "iid": 105, "title": "test",
                  "description": "bug", "state": "opened",
                  "url": "https://gitcode.com/r/issues/105",
                  "author": {"username": "wj911831", "name": "王"},
                  "created_at": "2026-08-11T19:00:00+08:00",
                  "updated_at": "2026-08-11T19:00:00+08:00"},
        "project": {"path_with_namespace": "g30074593/skillspackage"},
    }
    with mock.patch.object(mod, "db_find_requirement_by_issue_id",
                           return_value={"id": 57, "status": "dispatched",
                                         "multica_issue_id": "m-unknown",
                                         "multica_issue_number": "SKI-97",
                                         "title": "t", "body": "b"}), \
         mock.patch.object(mod, "gitcode_get", return_value=[]), \
         mock.patch.object(mod, "_multica_cli",
                           side_effect=Exception("timeout")), \
         mock.patch.object(mod, "multica_issue_create",
                           side_effect=AssertionError("不应新建")), \
         mock.patch.object(mod, "gitcode_issue_comment", return_value=1), \
         mock.patch.object(mod, "safe_notify", return_value=None):
        r = mod.handle_event(payload)
    assert r["action"] == "bot_fix_in_progress", r


# ===== req=None(无 DB 记录)的 issue 也能转机器人(2026-08-11 #101 实测修复) =====
def test_bot_fix_request_without_db_record():
    """codeowner/提交历史分配的 issue 不入库(#182)导致 DB 无记录(req=None),
    用户评论 '@skills_bot 帮我处理一下这些BUG' 应补建 requirement + 建 Multica
    任务转机器人(原实现 `req and ...` 使该分支成为死代码, 永远触发不了)。"""
    mod = _load_handler()
    inserted = {}

    def fake_insert(data):
        inserted.update(data)
        return 555

    payload = {
        "object_kind": "note",
        "object_attributes": {
            "id": 888001,
            "note": "[@skills_bot](https://gitcode.com/skills_bot) 帮我处理一下这些BUG",
            "noteable_type": "Issue",
            "author": {"username": "wj911831", "name": "王"},
        },
        "issue": {
            "id": 4242849, "iid": 101,
            "title": "[huawei-cloud-computing-query]基本功评测发现3条问题",
            "description": "# 问题单: huawei-cloud-computing-query\nbug 详情…",
            "state": "opened",
            "url": "https://gitcode.com/g30074593/skillspackage/issues/101",
            "author": {"username": "wj911831", "name": "王"},
            "created_at": "2026-08-11T19:00:00+08:00",
            "updated_at": "2026-08-11T19:00:00+08:00",
        },
        "project": {"path_with_namespace": "g30074593/skillspackage"},
    }

    with mock.patch.object(mod, "db_insert_requirement", side_effect=fake_insert), \
         mock.patch.object(mod, "db_find_requirement_by_issue_id", return_value=None), \
         mock.patch.object(mod, "db_insert_flow", return_value=None) as m_flow, \
         mock.patch.object(mod, "db_update_requirement", return_value=None), \
         mock.patch.object(mod, "db_update_flow", return_value=None), \
         mock.patch.object(mod, "multica_issue_create",
                           return_value=("m-999", "SKI-999")), \
         mock.patch.object(mod, "multica_issue_comment", return_value=1), \
         mock.patch.object(mod, "gitcode_get", return_value=[]), \
         mock.patch.object(mod, "gitcode_issue_comment", return_value=1), \
         mock.patch.object(mod, "classify_issue", return_value=("bug", None)), \
         mock.patch.object(mod, "_issue_has_bot_comment", return_value=False), \
         mock.patch.object(mod, "set_issue_labels", return_value=None), \
         mock.patch.object(mod, "close_gitcode_issue", return_value=None), \
         mock.patch.object(mod, "safe_notify", return_value=None):
        r = mod.handle_event(payload)
    assert r["event"] == "issue_commented"
    assert r["action"] == "bot_fix_requested", r
    assert inserted.get("status") == "dispatched"
    assert inserted.get("gitcode_issue_id") == 4242849
    assert inserted.get("gitcode_issue_number") == "101"
    assert inserted.get("multica_issue_id") == "m-999", \
        "补建 requirement 应带 multica_issue_id(对齐主路径, 而非 None)"
    assert r["multica_issue_id"] == "m-999"
    assert r["multica_issue_number"] == "SKI-999"
    assert r["requirement_id"] == 555
    # 2026-08-11 对齐主路径: flow 一次性写 node_source + node_preprocess(done)
    assert m_flow.call_args_list, "应调用 db_insert_flow"
    _fc = m_flow.call_args_list[0][0]
    assert "node_source" in _fc[3] and "node_preprocess" in _fc[3], \
        f"flow 应同时写 node_source+node_preprocess, 实际 {list(_fc[3].keys())}"
    assert _fc[3]["node_source"][0] == "done"
    assert _fc[3]["node_preprocess"][0] == "done"
    assert _fc[2] == "m-999", "flow 的 multica_issue_id 应为实际任务 id"
