#: 驱动平台镜像（阶段 7 起用，阶段 9 收口瘦身）。
#:
#: 两件事同时做：
#:   1) 多阶段构建——builder 里装依赖，runtime 只搬虚拟环境；最终镜像里没有 pip/setuptools/
#:      构建期的源码副本，也没有 pip 缓存。
#:   2) 基础镜像从 ``python:3.14-slim``（debian）换成 ``python:3.14-alpine``（musl）：
#:      镜像从 245 MB 降到 150 MB 左右。代价是 musl 而不是 glibc——本项目只用纯 Python
#:      依赖（fastapi / uvicorn / httpx / prometheus-client），``uvicorn[standard]`` 的
#:      uvloop 与 httptools 在 musllinux 上有 cp314 轮子，实测可 import。
#:      PostgreSQL 插件（psycopg）与截图归一化（Pillow）都是可选依赖，不进镜像。
#:
#: 基础镜像用标签而不是钉摘要：它随上游打补丁更新是想要的行为；真正要求「同一份 compose
#: 起同一个镜像」的两个第三方服务（Prometheus / Grafana）在 docker-compose.yml 里钉了摘要。

FROM python:3.14-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 先只放构建元数据 + 说明文件（pyproject.toml 里 readme = "README.md"）。
COPY pyproject.toml README.md ./
COPY src ./src

# 装进独立 venv，装完顺手删掉 pip / setuptools / wheel 与 .pyc：
# 运行时不需要它们，而删在同一个 RUN 里才真的从这一层消失（拆成两层只是加白洞）。
# 装完立刻 import 一次：缺 wheel 时在这里就炸，而不是等容器起来才发现。
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install . \
    && /opt/venv/bin/python -c "import fastapi, uvicorn, httpx, prometheus_client" \
    && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && rm -rf /opt/venv/lib/python3.14/site-packages/pip \
              /opt/venv/lib/python3.14/site-packages/pip-*.dist-info \
              /opt/venv/lib/python3.14/site-packages/setuptools \
              /opt/venv/lib/python3.14/site-packages/setuptools-*.dist-info \
              /opt/venv/lib/python3.14/site-packages/wheel \
              /opt/venv/lib/python3.14/site-packages/wheel-*.dist-info \
              /opt/venv/lib/python3.14/site-packages/_virtualenv.py \
              /opt/venv/lib/python3.14/site-packages/_virtualenv.pth

FROM python:3.14-alpine AS runtime

# 构建戳：滚动验收与现网排查要能区分两次构建的镜像——同一份源码重建会命中构建缓存，
# 镜像 ID 一模一样，就看不出「新副本跑的是新镜像」。构建时传入即可：
#   docker compose build --build-arg DRIVER_BUILD_STAMP=<日期或 commit>
ARG DRIVER_BUILD_STAMP=dev
LABEL driver-platform.build.stamp=$DRIVER_BUILD_STAMP

# 不写 .pyc、不缓冲日志：容器里 stdout 就是日志通道，缓冲会让 docker logs 晚一拍。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv

WORKDIR /app

# 依赖与包体只搬 venv；部署资产单独一层（改种子脚本不会让依赖层失效）。
COPY --from=builder /opt/venv /opt/venv
COPY deploy ./deploy

# 阶段 8 加固：容器里的进程不跑 root。
#   - 建一个固定 uid=10001 的普通用户（固定 uid 才能让宿主上的卷权限可预测）；
#   - /data 是数据卷挂载点，先建好并交给它——卷首次初始化时继承这个属主；
#   - 源码与 deploy 只读，属主是 root：进程能读不能改，被攻破也改不了自己的代码。
RUN adduser -D -u 10001 -s /sbin/nologin -h /home/driver driver \
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
