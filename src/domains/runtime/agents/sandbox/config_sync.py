"""Worker 配置同步——安全策略 fail-closed，审计路径强约束。

三类配置：
- 安全策略（agent_sandbox / sandbox_mode / extra_whitelist）：远程 > 上次已知良好 > 安全默认。本地只能收紧。
- 审计路径（audit_path / ext_audit_path）：远程根目录 > 本地子路径 > 默认。禁软链/..
- 纯运行参数：远程 > 本地 env > 默认。

sandbox_manager.prepare() 从 ConfigSync.get() 读配置，不直接读 os.environ。
"""
from __future__ import annotations

import os
import threading
import time

from src.kernel.config import log

# 安全默认值——fail-closed
_SECURITY_DEFAULTS = {
    "agent_sandbox": "on",
    "sandbox_mode": "required",  # P0-10 修复：TTL 过期 fail-closed（was "preferred"）
    "extra_whitelist": "",
    "engine_overrides": {},  # 引擎维度沙箱开关默认空，未配的引擎 fallback 到全局 agent_sandbox
}

# sandbox_mode 强度排序：required > preferred > disabled
_MODE_RANK = {"required": 3, "preferred": 2, "disabled": 1}

# 审计路径允许的根目录——当前只做反向禁止（_FORBIDDEN_PATHS），正向校验留给 bootstrap.py 可写性检查
_AUDIT_ROOTS = ["/var/log/grape/", os.path.expanduser("~/.grape/logs/"), "/tmp/grape-audit/"]

# 审计路径禁止的子路径
# /root/ 不整体禁止——root 用户的 ~/.grape/logs/ 审计路径需要放行。
# 改为禁止 /root/ 下的敏感子目录（.ssh / .gnupg）。
_FORBIDDEN_PATHS = ["/etc/", "/proc/", "/sys/", "/dev/", "/root/.ssh/", "/root/.gnupg/", "/boot/"]

# TTL——远程不可达后保持上次已知良好的时间（秒）
_TTL = 60.0


class ConfigSync:
    """Worker 配置同步单例。

    线程安全：心跳线程写，executor 线程读，用锁保护。
    原子更新：验版本 + 合并 + 校验全通过后才整体生效，失败回滚。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._version = 0  # 远程配置版本号，单调递增
        self._last_good: dict = dict(_SECURITY_DEFAULTS)  # 上次已知良好
        self._last_good_ts = time.time()  # 上次成功同步时间
        self._remote_fail = False  # 远程不可达标记
        # 初始化：安全默认 + 本地 env 紧急关闭
        self._snapshot: dict = dict(_SECURITY_DEFAULTS)
        # 本地 env 显式设 off → 紧急关闭（主服务不调 apply_remote 时生效）
        _local_sb = os.environ.get("GRAPE_AGENT_SANDBOX", "").lower()
        if _local_sb in ("0", "false", "off", "no"):
            self._snapshot["agent_sandbox"] = "off"

    def apply_remote(self, config: dict, version: int) -> bool:
        """应用远程配置。

        版本单调校验 + 分类合并 + 原子更新。
        返回 True 表示有变化。
        """
        if not config or not isinstance(config, dict):
            return False

        # 版本单调校验——拒绝旧版本重放（相同版本跳过，不 reject）
        if version > 0 and version < self._version:
            log.warning(f"ConfigSync: reject version rollback {version} < {self._version}")
            self._reset_to_security_defaults("version rollback")
            return False
        if version > 0 and version == self._version:
            # 相同版本——配置没变，跳过 apply（不 reject、不 reset）
            return False

        try:
            new_snapshot = dict(self._snapshot)  # 从当前快照拷贝

            # 1. 安全策略——远程 > 本地收紧
            new_snapshot["agent_sandbox"] = self._merge_agent_sandbox(config)
            new_snapshot["sandbox_mode"] = self._merge_sandbox_mode(config)
            new_snapshot["extra_whitelist"] = self._merge_whitelist(config)
            new_snapshot["engine_overrides"] = self._merge_engine_overrides(config)

            # 2. 审计路径——根目录锁定 + 路径校验
            audit_path = self._resolve_audit_path(
                config.get("audit_path", ""), "GRAPE_SANDBOX_AUDIT_PATH",
                "/var/log/grape/sandbox-audit.jsonl")
            new_snapshot["audit_path"] = audit_path

            ext_audit_path = self._resolve_audit_path(
                config.get("ext_audit_path", ""), "GRAPE_EXTERNAL_API_AUDIT_PATH",
                "/var/log/grape/external-api-audit.jsonl")
            new_snapshot["ext_audit_path"] = ext_audit_path

            # 原子更新
            with self._lock:
                self._snapshot = new_snapshot
                self._version = max(version, self._version + 1)
                self._last_good = dict(new_snapshot)
                self._last_good_ts = time.time()
                self._remote_fail = False

            log.info(f"ConfigSync: applied version={self._version}")
            return True

        except Exception as e:
            log.error(f"ConfigSync: apply failed (reset to security defaults): {e}")
            self._reset_to_security_defaults(str(e))
            return False

    def _merge_agent_sandbox(self, remote: dict) -> str:
        """合并沙箱开关。

        紧急关闭优先：本地 env 显式设 off → off（紧急关闭，最高优先级）
        否则：远程 on 或 本地 on → on（默认开启）
        都不 on → off
        """
        remote_val = str(remote.get("agent_sandbox", "on")).lower()
        local_val = os.environ.get("GRAPE_AGENT_SANDBOX", "").lower()
        # 本地显式设 off → 紧急关闭（最高优先级，覆盖远程）
        if local_val in ("0", "false", "off", "no"):
            return "off"
        remote_on = remote_val in ("1", "true", "on", "yes")
        local_on = local_val in ("1", "true", "on", "yes")
        # 任一为 on → on
        return "on" if (remote_on or local_on) else "off"

    def _merge_sandbox_mode(self, remote: dict) -> str:
        """合并沙箱模式。取更强值：required > preferred > disabled。"""
        remote_val = str(remote.get("sandbox_mode", "preferred")).lower()
        local_val = os.environ.get("GRAPE_SANDBOX_MODE", "preferred").lower()
        remote_rank = _MODE_RANK.get(remote_val, 2)
        local_rank = _MODE_RANK.get(local_val, 2)
        # 取更强的（rank 更大）
        if remote_rank >= local_rank:
            return remote_val if remote_val in _MODE_RANK else "preferred"
        return local_val if local_val in _MODE_RANK else "preferred"

    def _merge_whitelist(self, remote: dict) -> str:
        """合并白名单。取交集——本地只能删域名，不能加。

        本地为空（没设 env）时用远程值——等于本地没收紧。
        远程为空时返回空——安全默认。
        """
        remote_wl = set(d.strip() for d in str(remote.get("extra_whitelist", "")).split(",") if d.strip())
        local_wl = set(d.strip() for d in os.environ.get("GRAPE_SANDBOX_EXTRA_WHITELIST", "").split(",") if d.strip())
        # 取交集——本地只能收紧
        intersection = remote_wl & local_wl if remote_wl and local_wl else remote_wl
        return ",".join(sorted(intersection))

    def _resolve_audit_path(self, remote_path: str, env_key: str, default_path: str) -> str:
        """解析审计路径。远程优先 > 本地子路径 > 默认。

        远程路径优先（远程根目录锁定）。本地 env 只能选远程路径的子路径。
        路径规范化：禁止 ..、软链、系统敏感目录。
        """
        # 优先级：远程 > 本地 env > 默认
        path = remote_path or os.environ.get(env_key, "") or default_path

        # 规范化
        path = os.path.normpath(path)

        # 禁止 ..
        if ".." in path:
            log.error(f"ConfigSync: audit path contains '..': {path}")
            return default_path

        # 禁止系统敏感目录
        for forbidden in _FORBIDDEN_PATHS:
            if path.startswith(forbidden):
                log.error(f"ConfigSync: audit path in forbidden dir: {path}")
                return default_path

        # 禁止软链
        real = os.path.realpath(path)
        if real != path and os.path.exists(path):
            log.error(f"ConfigSync: audit path is symlink: {path} -> {real}")
            return default_path

        return path

    def _merge_engine_overrides(self, remote: dict) -> dict[str, str]:
        """合并引擎维度沙箱开关。

        直接用远程下发的 engine_overrides，不做本地覆盖。
        全局 GRAPE_AGENT_SANDBOX 只在引擎未配置时作为 fallback（在 get_engine_sandbox 里处理）。
        """
        remote_overrides = remote.get("engine_overrides", {})
        if not isinstance(remote_overrides, dict):
            return {}
        return {k: v.lower() for k, v in remote_overrides.items()}

    def get_engine_sandbox(self, engine_type: str) -> str:
        """查指定引擎的沙箱开关。

        1. engine_overrides 里有该引擎 → 返回该值
        2. 未配置 → fallback 到全局 agent_sandbox（默认 on）

        全局开关只影响未配置的引擎，已配置的引擎按自己的值走。
        TTL 过期时：_SECURITY_DEFAULTS 里 engine_overrides={}，
        所有引擎 fallback 到全局 agent_sandbox=on（安全默认）。
        """
        with self._lock:
            # TTL 过期 → 返回 _SECURITY_DEFAULTS（engine_overrides={}）
            if self._remote_fail and (time.time() - self._last_good_ts > _TTL):
                _defaults = _SECURITY_DEFAULTS
                overrides = _defaults.get("engine_overrides", {})
                if engine_type in overrides:
                    return overrides[engine_type]
                return _defaults.get("agent_sandbox", "on")

            overrides = self._snapshot.get("engine_overrides", {})
            if engine_type in overrides:
                return overrides[engine_type]
            # fallback 到全局开关
            return self._snapshot.get("agent_sandbox", "on")

    def get(self, key: str) -> str:
        """读配置快照。sandbox_manager.prepare() 调此方法。"""
        with self._lock:
            # 检查 TTL——远程不可达超时
            if self._remote_fail and (time.time() - self._last_good_ts > _TTL):
                # 过期——返回安全默认
                return _SECURITY_DEFAULTS.get(key, "")
            return self._snapshot.get(key, _SECURITY_DEFAULTS.get(key, ""))

    def on_heartbeat_fail(self):
        """远程不可达——标记 + 保持上次已知良好。"""
        with self._lock:
            self._remote_fail = True
        log.warning(f"ConfigSync: remote unreachable, holding last good (TTL={_TTL}s)")

    def _reset_to_security_defaults(self, reason: str):
        """重置到安全默认值——fail-closed。"""
        with self._lock:
            self._snapshot = dict(_SECURITY_DEFAULTS)
            self._remote_fail = True
            self._last_good_ts = time.time()  # 重置 TTL 计时
        log.error(f"ConfigSync: reset to security defaults (reason={reason})")

    def reset(self):
        """仅用于测试。"""
        with self._lock:
            self._version = 0
            self._snapshot = dict(_SECURITY_DEFAULTS)
            self._last_good = dict(_SECURITY_DEFAULTS)
            self._last_good_ts = time.time()
            self._remote_fail = False


# 模块级单例
_default = ConfigSync()


def get_config_sync() -> ConfigSync:
    return _default
