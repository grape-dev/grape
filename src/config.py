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


_load_routing_config()
_load_agent_routing_config()
