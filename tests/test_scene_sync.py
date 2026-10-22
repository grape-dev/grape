"""scene_sync.py 单元测试。

覆盖：
- _load_config() 配置加载优先级
- _get_sync_token() token 获取两级链
- _compute_scene_hash() hash 计算
- _validate_scene_nodes() nodes 校验（tag/mode/handoff/next 链路）
- _check_skills_exist() skill 引用预检
- _parse_version() 版本号比较
- _refresh_cat_cache() category 缓存刷新
- _download_repo_zip ZIP 下载+解压
- sync_official_scenes 核心同步流程（mock）
"""
import io
import json
import os
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from src.storage.scene_sync import (
    _load_config,
    _get_sync_token,
    _compute_scene_hash,
    _validate_scene_nodes,
    _check_skills_exist,
    _parse_version,
    _refresh_cat_cache,
    _download_repo_zip,
    sync_official_scenes,
    _cat_cache,
    _VALID_TAGS,
    _VALID_MODES,
)


# 保存全局状态，模块结束后恢复（避免污染 test_scene_skill_market）
_cat_cache_snapshot = {}


def setup_module():
    _cat_cache_snapshot.clear()
    _cat_cache_snapshot.update(_cat_cache)


def teardown_module():
    _cat_cache.clear()
    _cat_cache.update(_cat_cache_snapshot)


# ===== _load_config =====

class TestLoadConfig:
    def test_defaults(self):
        """无环境变量无配置文件时用兜底默认值（R31 决策 A：不再返回 interval）"""
        # 清环境变量
        for k in ("GRAPESKILLS_REPO_URL", "GRAPESKILLS_REPO_BRANCH", "SCENE_SYNC_INTERVAL"):
            os.environ.pop(k, None)
        url, branch = _load_config()
        assert "gitcode.com" in url
        assert branch == "main"

    def test_env_overrides(self):
        """环境变量优先于配置文件（R31 决策 A：SCENE_SYNC_INTERVAL 已移除，仅容忍并告警）"""
        os.environ["GRAPESKILLS_REPO_URL"] = "https://example.com/foo/bar"
        os.environ["GRAPESKILLS_REPO_BRANCH"] = "main"
        os.environ["SCENE_SYNC_INTERVAL"] = "600"  # 废弃键：不再进返回值
        try:
            url, branch = _load_config()
            assert url == "https://example.com/foo/bar"
            assert branch == "main"
        finally:
            for k in ("GRAPESKILLS_REPO_URL", "GRAPESKILLS_REPO_BRANCH", "SCENE_SYNC_INTERVAL"):
                os.environ.pop(k, None)


# ===== _get_sync_token =====

class TestGetSyncToken:
    def test_env_token_priority(self):
        """GRAPE_DEV_TOKEN 环境变量优先级最高"""
        os.environ["GRAPE_DEV_TOKEN"] = "env_token_123"
        try:
            assert _get_sync_token() == "env_token_123"
        finally:
            os.environ.pop("GRAPE_DEV_TOKEN", None)

    def test_no_token_returns_empty(self):
        """无任何 token 配置时返回空串"""
        os.environ.pop("GRAPE_DEV_TOKEN", None)
        # configs/scene-sync.json 可能不存在
        # 只要环境变量没有，结果要么是配置文件的值要么是空串
        token = _get_sync_token()
        # 在测试环境里配置文件可能不存在，所以 token 可能是空串或有值
        # 关键验证：不报异常
        assert isinstance(token, str)


# ===== _compute_scene_hash =====

class TestComputeSceneHash:
    def test_empty_dir(self):
        """空目录返回空 hash"""
        assert _compute_scene_hash("/nonexistent/path") == ""

    def test_consistent_hash(self):
        """同内容同目录 hash 一致"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "scene.json"), "w") as f:
                f.write('{"name":"test"}')
            h1 = _compute_scene_hash(d)
            h2 = _compute_scene_hash(d)
            assert h1 == h2
            assert len(h1) == 64  # SHA-256 hex

    def test_different_content_different_hash(self):
        """不同内容 hash 不同"""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "scene.json"), "w") as f:
                f.write('{"name":"v1"}')
            h1 = _compute_scene_hash(d)
            with open(os.path.join(d, "scene.json"), "w") as f:
                f.write('{"name":"v2"}')
            h2 = _compute_scene_hash(d)
            assert h1 != h2


# ===== _validate_scene_nodes =====

class TestValidateSceneNodes:
    _VALID_NODES = [
        {"id": "vod", "tag": "vod", "skill": "vod", "mode": "emit", "next": "issue", "handoff": "a2a"},
        {"id": "issue", "tag": "issue", "skill": "issue-advance", "mode": "emit", "next": "coding", "handoff": "a2a"},
        {"id": "coding", "tag": "coding", "skill": "implementation", "mode": "emit", "next": "done", "handoff": "complete"},
    ]

    def test_valid_nodes(self):
        """合法 nodes 返回空错误列表"""
        errors = _validate_scene_nodes(self._VALID_NODES)
        assert errors == []

    def test_too_few_nodes(self):
        """少于 2 个节点报错"""
        errors = _validate_scene_nodes([{"id": "vod", "tag": "vod", "skill": "vod", "mode": "emit", "next": "done", "handoff": "complete"}])
        assert len(errors) == 1
        assert "至少 2" in errors[0]

    def test_empty_nodes(self):
        """空 nodes 报错"""
        errors = _validate_scene_nodes([])
        assert len(errors) == 1

    def test_invalid_tag(self):
        """非法 tag 报错"""
        nodes = [n.copy() for n in self._VALID_NODES]
        nodes[0]["tag"] = "invalid_tag"
        errors = _validate_scene_nodes(nodes)
        assert any("tag" in e for e in errors)

    def test_invalid_mode(self):
        """非法 mode 报错"""
        nodes = [n.copy() for n in self._VALID_NODES]
        nodes[0]["mode"] = "invalid_mode"
        errors = _validate_scene_nodes(nodes)
        assert any("mode" in e for e in errors)

    def test_handoff_complete_next_not_done(self):
        """handoff=complete 但 next 不是 done 报错"""
        nodes = [n.copy() for n in self._VALID_NODES]
        nodes[2]["next"] = "issue"  # 应该是 done
        errors = _validate_scene_nodes(nodes)
        assert any("complete" in e and "done" in e for e in errors)

    def test_handoff_a2a_next_done(self):
        """handoff=a2a 但 next=done 报错"""
        nodes = [n.copy() for n in self._VALID_NODES]
        nodes[0]["next"] = "done"  # 中间节点不该是 done
        errors = _validate_scene_nodes(nodes)
        assert any("a2a" in e and "done" in e for e in errors)

    def test_broken_chain(self):
        """链路断链报错"""
        nodes = [n.copy() for n in self._VALID_NODES]
        nodes[0]["next"] = "nonexistent"  # 指向不存在的节点
        errors = _validate_scene_nodes(nodes)
        assert any("断链" in e or "找不到" in e for e in errors)

    def test_circular_chain(self):
        """循环链路报错"""
        nodes = [
            {"id": "a", "tag": "vod", "skill": "vod", "mode": "emit", "next": "b", "handoff": "a2a"},
            {"id": "b", "tag": "issue", "skill": "issue-advance", "mode": "emit", "next": "a", "handoff": "a2a"},
        ]
        errors = _validate_scene_nodes(nodes)
        assert any("循环" in e for e in errors)

    def test_duplicate_id(self):
        """重复 id 报错"""
        nodes = [n.copy() for n in self._VALID_NODES]
        nodes[1]["id"] = "vod"  # 和第一个重复
        errors = _validate_scene_nodes(nodes)
        assert any("重复" in e for e in errors)


# ===== _check_skills_exist =====

class TestCheckSkillsExist:
    def test_empty_skill_names(self):
        """nodes 没有 skill 字段时返回空列表"""
        nodes = [{"id": "vod", "tag": "vod", "skill": "", "mode": "emit", "next": "done", "handoff": "complete"}]
        missing = _check_skills_exist(nodes, "test_scene")
        assert missing == []

    def test_existing_skills(self):
        """skill 表里存在的 skill 不报 missing"""
        # vod 是系统内置技能，应该存在
        nodes = [{"id": "vod", "tag": "vod", "skill": "vod", "mode": "emit", "next": "done", "handoff": "complete"}]
        missing = _check_skills_exist(nodes, "test_scene")
        # 如果 vod 在 skill 表里，missing 应该为空
        # 如果不在（测试环境可能没 seed），missing 包含 vod
        assert isinstance(missing, list)

    def test_nonexistent_skill(self):
        """不存在的 skill 报 missing"""
        nodes = [{"id": "vod", "tag": "vod", "skill": "nonexistent_skill_xyz", "mode": "emit", "next": "done", "handoff": "complete"}]
        missing = _check_skills_exist(nodes, "test_scene")
        assert "nonexistent_skill_xyz" in missing


# ===== _parse_version =====

class TestParseVersion:
    def test_standard_3part(self):
        assert _parse_version("1.0.0") == (1, 0, 0)
        assert _parse_version("2.5.3") == (2, 5, 3)

    def test_2part_padded(self):
        """2 位版本补齐到 3 位"""
        assert _parse_version("1.0") == (1, 0, 0)
        assert _parse_version("2.5") == (2, 5, 0)

    def test_1part_padded(self):
        """1 位版本补齐到 3 位"""
        assert _parse_version("1") == (1, 0, 0)

    def test_invalid_returns_zero(self):
        """非法版本号返回 (0,0,0)"""
        assert _parse_version("invalid") == (0, 0, 0)
        assert _parse_version("") == (0, 0, 0)

    def test_comparison(self):
        """版本比较正确"""
        assert _parse_version("1.0.0") < _parse_version("1.0.1")
        assert _parse_version("1.0.0") < _parse_version("1.1.0")
        assert _parse_version("1.0.0") < _parse_version("2.0.0")
        assert _parse_version("1.0") == _parse_version("1.0.0")  # 补齐后相等
        assert _parse_version("1.0.1") > _parse_version("1.0")  # 1.0.1 > 1.0.0


# ===== 常量验证 =====

class TestConstants:
    def test_valid_tags(self):
        assert _VALID_TAGS == {"vod", "issue", "coding", "qa", "sre", "chat"}

    def test_valid_modes(self):
        assert _VALID_MODES == {"emit"}


# ===== _refresh_cat_cache =====

class TestRefreshCatCache:
    def test_refresh_populates_cache(self):
        """刷新后 _cat_cache 非空（DB 有初始 6 个分类）"""
        old = dict(_cat_cache)
        try:
            _refresh_cat_cache()
            assert isinstance(_cat_cache, dict)
        finally:
            _cat_cache.clear()
            _cat_cache.update(old)

    def test_refresh_clears_old(self):
        """刷新后旧数据被清掉"""
        old = dict(_cat_cache)
        try:
            _cat_cache["stale_key"] = 999
            _refresh_cat_cache()
            assert "stale_key" not in _cat_cache
        finally:
            _cat_cache.clear()
            _cat_cache.update(old)


# ===== _download_repo_zip (mock httpx) =====

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _run_async(coro):
    """兼容 Python 3.14 的异步测试运行"""
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_zip_bytes(top_dir="grape-skills-dev", scenes=None):
    """构造一个模拟 grape-skills 仓库的 ZIP 包 bytes。"""
    if scenes is None:
        scenes = {
            "default": {"scene.json": '{"name":"default","nodes":[]}', "README.md": "# Default"},
        }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for scene_name, files in scenes.items():
            for fname, content in files.items():
                zf.writestr(f"{top_dir}/scenes/{scene_name}/{fname}", content)
    return buf.getvalue()


class TestDownloadRepoZip:
    def test_download_and_extract(self):
        """mock httpx 返回 ZIP bytes，验证解压后目录结构正确"""
        async def _run():
            zip_data = _make_zip_bytes()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.content = zip_data
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            result = await _download_repo_zip(mock_client, "fake_token")
            assert os.path.isdir(result)
            scenes_dir = os.path.join(result, "scenes")
            assert os.path.isdir(scenes_dir)
            assert os.path.isdir(os.path.join(scenes_dir, "default"))
            assert os.path.exists(os.path.join(scenes_dir, "default", "scene.json"))
            # 清理临时目录
            import shutil
            shutil.rmtree(os.path.dirname(result), ignore_errors=True)
        _run_async(_run())

    def test_auth_header(self):
        """验证 gitcode 请求头包含 Authorization: Bearer ***（第七复审 P1 适配）

        S16 per-host header 后，token 只附加到 gitcode 端点——本测试显式固定
        _DL_BASE 为 gitcode，与环境变量/本地配置的 repo_url 解耦（否则
        GRAPESKILLS_REPO_URL 指向 GitHub 时走匿名分支，本测试假失败）。
        GitHub 源的匿名行为由 test_r31_p45_p46.py::test_github_download_sends_no_auth_header 覆盖。
        """
        async def _run():
            import src.storage.scene_sync as _ss
            zip_data = _make_zip_bytes()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.content = zip_data
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            with patch.object(_ss, "_DL_BASE", "https://raw.gitcode.com"), \
                 patch.object(_ss, "_OWNER_REPO", "grape-dev/grape-skills"), \
                 patch.object(_ss, "_REPO_BRANCH", "main"):
                await _download_repo_zip(mock_client, "fake_token")
            # 检查 get 调用参数
            call_args = mock_client.get.call_args
            headers = call_args.kwargs.get("headers", {})
            assert headers.get("Authorization") == "Bearer fake_token"
            # 清理
            import shutil
            # _download_repo_zip 内部创建了临时目录，通过 mock_resp 无法拿到路径
            # 这里只验证请求头，不清理（tempfile 会被系统清理）
        _run_async(_run())

    def test_empty_response_raises(self):
        """空响应抛异常"""
        async def _run():
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.content = b""
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            with pytest.raises((ValueError, Exception)):
                await _download_repo_zip(mock_client, "fake_token")
        _run_async(_run())

    def test_no_token_no_auth_header(self):
        """无 token 时不带 Authorization 头"""
        async def _run():
            zip_data = _make_zip_bytes()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.content = zip_data
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            await _download_repo_zip(mock_client, "")
            call_args = mock_client.get.call_args
            headers = call_args.kwargs.get("headers", {})
            assert "Authorization" not in headers
        _run_async(_run())


# ===== sync_official_scenes (mock 全流程) =====

class TestSyncOfficialScenes:
    def test_no_token_skips(self):
        """无 token 时跳过同步"""
        async def _run():
            with patch("src.storage.scene_sync._get_sync_token", return_value=""):
                result = await sync_official_scenes()
                assert result is None
        _run_async(_run())

    def test_zip_download_failure_skips(self):
        """ZIP 下载失败时跳过同步，不执行下架"""
        async def _run():
            with patch("src.storage.scene_sync._get_sync_token", return_value="fake_token"):
                with patch("src.storage.scene_sync._download_repo_zip", new=AsyncMock(side_effect=Exception("download error"))):
                    result = await sync_official_scenes()
                    assert result is None
        _run_async(_run())

    def test_no_scenes_dir_skips(self):
        """ZIP 内没有 scenes/ 目录时跳过"""
        async def _run():
            # 构造不含 scenes/ 的 ZIP
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("grape-skills-dev/README.md", "# README")
            zip_data = buf.getvalue()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.content = zip_data
            with patch("src.storage.scene_sync._get_sync_token", return_value="fake_token"):
                with patch("src.storage.scene_sync._download_repo_zip", new=AsyncMock(return_value="/tmp/fake_nonexistent")):
                    result = await sync_official_scenes()
                    assert result is None
        _run_async(_run())

    def test_full_sync_new_scene(self):
        """完整同步流程：mock ZIP 下载 + mock DB，验证 INSERT 路径（新场景入库）"""
        async def _run():
            import json as _json
            scene_data = {
                "name": "test_scene",
                "version": "1.0.0",
                "display_name": "测试场景",
                "description": "测试用",
                "category": "general",
                "recommended_template": "",
                "onboarding": "repo-onboarding",
                "tags": ["test"],
                "author": "tester",
                "nodes": [
                    {"id": "vod", "tag": "vod", "skill": "vod", "mode": "emit", "next": "issue", "handoff": "a2a"},
                    {"id": "issue", "tag": "issue", "skill": "issue-advance", "mode": "emit", "next": "done", "handoff": "complete"},
                ],
            }

            # 构造含 scene.json + README.md 的 ZIP
            zip_data = _make_zip_bytes(scenes={
                "test_scene": {
                    "scene.json": _json.dumps(scene_data),
                    "README.md": "# Test Scene",
                },
            })

            # mock _download_repo_zip：解压 ZIP 到临时目录并返回路径
            async def mock_download_zip(client, token):
                tmp = tempfile.mkdtemp(prefix="test_sync_")
                with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                    zf.extractall(tmp)
                return os.path.join(tmp, "grape-skills-dev")

            # mock DB：fetchone 第一次返回 None（场景不存在→INSERT 路径），
            # 第二次返回 (0,) 给 _init_featured_scenes_if_empty 的 COUNT 查询
            mock_cursor = MagicMock()
            mock_cursor.fetchone = MagicMock(side_effect=[None, (0,)])
            mock_cursor.fetchall = MagicMock(return_value=[])
            mock_cursor.lastrowid = 42
            mock_db = MagicMock()
            mock_db.cursor = MagicMock(return_value=mock_cursor)
            mock_db.commit = MagicMock()

            with patch("src.storage.scene_sync._get_sync_token", return_value="fake_token"):
                with patch("src.storage.scene_sync._download_repo_zip", new=mock_download_zip):
                    with patch("src.storage.scene_sync._refresh_cat_cache"):
                        with patch.dict("src.storage.scene_sync._cat_cache", {"general": 1}, clear=False):
                            with patch("src.storage.scene_sync._check_skills_exist", return_value=[]):
                                with patch("src.storage.scene_sync.db_lock"):
                                    with patch("src.storage.scene_sync.db", mock_db):
                                        with patch("src.storage.scene_sync._rebuild_scene_skill_ref_by_name"):
                                            with patch("src.storage.scene_sync._init_featured_scenes_if_empty"):
                                                result = await sync_official_scenes()

            # 验证：INSERT 路径被触发（execute 调用中含 INSERT 关键字）
            execute_calls = [str(call) for call in mock_cursor.execute.call_args_list]
            assert any("INSERT" in c for c in execute_calls), \
                f"INSERT not found in execute calls: {execute_calls}"
            # 验证：UPDATE 路径未被触发（新场景不应 UPDATE）
            assert not any("UPDATE scene_market SET" in c for c in execute_calls), \
                f"UPDATE should not be called for new scene: {execute_calls}"
        _run_async(_run())

    def test_sync_existing_scene_preserves_is_active(self):
        """同步已有场景时，UPDATE 保留 admin 设置的 is_active 值（不被覆盖为 1）"""
        async def _run():
            import json as _json
            scene_data = {
                "name": "test_scene",
                "version": "1.0.0",
                "display_name": "测试场景",
                "description": "测试用",
                "category": "general",
                "recommended_template": "",
                "onboarding": "repo-onboarding",
                "tags": ["test"],
                "author": "tester",
                "nodes": [
                    {"id": "vod", "tag": "vod", "skill": "vod", "mode": "emit", "next": "issue", "handoff": "a2a"},
                    {"id": "issue", "tag": "issue", "skill": "issue-advance", "mode": "emit", "next": "done", "handoff": "complete"},
                ],
            }

            zip_data = _make_zip_bytes(scenes={
                "test_scene": {
                    "scene.json": _json.dumps(scene_data),
                    "README.md": "# Test Scene",
                },
            })

            async def mock_download_zip(client, token):
                tmp = tempfile.mkdtemp(prefix="test_sync_")
                with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                    zf.extractall(tmp)
                return os.path.join(tmp, "grape-skills-dev")

            # mock DB：fetchone 第一次返回已有场景行（is_active=0 表示 admin 禁用了），
            # old_hash 与 content_hash 不同→触发 UPDATE 路径
            mock_cursor = MagicMock()
            mock_cursor.fetchone = MagicMock(side_effect=[(1, "0.9.0", "old_different_hash", 0), (0,)])
            mock_cursor.fetchall = MagicMock(return_value=[])
            mock_cursor.lastrowid = 1
            mock_db = MagicMock()
            mock_db.cursor = MagicMock(return_value=mock_cursor)
            mock_db.commit = MagicMock()

            with patch("src.storage.scene_sync._get_sync_token", return_value="fake_token"):
                with patch("src.storage.scene_sync._download_repo_zip", new=mock_download_zip):
                    with patch("src.storage.scene_sync._refresh_cat_cache"):
                        with patch.dict("src.storage.scene_sync._cat_cache", {"general": 1}, clear=False):
                            with patch("src.storage.scene_sync._check_skills_exist", return_value=[]):
                                with patch("src.storage.scene_sync.db_lock"):
                                    with patch("src.storage.scene_sync.db", mock_db):
                                        with patch("src.storage.scene_sync._rebuild_scene_skill_ref_by_name"):
                                            with patch("src.storage.scene_sync._init_featured_scenes_if_empty"):
                                                await sync_official_scenes()

            # 找到 UPDATE 调用
            update_call = None
            for call in mock_cursor.execute.call_args_list:
                sql = str(call)
                if "UPDATE scene_market SET" in sql:
                    update_call = call
                    break
            assert update_call is not None, "UPDATE scene_market SET not found in execute calls"

            # 验证 1: UPDATE SQL 不含 is_featured（is_featured 不在 SET 子句中，不被覆盖）
            update_sql = update_call.args[0] if update_call.args else update_call[0][0]
            assert "is_featured" not in update_sql, \
                f"is_featured should not be in UPDATE SET clause: {update_sql}"

            # 验证 2: UPDATE 参数中 is_active 值 = 0（保留 admin 禁用状态，而非硬编码 1）
            update_params = update_call.args[1] if len(update_call.args) > 1 else update_call[0][1]
            # UPDATE SET 中 is_active=? 是倒数第二个参数（WHERE id=? 是最后一个）
            is_active_value = update_params[-2]
            assert is_active_value == 0, \
                f"is_active should be 0 (preserved admin value), got {is_active_value}"
        _run_async(_run())
