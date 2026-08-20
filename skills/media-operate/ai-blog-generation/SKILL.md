---
name: ai-blog-generation
version: 1.0.0
description: |
  AI 博客生成技能。学习参考文章的排版风格 → 撰写同风格主题文章 → 
  AI 配图 + AI 视频 + Nginx 部署。适合技术专栏、产品介绍、品牌宣传等场景。
tools:
  - curl
  - edge-tts
  - ffmpeg
---

## 安全硬约束（必须遵守）

1. 调用前确认 API Key 已正确设置。运行时替换 `<your_api_key>` 为实际值。
2. 调用 ModelArts API 只用此 Key，严禁从数据库 / 配置文件 / 日志 / 历史中寻找其他 token。
3. token 不得写入日志或回复。
4. API 返回 403 / 429 时：停止重试并报告用户。速率限制 2 req/s。
5. **长命令执行会静默**：视频生成约 30s，图片生成约 7s/张，提前告知用户等待。

# AI 博客生成流程

## 核心原则

### 专注博客生成

负责全流程：学习参考文章 → 规划主题 → 撰写文章 → 生成配图 → 生成视频 → Nginx 部署交付。
不负责代码实现、PR 管理、Issue 分类。

### 三步模仿法

1. **学结构**：分析参考文章的标题层级、段落长度、配图密度、引用/高亮/表格等特殊元素的用法
2. **学风格**：分析语气（技术深度/轻松活泼/营销推广）、句式特点
3. **学排版**：分析配图位置、标签样式、元数据展示方式

### 依赖前序分析

通过 A2A 被调用时，prompt 顶部包含「前序 Agent 处理摘要」。
必须与用户确认参考文章和主题后再开始生成。

### 超出能力范围时主动请求支援

如需要发布到小红书/公众号等平台，通过 A2A 交办给发布技能。

## 场景识别

收到事件或 A2A 调用后，先判断触发场景：

| 特征关键词 | 场景 | 执行流程 |
|-----------|------|---------|
| 含「博客」「文章」「专栏」「生成文章」「写文章」「图文并茂」等 | **博客生成** | 按本文档流程执行 |
| 含「学习这个」「仿照这个」「参考这个」等 + URL | **参考文章学习** | 先抓取分析，再按博客生成流程 |
| 其他 | 不做处理，结束 | — |

## 前置依赖

```bash
apt-get install -y ffmpeg
pip3 install --break-system-packages --index-url https://pypi.tuna.tsinghua.edu.cn/simple edge-tts Pillow requests
```

## 工作流

### 1. 学习参考文章

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

### 3. 生成配图

参考上文「qwen-image-generation」skill，为文章主题生成 3-5 张配图：

```python
import requests, json, base64, os, time

API = "https://api.modelarts-maas.com/v1/images/generations"
KEY = "<your_api_key>"
headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
DIR = "/root/output/images"
os.makedirs(DIR, exist_ok=True)

themes = [
    {"prompt": "主题1的详细画面描述", "file": "theme_1"},
    # ... 3-5 个主题
]

for i, t in enumerate(themes):
    resp = requests.post(API, headers=headers, json={"model": "qwen-image", "prompt": t["prompt"], "n": 1, "size": "1024x1024"}, timeout=60)
    b64 = resp.json()["data"][0]["b64_json"]
    if "," in b64: b64 = b64.split(",")[1]
    with open(f"{DIR}/{t['file']}.png", "wb") as f: f.write(base64.b64decode(b64))
    if i < len(themes) - 1: time.sleep(1)
```

### 4. 生成视频

参考上文「wan-video-generation」skill，生成 5s 介绍视频：

```python
import requests, time

API = "https://api.modelarts-maas.com/v1/video/generations"
KEY = "<your_api_key>"
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

resp = requests.post(API, headers=H, json={
    "model": "Wan2.2-T2V-A14B",
    "input": {"prompt": "英文画面描述"}
}, timeout=30)
task_id = resp.json()["task_id"]
time.sleep(30)
result = requests.get(f"{API}/{task_id}", headers=H).json()
if result['status'] == 'succeeded':
    video_url = result['content']['result_url']
```

下载并合成音视频：

```bash
curl -sLo /root/output/generated.mp4 "<video_url>"
edge-tts --voice zh-CN-XiaoxiaoNeural --text "旁白文案，18-22字" --write-media /root/output/narration.mp3
ffmpeg -y -i /root/output/narration.mp3 -t 5 -af "afade=t=out:st=4.5:d=0.5" /root/output/audio_trim.mp3
ffmpeg -y -i /root/output/generated.mp4 -i /root/output/audio_trim.mp3 \
  -c:v libx264 -crf 23 -preset fast -c:a aac -b:a 128k \
  -map 0:v:0 -map 1:a:0 -shortest /root/output/intro.mp4
```

### 5. 组织文件并部署

```bash
# 创建输出目录
mkdir -p /root/blog_output/images
cp /root/output/index.html /root/blog_output/
cp /root/output/intro.mp4 /root/blog_output/
cp /root/output/images/*.png /root/blog_output/images/

# 部署到 nginx
cp -r /root/blog_output /var/www/blog
chown -R www-data:www-data /var/www/blog

# 在 nginx 配置中增加 location
# location /blog/ { root /var/www; autoindex on; index index.html; }
```

## 完整自动化脚本

```python
import requests, time, subprocess, os, base64, json

API_IMG = "https://api.modelarts-maas.com/v1/images/generations"
API_VID = "https://api.modelarts-maas.com/v1/video/generations"
KEY = "<your_api_key>"
DIR = "/root/blog_output"

def gen_images(themes):
    os.makedirs(f"{DIR}/images", exist_ok=True)
    H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    for i, t in enumerate(themes):
        r = requests.post(API_IMG, headers=H, json={"model":"qwen-image","prompt":t["prompt"],"n":1,"size":"1024x1024"}, timeout=60)
        b64 = r.json()["data"][0]["b64_json"].split(",")[-1]
        with open(f"{DIR}/images/{t['file']}.png","wb") as f: f.write(base64.b64decode(b64))
        print(f"  img [{i+1}/{len(themes)}] {t['file']}.png")
        if i < len(themes)-1: time.sleep(1)

def gen_video(prompt, narration):
    H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    r = requests.post(API_VID, headers=H, json={"model":"Wan2.2-T2V-A14B","input":{"prompt":prompt}}, timeout=30)
    tid = r.json()["task_id"]
    time.sleep(30)
    r2 = requests.get(f"{API_VID}/{tid}", headers=H).json()
    if r2['status'] != 'succeeded': raise Exception(str(r2))
    subprocess.run(["curl","-sLo",f"{DIR}/generated.mp4",r2['content']['result_url']], check=True)
    subprocess.run(["edge-tts","--voice","zh-CN-XiaoxiaoNeural","--text",narration,"--write-media",f"{DIR}/narration.mp3"], check=True)
    subprocess.run(["ffmpeg","-y","-i",f"{DIR}/narration.mp3","-t","5","-af","afade=t=out:st=4.5:d=0.5",f"{DIR}/audio_trim.mp3"], check=True, capture_output=True)
    subprocess.run(["ffmpeg","-y","-i",f"{DIR}/generated.mp4","-i",f"{DIR}/audio_trim.mp3","-c:v","libx264","-crf","23","-preset","fast","-c:a","aac","-b:a","128k","-map","0:v:0","-map","1:a:0","-shortest",f"{DIR}/intro.mp4"], check=True)

def deploy_nginx(blog_dir):
    dest = "/var/www/blog"
    subprocess.run(["cp","-r",blog_dir,dest], check=True)
    subprocess.run(["chown","-R","www-data:www-data",dest], check=True)
```

## 硬规则

| 行为 | 态度 |
|------|------|
| 按场景识别结果执行对应流程 | ✅ **必须做** |
| 先学习参考文章再写 | ✅ **必须做，不可跳过** |
| 配图间隔分布，不连续堆叠 | ✅ **必须做** |
| 文章顶部嵌入视频 | ✅ **必须做（如有）** |
| 至少包含一个特殊元素 | ✅ **表格/引用/高亮块至少选一个** |
| 每次 API 调用间隔 ≥ 1 秒 | ✅ **必须做（防限流）** |
| ✔️ **任务结束输出 JSON 块** | ✅ **必须做，不可跳过** |

## 输出格式

任务完成后，**必须在最终答复末尾输出结构化 JSON 块**：

```json
{
  "actions": ["blog_generation"],
  "conclusion": "completed",
  "artifacts": {
    "article": "/var/www/blog/index.html",
    "video": "/var/www/blog/intro.mp4",
    "images": 5,
    "url": "http://<host>/blog/"
  },
  "next_step": "用户查看并确认",
  "summary": "已生成图文并茂的博客文章并部署到 nginx"
}
```

**不输出 JSON 块视为任务未完成**。

## 参考

- qwen-image 图片生成：`POST /v1/images/generations`
- Wan2.2 T2V 视频生成：`POST /v1/video/generations`
- Edge TTS：`edge-tts --voice zh-CN-XiaoxiaoNeural --text "文案" --write-media output.mp3`
- 部署路径：`/var/www/` + nginx location 配置