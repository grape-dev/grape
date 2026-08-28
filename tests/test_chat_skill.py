"""chat skill / task-handling 技能「仅澄清+回帖，不执行任务」看护。

背景：#711 前，chat agent 在 task 场景（hello-html1 Issue #3）收到用户选"方式2"后
**自己 clone + 起 http.server 部署**，违反 task-handling（应只澄清+回帖，交还 pipeline）。
"""
import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
CHAT_SKILL = os.path.join(ROOT, "skills", "chat", "chat", "SKILL.md")
TASK_HANDLING = os.path.join(ROOT, "skills", "chat", "chat", "references", "task-handling.md")


def _read(p):
    return open(p, encoding="utf-8").read()


class TestChatSkillNoSelfExecute:
    def test_chat_forbids_write_code_git(self):
        s = _read(CHAT_SKILL)
        assert "不要写代码" in s and "执行 git 命令" in s, "chat/SKILL.md 应禁止写代码/执行 git"

    def test_task_scene_marks_clarify_only(self):
        s = _read(CHAT_SKILL)
        scene_line = [ln for ln in s.splitlines() if "task 任务处理" in ln][0]
        assert "仅澄清 + 回帖" in scene_line or "不执行任务" in scene_line, \
            "task 场景行应标注「仅澄清+回帖，不执行任务」"

    def test_ability_boundary_clarify_only(self):
        s = _read(CHAT_SKILL)
        assert "仅澄清" in s and "绝不亲自执行" in s, "能力边界应强调仅澄清不亲自执行"

    def test_operation_boundary_forbids_inline_deploy(self):
        s = _read(CHAT_SKILL)
        assert "clone/部署/起服务" in s and "❌ **禁止**" in s, \
            "操作边界应禁止在 chat 内 clone/部署/起服务（即便用户选了部署方式）"


class TestTaskHandlingNoSelfExecute:
    def test_boundary_section_present(self):
        s = _read(TASK_HANDLING)
        assert "任务边界" in s or "绝不执行任务本身" in s, \
            "task-handling.md 应有「任务边界」强约束"

    def test_forbids_clone_deploy_http_server(self):
        s = _read(TASK_HANDLING)
        assert "不要" in s and ("clone" in s) and ("http.server" in s or "起服务" in s), \
            "应明确禁止 clone/部署/起 http.server"

    def test_work_done_by_pipeline(self):
        s = _read(TASK_HANDLING)
        assert "pipeline" in s and "coding" in s and "sre" in s, \
            "应说明执行由 pipeline（coding/sre）完成"

    def test_reply_means_done(self):
        s = _read(TASK_HANDLING)
        assert "回帖即完成" in s, "§5 应强调「回帖即完成」，不再尝试部署/实现"

    def test_user_choice_is_clarification_not_deploy(self):
        s = _read(TASK_HANDLING)
        assert "需求澄清的一部分" in s or "不是让你立即去部署" in s, \
            "应说明用户选部署方式是需求澄清，不是授权立即部署"
