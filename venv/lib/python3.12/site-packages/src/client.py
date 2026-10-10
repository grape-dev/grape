"""HTTP 客户端：三通道认证 + httpx 封装。"""
import base64

import httpx

from src import config


class CliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class ApiClient:
    def __init__(self, base_url: str = "", token: str = "", channel: str = "bearer",
                 timeout: int = 0, *, require_token: bool = True):
        self.base_url, self.token = config.resolve(base_url, token)
        self.channel = channel  # bearer / PRIVATE-TOKEN / access_token
        self.timeout = int(timeout or 0) or config.cli_timeout()
        # 契约拉取（/static/openapi.json）为公开端点，不需要 token
        if require_token and not self.token:
            raise CliError(
                "Missing token: set GRAPE_SESSION_TOKEN (task) / GRAPE_API_TOKEN (user), "
                "or the 'token' field in ~/.grape/config.json"
            )

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Accept": "application/json"}
        if self.token:
            if self.channel == "bearer":
                h["Authorization"] = f"Bearer {self.token}"
            elif self.channel == "private-token":
                h["PRIVATE-TOKEN"] = self.token
        return h

    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body: dict | None = None, files: dict | None = None,
                data: dict | None = None) -> tuple[int, dict]:
        url = self.base_url + path
        query = None
        if params:
            query = {k: v for k, v in params.items() if v not in (None, "", [])}
        if self.channel == "access_token" and self.token:
            query = dict(query or {})
            query["access_token"] = self.token
        try:
            r = httpx.request(
                method.upper(), url, params=query or None,
                headers=self._headers(),
                json=None if (files or data) else json_body,
                timeout=self.timeout,
                files=files or None,
                data=data or None,
            )
        except httpx.ConnectError as e:
            raise CliError(f"base_url unreachable: {self.base_url} ({e})")
        except httpx.TimeoutException as e:
            raise CliError(
                f"request timeout (> {self.timeout}s; increase with --timeout): "
                f"{self.base_url}{path}"
            )
        except httpx.HTTPError as e:
            raise CliError(f"HTTP error: {self.base_url}{path} ({e})")
        try:
            data = r.json()
        except Exception:
            data = r.text
        # 解析服务端错误信封：新格式 {"detail": {"code","message"}}（message 英文约定）
        # 兼容旧格式 {"message": ...} 与纯文本
        msg = ""
        detail_code = r.status_code
        if isinstance(data, dict):
            _d = data.get("detail")
            if isinstance(_d, dict):
                msg = str(_d.get("message") or "")
                detail_code = _d.get("code") or r.status_code
            else:
                msg = str(data.get("message") or "")
        elif isinstance(data, str):
            msg = data
        if r.status_code == 401:
            raise CliError(
                f"authentication failed: {msg or 'token invalid or expired'}; "
                "re-inject GRAPE_SESSION_TOKEN / GRAPE_API_TOKEN",
                code=r.status_code,
            )
        if r.status_code >= 400:
            hint = ""
            if r.status_code == 403 and path.startswith("/api/repos"):
                hint = " (git-repo API: session token (tk_) only; check repo write permission)"
            elif r.status_code == 404 and path.startswith("/api/repos"):
                if "/contents" in path:
                    hint = " (path not found; try --path '' to list root)"
                else:
                    hint = " (check owner/repo/number)"
            code_txt = f" ({detail_code})" if detail_code != r.status_code else ""
            raise CliError(
                f"HTTP {r.status_code}{code_txt}: {msg or 'request failed'}{hint}",
                code=r.status_code,
            )
        return r.status_code, data


def basic_auth(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()