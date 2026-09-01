"""验证 claim_by_tag global scope skip 循环传参修复（SQL 1064 bug）"""
import pytest
import sys
sys.path.insert(0, '/opt/grape')

from src.runtime import service


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    """用 SQLite 临时库替代 MySQL"""
    import sqlite3
    db_path = str(tmp_path / "test.db")
    test_db = sqlite3.connect(db_path, check_same_thread=False)
    test_db.row_factory = sqlite3.Row

    # 建表
    test_db.executescript("""
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY,
        agent_session_id TEXT DEFAULT '',
        runtime_id INTEGER DEFAULT 0,
        event_seq INTEGER,
        issue_id TEXT,
        repo_key TEXT DEFAULT '',
        agent_type TEXT,
        skill TEXT,
        prompt TEXT DEFAULT '',
        cwd TEXT DEFAULT '',
        status TEXT DEFAULT 'queued',
        created_at TEXT,
        started_at TEXT,
        completed_at TEXT,
        attempts INTEGER DEFAULT 0,
        pool_tag TEXT DEFAULT '',
        pool_agent_type TEXT DEFAULT '',
        pool_engine_ids TEXT DEFAULT '',
        issue_session_id TEXT DEFAULT '',
        user_id INTEGER DEFAULT 0,
        skill_id INTEGER,
        exit_code INTEGER,
        result_text TEXT DEFAULT '',
        errors TEXT DEFAULT '',
        stderr TEXT DEFAULT '',
        usage_json TEXT DEFAULT '{}',
        knowledge_files TEXT DEFAULT '{}',
        skill_files TEXT DEFAULT '{}',
        project_info_files TEXT DEFAULT '{}',
        project_info_meta TEXT DEFAULT '{}',
        project_info_file_urls TEXT DEFAULT '{}',
        knowledge_file_urls TEXT DEFAULT '{}',
        env_extra TEXT DEFAULT '{}',
        agent_type_col TEXT DEFAULT '',
        server_url TEXT DEFAULT '',
        account TEXT DEFAULT '',
        repo_gitcode_token TEXT DEFAULT '',
        runtime_session_id TEXT DEFAULT '',
        affinity_runtime_id INTEGER DEFAULT 0,
        event_type TEXT DEFAULT ''
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY,
        credential_id INTEGER DEFAULT 0,
        name TEXT,
        status TEXT DEFAULT 'online',
        busy_slots INTEGER DEFAULT 0,
        max_concurrent INTEGER DEFAULT 4,
        agent_tags TEXT DEFAULT '[]',
        scope TEXT DEFAULT 'global',
        owner_id TEXT DEFAULT '',
        host_name TEXT DEFAULT '',
        last_seen_at TEXT
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY,
        scope TEXT DEFAULT 'global',
        owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '',
        device_name TEXT,
        last_ip TEXT,
        status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (
        runtime_id INTEGER,
        tag TEXT,
        max_concurrent INTEGER DEFAULT 4,
        enabled INTEGER DEFAULT 1
    );
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY,
        event_type TEXT,
        repo_key TEXT,
        status TEXT,
        created_at TEXT,
        runtime TEXT,
        payload TEXT DEFAULT ''
    );
    """)

    monkeypatch.setattr(service, 'db', test_db)
    monkeypatch.setattr(service, 'db_lock', __import__('threading').Lock())

    # 插入引擎
    test_db.execute("INSERT INTO agent_runtimes (id, name, status, agent_tags, credential_id) VALUES (76, 'codearts', 'online', '[\"vod\"]', 63)")
    test_db.execute("INSERT INTO runtime_credentials (id, scope, owner_name) VALUES (63, 'global', '')")
    test_db.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, enabled) VALUES (76, 'vod', 4, 1)")

    # 插入两个 vod 任务，第一个 pool_engine_ids 不含 76（应被跳过），第二个含 76（应被认领）
    test_db.execute("""
        INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, agent_session_id, issue_session_id, created_at, agent_type)
        VALUES ('t_skip_me', 'queued', 0, 'vod', '[999]', 's1', 'is1', '2026-08-31T06:55:00', 'vod')
    """)
    test_db.execute("""
        INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, agent_session_id, issue_session_id, created_at, agent_type)
        VALUES ('t_4e73a9363f87', 'queued', 0, 'vod', '[76]', 's2', 'is2', '2026-08-31T06:55:46', 'vod')
    """)
    test_db.commit()
    yield
    test_db.close()


def test_claim_by_tag_global_scope_skip_loop_passes_params(setup_db):
    """tag='vod'（非 'all'）+ global scope + skip 循环：必须传 params 避免 SQL 1064"""
    # 调用 claim_by_tag，tag='vod'，runtime_id=76
    # 第一个任务 t_skip_me 的 pool_engine_ids=[999] 不含 76 → 应跳过
    # 第二个任务 t_4e73a9363f87 的 pool_engine_ids=[76] 含 76 → 应认领
    result = service.claim_by_tag('vod', 76)
    
    # 修复前：claim_by_tag 返回 None（SQL 1064 错误被 except 吞掉）
    # 修复后：应成功认领 t_4e73a9363f87
    assert result is not None, "claim_by_tag returned None — SQL params bug not fixed!"
    assert result['id'] == 't_4e73a9363f87', f"expected t_4e73a9363f87, got {result.get('id')}"
    assert result['status'] == 'dispatched'


def test_claim_by_tag_all_tag_still_works(setup_db):
    """tag='all' 广播模式不受影响（params=[] 时 tuple(params)=() 无占位符）"""
    from src.runtime import service as svc
    # 先插入一个 pool_tag='qa' 的任务让 'all' 标签能匹配
    svc.db.execute("""
        INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, agent_session_id, issue_session_id, created_at, agent_type)
        VALUES ('t_all_test', 'queued', 0, 'qa', '', 's3', 'is3', '2026-08-31T07:00:00', 'qa')
    """)
    svc.db.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, enabled) VALUES (76, 'all', 4, 1)")
    svc.db.commit()
    
    result = service.claim_by_tag('all', 76)
    # 'all' tag 不应该报错，能认领任意 pool_tag 任务
    # 可能认领到 t_skip_me/t_4e73a9363f87/t_all_test 中的一个（按 created_at ASC）
    assert result is not None, "claim_by_tag 'all' returned None unexpectedly"


def test_claim_by_tag_repo_scope_prefix_mismatch(setup_db):
    """repo scope: owner_id 无前缀 vs agent_tasks.repo_key 带前缀，IN (?, ?) 必须两种都匹配"""
    from src.runtime import service as svc
    # 注册一个 repo scope 的 worker（owner_id 无前缀）
    svc.db.execute("INSERT INTO agent_runtimes (id, name, status, credential_id) VALUES (173, 'opencode', 'online', 167)")
    svc.db.execute("INSERT INTO runtime_credentials (id, scope, owner_id, owner_name) VALUES (167, 'repo', 'Sinkear/AgentLens', 'Sinkear')")
    svc.db.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, enabled) VALUES (173, 'all', 4, 1)")
    # 插入一个 repo_key 带前缀的 queued 任务
    svc.db.execute("""
        INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, agent_session_id, issue_session_id, created_at, agent_type, repo_key)
        VALUES ('t_repo_prefix', 'queued', 0, 'vod', '[173]', 's4', 'is4', '2026-08-31T08:00:00', 'vod', 'gitcode:Sinkear/AgentLens')
    """)
    svc.db.commit()

    result = service.claim_by_tag('all', 173)
    assert result is not None, "claim_by_tag repo scope returned None — owner_id/repo_key prefix mismatch not fixed!"
    assert result['id'] == 't_repo_prefix', f"expected t_repo_prefix, got {result.get('id')}"
