---
name: qwen-image-generation
version: 1.0.0
description: |
  图片生成技能。通过 ModelArts MaaS 的 qwen-image 模型批量生成主题配图。
  支持城市介绍、产品展示、景点宣传等场景的配图生成。
tools:
  - curl
---

## 安全硬约束（必须遵守）

1. 调用前确认 API Key 已正确设置，API Key **从项目知识（Project Knowledge）中获取**。
2. 调用 ModelArts API 只用此 Key，严禁从数据库 / 配置文件 / 日志 / 历史中寻找其他 token。
3. token 不得写入日志或回复。
4. API 返回 403 / 429 时：停止重试并报告用户。
5. **长命令执行会静默**：生成图片较慢时需告知用户等待。

# 图片生成流程

## 核心原则

### 专注图片生成

负责图片生成全流程：需求确认 → 规划主题 → 调用 qwen-image 批量生成 → 交付配图。
不负责代码实现、PR 管理、视频合成。

### 依赖前序分析

通过 A2A 被调用时，prompt 顶部包含「前序 Agent 处理摘要」。同时 prompt 的「用户反馈沉淀的共享知识」段中包含了项目知识中的 API 凭据信息。
- 从 chat / 对话 skill 触发 → 摘要包含用户原始需求
- 必须与用户确认主题数量和风格后再开始生成

### 超出能力范围时主动请求支援

如果需要将生成的图片用于视频合成或其他后续处理，通过 A2A 交办。

## 场景识别

收到事件或 A2A 调用后，先判断触发场景：

| 特征关键词 | 场景 | 执行流程 |
|-----------|------|---------|
| 含「图片」「配图」「生成图片」「插图」「封面图」等 | **图片生成** | 按本文档流程执行 |
| 含「深圳」「城市」「景点」「产品展示」等 + 「图片」 | **图片生成** | 按本文档流程执行 |
| 其他 | 不做处理，结束 | — |

## API 凭证

**API Key 存储在项目知识（Project Knowledge）中**。执行任务时，prompt 中的「用户反馈沉淀的共享知识」段会包含 API 凭据信息，从中提取使用。

> 图片理解模型：`qwen2.5-vl-72b`（`/v1/chat/completions`），可用于生成更精确的图片描述 prompt。同样需要在项目知识中获取该 API 的 Key。

## 工作流

### 1. 确认需求

与用户确认以下信息：
- **主题**：生成什么内容的配图（城市、产品、场景等）
- **数量**：需要几张（推荐 3-5 张）
- **风格**：实景/插画/科技感/水墨风等
- **用途**：配图/封面/视频素材等

### 2. 规划主题列表

根据用户主题规划 3-5 个具体场景，每个场景一个英文 prompt。以深圳为例：

| 场景 | prompt 方向 |
|------|------------|
| 城市天际线 | 平安金融中心天际线夜景，摩天大楼群，灯光璀璨 |
| 自然景观 | 深圳湾公园滨海长廊，日出，海面波光 |
| 商业地标 | 华强北电子市场，霓虹灯招牌，繁华商业街 |
| 科技园区 | 南山科技园，高新企业，现代化办公楼 |
| 未来城区 | 前海自贸区，玻璃幕墙大厦，未来感城市 |

### 3. 批量生成图片

创建输出目录：

```bash
mkdir -p /root/output/images
```

```python
import requests, json, base64, os, time

API = "https://api.modelarts-maas.com/v1/images/generations"
KEY = # 从 prompt 的「共享知识」段读取 KEY 值，如：os.environ.get("MODELARTS_API_KEY", "从知识段读取的KEY")
headers = {
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json"
}
DIR = "/root/output/images"
os.makedirs(DIR, exist_ok=True)

# 主题列表
themes = [
    {"prompt": "场景1的详细画面描述", "file": "theme_1"},
    {"prompt": "场景2的详细画面描述", "file": "theme_2"},
    {"prompt": "场景3的详细画面描述", "file": "theme_3"},
]

for i, theme in enumerate(themes):
    payload = {
        "model": "qwen-image",
        "prompt": theme["prompt"],
        "n": 1,
        "size": "1024x1024"
    }
    resp = requests.post(API, headers=headers, json=payload, timeout=60)
    data = resp.json()
    b64 = data["data"][0]["b64_json"]
    if "," in b64:
        b64 = b64.split(",")[1]
    img_data = base64.b64decode(b64)
    filepath = f"{DIR}/{theme['file']}.png"
    with open(filepath, "wb") as f:
        f.write(img_data)
    print(f"[{i+1}/{len(themes)}] {theme['file']}.png ✅")
    
    # 速率限制: 2 req/s
    if i < len(themes) - 1:
        time.sleep(1)
```

### 4. 交付结果

将生成的图片报告给用户：
- 图片数量及文件路径
- 每张图片的主题说明
- 支持后续使用（如视频合成配图）

## 获取更精准的 prompt（可选）

如果需要更高质量的图片，可以先调用 **qwen2.5-vl-72b** 图片理解模型来优化 prompt：

```python
import requests, json

API_CHAT = "https://api.modelarts-maas.com/v1/chat/completions"
KEY = # 从 prompt 的「共享知识」段读取 KEY 值

payload = {
    "model": "qwen2.5-vl-72b",
    "messages": [
        {
            "role": "user",
            "content": "为深圳城市天际线生成一句英文图片生成prompt，包含细节描述、光线、风格"
        }
    ]
}
resp = requests.post(API_CHAT, headers=headers, json=payload, timeout=30)
optimized_prompt = resp.json()["choices"][0]["message"]["content"]
```

## 硬规则

| 行为 | 态度 |
|------|------|
| 按场景识别结果执行对应流程 | ✅ **必须做** |
| 确认需求后再开始生成 | ✅ **必须做** |
| 每次生成间隔 ≥ 1 秒（防限流） | ✅ **必须做** |
| 生成后验证图片是否成功 | ✅ **必须做** |
| 使用英文 prompt 可获更好效果 | ✅ **推荐使用英文** |
| ✔️ **任务结束输出 JSON 块** | ✅ **必须做，不可跳过** |

## 输出格式

任务完成后，**必须在最终答复末尾输出结构化 JSON 块**：

```json
{
  "actions": ["image_generation"],
  "conclusion": "completed",
  "artifacts": {
    "count": 5,
    "directory": "/root/output/images",
    "files": [
      {"file": "theme_1.png", "description": "深圳天际线夜景"},
      {"file": "theme_2.png", "description": "深圳湾公园"}
    ]
  },
  "next_step": "用户确认图片或用于视频合成",
  "summary": "已成功生成 5 张深圳主题配图"
}
```

**不输出 JSON 块视为任务未完成**。

## 参考

- ModelArts qwen-image API：`POST /v1/images/generations`
- 速率限制：2 req/s
- 输出格式：返回 `b64_json`（base64 编码 PNG）