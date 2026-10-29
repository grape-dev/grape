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

_CONFIG_CACHE: Dict[str, Any] = {"path": None, "mtime": None, "data": {}, "loaded_at": 0.0}


def _config_path() -> str:
    env_p = os.environ.get("GRAPE_TASK_TIMEOUT_CONFIG", "")
    if env_p:
        return env_p
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
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
        env = json.loads(task.get("env_extra") or "{}")
        return env if isinstance(env, dict) else {}
    except Exception:
        return {}


def _lookup(cfg: Dict[str, Any], section: str, key: str) -> Optional[float]:
    """配置段查找：key 归一（去空白、skill/repo 大小写不敏感比较）。"""
    if not key:
        return None
    sect = cfg.get(section)
    if not isinstance(sect, dict):
        return None
    v = sect.get(key)
    if v is None:
        # 大小写不敏感兜底（skill 名/仓库名大小写漂移时仍可命中）
        kl = key.strip().lower()
        for k, kv in sect.items():
            if str(k).strip().lower() == kl:
                v = kv
                break
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
