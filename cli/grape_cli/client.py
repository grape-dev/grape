"""HTTP 客户端：三通道认证 + httpx 封装。"""
import base64

import httpx

from grape_cli import config


class CliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class ApiClient:
    def __init__(self, base_url: str = "", token: str = "", channel: str = "bearer"):
        self.base_url, self.token = config.resolve(base_url, token)
        self.channel = channel  # bearer / PRIVATE-TOKEN / access_token
        if not self.token:
            raise CliError("缺少 token：请设置 GRAPE_SESSION_TOKEN（任务级）/ GRAPE_API_TOKEN（用户级），或 ~/.grape/config.json 的 token")

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Accept": "application/json"}
        if self.channel == "bearer":
            h["Authorization"] = f"Bearer {self.token}"
        elif self.channel == "private-token":
            h["PRIVATE-TOKEN"] = self.token
        return h

    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body: dict | None = None) -> tuple[int, dict]:
        url = self.base_url + path
        query = None
        if params:
            query = {k: v for k, v in params.items() if v not in (None, "", [])}
        if self.channel == "access_token":
            query = dict(query or {})
            query["access_token"] = self.token
        try:
            r = httpx.request(method.upper(), url, params=query or None,
                              headers=self._headers(), json=json_body or None, timeout=30)
        except httpx.HTTPError as e:
            raise CliError(f"base_url 不可达：{self.base_url}（{e}）")
        if r.status_code == 401:
            raise CliError("认证失败：token 无效或已过期")
        try:
            data = r.json()
        except Exception:
            data = r.text
        if r.status_code >= 400:
            msg = data.get("message") if isinstance(data, dict) else str(data)
            raise CliError(f"HTTP {r.status_code}: {msg or '请求失败'}", code=r.status_code)
        return r.status_code, data


def basic_auth(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()