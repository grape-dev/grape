"""dark_tokenize.py — 把 view 里的字面浅色换成主题 token（P3 半自动）

安全规则：只替换「属性: 字面色」整对，且 property 明确（color/background/border*）。
不碰 gradient / box-shadow / 内联 JS 里的 echarts 颜色。
把 .knob/.dot/.thumb/.switch/.check 等「必须固定白」的选择器单列出来人工复核。
"""
import re, sys, json

MAP = [
    # (属性前缀, 旧值, 新值, 说明)
    (r'color',        '#86868b', 'var(--t3)',      't3 文字'),
    (r'color',        '#8d97a8', 'var(--t3)',      't3 文字'),
    (r'color',        '#6e6e73', 'var(--t2)',      't2 文字'),
    (r'color',        '#5b667b', 'var(--t2)',      't2 文字'),
    (r'color',        '#424245', 'var(--t2)',      't2 文字'),
    (r'color',        '#1d1d1f', 'var(--t)',       '正文'),
    (r'color',        '#101724', 'var(--t)',       '正文'),
    (r'color',        '#fff',    'var(--on-accent)', '彩色底白字'),
    (r'color',        '#ffffff', 'var(--on-accent)', '彩色底白字'),
    (r'border(?:-top|-bottom|-left|-right|-color)?', '#e8e8ed', 'var(--hairline)', '描边'),
    (r'border(?:-top|-bottom|-left|-right|-color)?', '#e5e7eb', 'var(--hairline)', '描边'),
    (r'border(?:-top|-bottom|-left|-right|-color)?', '#e3e6ee', 'var(--hairline)', '描边'),
    (r'border(?:-top|-bottom|-left|-right|-color)?', '#d2d2d7', 'var(--hairline)', '描边'),
    (r'background',   '#f5f5f7', 'var(--bg)',       '页面/嵌套底'),
    (r'background',   '#f5f6f8', 'var(--bg)',       '页面/嵌套底'),
    (r'background',   '#fafafa', 'var(--hover-card)', 'hover 底'),
    (r'background',   '#f8f9fb', 'var(--hover-row)',  '行 hover'),
    (r'background',   '#fbfbfd', 'var(--hover-card)', 'hover 底'),
    (r'background',   '#e8e8ed', 'var(--hairline)', '分隔/轨道'),
    (r'background',   '#fff',    'var(--c)',        '面板底'),
    (r'background',   '#ffffff', 'var(--c)',        '面板底'),
]
FLAG = re.compile(r'knob|dot|thumb|switch|check|avatar|badge-?i|on\b|act\b', re.I)


def tokenize(path):
    src = open(path, encoding='utf-8').read()
    out = src
    counts = {}
    for prop, old, new, label in MAP:
        # 匹配 "prop...: #hex"（允许 !important / 分号前后空格差异）
        pat = re.compile(r'(?P<p>' + prop + r')\s*:\s*' + re.escape(old) + r'\b(?P<imp>\s*!important)?', re.I)
        def sub(m):
            key = f'{m.group("p")}:{old}→{new}'
            counts[key] = counts.get(key, 0) + 1
            return f'{m.group("p")}: {new}{m.group("imp") or ""}'
        out = pat.sub(sub, out)

    # 复核清单：规则块里同时出现「固定白」类语义 + 刚被换掉的元素
    style = re.search(r'<style[^>]*>(.*?)</style>', out, re.S)
    flags = []
    if style:
        for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', style.group(1)):
            sel = ' '.join(m.group(1).split())
            decl = m.group(2)
            if FLAG.search(sel) and re.search(r'var\(--c\)|var\(--bg\)', decl):
                flags.append(f'{sel} :: {" ".join(decl.split())[:110]}')
    return out, counts, flags


if __name__ == '__main__':
    for path in sys.argv[1:]:
        out, counts, flags = tokenize(path)
        if not counts:
            print(f'— {path}: 无命中')
            continue
        open(path, 'w', encoding='utf-8').write(out)
        print(f'— {path}')
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f'   {v:3d}  {k}')
        if flags:
            print('   ⚠ 需复核：')
            for f in flags:
                print('     ', f)