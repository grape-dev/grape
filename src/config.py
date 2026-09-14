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

# ── 飞书 Bot Lease（集群部署，保证同一 bot 同一时刻只有一个实例连 WebSocket）──
BOT_LEASE_DISABLED = os.environ.get("BOT_LEASE_DISABLED", "false").lower() in ("1", "true", "yes")
BOT_LEASE_TTL = int(os.environ.get("BOT_LEASE_TTL", "90"))
BOT_LEASE_RENEW_INTERVAL = int(os.environ.get("BOT_LEASE_RENEW_INTERVAL", "20"))
BOT_LEASE_SWEEP_INTERVAL = int(os.environ.get("BOT_LEASE_SWEEP_INTERVAL", "10"))
# 本地缓存根（磁盘 LRU zip 缓存；默认 data/cache/storage）
OBS_LOCAL_CACHE = os.environ.get("OBS_LOCAL_CACHE", "")

# Cookie 安全——生产 HTTPS 默认 secure=True，开发 HTTP 配 COOKIE_SECURE=false
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")

# ====== 数据库路径（可通过环境变量指向独立测试库，防止测试污染生产库）======
DB_PATH = os.environ.get("DEVOPS_DB_PATH", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/sessions.db")))

# ====== 多账户配置（从 DB 加载） ======
ACCOUNTS: dict[str, dict] = {}
_DEFAULT_ACCOUNT = ""


def get_account(name=None) -> dict:
    """按名称获取账户配置；无匹配返回空 dict（不兜底 default）"""
    return ACCOUNTS.get(name or "", {})


def _load_accounts():
    """从 user_accounts 表加载账户配置"""
    global ACCOUNTS, _DEFAULT_ACCOUNT
    ACCOUNTS = {}
    try:
        from src.user.db import get_all_user_accounts
        for a in get_all_user_accounts():
            ACCOUNTS[a["name"]] = a
        # 自动检测并填入 webhook_url / server_base_url（全局配置 → meta 表）
        def _detect_server_base():
            methods = [
                "https://api.ipify.org?format=json",
                "http://api.ipify.org?format=json",
                "https://api.my-ip.io/ip.json",
            ]
            for url in methods:
                try:
                    resp = urllib.request.urlopen(url, timeout=3)
                    data = json.loads(resp.read())
                    ip = data.get("ip", "")
                    if ip:
                        return f"http://{ip}:8080"
                except:
                    pass
            try:
                hostname = socket.gethostname()
                ip = socket.gethostbyname(hostname)
                if ip and not ip.startswith("127.") and not ip.startswith("192.168."):
                    return f"http://{ip}:8080"
            except:
                pass
            return ""
        try:
            from src.user.db import get_meta_value, set_meta_value
            _cfg = read_server_config()
            # webhook_url（GitCode 回调注册用）：configs/server-config.json 显式配置 > 自动检测
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
            # server_base_url（A2A/tasks 等协议端点基址）：配置文件 > 自动检测
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
        log.info(f"Loaded {len(ACCOUNTS)} accounts from DB")
    except Exception as e:
        log.warning(f"Failed to load accounts from DB: {e}")


def save_accounts(new_accounts: list):
    """将账户列表写入 DB 并重载"""
    from src.user.db import save_user_account, delete_user_account
    existing = set(ACCOUNTS.keys())
    new_names = set()
    for a in new_accounts:
        name = a.get("name", "")
        if not name:
            continue
        save_user_account(name, a)
        new_names.add(name)
    for n in existing - new_names:
        delete_user_account(n)
    _load_accounts()


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


def is_gitcode_user_allowed(gitcode_login: str) -> bool:
    """白名单判定（GitCode 用户名，非昵称）。

    优先查 DB（login_whitelist 表），降级读 login-whitelist.json 文件。
    - DB 表存在且有数据 → gitcode_login 必须在表中
    - DB 不可用 → 降级文件逻辑
    - 文件缺失 / whitelist 为空 → 全部放行（默认开放）
    - 读取异常 → 保守放行（避免误锁所有用户）
    """
    if not gitcode_login:
        return False
    # 优先查 DB
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            count = c.execute("SELECT COUNT(*) FROM login_whitelist").fetchone()[0]
            if count > 0:
                row = c.execute("SELECT 1 FROM login_whitelist WHERE gitcode_username=?", (gitcode_login,)).fetchone()
                return row is not None
    except Exception:
        pass  # DB 不可用，降级文件
    # 降级：读文件
    try:
        if not os.path.exists(LOGIN_WHITELIST_PATH):
            return True
        with open(LOGIN_WHITELIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        wl = data.get("whitelist") if isinstance(data, dict) else None
        if not wl:
            return True
        return gitcode_login in wl
    except Exception:
        return True


def is_github_user_allowed(github_login: str) -> bool:
    """白名单判定（GitHub 用户名）。

    优先查 DB（login_whitelist 表 github_username 列），降级读 login-whitelist.json 文件。
    - DB 表存在且有数据 → github_login 必须在 github_username 列中
    - DB 不可用 → 降级文件逻辑
    - 文件缺失 / whitelist 为空 → 全部放行（默认开放）
    - 读取异常 → 保守放行（避免误锁所有用户）
    """
    if not github_login:
        return False
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            count = c.execute("SELECT COUNT(*) FROM login_whitelist").fetchone()[0]
            if count > 0:
                row = c.execute("SELECT 1 FROM login_whitelist WHERE github_username=?", (github_login,)).fetchone()
                return row is not None
    except Exception:
        pass
    try:
        if not os.path.exists(LOGIN_WHITELIST_PATH):
            return True
        with open(LOGIN_WHITELIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        wl = data.get("whitelist") if isinstance(data, dict) else None
        if not wl:
            return True
        return github_login in wl
    except Exception:
        return True


# ====== 技能目录解析（按 agent_type 一层分组：skills/<agent_type>/<skill>/SKILL.md）======
_OFFICIAL_AGENT_TYPES = ("vod", "issue", "coding", "qa", "sre", "chat")


def resolve_official_skill_path(skill_name: str) -> str:
    """官方技能 SKILL.md 绝对路径（skills/<agent_type>/<skill>/SKILL.md）。

    依 skill-description.json 的 agent_type/path 映射解析（skill-description-sync.md），
    兼容旧平铺布局（找不到 agent_type 目录时回退 skills/<skill>/SKILL.md）。
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
                _cand = os.path.join(SKILLS_DIR, _path, "SKILL.md")
                if os.path.isfile(_cand):
                    return _cand
        for _at in _OFFICIAL_AGENT_TYPES:
            _cand = os.path.join(SKILLS_DIR, _at, skill_name, "SKILL.md")
            if os.path.isfile(_cand):
                return _cand
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
# 默认开启（关 = 回滚，设 GRAPE_AGENT_SANDBOX=off）；bwrap 沙箱隔离 agent 子进程。
# 启用后 agent 子进程套 bwrap：只挂「基础系统 + 引擎运行时 + 工位 cwd」，
# 敏感路径（configs/、data/sessions.db、.env、/root、~/.grape、其他仓库/issue）天然不可见。
GRAPE_AGENT_SANDBOX = os.environ.get("GRAPE_AGENT_SANDBOX", "on").lower() in ("1", "true", "on", "yes")

# bwrap 白名单（只读）——基础系统：动态库 / CA 证书 / DNS / nsswitch / 基础命令。
# 注意：/run 必须挂（/etc/resolv.conf 是 symlink → /run/systemd/resolve/stub-resolv.conf，
# 缺 /run 则沙箱内 DNS 解析失败、出网全 000）。绝对不放行 configs/、data/、WORKSPACE 其他仓库、/root、~/.grape 等敏感/越界路径。
# /etc 不整体挂载（含 ssh 密钥/数据库密码/nginx 配置等敏感文件），改为白名单子集（BWRAP_RO_ETC_SAFE）。
# /opt 不整体挂载（含 grape/grape-admin 等敏感目录），只挂 /opt/node（worker 上 node 符号链接目标）。
BWRAP_RO_SYSTEM = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/run")
BWRAP_RO_SYSTEM_OPT = ("/opt/node",)  # Node.js 符号链接目标（ro-bind-try，不存在时跳过）

# /etc 安全子集（借鉴 Shadi/isolate 的 safeEtcMounts）— 只挂程序运行必需的文件，
# 不含 ssh 密钥/shadow/nginx 配置/数据库密码等敏感文件。用 --ro-bind-try（不存在时跳过）。
# 注意：/etc/resolv.conf 在 WSL 等环境中可能是符号链接指向另一个挂载点，
# --ro-bind 符号链接会失败。--ro-bind-try 不报错但可能挂载失败。
# 如果部署到 WSL 环境，需要实现 cross-mount repair（先 --tmpfs 占位再 --ro-bind 真实目标）。
# grape 当前部署目标是 Linux（非 WSL），resolv.conf 是普通文件，不受影响。
BWRAP_RO_ETC_SAFE = (
    "/etc/ssl",                  # CA 证书（HTTPS 验证）
    "/etc/ca-certificates",      # CA 证书（Debian/Ubuntu）
    "/etc/pki",                  # CA 证书（RHEL/CentOS）
    "/etc/resolv.conf",          # DNS 解析
    "/etc/hosts",                # 本地 DNS 覆盖
    "/etc/nsswitch.conf",        # 名称解析顺序
    "/etc/localtime",            # 时区
    "/etc/ld.so.cache",          # 动态链接器缓存
    "/etc/ld.so.conf",           # 动态链接器配置
    "/etc/ld.so.conf.d",         # 动态链接器配置目录
)

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
    # codex: 运行时写 ~/.codex（sqlite/sessions/logs/history）+ ~/.cache（缓存）
    "codex": ("~/.codex", "~/.cache"),
}


# ====== Agent 沙箱网络隔离（Issue #139）======

# 网络隔离开关（默认 off，灰度开启）
GRAPE_SANDBOX_NET = os.environ.get("GRAPE_SANDBOX_NET", "off").lower() in ("1", "true", "on", "yes")

# 沙箱白名单 — 环境变量作为 DB 不可用时的降级默认值

# cgroup 限额（0 = 不限制；memory 0 = 自动取宿主机 50%）
# 支持 K8s 格式：GRAPE_SANDBOX_MEM_LIMIT_MB=2Gi, GRAPE_SANDBOX_CPU_QUOTA=500m
# 内联解析逻辑（不 import cgroup，避免循环导入）
_MEM_SUFFIX = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
               "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}

def _parse_mem_env(raw: str) -> int:
    """解析内存量 → MB。支持 256Mi/1Gi/512M/2048（纯数字=字节）。"""
    raw = raw.strip()
    if raw.isdigit():
        return int(raw) // (1024 * 1024) if int(raw) > 10000 else int(raw)
    for suffix, mult in _MEM_SUFFIX.items():
        if raw.endswith(suffix):
            num = raw[:-len(suffix)].strip()
            if num.isdigit():
                return int(num) * mult // (1024 * 1024)
    return 0

def _parse_cpu_env(raw: str) -> int:
    """解析 CPU → 毫核（1000=1核）。支持 1/0.5/500m。"""
    raw = raw.strip()
    if raw.endswith("m"):
        num = raw[:-1].strip()
        return int(num) if num.isdigit() else 0
    try:
        return int(float(raw) * 1000)
    except ValueError:
        return 0

GRAPE_SANDBOX_MEM_LIMIT_MB = _parse_mem_env(os.environ.get("GRAPE_SANDBOX_MEM_LIMIT_MB", "0"))
GRAPE_SANDBOX_CPU_QUOTA = _parse_cpu_env(os.environ.get("GRAPE_SANDBOX_CPU_QUOTA", "0"))
GRAPE_SANDBOX_MAX_PIDS = int(os.environ.get("GRAPE_SANDBOX_MAX_PIDS", "0"))

# 审计日志路径（空 = 默认 {WORKSPACE_DIR}/logs/sandbox-audit.jsonl）
SANDBOX_AUDIT_LOG = os.environ.get("SANDBOX_AUDIT_LOG", "")
SANDBOX_AUDIT_HMAC_KEY = os.environ.get("SANDBOX_AUDIT_HMAC_KEY", "")

# 凭据遮蔽清单（逗号分隔的 env 变量名；P0 默认空 — 不遮蔽任何凭据）
SANDBOX_CREDENTIAL_ENV_VARS = [
    v.strip() for v in os.environ.get("SANDBOX_CREDENTIAL_ENV_VARS", "").split(",")
    if v.strip()
]

# 沙箱白名单 — 环境变量作为 DB 不可用时的降级默认值
# 生产环境通过 DB 表 sandbox_whitelist 管理（见 src/api/db_setup.py）
# 环境变量仅用于首次启动 / DB 不可用时降级
SANDBOX_WHITELIST_FALLBACK = [
    d.strip() for d in os.environ.get(
        "GRAPE_SANDBOX_WHITELIST",
        "gitcode.com,api.gitcode.com,raw.gitcode.com,"
        "github.com,api.github.com,"
        "opengw.clouddeveloper.club,api.deepseek.com,"
        "api.modelarts-maas.com,api.openai.com,"
        "registry.npmmirror.com,"
        "pypi.tuna.tsinghua.edu.cn,"
        "obs.cn-north-4.myhuaweicloud.com"
    ).split(",") if d.strip()
]

# 兼容：保留 SANDBOX_WHITELIST 名称供现有代码引用（= FALLBACK）
SANDBOX_WHITELIST = SANDBOX_WHITELIST_FALLBACK
