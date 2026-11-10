"""按服务基址诊断（Issue #1858）。

与 prompt 构建职责分离，供平台侧 A2A 自调用告警、CLI 指引与回归用例复用：
- 端点路径 → 服务名（vcs / issue / a2a）映射；
- 404/405 时给出候选 base_url、来源与非 0 退出码提示。
"""

import json

# 任务 env 中三面基址键（与 prompt_builder 注入保持一致）。
SERVICE_BASE_ENV_KEYS = {
    "vcs": "GRAPE_VCS_BASE_URL",
    "issue": "GRAPE_ISSUE_BASE_URL",
    "a2a": "GRAPE_A2A_BASE_URL",
}

SERVICE_ENDPOINT_PREFIXES = (
    ("/api/a2a", "a2a"),
    ("/api/git-repo", "vcs"),
    ("/api/issues", "issue"),
)


def service_for_endpoint(endpoint: str) -> str:
    """端点路径 → 服务名（vcs/issue/a2a）；非三面端点返回空串。"""
    path = (endpoint or "").split("?", 1)[0]
    for prefix, service in SERVICE_ENDPOINT_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return service
    return ""


def format_base_url_candidates(endpoint: str = "", runtime_id: int = 0) -> str:
    """生成 404/405 诊断文案：按端点前缀给出候选 base_url 与来源。

    与 grape-cli #151 的候选地址提示对齐；平台侧自调用告警复用，明确非 0 退出码，
    禁止调用方静默回退 localhost。解析入口延迟导入，避免与 prompt_builder 循环依赖。
    """
    from src.prompt_builder import resolve_service_base_env

    env = resolve_service_base_env(runtime_id)
    try:
        sources = json.loads(env.get("GRAPE_BASE_URL_SOURCES") or "{}")
    except Exception:
        sources = {}
    service = service_for_endpoint(endpoint)
    services = [service] if service else list(SERVICE_BASE_ENV_KEYS)
    lines = []
    for svc in services:
        key = SERVICE_BASE_ENV_KEYS[svc]
        lines.append(f"{key}={env.get(key, '')} (source={sources.get(svc, '')})")
    return ("候选 base_url: " + "；".join(lines) +
            "；base_url 不可达或端点返回 404/405 时请显式指定 --base-url，"
            "命令将以非 0 退出码返回")