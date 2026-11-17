"""Skill egress 策略——数据结构 + 最小策略引擎 + L7 校验逻辑。

三层授权架构（design-skill-egress-domains.md）：
  Layer 1: skill frontmatter 声明 egress 策略对象
  Layer 2: PolicyEngine.apply() 审批 → per-task 策略（本文件）
  Layer 3: NetworkProxy 逐请求校验（network.py）

Phase 1+2 信任假设：skill 来源可信（内部 skill，管理员已审核）。
Phase 3 补签名/哈希校验 + 管理员审批流程。
⚠️ Phase 1+2 不是安全边界——需 SANDBOX_EGRESS_ENABLED=1 显式开启。
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import threading
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

log = logging.getLogger("devops-bot")


# ===== 数据结构 =====

@dataclass
class EgressRule:
    """单条 egress 策略——skill frontmatter 声明的出站规则。"""
    host: str                                    # 域名或 IP
    type: str                                    # "domain" | "ip"
    ports: list[int]                             # 必填
    methods: list[str] = field(default_factory=lambda: ["GET"])
    path_prefix: str = "/"
    max_body: int = 1048576                      # 1MB
    redirect: str = "none"


@dataclass
class EgressPolicy:
    """per-task egress 策略——Layer 2 审批后输出，bwrap 只读这个。"""
    rules: list[EgressRule]
    skill_id: str                # 审计归因
    skill_digest: str            # 整包 digest（Phase 3）或 SKILL.md digest（Phase 1+2 旧任务）
    admin_policy: str = "auto_approve"  # auto_approve / manual_approve / deny
    rate_limits: dict | None = None     # 可选速率限制覆盖
    admin_exceptions: list = field(default_factory=list)  # 管理员例外列表


# ===== 常量 =====

_VALID_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
_VALID_TYPES = frozenset({"domain", "ip"})

_DEFAULT_METHODS = ["GET"]
_DEFAULT_PATH_PREFIX = "/"
_DEFAULT_MAX_BODY = 1048576  # 1MB
_DEFAULT_REDIRECT = "none"

# 绝对拒绝——永不可覆盖（与 network.py 保持一致）
_ABSOLUTE_DENIED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
]

# 整包 digest 约束
_MAX_SINGLE_FILE = 10 * 1024 * 1024   # 10MB
_MAX_PACKAGE_SIZE = 100 * 1024 * 1024  # 100MB
_DIGEST_EXCLUDE_DIRS = frozenset({".git", ".svn", "__pycache__", ".mypy_cache"})
_DIGEST_EXCLUDE_SUFFIXES = frozenset({".pyc", ".pyo", ".DS_Store"})

# digest 缓存——进程内，dict[skill_dir, digest]
# 加载时算一次，不每次任务都算
_digest_cache: dict[str, str] = {}
_digest_cache_lock = threading.Lock()


# ===== 整包 digest =====

def compute_skill_package_digest(skill_dir: str) -> str:
    """计算 skill 整包 digest。

    遍历目录下所有文件，按相对路径排序，拼接 path + content 做 sha256。
    覆盖 SKILL.md + references/ + scripts/ + assets/ 全部文件。

    约束：
    - 符号链接：拒绝（防止链接到外部文件或 /etc/passwd）
    - 大小上限：单文件 10MB、整包 100MB，超限拒绝
    - 排除：.git、.svn、__pycache__、.mypy_cache、*.pyc、*.pyo、.DS_Store
    - 缓存：进程内 dict[skill_dir, digest]，加载时算一次

    返回 "sha256:<hex16>" 或抛 ValueError（符号链接/超限）。
    """
    # 缓存检查
    with _digest_cache_lock:
        cached = _digest_cache.get(skill_dir)
        if cached:
            return cached

    # 先收集所有文件路径，全局排序后再计算（局部排序 ≠ 全局排序）
    all_files = []
    for root, dirs, files in os.walk(skill_dir):
        # 排除目录
        dirs[:] = [d for d in dirs if d not in _DIGEST_EXCLUDE_DIRS]
        for f in files:
            # 排除后缀
            if any(f.endswith(suf) for suf in _DIGEST_EXCLUDE_SUFFIXES):
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, skill_dir).replace(os.sep, "/")
            # 符号链接拒绝
            if os.path.islink(full_path):
                raise ValueError(f"symlink not allowed in skill package: {rel_path}")
            all_files.append((rel_path, full_path))

    # 全局排序
    all_files.sort(key=lambda x: x[0])

    h = hashlib.sha256()
    total_size = 0

    for rel_path, full_path in all_files:
        # 单文件大小限制
        file_size = os.path.getsize(full_path)
        if file_size > _MAX_SINGLE_FILE:
            raise ValueError(
                f"file too large: {rel_path} ({file_size} bytes > {_MAX_SINGLE_FILE})")

        total_size += file_size
        if total_size > _MAX_PACKAGE_SIZE:
            raise ValueError(
                f"package too large: total {total_size} bytes > {_MAX_PACKAGE_SIZE}")

        h.update(rel_path.encode("utf-8"))
        h.update(b"\0")
        with open(full_path, "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")

    digest = "sha256:" + h.hexdigest()  # 完整 64 hex chars，不截断
    with _digest_cache_lock:
        _digest_cache[skill_dir] = digest

    return digest


# ===== frontmatter 解析 + schema 校验 =====

def parse_egress_rules(raw: list) -> list[EgressRule]:
    """解析 egress 规则列表。

    非法值抛 ValueError——拒绝整个 skill，不忽略。
    调用方应 catch ValueError 并 fail-closed（不放行任何 egress 域名）。
    """
    if not isinstance(raw, list):
        raise ValueError("egress must be a list")
    rules = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"egress[{i}]: must be a dict")
        host = str(item.get("host", "")).strip()
        if not host:
            raise ValueError(f"egress[{i}]: host is required")
        type_val = str(item.get("type", "domain")).strip()
        if type_val not in _VALID_TYPES:
            raise ValueError(f"egress[{i}]: type must be 'domain' or 'ip', got '{type_val}'")
        ports_raw = item.get("ports")
        if not isinstance(ports_raw, list) or not ports_raw:
            raise ValueError(f"egress[{i}]: ports is required and must be non-empty list")
        try:
            ports = [int(p) for p in ports_raw]
        except (ValueError, TypeError) as e:
            raise ValueError(f"egress[{i}]: ports must be integers: {e}")
        for p in ports:
            if not (1 <= p <= 65535):
                raise ValueError(f"egress[{i}]: port {p} out of range 1-65535")
        methods_raw = item.get("methods", _DEFAULT_METHODS)
        if not isinstance(methods_raw, list) or not methods_raw:
            raise ValueError(f"egress[{i}]: methods must be non-empty list")
        methods = [str(m).upper() for m in methods_raw]
        for m in methods:
            if m not in _VALID_METHODS:
                raise ValueError(f"egress[{i}]: invalid method '{m}'")
        path_prefix = str(item.get("path_prefix", _DEFAULT_PATH_PREFIX))
        try:
            max_body = int(item.get("max_body", _DEFAULT_MAX_BODY))
        except (ValueError, TypeError) as e:
            raise ValueError(f"egress[{i}]: max_body must be integer: {e}")
        if max_body < 0:
            raise ValueError(f"egress[{i}]: max_body must be non-negative")
        redirect = str(item.get("redirect", _DEFAULT_REDIRECT))
        rules.append(EgressRule(
            host=host, type=type_val, ports=ports,
            methods=methods,
            path_prefix=path_prefix,
            max_body=max_body,
            redirect=redirect,
        ))
    return rules


# ===== IP 归一化 + 拒绝列表校验 =====

def normalize_ip(ip_str: str) -> ipaddress._BaseAddress:
    """归一化 IP——IPv4-mapped IPv6 转 IPv4 统一匹配。

    用 ipaddress.ip_address(x).ipv4_mapped or ip，不自写位运算。
    归一化后再查拒绝列表，顺序不能反。
    """
    ip = ipaddress.ip_address(ip_str)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped  # ::ffff:127.0.0.1 → 127.0.0.1
    return ip


def is_absolutely_denied(ip: ipaddress._BaseAddress) -> bool:
    """查绝对拒绝列表——永不可覆盖。"""
    for network in _ABSOLUTE_DENIED_NETWORKS:
        if ip in network:
            return True
    return False


def is_default_denied(ip: ipaddress._BaseAddress) -> bool:
    """查默认拒绝列表——Phase 1+2 无管理员例外，直接拒绝。Phase 3 补例外。"""
    for network in _DEFAULT_DENIED_NETWORKS:
        if ip in network:
            return True
    return False


_DEFAULT_DENIED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
]


def check_ip_against_deny_lists(ip_str: str) -> tuple[bool, str]:
    """IP 过拒绝列表。返回 (ok, reason)。

    ok=False 表示命中拒绝列表。
    先归一化再查，防止 IPv4-mapped 绕过。
    """
    try:
        ip = normalize_ip(ip_str)
    except ValueError as e:
        return False, f"invalid IP: {e}"
    if is_absolutely_denied(ip):
        return False, f"address in absolute deny list"
    if is_default_denied(ip):
        return False, f"address in default deny list (internal network)"
    return True, "ok"


# ===== 路径规范化 =====

def normalize_path(path: str) -> str:
    """路径规范化——防穿越。

    1. URL 解码（只做一次，不递归解码——避免 %252e 二次解码漏洞）
    2. 提取 path（去 query）
    3. 去除 .. 和 .
    4. 确保以 / 开头
    """
    decoded = unquote(path)
    parsed = urlparse(decoded)
    p = parsed.path
    parts = []
    for seg in p.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg and seg != ".":
            parts.append(seg)
    result = "/" + "/".join(parts)
    if not result.startswith("/"):
        return "/"
    return result


def check_path_prefix(rule_path_prefix: str, path: str) -> bool:
    """段边界匹配：/v1/ 匹配 /v1/foo，不匹配 /v1foo。"""
    normalized = normalize_path(path)
    prefix = rule_path_prefix
    if prefix == "/" or prefix == "":
        return True
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if not prefix.endswith("/"):
        prefix = prefix + "/"
    return normalized.startswith(prefix)


# ===== L7 校验函数 =====

def check_method(rule: EgressRule, method: str) -> bool:
    """方法校验——HTTP 标准转发模式做，CONNECT 隧道不做。"""
    return method.upper() in rule.methods


def check_port(rule: EgressRule, port: int) -> bool:
    """端口校验——HTTP 和 HTTPS 都做。"""
    return port in rule.ports


# ===== 最小策略引擎（Layer 2） =====

class PolicyEngine:
    """Layer 2 策略引擎——进程内实现。

    Phase 3：整包 digest + 管理员策略 + DB 查询 fail-closed。

    bwrap 只读 PolicyEngine.apply() 的输出，不直接解析 SKILL.md frontmatter。
    这保证升级时只改 PolicyEngine 内部逻辑，不动 bwrap/NetworkProxy。
    """

    def apply(self, skill_name: str, skill_id: int,
              raw_egress: list, skill_md_content: str = "",
              skill_dir: str = "") -> EgressPolicy | None:
        """Layer 1 声明 → Layer 2 审批 → per-task 策略。

        Phase 3 逻辑：
        1. 解析 raw_egress → EgressRule 列表（schema 校验，非法值拒绝整个 skill）
        2. 计算整包 digest（compute_skill_package_digest）
        3. 对比 DB content_hash → 不一致拒绝（fail-closed）
        4. 查 grape_t_egress_admin_policy → auto_approve / manual_approve / deny
           - manual_approve/deny → 返回 None（无出站模式启动）
           - DB 查询失败 → 返回 None（fail-closed，任务启动失败）
        5. 拒绝规则校验：绝对拒绝的 host 从规则中剔除
        6. 返回 EgressPolicy

        返回 None 表示审批失败（fail-closed → 不放行任何 egress 域名）。
        **None = 任务启动失败（fail-closed），不是"无策略放行"。**
        """
        if not raw_egress:
            return None

        # 1. 解析 + schema 校验
        try:
            rules = parse_egress_rules(raw_egress)
        except ValueError as e:
            log.warning(f"PolicyEngine: skill '{skill_name}' egress parse failed: {e}")
            return None

        if not rules:
            return None

        # 2. 计算整包 digest
        skill_digest = ""
        if skill_dir:
            try:
                skill_digest = compute_skill_package_digest(skill_dir)
            except ValueError as e:
                log.warning(f"PolicyEngine: skill '{skill_name}' digest failed: {e}")
                return None  # fail-closed
        else:
            # 无 skill_dir（旧路径）→ fallback SKILL.md digest
            skill_digest = "sha256:" + hashlib.sha256(
                skill_md_content.encode("utf-8")).hexdigest()

        # 3. 对比 DB content_hash
        db_hash = self._get_db_content_hash(skill_id)
        if db_hash and skill_digest and db_hash != skill_digest:
            log.warning(
                f"PolicyEngine: skill '{skill_name}' digest mismatch: "
                f"db={db_hash} actual={skill_digest}")
            # digest 不一致 → 拒绝（检测篡改）
            return None

        # 4. 查管理员策略
        admin_policy = self._match_admin_policy(skill_name, skill_id)
        if admin_policy in ("manual_approve", "deny"):
            log.info(
                f"PolicyEngine: skill '{skill_name}' admin_policy={admin_policy}, "
                f"egress denied (task runs without egress)")
            return None  # 无出站模式启动

        # 5. 拒绝规则校验——剔除绝对拒绝的 host
        filtered_rules = []
        for rule in rules:
            if rule.type == "ip":
                ok, reason = check_ip_against_deny_lists(rule.host)
                if not ok:
                    log.warning(
                        f"PolicyEngine: skill '{skill_name}' egress rule "
                        f"host={rule.host} rejected: {reason}")
                    continue
            else:
                try:
                    ip = ipaddress.ip_address(rule.host)
                    ok, reason = check_ip_against_deny_lists(rule.host)
                    if not ok:
                        log.warning(
                            f"PolicyEngine: skill '{skill_name}' egress rule "
                            f"host={rule.host} (IP literal in domain type) rejected: {reason}")
                        continue
                except ValueError:
                    pass
            filtered_rules.append(rule)

        if not filtered_rules:
            log.warning(f"PolicyEngine: skill '{skill_name}' all egress rules rejected by deny lists")
            return None

        # 6. 查管理员例外
        admin_exceptions = self._query_admin_exceptions(skill_name, skill_digest)

        log.info(
            f"PolicyEngine: skill '{skill_name}' (id={skill_id}) "
            f"applied {len(filtered_rules)} egress rules, digest={skill_digest}, "
            f"policy={admin_policy}, exceptions={len(admin_exceptions)}")

        return EgressPolicy(
            rules=filtered_rules,
            skill_id=str(skill_id) if skill_id else skill_name,
            skill_digest=skill_digest,
            admin_policy=admin_policy,
            admin_exceptions=admin_exceptions,
        )

    def _get_db_content_hash(self, skill_id: int) -> str:
        """从 API/DB 读 skill 的整包 digest。"""
        if not skill_id:
            return ""
        data = self._fetch_skill_egress(skill_id=skill_id)
        return data.get("content_hash", "")

    def _match_admin_policy(self, skill_name: str, skill_id: int) -> str:
        """查管理员策略。API/DB 失败 → 'manual_approve'（fail-closed）。"""
        data = self._fetch_skill_egress(skill_name=skill_name, skill_id=skill_id)
        policy = data.get("admin_policy", "")
        return policy if policy else "manual_approve"

    def _query_admin_exceptions(self, skill_name: str,
                                skill_digest: str) -> list:
        """查管理员例外。"""
        data = self._fetch_skill_egress(skill_name=skill_name)
        raw = data.get("admin_exceptions", [])
        exceptions = []
        for item in raw:
            ex_digest = item.get("skill_digest", "")
            scope = item.get("scope", "")
            if scope == "skill" and ex_digest and skill_digest and ex_digest != skill_digest:
                continue
            ports = item.get("ports", [])
            methods = item.get("methods", [])
            if isinstance(ports, str):
                import json
                try:
                    ports = json.loads(ports)
                except Exception:
                    ports = []
            if isinstance(methods, str):
                import json
                try:
                    methods = json.loads(methods)
                except Exception:
                    methods = ["GET"]
            exceptions.append({
                "host": item.get("host", ""), "host_type": item.get("host_type", ""),
                "ports": ports or [], "methods": methods or ["GET"],
                "scope": scope, "scope_target": item.get("scope_target", ""),
            })
        return exceptions

    def _fetch_skill_egress(self, skill_name: str = "", skill_id: int = 0) -> dict:
        """从服务端 API 获取 skill egress 策略（远程 worker 无 DB 连接）。

        优先调 /api/runtime/skill-egress API，API 不可用时 fallback 到本地 DB。
        缓存 60s。
        """
        import time as _time
        _key = f"{skill_name}:{skill_id}"
        _now = _time.time()
        if not hasattr(self, '_egress_cache'):
            self._egress_cache = {}
        if _key in self._egress_cache and _now - self._egress_cache[_key][1] < 60:
            return self._egress_cache[_key][0]
        try:
            import os as _os
            import requests as _requests
            server_url = _os.environ.get("GRAPE_SERVER_URL", "") or _os.environ.get("RUNTIME_SERVER_URL", "")
            token = _os.environ.get("RUNTIME_TOKEN", "")
            if server_url and token:
                resp = _requests.get(
                    f"{server_url}/api/runtime/skill-egress",
                    params={"skill_name": skill_name, "skill_id": skill_id},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._egress_cache[_key] = (data, _now)
                    return data
            raise Exception("no server_url/token, fallback to DB")
        except Exception:
            pass
        # DB fallback（仅本机 worker）
        result = {"content_hash": "", "admin_policy": "", "admin_exceptions": []}
        try:
            from src.kernel.bulk_read import reader
            with reader() as _r:
                if skill_id:
                    row = _r.execute(
                        "SELECT content_hash_package FROM skill WHERE id=? AND is_active=1",
                        (int(skill_id),)
                    ).fetchone()
                    if row and row[0]:
                        result["content_hash"] = row[0]
                if skill_name:
                    row = _r.execute(
                        "SELECT policy FROM grape_t_egress_admin_policy WHERE skill_name=? ORDER BY skill_scope DESC LIMIT 1",
                        (skill_name,)
                    ).fetchone()
                    if not row:
                        row = _r.execute(
                            "SELECT policy FROM grape_t_egress_admin_policy WHERE skill_name='*' LIMIT 1"
                        ).fetchone()
                    if row and row[0]:
                        result["admin_policy"] = row[0]
                rows = _r.execute(
                    "SELECT host, host_type, ports, methods, scope, scope_target, skill_digest "
                    "FROM grape_t_egress_admin_exception "
                    "WHERE is_active=1 AND (expires_at IS NULL OR expires_at > datetime('now'))"
                ).fetchall()
                for r in rows:
                    result["admin_exceptions"].append({
                        "host": r[0], "host_type": r[1], "ports": r[2],
                        "methods": r[3], "scope": r[4], "scope_target": r[5],
                        "skill_digest": r[6],
                    })
        except Exception as e:
            log.warning(f"PolicyEngine: _fetch_skill_egress DB fallback failed: {e}")
        self._egress_cache[_key] = (result, _now)
        return result


# ===== skill egress 加载入口 =====

def load_skill_egress(skill_name: str, skill_id: int = 0,
                      cwd: str = "") -> EgressPolicy | None:
    """从 skill SKILL.md 加载 egress 策略，经 PolicyEngine 审批。

    Feature flag 关闭时直接返回 None。
    bwrap 调这个函数，不直接解析 frontmatter——保证 Layer 2 分层。

    路径解析顺序：
    1. resolve_official_skill_path（本机 SKILLS_DIR）
    2. get_skill_path（DB skill_id → SKILLS_DIR）
    3. cwd/.skills/{skill_name}/SKILL.md（远端 worker 物化路径）

    返回 None 表示无 egress 策略（fail-closed → 不放行任何 egress 域名）。
    """
    # Feature flag——默认关闭
    if not os.environ.get("SANDBOX_EGRESS_ENABLED"):
        return None

    if not skill_name:
        return None

    # 解析 SKILL.md 路径
    skill_md_path = ""
    try:
        from src.kernel.config import resolve_official_skill_path
        skill_md_path = resolve_official_skill_path(skill_name)
    except Exception:
        pass

    if not skill_md_path and skill_id:
        try:
            from src.domains.mgmt.user.db import get_skill_path
            skill_md_path = get_skill_path(skill_id) or ""
        except Exception:
            pass

    # 3. 远端 worker 物化路径：cwd/.skills/{skill_name}/SKILL.md
    if not skill_md_path and cwd:
        _candidate = os.path.join(cwd, ".skills", skill_name, "SKILL.md")
        if os.path.isfile(_candidate):
            skill_md_path = _candidate

    if not skill_md_path:
        log.warning(f"load_skill_egress: SKILL.md not found for '{skill_name}' (id={skill_id})")
        return None

    # 读 SKILL.md
    try:
        with open(skill_md_path, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        log.warning(f"load_skill_egress: read failed for {skill_md_path}: {e}")
        return None

    # skill 目录（整包 digest 用）
    skill_dir = os.path.dirname(skill_md_path)

    # 解析 frontmatter
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None  # 无 frontmatter

    import yaml
    try:
        fm = yaml.safe_load(parts[1])
    except Exception as e:
        log.warning(f"load_skill_egress: YAML parse failed for '{skill_name}': {e}")
        return None

    if not isinstance(fm, dict):
        return None

    raw_egress = fm.get("egress")
    if not isinstance(raw_egress, list):
        return None  # 无 egress 声明

    # skill frontmatter 不允许声明 rate_limits（只能管理员/主控设置）
    if "rate_limits" in fm:
        log.warning(f"load_skill_egress: skill '{skill_name}' frontmatter contains rate_limits, rejecting")
        return None

    # 调 PolicyEngine 审批
    engine = PolicyEngine()
    return engine.apply(
        skill_name=skill_name,
        skill_id=skill_id,
        raw_egress=raw_egress,
        skill_md_content=content,
        skill_dir=skill_dir,
    )
