"""Issue #372 §12.8: PAT 绑定漏写 unified_user_mapping — 单元测试

测试覆盖：
- T13-01: PAT 绑定成功后写入 unified_user_mapping（gitcode_user = GitCode login）
- T13-02: PAT 绑定更新（已有记录）时 unified_user_mapping 也更新（UPDATE 覆盖）
- T13-03: GitHub PAT 绑定后写入 unified_user_mapping.github_login
- T13-04: PAT 绑定写入的 gitcode_user 是 gc_login（非 grape_accounts.username）
- T13-05: pipeline actor 查 _get_user_id_by_gitcode(gc_login) 能命中
- T13-06: 用户先绑 GitCode 再绑 GitHub → 两字段都保留（不互相覆盖）
- T13-07: 用户先绑 GitHub 再绑 GitCode → 两字段都保留（顺序无关）

测试方式：源码检查（inspect source），验证写入逻辑存在且用 INSERT OR IGNORE + UPDATE。
"""
import inspect
import os

import pytest


_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")


def _read_src(rel_path):
    with open(os.path.join(_SRC_DIR, rel_path), encoding="utf-8") as f:
        return f.read()


# ===========================================================================
# T13-01~02, T13-04: GitCode PAT 绑定写 unified_user_mapping
# ===========================================================================

class TestGitCodePatBindingUnifiedMapping:
    """测试 GitCode PAT 绑定后写入 unified_user_mapping。"""

    def test_t13_01_gitcode_pat_writes_unified_mapping(self):
        """T13-01: PAT 绑定成功后写入 unified_user_mapping（gitcode_user = gc_login）。"""
        src = _read_src("api/routes/settings.py")
        assert "unified_user_mapping" in src, \
            "settings.py should write unified_user_mapping after PAT binding"
        # 用 INSERT OR IGNORE + UPDATE（不用 INSERT OR REPLACE）
        assert "INSERT OR IGNORE INTO unified_user_mapping" in src, \
            "should use INSERT OR IGNORE (not INSERT OR REPLACE)"
        assert "UPDATE unified_user_mapping SET gitcode_user" in src, \
            "should UPDATE gitcode_user field"

    def test_t13_02_gitcode_pat_updates_existing(self):
        """T13-02: PAT 绑定更新（已有记录）时 unified_user_mapping 也更新。

        INSERT OR IGNORE 不删旧行，UPDATE 覆盖 gitcode_user 字段。
        """
        src = _read_src("api/routes/settings.py")
        # 不应用 INSERT OR REPLACE（会删整行重插）
        assert "INSERT OR REPLACE INTO unified_user_mapping" not in src, \
            "should NOT use INSERT OR REPLACE (would overwrite other platform fields)"
        # 应用 UPDATE 更新字段
        assert "UPDATE unified_user_mapping SET gitcode_user" in src, \
            "should use UPDATE to update gitcode_user (not replace entire row)"

    def test_t13_04_gitcode_uses_gc_login_not_username(self):
        """T13-04: PAT 绑定写入的 gitcode_user 是 gc_login（非 grape_accounts.username）。"""
        src = _read_src("api/routes/settings.py")
        gc_log_idx = src.find("GitCode token saved")
        assert gc_log_idx > 0
        after_gc = src[gc_log_idx:gc_log_idx + 1200]
        assert "gc_login" in after_gc, \
            "gitcode_user should be gc_login (from GitCode API), not user['username']"
        assert 'user["id"]' in after_gc, \
            "user_id should be user['id'] (grape user id)"


# ===========================================================================
# T13-03: GitHub PAT 绑定写 unified_user_mapping.github_login
# ===========================================================================

class TestGitHubPatBindingUnifiedMapping:
    """测试 GitHub PAT 绑定后写入 unified_user_mapping.github_login。"""

    def test_t13_03_github_pat_writes_unified_mapping(self):
        """T13-03: GitHub PAT 绑定后写入 unified_user_mapping.github_login。"""
        src = _read_src("api/routes/settings.py")
        gh_bind_idx = src.find("GitHub token saved")
        assert gh_bind_idx > 0, "GitHub token saved log not found"
        after_log = src[gh_bind_idx:gh_bind_idx + 800]
        assert "unified_user_mapping" in after_log, \
            "unified_user_mapping write should be after GitHub token saved"
        assert "github_login" in after_log, \
            "unified_user_mapping should write github_login"
        assert "gh_login" in after_log, \
            "unified_user_mapping should use gh_login as github_login"
        assert "INSERT OR IGNORE" in after_log, \
            "GitHub should use INSERT OR IGNORE (not INSERT OR REPLACE)"
        assert "UPDATE unified_user_mapping SET github_login" in after_log, \
            "GitHub should UPDATE github_login field"

    def test_t13_04_gitcode_uses_gc_login_not_username(self):
        """T13-04: PAT 绑定写入的 gitcode_user 是 gc_login（非 grape_accounts.username）。"""
        src = _read_src("api/routes/settings.py")
        gc_log_idx = src.find("GitCode token saved")
        assert gc_log_idx > 0
        after_gc = src[gc_log_idx:gc_log_idx + 1200]
        assert "gc_login" in after_gc, \
            "gitcode_user should be gc_login (from GitCode API), not user['username']"
        assert 'user["id"]' in after_gc, \
            "user_id should be user['id'] (grape user id)"


# ===========================================================================
# T13-05: pipeline actor 查询命中
# ===========================================================================

class TestPipelineActorLookup:
    """测试 pipeline actor 查 _get_user_id_by_gitcode 能命中。"""

    def test_t13_05_pipeline_actor_lookup_will_match(self):
        """T13-05: _get_user_id_by_gitcode 查 unified_user_mapping.gitcode_user / github_login。"""
        issues_src = _read_src("api/issues_center.py")
        assert "unified_user_mapping" in issues_src, \
            "_get_user_id_by_gitcode should query unified_user_mapping"
        assert "gitcode_user" in issues_src, \
            "_get_user_id_by_gitcode should query gitcode_user field"
        # Issue #372 §37: also query github_login
        assert "github_login" in issues_src, \
            "_get_user_id_by_gitcode should also query github_login field"


# ===========================================================================
# T13-06~07: 双平台绑定不互相覆盖
# ===========================================================================

class TestDualPlatformNoOverwrite:
    """测试用户同时绑定 GitCode + GitHub 时 unified_user_mapping 字段不互相覆盖。"""

    def test_t13_06_no_insert_or_replace(self):
        """T13-06: 不使用 INSERT OR REPLACE（会删整行重插，覆盖其他平台字段）。

        验证源码中 unified_user_mapping 相关代码全部用 INSERT OR IGNORE + UPDATE。
        """
        src = _read_src("api/routes/settings.py")
        # 不应有任何 INSERT OR REPLACE INTO unified_user_mapping
        assert "INSERT OR REPLACE INTO unified_user_mapping" not in src, \
            "must NOT use INSERT OR REPLACE for unified_user_mapping (would overwrite other platform fields)"

    def test_t13_07_gitcode_update_only_gitcode_fields(self):
        """T13-07: GitCode 绑定只 UPDATE gitcode_user/gitcode_nickname，不碰 github_login。"""
        src = _read_src("api/routes/settings.py")
        # GitCode UPDATE 语句只改 gitcode_user 和 gitcode_nickname
        gc_log_idx = src.find("GitCode token saved")
        after_gc = src[gc_log_idx:gc_log_idx + 1000]
        update_idx = after_gc.find("UPDATE unified_user_mapping SET gitcode_user")
        assert update_idx > 0
        update_block = after_gc[update_idx:update_idx + 200]
        # UPDATE 语句中不应包含 github_login
        assert "github_login" not in update_block, \
            "GitCode UPDATE should NOT touch github_login field"

    def test_t13_07b_github_update_only_github_fields(self):
        """T13-07b: GitHub 绑定只 UPDATE github_login，不碰 gitcode_user。"""
        src = _read_src("api/routes/settings.py")
        gh_log_idx = src.find("GitHub token saved")
        after_gh = src[gh_log_idx:gh_log_idx + 800]
        update_idx = after_gh.find("UPDATE unified_user_mapping SET github_login")
        assert update_idx > 0
        # UPDATE 语句行中不应包含 gitcode_user
        update_line = after_gh[update_idx:after_gh.find("\n", update_idx) + 1]
        assert "gitcode_user" not in update_line, \
            "GitHub UPDATE should NOT touch gitcode_user field"
