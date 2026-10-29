#!/usr/bin/env python3
"""Frontend color audit: extract every color from shell CSS + all Vue views,
cluster by role (surface/text/border/accent/shadow), report per-file usage
and palette consistency. Read-only analysis, no code changes."""
import re, os, glob, collections

ROOT = '/home/mys/grape/src/api/frontend'
FILES = {
    'shell:app.css': f'{ROOT}/legacy/css/app.css',
    'shell:apple-theme.css': f'{ROOT}/legacy/css/apple-theme.css',
    'shell:auth.css': f'{ROOT}/legacy/css/auth.css',
    'shell:tokens.css': f'{ROOT}/legacy/css/tokens.css',
    'shell:app.js(inline)': f'{ROOT}/legacy/js/app.js',
}
for v in sorted(glob.glob(f'{ROOT}/app/src/views/*.vue')):
    FILES['view:' + os.path.basename(v)] = v

HEX = re.compile(r'#[0-9a-fA-F]{3,8}\b')
RGBA = re.compile(r'rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+(?:\s*,\s*[\d.]+)?\s*\)')

def luminance(r, g, b):
    return 0.2126*r + 0.7152*g + 0.0722*b

def classify(color):
    m = re.match(r'rgba?\(([^)]+)\)', color)
    if m:
        parts = [float(x) for x in m.group(1).split(',')]
        r, g, b = parts[:3]; a = parts[3] if len(parts) > 3 else 1.0
    else:
        h = color.lstrip('#')
        if len(h) == 3: h = ''.join(c*2 for c in h)
        r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16); a = 1.0
    L = luminance(r,g,b)
    sat = (max(r,g,b) - min(r,g,b)) / 255 if max(r,g,b) else 0
    if a < 0.35: return 'overlay/mask (alpha)'
    if sat < 0.12:
        if a < 1: return f'gray-surface-tint (a={a})'
        if L > 235: return 'near-white surface'
        if L > 180: return 'light gray'
        if L > 100: return 'mid gray'
        if L > 40: return 'dark gray'
        return 'near-black'
    # hue
    import colorsys
    hdeg, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
    hdeg *= 360
    hue = ('red' if hdeg < 15 or hdeg >= 345 else 'orange' if hdeg < 45 else 'yellow' if hdeg < 70 else
           'green' if hdeg < 165 else 'cyan' if hdeg < 200 else 'blue' if hdeg < 260 else 'purple')
    if sat > 0.75: return f'SATURATED {hue}'
    return f'accent {hue}'

report = {}
for name, path in FILES.items():
    if not os.path.exists(path): continue
    src = open(path, encoding='utf-8', errors='ignore').read()
    colors = HEX.findall(src) + RGBA.findall(src)
    counter = collections.Counter(colors)
    report[name] = counter

# 1. global overview: top colors by usage
total = collections.Counter()
for c in report.values(): total.update(c)
print('════ 全局 TOP 30 颜色（按出现次数） ════')
for color, n in total.most_common(30):
    print(f'  {n:4d}x {color:28s} {classify(color)}')

# 2. saturated "neon" colors per file
print('\n════ 各文件高饱和色(SATURATED) ════')
for name, counter in report.items():
    sat = {c: n for c, n in counter.items() if 'SATURATED' in classify(c)}
    if sat:
        top = sorted(sat.items(), key=lambda x: -x[1])[:6]
        print(f'  {name}: ' + ', '.join(f'{c}({n})' for c, n in top))

# 3. dark-unfriendly: hardcoded near-white surfaces & near-black text in views
print('\n════ 视图内硬编码近白表面 / 近黑文字（暗色不跟随的重灾区） ════')
for name, counter in report.items():
    if not name.startswith('view:'): continue
    whites = {c: n for c, n in counter.items() if classify(c) == 'near-white surface'}
    blacks = {c: n for c, n in counter.items() if classify(c) == 'near-black'}
    if whites or blacks:
        print(f'  {name}:')
        if whites: print(f'    白表面: ' + ', '.join(f'{c}({n})' for c, n in sorted(whites.items(), key=lambda x:-x[1])[:5]))
        if blacks: print(f'    黑文字: ' + ', '.join(f'{c}({n})' for c, n in sorted(blacks.items(), key=lambda x:-x[1])[:5]))

# 4. accent palette spread: which "blue"s exist
print('\n════ 蓝色系色值分布（应统一到 #0A84FF/#409CFF/#0060DF） ════')
blues = collections.Counter()
for name, counter in report.items():
    for c, n in counter.items():
        cl = classify(c)
        if 'blue' in cl or 'cyan' in cl:
            blues[c] += n
for c, n in blues.most_common(20):
    print(f'  {n:4d}x {c}')
