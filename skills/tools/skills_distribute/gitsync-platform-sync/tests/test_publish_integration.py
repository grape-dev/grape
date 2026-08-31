"""Integration test for the publish command end-to-end (CLI mocked)."""
from unittest import mock

from gitsync.config import Config, ClawhubConfig, IdentityConfig, TargetConfig
from gitsync.commands.publish import publish_command
from gitsync.exitcodes import EXIT_OK


def _make_config(tmp_path):
    skills = tmp_path / "repo" / "skills"
    (skills / "huawei-demo").mkdir(parents=True)
    (skills / "huawei-demo" / "SKILL.md").write_text(
        "---\nname: huawei-demo\ndescription: demo\n---\n"
    )
    return Config(
        identity=IdentityConfig(name="b", email="b@e.com"),
        remotes={"gitcode": str(tmp_path / "repo")},
        branches={"source_master": "master", "source_branch": "github"},
        targets=[TargetConfig(name="t", remote="github", branch="master",
                              rewrite_identity=True, match_strategy="content")],
        clawhub=ClawhubConfig(
            skills_dir="skills",
            manifest_file=str(tmp_path / "manifest.json"),
            manifest_backup_file=str(tmp_path / "manifest.backup.json"),
        ),
        map_file="",
    )


def test_publish_publishes_and_writes_manifest(tmp_path):
    cfg = _make_config(tmp_path)
    # skills_root points directly at the skills dir (bypass clone in test)
    with mock.patch("gitsync.commands.publish.skill_publish",
                    return_value={"ok": True}):
        rc = publish_command(cfg, mock.Mock(), skills_root=str(tmp_path / "repo" / "skills"))
    assert rc == EXIT_OK

    from gitsync.manifest import Manifest
    m = Manifest(cfg.clawhub.manifest_file, cfg.clawhub.manifest_backup_file)
    m.load()
    assert m.highest_version("huawei-demo") == "1.0.0"
    assert m.versions("huawei-demo")[0]["status"] == "in_review"


def test_publish_second_run_bumps_version(tmp_path):
    cfg = _make_config(tmp_path)
    root = str(tmp_path / "repo" / "skills")

    calls = []
    def fake_publish(folder, version, config_path):
        calls.append(version)
        if version == "1.0.0":
            return {"error": "Version 1.0.0 already exists.", "returncode": 1}
        return {"ok": True}

    with mock.patch("gitsync.commands.publish.skill_publish",
                    side_effect=fake_publish):
        rc = publish_command(cfg, mock.Mock(), skills_root=root)
    assert rc == EXIT_OK
    assert "1.0.1" in calls
