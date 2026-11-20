"""沙箱 DB 查询 DAO 层——集中 SQL 和事务逻辑。

manager.py 不直接 import session_manager，通过 SandboxRepository 委托 DB 访问。
每个方法独立 with db_lock + db.commit()，不跨方法持有 session。

Repository 只负责 DB 访问，不放业务判断。
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)


class SandboxRepository:
    """沙箱相关 DB 查询——集中 SQL 和事务逻辑。

    每个方法内部 with db_lock + db.commit()，不跨方法持有 session。
    测试时可注入 mock session_factory 替换 DB 连接。
    """

    def __init__(self, session_factory=None):
        """Args:
            session_factory: 可选，返回 (db, db_lock) 元组的 callable。
                默认从 src.kernel.session_manager import db, db_lock。
        """
        self._session_factory = session_factory

    def _get_session(self):
        """返回 (db, db_lock)。延迟 import 避免循环。"""
        if self._session_factory:
            return self._session_factory()
        from src.kernel.session_manager import db, db_lock
        return db, db_lock

    def query_task_context(self, task_id: str) -> tuple[str, str, int]:
        """查 agent_tasks 拿 issue_id/repo_key/user_id。

        Returns:
            (issue_id, repo_key, user_id) — 查不到返回 ("", "", 0)
        """
        if not task_id:
            return "", "", 0
        try:
            db, db_lock = self._get_session()
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT issue_id, repo_key, user_id FROM agent_tasks WHERE id=?",
                    (task_id,),
                )
                row = c.fetchone()
            if row:
                return row[0] or "", row[1] or "", row[2] or 0
            log.warning(f"repository: task {task_id} not found in agent_tasks")
        except Exception as e:
            log.warning(f"repository: query_task_context failed: {e}")
            raise
        return "", "", 0

    def query_llm_env(self, user_id: int, engine_type: str = "") -> dict[str, str]:
        """查 grape_t_user_llm_credentials 拿 LLM 凭据。两级 fallback: 引擎专属 → 通用兜底。

        Args:
            user_id: 用户 ID
            engine_type: 引擎类型(hermes/opencode)，空串走通用兜底

        Returns:
            _LLM_* env dict — 查不到返回空 dict
        """
        if not user_id:
            return {}
        try:
            db, db_lock = self._get_session()
            from src.domains.runtime.agents.sandbox.llm_cred_selector import select_llm_cred
            with db_lock:
                c = db.cursor()
                row = select_llm_cred(c, user_id, engine_type)
            if row:
                return {
                    "_LLM_PROVIDER": row[0],
                    "_LLM_API_KEY": row[1],
                    "_LLM_BASE_URL": row[2],
                    "_LLM_MODEL": row[3],
                }
        except Exception as e:
            log.warning(f"repository: query_llm_env failed: {e}")
        return {}

    def query_sandbox_config(self, runtime_id: int, engine_name: str) -> tuple[str, str]:
        """查 grape_t_engine_sandbox_config 拿 sandbox_mode + degrade_policy。

        Returns:
            (mode_str, degrade_str) — 查不到返回 ("", "")
        """
        try:
            # 表归 mgmt：SQL 在 mgmt public 一份
            from src.domains.mgmt import public as _mg_mode
            row = _mg_mode.engine_sandbox_mode(runtime_id, engine_name)
            if row:
                return row[0] or "", row[1] or ""
        except Exception as e:
            log.warning(f"repository: query_sandbox_config failed: {e}")
        return "", ""

    def query_runtime_info(self, runtime_id: int) -> dict:
        """查 agent_runtimes.runtime_info JSON 解析为 dict。

        Returns:
            runtime_info dict — 查不到返回空 dict
        """
        try:
            import json as _json
            db, db_lock = self._get_session()
            with db_lock:
                c = db.cursor()
                c.execute("SELECT runtime_info FROM agent_runtimes WHERE id=?", (runtime_id,))
                row = c.fetchone()
            if row and row[0]:
                return _json.loads(row[0])
        except Exception as e:
            log.warning(f"repository: query_runtime_info failed: {e}")
        return {}

    def query_sandbox_template(self, engine_name: str, runtime_id: int | None = None) -> tuple[str, str]:
        """查沙箱模板——先查 grape_t_sandbox_templates 表，再 fallback grape_t_engine_sandbox_config。

        Returns:
            (template_id, provider_name) — 查不到返回 ("", "")
        """
        try:
            db, db_lock = self._get_session()
            with db_lock:
                c = db.cursor()
                if engine_name:
                    from src.domains.runtime.agents.sandbox.backends import get_cloud_platforms, get_adapter
                    _cloud_platforms = sorted(
                        get_cloud_platforms(),
                        key=lambda p: getattr(get_adapter(p), "priority", 100),
                    )
                    if _cloud_platforms:
                        # 表归 mgmt：候选集从 public 取，原先库里的 ORDER BY CASE provider
                        # 排序挪到这里做（跨库后无法在 SQL 里按调用方的 platform 顺序排）。
                        from src.domains.mgmt import public as _mg_tpl
                        _prio = {p_: i for i, p_ in enumerate(_cloud_platforms)}
                        _cands = _mg_tpl.sandbox_template_candidates(f'%"{engine_name}"%')
                        if _cands:
                            # 等价于原来的
                            #   ORDER BY CASE provider <_cloud_platforms 顺序> ELSE 999 END,
                            #           CASE WHEN engines = '["<engine>"]' THEN 0 ELSE 1 END,
                            #           updated_at DESC
                            #   LIMIT 1
                            _exact = f'["{engine_name}"]'

                            def _rank(x):
                                return (_prio.get(x[1], 999), 0 if x[2] == _exact else 1)

                            _best_rank = min(_rank(x) for x in _cands)
                            _tied = [x for x in _cands if _rank(x) == _best_rank]
                            _top = max(_tied, key=lambda x: str(x[3] or ""))   # updated_at 降序
                            return _top[0] or "", _top[1] or ""
                # fallback：grape_t_engine_sandbox_config 旧表（查 sandbox_provider）
                from src.domains.runtime.agents.sandbox.backends import get_cloud_platforms as _get_cp
                _cloud_list = _get_cp()
                if not _cloud_list:
                    return "", ""
                # 表归 mgmt：两个分支都有现成 public 原语（单点 + 任意行兜底）
                from src.domains.mgmt import public as _mg_esc
                if runtime_id is not None and engine_name:
                    _sp = _mg_esc.engine_sandbox_provider_of(runtime_id, engine_name, _cloud_list)
                else:
                    _sp = _mg_esc.any_engine_sandbox_provider(_cloud_list)
                if _sp:
                    return "", _sp
        except Exception:
            pass
        return "", ""

    def make_db_state_reporter(self) -> Callable:
        """构造 DB 版 state_reporter——写 grape_t_cloud_sandboxes 表。

        返回的 reporter 每次调用时创建短生命周期 session（with db_lock），
        不长期持有 DB 连接。测试时可注入 mock session_factory。

        Returns:
            state_reporter 回调函数
        """
        def _reporter(*, state, sandbox_id="", issue_id="", repo_key="",
                      provider="", last_refresh_at="", agent_installed=None):
            from datetime import datetime, timezone
            _ts = datetime.now(timezone.utc).isoformat()
            try:
                db, db_lock = self._get_session()
                with db_lock:
                    c = db.cursor()
                    if state == "creating":
                        from src.kernel.sandbox_uid import new_row_uid as _nru
                        c.execute(
                            "INSERT OR IGNORE INTO grape_t_cloud_sandboxes "
                            "(id, issue_id, repo_key, sandbox_id, provider, state, created_at, last_refresh_at, agent_installed) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                            (_nru(), issue_id, repo_key, sandbox_id, provider, state, _ts, _ts),
                        )
                        # UPDATE 按 sandbox_id（每环节新建，sandbox_id 全局唯一）
                        if sandbox_id:
                            c.execute(
                                "UPDATE grape_t_cloud_sandboxes SET provider=?, state=?, last_refresh_at=? "
                                "WHERE sandbox_id=?",
                                (provider, state, _ts, sandbox_id),
                            )
                    else:
                        if last_refresh_at:
                            c.execute(
                                "UPDATE grape_t_cloud_sandboxes SET state=?, last_refresh_at=? "
                                "WHERE sandbox_id=?",
                                (state, last_refresh_at, sandbox_id),
                            )
                        else:
                            c.execute(
                                "UPDATE grape_t_cloud_sandboxes SET state=? "
                                "WHERE sandbox_id=?",
                                (state, sandbox_id),
                            )
                        if agent_installed is not None:
                            c.execute(
                                "UPDATE grape_t_cloud_sandboxes SET agent_installed=? "
                                "WHERE sandbox_id=?",
                                (agent_installed, sandbox_id),
                            )
                    db.commit()
            except Exception as e:
                log.error(f"db_state_reporter failed: {e}")
                raise  # A 机器 DB 失败 = 资源泄漏
        return _reporter
