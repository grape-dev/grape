"""Bot 配置：全局常量、事件路由、多账户配置（从 accounts.json 读取）"""
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
WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/workspaces"))
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/logs"))
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ====== 模块变量（从 user_accounts 表加载） ======
GITCODE_API_TOKEN = ""
GITCODE_WEBHOOK_TOKEN = ""
BOT_USERNAMES: list[str] = []
FEISHU_APP_ID = ""
FEISHU_APP_SECRET = ""
FEISHU_BOT_NAME = "DevOps Bot"
HUAWEICLOUD_SDK_AK = ""
HUAWEICLOUD_SDK_SK = ""
CODEARTS_CLI_AK = ""
CODEARTS_CLI_SK = ""

# ====== 多账户配置（从 DB 加载） ======
ACCOUNTS: dict[str, dict] = {}
_DEFAULT_ACCOUNT = "default"


def _load_accounts():
    """从 user_accounts 表加载账户配置，并设置模块级变量"""
    global ACCOUNTS, _DEFAULT_ACCOUNT
    global GITCODE_API_TOKEN, GITCODE_WEBHOOK_TOKEN, BOT_USERNAMES
    global FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_BOT_NAME
    global HUAWEICLOUD_SDK_AK, HUAWEICLOUD_SDK_SK
    global CODEARTS_CLI_AK, CODEARTS_CLI_SK

    ACCOUNTS = {}
    try:
        from src.user.db import get_all_user_accounts, save_user_account
        for a in get_all_user_accounts():
            ACCOUNTS[a["name"]] = a
        # 自动检测并填入 webhook_url
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
        detected = False
        for n, a in ACCOUNTS.items():
            if not a.get("webhook_url"):
                if not detected:
                    url = _detect_webhook_url()
                    if url:
                        a["webhook_url"] = url
                        save_user_account(n, a)
                        detected = True
                        log.info(f"Auto-detected webhook URL: {url}")
                    else:
                        break
                else:
                    a["webhook_url"] = list(ACCOUNTS.values())[0].get("webhook_url", "")
                    save_user_account(n, a)
    except Exception as e:
        log.warning(f"Failed to load accounts from DB: {e}")
        return

    default = get_account()
    GITCODE_API_TOKEN = default.get("gitcode_api_token", "")
    GITCODE_WEBHOOK_TOKEN = default.get("gitcode_webhook_token", "")
    BOT_USERNAMES = default.get("bot_usernames", [])
    if isinstance(BOT_USERNAMES, str):
        BOT_USERNAMES = [u.strip() for u in BOT_USERNAMES.split(",") if u.strip()]
    FEISHU_APP_ID = default.get("feishu_app_id", "")
    FEISHU_APP_SECRET = default.get("feishu_app_secret", "")
    FEISHU_BOT_NAME = default.get("feishu_bot_name", "DevOps Bot")
    HUAWEICLOUD_SDK_AK = default.get("huawei_cloud_ak", "")
    HUAWEICLOUD_SDK_SK = default.get("huawei_cloud_sk", "")
    CODEARTS_CLI_AK = default.get("CODEARTS_CLI_AK", "")
    CODEARTS_CLI_SK = default.get("CODEARTS_CLI_SK", "")
    log.info(f"Loaded {len(ACCOUNTS)} accounts from DB, default={_DEFAULT_ACCOUNT}")


def get_account(name=None) -> dict:
    return ACCOUNTS.get(name or _DEFAULT_ACCOUNT, ACCOUNTS.get("default", {}))


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
    # 删除不存在的
    for n in existing - new_names:
        delete_user_account(n)
    _load_accounts()


def resolve_account_by_webhook_token(token: str) -> str | None:
    from src.user.db import resolve_user_account_by_webhook_token
    return resolve_user_account_by_webhook_token(token)


# ====== 事件路由 ======
# ====== 事件路由 ======


def get_domain_skill(evt_type: str, repo_key: str = "", user_id: int = 0) -> str:
    """从 DB 读取事件路由（托管给 src.api.routing）"""
    try:
        from src.api.routing import get_domain_skill as _db_route
        return _db_route(evt_type, repo_key, user_id)
    except Exception:
        return ""


def get_agent_backend(skill_name: str = "") -> str:
    """返回默认后端引擎"""
    return "opencode"


_load_accounts()

# 环境变量兜底——无论 _load_accounts 何时运行，模块加载时总会执行一次
import os as _os
for _var in ["GITCODE_API_TOKEN", "GITCODE_WEBHOOK_TOKEN", "FEISHU_APP_ID",
             "FEISHU_APP_SECRET", "HUAWEICLOUD_SDK_AK", "HUAWEICLOUD_SDK_SK",
             "CODEARTS_CLI_AK", "CODEARTS_CLI_SK"]:
    if not globals().get(_var):
        val = _os.environ.get(_var, "")
        if val:
            globals()[_var] = val
