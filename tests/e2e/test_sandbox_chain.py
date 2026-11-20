"""沙箱全链 E2E：装 worker → 探测识别 → 场景绑定 → 触发 → A2A 流转。

默认跳过（依赖外部环境）；设 GRAPE_E2E_SANDBOX=1 且提供以下环境变量后启用：

    GRAPE_CONTROLLER      控制器地址——必须是 worker 目标机可达的地址
                          （如 http://<controller-host>:8081，不要用 127.0.0.1）
    GRAPE_ADMIN_USER      管理员账号（默认 admin）
    GRAPE_ADMIN_PASSWORD  管理员密码
    GRAPE_REPO            目标仓库 full_name（如 org/repo）
    GRAPE_CUBE_ENDPOINT   沙箱平台地址（如 https://cube.example.com）
    GRAPE_WORKER_HOST     worker 安装目标机 SSH 地址（如 <worker-host>）
    GRAPE_WORKER_USER     SSH 用户（如 <worker-user>）
    GRAPE_WORKER_PASSWORD SSH 密码

前置：目标机已装 python3/curl；控制器 configs/crypto.json 就绪；
沙箱平台（CubeMaster）资源充足（130597 = 资源耗尽，部分模板探测失败
属可容忍，S3 断言只要求每 Provider 至少一个模板探到引擎）。

依赖：requests、paramiko（worker 安装通道）。
步骤/断言明细见 docs/sandbox/e2e-sandbox-chain-a2a-use-case.md。
"""
import hashlib
import json
import os
import time
import uuid

import pytest
import requests

pytestmark = pytest.mark.skipif(
    os.environ.get("GRAPE_E2E_SANDBOX") != "1",
    reason="sandbox full-chain E2E — set GRAPE_E2E_SANDBOX=1 to run",
)

CTRL = os.environ.get("GRAPE_CONTROLLER", "http://127.0.0.1:8081")
REPO = os.environ.get("GRAPE_REPO", "org/repo")
ADMIN_USER = os.environ.get("GRAPE_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("GRAPE_ADMIN_PASSWORD", "")
CUBE = os.environ.get("GRAPE_CUBE_ENDPOINT", "https://cube.example.com")
WHOST = os.environ.get("GRAPE_WORKER_HOST", "")
WUSER = os.environ.get("GRAPE_WORKER_USER", "root")
WPASSWORD = os.environ.get("GRAPE_WORKER_PASSWORD", "")
REPO_KEY = f"gitcode:{REPO}"
PROBE_TIMEOUT = 900   # 探测批（含 130597 重试退避）最长等待
TASK_TIMEOUT = 900    # 单节点任务最长等待

CRED: dict = {}       # scope -> {"id":..., "token":...}


def _http(method, path, token=None, body=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, f"{CTRL}{path}", headers=headers,
                            data=json.dumps(body) if body is not None else None,
                            timeout=timeout)


def _ssh(cmd):
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(WHOST, username=WUSER, password=WPASSWORD, timeout=10)
    _, stdout, stderr = cli.exec_command(cmd, timeout=600)
    out = stdout.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    cli.close()
    assert rc == 0, f"remote cmd failed rc={rc}: {cmd}\n{out[-400:]}"
    return out


def _wait_until(fn, timeout, interval=15, desc=""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    pytest.fail(f"timeout waiting: {desc}; last={last}")


@pytest.fixture(scope="module")
def admin_token():
    r = _http("POST", "/api/auth/login", body={
        "username": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text[:200]
    return r.json()["token"]


@pytest.fixture(scope="module")
def webhook_secret(admin_token):
    """managed_repos 的 webhook secret（经管理 API；响应结构按部署校准）。"""
    def _via_api():
        r = _http("GET", "/api/repos/managed", token=admin_token)
        data = r.json()
        items = data if isinstance(data, list) else (data.get("repos") or data.get("data") or [])
        for it in items:
            if it.get("full_name") == REPO:
                return it.get("gitcode_webhook_token") or it.get("webhook_secret") or ""
        return ""
    return _wait_until(lambda: _via_api() or None, 60, desc="webhook secret")


def test_01_create_credentials_and_bind_platform(admin_token):
    """S1 全局+仓库级沙箱凭据各一；Provider 行自动登记（带 provider_uid）；
    随后绑定沙箱平台 endpoint（模拟用户在 UI 填写；worker 端重试循环会
    自动接手——endpoint 晚于 worker 启动配置是受支持场景）。"""
    for scope, extra in (("global", {}), ("repo", {"repo_key": REPO_KEY})):
        r = _http("POST", "/api/settings/runtime-credentials", token=admin_token,
                  body={"name": f"e2e-sb-{scope}-{uuid.uuid4().hex[:6]}",
                        "cred_type": "sandbox", "platform": "cubesandbox",
                        "scope": scope, **extra})
        assert r.status_code == 200, r.text[:200]
        data = r.json()
        assert data["token"].startswith("rta_")
        CRED[scope] = {"id": data["id"], "token": data["token"]}

    # Provider 自动登记（一凭据一 Provider，带 provider_uid）→ 绑平台 endpoint
    r = _http("GET", "/api/settings/sandbox-provider-configs", token=admin_token)
    cfgs = r.json()
    items = cfgs if isinstance(cfgs, list) else (cfgs.get("configs") or cfgs.get("data") or [])
    for scope in ("global", "repo"):
        cid = CRED[scope]["id"]
        prov = next((p for p in items
                     if p.get("runtime_credential_id") == cid), None)
        assert prov, f"provider row missing for cred {cid}"
        assert prov.get("provider_uid"), f"provider_uid empty for cred {cid} (regression)"
        assert (prov.get("endpoint") or "").strip() == "", \
            "provider endpoint should start empty until user binds platform"
        pu = _http("PUT", f"/api/settings/sandbox-providers/{prov['id']}",
                   token=admin_token, body={"endpoint": CUBE})
        assert pu.status_code < 400, pu.text[:200]
        CRED[scope]["provider_id"] = prov["id"]


def test_02_install_two_workers():
    """S2 同机双 worker 一键安装（新凭据 → 新目录 sandbox-worker-<cred_uid8>）。"""
    for scope in ("GLOBAL", "REPO"):
        token = CRED[scope]["token"]
        out = _ssh(
            f"curl -sSL {CTRL}/api/runtime/install -o /tmp/iw-{scope.lower()}.sh && "
            f"nohup bash /tmp/iw-{scope.lower()}.sh --server {CTRL} --token {token} "
            f"> /tmp/iw-{scope.lower()}.log 2>&1 & echo started")
        assert "started" in out


def test_03_workers_registered_and_engines_detected():
    """S3 全自动探测（endpoint 晚配场景）：每凭据至少一个模板 enabled=1 且
    探到引擎（130597 挡掉的模板可缺——重试循环继续兜底）。"""
    def _check():
        for scope in ("global", "repo"):
            token = CRED[scope]["token"]
            r = requests.get(f"{CTRL}/api/settings/sandbox-templates?page_size=0",
                             headers={"Authorization": f"Bearer {token}"}, timeout=15)
            tpls = (r.json() or {}).get("templates") or []
            with_engines = [t for t in tpls
                            if t.get("enabled") and (t.get("engines") or "")]
            if not with_engines:
                return None
        return True
    assert _wait_until(_check, PROBE_TIMEOUT, desc="per-credential template with detected engines")


def test_04_engines_online_with_uid(admin_token):
    """S4 每凭据至少一个沙箱引擎 online 且 engine_uid 非空。"""
    def _check():
        for scope in ("global", "repo"):
            token = CRED[scope]["token"]
            r = requests.get(f"{CTRL}/api/settings/agent-runtimes?type=sandbox",
                             headers={"Authorization": f"Bearer {token}"}, timeout=15)
            rows = _unwrap(r)
            mine = [x for x in rows
                    if x.get("status") == "online" and x.get("engine_uid")]
            if not mine:
                return None
            CRED[scope]["engine_uid"] = mine[0].get("engine_uid")
        return True
    assert _wait_until(_check, 300, desc="sandbox engines online with engine_uid")


def test_05_scene_bind_trigger_vod_on_sandbox(admin_token, webhook_secret):
    """S5 场景绑定沙箱引擎 + 直发任务型 issue → 沙箱引擎认领 → completed。"""
    engine_uid = CRED["global"].get("engine_uid") or ""
    assert engine_uid, "no sandbox engine uid from S4"
    _bind_active_scene_nodes(admin_token, engine_uid)

    iid = int(time.time()) % 100000
    payload = {
        "action": "open", "action_desc": "opened", "event": "issue",
        "repository": {"full_name": REPO, "path_with_namespace": REPO,
                       "owner": {"username": REPO.split("/")[0]}},
        "issue": {"iid": iid, "number": 0,
                  "title": f"[e2e] add e2e-{uuid.uuid4().hex[:6]}.txt (dev task)",
                  "body": "Add the file in repo root with content: ok. Then hand off to the next stage.",
                  "state": "open", "user": {"username": REPO.split("/")[0]}},
    }
    r = requests.post(f"{CTRL}/webhook/gitcode",
                      headers={"Content-Type": "application/json",
                               "X-GitCode-Token": webhook_secret,
                               "X-Gitcode-Event": "Issue Hook"},
                      data=json.dumps(payload), timeout=20)
    assert r.status_code == 200, r.text[:200]

    def _completed():
        for t in _cred_tasks_all():
            if t.get("agent_type") == "vod":
                if t.get("status") == "completed":
                    return t
                if t.get("status") == "failed":
                    pytest.fail(f"vod task failed: {t.get('errors') or t.get('result_text')}")
        return None
    task = _wait_until(_completed, TASK_TIMEOUT, interval=20,
                       desc="vod task completed on sandbox engine")
    CRED["vod_task"] = task.get("id") or ""


def test_06_a2a_handoff_advanced(admin_token):
    """S6 A2A 推进：vod 完成后 a2a_event 产生，下一节点任务被认领（running/completed）。"""
    vod_task = CRED.get("vod_task", "")

    def _advanced():
        for t in _cred_tasks_all():
            if t.get("id") == vod_task:
                continue
            if t.get("agent_type") != "vod" and t.get("status") in ("running", "completed"):
                return t
            if t.get("status") == "failed" and t.get("agent_type") != "vod":
                pytest.fail(f"next-node task failed: {t.get('errors') or t.get('result_text')}")
        return None
    nxt = _wait_until(_advanced, 600, interval=20, desc="a2a advanced next-node task")
    assert nxt.get("agent_type") in ("issue", "coding", "qa", "sre"), nxt


# ---------- helpers ----------

def _unwrap(r):
    data = r.json()
    return data.get("runtimes") or data.get("data") or (data if isinstance(data, list) else [])


def _bind_active_scene_nodes(admin_token, engine_uid):
    """active 场景全节点绑定引擎（engine_uid，validator 兼容数值 id）。

    场景保存路由按部署版本可能是 PUT /api/scenes/{id} 或管道接口——先读场景
    with-pipeline 拿 nodes，再回写 engine_runtime_ids。
    """
    r = _http("GET", "/api/scenes/with-pipeline", token=admin_token)
    data = r.json()
    scenes = data if isinstance(data, list) else (data.get("scenes") or data.get("data") or [])
    target = None
    for s in scenes:
        if s.get("repo_key") == REPO_KEY and s.get("is_active"):
            target = s
    assert target, "no active scene for repo"
    nodes = target.get("nodes") or []
    for n in nodes:
        if n.get("enabled"):
            n["engine_runtime_ids"] = [engine_uid]
    sid = target.get("scene_id") or target.get("id")
    put = _http("PUT", f"/api/scenes/{sid}", token=admin_token, body={"nodes": nodes})
    if put.status_code >= 400:
        put = _http("PUT", f"/api/pipeline/scenes/{sid}",
                    token=admin_token, body={"nodes": nodes})
    assert put.status_code < 400, put.text[:200]


def _cred_tasks_all():
    """两个沙箱凭据聚合视角下的全部任务（runtime-admin-two-view §4.3 端点）。"""
    out, seen = [], set()
    for scope in ("global", "repo"):
        cid = CRED.get(scope, {}).get("id")
        if not cid:
            continue
        r = _http("GET", f"/api/runtime/credentials/{cid}/tasks", token=CRED[scope]["token"])
        if r.status_code != 200:
            continue
        for t in (r.json() or {}).get("tasks") or []:
            if t.get("id") in seen:
                continue
            seen.add(t.get("id"))
            out.append(t)
    return out
