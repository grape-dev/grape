"""Issue #1208 原子契约测试：T11 存量场景节点 uid 升格 backfill 迁移（202611050002）。

覆盖（Issue 正文 §2.7 / §3 验收 8 / E2E-8 断言 4/5/6）：
- ids 有效 → JOIN 查 engine_uid → 写 uids（v2 整改：保留 ids 双写并存，清退并入 #646）；
- 迁移改写前 scene 行原文备份到 devops_pipeline_scene_uid_backup_1208（报告记 backup_rows）；
- id 悬空（引擎行不存在/uid NULL）→ 跳过不补 + 迁移报告列清单（不猜不静默）；
- 无绑定 / selector 节点 → 不动（不自动生成 selector）；
- 幂等两跑 uid 稳定；报告落盘（tag=1208）；
- 升格前后读侧派发命中同一批引擎（uids 优先回落 ids 读侧等价）。
"""
import json
import sqlite3

import pytest

UID1 = "aaaaaaaa-1111-4111-8111-111111111111"
UID2 = "bbbbbbbb-2222-4222-8222-222222222222"


@pytest.fixture()
def mig_conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "mig.db"))
    c.executescript("""
    CREATE TABLE devops_pipeline_scene (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scene_id TEXT, name TEXT DEFAULT '',
        description TEXT DEFAULT '', nodes_json TEXT DEFAULT '[]',
        onboarding TEXT DEFAULT '', is_active INTEGER DEFAULT 1, repo_key TEXT DEFAULT ''
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT DEFAULT '', engine_uid VARCHAR(36) DEFAULT NULL,
        credential_id INTEGER DEFAULT 0, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        status TEXT DEFAULT 'online', healthy INTEGER DEFAULT 1, installed INTEGER DEFAULT 1,
        last_seen_at TEXT DEFAULT '', role_tags TEXT DEFAULT ''
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, device_name TEXT DEFAULT '', owner_name TEXT DEFAULT ''
    );
    """)
    yield c
    c.close()


def _seed(c):
    c.execute("INSERT INTO agent_runtimes (id, engine_uid) VALUES (10, ?)", (UID1,))
    c.execute("INSERT INTO agent_runtimes (id, engine_uid) VALUES (11, ?)", (UID2,))
    c.execute("INSERT INTO agent_runtimes (id, engine_uid) VALUES (12, NULL)")  # uid 缺失=悬空
    c.executemany(
        "INSERT INTO devops_pipeline_scene (scene_id, nodes_json, is_active, repo_key) VALUES (?,?,1,'')",
        [
            ("sc-valid", json.dumps([{"id": "coding", "tag": "coding",
                                      "engine_runtime_ids": [10, 11], "engine_runtime_uids": []}])),
            ("sc-dangling", json.dumps([{"id": "coding", "tag": "coding",
                                         "engine_runtime_ids": [99]}])),
            ("sc-null-uid", json.dumps([{"id": "coding", "tag": "coding",
                                         "engine_runtime_ids": [12]}])),
            ("sc-nobind", json.dumps([{"id": "coding", "tag": "coding",
                                       "engine_runtime_ids": [], "engine_runtime_uids": []}])),
            ("sc-selector", json.dumps([{"id": "coding", "tag": "coding",
                                         "engine_runtime_ids": [],
                                         "engine_selector": {"tags": ["scope:repo"]}}])),
            ("sc-inactive", json.dumps([{"id": "coding", "tag": "coding",
                                         "engine_runtime_ids": [10]}])),
        ])
    c.execute("UPDATE devops_pipeline_scene SET is_active=0 WHERE scene_id='sc-inactive'")
    c.commit()


class TestT11Backfill:
    def test_valid_upgraded_dangling_skipped(self, mig_conn):
        from src.api.db_setup import _migration_1208_backfill_scene_uids
        _seed(mig_conn)
        report = _migration_1208_backfill_scene_uids(conn=mig_conn)
        nodes = {r[0]: json.loads(r[1]) for r in
                 mig_conn.execute("SELECT scene_id, nodes_json FROM devops_pipeline_scene")}
        # 有效：uids 写入；v2 整改：ids 保留双写并存（真正清退并入 #646 退役批次）
        nv = nodes["sc-valid"][0]
        assert nv["engine_runtime_uids"] == [UID1, UID2]
        assert nv["engine_runtime_ids"] == [10, 11]
        # 改写行原文备份 + 报告行数
        bk = mig_conn.execute(
            "SELECT scene_id, nodes_json FROM devops_pipeline_scene_uid_backup_1208 "
            "WHERE scene_id='sc-valid'").fetchall()
        assert len(bk) == 1
        assert json.loads(bk[0][1])[0]["engine_runtime_ids"] == [10, 11]
        assert report["backup_rows"] == 1 and report["ids_retained"] is True
        # 悬空（行不存在）：跳过不补
        nd = nodes["sc-dangling"][0]
        assert nd["engine_runtime_ids"] == [99] and nd.get("engine_runtime_uids") in (None, [])
        assert any(d["scene_id"] == "sc-dangling" for d in report.get("dangling", []))
        # uid NULL 行：悬空同口径
        assert nodes["sc-null-uid"][0]["engine_runtime_ids"] == [12]
        assert any(d["scene_id"] == "sc-null-uid" for d in report.get("dangling", []))
        # 无绑定/selector：不动
        assert nodes["sc-nobind"][0]["engine_runtime_ids"] == []
        assert nodes["sc-selector"][0]["engine_selector"] == {"tags": ["scope:repo"]}
        assert not nodes["sc-selector"][0].get("engine_runtime_uids")
        # 非活跃行不扫
        assert nodes["sc-inactive"][0]["engine_runtime_ids"] == [10]

    def test_idempotent_two_runs_stable(self, mig_conn):
        from src.api.db_setup import _migration_1208_backfill_scene_uids
        _seed(mig_conn)
        r1 = _migration_1208_backfill_scene_uids(conn=mig_conn)
        n1 = mig_conn.execute(
            "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id='sc-valid'").fetchone()[0]
        r2 = _migration_1208_backfill_scene_uids(conn=mig_conn)
        n2 = mig_conn.execute(
            "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id='sc-valid'").fetchone()[0]
        assert n1 == n2                      # 两跑 uid 稳定
        assert r2["upgraded_nodes"] == 0     # 二跑零变更
        assert r1["upgraded_nodes"] == 1     # 仅 sc-valid（悬空两例跳过不补）

    def test_read_side_equivalence(self, mig_conn, monkeypatch):
        # 升格前后读侧派发命中同一批引擎（uids 优先回落 ids 读侧等价，验收 E2E-8.6）
        from src.api.db_setup import _migration_1208_backfill_scene_uids
        import src.kernel.session_manager as sm
        _seed(mig_conn)
        # 迁移适配：monkeypatch（可回滚），避免裸赋值把连接泄漏给后续用例
        monkeypatch.setattr(sm, "db", mig_conn)
        monkeypatch.setattr(sm, "db_lock", __import__("threading").RLock())
        from src.api import pipeline_config as pc
        from src.domains.runtime.engine.engine_select import _engines_by_ids, _engines_by_uids
        from src.api.pipeline_config import get_scene_engine_binding
        pc.invalidate_scene_cache()
        before_ids = get_scene_engine_binding("sc-valid", "coding", repo_key="")["engine_ids"]
        before = sorted(m["runtime_id"] for m in _engines_by_ids(before_ids, "", {}))
        assert before == [10, 11]           # 升格前：ids 轨可解析

        _rep = _migration_1208_backfill_scene_uids(conn=mig_conn)
        assert _rep["upgraded_scenes"] == ["sc-valid"]

        # DB 真相：uids 已写入、ids 按 v2 整改保留（双写并存，退役并入 #646 批次）
        import json as _json
        _node = _json.loads(mig_conn.execute(
            "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id='sc-valid'").fetchone()[0])[0]
        _uids = _node["engine_runtime_uids"]
        assert _uids and _node["engine_runtime_ids"] == [10, 11]

        # 读侧等价：uids 轨解析出同一批引擎（不依赖 60s 配置缓存，直接以 DB 真相验证）
        after = sorted(m["runtime_id"] for m in _engines_by_uids(_uids, ""))
        assert after == before == [10, 11]

    def test_missing_tables_tolerated(self, tmp_path):
        # 场景表未建（异常库形态）：迁移不抛错不阻断启动
        from src.api.db_setup import _migration_1208_backfill_scene_uids
        c = sqlite3.connect(str(tmp_path / "empty.db"))
        report = _migration_1208_backfill_scene_uids(conn=c)
        assert report.get("scenes_total", 0) == 0


# ===== v3 整改回归：迁移遗留 ids/uids 双写不阻塞保存（QA 复审阻塞项） =====
# 迁移 202611050002 升格保留 ids（双写并存）后，保存端 _validate_scene_engine_ids
# 必须对「uids 解析集合与 ids 一致」的 legacy 双写归一放行（uids 权威），
# 且透明升格把落库双写归一为 uids-only；显式双轨（集合不一致）仍 400。

class TestLegacyDualWriteSaveCompat:
    @pytest.fixture()
    def save_compat_db(self, mig_conn, monkeypatch):
        c = mig_conn
        # 校验/升格链路所需列与表（迁移 fixture 之上补齐）
        c.execute("ALTER TABLE runtime_credentials ADD COLUMN scope TEXT DEFAULT 'global'")
        c.execute("ALTER TABLE runtime_credentials ADD COLUMN owner_id TEXT DEFAULT ''")
        c.execute("CREATE TABLE engine_capabilities (runtime_id INTEGER, tag TEXT, enabled INTEGER DEFAULT 1)")
        c.execute("INSERT INTO engine_capabilities VALUES (10, 'coding', 1)")
        c.execute("INSERT INTO engine_capabilities VALUES (11, 'coding', 1)")
        c.commit()
        import src.kernel.session_manager as sm
        monkeypatch.setattr(sm, "db", c)
        monkeypatch.setattr(sm, "db_lock", __import__("threading").RLock())
        import src.domains.mgmt.user.db as udb
        monkeypatch.setattr(udb, "db", c)
        monkeypatch.setattr(udb, "db_lock", __import__("threading").RLock())
        from src.api import pipeline_config as pc
        monkeypatch.setattr(pc, "_SCENE_CACHE", {})
        yield c

    def _migrated_node(self, db):
        from src.api.db_setup import _migration_1208_backfill_scene_uids
        _seed(db)
        _migration_1208_backfill_scene_uids(conn=db)
        return json.loads(db.execute(
            "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id='sc-valid'").fetchone()[0])[0]

    def test_migrated_dual_write_passes_validation(self, save_compat_db):
        node = self._migrated_node(save_compat_db)
        assert node["engine_runtime_ids"] == [10, 11]          # 迁移保留 ids 双写
        assert node["engine_runtime_uids"] == [UID1, UID2]
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        _validate_scene_engine_ids([node], "gitcode:o/r", "u")  # 不抛（遗留双写归一）

    def test_explicit_mismatched_dual_write_rejected(self, save_compat_db):
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
        node = self._migrated_node(save_compat_db)
        node["engine_runtime_ids"] = [10, 99]                  # 与 uids 解析集不一致=显式双轨
        with pytest.raises(Exception) as ei:
            _validate_scene_engine_ids([node], "gitcode:o/r", "u")
        assert getattr(ei.value, "status_code", 0) == 400

    def test_migrated_node_upgrade_normalizes_to_uids_only(self, save_compat_db):
        node = self._migrated_node(save_compat_db)
        from src.domains.mgmt.routes.scenes import _upgrade_scene_nodes_uid
        _upgrade_scene_nodes_uid([node])
        assert node["engine_runtime_uids"] == [UID1, UID2]
        assert node["engine_runtime_ids"] == []                # 落库归一 uids-only

    def test_migrated_scene_end_to_end_save(self, save_compat_db):
        # 迁移后取节点 → 校验 → 升格 → 落库：与页面保存链路同序，全链不 400
        from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids, _upgrade_scene_nodes_uid
        nodes = self._migrated_node(save_compat_db)
        _validate_scene_engine_ids([nodes], "gitcode:o/r", "u")
        _upgrade_scene_nodes_uid([nodes])
        save_compat_db.execute(
            "UPDATE devops_pipeline_scene SET nodes_json=? WHERE scene_id='sc-valid'",
            (json.dumps([nodes], ensure_ascii=False),))
        save_compat_db.commit()
        saved = json.loads(save_compat_db.execute(
            "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id='sc-valid'").fetchone()[0])[0]
        assert saved["engine_runtime_uids"] == [UID1, UID2]
        assert saved["engine_runtime_ids"] == []
