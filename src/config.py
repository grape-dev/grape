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

# ====== 模块变量（从 bot_accounts 表加载） ======
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
    """从 bot_accounts 表加载账户配置，并设置模块级变量"""
    global ACCOUNTS, _DEFAULT_ACCOUNT
    global GITCODE_API_TOKEN, GITCODE_WEBHOOK_TOKEN, BOT_USERNAMES
    global FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_BOT_NAME
    global HUAWEICLOUD_SDK_AK, HUAWEICLOUD_SDK_SK
    global CODEARTS_CLI_AK, CODEARTS_CLI_SK

    ACCOUNTS = {}
    try:
        from src.user.db import get_all_bot_accounts, save_bot_account
        for a in get_all_bot_accounts():
            ACCOUNTS[a["name"]] = a
        # 自动检测并填入 webhook_url
        def _detect_webhook_url():
            try:
                resp = urllib.request.urlopen("https://api.ipify.org?format=json", timeout=5)
                ip = json.loads(resp.read())["ip"]
                return f"http://{ip}:8080/webhook/gitcode"
            except:
                pass
            try:
                hostname = socket.gethostname()
                ip = socket.gethostbyname(hostname)
                if ip and not ip.startswith("127."):
                    return f"http://{ip}:8080/webhook/gitcode"
            except:
                pass
            return ""
        detected = False
        for n, a in ACCOUNTS.items():
            if not a.get("webhook_url"):
                if not detected:
                    url = _detect_webhook_url()
                    if url:
                        a["webhook_url"] = url
                        save_bot_account(n, a)
                        detected = True
                        log.info(f"Auto-detected webhook URL: {url}")
                    else:
                        break
                else:
                    a["webhook_url"] = list(ACCOUNTS.values())[0].get("webhook_url", "")
                    save_bot_account(n, a)
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
    from src.user.db import save_bot_account, delete_bot_account
    existing = set(ACCOUNTS.keys())
    new_names = set()
    for a in new_accounts:
        name = a.get("name", "")
        if not name:
            continue
        save_bot_account(name, a)
        new_names.add(name)
    # 删除不存在的
    for n in existing - new_names:
        delete_bot_account(n)
    _load_accounts()


def resolve_account_by_webhook_token(token: str) -> str | None:
    from src.user.db import resolve_bot_account_by_webhook_token
    return resolve_bot_account_by_webhook_token(token)


# ====== 事件路由 ======
EVENT_ROUTING = {}
_EVENT_ROUTING_FALLBACK = "devops-event-ops"


def get_domain_skill(evt_type: str, repo_key: str = "", user_id: int = 0) -> str:
    # 1. 用户自定义映射（最高优先级）
    if user_id and repo_key:
        from src.user.db import get_skill_for_event
        skill = get_skill_for_event(user_id, repo_key, evt_type)
        if skill:
            return skill
    # 2. 全局 repo_routing
    repo_skills = EVENT_ROUTING.get("repo_routing", {}).get(repo_key, {}) if repo_key else {}
    if repo_key and evt_type in repo_skills:
        return repo_skills[evt_type]
    # 3. 全局 routing（最低优先级）
    return EVENT_ROUTING.get("routing", {}).get(evt_type, _EVENT_ROUTING_FALLBACK)


def _load_routing_config():
    global EVENT_ROUTING, _EVENT_ROUTING_FALLBACK
    _routing_path = os.path.join(CONFIG_DIR, "event-routing.json")
    if not os.path.exists(_routing_path):
        log.info(f"Event routing config not found at {_routing_path}, fallback={_EVENT_ROUTING_FALLBACK}")
        return
    try:
        with open(_routing_path, encoding="utf-8") as _f:
            _routing_data = json.load(_f)
        EVENT_ROUTING = _routing_data
        _EVENT_ROUTING_FALLBACK = _routing_data.get("fallback_skill", "devops-event-ops")
        repo_count = len(_routing_data.get("repo_routing", {}))
        log.info(f"Event routing loaded: {len(_routing_data.get('routing', {}))} rules, {repo_count} repo overrides, fallback={_EVENT_ROUTING_FALLBACK}")
    except Exception as _e:
        log.warning(f"Failed to load event routing config: {_e}")


# ====== Agent 后端路由 ======
AGENT_ROUTING = {"default_backend": "opencode", "skill_backends": {}}


def get_agent_backend(skill_name: str) -> str:
    return AGENT_ROUTING.get("skill_backends", {}).get(skill_name, AGENT_ROUTING.get("default_backend", "opencode"))


def _load_agent_routing_config():
    global AGENT_ROUTING
    _path = os.path.join(CONFIG_DIR, "agent-routing.json")
    if not os.path.exists(_path):
        log.info(f"Agent routing config not found at {_path}, using defaults")
        return
    try:
        with open(_path, encoding="utf-8") as _f:
            AGENT_ROUTING = json.load(_f)
        log.info(f"Agent routing loaded: default={AGENT_ROUTING.get('default_backend')}, {len(AGENT_ROUTING.get('skill_backends', {}))} skill mappings")
    except Exception as _e:
        log.warning(f"Failed to load agent routing config: {_e}")


# ====== 启动时加载 ======
_load_routing_config()
_load_agent_routing_config()
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
