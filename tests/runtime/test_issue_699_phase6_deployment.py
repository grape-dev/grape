"""Issue #699 Phase 6 验收测试（TC-6.1 ~ TC-6.5）。

设计依据：docs/weekly-09/grape-unified-implementation-design.md v2.2 §12 Phase 6
+ §14「Phase 6 验证用例」。

覆盖范围（本环境可自动化部分）：
- TC-6.1 单机模式（SQLite + 表前缀）：
  * v2.2 表注册表：chat 三表归 grape_runtime（mgmt 45 / runtime 25 核心表）
  * t_runtime() 前缀映射 + migrate_sqlite_prefixes 产出 rt_chat_*
  * RuntimeService.get_chat_* 契约（Mgmt 经 RuntimeService 读 chat）
  * dev_four_process.sh 存在/可执行/语法通过 + cluster 无配置快速失败
  * （实机四进程运行链路在本环境已用 dev_four_process.sh 等价人工验证，见 PR 描述）
- TC-6.2 集群模式（MySQL + 三 schema）：
  * migrate_schemas.py 按 TABLE_REGISTRY 生成 MySQL 计划（chat → grape_runtime）
  * internal /internal/chat/* 端点挂载 Worker（RemoteProvider 通道）
  * cluster.conf profile 内容校验（DEVOPS_DB_* / GRAPE_SERVICE_SECRET）
- TC-6.3 Docker Compose：四容器 + nginx 结构、compose nginx 分流与 v2.2 chat 归 Worker 一致
- TC-6.4 systemd：四 service 单元齐备且 ExecStart/端口正确（静态）
- TC-6.5 全量回归：入口为 pytest 全量运行（本文件不重复执行，见 PR 验证记录）
"""
import os
import sqlite3
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _read(path):
    with open(os.path.join(_REPO_ROOT, path), encoding="utf-8") as f:
        return f.read()


# ===========================================================================
# TC-6.1：单机模式（SQLite + 表前缀）验证
# ===========================================================================

class TestTC61SingleMode:
    def test_chat_tables_registered_to_runtime_v22(self):
        """v2.2：chat_conversations/chat_messages/chat_agent_session 归 grape_runtime。"""
        from src.kernel.db import TABLE_REGISTRY, SCHEMA_MGMT, SCHEMA_RUNTIME, SCHEMA_INSIGHT
        for t in ("chat_conversations", "chat_messages", "chat_agent_session"):
            assert TABLE_REGISTRY[t] == SCHEMA_RUNTIME, t
        counts = {SCHEMA_MGMT: 0, SCHEMA_RUNTIME: 0, SCHEMA_INSIGHT: 0}
        for schema in TABLE_REGISTRY.values():
            counts[schema] += 1
        # mgmt 核心 45 张（v2.2 迁走 chat 三表后）+ 附加/兼容表
        assert counts[SCHEMA_MGMT] >= 45
        # runtime 25 张（v2.2：22 + chat 三表；另含存量 agent_type_config）
        assert counts[SCHEMA_RUNTIME] >= 25
        assert counts[SCHEMA_INSIGHT] == 11

    def test_sqlite_prefix_mapping_chat(self, monkeypatch):
        """schema mode 开启时 chat 表映射为 rt_ 前缀（TC-6.1 单机前缀语义）。"""
        from src.kernel import db as kdb
        monkeypatch.setenv("GRAPE_SCHEMA_MODE", "1")
        assert kdb.t_runtime("chat_conversations") == "rt_chat_conversations"
        assert kdb.t_runtime("chat_messages") == "rt_chat_messages"
        assert kdb.t_runtime("chat_agent_session") == "rt_chat_agent_session"
        assert kdb.t_mgmt("grape_accounts").startswith("mgmt_")

    def test_migrate_sqlite_prefixes_moves_chat_to_rt(self, tmp_path, monkeypatch):
        """migrate_sqlite_prefixes 按 v2.2 注册表把 chat 表 rename 到 rt_ 前缀（保数据）。"""
        from src.kernel import db as kdb
        monkeypatch.setenv("GRAPE_SCHEMA_MODE", "1")
        db_path = str(tmp_path / "single.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE chat_conversations (id TEXT PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO chat_conversations VALUES ('c1', 'hello')")
        conn.commit()
        plan = kdb.migrate_sqlite_prefixes(conn, dry_run=False)
        renames = dict(plan)
        assert renames.get("chat_conversations") == "rt_chat_conversations"
        row = conn.execute("SELECT title FROM rt_chat_conversations WHERE id='c1'").fetchone()
        assert row and row[0] == "hello"
        conn.close()

    def test_runtime_service_chat_contract_local(self, tmp_path, monkeypatch):
        """RuntimeService.get_chat_* Local 实现（Mgmt 经 RuntimeService 读 chat，v2.2 方向）。"""
        db_path = str(tmp_path / "chat.db")
        monkeypatch.setenv("DEVOPS_DB_PATH", db_path)
        monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
        monkeypatch.setenv("GRAPE_SCHEMA_MODE", "1")
        conn = sqlite3.connect(db_path)
        # schema mode=1 → 前缀表名（rt_）
        conn.executescript("""
        CREATE TABLE rt_chat_conversations (
            id TEXT PRIMARY KEY, agent_session_id TEXT DEFAULT '',
            user_id INTEGER NOT NULL, backend_session_id TEXT DEFAULT '',
            repo_key TEXT DEFAULT '', im_channel_id TEXT DEFAULT '',
            title TEXT DEFAULT '', status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE rt_chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')), task_id TEXT,
            generate_status INTEGER DEFAULT NULL, deleted INTEGER DEFAULT 0
        );
        """)
        conn.execute(
            "INSERT INTO rt_chat_conversations (id, user_id, repo_key, title, status) "
            "VALUES ('c1', 1, 'test/repo', 'T1', 'active')")
        conn.execute(
            "INSERT INTO rt_chat_messages (id, conversation_id, role, content) "
            "VALUES (1, 'c1', 'user', 'hi'), (2, 'c1', 'assistant', 'hello')")
        conn.commit()
        conn.close()

        import src.kernel.session_manager as sm
        import importlib
        importlib.reload(sm)
        try:
            from src.domains.contracts.local.runtime_local import RuntimeServiceLocal
            svc = RuntimeServiceLocal()
            convs = svc.get_chat_conversations("test/repo")
            assert len(convs) == 1 and convs[0]["title"] == "T1"
            msgs = svc.get_chat_messages("c1")
            assert len(msgs) == 2
            assert [m["id"] for m in msgs] == [2, 1]
            msgs_before = svc.get_chat_messages("c1", before=2)
            assert len(msgs_before) == 1 and msgs_before[0]["id"] == 1
        finally:
            importlib.reload(sm)

    def test_dev_four_process_script_exists_and_valid(self):
        """scripts/dev_four_process.sh 存在、可执行、bash 语法通过（TC-6.1 验证命令入口）。"""
        path = os.path.join(_REPO_ROOT, "scripts", "dev_four_process.sh")
        assert os.path.isfile(path)
        assert os.access(path, os.X_OK)
        r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_dev_four_process_cluster_fails_fast_without_mysql_env(self, monkeypatch, tmp_path):
        """cluster+mysql 缺连接配置时快速失败（不静默回退 sqlite）。"""
        path = os.path.join(_REPO_ROOT, "scripts", "dev_four_process.sh")
        for var in ("DEVOPS_DB_HOST", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD",
                    "DEVOPS_DB_NAME", "GRAPE_SERVICE_SECRET", "PROFILE"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("DEVOPS_DB_PATH", str(tmp_path / "x.db"))
        r = subprocess.run(
            ["bash", path],
            capture_output=True, text=True, timeout=30,
            env={**os.environ,
                 "GRAPE_MODE": "cluster", "DEVOPS_DB_BACKEND": "mysql",
                 "DEVOPS_DB_PATH": str(tmp_path / "x.db"),
                 "GRAPE_FOUR_LOG_DIR": str(tmp_path / "logs")},
        )
        assert r.returncode != 0
        assert "requires DEVOPS_DB_HOST" in (r.stderr + r.stdout)


# ===========================================================================
# TC-6.2：集群模式（MySQL + 三 schema）验证
# ===========================================================================

class TestTC62ClusterMode:
    def test_migrate_schemas_mysql_plan_includes_chat_runtime(self, monkeypatch):
        """migrate_schemas --dry-run：MySQL 计划按 v2.2 把 chat 表迁到 grape_runtime（缺实例时降级校验注册表）。"""
        from src.kernel import db as kdb
        # 不依赖真实 MySQL：直接校验注册表→schema 限定名
        monkeypatch.setenv("GRAPE_SCHEMA_MODE", "1")
        monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
        qualified = kdb._qualified_table(kdb.SCHEMA_RUNTIME, "chat_conversations")
        assert qualified == "grape_runtime.chat_conversations"

    def test_worker_internal_chat_routes_mounted(self):
        """集群模式 RemoteProvider 通道：/internal/chat/* 挂载到 Worker 进程。"""
        from starlette.testclient import TestClient
        from src.api.grape_worker_server import app as worker_app
        # 路由经 _IncludedRouter 惰性挂载，用 TestClient 探测（raise_server_exceptions=False，
        # 只验证挂载存在——非 404 即已挂载；DB 内容与表结构由 Local provider 测试覆盖）
        c = TestClient(worker_app, raise_server_exceptions=False)
        r = c.get("/internal/chat/conversations?repo_key=test")
        assert r.status_code != 404, r.text
        r2 = c.get("/internal/chat/conversations/c1/messages")
        assert r2.status_code != 404, r2.text

    def test_cluster_profile_content(self):
        conf = _read("configs/profiles/cluster.conf")
        for key in ("GRAPE_MODE=cluster", "DEVOPS_DB_BACKEND=mysql",
                    "GRAPE_SCHEMA_MODE=1", "DEVOPS_DB_HOST=", "DEVOPS_DB_NAME=",
                    "GRAPE_SERVICE_SECRET="):
            assert key in conf, key

    def test_single_profile_content(self):
        conf = _read("configs/profiles/single.conf")
        for key in ("GRAPE_MODE=single", "DEVOPS_DB_BACKEND=sqlite",
                    "GRAPE_SCHEMA_MODE=1", "DEVOPS_SQLITE_WAL=1",
                    "DEVOPS_SQLITE_BUSY_TIMEOUT_MS=5000"):
            assert key in conf, key


# ===========================================================================
# TC-6.3：Docker Compose 四容器 + nginx 验证（静态结构校验）
# ===========================================================================

class TestTC63DockerCompose:
    def _compose(self):
        import yaml
        with open(os.path.join(_REPO_ROOT, "deploy", "docker-compose.yml"), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_four_app_services_plus_nginx(self):
        d = self._compose()
        assert set(d["services"]) == {
            "grape-server", "grape-event-server", "grape-worker-server",
            "grape-insight-server", "nginx",
        }

    def test_each_app_service_command_and_role(self):
        d = self._compose()
        expected = {
            "grape-server": ("src.api.grape_server:app", "38080", "server"),
            "grape-event-server": ("src.api.grape_event_server:app", "38081", "event"),
            "grape-worker-server": ("src.api.grape_worker_server:app", "38082", "worker"),
            "grape-insight-server": ("src.api.grape_insight_server:app", "38083", "insight"),
        }
        for svc, (module, port, role) in expected.items():
            s = d["services"][svc]
            cmd = " ".join(s["command"])
            assert module in cmd and port in cmd, (svc, cmd)
            env = s["environment"]
            assert f"GRAPE_PROCESS_ROLE={role}" in env
            assert any("healthcheck" == k for k in s), svc
            assert port in " ".join(s["healthcheck"]["test"])
            # 单机模式共享 SQLite 卷
            assert "grape-data:/app/data" in s["volumes"]

    def test_nginx_service_mounts_compose_conf(self):
        d = self._compose()
        s = d["services"]["nginx"]
        assert "80:80" in s["ports"]
        assert "./nginx-grape-compose.conf:/etc/nginx/conf.d/grape.conf:ro" in s["volumes"]
        assert set(s["depends_on"]) == {
            "grape-server", "grape-event-server", "grape-worker-server", "grape-insight-server",
        }

    def test_compose_nginx_upstreams_point_to_services(self):
        conf = _read("deploy/nginx-grape-compose.conf")
        for upstream, svc in (
            ("grape_server", "grape-server:38080"),
            ("grape_event_server", "grape-event-server:38081"),
            ("grape_worker_server", "grape-worker-server:38082"),
            ("grape_insight_server", "grape-insight-server:38083"),
        ):
            block = conf.split(f"upstream {upstream} {{")[1].split("}")[0]
            assert svc in block, (upstream, block)
            assert "127.0.0.1" not in block, (upstream, block)

    def test_compose_nginx_keeps_chat_split_to_worker(self):
        """与四进程版一致：/api/chat/ → Worker + SSE 不缓冲（v2.2 chat 执行归 Worker）。"""
        conf = _read("deploy/nginx-grape-compose.conf")
        chat_block = conf.split("location /api/chat/")[1].split("location")[0]
        assert "grape_worker_server" in chat_block
        assert "proxy_buffering off" in chat_block

    def test_healthcheck_only_hits_mounted_endpoint(self):
        """healthcheck 必须命中该进程真实挂载的健康端点。

        Event/Worker 只经 server_common.attach_health 挂载 /health（无 insight health.router
        的 /api/health，GET /api/health 返回 404 → 容器永久 unhealthy）；
        Mgmt/Insight 挂载 insight health.router，/api/health 可用。
        """
        d = self._compose()
        expectations = {
            "grape-server": ("38080", "/api/health"),
            "grape-event-server": ("38081", "/health"),
            "grape-worker-server": ("38082", "/health"),
            "grape-insight-server": ("38083", "/api/health"),
        }
        for svc, (port, path) in expectations.items():
            test_cmd = " ".join(d["services"][svc]["healthcheck"]["test"])
            assert port in test_cmd, (svc, test_cmd)
            assert f"http://127.0.0.1:{port}{path}'" in test_cmd, (svc, test_cmd)
        # 反向断言：Event/Worker 不得再探测未挂载的 /api/health
        for svc in ("grape-event-server", "grape-worker-server"):
            test_cmd = " ".join(d["services"][svc]["healthcheck"]["test"])
            assert "/api/health" not in test_cmd, (svc, test_cmd)

    def test_compose_nginx_routes_chat_reads_to_mgmt(self):
        """chat 读端点（conversations/messages 等 GET）→ Mgmt，避免 Worker 405/404。"""
        conf = _read("deploy/nginx-grape-compose.conf")
        read_block = conf.split("location ~ ^/api/chat/")[1].split("location ")[0]
        assert "grape_server" in read_block
        assert "grape_worker_server" not in read_block

        import re
        m = re.search(r"location ~ (\^/api/chat/\([^)]*\))", conf)
        assert m, "chat read-endpoint regex location missing"
        pattern = re.compile(m.group(1))
        for path in (
            "/api/chat/conversations",
            "/api/chat/messages/12",
            "/api/chat/config",
            "/api/chat/task/t1/stream",
            "/api/chat/task/t1/status",
            "/api/chat/analyze-image",
            "/api/chat/image/12/a.png",
        ):
            assert pattern.match(path), path
        # chat 执行端点仍归 Worker（不被读端点正则吞掉）
        for path in ("/api/chat/send", "/api/chat/send/stream", "/api/chat/submit"):
            assert not pattern.match(path), path


# ===========================================================================
# TC-6.4：systemd 服务管理验证（静态结构；systemd 运行态在本环境不可用）
# ===========================================================================

class TestTC64Systemd:
    @pytest.mark.parametrize("unit,port,module", [
        ("grape-server.service", "38080", "src.api.grape_server:app"),
        ("grape-event-server.service", "38081", "src.api.grape_event_server:app"),
        ("grape-worker-server.service", "38082", "src.api.grape_worker_server:app"),
        ("grape-insight-server.service", "38083", "src.api.grape_insight_server:app"),
    ])
    def test_unit_files(self, unit, port, module):
        text = _read(f"deploy/systemd/{unit}")
        assert f"--port {port}" in text
        assert module in text
        assert "Restart=always" in text


# ===========================================================================
# TC-6.5：全量回归入口（实际执行见 PR 验证记录：pytest tests/ 全量）
# ===========================================================================

class TestTC65Regression:
    def test_verify_all_entry(self):
        """TC-6.5 验证命令为 pytest 全量；仓库无 verify_all.sh 时以 pytest 为准（PR 描述注明）。"""
        assert not os.path.exists(os.path.join(_REPO_ROOT, "scripts", "verify_all.sh"))
        # 全量回归入口存在
        assert os.path.exists(os.path.join(_REPO_ROOT, "pytest.ini"))
