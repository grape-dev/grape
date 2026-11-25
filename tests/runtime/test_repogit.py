"""M3 git 基建测试（见 multi-issue-isolation-design.md §7.4）：bare mirror 基线 + per-Issue worktree"""
import os, subprocess
import pytest

from src.domains.runtime.engine import repogit

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
    monkeypatch.setattr("src.kernel.config.WORKSPACE_DIR", str(tmp_path / "ws"))
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


class TestWorktreePreflightDisposal:
    """Issue #3482（B）：`git worktree add` **之前**先 stat 目标目录并按内容分流处置。

    「目标目录已存在且无 `.git`」有两个来源：direct 端旧顺序（物化先于建树）与上次运行
    残留。此前只有失败后的兜底（症状侧、且靠 stderr 文案）；现在改为建树前处置：
    仅平台产物 → 清空重建；含未知内容 → 隔离为 `<dir>.stale-<ts>` 留证后重建。
    """

    def _preset(self, issue_num, entries):
        wt = repogit.issue_dir_for("gitcode:owner/repo", "coding", issue_num)
        os.makedirs(wt, exist_ok=True)
        for rel, content in entries.items():
            p = os.path.join(wt, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        return wt

    @staticmethod
    def _has_git(wt):
        return (os.path.isdir(os.path.join(wt, ".git"))
                or os.path.isfile(os.path.join(wt, ".git")))

    @staticmethod
    def _stale_dirs(wt):
        parent = os.path.dirname(wt)
        return [p for p in os.listdir(parent) if p.startswith("coding.stale-")]

    def test_platform_assets_only_cleared_and_rebuilt(self):
        """预置 .skills/.project-info/.knowledge（无 .git）→ 清空重建：可 commit、
        inject_git_write_context 不误置 GRAPE_GIT_READONLY、不产生 .stale 隔离。"""
        wt = self._preset("16", {".skills/coding-cli-1/SKILL.md": "# skill\n",
                                 ".project-info/graph.md": "# pi\n",
                                 ".knowledge/k.md": "# k\n"})
        got = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "16")
        assert got == wt, "清空重建后应返回目标目录"
        assert self._has_git(wt), "重建后 .git 应落位"
        assert self._stale_dirs(wt) == [], "平台产物应清空重建，不做隔离"
        assert not os.path.exists(os.path.join(wt, ".skills")), "平台产物已清空（由本轮重新物化）"
        with open(os.path.join(wt, "fix-3482.txt"), "w", encoding="utf-8") as f:
            f.write("ok\n")
        _git(["config", "user.email", "agent@example.com"], wt)
        _git(["config", "user.name", "Agent"], wt)
        assert _git(["add", "fix-3482.txt"], wt).returncode == 0
        assert _git(["commit", "-m", "verify writable after preflight"], wt).returncode == 0
        env = {}
        _prompt, status = repogit.inject_git_write_context(
            {"repo_key": "gitcode:owner/repo", "agent_type": "coding",
             "issue_id": "#16"}, env, "p")
        assert status.get("writable") is True
        assert env.get("GRAPE_GIT_WRITABLE") == "true"
        assert "GRAPE_GIT_READONLY" not in env, "建树成功不得误置 GRAPE_GIT_READONLY"

    def test_unknown_content_quarantined_as_stale(self):
        """含未知文件 → 整体隔离为 <dir>.stale-<ts>（留证不删），随后建树成功。"""
        wt = self._preset("17", {".skills/coding-cli-1/SKILL.md": "# skill\n",
                                 "notes.txt": "user work\n"})
        got = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "17")
        assert got == wt
        stales = self._stale_dirs(wt)
        assert len(stales) == 1, f"未知内容应隔离为单个 .stale 目录: {stales}"
        stale = os.path.join(os.path.dirname(wt), stales[0])
        assert os.path.isfile(os.path.join(stale, "notes.txt")), "未知内容必须留证不删除"
        assert os.path.isfile(os.path.join(stale, ".skills", "coding-cli-1", "SKILL.md"))
        assert self._has_git(wt), "隔离后应重建出带 .git 的 worktree"
        assert not os.path.exists(os.path.join(wt, "notes.txt")), "新 worktree 不应带未知内容"

    def test_quarantine_failure_returns_none(self, monkeypatch):
        """隔离失败（跨设备/权限）→ 返回 None（workspace 不可用，上游置 failed），
        不得 fail-open 继续跑成「只读 completed」（验收 3）。"""
        wt = self._preset("18", {"notes.txt": "x\n"})
        real_rename = os.rename

        def _fail_quarantine(src, dst):
            if src == wt and ".stale-" in dst:
                raise OSError("cross-device link simulated")
            return real_rename(src, dst)

        monkeypatch.setattr(os, "rename", _fail_quarantine)
        assert repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "18") is None

    def test_dispose_classification(self):
        """分流判据：空目录/不存在 → none；仅平台产物 → cleared；含未知 → quarantined。"""
        assert repogit.dispose_occupied_workdir_target(
            "gitcode:owner/repo", "/nonexistent-3482-dir")["action"] == "none"
        wt = self._preset("19", {".skills/a/SKILL.md": "# s\n"})
        assert repogit.dispose_occupied_workdir_target(
            "gitcode:owner/repo", wt)["action"] == "cleared"
        wt2 = self._preset("20", {"mine.py": "print(1)\n"})
        d = repogit.dispose_occupied_workdir_target("gitcode:owner/repo", wt2)
        assert d["action"] == "quarantined" and d["unknown"] == ["mine.py"]
        assert os.path.isdir(d["stale_path"])


class TestAlreadyExistsLastDefense:
    """Issue #3482：前置处置之后的最后一道防线（竞态 / 预置目录仍被占）。"""

    def test_last_defense_rebuilds_and_merges_assets(self):
        wt = repogit.issue_dir_for("gitcode:owner/repo", "coding", "21")
        skill_md = os.path.join(wt, ".skills", "coding-cli-1", "SKILL.md")
        os.makedirs(os.path.dirname(skill_md), exist_ok=True)
        with open(skill_md, "w", encoding="utf-8") as f:
            f.write("# skill\n")
        base = repogit.ensure_repo_base("gitcode:owner/repo")
        assert base
        ok = repogit._recover_worktree_already_exists(
            "gitcode:owner/repo", wt, base, "feat/issue-21", "main", False)
        assert ok, "最后防线应完成迁移重建"
        assert (os.path.isdir(os.path.join(wt, ".git"))
                or os.path.isfile(os.path.join(wt, ".git")))
        assert os.path.isfile(skill_md), "最后防线保留占用产物"
        assert not os.path.exists(wt + ".grape-collide-backup"), "备份目录应清理"

    def test_merge_keeps_backup_when_asset_conflicts(self):
        """QA 复审：同名文件未合并时不得删备份（「迁移不删除」红线）。"""
        wt = repogit.issue_dir_for("gitcode:owner/repo", "coding", "23")
        backup = wt + ".grape-collide-backup"
        os.makedirs(backup, exist_ok=True)
        os.makedirs(wt, exist_ok=True)
        with open(os.path.join(backup, "a.txt"), "w", encoding="utf-8") as f:
            f.write("backup-a\n")
        with open(os.path.join(backup, "extra.txt"), "w", encoding="utf-8") as f:
            f.write("extra\n")
        with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
            f.write("baseline-a\n")
        assert repogit._merge_collided_assets(backup, wt) is False
        assert os.path.isdir(backup), "未合并条目存在时备份必须保留（留证）"
        assert os.path.isfile(os.path.join(backup, "a.txt"))
        assert os.path.isfile(os.path.join(wt, "extra.txt")), "可合并条目应已迁入"
        with open(os.path.join(wt, "a.txt"), encoding="utf-8") as f:
            assert f.read() == "baseline-a\n", "同名文件以基线为准"

    def test_is_already_exists_collision(self):
        """错误特征匹配：真实 already exists stderr 命中；其它失败不命中。"""
        assert repogit._is_already_exists_collision(
            "fatal: '/ws/issues/16/coding' already exists") is True
        assert repogit._is_already_exists_collision(
            "fatal: unable to access") is False
        assert repogit._is_already_exists_collision("") is False


class TestReadonlyErrnoJudgement:
    """Issue #3482（D）：只读判据 = stat 目标目录 + errno(EROFS/EACCES/EPERM)，
    不再依赖 stderr 文案白名单。"""

    @staticmethod
    def _mock_errno(monkeypatch, path_prefix, err):
        real_open = os.open
        root = os.path.abspath(path_prefix)

        def _fake_open(path, flags, *a, **kw):
            if os.path.abspath(str(path)).startswith(root):
                raise OSError(err, os.strerror(err))
            return real_open(path, flags, *a, **kw)

        monkeypatch.setattr(os, "open", _fake_open)

    def test_erofs_eprem_eacces_detected(self, tmp_path, monkeypatch):
        import errno as _errno
        for err, code in ((_errno.EROFS, "EROFS"), (_errno.EACCES, "EACCES"),
                          (_errno.EPERM, "EPERM")):
            with monkeypatch.context() as m:
                self._mock_errno(m, str(tmp_path), err)
                reason = repogit.readonly_errno_reason(str(tmp_path))
                assert code in reason, f"errno {code} 应判只读: {reason}"

    def test_other_errno_not_readonly(self, tmp_path, monkeypatch):
        import errno as _errno
        for err in (_errno.ENOSPC, _errno.EIO):
            with monkeypatch.context() as m:
                self._mock_errno(m, str(tmp_path), err)
                assert repogit.readonly_errno_reason(str(tmp_path)) == "", \
                    f"errno {err} 不是只读，不许据此降级建树"

    def test_writable_dir_not_readonly(self, tmp_path):
        assert repogit.readonly_errno_reason(str(tmp_path)) == ""
        assert repogit.readonly_errno_reason(os.path.join(str(tmp_path), "gone")) == ""

    def test_readonly_base_writable_target_judged_readonly(self, tmp_path, monkeypatch):
        """Issue #3482 复审 Required：#2245 只读基线 + 可写目标——base 命中 errno
        即判只读（前缀区分 target/base），否则该场景会误走 refuse degrade。"""
        import errno as _errno
        base = tmp_path / "base"
        base.mkdir()
        target = tmp_path / "ws" / "issues" / "1" / "coding"
        target.mkdir(parents=True)
        self._mock_errno(monkeypatch, str(base), _errno.EROFS)
        reason = repogit.readonly_errno_reason(str(target), str(base))
        assert reason.startswith("base:"), f"只读基线应标 base 命中: {reason}"
        assert "EROFS" in reason

    def test_readonly_target_prefix_distinguishes_location(self, tmp_path, monkeypatch):
        import errno as _errno
        self._mock_errno(monkeypatch, str(tmp_path), _errno.EACCES)
        reason = repogit.readonly_errno_reason(str(tmp_path), "/no-such-base")
        assert reason.startswith("target:") and "EACCES" in reason

    def test_both_writable_not_readonly(self, tmp_path):
        base = tmp_path / "b"
        base.mkdir()
        assert repogit.readonly_errno_reason(str(tmp_path), str(base)) == ""


class TestWorkspaceMissingIsError:
    """Issue #3482（C）：`git dir not found` 不再降级只读。"""

    def test_missing_gitdir_marks_workspace_missing_not_readonly(self):
        wt = repogit.issue_dir_for("gitcode:owner/repo", "coding", "22")
        os.makedirs(wt, exist_ok=True)  # 有目录但无 .git
        env = {}
        prompt, status = repogit.inject_git_write_context(
            {"repo_key": "gitcode:owner/repo", "agent_type": "coding",
             "issue_id": "#22"}, env, "p")
        assert status.get("writable") is False
        assert status.get("mode") == "missing"
        assert "GRAPE_GIT_READONLY" not in env, "gitdir 不存在不是只读降级"
        assert env.get("GRAPE_GIT_WRITABLE") == "false"
        assert env.get("GRAPE_GIT_WORKSPACE_MISSING") == "true"
        assert "NOT a read-only session" in prompt


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
                            lambda rk, ag, num, token="", work_branch="", task_id="", strict_override=None: (rk, token))
        local_called = {"n": 0}

        class _FakeRepo:
            def __init__(self, rk):
                pass

            @property
            def bot_token(self):
                local_called["n"] += 1
                return "tok_local"

        monkeypatch.setattr("src.kernel.repo.Repo", _FakeRepo)
        env = {"GRAPE_LINKED_REPO_TOKENS": '{"gitcode:OPCTeam/HDBlogService": "tok_env"}'}
        wts = repogit.ensure_linked_repo_worktrees(self.LINKED, "coding", "466", env=env)
        assert wts and wts[0]["worktree_dir"] == ("gitcode:OPCTeam/HDBlogService", "tok_env")
        assert local_called["n"] == 0, "env 有 token 时不应回退本地解析"

    def test_env_missing_key_falls_back_to_local(self, monkeypatch):
        """env 无该仓键 → 回退本地 Repo.bot_token（本地模式可用性保持）。"""
        monkeypatch.setattr(repogit, "ensure_issue_worktree",
                            lambda rk, ag, num, token="", work_branch="", task_id="", strict_override=None: (rk, token))

        class _FakeRepo:
            def __init__(self, rk):
                pass

            @property
            def bot_token(self):
                return "tok_local"

        monkeypatch.setattr("src.kernel.repo.Repo", _FakeRepo)
        env = {"GRAPE_LINKED_REPO_TOKENS": '{"gitcode:other/repo": "tok_env"}'}
        wts = repogit.ensure_linked_repo_worktrees(self.LINKED, "coding", "466", env=env)
        assert wts and wts[0]["worktree_dir"] == ("gitcode:OPCTeam/HDBlogService", "tok_local")

    def test_both_missing_skips(self, monkeypatch):
        """env 与本地均无 token → 该仓 skip（维持降级语义，不抛异常）。"""
        monkeypatch.setattr(repogit, "ensure_issue_worktree",
                            lambda rk, ag, num, token="", work_branch="", task_id="", strict_override=None: (rk, token))

        class _FakeRepo:
            def __init__(self, rk):
                pass

            @property
            def bot_token(self):
                return ""

        monkeypatch.setattr("src.kernel.repo.Repo", _FakeRepo)
        wts = repogit.ensure_linked_repo_worktrees(self.LINKED, "coding", "466", env={})
        assert wts == [], "双无 token 应 skip 该关联仓"

    def test_env_json_invalid_falls_back(self, monkeypatch):
        """env JSON 损坏 → 视为无 env token，回退本地解析。"""
        monkeypatch.setattr(repogit, "ensure_issue_worktree",
                            lambda rk, ag, num, token="", work_branch="", task_id="", strict_override=None: (rk, token))

        class _FakeRepo:
            def __init__(self, rk):
                pass

            @property
            def bot_token(self):
                return "tok_local"

        monkeypatch.setattr("src.kernel.repo.Repo", _FakeRepo)
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

        monkeypatch.setattr("src.kernel.repo.Repo", _FakeRepo)
        env = {"GRAPE_LINKED_REPO_TOKENS": '{"gitcode:OPCTeam/HDBlogService": "tok_env"}'}
        result = repogit.ensure_repo_base_multi(self.LINKED, env=env)
        assert result["gitcode:OPCTeam/HDBlogService"] == {
            "rk": "gitcode:OPCTeam/HDBlogService", "token": "tok_env"}

    def test_default_env_reads_os_environ(self, monkeypatch):
        """env 参数缺省（None）→ 读 os.environ（本地直调/进程内兼容）。"""
        monkeypatch.setattr(repogit, "ensure_issue_worktree",
                            lambda rk, ag, num, token="", work_branch="", task_id="", strict_override=None: (rk, token))
        monkeypatch.setenv("GRAPE_LINKED_REPO_TOKENS",
                           '{"gitcode:OPCTeam/HDBlogService": "tok_osenv"}')

        class _FakeRepo:
            def __init__(self, rk):
                pass

            @property
            def bot_token(self):
                return "tok_local"

        monkeypatch.setattr("src.kernel.repo.Repo", _FakeRepo)
        wts = repogit.ensure_linked_repo_worktrees(self.LINKED, "coding", "466")
        assert wts and wts[0]["worktree_dir"] == ("gitcode:OPCTeam/HDBlogService", "tok_osenv")


class TestPrScopedWorktree:
    """Issue #3375：同 Issue 多 PR 派生独立分支/目录，清理链路按 PR 维度对称。"""

    def test_multi_pr_derives_branch_and_dir(self):
        wt12 = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5",
                                            pr_number="12")
        wt13 = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5",
                                            pr_number="13")
        assert wt12 and wt13 and wt12 != wt13
        assert wt12.endswith(os.path.join("issues", "5", "pr-12", "coding")), wt12
        assert wt13.endswith(os.path.join("issues", "5", "pr-13", "coding")), wt13
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], wt12).stdout.strip() == "feat/issue-5-pr-12"
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], wt13).stdout.strip() == "feat/issue-5-pr-13"

    def test_no_pr_branch_unchanged(self):
        wt = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5")
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], wt).stdout.strip() == "feat/issue-5"

    def test_cleanup_pr_scoped_only_removes_target(self):
        wt12 = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5",
                                            pr_number="12")
        wt13 = repogit.ensure_issue_worktree("gitcode:owner/repo", "coding", "5",
                                            pr_number="13")
        repogit.cleanup_issue_worktree("gitcode:owner/repo", "coding", "5",
                                       pr_number="12")
        assert not os.path.isdir(wt12)
        assert os.path.isdir(wt13)

    def test_cleanup_issue_less_pr_workdir(self):
        ws = repogit.pr_workdir_dir("gitcode:owner/repo", "coding", "452")
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "out.txt"), "w", encoding="utf-8") as f:
            f.write("artifact")
        assert repogit.cleanup_pr_worktree("gitcode:owner/repo", "coding", "452")
        assert not os.path.isdir(ws)

    def test_cleanup_pr_worktree_missing_is_noop(self):
        assert repogit.cleanup_pr_worktree("gitcode:owner/repo", "coding", "999") is False
