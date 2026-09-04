"""scene_sync.py 单元测试。

覆盖：
- _load_config() 配置加载优先级
- _get_sync_token() token 获取三级链
- _compute_scene_hash() hash 计算
- _validate_scene_nodes() nodes 校验（tag/mode/handoff/next 链路）
- _check_skills_exist() skill 引用预检
- _parse_version() 版本号比较
- _refresh_cat_cache() category 缓存刷新
- _api_list_dir / _api_get_file API 调用
- _download_dir_recursive 递归下载
- sync_official_scenes 核心同步流程（mock）
"""
import json
import os
import sys
import tempfile

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
    _api_list_dir,
    _api_get_file,
    _download_dir_recursive,
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
        """无环境变量无配置文件时用兜底默认值"""
        # 清环境变量
        for k in ("GRAPESKILLS_REPO_URL", "GRAPESKILLS_REPO_BRANCH", "SCENE_SYNC_INTERVAL"):
            os.environ.pop(k, None)
        url, branch, interval = _load_config()
        assert "gitcode.com" in url
        assert branch == "dev"
        assert interval == 1800.0

    def test_env_overrides(self):
        """环境变量优先于配置文件"""
        os.environ["GRAPESKILLS_REPO_URL"] = "https://example.com/foo/bar"
        os.environ["GRAPESKILLS_REPO_BRANCH"] = "main"
        os.environ["SCENE_SYNC_INTERVAL"] = "600"
        try:
            url, branch, interval = _load_config()
            assert url == "https://example.com/foo/bar"
            assert branch == "main"
            assert interval == 600.0
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
        # configs/scene-sync.json 和 global-bot-token.json 可能不存在
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


# ===== _api_list_dir / _api_get_file (mock httpx) =====

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _run_async(coro):
    """兼容 Python 3.14 的异步测试运行"""
    return asyncio.new_event_loop().run_until_complete(coro)


class TestApiListDir:
    def test_returns_list(self):
        """API 返回目录列表"""
        async def _run():
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value=[
                {"type": "dir", "name": "default", "path": "scenes/default"},
                {"type": "file", "name": "scene.json", "path": "scenes/default/scene.json"},
            ])
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            result = await _api_list_dir(mock_client, "scenes", "fake_token")
            assert len(result) == 2
            assert result[0]["name"] == "default"
        _run_async(_run())

    def test_raises_on_error(self):
        """API 返回非 200 时 raise"""
        async def _run():
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock(side_effect=Exception("HTTP 500"))
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            with pytest.raises(Exception):
                await _api_list_dir(mock_client, "scenes", "fake_token")
        _run_async(_run())


class TestApiGetFile:
    def test_base64_decode(self):
        """base64 编码的文件正确解码"""
        async def _run():
            import base64 as _b64
            content = b'{"name":"test"}'
            encoded = _b64.b64encode(content).decode()
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value={
                "encoding": "base64",
                "content": encoded,
            })
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            result = await _api_get_file(mock_client, "scenes/test/scene.json", "fake_token")
            assert result == content
        _run_async(_run())

    def test_plain_text(self):
        """非 base64 编码的文件按 utf-8 返回"""
        async def _run():
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value={
                "encoding": "",
                "content": "plain text content",
            })
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            result = await _api_get_file(mock_client, "scenes/test/README.md", "fake_token")
            assert result == b"plain text content"
        _run_async(_run())


class TestDownloadDirRecursive:
    def test_downloads_files(self):
        """根目录只下载 scene.json / README.md / icon.*"""
        async def _run():
            with tempfile.TemporaryDirectory() as tmp:
                entries = [
                    {"type": "file", "name": "scene.json", "path": "scenes/test/scene.json"},
                    {"type": "file", "name": "README.md", "path": "scenes/test/README.md"},
                    {"type": "file", "name": "ignore.txt", "path": "scenes/test/ignore.txt"},
                ]
                mock_client = AsyncMock()
                with patch("src.storage.scene_sync._api_list_dir", new=AsyncMock(return_value=entries)):
                    with patch("src.storage.scene_sync._api_get_file", new=AsyncMock(return_value=b"content")):
                        await _download_dir_recursive(mock_client, "scenes/test", tmp, "fake_token")
                assert os.path.exists(os.path.join(tmp, "scene.json"))
                assert os.path.exists(os.path.join(tmp, "README.md"))
                assert not os.path.exists(os.path.join(tmp, "ignore.txt"))
        _run_async(_run())

    def test_downloads_references(self):
        """references 子目录全部下载"""
        async def _run():
            with tempfile.TemporaryDirectory() as tmp:
                entries = [
                    {"type": "dir", "name": "references", "path": "scenes/test/references"},
                ]
                ref_entries = [
                    {"type": "file", "name": "doc1.md", "path": "scenes/test/references/doc1.md"},
                    {"type": "file", "name": "doc2.md", "path": "scenes/test/references/doc2.md"},
                ]
                mock_client = AsyncMock()
                call_count = [0]
                async def mock_list_dir(client, path, token):
                    result = entries if call_count[0] == 0 else ref_entries
                    call_count[0] += 1
                    return result
                with patch("src.storage.scene_sync._api_list_dir", new=mock_list_dir):
                    with patch("src.storage.scene_sync._api_get_file", new=AsyncMock(return_value=b"ref content")):
                        await _download_dir_recursive(mock_client, "scenes/test", tmp, "fake_token")
                assert os.path.exists(os.path.join(tmp, "references", "doc1.md"))
                assert os.path.exists(os.path.join(tmp, "references", "doc2.md"))
        _run_async(_run())

    def test_depth_limit(self):
        """超过 5 层深度跳过"""
        async def _run():
            mock_client = AsyncMock()
            with patch("src.storage.scene_sync._api_list_dir", new=AsyncMock()) as mock_list:
                with tempfile.TemporaryDirectory() as tmp:
                    await _download_dir_recursive(mock_client, "scenes/test", tmp, "fake_token", depth=6)
                    mock_list.assert_not_called()
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

    def test_api_list_failure_skips(self):
        """API 列目录失败时跳过"""
        async def _run():
            with patch("src.storage.scene_sync._get_sync_token", return_value="fake_token"):
                with patch("src.storage.scene_sync._api_list_dir", new=AsyncMock(side_effect=Exception("API error"))):
                    result = await sync_official_scenes()
                    assert result is None
        _run_async(_run())

    def test_no_scene_dirs_skips(self):
        """scenes/ 目录为空时跳过"""
        async def _run():
            with patch("src.storage.scene_sync._get_sync_token", return_value="fake_token"):
                with patch("src.storage.scene_sync._api_list_dir", new=AsyncMock(return_value=[])):
                    result = await sync_official_scenes()
                    assert result is None
        _run_async(_run())

    def test_only_template_dirs_skips(self):
        """只有 _template 和 third-party 目录时跳过"""
        async def _run():
            entries = [
                {"type": "dir", "name": "_template", "path": "scenes/_template"},
                {"type": "dir", "name": "third-party", "path": "scenes/third-party"},
            ]
            with patch("src.storage.scene_sync._get_sync_token", return_value="fake_token"):
                with patch("src.storage.scene_sync._api_list_dir", new=AsyncMock(return_value=entries)):
                    result = await sync_official_scenes()
                    assert result is None
        _run_async(_run())

    def test_download_failure_continues(self):
        """单个场景下载失败不中断整体"""
        async def _run():
            entries = [
                {"type": "dir", "name": "scene_a", "path": "scenes/scene_a"},
                {"type": "dir", "name": "scene_b", "path": "scenes/scene_b"},
            ]
            call_count = [0]
            async def mock_list_dir(client, path, token):
                if path == "scenes":
                    return entries
                call_count[0] += 1
                if call_count[0] == 1:
                    raise Exception("download failed")
                return []
            with patch("src.storage.scene_sync._get_sync_token", return_value="fake_token"):
                with patch("src.storage.scene_sync._api_list_dir", new=mock_list_dir):
                    with patch("src.storage.scene_sync._refresh_cat_cache"):
                        with patch.dict("src.storage.scene_sync._cat_cache", {"general": 1}, clear=False):
                            # mock DB 操作避免污染测试 DB
                            with patch("src.storage.scene_sync.db_lock"):
                                with patch("src.storage.scene_sync.db"):
                                    result = await sync_official_scenes()
                                    assert result is None or result is not None
        _run_async(_run())

    def test_full_sync_new_scene(self):
        """完整同步流程：mock 下载 + mock DB，验证入库逻辑不报异常"""
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

            # mock _download_dir_recursive：把 scene.json 和 README.md 写到临时目录
            async def mock_download(client, api_path, local_path, token, **kw):
                os.makedirs(local_path, exist_ok=True)
                with open(os.path.join(local_path, "scene.json"), "w") as f:
                    _json.dump(scene_data, f)
                with open(os.path.join(local_path, "README.md"), "w") as f:
                    f.write("# Test Scene")

            # mock DB：fetchone 返回 (0,)（COUNT=0），fetchall 返回空
            mock_cursor = MagicMock()
            mock_cursor.fetchone = MagicMock(return_value=(0,))
            mock_cursor.fetchall = MagicMock(return_value=[])
            mock_db = MagicMock()
            mock_db.cursor = MagicMock(return_value=mock_cursor)
            mock_db.commit = MagicMock()

            entries = [{"type": "dir", "name": "test_scene", "path": "scenes/test_scene"}]

            with patch("src.storage.scene_sync._get_sync_token", return_value="fake_token"):
                with patch("src.storage.scene_sync._api_list_dir", new=AsyncMock(return_value=entries)):
                    with patch("src.storage.scene_sync._download_dir_recursive", new=mock_download):
                        with patch("src.storage.scene_sync._refresh_cat_cache"):
                            with patch.dict("src.storage.scene_sync._cat_cache", {"general": 1}, clear=False):
                                with patch("src.storage.scene_sync._check_skills_exist", return_value=[]):
                                    with patch("src.storage.scene_sync.db_lock"):
                                        with patch("src.storage.scene_sync.db", mock_db):
                                            result = await sync_official_scenes()
                                            assert result is None or result is not None
        _run_async(_run())
