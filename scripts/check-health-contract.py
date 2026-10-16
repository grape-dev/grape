#!/usr/bin/env python3
"""校验健康面板 API 返回结构符合 schema。可本地或 CI 运行。"""
import json, sys, os

def validate(response):
    """纯 Python 校验，不依赖 jsonschema 库。"""
    if not isinstance(response, dict):
        return ['response is not dict']
    errors = []
    # overall
    if response.get('overall') not in ('healthy', 'degraded', 'unhealthy'):
        errors.append(f"overall must be healthy/degraded/unhealthy, got: {response.get('overall')}")
    # platforms
    platforms = response.get('platforms', {})
    if not isinstance(platforms, dict):
        errors.append('platforms must be object')
    else:
        for prov, p in platforms.items():
            if prov not in ('gitcode', 'github'):
                errors.append(f'unknown platform key: {prov}')
                continue
            for field in ('healthy', 'degraded', 'unhealthy'):
                if not isinstance(p.get(field), bool):
                    errors.append(f'{prov}.{field} must be boolean')
            if not isinstance(p.get('consecutive_failures'), int):
                errors.append(f'{prov}.consecutive_failures must be integer')
            if p.get('error_type', '') not in ('', 'auth', 'network', 'config'):
                errors.append(f'{prov}.error_type invalid: {p.get("error_type")}')
    # checked_at
    if not isinstance(response.get('checked_at', ''), str):
        errors.append('checked_at must be string')
    return errors

# 可用 mock 数据校验，或请求真实 API
if __name__ == '__main__':
    # 用 mock 数据校验 schema 正确性
    mock = {
        "overall": "degraded",
        "platforms": {
            "gitcode": {"healthy": True, "degraded": False, "unhealthy": False,
                        "consecutive_failures": 0, "error_type": "", "error": ""},
            "github": {"healthy": False, "degraded": True, "unhealthy": False,
                       "consecutive_failures": 2, "error_type": "auth", "error": "401 Unauthorized"}
        },
        "checked_at": "2026-10-15 14:30:00"
    }
    errors = validate(mock)
    if errors:
        print('FAIL: health contract validation failed:')
        for e in errors: print(f'  - {e}')
        sys.exit(1)
    else:
        print('OK: health contract schema valid')
