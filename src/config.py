"""Bot 配置：环境变量、全局常量、事件路由"""
import os, json, logging, logging.handlers

# .env 加载
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            _k, _v = _k.strip(), _v.strip().strip("\"'")
            if not os.environ.get(_k):
                os.environ[_k] = _v

GITCODE_API_TOKEN = os.environ.get("GITCODE_API_TOKEN", "")
GITCODE_WEBHOOK_TOKEN = os.environ.get("GITCODE_WEBHOOK_TOKEN", "")
BOT_USERNAMES = [u.strip() for u in os.environ.get("BOT_USERNAMES", "").split(",") if u.strip()]
SKILLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills"))
CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "configs"))
WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/workspaces"))
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/logs"))
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# 日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.handlers.TimedRotatingFileHandler(
        os.path.join(os.path.dirname(__file__), "..", "data/devops-bot.log"),
        when="midnight", interval=1, backupCount=30, encoding="utf-8"
    ), logging.StreamHandler()]
)
log = logging.getLogger("devops-bot")

# 事件路由
EVENT_ROUTING = {}
_EVENT_ROUTING_FALLBACK = "devops-event-ops"

# Agent 后端路由
AGENT_ROUTING = {"default_backend": "opencode", "skill_backends": {}}


def get_domain_skill(evt_type: str) -> str:
    return EVENT_ROUTING.get(evt_type, _EVENT_ROUTING_FALLBACK)


def get_agent_backend(skill_name: str) -> str:
    """返回 skill 对应的 Agent 后端名称（默认 opencode）"""
    return AGENT_ROUTING.get("skill_backends", {}).get(skill_name, AGENT_ROUTING.get("default_backend", "opencode"))


def _load_routing_config():
    global EVENT_ROUTING, _EVENT_ROUTING_FALLBACK
    _routing_path = os.path.join(CONFIG_DIR, "event-routing.json")
    if not os.path.exists(_routing_path):
        log.info(f"Event routing config not found at {_routing_path}, fallback={_EVENT_ROUTING_FALLBACK}")
        return
    try:
        with open(_routing_path, encoding="utf-8") as _f:
            _routing_data = json.load(_f)
        EVENT_ROUTING = _routing_data.get("routing", {})
        _EVENT_ROUTING_FALLBACK = _routing_data.get("fallback_skill", "devops-event-ops")
        log.info(f"Event routing loaded: {len(EVENT_ROUTING)} rules, fallback={_EVENT_ROUTING_FALLBACK}")
    except Exception as _e:
        log.warning(f"Failed to load event routing config: {_e}")


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


# ====== 多账户配置 ======
ACCOUNTS = {}
_DEFAULT_ACCOUNT = "default"


def _load_accounts():
    global ACCOUNTS, _DEFAULT_ACCOUNT
    path = os.path.join(CONFIG_DIR, "accounts.json")
    if not os.path.exists(path):
        ACCOUNTS["default"] = {
            "gitcode_api_token": GITCODE_API_TOKEN,
            "gitcode_webhook_token": GITCODE_WEBHOOK_TOKEN,
            "feishu_app_id": os.environ.get("FEISHU_APP_ID", ""),
            "feishu_app_secret": os.environ.get("FEISHU_APP_SECRET", ""),
            "huawei_cloud_ak": os.environ.get("HUAWEICLOUD_SDK_AK", ""),
            "huawei_cloud_sk": os.environ.get("HUAWEICLOUD_SDK_SK", ""),
            "bot_usernames": BOT_USERNAMES,
        }
        log.info(f"Loaded single default account from .env (GITCODE_API_TOKEN={'YES' if GITCODE_API_TOKEN else 'NO'})")
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for a in data.get("accounts", []):
            a.setdefault("gitcode_api_token", os.environ.get("GITCODE_API_TOKEN", ""))
            a.setdefault("gitcode_webhook_token", os.environ.get("GITCODE_WEBHOOK_TOKEN", ""))
            a.setdefault("feishu_app_id", os.environ.get("FEISHU_APP_ID", ""))
            a.setdefault("feishu_app_secret", os.environ.get("FEISHU_APP_SECRET", ""))
            a.setdefault("bot_usernames", BOT_USERNAMES)
            a.setdefault("huawei_cloud_ak", os.environ.get("HUAWEICLOUD_SDK_AK", ""))
            a.setdefault("huawei_cloud_sk", os.environ.get("HUAWEICLOUD_SDK_SK", ""))
            ACCOUNTS[a["name"]] = a
        _DEFAULT_ACCOUNT = data.get("default", "default")
        log.info(f"Loaded {len(ACCOUNTS)} accounts from config, default={_DEFAULT_ACCOUNT}")
    except Exception as e:
        log.warning(f"Failed to load accounts config: {e}, falling back to .env")
        _load_accounts()  # retry as single-account


def get_account(name=None) -> dict:
    return ACCOUNTS.get(name or _DEFAULT_ACCOUNT, ACCOUNTS.get("default", {}))


def save_accounts(new_accounts: list):
    """将 accounts 列表写回 accounts.json 并重载"""
    global ACCOUNTS
    path = os.path.join(CONFIG_DIR, "accounts.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"default": _DEFAULT_ACCOUNT, "accounts": new_accounts}, f, indent=2, ensure_ascii=False)
    _load_accounts()


def resolve_account_by_webhook_token(token: str) -> str | None:
    for name, a in ACCOUNTS.items():
        if a.get("gitcode_webhook_token") == token:
            return name
    return None


_load_routing_config()
_load_agent_routing_config()
_load_accounts()
