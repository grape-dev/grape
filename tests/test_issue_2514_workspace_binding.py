"""Issue #2514 回归：coding 工作区与「当前 Issue + 当前基线」严格绑定。

覆盖 Spec（评论 id 200731012）验收要求：
1. 复用闸门：错误分支 / 脏树 / 无归属标记 → 拒绝复用并重建，日志含 `workspace.reuse_refused`
   与 dirty 文件数，证据落 `artifacts/workspace-<task_id>-reuse-refused.patch`；
2. 基线闸门：HEAD 与 `origin/<baseline>` 分叉 → 进入 Agent 前抛 WorkspaceBaselineError，
   错误六要素 issue/path/expected_baseline/actual_head/ahead/behind 齐全；
3. **Critical 回归（PR #1176 遗留）**：注入基线在远端不存在时，expected 必须取**已核实的
   解析基线**（回退到默认分支）且回退可见，闸门不得恒报 head_unreachable；
4. 终态清理：脏树先存档再清、未推提交归档后清、远端不可核对时保留现场；
5. 派发前巡检：回收超 TTL 带标记残留 / 空 orphan，活跃 cwd 与无标记目录不误删。

夹具为真实本地 git（bare mirror + 本地源仓），零网络。
"""
import json
import logging
import os
import subprocess

import pytest

from src.domains.runtime.engine import repogit
from src.domains.runtime.engine.repogit import WorkspaceBaselineError

REPO = "gitcode:owner/repo"


def _git(args, cwd):
    return subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True)


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
    # 默认 strict=on；显式清理 worker 级环境，避免用例间污染
    monkeypatch.delenv("GRAPE_WORKSPACE_STRICT", raising=False)
    monkeypatch.delenv("GRAPE_WORKSPACE_TTL_HOURS", raising=False)
    monkeypatch.delenv("GRAPE_WORKSPACE_PATROL_INTERVAL", raising=False)
    # worktree 内 commit 需要 git 身份（bare mirror 无全局配置）
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    yield


def _advance_main(git_src, text):
    """源仓 main 前进一个 commit（模拟基线更新）。"""
    (git_src / "a.txt").write_text(text, encoding="utf-8")
    _git(["add", "-A"], str(git_src))
    _git(["commit", "-m", f"main -> {text}"], str(git_src))


def _wt(agent="coding", num="2514", **kw):
    wt = repogit.ensure_issue_worktree(REPO, agent, num, **kw)
    assert wt, "ensure_issue_worktree 应成功预置 worktree"
    return wt


def _artifacts(*name):
    from src.kernel.repo_key import workspace_repo_dir
    return os.path.join(workspace_repo_dir(REPO), "artifacts", *name)


# ===================== 干净预置与直通复用 =====================

class TestFreshProvision:
    def test_marker_written_and_excluded_from_git(self):
        """归属标记落盘 + 写进 info/exclude：status 不误报脏、add -A 不提交。"""
        wt = _wt(task_id="t-1")
        marker_path = os.path.join(wt, repogit.WORKSPACE_MARKER)
        assert os.path.isfile(marker_path)
        with open(marker_path, encoding="utf-8") as f:
            marker = json.load(f)
        assert marker["issue_number"] == "2514"
        assert marker["agent"] == "coding"
        assert marker["task_id"] == "t-1"
        assert marker["baseline"] == "main"
        assert marker["created_at"]
        st = _git(["status", "--porcelain"], wt)
        assert repogit.WORKSPACE_MARKER not in (st.stdout or "")
        add = _git(["add", "-A"], wt)
        assert add.returncode == 0
        staged = _git(["diff", "--cached", "--name-only"], wt)
        assert repogit.WORKSPACE_MARKER not in (staged.stdout or "")

    def test_clean_reuse_pass_through(self):
        """同一 Issue 二次 ensure 直通复用（路径/分支不变，task_id 刷新）。"""
        a = _wt(task_id="t-1")
        b = _wt(task_id="t-2")
        assert a == b
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], b).stdout.strip() == "feat/issue-2514"
        with open(os.path.join(b, repogit.WORKSPACE_MARKER), encoding="utf-8") as f:
            assert json.load(f)["task_id"] == "t-2"

    def test_ff_allowed_when_behind_only(self, git_src):
        """ahead=0, behind>0 → fast-forward 对齐基线并记录，不报错。"""
        wt = _wt()
        _advance_main(git_src, "v2")
        wt2 = _wt()
        assert wt2 == wt
        with open(os.path.join(wt2, "a.txt"), encoding="utf-8") as f:
            assert f.read() == "v2"
        r = _git(["rev-list", "--left-right", "--count", "HEAD...origin/main"], wt2)
        assert r.stdout.split() == ["0", "0"]

    def test_ahead_only_allowed(self):
        """ahead>0, behind=0（本 Issue 多回合提交）→ 正常复用，不误杀。"""
        wt = _wt()
        with open(os.path.join(wt, "round1.txt"), "w", encoding="utf-8") as f:
            f.write("r1")
        _git(["add", "-A"], wt)
        _git(["commit", "-m", "round1"], wt)
        wt2 = _wt()
        assert wt2 == wt
        assert os.path.isfile(os.path.join(wt2, "round1.txt"))


# ===================== 复用闸门 =====================

class TestReuseGates:
    def test_dirty_reuse_refused_and_rebuilt(self, caplog):
        """脏树（他人未提交改动）→ 拒绝复用 → 存档 → 重建干净工作区。"""
        wt = _wt(task_id="t-a")
        with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
            f.write("unrelated local change from another issue")
        wt2 = _wt(task_id="t-b")
        assert wt2 == wt
        assert repogit._worktree_state(wt2)[0] == 0
        with open(os.path.join(wt2, "a.txt"), encoding="utf-8") as f:
            assert f.read() == "v1"
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], wt2).stdout.strip() == "feat/issue-2514"
        art = _artifacts("workspace-t-b-reuse-refused.patch")
        assert os.path.isfile(art), "拒绝复用前必须先落 patch 存档"
        with open(art, encoding="utf-8") as f:
            content = f.read()
        assert "unrelated local change" in content
        assert "dirty_files=1" in content
        msgs = [r.getMessage() for r in caplog.records]
        assert any("workspace.reuse_refused" in m and "reason=dirty" in m
                   and "dirty_files=1" in m for m in msgs), \
            "必须有 workspace.reuse_refused 结构化日志（含 dirty 文件数）"

    def test_wrong_branch_refused_and_rebuilt(self):
        """#2506 实测形态：检出在其它 Issue 分支（feat/issue-1588）→ 拒绝复用并重建。"""
        wt = _wt()
        _git(["checkout", "-b", "feat/issue-1588"], wt)
        wt2 = _wt(task_id="t-d")
        assert wt2 == wt
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], wt2).stdout.strip() == "feat/issue-2514"
        # 闸门口径：树干净（平台 askpass 轻文件不计入脏）
        assert repogit._worktree_state(wt2)[0] == 0

    def test_stale_marker_refused_and_rebuilt(self):
        """无归属标记（历史 Issue 残留）→ reason=stale_marker → 不复用，重建。"""
        wt = _wt()
        os.remove(os.path.join(wt, repogit.WORKSPACE_MARKER))
        with open(os.path.join(wt, "b.txt"), "w", encoding="utf-8") as f:
            f.write("stale residue")
        wt2 = _wt(task_id="t-c")
        assert wt2 == wt
        assert not os.path.exists(os.path.join(wt2, "b.txt"))
        with open(os.path.join(wt2, repogit.WORKSPACE_MARKER), encoding="utf-8") as f:
            assert json.load(f)["task_id"] == "t-c"

    def test_wrong_branch_and_dirty_together(self):
        """验收组合形态「错误分支 + 脏文件」：不在该目录直接提交，而是重建干净工作区。"""
        wt = _wt(task_id="t-e")
        _git(["checkout", "-b", "feat/issue-1588"], wt)
        with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
            f.write("dirty from foreign issue round")
        with open(os.path.join(wt, "foreign.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        wt2 = _wt(task_id="t-f")
        assert wt2 == wt
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], wt2).stdout.strip() == "feat/issue-2514"
        assert repogit._worktree_state(wt2)[0] == 0
        assert not os.path.exists(os.path.join(wt2, "foreign.txt"))
        art = _artifacts("workspace-t-f-reuse-refused.patch")
        assert os.path.isfile(art), "错误分支 + 脏文件必须先存档再重建"


# ===================== 基线闸门 =====================

class TestBaselineGate:
    def test_diverged_head_raises_structured_error(self, git_src):
        """真分叉（ahead>0 且 behind>0）→ 进入 Agent 前结构化失败，六要素齐全。"""
        wt = _wt()
        with open(os.path.join(wt, "local.txt"), "w", encoding="utf-8") as f:
            f.write("local")
        _git(["add", "-A"], wt)
        _git(["commit", "-m", "local commit"], wt)
        _advance_main(git_src, "v2")
        with pytest.raises(WorkspaceBaselineError) as ei:
            _wt()
        msg = str(ei.value)
        assert "workspace baseline mismatch" in msg
        assert "issue=#2514" in msg
        assert f"path={wt}" in msg
        assert "expected_baseline=main" in msg
        assert "actual_head=feat/issue-2514@" in msg
        assert "ahead=1" in msg
        assert "behind=1" in msg
        assert "reason=diverged" in msg
        assert ei.value.path == wt
        assert ei.value.expected_baseline == "main"
        assert (ei.value.ahead, ei.value.behind) == (1, 1)
        d = ei.value.to_dict()
        assert d["issue"] == "2514" and d["expected_baseline"] == "main"
        assert d["ahead"] == 1 and d["behind"] == 1

    def test_injected_baseline_absent_falls_back_visibly(self, caplog):
        """Critical 回归（PR #1176 遗留 / 同 #2449）：注入基线不存在 → expected 取回退基线。

        旧实现把注入值直接塞进 expected → fetch/rev-list 必失败 → 闸门恒报 head_unreachable，
        且 `_default_base_branch` 的回退结果不参与判定。本用例断言：
        - 不抛 head_unreachable；
        - 标记 baseline = 已核实的解析基线（main）；
        - 回退有结构化日志 workspace.baseline_fallback（requested 可见）。
        """
        wt = _wt(work_branch="arch-weekly-11-zj", task_id="t-fb")
        with open(os.path.join(wt, repogit.WORKSPACE_MARKER), encoding="utf-8") as f:
            assert json.load(f)["baseline"] == "main"
        msgs = [r.getMessage() for r in caplog.records]
        assert any("workspace.baseline_fallback" in m and "arch-weekly-11-zj" in m
                   for m in msgs), "基线回退必须可见（workspace.baseline_fallback）"
        assert not any("head_unreachable" in m for m in msgs), \
            "注入基线不存在不得把闸门打成恒失败"

    def test_diverged_error_carries_requested_baseline(self, git_src):
        """分叉错误里同时给 resolved expected 与 injected requested（可溯源）。"""
        wt = _wt(task_id="t-x")
        with open(os.path.join(wt, "local.txt"), "w", encoding="utf-8") as f:
            f.write("local")
        _git(["add", "-A"], wt)
        _git(["commit", "-m", "local commit"], wt)
        _advance_main(git_src, "v2")
        with pytest.raises(WorkspaceBaselineError) as ei:
            _wt(work_branch="arch-weekly-11-zj")
        msg = str(ei.value)
        assert "expected_baseline=main" in msg
        assert "requested_baseline=arch-weekly-11-zj" in msg
        assert ei.value.to_dict()["requested_baseline"] == "arch-weekly-11-zj"

    def test_strict_off_downgrades_to_warning(self, git_src, monkeypatch):
        """GRAPE_WORKSPACE_STRICT=0 → 降级 warn 继续复用（escape hatch）。"""
        wt = _wt()
        with open(os.path.join(wt, "local.txt"), "w", encoding="utf-8") as f:
            f.write("local")
        _git(["add", "-A"], wt)
        _git(["commit", "-m", "local commit"], wt)
        _advance_main(git_src, "v2")
        monkeypatch.setenv("GRAPE_WORKSPACE_STRICT", "0")
        assert _wt(strict_override="0") == wt  # 任务 env 显式值优先

    def test_inspector_skips_branch_baseline_gate(self, git_src, caplog):
        """qa（inspector）按交付 sha 对齐，不走分支基线闸门（不因交付提交 ahead 而失败）。"""
        caplog.set_level(logging.INFO)
        coding_wt = _wt(agent="coding")
        with open(os.path.join(coding_wt, "c.txt"), "w", encoding="utf-8") as f:
            f.write("coding round")
        _git(["add", "-A"], coding_wt)
        _git(["commit", "-m", "coding round"], coding_wt)
        assert _git(["push", "origin", "HEAD:refs/heads/feat/issue-2514"], coding_wt).returncode == 0
        _advance_main(git_src, "v2")  # 交付分支与基线分叉
        qa_wt = _wt(agent="qa", task_id="t-qa",
                    delivery_branch="feat/issue-2514", delivery_info={})
        head = _git(["rev-parse", "HEAD"], qa_wt).stdout.strip()
        delivery = _git(["rev-parse", "origin/feat/issue-2514"], qa_wt).stdout.strip()
        assert head == delivery, "qa 工作区应对齐到交付 sha"
        msgs = [r.getMessage() for r in caplog.records]
        assert any("workspace baseline gate skipped for inspector" in m for m in msgs), \
            "inspector 不应走分支基线闸门（交付提交相对基线 ahead 属正常）"
        # 二次复用同样通过（对齐后的 HEAD 不触发 wrong_branch / 基线失败）
        assert _wt(agent="qa", task_id="t-qa2",
                   delivery_branch="feat/issue-2514", delivery_info={}) == qa_wt


# ===================== 终态清理 =====================

class TestTerminalCleanup:
    def test_terminal_cleanup_archives_dirty_then_rebuild_clean(self):
        """脏树先落 workspace-<task_id>.patch，终态删除后再 ensure 得到干净重建。"""
        wt = _wt(task_id="t-cl")
        base = repogit.repo_base_dir(REPO)
        with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
            f.write("dirty before terminal cleanup")
        assert repogit.remove_issue_worktree_if_pushed(REPO, "coding", "2514",
                                                      task_id="t-cl") is True
        assert not os.path.isdir(wt)
        assert wt not in (_git(["worktree", "list", "--porcelain"], base).stdout or "")
        art = _artifacts("workspace-t-cl.patch")
        assert os.path.isfile(art), "脏树移除前必须落 workspace-<task_id>.patch"
        with open(art, encoding="utf-8") as f:
            assert "dirty before terminal cleanup" in f.read()
        wt2 = _wt(task_id="t-cl2")
        assert wt2 == wt
        with open(os.path.join(wt2, "a.txt"), encoding="utf-8") as f:
            assert f.read() == "v1"
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], wt2).stdout.strip() == "feat/issue-2514"


# ===================== 派发前巡检 =====================

class TestPatrol:
    def _ws(self):
        from src.kernel.config import WORKSPACE_DIR
        return WORKSPACE_DIR

    def test_patrol_recycles_stale_marked_and_orphan_empty(self):
        """超 TTL 的无活跃任务带标记目录 + 空 orphan 目录被回收。"""
        wt_marked = _wt(task_id="t-old")
        orphan_dir = os.path.join(self._ws(), "gitcode", "owner", "repo",
                                  "issues", "1588", "coding")
        os.makedirs(orphan_dir, exist_ok=True)
        for d in (wt_marked, orphan_dir):
            os.utime(d, (0, 0))
        stats = repogit.patrol_workspaces(active_dirs=[], ttl_hours=1)
        assert stats["removed"] >= 1, "超 TTL 带标记目录应被回收"
        assert not os.path.isdir(wt_marked)
        assert not os.path.isdir(orphan_dir), "空 orphan 残留应被回收"
        assert stats["orphan"] >= 1

    def test_patrol_protects_active_and_unmarked_dirs(self):
        """活跃任务 cwd 与无标记（人工）目录不误删；未超 TTL 目录保留。"""
        wt_live = _wt(task_id="t-live")
        manual_dir = os.path.join(self._ws(), "gitcode", "owner", "repo",
                                  "issues", "2001", "coding")
        os.makedirs(manual_dir, exist_ok=True)
        with open(os.path.join(manual_dir, "work.txt"), "w", encoding="utf-8") as f:
            f.write("manual")
        os.utime(wt_live, (0, 0))
        os.utime(manual_dir, (0, 0))
        stats = repogit.patrol_workspaces(active_dirs=[wt_live], ttl_hours=1)
        assert os.path.isdir(wt_live), "活跃任务 cwd 不应被回收（in-flight 保护）"
        assert os.path.isdir(manual_dir), "无归属标记的非空目录不得回收（防误删人工 worktree）"
        assert stats["kept"] >= 1

    def test_patrol_keeps_fresh_dirs(self):
        """未超 TTL 的目录保留。"""
        wt = _wt(task_id="t-fresh")
        stats = repogit.patrol_workspaces(active_dirs=[], ttl_hours=24)
        assert os.path.isdir(wt)
        assert stats["kept"] >= 1

    def test_patrol_archives_dirty_marked_before_removal(self):
        """超期带标记且脏 → 先存档再回收（证据不丢）。"""
        wt = _wt(task_id="t-dirty")
        with open(os.path.join(wt, "a.txt"), "w", encoding="utf-8") as f:
            f.write("dirty in stale workspace")
        os.utime(wt, (0, 0))
        repogit.patrol_workspaces(active_dirs=[], ttl_hours=1)
        assert not os.path.isdir(wt)
        art = _artifacts("workspace-unknown-patrol.patch")
        assert os.path.isfile(art), "巡检回收脏目录前必须先存档"