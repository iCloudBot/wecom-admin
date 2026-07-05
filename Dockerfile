FROM python:3.11-alpine AS builder

WORKDIR /build

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.11-alpine

LABEL maintainer="wecom-admin"
LABEL description="企业微信消息推送后台管理系统"

# 时区设置为 Asia/Shanghai
ENV TZ=Asia/Shanghai \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_ENV=production

RUN apk upgrade --no-cache && \
    apk add libcap tzdata git openssh --no-cache && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone

COPY --from=builder /install /usr/local

WORKDIR /wecom-admin

# 复制项目源码
COPY . .

RUN echo '#!/bin/sh' > /entrypoint && \
    echo -e 'set -e\n' >> /entrypoint && \
    echo 'python run.py' >> /entrypoint && \
    chmod +x /entrypoint

# 暴露端口
EXPOSE 5000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/auth/me')" || exit 1

# 启动命令
CMD ["/entrypoint"]
