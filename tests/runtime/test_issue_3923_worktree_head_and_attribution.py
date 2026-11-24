"""Issue #3923：worker 侧建树失败不得被误归因为 credential_not_configured。

根因（两个独立缺陷叠加）：
  1. worker 进程 MANIFEST 不含 `src/api/contracts_wiring.py`，`vcs_provider`
     （及 `vcs_cards`）端口永远绑不上，建树失败的凭据归因必然抛/为空，旧码
     再把「归因为空」当默认值 `not_configured` → worker 上任何 git 失败都变成
     「仓库没配凭据」；
  2. 裸仓 HEAD 被写成 `ref: refs/remotes/origin/<baseline>` 时，`git worktree add`
     必然 `fatal: HEAD not found below refs/heads!`，旧码既不校验也不自愈，
     失败只 refuse degrade。

本文件锁四件事：
  1. 裸仓 HEAD 非法（refs/remotes/*）→ 建树入口自愈到 refs/heads/*，日志有修复记录；
  2. `worktree add` 因 HEAD 断裂失败 → 先自愈再重试一次，重试成功即建树成功；
  3. worker（端口未绑）归因不可用 → 报 git 原文 + 失败阶段，**不出现**
     `credential_not_configured`；
  4. 反向回归：确实无凭据（归因跑完且为空）时仍报 `credential_not_configured`。
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from issue_3713_fakes import CredWorld, install, restore_ports, snapshot_ports
from src.domains.runtime.engine import agent as agent_mod
from src.domains.runtime.engine import repogit

REPO = "gitcode:owner/repo"
RK = REPO


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
    monkeypatch.delenv("GRAPE_WORKSPACE_STRICT", raising=False)
    repogit.clear_worktree_failure()
    yield
    repogit.clear_worktree_failure()


def _break_head_to_remote(base: str, branch: str = "main"):
    """把裸仓 HEAD 指到 refs/remotes/*（现场形态，必报 HEAD not found）。"""
    _git(["update-ref", f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"], base)
    _git(["symbolic-ref", "HEAD", f"refs/remotes/origin/{branch}"], base)


@pytest.fixture
def _unavailable_attribution(monkeypatch):
    """把凭据链钉成 worker 的「归因不可用」，避免用例触碰端口/DB。"""
    from src.kernel import token_resolver
    from src.kernel.token_resolver import (
        CREDENTIAL_ATTRIBUTION_UNAVAILABLE, TokenResult,
    )

    def _fake(repo_key):
        return TokenResult(token="", repo_key=repo_key,
                           attribution=CREDENTIAL_ATTRIBUTION_UNAVAILABLE,
                           attribution_detail="test: 端口未绑定")

    monkeypatch.setattr(token_resolver, "resolve_exec_token", _fake)
    monkeypatch.setattr(repogit, "repo_base_has_refs", lambda rk: True)


class TestBareHeadRepair:
    def test_valid_head_is_left_alone(self):
        base = repogit.ensure_repo_base(REPO)
        assert base
        before = repogit.bare_head_ref(base)
        out = repogit.ensure_bare_head(base, "main")
        assert out["ok"] and not out["repaired"]
        assert out["current"] == before == "refs/heads/main"

    def test_remote_tracking_head_is_repaired_to_heads(self):
        base = repogit.ensure_repo_base(REPO)
        _break_head_to_remote(base)
        assert repogit.bare_head_ref(base).startswith("refs/remotes/")
        out = repogit.ensure_bare_head(base, "main", repo_key=REPO, stage="test")
        assert out["ok"] and out["repaired"], out
        assert out["previous"].startswith("refs/remotes/")
        assert out["current"] == "refs/heads/main"
        assert out["branch"] == "main"
        assert repogit.bare_head_ref(base) == "refs/heads/main"

    def test_ensure_repo_base_repairs_broken_head(self):
        """建树入口（ensure_repo_base）在 fetch 后即做 HEAD 校验并自愈。"""
        base = repogit.ensure_repo_base(REPO)
        _break_head_to_remote(base)
        again = repogit.ensure_repo_base(REPO)
        assert again == base
        assert repogit.bare_head_ref(base) == "refs/heads/main", (
            "ensure_repo_base 必须在建树前把 HEAD 修回 refs/heads/*")

    def test_repair_falls_back_when_preferred_branch_missing(self):
        """指定基线不存在时，用任一可用分支修复（不因基线名而放弃自愈）。"""
        base = repogit.ensure_repo_base(REPO)
        _break_head_to_remote(base)
        out = repogit.ensure_bare_head(base, "no-such-baseline")
        assert out["ok"] and out["repaired"], out
        assert repogit.bare_head_ref(base).startswith("refs/heads/")

    def test_repair_reports_impossible_on_empty_repo(self, tmp_path, monkeypatch):
        """仓库没有任何分支时如实上报「修不了」，不伪造成功。"""
        empty = tmp_path / "empty.git"
        subprocess.run(["git", "init", "--bare", str(empty)], capture_output=True)
        out = repogit.ensure_bare_head(str(empty), "main")
        assert not out["ok"] and not out["repaired"]
        assert "HEAD" in out["detail"]


class TestWorktreeAddRetriesAfterHeadRepair:
    def test_worktree_add_success_after_head_self_heal(self):
        """HEAD 断裂 → worktree add 失败 → 自愈后重试一次并成功（验收标准 1）。"""
        base = repogit.ensure_repo_base(REPO)
        _break_head_to_remote(base)
        wt = repogit.ensure_issue_worktree(REPO, "coding", "3923")
        assert wt, "HEAD 自愈后建树必须成功"
        assert os.path.isdir(wt)
        assert repogit.bare_head_ref(base) == "refs/heads/main"
        head = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
        assert head.stdout.strip() == "feat/issue-3923"
        assert repogit.worktree_last_failure(REPO, issue_num="3923",
                                             agent_type="coding") == {}

    def test_retry_path_when_base_refresh_is_skipped(self, monkeypatch):
        """只读基线跳过 ensure_repo_base 刷新时，失败重试兜底仍能自愈建树。

        该路径覆盖「建树前校验没机会跑」的场景：HEAD 断在 fetch 之后才出现，
        只能在 `worktree add` 失败后自愈重试一次。
        """
        base = repogit.ensure_repo_base(REPO)
        _break_head_to_remote(base)
        monkeypatch.setattr(repogit, "ensure_repo_base",
                            lambda repo_key, token="", issue_num=None,
                            agent_type="": base)
        assert repogit.ensure_bare_head  # 自愈入口存在
        wt = repogit.ensure_issue_worktree(REPO, "coding", "3924")
        assert wt, "worktree add 失败后应先自愈 HEAD 再重试一次"
        assert repogit.bare_head_ref(base) == "refs/heads/main"
        head = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt)
        assert head.stdout.strip() == "feat/issue-3924"

    def test_non_head_failure_is_not_recorded_as_head_repair(self):
        """非 HEAD 类失败不做自愈（避免误改正常仓库），现场记为 worktree_add。"""
        base = repogit.ensure_repo_base(REPO)
        good = repogit.bare_head_ref(base)
        # 先构造一次真实的非 HEAD 失败：目标目录被外来文件占据 →
        # worktree add 必然 "already exists"，HEAD 本身完好 → 不触发自愈
        blocked = repogit.issue_dir_for(REPO, "coding", "3925")
        os.makedirs(blocked, exist_ok=True)
        with open(os.path.join(blocked, "stray.txt"), "w", encoding="utf-8") as fh:
            fh.write("foreign file")
        assert repogit.ensure_issue_worktree(REPO, "coding", "3925") is None
        scene = repogit.worktree_last_failure(REPO, issue_num="3925",
                                             agent_type="coding")
        assert scene, "非 HEAD 失败必须记失败现场"
        assert scene["stage"] == repogit.WORKTREE_STAGE_ADD, scene
        assert not scene["head_repair"].get("repaired"), (
            "非 HEAD 失败不得记成 head_repair 现场")
        assert repogit.bare_head_ref(base) == good, "正常 HEAD 不得被改动"
        err = repogit.worktree_failure_error(REPO, issue_num="3925",
                                             agent_type="coding")
        assert scene["stderr"] and "already exists" in scene["stderr"], (
            "git 原文必须透传")
        # 成功路径的复用不产生失败现场
        wt = repogit.ensure_issue_worktree(REPO, "coding", "3923")
        assert wt
        other = repogit.ensure_issue_worktree(REPO, "coding", "3923")
        assert other == wt, "同一 Issue 复用工作区"
        assert repogit.worktree_last_failure(REPO, issue_num="3923",
                                             agent_type="coding") == {}, (
            "非 HEAD 类失败不得被记成 head_repair 现场")


class TestAttributionTriState:
    """归因不可用 ≠ 没有凭据：worker（端口未绑）不得落 not_configured。"""

    @pytest.fixture(autouse=True)
    def _ports_baseline(self):
        snapshot = snapshot_ports()
        yield
        restore_ports(snapshot)

    def test_worker_without_assembly_reports_unavailable(self):
        """worker 没有装配层：resolve_exec_token 返回 unavailable，而不是「无凭据」。"""
        from src.kernel.ports._base import clear_bindings
        from src.kernel.token_resolver import (
            CREDENTIAL_ATTRIBUTION_UNAVAILABLE, resolve_exec_token,
        )

        clear_bindings()  # 模拟 worker 进程：端口全部未绑定
        sel = resolve_exec_token(RK)
        assert sel.token == ""
        assert sel.attribution == CREDENTIAL_ATTRIBUTION_UNAVAILABLE, sel
        assert "端口未绑定" in sel.attribution_detail

    def test_worker_git_failure_does_not_render_credential_not_configured(self):
        """worker 上任意非凭据类 git 失败只反映真实 git 错误（验收标准 2）。"""
        from src.kernel.ports._base import clear_bindings

        clear_bindings()
        base = repogit.ensure_repo_base(REPO)
        assert base  # 本地仓库可直接建基线（clone_url 指本地源仓，无需凭据）
        repogit._record_worktree_failure(
            RK, repogit.WORKTREE_STAGE_ADD,
            "fatal: HEAD not found below refs/heads!\n", issue_num="3923",
            agent_type="coding")
        err = repogit.worktree_failure_error(RK, issue_num="3923", agent_type="coding")
        assert "credential_not_configured" not in err, err
        assert "worktree_add" in err
        assert "HEAD not found below refs/heads" in err, "git 原文必须透传"
        assert "凭据归因不可用" in err, (
            "归因不可用与无凭据必须可区分（验收标准 4）")
        blk = agent_mod.worktree_block_result("t_3923", RK, issue_num="3923",
                                              agent_type="coding")
        assert blk and blk["_started"] is False
        assert "credential_not_configured" not in blk["_error"]

    def test_credential_none_still_reports_not_configured(self, monkeypatch):
        """反向回归：归因跑完且确实无凭据 → 仍报 credential_not_configured（验收标准 3）。"""
        install(monkeypatch, CredWorld(rc_value=None, host_user_id="15962"))
        monkeypatch.setattr(repogit, "repo_base_has_refs", lambda rk: True)
        err = repogit.worktree_credential_error(RK)
        assert err.startswith("credential_not_configured"), err
        assert "真因在凭据" not in err, "猜测性结论必须删除"

    def test_usable_credential_keeps_git_error_visible(self, monkeypatch):
        """凭据可用 → 不归因到凭据，git 原文照常可见。"""
        install(monkeypatch, CredWorld(rc_value="rc-bot-token", host_user_id="15962"))
        monkeypatch.setattr(repogit, "repo_base_has_refs", lambda rk: True)
        repogit._record_worktree_failure(
            RK, repogit.WORKTREE_STAGE_ADD, "fatal: ref conflict",
            issue_num="3923", agent_type="coding")
        err = repogit.worktree_failure_error(RK, issue_num="3923", agent_type="coding")
        assert err and "credential_" not in err
        assert "fatal: ref conflict" in err

    def test_failure_text_has_no_guesswork_conclusion(self):
        """错误文案只陈述事实与已执行动作（验收标准 4 / 期望改动 C）。"""
        import inspect
        src = inspect.getsource(repogit.worktree_credential_error)
        assert "真因在凭据" not in src
        src2 = inspect.getsource(repogit.worktree_failure_error)
        assert "真因在凭据" not in src2

    def test_block_resolves_exec_token_once(self, _unavailable_attribution, monkeypatch):
        """单次阻断只解析一次凭据链（审查 Optional 的验收：计数为 1）。"""
        from src.kernel import token_resolver
        from src.kernel.token_resolver import (
            CREDENTIAL_ATTRIBUTION_UNAVAILABLE, TokenResult,
        )

        calls = {"n": 0}

        def _counted(repo_key):
            calls["n"] += 1
            return TokenResult(token="", repo_key=repo_key,
                               attribution=CREDENTIAL_ATTRIBUTION_UNAVAILABLE,
                               attribution_detail="test: 端口未绑定")

        monkeypatch.setattr(token_resolver, "resolve_exec_token", _counted)
        repogit._record_worktree_failure(
            RK, repogit.WORKTREE_STAGE_ADD, "fatal: ref conflict",
            issue_num="3923", agent_type="coding")
        blk = agent_mod.worktree_block_result("t_3923", RK, issue_num="3923",
                                              agent_type="coding")
        assert blk and blk["_started"] is False
        assert calls["n"] == 1, calls


class TestFailureSceneLifecycle:
    def test_ensure_bare_head_clears_stale_failure_after_success(self):
        base = repogit.ensure_repo_base(REPO)
        # 只清本 (repo, issue, agent) 的现场（并发任务互不干扰）
        repogit._record_worktree_failure(RK, repogit.WORKTREE_STAGE_ADD, "old error",
                                         issue_num="3923", agent_type="coding")
        assert repogit.worktree_last_failure(RK, issue_num="3923", agent_type="coding")
        wt = repogit.ensure_issue_worktree(REPO, "coding", "3923")
        assert wt
        assert repogit.worktree_last_failure(RK, issue_num="3923",
                                             agent_type="coding") == {}, (
            "建树成功必须清掉旧失败现场")
        assert repogit.bare_head_ref(base) == "refs/heads/main"

    def test_other_task_failure_scene_is_preserved(self):
        """清现场只作用于本三元组：别的任务的失败现场不被静默抹掉。"""
        base = repogit.ensure_repo_base(REPO)
        repogit._record_worktree_failure(RK, repogit.WORKTREE_STAGE_ADD, "other task",
                                         issue_num="9000", agent_type="qa")
        wt = repogit.ensure_issue_worktree(REPO, "coding", "3923")
        assert wt
        assert repogit.worktree_last_failure(RK, issue_num="9000", agent_type="qa"), (
            "别的任务的失败现场必须保留，供其错误面透传")
        assert repogit.bare_head_ref(base) == "refs/heads/main"

    def test_repo_base_failure_is_visible_to_every_concurrent_task(
            self, _unavailable_attribution, monkeypatch):
        """同仓两个任务先后 base 失败：各自现场按三元组键写入，互不覆盖。

        Issue #3923 复审 Optional：issue/agent 下传 ensure_repo_base 后，失败
        直接写 (repo, issue, agent) 键——每个任务读到的是自己的 git 原文。
        """
        def _fail_base(repo_key, token="", issue_num=None, agent_type=""):
            repogit._record_worktree_failure(
                repo_key, repogit.WORKTREE_STAGE_BASE,
                f"fatal: task {issue_num} fetch failed: Could not resolve host",
                issue_num=issue_num, agent_type=agent_type)
            return None

        monkeypatch.setattr(repogit, "ensure_repo_base", _fail_base)

        assert repogit.ensure_issue_worktree(REPO, "coding", "3923") is None
        assert repogit.ensure_issue_worktree(REPO, "coding", "3924") is None

        for issue in ("3923", "3924"):
            err = repogit.worktree_failure_error(REPO, issue_num=issue,
                                                 agent_type="coding")
            assert err, f"任务 {issue} 必须读到 base 失败现场"
            assert f"task {issue} fetch failed" in err, (
                f"任务 {issue} 读到的必须是自己的 git 原文: {err}")
            assert "基线 fetch/clone 失败" in err, err
            assert "git worktree add 失败" not in err, err
            blk = agent_mod.worktree_block_result(f"t_{issue}", REPO,
                                                  issue_num=issue,
                                                  agent_type="coding")
            assert blk and blk["_started"] is False, (issue, blk)

    def test_base_init_failure_records_scene_and_blocks(
            self, _unavailable_attribution, tmp_path, monkeypatch):
        """git init --bare 失败也必须写失败现场并阻断（复审 Required）。

        现场形态：base 路径被普通文件占据 → init returncode != 0。
        旧码此路径直接 return None 不记录 → worktree_failure_error 为空 →
        任务在无工作区状态下继续执行。
        """
        base_as_file = tmp_path / "blocked.git"
        base_as_file.write_text("not a dir", encoding="utf-8")
        monkeypatch.setattr(repogit, "repo_base_dir", lambda rk: str(base_as_file))

        assert repogit.ensure_issue_worktree(REPO, "coding", "3923") is None
        fail = repogit.worktree_last_failure(REPO, issue_num="3923",
                                             agent_type="coding")
        assert fail, "init 失败必须记录失败现场"
        assert fail["stage"] == repogit.WORKTREE_STAGE_BASE, fail
        assert fail["stderr"], "init 的 git 原文必须保留"
        err = repogit.worktree_failure_error(REPO, issue_num="3923",
                                             agent_type="coding")
        assert err and "基线 fetch/clone 失败" in err, err
        blk = agent_mod.worktree_block_result("t_3923", REPO, issue_num="3923",
                                              agent_type="coding")
        assert blk and blk["_started"] is False, blk

    def test_base_exception_failure_records_scene_and_blocks(
            self, _unavailable_attribution, tmp_path, monkeypatch):
        """ensure_repo_base 异常兜底路径同样必须写失败现场并阻断（复审 Required）。

        现场形态：base 的父路径是普通文件 → makedirs 抛异常 → except 兜底。
        """
        parent_as_file = tmp_path / "parent-is-file"
        parent_as_file.write_text("not a dir", encoding="utf-8")
        monkeypatch.setattr(repogit, "repo_base_dir",
                            lambda rk: str(parent_as_file / "x.git"))

        assert repogit.ensure_issue_worktree(REPO, "coding", "3923") is None
        fail = repogit.worktree_last_failure(REPO, issue_num="3923",
                                             agent_type="coding")
        assert fail, "异常兜底必须记录失败现场"
        assert fail["stage"] == repogit.WORKTREE_STAGE_BASE, fail
        assert "ensure_repo_base exception" in fail["stderr"], fail
        err = repogit.worktree_failure_error(REPO, issue_num="3923",
                                             agent_type="coding")
        assert err, "worktree_failure_error 不得为空"
        blk = agent_mod.worktree_block_result("t_3923", REPO, issue_num="3923",
                                              agent_type="coding")
        assert blk and blk["_started"] is False, blk

    def test_failure_scene_has_stage_specific_wording(self, _unavailable_attribution):
        """阶段必须按事实映射措辞，不能把 repo_base 也写成 worktree add 失败。"""
        cases = [
            (repogit.WORKTREE_STAGE_BASE, "基线 fetch/clone 失败",
             "git worktree add 失败"),
            (repogit.WORKTREE_STAGE_ADD, "git worktree add 失败", None),
            (repogit.WORKTREE_STAGE_HEAD_REPAIR, "HEAD 自愈后重试仍失败",
             "git worktree add 失败"),
        ]
        for stage, expected, unexpected in cases:
            repogit.clear_worktree_failure()
            repogit._record_worktree_failure(
                RK, stage, "fatal: demo error", issue_num="3923",
                agent_type="coding")
            err = repogit.worktree_failure_error(RK, issue_num="3923",
                                                 agent_type="coding")
            assert expected in err, (stage, err)
            if unexpected:
                assert unexpected not in err, (stage, err)

    def test_stale_repo_level_failure_expires(self, _unavailable_attribution):
        """仓级兜底现场按 TTL 收敛，旧记录不得被当成当前失败原因。"""
        repogit._LAST_WORKTREE_FAILURE[RK] = {
            "stage": repogit.WORKTREE_STAGE_BASE,
            "stderr": "fatal: stale",
            "at": time.time() - repogit._LAST_WORKTREE_FAILURE_REPO_TTL - 1,
        }
        assert repogit.worktree_last_failure(RK, issue_num="3923",
                                             agent_type="coding") == {}
