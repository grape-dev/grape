FROM python:3.12-slim

WORKDIR /app

# 系统依赖——gcc/g++ 编译 C 扩展（bcrypt/cffi 等）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Python 依赖（Issue #452: 使用完整 requirements.txt，生产部署需全部功能）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 代码
COPY . .

# 默认 .env（Issue #452: SQLite + OBS 关闭，生产环境通过环境变量覆盖）
COPY .env.example .env.example

# 时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Issue #452: 内网/Docker 环境关闭公网 IP 探测
ENV GRAPE_AUTO_DETECT_URL=false

EXPOSE 3000

# 只启动 3000 主服务——main:app 已包含所有路由（含 webhook + A2A + runtime API）
# 不启动 8080——run_webhook:app 是冗余的，main:app 已注册 webhook_router（main.py 第 119 行）
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers", "--forwarded-allow-ips=*"]
