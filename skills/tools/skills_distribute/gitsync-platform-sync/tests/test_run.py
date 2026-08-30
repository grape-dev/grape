"""Tests for the `run` one-shot command (failure isolation + product artifact)."""
import json
from unittest import mock

from gitsync.config import (ClawhubConfig, Config, DbConfig, IdentityConfig,
                            TargetConfig)
from gitsync.commands import run as run_mod
from gitsync.commands.run import run_command


def _make_config(tmp_path):
    return Config(
        identity=IdentityConfig(name="b", email="b@e.com"),
        remotes={"gitcode": "/nonexistent/repo.git",
                 "github": "/nonexistent/gh.git"},
        branches={"source_master": "master", "source_branch": "github"},
        targets=[TargetConfig(name="t", remote="github", branch="master",
                              rewrite_identity=True, match_strategy="content")],
        db=DbConfig(host="h", user="u", password="p", database="d",
                    product_dir=str(tmp_path / "products")),
        map_file=str(tmp_path / "map.txt"),
        clawhub=ClawhubConfig(
            base_url="https://clawhub.ai",
            organization="org",
            manifest_file=str(tmp_path / "manifest.json"),
            manifest_backup_file=str(tmp_path / "manifest.backup.json"),
        ),
    )


def _write_manifest(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({
        "huawei-skill": {
            "dir": "ai/maas/huawei-skill",
            "content_hash": "h",
            "versions": [{"version": "1.0.0", "status": "in_review"}],
        }
    }))


def _sync_result(target_status="ok"):
    result = mock.Mock()
    result.targets = {"t": {"status": target_status, "new_commits": 0,
                            "message": ""}}
    result.hop1_cherrypicked = []
    return result


def test_run_isolates_failures(tmp_path):
    cfg = _make_config(tmp_path)

    wt = mock.Mock()
    with mock.patch.object(run_mod, "Worktree") as W:
        W.create.return_value = wt
        with mock.patch.object(run_mod, "run_sync",
                               side_effect=Exception("sync boom")):
            with mock.patch("gitsync.commands.publish.publish_command",
                            return_value=0) as pub:
                with mock.patch("gitsync.commands.dbupdate.db_update_command",
                                return_value=0) as dbu:
                    args = mock.Mock(multica_issue="u", force=False, verbose=False)
                    rc = run_command(cfg, args)

    # sync failed but publish + db-update still executed
    pub.assert_called_once()
    dbu.assert_called_once()
    assert rc == 2


def test_run_worst_exit(tmp_path):
    cfg = _make_config(tmp_path)
    wt = mock.Mock()
    with mock.patch.object(run_mod, "Worktree") as W:
        W.create.return_value = wt
        with mock.patch.object(run_mod, "run_sync") as sync_mock:
            sync_mock.return_value = _sync_result()
            with mock.patch("gitsync.commands.publish.publish_command", return_value=2):
                with mock.patch("gitsync.commands.dbupdate.db_update_command", return_value=0):
                    args = mock.Mock(multica_issue="u", force=False, verbose=False)
                    rc = run_command(cfg, args)
    assert rc == 2  # publish failed -> worst


def test_run_product_has_urls_and_platforms(tmp_path):
    _write_manifest(tmp_path)
    cfg = _make_config(tmp_path)
    wt = mock.Mock()
    with mock.patch.object(run_mod, "Worktree") as W:
        W.create.return_value = wt
        with mock.patch.object(run_mod, "run_sync") as sync_mock:
            sync_mock.return_value = _sync_result()
            with mock.patch("gitsync.commands.publish.publish_command", return_value=0):
                with mock.patch("gitsync.commands.dbupdate.db_update_command", return_value=0):
                    with mock.patch.object(run_mod, "write_product") as wp:
                        args = mock.Mock(multica_issue="u", force=False, verbose=False)
                        rc = run_command(cfg, args)
    assert rc == 0
    wp.assert_called_once()
    data = wp.call_args[0][1]
    skill = data["skills"][0]
    assert skill["name"] == "huawei-skill"
    assert skill["url"] == skill["urls"]["github"]
    assert skill["urls"]["github"] == "/nonexistent/gh/tree/master/ai/maas/huawei-skill"
    assert skill["urls"]["gitcode"] == "/nonexistent/repo/tree/github/ai/maas/huawei-skill"
    assert skill["urls"]["clawhub"] == "https://clawhub.ai/org/skills/huawei-skill"
    assert skill["platforms"] == ["clawhub", "gitcode", "github"]


def test_run_product_written_before_db_update(tmp_path):
    _write_manifest(tmp_path)
    cfg = _make_config(tmp_path)
    wt = mock.Mock()
    order = []

    def _rec_write(*a, **k):
        order.append("write")

    def _rec_dbu(*a, **k):
        order.append("dbu")

    with mock.patch.object(run_mod, "Worktree") as W:
        W.create.return_value = wt
        with mock.patch.object(run_mod, "run_sync") as sync_mock:
            sync_mock.return_value = _sync_result()
            with mock.patch("gitsync.commands.publish.publish_command", return_value=0):
                with mock.patch("gitsync.commands.dbupdate.db_update_command",
                                side_effect=_rec_dbu):
                    with mock.patch.object(run_mod, "write_product",
                                           side_effect=_rec_write):
                        args = mock.Mock(multica_issue="u", force=False, verbose=False)
                        run_command(cfg, args)
    assert order == ["write", "dbu"]


def test_run_platforms_reflect_sync_guard_failure(tmp_path):
    _write_manifest(tmp_path)
    cfg = _make_config(tmp_path)
    wt = mock.Mock()
    with mock.patch.object(run_mod, "Worktree") as W:
        W.create.return_value = wt
        with mock.patch.object(run_mod, "run_sync") as sync_mock:
            sync_mock.return_value = _sync_result("guard_failed")
            with mock.patch("gitsync.commands.publish.publish_command", return_value=0):
                with mock.patch("gitsync.commands.dbupdate.db_update_command", return_value=0):
                    with mock.patch.object(run_mod, "write_product") as wp:
                        args = mock.Mock(multica_issue="u", force=False, verbose=False)
                        rc = run_command(cfg, args)
    assert rc == 2  # guard_failed -> worst
    data = wp.call_args[0][1]
    skill = data["skills"][0]
    # hop1 ok -> gitcode; github guard_failed -> excluded; clawhub publish ok
    assert skill["platforms"] == ["clawhub", "gitcode"]


def test_run_missing_organization_aborts(tmp_path):
    _write_manifest(tmp_path)
    cfg = _make_config(tmp_path)
    cfg.clawhub = ClawhubConfig(base_url="https://clawhub.ai")
    wt = mock.Mock()
    with mock.patch.object(run_mod, "Worktree") as W:
        W.create.return_value = wt
        with mock.patch.object(run_mod, "run_sync") as sync_mock:
            sync_mock.return_value = _sync_result()
            with mock.patch("gitsync.commands.publish.publish_command") as pub:
                with mock.patch("gitsync.commands.dbupdate.db_update_command") as dbu:
                    with mock.patch.object(run_mod, "write_product") as wp:
                        args = mock.Mock(multica_issue="u", force=False, verbose=False)
                        rc = run_command(cfg, args)
    assert rc == 2
    pub.assert_not_called()
    dbu.assert_not_called()
    wp.assert_not_called()
