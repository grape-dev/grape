---
name: ai-blog-generation
version: 3.0.0
description: |
  AI 博客生成技能。学习参考文章排版风格 → 撰写同风格主题文章 →
  AI 配图 + AI 视频 → 提交博客内容到代码仓 blog/ 目录并创建 PR →
  交办 SRE Agent 发布。整合了 coding 技能的工作流（git 操作 + PR 创建）。
tools:
  - curl
  - ffmpeg
  - git
---

## 安全硬约束（必须遵守）

1. **所有 API 信息（Key、URL、Model）从项目知识（Project Knowledge）中获取**，来自 `project-info/member/ai-models.yaml`。包含：
   - 图片生成：`ai_models.image`（url、key、model）
   - 视频生成：`ai_models.video`（url、key、model）
2. 调用 ModelArts API 只用此 Key，严禁从数据库 / 配置文件 / 日志 / 历史中寻找其他 token。
3. token 不得写入日志或回复。
4. API 返回 403 / 429 时：停止重试并报告用户。速率限制 2 req/s。
5. **长命令执行会静默**：视频生成约 30s，图片生成约 7s/张，提前告知用户等待。
6. 调用 GitCode API 一律以 `$SKILL_DIR/references/gitcode-api-reference.md` 为准，严禁根据经验/记忆/猜测自行拼写 API 路径。
7. API 返回 403/无权限时：立即停止该操作并回复用户「该操作需要管理员/维护者权限」，严禁尝试其他 token 或绕行手段。

# AI 博客生成 + PR 提交流程

## 核心原则

### 专注博客内容生成与 PR 提交
负责内容生成到 PR 创建：学习参考文章 → 规划主题 → 撰写文章 → 生成配图 → 生成视频 → 提交到仓库 blog/ 目录 → 创建 PR → **交办 SRE 发布**。

**SRE 职责（本技能绝对禁止执行以下操作）：**
- ❌ **禁止** 直接调用博客发布 API（curl/requests 调任何 blog API 都不行）
- ❌ **禁止** 直接上传文件到博客服务器
- ❌ **禁止** 直接创建/更新/删除博客文章
- ✅ **必须** 创建 PR 并通过 A2A 交办 SRE Agent

本技能的唯一发布出口是：**git commit + git push → GitCode PR → A2A 通知 SRE**。任何绕过此流程直接发布的行为都违反硬规则。

### 博客目录结构约定
博客产物统一提交到代码仓库的 `blog/` 目录（与 `src/` 同级）。结构如下：

```
blog/
├── _tools/                       # 不同博客之间通用的工具/模板文件
│   ├── blog-styles.css           # 通用样式
│   ├── article-template.html     # 文章模板
│   └── README.md                 # 博客目录说明
├── {topic-slug}/                 # 每篇博客一个独立子文件夹（用英文短横线命名）
│   ├── index.html                # 博客文章（HTML）
│   ├── intro.mp4                 # 5秒介绍视频
│   ├── cover.png                 # 封面图
│   ├── images/                   # 配图
│   │   ├── theme_1.png
│   │   └── ...
│   └── meta.json                 # 博客元数据（标题、摘要、标签、作者、日期）
├── {another-topic}/
│   └── ...
└── _tools/README.md
```

- `blog/_tools/`：存放所有博客共用的工具/模板/样式文件（如果已存在则复用，不覆盖）
- `blog/{topic-slug}/`：每篇博客一个独立子目录，保持内容隔离
- `meta.json` 格式：
  ```json
  {
    "title": "博客标题",
    "description": "一句话摘要",
    "tags": ["tag1", "tag2"],
    "author": "Grape AI",
    "date": "2026-08-24",
    "featured_image": "cover.png"
  }
  ```

### 三步模仿法
1. **学结构**：分析参考文章的标题层级、段落长度、配图密度、引用/高亮/表格等特殊元素的用法
2. **学风格**：分析语气（技术深度/轻松活泼/营销推广）、句式特点
3. **学排版**：分析配图位置、标签样式、元数据展示方式

### 依赖前序分析
通过 A2A 被调用时，prompt 顶部包含「前序 Agent 处理摘要」。
必须与用户确认参考文章和主题后再开始生成。

### 超出能力范围时主动请求支援
如需要发布到小红书/公众号等平台，通过 A2A 交办给发布技能（不经过本流程）。

## 场景识别

收到事件或 A2A 调用后，先判断触发场景：

| 特征关键词 | 场景 | 执行流程 |
|-----------|------|---------|
| 含「博客」「文章」「专栏」「生成文章」「写文章」「图文并茂」等 | **博客生成** | 按本文档流程执行 |
| 含「学习这个」「仿照这个」「参考这个」等 + URL | **参考文章学习** | 先抓取分析，再按博客生成流程 |
| 其他 | 不做处理，结束 | — |

## 前置依赖

```bash
apt-get install -y ffmpeg git
pip3 install --break-system-packages --index-url https://pypi.tuna.tsinghua.edu.cn/simple edge-tts Pillow requests
```

## 环境变量（系统注入）

| 变量 | 来源 | 说明 |
|------|------|------|
| `$BOT_GITCODE_TOKEN` | 系统 prompt 注入 | GitCode API token（PRIVATE-TOKEN） |
| `$REPO_FULL_NAME` | 系统 prompt 注入 | 当前仓库全名（owner/repo） |
| `$ISSUE_NUMBER` | 系统 prompt 注入 | Issue 编号 |
| `$BOT_USERNAME` | 系统 prompt 注入 | Bot 用户名 |

## 工作流

### 1. 学习参考文章（如有）

用浏览器打开目标 URL，提取以下信息：

```python
# 使用 browser_navigate + browser_console 提取文章正文
# 用 document.body.innerText 获取全部文本
# 分析：标题层级、段落长度、配图数量、特殊元素（表格/引用/高亮块/标签）
```

输出分析结论：

| 分析维度 | 记录内容 |
|---------|---------|
| 标题风格 | 吸引型/技术型/营销型 |
| 段落结构 | 每段字数范围、配图间隔 |
| 特殊元素 | 是否有表格/引用块/高亮块/对比表格 |
| 语言风格 | 技术深度、是否使用第一人称 |
| 配图数量 | 每篇文章约几张 |

### 2. 撰写文章（HTML）

根据参考文章的风格，撰写同风格 HTML 文章。推荐的内联样式结构：

```html
<!DOCTYPE html>
<html>
<head>
<style>
body { font-family: -apple-system, 'PingFang SC', sans-serif; background: #f5f7fa; }
.article { max-width: 860px; margin: 0 auto; background: #fff; padding: 40px 50px; }
h1 { font-size: 28px; font-weight: 700; color: #1a1a2e; }
h2 { font-size: 22px; border-left: 4px solid #0d6efd; padding-left: 12px; }
.figure img { max-width: 100%; border-radius: 8px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }
blockquote { background: #f0f4ff; border-left: 4px solid #0d6efd; padding: 16px 20px; border-radius: 0 8px 8px 0; }
.highlight { background: linear-gradient(135deg, #0d6efd, #6610f2); color: #fff; padding: 24px; border-radius: 12px; }
.tag { display: inline-block; padding: 3px 12px; background: #e8f0fe; color: #0d6efd; border-radius: 12px; font-size: 12px; }
table { width: 100%; border-collapse: collapse; }
th { background: #f0f4ff; padding: 12px 16px; border: 1px solid #e0e6ed; }
td { padding: 12px 16px; border: 1px solid #e0e6ed; }
</style>
</head>
<body>
<div class="article">
  <!-- 文章内容 -->
</div>
</body>
</html>
```

文章须包含：
- 标题 + 元数据（时间、标签、阅读量）
- 顶部嵌入视频（5s 介绍视频）
- 配图间隔分布（每 1-2 段插一张）
- 至少一个特殊元素（表格 / 引用块 / 高亮块）
- 结尾署名

### 3. 产品实物调研与视觉特征提取

**仅当博客主题涉及具体产品或品牌时执行此步骤。** 如主题为抽象概念、通用知识、纯教程，则跳过此步。

#### 3a. 定位产品官网

从 Issue 需求中提取产品/品牌名称（如「华为问界M9」「iPhone 16」「理想L9」），通过搜索引擎定位其**中国大陆官方网站**：

```bash
# 示例：搜索品牌官网
curl -sL "https://www.baidu.com/s?wd=<产品名>+官方网站" \
  -H "User-Agent: Mozilla/5.0" | python3 -c "
import sys, re
html = sys.stdin.read()
# 提取搜索结果中的 URL
urls = re.findall(r'https?://[^\s\"<>]+', html)
# 过滤并输出最可能的官网
for u in urls:
    if any(dom in u for dom in ['huawei', 'apple', 'xiaomi', 'oppo', 'vivo',
           'honor', 'lenovo', 'dji', 'ideal', 'xpeng', 'nio', 'byd', 'mitsubishi',
           'toyota', 'honda', 'audi', 'bmw', 'mercedes', 'ford']):
        print(u)
        break
"
```

如果自动化提取失败，允许手动打开浏览器访问：

```python
# 使用 browser_navigate + browser_console 访问官网取图
browser_navigate(f"https://www.{product_brand}.com/{product_model}")
# 提取产品图片 URL
browser_console(expression="[...document.querySelectorAll('img[src*=\"product\"]')].map(i=>i.src)")
```

#### 3b. 提取视觉特征

收集到真实产品图片后，从以下维度分析视觉特征并记录到临时文件供后续使用：

```python
# 保存到 /tmp/blog-gen-{pid}/product_visual_info.json
visual_info = {
    "brand": "华为",
    "product": "问界M9",
    "colors": ["星河蓝", "陶瓷白", "深空灰"],          # 官网提供的车身颜色
    "design": {
        "front": "封闭式前脸+贯穿式日行灯+HUAWEI标识",
        "side": "流线型车身+隐藏式门把手+双色轮毂",
        "rear": "贯穿式尾灯+AITO标识"
    },
    "interior": "三屏中控+平底方向盘+Nappa真皮座椅",
    "logo": "AITO logo 在车头/方向盘/轮毂",
    "scene": "城市道路/高速/充电站/家庭出行",
    "reference_urls": ["https://aito.auto/m9/gallery"]  # 图片来源
}
```

#### 3c. 将视觉特征注入生成提示词

在后续图片生成和视频生成中，**必须**将 `visual_info` 中的实际产品特征嵌入到 prompt 中，确保生成结果与实物一致：

```
# 图片生成 prompt 中加入品牌/颜色/设计细节
prompt = f"""
{theme_prompt}。画面中的车辆必须是{visual_info['brand']}{visual_info['product']}，
采用{visual_info['colors'][0]}车身配色，{visual_info['design']['front']}的经典前脸设计，
{visual_info['design']['side']}侧面轮廓，{visual_info['logo']}品牌标识清晰可见。
风格：写实产品渲染，高质感光影，8K细节。
"""
```

**严禁**仅凭模型预训练记忆生成产品外观——实际产品的颜色、logo位置、前脸格栅造型、轮毂样式等细节必须来自官网提取的真实信息。

#### 3d. 提取真实产品 Logo 并保存

下载一张官网的正面/前侧方产品图，用 Pillow 裁剪出 logo 区域并保存为透明 PNG。**后续所有图片生成后，必须将真实 logo 叠加到图上**，因为 AI 文生图模型无法准确还原品牌 logo。

```python
from PIL import Image
import os, json

# 读取已保存的 visual_info
with open(f"/tmp/blog-gen-{os.getpid()}/product_visual_info.json") as f:
    vi = json.load(f)

# 从 reference_urls 中选一个正面清晰的产品图下载
import urllib.request
real_img_url = vi.get("reference_urls", [])[0]  # 第一张官方图
urllib.request.urlretrieve(real_img_url, f"/tmp/blog-gen-{os.getpid()}/real_product.jpg")

# 打开并裁剪 logo 区域（位置需要根据图片类型估计）
# 全尺寸SUV前脸：logo 约在宽度 40-58%、高度 20-38% 区域
img = Image.open(f"/tmp/blog-gen-{os.getpid()}/real_product.jpg")
w, h = img.size
logo_box = (int(w*0.42), int(h*0.22), int(w*0.58), int(h*0.38))
logo = img.crop(logo_box)

# 缩放至合适大小并去底（白色背景→透明）
logo_small = logo.resize((80, 80), Image.LANCZOS)
logo_rgba = logo_small.convert("RGBA")
pixels = logo_rgba.load()
for y in range(logo_rgba.height):
    for x in range(logo_rgba.width):
        r, g, b, a = logo_rgba.getpixel((x, y))
        if r > 220 and g > 220 and b > 220:
            logo_rgba.putpixel((x, y), (r, g, b, 0))

logo_rgba.save(f"/tmp/blog-gen-{os.getpid()}/logo_overlay.png")
print(f"✅ Logo extracted: {logo_rgba.size} with transparency")

# 记录不同图型的 logo 叠加位置
logo_positions = {
    "cover":       (int(w*0.47), int(h*0.28)),  # 封面/前脸车标
    "theme_1":     (int(w*0.47), int(h*0.28)),  # 前脸主题
    "theme_2":     (int(w*0.30), int(h*0.50)),  # 内饰/方向盘
    "theme_3":     (int(w*0.47), int(h*0.30)),  # 座舱主题
    "theme_4":     (int(w*0.47), int(h*0.28)),  # 驾驶场景
    "theme_5":     (int(w*0.48), int(h*0.55)),  # 尾部场景
}
with open(f"/tmp/blog-gen-{os.getpid()}/logo_positions.json", "w") as f:
    json.dump(logo_positions, f)
```

> ⚠️ **字体版权警告**：从官网图片裁剪的 logo 仅供 AI 增强配图使用，不得作为独立素材分发或商用。

### 4. 生成配图

从项目知识中获取 qwen-image 的 API Key 和 URL，调用 ModelArts 图片生成 API：

```python
import requests, json, base64, os, time

# API 信息从项目知识读取（ai_models.image.url / .key / .model）
API_IMAGE = image_url
KEY_IMAGE = image_key
MODEL_IMAGE = image_model
headers = {"Authorization": f"Bearer {KEY_IMAGE}", "Content-Type": "application/json"}

# 先以本地临时目录生成，后续步骤统一迁入 blog/
IMG_DIR = f"/tmp/blog-gen-{os.getpid()}/images"
os.makedirs(IMG_DIR, exist_ok=True)

# 各配图 prompt 构造，优先使用步骤 3 提取的 visual_info
themes = [
    # 如果 product_visual_info.json 存在，每个 prompt 自动嵌入实际产品特征
    {"prompt": "主题1的详细画面描述，须包含product品牌+型号+颜色等真实视觉特征", "file": "theme_1"},
]

for i, t in enumerate(themes):
    resp = requests.post(API_IMAGE, headers=headers, json={"model": MODEL_IMAGE, "prompt": t["prompt"], "n": 1, "size": "1024x1024"}, timeout=60)
    b64 = resp.json()["data"][0]["b64_json"]
    if "," in b64: b64 = b64.split(",")[1]
    with open(f"{IMG_DIR}/{t['file']}.png", "wb") as f: f.write(base64.b64decode(b64))

    # ✅ 图片生成后立即叠加真实 logo（如存在）
    logo_overlay = f"/tmp/blog-gen-{os.getpid()}/logo_overlay.png"
    logo_pos = f"/tmp/blog-gen-{os.getpid()}/logo_positions.json"
    if os.path.exists(logo_overlay) and os.path.exists(logo_pos):
        try:
            from PIL import Image
            with open(logo_pos) as f:
                positions = json.load(f)
            pos = positions.get(t['file'], positions.get("cover", (480, 280)))
            ai_img = Image.open(f"{IMG_DIR}/{t['file']}.png").convert("RGBA")
            logo = Image.open(logo_overlay).convert("RGBA")
            ai_img.paste(logo, pos, logo)
            ai_img.convert("RGB").save(f"{IMG_DIR}/{t['file']}.png")
            size_kb = os.path.getsize(f"{IMG_DIR}/{t['file']}.png") / 1024
            print(f"  [logo] 叠加到 {t['file']}.png ({size_kb:.0f}KB)")
        except Exception as e:
            print(f"  [logo] 叠加失败: {e}")
    # ⚡ 限流间隔
    if i < len(themes) - 1: time.sleep(1)
```

### 5. 生成视频

从项目知识中获取视频生成模型的 API Key、URL 和 Model（`ai_models.video`）。
⚠️ **禁止使用 I2V（图生视频）模型**——只能使用 T2V（文生视频）模型。模型名中含 `I2V`（如 `Wan2.2-I2V-A14B`）的一律不得调用，需在项目知识中配置 T2V 模型（如 `Wan2.2-T2V-A14B`）。

视频 prompt 必须基于步骤 3 提取的 `visual_info` 来构造准确的产品外观描述：

```python
import requests, json, time
import os, glob

# ⚠️ 模型校验：禁止 I2V 模型
MODEL_VIDEO = video_model
assert "I2V" not in MODEL_VIDEO.upper(), f"禁止使用 I2V 模型({MODEL_VIDEO})，请配置 T2V 模型"

# 尝试加载产品视觉特征（如存在）
VIDEO_PROMPT = "英文画面描述"  # 默认fallback
product_info_path = f"/tmp/blog-gen-{os.getpid()}/product_visual_info.json"
if os.path.exists(product_info_path):
    with open(product_info_path) as f:
        vi = json.load(f)
    VIDEO_PROMPT = (
        f"Realistic 5-second cinematic shot of a {vi['brand']} {vi['product']} "
        f"in {vi['colors'][0]} color, driving on a modern city road at sunset. "
        f"The vehicle features {vi['design']['front']}, {vi['design']['side']}. "
        f"Camera slowly pans from front 3/4 view to side profile. "
        f"Clean background, professional automotive cinematography, 4K, photorealistic."
    )

API_VIDEO = video_url
KEY_VIDEO = video_key
H = {"Authorization": f"Bearer {KEY_VIDEO}", "Content-Type": "application/json"}

resp = requests.post(API_VIDEO, headers=H, json={
    "model": MODEL_VIDEO,       # 必须是 T2V 模型（如 Wan2.2-T2V-A14B）
    "input": {"prompt": VIDEO_PROMPT}
}, timeout=30)
task_id = resp.json()["task_id"]
time.sleep(30)
result = requests.get(f"{API_VIDEO}/{task_id}", headers=H).json()
if result['status'] == 'succeeded':
    video_url = result['content']['result_url']
```

下载并合成音视频：

```bash
TMP_DIR="/tmp/blog-gen-$$"
mkdir -p "$TMP_DIR"
curl -sLo "$TMP_DIR/generated.mp4" "$video_url"
edge-tts --voice zh-CN-XiaoxiaoNeural --text "旁白文案，18-22字" --write-media "$TMP_DIR/narration.mp3"
ffmpeg -y -i "$TMP_DIR/narration.mp3" -t 5 -af "afade=t=out:st=4.5:d=0.5" "$TMP_DIR/audio_trim.mp3"
ffmpeg -y -i "$TMP_DIR/generated.mp4" -i "$TMP_DIR/audio_trim.mp3" \
  -c:v libx264 -crf 23 -preset fast -c:a aac -b:a 128k \
  -map 0:v:0 -map 1:a:0 -shortest "$TMP_DIR/intro.mp4"
```

### 6. 组织产物并写入博客目录

确定 topic slug（从文章标题自动生成英文短横线名），在代码仓本地 `blog/` 下创建目录结构。

**如果 `blog/` 目录尚未存在，先创建它并初始化 `_tools/`：**
```bash
# 假设当前在仓库根目录
mkdir -p blog/_tools
cat > blog/_tools/README.md << 'TOOLSEOF'
# Blog Tools

此目录存放所有博客通用的工具/模板文件。
- `blog-styles.css` — 通用样式
- `article-template.html` — 文章 HTML 模板
TOOLSEOF
```

**如果 `blog/` 已有 `_tools/`，直接复用不覆盖已有文件。**

**将临时目录中的产物迁入博客子目录：**
```bash
TOPIC_SLUG="从标题自动生成的英文短横线名"
mkdir -p "blog/$TOPIC_SLUG/images"
cp /tmp/blog-gen-*/index.html "blog/$TOPIC_SLUG/index.html"
cp /tmp/blog-gen-*/intro.mp4 "blog/$TOPIC_SLUG/intro.mp4"
cp /tmp/blog-gen-*/cover.png "blog/$TOPIC_SLUG/cover.png"
cp /tmp/blog-gen-*/images/*.png "blog/$TOPIC_SLUG/images/" 2>/dev/null || true
```

**创建 meta.json：**
```bash
cat > "blog/$TOPIC_SLUG/meta.json" << JSONEOF
{
  "title": "博客标题",
  "description": "一句话摘要",
  "tags": ["tag1", "tag2"],
  "author": "Grape AI",
  "date": "$(date +%Y-%m-%d)",
  "featured_image": "cover.png"
}
JSONEOF
```

### 7. 提交 PR

**前置准备：** 确保仓库已克隆到本地且当前在正确的分支。从 `dev` 或 `main` 分出一个 feature 分支：

```bash
cd /path/to/repo
git fetch origin
BASE_BRANCH="dev"  # 或从仓库配置读取
BRANCH_NAME="feat/blog-${TOPIC_SLUG}-$(date +%Y%m%d)"
git checkout -b "$BRANCH_NAME" "origin/$BASE_BRANCH"
```

**阶段性提交（每个文件一个原子变更，方便审查）：**
```bash
# 1. 先提交 _tools/（如果没有变动则跳过）
if git diff --name-only --cached | grep -q '^blog/_tools/'; then
  git add blog/_tools/
  git commit -m "docs(blog): add shared blog tools and templates"
fi

# 2. 提交博客内容
git add "blog/$TOPIC_SLUG/"
git commit -m "docs(blog): add article '$BLOG_TITLE'

- HTML 文章
- 封面图 + 配图
- 5秒介绍视频
- 元数据 meta.json"
```

**推送并创建 PR（使用 Python 调用 GitCode API）：**

```python
import json, urllib.request, os

TOKEN = os.environ.get("BOT_GITCODE_TOKEN", "")
REPO = os.environ.get("REPO_FULL_NAME", "")
BRANCH = os.environ["BRANCH_NAME"]
BASE_BRANCH = "dev"
TOPIC = "博客标题"

# 推送
os.system(f"git push origin {BRANCH}")

# 创建 PR
pr_body = json.dumps({
    "title": f"[博客] {TOPIC}",
    "head": BRANCH,
    "base": BASE_BRANCH,
    "body": f"## 变更说明\n\n新增博客文章：{TOPIC}\n\n## 文件变更\n\n"
            f"| 文件 | 说明 |\n"
            f"|------|------|\n"
            f"| `blog/{BRANCH.split('-', 2)[-1]}/` | 博客产物目录 |\n"
            f"| `blog/_tools/` | 博客通用工具（首次创建） |\n\n"
            f"## 后续步骤\n\n创建后交办 SRE Agent 发布博客。"
}).encode("utf-8")

req = urllib.request.Request(
    f"https://api.gitcode.com/api/v5/repos/{REPO}/pulls",
    data=pr_body,
    method="POST"
)
req.add_header("PRIVATE-TOKEN", TOKEN)
req.add_header("Content-Type", "application/json")

with urllib.request.urlopen(req) as r:
    result = json.loads(r.read())
    pr_number = result.get("number", "")
```

**在 Issue 发评论告知 PR 已创建（评论 body 前缀 `[coding Agent]`）：**

```bash
curl -s --max-time 30 -X POST \
  -H "PRIVATE-TOKEN: $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "[coding Agent] ✅ 博客已提交\n\n博客文章已生成并通过 PR #PR_NUMBER 提交到仓库。\n\n- 目录：blog/TOPIC_SLUG/\n- 分支：BRANCH_NAME\n- PR：'$REPO_FULL_NAME'/pull/PR_NUMBER\n\n下一步由 SRE Agent 进行博客发布。"}' \
  "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME/issues/$ISSUE_NUMBER/comments"
```

### 8. ✅ 交办 SRE 发布（必须执行）

PR 创建后，**必须**通过 A2A 通知 SRE Agent 进行博客发布。传递的上下文中包含 PR 信息，SRE 需 PR 合并后才能发布：

```bash
# 构造 A2A 上下文
A2A_CONTEXT='{
  "agent": "sre",
  "context": "博客内容已生成并提交到代码仓库。\n\n- 仓库：'$REPO_FULL_NAME'\n- 分支：'$BRANCH_NAME'\n- PR 编号：'$PR_NUMBER'\n- 博客目录：blog/'$TOPIC_SLUG'/（仓库内与 src/ 同级）\n- 博客主题：'$TOPIC'\n\n请完成以下步骤：\n1. 合并 PR\n2. 从 blog/ 目录读取文章内容\n3. **上传发布到目标博客网站**\n4. 在 Issue 评论反馈发布结果"
}'

curl -X POST {{SERVER_URL}}/api/a2a \
  -H "Authorization: Bearer {{A2A_TOKEN}}" \
  -H "Content-Type: application/json" \
  -d "$A2A_CONTEXT"
```

> ⚠️ `{{SERVER_URL}}` 和 `{{A2A_TOKEN}}` 从项目知识中获取，勿硬编码。

## 硬规则

| 行为 | 态度 |
|------|------|
| 按场景识别结果执行对应流程 | ✅ **必须做** |
| 先学习参考文章再写 | ✅ **必须做，不可跳过** |
| 配图间隔分布，不连续堆叠 | ✅ **必须做** |
| 文章顶部嵌入视频 | ✅ **必须做（如有）** |
| 至少包含一个特殊元素 | ✅ **表格/引用/高亮块至少选一个** |
| 涉及具体产品时先官网调研再生成配图/视频 | ✅ **必须做（不可跳过）** |
| 图片/视频 prompt 必须包含 real product 视觉特征 | ✅ **必须做（颜色、logo、前脸等须官网核实）** |
| 图片生成后必须叠加真实产品 logo | ✅ **必须做（logo_overlay.png 存在时不可跳过）** |
| 每次 API 调用间隔 ≥ 1 秒 | ✅ **必须做（防限流）** |
| 博客产物必须提交到仓库 blog/ 目录而非临时路径 | ✅ **必须做** |
| blog/_tools/ 已存在时复用它，不覆盖已有文件 | ✅ **必须做** |
| 从 dev 分支分出 feature 分支 | ✅ **必须做** |
| 提交 PR 后在 Issue 发评论告知 | ✅ **必须做，不可跳过** |
| ✔️ **创建 PR 后 A2A 通知 SRE 发布** | ✅ **必须做，不可跳过** |
| ✔️ **任务结束输出 JSON 块** | ✅ **必须做，不可跳过** |
| 直接合并 PR | ❌ **禁止（仅创建，合并由 SRE 执行）** |
| 修改 blog/ 以外仓库文件 | ❌ **禁止（除非博客工具初始化）** |
| 直接调用博客发布 API（curl/requests 调用 blog 相关 API） | ❌ **禁止（SRE 职责）** |
| 直接上传文件到博客服务器 | ❌ **禁止（SRE 职责）** |
| 直接创建/更新/删除博客文章 | ❌ **禁止（SRE 职责）** |
| 仅凭模型预训练记忆生成产品外观不核实官网 | ❌ **禁止（须先执行步骤 3 产品调研）** |
| 使用 I2V（图生视频）模型生成视频 | ❌ **禁止（只能用 T2V 文生视频模型）** |

## 输出格式

任务完成后，**必须在最终答复末尾输出结构化 JSON 块**：

```json
{
  "actions": ["blog_generation", "pr_created", "a2a_to_sre"],
  "conclusion": "completed",
  "artifacts": {
    "blog_dir": "blog/{topic-slug}/",
    "branch": "feat/blog-{topic}-{date}",
    "pr_number": 123,
    "pr_url": "https://gitcode.com/{owner}/{repo}/pull/123",
    "images": 5,
    "has_video": true
  },
  "next_step": "已创建 PR 并 A2A 通知 SRE Agent 合并后发布博客",
  "summary": "已生成图文并茂的博客文章，提交到仓库 blog/ 目录，创建 PR 并交办 SRE 发布"
}
```

**不输出 JSON 块视为任务未完成。**

## 参考

- `$SKILL_DIR/references/gitcode-api-reference.md` — GitCode API 调用规范
- qwen-image 图片生成：`POST /v1/images/generations`（URL/Key/Model 从项目知识获取）
- Wan2.2 T2V 视频生成：`POST /v1/video/generations`（URL/Key/Model 从项目知识获取，⚠️ 只能用 T2V 模型，禁止 I2V）
- Edge TTS：`edge-tts --voice zh-CN-XiaoxiaoNeural --text "文案" --write-media output.mp3`
- A2A 端点：`POST {{SERVER_URL}}/api/a2a`（URL 和 Token 从项目知识获取）