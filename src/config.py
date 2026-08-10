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
WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/workspaces"))
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/logs"))
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

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
        # 自动检测并填入 webhook_url（全局配置 → meta 表）
        def _detect_webhook_url():
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
                        return f"http://{ip}:8080/webhook/gitcode"
                except:
                    pass
            try:
                hostname = socket.gethostname()
                ip = socket.gethostbyname(hostname)
                if ip and not ip.startswith("127.") and not ip.startswith("192.168."):
                    return f"http://{ip}:8080/webhook/gitcode"
            except:
                pass
        try:
            from src.user.db import get_meta_value, set_meta_value
            if not get_meta_value("webhook_url"):
                url = _detect_webhook_url()
                if url:
                    set_meta_value("webhook_url", url)
                    log.info(f"Auto-detected webhook URL: {url}")
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


def get_agent_backend(skill_name: str = "") -> str:
    return "opencode"


# ====== 登录白名单 ======

LOGIN_WHITELIST_PATH = os.path.join(CONFIG_DIR, "login-whitelist.json")


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


# 延迟到 main.py 中显式调用，避免循环导入
# _load_accounts()
