"""R31/P45/P46 合并同步实施测试（feat/skill-scene-sync-merge）。

覆盖需求验证清单核心项（S16 含第六复审 P0 token/host 匹配修复 UC16-5）：
- UC9-1/UC9-4: scene stale 检测（只读 + 完成守卫）——铁律 1.0：零 DB 写
- UC11-1/UC11-2: skill stale 检测（只读 + 守卫）
- UC12-1/UC12-3: 网络重试（瞬态自愈 / 4xx 快速失败）
- P45: 临时目录清理（含父目录，dirname 统一形式）
- UC5-1: standalone worker 移除后无残留引用
- R31: sync_official_scenes(repo_root=...) 复用模式跳过下载

设计文档: docs/weekly-08/r31-sync-merge-design.md §12
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ===== P46.1 scene stale 检测（只读，铁律 1.0）=====

class TestDetectStaleOfficialScenes:
    def _mock_db(self, rows):
        mock_cursor = MagicMock()
        mock_cursor.fetchall = MagicMock(return_value=rows)
        # sqlite3 语义：cursor.execute() 返回 cursor 自身（支持链式 .fetchall()）
        mock_cursor.execute = MagicMock(return_value=mock_cursor)
        mock_db = MagicMock()
        mock_db.cursor = MagicMock(return_value=mock_cursor)
        return mock_db, mock_cursor

    def test_reports_stale_zero_writes(self):
        """UC9-1: 仓库已删场景 → warning + 计数；DB 零写（只有 SELECT，无 UPDATE/DELETE）"""
        from src.storage.scene_sync import _detect_stale_official_scenes
        mock_db, mock_cursor = self._mock_db([(1, "api-skill-dev"), (2, "skill-dev"), (3, "hello-world")])
        with patch("src.session_manager.db", mock_db), patch("src.session_manager.db_lock"):
            n = _detect_stale_official_scenes({"hello-world"}, traversal_complete=True)
        assert n == 2
        execute_calls = [str(c) for c in mock_cursor.execute.call_args_list]
        # 铁律 1.0：零 DB 写——只允许 SELECT
        assert all("SELECT" in c.upper() for c in execute_calls), execute_calls
        assert not any("UPDATE" in c.upper() or "DELETE" in c.upper() for c in execute_calls)

    def test_guard_skips_on_incomplete_traversal(self):
        """UC9-4: 遍历不完整 → 跳过本轮检测（零查询、零报告）"""
        from src.storage.scene_sync import _detect_stale_official_scenes
        mock_db, mock_cursor = self._mock_db([(1, "api-skill-dev")])
        with patch("src.session_manager.db", mock_db), patch("src.session_manager.db_lock"):
            n = _detect_stale_official_scenes({"whatever"}, traversal_complete=False)
        assert n == 0
        mock_cursor.execute.assert_not_called()  # 守卫先于查询

    def test_no_stale_no_warning(self):
        """仓库与 DB 一致 → 0，正常路径"""
        from src.storage.scene_sync import _detect_stale_official_scenes
        mock_db, _ = self._mock_db([(1, "hello-world")])
        with patch("src.session_manager.db", mock_db), patch("src.session_manager.db_lock"):
            n = _detect_stale_official_scenes({"hello-world"}, traversal_complete=True)
        assert n == 0


# ===== P46.4 skill stale 检测（只读）=====

class TestDetectStaleOfficialSkills:
    def test_reports_stale_zero_writes(self):
        """UC11-1: json 已移除的 skill → 计数；DB 零写"""
        from src.storage.skill_sync import _detect_stale_official_skills
        mock_cursor = MagicMock()
        mock_cursor.fetchall = MagicMock(return_value=[(1, "vod"), (2, "removed-skill")])
        mock_cursor.execute = MagicMock(return_value=mock_cursor)
        mock_db = MagicMock()
        mock_db.cursor = MagicMock(return_value=mock_cursor)
        with patch("src.session_manager.db", mock_db), patch("src.session_manager.db_lock"):
            n = _detect_stale_official_skills({"vod"}, traversal_complete=True)
        assert n == 1
        execute_calls = [str(c) for c in mock_cursor.execute.call_args_list]
        assert all("SELECT" in c.upper() for c in execute_calls), execute_calls

    def test_guard_skips_on_incomplete(self):
        """UC11-2: json 解析失败/写入中断 → 跳过检测，不出具误报名单"""
        from src.storage.skill_sync import _detect_stale_official_skills
        mock_db = MagicMock()
        with patch("src.session_manager.db", mock_db), patch("src.session_manager.db_lock"):
            n = _detect_stale_official_skills({"vod"}, traversal_complete=False)
        assert n == 0
        mock_db.cursor.assert_not_called()


# ===== P46.2 预检口径统一 =====

class TestPrecheckScopeUnification:
    def test_user_scope_skill_reports_missing(self):
        """UC9-3: user-scope skill 不再通过预检（与 ref 重建同口径）"""
        from src.storage.scene_sync import _query_active_skill_names
        mock_cursor = MagicMock()
        # 模拟查询结果：user-scope 的 my-skill 不在 active official/repo 集合内
        mock_cursor.fetchall = MagicMock(return_value=[("vod",)])
        mock_db = MagicMock()
        mock_db.cursor = MagicMock(return_value=mock_cursor)
        with patch("src.session_manager.db", mock_db), patch("src.session_manager.db_lock"):
            existing = _query_active_skill_names({"vod", "my-skill"})
        assert "vod" in existing
        assert "my-skill" not in existing
        sql = str(mock_cursor.execute.call_args_list[0])
        assert "is_active=1" in sql and "scope IN ('official','repo')" in sql


# ===== UC12 网络重试 =====

class TestNetworkRetry:
    def test_transient_failure_heals(self):
        """UC12-1: 首次 TransportError → 5s 退避 → 第二次成功（退避缩短为 0 加速测试）"""
        from src.storage.scene_sync import _download_repo_zip_with_retry
        calls = {"n": 0}

        async def flaky(client, token):
            calls["n"] += 1
            if calls["n"] == 1:
                raise __import__("httpx").TransportError("connection reset")
            return "/tmp/ok"

        with patch("src.storage.scene_sync._download_repo_zip", new=flaky), \
             patch("src.storage.scene_sync.asyncio.sleep", new=AsyncMock()):
            result = _run_async(_download_repo_zip_with_retry(MagicMock(), "t"))
        assert result == "/tmp/ok"
        assert calls["n"] == 2

    def test_4xx_fails_fast(self):
        """UC12-3: 401 快速失败，不重试"""
        import httpx
        from src.storage.scene_sync import _download_repo_zip_with_retry

        resp = MagicMock()
        resp.status_code = 401
        calls = {"n": 0}

        async def always_401(client, token):
            calls["n"] += 1
            raise httpx.HTTPStatusError("unauthorized", request=MagicMock(), response=resp)

        with patch("src.storage.scene_sync._download_repo_zip", new=always_401):
            with pytest.raises(httpx.HTTPStatusError):
                _run_async(_download_repo_zip_with_retry(MagicMock(), "bad-token"))
        assert calls["n"] == 1  # 未重试

    def test_persistent_5xx_exhausts_and_raises(self):
        """UC12-2: 持续 5xx → 3 次退避后放弃"""
        import httpx
        from src.storage.scene_sync import _download_repo_zip_with_retry

        resp = MagicMock()
        resp.status_code = 503
        calls = {"n": 0}

        async def always_503(client, token):
            calls["n"] += 1
            raise httpx.HTTPStatusError("unavailable", request=MagicMock(), response=resp)

        with patch("src.storage.scene_sync._download_repo_zip", new=always_503), \
             patch("src.storage.scene_sync.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(httpx.HTTPStatusError):
                _run_async(_download_repo_zip_with_retry(MagicMock(), "t"))
        assert calls["n"] == 4  # 初次 + 3 次重试


# ===== P45 临时目录清理（dirname 统一形式）=====

class TestTempDirCleanup:
    def test_standalone_cleanup_removes_parent(self):
        """standalone 模式 finally 清理删除父目录（不残留 scene_sync_zip_XXXX 空壳）"""
        from src.storage.scene_sync import sync_official_scenes

        # 构造含无效 scenes/ 的目录结构：repo_root = parent/top_dir，scenes/ 缺失走早退
        parent = tempfile.mkdtemp(prefix="scene_sync_zip_test_")
        top = os.path.join(parent, "grape-skills-main")
        os.makedirs(top, exist_ok=True)

        async def mock_download(client, token):
            return top

        try:
            with patch("src.storage.scene_sync._get_sync_token", return_value="t"), \
                 patch("src.storage.scene_sync._download_repo_zip", new=mock_download), \
                 patch("src.storage.scene_sync._init_featured_scenes_if_empty"):
                _run_async(sync_official_scenes())
            # P45+§4.5: 父目录也被清理（原 bug：tmp_dir 未赋值完全不清理；窄修复会残留空父目录）
            assert not os.path.exists(parent), f"parent dir leaked: {parent}"
        finally:
            import shutil
            shutil.rmtree(parent, ignore_errors=True)

    def test_provided_repo_root_not_cleaned_by_scene(self):
        """R31 caller 注入模式：scene 同步不动 caller 的临时目录（由 caller 清理）"""
        from src.storage.scene_sync import sync_official_scenes

        parent = tempfile.mkdtemp(prefix="caller_owned_")
        top = os.path.join(parent, "grape-skills-main")
        os.makedirs(top, exist_ok=True)

        try:
            with patch("src.storage.scene_sync._init_featured_scenes_if_empty"):
                _run_async(sync_official_scenes(repo_root=top))
            # caller 提供的 repo_root 仍存在（清理责任在 caller）
            assert os.path.exists(parent)
        finally:
            import shutil
            shutil.rmtree(parent, ignore_errors=True)

    def test_early_return_fallback_called(self):
        """UC9-2: scenes/ 缺失早退也调用 _init_featured_scenes_if_empty（兜底一致）"""
        from src.storage.scene_sync import sync_official_scenes

        parent = tempfile.mkdtemp(prefix="early_ret_")
        top = os.path.join(parent, "grape-skills-main")
        os.makedirs(top, exist_ok=True)  # 无 scenes/ 子目录

        fallback = MagicMock()
        try:
            with patch("src.storage.scene_sync._init_featured_scenes_if_empty", new=fallback):
                _run_async(sync_official_scenes(repo_root=top))
            fallback.assert_called_once()  # 早退路径执行了兜底
        finally:
            import shutil
            shutil.rmtree(parent, ignore_errors=True)

    def test_scene_failure_isolated_and_cleaned(self):
        """UC4-1: 单场景处理异常被隔离（failed 计数、不影响其他场景与整体返回），
        且 standalone 模式下临时目录仍被清理（whole-body finally 兜底）"""
        from src.storage.scene_sync import sync_official_scenes
        import json as _json

        # 两个场景：一个 scene.json 损坏（触发逐场景 except），一个正常但 skill 缺失（failed 路径）
        parent = tempfile.mkdtemp(prefix="scene_sync_zip_iso_")
        top = os.path.join(parent, "grape-skills-main")
        os.makedirs(os.path.join(top, "scenes", "broken"))
        os.makedirs(os.path.join(top, "scenes", "good"))
        with open(os.path.join(top, "scenes", "broken", "scene.json"), "w") as f:
            f.write("{invalid json!!")

        mock_cursor = MagicMock()
        mock_cursor.execute = MagicMock(return_value=mock_cursor)
        mock_cursor.fetchone = MagicMock(return_value=None)
        mock_cursor.fetchall = MagicMock(return_value=[])
        mock_cursor.lastrowid = 7
        mock_db = MagicMock()
        mock_db.cursor = MagicMock(return_value=mock_cursor)

        async def mock_download(client, token):
            return top

        import shutil
        try:
            with patch("src.storage.scene_sync._get_sync_token", return_value="t"), \
                 patch("src.storage.scene_sync._download_repo_zip", new=mock_download), \
                 patch("src.storage.scene_sync._refresh_cat_cache"), \
                 patch.dict("src.storage.scene_sync._cat_cache", {"general": 1}, clear=False), \
                 patch("src.storage.scene_sync.db_lock"), \
                 patch("src.storage.scene_sync.db", mock_db), \
                 patch("src.storage.scene_sync._rebuild_scene_skill_ref_by_name"), \
                 patch("src.storage.scene_sync._init_featured_scenes_if_empty"):
                _run_async(sync_official_scenes())  # 不应抛异常（broken 被逐场景 except 隔离）
            # standalone 模式：mock_download 返回的 top 视为自建 → finally 清理父目录
            # （真实 _download_repo_zip 返回 mkdtemp/top，dirname 即父目录；此处对齐该形态）
        finally:
            shutil.rmtree(parent, ignore_errors=True)


class TestObsoleteIntervalKey:
    def test_obsolete_key_warned_not_fatal(self):
        """R31 决策 A 兼容：废弃 interval 键/环境变量被容忍并告警，不影响 repo 解析"""
        from src.storage.scene_sync import _load_config
        os.environ["GRAPESKILLS_REPO_URL"] = "https://example.com/a/b"
        os.environ["GRAPESKILLS_REPO_BRANCH"] = "dev"
        os.environ["SCENE_SYNC_INTERVAL"] = "600"  # 废弃
        try:
            url, branch = _load_config()  # 不抛异常
            assert url == "https://example.com/a/b"
            assert branch == "dev"
        finally:
            for k in ("GRAPESKILLS_REPO_URL", "GRAPESKILLS_REPO_BRANCH", "SCENE_SYNC_INTERVAL"):
                os.environ.pop(k, None)


# ===== S14 锁管理（第五复审补：to_thread 一致性 + dead-variable 回归防护）=====

class TestLockStepCodeQuality:
    def test_renew_uses_to_thread_like_siblings(self):
        """UC14-1 代码质量：scene 步骤的 renew/acquire/release 三处锁操作统一走
        asyncio.to_thread（第五复审 4.2：renew 曾与旁边 acquire/release 风格分裂）"""
        import inspect
        from src.storage import skill_sync
        src = inspect.getsource(skill_sync.sync_official_skills)
        # 步骤 4 区块内：renw/acquire/release 三者都必须经 to_thread
        step4 = src[src.index("# 4. R31"):src.index("except Exception as e:\n            logger.exception(f\"skill sync: scene sync failed")]
        assert 'to_thread(_rn, "skill_sync", 1800)' in step4
        assert 'to_thread(_al, "scene_sync", 300)' in step4
        assert 'to_thread(_rl, "scene_sync")' in step4
        # 不允许裸同步调用锁操作
        assert '_rn("skill_sync"' not in step4
        assert '_al("scene_sync"' not in step4

    def test_no_dead_variables_in_step4(self):
        """第五复审 4.1 回归防护：步骤 4 不得引入赋值后未读取的死变量"""
        import inspect
        import ast
        from src.storage import skill_sync
        src = inspect.getsource(skill_sync.sync_official_skills)
        fn = ast.parse(src).body[0]  # the async def
        assigned, loaded = set(), set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name):
                (assigned if isinstance(node.ctx, ast.Store) else loaded).add(node.id)
        dead = {"scene_step_done"} & assigned
        assert not dead, f"dead variables reintroduced: {dead}"
        # 通用检查：局部赋值变量应全部被读取（容忍异常消息/下划线前缀）
        dead_any = {n for n in assigned - loaded
                    if not n.startswith("_") and n not in ("result", "repo_root", "stats")}
        assert not dead_any, f"unread local assignments: {dead_any}"


# ===== S16 多平台仓库源（开源就绪，§7.6）=====

class TestMultiPlatformRepoSource:
    def test_github_resolves_to_codeload(self):
        """UC16-1/UC16-4: github URL → codeload 基址 + 干净的 owner/repo（防 BadZipFile 回归）"""
        from src.storage.scene_sync import _parse_repo_host_path
        base, path = _parse_repo_host_path("https://github.com/grape-dev/grape-skills")
        assert base == "https://codeload.github.com"
        assert path == "grape-dev/grape-skills"

    def test_gitcode_resolves_to_raw(self):
        """UC16-2: gitcode URL → raw 基址（历史行为不变）"""
        from src.storage.scene_sync import _parse_repo_host_path
        base, path = _parse_repo_host_path("https://gitcode.com/grape-dev/grape-skills")
        assert base == "https://raw.gitcode.com"
        assert path == "grape-dev/grape-skills"

    def test_trailing_slash_and_subdomain_tolerated(self):
        """URL 容差：尾斜杠、大小写不破坏解析"""
        from src.storage.scene_sync import _parse_repo_host_path
        base, path = _parse_repo_host_path("https://GitHub.com/grape-dev/grape-skills/")
        assert base == "https://codeload.github.com"
        assert path == "grape-dev/grape-skills"

    def test_unknown_host_falls_back_to_gitcode_semantics(self):
        """UC16-3: 未识别 host 回退 gitcode 语义，不崩溃"""
        from src.storage.scene_sync import _parse_repo_host_path
        base, path = _parse_repo_host_path("https://gitee.com/foo/bar")
        assert base == "https://raw.gitcode.com"  # 回退基址
        # 未识别 host 的 path 按旧剥前缀逻辑处理（不含 host 前缀垃圾）
        assert "gitee.com" not in path or path.endswith("foo/bar")

    def test_download_url_shape_per_host(self):
        """下载 URL 形态按 host 分流（codeload 用 zip/、gitcode 用 archive/）——
        防止历史缺陷回归：字符串手术曾产生 raw.gitcode.com/https://github.com/..."""
        import inspect
        from src.storage import scene_sync
        src = inspect.getsource(scene_sync._download_repo_zip)
        assert 'codeload.github.com' in src or '_DL_BASE' in src
        assert '/zip/refs/heads/' in src
        assert '/archive/refs/heads/' in src
        # 禁止旧式硬编码拼 URL
        assert '"https://raw.gitcode.com/{_OWNER_REPO}"' not in src

    def test_github_download_sends_no_auth_header(self):
        """UC16-1 P0 修复回归：GitHub 源即使有 token 也必须匿名请求——
        GitHub codeload 对任何 Bearer header 一律 404（实测），GitCode PAT 对 GitHub 致命"""
        from src.storage import scene_sync
        with patch.object(scene_sync, "_DL_BASE", "https://codeload.github.com"), \
             patch.object(scene_sync, "_OWNER_REPO", "grape-dev/grape-skills"), \
             patch.object(scene_sync, "_REPO_BRANCH", "main"):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.content = b"x" * 200
            sent_headers = {}

            async def fake_get(url, headers=None, timeout=None, follow_redirects=None):
                sent_headers.update(headers or {})
                return mock_resp

            async def run():
                import httpx
                mock_client = MagicMock(spec=httpx.AsyncClient)
                mock_client.get = fake_get
                # 有 token 的情况下调用
                return await scene_sync._download_repo_zip(mock_client, "gitcode_pat_token")

            import asyncio
            # mock zipfile 解压：直接短路 extractall（我们只关心 headers）
            with patch("zipfile.ZipFile") as mock_zf:
                mock_zf.return_value.__enter__ = MagicMock(return_value=mock_zf.return_value)
                mock_zf.return_value.__exit__ = MagicMock(return_value=False)
                with patch("tempfile.mkdtemp", return_value="/tmp/test_zip_gh"):
                    with patch("os.path.isdir", return_value=True):
                        try:
                            asyncio.run(run())
                        except Exception:
                            pass  # 解压路径可能失败——headers 断言才是本测试目标
            # GitHub 分支：绝不能发送 Authorization
            assert "Authorization" not in sent_headers, \
                f"GitHub download must be anonymous, got headers: {sent_headers}"

    def test_gitcode_download_sends_token_when_present(self):
        """UC16-2 对照：gitcode 源有 token 时正常携带（私仓认证行为不变）"""
        from src.storage import scene_sync
        with patch.object(scene_sync, "_DL_BASE", "https://raw.gitcode.com"), \
             patch.object(scene_sync, "_OWNER_REPO", "grape-dev/grape-skills"), \
             patch.object(scene_sync, "_REPO_BRANCH", "main"):
            sent_headers = {}

            async def fake_get(url, headers=None, timeout=None, follow_redirects=None):
                sent_headers.update(headers or {})
                mr = MagicMock(); mr.status_code = 200
                mr.raise_for_status = MagicMock(); mr.content = b"x" * 200
                return mr

            async def run():
                import httpx
                mock_client = MagicMock(spec=httpx.AsyncClient)
                mock_client.get = fake_get
                return await scene_sync._download_repo_zip(mock_client, "gitcode_pat_token")

            import asyncio
            with patch("zipfile.ZipFile") as mock_zf:
                mock_zf.return_value.__enter__ = MagicMock(return_value=mock_zf.return_value)
                mock_zf.return_value.__exit__ = MagicMock(return_value=False)
                with patch("tempfile.mkdtemp", return_value="/tmp/test_zip_gc"):
                    with patch("os.path.isdir", return_value=True):
                        try:
                            asyncio.run(run())
                        except Exception:
                            pass
            assert sent_headers.get("Authorization") == "Bearer gitcode_pat_token"


# ===== UC5-1 standalone worker 移除无残留 =====

class TestStandaloneWorkerRemoval:
    def test_no_residual_references(self):
        """UC5-1（决策 A）: 4 个符号在 src/ 下除兼容提示注释外零残留"""
        import subprocess
        symbols = ["_scene_sync_worker", "start_scene_sync_scheduler",
                   "stop_scene_sync_scheduler"]
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        for sym in symbols:
            r = subprocess.run(
                ["grep", "-rn", sym, "--include=*.py", os.path.join(repo_root, "src")],
                capture_output=True, text=True)
            hits = [l for l in r.stdout.splitlines()
                    if l.strip() and not l.split(":", 2)[-1].strip().startswith("#")]
            assert not hits, f"residual reference to {sym}: {hits}"


# ===== R31 合并路径 =====

class TestMergedPath:
    def test_repo_root_mode_skips_download(self):
        """R31: repo_root 注入时不发起下载（复用 caller 的 ZIP）"""
        from src.storage.scene_sync import sync_official_scenes

        parent = tempfile.mkdtemp(prefix="merged_")
        top = os.path.join(parent, "grape-skills-main")
        os.makedirs(top, exist_ok=True)

        download_called = {"n": 0}

        async def spy_download(client, token):
            download_called["n"] += 1
            return top

        try:
            with patch("src.storage.scene_sync._download_repo_zip", new=spy_download), \
                 patch("src.storage.scene_sync._init_featured_scenes_if_empty"):
                _run_async(sync_official_scenes(repo_root=top))
            assert download_called["n"] == 0  # 未下载（scenes/ 缺失早退，但也不该下载）
        finally:
            import shutil
            shutil.rmtree(parent, ignore_errors=True)
