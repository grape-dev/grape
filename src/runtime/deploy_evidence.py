"""部署类任务收口证据校验（Issue #537 P1-1 纵深防御）。

背景：autopilot 收口此前仅看 agent_tasks.status —— 停双主/重启 master 场景下
worker 进程实际完成（双主已拉起、版本已替换）但终态被 master 侧状态清理误判
failed（生产 exec incident case），也可能出现"进程跑完但实际未落地"的 false-success。

本模块在收口写 success 前对部署/升级类任务做**实际落地证据**校验：
- 版本指纹：agent_runtimes.worker_version（worker 实际运行版本）vs 预期版本
  （env_extra.EXPECTED_VERSION 结构化字段）；
- 健康检查：HEALTH_URL 端点 HTTP 2xx（env_extra.HEALTH_URL / EVIDENCE_HEALTH_URL 环境变量）。

QA 审查（PR #751）加固点：
- Required：部署类判定排除 result_text 自由文本（普通任务输出含"升级/发布/version"
  字样不得误判部署类）；版本判失败仅限 env_extra.EXPECTED_VERSION 显式结构化预期，
  从指令/结果文本正则提取的版本仅作诊断参考（referenced_version），不参与 passed 判定。
- Recommended：健康检查增加 SSRF 基础防护（仅 http/https；回环/私网/链路本地/保留地址
  一律 skip，不发起出站请求）。

安全边界（绝不阻断主链路）：
- 非部署类任务：checked=False，按原逻辑收口；
- 外部依赖不可达（health URL 连不上 / 无法获取实际版本）：该维度 skip（None），不判失败；
- 仅在**明确矛盾**时判失败：版本已知不匹配 / 健康端点可达但非 2xx；
- 任何异常 → 降级 skip + warning（不抛错，不阻断收口）。
"""
import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlparse

from src.config import log

_DEPLOY_KEYWORDS = (
    "deploy", "upgrade", "升级", "部署", "发布", "停双主", "双主", "重启 master",
)
# 仅作诊断参考（referenced_version），不参与判失败（QA Required）。
_VERSION_PATTERN = re.compile(
    r"(?:目标版本|期望版本|升级到|expected\s*version|upgrade\s*to|version)"
    r"\s*[:：=]?\s*([A-Za-z0-9][A-Za-z0-9._\-]*)",
    re.IGNORECASE,
)
_DEFAULT_HEALTH_URL = os.environ.get("EVIDENCE_HEALTH_URL", "")
_HTTP_TIMEOUT = int(os.environ.get("EVIDENCE_HTTP_TIMEOUT", "3"))


def is_deploy_task(skill: str = "", prompt: str = "", result_text: str = "",
                   extra_text: str = "") -> bool:
    """判定是否为部署/升级类任务（基于 skill/prompt/instruction 等**结构化来源**）。

    QA 审查（PR #751 Required）：result_text 是任务产出自由文本，普通任务输出若含
    "升级依赖 version X" 等字样会被误判部署类 → 收口证据校验翻 failed（与 #537 要
    消除的假 failed 同向）。部署类判定排除 result_text，只看 skill/prompt/指令。
    """
    _text = " ".join([skill or "", prompt or "", extra_text or ""]).lower()
    return any(k in _text for k in _DEPLOY_KEYWORDS)


def _parse_env_extra(env_extra) -> dict:
    """agent_tasks.env_extra：可能为 dict 或 JSON 字符串。"""
    if isinstance(env_extra, dict):
        return env_extra
    if isinstance(env_extra, str) and env_extra:
        try:
            _d = json.loads(env_extra)
            return _d if isinstance(_d, dict) else {}
        except Exception:
            return {}
    return {}


def _resolve_expected_version(env: dict) -> str:
    """预期版本（**判失败依据**）：仅 env_extra.{EXPECTED_VERSION,expected_version,target_version}。

    QA 审查（PR #751 Required）：不再从 prompt/result_text 正则提取作为判失败依据——
    普通任务文本含 "version X" 不得触发版本不匹配。文本提取仅作参考（_extract_version_reference）。
    """
    for k in ("EXPECTED_VERSION", "expected_version", "target_version"):
        if env.get(k):
            return str(env[k]).strip()
    return ""


def _extract_version_reference(prompt: str = "", result_text: str = "") -> str:
    """只读诊断参考：从指令/结果文本正则提取版本号（不参与判失败，QA Required）。"""
    m = _VERSION_PATTERN.search(" ".join([prompt or "", result_text or ""]))
    return m.group(1) if m else ""


def _resolve_health_url(env: dict) -> str:
    """健康检查地址：env_extra.HEALTH_URL 优先，其次 EVIDENCE_HEALTH_URL 环境变量。"""
    for k in ("HEALTH_URL", "health_url"):
        if env.get(k):
            return str(env[k]).strip()
    return _DEFAULT_HEALTH_URL


def _fetch_worker_version(runtime_id) -> str:
    """实际运行版本：agent_runtimes.worker_version（无/失败 → 空串 = 无信息，不判失败）。"""
    if not runtime_id:
        return ""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            r = db.cursor().execute(
                "SELECT worker_version FROM agent_runtimes WHERE id=?", (runtime_id,)).fetchone()
        return (r[0] or "").strip() if r else ""
    except Exception as _e:
        log.warning(f"deploy_evidence: fetch worker_version failed runtime={runtime_id}: {_e}")
        return ""


def _is_blocked_target(url: str) -> bool:
    """SSRF 基础防护（QA Recommended）：非 http/https、或回环/私网/链路本地/保留地址 → True。

    仅做字面解析，不解析 DNS（避免放大 SSRF 面）；主机名仅拦截已知本机别名。
    """
    try:
        _p = urlparse(url)
        if _p.scheme not in ("http", "https"):
            return True
        _host = _p.hostname
        if not _host:
            return True
        try:
            _addr = ipaddress.ip_address(_host)
            return (_addr.is_loopback or _addr.is_private or _addr.is_link_local
                    or _addr.is_reserved or _addr.is_multicast or _addr.is_unspecified)
        except ValueError:
            # 非 IP 主机名：拦截已知本机别名，其余域名放行（不做 DNS 解析）
            return _host.lower() in ("localhost", "localhost.localdomain") or _host.lower().endswith(".local")
    except Exception:
        return True


def _check_health(url: str) -> Optional[bool]:
    """健康检查：可达且 2xx → True；可达但非 2xx → False；不可达/异常/SSRF 风险 → None（skip）。"""
    if not url or _is_blocked_target(url):
        return None
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "grape-evidence/1.0"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        # 服务可达但返回错误码：明确矛盾 → False
        return 200 <= e.code < 300
    except Exception:
        # 不可达/超时/解析失败：外部依赖问题 → skip（不阻断）
        return None


def check_deploy_evidence(task: dict) -> dict:
    """部署类任务收口证据校验（幂等、只读、永不抛错）。

    入参 task：agent_tasks 行 dict，至少含 skill/prompt/result_text/env_extra/runtime_id；
    可选 instruction（autopilot 指令文本）。

    返回：
      checked    - 是否为部署类任务（False = 跳过，调用方按原逻辑收口）
      passed     - 是否通过证据校验（True = 可 success；False = 应转 failed）
      reason     - 结果说明（失败时含 evidence 详情）
      version_ok - 版本指纹：None=无显式预期或信息不足跳过 / True / False
      health_ok  - 健康检查：None=未配置/不可达/SSRF 风险跳过 / True / False
      expected_version / actual_version / referenced_version - 诊断信息
    """
    try:
        skill = task.get("skill") or ""
        prompt = task.get("prompt") or ""
        result_text = task.get("result_text") or ""
        if not is_deploy_task(skill, prompt, result_text, task.get("instruction") or ""):
            return {"checked": False, "passed": True, "reason": "非部署类任务，跳过 evidence 校验",
                    "version_ok": None, "health_ok": None,
                    "expected_version": "", "actual_version": "",
                    "referenced_version": ""}
        env = _parse_env_extra(task.get("env_extra"))
        expected = _resolve_expected_version(env)
        # 无显式结构化预期 → 版本维度跳过（不判失败）；文本提取版本仅作诊断参考
        actual = _fetch_worker_version(task.get("runtime_id") or 0)
        version_ok: Optional[bool] = None
        if expected and actual:
            version_ok = expected.lower() == actual.lower()
        # expected 已知但 actual 未知（无版本信息）→ skip（不判失败，避免误杀可重复部署场景）

        url = _resolve_health_url(env)
        health_ok = _check_health(url) if url else None

        _reasons = []
        if version_ok is False:
            _reasons.append(f"版本指纹不匹配：期望 {expected}，实际 {actual}")
        if health_ok is False:
            _reasons.append(f"健康检查失败：{url} 返回非 2xx")
        passed = (version_ok is not False) and (health_ok is not False)
        if passed:
            _reasons.append("证据校验通过" if (version_ok or health_ok) else "证据信息不足，按通过处理")
        return {
            "checked": True, "passed": passed, "reason": "；".join(_reasons),
            "version_ok": version_ok, "health_ok": health_ok,
            "expected_version": expected, "actual_version": actual,
            "referenced_version": _extract_version_reference(prompt, result_text),
        }
    except Exception as _e:
        log.warning(f"deploy_evidence: check failed (degrade to skip): {_e}")
        return {"checked": False, "passed": True, "reason": "evidence 校验降级 skip",
                "version_ok": None, "health_ok": None,
                "expected_version": "", "actual_version": "",
                "referenced_version": ""}