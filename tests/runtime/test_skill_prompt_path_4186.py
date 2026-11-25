"""Issue #4186：prompt 注入技能路径与实际物化工作区目录同源化测试。

反馈 1304 形态：事件 prompt 提示的技能目录（issues/<n>/issue/.skills，按 agent
类型构造）不存在，任务工作区实际物化在 issues/<n>/sre/.skills（按路由命名的
cwd），Agent 按 prompt 提示 read SKILL.md → File not found，只能靠
DEVOPS_SKILLS_DIR 兜底。

修复（worker 端 _normalize_prompt_skill_path，物化后执行）：
1) prompt 技能路径从实际物化结果单一来源派生（同源化）；
2) DEVOPS_SKILLS_DIR 可读路径优先注入；
3) 无任何可读 SKILL.md → 显式 degraded 标注 + 回退路径；
4) 正常同源路径幂等零改动（不注入噪声）。

回归关联：#564（DEVOPS_SKILLS_DIR 目录全空未物化）、#3663（DEVOPS_SKILLS_DIR
指向未物化目录）——两者均落在本函数 degraded 分支之后仍保留既有
[Skill Materialization Notice] 语义，不破坏原降级链路。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.domains.runtime.engine.agent import (
    _normalize_prompt_skill_path,
    _resolve_prompt_skill_root,
    _prompt_referenced_skill_roots,
)


def _build_prompt(skill_dir: str, skill_name: str) -> str:
    """复刻 build_devops_agent_prompt 的技能段（{{SKILL_DIR}} 已被替换形态）。"""
    return (
        f"## Skill: {skill_name}\n"
        f"Skill file: {skill_dir}/{skill_name}/SKILL.md\n"
        f"Skill content location: DEVOPS_SKILLS_DIR environment variable points to the "
        f"same directory as {skill_dir}; layout: {skill_name}/SKILL.md (main) plus "
        f"{skill_name}/references/ files under the same directory.\n"
        f"**MANDATORY**: Read this SKILL.md with the read tool and strictly follow its "
        f"instructions (do not skip reading or guess from the name).\n"
    )


def _mk_skill(root: str, skill_name: str) -> str:
    d = os.path.join(root, skill_name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {skill_name}\n---\nbody\n")
    return d


class TestPromptSkillPathNormalization(unittest.TestCase):
    """_normalize_prompt_skill_path / _resolve_prompt_skill_root 行为。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sk4186-")
        self.base = self._tmp.name
        # 路由名（sre）≠ agent 类型（issue）双目录：反馈 1304 原始形态
        self.issue_ws = os.path.join(self.base, "issues", "263", "issue")
        self.sre_ws = os.path.join(self.base, "issues", "263", "sre")
        os.makedirs(self.issue_ws, exist_ok=True)
        os.makedirs(self.sre_ws, exist_ok=True)
        self.skill = "issue-cli"
        self.env = {}

    def tearDown(self):
        self._tmp.cleanup()

    def test_route_name_differs_from_agent_type(self):
        """核心场景（反馈 1304）：prompt 提示 issue/.skills，实际物化 sre/.skills。

        物化在 sre 工位成功 + DEVOPS_SKILLS_DIR 指向 sre → prompt 中不可读的
        issue 路径被重写为 sre 可读路径，并追加 aligned 说明。
        """
        _mk_skill(os.path.join(self.sre_ws, ".skills"), self.skill)
        self.env["DEVOPS_SKILLS_DIR"] = os.path.join(self.sre_ws, ".skills")
        stale_dir = os.path.join(self.issue_ws, ".skills")
        prompt = _build_prompt(stale_dir, self.skill)

        out = _normalize_prompt_skill_path(
            {"skill": self.skill}, self.env, prompt,
            os.path.join(self.sre_ws, ".skills"))

        auth = os.path.join(self.sre_ws, ".skills")
        self.assertIn(f"Skill file: {auth}/{self.skill}/SKILL.md", out)
        self.assertIn(f"same directory as {auth}", out)
        self.assertNotIn(stale_dir, out)
        self.assertIn("(aligned)", out)
        # 重写后的提示路径真实存在（Spec 验收 1：SKILL.md 可直接 read 成功）
        self.assertTrue(os.path.isfile(os.path.join(auth, self.skill, "SKILL.md")))

    def test_devops_skills_dir_takes_priority(self):
        """Spec 注入优先级：DEVOPS_SKILLS_DIR 可读路径优先于 cwd 物化目录。

        两处都有可读技能时（引用路径本身可读不重写），权威解析取 env 路径。
        """
        cwd_skills = _mk_skill(os.path.join(self.sre_ws, ".skills"), self.skill)
        env_skills_root = os.path.join(self.base, "envskills")
        _mk_skill(env_skills_root, self.skill)
        self.env["DEVOPS_SKILLS_DIR"] = env_skills_root

        auth = _resolve_prompt_skill_root(
            self.env, os.path.join(self.sre_ws, ".skills"), self.skill)
        self.assertEqual(auth, env_skills_root)
        # cwd 物化目录同样可读（引用路径可读 → 不被重写，幂等零噪声）
        out = _normalize_prompt_skill_path(
            {"skill": self.skill}, self.env,
            _build_prompt(os.path.dirname(cwd_skills), self.skill),
            os.path.join(self.sre_ws, ".skills"))
        self.assertNotIn("(aligned)", out)
        self.assertNotIn("(degraded)", out)

    def test_degraded_when_no_readable_skill(self):
        """Spec 验收 3：目录缺失/物化降级 → 显式 degraded + 回退路径。"""
        self.env["DEVOPS_SKILLS_DIR"] = os.path.join(self.sre_ws, ".skills")  # 空
        stale_dir = os.path.join(self.issue_ws, ".skills")
        prompt = _build_prompt(stale_dir, self.skill)

        out = _normalize_prompt_skill_path(
            {"skill": self.skill}, self.env, prompt,
            os.path.join(self.sre_ws, ".skills"))
        self.assertIn("(degraded)", out)
        self.assertIn("grape skills detail", out)
        self.assertIn(os.path.join(self.sre_ws, ".skills"), out)

    def test_degraded_without_devops_env(self):
        """#564/#3663 回归形态：DEVOPS_SKILLS_DIR 未注入（空目录/未物化）→ degraded。"""
        prompt = _build_prompt(os.path.join(self.issue_ws, ".skills"), self.skill)
        out = _normalize_prompt_skill_path(
            {"skill": self.skill}, {}, prompt,
            os.path.join(self.sre_ws, ".skills"))
        self.assertIn("(degraded)", out)
        self.assertIn("grape skills detail", out)

    def test_same_source_path_idempotent(self):
        """正常同源路径（{{SKILL_DIR}}=cwd/.skills 且可读）→ 幂等零改动。"""
        skills = os.path.join(self.sre_ws, ".skills")
        _mk_skill(skills, self.skill)
        self.env["DEVOPS_SKILLS_DIR"] = skills
        prompt = _build_prompt(skills, self.skill)
        out = _normalize_prompt_skill_path({"skill": self.skill}, self.env, prompt, skills)
        self.assertEqual(out, prompt)

    def test_fallback_checker_skill_name_used(self):
        """异名回退（#617 skill_fallback）：task.skill 未落盘但回退检查器可读。

        QA review 修复口径：按 task.skill_fallback.skill_name（实际物化名）探测，
        prompt 的 Skill file / same directory as 重写为可读检查器路径，
        不再误判 degraded、不再指向未物化的绑定名路径。
        """
        skills = os.path.join(self.sre_ws, ".skills")
        _mk_skill(skills, "issue-checker")  # 回退检查器物化名
        self.env["DEVOPS_SKILLS_DIR"] = skills
        prompt = _build_prompt(skills, self.skill)  # 提示绑定名路径
        out = _normalize_prompt_skill_path(
            {"skill": self.skill,
             "skill_fallback": {"skill_name": "issue-checker", "skill_id": 7}},
            self.env, prompt, skills)
        self.assertIn(f"Skill file: {skills}/issue-checker/SKILL.md", out)
        self.assertIn(f"same directory as {skills}", out)
        self.assertNotIn(f"{self.skill}/SKILL.md", out)
        self.assertNotIn("(degraded)", out)
        # 重写后的检查器路径真实可读（Spec 验收：SKILL.md 直接 read 成功）
        self.assertTrue(os.path.isfile(os.path.join(skills, "issue-checker", "SKILL.md")))

    def test_fallback_single_notice_outlet(self):
        """通知单一出口：_materialize_worker_skill 已追加 Notice（fallback）时，
        规整重写后不再追加第二段 aligned Notice。"""
        skills = os.path.join(self.sre_ws, ".skills")
        _mk_skill(skills, "issue-checker")
        self.env["DEVOPS_SKILLS_DIR"] = skills
        prompt = _build_prompt(skills, self.skill)
        prompt += (f"\n\n[Skill Materialization Notice] DEVOPS_SKILLS_DIR={skills} "
                   f"(fallback): skill 'issue-cli' not found; materialized official "
                   f"checker 'issue-checker'.")
        out = _normalize_prompt_skill_path(
            {"skill": self.skill,
             "skill_fallback": {"skill_name": "issue-checker", "skill_id": 7}},
            self.env, prompt, skills)
        self.assertEqual(out.count("[Skill Materialization Notice]"), 1)
        self.assertNotIn("(aligned)", out)

    def test_same_dir_replace_respects_boundary(self):
        """QA review：same directory as 裸 str.replace 会误伤前缀相近根目录
        （/a/.skills vs /a/.skills2）；带边界正则只替换完整路径。"""
        skills = os.path.join(self.sre_ws, ".skills")
        _mk_skill(skills, self.skill)
        self.env["DEVOPS_SKILLS_DIR"] = skills
        stale = os.path.join(self.issue_ws, ".skills")
        prefix_trap = stale + "2"  # /a/.skills2：stale 的前缀相近目录（本身可读）
        _mk_skill(prefix_trap, self.skill)
        prompt = (
            f"Skill file: {stale}/{self.skill}/SKILL.md\n"
            f"same directory as {stale}; layout: {self.skill}/SKILL.md\n"
            f"other ref: same directory as {prefix_trap} stays\n")
        out = _normalize_prompt_skill_path(
            {"skill": self.skill}, self.env, prompt, skills)
        self.assertIn(f"same directory as {skills};", out)
        # 前缀相近但本身可读的根目录不被裸前缀替换误伤
        self.assertIn(f"same directory as {prefix_trap} stays", out)

    def test_normalize_exception_appends_degraded(self):
        """QA review：异常分支不静默——补显式 degraded 标注 + 回退提示。"""

        class _Boom:
            def get(self, *_a, **_k):
                raise RuntimeError("boom")

        prompt = _build_prompt(os.path.join(self.issue_ws, ".skills"), self.skill)
        out = _normalize_prompt_skill_path(
            _Boom(), self.env, prompt, os.path.join(self.sre_ws, ".skills"))
        self.assertIn("(degraded)", out)
        self.assertIn("grape skills detail", out)

    def test_no_skill_or_empty_prompt_noop(self):
        """无绑定技能 / 空 prompt → 原样返回，不注入噪声。"""
        self.assertEqual(_normalize_prompt_skill_path({}, self.env, "", "/x"), "")
        self.assertEqual(
            _normalize_prompt_skill_path({"skill": ""}, self.env, "p", "/x"), "p")

    def test_referenced_roots_extraction(self):
        """Skill file: / same directory as 两处引用目录均被提取。"""
        prompt = (
            "Skill file: /a/.skills/issue-cli/SKILL.md\n"
            "same directory as /a/.skills; layout: issue-cli/SKILL.md\n"
            "Skill file: /b/.skills/other/SKILL.md\n")
        roots = _prompt_referenced_skill_roots(prompt)
        self.assertIn("/a/.skills", roots)
        self.assertIn("/b/.skills", roots)


if __name__ == "__main__":
    unittest.main()
