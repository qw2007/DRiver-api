# 阶段 7：驱动平台镜像。
#
# 分层顺序按「变化频率从低到高」排：依赖（pyproject.toml）→ 源码（src/）→ 部署资产（deploy/）。
# 改一行源码不会重装依赖，改种子脚本不会重装依赖也不会重装源码。
#
# 基础镜像用官方 slim 标签而不是钉摘要：它随上游打补丁更新是想要的行为，
# 真正要求「同一份 compose 起同一个镜像」的两个第三方服务（Prometheus / Grafana）
# 在 docker-compose.yml 里钉了摘要。取舍写在 docs/stages/stage-7.md。

FROM python:3.14-slim

# 不写 .pyc、不缓冲日志：容器里 stdout 就是日志通道，缓冲会让 docker logs 晚一拍。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先只放构建元数据 + 说明文件（pyproject.toml 里 readme = "README.md"），
# 这一层只在依赖变化时失效。
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# 部署资产：容器入口脚本 + 演示库种子（见 deploy/docker/entrypoint.py）。
COPY deploy ./deploy

EXPOSE 8000

# 镜像级健康检查：docker run 直接起时也能看出是否可用；compose 里另有更短周期的同名检查。
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

ENTRYPOINT ["python", "/app/deploy/docker/entrypoint.py"]
CMD ["uvicorn", "driver_platform.main:app", "--host", "0.0.0.0", "--port", "8000"]
