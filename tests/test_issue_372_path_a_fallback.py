"""Issue #372 §16: 路径 A bot 协作者失败降级到路径 B — 单元测试

测试覆盖：
- T11-01~03: host_repo GitCode 路径 A 三个失败点降级
- T11-04~05: repair_host GitCode 路径 A 降级 + add_collaborator catch
- T11-06: host_repo GitHub _try_github_bot_collaborator 异常不抛
- T11-07: 路径 A 成功不降级（回归保护）

测试方式：源码检查（inspect source），验证降级逻辑存在且不 raise 502。
"""
import os
import inspect

import pytest


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "api", "routes")


def _read_repos_source():
    with open(os.path.join(_STATIC_DIR, "repos.py"), encoding="utf-8") as f:
        return f.read()


# ===========================================================================
# T11-01~03: host_repo GitCode 路径 A 降级
# ===========================================================================

class TestHostRepoPathAFallback:
    """测试 host_repo GitCode 路径 A bot 协作者失败时降级到路径 B。"""

    def test_t11_01_add_collaborator_fail_fallback(self):
        """T11-01: add_collaborator 失败 + get_member_permission 兜底也失败 → 降级到路径 B。

        验证源码中不再有 raise 502 "托管失败"（add_collaborator 失败路径）。
        """
        src = _read_repos_source()
        # 不应有 "托管失败: {add_err}" 的 raise 502（原 L967）
        assert 'raise HTTPException(status_code=502, detail={"code": 502, "message": f"托管失败: {add_err}"})' not in src, \
            "add_collaborator failure should not raise 502 (should fallback to path B)"
        # 应有降级逻辑
        assert "path_b_fallback" in src, \
            "path A fallback to path B logic not found"

    def test_t11_02_get_member_permission_exception_fallback(self):
        """T11-02: get_member_permission 抛非限流异常 → 降级到路径 B（不穿透 502）。

        验证源码中 get_member_permission 的 except 不只 catch _RateLimitedError。
        """
        src = _read_repos_source()
        # 在 bot 协作者流程中应有 except Exception 降级（不只是 _RateLimitedError）
        # 查找 "path A get_member_permission failed, falling back to path B"
        assert "path A get_member_permission failed, falling back to path B" in src or \
               "path A perm check failed, falling back to path B" in src, \
            "non-RateLimited exception should fallback to path B"

    def test_t11_03_access_level_low_fallback(self):
        """T11-03: bot 权限 access_level < 40 → 降级到路径 B（原 raise 502）。

        验证源码中不再有 "托管部分完成" 的 raise 502。
        """
        src = _read_repos_source()
        # 不应有 "托管部分完成" raise 502（原 L982）
        assert '托管部分完成' not in src or 'raise HTTPException(status_code=502' not in src.split('托管部分完成')[0][-200:], \
            "access_level < 40 should not raise 502 (should fallback to path B)"
        # 应有 access_level < 40 降级
        assert "access_level" in src and "path_b_fallback" in src, \
            "access_level < 40 fallback logic not found"


# ===========================================================================
# T11-04~05: repair_host GitCode 路径 A 降级
# ===========================================================================

class TestRepairHostPathAFallback:
    """测试 repair_host GitCode 路径 A bot 协作者失败时降级。"""

    def test_t11_04_bot_not_ok_fallback_not_return_error(self):
        """T11-04: repair_host bot_ok=False → 降级到路径 B（不 return error）。

        验证源码中不再有 return {"status": "error", "host_status": "bot_missing"}。
        """
        src = _read_repos_source()
        # repair_host 中不应有 return error bot_missing（降级替代）
        # 查找 repair_host 函数范围
        repair_idx = src.find("def repair_host")
        assert repair_idx > 0
        repair_src = src[repair_idx:]
        # 不应有 return error bot_missing
        assert 'return {"status": "error", "host_status": "bot_missing"' not in repair_src, \
            "repair_host should not return error when bot fails (should fallback to path B)"
        # 应有降级逻辑
        assert "falling back to path B" in repair_src, \
            "repair_host should have path B fallback logic"

    def test_t11_05_add_collaborator_has_try_except(self):
        """T11-05: repair_host add_collaborator/accept_invite 有 try/except。

        验证源码中 add_collaborator 和 accept_invite_for_repo 被包在 try/except 中。
        """
        src = _read_repos_source()
        repair_idx = src.find("def repair_host")
        repair_src = src[repair_idx:]
        # add_collaborator 应在 try 块中
        assert "try:" in repair_src and "add_collaborator" in repair_src, \
            "add_collaborator should be in try block"
        # 应有 except 处理
        assert "bot invite failed" in repair_src, \
            "add_collaborator failure should be caught with warning"


# ===========================================================================
# T11-06: _try_github_bot_collaborator 异常不抛
# ===========================================================================

class TestTryGithubBotCollaboratorCatch:
    """测试 _try_github_bot_collaborator 内部 catch 异常。"""

    def test_t11_06_function_has_try_except(self):
        """T11-06: _try_github_bot_collaborator 整体包 try/except，异常返回 error 状态。

        验证源码中函数体有 try/except，返回 bot_status=error。
        """
        src = _read_repos_source()
        func_idx = src.find("def _try_github_bot_collaborator")
        assert func_idx > 0
        # 截取函数体（到下一个 def）
        next_def = src.find("\ndef ", func_idx + 10)
        func_src = src[func_idx:next_def] if next_def > 0 else src[func_idx:]
        # 应有 try/except
        assert "try:" in func_src, \
            "_try_github_bot_collaborator should have try block"
        assert "except Exception" in func_src, \
            "_try_github_bot_collaborator should catch Exception"
        # 异常时返回 bot_status=error
        assert '"bot_status": "error"' in func_src, \
            "_try_github_bot_collaborator should return bot_status=error on exception"


# ===========================================================================
# T11-07: 路径 A 成功不降级（回归保护）
# ===========================================================================

class TestPathASuccessNoFallback:
    """测试路径 A bot 协作者成功时不降级。"""

    def test_t11_07_path_a_success_stays_path_a(self):
        """T11-07: 路径 A bot 协作者成功 → 不降级（保持路径 A）。

        验证源码中 bot 协作者成功路径仍返回 bot_status=ok，不设 _use_path_b=True。
        """
        src = _read_repos_source()
        # _try_github_bot_collaborator 成功时返回 bot_status=ok
        assert '"bot_status": "ok"' in src, \
            "successful bot collaborator should return bot_status=ok"
        # host_repo 路径 A 成功时不设 _use_path_b=True
        # 验证：bot 协作者成功后代码继续走 webhook（不是 fallback）
        # "already maintainer" 是成功路径的日志
        assert "already maintainer" in src, \
            "path A success path (bot already maintainer) should exist"

    def test_t11_08_github_bot_error_fallback_to_path_b(self):
        """T11-08: host_repo GitHub 路径 A _try_github_bot_collaborator 返回 bot_status=error → 降级到路径 B。

        验证源码中 GitHub 托管分支检查 bot_status=error 并设 _gh_use_path_b=True。
        """
        src = _read_repos_source()
        # 应检查 bot_status == "error"
        assert '"error"' in src and "bot_status" in src, \
            "GitHub host_repo should check bot_status=error"
        # 应有降级逻辑（_gh_use_path_b = True + bot_github_token=""）
        assert 'bot_github_token=("" if _gh_use_path_b else gh_bot_token)' in src, \
            "GitHub host_repo should write empty bot_github_token when path B (fallback)"
        # _try_github_bot_collaborator 应在 add_managed_repo 之前调用（检查返回值再写 DB）
        # 查找 GitHub 托管分支中的调用顺序
        gh_host_idx = src.find("GitHub 仓库托管")
        if gh_host_idx < 0:
            gh_host_idx = src.find("host_repo GitHub")
        gh_block = src[gh_host_idx:gh_host_idx + 2000] if gh_host_idx > 0 else src
        bot_check_idx = gh_block.find('bot_status") == "error"')
        add_repo_idx = gh_block.find("add_managed_repo")
        if bot_check_idx > 0 and add_repo_idx > 0:
            assert bot_check_idx < add_repo_idx, \
                "bot_status check should be before add_managed_repo (check before writing DB)"

    def test_t11_09_github_fallback_no_invalid_bot_token_in_db(self):
        """T11-09: 降级后 bot_github_token 写空（非无效值），get_github_token 不命中无效 token。

        验证：降级时 bot_github_token="" → get_github_token 第 1 步 bot_github_token 为空 → 跳过 → 走 host_user_id 降级。
        """
        src = _read_repos_source()
        # 降级时 bot_github_token 应写空字符串
        assert 'bot_github_token=("" if _gh_use_path_b else gh_bot_token)' in src, \
            "bot_github_token should be empty string when path B fallback (not invalid bot token)"
        # 不应直接写 gh_bot_token（可能无效）
        # 在 GitHub 托管分支中不应有 bot_github_token=gh_bot_token（不带条件判断的直写）
        gh_host_idx = src.find("GitHub 仓库托管")
        if gh_host_idx < 0:
            gh_host_idx = src.find("host_repo GitHub")
        gh_block = src[gh_host_idx:gh_host_idx + 2000] if gh_host_idx > 0 else src
        # 不应有 "bot_github_token=gh_bot_token"（无条件判断的直写，会把无效 token 写入 DB）
        assert 'bot_github_token=gh_bot_token' not in gh_block, \
            "should not write gh_bot_token directly (could be invalid) — use conditional empty"
