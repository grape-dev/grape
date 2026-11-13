#!/usr/bin/env python3
"""校验页面样式的 CSS 变量引用都有定义。

覆盖口径（N-4 校正）：本守卫只兜 `src/api/frontend/shell/admin.html` 的内联 <style>
与 tokens.css 的定义面。迁移后的主体样式是 `src/api/frontend/app/src/styles/global/app.css`
（构建期出 /static-vue/assets/css/app.css），其变量使用靠 tokens.css 兜底，暂不纳入本守卫
逐条比对——不要以为它覆盖了全站样式。

判定：引用未定义变量时，带 fallback（var(--x, #fff)）只 WARN（不会破图）；
**无 fallback 的未定义引用直接 FAIL**（浏览器会丢弃整条声明，属真实样式缺陷）。
"""
import re
import sys

TOKENS = 'src/api/frontend/app/src/styles/global/tokens.css'
ADMIN = 'src/api/frontend/shell/admin.html'


def extract_vars(filepath):
    with open(filepath, encoding='utf-8') as f:
        content = f.read()
    # 提取 --xxx 定义
    defined = set(re.findall(r'(--[\w-]+)\s*:', content))
    # 提取 var(--xxx) / var(--xxx, fallback) 引用
    referenced = set(re.findall(r'var\((--[\w-]+)', content))
    return defined, referenced


tok_defined, _ = extract_vars(TOKENS)

with open(ADMIN, encoding='utf-8') as f:
    html = f.read()
m = re.search(r'<style>(.*?)</style>', html, re.DOTALL)
admin_css = m.group(1) if m else ''
admin_defined = set(re.findall(r'(--[\w-]+)\s*:', admin_css))
admin_referenced = set(re.findall(r'var\((--[\w-]+)', admin_css))

# admin 可用的变量 = tokens.css 定义 + admin 自己定义
available = tok_defined | admin_defined
undefined = admin_referenced - available

# 区分「带 fallback」（浏览器仍能取值）与「裸引用」（整条声明失效）
bare = set()
for var in re.findall(r'var\((--[\w-]+)([^)]*)\)', admin_css):
    name, rest = var
    if name in undefined and 'fallback' not in rest and not rest.lstrip().startswith(','):
        bare.add(name)

if bare:
    print(f'FAIL: admin.html 引用未定义且无 fallback 的 CSS 变量: {sorted(bare)}')
    sys.exit(1)
if undefined:
    print(f'WARN: admin references undefined CSS vars (with fallback): {sorted(undefined)}')
    sys.exit(0)

print(f'OK: all {len(admin_referenced)} admin CSS var references defined in tokens.css or admin :root')
sys.exit(0)