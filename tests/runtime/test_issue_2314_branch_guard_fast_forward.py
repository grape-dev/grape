"""Issue #2314：pre-push 分支守卫不得用过期租约把普通 fast-forward 误判为 remote_drift。

现场（用户反馈 5585，任务 `t_ecad294099b4` / `seq=70358`）：远端 head `b22b43a6` 是
本地 HEAD 的祖先，租约期望却停在历史提交 `8868f61a`，守卫以「等值比较即漂移」阻断，
只能 `--no-verify` 旁路。

覆盖四条验收回归 + 两个衍生缺口：
- ① fast-forward 放行（远端是本地待推提交的祖先）；
- ② 真分叉（无祖先关系）仍阻断，行为不回归；
- ③ pending 未落地（push 失败）不误伤下一次合法 push；
- ④ 钩子版本失配可检出并幂等重装（沿用 #2028 实现，只补覆盖）；
- ⑤ 陈旧 expected 先验自愈推进；首次 push 以实时远端 head 初始化期望；
- ⑥ remote_drift 返回体/日志含 expected_updated_at / pending_expected_sha / pending_prev_sha，
  且 merge-base 无法判定时 fail-closed。
"""
import io
import os
import subprocess

import pytest

from src.domains.runtime.engine import branch_guard as bg

REPO_KEY = "gitcode:owner/repo"
BRANCH = "feat/issue-2314"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from src.kernel.session_manager import db
    try:
        db.execute("DELETE FROM distributed_locks WHERE lock_key LIKE 'branch_guard:%'")
        db.execute("DELETE FROM branch_guard_state")
        db.commit()
    except Exception:
        pass
    # 钩子用 cwd 作对象库目录；默认切到空目录，避免误用仓库自身的历史。
    empty = tmp_path / "cwd-empty"
    empty.mkdir(exist_ok=True)
    monkeypatch.chdir(empty)
    yield
    try:
        db.execute("DELETE FROM distributed_locks WHERE lock_key LIKE 'branch_guard:%'")
        db.execute("DELETE FROM branch_guard_state")
        db.commit()
    except Exception:
        pass


# ===================== git 夹具 =====================

def _git(*args, cwd=None):
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True, env=env)


def _rev(repo, ref="HEAD"):
    return _git("rev-parse", ref, cwd=repo).stdout.strip()


def _linear_history(tmp_path, name="repo", count=3):
    """构造线性历史，返回 (repo, [sha0, sha1, ...])（sha[i] 是 sha[i+1] 的祖先）。"""
    repo = tmp_path / name
    repo.mkdir()
    _git("init", cwd=repo)
    _git("checkout", "-b", "main", cwd=repo)
    shas = []
    for i in range(count):
        (repo / f"f{i}.txt").write_text(str(i))
        _git("add", "-A", cwd=repo)
        _git("commit", "-m", f"c{i}", cwd=repo)
        shas.append(_rev(repo))
    return repo, shas


def _diverged_history(tmp_path, name="repo"):
    """构造真分叉：base ← side（与 main 无祖先关系），返回 (repo, {shas})."""
    repo, shas = _linear_history(tmp_path, name=name, count=3)
    _git("checkout", "-b", "side", shas[0], cwd=repo)
    (repo / "side.txt").write_text("side")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "side", cwd=repo)
    shas.append(_rev(repo))  # shas[3] = side head，与 shas[1], shas[2] 无祖先关系
    _git("checkout", "main", cwd=repo)
    return repo, shas


def _ref_line(branch, local, remote):
    return f"refs/heads/{branch} {local} refs/heads/{branch} {remote}\n"


# ===================== ① fast-forward 放行 =====================

def test_fast_forward_allowed_when_remote_is_ancestor_of_local(tmp_path):
    """现场复现：远端 head 只是本地待推提交的祖先 → 放行，不再 remote_drift。"""
    repo, shas = _linear_history(tmp_path)
    verdict = bg.check_push_allowed(
        REPO_KEY, BRANCH, remote_sha=shas[1], expected_remote_sha=shas[0],
        local_sha=shas[2], repo_dir=str(repo))
    assert verdict["allowed"] is True
    assert verdict["reason"] == "fast_forward"
    assert verdict["refresh_expected"] == shas[1]
    assert verdict["ancestor_basis"] == "remote_is_ancestor_of_local"


def test_fast_forward_allowed_when_local_sha_unknown(tmp_path):
    """判据 A 兜底：服务端无 local sha 时，expected 是远端祖先 → 仅租约落后，放行。"""
    repo, shas = _linear_history(tmp_path)
    verdict = bg.check_push_allowed(
        REPO_KEY, BRANCH, remote_sha=shas[2], expected_remote_sha=shas[0],
        repo_dir=str(repo))
    assert verdict["allowed"] is True
    assert verdict["reason"] == "fast_forward"
    assert verdict["refresh_expected"] == shas[2]
    assert verdict["ancestor_basis"] == "expected_is_ancestor_of_remote"


def test_equal_expected_and_remote_still_fast_path_ok(tmp_path):
    repo, shas = _linear_history(tmp_path)
    verdict = bg.check_push_allowed(
        REPO_KEY, BRANCH, remote_sha=shas[2], expected_remote_sha=shas[2],
        local_sha=shas[2], repo_dir=str(repo))
    assert verdict["allowed"] is True and verdict["reason"] == "ok"


# ===================== ② 真分叉阻断（不回归） =====================

def test_true_divergence_still_blocks(tmp_path):
    repo, shas = _diverged_history(tmp_path)
    # remote = side head，local = main head，两者无祖先关系 → 真分叉
    verdict = bg.check_push_allowed(
        REPO_KEY, BRANCH, remote_sha=shas[3], expected_remote_sha=shas[0],
        local_sha=shas[2], repo_dir=str(repo))
    assert verdict["allowed"] is False
    assert verdict["reason"] == "remote_drift"
    assert verdict["drift_relation"] == "unrelated"
    assert "fetch/rebase" in verdict["message"]


def test_local_behind_remote_blocks(tmp_path):
    """本地落后于远端（非 force 无法推送）不得被误判成 fast-forward 放行。"""
    repo, shas = _linear_history(tmp_path)
    verdict = bg.check_push_allowed(
        REPO_KEY, BRANCH, remote_sha=shas[2], expected_remote_sha=shas[0],
        local_sha=shas[0], repo_dir=str(repo))
    assert verdict["allowed"] is False and verdict["reason"] == "remote_drift"


def test_merge_base_undetermined_is_fail_closed():
    """无法判定祖先关系（无对象库 / 对象不可达）→ 按漂移阻断，不放行。"""
    verdict = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="b" * 40,
                                    expected_remote_sha="a" * 40, local_sha="c" * 40)
    assert verdict["allowed"] is False
    assert verdict["reason"] == "remote_drift"
    assert verdict["drift_relation"] == "undetermined"
    assert "按漂移阻断" in verdict["message"]


def test_merge_base_error_on_unknown_object_is_fail_closed(tmp_path):
    repo, _shas = _linear_history(tmp_path)
    verdict = bg.check_push_allowed(
        REPO_KEY, BRANCH, remote_sha="b" * 40, expected_remote_sha="a" * 40,
        local_sha="c" * 40, repo_dir=str(repo))
    assert verdict["allowed"] is False and verdict["drift_relation"] == "undetermined"


# ===================== ⑤ 陈旧 expected 先验自愈 =====================

def test_reconcile_pending_advances_stale_expected_when_remote_moved(tmp_path):
    """上次 push 已落地、远端前进：reconcile 把陈旧 expected 推进到远端当前 head。"""
    repo, shas = _linear_history(tmp_path)
    bg.save_lease_state(REPO_KEY, BRANCH, expected_remote_sha=shas[0])
    got = bg.reconcile_pending(REPO_KEY, BRANCH, shas[2], repo_dir=str(repo))
    assert got == shas[2]
    state = bg.load_lease_state(REPO_KEY, BRANCH)
    assert state["expected_remote_sha"] == shas[2]
    assert state["pending_expected_sha"] == "" and state["pending_prev_sha"] == ""


def test_reconcile_pending_keeps_expected_on_true_divergence(tmp_path):
    """非祖先关系不得推进——交由判定层 fail-closed 阻断。"""
    repo, shas = _diverged_history(tmp_path)
    bg.save_lease_state(REPO_KEY, BRANCH, expected_remote_sha=shas[3])
    got = bg.reconcile_pending(REPO_KEY, BRANCH, shas[2], repo_dir=str(repo))
    assert got == shas[3]
    assert bg.load_lease_state(REPO_KEY, BRANCH)["expected_remote_sha"] == shas[3]


def test_reconcile_pending_without_repo_dir_keeps_expected(tmp_path):
    """无法判定（无对象库）时不推进，保持旧值 → 后续判定 fail-closed 阻断。"""
    repo, shas = _linear_history(tmp_path)
    bg.save_lease_state(REPO_KEY, BRANCH, expected_remote_sha=shas[0])
    assert bg.reconcile_pending(REPO_KEY, BRANCH, shas[2]) == shas[0]


# ===================== ③ pending 未落地不误伤 =====================

def test_pending_not_landed_does_not_false_drift(tmp_path):
    """push 失败（远端仍停在旧值）→ 丢弃候选，下一次合法 push 放行。"""
    repo, shas = _linear_history(tmp_path)
    bg.save_lease_state(REPO_KEY, BRANCH, expected_remote_sha=shas[0])
    bg.record_pending_push(REPO_KEY, BRANCH, shas[2], prev_remote_sha=shas[0])
    expected = bg.reconcile_pending(REPO_KEY, BRANCH, shas[0], repo_dir=str(repo))
    assert expected == shas[0]
    verdict = bg.check_push_allowed(
        REPO_KEY, BRANCH, remote_sha=shas[0], expected_remote_sha=expected,
        local_sha=shas[2], repo_dir=str(repo))
    assert verdict["allowed"] is True
    assert bg.load_lease_state(REPO_KEY, BRANCH)["pending_expected_sha"] == ""


def test_pending_not_landed_allows_next_push_with_advanced_local(tmp_path):
    """候选未落地且本地继续前进：仍按 fast-forward 放行，不被判漂移。"""
    repo, shas = _linear_history(tmp_path)
    bg.save_lease_state(REPO_KEY, BRANCH, expected_remote_sha=shas[0])
    bg.record_pending_push(REPO_KEY, BRANCH, shas[1], prev_remote_sha=shas[0])
    expected = bg.reconcile_pending(REPO_KEY, BRANCH, shas[0], repo_dir=str(repo))
    verdict = bg.check_push_allowed(
        REPO_KEY, BRANCH, remote_sha=shas[0], expected_remote_sha=expected,
        local_sha=shas[2], repo_dir=str(repo))
    assert verdict["allowed"] is True and verdict["reason"] == "ok"


# ===================== ⑥ 漂移日志定位字段 =====================

def test_remote_drift_verdict_carries_lease_locator_fields(tmp_path):
    _repo, shas = _diverged_history(tmp_path)
    bg.save_lease_state(REPO_KEY, BRANCH, expected_remote_sha=shas[3],
                        pending_expected_sha="p" * 40, pending_prev_sha="q" * 40)
    verdict = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha=shas[2],
                                    local_sha=shas[1], repo_dir=str(tmp_path / "repo"))
    assert verdict["allowed"] is False and verdict["reason"] == "remote_drift"
    state = bg.load_lease_state(REPO_KEY, BRANCH)
    assert verdict["expected_updated_at"] == state["updated_at"]
    assert verdict["expected_updated_at"]
    assert verdict["pending_expected_sha"] == "p" * 40
    assert verdict["pending_prev_sha"] == "q" * 40
    fields = bg._drift_log_fields(verdict)
    assert "expected_updated_at=" in fields
    assert f"pending_expected_sha={('p' * 40)[:12]}" in fields
    assert f"pending_prev_sha={('q' * 40)[:12]}" in fields


# ===================== 钩子级端到端 =====================

def _patch_pr_state(monkeypatch, state="open"):
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: {"state": state})


def test_hook_first_push_initializes_expected_from_realtime_remote(tmp_path, monkeypatch):
    """首次无状态：以推送时实时远端 head 初始化 expected，不沿用历史 sha。"""
    repo, shas = _linear_history(tmp_path)
    monkeypatch.chdir(repo)
    _patch_pr_state(monkeypatch)
    monkeypatch.setattr(bg.sys, "stdin",
                        io.StringIO(_ref_line(BRANCH, shas[2], shas[1])))
    assert bg.hook_main([REPO_KEY]) == 0
    state = bg.load_lease_state(REPO_KEY, BRANCH)
    assert state["expected_remote_sha"] == shas[1]
    assert state["pending_expected_sha"] == shas[2]
    assert state["pending_prev_sha"] == shas[1]


def test_hook_stale_expected_self_heals_and_allows(tmp_path, monkeypatch, capsys):
    """陈旧 expected 是远端祖先 → 钩子内先验收敛，本次 fast-forward 放行。"""
    repo, shas = _linear_history(tmp_path)
    monkeypatch.chdir(repo)
    _patch_pr_state(monkeypatch)
    bg.save_lease_state(REPO_KEY, BRANCH, expected_remote_sha=shas[0])
    monkeypatch.setattr(bg.sys, "stdin",
                        io.StringIO(_ref_line(BRANCH, shas[2], shas[1])))
    assert bg.hook_main([REPO_KEY]) == 0
    err = capsys.readouterr().err
    assert "remote_drift" not in err
    state = bg.load_lease_state(REPO_KEY, BRANCH)
    assert state["expected_remote_sha"] == shas[1]
    assert state["pending_expected_sha"] == shas[2]


def test_hook_remote_ancestor_allows_even_with_unrelated_expected(tmp_path, monkeypatch, capsys):
    """判据 B 现场：expected 与远端无祖先关系，但远端是本地祖先 → 仍按 fast-forward 放行。"""
    repo, shas = _diverged_history(tmp_path)
    monkeypatch.chdir(repo)
    _patch_pr_state(monkeypatch)
    bg.save_lease_state(REPO_KEY, BRANCH, expected_remote_sha=shas[3])  # side head
    monkeypatch.setattr(bg.sys, "stdin",
                        io.StringIO(_ref_line(BRANCH, shas[2], shas[1])))
    assert bg.hook_main([REPO_KEY]) == 0
    assert "remote_drift" not in capsys.readouterr().err
    assert bg.load_lease_state(REPO_KEY, BRANCH)["expected_remote_sha"] == shas[1]


def test_hook_true_divergence_blocks_with_locator_fields(tmp_path, monkeypatch, capsys):
    """真分叉仍阻断，且日志带 expected_updated_at / pending / prev 定位字段。"""
    repo, shas = _diverged_history(tmp_path)
    monkeypatch.chdir(repo)
    _patch_pr_state(monkeypatch)
    bg.save_lease_state(REPO_KEY, BRANCH, expected_remote_sha=shas[3],
                        pending_expected_sha="p" * 40, pending_prev_sha="q" * 40)
    logged = []
    monkeypatch.setattr(bg.log, "warning", lambda m, *a, **k: logged.append(m))
    monkeypatch.setattr(bg.sys, "stdin",
                        io.StringIO(_ref_line(BRANCH, shas[2], shas[3])))
    assert bg.hook_main([REPO_KEY]) == 1
    err = capsys.readouterr().err
    assert "[branch-guard]" in err and "已漂移" in err
    assert logged and "remote_drift" in logged[0]
    assert "expected_updated_at=" in logged[0]
    assert "pending_expected_sha=" in logged[0]
    assert "pending_prev_sha=" in logged[0]


def test_hook_pending_candidate_not_reused_as_expected(tmp_path, monkeypatch):
    """已有 pending 候选时钩子不改写 expected（交由 reconcile 收敛，避免覆盖候选）。"""
    repo, shas = _linear_history(tmp_path)
    monkeypatch.chdir(repo)
    _patch_pr_state(monkeypatch)
    bg.save_lease_state(REPO_KEY, BRANCH, expected_remote_sha=shas[0],
                        pending_expected_sha=shas[1], pending_prev_sha=shas[0])
    assert bg._initial_expected(REPO_KEY, BRANCH, shas[1]) is None
    assert bg._initial_expected(REPO_KEY, BRANCH, "") is None


# ===================== ④ 钩子版本失配 / 幂等重装 =====================

def test_hook_template_version_is_registered():
    """#2028 治理已在目标分支落地：版本指纹与重装能力必须保持。"""
    assert bg.HOOK_TEMPLATE_VERSION
    assert bg.HOOK_VERSION_LINE == f"# hook-template-version: {bg.HOOK_TEMPLATE_VERSION}"
    assert "reconcile_pending" in open(bg._hook_module_path(), encoding="utf-8").read()


def test_older_template_version_triggers_reinstall(tmp_path):
    """版本失配（旧版本行命中 marker）→ 幂等重装覆盖为当前模板。"""
    repo = tmp_path / "ver-repo"
    repo.mkdir()
    _git("init", cwd=repo)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    stale = hooks / "pre-push"
    stale.write_text("#!/bin/sh\n" + bg.HOOK_MARKER + "\n"
                     "# hook-template-version: 2028-03\n"
                     "echo stale-version\n", encoding="utf-8")

    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    content = stale.read_text(encoding="utf-8")
    assert bg.HOOK_VERSION_LINE in content
    assert "2028-03" not in content
    assert "echo stale-version" not in content
    assert bg.HOOK_MARKER in content

    # 幂等：当前版本再装一次内容不变
    before = stale.read_text(encoding="utf-8")
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    assert stale.read_text(encoding="utf-8") == before


def test_version_line_present_in_fresh_install(tmp_path):
    repo = tmp_path / "fresh-repo"
    repo.mkdir()
    _git("init", cwd=repo)
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    content = (repo / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
    assert bg.HOOK_VERSION_LINE in content
    assert bg._existing_hook_usable(content, REPO_KEY) is True