"""Personal pool checks must use the same account identity as claim_by_tag.

Exercise the real reachability function against isolated SQLite tables without
starting the application's global database or background services.
"""
import ast
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import time
from types import ModuleType
import unittest
from unittest.mock import patch


class PersonalPoolIdentityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE agent_runtimes (id INTEGER, status TEXT, healthy INTEGER,
                probe_status TEXT, last_seen_at TEXT, credential_id INTEGER);
            CREATE TABLE engine_capabilities (runtime_id INTEGER, enabled INTEGER, tag TEXT);
            CREATE TABLE runtime_credentials (id INTEGER, scope TEXT, owner_id TEXT, owner_name TEXT);
            CREATE TABLE grape_accounts (id INTEGER, username TEXT);
            CREATE TABLE unified_user_mapping (user_id INTEGER, gitcode_user TEXT);
        ''')
        self.db.execute('INSERT INTO agent_runtimes VALUES (1,?,?,?,?,1)',
                        ('online', 1, 'ok', datetime.now(timezone.utc).isoformat()))
        self.db.execute("INSERT INTO engine_capabilities VALUES (1,1,'sre')")
        self.db.execute("INSERT INTO runtime_credentials VALUES (1,'user','123','fixture-owner')")
        self.db.execute("INSERT INTO grape_accounts VALUES (123,'fixture-owner')")
        repo = ModuleType('src.repo_key')
        repo.from_repo_key = lambda value: value.removeprefix('gitcode:')
        health = ModuleType('src.runtime.health')
        health.parse_dt = lambda value: datetime.fromisoformat(value).timestamp()
        modules = patch.dict(sys.modules, {'src.repo_key': repo, 'src.runtime.health': health})
        modules.start()
        self.addCleanup(modules.stop)
        source = Path(__file__).resolve().parents[1] / 'src/runtime/service.py'
        tree = ast.parse(source.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == '_check_pool_engines_reachable')
        namespace = {'db': self.db, 'time': time, '_RUNTIME_STALE_SECONDS': 100}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        self.reachable = namespace['_check_pool_engines_reachable']

    def check(self, user_id=123, tag='sre'):
        return self.reachable([1], tag, 'gitcode:fixture/repo', task_user_id=user_id)

    def legacy(self, user_id):
        self.db.execute("INSERT INTO unified_user_mapping VALUES (?,'fixture-owner')", (user_id,))

    def test_missing_legacy_mapping_keeps_canonical_owner_reachable(self):
        self.assertTrue(self.check())

    def test_stale_legacy_mapping_keeps_canonical_owner_reachable(self):
        self.legacy(99)
        self.assertTrue(self.check())

    def test_matching_legacy_mapping_remains_reachable(self):
        self.legacy(123)
        self.assertTrue(self.check())

    def test_stale_legacy_id_cannot_authorize_another_task_owner(self):
        self.legacy(99)
        self.assertFalse(self.check(user_id=99))

    def test_missing_canonical_owner_is_rejected(self):
        self.legacy(123)
        self.db.execute('DELETE FROM grape_accounts')
        self.assertFalse(self.check())

    def test_other_canonical_user_is_rejected(self):
        self.assertFalse(self.check(user_id=124))

    def test_offline_engine_is_rejected(self):
        self.db.execute("UPDATE agent_runtimes SET status='offline'")
        self.assertFalse(self.check())

    def test_unhealthy_engine_is_rejected(self):
        self.db.execute('UPDATE agent_runtimes SET healthy=0')
        self.assertFalse(self.check())

    def test_failed_probe_is_rejected(self):
        self.db.execute("UPDATE agent_runtimes SET probe_status='fail'")
        self.assertFalse(self.check())

    def test_stale_heartbeat_is_rejected(self):
        self.db.execute("UPDATE agent_runtimes SET last_seen_at='2000-01-01T00:00:00+00:00'")
        self.assertFalse(self.check())

    def test_wrong_tag_is_rejected(self):
        self.assertFalse(self.check(tag='qa'))

    def test_repo_scope_still_requires_same_repository(self):
        self.db.execute("UPDATE runtime_credentials SET scope='repo',owner_id='fixture/other'")
        self.assertFalse(self.check())
        self.db.execute("UPDATE runtime_credentials SET owner_id='fixture/repo'")
        self.assertTrue(self.check())


if __name__ == '__main__':
    unittest.main()
