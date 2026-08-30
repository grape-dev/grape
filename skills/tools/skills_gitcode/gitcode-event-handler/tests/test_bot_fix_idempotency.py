"""评论转机器人补建路径的幂等加固测试(2026-08-12, issue #102 复盘)。

背景: gitcode_issue_id 依赖 payload 顶层 issue.id, GitCode 格式变化/字段缺失时
解析为 None -> 补建 requirement 落 gitcode_issue_id=NULL, MySQL 唯一索引对
NULL 不生效 -> 每次评论都重复补建 + force_new 新建 Multica 任务(雪崩), 且
NULL 孤儿记录按 id 永远查不回。修复三件套:
  1. normalize_payload 放宽 note 事件 issue 对象构造(不再硬依赖 "iid" 键)
  2. handle_issue_comment 按 id 查不到时用 (number+repo) 兜底查询
  3. 补建前无法定位 issue(id 和 number 皆无)时拒绝补建
"""
import importlib.util
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _note_payload(note_id, note_body, issue=None, repo="g30074593/skillspackage"):
    """构造 GitLab 风格 note payload; issue 可传 None(顶层缺 issue 对象)。"""
    p = {
        "object_kind": "note",
        "object_attributes": {
            "id": note_id, "note": note_body,
            "noteable_type": "Issue",
            "author": {"username": "wj911831", "name": "王"},
        },
        "project": {"path_with_namespace": repo},
    }
    if issue is not None:
        p["issue"] = issue
    return p


# ===== 1. normalize_payload: 顶层 issue 只有 id 没有 iid 也要构造(幂等锚点) =====
def test_note_payload_issue_without_iid_still_parsed():
    mod = _load_handler()
    payload = _note_payload(
        888101, "test",
        issue={"id": 4243001, "title": "t", "description": "d",
               "url": "https://gitcode.com/g30074593/skillspackage/issues/9"},
    )  # 顶层 issue 对象只有 id, 没有 iid(模拟 GitCode 格式变化)
    norm = mod.normalize_payload(payload)
    iss = norm.get("issue") or {}
    assert iss.get("id") == 4243001, \
        f"缺 iid 时 id 仍应解析保留(幂等锚点), 实际 {iss.get('id')}"
    # number 缺 iid 时可为 None, 但 id 必须在 —— 补建 gitcode_issue_id 非 NULL
    assert norm.get("note", {}).get("body") == "test"


# ===== 2. number+repo 兜底: 历史 NULL 记录(有任务执行中) -> 拦截不重复建 =====
def test_bot_fix_number_fallback_reuses_null_record():
    """gitcode_issue_id=NULL 的历史记录: 按 id 查不到, 按 number+repo 兜底命中
    且已有执行中任务 -> 拦截 bot_fix_in_progress, 绝不重复建任务。"""
    mod = _load_handler()
    payload = _note_payload(
        888102, "[@skills_bot](https://gitcode.com/skills_bot) 帮我修复一下",
        issue={"id": None, "iid": 102, "title": "t", "description": "d",
               "state": "opened",
               "url": "https://gitcode.com/g30074593/skillspackage/issues/102",
               "author": {"username": "wj911831", "name": "王"},
               "created_at": "2026-08-11T19:00:00+08:00",
               "updated_at": "2026-08-11T19:00:00+08:00"},
    )
    with mock.patch.object(mod, "db_find_requirement_by_issue_id",
                           return_value=None), \
         mock.patch.object(mod, "db_find_requirement_by_issue_number",
                           return_value={"id": 66, "status": "dispatched",
                                         "multica_issue_id": "m-null",
                                         "multica_issue_number": "SKI-66",
                                         "title": "t", "body": "b"}), \
         mock.patch.object(mod, "gitcode_get", return_value=[]), \
         mock.patch.object(mod, "_multica_cli",
                           return_value=(0, '{"status": "in_progress"}', "")), \
         mock.patch.object(mod, "multica_issue_create",
                           side_effect=AssertionError("已有任务不应新建")), \
         mock.patch.object(mod, "db_insert_requirement",
                           side_effect=AssertionError("已有记录不应补建")), \
         mock.patch.object(mod, "gitcode_issue_comment", return_value=1), \
         mock.patch.object(mod, "safe_notify", return_value=None):
        r = mod.handle_event(payload)
    assert r["event"] == "issue_commented", r
    assert r["action"] == "bot_fix_in_progress", r
    assert r["multica_issue_id"] == "m-null"
    assert r["requirement_id"] == 66


# ===== 3. 普通评论(非转机器人)无 Multica 任务 -> 静默跳过(2026-08-12) =====
def test_comment_no_multica_silent():
    """用户要求: 普通评论(非转机器人意图)无任务时静默跳过, 不再发
    '未找到 Multica 任务'提示 —— bug 类型 issue 已不入库, 无任务时
    评论无需转发; 要转机器人的用户会评论艾特机器人(走转机器人分支)。"""
    mod = _load_handler()
    payload = _note_payload(
        888104, "这个问题我看了一下, 是配置问题",
        issue={"id": 4244679, "iid": 104, "title": "t", "description": "d",
               "state": "opened",
               "url": "https://gitcode.com/g30074593/skillspackage/issues/104",
               "author": {"username": "wj911831", "name": "王"},
               "created_at": "2026-08-12T06:00:00+08:00",
               "updated_at": "2026-08-12T06:00:00+08:00"},
    )
    notified = []
    with mock.patch.object(mod, "db_find_requirement_by_issue_id",
                           return_value=None), \
         mock.patch.object(mod, "db_find_requirement_by_issue_number",
                           return_value=None), \
         mock.patch.object(mod, "is_irrelevant_comment", return_value=False), \
         mock.patch.object(mod, "safe_notify",
                           side_effect=lambda fn, *a, **k:
                           notified.append(getattr(fn, "__name__", str(fn)))):
        r = mod.handle_event(payload)
    assert r["event"] == "issue_commented", r
    assert r["action"] == "skip", r
    assert "gitcode_issue_comment" not in notified, \
        f"普通评论应静默跳过, 实际调用了评论: {notified}"


# ===== 4. codeowner 分配不入库(2026-08-12 用户要求) =====
def test_codeowner_assign_no_db_insert():
    """bug 类型 issue 创建时 codeowner 分配只发评论, 不建 requirement 记录;
    转机器人补建是唯一的入库入口(艾特了才存表跟踪)。"""
    mod = _load_handler()
    with mock.patch.object(mod, "fetch_codeowner_mapping",
                           return_value=([], None)), \
         mock.patch.object(mod, "extract_skill_names",
                           return_value=["huawei-cloud-x"]), \
         mock.patch.object(mod, "_skill_primary_contributor",
                           return_value="Jason7602"), \
         mock.patch.object(mod, "_issue_has_bot_comment",
                           return_value=False), \
         mock.patch.object(mod, "gitcode_issue_comment", return_value=1) as m_c, \
         mock.patch.object(mod, "db_insert_requirement") as m_ins, \
         mock.patch.object(mod, "db_insert_flow") as m_flow:
        r = mod.codeowner_assign_issue(
            "g30074593/skillspackage", 104, "title", "huawei-cloud-x bug",
            4244679, "url", None, None, "wj911831", "wj911831")
    assert r.get("action") == "assigned", r
    assert r.get("requirement_id") is None, "不入库 -> requirement_id 应为 None"
    m_ins.assert_not_called()
    m_flow.assert_not_called()
    m_c.assert_called_once()  # 分配评论照发
def test_bot_fix_number_fallback_then_creates_once():
    """NULL 记录存在但尚无 Multica 任务: 兜底命中后应走正常转机器人流程
    (补建任务 + 更新该记录), 而不是再插一条新 requirement。"""
    mod = _load_handler()
    payload = _note_payload(
        888103, "[@skills_bot](https://gitcode.com/skills_bot) 帮我修复一下",
        issue={"id": None, "iid": 103, "title": "t", "description": "d",
               "state": "opened",
               "url": "https://gitcode.com/g30074593/skillspackage/issues/103",
               "author": {"username": "wj911831", "name": "王"},
               "created_at": "2026-08-11T19:00:00+08:00",
               "updated_at": "2026-08-11T19:00:00+08:00"},
    )
    updated = {}

    def fake_update(rid, data):
        updated.update(data)

    with mock.patch.object(mod, "db_find_requirement_by_issue_id",
                           return_value=None), \
         mock.patch.object(mod, "db_find_requirement_by_issue_number",
                           return_value={"id": 67, "status": "codeowner_assigned",
                                         "multica_issue_id": None,
                                         "multica_issue_number": None,
                                         "title": "t", "body": "b"}), \
         mock.patch.object(mod, "gitcode_get", return_value=[]), \
         mock.patch.object(mod, "multica_issue_create",
                           return_value=("m-667", "SKI-667")), \
         mock.patch.object(mod, "db_insert_requirement",
                           side_effect=AssertionError("已有记录不应再插入新行")), \
         mock.patch.object(mod, "db_update_requirement", side_effect=fake_update), \
         mock.patch.object(mod, "db_insert_flow", return_value=None), \
         mock.patch.object(mod, "gitcode_issue_comment", return_value=1), \
         mock.patch.object(mod, "classify_issue", return_value=("bug", None)), \
         mock.patch.object(mod, "_issue_has_bot_comment", return_value=False), \
         mock.patch.object(mod, "set_issue_labels", return_value=None), \
         mock.patch.object(mod, "safe_notify", return_value=None):
        r = mod.handle_event(payload)
    assert r["action"] == "bot_fix_requested", r
    assert updated.get("multica_issue_id") == "m-667", \
        "应更新已有 NULL 记录的任务关联, 而非插入新行"
    assert updated.get("status") == "dispatched"


# ===== 3. 无法定位 issue(id 和 number 皆无) -> 拒绝补建防雪崩 =====
def test_bot_fix_no_issue_identity_rejects():
    """payload 顶层缺 issue 对象(拿不到 id 和 number): 拒绝补建, 不建任务
    不插记录 —— 宁可失败提示, 不可静默重复建任务。"""
    mod = _load_handler()
    payload = _note_payload(
        888104, "[@skills_bot](https://gitcode.com/skills_bot) 帮我修复一下",
        issue=None,  # 顶层无 issue 对象 -> normalize 后 issue_id/number 皆空
    )
    with mock.patch.object(mod, "db_find_requirement_by_issue_id",
                           return_value=None), \
         mock.patch.object(mod, "db_find_requirement_by_issue_number",
                           return_value=None), \
         mock.patch.object(mod, "gitcode_get", return_value=[]), \
         mock.patch.object(mod, "multica_issue_create",
                           side_effect=AssertionError("不应建任务")), \
         mock.patch.object(mod, "db_insert_requirement",
                           side_effect=AssertionError("不应补建记录")), \
         mock.patch.object(mod, "db_insert_flow",
                           side_effect=AssertionError("不应写 flow")), \
         mock.patch.object(mod, "gitcode_issue_comment", return_value=1), \
         mock.patch.object(mod, "safe_notify", return_value=None):
        r = mod.handle_event(payload)
    assert r["event"] == "issue_commented", r
    assert r["action"] == "bot_fix_skip", r
    assert "无法识别" in r["reason"], r
