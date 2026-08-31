"""同 skill 多 PR 入口硬门槛 + 检测点32 空文件检查测试(2026-08-16 用户要求)。

背景: 原检测点32「同一个Skill不能存在多个PR」(🔴 审查内)上移为 handle_event
入口硬门槛(check_pr_dup_skill, 在 check_pr_file_limit 之前执行): 命中重复
直接阻断(评论提示 PR 创建者 + 跳过整个 PR 处理)。原 32 位替换为
「空文件检查」(🔵 提示级, 列出空文件名)。
"""
import importlib.util
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"

SKILL_DIR = "skills/network/ops/huawei-cloud-e2e-dupcheck"
MD = ("---\nname: huawei-cloud-e2e-dupcheck\ndescription: dup check test\n---\n"
      "# 概述\n测试\n# 前置条件\n无\n")


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _review(mod, repo="g30074593/skillspackage", pr_number=100):
    files = [{"filename": f"{SKILL_DIR}/SKILL.md", "patch": "", "raw_url": ""}]
    contents = {f"{SKILL_DIR}/SKILL.md": MD}
    return mod._skill_review(SKILL_DIR, files, contents, repo, pr_number)


def _item32(results):
    return [r for r in results if r[0] == 32][0]


# ==================== 入口硬门槛 check_pr_dup_skill ====================
def _mock_pr_files(mod, files_by_pr, current_pr=100):
    """mock _fetch_pr_files 与 gitcode_get(open PR 列表 + 各 PR 文件)"""
    def fake_fetch(repo, number):
        return [{"filename": fn} for fn in files_by_pr.get(number, [])]

    def fake_get(url):
        if url.endswith("/pulls?state=open&per_page=100"):
            return [{"number": n} for n in files_by_pr if n != current_pr]
        if "/files" in url:
            pr_part = url.split("/pulls/")[1].split("/files")[0]
            try:
                n = int(pr_part)
            except ValueError:
                return []
            return [{"filename": fn} for fn in files_by_pr.get(n, [])]
        return []
    return (mock.patch.object(mod, "_fetch_pr_files", side_effect=fake_fetch),
            mock.patch.object(mod, "gitcode_get", side_effect=fake_get))


def test_gate_no_dup_returns_false():
    mod = _load_handler()
    files_by_pr = {
        100: [f"{SKILL_DIR}/SKILL.md"],
        101: ["skills/compute/ecs/huawei-cloud-ecs-list/SKILL.md"],
    }
    with _mock_pr_files(mod, files_by_pr)[0], \
         _mock_pr_files(mod, files_by_pr)[1]:
        assert mod.check_pr_dup_skill("r/r", 100) is False, "无重复应放行"


def test_gate_dup_blocks_and_comments():
    mod = _load_handler()
    files_by_pr = {
        100: [f"{SKILL_DIR}/SKILL.md"],
        103: [f"{SKILL_DIR}/scripts/run.sh"],  # 同 skill -> 重复
    }
    with _mock_pr_files(mod, files_by_pr)[0], \
         _mock_pr_files(mod, files_by_pr)[1], \
         mock.patch.object(mod, "_pr_has_bot_comment", return_value=False), \
         mock.patch.object(mod, "gitcode_pr_comment", return_value=1) as m_comment, \
         mock.patch.object(mod, "safe_notify", side_effect=lambda *a, **k: None) as m_notify:
        blocked = mod.check_pr_dup_skill("r/r", 100,
                                         pr={"user": {"login": "lccode"}})
    assert blocked is True, "存在重复 PR 应阻断"
    assert m_notify.called, "应发评论提示 PR 创建者"


def test_gate_self_excluded():
    mod = _load_handler()
    files_by_pr = {
        100: [f"{SKILL_DIR}/SKILL.md"],
        # 无其他 PR 涉及同一 skill
        101: ["skills/storage/rds/huawei-cloud-rds-list/SKILL.md"],
    }
    with _mock_pr_files(mod, files_by_pr)[0], \
         _mock_pr_files(mod, files_by_pr)[1]:
        assert mod.check_pr_dup_skill("r/r", 100) is False, "自身应排除"


def test_gate_query_fail_fail_open():
    mod = _load_handler()
    with mock.patch.object(mod, "_fetch_pr_files", side_effect=RuntimeError("429")), \
         mock.patch.object(mod, "gitcode_get", side_effect=RuntimeError("429")):
        assert mod.check_pr_dup_skill("r/r", 100) is False, "查询失败应放行(fail-open)"


def test_gate_no_skill_files_returns_false():
    mod = _load_handler()
    with mock.patch.object(mod, "_fetch_pr_files", return_value=[
            {"filename": "README.md"}]), \
         mock.patch.object(mod, "gitcode_get", return_value=[]):
        assert mod.check_pr_dup_skill("r/r", 100) is False, "无 skill 变更无从判重"


# ==================== 入口硬门槛接入 handle_event ====================
def _pr_payload(number=100):
    return {
        "object_kind": "merge_request",
        "object_attributes": {
            "id": 999, "iid": number, "number": number,
            "title": "update skill: huawei-cloud-e2e-dupcheck",
            "description": "update skill",
            "action": "open", "state": "opened",
            "source_branch": "dev/dup", "target_branch": "master",
            "url": f"https://gitcode.com/r/r/pulls/{number}",
            "author": {"username": "lccode", "name": "汪亮初"},
            "created_at": "2026-08-16T10:00:00+08:00",
        },
        "project": {"path_with_namespace": "r/r"},
        "merge_request": {"id": 999, "iid": number, "number": number,
                          "title": "update skill", "state": "opened",
                          "author": {"username": "lccode", "name": "汪亮初"}},
    }


def test_event_dup_skill_skips_handle_pr():
    mod = _load_handler()
    payload = _pr_payload()
    with mock.patch.object(mod, "get_repo_full_name", return_value="r/r"), \
         mock.patch.object(mod, "check_pr_dup_skill", return_value=True), \
         mock.patch.object(mod, "handle_pr",
                           side_effect=AssertionError("阻断后不应进 handle_pr")):
        r = mod.handle_event(payload)
    assert r["action"] == "skipped_dup_skill", str(r)
    assert "open PR" in r["reason"]


def test_event_dup_skill_before_file_limit():
    mod = _load_handler()
    payload = _pr_payload()
    order = []
    with mock.patch.object(mod, "get_repo_full_name", return_value="r/r"), \
         mock.patch.object(mod, "check_pr_dup_skill",
                           side_effect=lambda *a, **k: order.append("dup") or False), \
         mock.patch.object(mod, "check_pr_file_limit",
                           side_effect=lambda *a, **k: order.append("limit") or False), \
         mock.patch.object(mod, "handle_pr", return_value={"action": "matched"}):
        mod.handle_event(payload)
    assert order == ["dup", "limit"], f"dup 检查应在文件数检查之前, 实际 {order}"


# ==================== 检测点32 空文件检查 ====================
def test_empty_file_detected():
    mod = _load_handler()
    files = [
        {"filename": f"{SKILL_DIR}/SKILL.md", "patch": "", "raw_url": ""},
        {"filename": f"{SKILL_DIR}/scripts/empty.py", "patch": "", "raw_url": ""},
        {"filename": f"{SKILL_DIR}/scripts/ok.py", "patch": "", "raw_url": ""},
    ]
    contents = {
        f"{SKILL_DIR}/SKILL.md": MD,
        f"{SKILL_DIR}/scripts/empty.py": "",          # 空文件
        f"{SKILL_DIR}/scripts/ok.py": "print(1)\n",   # 正常
    }
    _, results = mod._skill_review(SKILL_DIR, files, contents, "r/r", 100)
    _, _, sev, res, note = _item32(results)
    assert sev == "🔵", f"空文件检查应为提示级, 实际 {sev}"
    assert res == "⚠️", f"存在空文件应提示, 实际 {res}: {note}"
    assert "empty.py" in note, f"应列出空文件名, 实际 {note}"


def test_no_empty_file_pass():
    mod = _load_handler()
    files = [{"filename": f"{SKILL_DIR}/SKILL.md", "patch": "", "raw_url": ""}]
    contents = {f"{SKILL_DIR}/SKILL.md": MD}
    _, results = mod._skill_review(SKILL_DIR, files, contents, "r/r", 100)
    _, _, sev, res, note = _item32(results)
    assert sev == "🔵"
    assert res == "✅" and "无空文件" in note, f"实际 {res}: {note}"


def test_whitespace_only_is_empty():
    mod = _load_handler()
    files = [{"filename": f"{SKILL_DIR}/scripts/blank.txt",
              "patch": "", "raw_url": ""}]
    contents = {f"{SKILL_DIR}/scripts/blank.txt": "   \n\n  "}
    _, results = mod._skill_review(SKILL_DIR, files, contents, "r/r", 100)
    _, _, sev, res, note = _item32(results)
    assert sev == "🔵"
    assert res == "⚠️" and "blank.txt" in note, f"仅空白应算空文件, 实际 {res}: {note}"


def test_checklist_32_is_empty_file_check():
    mod = _load_handler()
    items = dict((i, t) for i, t, _ in mod.SKILL_CHECKLIST)
    assert 32 in items, "SKILL_CHECKLIST 应含检测点32"
    assert items[32] == "空文件检查", f"检测点32 应为空文件检查, 实际 {items[32]}"
