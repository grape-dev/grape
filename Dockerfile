FROM python:3.12-slim

WORKDIR /app

# 系统依赖——gcc/g++ 编译 C 扩展（bcrypt/cffi 等）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Python 依赖（Issue #452: 使用完整 requirements.txt，生产部署需全部功能）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Issue #666 (M3): 引擎基线镜像预装 codegraph（project-info 全量生成引擎）。
# 分发形式：npm 平台二进制包 @colbymchenry/codegraph（自包含：内置 node 运行时 + Rust 内核，
# bin/codegraph 可直接执行，见 scripts/install-codegraph.sh 注释）——非 pip 包。
# 构建期用 nodejs/npm 在线安装，装完卸载 nodejs/npm 保持镜像精简（二进制自包含不依赖系统 node）。
# best-effort：安装失败不阻断镜像构建，运行时走降级路径并显式告警（src/project_info/builder.py）。
# 安装位置：独立 prefix /opt/codegraph（QA 修复 #666：规避 apt purge nodejs/npm 时
# npm 全局目录 /usr/lib/node_modules 被连带清理的风险）；bin 软链到 /usr/local/bin。
# purge 后最终复核 codegraph --version（不可用则清理失效软链并告警，镜像仍可构建）。
RUN set -x; \
    apt-get update && apt-get install -y --no-install-recommends nodejs npm; \
    if npm install -g --prefix /opt/codegraph @colbymchenry/codegraph; then \
        ln -sf /opt/codegraph/bin/codegraph /usr/local/bin/codegraph; \
    else \
        echo "warning: codegraph install failed, project-info will run degraded with warning"; \
    fi; \
    apt-get purge -y nodejs npm; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    if codegraph --version >/dev/null 2>&1; then \
        codegraph --version; \
    else \
        echo "warning: codegraph missing after node purge, project-info will run degraded with warning"; \
        rm -f /usr/local/bin/codegraph; \
    fi

# 代码
COPY . .

# 默认 .env（Issue #452: SQLite + OBS 关闭，生产环境通过环境变量覆盖）
# 审查修复: COPY .env.example .env，确保容器内始终有 /app/.env，不再依赖宿主 bind mount
COPY .env.example .env

# 时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Issue #452: 内网/Docker 环境关闭公网 IP 探测
ENV GRAPE_AUTO_DETECT_URL=false

EXPOSE 3000

# 只启动 3000 主服务——main:app 已包含所有路由（含 webhook + A2A + runtime API）
# 不启动 8080——run_webhook:app 是冗余的，main:app 已注册 webhook_router（main.py 第 119 行）
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers", "--forwarded-allow-ips=*"]
