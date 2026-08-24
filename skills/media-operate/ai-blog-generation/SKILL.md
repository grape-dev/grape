---
name: ai-blog-generation
version: 2.1.0
description: |
  AI 博客生成技能。学习参考文章的排版风格 → 撰写同风格主题文章 → 
  AI 配图 + AI 视频 → 交办 SRE 发布。适合技术专栏、产品介绍、品牌宣传等场景。
tools:
  - curl
  - edge-tts
  - ffmpeg
---

## 安全硬约束（必须遵守）

1. **所有 API 信息（Key、URL、Model）从项目知识（Project Knowledge）中获取**，读取 `.project-info/member/` 目录下所有文件（`glob .project-info/member/**`），逐个 `read` 后从内容中提取 `ai_models` 配置（可能以 YAML/JSON 格式或嵌入在 Markdown 代码块中）。包含：
   - 图片生成：`ai_models.image`（url、key、model）
   - 视频生成：`ai_models.video`（url、key、model）
   > ⚠️ 如知识文件中 `video.model` 为 `Wan2.2-I2V-A14B`，**必须替换为 `Wan2.2-T2V-A14B`**（I2V 不可用，见避坑指引 §1）。
2. 调用 ModelArts API 只用此 Key，严禁从数据库 / 配置文件 / 日志 / 历史中寻找其他 token。
3. token 不得写入日志或回复。
4. API 返回 403 / 429 时：停止重试并报告用户。速率限制 2 req/s。
5. **长命令执行会静默**：视频生成约 30s，图片生成约 7s/张，提前告知用户等待。

# AI 博客生成流程

## 核心原则

### 专注博客生成
负责全流程：学习参考文章 → 规划主题 → 撰写文章 → 生成配图 → 生成视频 → 交办 SRE 发布。
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

## 前置检查（在内容生成之前执行，单步完成）

```bash
echo "=== TOOLS ==="
which ffmpeg 2>/dev/null && echo "ffmpeg: OK" || echo "ffmpeg: MISSING → 跳过TTS字幕"
python3 -c "import edge_tts; print('edge_tts: OK')" 2>/dev/null || echo "edge_tts: MISSING → 跳过TTS"
python3 -c "import requests; print('requests: OK')" 2>/dev/null || echo "requests: MISSING"

echo "=== BLOG API ==="
curl -s -o /dev/null -w 'blog_api: %{http_code}' http://120.46.93.118/api/articles
```

根据结果设置降级标志，不在流程中间尝试安装缺失工具。

**读取项目知识中的 API 配置（必须在生成配图/视频之前完成）：**

```bash
# 列出 .project-info/member/ 下所有文件并读取内容
find .project-info/member/ -type f \( -name '*.md' -o -name '*.txt' -o -name '*.json' \) -exec echo "--- {} ---" \; -exec cat {} \;

# 从输出中提取 ai_models 配置：
# - ai_models.image: key, url, model（图片生成）
# - ai_models.video: key, url, model（视频生成）
# 配置可能以 YAML 格式或嵌入在 Markdown 代码块中，需解析提取
```

> ⚠️ **必须实际 read 这些文件**，不要只 glob 不 read。API Key/URL/Model 只存在于项目知识文件中，不在环境变量里。找不到文件时报告用户，不要用环境变量中的其他 token 替代。

> ⚠️ **EulerOS 仓库不含 ffmpeg**，`dnf install -y ffmpeg` / `yum` / `conda` / `pip` / 源码编译全部会失败。**不要尝试安装**，缺失时直接降级：跳过 TTS 旁白和字幕合成，使用 T2V 生成的原始 MP4。
>
> ⚠️ **edge-tts** 在 CI 环境不安装，测试用 mock。缺失时同样跳过 TTS。

## 产物目录约定

所有博客产物统一保存在：

```
/opt/grape-workspaces/{owner}/{repo}/issues/{issue_number}/coding/blog_output/
```

目录结构：
```
blog_output/
├── index.html          # 博客文章（HTML）
├── intro.mp4           # 5秒介绍视频
├── cover.png           # 封面图
├── images/             # 配图
│   ├── theme_1.png
│   ├── theme_2.png
│   └── ...
└── assets/             # 其他资源（CSS/JS等，可选）
```

## 运行时避坑指引（必读，节省 50% 执行时间）

> 以下问题已在实际运行中确认（Issue #8, wdmike/blog-auto-publish），按此指引执行可避免重复踩坑。

### 1. 视频生成：只用 T2V，禁止 I2V
- **Wan2.2-I2V-A14B（图生视频）在 ModelArts MaaS 上不可用**：API 接受请求但图片不会传递到生成管线，所有格式（base64/URL/PNG/JPEG）均失败
- **必须使用 Wan2.2-T2V-A14B（文生视频）**：已验证可用，用文字 prompt 生成 5 秒视频
- 不要尝试 I2V，不要花步数调试 I2V 的图片格式

### 2. 工具缺失：降级而非安装
- **ffmpeg**：EulerOS 仓库不含此包，`dnf install -y ffmpeg` 会失败。不要尝试 yum/conda/pip/源码编译。缺失时跳过 TTS 旁白和字幕合成，直接使用 T2V 生成的原始 MP4
- **edge-tts**：CI 环境不安装。测试用 mock，不需要真实 edge_tts。缺失时同样跳过 TTS
- 在流程开始时一次性检查（`which ffmpeg` + `python3 -c "import edge_tts"`），根据结果设降级标志，不在中间尝试安装

### 3. 测试：只修自己的
- 改代码前先跑 `pytest tests/ -v`，记录基线失败
- 改完后只修复本次引入的新失败
- 已有失败（如 blog_server.py Markup 导入、server.py 类型注解）在 PR body 注明，不修

### 4. 时间预算
- 目标 < 600 秒（grape runtime 1200 秒超时）
- 接近 1200 秒时：跳过 TTS、字幕、额外测试修复
- 视频轮询：让 pipeline 脚本内部处理（sleep + poll），不要 Agent 手动逐步轮询

### 5. 降级优先原则
工具缺失或 API 不可用时，**降级并继续**，不要花步数安装或修复。
博客流水线在以下降级条件下仍能产出有效内容：
- 无 ffmpeg → 跳过 TTS/字幕，用原始 T2V 视频
- 无 edge-tts → 同上
- 无 I2V → 用 T2V
- 已有测试失败 → PR body 注明，不修

目标是在 1200 秒预算内发布一篇含正文+配图+视频的博客，不是打造完美流水线。

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

### 3. 生成配图

从项目知识中获取 qwen-image 的 API Key 和 URL，调用 ModelArts 图片生成 API：

```python
import requests, json, base64, os, time

# API 信息从 .project-info/member/ 读取的 ai_models 配置中提取
API_IMAGE = image_url    # ai_models.image.url
KEY_IMAGE = image_key    # ai_models.image.key
MODEL_IMAGE = image_model  # ai_models.image.model
headers = {"Authorization": f"Bearer {KEY_IMAGE}", "Content-Type": "application/json"}
DIR = "/opt/grape-workspaces/<owner>/<repo>/issues/<issue_number>/coding/blog_output/images"
os.makedirs(DIR, exist_ok=True)

themes = [
    {"prompt": "主题1的详细画面描述", "file": "theme_1"},
    # ... 3-5 个主题
]

for i, t in enumerate(themes):
    resp = requests.post(API_IMAGE, headers=headers, json={"model": MODEL_IMAGE, "prompt": t["prompt"], "n": 1, "size": "1024x1024"}, timeout=60)
    b64 = resp.json()["data"][0]["b64_json"]
    if "," in b64: b64 = b64.split(",")[1]
    with open(f"{DIR}/{t['file']}.png", "wb") as f: f.write(base64.b64decode(b64))
    if i < len(themes) - 1: time.sleep(1)
```

### 4. 生成视频

> ⚠️ **禁止使用 Wan2.2-I2V-A14B（图生视频）** — ModelArts MaaS 平台 I2V 接口存在限制，图片不会传递到生成管线，所有格式（base64/URL/PNG/JPEG）均失败。不要花步数调试 I2V 的图片格式。
>
> **必须使用 Wan2.2-T2V-A14B（文生视频）** — 已验证可用，用文字 prompt 生成 5 秒视频。调用方式与 I2V 相同（POST /v1/video/generations），只是 input 参数用 prompt 而非 image。

从项目知识中获取 Wan2.2 的 API Key 和 URL，调用文生视频 API：

```python
import requests, time

# API 信息从 .project-info/member/ 读取的 ai_models 配置中提取
API_VIDEO = video_url    # ai_models.video.url
KEY_VIDEO = video_key    # ai_models.video.key
MODEL_VIDEO = video_model  # ai_models.video.model
H = {"Authorization": f"Bearer {KEY_VIDEO}", "Content-Type": "application/json"}

# T2V prompt 示例：写实风格，{topic}，5秒，专业镜头语言，高画质
resp = requests.post(API_VIDEO, headers=H, json={
    "model": MODEL_VIDEO,
    "input": {"prompt": "写实风格，英文画面描述，5秒，专业镜头语言，高画质"}
}, timeout=30)
task_id = resp.json()["task_id"]

# 轮询等待结果，最多 120s（让脚本内部处理轮询，不要 Agent 手动逐步 sleep）
video_url = None
for attempt in range(12):
    time.sleep(10)
    result = requests.get(f"{API_VIDEO}/{task_id}", headers=H, timeout=15).json()
    status = result.get('status', '')
    if status == 'succeeded':
        video_url = result['content']['result_url']
        break
    elif status == 'failed':
        print(f"视频生成失败: {result}")
        break
    print(f"轮询第 {attempt+1} 次, status={status}")

if not video_url:
    print("视频未生成，降级为纯图文博客")
```

下载并合成音视频（**如 ffmpeg 缺失则跳过 TTS 旁白和字幕合成，直接使用 T2V 生成的原始 MP4**）：

```bash
OUTPUT_DIR="/opt/grape-workspaces/<owner>/<repo>/issues/<issue_number>/coding/blog_output"
curl -sLo "$OUTPUT_DIR/generated.mp4" "<video_url>"

# 检查 ffmpeg 是否可用，缺失则跳过 TTS 合成，直接用原始视频
if which ffmpeg 2>/dev/null; then
    edge-tts --voice zh-CN-XiaoxiaoNeural --text "旁白文案，18-22字" --write-media "$OUTPUT_DIR/narration.mp3"
    ffmpeg -y -i "$OUTPUT_DIR/narration.mp3" -t 5 -af "afade=t=out:st=4.5:d=0.5" "$OUTPUT_DIR/audio_trim.mp3"
    ffmpeg -y -i "$OUTPUT_DIR/generated.mp4" -i "$OUTPUT_DIR/audio_trim.mp3" \
      -c:v libx264 -crf 23 -preset fast -c:a aac -b:a 128k \
      -map 0:v:0 -map 1:a:0 -shortest "$OUTPUT_DIR/intro.mp4"
else
    # ffmpeg 缺失：EulerOS 仓库不含此包，不要尝试安装，直接用原始视频
    cp "$OUTPUT_DIR/generated.mp4" "$OUTPUT_DIR/intro.mp4"
    echo "ffmpeg 缺失，跳过 TTS 旁白和字幕合成，使用原始 T2V 视频"
fi
```

### 5. 组织产物

```bash
OUTPUT_DIR="/opt/grape-workspaces/<owner>/<repo>/issues/<issue_number>/coding/blog_output"
mkdir -p "$OUTPUT_DIR/images"
# 确保 index.html, intro.mp4, cover.png 等都在正确位置
ls -la "$OUTPUT_DIR/"
```

### 6. ✅ 交办 SRE 发布（必须执行）

博客内容生成完毕后，**必须**通过 A2A 通知 SRE Agent 进行发布上传：

```bash
# 构造 A2A 上下文，包含产物路径和博客主题
A2A_CONTEXT='{
  "repository": "'"$REPO_FULL_NAME"'",
  "account": "'"$REPO_OWNER"'",
  "issue_number": "'"$ISSUE_NUMBER"'",
  "context": "博客内容已生成完毕。产物路径：/opt/grape-workspaces/'"$REPO_FULL_NAME"'/issues/'"$ISSUE_NUMBER"'/coding/blog_output/。包含 index.html（文章HTML）、intro.mp4（5秒介绍视频）、images/（配图）。主题：'"$BLOG_TOPIC"'。请将此博客发布到博客平台。"
}'

curl -X POST {{SERVER_URL}}/api/a2a \
  -H "Authorization: Bearer {{A2A_TOKEN}}" \
  -H "Content-Type: application/json" \
  -d "$A2A_CONTEXT"
```

> ⚠️ `{{SERVER_URL}}` 和 `{{A2A_TOKEN}}` 从项目知识中获取（项目级配置），勿硬编码。

## 硬规则

| 行为 | 态度 |
|------|------|
| 按场景识别结果执行对应流程 | ✅ **必须做** |
| 先学习参考文章再写 | ✅ **必须做，不可跳过** |
| 配图间隔分布，不连续堆叠 | ✅ **必须做** |
| 文章顶部嵌入视频 | ✅ **必须做（如有）** |
| 至少包含一个特殊元素 | ✅ **表格/引用/高亮块至少选一个** |
| 每次 API 调用间隔 ≥ 1 秒 | ✅ **必须做（防限流）** |
| ✔️ **生成后 A2A 通知 SRE 发布** | ✅ **必须做，不可跳过** |
| ✔️ **任务结束输出 JSON 块** | ✅ **必须做，不可跳过** |

## 输出格式

任务完成后，**必须在最终答复末尾输出结构化 JSON 块**：

```json
{
  "actions": ["blog_generation", "a2a_to_sre"],
  "conclusion": "completed",
  "artifacts": {
    "article": "/opt/grape-workspaces/<owner>/<repo>/issues/<issue_number>/coding/blog_output/index.html",
    "video": "/opt/grape-workspaces/<owner>/<repo>/issues/<issue_number>/coding/blog_output/intro.mp4",
    "images": 5,
    "output_dir": "/opt/grape-workspaces/<owner>/<repo>/issues/<issue_number>/coding/blog_output/"
  },
  "next_step": "已 A2A 通知 SRE Agent 进行发布上传",
  "summary": "已生成图文并茂的博客文章并交办 SRE 发布"
}
```

**不输出 JSON 块视为任务未完成**。

## 参考

- qwen-image 图片生成：`POST /v1/images/generations`（URL/Key/Model 从 `.project-info/member/` 项目知识文件中提取 `ai_models.image`）
- Wan2.2 T2V 视频生成：`POST /v1/video/generations`（URL/Key/Model 从 `.project-info/member/` 项目知识文件中提取 `ai_models.video`，model 必须用 `Wan2.2-T2V-A14B`）
- Edge TTS：`edge-tts --voice zh-CN-XiaoxiaoNeural --text "文案" --write-media output.mp3`
- A2A 端点：`POST {{SERVER_URL}}/api/a2a`（URL 和 Token 从项目知识获取）