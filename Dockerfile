# 多阶段构建 Dockerfile for AI Story Creator
# 支持多架构构建: linux/amd64, linux/arm64

# 构建参数
ARG USE_CN_MIRROR=false
ARG EMBEDDING_MODEL_REVISION=60750e200f336606cdd1ecbda9bb33fbf4d5b2a1

# 阶段1: 构建前端
FROM node:22-alpine AS frontend-builder

ARG USE_CN_MIRROR

WORKDIR /frontend

# 复制前端依赖文件
COPY frontend/package*.json ./

# 根据参数决定是否使用国内npm镜像
RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
        npm config set registry https://registry.npmmirror.com; \
    fi

# 删除 package-lock.json 以避免因镜像源不一致导致的 404 错误
RUN rm -f package-lock.json

# 安装依赖
RUN npm install

# 复制前端源代码
COPY frontend/ ./

# 临时修改vite配置，使其输出到dist目录（而不是../backend/static）
RUN sed -i "s|outDir: '../backend/static'|outDir: 'dist'|g" vite.config.ts

# 构建前端
RUN npm run build

# 阶段2: 构建最终镜像
FROM python:3.12-slim

ARG USE_CN_MIRROR
ARG TARGETPLATFORM
ARG TARGETARCH
ARG EMBEDDING_MODEL_REVISION

# 设置工作目录
WORKDIR /app

# 根据参数决定是否使用国内镜像源
RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
        sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
        sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources; \
    fi

# 安装系统依赖（添加数据库工具）
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    postgresql-client \
    netcat-traditional \
    && rm -rf /var/lib/apt/lists/*

# 复制后端依赖文件
COPY backend/requirements.txt ./

# 安装不包含 PyTorch/Transformers 的 ONNX 运行时依赖
RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
        pip install --no-cache-dir -r requirements.txt -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

# 直接从 ModelScope 下载已转换的 ONNX 部署文件，并校验发布清单。
ENV ONNX_EMBEDDING_MODEL_DIR=/app/embedding/onnx/paraphrase-multilingual-MiniLM-L12-v2
RUN set -eu; \
    model_url="https://modelscope.cn/models/mumujie/paraphrase-multilingual-MiniLM-L12-v2-ONNX/resolve/${EMBEDDING_MODEL_REVISION}"; \
    mkdir -p "$ONNX_EMBEDDING_MODEL_DIR"; \
    curl --fail --location --retry 5 --retry-all-errors "$model_url/model.onnx" -o "$ONNX_EMBEDDING_MODEL_DIR/model.onnx"; \
    curl --fail --location --retry 5 --retry-all-errors "$model_url/tokenizer.json" -o "$ONNX_EMBEDDING_MODEL_DIR/tokenizer.json"; \
    curl --fail --location --retry 5 --retry-all-errors "$model_url/embedding_config.json" -o "$ONNX_EMBEDDING_MODEL_DIR/embedding_config.json"; \
    echo "e7515ed8b2f63e84f99dfed652b572e61a9a799f694a1c9399a7f3845b69cda5  $ONNX_EMBEDDING_MODEL_DIR/model.onnx" | sha256sum -c -; \
    echo "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8  $ONNX_EMBEDDING_MODEL_DIR/tokenizer.json" | sha256sum -c -; \
    echo "d9cfbb22ea59e66294db9bd5b35b452326658a2fe1580e409f0c806be01973c2  $ONNX_EMBEDDING_MODEL_DIR/embedding_config.json" | sha256sum -c -

# 复制后端代码（不包含embedding，因为已经下载了）
COPY backend/ ./

# 从前端构建阶段复制构建好的静态文件
COPY --from=frontend-builder /frontend/dist ./static

# 复制 Alembic 迁移配置和脚本（PostgreSQL）
COPY backend/alembic-postgres.ini ./alembic.ini
COPY backend/alembic/postgres ./alembic
COPY backend/scripts/entrypoint.sh /app/entrypoint.sh
COPY backend/scripts/migrate.py ./scripts/migrate.py

# 赋予执行权限
RUN chmod +x /app/entrypoint.sh

# 创建必要的目录
RUN mkdir -p /app/data /app/logs

# 暴露端口
EXPOSE 8000

# 设置环境变量
ENV PYTHONUNBUFFERED=1
ENV APP_HOST=0.0.0.0
ENV APP_PORT=8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# 使用 entrypoint 脚本启动（自动执行迁移）
ENTRYPOINT ["/app/entrypoint.sh"]
