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

FALLBACK_FILENAME = "FALLBACK.md"

# Issue #1070：跨技能前缀引用一次性 INFO 日志去重（同进程同技能只记一次）。
_CROSS_SKILL_LOGGED: set = set()


def _extract_cross_skill_prefix(ref: str, skill_name: str) -> str:
    """Issue #1070：判定 references 引用是否带跨技能前缀。

    返回非空串 = 跨技能前缀（如 ``other-skill/``），应从本技能校验清单排除；
    返回空串 = 本技能自有引用（无前缀，或前缀即本技能自身目录名）。
    """
    head = ref.split("references/", 1)[0]
    if not head:
        return ""
    owner = head.rstrip("/").rsplit("/", 1)[-1]
    if owner and owner != skill_name:
        return owner
    return ""


def _rmtree_safe(path: str):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def parse_skill_md_references(skill_md_path: str, skill_name: str = "") -> list:
    """解析 SKILL.md 正文中引用的 references/ 相对路径（去重保序）。

    Issue #1070：带跨技能前缀的引用（如 ``other-skill/references/x.md``，前缀
    技能目录 ≠ 本技能自身目录）不在本技能 zip 包内（设计如此，运行时经
    ``grape skills detail`` / 平台 API 按需获取），直接排除出本技能校验清单，
    并记一次性 INFO 日志；仅对本技能自有 references 文件保留校验。
    """
    refs = []
    skill_name = (skill_name
                  or os.path.basename(os.path.dirname(os.path.abspath(skill_md_path)))
                  or "").strip()
    skipped = 0
    try:
        with open(skill_md_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return refs
    for m in re.finditer(r"(?:([\w.-]+)/)?(references/[\w./-]+\.[\w]+)", text):
        prefix = m.group(1) or ""
        r = m.group(2).strip()
        if not r:
            continue
        if prefix and prefix != skill_name:
            # 跨技能前缀引用（含 ``docs/references/...`` 等路径段前缀）：跳过校验
            skipped += 1
            continue
        if r not in refs:
            refs.append(r)
    if skipped and skill_name:
        key = f"{skill_name}:{skill_md_path}"
        if key not in _CROSS_SKILL_LOGGED:
            _CROSS_SKILL_LOGGED.add(key)
            _log.info(f"[skill] skipped {skipped} cross-skill refs for skill {skill_name}")
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


def write_skill_fallback(skills_root: str, skill_name: str,
                         reason: str = "", stage: str = "") -> bool:
    """Issue #607：技能物化失败兜底——向 ``skills_root/<skill>/FALLBACK.md`` 写降级指引。

    worker（runtime/agent.py _execute_task）与 direct（api/event_pipeline.py
    _materialize_direct_workdir）两侧共用（单一实现两端复用）。

    语义：
    - 目录已有 SKILL.md 时不写（物化成功/官方包回退成功路径不受影响）；
    - 写入成功 → log.warning 结构化留痕（tag=skill、技能名、stage、原因），
      供统计间歇性物化失败率；
    - 自身失败仅 log.error 静默降级，绝不影响主任务执行；
    - 指引口径与任务下发头 [Skill Materialization Notice] 一致
      （均为 ``grape skills detail`` 权威取回）。
    """
    if not skill_name:
        return False
    dst = os.path.join(skills_root, skill_name)
    try:
        if os.path.isfile(os.path.join(dst, "SKILL.md")):
            return False
        os.makedirs(dst, exist_ok=True)
        content = (
            "# Skill Materialization Fallback Notice\n\n"
            "This is NOT a SKILL.md - it is a degraded-delivery notice (Issue #607).\n"
            "Skill materialization failed and this directory has no skill content.\n\n"
            f"- Skill name: {skill_name}\n"
            f"- Failed stage: {stage or 'unknown'}\n"
            f"- Failure reason: {reason or 'unknown'}\n"
            f"- Local directory: {dst}\n\n"
            "## How to recover\n\n"
            "Fetch the authoritative skill content with:\n\n"
            f"    grape skills detail --skill {skill_name}\n\n"
            "(or call the platform skills API), then follow the retrieved SKILL.md\n"
            "instructions. This matches the [Skill Materialization Notice] in the\n"
            "task prompt, which refers to the same `grape skills detail` path.\n"
        )
        with open(os.path.join(dst, FALLBACK_FILENAME), "w", encoding="utf-8") as f:
            f.write(content)
        _log.warning(f"[skill] fallback file written: skill={skill_name} "
                     f"stage={stage or 'unknown'} dir={dst} reason={reason or 'unknown'}")
        return True
    except Exception as e:
        _log.error(f"[skill] fallback file write failed: skill={skill_name} "
                   f"stage={stage}: {e}")
        return False


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


def _scan_materialized_skills(skill_dir: str) -> list:
    """扫描 skill_dir 下已物化（含 SKILL.md）的技能名列表（Issue #887）。"""
    skills = []
    try:
        for entry in sorted(os.listdir(skill_dir)):
            if entry.startswith(TMP_PREFIX):
                continue
            if os.path.isfile(os.path.join(skill_dir, entry, "SKILL.md")):
                skills.append(entry)
    except Exception:
        pass
    return skills


# Issue #887：evt 携带一致性判定结果的契约键（event_pipeline / scheduler 共用，
# 消除跨 ~500 行的魔法字符串隐式耦合；缺失时调用方显式降级放行 + 告警）。
SKILL_CONSISTENCY_KEY = "_skill_consistency"


def skill_mismatch_error(sv: dict) -> str:
    """Issue #887：一致性拒绝错误消息（worker/direct/autopilot 三处共用单一实现）。

    sv 为 verify_skill_execution_consistency 返回值；统一 `[skill_mismatch] <reason>`
    格式，避免各调用点手写拼接漂移。
    """
    _reason = (sv or {}).get("reason") or "skill consistency check failed"
    return f"[skill_mismatch] {_reason}"


def verify_skill_execution_consistency(skill_name: str, skill_dir: str,
                                       skill_fallback: dict | None = None,
                                       extra_scan_dirs: list | None = None) -> dict:
    """Issue #887：执行前「即将执行的 skill」与 agent 绑定一致性校验（fail-fast 判定）。

    服务端（event_pipeline._materialize_direct_workdir / scheduler direct）与
    worker（agent.py _execute_task 执行前硬校验）两侧共用，零 DB 依赖。

    extra_scan_dirs（QA 修复）：引擎原生技能目录（os_adapter.engine_skill_dirs /
    opencode SKILLS_DIR 等）——worker 门必须连同引擎原生缓存一起判定，否则
    seq=46108 的残留（casegen-prep）落在引擎原生缓存而非 ws/.skills 时漏检。

    语义（对齐 Issue #887 Spec）：
    - 绑定 skill 已物化（skill_dir/<bound>/SKILL.md 存在）→ actual=bound，ok（source=bound）；
    - 绑定为空 → ok（source=unbound，引擎本地缓存仅绑定为空时兜底）；
    - 服务端明示回退（#617 claim 下发的 skill_fallback）已物化 → ok（source=declared_fallback，
      mismatch=True 显式标注，不静默）；
    - 绑定未物化但引擎原生缓存持有同名技能 → ok（source=engine_native，引擎将按
      绑定名从原生缓存加载，内容与绑定一致，显式标注可观测）；
    - 绑定未物化且 skill_dir 本地缓存存在**其它**技能 → NOT ok（source=local_cache，
      引擎将随本地缓存跑错技能——events seq=46108：绑定 caserun-prep 实际执行
      casegen-prep，静默跳过环境硬校验；缓存不得覆盖仓库级/场景级绑定 → 拒绝执行）；
    - 绑定未物化、skill_dir 无其它技能但引擎原生缓存存在其它技能且无 FALLBACK.md
      降级指引 → NOT ok（source=engine_native，actual=原生缓存首个其它技能——
      绑定技能无处可加载而引擎原生缓存将驱动执行，即 seq=46108 的原生缓存残留
      场景，静默跑错技能风险 → 拒绝执行）；
    - 绑定未物化且无其它本地技能（仅 FALLBACK.md 降级指引/空目录）→ ok
      （source=degraded_recovery，#607 权威取回路径，actual 仍为绑定值）；
    - 绑定未物化且任何缓存均无技能内容 → ok（source=missing，无可跑偏内容）。

    返回 dict {"ok": bool, "bound": str, "actual": str, "source": str,
               "reason": str, "local_cache": list}。
    """
    bound = (skill_name or "").strip()
    result = {"ok": True, "bound": bound, "actual": bound,
              "source": "bound", "reason": "", "local_cache": []}
    if not bound:
        result.update({"actual": "", "source": "unbound",
                       "reason": "no bound skill; engine local cache may apply as fallback"})
        return result
    from src.runtime.health import check_skill_consistency
    if os.path.isfile(os.path.join(skill_dir, bound, "SKILL.md")):
        result["reason"] = "bound skill materialized"
        return result
    _fb_name = str(((skill_fallback or {}).get("skill_name")) or "").strip()
    if _fb_name and os.path.isfile(os.path.join(skill_dir, _fb_name, "SKILL.md")):
        _chk = check_skill_consistency(bound, _fb_name, declared=True)
        result.update({"actual": _fb_name, "source": "declared_fallback",
                       "ok": _chk["ok"], "reason": _chk["detail"]})
        return result
    # 引擎原生缓存（extra_scan_dirs）：绑定同名技能存在 → 引擎按绑定名加载，内容一致
    _extra = [d for d in (extra_scan_dirs or []) if d and os.path.isdir(d)]
    _extra_others: list[str] = []
    for _d in _extra:
        _extra_others.extend(s for s in _scan_materialized_skills(_d) if s != bound)
    # 绑定未物化：任务技能目录本地缓存若存在其它技能，执行将由缓存决定 → 拒绝（fail-fast）
    # （seq=46108 原始场景：残留优先于 FALLBACK.md，杜绝静默跑错流程）
    _others = [s for s in _scan_materialized_skills(skill_dir) if s != bound]
    if _others:
        result.update({"ok": False, "actual": _others[0], "source": "local_cache",
                       "reason": (f"bound skill '{bound}' not materialized while local skill "
                                  f"cache holds other skill(s) [{', '.join(_others[:5])}]; "
                                  f"refusing execution to prevent silent wrong-skill run "
                                  f"(Issue #887)"),
                       "local_cache": _others})
        return result
    if os.path.isfile(os.path.join(skill_dir, bound, FALLBACK_FILENAME)):
        result.update({"source": "degraded_recovery",
                       "reason": ("bound skill not materialized; FALLBACK.md instructs "
                                  "authoritative fetch via `grape skills detail` "
                                  "(Issue #607 recovery, actual stays bound)"),
                       "local_cache": _extra_others[:10]})
        return result
    if any(os.path.isfile(os.path.join(_d, bound, "SKILL.md")) for _d in _extra):
        result.update({"source": "engine_native",
                       "reason": ("bound skill not materialized in task skill dir but "
                                  "present in engine native skill cache; engine loads "
                                  "the bound skill by name (Issue #887)"),
                       "local_cache": _extra_others[:10]})
        return result
    # 引擎原生缓存残留（seq=46108 原生缓存版）：绑定无处可加载 + 原生缓存持有其它
    # 技能 + 无 FALLBACK.md 权威取回指引 → 引擎将随原生缓存跑错技能 → 拒绝。
    if _extra_others:
        result.update({"ok": False, "actual": _extra_others[0], "source": "engine_native",
                       "reason": (f"bound skill '{bound}' not materialized anywhere while "
                                  f"engine native skill cache holds other skill(s) "
                                  f"[{', '.join(_extra_others[:5])}]; refusing execution "
                                  f"to prevent silent wrong-skill run (Issue #887)"),
                       "local_cache": _extra_others[:10]})
        return result
    result.update({"source": "missing",
                   "reason": "bound skill not materialized and no local cache content; "
                             "prompt instructs authoritative fetch"})
    return result
