#!/usr/bin/env python3
"""Manually trigger A2A for issue #59 to advance pipeline from vod -> issue."""
import requests, json

BOT_TOKEN = "m4kj4JfiLDKLxisQpbfjURwh"

resp = requests.post("https://example.com/api/a2a",
    json={
        "agent": "issue",
        "payload": {
            "repository": "songspace/shelldev",
            "issue_number": 59,
            "context": "VOD分析完成：开发丽江城市介绍web页面，复用现有城市页模板，部署到华为云ECS。可开发性ready，推荐场景cloud-native。",
            "recommended_scene": "cloud-native"
        }
    },
    headers={
        "Authorization": f"Bearer {BOT_TOKEN}",
        "Content-Type": "application/json"
    },
    timeout=30)
print(f"A2A response: {resp.status_code}")
print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
