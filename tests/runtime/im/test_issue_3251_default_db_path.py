"""Issue #3251 回归：裸启动默认 DB 路径口径一致 + DB 打开失败日志一次性化。

Seams（公开接口测试，不测内部拼接细节）：
- A: src.kernel.session_manager.default_db_path()（新导出）与
     src.domains.runtime.im.engine_wiring.default_db_path()（既有 __all__ 导出）
     在裸启动（不设 DEVOPS_DB_PATH）下解析到同一可打开的 DB。
- B: DEVOPS_DB_PATH 已设置时两处口径都返回 env 值（既有行为不变）。
- C: engine_wiring._connect() 对不可打开路径仅报一次 ERROR（含解析路径与 env 状态）。
- D: OutboxConsumer.run() 对已一次性报告的 DB 打开错误不再逐轮 WARNING。
"""
import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
sys.path.insert(0, ROOT)

EW_LOGGER = "src.domains.runtime.im.engine_wiring"
OUTBOX_LOGGER = "src.domains.runtime.im.outbox"


def test_bare_start_im_path_equals_session_manager(monkeypatch):
    """Seam A：裸启动时 IM 装配层与主库解析到同一可打开的 DB。"""
    monkeypatch.delenv("DEVOPS_DB_PATH", raising=False)
    from src.domains.runtime.im import engine_wiring
    from src.kernel import session_manager

    im_path = engine_wiring.default_db_path()
    sm_path = session_manager.default_db_path()

    assert im_path == sm_path
    expected = Path(session_manager.__file__).resolve().parents[2] / "data" / "sessions.db"
    assert Path(im_path) == expected
    assert Path(im_path).parent.is_dir()


def test_env_devops_db_path_takes_precedence(monkeypatch, tmp_path):
    """Seam B：DEVOPS_DB_PATH 已设置时两处口径都取 env 值。"""
    env_db = str(tmp_path / "env_sessions.db")
    monkeypatch.setenv("DEVOPS_DB_PATH", env_db)
    from src.domains.runtime.im import engine_wiring
    from src.kernel import session_manager

    assert engine_wiring.default_db_path() == env_db
    assert session_manager.default_db_path() == env_db


def test_connect_open_failure_logs_single_error(monkeypatch, tmp_path, caplog):
    """Seam C：回退路径不可打开——连续多次连接失败仅 1 条 ERROR（含路径与 env 状态）。"""
    monkeypatch.delenv("DEVOPS_DB_PATH", raising=False)
    blocker = tmp_path / "blocker"
    blocker.write_text("parent is a file, not a dir")
    bad_db = str(blocker / "sessions.db")

    from src.domains.runtime.im import engine_wiring

    monkeypatch.setattr(engine_wiring, "_DB_OPEN_ERROR_LOGGED", set())
    caplog.set_level(logging.DEBUG, logger=EW_LOGGER)

    for _ in range(5):
        with pytest.raises(sqlite3.OperationalError):
            engine_wiring._connect(bad_db)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert bad_db in msg
    assert "not set" in msg


def test_outbox_round_db_open_failure_not_spammed(monkeypatch, caplog):
    """Seam D：outbox 消费轮遇已一次性报告的 DB 打开错误 → 不再逐轮 WARNING 刷屏。"""
    from src.domains.runtime.im import outbox as ob

    err = sqlite3.OperationalError("unable to open database file")
    err._db_open_logged = True

    consumer = ob.OutboxConsumer(engine=None, poll_interval=0.01)

    async def _run_once():
        raise err

    monkeypatch.setattr(consumer, "run_once", _run_once)

    async def _drive():
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.06)
        await consumer.stop()
        await task

    caplog.set_level(logging.DEBUG, logger=OUTBOX_LOGGER)
    asyncio.run(_drive())
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
