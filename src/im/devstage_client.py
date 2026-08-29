"""DevStage cloud dev environment API client (Huawei Cloud OpenAPI).
Auth: IAM5.0 AK/SK signing (SDK-HMAC-SHA256).
"""
import os, json, hashlib, hmac, datetime, urllib.request, urllib.error, urllib.parse
from src.config import log

_DEFAULT_BASE_URL = "https://devstation.myhuaweicloud.com"
SOURCE = "CODEARTS-AGENT"


def _resolve_base_url():
    env = os.environ.get("DEVOPS_DEVSTAGE_BASE_URL", "").strip()
    if env:
        return env
    try:
        from src.user.db import get_meta_value
        db_val = get_meta_value("devstage_base_url")
        if db_val:
            return db_val.strip()
    except Exception:
        pass
    return _DEFAULT_BASE_URL


class DevStageError(Exception):
    def __init__(self, code: str, msg: str):
        self.code = code
        super().__init__(f"[{code}] {msg}")


# ====== AK/SK signing (SDK-HMAC-SHA256) ======

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(sk: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("SDK" + sk).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "sdk_request")


def _build_auth_header(method, url, body, ak, sk):
    """Build Authorization + X-Sdk-Date headers for IAM5.0 AK/SK signing."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    path = parsed.path or "/"
    query = parsed.query

    # Canonical URI: SDK appends trailing /
    if not path.endswith("/"):
        path = path + "/"

    # Canonical query string (sorted)
    if query:
        params = sorted(urllib.parse.parse_qsl(query, keep_blank_values=True))
        canonical_query = "&".join("{}={}".format(k, urllib.parse.quote(v, safe="")) for k, v in params)
    else:
        canonical_query = ""

    # Body hash: empty body = sha256 of empty
    body_str = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body else ""
    if not body_str:
        body_hash = hashlib.sha256(b"").hexdigest()
    else:
        body_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()

    # Timestamp
    now = datetime.datetime.utcnow()
    x_sdk_date = now.strftime("%Y%m%dT%H%M%SZ")

    # Canonical headers (each header: key:value\n)
    canonical_headers = "content-type:application/json\nhost:{}\nx-sdk-date:{}\n".format(host, x_sdk_date)
    signed_headers = "content-type;host;x-sdk-date"

    # Canonical request
    canonical_request = "{}\n{}\n{}\n{}\n{}\n{}".format(
        method, path, canonical_query, canonical_headers, signed_headers, body_hash
    )

    # String to sign
    hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = "SDK-HMAC-SHA256\n{}\n{}".format(x_sdk_date, hashed_canonical)

    # Signature: HMAC-SHA256 with SK as key
    signature = hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    # Authorization header
    auth = "SDK-HMAC-SHA256 Access={}, SignedHeaders={}, Signature={}".format(ak, signed_headers, signature)
    return auth, x_sdk_date, body_str


def _request(method, path, ak, sk, body=None):
    """Send AK/SK signed request to DevStage API."""
    base = _resolve_base_url().rstrip("/")
    url = f"{base}{path}"
    auth, x_sdk_date, body_str = _build_auth_header(method, url, body, ak, sk)
    data = body_str.encode("utf-8") if body_str else None
    headers = {
        "Content-Type": "application/json",
        "X-Sdk-Date": x_sdk_date,
        "Authorization": auth,
    }
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            raw = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise DevStageError(str(e.code), "HTTP {}: {}".format(e.code, e.reason))
    except urllib.error.URLError as e:
        raise DevStageError("NETWORK", "Network error: {}".format(e.reason))
    except json.JSONDecodeError as e:
        raise DevStageError("PARSE", "JSON error: {}".format(e))
    err = raw.get("error_code", "0000")
    if err != "0000":
        raise DevStageError(err, raw.get("error_msg", "Unknown"))
    return raw.get("result") or {}


# ====== API functions ======

# v1 数字状态码 → cde.* 字符串映射
# v2 API 对已停止环境错误返回 devenv.0001，用 v1 数字状态做交叉校验
_V1_STATUS_MAP = {
    0: "cde.0000",  # 等待中
    1: "cde.0001",  # 创建中
    2: "cde.0002",  # 运行中
    3: "cde.0003",  # 开机中
    4: "cde.0004",  # 已就绪
    5: "cde.0005",  # 关机中
    6: "cde.0006",  # 删除中
    9: "cde.0009",  # 已停止/已关闭
    10: "cde.0010", # 异常
}

def _normalize_status(env):
    """归一化状态码：华为云不同 API 版本返回 devenv.* 或 cde.* 前缀，统一为 cde.*
    v2 API 对已停止环境错误返回 devenv.0001，用 _v1_status 做交叉校验修正。
    """
    st = env.get("status", "")
    if st.startswith("devenv."):
        st = "cde." + st[len("devenv."):]
        env["status"] = st
    # v1 数字状态交叉校验：v1 status=9 表示已停止，但 v2 返回 devenv.0001
    v1_st = env.get("_v1_status")
    if v1_st is not None and v1_st in _V1_STATUS_MAP:
        correct = _V1_STATUS_MAP[v1_st]
        if st != correct:
            env["status"] = correct
    return env

def list_environments(ak, sk):
    """GET /open-api-public/v2/devenvs，并用 v1 API 交叉校验状态"""
    r = _request("GET", "/open-api-public/v2/devenvs", ak, sk)
    if isinstance(r, list):
        envs = []
        for e in r:
            did = e.get("id", "")
            if did:
                try:
                    detail = _request("GET", f"/open-api-public/v1/devenvs/{did}", ak, sk)
                    e["_v1_status"] = detail.get("status")
                except Exception:
                    pass
            envs.append(_normalize_status(e))
        return envs
    return []


def create_environment(ak, sk, *, name, cores=4, arch="ARM", memory=8,
                       flavor_id="", template_id="",
                       env_vars=None, init_scripts=None):
    """POST /open-api-public/v2/devenvs, return devstage_id.
    自动检测模板是否要求 NPU（Ascend），如有则传入 npu 参数。
    """
    body = {
        "source": SOURCE, "type": "Container",
        "name": name, "cores": cores, "arch": arch, "memory": memory,
        "flavor_id": flavor_id, "template_id": template_id,
    }
    # 检测模板条件是否要求 NPU，如要求则注入到 conditions 中
    # 注意：NPU 参数必须放在 body["conditions"]["npu"] 里，
    # 放在 body 顶层会被 API 静默忽略，导致环境永远卡在 cde.0001（创建中）
    if template_id:
        try:
            tmpl = _request("GET", "/open-api-public/v1/templates", ak, sk)
            for t in (tmpl.get("content") or []):
                if t.get("uuid") == template_id:
                    conditions = (t.get("config") or {}).get("conditions") or {}
                    npu_cond = conditions.get("npu")
                    if npu_cond:
                        npu_type = (npu_cond.get("type") or [""])[0]
                        npu_arch = npu_cond.get("arch", "ASCEND")
                        npu_min = (npu_cond.get("size") or {}).get("min", 1)
                        body["conditions"] = {
                            "npu": {
                                "size": int(npu_min),
                                "arch": npu_arch,
                                "type": npu_type,
                            }
                        }
                        log.info(f"模板 {template_id} 要求 NPU({npu_arch} {npu_type} x{npu_min})，已注入 conditions.npu")
                    break
        except Exception as e:
            log.warning(f"查询模板 NPU 条件失败: {e}")
    if env_vars:
        body["env"] = env_vars
    if init_scripts:
        body["user_init_scripts"] = init_scripts
    r = _request("POST", "/open-api-public/v2/devenvs", ak, sk, body)
    did = (r.get("devstage_id") or r.get("dev_stage_instance_id") or "").strip()
    if not did:
        raise DevStageError("RESULT", "No devstage_id in response")
    return did


def start_environment(ak, sk, devstage_id):
    """POST /open-api-public/v1/devenvs/{id}/start"""
    return _request("POST", "/open-api-public/v1/devenvs/{}/start".format(devstage_id), ak, sk, {"source": SOURCE})


def stop_environment(ak, sk, devstage_id):
    """POST /open-api-public/v1/devenvs/{id}/close"""
    return _request("POST", "/open-api-public/v1/devenvs/{}/close".format(devstage_id), ak, sk, {"source": SOURCE})


def delete_environment(ak, sk, devstage_id):
    """DELETE /open-api-public/v1/devenvs/{id}"""
    return _request("DELETE", "/open-api-public/v1/devenvs/{}?source={}".format(devstage_id, SOURCE), ak, sk)


def list_specs(ak, sk, template_id=""):
    """GET /open-api-public/v1/specs"""
    path = "/open-api-public/v1/specs"
    if template_id:
        path += "?template_id=" + urllib.parse.quote(template_id)
    r = _request("GET", path, ak, sk)
    return r if isinstance(r, list) else []
