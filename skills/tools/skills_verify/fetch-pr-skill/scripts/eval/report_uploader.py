#!/usr/bin/env python3
import os
import datetime
from typing import Optional

import pymysql

from .config import DB_CONFIG

try:
    import obs
except ImportError:
    obs = None

OBS_CRED_FILE = os.path.expanduser("~/.fetch-pr-skill/obs-cred.conf")


def _get_connection():
    return pymysql.connect(
        host=DB_CONFIG["host"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        charset=DB_CONFIG.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
    )


def load_obs_config() -> Optional[dict]:
    if not os.path.exists(OBS_CRED_FILE):
        print(f"    Warning: OBS config not found at {OBS_CRED_FILE}")
        return None
    config = {}
    try:
        with open(OBS_CRED_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    key = key.strip().upper()
                    value = value.strip()
                    if key == "OBS_AK":
                        config["ak"] = value
                    elif key == "OBS_SK":
                        config["sk"] = value
                    elif key == "OBS_BUCKET":
                        config["bucket"] = value
                    elif key == "OBS_CDN_DOMAIN":
                        config["cdn_domain"] = value
                    elif key == "OBS_REGION":
                        config["region"] = value
        if not all(k in config for k in ("ak", "sk", "bucket", "cdn_domain", "region")):
            print("    Warning: OBS config incomplete")
            return None
        return config
    except Exception as e:
        print(f"    Warning: OBS config load failed: {e}")
        return None


def build_report_content(skill_name, pr_url, eval_result, metrics, security_risk, report_id, session_id="") -> str:
    lines = []
    lines.append(f"# 评测报告: {skill_name}")
    lines.append("")
    lines.append(f"- **Skill**: `{skill_name}`")
    lines.append(f"- **PR**: {pr_url or 'N/A'}")
    lines.append(f"- **报告ID**: {report_id}")
    lines.append(f"- **评测日期**: {datetime.date.today().isoformat()}")
    lines.append("")
    lines.append(f"## 综合得分: {eval_result.total_score}/10")
    lines.append("")
    lines.append("## 维度得分")
    lines.append("")
    lines.append("| 维度 | 得分 | 满分 |")
    lines.append("|------|------|------|")
    lines.append(f"| 稳定性 | {eval_result.stability_score} | 4 |")
    lines.append(f"| 成本 | {eval_result.cost_score} | 1 |")
    lines.append(f"| 效率 | {eval_result.efficiency_score} | 2 |")
    lines.append(f"| 安全 | {eval_result.security_score} | 3 |")
    lines.append("")
    lines.append("## 12 项评分明细")
    lines.append("")
    lines.append("| 分类 | 项目 | 得分 | 满分 |")
    lines.append("|------|------|------|------|")
    for item in eval_result.item_scores:
        lines.append(f"| {item.category} | {item.item_name} | {item.actual_score} | {item.max_score} |")
    lines.append("")
    lines.append("## 原始指标")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|------|------|")
    lines.append(f"| 总消耗Token | {metrics.total_tokens} |")
    lines.append(f"| 完成耗时 | {metrics.duration_seconds}s |")
    lines.append(f"| 工具失败次数 | {metrics.tool_fail_count} |")
    lines.append(f"| 重试次数 | {metrics.retry_count} |")
    lines.append(f"| CLI调用次数 | {metrics.cli_call_count} |")
    lines.append(f"| SDK调用次数 | {metrics.sdk_call_count} |")
    lines.append(f"| API调用次数 | {metrics.api_call_count} |")
    lines.append(f"| LLM调用次数 | {metrics.llm_call_count} |")
    lines.append(f"| 上下文是否压缩 | {'yes' if metrics.context_compressed else 'no'} |")
    lines.append("")
    lines.append(f"## 安全审计等级: {security_risk}")
    lines.append("")
    lines.append(f"## 是否存在BUG: {'是' if metrics.has_bug else '否'}")
    lines.append("")
    lines.append(f"## Session 信息: {session_id}")
    lines.append("")
    return "\n".join(lines)


def upload_report(config, object_key, content) -> bool:
    if obs is None:
        print("    Warning: esdk-obs-python not available")
        return False
    try:
        endpoint = f"https://obs.{config['region']}.myhuaweicloud.com"
        client = obs.ObsClient(access_key_id=config["ak"], secret_access_key=config["sk"], server=endpoint)
        try:
            resp = client.putContent(config["bucket"], object_key, content)
            if getattr(resp, "status", None) == 200:
                return True
            print(f"    Warning: OBS upload returned status {getattr(resp, 'status', None)}")
            return False
        finally:
            client.close()
    except Exception as e:
        print(f"    Warning: OBS upload failed: {e}")
        return False


def build_download_url(config, object_key) -> str:
    return f"http://{config['cdn_domain']}/{object_key}"


def update_report_url(report_id, url) -> bool:
    try:
        conn = _get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE hd_eval_report SET report_url = %s WHERE id = %s",
                    (url, report_id),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        print(f"    Warning: update_report_url failed: {e}")
        return False
