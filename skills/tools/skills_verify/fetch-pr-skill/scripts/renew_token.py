#!/usr/bin/env python3
"""renew_token.py — 每日保活刷新 GITCODE token（ACCESS 永不过期）

从 /root/.gitcode_tokens 读入 token，调用 ensure_access_token() 刷新
（剩余 <1 天自动续），成功后写回磁盘。供 cron 每日执行，保证流水线
任何时候触发都有有效 ACCESS 可续/可用。

用法: python3 renew_token.py
日志: 追加到 /tmp/gitcode_token_renew.log
"""
import os
import sys
import json
import hmac
import hashlib
import datetime
import logging
from urllib.parse import urlparse, quote

sys.path.insert(0, os.getenv("FETCH_PR_SCRIPTS", "/root/.agents/skills/fetch-pr-skill/scripts"))

TOKENS_FILE = os.getenv("GITCODE_TOKENS_FILE", "/root/.gitcode_tokens")
ALERT_DAYS = float(os.getenv("GITCODE_TOKEN_ALERT_DAYS", "5"))

# SMN 预警（邮件 topic；topic URN 内含 project_id）
SMN_TOPIC_URN = os.getenv(
    "HUAWEI_CLOUD_SMN_TOPIC_URN",
    "urn:smn:cn-north-4:019dd22c23467301b803591e30176830:auto_cleanup_failed_alert",
)
SMN_REGION = os.getenv("HUAWEI_CLOUD_REGION", "cn-north-4")

logging.basicConfig(
    filename="/tmp/gitcode_token_renew.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("renew_token")


def _smn_request(method: str, url: str, body: str = "") -> tuple:
    """华为云 SMN 请求（SDK-HMAC-SHA256 签名，复用 OBS 凭证，其有 SMN 权限）。"""
    ak = os.getenv("HUAWEI_ACCESS_KEY") or os.getenv("HUAWEI_CLOUD_AK") or ""
    sk = os.getenv("HUAWEI_SECRET_KEY") or os.getenv("HUAWEI_CLOUD_SK") or ""
    if not ak or not sk:
        log.warning("无 AK/SK，跳过 SMN 预警")
        return "000", "no AK/SK"
    parsed = urlparse(url)
    path = parsed.path
    query = parsed.query
    encoded_path = "/".join(quote(seg, safe="") for seg in path.split("/"))
    if not encoded_path.endswith("/"):
        encoded_path += "/"
    xdate = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    headers = {"Host": parsed.hostname, "X-Sdk-Date": xdate, "Content-Type": "application/json"}
    if body:
        signed_headers = ["content-type", "host", "x-sdk-date"]
    else:
        signed_headers = ["host", "x-sdk-date"]
    lower = {k.lower(): v for k, v in headers.items()}
    canonical_headers = "\n".join(f"{h}:{lower[h]}" for h in signed_headers) + "\n"
    signed_header_keys = ";".join(signed_headers)
    payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest() if body else "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    canonical_request = "\n".join([method, encoded_path, query, canonical_headers, signed_header_keys, payload_hash])
    string_to_sign = "SDK-HMAC-SHA256\n" + xdate + "\n" + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    signature = hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = f"SDK-HMAC-SHA256 Access={ak}, SignedHeaders={signed_header_keys}, Signature={signature}"
    import urllib.request
    req = urllib.request.Request(url, method=method, headers=headers)
    if body:
        req.data = body.encode("utf-8")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return str(r.status), r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return "000", str(e)


def _send_expiry_alert(days_left: float) -> bool:
    """REFRESH 剩 ALERT_DAYS 天内 → 发 SMN 通知（邮件/短信按 topic 订阅）。"""
    if not SMN_TOPIC_URN:
        return False
    # topic URN: urn:smn:{region}:{project_id}:{name}
    parts = SMN_TOPIC_URN.split(":")
    if len(parts) < 5:
        return False
    project_id = parts[3]
    url = (
        f"https://smn.{SMN_REGION}.myhuaweicloud.com/v2/{project_id}"
        f"/notifications/topics/{SMN_TOPIC_URN}/publish"
    )
    payload = json.dumps({
        "subject": "[ALERT] GitCode REFRESH token 即将过期",
        "message": (
            f"GitCode GITCODE_REFRESH_TOKEN 剩余 {days_left:.1f} 天过期，"
            f"即将无法自动刷新 ACCESS，合并人/issue 负责人设置会失效。\n"
            f"请浏览器重新登录 GitCode 导出新 token 更新 /root/.gitcode_tokens。\n"
            f"时间: {datetime.datetime.now().isoformat()}"
        ),
        "time_to_live": "86400",
    })
    code, resp = _smn_request("POST", url, payload)
    ok = code in ("200", "201", "202")
    if ok:
        log.info(f"SMN 过期预警已发送 (剩余 {days_left:.1f} 天)")
    else:
        log.warning(f"SMN 预警发送失败: HTTP {code} {resp[:200]}")
    return ok


def load_tokens() -> dict:
    out = {}
    if not os.path.exists(TOKENS_FILE):
        return out
    for line in open(TOKENS_FILE, encoding="utf-8"):
        k, _, v = line.strip().partition("=")
        if v:
            out[k] = v
    return out


def main() -> int:
    tokens = load_tokens()
    if not tokens.get("GITCODE_REFRESH_TOKEN"):
        log.warning("无 REFRESH token，无法刷新")
        print("❌ /root/.gitcode_tokens 无 GITCODE_REFRESH_TOKEN")
        return 1
    os.environ["GITCODE_ACCESS_TOKEN"] = tokens.get("GITCODE_ACCESS_TOKEN", "")
    os.environ["GITCODE_REFRESH_TOKEN"] = tokens["GITCODE_REFRESH_TOKEN"]

    from eval.gitcode_issue import ensure_access_token, _decode_jwt_exp

    tok = ensure_access_token()
    if not tok:
        log.error("刷新失败")
        print("❌ 刷新失败，请人工检查")
        return 1
    exp = _decode_jwt_exp(tok)
    days = round((exp - datetime.datetime.now().timestamp()) / 86400, 1) if exp else "?"
    log.info(f"OK token exp剩余 {days} 天")
    print(f"✅ token 有效，剩余 {days} 天")

    # REFRESH 到期预警
    ref_exp = _decode_jwt_exp(tokens["GITCODE_REFRESH_TOKEN"])
    if ref_exp:
        ref_days = (ref_exp - datetime.datetime.now().timestamp()) / 86400
        print(f"   REFRESH 剩余 {ref_days:.1f} 天")
        if ref_days <= ALERT_DAYS:
            _send_expiry_alert(ref_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
