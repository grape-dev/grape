# -*- coding: utf-8 -*-
"""Issue #4353 回归：建树前置判定（复用 / 残留清理 / 真冲突）+ 拒绝降级与只读标记互斥。

生产形态（1 小时 7 次 / 3 条路径）：同名残留目录 → `git worktree add` 报
`fatal: '<path>' already exists` → `non-readonly failure, refuse degrade` →
`read-only mode marked`，QA 会话在只读区跑完整流程、终态仍可能 completed。

本用例覆盖 Spec 的验收项：
1. 已是本仓合法 worktree → 复用，不重复 add，不破坏 `.skills`；
2. 残留目录（无 `.git` / `.git` 指向失效 gitdir，含**已登记**的残留）→ prune + 安全移走
   后建树成功，`.skills` 不丢失；
3. 真冲突（目标非目录 / `.git` 属于其它仓库）→ 判 failed + `error_code=WORKSPACE_WORKTREE_CONFLICT`；
4. `refuse degrade` 与 `read-only mode marked` **不同时出现**；
5. 同名 PR 跨轮重派（新 task_id）不再命中同一残留目录。

夹具为真实本地 git（bare mirror + 本地源仓），零网络。
"""
import json
import logging
import os
import subprocess

import pytest

from src.domains.runtime.engine import repogit

REPO = "gitcode:owner/repo"


def _git(args, cwd):
    return subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True)


class _Result:
    """`_run_git` 返回值的替身（只用到 returncode / stdout / stderr）。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def git_src(tmp_path):
    """本地源仓库（main 基线 + 一个 commit），repogit 的 clone_url 指向它。"""
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
    monkeypatch.delenv("GRAPE_WORKSPACE_STRICT", raising=False)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    yield


@pytest.fixture(autouse=True)
def _clean_state():
    """失败现场/拒绝降级状态是模块级字典，用例间必须隔离。"""
    repogit.clear_worktree_failure()
    yield
    repogit.clear_worktree_failure()


def _wt(agent="coding", num="4353", **kw):
    wt = repogit.ensure_issue_worktree(REPO, agent, num, **kw)
    assert wt, f"ensure_issue_worktree({agent}, {num}) 应成功建树"
    return wt


def _has_git(wt):
    p = os.path.join(wt, ".git")
    return os.path.isdir(p) or os.path.isfile(p)


def _write_skill(wt, rel="implementation/SKILL.md"):
    path = os.path.join(wt, ".skills", rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# skill\n")
    return path


def _registered_paths():
    base = repogit.repo_base_dir(REPO)
    r = _git(["worktree", "list", "--porcelain"], base)
    return [line[len("worktree "):].strip() for line in r.stdout.splitlines()
            if line.startswith("worktree ")]


def _stale_siblings(wt):
    parent = os.path.dirname(wt)
    prefix = os.path.basename(wt) + ".stale-"
    return [n for n in os.listdir(parent) if n.startswith(prefix)] if os.path.isdir(parent) else []


# ===================== 路径一：合法 worktree → 复用 =====================

class TestLegalWorktreeReuse:
    def test_reuse_does_not_readd_and_keeps_assets(self, caplog):
        """已是本仓合法 worktree（`.git` 指针有效）→ 复用，不重复 add，不动 `.skills`。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        wt = _wt(task_id="t-round1")
        skill = _write_skill(wt)
        got = _wt(task_id="t-round2")
        assert got == wt, "合法 worktree 必须原地复用"
        assert _has_git(wt)
        assert os.path.isfile(skill), "复用不得破坏上一轮物化的 .skills"
        assert not _stale_siblings(wt), "复用路径不得产生隔离目录"
        assert "workspace.worktree_target_legal" in caplog.text
        assert "workspace.reuse_ok" in caplog.text
        # 登记表里该路径只有一条（没有重复 add）
        hits = [p for p in _registered_paths() if os.path.realpath(p) == os.path.realpath(wt)]
        assert len(hits) == 1, f"复用不得重复登记 worktree: {hits}"

    def test_marker_records_latest_dispatch_task_id(self):
        """复用路径把本轮 task_id 写回归属标记（跨轮派发身份可观测）。"""
        wt = _wt(task_id="t-old")
        _wt(task_id="t-new")
        with open(os.path.join(wt, repogit.WORKSPACE_MARKER), encoding="utf-8") as f:
            marker = json.load(f)
        assert marker.get("task_id") == "t-new"


# ===================== 路径二：残留目录 → prune + 移走 → 建树 =====================

class TestResidualTargetCleanup:
    def test_residual_without_git_pruned_and_rebuilt(self, caplog):
        """无 `.git` 的残留（上一轮产物）→ prune + 安全移走 → 建树成功，资产留证。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        wt = repogit.issue_dir_for(REPO, "coding", "4301")
        _write_skill(wt)
        with open(os.path.join(wt, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("user work\n")
        got = _wt(num="4301")
        assert got == wt
        assert _has_git(wt), "残留处置后必须重建出带 .git 的 worktree"
        assert not os.path.exists(os.path.join(wt, "notes.txt")), "未知内容不得进入新工作区"
        stales = _stale_siblings(wt)
        assert len(stales) == 1, f"残留应整体移走留证（单个 .stale 目录）: {stales}"
        stale = os.path.join(os.path.dirname(wt), stales[0])
        assert os.path.isfile(os.path.join(stale, "notes.txt")), "移走的目录必须保留未知内容"
        assert os.path.isfile(os.path.join(stale, ".skills", "implementation", "SKILL.md")), \
            "移走的目录必须保留 .skills（迁移不删除）"
        assert "workspace.worktree_target_residual_cleaned" in caplog.text

    def test_registered_residual_without_git_rebuilt(self, caplog):
        """**已登记**残留（目录还在、`.git` 指针被删）→ prune 后重建成功。

        这正是生产事故的形态：登记仍在 → 单纯 `git worktree add` 恒报
        `fatal: '<path>' already exists`；必须先移走再 prune 才回收登记。
        """
        caplog.set_level(logging.INFO, logger="devops-bot")
        wt = _wt(num="4302")
        skill = _write_skill(wt)
        # 模拟上一轮中断留下的残留：目录还在、`.git` 指针丢失，但 worktree 仍被登记
        os.remove(os.path.join(wt, ".git"))
        assert any(os.path.realpath(p) == os.path.realpath(wt)
                   for p in _registered_paths()), "前置条件：目标仍被登记"
        # 反证：直接对该路径 `worktree add` 必然失败——这正是生产的失败形态
        # （`fatal: '<path>' already exists`，加 `--force` 也无效）。
        _probe = _git(["-C", repogit.repo_base_dir(REPO), "worktree", "add", wt,
                       "-b", "probe/should-fail", "origin/main"],
                      repogit.repo_base_dir(REPO))
        assert _probe.returncode != 0 and "already exists" in _probe.stderr, \
            f"前置条件不成立（应复现 already exists）: {_probe.stderr}"
        got = _wt(num="4302")
        assert got == wt
        assert _has_git(wt), "已登记残留必须先 prune 再建树，否则 add 恒报 already exists"
        hits = [p for p in _registered_paths() if os.path.realpath(p) == os.path.realpath(wt)]
        assert len(hits) == 1, f"残留的失效登记必须被 prune 回收: {hits}"
        # 新工作区可写（不是只读会话）
        with open(os.path.join(wt, "after-fix.txt"), "w", encoding="utf-8") as f:
            f.write("ok\n")
        _git(["config", "user.email", "agent@example.com"], wt)
        _git(["config", "user.name", "Agent"], wt)
        assert _git(["add", "after-fix.txt"], wt).returncode == 0
        assert _git(["commit", "-m", "verify writable after residual rebuild"], wt).returncode == 0
        assert os.path.isfile(skill) or _stale_siblings(wt), "`.skills` 不得被静默删除"
        assert "workspace.worktree_target_residual_cleaned" in caplog.text

    def test_residual_rebuild_keeps_readonly_flag_absent(self):
        """残留重建成功后 git 可写：不得置 `GRAPE_GIT_READONLY`。"""
        wt = _wt(num="4303")
        os.remove(os.path.join(wt, ".git"))
        got = _wt(num="4303")
        env = {}
        _prompt, status = repogit.inject_git_write_context(
            {"repo_key": REPO, "agent_type": "coding", "issue_id": "#4303"}, env, "p")
        assert got and status.get("writable") is True
        assert env.get("GRAPE_GIT_WRITABLE") == "true"
        assert "GRAPE_GIT_READONLY" not in env


# ===================== 路径三：真冲突 → failed + error_code =====================

class TestTrueConflict:
    def test_target_path_non_directory_is_conflict(self, caplog):
        """目标路径被普通文件占用 → 真冲突：返回 None + 拒绝降级 + error_code。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        wt = repogit.issue_dir_for(REPO, "coding", "4304")
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        with open(wt, "w", encoding="utf-8") as f:
            f.write("not a workspace dir\n")
        assert repogit.ensure_issue_worktree(REPO, "coding", "4304") is None, \
            "真冲突必须判失败，不得降级只读继续执行"
        refused = repogit.degrade_refused(REPO, issue_num="4304", agent_type="coding")
        assert refused.get("code") == repogit.WORKSPACE_WORKTREE_CONFLICT
        assert "workspace.worktree_target_conflict" in caplog.text
        assert f"error_code={repogit.WORKSPACE_WORKTREE_CONFLICT}" in caplog.text

    def test_foreign_gitdir_is_conflict_not_disposed(self, tmp_path):
        """`.git` 指向**其它仓库**的 gitdir → 判冲突，不处置别人的工作树。"""
        other = tmp_path / "other-repo"
        other.mkdir()
        _git(["init", "-b", "main"], str(other))
        _git(["config", "user.email", "o@example.com"], str(other))
        _git(["config", "user.name", "Other"], str(other))
        (other / "b.txt").write_text("x", encoding="utf-8")
        _git(["add", "-A"], str(other))
        _git(["commit", "-m", "other"], str(other))
        wt = repogit.issue_dir_for(REPO, "coding", "4305")
        os.makedirs(wt, exist_ok=True)
        with open(os.path.join(wt, ".git"), "w", encoding="utf-8") as f:
            f.write(f"gitdir: {other}/.git\n")
        assert repogit.ensure_issue_worktree(REPO, "coding", "4305") is None
        assert os.path.isfile(os.path.join(wt, ".git")), \
            "别仓 gitdir 属于他人工作树，不得移走/清理"
        assert os.path.isfile(os.path.join(other, ".git", "HEAD")), "别仓版本库不得被破坏"

    def test_conflict_error_surface_carries_error_code(self):
        """建树失败的错误面必须带 `error_code=WORKSPACE_WORKTREE_CONFLICT`。

        测试环境无凭据链，凭据归因会先命中 `credential_not_configured`（#3713 既有优先级），
        这里显式给出「凭据可用」的归因结果，让错误面落到冲突原文 + error_code 上。
        """
        wt = repogit.issue_dir_for(REPO, "coding", "4306")
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        with open(wt, "w", encoding="utf-8") as f:
            f.write("occupied by file\n")
        assert repogit.ensure_issue_worktree(REPO, "coding", "4306") is None
        _tok = type("T", (), {"token": "tk-local", "stage": "", "attribution": ""})()
        err = repogit.worktree_failure_error(REPO, issue_num="4306", agent_type="coding",
                                             token_result=_tok)
        assert "error_code=WORKSPACE_WORKTREE_CONFLICT" in err, err
        assert "worktree target conflict" in err, err


# ============ 拒绝降级 与 read-only 标记 互斥（本 Issue 的核心矛盾） ============

class TestRefuseDegradeVsReadOnly:
    def test_refuse_degrade_never_marks_read_only(self, monkeypatch, caplog):
        """拒绝降级后：会话侧不置只读、不出 read-only 措辞，只出冲突 error_code。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        real_run = repogit._run_git

        def _fake_run(args, **kw):
            if "worktree" in args and "add" in args:
                # 非只读失败（分支被其它 worktree 占用类的文本，不带只读线索）
                return _Result(returncode=128,
                               stderr="fatal: 'refs/heads/feat/issue-4307' is already "
                                      "checked out at '/ws/other'")
            return real_run(args, **kw)

        monkeypatch.setattr(repogit, "_run_git", _fake_run)
        assert repogit.ensure_issue_worktree(REPO, "coding", "4307", task_id="t-conflict") is None
        assert "refuse degrade" in caplog.text, "非只读失败必须走拒绝降级"
        env = {}
        prompt, status = repogit.inject_git_write_context(
            {"repo_key": REPO, "agent_type": "coding", "issue_id": "#4307",
             "task_id": "t-conflict"}, env, "P")
        assert status.get("mode") == "conflict", f"应标冲突而非只读: {status}"
        assert status.get("writable") is False
        assert "GRAPE_GIT_READONLY" not in env, "拒绝降级后不得再标只读（互斥）"
        assert "GRAPE_GIT_READONLY_REASON" not in env
        assert env.get("GRAPE_GIT_WORKSPACE_CONFLICT") == "true"
        assert env.get("GRAPE_GIT_WORKSPACE_ERROR_CODE") == repogit.WORKSPACE_WORKTREE_CONFLICT
        assert "WORKSPACE_WORKTREE_CONFLICT" in prompt
        # 关键断言：refuse degrade 与 read-only mode marked 不同时出现
        _level, _msg = repogit.describe_git_write_mode(status)
        assert _level == "error"
        assert "read-only mode marked" not in _msg
        assert "read-only mode marked" not in caplog.text, \
            "拒绝降级与只读标记必须互斥（不得同轮同时出现）"

    def test_readonly_mode_still_marked_for_readonly_gitdir(self, monkeypatch):
        """真正的只读 gitdir（非拒绝降级）仍标 read-only——互斥不得把只读语义也吞掉。"""
        _wt(num="4308")
        monkeypatch.setattr(repogit, "probe_git_write",
                            lambda gitdir: (False, "Read-only file system"))
        monkeypatch.setattr(repogit, "_relocate_gitdir_to_session",
                            lambda *a, **k: {"ok": False, "error": "EROFS: Read-only file system"})
        env = {}
        _prompt, status = repogit.inject_git_write_context(
            {"repo_key": REPO, "agent_type": "coding", "issue_id": "#4308"}, env, "P")
        assert status.get("mode") == "readonly"
        assert env.get("GRAPE_GIT_READONLY") == "true"
        level, msg = repogit.describe_git_write_mode(status)
        assert level == "warning" and "read-only mode marked" in msg

    def test_degrage_refusal_state_is_task_scoped_and_cleared_on_success(self):
        """拒绝降级状态按 (repo, issue, agent) 隔离，建树成功即清除（不跨轮污染）。"""
        repogit.mark_degrade_refused(REPO, issue_num="4309", agent_type="coding",
                                     reason="simulated")
        assert repogit.degrade_refused(REPO, issue_num="4309", agent_type="coding")
        assert not repogit.degrade_refused(REPO, issue_num="4310", agent_type="coding"), \
            "拒绝降级状态不得串到其它 Issue"
        assert not repogit.degrade_refused(REPO, issue_num="4309", agent_type="qa"), \
            "拒绝降级状态不得串到其它 agent"
        _wt(num="4309")
        assert not repogit.degrade_refused(REPO, issue_num="4309", agent_type="coding"), \
            "建树成功必须清除拒绝降级状态"


# ===================== ⑤ 跨轮重派：不再命中同一残留目录 =====================

class TestRedispatchSamePseudIdent:
    def test_redispatch_disposes_previous_residual(self, caplog):
        """同名 PR（伪 ident）跨轮重派：上一轮残留必被处置，新一轮照常建树。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        ident = "pr_2157_5b8e-uuid"
        wt = repogit.issue_dir_for(REPO, "qa", ident)
        wt1 = _wt(agent="qa", num=ident, task_id="t-round1")
        assert wt1 == wt
        _write_skill(wt, "qa-cli/SKILL.md")
        # 上一轮被强杀：目录留下、`.git` 指针丢失（跨轮重派的「同一残留目录」）
        os.remove(os.path.join(wt, ".git"))
        wt2 = _wt(agent="qa", num=ident, task_id="t-round2")
        assert wt2 == wt and _has_git(wt), "重派必须能重建，不再卡在 already exists"
        with open(os.path.join(wt, repogit.WORKSPACE_MARKER), encoding="utf-8") as f:
            marker = json.load(f)
        assert marker.get("task_id") == "t-round2", "归属标记应记录本轮 task_id"
        # 残留以「本轮 task_id」移走留证：跨轮留证不互相覆盖
        stales = _stale_siblings(wt)
        assert len(stales) == 1, f"每轮残留应各自移走留证: {stales}"
        assert "t-round2" in stales[0], f"留证目录名应带本轮 task_id: {stales[0]}"
        assert os.path.isfile(os.path.join(os.path.dirname(wt), stales[0],
                                          ".skills", "qa-cli", "SKILL.md")), \
            "移走的残留必须保留 .skills"

    def test_dispatch_logs_carry_task_id(self, caplog):
        """三条路径的结构化日志都带 repo/issue/agent/task/path/decision 字段。"""
        caplog.set_level(logging.INFO, logger="devops-bot")
        wt = _wt(num="4311", task_id="t-log")
        assert "workspace.worktree_target_absent" in caplog.text
        caplog.clear()
        _wt(num="4311", task_id="t-log2")
        assert "workspace.worktree_target_legal" in caplog.text
        assert "task=t-log2" in caplog.text
        assert f"path={wt}" in caplog.text
        assert "issue=4311" in caplog.text and "agent=coding" in caplog.text