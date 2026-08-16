"""implementation / issue-advance 技能「前台长驻命令禁止」看护（coding-worktree-skill-collision.md §4.5.5/4.5.6）。

背景：#711 coding 前台执行 `python3 -m http.server 8080`（长驻不退出）→ opencode 等待命令退出
→ 120s 超时 SIGTERM（-15）。修复要求：
1. implementation SKILL.md 硬约束禁止前台长驻命令，引导一次性验证模式；
2. spec-generation.md 生成 Spec 时把前台命令改写为一次性模式。
"""
import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
IMPLEMENTATION = os.path.join(ROOT, "skills", "implementation", "SKILL.md")
SPEC_GEN = os.path.join(ROOT, "skills", "issue-advance", "references", "spec-generation.md")


def _read(p):
    return open(p, encoding="utf-8").read()


class TestImplementationSkillNoForegroundDaemon:
    def test_skill_forbids_foreground_long_running(self):
        s = _read(IMPLEMENTATION)
        assert "禁止前台运行长驻" in s or "阻塞不退出" in s, \
            "implementation SKILL.md 应含「禁止前台运行长驻/服务类命令」约束"

    def test_skill_teaches_one_shot_verify_pattern(self):
        s = _read(IMPLEMENTATION)
        # 一次性验证模式：后台启动 + curl 验证 + kill 收尾
        assert "http.server 8080" in s and "kill %1" in s, \
            "应教导后台 + 验证 + kill 的一次性模式"
        assert "&" in s, "应包含后台启动（&）"

    def test_no_bare_foreground_http_server(self):
        # 裸前台 `python3 -m http.server 8080`（无 & / 无 kill / 无后台）不应作为唯一示例
        s = _read(IMPLEMENTATION)
        assert ">/tmp/httpd.log 2>&1 &" in s, "http.server 示例应为后台重定向形式"


class TestSpecGenerationRewritesForegroundCmd:
    def test_spec_gen_mentions_one_shot_rewrite(self):
        s = _read(SPEC_GEN)
        assert "http.server" in s or "一次性验证" in s or "运行验证" in s, \
            "spec-generation.md 应含前台命令改写 / 运行验证指引"

    def test_spec_gen_forbids_bare_foreground(self):
        s = _read(SPEC_GEN)
        assert "严禁把裸前台命令写入 Spec" in s or "改写为一次性验证模式" in s, \
            "spec-generation.md 应明确禁止裸前台命令写入 Spec"
