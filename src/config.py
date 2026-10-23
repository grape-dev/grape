"""Bot 配置：全局常量、事件路由、多账户配置"""
import os, json, logging, logging.handlers, urllib.request, socket
import ssl

# api.gitcode.com 被 GFW SNI 拦截（返回自签名证书），全局禁用 urllib SSL 验证。
# 影响所有用 urllib.request.urlopen 调 api.gitcode.com 的模块（event_pipeline / auth / repo_manager 等）。
# requests 库的调用在各调用点单独 verify=False。
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE
_https_handler = urllib.request.HTTPSHandler(context=_ssl_ctx)
_opener = urllib.request.build_opener(_https_handler)
urllib.request.install_opener(_opener)

# 抑制 requests 库的 InsecureRequestWarning
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass

# ====== 日志 ======
# 结构化 JSON 日志（参考 multica logger/logger.go）：
# - LOG_LEVEL 环境变量控制级别（debug/info/warn/error，默认 info）
# - JSON 格式输出，字段：timestamp, level, component, request_id, message
# - request_id 通过 ContextVar 注入（request_id_middleware 设置）
# - 保留 TimedRotatingFileHandler（按天轮转，保留30天）+ stderr 双输出

import contextvars

request_id_var: contextvars.ContextVar = contextvars.ContextVar("request_id", default="-")
repo_var: contextvars.ContextVar = contextvars.ContextVar("repo_key", default="")
user_var: contextvars.ContextVar = contextvars.ContextVar("user_id", default="")


class _RequestIdFilter(logging.Filter):
    """把 ContextVar 中的 request_id / repo_key / user_id 注入到每条 LogRecord 中。"""

    def filter(self, record):
        record.request_id = request_id_var.get()
        record.repo_key = repo_var.get()
        record.user_id = user_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    """JSON 行格式日志输出器。

    兼容已有 log.info("f-string") 调用——message 字段放原始文本，
    其余字段从 LogRecord / ContextVar 提取。

    HTTP 请求日志中间件通过 log.info(..., extra={...}) 注入结构化字段：
    method, path, status, duration_ms — 这些会作为独立 JSON 字段输出，
    而非塞在 message 文本中，便于 Loki/ELK 按字段过滤/聚合。
    """

    # extra 字段白名单：只有这些 LogRecord 属性会被提升为顶层 JSON 字段
    _EXTRA_FIELDS = ("method", "path", "status", "duration_ms", "user_id", "repo_key")

    def format(self, record):
        import time as _t
        log_entry = {
            "ts": _t.strftime("%Y-%m-%dT%H:%M:%S.", _t.gmtime(record.created))
                  + f"{record.msecs:03.0f}Z",
            "level": record.levelname,
            "component": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        # 提取 extra 字段（HTTP 请求日志中间件注入的结构化维度）
        for field in self._EXTRA_FIELDS:
            val = getattr(record, field, None)
            if val is not None:
                log_entry[field] = val
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, ensure_ascii=False)


def _parse_level(s: str) -> int:
    s = (s or "").strip().lower()
    if s == "debug":
        return logging.DEBUG
    if s in ("warn", "warning"):
        return logging.WARNING
    if s == "error":
        return logging.ERROR
    return logging.INFO


_LOG_LEVEL = _parse_level(os.environ.get("LOG_LEVEL", ""))
_LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/logs"))
os.makedirs(_LOG_DIR, exist_ok=True)

_file_handler = logging.handlers.TimedRotatingFileHandler(
    os.path.join(_LOG_DIR, "devops-bot.log"),
    when="midnight", interval=1, backupCount=30, encoding="utf-8",
)
_file_handler.setLevel(_LOG_LEVEL)
_file_handler.setFormatter(_JsonFormatter())
_file_handler.addFilter(_RequestIdFilter())

_stream_handler = logging.StreamHandler()
_stream_handler.setLevel(_LOG_LEVEL)
_stream_handler.setFormatter(_JsonFormatter())
_stream_handler.addFilter(_RequestIdFilter())

logging.basicConfig(
    level=_LOG_LEVEL,
    handlers=[_file_handler, _stream_handler],
)
log = logging.getLogger("devops-bot")
log.addFilter(_RequestIdFilter())

# ====== 目录常量 ======
SKILLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills"))
CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "configs"))
EXAMPLE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "examples"))


def _resolve_workspace_dir() -> str:
    """工作区根目录解析（用户要求 2026-08-12：worker 应写入自己家目录，而非共享代码目录）。

    优先级：
    1. 环境变量 DEVOPS_WORKSPACE_DIR（运维/测试显式指定，最优先）
    2. 运行用户家目录 ~/grape-workspaces —— 远程 Worker 部署在共享代码目录时，
       工作区跟随运行用户（如 /home/lvjuntao/grape-workspaces），无需与代码目录属主
       共享写权限（修复远程 Worker Permission denied 问题）
    3. 兜底：代码目录 data/workspaces（无家目录的系统服务场景）
    """
    env = os.environ.get("DEVOPS_WORKSPACE_DIR", "").strip()
    if env:
        return os.path.abspath(env)
    home = os.path.expanduser("~")
    if home and os.path.isdir(home):
        return os.path.join(home, "grape-workspaces")
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/workspaces"))


WORKSPACE_DIR = _resolve_workspace_dir()
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/logs"))
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# Chat 工作目录下发的逻辑基准（远端 worker _remap_cwd 的默认映射契约）。
# 服务端必须用逻辑基准下发而非本机 WORKSPACE_DIR，否则非 root 用户 worker
# （如 mlc）拿 /opt/grape-workspaces/... 映射不到 → 写 /root → Permission denied。
# _remap_cwd 默认将此值映射到 worker 运行用户的 home 目录。
CHAT_CWD_BASE = os.environ.get("CHAT_CWD_BASE", os.path.expanduser("~/grape_workspace"))

# ====== 对象存储（OBS）配置（见 docs/new-version2/obs-storage/obs-skill-storage-design.md §3 N1）======
# AK/SK 仅经环境变量注入（gitignored），不入配置文件、不入日志。
OBS_ENABLED = os.environ.get("OBS_ENABLED", "false").lower() in ("1", "true", "yes")
OBS_ENDPOINT = os.environ.get("OBS_ENDPOINT", "https://obs.cn-north-4.myhuaweicloud.com")
OBS_BUCKET = os.environ.get("OBS_BUCKET", "")
OBS_ACCESS_KEY = os.environ.get("OBS_ACCESS_KEY", "")
OBS_SECRET_KEY = os.environ.get("OBS_SECRET_KEY", "")
OBS_PREFIX = os.environ.get("OBS_PREFIX", "grape/prod")
OBS_URL_EXPIRES = int(os.environ.get("OBS_URL_EXPIRES", "300"))

# ── 技能本地存储兜底（OBS 未启用时单机可用，docs/weekly-06/skill-local-storage-fallback-design.md）──
# SKILL_STORAGE = auto|obs|local；auto=OBS 有则 OBS 否则本地；obs=仅 OBS；local=强制本地（单机）
SKILL_STORAGE = os.environ.get("SKILL_STORAGE", "auto").strip().lower()
# 技能对象本地权威落盘根（默认 <部署根>/data/skills/；多机集群请指向 OBS 或共享盘，勿用 local）
SKILL_STORAGE_DIR = os.path.abspath(os.environ.get("SKILL_STORAGE_DIR", os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "skills",)))

# ── 飞书 Bot Lease（集群部署，保证同一 bot 同一时刻只有一个实例连 WebSocket）──
BOT_LEASE_DISABLED = os.environ.get("BOT_LEASE_DISABLED", "false").lower() in ("1", "true", "yes")
BOT_LEASE_TTL = int(os.environ.get("BOT_LEASE_TTL", "180"))
BOT_LEASE_RENEW_INTERVAL = int(os.environ.get("BOT_LEASE_RENEW_INTERVAL", "30"))
BOT_LEASE_SWEEP_INTERVAL = int(os.environ.get("BOT_LEASE_SWEEP_INTERVAL", "10"))
# BOT_LEASE_GRACE：续约连续失败后进入宽限期，宽限期到才断开（抗瞬时 DB 抖动）
BOT_LEASE_GRACE = int(os.environ.get("BOT_LEASE_GRACE", "90"))
# BOT_LEASE_RENEW_FAIL_THRESHOLD：连续续约失败多少次（每次 = 一个 renew 周期）才进入宽限期
BOT_LEASE_RENEW_FAIL_THRESHOLD = int(os.environ.get("BOT_LEASE_RENEW_FAIL_THRESHOLD", "3"))
# IM_LEASE_STICKY：一致性哈希归属（均匀 + 粘性）。=0 退回"先到先得"旧模式（灰度回退）
IM_LEASE_STICKY = os.environ.get("IM_LEASE_STICKY", "true").lower() in ("1", "true", "yes")
# IM_INSTANCE_TIMEOUT：实例心跳超时（秒）——超过视为失联，其 bot 可被接管
IM_INSTANCE_TIMEOUT = int(os.environ.get("IM_INSTANCE_TIMEOUT", "45"))
# IM_STEAL_AFTER：lease 已过期但对存活实例的接管保护窗（秒）——超过才允许从存活实例强制接管（防假死）
IM_STEAL_AFTER = int(os.environ.get("IM_STEAL_AFTER", "60"))
# IM_ACQUIRE_COOLDOWN：抢到 lease 但连接失败后，多长时间内不重抢同一 bot（秒）
IM_ACQUIRE_COOLDOWN = int(os.environ.get("IM_ACQUIRE_COOLDOWN", "30"))
# IM_INSTANCE_HEARTBEAT：实例心跳/环 owner 过滤的虚拟节点数（一致性哈希均匀度）
IM_HASH_VNODES = int(os.environ.get("IM_HASH_VNODES", "50"))
# 本地缓存根（磁盘 LRU zip 缓存；默认 data/cache/storage）
OBS_LOCAL_CACHE = os.environ.get("OBS_LOCAL_CACHE", "")

# Cookie 安全——生产 HTTPS 默认 secure=True，开发 HTTP 配 COOKIE_SECURE=false
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")

# 注意：此处禁止 import src.auth.*（worker 包含 config.py 但不含 src/auth/，
# 顶层 import 会直接打崩 runtime-agent 启动，2026-10-14 五台 worker 心跳全断事故根因）。
# 服务端如需 is_production，直接 from src.auth.security_config import is_production。
# IS_PRODUCTION 常量已删除：全库零消费（死代码）。

# JWT 密钥安全门禁
_INSECURE_JWT_SECRETS = {"", "grape-dev-secret-change-in-production", "change-me-in-production"}

# 邮件服务配置（环境变量驱动）
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_TLS = os.environ.get("SMTP_TLS", "true").lower() in ("1", "true", "yes")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", "noreply@grape.dev").strip()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
GRAPE_DEV_VERIFICATION_CODE = os.environ.get("GRAPE_DEV_VERIFICATION_CODE", "").strip()

# ====== 数据库路径（可通过环境变量指向独立测试库，防止测试污染生产库）======
DB_PATH = os.environ.get("DEVOPS_DB_PATH", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/sessions.db")))

# ====== 多账户配置（从 DB 加载） ======
ACCOUNTS: dict[str, dict] = {}
_DEFAULT_ACCOUNT = ""


def get_account(name=None) -> dict:
    """按名称获取账户配置；无匹配返回空 dict（不兜底 default）"""
    return ACCOUNTS.get(name or "", {})


def _load_accounts():
    """D32-补丁：不再从 user_accounts 加载 ACCOUNTS 字典（死代码，无调用方）。
    保留 webhook_url / server_base_url 自动检测逻辑。
    """
    try:
        def _detect_server_base():
            # Issue #452: GRAPE_AUTO_DETECT_URL=false 时跳过公网 IP 探测，避免内网/受限网络启动拖慢约 40s
            _auto_detect = os.environ.get("GRAPE_AUTO_DETECT_URL", "true").lower().strip()
            if _auto_detect in ("0", "false", "no", "off"):
                log.info("_detect_server_base: skipped (GRAPE_AUTO_DETECT_URL=false)")
                try:
                    from src.user.db import set_meta_value
                    set_meta_value("webhook_url_detect_failed", "1")
                except Exception:
                    pass
                return ""
            # Issue #372 §15.4: 端口从 GRAPE_PORT 环境变量读取，默认 3000（非 8080）
            _port = os.environ.get("GRAPE_PORT", "3000")
            # Issue #452: 公网探测超时从 3s 降至 2s，减少内网等待
            _probe_timeout = float(os.environ.get("GRAPE_DETECT_TIMEOUT", "2"))
            methods = [
                "https://api.ipify.org?format=json",
                "http://api.ipify.org?format=json",
                "https://api.my-ip.io/ip.json",
            ]
            for url in methods:
                try:
                    resp = urllib.request.urlopen(url, timeout=_probe_timeout)
                    data = json.loads(resp.read())
                    ip = data.get("ip", "")
                    if ip:
                        return f"http://{ip}:{_port}"
                except Exception as e:
                    log.warning(f"_detect_server_base: {url} failed: {e}")
            try:
                hostname = socket.gethostname()
                ip = socket.gethostbyname(hostname)
                if ip and not ip.startswith("127.") and not ip.startswith("192.168."):
                    return f"http://{ip}:{_port}"
                log.warning(f"_detect_server_base: hostname '{hostname}' resolves to {ip} (loopback/private, skipped)")
            except Exception as e:
                log.warning(f"_detect_server_base: hostname resolution failed: {e}")
            # Issue #372 §14.2.1: 全部失败 → 写标记供设置页展示
            try:
                from src.user.db import set_meta_value
                set_meta_value("webhook_url_detect_failed", "1")
                log.warning("_detect_server_base: all detection methods failed, webhook_url will be empty until configured or detected from Host header")
            except Exception:
                pass
            return ""
        try:
            from src.user.db import get_meta_value, set_meta_value
            _cfg = read_server_config()
            if not get_meta_value("webhook_url"):
                _wh = (_cfg.get("webhook_url") or "").strip()
                if _wh:
                    set_meta_value("webhook_url", _wh)
                    log.info(f"Configured webhook URL from server-config.json: {_wh}")
                else:
                    base = _detect_server_base()
                    if base:
                        set_meta_value("webhook_url", base + "/webhook/gitcode")
                        log.info(f"Auto-detected webhook URL: {base}/webhook/gitcode")
            if not get_meta_value("server_base_url"):
                _sb = (_cfg.get("server_base_url") or "").strip().rstrip("/")
                if _sb:
                    set_meta_value("server_base_url", _sb)
                    log.info(f"Configured server base URL from server-config.json: {_sb}")
                else:
                    base = _detect_server_base()
                    if base:
                        set_meta_value("server_base_url", base)
                        log.info(f"Auto-detected server base URL: {base}")
        except Exception:
            pass
    except Exception as e:
        log.warning(f"Failed to load server config: {e}")


def save_accounts(new_accounts: list):
    """D32-补丁：已废弃——ACCOUNTS 字典无调用方，不再写 user_accounts 老表。"""
    pass


# ====== 事件路由 ======


def get_domain_agent(evt_type: str, repo_key: str = "", user_id: int = 0) -> str:
    try:
        from src.api.routing import get_domain_agent as _db_route
        return _db_route(evt_type)
    except Exception:
        return ""


# ====== 登录白名单 ======

LOGIN_WHITELIST_PATH = os.path.join(CONFIG_DIR, "login-whitelist.json")

# ====== API 慢请求监控 ======

SLOW_REQUEST_THRESHOLD_MS = int(os.environ.get("SLOW_REQUEST_THRESHOLD_MS", "500"))

# server-config.json（gitignored）：server_base_url / webhook_url 显式配置，见 DEPLOYMENT-REMOTE.md §4
SERVER_CONFIG_PATH = os.path.join(CONFIG_DIR, "server-config.json")

# ===== Chat 流式响应超时（Issue #399） =====
# CHAT_STREAM_TIMEOUT：chat agent SSE 流式响应总超时（秒）——DB 轮询/SSE 等待 agent 输出的最长时间
CHAT_STREAM_TIMEOUT = int(os.environ.get("CHAT_STREAM_TIMEOUT", "300"))
# CHAT_STREAM_HEARTBEAT_INTERVAL：等待期间「思考中」心跳反馈间隔（秒）
CHAT_STREAM_HEARTBEAT_INTERVAL = int(os.environ.get("CHAT_STREAM_HEARTBEAT_INTERVAL", "15"))


def read_server_config() -> dict:
    """读取 configs/server-config.json（gitignored，动态读，改后无需重启）。

    返回 {"server_base_url": "...", "webhook_url": "..."}；文件缺失/损坏返回 {}。
    """
    try:
        if os.path.exists(SERVER_CONFIG_PATH):
            with open(SERVER_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def get_chat_stream_timeout() -> int:
    """chat agent SSE 流式响应总超时（秒，Issue #399）。

    优先读 server-config.json 的 chat_stream_timeout（动态，改后无需重启），
    缺失/非法时回退环境变量常量 CHAT_STREAM_TIMEOUT（默认 300）。
    """
    try:
        raw = read_server_config().get("chat_stream_timeout")
        if raw not in (None, ""):
            val = int(raw)
            if val > 0:
                return val
    except Exception:
        pass
    return CHAT_STREAM_TIMEOUT


_WL_ENABLED_CACHE = None
_WL_CACHE_TS = 0.0
_WL_CACHE_TTL = 60.0  # 60s TTL—meta 配置变更频率极低


def _is_login_whitelist_enabled() -> bool:
    """登录白名单是否启用。

    显式开关：meta 表 login_whitelist_enabled（true=启用，false=关闭）。
    兼容旧部署：meta 未设置时回退到旧逻辑——login_whitelist 表有数据则启用，
    并打 WARNING 日志提示管理员显式设置开关。
    异常时默认关闭（不拦截），避免误锁所有用户。
    结果缓存 60 秒（meta 配置变更频率极低）。
    """
    import time as _t
    global _WL_ENABLED_CACHE, _WL_CACHE_TS
    now = _t.time()
    if _WL_ENABLED_CACHE is not None and (now - _WL_CACHE_TS) < _WL_CACHE_TTL:
        return _WL_ENABLED_CACHE
    try:
        from src.user.db import get_meta_value
        val = get_meta_value("login_whitelist_enabled")
        if val is not None and val != "":
            result = val.lower() in ("true", "1", "yes")
        else:
            # meta 未设置——兼容旧部署：表有数据则隐式启用
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                count = c.execute("SELECT COUNT(*) FROM login_whitelist").fetchone()[0]
                if count > 0:
                    log.warning(
                        "login_whitelist has %d entries but meta 'login_whitelist_enabled' is not set. "
                        "Falling back to legacy implicit enable. "
                        "Set meta 'login_whitelist_enabled=true' to explicitly enable, "
                        "or 'false' to disable.", count
                    )
                    result = True
                else:
                    result = False
    except Exception:
        result = False  # 异常时默认关闭（不拦截）
    _WL_ENABLED_CACHE = result
    _WL_CACHE_TS = now
    return result


def invalidate_login_whitelist_cache():
    """Invalidate the login_whitelist_enabled cache.

    Called by admin/db.py set_login_whitelist_enabled() so that toggling
    the whitelist takes effect immediately instead of waiting up to 60s.
    """
    global _WL_ENABLED_CACHE, _WL_CACHE_TS
    _WL_ENABLED_CACHE = None
    _WL_CACHE_TS = 0.0


def is_user_allowed(whitelist_column: str, login: str) -> bool:
    """通用白名单判定——需显式启用才校验，未启用时全部放行。

    开关：meta 表 login_whitelist_enabled（true=启用，未设置=关闭）
    启用时：按平台列独立校验 login_whitelist 表
    未启用时：直接返回 True
    降级：DB 不可用时读 login-whitelist.json 文件
    - 文件缺失 / whitelist 为空 → 全部放行
    - 读取异常 → 放行（避免误锁所有用户）
    """
    if not login:
        return False

    # 显式开关检查——关闭时全部放行
    if not _is_login_whitelist_enabled():
        return True

    # 列名安全校验（防 SQL 注入——列名来自 provider 配置）
    import re
    if not re.match(r'^[a-z_]+$', whitelist_column):
        log.warning(f"is_user_allowed: invalid whitelist_column='{whitelist_column}'")
        return False
    # 查 DB——按平台列独立校验
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            # P2-6: 检查表非空，避免开关启用但表为空时锁死所有用户
            count = c.execute("SELECT COUNT(*) FROM login_whitelist").fetchone()[0]
            if count == 0:
                log.warning("login_whitelist_enabled=true but table is empty — allowing all logins")
                return True
            row = c.execute(
                f"SELECT 1 FROM login_whitelist WHERE LOWER({whitelist_column})=LOWER(?)", (login,)
            ).fetchone()
            return row is not None
    except Exception:
        pass  # DB 不可用或列不存在，降级文件
    # 降级：读文件
    try:
        if not os.path.exists(LOGIN_WHITELIST_PATH):
            return True
        with open(LOGIN_WHITELIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        wl = data.get("whitelist") if isinstance(data, dict) else None
        if not wl:
            return True
        return login.lower() in [w.lower() for w in wl]
    except Exception:
        return True


def is_gitcode_user_allowed(gitcode_login: str) -> bool:
    """白名单判定（GitCode 用户名）——委托通用函数。"""
    return is_user_allowed("gitcode_username", gitcode_login)


def is_github_user_allowed(github_login: str) -> bool:
    """白名单判定（GitHub 用户名）——委托通用函数。"""
    return is_user_allowed("github_username", github_login)


# ====== 技能目录解析（按 agent_type 一层分组：skills/<agent_type>/<skill>/SKILL.md）======
_OFFICIAL_AGENT_TYPES = ("vod", "issue", "coding", "qa", "sre", "chat", "auto")


def resolve_official_skill_path(skill_name: str) -> str:
    """官方技能 SKILL.md 绝对路径。

    解析顺序：
    1. skill-description.json 的 path 字段（3 层：agent_type/sub_scene/skill）
    2. path 去掉中间层（2 层兼容：agent_type/skill）
    3. skills/<agent_type>/<skill>/SKILL.md（按 agent_type 扫描）
    4. skills/<skill>/SKILL.md（旧平铺兼容）
    返回空串表示未找到。
    """
    if not skill_name:
        return ""
    try:
        _cfg = os.path.join(CONFIG_DIR, "skill-description.json")
        if os.path.isfile(_cfg):
            with open(_cfg, "r", encoding="utf-8") as _f:
                _data = json.load(_f)
            _info = _data.get(skill_name) or {}
            _path = (_info.get("path") or "").strip().replace("/", os.sep)
            if _path:
                # 1. 完整 path（3 层：agent_type/sub_scene/skill）
                _cand = os.path.join(SKILLS_DIR, _path, "SKILL.md")
                if os.path.isfile(_cand):
                    return _cand
                # 2. 去掉中间层（2 层兼容：agent_type/skill）
                _parts = _path.split(os.sep)
                if len(_parts) >= 3:
                    _cand2 = os.path.join(SKILLS_DIR, _parts[0], _parts[-1], "SKILL.md")
                    if os.path.isfile(_cand2):
                        return _cand2
        # 3. 按 agent_type 扫描（2 层）
        for _at in _OFFICIAL_AGENT_TYPES:
            _cand = os.path.join(SKILLS_DIR, _at, skill_name, "SKILL.md")
            if os.path.isfile(_cand):
                return _cand
        # 4. 旧平铺（1 层）
        _legacy = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
        if os.path.isfile(_legacy):
            return _legacy
    except Exception:
        pass
    return ""


_os = os

def replace_skill_dir_placeholders(prompt: str, base_dir: str = "") -> str:
    """将 prompt 中 `{{SKILL_DIR}}/<skill>/SKILL.md` 替换为实际技能路径。

    用于 direct（服务端本地）执行：技能按 agent_type 分组存储在
    `skills/<agent_type>/<skill>/SKILL.md`。兼容旧平铺布局与 worker 侧
    只展开 `{{SKILL_DIR}}` 的调用（worker 物化后仍是 <skill>/ 平铺）。

    - 匹配到技能名 → 替换为 resolve_official_skill_path 结果；
    - 未匹配（如 {{SKILL_DIR}} 单独出现）→ 替换为 base_dir / SKILLS_DIR。
    """
    import re as _re

    base = base_dir or SKILLS_DIR

    def _sub(m):
        name = m.group(1)
        p = resolve_official_skill_path(name)
        return p if p else _os.path.join(base, name, "SKILL.md")

    prompt = _re.sub(r"\{\{SKILL_DIR\}\}/([^/\s]+)/SKILL\.md", _sub, prompt)
    return prompt.replace("{{SKILL_DIR}}", base)


# 延迟到 main.py 中显式调用，避免循环导入
# _load_accounts()


# ====== Agent 文件系统隔离（组件2：bwrap 锁）======
# 默认关闭（开 = 设 GRAPE_AGENT_SANDBOX=on）；bwrap 沙箱隔离 agent 子进程。
# 关闭原因：Issue #168 — bwrap 白名单不完整，DevStage worker 三引擎全部失败。
# 白名单修复由 Issue #156 系统性解决后方可重新开启。
GRAPE_AGENT_SANDBOX = os.environ.get("GRAPE_AGENT_SANDBOX", "off").lower() in ("1", "true", "on", "yes")

# bwrap 白名单（只读）——基础系统：动态库 / CA 证书 / DNS / nsswitch / 基础命令。
# 注意：/run 必须挂（/etc/resolv.conf 是 symlink → /run/systemd/resolve/stub-resolv.conf，
# 缺 /run 则沙箱内 DNS 解析失败、出网全 000）。绝对不放行 configs/、data/、WORKSPACE 其他仓库、/root、~/.grape 等敏感/越界路径。
# /etc 整体挂载（含 resolv.conf / hosts / ssl 证书等基础配置）。
BWRAP_RO_SYSTEM = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/run")

# bwrap 白名单（只读）——引擎运行时，按后端名展开；二进制 + 配置 + 运行时（~ 由 sandbox 模块展开）。
# 三引擎白名单不同（§7 兼容性清单），分别覆盖；不存在目录自动跳过。
# 注：codearts 是 opencode 的华为云定制版（Node 应用），依赖模型与 opencode 一致：
#   ~/.nvm(Node 运行时) + ~/.local(codearts 二进制) + ~/.cache(Node 缓存) + ~/.codeartsdoer(专属配置)；
#   未实测（当前环境无 codearts 引擎），目录按推断补齐，留待真实环境 K6 验证。
BWRAP_RO_ENGINE_COMMON = ("/usr/local",)
BWRAP_RO_ENGINE = {
    "opencode": ("~/.nvm", "~/.config/opencode", "~/.cache", "~/.opencode"),
    "codearts": ("~/.codeartsdoer", "~/.nvm", "~/.local", "~/.cache"),
    "hermes": ("~/.local",),
    # hwcloud: 二进制在 ~/.local/bin（同 hermes），settings/会话由 OPENAGENT_CLI_CONFIG
    # 指到 ~/.cache/hwcloud-acp-homes/<cwd-hash>/（BWRAP_RW_ENGINE rw 挂载）。
    "hwcloud": ("~/.local",),
    # codex: 二进制 /usr/local/bin/codex 已在 COMMON(/usr/local) 覆盖。
    # ~/.codex 和 ~/.cache 在 BWRAP_RW_ENGINE 中 rw 挂载（需写 sessions/logs/cache）。
}

# bwrap 白名单（读写）——引擎缓存；hermes overlay 由 sandbox 模块按 md5(cwd) 动态计算绑定。
# 注意：hermes 的 HERMES_HOME 必须 rw —— hermes_acp._build_overlay_home 把 HERMES_HOME 的
# logs/skills/plugins/cache 等子目录 symlink 进 overlay，hermes 运行时读写全落到 HERMES_HOME 真身；
# ro 会导致写 logs 报 [Errno 30] Read-only file system（2026-09-03 实测踩坑）。
# codearts 底层同 opencode 用 bun，需 /var/cache/bun-compile 写权限（JIT .so 缓存）。
_hermes_home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
BWRAP_RW_ENGINE = {
    "opencode": ("/var/cache/bun-compile",),
    "codearts": ("~/.codeartsdoer", "~/.cache/codearts-tasks", "/var/cache/codearts-tasks", "/var/cache/bun-compile"),
    "hermes": (_hermes_home,),
    # hwcloud: 会话 sqlite + per-task settings.json 全在 ~/.cache/hwcloud-acp-homes/
    # （hwcloud_acp._task_home_root，按 cwd-hash 分片；sandbox 模块同 hermes overlay
    # 按 md5(cwd) 动态 rw-bind，这里是根目录兜底挂载）
    "hwcloud": ("~/.cache/hwcloud-acp-homes",),
    # codex: 运行时写 ~/.codex（sqlite/sessions/logs/history）+ ~/.cache（缓存）
    "codex": ("~/.codex", "~/.cache"),
}




# ===== Admin Console =====
ADMIN_CONSOLE_URL = os.environ.get("ADMIN_CONSOLE_URL", "").rstrip("/")
ADMIN_BETA_APPLY_API_KEY = os.environ.get("ADMIN_BETA_APPLY_API_KEY", "")
