---
name: wan-video-generation
version: 1.0.0
description: |
  视频/媒体生成技能。通过 ModelArts MaaS 的 Wan2.2-T2V-A14B 文生视频模型 + Edge TTS + FFmpeg 
  合成带中文旁白的短视频。替换 coding agent 的 implementation skill。
tools:
  - curl
  - edge-tts
  - ffmpeg
---

## 安全硬约束（必须遵守）

1. 调用前确认 API Key 已正确设置，API Key **从项目知识（Project Knowledge）中获取**。
2. 调用 ModelArts API 只用此 Key，严禁从数据库 / 配置文件 / 日志 / 历史中寻找其他 token。
3. token 不得写入日志或回复。
4. API 返回 403 / 429 时：停止重试并报告用户。
5. **调用 GitCode API 一律以 `references/gitcode-api-reference.md` 为准**：严禁根据经验/记忆/猜测自行拼写 API 路径。
6. **长命令执行会静默**：`pip install`、`git clone` 等长命令运行时无输出。执行时加 `timeout 300 ...` 限制。
7. **禁止前台运行长驻/服务类命令**：如 `python -m http.server`、`npm run dev` 等。改用后台启动 + curl 验证 + kill 的一次性验证模式。

# 视频/媒体生成流程

## 核心原则

### 专注视频生成

负责视频生成全流程：需求确认 → 调用 Wan2.2 T2V → Edge TTS 生成旁白 → FFmpeg 合成 → 交付结果。
不负责代码实现、PR 管理、Issue 分类（这些移交给 coding agent 的其他 skill）。

### 依赖前序分析

通过 A2A 被调用时，prompt 顶部包含「前序 Agent 处理摘要」。同时 prompt 的「用户反馈沉淀的共享知识」段中包含了项目知识中的 API 凭据信息。
- 从 chat / 对话 skill 触发 → 摘要包含用户原始需求
- 此摘要可作为快速参考，但**不保证完整性**，必须与用户再次确认关键参数后再开始工作。

### 超出能力范围时主动请求支援

如果视频生成过程中发现需要其他技能协助（如需要 GitCode API 创建 Issue 跟踪需求），通过 A2A API 交办。

### 禁止探索 Grape 内部

**不读取 Grape 内部数据**（data/、configs/、sessions.db 等）。

## 场景识别

收到事件或 A2A 调用后，先判断触发场景：

| 特征关键词 | 场景 | 执行流程 |
|-----------|------|---------|
| 含「视频」「生成视频」「短视频」「配音」「旁白」「介绍视频」等 | **视频生成** | 按本文档流程执行 |
| 含「深圳」「城市介绍」「产品展示」「景点宣传」等 | **视频生成** | 按本文档流程执行 |
| 其他 | 不做处理，结束 | — |

## API 凭证

**所有 API 信息（Key、URL、Model）均从项目知识（Project Knowledge）中读取**。执行任务时，prompt 中的「用户反馈沉淀的共享知识」段会包含 `ai-models.yaml` 中的配置信息，从中提取使用：

```yaml
# project-info/member/ai-models.yaml（示例结构）
ai_models:
  video:
    key: "<你的API Key>"
    url: "https://api.modelarts-maas.com/v1/video/generations"
    model: "Wan2.2-T2V-A14B"
```

代码中按以下方式引用（勿硬编码值）：

```python
# 从 prompt 的「共享知识」段读取，示例：
# video_url  = 共享知识中 ai_models.video.url 的值
# video_key  = 共享知识中 ai_models.video.key 的值
# video_model = 共享知识中 ai_models.video.model 的值
API = video_url    # 从项目知识中读取，勿硬编码
KEY = video_key    # 从项目知识中读取
MODEL = video_model # 从项目知识中读取
```

> ⚠️ `Wan2.2-I2V-A14B`（图生视频）有后端 bug，`input.image` 不会传给底层 pipeline，不可用。

## 前置依赖

```bash
apt-get install -y ffmpeg
pip3 install --break-system-packages --index-url https://pypi.tuna.tsinghua.edu.cn/simple edge-tts Pillow requests
```

## 工作流

### 1. 确认需求

与用户确认以下信息：
- **主题**：介绍什么（城市、产品、景点等）
- **时长**：推荐 5 秒
- **旁白文案**：用户提供或自动生成（中文约 3-4 字/秒）
- **画面风格**：默认 cinematic/aerial

### 2. 生成视频（Wan2.2 T2V）

创建输出目录：

```bash
mkdir -p /root/output
```

```python
import requests, time

API = video_url  # 从项目知识读取（ai_models.video.url）
KEY = video_key  # 从项目知识读取（ai_models.video.key）
MODEL = video_model  # 从项目知识读取（ai_models.video.model）
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

resp = requests.post(API, headers=H, json={
    "model": MODEL,
    "input": {"prompt": "英文画面描述，如：A cinematic aerial view of Shenzhen at night, skyscrapers, lights"}
}, timeout=30)
task_id = resp.json()["task_id"]
print(f"Task submitted: {task_id}")
```

轮询约 20-30 秒：

```python
time.sleep(30)
result = requests.get(f"{API}/{task_id}", headers=H).json()
if result['status'] == 'succeeded':
    video_url = result['content']['result_url']
    print(f"Video URL: {video_url}")
```

下载：

```bash
curl -sLo /root/output/generated.mp4 "<video_url>"
```

### 3. 生成旁白（Edge TTS）

时长估算：中文 3-4 字/秒

| 时长 | 字数 |
|------|------|
| 5s | 18-22 字 |
| 3s | 10-12 字 |

```bash
edge-tts --voice zh-CN-XiaoxiaoNeural --text "确认的文案" --write-media /root/output/narration.mp3
```

常用语音：`XiaoxiaoNeural`(女声) / `YunxiNeural`(男声) / `XiaoyiNeural`(解说)

### 4. FFmpeg 合成

```bash
# 修剪音频到 5 秒并淡出
ffmpeg -y -i /root/output/narration.mp3 -t 5 -af "afade=t=out:st=4.5:d=0.5" /root/output/audio_trim.mp3

# 合成视频+音频
ffmpeg -y -i /root/output/generated.mp4 -i /root/output/audio_trim.mp3 \
  -c:v libx264 -crf 23 -preset fast \
  -c:a aac -b:a 128k \
  -map 0:v:0 -map 1:a:0 -shortest /root/output/final.mp4
```

### 5. 交付结果

将最终视频位置报告给用户：
- 视频文件路径：`/root/output/final.mp4`
- 时长信息
- 旁白文案

## 一键自动化脚本

```python
import requests, time, subprocess, os

API = video_url  # 从项目知识读取
KEY = video_key  # 从项目知识读取
MODEL = video_model  # 从项目知识读取
DIR = "/root/output"

def gen_video(prompt):
    H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    r = requests.post(API, headers=H, json={"model": MODEL, "input": {"prompt": prompt}}, timeout=30)
    tid = r.json()["task_id"]
    time.sleep(30)
    r2 = requests.get(f"{API}/{tid}", headers=H).json()
    if r2['status'] != 'succeeded':
        raise Exception(str(r2))
    os.makedirs(DIR, exist_ok=True)
    subprocess.run(["curl", "-sL", "-o", f"{DIR}/v.mp4", r2['content']['result_url']], check=True)
    return f"{DIR}/v.mp4"

def gen_audio(text, dur=5):
    subprocess.run(["edge-tts", "--voice", "zh-CN-XiaoxiaoNeural", "--text", text, "--write-media", f"{DIR}/a.mp3"], check=True)
    subprocess.run(["ffmpeg", "-y", "-i", f"{DIR}/a.mp3", "-t", str(dur), "-af",f"afade=t=out:st={dur-0.5}:d=0.5", f"{DIR}/a2.mp3"], check=True, capture_output=True)
    return f"{DIR}/a2.mp3"

def combine(v, a, out):
    subprocess.run(["ffmpeg", "-y", "-i", v, "-i", a, "-c:v", "libx264", "-crf", "23", "-preset", "fast", "-c:a", "aac", "-b:a", "128k", "-map", "0:v:0", "-map", "1:a:0", "-shortest", out], check=True)
    return out

v = gen_video("A cinematc view of Shenzhen at night, skyscrapers, lights")
a = gen_audio("深圳，中国改革开放的窗口，一座创新之城，活力无限。")
final = combine(v, a, f"{DIR}/final.mp4")
print(f"✅ {final}")
```

## 硬规则

| 行为 | 态度 |
|------|------|
| 按场景识别结果执行对应流程 | ✅ **必须做** |
| 确认需求后再开始生成 | ✅ **必须做** |
| 使用 Wan2.2-T2V-A14B（T2V） | ✅ **必须用 T2V，I2V 有 bug** |
| 英文 prompt 描述画面 | ✅ **推荐使用英文** |
| 中文旁白控制字数（5s 用 18-22 字） | ✅ **必须控制** |
| 用 FFmeeg 合成音视频 | ✅ **必须做** |
| 尝试 I2V 图生视频 | ❌ **禁止（已知 bug）** |
| 修改或泄露 API Key | ❌ **禁止** |
| ️ **任务结束输出 JSON 块** | ✅ **必须做，不可跳过** |

## 输出格式

任务完成后，**必须在最终答复末尾输出结构化 JSON 块**：

```json
{
  "actions": ["video_generation"],
  "conclusion": "completed",
  "artifacts": {
    "video_path": "/root/output/final.mp4",
    "duration": "5s",
    "narration_text": "旁白文案"
  },
  "next_step": "人工查看视频",
  "summary": "已成功生成 5 秒深圳介绍视频"
}
```

**不输出 JSON 块视为任务未完成**。

## 参考

- 需要调用 GitCode API 时，读取 `references/gitcode-api-reference.md`。
- Edge TTS 语音列表：`edge-tts --list-voices`。