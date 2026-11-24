#!/usr/bin/env python3
"""Issue #4239：校验前端 t('<ns>.<key>') 静态调用与语言包 key 是否对得上。

`check-i18n-keys.py` 只比对 zh/en 两份词条集合是否对称，无法发现「调用处有、
词条无」的缺口：这类 key 会在页面直接渲染出 key 原文（t() 返回 key 本身），
或回退到组件本地 DEFAULTS（英文环境显示中文）。

本脚本按文件解析 t()/td()/t2()/tf()/tc()/tOr()/tp() 的命名空间前缀映射，
展开模板/脚本里的静态 key，与合并后的语言包做差集，并单独列出仅靠 DEFAULTS
兜底（语言包缺词条）的项。

用法：
    python3 scripts/check-i18n-usage.py
    python3 scripts/check-i18n-usage.py --src-dir <dir> --locales-dir <dir>
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# 扫描范围 = 整个前端（app/src Vue 组件 + shell/js 壳页面脚本）。
# 只扫 app/src 会漏掉 shell/js 里直接调用 `I18N.t('<ns>.<key>')` 的词条缺口。
DEFAULT_SRC = REPO / 'src/api/frontend'
DEFAULT_LOCALES = REPO / 'src/api/frontend/shell/locales'

# 组件内 t() 包装函数的命名（按长度从长到短，避免 t 抢先匹配 t2/td/tf/tc/tp）。
FUNC_NAMES = ('tOr', 't2', 'td', 'tf', 'tc', 'tp', 't')
FUNC_RE = re.compile(r'function\s+(' + '|'.join(FUNC_NAMES) + r')\s*\(')
# 箭头函数包装：`const t = (key) => G.I18N.t('sandbox.llm.' + key)`、
# `const t = key => ...`。漏掉这类写法会静默跳过整个文件的 t() 前缀映射。
ARROW_RE = re.compile(
    r'(?:const|let|var)\s+(' + '|'.join(FUNC_NAMES) + r')\s*=\s*'
    r'(?:\(\s*[^)]*\)|[A-Za-z_$][\w$]*)\s*=>')
PREFIX_RE = re.compile(r"""['"]([A-Za-z_][\w.-]*)\.\s*['"]\s*\+\s*key""")
RAW_RE = re.compile(r"""I18N\s*\.\s*t\(\s*key\b""")
ALIAS_RE = re.compile(r'\b(' + '|'.join(FUNC_NAMES) + r')\s*\(')
CALL_RE = re.compile(
    r'(?<![\w.])(' + '|'.join(FUNC_NAMES) + r")\(\s*(['\"])([^'\"\n]+)\2")
DIRECT_RE = re.compile(r"""I18N\s*\.\s*t\(\s*(['\"])([^'\"\n]+)\1""")
DEFAULTS_DECL_RE = re.compile(
    r'(?:const|let|var)\s+[A-Za-z_$][\w$]*DEFAULTS[A-Za-z0-9_$]*\s*=\s*\{')


def flat_keys(obj, prefix=''):
    keys = set()
    for k, v in obj.items():
        full = f'{prefix}.{k}' if prefix else k
        if isinstance(v, dict):
            keys |= flat_keys(v, full)
        else:
            keys.add(full)
    return keys


def load_pack_keys(locales_dir):
    """返回 {lang: set(full_key)}，full_key 形如 pipeline.status_failed。

    模块清单从语言包目录派生（`locales/<lang>/*.json`），不写死列表：
    新增模块（如 beta_apply.json）自动纳入，避免"注释说目录派生、实为硬编码"。
    """
    packs = {}
    lang_dirs = [p for p in Path(locales_dir).iterdir() if p.is_dir()]
    for lang_dir in lang_dirs:
        merged = set()
        for path in sorted(lang_dir.glob('*.json')):
            with open(path, encoding='utf-8') as f:
                merged |= flat_keys(json.load(f), path.stem)
        packs[lang_dir.name] = merged
    return packs


def _read_js_string(txt, start):
    """txt[start] 为引号；返回 (字符串内容, 引号后位置)。"""
    quote = txt[start]
    i = start + 1
    out = []
    while i < len(txt):
        c = txt[i]
        if c == '\\':
            out.append(txt[i:i + 2])
            i += 2
            continue
        if c == quote:
            return ''.join(out), i + 1
        out.append(c)
        i += 1
    return ''.join(out), i


def _skip_ws_comments(txt, i):
    n = len(txt)
    while i < n:
        if txt[i].isspace():
            i += 1
        elif txt.startswith('//', i):
            j = txt.find('\n', i)
            i = n if j < 0 else j + 1
        elif txt.startswith('/*', i):
            j = txt.find('*/', i + 2)
            i = n if j < 0 else j + 2
        else:
            break
    return i


def extract_object_keys(txt, brace_start):
    """提取对象字面量第一层的 key（跳过嵌套对象/值）。"""
    keys = set()
    depth = 0
    i = brace_start
    n = len(txt)
    while i < n:
        c = txt[i]
        if c in '\'"`':
            _, i = _read_js_string(txt, i)
            continue
        if c == '{':
            depth += 1
            i += 1
            continue
        if c == '}':
            depth -= 1
            i += 1
            if depth == 0:
                break
            continue
        if depth == 1:
            i = _skip_ws_comments(txt, i)
            if i >= n:
                break
            if txt[i] == ',':
                i += 1
                continue
            if txt[i] in '\'"':
                key, i = _read_js_string(txt, i)
            else:
                m = re.match(r'[A-Za-z_$][\w$]*', txt[i:])
                if not m:
                    i += 1
                    continue
                key = m.group(0)
                i += len(key)
            i = _skip_ws_comments(txt, i)
            if i < n and txt[i] == ':':
                keys.add(key)
                i += 1
            continue
        i += 1
    return keys


def extract_defaults_keys(txt):
    keys = set()
    for m in DEFAULTS_DECL_RE.finditer(txt):
        brace = txt.find('{', m.end() - 1)
        if brace < 0:
            continue
        keys |= extract_object_keys(txt, brace)
    return keys


def _function_body(txt, start):
    brace = txt.find('{', start)
    if brace < 0:
        return ''
    depth = 0
    i = brace
    n = len(txt)
    while i < n:
        c = txt[i]
        if c in '\'"`':
            _, i = _read_js_string(txt, i)
            continue
        if txt.startswith('//', i):
            j = txt.find('\n', i)
            i = n if j < 0 else j + 1
            continue
        if txt.startswith('/*', i):
            j = txt.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return txt[brace:i + 1]
        i += 1
    return txt[brace:]


def _arrow_body(txt, start):
    """箭头函数体：`=> { ... }` 走 `_function_body`，`=> expr` 读到顶层 `;`/换行。"""
    i = _skip_ws_comments(txt, start)
    n = len(txt)
    if i < n and txt[i] == '{':
        return _function_body(txt, i)
    depth = 0
    out_start = i
    while i < n:
        c = txt[i]
        if c in '\'"`':
            _, i = _read_js_string(txt, i)
            continue
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                break
            depth -= 1
        elif c == ';' and depth == 0:
            break
        elif c == '\n' and depth == 0:
            break
        i += 1
    return txt[out_start:i]


def scan_file(path, merged_keys):
    """返回 [(kind, key)]；kind 为 leak（无 DEFAULTS 兜底）或 fallback（DEFAULTS 兜底）。"""
    txt = Path(path).read_text(encoding='utf-8')
    if 'I18N' not in txt:
        return []

    func_prefixes = {}
    func_raw = {}
    func_alias = {}
    for m in FUNC_RE.finditer(txt):
        name = m.group(1)
        body = _function_body(txt, m.end())
        if 'I18N' in body:
            func_prefixes[name] = set(PREFIX_RE.findall(body))
            func_raw[name] = bool(RAW_RE.search(body))
        else:
            alias = [a for a in FUNC_NAMES if re.search(r'\b' + a + r'\s*\(', body)]
            if alias:
                func_alias[name] = alias[0]
    for m in ARROW_RE.finditer(txt):
        name = m.group(1)
        body = _arrow_body(txt, m.end())
        if 'I18N' in body:
            func_prefixes[name] = set(PREFIX_RE.findall(body))
            func_raw[name] = bool(RAW_RE.search(body))
        else:
            alias = [a for a in FUNC_NAMES if re.search(r'\b' + a + r'\s*\(', body)]
            if alias:
                func_alias[name] = alias[0]

    def resolve(name, seen=None):
        seen = seen or set()
        if name in seen:
            return set(), False
        seen.add(name)
        if name in func_prefixes:
            return func_prefixes[name], func_raw.get(name, False)
        if name in func_alias:
            return resolve(func_alias[name], seen)
        return set(), False

    defaults = extract_defaults_keys(txt)
    issues = []
    seen = set()
    for m in CALL_RE.finditer(txt):
        name, _quote, key = m.groups()
        if name not in func_prefixes and name not in func_alias:
            continue
        if _is_dynamic(key):
            continue
        prefixes, raw = resolve(name)
        candidates = {f'{p}.{key}' for p in prefixes}
        if raw:
            candidates.add(key)
        if not candidates:
            continue
        if candidates & merged_keys:
            continue
        if key in seen:
            continue
        seen.add(key)
        has_fallback = name in ('tOr', 'tf', 'tc') or _is_fallback(key, defaults)
        issues.append(('fallback' if has_fallback else 'leak', key))

    for m in DIRECT_RE.finditer(txt):
        key = m.group(2)
        if _is_dynamic(key) or key in seen or key in merged_keys:
            continue
        seen.add(key)
        issues.append(('fallback' if _is_fallback(key, defaults) else 'leak', key))
    return issues


def _is_dynamic(key):
    return (not key or key.endswith('.') or key.endswith('_')
            or any(c in key for c in '+$`{}'))


def _is_fallback(key, defaults):
    if key in defaults:
        return True
    if key.startswith('detail.'):
        return key[len('detail.'):] in defaults
    return f'detail.{key}' in defaults


def scan_tree(src_dir, locales_dir):
    packs = load_pack_keys(locales_dir)
    merged = set()
    for keys in packs.values():
        merged |= keys
    src = Path(src_dir)
    findings = []
    for path in sorted(src.rglob('*')):
        if path.suffix not in ('.vue', '.js'):
            continue
        for kind, key in scan_file(path, merged):
            findings.append((str(path), kind, key))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--src-dir', default=str(DEFAULT_SRC))
    parser.add_argument('--locales-dir', default=str(DEFAULT_LOCALES))
    args = parser.parse_args(argv)

    findings = scan_tree(args.src_dir, args.locales_dir)
    leaks = [f for f in findings if f[1] == 'leak']
    fallbacks = [f for f in findings if f[1] == 'fallback']

    if leaks:
        print('FAIL: 调用处静态 key 在语言包缺失（t() 会返回 key 原文）:')
        for path, _kind, key in leaks:
            print(f'  - {path}: {key}')
    if fallbacks:
        print('FAIL: 调用处静态 key 仅靠组件 DEFAULTS 兜底（英文环境会显示中文）:')
        for path, _kind, key in fallbacks:
            print(f'  - {path}: {key}')
    if not findings:
        print(f'OK: 前端静态 i18n key 与语言包一致（扫描 {args.src_dir}）')
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
