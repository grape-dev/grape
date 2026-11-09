"""Issue #439: overlay home 条目分类构建 + 遗留旧链接清理 + 可写目录兜底测试。

覆盖：
- 可写状态目录（cron/memories 等）→ 真实目录（非 symlink/reparse point）
- lock/pid 文件（processes.json/*.lock 等）→ 真实空文件
- 只读资产（skills/auth.json）→ symlink
- 历史遗留同类旧链接（旧版本全量 symlink 策略产物）构建期清理重建
- 共享 home 中不存在的可写目录也会补建为真实目录
- ensure_overlay_writable_dirs 写前兜底替换 reparse point/链接
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.runtime.agent import _backend_for_engine  # noqa: F401,E402  先入 runtime 打破循环导入
from src.agent import hermes_acp  # noqa: E402


def _mk_src_home(tmp_path, entries):
    """构造共享 HERMES_HOME：entries = {name: "dir"|"file"}"""
    src = tmp_path / "src_home"
    src.mkdir()
    (src / "config.yaml").write_text("tool_progress: old\n")
    (src / ".env").write_text("TOKEN=x\n")
    for name, kind in entries.items():
        p = src / name
        if kind == "dir":
            p.mkdir()
            (p / "keep.txt").write_text("x")
        else:
            p.write_text("asset")
    return str(src)


class TestOverlayEntryKind:
    def test_writable_dirs_classified_real(self):
        assert hermes_acp._overlay_entry_kind("cron", None) == "dir"
        assert hermes_acp._overlay_entry_kind("memories", None) == "dir"
        assert hermes_acp._overlay_entry_kind("sessions", None) == "dir"

    def test_lock_files_classified_real_file(self):
        assert hermes_acp._overlay_entry_kind("processes.json", None) == "file"
        assert hermes_acp._overlay_entry_kind("gateway.pid", None) == "file"
        assert hermes_acp._overlay_entry_kind("state.lock", None) == "file"

    def test_readonly_dirs_classified_symlink(self):
        assert hermes_acp._overlay_entry_kind("skills", None) == "symlink"
        assert hermes_acp._overlay_entry_kind("plugins", None) == "symlink"
        assert hermes_acp._overlay_entry_kind("venv", None) == "symlink"

    def test_readonly_files_classified_symlink(self):
        assert hermes_acp._overlay_entry_kind("auth.json", None) == "symlink"


class TestBuildOverlayHome:
    def test_classification_build(self, tmp_path):
        """可写目录→真实目录；lock→真实空文件；只读资产→symlink。"""
        src = _mk_src_home(tmp_path, {
            "cron": "dir", "memories": "dir", "skills": "dir",
            "processes.json": "file", "auth.json": "file",
        })
        iso = str(tmp_path / "iso")
        os.makedirs(iso)
        hermes_acp._build_overlay_home(iso, src)

        assert os.path.isdir(os.path.join(iso, "cron"))
        assert not os.path.islink(os.path.join(iso, "cron"))
        assert os.path.isdir(os.path.join(iso, "memories"))
        assert not os.path.islink(os.path.join(iso, "memories"))
        assert os.path.islink(os.path.join(iso, "skills"))
        assert os.path.isfile(os.path.join(iso, "processes.json"))
        assert not os.path.islink(os.path.join(iso, "processes.json"))
        assert os.path.islink(os.path.join(iso, "auth.json"))
        # config.yaml / .env 覆写仍然生效
        assert os.path.isfile(os.path.join(iso, "config.yaml"))
        assert os.path.isfile(os.path.join(iso, ".env"))

    def test_writable_dir_missing_in_src_still_created(self, tmp_path):
        """共享 home 无 cron/memories 时 overlay 仍补建真实目录（引擎会写它们）。"""
        src = _mk_src_home(tmp_path, {"skills": "dir"})
        iso = str(tmp_path / "iso")
        os.makedirs(iso)
        hermes_acp._build_overlay_home(iso, src)
        for name in ("cron", "sessions", "logs", "cache", "tmp", "memories"):
            p = os.path.join(iso, name)
            assert os.path.isdir(p), f"{name} should be a real dir"
            assert not os.path.islink(p), f"{name} should not be a link"

    def test_legacy_symlinks_cleaned_and_rebuilt(self, tmp_path):
        """历史遗留：旧版本把可写目录/lock 全量 symlink —— 构建期清理重建。"""
        src = _mk_src_home(tmp_path, {
            "cron": "dir", "memories": "dir", "skills": "dir", "processes.json": "file",
        })
        iso = str(tmp_path / "iso")
        os.makedirs(iso)
        # 模拟旧版本构建产物：全量 symlink
        for entry in ("cron", "memories", "skills", "processes.json"):
            os.symlink(os.path.join(src, entry), os.path.join(iso, entry))

        hermes_acp._build_overlay_home(iso, src)

        # 可写目录与 lock 重建为真实目录/文件
        assert not os.path.islink(os.path.join(iso, "cron"))
        assert os.path.isdir(os.path.join(iso, "cron"))
        assert not os.path.islink(os.path.join(iso, "memories"))
        assert not os.path.islink(os.path.join(iso, "processes.json"))
        assert os.path.isfile(os.path.join(iso, "processes.json"))
        # 只读资产 symlink 保持/重建为 symlink
        assert os.path.islink(os.path.join(iso, "skills"))

    def test_idempotent_second_build(self, tmp_path):
        """二次构建幂等：真实目录不误删、写入内容保留。"""
        src = _mk_src_home(tmp_path, {"cron": "dir", "skills": "dir"})
        iso = str(tmp_path / "iso")
        os.makedirs(iso)
        hermes_acp._build_overlay_home(iso, src)
        marker = os.path.join(iso, "cron", "written.txt")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("task-state")
        hermes_acp._build_overlay_home(iso, src)
        assert os.path.isfile(marker)
        assert os.path.isdir(os.path.join(iso, "cron"))
        assert os.path.islink(os.path.join(iso, "skills"))

    def test_symlink_fallback_still_works(self, tmp_path):
        """symlink 失败（无权限场景模拟）→ 降级 _mirror_without_symlink 不回归（issue #416）。"""
        src = _mk_src_home(tmp_path, {"skills": "dir", "auth.json": "file"})
        iso = str(tmp_path / "iso")
        os.makedirs(iso)
        _real_symlink = os.symlink

        def _fail_symlink(src_p, dst_p, *a, **k):
            raise OSError(1314, "A required privilege is not held by the client")

        import unittest.mock as mock
        with mock.patch.object(os, "symlink", side_effect=_fail_symlink):
            hermes_acp._build_overlay_home(iso, src)
        assert os.path.isdir(os.path.join(iso, "skills"))
        assert not os.path.islink(os.path.join(iso, "skills"))
        assert os.path.isfile(os.path.join(iso, "auth.json"))
        assert not os.path.islink(os.path.join(iso, "auth.json"))


class TestRemovePathReparsePoint:
    def test_junction_removed_with_rmdir_not_rmtree(self, tmp_path, monkeypatch):
        """Windows junction（reparse point，islink=False 但 isdir=True）删除必须
        用 os.rmdir 仅删链接本身；禁止 shutil.rmtree 穿透删除共享目标目录。"""
        import shutil
        base = tmp_path / "base"
        base.mkdir()
        # 模拟 junction 目标目录（共享 HERMES_HOME 内容，必须存活）
        target = base / "target"
        target.mkdir()
        (target / "asset.txt").write_text("shared-asset")
        # 模拟 junction 链接本身：islink=False、isdir=True（reparse point）
        link = base / "link"
        link.mkdir()
        abs_link = os.path.abspath(str(link))

        # 模拟 Windows junction 的 os 语义
        monkeypatch.setattr(os.path, "islink", lambda p: False)
        monkeypatch.setattr(
            hermes_acp, "_is_reparse_point_or_link",
            lambda p: os.path.abspath(str(p)) == abs_link)

        rmtree_called = []
        monkeypatch.setattr(shutil, "rmtree",
                            lambda *a, **k: rmtree_called.append(a))

        hermes_acp._remove_path(str(link))

        # 只删链接本身（os.rmdir），绝不调用 rmtree
        assert not rmtree_called, "rmtree must not be used on reparse point dir"
        assert not os.path.exists(str(link))
        # 目标目录内容完好（共享 HERMES_HOME 不被穿透删除）
        assert os.path.isfile(str(target / "asset.txt"))

    def test_real_dir_still_rmtree(self, tmp_path, monkeypatch):
        """真实目录（非 reparse point）仍走 rmtree 删除，行为不回归。"""
        import shutil
        real = tmp_path / "real"
        real.mkdir()
        (real / "f.txt").write_text("x")
        monkeypatch.setattr(os.path, "islink", lambda p: False)
        monkeypatch.setattr(hermes_acp, "_is_reparse_point_or_link",
                            lambda p: False)
        rmtree_called = []
        real_rmtree = shutil.rmtree
        monkeypatch.setattr(
            shutil, "rmtree",
            lambda *a, **k: (rmtree_called.append(a), real_rmtree(*a, **k)))
        hermes_acp._remove_path(str(real))
        assert rmtree_called and not os.path.exists(str(real))


class TestEnsureOverlayWritableDirs:
    def test_replaces_links_with_real_dirs(self, tmp_path):
        """worker 写前兜底：存量 overlay 里可写目录仍是链接 → 就地替换真实目录。"""
        src = _mk_src_home(tmp_path, {"cron": "dir"})
        iso = str(tmp_path / "iso")
        os.makedirs(iso)
        os.symlink(os.path.join(src, "cron"), os.path.join(iso, "cron"))
        hermes_acp.ensure_overlay_writable_dirs(iso)
        assert not os.path.islink(os.path.join(iso, "cron"))
        assert os.path.isdir(os.path.join(iso, "cron"))
        # 其他可写目录也确保存在
        assert os.path.isdir(os.path.join(iso, "memories"))
        # 只读资产不受影响
        assert not os.path.exists(os.path.join(iso, "skills"))

    def test_keeps_existing_real_dirs(self, tmp_path):
        iso = str(tmp_path / "iso")
        os.makedirs(os.path.join(iso, "cron"))
        marker = os.path.join(iso, "cron", "m.txt")
        open(marker, "w").close()
        hermes_acp.ensure_overlay_writable_dirs(iso)
        assert os.path.isfile(marker)
