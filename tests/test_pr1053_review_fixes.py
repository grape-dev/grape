"""架构审查 #1053 C1/M1/M2 的回归用例（纯本地，不连远端、不触碰生产库）。

- M2：autopilot_executions.agent_task_id 列未落（MySQL 已盖章但列未建）时，
  /api/autopilot/executions 必须降级为空串而不是 1054 -> 500；列存在时正常读取。
- M1：ZOMBIE_YOUNG_GRACE_SECONDS 支持 env 覆盖（与相邻宽限常量同口径）。
- C1：index.html 里 dashboard.js / app.js 的缓存版本号已 bump（静态断言）。
"""
import os
import re
import subprocess
import sys

from src.api.routes import autopilot as ap
from src.session_manager import db, db_lock

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
EXEC_ID = 1905300001
TASK_ID = 1053
ATID = "t_1053_probe"


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE id=?", (EXEC_ID,))
        db.commit()


def _seed():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE id=?", (EXEC_ID,))
        c.execute(
            "INSERT INTO autopilot_executions "
            "(id, task_id, status, retry_count, result, error, started_at, completed_at, "
            " created_at, agent_type, findings_json, agent_task_id) "
            "VALUES (?, ?, 'success', 0, 'ok', '', '2026-11-09T10:00:00+00:00', "
            " '2026-11-09T10:01:00+00:00', '2026-11-09T10:00:00+00:00', 'vod', '', ?)",
            (EXEC_ID, TASK_ID, ATID),
        )
        db.commit()


def _find(result):
    for row in result["executions"]:
        if row["id"] == EXEC_ID:
            return row
    raise AssertionError("seeded execution not returned")


def test_executions_degrades_when_agent_task_id_column_missing(monkeypatch):
    """M2：模拟列缺失（探测返回的列集合不含 agent_task_id）→ 接口降级，不抛异常。"""
    _seed()
    try:
        real_cols = ["id", "task_id", "status", "retry_count", "result", "error",
                     "started_at", "completed_at", "created_at", "agent_type", "findings_json"]
        monkeypatch.setattr(ap, "_autopilot_exec_cols", lambda: list(real_cols))
        out = ap.list_executions(user={}, task_id=None, status=None, limit=50, offset=0)
        assert _find(out)["agent_task_id"] == ""
    finally:
        _cleanup()


def test_executions_reads_agent_task_id_when_column_present():
    """对照：真实 schema 有列 → 直接带出 agent_task_id。"""
    _seed()
    try:
        out = ap.list_executions(user={}, task_id=None, status=None, limit=50, offset=0)
        assert _find(out)["agent_task_id"] == ATID
    finally:
        _cleanup()


def test_server_url_placeholders_use_matching_bases(monkeypatch):
    """M3：{{SERVER_URL}} = 业务 API 基址，{{RUNTIME_SERVER_URL}} = 回连基址（先替换长占位符）。"""
    from src import prompt_builder as pb

    monkeypatch.setattr(pb, "_resolve_server_url", lambda runtime_id=0: "http://rt.example:8080")
    monkeypatch.setattr(pb, "_resolve_business_server_url", lambda runtime_id=0: "http://biz.example")
    out = pb._apply_server_url_placeholders(
        "A {{SERVER_URL}}/api/issues B {{RUNTIME_SERVER_URL}}/api/a2a C")
    assert out == ("A http://biz.example/api/issues "
                   "B http://rt.example:8080/api/a2a C")
    # 无残留占位符
    assert "{{" not in out


def test_zombie_young_grace_env_override():
    """M1：常量可由 env 覆盖，默认 120。"""
    code = ("from src.runtime.scheduler import ZOMBIE_YOUNG_GRACE_SECONDS as v;"
            "print(v)")
    for env_value, expected in (("77", "77"), (None, "120")):
        env = dict(os.environ)
        env.pop("ZOMBIE_YOUNG_GRACE_SECONDS", None)
        if env_value is not None:
            env["ZOMBIE_YOUNG_GRACE_SECONDS"] = env_value
        proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-800:]
        assert proc.stdout.strip().splitlines()[-1] == expected


def test_index_html_js_cache_versions_bumped():
    """C1：dashboard.js / app.js 版本号必须高于打回时的 111 / 213。"""
    html = open(os.path.join(ROOT, "src/api/static/index.html"), encoding="utf-8").read()
    dash_m = re.search(r"pages/dashboard\.js\?v=(\d+)", html)
    app_m = re.search(r"js/app\.js\?v=(\d+)", html)
    assert dash_m and app_m, "index.html 缺少 JS 版本标记"
    assert int(dash_m.group(1)) >= 112, dash_m.group(0)
    assert int(app_m.group(1)) >= 214, app_m.group(0)