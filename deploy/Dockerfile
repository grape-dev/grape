FROM python:3.12-slim

WORKDIR /app

# 镜像可复现：引擎与 codegraph 版本固定（opencode 1.18.x 与 1.19.x 的 provider schema 不同，
# 不 pin 会出现同一份代码在不同时间构建出不同引擎）。模板仓 ref 可用 --build-arg 固定到 tag。
ARG OPENCODE_VERSION=1.19.5
ARG CODEGRAPH_VERSION=1.6.2
ARG TEMPLATES_REF=main

# 系统依赖——gcc/g++ 编译 C 扩展（bcrypt/cffi 等）
# git/curl/ca-certificates：模板同步（scripts/sync_templates.sh 走 git clone）、
# 配置种子兜底下载、TLS 证书链
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ git curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Python 依赖（Issue #699 Phase 0: pyproject.toml replaces requirements.txt）
# 安装全部可选依赖（mysql + obs + im），生产部署需全部功能
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[all]"

# 执行引擎 opencode（宿主与托管都需要：缺它任务直接报 "no available engine"）。
# node 22 是引擎运行时，装完**不卸载**——历史坑：purge nodejs/npm 会把引擎依赖的 node 一起删掉。
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm i -g opencode-ai@${OPENCODE_VERSION} && \
    npm cache clean --force && \
    rm -rf /var/lib/apt/lists/* && \
    opencode --version

# Issue #666 (M3): 引擎基线镜像预装 codegraph（project-info 全量生成引擎）。
# 分发形式：npm 平台二进制包 @colbymchenry/codegraph（自包含：内置 node 运行时 + Rust 内核，
# bin/codegraph 可直接执行，见 scripts/install-codegraph.sh 注释）——非 pip 包。
# 安装位置：独立 prefix /opt/codegraph；bin 软链到 /usr/local/bin。
# best-effort：安装失败不阻断镜像构建，运行时走降级路径并显式告警（src/project_info/builder.py）。
# 与上游差异：不再 purge nodejs——引擎（opencode）依赖 node，purge 会连带破坏引擎。
RUN ( npm install -g --prefix /opt/codegraph @colbymchenry/codegraph@${CODEGRAPH_VERSION} \
      && ln -sf /opt/codegraph/bin/codegraph /usr/local/bin/codegraph \
      || echo "warning: codegraph install failed, project-info will run degraded with warning" ) \
 && ( codegraph --version \
      || ( echo "warning: codegraph unavailable, project-info will run degraded with warning"; rm -f /usr/local/bin/codegraph ) )

# 代码
COPY . .

# 前端构建产物：vite build → src/api/frontend/static-dist（vite base=/static-vue/）
# 仓库不提交产物（.gitignore 已忽略 static-dist），必须在构建期生成；否则 /static-vue/*
# 会被 SPA 兜底返回 index.html，表现为页面停在「加载中…」且无任何样式。node 22 已在上方安装。
RUN cd src/api/frontend/app \
    && (npm ci --no-audit --no-fund || npm install --no-audit --no-fund) \
    && npm run build

# 官方项目模板：configs/project-templates.json 与 examples/<模板>/ 都不入库，
# 必须构建期铺入，否则 UI 模板页为空。必须放在 COPY . . 之后：脚本在 scripts/ 下，
# 且不能被随后的一次 COPY 覆盖。可用 --build-arg TEMPLATES_REF=<tag> 固定模板版本。
# best-effort：模板仓不可达不阻断构建（运行时 entrypoint 会再试一次），
# 与 codegraph 的降级风格一致；如需硬保证，可在构建后检查 /app/configs/project-templates.json。
RUN TEMPLATES_REF=${TEMPLATES_REF} bash scripts/sync_templates.sh \
    || echo "warning: template sync failed at build time; the runtime entrypoint will retry"

# 默认 .env（Issue #452: SQLite + OBS 关闭，生产环境通过环境变量覆盖）
# 审查修复: COPY .env.example .env，确保容器内始终有 /app/.env，不再依赖宿主 bind mount
COPY .env.example .env

# 时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Issue #452: 内网/Docker 环境关闭公网 IP 探测
ENV GRAPE_AUTO_DETECT_URL=false

# 托管平台启动引导：补 JWT 密钥、对外基址、引擎模型配置。
# 默认惰性——只有检测到平台环境变量（RAILWAY_* / PUBLIC_DOMAIN / SERVER_BASE_URL / LLM_API_KEY）
# 或显式 GRAPE_HOSTED_BOOTSTRAP=1 才接管；否则原样 exec 下面的 CMD，自建行为不变。
COPY deploy/hosted/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

EXPOSE 3000

# 只启动 3000 主服务——main:app 已包含所有路由（含 webhook + A2A + runtime API）
# 不启动 8080——run_webhook:app 是冗余的，main:app 已注册 webhook_router（main.py 第 119 行）
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers", "--forwarded-allow-ips=*"]