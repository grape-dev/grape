"""用户↔仓库平台关系：PAT 凭据作为第二来源（2026-11-14 用户口径）。

用户原话：「应该去查询系统支持哪些平台仓库，然后基于这些平台仓库查询是否管理了
对应的 PAT 凭据配置」。即关系不能只靠保存动作时写的 hooks——存量凭据、脚本写入、
旧实例写入的 PAT 都要算「已接入」，并自愈补写关系行。

本锁：
  1. 合并逻辑是纯函数，关系行 ∪ PAT 平台，去重排序；
  2. 读口按「系统支持的平台目录」逐个查凭据域 `<p>_pat`（kind=pat），不按平台名硬编码；
  3. PAT 有凭据但关系行缺失 → 幂等补写（hooks.on_pat_saved），不掩盖已撤销关系；
  4. has_any 走合并后的集合（引导门因此不再误判「未接入」）。
"""
import re
from pathlib import Path

AUTH = Path(__file__).resolve().parents[2] / "src/domains/mgmt/routes/auth.py"


def _src() -> str:
    return AUTH.read_text(encoding="utf-8")


def test_merge_helper_is_pure_and_union():
    from src.domains.mgmt.routes.auth import _merge_pat_platforms

    assert _merge_pat_platforms(["gitcode"], ["gitee"]) == ["gitcode", "gitee"]
    assert _merge_pat_platforms([], ["gitcode", "gitcode"]) == ["gitcode"]
    assert _merge_pat_platforms(["gitlab"], []) == ["gitlab"]
    assert _merge_pat_platforms(None, None) == []


def test_pat_scan_uses_supported_platform_catalog():
    src = _src()
    i = src.index("def _pat_platforms(")
    seg = src[i:src.index("def _merge_pat_platforms(")]
    assert "supported_platforms()" in seg, "必须遍历系统支持的平台目录（对象模型读口）"
    assert 'get_credential(str(user_id), str(p), kind="pat")' in seg, "必须按 kind=pat 查凭据域"
    assert 'cfg.get("token")' in seg, "有 token 才算已接入"


def test_relation_read_unions_pat_and_backfills():
    src = _src()
    i = src.index("def _repo_platform_links(")
    seg = src[i:i + 3600]
    assert "_pat_platforms(user_id)" in seg, "关系读口未接入 PAT 第二来源"
    assert "_merge_pat_platforms(active, pats)" in seg, "未合并关系行与 PAT 平台"
    assert "_backfill_pat_links(user_id, active, pats)" in seg, "缺关系行自愈补写"
    # 2026-11-16 起判据叠加「凭据可用性」：providers/has_any 走 usable 子集，
    # 不可用平台单列 needs_reauth（见 D109 §20.7 / FR-33 R7）
    assert 'bool(usable)' in seg, "has_any 未走「关系 ∩ 凭据可用」后的集合"
    assert '"needs_reauth"' in seg, "不可用平台未单列 needs_reauth"


def test_backfill_is_idempotent_and_scoped():
    src = _src()
    i = src.index("def _backfill_pat_links(")
    seg = src[i:src.index("def _repo_platform_links(")]
    assert "hooks.on_pat_saved" in seg, "补写必须走 hooks（关系表唯一写口）"
    assert "not in {str(a) for a in (active or [])}" in seg, "只补缺失项（幂等，不重复写）"
    assert re.search(r"except Exception as e:\s*\n\s*log\.warning", seg), "补写失败必须 loud 但不阻断读"