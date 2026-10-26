"""M3 git 基建测试（见 multi-issue-isolation-design.md §7.4）：bare mirror 基线 + per-Issue worktree"""
import os, subprocess
import pytest

from src.runtime import repogit

# 原始 clone_url_for（autouse patch 会替换 repogit.clone_url_for，URL 测试用原函数）
_ORIG_CLONE_URL = repogit.clone_url_for


def _git(args, cwd):
    return subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True)


@pytest.fixture
def git_src(tmp_path):
    """本地源仓库（含一个 commit），并让 repogit 的 clone_url 指向它。"""
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-b", "main"], str(src))
    _git(["config", "user.email", "test@example.com"], str(src))
    _git(["config", "user.name", "Test User"], str(src))
    (src / "a.txt").write_text("v1", encoding="utf-8")
    _git(["add", "-A"], str(src))
    _git(["commit", "-m", "init"], str(src))
    return src


@pytest.fixture(autouse=True)
def _patch(git_src, tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(repogit, "clone_url_for", lambda repo_key: str(git_src))
    yield


class TestCloneUrl:
    def test_gitcode_url(self):
        assert _ORIG_CLONE_URL("gitcode:org/test") == "https://gitcode.com/org/test.git"

    def test_github_url(self):
        assert _ORIG_CLONE_URL("github:org/test") == "https://github.com/org/test.git"


class TestRepoBase:
    def test_ensure_base_creates_bare_mirror(self):
        base = repogit.ensure_repo_base("gitcode:owner/repo")
        assert base and os.path.isdir(base)
        r = _git(["rev-parse", "--is-bare-repository"], base)
        assert r.stdout.strip() == "true"

    def test_fetch_refreshes(self, git_src, tmp_path):
        base = repogit.ensure_repo_base("gitcode:owner/repo")
        # 源仓库新增 commit
        (git_src / "b.txt").write_text("v2", encoding="utf-8")
        _git(["add", "-A"], str(git_src))
        _git(["commit", "-m", "second"], str(git_src))
        repogit.ensure_repo_base("gitcode:owner/repo")
        r = _git(["log", "--oneline", "-1", "origin/main"], base)
        assert "second" in r.stdout


class TestIssueWorktree:
    def test_worktree_branch_and_isolation(self):
        wt5 = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5")
        wt6 = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "6")
        assert wt5 and wt6 and wt5 != wt6
        br5 = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt5)
        br6 = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt6)
        assert br5.stdout.strip() == "feat/issue-5"
        assert br6.stdout.strip() == "feat/issue-6"
        # 各自 commit 互不干扰
        open(os.path.join(wt5, "a.txt"), "w", encoding="utf-8").write("changed-5")
        _git(["add", "-A"], wt5)
        _git(["commit", "-m", "issue5 change"], wt5)
        r6 = _git(["show", "HEAD:a.txt"], wt6)
        assert r6.stdout.strip() == "v1", "issue6 不应看到 issue5 的改动"

    def test_worktree_reuse_same_issue(self):
        a = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5")
        b = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5")
        assert a == b

    def test_non_git_agent_skipped(self):
        assert repogit.ensure_issue_worktree("gitcode:owner/repo", "issue", "5") is None

    def test_git_agent_guard_only_coding_qa_sre(self):
        """触发范围守卫：ensure_issue_worktree 仅对 git agent 建 worktree，vod/issue 返回 None。"""
        for agent in ("coding", "qa", "sre"):
            assert repogit.is_git_agent(agent), f"{agent} 应视为 git agent"
            wt = repogit.ensure_issue_worktree("gitcode:owner/repo", agent, "5")
            assert wt and os.path.isdir(wt), f"{agent} 应成功创建 worktree"
        for agent in ("vod", "issue"):
            assert not repogit.is_git_agent(agent), f"{agent} 不应视为 git agent"
            assert repogit.ensure_issue_worktree("gitcode:owner/repo", agent, "5") is None, \
                f"{agent} 不应创建 worktree"

    def test_worktree_created_before_skill_occupies_cwd(self):
        """方案 A 顺序看护：先建 worktree（cwd 空），再物化技能到 cwd/.skills——不复现 #711 的
        "already exists"。若时序回归为"先技能后 worktree"，此处 worktree add 会因目录非空失败。"""
        import shutil
        wt = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "9")
        assert wt and os.path.exists(os.path.join(wt, ".git")), "worktree 应含 .git（目录或 gitdir 文件）"
        # worktree 建好后落地技能（模拟 _execute_task 物化 .skills）
        skill_dir = os.path.join(wt, ".skills")
        os.makedirs(os.path.join(skill_dir, "implementation"), exist_ok=True)
        with open(os.path.join(skill_dir, "implementation", "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("# skill\n")
        # 二次调用复用（.skills 已存在但 .git 也在）→ 直接复用不重建
        wt2 = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "9")
        assert wt2 == wt
        assert os.path.isfile(os.path.join(wt, ".skills", "implementation", "SKILL.md")), "技能不应被覆盖"
        shutil.rmtree(wt, ignore_errors=True)

    def test_cleanup_removes_worktree(self):
        wt = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5")
        assert os.path.isdir(wt)
        repogit.cleanup_issue_worktree("gitcode:owner/repo", "coding", "5")
        assert not os.path.isdir(wt)
        base = repogit.repo_base_dir("gitcode:owner/repo")
        r = _git(["worktree", "list"], base)
        assert wt not in r.stdout


class TestTokenNoPersist:
    def test_token_not_on_disk(self):
        repogit.ensure_repo_base("gitcode:owner/repo", token="sk_secret_123")
        repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5", token="sk_secret_123")
        base = repogit.repo_base_dir("gitcode:owner/repo")
        wt = repogit.issue_dir_for("gitcode:owner/repo", "coding", "5")
        # 遍历 git 相关配置/文件，确认无 token
        for p in (os.path.join(base, "config"), os.path.join(base, "config.worktree")):
            if os.path.isfile(p):
                assert "sk_secret_123" not in open(p, encoding="utf-8", errors="ignore").read()
        for root, _d, files in os.walk(wt):
            for f in files:
                if f == ".git-askpass.sh":
                    # 辅助脚本只从环境变量读 token，不落盘
                    content = open(os.path.join(root, f), encoding="utf-8").read()
                    assert "sk_secret_123" not in content
                    assert "BOT_GITCODE_TOKEN" in content
        # 无 credentials 文件
        assert not os.path.exists(os.path.join(wt, ".git-credentials"))




class TestEnsureFeatureBranch:
    """分支护栏 helper（Issue #15）：先 checkout 已存在分支，失败再 -b 新建。"""

    def test_already_on_target_branch(self, git_src):
        """已在目标分支 → 直接返回 True，不执行 checkout。"""
        ok = repogit.ensure_feature_branch(str(git_src), "main", "main")
        assert ok is True
        br = _git(["rev-parse", "--abbrev-ref", "HEAD"], str(git_src))
        assert br.stdout.strip() == "main"

    def test_switch_to_existing_branch(self, git_src):
        """目标分支已存在但当前不在其上 → checkout 切换成功（分支已存在场景）。"""
        _git(["checkout", "-b", "feat/issue-99"], str(git_src))
        _git(["checkout", "main"], str(git_src))
        ok = repogit.ensure_feature_branch(str(git_src), "feat/issue-99", "main")
        assert ok is True
        br = _git(["rev-parse", "--abbrev-ref", "HEAD"], str(git_src))
        assert br.stdout.strip() == "feat/issue-99"

    def test_create_new_branch_when_not_exists(self, git_src):
        """目标分支不存在 → 先 checkout 失败，再 -b 新建（本地分支追踪 HEAD）。"""
        ok = repogit.ensure_feature_branch(str(git_src), "feat/issue-77", "main")
        assert ok is True
        br = _git(["rev-parse", "--abbrev-ref", "HEAD"], str(git_src))
        assert br.stdout.strip() == "feat/issue-77"

    def test_invalid_dir_returns_false(self):
        """无效目录 → 不崩溃，返回 False。"""
        ok = repogit.ensure_feature_branch("/nonexistent/path", "feat/issue-1", "main")
        assert ok is False


class TestLinkedRepoTokenFromEnv:
    """Issue #466 断点二：关联仓 token 解析——服务端 env 分发（GRAPE_LINKED_REPO_TOKENS）
    优先，本地 Repo.bot_token 回退，两者皆无维持 skip。

    场景：GitHub 主仓 + GitCode 关联仓。worker 本地 sqlite 无 managed_repos 数据，
    Repo(rk).bot_token 必为空 → worktree 全部 skip。服务端派发时把各关联仓 token
    随 env 下发后，worker 应优先用 env token 完成初始化。
    """

    LINKED = [{"repo_key": "gitcode:OPCTeam/HDBlogService", "branch": "main"}]

    def test_env_token_preferred_over_local(self, monkeypatch):
        """env 有该仓 token → 优先使用，不再走本地 Repo.bot_token。"""
        monkeypatch.setattr(repogit, "ensure_issue_worktree",
                            lambda rk, ag, num, token="", work_branch="": (rk, token))
        local_called = {"n": 0}

        class _FakeRepo:
            def __init__(self, rk):
                pass

            @property
            def bot_token(self):
                local_called["n"] += 1
                return "tok_local"

        monkeypatch.setattr("src.repo.Repo", _FakeRepo)
        env = {"GRAPE_LINKED_REPO_TOKENS": '{"gitcode:OPCTeam/HDBlogService": "tok_env"}'}
        wts = repogit.ensure_linked_repo_worktrees(self.LINKED, "coding", "466", env=env)
        assert wts and wts[0]["worktree_dir"] == ("gitcode:OPCTeam/HDBlogService", "tok_env")
        assert local_called["n"] == 0, "env 有 token 时不应回退本地解析"

    def test_env_missing_key_falls_back_to_local(self, monkeypatch):
        """env 无该仓键 → 回退本地 Repo.bot_token（本地模式可用性保持）。"""
        monkeypatch.setattr(repogit, "ensure_issue_worktree",
                            lambda rk, ag, num, token="", work_branch="": (rk, token))

        class _FakeRepo:
            def __init__(self, rk):
                pass

            @property
            def bot_token(self):
                return "tok_local"

        monkeypatch.setattr("src.repo.Repo", _FakeRepo)
        env = {"GRAPE_LINKED_REPO_TOKENS": '{"gitcode:other/repo": "tok_env"}'}
        wts = repogit.ensure_linked_repo_worktrees(self.LINKED, "coding", "466", env=env)
        assert wts and wts[0]["worktree_dir"] == ("gitcode:OPCTeam/HDBlogService", "tok_local")

    def test_both_missing_skips(self, monkeypatch):
        """env 与本地均无 token → 该仓 skip（维持降级语义，不抛异常）。"""
        monkeypatch.setattr(repogit, "ensure_issue_worktree",
                            lambda rk, ag, num, token="", work_branch="": (rk, token))

        class _FakeRepo:
            def __init__(self, rk):
                pass

            @property
            def bot_token(self):
                return ""

        monkeypatch.setattr("src.repo.Repo", _FakeRepo)
        wts = repogit.ensure_linked_repo_worktrees(self.LINKED, "coding", "466", env={})
        assert wts == [], "双无 token 应 skip 该关联仓"

    def test_env_json_invalid_falls_back(self, monkeypatch):
        """env JSON 损坏 → 视为无 env token，回退本地解析。"""
        monkeypatch.setattr(repogit, "ensure_issue_worktree",
                            lambda rk, ag, num, token="", work_branch="": (rk, token))

        class _FakeRepo:
            def __init__(self, rk):
                pass

            @property
            def bot_token(self):
                return "tok_local"

        monkeypatch.setattr("src.repo.Repo", _FakeRepo)
        wts = repogit.ensure_linked_repo_worktrees(self.LINKED, "coding", "466",
                                                   env={"GRAPE_LINKED_REPO_TOKENS": "{broken"})
        assert wts and wts[0]["worktree_dir"] == ("gitcode:OPCTeam/HDBlogService", "tok_local")

    def test_ensure_repo_base_multi_env_preferred(self, monkeypatch):
        """ensure_repo_base_multi 同口径：env token 优先传给 ensure_repo_base。"""
        monkeypatch.setattr(repogit, "ensure_repo_base",
                            lambda rk, token="": {"rk": rk, "token": token})

        class _FakeRepo:
            def __init__(self, rk):
                pass

            @property
            def bot_token(self):
                return "tok_local"

        monkeypatch.setattr("src.repo.Repo", _FakeRepo)
        env = {"GRAPE_LINKED_REPO_TOKENS": '{"gitcode:OPCTeam/HDBlogService": "tok_env"}'}
        result = repogit.ensure_repo_base_multi(self.LINKED, env=env)
        assert result["gitcode:OPCTeam/HDBlogService"] == {
            "rk": "gitcode:OPCTeam/HDBlogService", "token": "tok_env"}

    def test_default_env_reads_os_environ(self, monkeypatch):
        """env 参数缺省（None）→ 读 os.environ（本地直调/进程内兼容）。"""
        monkeypatch.setattr(repogit, "ensure_issue_worktree",
                            lambda rk, ag, num, token="", work_branch="": (rk, token))
        monkeypatch.setenv("GRAPE_LINKED_REPO_TOKENS",
                           '{"gitcode:OPCTeam/HDBlogService": "tok_osenv"}')

        class _FakeRepo:
            def __init__(self, rk):
                pass

            @property
            def bot_token(self):
                return "tok_local"

        monkeypatch.setattr("src.repo.Repo", _FakeRepo)
        wts = repogit.ensure_linked_repo_worktrees(self.LINKED, "coding", "466")
        assert wts and wts[0]["worktree_dir"] == ("gitcode:OPCTeam/HDBlogService", "tok_osenv")
