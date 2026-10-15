"""Issue 306: a full bound worker is reachable; next-stage work must queue."""
import concurrent.futures
from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest
from src.session_manager import db, db_lock
from src.runtime import service

REPO = 'test_handoff/repo'


@pytest.fixture
def engines():
    ids, credentials = [], []
    with db_lock:
        cursor = db.cursor()
        for name in ('bound', 'outside'):
            suffix = uuid.uuid4().hex
            cursor.execute('INSERT INTO runtime_credentials (token_hash,name,scope,owner_id) VALUES (?,?,?,?)',
                           (suffix, 'test_handoff_' + name + suffix, 'repo', REPO))
            credentials.append(cursor.lastrowid)
            cursor.execute('INSERT INTO agent_runtimes '
                           '(credential_id,name,status,healthy,probe_status,last_seen_at,busy_slots,max_concurrent) '
                           'VALUES (?,?,?,?,?,?,0,1)', (cursor.lastrowid,'test_handoff_'+name+suffix,
                           'online',1,'ok',datetime.now(timezone.utc).isoformat()))
            ids.append(cursor.lastrowid)
            cursor.execute("INSERT INTO engine_capabilities (runtime_id,tag,enabled) VALUES (?,'all',1)", (ids[-1],))
        db.commit()
    yield ids
    with db_lock:
        for rid in ids:
            db.execute('DELETE FROM engine_capabilities WHERE runtime_id=?',(rid,))
            db.execute('DELETE FROM agent_runtimes WHERE id=?',(rid,))
        for cid in credentials: db.execute('DELETE FROM runtime_credentials WHERE id=?',(cid,))
        db.execute('DELETE FROM agent_tasks WHERE repo_key=?',('gitcode:'+REPO,))
        db.commit()


def enqueue(runtime=0, *, bound=None, session=None, issue_session=''):
    return service.enqueue_task(agent_session_id=session or uuid.uuid4().hex,
        runtime_id=runtime, prompt='synthetic handoff', event_seq=0, issue_id='#6',
        repo_key='gitcode:'+REPO, agent_type='qa', skill='', env_extra={},
        issue_session_id=issue_session, pool_tag='qa' if not runtime else '',
        pool_agent_type='qa' if not runtime else '',
        pool_engine_ids=json.dumps([bound]) if bound else '')


def busy(rid):
    with db_lock:
        return db.execute('SELECT busy_slots FROM agent_runtimes WHERE id=?',(rid,)).fetchone()[0]


def test_parent_completion_allows_only_bound_worker_to_claim_child(engines):
    bound, outside = engines
    parent = enqueue(bound)
    assert service.claim(bound)['id'] == parent
    child = enqueue(bound=bound)
    for _ in range(3):
        assert service.claim_by_tag('qa',outside) is None
        assert service.get_task(child)['status'] == 'queued'
        assert json.loads(service.get_task(child)['pool_engine_ids']) == [bound]
        assert service.claim_by_tag('qa',bound) is None
    assert busy(bound) == 1 and busy(outside) == 0
    service.update_task_status(parent,'completed',exit_code=0)
    assert service.claim_by_tag('qa',outside) is None
    assert service.claim_by_tag('qa',bound)['id'] == child
    assert service.get_task(child)['runtime_id'] == bound
    assert busy(bound) == 1 and busy(outside) == 0
    assert service.claim_by_tag('qa',bound) is None


def test_shared_issue_session_remains_serial(engines):
    bound, outside = engines
    parent = enqueue(bound,issue_session='synthetic-issue-session')
    assert service.claim(bound)['id'] == parent
    child = enqueue(bound=bound,issue_session='synthetic-issue-session')
    assert service.claim_by_tag('qa',outside) is None
    assert service.get_task(child)['status'] == 'queued'
    service.update_task_status(parent,'completed',exit_code=0)
    assert service.claim_by_tag('qa',bound)['id'] == child


def test_concurrent_polls_do_not_exceed_bound_capacity(engines):
    bound, outside = engines
    children = {enqueue(bound=bound),enqueue(bound=bound)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _:service.claim_by_tag('qa',bound),range(8)))
    claimed = [r for r in results if r]
    assert len(claimed) == 1 and claimed[0]['id'] in children
    assert busy(bound) == 1
    assert service.claim_by_tag('qa',outside) is None
    queued = children - {claimed[0]['id']}
    assert all(service.get_task(tid)['status'] == 'queued' for tid in queued)


@pytest.mark.parametrize('failure',['offline','stale','unhealthy','probe','tag','scope'])
def test_physically_unreachable_bound_engine_still_fails_closed(engines,failure):
    bound, outside = engines
    with db_lock:
        if failure == 'offline': db.execute("UPDATE agent_runtimes SET status='offline' WHERE id=?",(bound,))
        if failure == 'stale': db.execute('UPDATE agent_runtimes SET last_seen_at=? WHERE id=?',
            ((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),bound))
        if failure == 'unhealthy': db.execute('UPDATE agent_runtimes SET healthy=0 WHERE id=?',(bound,))
        if failure == 'probe': db.execute("UPDATE agent_runtimes SET probe_status='failed' WHERE id=?",(bound,))
        if failure == 'tag': db.execute('DELETE FROM engine_capabilities WHERE runtime_id=?',(bound,))
        if failure == 'scope': db.execute("UPDATE runtime_credentials SET owner_id='other/repo' WHERE id IN "
            '(SELECT credential_id FROM agent_runtimes WHERE id=?)',(bound,))
        db.commit()
    child = enqueue(bound=bound)
    assert service.claim_by_tag('qa',outside) is None
    assert service.get_task(child)['status'] == 'failed'
    assert json.loads(service.get_task(child)['pool_engine_ids']) == [bound]
    assert busy(outside) == 0
