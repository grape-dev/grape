"""Issue #516 M1：任务超时上限配置化（配置读取链）。

背景：worker 侧 `_wrap` 原先只有全局 `_env_float("RUNTIME_TASK_TIMEOUT", 3600)` 一个
硬编码超时，长耗时 coding 子任务跑满 1h 即被强制失败（2026-10-23 起 36 个
agent_task 因 `timed out after 3600` 触顶）。

解析优先级（高 → 低，见 docs/issue-516/plan.md）：
1. 任务级显式 env：task `env_extra` 里 `RUNTIME_TASK_TIMEOUT`（手动调参；setdefault
   语义——上层已显式设置时不覆盖，与 #378 静默超时注入同款约定）
2. per-skill 覆盖：配置文件 `per_skill[task["skill"]]`
3. per-repo 覆盖：配置文件 `per_repo[task["repo_key"]]`
4. agent 默认：`AGENT_TIMEOUT_DEFAULTS`（coding=7200；其余 agent 不回退）
5. 全局进程 env `RUNTIME_TASK_TIMEOUT`（兼容既有部署）
6. 兜底 3600

配置文件（可选 JSON，路径 env `GRAPE_TASK_TIMEOUT_CONFIG`，缺省
`configs/task-timeout.json`，schema 见 configs/task-timeout.example.json）：
    {
      "agent_defaults": {"coding": 7200},
      "per_repo": {"owner/repo": 7200},
      "per_skill": {"skill-name": 7200},
      "timeout_retries": 1,
      "retry_backoff_sec": 30
    }

健壮性约定（与 observability 配置读取一致）：文件缺失/损坏/字段非法一律静默
回退默认，不抛异常——超时配置坏不能拖垮任务执行主链路。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional, Tuple

#: 全局兜底超时（秒）——与旧硬编码 3600 保持一致，未配置路径行为不回退
DEFAULT_TASK_TIMEOUT = 3600.0

#: agent 级默认超时：coding 长任务上调至 7200（Issue #516 验收：≥7200s）。
#: 其余 agent 不在映射中 → 走全局默认 3600（不回退）。
AGENT_TIMEOUT_DEFAULTS: Dict[str, float] = {
    "coding": 7200.0,
}

#: 超时自动重试次数上限（M2）：防雪崩，验收要求 ≥1 次
DEFAULT_TIMEOUT_RETRIES = 1

#: 重试前退避间隔（秒）
DEFAULT_RETRY_BACKOFF_SEC = 30.0

#: Issue #2793：running 无新日志/无进展超时自愈默认阈值（秒）。实测假死表现为
#: 6 分钟仅 1 条日志后无产出；600s 不误伤常见长命令静默，且可按 skill/repo 调低。
DEFAULT_NO_PROGRESS_TIMEOUT_SEC = 600.0

#: Issue #2793：单任务无进展自动重派次数上限（与 timeout_retries 对齐，防雪崩）。
DEFAULT_NO_PROGRESS_RETRIES = 1

#: Issue #2793：无进展自愈总开关（false = 整体回滚，仅留诊断不重派）。
DEFAULT_NO_PROGRESS_ENABLED = True

#: Issue #3741：连续相同 tool+input 指纹的步数阈值（0 = 关闭指纹判据）。
#: 实测死循环表现为同一条 `sleep 115; tail ...; pgrep ...` 重复 43 次（约 115s/次），
#: 8 步（约 15 分钟内）足以识别空转，又不至于把「正常重试同一命令两三次」误判。
DEFAULT_NO_PROGRESS_REPEAT_STEPS = 8

#: Issue #2793：无进展阈值/重派次数的任务级显式 env（手动调参，最高优先）。
_NO_PROGRESS_TIMEOUT_ENV = "RUNTIME_NO_PROGRESS_TIMEOUT"
_NO_PROGRESS_RETRIES_ENV = "RUNTIME_NO_PROGRESS_RETRIES"
_NO_PROGRESS_ENABLED_ENV = "RUNTIME_NO_PROGRESS_ENABLED"
_NO_PROGRESS_REPEAT_STEPS_ENV = "RUNTIME_NO_PROGRESS_REPEAT_STEPS"

#: 超时 fail 消息签名（PR #722 审查建议集中定义）：worker 侧超时 kill 上报
#: `task timed out after {N}s`，alerts.py / metric_rollup.py 的 M3 观测按此
#: LIKE 过滤聚合——签名常量集中在此，避免双处硬编码后消息改版导致指标静默失效。
TIMEOUT_FAIL_SIGNATURE = "timed out after"

_CONFIG_CACHE: Dict[str, Any] = {"path": None, "mtime": None, "data": {}, "loaded_at": 0.0}


def _config_path() -> str:
    env_p = os.environ.get("GRAPE_TASK_TIMEOUT_CONFIG", "")
    if env_p:
        return env_p
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
                        "configs", "task-timeout.json")


def load_timeout_config(path: Optional[str] = None, max_age_sec: float = 5.0) -> Dict[str, Any]:
    """加载超时配置 JSON（带 mtime 缓存）。

    文件缺失/损坏/非 dict → 返回空 dict（调用方走默认值）。测试可传 path 直接读。
    """
    p = path or _config_path()
    now = time.monotonic()
    try:
        st = os.stat(p)
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        _CONFIG_CACHE.update({"path": p, "mtime": None, "data": {}, "loaded_at": now})
        return {}
    if (_CONFIG_CACHE.get("path") == p and _CONFIG_CACHE.get("mtime") == (mtime, size)
            and now - _CONFIG_CACHE.get("loaded_at", 0.0) < max_age_sec):
        return _CONFIG_CACHE.get("data") or {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _CONFIG_CACHE.update({"path": p, "mtime": (mtime, size), "data": data, "loaded_at": now})
    return data


def _positive_number(raw: Any) -> Optional[float]:
    """正有限数值才有效（0/负/NaN/非数字 → None，调用方忽略该覆盖）。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v or v <= 0 or v == float("inf"):  # NaN / 非正 / 无穷
        return None
    return v


def _task_env(task: Dict[str, Any]) -> Dict[str, str]:
    """解析 task 的 env_extra（坏 JSON → 空 dict）。"""
    try:
        from src.domains.runtime.engine.env_extra_security import decrypt_env_extra
        # #3770：带行 id → 存量明文行在该读点也能就地自愈（不只计数）
        env = decrypt_env_extra(task.get("env_extra"),
                                task_id=str(task.get("id") or ""))  # enc:v1 信封解密（#2423）
        return env if isinstance(env, dict) else {}
    except Exception:
        return {}


def _section_entry(cfg: Dict[str, Any], section: str, key: str) -> Any:
    """配置段原始条目查找（含大小写不敏感兜底）。缺省/无该段 → None。

    条目既可以是旧的标量超时值，也可以是 Issue #2793 引入的 dict 形态
    （如 `{"timeout": 7200, "no_progress_timeout_sec": 300}`），由调用方按需取值。
    """
    if not key:
        return None
    sect = cfg.get(section)
    if not isinstance(sect, dict):
        return None
    v = sect.get(key)
    if v is None:
        kl = key.strip().lower()
        for k, kv in sect.items():
            if str(k).strip().lower() == kl:
                v = kv
                break
    return v


def _lookup(cfg: Dict[str, Any], section: str, key: str) -> Optional[float]:
    """配置段查找：key 归一（去空白、skill/repo 大小写不敏感比较）。

    条目为 dict 时取 `timeout` 键（Issue #2793 扩展形态），兼容旧的标量写法。
    """
    v = _section_entry(cfg, section, key)
    if isinstance(v, dict):
        v = v.get("timeout")
    return _positive_number(v)


def resolve_task_timeout(task: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None,
                         process_env: Optional[Dict[str, str]] = None
                         ) -> Tuple[float, str]:
    """解析单任务超时上限（秒）。返回 (timeout, source)。

    `task`：claim 下发的任务 dict（用 env_extra / skill / repo_key / agent_type）。
    `cfg`：注入配置（测试用）；缺省自动加载配置文件。
    `process_env`：进程环境（测试用）；缺省 os.environ。
    """
    if cfg is None:
        cfg = load_timeout_config()
    env = os.environ if process_env is None else process_env

    # 1. 任务级显式 env（手动调参，最高优先）
    tenv = _task_env(task)
    v = _positive_number(tenv.get("RUNTIME_TASK_TIMEOUT"))
    if v is not None:
        return v, "task-env"

    skill = str(task.get("skill") or "").strip()
    repo = str(task.get("repo_key") or "").strip()
    agent = str(task.get("agent_type") or "").strip().lower()

    # 2. per-skill
    v = _lookup(cfg, "per_skill", skill)
    if v is not None:
        return v, f"per-skill:{skill}"

    # 3. per-repo
    v = _lookup(cfg, "per_repo", repo)
    if v is not None:
        return v, f"per-repo:{repo}"

    # 4. agent 默认（coding=7200，其余不回退）
    v = _lookup(cfg, "agent_defaults", agent) or _positive_number(AGENT_TIMEOUT_DEFAULTS.get(agent))
    if v is not None:
        return v, f"agent-default:{agent}"

    # 5. 全局进程 env（兼容既有部署）
    v = _positive_number(env.get("RUNTIME_TASK_TIMEOUT"))
    if v is not None:
        return v, "env:RUNTIME_TASK_TIMEOUT"

    # 6. 兜底
    return DEFAULT_TASK_TIMEOUT, "default"


def timeout_retry_count(task: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> int:
    """解析超时自动重试次数（M2）。

    优先级：任务 env_extra `RUNTIME_TASK_TIMEOUT_RETRIES`（显式手动，含 0=禁用）
    > 配置文件 `timeout_retries` > 进程 env `RUNTIME_TASK_TIMEOUT_RETRIES` > 默认 1。
    负数/非法 → 默认值。
    """
    if cfg is None:
        cfg = load_timeout_config()

    def _int(raw: Any) -> Optional[int]:
        try:
            iv = int(float(raw))
        except (TypeError, ValueError):
            return None
        return iv if iv >= 0 else None

    v = _int(_task_env(task).get("RUNTIME_TASK_TIMEOUT_RETRIES"))
    if v is not None:
        return v
    v = _int(cfg.get("timeout_retries"))
    if v is not None:
        return v
    v = _int(os.environ.get("RUNTIME_TASK_TIMEOUT_RETRIES"))
    if v is not None:
        return v
    return DEFAULT_TIMEOUT_RETRIES


def retry_backoff_sec(cfg: Optional[Dict[str, Any]] = None) -> float:
    """重试前退避间隔（秒）。非法回退默认 30s。"""
    if cfg is None:
        cfg = load_timeout_config()
    v = _positive_number(cfg.get("retry_backoff_sec"))
    if v is not None:
        v2 = _positive_number(os.environ.get("RUNTIME_TASK_TIMEOUT_RETRY_BACKOFF"))
        if v2 is not None:
            return v2
        return v
    v2 = _positive_number(os.environ.get("RUNTIME_TASK_TIMEOUT_RETRY_BACKOFF"))
    if v2 is not None:
        return v2
    return DEFAULT_RETRY_BACKOFF_SEC


# ── Issue #2793：无进展（假死）自愈配置链 ────────────────────────────────
#
# 优先级（高 → 低）：
#   1. 任务级 env_extra（RUNTIME_NO_PROGRESS_TIMEOUT / _RETRIES / _ENABLED / _REPEAT_STEPS）
#   2. per_skill[skill] 的 dict 条目字段（no_progress_timeout_sec 等）
#   3. per_repo[repo_key] 的 dict 条目字段
#   4. 配置文件顶层 no_progress_timeout_sec / no_progress_retries / no_progress_enabled
#   5. 进程 env 同名变量
#   6. 默认值（600s / 1 次 / 开启）
# 非法值（0/负/NaN/非数字/坏 JSON）一律静默回退下一层，坏配置不得拖垮主链路。


def _flag(raw: Any) -> Optional[bool]:
    """宽松解析布尔开关；无法识别 → None（调用方回退下一层）。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    return None


def _non_negative_int(raw: Any) -> Optional[int]:
    """非负整数才有效（负数/NaN/非法字符串 → None）。"""
    try:
        iv = int(float(raw))
    except (TypeError, ValueError):
        return None
    return iv if iv >= 0 else None


def _no_progress_field_from_entry(entry: Any, field: str) -> Any:
    """per_skill/per_repo 条目中的无进展字段；旧标量条目无该字段 → None。"""
    if isinstance(entry, dict):
        return entry.get(field)
    return None


def no_progress_timeout_sec(task: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None
                            ) -> Tuple[float, str]:
    """解析单任务「无进展」静默阈值（秒）。返回 (timeout, source)。"""
    if cfg is None:
        cfg = load_timeout_config()
    tenv = _task_env(task)
    v = _positive_number(tenv.get(_NO_PROGRESS_TIMEOUT_ENV))
    if v is not None:
        return v, "task-env"

    skill = str(task.get("skill") or "").strip()
    repo = str(task.get("repo_key") or "").strip()
    for section, key in (("per_skill", skill), ("per_repo", repo)):
        entry = _section_entry(cfg, section, key)
        v = _positive_number(_no_progress_field_from_entry(entry, "no_progress_timeout_sec"))
        if v is not None:
            return v, f"{section.replace('_', '-')}:{key}"

    v = _positive_number(cfg.get("no_progress_timeout_sec"))
    if v is not None:
        return v, "config"
    v = _positive_number(os.environ.get(_NO_PROGRESS_TIMEOUT_ENV))
    if v is not None:
        return v, f"env:{_NO_PROGRESS_TIMEOUT_ENV}"
    return DEFAULT_NO_PROGRESS_TIMEOUT_SEC, "default"


def no_progress_retries(task: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> int:
    """解析单任务无进展自动重派次数上限（0 = 只诊断不重派）。"""
    if cfg is None:
        cfg = load_timeout_config()
    tenv = _task_env(task)
    v = _non_negative_int(tenv.get(_NO_PROGRESS_RETRIES_ENV))
    if v is not None:
        return v

    skill = str(task.get("skill") or "").strip()
    repo = str(task.get("repo_key") or "").strip()
    for section, key in (("per_skill", skill), ("per_repo", repo)):
        entry = _section_entry(cfg, section, key)
        v = _non_negative_int(_no_progress_field_from_entry(entry, "no_progress_retries"))
        if v is not None:
            return v

    v = _non_negative_int(cfg.get("no_progress_retries"))
    if v is not None:
        return v
    v = _non_negative_int(os.environ.get(_NO_PROGRESS_RETRIES_ENV))
    if v is not None:
        return v
    return DEFAULT_NO_PROGRESS_RETRIES


def no_progress_repeat_steps(task: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> int:
    """Issue #3741：解析「连续相同 tool+input 指纹」判据的步数阈值（0 = 关闭）。

    与 no_progress_retries 同一条配置链，0 是**合法值**（显式关闭指纹判据、只保留
    #2793 的活跃度判据）；负数/非法字符串视为未配置，继续回退下一层。
    """
    if cfg is None:
        cfg = load_timeout_config()
    tenv = _task_env(task)
    v = _non_negative_int(tenv.get(_NO_PROGRESS_REPEAT_STEPS_ENV))
    if v is not None:
        return v

    skill = str(task.get("skill") or "").strip()
    repo = str(task.get("repo_key") or "").strip()
    for section, key in (("per_skill", skill), ("per_repo", repo)):
        entry = _section_entry(cfg, section, key)
        v = _non_negative_int(_no_progress_field_from_entry(entry, "no_progress_repeat_steps"))
        if v is not None:
            return v

    v = _non_negative_int(cfg.get("no_progress_repeat_steps"))
    if v is not None:
        return v
    v = _non_negative_int(os.environ.get(_NO_PROGRESS_REPEAT_STEPS_ENV))
    if v is not None:
        return v
    return DEFAULT_NO_PROGRESS_REPEAT_STEPS


def no_progress_enabled(task: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> bool:
    """解析无进展自愈总开关（false = 只留诊断、不重派，用于误杀回滚）。"""
    if cfg is None:
        cfg = load_timeout_config()
    tenv = _task_env(task)
    v = _flag(tenv.get(_NO_PROGRESS_ENABLED_ENV))
    if v is not None:
        return v

    skill = str(task.get("skill") or "").strip()
    repo = str(task.get("repo_key") or "").strip()
    for section, key in (("per_skill", skill), ("per_repo", repo)):
        entry = _section_entry(cfg, section, key)
        v = _flag(_no_progress_field_from_entry(entry, "no_progress_enabled"))
        if v is not None:
            return v

    v = _flag(cfg.get("no_progress_enabled"))
    if v is not None:
        return v
    v = _flag(os.environ.get(_NO_PROGRESS_ENABLED_ENV))
    if v is not None:
        return v
    return DEFAULT_NO_PROGRESS_ENABLED
