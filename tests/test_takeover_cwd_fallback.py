"""接管 bundle 的 cwd 兜底回归测试。

背景：`agent_tasks.cwd` 只有部分入队路径会回填；走 claim 的任务常常是空字符串
（worker 执行时自己按 get_agent_workdir 算路径，但不回写库）。接管要靠这个路径
建本地工作区，空值会让接管直接失败（实测本地环境就是这种情况）。

修法：`runtime.takeover.build_bundle()` 在原值为空时按同一规则推导
（旧 `takeover-context` 端点已随接管重构下线，本文件改测新入口）。
"""
import os
from datetime import datetime, timezone

import pytest

from src.domains.runtime.pipeline.event_pipeline import get_agent_workdir
from src.domains.runtime.engine import takeover as svc
from src.kernel.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _bind_db():
    """每次用例前重新绑定「被测代码实际使用的」连接。

    全树收集（pytest -k / 全量）时，其它测试模块会在导入期改 DEVOPS_DB_PATH 甚至
    reload session_manager，模块级 `from ... import db` 会变成陈旧句柄 ——
    种子写进旧库、被测代码读新库，表现为"表里查不到"。绑定到 takeover 模块的
    globals 可以保证种子与读取指向同一个连接。
    """
    global db, db_lock
    from src.domains.runtime.engine import takeover as _svc
    db, db_lock = _svc.db, _svc.db_lock

REPO = "gitcode:testtakeovercwd/repo"
ISSUE = "#12"


@pytest.fixture(autouse=True)
def _clean():
    def wipe():
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:testtakeovercwd/%'")
            c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:testtakeovercwd/%'")
            c.execute("DELETE FROM issue WHERE repo_key LIKE 'gitcode:testtakeovercwd/%'")
            db.commit()

    wipe()
    yield
    wipe()


def _seed(task_id="t_cwd_1", cwd="", status="running", agent_type="issue"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, skill, prompt, status, cwd, created_at) "
            "VALUES (?, ?, 1, 0, ?, ?, ?, 'issue-cli', 'p', ?, ?, ?)",
            (task_id, f"sess-{task_id}", ISSUE, REPO, agent_type, status, cwd,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()


def _bundle(task_id="t_cwd_1"):
    task = svc.get_task(task_id)
    assert task, "seed 失败"
    bundle, _warnings, _persist = svc.build_bundle(
        task=task, session_id="tkos_cwd_test", token="tko_dummy",
        token_expires_at=0, user={"id": 1, "username": "tester"},
    )
    return bundle


class TestCwdFallback:
    def test_empty_cwd_is_derived(self):
        """cwd 为空时按 get_agent_workdir 推导，接管才拿得到工作区路径。"""
        _seed(cwd="")
        cwd = _bundle()["workspace"]["cwd_hint"]
        assert cwd == get_agent_workdir(REPO, "issue", ISSUE)
        assert cwd.endswith("/issues/12/issue")
        assert os.path.isabs(cwd)

    def test_existing_cwd_is_preserved(self):
        """已有 cwd 时不能被推导值覆盖。"""
        _seed(cwd="/tmp/some-explicit-workdir")
        assert _bundle()["workspace"]["cwd_hint"] == "/tmp/some-explicit-workdir"

    def test_derived_cwd_matches_worker_layout(self):
        """推导路径必须和 worker 实际创建的目录同构（provider/owner/repo/issues/N/agent）。"""
        _seed(cwd="", agent_type="vod")
        cwd = _bundle()["workspace"]["cwd_hint"]
        assert cwd == get_agent_workdir(REPO, "vod", ISSUE)
        assert "/gitcode/testtakeovercwd/repo/issues/12/vod" in cwd

    def test_missing_task_still_404(self):
        """没有 running 任务时仍是 404，兜底不能把 404 变成假成功。"""
        with pytest.raises(svc.TakeoverError) as ei:
            svc.start_takeover(repo_key=REPO, issue_number="9999", user={"id": 1, "username": "tester"})
        assert ei.value.status == 404
        assert ei.value.code == "no_running_task"