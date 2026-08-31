"""Tests for db-update with a mocked MySQL connection."""
from unittest import mock

from gitsync.config import Config, DbConfig, IdentityConfig, TargetConfig
from gitsync.commands import dbupdate
from gitsync.commands.dbupdate import db_update_command
from gitsync.product import write_product


def _make_config(tmp_path, product_dir):
    return Config(
        identity=IdentityConfig(name="b", email="b@e.com"),
        remotes={},
        branches={"source_master": "master", "source_branch": "github"},
        targets=[TargetConfig(name="t", remote="r", branch="m",
                              rewrite_identity=True, match_strategy="content")],
        db=DbConfig(host="h", user="u", password="p", database="d",
                    product_dir=str(product_dir)),
        map_file="",
    )


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "t_skills_dev_requirement" in sql:
            return len(self._rows)
        return self.rowcount

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        pass


def _run(cfg, rows, multica="uuid", target=None):
    fake = _FakeConn(_FakeCursor(rows))
    args = mock.Mock(multica_issue=multica, target=target)
    with mock.patch("pymysql.connect", return_value=fake):
        rc = db_update_command(cfg, args)
    return rc, fake


def test_db_update_success(tmp_path):
    write_product(str(tmp_path), {
        "skills": [{"name": "huawei-skill", "url": "https://x/tree/main/p",
                    "version": "1.0.0"}]
    })
    cfg = _make_config(tmp_path, tmp_path)
    rc, fake = _run(cfg, [(42, "huawei-skill")])
    assert rc == 0
    assert fake.committed
    sqls = [s for s, _ in fake._cursor.executed]
    assert any("t_skills_dev_flow" in s and "platform_github_status" in s for s in sqls)
    assert any("platform_clawhub_status" in s for s in sqls)


def _updates(fake):
    """Return {platform: (sql, url_param)} for executed UPDATE statements."""
    out = {}
    for sql, params in fake._cursor.executed:
        if "UPDATE t_skills_dev_flow" not in sql:
            continue
        for plat in ("github", "gitcode", "clawhub"):
            if f"platform_{plat}_status" in sql:
                out[plat] = (sql, params[0])
    return out


def test_db_update_uses_per_target_url(tmp_path):
    write_product(str(tmp_path), {
        "skills": [{
            "name": "huawei-skill",
            "url": "https://gh/tree/main/p",
            "urls": {
                "github": "https://gh/tree/main/p",
                "gitcode": "https://gc/tree/github/p",
                "clawhub": "https://clawhub.ai/org/skills/huawei-skill",
            },
            "platforms": ["github", "gitcode", "clawhub"],
        }]
    })
    cfg = _make_config(tmp_path, tmp_path)
    rc, fake = _run(cfg, [(42, "huawei-skill")])
    assert rc == 0
    updates = _updates(fake)
    assert updates["github"][1] == "https://gh/tree/main/p"
    assert updates["gitcode"][1] == "https://gc/tree/github/p"
    assert updates["clawhub"][1] == "https://clawhub.ai/org/skills/huawei-skill"


def test_db_update_skips_unpublished_platform(tmp_path):
    write_product(str(tmp_path), {
        "skills": [{
            "name": "huawei-skill",
            "url": "https://gh/tree/main/p",
            "urls": {"github": "https://gh/tree/main/p",
                     "gitcode": "https://gc/tree/github/p",
                     "clawhub": "https://clawhub.ai/org/skills/huawei-skill"},
            "platforms": ["github", "gitcode"],
        }]
    })
    cfg = _make_config(tmp_path, tmp_path)
    rc, fake = _run(cfg, [(42, "huawei-skill")])
    assert rc == 0
    updates = _updates(fake)
    assert set(updates) == {"github", "gitcode"}
    assert "clawhub" not in updates


def test_db_update_legacy_product_updates_all(tmp_path):
    write_product(str(tmp_path), {
        "skills": [{"name": "huawei-skill", "url": "https://x/tree/main/p"}]
    })
    cfg = _make_config(tmp_path, tmp_path)
    rc, fake = _run(cfg, [(42, "huawei-skill")])
    assert rc == 0
    updates = _updates(fake)
    assert set(updates) == {"github", "gitcode", "clawhub"}
    for sql, url in updates.values():
        assert url == "https://x/tree/main/p"


def test_db_update_no_platform_matches(tmp_path):
    write_product(str(tmp_path), {
        "skills": [{
            "name": "huawei-skill",
            "url": "https://gh/tree/main/p",
            "urls": {"github": "https://gh/tree/main/p",
                     "gitcode": "https://gc/tree/github/p",
                     "clawhub": "https://clawhub.ai/org/skills/huawei-skill"},
            "platforms": ["github"],
        }]
    })
    cfg = _make_config(tmp_path, tmp_path)
    rc, fake = _run(cfg, [(42, "huawei-skill")], target="clawhub")
    assert rc == 1  # EXIT_DIFF: requested clawhub not in product platforms
    assert not fake.committed


def test_db_update_target_only(tmp_path):
    write_product(str(tmp_path), {
        "skills": [{"name": "huawei-skill", "url": "https://x/tree/main/p"}]
    })
    cfg = _make_config(tmp_path, tmp_path)
    rc, fake = _run(cfg, [(42, "huawei-skill")], target="clawhub")
    assert rc == 0
    sqls = [s for s, _ in fake._cursor.executed]
    assert any("platform_clawhub_status" in s for s in sqls)
    assert not any("platform_github_status" in s for s in sqls)


def test_db_update_dirty_data(tmp_path):
    cfg = _make_config(tmp_path, tmp_path)
    rc, fake = _run(cfg, [(1, "a"), (2, "b")])
    assert rc == 2
    assert not fake.committed


def test_db_update_skill_not_in_product(tmp_path):
    write_product(str(tmp_path), {"skills": []})
    cfg = _make_config(tmp_path, tmp_path)
    rc, _ = _run(cfg, [(7, "missing-skill")])
    assert rc == 1


def test_db_update_invalid_target_rejected(tmp_path):
    write_product(str(tmp_path), {
        "skills": [{"name": "huawei-skill", "url": "https://x/tree/main/p"}]
    })
    cfg = _make_config(tmp_path, tmp_path)
    rc, _ = _run(cfg, [(42, "huawei-skill")],
                 target="github'; DROP TABLE t_skills_dev_flow; --")
    assert rc == 2
    assert not _.committed
