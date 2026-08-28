"""Bot 配置：全局常量、事件路由、多账户配置"""
import os, json, logging, logging.handlers, urllib.request, socket

# ====== 日志 ======
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.handlers.TimedRotatingFileHandler(
        os.path.join(os.path.dirname(__file__), "..", "data/devops-bot.log"),
        when="midnight", interval=1, backupCount=30, encoding="utf-8"
    ), logging.StreamHandler()]
)
log = logging.getLogger("devops-bot")

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

# ====== 对象存储（OBS）配置（见 docs/new-version2/obs-storage/obs-skill-storage-design.md §3 N1）======
# AK/SK 仅经环境变量注入（gitignored），不入配置文件、不入日志。
OBS_ENABLED = os.environ.get("OBS_ENABLED", "false").lower() in ("1", "true", "yes")
OBS_ENDPOINT = os.environ.get("OBS_ENDPOINT", "https://obs.cn-north-4.myhuaweicloud.com")
OBS_BUCKET = os.environ.get("OBS_BUCKET", "")
OBS_ACCESS_KEY = os.environ.get("OBS_ACCESS_KEY", "")
OBS_SECRET_KEY = os.environ.get("OBS_SECRET_KEY", "")
OBS_PREFIX = os.environ.get("OBS_PREFIX", "grape/prod")
OBS_URL_EXPIRES = int(os.environ.get("OBS_URL_EXPIRES", "300"))
# 本地缓存根（磁盘 LRU zip 缓存；默认 data/cache/storage）
OBS_LOCAL_CACHE = os.environ.get("OBS_LOCAL_CACHE", "")

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

    - 配置文件缺失 / whitelist 为空 → 全部放行（默认开放）
    - 读取异常 → 保守放行（避免误锁所有用户）
    - 否则 gitcode_login 必须在 whitelist 内才放行
    """
    if not gitcode_login:
        return False
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
