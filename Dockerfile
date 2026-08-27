FROM python:3.11-slim

WORKDIR /app

ENV TZ=Asia/Shanghai \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 安装基础依赖
# ca-certificates: httpx 走 HTTPS 调 ip-api.com / Cloudflare / Telegram 时校验证书用
# curl: 应用本身不用，留着方便进容器 curl 接口排查问题
# （iputils-ping 已移除：三网延迟测速功能删掉后，容器内不再有 ping 调用，
#   一键部署和 X-UI 管理里的 ping/curl 都是在目标 VPS 上通过 SSH 执行的）
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 设置时区
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 升级 pip 并安装依赖
COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

EXPOSE 8080

CMD ["python", "app/main.py"]
