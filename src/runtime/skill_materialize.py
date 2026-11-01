# -*- coding: utf-8 -*-
"""Issue #564：技能包原子物化 + 完整性校验 + 官方包回退 + 降级状态。

服务端（event_pipeline._materialize_direct_workdir / scheduler direct 物化）与
worker（runtime/agent.py 任务执行技能物化）两侧共用。零 DB 依赖（worker 无库）。

核心语义（对齐 docs/issue-564/spec.md）：
- 原子性：先写 ``skills_root/.tmp/<uuid>/`` 临时目录，校验通过后 rename 到
  ``skills_root/<skill_name>/``；任何失败都清理 tmp，不留半写 ``.skills`` 目录。
- 完整性：SKILL.md 必须存在；技能定义清单（zip 内文件名单 / skill_files 键）逐一
  落盘；SKILL.md 正文中引用的 ``references/...`` 文件必须存在（#553：仅 SKILL.md
  存在而 references 缺失同样致降级执行）。
- 回退：主渠道物化失败 → 官方技能包（resolve_official_skill_path 指向的本地目录）
  copytree 原子落盘；仍失败 → 降级（status=degraded），调用方在事件/prompt 中标注。
"""

import os
import re
import shutil
import uuid
import zipfile

try:  # 服务端有 src.config；worker 同仓部署同样可导入。保持导入失败也不致命。
    from src.config import log as _log
except Exception:  # pragma: no cover
    import logging as _logging

    _log = _logging.getLogger(__name__)


TMP_PREFIX = ".tmp"


def _rmtree_safe(path: str):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def parse_skill_md_references(skill_md_path: str) -> list:
    """解析 SKILL.md 正文中引用的 references/ 相对路径（去重保序）。"""
    refs = []
    try:
        with open(skill_md_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return refs
    for m in re.finditer(r"(?:[\w./-]*?/)?(references/[\w./-]+\.[\w]+)", text):
        r = m.group(1).strip()
        if r and r not in refs:
            refs.append(r)
    return refs


def validate_skill_dir(skill_dir: str, expected_files=None) -> tuple:
    """校验技能目录完整性。

    - SKILL.md 必须存在；
    - expected_files（技能定义清单，相对 skill_dir 的相对路径）逐一存在；
    - SKILL.md 引用的 references/ 文件必须存在。

    返回 (ok, missing_list)。
    """
    missing = []
    if not os.path.isdir(skill_dir):
        return False, [skill_dir]
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_md):
        missing.append("SKILL.md")
    for rel in (expected_files or []):
        rel = str(rel).lstrip("./")
        if rel and rel != "SKILL.md" and not os.path.isfile(os.path.join(skill_dir, rel)):
            missing.append(rel)
    if os.path.isfile(skill_md):
        for ref in parse_skill_md_references(skill_md):
            if not os.path.isfile(os.path.join(skill_dir, ref)):
                missing.append(ref)
    return (not missing), missing


def _atomic_install(tmp_dir: str, dst_dir: str) -> None:
    """tmp_dir 原子 rename 到 dst_dir；dst 已存在且合法则幂等跳过；空/坏 dst 先删。"""
    if os.path.isdir(dst_dir):
        ok, _ = validate_skill_dir(dst_dir)
        if ok:
            _rmtree_safe(tmp_dir)
            return
        # 空/坏目标目录：删除后 rename（spec 风险表：旧空 .skills 目录 rename 冲突）
        _rmtree_safe(dst_dir)
    parent = os.path.dirname(dst_dir)
    os.makedirs(parent, exist_ok=True)
    os.rename(tmp_dir, dst_dir)


def materialize_skill_files_atomic(skills_root: str, skill_name: str, files: dict) -> tuple:
    """skill_files 文本物化（老任务格式）：tmp 写盘 → 校验 → 原子 rename。

    返回 (ok, reason)。
    """
    if not skill_name:
        return False, "empty skill_name"
    if not files:
        return False, "empty skill_files"
    os.makedirs(skills_root, exist_ok=True)
    tmp_dir = os.path.join(skills_root, f"{TMP_PREFIX}-{skill_name}-{uuid.uuid4().hex[:8]}")
    try:
        expected = []
        for rel, content in (files or {}).items():
            rel = str(rel).lstrip("./")
            dest = os.path.join(tmp_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content if isinstance(content, str) else str(content))
            expected.append(rel)
        ok, missing = validate_skill_dir(tmp_dir, expected)
        if not ok:
            _rmtree_safe(tmp_dir)
            return False, f"skill_files incomplete, missing: {', '.join(missing[:10])}"
        dst = os.path.join(skills_root, skill_name)
        _atomic_install(tmp_dir, dst)
        return True, ""
    except Exception as e:
        _rmtree_safe(tmp_dir)
        return False, f"skill_files materialize failed: {e}"


def materialize_skill_zip_atomic(skills_root: str, zip_bytes: bytes) -> tuple:
    """OBS 技能 zip 物化：tmp 解压（恢复 Unix 权限）→ 校验 → 逐技能原子 rename。

    zip 根 = {skill_name}/（可含多个顶层技能目录）。
    返回 (ok, skills_list, reason)。
    """
    if not zip_bytes:
        return False, [], "empty zip bytes"
    os.makedirs(skills_root, exist_ok=True)
    tmp_root = os.path.join(skills_root, f"{TMP_PREFIX}-zip-{uuid.uuid4().hex[:8]}")
    try:
        import io

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp_root)
            for info in zf.infolist():
                mode = info.external_attr >> 16
                if mode and not info.is_dir():
                    p = os.path.join(tmp_root, info.filename)
                    if os.path.isfile(p):
                        # 剥离 setuid/setgid/sticky 防提权（与 agent.py 旧行为一致）
                        os.chmod(p, mode & 0o777)
        top_dirs = [d for d in sorted(os.listdir(tmp_root))
                    if os.path.isdir(os.path.join(tmp_root, d))]
        if not top_dirs:
            _rmtree_safe(tmp_root)
            return False, [], "zip has no top-level skill dir"
        installed = []
        reasons = []
        for d in top_dirs:
            src = os.path.join(tmp_root, d)
            expected = []
            for root, _dirs, fnames in os.walk(src):
                for fn in fnames:
                    full = os.path.join(root, fn)
                    expected.append(os.path.relpath(full, src))
            ok, missing = validate_skill_dir(src, expected)
            if not ok:
                reasons.append(f"{d}: missing {', '.join(missing[:10])}")
                continue
            _atomic_install(src, os.path.join(skills_root, d))
            installed.append(d)
        _rmtree_safe(tmp_root)  # 清掉未通过校验的残留
        if not installed:
            return False, [], "; ".join(reasons) or "zip validation failed"
        if reasons:
            # 部分成功：成功者已落盘，失败者在 reason 中说明
            return True, installed, "; ".join(reasons)
        return True, installed, ""
    except Exception as e:
        _rmtree_safe(tmp_root)
        return False, [], f"zip materialize failed: {e}"


def materialize_skill_dir_atomic(skills_root: str, skill_name: str, src_dir: str) -> tuple:
    """本地技能目录物化：copytree 到 tmp → 校验 → 原子 rename。返回 (ok, reason)。"""
    if not skill_name or not os.path.isdir(src_dir):
        return False, f"local skill dir invalid: {src_dir}"
    os.makedirs(skills_root, exist_ok=True)
    tmp_dir = os.path.join(skills_root, f"{TMP_PREFIX}-{skill_name}-{uuid.uuid4().hex[:8]}")
    try:
        shutil.copytree(src_dir, tmp_dir)
        expected = []
        for root, _dirs, fnames in os.walk(tmp_dir):
            for fn in fnames:
                expected.append(os.path.relpath(os.path.join(root, fn), tmp_dir))
        ok, missing = validate_skill_dir(tmp_dir, expected)
        if not ok:
            _rmtree_safe(tmp_dir)
            return False, f"local skill incomplete, missing: {', '.join(missing[:10])}"
        _atomic_install(tmp_dir, os.path.join(skills_root, skill_name))
        return True, ""
    except Exception as e:
        _rmtree_safe(tmp_dir)
        return False, f"local skill materialize failed: {e}"


def materialize_skill_with_fallback(skills_root: str, skill_name: str, primary,
                                     agent_type: str = "",
                                     agent_type_fallback=None) -> tuple:
    """主渠道物化 + 官方技能包回退 + 降级状态。

    primary: 闭包，返回 (kind, payload)：
        ("zip", bytes) / ("files", dict) / ("dir", path) / ("none", reason)
    agent_type_fallback（Issue #617，可选）: 闭包 agent_type →
        {"skill_name": str, "skill_id": int, "skill_dir": str} 或 None。
        按名未命中（官方库无该技能名）时按 agent_type 回退官方检查器；
        本模块零 DB 依赖，resolver 由服务端调用方注入。
    返回 dict {"status": ok|fallback|degraded, "detail": str, "skill_dir": str,
               "skill_id": int}（skill_id 为回退检查器技能 id，0=未知）。
    """
    result = {"status": "degraded", "detail": "", "skill_dir": "", "skill_id": 0}
    try:
        kind, payload = primary() if callable(primary) else ("none", "no primary source")
    except Exception as e:
        kind, payload = "none", f"primary source error: {e}"

    ok, reason = False, ""
    if kind == "zip":
        _zip_ok, _skills, reason = materialize_skill_zip_atomic(skills_root, payload or b"")
        ok = _zip_ok
        # Issue #564 review：zip 部分成功（或全成功）都必须确认目标 skill_name
        # 属于实际 installed 列表，否则视为未物化 → 进入官方包回退链。
        if ok and skill_name and skill_name not in (_skills or []):
            ok = False
            reason = (f"target skill '{skill_name}' not in materialized list "
                      f"[{', '.join(_skills or [])}]")
        if not ok and not reason:
            reason = "zip materialize failed"
    elif kind == "files":
        ok, reason = materialize_skill_files_atomic(skills_root, skill_name, payload or {})
    elif kind == "dir":
        ok, reason = materialize_skill_dir_atomic(skills_root, skill_name, payload)

    if ok:
        result["status"] = "ok"
        result["skill_dir"] = os.path.join(skills_root, skill_name) if skill_name else skills_root
        return result

    # 主渠道失败/缺失 → 回退官方技能包（resolve_official_skill_path → 本地目录 copytree）
    _fb_reason = reason if kind != "none" else (payload or "skill source unavailable")
    try:
        from src.config import resolve_official_skill_path
        _official = resolve_official_skill_path(skill_name or "")
    except Exception as e:
        _official = ""
        _log.warning(f"skill fallback resolve failed for {skill_name}: {e}")
    if _official and os.path.isfile(_official):
        ok2, reason2 = materialize_skill_dir_atomic(
            skills_root, skill_name or os.path.basename(os.path.dirname(_official)),
            os.path.dirname(_official))
        if ok2:
            result["status"] = "fallback"
            result["detail"] = f"primary skill source failed ({_fb_reason}); " \
                               f"materialized official skill package instead"
            result["skill_dir"] = os.path.join(skills_root, skill_name)
            return result
        _fb_reason = f"{_fb_reason}; official fallback failed ({reason2})"
    else:
        _fb_reason = f"{_fb_reason}; official skill package not found"

    # Issue #617：按名未命中 → 按 agent_type 回退官方检查器（resolver 由服务端注入）
    if agent_type and callable(agent_type_fallback):
        _fb_skill = {}
        try:
            _fb_skill = agent_type_fallback(agent_type) or {}
        except Exception as e:
            _log.warning(f"agent_type fallback resolve failed for {agent_type}: {e}")
        _fb_name = str(_fb_skill.get("skill_name") or "")
        _fb_dir = str(_fb_skill.get("skill_dir") or "")
        _fb_id = int(_fb_skill.get("skill_id") or 0)
        if _fb_name and _fb_dir and os.path.isfile(os.path.join(_fb_dir, "SKILL.md")):
            ok3, reason3 = materialize_skill_dir_atomic(skills_root, _fb_name, _fb_dir)
            if ok3:
                result["status"] = "fallback"
                result["skill_dir"] = os.path.join(skills_root, _fb_name)
                result["skill_id"] = _fb_id
                result["detail"] = (
                    f"skill '{skill_name}' not found ({_fb_reason}); materialized official "
                    f"checker '{_fb_name}' for agent_type '{agent_type}' instead "
                    f"(fetch via `grape skills detail --skill_id {_fb_id}`)")
                return result
            _fb_reason = f"{_fb_reason}; agent_type checker fallback failed ({reason3})"

    result["status"] = "degraded"
    result["detail"] = _fb_reason
    return result
