"""Issue-39 方案 B：VOD 场景行为测试。

覆盖：
- T12/T14 _inject_vod_scenes 在仓库有场景时注入场景表 + 无 default 约束
- T13 _inject_vod_scenes 仓库无场景时输出「未配置场景」终止约束
- T15-T17 event_pipeline VOD done handler（recommended_scene 写入 / default 不写 / terminate 清空）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402

# _inject_vod_scenes was removed in weekly-02 prompt_builder refactor;
# T12-T14 are skipped if the function is not available.
try:
    from src.prompt_builder import _inject_vod_scenes  # noqa: E402
except ImportError:
    _inject_vod_scenes = None


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    """先 init 全部基础表（issue_state 等），供 VOD done 测试依赖。"""
    from src.api import db_setup
    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean_scene(data=None):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM devops_pipeline_scene")
        except Exception:
            pass
        try:
            c.execute("DELETE FROM issue_state")
        except Exception:
            pass
        db.commit()
    yield


def _add_repo_scene(repo_key, scene_id, name="自定义场景", nodes=None):
    from src.api.pipeline_config import create_custom_scene
    create_custom_scene(scene_id, name, "", repo_key, nodes or [{"id": "issue", "skill": "issue-v2", "tag": "issue"}])
    return scene_id


class TestVodPromptScenes:
    @pytest.mark.skipif(_inject_vod_scenes is None,
                        reason="_inject_vod_scenes removed in weekly-02 refactor")
    def test_inject_repo_scenes(self):
        """T12：仓库有场景时 prompt 注入该场景 + 「没有 default」约束。"""
        _add_repo_scene("gitcode:o/r", "grape-scene", "grape需求")
        lines = []
        _inject_vod_scenes(lines, "vod", "gitcode:o/r")
        text = "\n".join(lines)
        assert "grape需求" in text, f"repo scene not injected: {text}"
        assert "没有 `default` 场景" in text, f"'no default' constraint missing: {text}"
        assert "grape-scene" in text, f"scene table row missing: {text}"

    @pytest.mark.skipif(_inject_vod_scenes is None,
                        reason="_inject_vod_scenes removed in weekly-02 refactor")
    def test_inject_no_scenes_terminate(self):
        """T13：仓库无场景时 prompt 含「未配置场景」终止约束。"""
        lines = []
        _inject_vod_scenes(lines, "vod", "gitcode:empty/repo")
        text = "\n".join(lines)
        assert "未配置任何开发场景" in text, f"terminate hint missing: {text}"
        assert "terminate" in text, f"terminate JSON schema missing: {text}"

    @pytest.mark.skipif(_inject_vod_scenes is None,
                        reason="_inject_vod_scenes removed in weekly-02 refactor")
    def test_inject_non_vod_skip(self):
        """T14：非 vod agent 不注入。"""
        lines = []
        _inject_vod_scenes(lines, "coding", "gitcode:o/r")
        assert lines == [], f"non-vod should not inject: {lines}"


# ===== event_pipeline VOD done 场景处理（抽取自 event_pipeline 逻辑，直接验证行为）=====


def _parse_vod_and_update(repo_key, issue_id, final_text, allow_override=True):
    """复刻 event_pipeline VOD done 场景处理逻辑（含宽松 JSON 兜底），校验最终 issue_state.scene_id。"""
    import re as _re
    from src.api.session_store import set_scene_override_flag, get_scene_override_flag, set_issue_scene, get_issue_scene
    from src.runtime.engine_select import _repo_user_id
    from src.api.event_store import parse_agent_output_json
    set_scene_override_flag(repo_key, issue_id, allow_override)
    if not get_scene_override_flag(repo_key, issue_id):
        return None
    _scene_out = parse_agent_output_json(final_text) or {}
    # 宽松兜底（与 event_pipeline 一致）
    if "recommended_scene" not in _scene_out and _scene_out.get("terminate") is None:
        _m_term = _re.search(r'"terminate"\s*:\s*true', final_text)
        _m_rec = _re.search(r'"recommended_scene"\s*:\s*"([^"]+)"', final_text)
        if _m_term:
            _scene_out["terminate"] = True
        if _m_rec:
            _scene_out["recommended_scene"] = _m_rec.group(1)
        _m_reason = _re.search(r'"reason"\s*:\s*"([^"]+)"', final_text)
        if _m_reason:
            _scene_out["reason"] = _m_reason.group(1)
    if _scene_out.get("terminate") or _scene_out.get("reason") == "未配置场景":
        _vod_uid = _repo_user_id(repo_key)
        set_issue_scene(repo_key, issue_id, "", scene_user_id=_vod_uid)
        return ""
    _rec_scene = _scene_out.get("recommended_scene", "") or ""
    if _rec_scene in ("", "default"):
        return get_issue_scene(repo_key, issue_id)[0]  # 保持原值不覆写
    _vod_uid = _repo_user_id(repo_key)
    set_issue_scene(repo_key, issue_id, _rec_scene, scene_user_id=_vod_uid)
    return _rec_scene


class TestVodDoneScene:
    def test_vod_recommended_uses_repo_scene(self):
        """T15：VOD 推荐仓库场景时写入该场景。"""
        _add_repo_scene("gitcode:o/r", "custom-abc", "grape需求")
        from src.user.db import add_managed_repo
        add_managed_repo("gitcode:o/r", "o", "r")
        out = _parse_vod_and_update(
            "gitcode:o/r", "#1",
            '{"recommended_scene": "custom-abc", "scene_reason": "匹配"}')
        assert out == "custom-abc"

    def test_vod_terminate_no_scene(self):
        """T16：VOD 输出 terminate=未配置场景 → scene 清空。"""
        from src.user.db import add_managed_repo
        add_managed_repo("gitcode:o/r", "o", "r")
        out = _parse_vod_and_update(
            "gitcode:o/r", "#1",
            '{"terminate": true, "reason": "未配置场景"}')
        assert out == ""

    def test_vod_default_never_written(self):
        """T17：VOD 输出 recommended_scene=default → 不覆写 scene_id。"""
        from src.user.db import add_managed_repo, set_repo_scene
        add_managed_repo("gitcode:o/r", "o", "r")
        set_repo_scene("gitcode:o/r", "custom-keep")
        out = _parse_vod_and_update(
            "gitcode:o/r", "#1",
            '{"recommended_scene": "default", "scene_reason": "通用"}')
        # scene 保持仓库默认场景，未写 default
        assert out != "default"
        from src.api.session_store import get_issue_scene
        scene, _ = get_issue_scene("gitcode:o/r", "#1")
        assert scene != "default"