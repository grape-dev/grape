#!/usr/bin/env python3
"""校验 zh/en 语言包 key 集合一致。"""
import json, sys, os

LOCALES_DIR = 'src/api/static/locales'
MODULES = ['common', 'admin']  # admin 涉及的模块

def flat_keys(obj, prefix=''):
    keys = set()
    for k, v in obj.items():
        full = f'{prefix}.{k}' if prefix else k
        if isinstance(v, dict):
            keys |= flat_keys(v, full)
        else:
            keys.add(full)
    return keys

fail = 0
for mod in MODULES:
    zh_path = f'{LOCALES_DIR}/zh/{mod}.json'
    en_path = f'{LOCALES_DIR}/en/{mod}.json'
    if not os.path.exists(en_path):
        continue
    with open(zh_path, encoding='utf-8') as f: zh = json.load(f)
    with open(en_path, encoding='utf-8') as f: en = json.load(f)
    zh_keys = flat_keys(zh, mod)
    en_keys = flat_keys(en, mod)
    missing_in_en = zh_keys - en_keys
    missing_in_zh = en_keys - zh_keys
    if missing_in_en:
        print(f'FAIL: {mod} keys missing in en: {sorted(missing_in_en)}')
        fail = 1
    if missing_in_zh:
        print(f'FAIL: {mod} keys missing in zh: {sorted(missing_in_zh)}')
        fail = 1
    if not missing_in_en and not missing_in_zh:
        print(f'OK: {mod} zh/en key sets match ({len(zh_keys)} keys)')

sys.exit(fail)
