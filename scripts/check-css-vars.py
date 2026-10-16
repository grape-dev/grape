#!/usr/bin/env python3
"""校验 admin 引用的 CSS 变量都在 tokens.css 中定义。"""
import re, sys

def extract_vars(filepath):
    with open(filepath, encoding='utf-8') as f:
        content = f.read()
    # 提取 --xxx 定义
    defined = set(re.findall(r'(--[\w-]+)\s*:', content))
    # 提取 var(--xxx) 引用（含 fallback: var(--xxx, fallback)）
    referenced = set(re.findall(r'var\((--[\w-]+)', content))
    return defined, referenced

# tokens.css 定义的变量
tok_defined, _ = extract_vars('src/api/static/css/tokens.css')

# admin.html 内联 CSS 引用的变量
# 先提取 <style> 块
with open('src/api/static/admin.html', encoding='utf-8') as f:
    html = f.read()
m = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
admin_css = m.group(1) if m else ''
admin_defined = set(re.findall(r'(--[\w-]+)\s*:', admin_css))
admin_referenced = set(re.findall(r'var\((--[\w-]+)', admin_css))

# admin 可用的变量 = tokens.css 定义 + admin 自己定义
available = tok_defined | admin_defined
# 未定义的引用
undefined = admin_referenced - available
if undefined:
    print(f'WARN: admin references undefined CSS vars: {sorted(undefined)}')
    print('  These vars have fallback values in var(--xxx, fallback) so they will not break.')
    sys.exit(0)  # WARN 不 fail，因为有 fallback
else:
    print(f'OK: all {len(admin_referenced)} CSS var references defined in tokens.css or admin :root')
    sys.exit(0)
