#!/usr/bin/env python3
import os
import re
import json
import hashlib
import hmac
import datetime
import subprocess
from dataclasses import dataclass, field
from typing import List
from urllib.parse import urlparse, urlencode, quote

NOTIFICATION_EMAILS = [
    e.strip() for e in os.getenv("EVAL_NOTIFICATION_EMAILS", "").split(",") if e.strip()
] or ["wangjie594@h-partners.com", "hugai@huawei.com"]

HUAWEI_AK = os.getenv("HUAWEI_CLOUD_AK", "")
HUAWEI_SK = os.getenv("HUAWEI_CLOUD_SK", "")
HUAWEI_PROJECT_ID = os.getenv("HUAWEI_CLOUD_PROJECT_ID", "")
HUAWEI_REGION = os.getenv("HUAWEI_CLOUD_REGION", "cn-north-4")
SMN_TOPIC_URN = os.getenv("HUAWEI_CLOUD_SMN_TOPIC_URN", "")
_CACHED_PROJECT_ID = None


def _resolve_project_id():
    global _CACHED_PROJECT_ID
    if _CACHED_PROJECT_ID:
        return _CACHED_PROJECT_ID
    if HUAWEI_PROJECT_ID:
        _CACHED_PROJECT_ID = HUAWEI_PROJECT_ID
        return _CACHED_PROJECT_ID
    if not HUAWEI_AK or not HUAWEI_SK:
        return ""
    iam_url = f"https://iam.myhuaweicloud.com/v3/projects"
    http_code, resp = _huawei_request("GET", iam_url)
    if http_code == "200":
        try:
            projects = json.loads(resp).get("projects", [])
            for p in projects:
                if p.get("name") == HUAWEI_REGION:
                    _CACHED_PROJECT_ID = p["id"]
                    return _CACHED_PROJECT_ID
        except Exception:
            pass
    print(f"    Warning: Could not auto-resolve project_id for region {HUAWEI_REGION}")
    return ""

RESOURCE_PATTERNS = {
    "ecs": [
        re.compile(r"instance_id[:\s]+([i]-[\w-]+)", re.IGNORECASE),
        re.compile(r"server_id[:\s]+([\w-]+)", re.IGNORECASE),
        re.compile(r"server.*id[:\s]+([\w-]{8,})", re.IGNORECASE),
    ],
    "obs": [
        re.compile(r'"bucket"\s*:\s*"([\w][\w.-]*)"', re.IGNORECASE),
        re.compile(r'bucket_name\s*[:=]\s*"?([\w][\w.-]*)"?', re.IGNORECASE),
        re.compile(r'obs.*bucket.*[:=]\s*"?([\w][\w.-]{5,})"?', re.IGNORECASE),
    ],
    "vpc": [
        re.compile(r"vpc_id[:\s]+([\w-]+)", re.IGNORECASE),
    ],
    "subnet": [
        re.compile(r"subnet_id[:\s]+([\w-]+)", re.IGNORECASE),
    ],
    "eip": [
        re.compile(r"eip_id[:\s]+([\w-]+)", re.IGNORECASE),
        re.compile(r"public_ip.*id[:\s]+([\w-]+)", re.IGNORECASE),
    ],
    "swr_namespace": [
        re.compile(r'"namespace"\s*:\s*"([\w][\w.-]*)"', re.IGNORECASE),
    ],
    "swr_repository": [
        re.compile(r'"repository"\s*:\s*"([\w][\w.-]*)"', re.IGNORECASE),
    ],
    "swr_tag": [
        re.compile(r'"tag"\s*:\s*"([\w][\w.-]*)"', re.IGNORECASE),
    ],
}


@dataclass
class CloudResource:
    resource_type: str
    resource_id: str
    released: bool = False
    error: str = ""
    delete_cmd: str = ""


@dataclass
class CleanupResult:
    resources_found: List[CloudResource] = field(default_factory=list)
    resources_released: int = 0
    resources_failed: int = 0
    local_cleaned: bool = False
    skill_dir_cleaned: bool = False


_EMPTY_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _sign_huawei_request(method, url, headers, body=""):
    parsed = urlparse(url)
    path = parsed.path
    query = parsed.query

    # canonical uri: 对每段做 URL 编码，且确保末尾带 "/"（华为云签名规范）
    encoded_path = "/".join(quote(seg, safe="") for seg in path.split("/"))
    if not encoded_path.endswith("/"):
        encoded_path += "/"

    lower_headers = {k.lower(): v.strip() for k, v in headers.items()}
    # 有 body → 签 content-type；无 body → 只签 host + x-sdk-date
    if body:
        signed_headers = ["content-type", "host", "x-sdk-date"]
    else:
        signed_headers = ["host", "x-sdk-date"]
    canonical_headers = "\n".join(f"{h}:{lower_headers[h]}" for h in signed_headers) + "\n"
    signed_header_keys = ";".join(signed_headers)

    payload_hash = (
        hashlib.sha256(body.encode("utf-8")).hexdigest() if body else _EMPTY_HASH
    )
    canonical_request = "\n".join([
        method, encoded_path, query,
        canonical_headers, signed_header_keys, payload_hash,
    ])

    xdate = lower_headers["x-sdk-date"]
    string_to_sign = "SDK-HMAC-SHA256\n" + xdate + "\n" + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    signature = hmac.new(HUAWEI_SK.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = f"SDK-HMAC-SHA256 Access={HUAWEI_AK}, SignedHeaders={signed_header_keys}, Signature={signature}"
    return headers


def _huawei_request(method, url, body="", content_type="application/json"):
    import urllib.request
    import urllib.error

    now = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    headers = {
        "Host": urlparse(url).hostname,
        "X-Sdk-Date": now,
        "Content-Type": content_type,
    }
    headers = _sign_huawei_request(method, url, headers, body)

    req = urllib.request.Request(url, method=method, headers=headers)
    if body:
        req.data = body.encode("utf-8")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return str(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return str(e.code), e.read().decode("utf-8", errors="replace")[:500]
    except Exception as e:
        return "000", str(e)


def _parse_output_log(session_dir):
    path = os.path.join(session_dir, "output.log")
    if os.path.exists(path):
        with open(path, "r", errors="replace") as f:
            return f.read()
    return ""


def extract_cloud_resources(session_dir):
    log_text = _parse_output_log(session_dir)
    resources = []
    seen = set()
    for res_type, patterns in RESOURCE_PATTERNS.items():
        for pattern in patterns:
            for match in pattern.finditer(log_text):
                res_id = match.group(1).strip()
                key = (res_type, res_id)
                if key not in seen:
                    seen.add(key)
                    resources.append(CloudResource(resource_type=res_type, resource_id=res_id))

    clean_log = re.sub(r"\x1b\[[0-9;]*m", "", log_text)
    hcloud_resources = _extract_hcloud_creates(clean_log)
    for r in hcloud_resources:
        key = (r.resource_type, r.resource_id)
        if key not in seen:
            seen.add(key)
            resources.append(r)

    return resources


def _release_via_hcloud(resource, namespace=None):
    region = os.getenv("HUAWEI_CLOUD_REGION", "cn-north-4")
    rid = resource.resource_id
    try:
        if resource.delete_cmd:
            cmd = resource.delete_cmd
        elif resource.resource_type == "swr_namespace":
            cmd = f'hcloud SWR DeleteNamespaces --namespace={rid} --cli-region={region}'
        elif resource.resource_type == "swr_repository":
            if not namespace:
                resource.error = "SWR repository deletion requires namespace context"
                return False
            cmd = f'hcloud SWR DeleteRepo --namespace={namespace} --repository={rid} --cli-region={region}'
        elif resource.resource_type == "swr_tag":
            if not namespace:
                resource.error = "SWR tag deletion requires namespace context"
                return False
            cmd = f'hcloud SWR DeleteRepoTag --namespace={namespace} --tag={rid} --cli-region={region}'
        else:
            resource.error = f"No hcloud delete command for type: {resource.resource_type}"
            return False
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30,
            env={**os.environ, "HUA@WEI_CLOUD_REGION": region},
        )
        if result.returncode == 0:
            resource.released = True
            return True
        resource.error = f"hcloud exit {result.returncode}: {(result.stderr or result.stdout)[:200]}"
        return False
    except Exception as e:
        resource.error = str(e)
        return False


_HCLOUD_CREATE_TO_DELETE = {
    "CreateNamespace": ("SWR", "DeleteNamespaces", "--namespace={namespace}"),
    "CreateRepo": ("SWR", "DeleteRepo", "--namespace={namespace} --repository={repository}"),
    "CreateRepoTag": ("SWR", "DeleteRepoTag", "--namespace={namespace} --tag={destination_tag}"),
    "CreateServer": ("ECS", "DeleteServers", "--servers={id}"),
    "CreateBucket": ("OBS", "DeleteBucket", "--bucket={bucket}"),
    "CreateVpc": ("VPC", "DeleteVpc", "--vpc_id={vpc_id}"),
    "CreateSubnet": ("VPC", "DeleteSubnet", "--vpc_id={vpc_id} --subnet_id={subnet_id}"),
    "CreatePublicip": ("EIP", "DeletePublicip", "--publicip_id={id}"),
    "CreateInstance": ("RDS", "DeleteInstance", "--instance_id={instance_id}"),
    "CreateCluster": ("CCE", "DeleteCluster", "--cluster_id={cluster_id}"),
    "Create<Loadbalancer>": ("ELB", "DeleteLoadbalancer", "--loadbalancer_id={id}"),
    "CreateNatGateway": ("NAT", "DeleteNatGateway", "--nat_gateway_id={nat_gateway_id}"),
    "CreateZone": ("DNS", "DeleteZone", "--zone_id={zone_id}"),
    "CreateJob": ("ModelArts", "DeleteJob", "--job_id={job_id}"),
}


def _extract_hcloud_creates(log_text):
    clean = re.sub(r"\x1b\[[0-9;]*m", "", log_text)
    resources = []
    seen = set()
    region = os.getenv("HUAWEI_CLOUD_REGION", "cn-north-4")

    create_pattern = re.compile(
        r"hcloud\s+(\w+)\s+(Create\w+)\s+(.*?)(?:--cli-region|\n|$)",
        re.IGNORECASE,
    )
    for m in create_pattern.finditer(clean):
        service = m.group(1)
        operation = m.group(2)
        args_str = m.group(3)

        params = {}
        for pm in re.finditer(r"--(\w+)=(\S+)", args_str):
            params[pm.group(1)] = pm.group(2)

        delete_info = _HCLOUD_CREATE_TO_DELETE.get(operation)
        if not delete_info:
            res_type = f"{service}_{operation}"
            res_id = params.get("namespace") or params.get("repository") or params.get("name") or args_str[:40]
            key = (res_type, res_id)
            if key not in seen:
                seen.add(key)
                delete_op = operation.replace("Create", "Delete", 1)
                delete_args = args_str.replace("--cli-region", "").strip()
                delete_cmd = f"hcloud/ {service} {delete_op} {delete_args} --cli-region={region}"
                resources.append(CloudResource(
                    resource_type=res_type,
                    resource_id=res_id,
                    delete_cmd=delete_cmd,
                ))
            continue

        del_service, del_op, del_args_template = delete_info
        try:
            del_args = del_args_template.format(**params)
        except KeyError:
            del_args = del_args_template

        res_type = f"{service}_{operation}"
        res_id = params.get("namespace") or params.get("repository") or params.get("name") or "unknown"
        key = (res_type, res_id)
        if key not in seen:
            seen.add(key)
            delete_cmd = f"hcloud {del_service} {del_op} {del_args} --cli-region={region}"
            resources.append(CloudResource(
                resource_type=res_type,
                resource_id=res_id,
                delete_cmd=delete_cmd,
            ))

    return resources


def release_resource(resource, swr_namespace=None):
    if resource.delete_cmd:
        return _release_via_hcloud(resource, namespace=swr_namespace)

    if resource.resource_type.startswith("swr_"):
        return _release_via_hcloud(resource, namespace=swr_namespace)

    pid = _resolve_project_id()
    if not HUAWEI_AK or not HUAWEI_SK or not pid:
        resource.error = "Missing HUAWEI_AK/SK, or cannot resolve project_id"
        return False

    region = HUAWEI_REGION
    rid = resource.resource_id

    endpoints = {
        "ecs": f"https://ecs.{region}.myhuaweicloud.com/v1/{pid}/cloudservers/{rid}",
        "obs?obs": f"https://obs.{region}.myhuaweicloud.com/v1/{pid}/obs/buckets/{rid}",
        "vpc": f"https://vpc.{region}.myhuaweicloud.com/v1/{pid}/vpcs/{rid}",
        "subnet": f"https://vpc.{region}.myhuaweicloud.com/v1/{pid}/subnets/{rid}",
        "eip": f"https://eip.{region}.myhuaweicloud.com/v1/{pid}/publicips/{rid}",
    }

    url = endpoints.get(resource.resource_type)
    if not url:
        resource.error = f"Unknown resource type: {resource.resource_type}"
        return False

    if resource.resource_type == "ecs":
        url = f"https://ecs.{region}.myhuaweicloud.com/v1/{pid}/cloudservers"
        body = json.dumps({"servers": [{"id": rid}]})
        http_code, resp = _huawei_request("POST", url, body=body)
        if http_code in ("200", "202", "204"):
            resource.released = True
            return True
    else:
        http_code, resp = _huawei_request("DELETE", url)
        if http_code in ("200", "202", "204"):
            resource.released = True
            return True

    resource.error = f"HTTP {http_code}: {resp[:200]}"
    return False


def send_failure_notification(failed_resources, session_id, skill_name):
    if not HUAWEI_AK or not HUAWEI_SK:
        print(f"    Warning: HUAWEI_CLOUD_AK/SK not configured, skipping SMN notification")
        return

    body_lines = [
        f"Skill evaluation resource cleanup failed for session: {session_id}",
        f"Skill: {skill_name}",
        f"",
        f"Failed to release the following Huawei Cloud resources:",
        f"",
    ]
    for r in failed_resources:
        body_lines.append(f"  - Type: {r.resource_type}, ID: {r.resource_id}, Error: {r.error}")
    body_lines.append("")
    body_lines.append("Please manually release these resources to avoid unnecessary charges.")
    body_lines.append(f"\nTimestamp: {datetime.datetime.now().isoformat()}")

    subject = f"[ALERT] Skill Eval Resource Cleanup Failed - {skill_name} ({session_id})"

    if SMN_TOPIC_URN:
        pid = _resolve_project_id()
        smn_url = f"https://smn.{HUAWEI_REGION}.myhuaweicloud.com/v2/{pid}/notifications/topics/{SMN_TOPIC_URN}/publish"
        payload = json.dumps({
            "subject": subject,
            "message": "\n".join(body_lines),
            "time_to_live": "86400",
        })
        http_code, resp = _huawei_request("POST", smn_url, body=payload)
        if http_code in ("200", "201", "202"):
            print(f"    SMN notification sent (topic: {SMN_TOPIC_URN})")
        else:
            print(f"    Warning: SMN notification failed: HTTP {http_code} - {resp[:200]}")
    else:
        print(f"    Warning: HUAWEI_CLOUD_SMN_TOPIC_URN not set, skipping SMN notification")
        print(f"    Alert details: {subject}")


def clean_cloud_resources(session_dir):
    resources = extract_cloud_resources(session_dir)
    if not resources:
        print(f"    No cloud resources found in session logs")
        return CleanupResult(), []

    print(f"    Found {len(resources)} cloud resource(s) to release:")
    for r in resources:
        delete_info = f" → {r.delete_cmd}" if r.delete_cmd else ""
        print(f"      - {r.resource_type}: {r.resource_id}{delete_info}")

    swr_namespace = None
    for r in resources:
        if r.resource_type == "swr_namespace":
            swr_namespace = r.resource_id
            break

    failed = []
    for r in resources:
        success = release_resource(r, swr_namespace=swr_namespace)
        if success:
            print(f"      Released: {r.resource_type}/{r.resource_id}")
        else:
            print(f"      FAILED: {r.resource_type}/{r.resource_id} - {r.error}")
            failed.append(r)

    result = CleanupResult(
        resources_found=resources,
        resources_released=len(resources) - len(failed),
        resources_failed=len(failed),
    )
    return result, failed


def cleanup_session(session_dir, skill_output_dir=None, keep_session=True):
    import shutil
    result = CleanupResult()

    cloud_result, failed = clean_cloud_resources(session_dir)
    result.resources_found = cloud_result.resources_found
    result.resources_released = cloud_result.resources_released
    result.resources_failed = cloud_result.resources_failed

    if not keep_session and session_dir and os.path.isdir(session_dir):
        shutil.rmtree(session_dir)
        result.local_cleaned = True
        print(f"    Session directory removed: {session_dir}")
    elif session_dir and os.path.isdir(session_dir):
        print(f"    Session directory kept: {session_dir}")

    if skill_output_dir and os.path.isdir(skill_output_dir):
        shutil.rmtree(skill_output_dir)
        result.skill_dir_cleaned = True
        print(f"    Skill output directory removed: {skill_output_dir}")

    return result, failed
