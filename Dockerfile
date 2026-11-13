# 阶段 7：驱动平台镜像。
#
# 分层顺序按「变化频率从低到高」排：依赖（pyproject.toml）→ 源码（src/）→ 部署资产（deploy/）。
# 改一行源码不会重装依赖，改种子脚本不会重装依赖也不会重装源码。
#
# 基础镜像用官方 slim 标签而不是钉摘要：它随上游打补丁更新是想要的行为，
# 真正要求「同一份 compose 起同一个镜像」的两个第三方服务（Prometheus / Grafana）
# 在 docker-compose.yml 里钉了摘要。取舍写在 docs/stages/stage-7.md。

FROM python:3.14-slim

# 构建戳：滚动验收与现网排查要能区分两次构建的镜像——同一份源码重建会命中构建缓存，
# 镜像 ID 一模一样，就看不出「新副本跑的是新镜像」。构建时传入即可：
#   docker compose build --build-arg DRIVER_BUILD_STAMP=<日期或 commit>
ARG DRIVER_BUILD_STAMP=dev
LABEL driver-platform.build.stamp=$DRIVER_BUILD_STAMP

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

# 阶段 8 加固：容器里的进程不跑 root。
#   - 建一个固定 uid=10001 的普通用户（固定 uid 才能让宿主上的卷权限可预测）；
#   - /data 是数据卷挂载点，先建好并交给它——卷首次初始化时继承这个属主；
#   - 源码与 deploy 只读，属主是 root：进程能读不能改，被攻破也改不了自己的代码。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin driver \
    && mkdir -p /data \
    && chown -R driver:driver /data \
    && chmod -R a-w /app

USER driver

# 数据目录：compose 里是命名卷；单跑 docker run 时声明成卷，
# 这样 `--read-only` 的根文件系统不会连数据库一起冻住。
VOLUME ["/data"]
ENV DRIVER_SQLITE_PATH=/data/demo.db

EXPOSE 8000

# 镜像级健康检查：docker run 直接起时也能看出是否可用；compose 里另有更短周期的同名检查。
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

ENTRYPOINT ["python", "/app/deploy/docker/entrypoint.py"]
# 阶段 8：--timeout-graceful-shutdown 20 让 uvicorn 收到 SIGTERM 后先停收新连接、等在途请求收尾
# （最长 20 秒）再退出；退出阶段 FastAPI 的 lifespan 会卸载插件（关掉 SQLite 连接）。
# 没有这个窗口，滚动替换期间的最后一个在途请求会被直接掐断。
CMD ["uvicorn", "driver_platform.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
